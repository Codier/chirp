# Chirp server

Local streaming speech-to-text for Chirp. Python 3.13, Starlette and Parakeet MLX on Apple Silicon, managed by [uv](https://docs.astral.sh/uv/).

| At a glance | |
|---|---|
| Binds to | `127.0.0.1:7821` only — never the network |
| Engine | Selectable Parakeet TDT via MLX (English v2 default, multilingual v3 available); ONNX v2 int8 alternative |
| Auth | Bearer token, constant-time comparison; audio is rejected before authentication |
| Retention | Nothing written by default; debug retention is opt-in, local and loudly visible |
| Clean-up ("correction") | Off by default; optional on-Mac Qwen model or external OpenAI-compatible endpoint |

No service, network publication or model download happens on installation.

**Contents**

- [Install](#install) · [Run](#run) · [Pair a device](#pair-a-device)
- [The API](#the-api) — endpoints, auth, limits, error codes, streaming modes
- [Configure](#configure) · [Clean-up](#clean-up) — modes, managed model, keep_loaded, profiles, LM Studio
- [Doctor](#doctor) · [Privacy and debugging](#privacy-and-debugging) · [Optional LaunchAgent](#optional-launchagent-operator-action)
- [Develop and test](#develop-and-test) · [Explicit downloads and cleanup](#explicit-optional-downloads-and-cleanup)

## Install

```sh
cd server
uv sync --locked
```

This installs the Python package only. It starts no service, publishes nothing, and downloads no model.

## Run

```sh
uv run chirp-server
```

The server validates and reuses the cached English TDT v2 model at `~/.koi/voice-models/parakeet-mlx`. If that cache is missing, run the explicit download instead (pinned Hugging Face revision):

```sh
uv run chirp-server model fetch
```

`--model-dir /path/to/model` takes precedence over both. At startup the server loads the model and silently warms it with a real decode, on the same dedicated thread that later runs every decode and streaming-context operation.

Useful flags:

| Flag | Effect |
|---|---|
| `--root-path /chirp` | Serves `/chirp/v1/...` **and** `/v1/...`, so Tailscale Serve may strip the mount point or not |
| `--model-dir /path/to/model` | Use this model directory instead of the cache/store |
| `--engine onnx` | Use the ONNX int8 engine (install first: `uv sync --locked --extra onnx`; fetch with `uv run chirp-server model fetch --engine onnx`) |
| `--onnx-intra-op-num-threads N` | Parakeet ONNX threads within operators; `0` lets ONNX Runtime choose |
| `--onnx-inter-op-num-threads N` | Parakeet ONNX threads across operators; only effective with parallel execution |
| `--engine sensevoice` | Local CPU Mandarin/English recognition; see [SenseVoice](#sensevoice-cpu-backend) |
| `--precision float16\|bfloat16` | Experiments; see [streaming and finalization](#streaming-and-finalization) |
| `--streaming incremental` | Upstream cached-context streaming instead of the measured rolling default |
| `--finalization segments` | Opt-in pause-precommit finalization |
| `--debug-retain` | Opt into local debug recording — see [Privacy and debugging](#privacy-and-debugging) |
| `--config /path/to/server.toml` | Config file instead of `~/.config/chirp/server.toml` |

## SenseVoice CPU backend

SenseVoice Small is an optional local backend for Mandarin and English, with automatic language
detection and Simplified Chinese output for Mandarin in the public fixtures. The default remains
Parakeet MLX. Existing Mac and Android clients use the same v1 protocol and pairing credentials.

From `server/`, with Python 3.13:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install '.[sensevoice]'
.venv/bin/chirp-server model fetch --engine sensevoice
.venv/bin/chirp-server --engine sensevoice
```

With an existing uv installation, use `uv sync --locked --extra sensevoice` and
`uv run chirp-server model fetch --engine sensevoice`, then `uv run chirp-server --engine sensevoice`.
MLX dependencies install only on macOS. SenseVoice needs neither MLX nor the Parakeet ONNX extra
on Linux. No system service or network publication is part of this setup.

Model acquisition is explicit. The download selects full-precision `model.onnx` and `tokens.txt`
from [`csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`](https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17)
at revision `2365baeacb507f821a0c8120fcee3d484dba7a07`. It also downloads
[Silero VAD](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx),
verified against SHA-256 `9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6`.
The recognizer is pinned to sherpa-onnx 1.13.8. Model code is never downloaded or executed.
The assets occupy about 938 MB in `~/Library/Application Support/Chirp/models/sensevoice-small/`
on either platform, following the existing model-store convention. Delete that directory to remove
them. Startup and inference require local files and never fetch missing models.
`--model-dir` may point to another directory containing all three files.

To save the backend choice in `~/.config/chirp/server.toml`:

```toml
engine = "sensevoice"

[sensevoice]
num_threads = 4
```

`--engine` overrides the saved engine. `--sensevoice-num-threads 2` overrides the thread count,
which must be an integer from 1 to 256. The Mac model picker manages MLX choices. While SenseVoice
is configured, speech selection and `setup start` report a clear message directing you to start
`chirp-server` directly;
set `engine = "mlx"` to use the Mac helper again. A saved MLX selection does not supply
SenseVoice's model directory.
`chirp-server --engine sensevoice doctor --quick` checks the optional package and local assets;
without `--quick`, it also loads and warms the model.

SenseVoice uses Silero VAD pauses to split recordings into phrases. Short phrases share context for
language detection, and uninterrupted speech is cut at 20 seconds. Original audio is retained for
the final decode. Previews reuse completed phrase results in chronological order, without matching
or removing repeated Chinese characters or English words. Previews may revise earlier text and
do not promise a stable prefix or word timestamps. SenseVoice always uses this preview strategy
and a full final decode; the Parakeet `--streaming`, `--finalization` and `--precision` switches
do not change it. Cancel and disconnect discard the session through the existing engine worker.
Authentication, loopback binding, correction-off and retention-off defaults are unchanged.

Recognition has limits. An unsegmented long mixed-language decode dropped content in an early
probe; phrase segmentation retained both languages in the public fixture. INT8 made more errors in
those fixtures, so this backend uses full precision. The Mandarin fixture still has a homophone
error, and the English/mixed fixtures have word and punctuation errors. Natural language switching
within one sentence remains insufficiently tested. Pauses shorter than the grouping window may
keep different languages in one decode; a hard cut during uninterrupted speech can split a word.
Very quiet or very short speech can be missed by VAD. This is not a promise of lossless transcription.

Cached-model WebSocket regressions are opt-in and never download audio or weights:

```sh
CHIRP_SENSEVOICE_MODEL_DIR=/path/to/sensevoice-small \
CHIRP_SENSEVOICE_FIXTURES=/path/to/public-fixtures \
  .venv/bin/python -m pytest -m model tests/test_sensevoice_model.py
```

The fixture directory must contain `zh.wav` from the pinned model's `test_wavs/zh.wav`, and
[`jfk.wav`](https://github.com/ggml-org/whisper.cpp/blob/master/samples/jfk.wav).
Tests check their hashes before using them, send Chinese, English and joined language-switching
audio at real-time pace through the existing Mac and Android WebSocket messages, and check
silence and cancellation. Batch/final equality checks transport consistency; separate content
assertions check Simplified Chinese and English retention. Neither establishes general recognition
accuracy. These tests use only in-process sockets; a real loopback server should also be checked
with `tests/client_sim.py` before reporting network timings.

## Selectable local models

The Mac app's **Manage Local Models…** window provides selection, explicit downloads and update checks without editing configuration. For a standalone server install run `./scripts/install-mac.sh --server-only` from the repo root; it uses a locked environment in `~/Library/Application Support/Chirp/server`.

From `server/`, the equivalent helper commands are:

```sh
uv run chirp-server setup status          # offline catalog, sizes, cached and selected models
uv run chirp-server setup refresh         # metadata only; checks latest repository revisions
uv run chirp-server setup download --model speech --choice parakeet-v3
uv run chirp-server setup select --model speech --choice parakeet-v3
uv run chirp-server setup download --model cleanup --choice qwen3.5-2b-4bit
uv run chirp-server setup select --model cleanup --choice qwen3.5-2b-4bit
uv run chirp-server setup start           # start/restart to apply saved selections
```

`setup add --model speech|cleanup --repository owner/model` adds a compatible public Hugging Face model. Its returned catalog ID includes an immutable revision; use that ID with download/select. Refresh never upgrades a running model or downloads its weights. Compatibility checks accept 16 kHz Parakeet TDT for speech and quantized MLX-LM architectures for correction; actual loading is checked before selection. Newly added models are not quality-benchmarked, and new architectures may require a runtime update. Startup and inference remain offline, with `trust_remote_code=False`.

User choices live in `~/.config/chirp/model-selection.json`, checked metadata in `model-catalog.json`. Selecting correction enables managed local mode; config TOML is preserved. After a change the UI asks for **Apply & Restart Server**, which ends active dictation sessions. Older pinned versions remain available. `--model-dir` explicitly overrides the speech selection; managed launch agents with that override must be adjusted before the UI can apply a different model.

Authenticated health adds `speech_model` and `correction_model`; unauthenticated health remains `{"ok":true}` only.

## Pair a device

```sh
uv run chirp-server pair --url https://<your-mac>.<tailnet>.ts.net/chirp
```

`pair` prints a **secret** pairing URI and a terminal QR code, locally only. Do not paste it into logs or chat. The generated bearer token lives at `~/.config/chirp/token`, mode 0600.

## The API

Full protocol details, including every message field, are in [the architecture doc](../docs/architecture.md#3-wire-protocol-v1-frozen-for-parallel-work). Summary:

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /v1/health` | none | `{"ok":true}` only |
| `GET /v1/health` | bearer | Adds readiness, retention and correction information |
| `GET /v1/correction/profiles` | bearer | Clean-up profile list with editable prompts (works even when correction is off) |
| `GET`/`PUT /v1/correction/settings` | bearer | Clean-up `keep_loaded` state (see [keep_loaded](#keep_loaded)) |
| `POST /v1/notes/structure` | bearer | Live Notes: structure a transcript window into Markdown (see [Notes structuring](#notes-structuring)) |
| `POST /v1/notes/reorganize` | bearer | Live Notes: reorganize an edited notes document (see [Notes structuring](#notes-structuring)) |
| `POST /v1/transcribe` | bearer | 16 kHz mono PCM16 WAV body (same limits as streaming); returns the final object |
| `WS /v1/stream` | bearer | Streaming dictation |

### Authentication

- Every request carries `Authorization: Bearer <token>`. HTTP and WS checks share a constant-time comparison.
- A browser WebSocket that cannot set headers sends `{"type":"auth","token":"…"}` as its first frame instead.
- Audio is rejected before authentication: unauthenticated clients never get to send speech.
- Unauthenticated health deliberately reports only `{"ok":true}`.

### Streaming flow and limits

A socket carries sequential utterances: `start` → binary PCM frames → `stop` (finalize) or `cancel` (discard). Partials stream back while you speak; the final carries text, raw text and timings.

| Limit | Value |
|---|---|
| Audio format | Mono, little-endian signed 16-bit PCM at 16 kHz |
| Frame size | Frames of 1 MB or more are rejected |
| Utterance length | 120 s maximum |
| Concurrent utterances | One model, one utterance at a time; requests wait up to 10 s then get `busy` |
| First-frame auth deadline | 5 s; at most 8 sockets may await credentials concurrently |
| Idle socket | Closed after 60 s without client traffic |

Cancel or disconnect during finalization discards the utterance: no correction request is sent (an in-flight one is cancelled) and nothing is retained. A cancelled utterance delivers no final and no error acknowledgement; the socket stays usable for the next start. `POST /v1/transcribe` uses raw decoding — no client correction request.

### Error codes

| Code | Meaning |
|---|---|
| `auth` | Missing/invalid credentials (HTTP 401 on the REST endpoints) |
| `busy` | Engine occupied longer than the queue wait |
| `too_long` | Utterance exceeded 120 s (HTTP 413 on POST) |
| `bad_audio` | Invalid audio or control frame |
| `bad_request` | Invalid correction prompt/settings body (HTTP 400) |
| `unsupported` | PUT settings outside managed mode (HTTP 409) |
| `bad_notes` | Invalid notes-structuring or reorganize request (HTTP 400) |
| `notes_off` | Notes structuring needs correction enabled (HTTP 409) |
| `engine` | Speech engine unavailable |

### Streaming and finalization

- **Rolling partials (default)** consume roughly 200 ms of new audio at a time. When the engine lags, up to two seconds coalesce into one inference. MLX previews decode an eight-second window and reuse the library's timestamped token merger, so the whole live transcript is retained.
- **The default final** performs a full-utterance decode, using overlapping 30-second chunks for long utterances. At stop, queued partial inference is skipped and its audio is buffered directly for the final.
- `stable` is deliberately empty in this mode, because the full final can revise any word.
- Silence produces no hallucinated final.
- `--finalization segments` explicitly enables pause precommit: after at least 400 ms of low signal, the preceding segment is full-decoded and frozen, and only the remaining tail is decoded at stop. Its `stable` contains the frozen segments (suppressed when final correction can revise them). Both modes matched full-decode WER on the measured continuous and controlled-pause fixtures, but segment mode regressed the short continuous fixture's paired median final latency, so it remains opt-in.
- `--streaming incremental` uses upstream's documented cached context and one-second chunks, with full re-decoding for final quality. The measured rolling path stays the default.
- `--precision float16|bfloat16` enables experiments. Spectral preprocessing stays fp32 because upstream 0.5.3's reduced-precision FFT view has an invalid shape.

### Word timings (opt-in)

A client that sends `"word_timings": true` in `start` gets `words` on every partial, on
`correcting` and on the final: `[{"text": "hello,", "start": 0.48, "end": 0.71, "stable": false}, …]`,
aligned with the raw text (joining the entries with spaces gives `text`, or `raw` for correcting
and finals), in seconds from the utterance's first sample. Parakeet TDT times its SentencePiece
pieces; `engine/words.py` folds them into words at the `▁` boundaries, so attached punctuation
stays with its word and the result matches the rendered transcript. The rolling preview carries
its merged timestamped tokens, so words keep their positions across the eight-second windows
and pause segments are shifted by the audio cut off the front. `stable` marks words later
partials leave alone (committed segments, or older than the preview's merge reach); the final's
words are all stable, and `words` always describe the raw decode (cleaned text cannot be mapped
word for word). The ONNX fallback has no token timings and omits the field. Without the flag
every message is byte-identical to before, so old Mac and Android clients are unaffected. The
grouping is a linear pass over at most a few hundred pieces: measured per-partial latency on the
15 s fixture is unchanged within run-to-run noise (see the branch report in `docs/review/word-waveform.md`).

## Configure

Optional `~/.config/chirp/server.toml` (or `--config /path/to/server.toml`). Restart the server after editing it.

```toml
[correction]
mode = "off" # off (default), managed, endpoint
base_url = "http://127.0.0.1:1234/v1"
model = "your-loaded-model"
timeout = 8.0
# notes_timeout = 15.0
# structure_timeout = 30.0
# reorganize_timeout = 45.0
api_key_env = "CHIRP_LLM_API_KEY"
# keychain_service = "chirp-llm" # security find-generic-password -s SERVICE -w
# prompt = "Custom transcript cleanup prompt"
# idle_seconds = 60.0
# keep_loaded = false

[debug]
retain = false
retain_days = 3
```

| `[correction]` key | Meaning |
|---|---|
| `mode` | `off` (default), `managed` (on-Mac Qwen model) or `endpoint` (OpenAI-compatible URL) |
| `base_url` | Endpoint-mode server URL |
| `model` | Endpoint-mode model identifier |
| `timeout` | Whole-call correction timeout in seconds (default 8.0) |
| `notes_timeout` | Whole-call timeout for a Live Notes segment's clean-up (`live_notes` style) in seconds (default 15.0); dictation keeps `timeout` |
| `structure_timeout` | Whole-call Notes structuring timeout in seconds (default 30.0); the same finite, positive validation as `timeout` |
| `reorganize_timeout` | Whole-call Notes reorganize timeout in seconds (default 45.0; up to 1024 output tokens, about 15 s on an M5 Pro at 3500 characters); the same validation |
| `api_key_env` | Environment variable holding the API key (endpoint mode) |
| `keychain_service` | Read the key from Keychain: `security find-generic-password -s SERVICE -w` |
| `prompt` | Legacy override for the `default` profile's prompt |
| `idle_seconds` | Managed helper idle unload time (default 60.0) |
| `keep_loaded` | Keep the managed helper resident; see [keep_loaded](#keep_loaded) |

API keys come from the named environment variable or Keychain — never from the TOML file, and they are used only by endpoint mode.

### Parakeet ONNX CPU threads

For `--engine onnx`, you can tune CPU inference in `server.toml`:

```toml
[onnx]
intra_op_num_threads = 4
inter_op_num_threads = 1
```

Or set each value on the server command line:

```sh
uv run chirp-server --engine onnx --onnx-intra-op-num-threads 4 --onnx-inter-op-num-threads 1
```

Both keys are optional. Leaving them unset preserves the existing ONNX Runtime behavior,
without passing custom session options. Each value must be an integer from `0` to
`2147483647`; `0` asks ONNX Runtime to choose automatically. Booleans, strings, fractional
and negative values are rejected. An explicit CLI value overrides that key in TOML,
including `0`; omitting a flag keeps the configured value. These settings only affect
Parakeet ONNX and take effect after restarting the server. They do not select the engine.

The example is a starting point for measurement, not a default for all CPUs. An earlier
INT8 experiment on a shared six-core Intel Core i5-8500T measured a 7.435-second clip at
557 ms and a 30.435-second clip at 2120 ms with intra-op `4` and inter-op `1`, compared
with 1144–1568 ms and 4873–5313 ms using automatic thread selection. Those are prototype
measurements on that host, not a benchmark of this implementation. Measure your own clips
with your normal concurrent workloads before keeping a setting.

Chirp passes these values through the supported `onnx_asr.load_model(sess_options=...)`
interface. Execution mode and spinning policy keep their existing defaults. ONNX Runtime
uses sequential execution by default, where `inter_op_num_threads` has no effect. Chirp
does not enable parallel execution. See [ONNX Runtime thread management](https://onnxruntime.ai/docs/performance/tune-performance/threading.html).

## Clean-up

Clean-up ("correction") polishes the **final** transcript only; partials always stay raw. It requires a server mode of `managed` or `endpoint` **and** the client's `start.correction=true`. Legacy `enabled=true` still selects `endpoint` when `mode` is omitted; an explicit `mode` takes precedence.

Only the final transcript is submitted. The raw transcript is always returned for undo. Timeouts, HTTP failures, empty/truncated/answer-shaped output return the raw text safely. Every final includes both `text` and `raw`, plus `corrected`, `correction_error` and cleanup timing.

Enabled external correction reports `correction_leaves_machine=true` and `correction_host` so clients can label the destination. Set `mode = "off"` to disable cleanup entirely.

### Private managed cleanup

Managed cleanup runs a Qwen model in MLX on Apple Silicon, in a supervised child process. Fetch a model explicitly (this is the only download):

```sh
uv run chirp-server model fetch --cleanup
# Or select a pinned catalog entry explicitly:
uv run chirp-server model fetch --cleanup qwen3.5-2b-4bit
uv run chirp-server model fetch --cleanup qwen3.5-0.8b-4bit
```

```toml
[correction]
mode = "managed"
# model = "qwen3-4b-instruct-2507-4bit"
timeout = 8.0
idle_seconds = 60.0
keep_loaded = false  # true: never idle-unload; also prewarm at server startup
```

How the helper behaves:

- Model loading and all generation use the same thread. The first final loads the model; later finals reuse it.
- After 60 idle seconds the helper exits and releases its weights.
- The whole-call timeout includes queueing and cold load.
- Timeout, cancellation, crash, missing weights or rejected output all safely return raw text; cancellation suppresses the final entirely.
- No download is triggered by startup, dictation, tests or benchmarks. Managed inference forces Hugging Face and Transformers offline and sends no text to a service. The runtime is locked with the server.

**Prewarm.** A WebSocket `start` with `correction: true` begins the helper load in the background, so the ~2.5 s cold load (up to ~10 s on a busy host) overlaps with the user speaking instead of delaying the final. The prewarm never delays the start ack, partials or ASR; never counts against an utterance's correction timeout (a final's whole-call timeout may still wait for an in-flight load); survives a cancelled utterance on the normal idle timer; and a failed load falls back to raw text exactly as a cold start does. Concurrent starts share one load.

### keep_loaded

`keep_loaded = true` keeps the helper resident: no idle unload, and the server prewarms the helper at startup.

The effective value is a client-persisted runtime override in `~/Library/Application Support/Chirp/server-state.json` (mode 0600, atomic write, containing only `{"correction_keep_loaded": bool}`), which wins over the TOML value; `server.toml` is never rewritten.

The authenticated `GET /v1/correction/settings` endpoint (same bearer auth as the profiles endpoint) reports `{"keep_loaded", "loaded", "loading", "model", "memory_mb", "memory_estimated", "mode"}`. `PUT` with `{"keep_loaded": bool}` persists the override and applies it immediately: `true` loads in the background and never idle-unloads; `false` re-arms the idle timer. Invalid bodies return 400 `bad_request`; outside managed mode PUT returns 409 `unsupported`.

`memory_mb` is the loaded helper's measured memory — its current physical footprint, the figure Activity Monitor's Memory column shows, read via `proc_pid_rusage(RUSAGE_INFO_V4).ri_phys_footprint` and refreshed after every generate (see [bench.md](../docs/bench.md#cleanup-prewarm-and-keep_loaded-2026-10-17)) — or the last measured value when unloaded, or a per-model catalog estimate with `memory_estimated: true`; null outside managed mode. It is binary MiB (bytes / 1048576, rounded to int); clients format gigabytes as `memory_mb / 1024`.

Authenticated `/v1/health` adds `correction_keep_loaded` and `correction_loaded` (additive; unauthenticated health stays `{"ok": true}`), and `chirp-server doctor` reports the effective keep_loaded state and the loaded-helper memory estimate.

### Where the weights live

Weights use the shared Hugging Face cache (`~/.cache/huggingface/hub`, or `HF_HOME` / `HF_HUB_CACHE` when explicitly set). `model fetch --cleanup` reuses existing pinned snapshots and blobs without copying them into Chirp's model store. To remove a cleanup model, stop the server and remove its `models--mlx-community--<model-name>` directory from that cache, or use Hugging Face's cache tools. Other applications may share those weights. No Koi model files are changed. Catalog revisions are recorded in `src/chirp_server/cleanup_models.py`.

### What the default profile does (and its limits)

The default profile preserves the speaker's words, clauses, hedges and tone. It removes fillers, accidental repetitions and explicit self-corrections, and fixes punctuation and casing. It preserves spoken terms such as "package dot json" instead of rewriting them as code. Unsupported word deletions, additions or reordering fall back to raw text; ambiguous repairs may also fall back to raw.

The 4B default passed 40/40 synthetic evaluation cases; the faster 2B and 0.8B alternatives passed 23/40 and 20/40. See [bench.md](../docs/bench.md#conservative-cleanup-evaluation-2026-10-17) for scoring, timings, guard limitations and reproduction commands.

### Correction destinations

Managed mode and loopback endpoint mode keep text on this Mac. A remote endpoint receives final text only when the client opts in. Endpoint redirects and environment proxy settings are disabled. API keys are used only by endpoint mode. Authenticated health retains `correction = "off|available"` for existing clients and adds `correction_mode = "off|managed|endpoint"`; managed mode reports a null host and `correction_leaves_machine = false`.

### Editing cleanup profiles

Profiles ship in code; no configuration is needed to use `default`, `chat`, `email`, `code`, `notes` or `custom`:

| Profile | Behaviour | `rewrites` |
|---|---|---|
| `default` | Conservative cleanup: preserves words, clauses, hedges and tone | false |
| `chat` | Allows lowercase, skips periods on short messages | false |
| `email` | Polishes sentences without adding content | false |
| `code` | Preserves literals, permits unambiguous spoken symbols | false |
| `notes` | Formats spoken lists | true |
| `custom` | Your instructions within the fixed safety rules | true |

Notes and custom advertise `rewrites=true` because they can restructure text. Authenticated `/v1/health` lists the available IDs and display metadata, without prompt text. Authenticated `GET /v1/correction/profiles` returns `{"profiles":[{"id","name","description","rewrites":bool,"prompt":"…","max_prompt_chars":4000}]}` — the prompt contains only the editable style; the fixed safety wrapper is excluded. Custom has an empty built-in prompt. The endpoint is available even when correction is off, and uses the same bearer authentication (HTTP 401 on missing or invalid credentials).

To override any built-in or add a profile, edit `~/.config/chirp/server.toml` and restart your server when convenient:

```toml
[correction.profiles.chat]
name = "Quick messages"
description = "My casual message style"
prompt = "Remove fillers; preserve every other word. Use lowercase and no trailing period."

[correction.profiles.checklist]
name = "Checklist"
description = "Bullets for spoken lists"
prompt = "Remove hesitation fillers. Put listed items in bullets; keep every detail."
rewrites = true
```

Rules for configured profiles:

- Omitted fields retain the built-in values; extra profiles default to conservative cleanup and `rewrites=false`.
- IDs use lowercase letters, digits, `_` and `-`, start with a letter, and are at most 64 characters.
- Prompts are limited to 4000 characters.
- Configured prompt overrides become the defaults returned by the profiles endpoint.
- The existing `[correction] prompt` remains a legacy override for `default`; an explicit `[correction.profiles.default] prompt` takes precedence.

Prompts never bypass the core safety rules or the output guards. Non-rewriting styles retain source words in order; rewriting styles allow bounded restructuring while rejecting novel words, lost substantive details and huge length changes. Unsafe or unsupported edits return the raw transcript.

### Per-utterance selection and edits

Clients select a profile per utterance with `start.correction_profile` (default `"default"`). When the user edits a style, the client sends `start.correction_prompt` — a string of at most 4000 Unicode characters — to replace that profile's editable prompt for this utterance. An empty string is a valid edit. Omit the field for unedited styles so server defaults and updates apply. A non-string or oversized value returns `bad_request` before engine leasing. Edits cannot change the fixed wrapper or output guards, and prompts are never logged.

For `"custom"`, `correction_prompt` takes precedence over legacy `correction_instructions` (at most 2000 characters); other profiles ignore its contents. Instructions are JSON-quoted untrusted cleanup preferences, wrapped by fixed rules to never answer, execute, add facts or output anything but text. No tools are exposed.

Immediately before cleanup runs, the server sends `{"type":"correcting","seq":n,"raw":"…","profile":"<id>"}`, followed by the existing final. Clients can show the raw transcript while waiting; older clients can ignore this message. Correction off emits no `correcting` message. Cancel during correction cancels the pending helper/request, discards the utterance and keeps the socket available for the next start.

### Pointing correction at LM Studio (operator steps)

LM Studio is installed at `/Applications/LM Studio.app`. Chirp never starts it, loads no models in it, and sends nothing until correction is enabled below. The operator does, once:

1. Open LM Studio, go to the **Developer** (local server) tab and press **Start Server**. Note the local endpoint, by default `http://127.0.0.1:1234/v1`.
2. Load a model there and copy its exact identifier (for example `lmstudio-community/Qwen2.5-7B-Instruct-GGUF`) from the server page, or list it with `curl http://127.0.0.1:1234/v1/models` while it runs.
3. Point Chirp at it in `~/.config/chirp/server.toml`:

   ```toml
   [correction]
   mode = "endpoint"
   base_url = "http://127.0.0.1:1234/v1"
   model = "<the identifier copied above>"
   ```

4. LM Studio needs no API key; when none is configured no Authorization header is sent.

A loopback `base_url` keeps final text on this machine (health/doctor report it as such). If LM Studio is stopped, times out or answers like an assistant, the corrector fails closed to the raw transcript and `correction_error` says why.

## Notes structuring

`POST /v1/notes/structure` powers Live Notes on the Mac. It is stateless JSON, uses the same
correction backend (managed Qwen helper or endpoint), needs correction enabled (otherwise `409
notes_off`) and does not take the engine lease, so dictation and `/v1/stream` are unaffected.

```sh
curl -s -H "Authorization: Bearer $(cat ~/.config/chirp/token)" -H 'Content-Type: application/json' \
  -d '{"transcript":"We decided to keep the vendor. Maya will send the spec by Friday.","outline":[]}' \
  http://127.0.0.1:7821/v1/notes/structure
```

A success returns `{"markdown":"…","structured":true,"error":null,"timings_ms":{"structure":…}}`.
When the model fails, times out or its output is rejected by the guards (invented names, numbers,
dates or contacts, an unrelated answer, a negated statement flipped to a positive one, formatting
outside the allowed Markdown subset), the answer is HTTP 200 with
`"markdown":null,"structured":false` and `error` of `timeout`, `http` or `rejected` (plus `reason`,
the name of the guard that rejected it, such as `name` or `negation`; never text), and the client
keeps its previous notes. Only one structuring request runs at a time (a second gets `503 busy`),
a request body is capped at 64 KiB, and the transcript at 6000 characters. Request and response
details are in [the architecture doc](../docs/architecture.md#live-notes-structuring-v1-compatible-addition).
Text is never retained or logged (the log line has lengths, counts, the error code and timings),
even with `--debug-retain`.

In managed mode a queued dictation correction is always served before a queued structuring
request; a running structuring generation (at most 768 tokens) is never interrupted.

`POST /v1/notes/reorganize` regroups the Mac's editable notes document: it merges duplicate
sections and bullets and moves loose lines under the right heading, without dropping or inventing
anything. The body is `{"notes":"<Markdown, at most 3500 chars>","outline":["Earlier heading"],
"final":false}`; the answer is `{"markdown":"…","reorganized":true,"changed":true|false,
"error":null,"timings_ms":{"reorganize":…}}`, or `"markdown":null,"reorganized":false` with
`error` of `timeout`, `http`, `rejected` or `preempted` (and `reason`, the rejecting guard's name).
`changed:false` means the output equalled the input
apart from whitespace. Its guards reject dropped lines, numbers, dates, tasks, Decisions and
Questions, changed task states, invented names, numbers, dates, tasks or headings, flipped, added
or dropped negations and hedges, summaries and padding, and markup the input did not have. It
shares the one-at-a-time structuring lock (`503 busy`), uses up to 1024 output tokens, and in
managed mode gives way to any correction that queues while it runs (it then answers `preempted`,
which the client treats as not run and retries after the next segment). Details are in [the architecture
doc](../docs/architecture.md#live-notes-reorganize-v1-compatible-addition); benchmarks:

```sh
uv run python scripts/bench_notes.py --mode reorganize --repetitions 1 --dump /tmp/reorg-out.jsonl
```

(`--mode structure`, the default, is the structuring bench; `--mode both` runs both.)

Measure it on your Mac with the cached default model (not run in CI):

```sh
uv run python scripts/bench_notes.py --repetitions 3 [--dump out.jsonl]
```

It structures the synthetic transcripts in `tests/notes_eval.jsonl` (about 800, 1600, 2400 and
4000 characters) and prints p50/p95 latency, output tokens and how many outputs pass the guards,
with a histogram of which guard rejected the rest. Results are in the Notes structuring section of
[bench.md](../docs/bench.md). `uv run pytest -m model` additionally structures two of those entries.

## Doctor

```sh
uv run chirp-server doctor          # full run, including a real model load and fixture decode
uv run chirp-server doctor --quick  # same checks, but skips the model load/decode
```

Read-only diagnostics: one `[OK]/[WARN]/[FAIL]` line per check, with a remediation hint where needed. Doctor checks:

- Python/uv environment and MLX availability
- The model directory (which path) and a warm decode of the synthetic 5 s fixture (`--quick` skips it; the transcript is matched on a keyword in memory and never printed)
- Config file parse
- Token presence and mode (the value is never printed or created)
- Port 7821 free vs held by chirp-server
- Debug retention state plus the count and size of retained files
- The correction host and whether final text leaves the machine
- The effective cleanup keep_loaded state (persisted override or server.toml) with the loaded-helper memory estimate
- `tailscale serve status --json` parsed read-only, to report whether `/chirp` is published and tailnet-only (doctor never changes Tailscale)
- Whether the optional LaunchAgent is installed

The exit code is 1 only when a check FAILs. Doctor creates no files and starts no services.

## Privacy and debugging

Audio and text stay in memory and are discarded on stop/cancel/disconnect. No recording, transcript, temporary WAV or transcript log is written by default. Diagnostics contain only random session IDs, byte counts, signal levels, timings and safe error codes. Access logging is disabled. The simulator likewise prints counts and timings unless `--show-text` is explicit.

To keep local copies for debugging, opt in explicitly with `uv run chirp-server --debug-retain` or `[debug] retain=true`:

- Writes `~/Library/Application Support/Chirp/debug/<session>.wav` and `.json`, modes 0700/0600.
- Startup logs a warning, and authenticated health plus every WS ready frame expose the flag.
- Files expire after three days by default and purge at startup and hourly while enabled.
- Cancelled utterances are discarded even in debug mode.

```sh
uv run chirp-server debug purge   # delete retained files now
```

## Optional LaunchAgent (operator action)

A reference plist is in `launchd/dev.chirp.server.plist`. Only when the operator chooses:

```sh
uv run chirp-server --root-path /chirp service install   # flags before "service" are kept in the LaunchAgent
uv run chirp-server service uninstall
```

Install renders the current virtual environment's absolute Python path and bootstraps a user LaunchAgent. Keep this checkout and virtual environment in place while installed. Output goes to `/dev/null`; run in a terminal for metadata diagnostics or visible debug warnings. Debug retention is also always visible in the health/ready flags. Installation has **not** been run. Tailnet publication is managed by integration; this package does not change Tailscale settings.

## Develop and test

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run pytest -m model
uv run chirp-server doctor --quick
uv run python scripts/generate_fixtures.py
uv run python tests/client_sim.py "$HOME/Library/Caches/Chirp/fixtures/synthetic-5s.wav"
uv run python scripts/bench.py --output "$HOME/Library/Caches/Chirp/bench.json"
```

The managed-cleanup prewarm/keep_loaded benchmark is `HF_HUB_OFFLINE=1 uv run python scripts/bench_prewarm.py` (cached weights only).

Notes on the tools:

- The simulator streams 50 ms frames at real-time pace and prints partial arrival counts and final timings. `--show-text` is for synthetic/public fixtures in an unrecorded local terminal only.
- Model tests generate Samantha `say` fixtures outside the repository, never capture a microphone, and check keywords without including recognized text in assertion output.
- The benchmark reports load plus warmup, median of three warm decodes, WER, peak process RSS, first partial, changed-partial cadence and stop-to-final latency for nominal 5/15/60 second clips. Fixtures contain complete synthetic sentences, so actual durations differ slightly. Results and hardware/load caveats are in [bench.md](../docs/bench.md).

## Explicit optional downloads and cleanup

Starting `chirp-server`, running the ordinary test suite, and running `scripts/bench.py` never fetch a missing recognition model. Missing models produce a `chirp-server model fetch` hint. Real-model tests use existing cached weights and generate only synthetic `say` fixtures. The optional Swift runners never request Microphone or Speech Recognition authorization. Apple probing checks installed assets and skips if they are missing; grant-dependent paths are not invoked. No launch agent or Tailscale setting is installed by benchmarks.

| Explicit command | Download or generated files | Removal |
|---|---|---|
| `chirp-server model fetch` | Pinned MLX v2 weights in `~/Library/Application Support/Chirp/models/parakeet-mlx/` | Delete that directory; Koi's existing model is untouched |
| `chirp-server model fetch --engine onnx` | Pinned ONNX v2 int8 weights in `~/Library/Application Support/Chirp/models/parakeet-onnx/` | Delete that directory |
| `chirp-server model fetch --cleanup [model]` | Pinned Qwen cleanup weights in the shared Hugging Face cache | See [Where the weights live](#where-the-weights-live) |
| `scripts/generate_fixtures.py` | Synthetic WAVs + reference manifest in `~/Library/Caches/Chirp/fixtures/` | Delete that directory |
| `uv run --extra onnx python scripts/bench.py --engine onnx` | No download; requires explicit ONNX extra install and already local weights | Remove explicitly fetched Chirp weights if no longer needed |
| `scripts/bench.py --with-fluidaudio` | Stable FluidAudio 0.17.7 source/build in `~/Library/Caches/Chirp/FluidAudio-0.17.7/`, CoreML v2 models in `~/Library/Caches/Chirp/FluidModels/`; compiler/SwiftPM may populate their normal caches | Delete these two Chirp cache subdirectories; optional Swift build caches are shared system tooling caches |
| `scripts/bench.py --with-apple` | Builds a small CLI in `~/Library/Caches/Chirp/apple-bench`; **does not download assets** | Delete the CLI; speech models are owned/managed by macOS |

For candidate-only runs, `uv run python scripts/run_swift_benches.py --with-fluidaudio --with-apple` uses the same explicit gates. Without either flag it skips everything. `scripts/prepare_fluid.sh` is an explicit source-download/build command, and never acquires recognition weights by itself. Both candidates print metrics only; recognized output stays in process memory.

History note: the initial M1 Apple probe explicitly downloaded the en-US `SpeechTranscriber` model through `AssetInventory` (system-managed asset storage, outside the Chirp cache). It did not call any permission-request API, open a microphone, install software/services or change Tailscale. The benchmark no longer contains an asset-install path. Removing OS-managed speech assets is an operator action through macOS asset/storage controls; do not delete system asset files by hand.

`~/Library/Caches/Chirp/` can be removed entirely to clear generated fixtures, candidate source/builds, FluidAudio models and metrics. It contains no user recordings or user transcripts.
