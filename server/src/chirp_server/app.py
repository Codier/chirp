"""Frozen v1 HTTP/WS protocol. Logs contain metadata and safe error codes only."""

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from uuid import uuid4

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import notes
from .audio import MAX_FRAME, MAX_PCM, AudioError, decode_wav, levels, samples, silent
from .cleanup_models import DEFAULT_MODEL, model_spec
from .config import Settings, load_token, persist_keep_loaded, read_keep_loaded
from .correction import CorrectionResult, Corrector
from .engine import EngineWorker
from .managed_cleanup import ManagedCleanup
from .profiles import MAX_PROMPT_CHARS
from .retention import Retention

log = logging.getLogger("chirp")
MESSAGES = {
    "auth": "Authentication required",
    "busy": "Speech engine busy; retry shortly",
    "too_long": "Maximum utterance is 120 seconds",
    "bad_request": "Invalid correction prompt",
    "bad_audio": "Invalid audio or control frame",
    "engine": "Speech engine unavailable",
    "unsupported": "keep_loaded requires managed correction mode",
    "bad_notes": "Invalid notes request",
    "notes_off": "Notes structuring needs correction enabled on the server",
}


def error(code):
    return {"type": "error", "code": code, "message": MESSAGES[code]}


def authorized(token, value):
    # Comparing UTF-8 bytes also handles malicious non-ASCII credentials safely.
    return isinstance(value, str) and secrets.compare_digest(token.encode(), value.encode())


def bearer(headers):
    value = headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    return token if scheme.lower() == "bearer" else ""


class Utterance:
    def __init__(self, ws, worker, settings, corrector, retention):
        self.ws, self.worker, self.settings = ws, worker, settings
        self.corrector, self.retention = corrector, retention
        self.session = uuid4().hex
        self.pcm = bytearray()
        self.started = time.perf_counter()
        self.first_partial = None
        self.stream = None
        self.task = None
        self.changed = asyncio.Event()
        self.stopping = False
        self.offset = 0
        self.seq = 0
        self.correction = False
        self.profile = "default"
        self.instructions = ""
        self.correction_prompt = None
        self.leased = False
        self.failure = False
        self.aborted = False
        self.cancelled = False
        self.correcting = None
        self.peak = self.rms = 0.0
        # Opt-in `start.word_timings`: partials, correcting and the final carry `words`.
        self.word_timings = False

    async def start(self, control):
        if "correction_prompt" in control:
            prompt = control["correction_prompt"]
            if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
                raise AudioError("bad_request")
            self.correction_prompt = prompt
        profile = control.get("correction_profile", "default")
        instructions = control.get("correction_instructions", "")
        if (
            not isinstance(profile, str)
            or self.settings.correction.profile(profile) is None
            or not isinstance(instructions, str)
            or len(instructions) > 2000
        ):
            raise AudioError()
        if (
            control.get("sample_rate") != 16000
            or control.get("encoding") != "pcm_s16le"
            or control.get("client") not in {"mac", "android", "ios"}
            or not isinstance(control.get("correction", False), bool)
            or not isinstance(control.get("word_timings", False), bool)
        ):
            raise AudioError()
        self.word_timings = control.get("word_timings", False)
        helper = getattr(self.corrector, "managed", None)
        managed = helper if isinstance(helper, ManagedCleanup) else None
        if control.get("correction", False) and managed is not None:
            # Managed cleanup: begin the cold load while the user is still speaking.
            # This never delays the ack, partials or ASR, and never counts against
            # an utterance's correction timeout; only a final may wait for the load.
            managed.prewarm()
        await asyncio.wait_for(self.worker.lease.acquire(), self.settings.queue_timeout)
        self.leased = True
        self.correction = control.get("correction", False)
        self.profile = profile
        self.instructions = instructions if profile == "custom" else ""
        self.stream = await self.worker.call(self.worker.engine.open_stream)
        await self.ws.send_json(
            {
                "type": "ready",
                "session": self.session,
                "engine": self.worker.engine.name,
                "debug_retention": self.settings.debug_retain,
            }
        )
        self.task = asyncio.create_task(self.partials())

    def add(self, chunk):
        if len(chunk) >= MAX_FRAME or not chunk or len(chunk) % 2:
            raise AudioError()
        if len(self.pcm) + len(chunk) > MAX_PCM:
            raise AudioError("too_long")
        self.pcm.extend(chunk)
        if len(self.pcm) - self.offset >= 6400:  # 200 ms of new PCM16
            self.changed.set()

    async def partials(self):
        previous = previous_stable = ""
        try:
            while True:
                await self.changed.wait()
                self.changed.clear()
                if self.aborted or self.stopping:
                    return
                end = min(len(self.pcm), self.offset + 64000)
                chunk = samples(bytes(self.pcm[self.offset : end]))
                self.offset = end
                if chunk.size:
                    peak, rms = levels(chunk)
                    self.peak, self.rms = max(self.peak, peak), max(self.rms, rms)
                    # Still feed silence to preserve the model's time/context alignment.
                    partial = await self.worker.call(self.stream.feed, chunk)
                    stable = partial.stable if partial else ""
                    if self.correction and self.settings.correction.enabled:
                        stable = ""
                    if (
                        not self.aborted
                        and not self.stopping
                        and partial
                        and partial.text
                        and (partial.text != previous or stable != previous_stable)
                        and (not silent(chunk) or stable)
                    ):
                        previous, previous_stable = partial.text, stable
                        self.seq += 1
                        elapsed = (time.perf_counter() - self.started) * 1000
                        if self.first_partial is None:
                            self.first_partial = elapsed
                        message = {
                            "type": "partial",
                            "seq": self.seq,
                            "text": partial.text,
                            "stable": stable,
                        }
                        self.add_words(message, partial.words)
                        await self.ws.send_json(message)
                if self.stopping and self.offset == len(self.pcm):
                    return
                if len(self.pcm) - self.offset >= 6400 or (
                    self.stopping and self.offset < len(self.pcm)
                ):
                    self.changed.set()
        except (WebSocketDisconnect, RuntimeError):
            self.failure = True
        except Exception:
            # Never include library exception strings: they may contain user data.
            self.failure = True
            log.warning("code=engine session=%s", self.session)

    def add_words(self, message, words):
        """Attach `words` when the client asked for timings and the engine produced them.
        Without the flag every message is byte-identical to the pre-timings protocol."""
        if self.word_timings and words is not None:
            message["words"] = [word.payload() for word in words]

    def cancel(self):
        """Mark cancelled from the receive loop; also cancels a pending correction request."""
        self.cancelled = True
        if self.correcting is not None:
            self.correcting.cancel()

    def discard(self):
        # Cancelled during finalization: no correction, no retention, no final.
        log.info("session=%s bytes=%d cancelled_during_finalize=1", self.session, len(self.pcm))
        return None

    async def finish(self):
        started = time.perf_counter()
        self.stopping = True
        self.changed.set()
        await self.task
        if self.failure:
            raise RuntimeError("engine")
        if self.cancelled:
            return self.discard()
        if not self.pcm:
            # A tap released before the first audio frame: an empty final, not a protocol error.
            await self.worker.call(self.stream.cancel)
            self.stream = None
            self.release()
            return await make_final(
                "",
                b"",
                self.first_partial,
                started,
                self.corrector,
                False,
                self.profile,
                self.instructions,
                self.correction_prompt,
                words=() if self.word_timings else None,
            )
        if self.offset < len(self.pcm):
            pending = samples(bytes(self.pcm[self.offset :]))
            buffer = getattr(self.stream, "buffer", self.stream.feed)
            await self.worker.call(buffer, pending)
            if self.cancelled:
                return self.discard()
            self.offset = len(self.pcm)
        transcript = await self.worker.call(self.stream.finish)
        raw, words = transcript.text, transcript.words if self.word_timings else None
        self.stream = None
        self.release()
        if self.cancelled:
            return self.discard()
        enabled = self.correction and self.settings.correction.enabled and bool(raw)
        if enabled:
            self.seq += 1
            message = {"type": "correcting", "seq": self.seq, "raw": raw, "profile": self.profile}
            self.add_words(message, words)
            await self.ws.send_json(message)
        if self.cancelled:
            return self.discard()
        # A cancel racing this task cancels the pending correction request with it.
        self.correcting = asyncio.create_task(
            make_final(
                raw,
                bytes(self.pcm),
                self.first_partial,
                started,
                self.corrector,
                enabled,
                self.profile,
                self.instructions,
                self.correction_prompt,
                words=words,
            )
        )
        try:
            final = await self.correcting
        except asyncio.CancelledError:
            return self.discard()
        finally:
            self.correcting = None
        if self.cancelled:
            return self.discard()
        self.retention.save(self.session, bytes(self.pcm), final)
        log.info(
            "session=%s bytes=%d peak=%.5f rms=%.5f finalize_ms=%.1f identity_present=%s",
            self.session,
            len(self.pcm),
            self.peak,
            self.rms,
            final["timings_ms"]["finalize"],
            "tailscale-user-login" in self.ws.headers,
        )
        return final

    def release(self):
        if self.leased:
            self.worker.lease.release()
            self.leased = False

    async def close(self):
        # Let an in-flight feed finish before disposing its thread-owned context.
        self.aborted = True
        self.stopping = True
        self.changed.set()
        if self.task:
            with contextlib.suppress(Exception):
                await self.task
        try:
            if self.stream:
                await self.worker.call(self.stream.cancel)
        finally:
            self.stream = None
            self.pcm.clear()
            self.release()


async def make_final(
    raw,
    pcm,
    first_partial,
    started,
    corrector,
    enabled,
    profile="default",
    instructions="",
    correction_prompt=None,
    words=None,
):
    finalized = (time.perf_counter() - started) * 1000
    correction_started = time.perf_counter()
    if enabled:
        args = (raw, profile, instructions)
        result = (
            await corrector.process(*args, correction_prompt)
            if correction_prompt is not None
            else await corrector.process(*args)
        )
    else:
        result = CorrectionResult(raw)
    final = {
        "type": "final",
        "text": result.text,
        "raw": raw,
        "corrected": result.corrected,
        "correction_error": result.error,
        "timings_ms": {
            "audio": len(pcm) / 32,
            "first_partial": first_partial,
            "finalize": finalized,
            "correction": (time.perf_counter() - correction_started) * 1000 if enabled else 0.0,
        },
    }
    if words is not None:
        # Timings describe `raw`; cleaned text cannot map onto the audio word for word.
        final["words"] = [word.payload() for word in words]
    return final


def create_app(settings=None, engine=None, token=None, corrector=None):
    settings = settings or Settings.read()
    if engine is None:
        if settings.engine == "onnx":
            from .engine.onnx import ParakeetONNX

            engine = ParakeetONNX(
                settings.model_dir,
                settings.finalization,
                intra_op_num_threads=settings.onnx.intra_op_num_threads,
                inter_op_num_threads=settings.onnx.inter_op_num_threads,
            )
        else:
            from .engine.mlx import ParakeetMLX

            engine = ParakeetMLX(
                settings.model_dir, settings.precision, settings.streaming, settings.finalization
            )
    if settings.speech_model != "parakeet-v2" and settings.engine == "mlx":
        engine.name = f"parakeet-mlx:{settings.speech_model}"
    worker = EngineWorker(engine)
    retention = Retention(settings)
    corrector = corrector or Corrector(settings.correction)
    helper = getattr(corrector, "managed", None)
    managed = helper if isinstance(helper, ManagedCleanup) else None
    config = getattr(corrector, "config", settings.correction)
    # A client-persisted override wins over the toml value; server.toml is never rewritten.
    override = read_keep_loaded(settings.state_path)
    keep = {"value": bool(override if override is not None else config.keep_loaded)}
    if managed is not None:
        managed.keep_loaded = keep["value"]
    credential = token
    pending_auth = 0
    structuring = asyncio.Lock()  # at most one structuring pass server-wide

    async def housekeeping():
        while True:
            await asyncio.sleep(3600)
            retention.purge()

    @asynccontextmanager
    async def lifespan(app):
        nonlocal credential
        credential = credential or load_token(settings.config_dir)
        cleanup = None
        try:
            if settings.debug_retain:
                log.warning("DEBUG: recording kept — debug retention is ENABLED")
                retention.purge()
                cleanup = asyncio.create_task(housekeeping())
            await worker.start()
            if managed is not None and keep["value"]:
                # keep_loaded: prewarm during server bring-up; the background load
                # never blocks readiness and never idle-unloads while enabled.
                managed.prewarm()
            yield
        finally:
            if cleanup:
                cleanup.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cleanup
            await worker.close()
            if isinstance(corrector, Corrector):
                await corrector.close()

    async def health(request: Request):
        if not authorized(credential, bearer(request.headers)):
            # Frozen protocol intentionally exposes only liveness without credentials.
            return JSONResponse({"ok": True})
        correction = settings.correction
        return JSONResponse(
            {
                "ok": True,
                "engine": engine.name,
                "speech_model": settings.speech_model,
                "correction_model": correction.model or DEFAULT_MODEL,
                "ready": worker.ready,
                "debug_retention": settings.debug_retain,
                "correction": "available" if correction.enabled else "off",
                "correction_mode": correction.mode,
                "correction_leaves_machine": correction.enabled and correction.leaves_machine,
                "correction_host": correction.host,
                "correction_profiles": [
                    profile.metadata() for profile in correction.resolved_profiles.values()
                ],
                "correction_keep_loaded": keep["value"],
                "correction_loaded": bool(managed and managed.loaded),
                "notes_structure": True,
                # The hidden `live_notes` style for Live Notes segments exists.
                "notes_correction": True,
                "notes_reorganize": True,
            }
        )

    async def profiles(request: Request):
        if not authorized(credential, bearer(request.headers)):
            return JSONResponse(
                error("auth"), status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
        return JSONResponse(
            {
                "profiles": [
                    {
                        **profile.metadata(),
                        "prompt": profile.prompt,
                        "max_prompt_chars": MAX_PROMPT_CHARS,
                    }
                    for profile in settings.correction.resolved_profiles.values()
                ]
            }
        )

    def correction_settings_payload():
        if managed is None:
            return {
                "keep_loaded": keep["value"],
                "loaded": False,
                "loading": False,
                "model": None,
                "memory_mb": None,
                "memory_estimated": False,
                "mode": config.mode,
            }
        try:
            estimate = model_spec(config.model).memory_mb
        except ValueError:
            estimate = None
        memory, estimated = managed.memory(estimate)
        return {
            "keep_loaded": keep["value"],
            "loaded": managed.loaded,
            "loading": managed.loading,
            "model": config.model or DEFAULT_MODEL,
            "memory_mb": memory,
            "memory_estimated": estimated,
            "mode": config.mode,
        }

    async def correction_settings(request: Request):
        if not authorized(credential, bearer(request.headers)):
            return JSONResponse(
                error("auth"), status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
        if request.method == "GET":
            return JSONResponse(correction_settings_payload())
        try:
            body = json.loads(await request.body())
        except ValueError:
            body = None
        if (
            not isinstance(body, dict)
            or set(body) != {"keep_loaded"}
            or not isinstance(body["keep_loaded"], bool)
        ):
            return JSONResponse(error("bad_request"), status_code=400)
        if managed is None:
            return JSONResponse(error("unsupported"), status_code=409)
        try:
            persist_keep_loaded(settings.state_path, body["keep_loaded"])
        except OSError:
            return JSONResponse(error("engine"), status_code=500)
        keep["value"] = body["keep_loaded"]
        await managed.set_keep_loaded(body["keep_loaded"])
        if body["keep_loaded"]:
            # Load immediately in the background; while on, it never idle-unloads.
            managed.prewarm()
        return JSONResponse(correction_settings_payload())

    async def notes_pass(request: Request, validate, run, respond):
        """Shared by the notes endpoints: stateless, no engine lease, no retention, and one
        notes generation at a time server-wide (`structuring`)."""
        if not authorized(credential, bearer(request.headers)):
            return JSONResponse(
                error("auth"), status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
        if not config.enabled:
            return JSONResponse(error("notes_off"), status_code=409)
        try:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > notes.MAX_BODY_BYTES:
                    raise ValueError("Request too large")
                body.extend(chunk)
            fields = validate(json.loads(bytes(body)))
        except ValueError:  # includes malformed JSON and invalid UTF-8
            return JSONResponse(error("bad_notes"), status_code=400)
        if structuring.locked():
            return JSONResponse(error("busy"), status_code=503)
        async with structuring:
            started = time.perf_counter()
            if managed is not None:
                managed.prewarm()
            result = await run(*fields[:2])
            elapsed = (time.perf_counter() - started) * 1000
        return respond(fields, result, elapsed)

    async def notes_structure(request: Request):
        """Lengths-only logging; the transcript and Markdown never reach the log."""

        def respond(fields, result, elapsed):
            transcript, outline, _final = fields
            log.info(
                "notes_structure chars=%d outline=%d structured=%s error=%s reason=%s ms=%.1f",
                len(transcript),
                len(outline),
                result.structured,
                result.error,
                result.reason,
                elapsed,
            )
            body = {
                "markdown": result.markdown,
                "structured": result.structured,
                "error": result.error,
                "timings_ms": {"structure": elapsed},
            }
            return JSONResponse(body if result.structured else body | {"reason": result.reason})

        return await notes_pass(
            request,
            notes.validate_request,
            lambda transcript, outline: notes.structure(corrector, transcript, outline),
            respond,
        )

    async def notes_reorganize(request: Request):
        """Lengths-only logging; the notes and Markdown never reach the log."""

        def respond(fields, result, elapsed):
            text, outline, _final = fields
            log.info(
                "notes_reorganize chars=%d outline=%d reorganized=%s changed=%s error=%s reason=%s "
                "ms=%.1f",
                len(text),
                len(outline),
                result.reorganized,
                result.changed,
                result.error,
                result.reason,
                elapsed,
            )
            body = {
                "markdown": result.markdown,
                "reorganized": result.reorganized,
                "changed": result.changed,
                "error": result.error,
                "timings_ms": {"reorganize": elapsed},
            }
            return JSONResponse(body if result.reorganized else body | {"reason": result.reason})

        return await notes_pass(
            request,
            notes.validate_reorganize_request,
            lambda text, outline: notes.reorganize(corrector, text, outline),
            respond,
        )

    async def transcribe(request: Request):
        if not authorized(credential, bearer(request.headers)):
            return JSONResponse(
                error("auth"), status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
        try:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_PCM + 65536:
                    raise AudioError("too_long")
                body.extend(chunk)
            pcm = decode_wav(bytes(body))
            await asyncio.wait_for(worker.lease.acquire(), settings.queue_timeout)
            try:
                started = time.perf_counter()
                raw = await worker.call(engine.transcribe, samples(pcm))
            finally:
                worker.lease.release()
            final = await make_final(raw, pcm, None, started, corrector, False)
            retention.save(uuid4().hex, pcm, final)
            log.info(
                "bytes=%d identity_present=%s", len(pcm), "tailscale-user-login" in request.headers
            )
            return JSONResponse(final)
        except AudioError as exc:
            return JSONResponse(error(exc.code), status_code=413 if exc.code == "too_long" else 400)
        except TimeoutError:
            return JSONResponse(error("busy"), status_code=503)
        except Exception:
            log.warning("code=engine")
            return JSONResponse(error("engine"), status_code=503)

    async def receive(ws, deadline=None):
        message = await asyncio.wait_for(
            ws.receive(), settings.idle_seconds if deadline is None else deadline
        )
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        value = message.get("text")
        if value is not None:
            if len(value.encode()) >= MAX_FRAME:
                raise AudioError()
            try:
                control = json.loads(value)
            except (ValueError, TypeError) as exc:
                raise AudioError() from exc
            if not isinstance(control, dict):
                raise AudioError()
            return control
        return message.get("bytes", b"")

    async def finalize(ws, utterance):
        """Await finish() while still observing the socket (R2-01).

        Cancel or disconnect during finalization marks the utterance cancelled, which
        suppresses correction, retention and final delivery; native inference still
        drains on its owning thread. Frames racing with finalization are returned for
        normal dispatch afterwards.
        """
        finisher = asyncio.create_task(utterance.finish())
        raced = []
        while not finisher.done():
            reader = asyncio.create_task(receive(ws))
            done, _ = await asyncio.wait({finisher, reader}, return_when=asyncio.FIRST_COMPLETED)
            if finisher in done:
                # A completed reader's frame must not be lost; keep it for re-dispatch.
                if reader.done() and not reader.cancelled():
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        raced.append(reader.result())
                reader.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await reader
                break
            try:
                message = reader.result()
            except WebSocketDisconnect:
                utterance.cancel()
                await finisher
                break
            except TimeoutError:
                continue  # The idle deadline restarts; keep waiting for finalization.
            except AudioError:
                utterance.cancel()
                await finisher
                raise
            if isinstance(message, bytes):
                raced.append(message)
            elif message.get("type") == "cancel":
                utterance.cancel()
            else:
                raced.append(message)
        return finisher.result(), raced

    async def stream(ws: WebSocket):
        nonlocal pending_auth
        utterance = None
        await ws.accept()
        try:
            valid = authorized(credential, bearer(ws.headers))
            # An explicitly supplied wrong header cannot switch authentication paths.
            if not valid and "authorization" not in ws.headers:
                if pending_auth >= settings.max_pending_auth:
                    await ws.send_json(error("auth"))
                    await ws.close(code=4401)
                    return
                pending_auth += 1
                try:
                    auth = await receive(ws, settings.auth_seconds)
                    valid = (
                        isinstance(auth, dict)
                        and auth.get("type") == "auth"
                        and authorized(credential, auth.get("token"))
                    )
                except (AudioError, TimeoutError):
                    valid = False
                finally:
                    pending_auth -= 1
            if not valid:
                await ws.send_json(error("auth"))
                await ws.close(code=4401)
                return
            carry = deque()
            while True:
                message = carry.popleft() if carry else await receive(ws)
                if isinstance(message, bytes):
                    if utterance is None:
                        raise AudioError()
                    utterance.add(message)
                    continue
                kind = message.get("type")
                if kind == "start" and utterance is None:
                    utterance = Utterance(ws, worker, settings, corrector, retention)
                    await utterance.start(message)
                elif kind == "cancel" and utterance:
                    await utterance.close()
                    utterance = None
                elif kind == "stop" and utterance:
                    final, raced = await finalize(ws, utterance)
                    # A cancelled utterance sends nothing: no final, no error; the
                    # socket stays usable for the next start.
                    if final is not None:
                        await ws.send_json(final)
                    await utterance.close()
                    utterance = None
                    carry.extend(raced)
                else:
                    raise AudioError()
        except WebSocketDisconnect:
            pass
        except TimeoutError:
            if utterance is not None:
                await ws.send_json(error("busy"))
            await ws.close(code=1000)
        except AudioError as exc:
            await ws.send_json(error(exc.code))
            await ws.close(code=1009 if exc.code == "too_long" else 1003)
        except Exception:
            log.warning("code=engine")
            with contextlib.suppress(Exception):
                await ws.send_json(error("engine"))
                await ws.close(code=1011)
        finally:
            if utterance:
                await utterance.close()

    app = Starlette(
        routes=[
            Route("/v1/health", health),
            Route("/v1/correction/profiles", profiles),
            Route("/v1/correction/settings", correction_settings, methods=["GET", "PUT"]),
            Route("/v1/transcribe", transcribe, methods=["POST"]),
            Route("/v1/notes/structure", notes_structure, methods=["POST"]),
            Route("/v1/notes/reorganize", notes_reorganize, methods=["POST"]),
            WebSocketRoute("/v1/stream", stream),
        ],
        lifespan=lifespan,
    )
    app.state.worker = worker
    return app


def accept_prefix(app, prefix):
    """Serve the same routes with or without `prefix`, whichever form the proxy forwards.

    `tailscale serve --set-path /chirp` may or may not strip its mount point; both work.
    """
    prefix = "/" + prefix.strip("/") if prefix and prefix.strip("/") else ""
    if not prefix:
        return app

    async def wrapped(scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] in ("http", "websocket") and (
            path == prefix or path.startswith(prefix + "/")
        ):
            scope = dict(scope, path=path[len(prefix) :] or "/")
            if "raw_path" in scope and scope["raw_path"] is not None:
                scope["raw_path"] = scope["raw_path"][len(prefix) :] or b"/"
        await app(scope, receive, send)

    return wrapped
