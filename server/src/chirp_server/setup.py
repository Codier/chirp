"""Explicit local setup for the Mac app. Status is offline; downloads need a user action."""

import json
import os
import subprocess
import time
from pathlib import Path

from . import cleanup_models, model_catalog
from .config import Settings
from .engine import models as speech_models


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def status(settings):
    result = []
    for kind, title, discover in (
        (
            "speech",
            "Parakeet · Speech recognition",
            lambda: (
                speech_models.discover(settings.model_dir)
                if settings.model_dir
                else speech_models.discover()
            ),
        ),
        (
            "cleanup",
            f"Qwen · {settings.correction.model or cleanup_models.DEFAULT_MODEL}",
            lambda: cleanup_models.discover(settings.correction.model),
        ),
    ):
        try:
            discover()
            ready = True
        except (OSError, ValueError):
            ready = False
        result.append({"id": kind, "title": title, "ready": ready})
    return result


def configure_downloads():
    # HTTP gives resumable byte progress, including behind inspecting proxies.
    # Determinate exports the Mac's trusted Keychain roots for Python/OpenSSL.
    # Honor an explicit CA override; never disable certificate verification.
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    # Reading a selected model can import the Hub before download configuration.
    # Its constants are initialized once, so update the already-loaded setting too.
    from huggingface_hub import constants

    constants.HF_HUB_DISABLE_XET = True
    keychain = Path("/etc/nix/macos-keychain.crt")
    if "SSL_CERT_FILE" not in os.environ and keychain.is_file():
        os.environ["SSL_CERT_FILE"] = str(keychain)


def download(kind, settings, choice=None):
    configure_downloads()
    from huggingface_hub import hf_hub_download, snapshot_download
    from huggingface_hub.utils import tqdm

    if choice:
        option = model_catalog.resolve(choice, kind)
        repository, revision = option.repository, option.revision
        patterns = speech_models.FILES if kind == "speech" else cleanup_models.PATTERNS
        local = speech_models.model_root() / "parakeet-mlx" if option.id == "parakeet-v2" else None

        def discover():
            return model_catalog.directory(option)
    elif kind == "speech":
        repository, revision = speech_models.REPOSITORY, speech_models.REVISION
        patterns = speech_models.FILES
        local = speech_models.model_root() / "parakeet-mlx"
        discover = speech_models.discover
    else:
        model = cleanup_models.model_spec(settings.correction.model)
        repository, revision = model.repository, model.revision
        patterns, local = cleanup_models.PATTERNS, None

        def discover():
            return cleanup_models.discover(settings.correction.model)

    emit("progress", model=kind, phase="preparing", completed_bytes=0, total_bytes=0)
    try:
        discover()
        emit("complete", model=kind)
        return
    except (OSError, ValueError):
        pass

    plan = snapshot_download(
        repository, revision=revision, allow_patterns=patterns, local_dir=local, dry_run=True
    )
    total = sum(item.file_size for item in plan)
    completed = sum(item.file_size for item in plan if not item.will_download)
    last = 0.0

    def progress(current, *, force=False):
        nonlocal last
        now = time.monotonic()
        if force or now - last >= 0.15:
            emit(
                "progress",
                model=kind,
                phase="downloading",
                completed_bytes=min(total, max(0, current)),
                total_bytes=total,
            )
            last = now

    class ByteProgress(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)
            progress(completed + self.n, force=True)

        def update(self, n=1):
            self.n += n or 0
            progress(completed + self.n)

    progress(completed, force=True)
    for item in plan:
        if not item.will_download:
            continue
        hf_hub_download(
            repository, item.filename, revision=revision, local_dir=local, tqdm_class=ByteProgress
        )
        completed += item.file_size
        progress(completed, force=True)
    discover()  # Only a complete usable snapshot gets a green checkmark.
    emit("complete", model=kind)


def server_running(port):
    import httpx

    try:
        response = httpx.get(f"http://127.0.0.1:{port}/v1/health", timeout=1, trust_env=False)
        return response.status_code == 200 and response.json().get("ok") is True
    except (httpx.HTTPError, ValueError):
        return False


def server_matches(port, wanted):
    if not any(kind in wanted for kind in ("speech", "cleanup")):
        return True
    import httpx

    try:
        token = (model_catalog.root() / "token").read_text().strip()
        response = httpx.get(
            f"http://127.0.0.1:{port}/v1/health",
            timeout=1,
            trust_env=False,
            headers={"Authorization": "Bearer " + token},
        )
        response.raise_for_status()
        health = response.json()
        if health.get("ready") is not True:
            return False
        if "speech" in wanted and health.get("speech_model") != wanted["speech"]["id"]:
            return False
        if "cleanup" in wanted:
            return (
                health.get("correction_model") == wanted["cleanup"]["id"]
                and health.get("correction_mode") == "managed"
                and health.get("correction_leaves_machine") is False
            )
        return True
    except (OSError, httpx.HTTPError, ValueError):
        return False


def start_server(port, model_dir=None):
    speech_models.discover(model_dir) if model_dir else speech_models.discover()
    if server_running(port) and not model_catalog.pending_restart():
        emit("started")
        return
    emit("starting")
    wanted = model_catalog.read_json(model_catalog.root() / "model-selection.json")
    label = "dev.chirp.server"
    domain = f"gui/{os.getuid()}"
    destination = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    if destination.exists():
        # Reload a launchd job that was registered before its venv existed.
        # Reuse nix-darwin's plist, including its port/root-path configuration.
        import plistlib

        document = plistlib.loads(destination.read_bytes())
        arguments = document.get("ProgramArguments", [])
        if document.get("Label") != label or arguments[1:3] != ["-m", "chirp_server"]:
            raise ValueError("Unexpected local server service")
        if "--model-dir" in arguments and model_catalog.pending_restart():
            raise ValueError("Remove the service model-dir override before applying model choices")
        service_port = (
            int(arguments[arguments.index("--port") + 1]) if "--port" in arguments else 7821
        )
        if service_port != port:
            raise ValueError("Local server port differs from the app setting")
        subprocess.run(
            ["/bin/launchctl", "bootout", f"{domain}/{label}"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        # launchd can briefly reject bootstrap while a previous bootout settles.
        for attempt in range(3):
            try:
                subprocess.run(
                    ["/bin/launchctl", "bootstrap", domain, str(destination)],
                    capture_output=True,
                    timeout=5,
                    check=True,
                )
                break
            except subprocess.CalledProcessError:
                if attempt == 2:
                    raise
                time.sleep(0.5)
    else:
        from .cli import service

        service("install", ["--port", str(port)] if port != 7821 else [])
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if server_running(port) and server_matches(port, wanted):
            result = model_catalog.update_json(
                model_catalog.root() / "model-selection.json",
                lambda data: (
                    {**data, "restart_required": False, "correction_changed": False}
                    if all(data.get(key) == wanted.get(key) for key in ("speech", "cleanup"))
                    else data
                ),
            )
            emit(
                "started",
                enable_correction=bool(
                    wanted.get("correction_changed") and not result.get("restart_required")
                ),
            )
            return
        time.sleep(0.5)
    raise TimeoutError("Server did not become ready")


def emit_catalog(settings):
    emit(
        "catalog",
        options=model_catalog.options(settings),
        active_models=model_catalog.active_ids(settings),
        restart_required=model_catalog.pending_restart(),
    )


def run(action, model, config, port, choice=None, repository=None):
    try:
        settings = Settings.read(config)
        if settings.engine == "sensevoice" and (
            action == "start" or (action == "select" and model == "speech")
        ):
            emit(
                "error",
                model=model,
                message="SenseVoice is configured. Start it with chirp-server directly. "
                "Mac local speech selection and setup start manage MLX; "
                'set engine = "mlx" to use them.',
            )
            return 1
        if action == "status":
            emit(
                "status",
                models=status(settings),
                correction_mode=settings.correction.mode,
                server_running=server_running(port),
            )
            emit_catalog(settings)
        elif action == "download":
            download(model, settings, choice) if choice else download(model, settings)
        elif action == "select":
            if not choice:
                raise ValueError("Choose a model first")
            emit("progress", model=model, phase="verifying")
            selected = model_catalog.choose(choice, model)
            emit("selected", model=model, choice=selected.id)
            emit_catalog(Settings.read(config))
        elif action == "add":
            configure_downloads()
            added = model_catalog.add(model, repository or "")
            emit("added", model=model, choice=added.id)
            emit_catalog(settings)
        elif action == "refresh":
            configure_downloads()
            sources = {(m.kind, m.repository) for m in model_catalog.catalog().values()}
            failures = 0
            for kind, source in sorted(sources):
                emit("catalog_progress", message=f"Checking {source.split('/')[-1]}…")
                try:
                    model_catalog.add(kind, source)
                except Exception:
                    failures += 1
            emit_catalog(settings)
            if failures:
                emit(
                    "error",
                    message=f"Could not check {failures} model(s). Choices kept; retry online.",
                )
                return 1
            emit("catalog_progress", message="Models are up to date. Choose a model to download.")
        else:
            start_server(port, settings.model_dir) if settings.model_dir else start_server(port)
        return 0
    except KeyboardInterrupt:
        emit("cancelled", model=model)
        return 130
    except Exception:
        # Exception text can contain signed download URLs or credentials.
        messages = {
            "start": "Could not start the server. Check its configuration and try again.",
            "download": (
                "Could not finish setup. Check your connection and free disk space, then retry."
            ),
            "select": (
                "This model could not be loaded. Your previous selection is kept. "
                "Choose a compatible model or retry its download."
            ),
            "add": (
                "Could not add this model. Use a public Hugging Face repository with "
                "compatible Parakeet TDT or quantized MLX text weights."
            ),
        }
        emit(
            "error",
            model=model if action in {"download", "select"} else None,
            message=messages.get(
                action, "Could not read local setup. Check the connection and server configuration."
            ),
        )
        return 1
