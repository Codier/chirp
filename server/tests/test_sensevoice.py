"""SenseVoice regressions without model downloads or native inference."""

import io
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import HEADERS, PCM, START, TOKEN
from starlette.testclient import TestClient
from test_protocol import final

from chirp_server import cli
from chirp_server.app import create_app
from chirp_server.audio import RATE
from chirp_server.config import Settings
from chirp_server.engine import Transcript
from chirp_server.engine import sensevoice_models as models
from chirp_server.engine.sensevoice import MAX_SAMPLES, SenseVoice, join_text


class Detector:
    """Only the segmentation boundary source is replaced; rendering runs real code."""

    def __init__(self, spans):
        self.spans = spans

    def reset(self):
        self.queue = []

    def accept_waveform(self, chunk):
        pass

    def flush(self):
        self.queue = [SimpleNamespace(start=a, samples=np.empty(b - a)) for a, b in self.spans]

    def empty(self):
        return not self.queue

    @property
    def front(self):
        return self.queue[0]

    def pop(self):
        self.queue.pop(0)


def stub_engine(spans=()):
    engine = SenseVoice()
    engine.vad, engine.window_size = Detector(spans), 512
    engine.load = engine.warm = lambda: None
    engine.decode = lambda pcm: "测试API接口。" if pcm[0] < 0.5 else "Review the plan."
    return engine


def test_cuts_keep_all_samples_and_bound_uninterrupted_speech():
    engine = stub_engine([(8000, 65 * RATE)])
    pcm = np.full(65 * RATE + 131, 0.1, dtype=np.float32)
    cuts = engine.cuts(pcm)
    chunks = [pcm[a:b] for a, b in zip(cuts, cuts[1:], strict=False)]
    assert max(map(len, chunks)) <= MAX_SAMPLES
    assert np.array_equal(np.concatenate(chunks), pcm)
    assert engine.transcribe(pcm).count("测试API接口。") == 4


def test_pause_cuts_keep_short_context_and_language_switch():
    engine = stub_engine([(0, 5 * RATE), (6 * RATE, 11 * RATE)])
    pcm = np.r_[np.full(5 * RATE + RATE // 2, 0.1), np.full(6 * RATE, 0.6)].astype(np.float32)
    assert engine.transcribe(pcm) == "测试API接口。 Review the plan."
    assert join_text("hello", "world") == "hello world"
    assert join_text("", "中文") == "中文"


def test_previews_retain_chinese_and_repeated_phrases_across_long_silence():
    engine = stub_engine([(0, 5 * RATE), (6 * RATE, 11 * RATE)])
    stream = engine.open_stream()
    stream.feed(np.full(12 * RATE, 0.1, dtype=np.float32))
    preview = stream.feed(np.zeros(10 * RATE, dtype=np.float32))
    assert preview.text.count("测试API接口。") == 2
    assert preview.stable == "" and preview.words is None
    # Finalization uses retained audio, never a potentially stale preview cache.
    stream.cache[(0, 5 * RATE + RATE // 2)] = "stale"
    result = stream.finish()
    assert isinstance(result, Transcript) and "stale" not in result.text
    assert stream.parts == [] and stream.cache == {}


def test_silence_and_cancel_do_not_decode_or_leak_into_next_utterance():
    engine = stub_engine([(0, RATE)])
    stream = engine.open_stream()
    assert stream.feed(np.zeros(RATE, dtype=np.float32)).text == ""
    stream.cancel()
    assert stream.finish().text == ""
    stream.feed(np.full(RATE, 0.1, dtype=np.float32))
    stream.cancel()
    assert stream.feed(np.full(RATE, 0.6, dtype=np.float32)).text == "Review the plan."
    assert stream.finish().text == "Review the plan."


def test_no_vad_speech_never_hallucinates():
    engine = stub_engine()
    engine.decode = lambda _: pytest.fail("decoder called without VAD speech")
    assert engine.transcribe(np.full(RATE, 0.1, dtype=np.float32)) == ""


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "4", 257])
def test_thread_limits(value):
    with pytest.raises(ValueError, match="num_threads"):
        SenseVoice(num_threads=value)


def test_missing_assets_and_optional_package_have_actionable_errors(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="model fetch --engine sensevoice"):
        SenseVoice(tmp_path).load()
    for name in models.FILES:
        (tmp_path / name).write_bytes(b"fixture")
    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)
    with pytest.raises(ValueError, match=r"chirp-server\[sensevoice\]"):
        SenseVoice(tmp_path).load()


def test_fetch_pins_model_and_rejects_vad_replacement(tmp_path, monkeypatch):
    import huggingface_hub

    requests = []

    def download(repository, **kwargs):
        requests.append((repository, kwargs))
        directory = kwargs["local_dir"]
        directory.mkdir(parents=True)
        for name in ("model.onnx", "tokens.txt"):
            (directory / name).write_bytes(b"fixture")
        (directory / "silero_vad.onnx").write_bytes(b"previous")

    monkeypatch.setattr(models, "model_root", lambda: tmp_path)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    monkeypatch.setattr(models, "urlopen", lambda *a, **k: io.BytesIO(b"tampered"))
    with pytest.raises(ValueError, match="checksum mismatch"):
        models.fetch()
    assert (tmp_path / "sensevoice-small/silero_vad.onnx").read_bytes() == b"previous"
    assert not list((tmp_path / "sensevoice-small").glob(".vad-*"))
    assert requests[0][0] == models.REPOSITORY
    assert requests[0][1]["revision"] == models.REVISION
    assert requests[0][1]["allow_patterns"] == ["model.onnx", "tokens.txt", "LICENSE"]


def test_config_and_cli_select_optional_backend_without_changing_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Settings.read().engine == "mlx"
    config = tmp_path / "server.toml"
    config.write_text('engine = "sensevoice"\n[sensevoice]\nnum_threads = 2\n')
    settings = Settings.read(config)
    assert (settings.engine, settings.speech_model, settings.sensevoice_num_threads) == (
        "sensevoice",
        "sensevoice-small",
        2,
    )
    assert not settings.debug_retain and settings.correction.mode == "off"
    assert cli.parser().parse_args(["--engine", "sensevoice"]).engine == "sensevoice"
    assert (
        cli.parser().parse_args(["model", "fetch", "--engine", "sensevoice"]).engine == "sensevoice"
    )


def test_app_dispatches_settings_to_sensevoice_and_reports_model(settings, monkeypatch):
    from chirp_server.engine import sensevoice

    requested = []
    engine = stub_engine([(0, RATE)])

    def construct(model_dir, threads):
        requested.append((model_dir, threads))
        return engine

    monkeypatch.setattr(sensevoice, "SenseVoice", construct)
    settings.engine, settings.sensevoice_num_threads = "sensevoice", 2
    with TestClient(create_app(settings, token=TOKEN)) as client:
        assert (
            client.get("/v1/health", headers=HEADERS).json()["speech_model"] == "sensevoice-small"
        )
    assert requested == [(settings.model_dir, 2)]


def test_cli_overrides_config_and_separates_mlx_selection(tmp_path, monkeypatch):
    import uvicorn

    from chirp_server import app, model_catalog

    monkeypatch.setenv("HOME", str(tmp_path))

    def selection(settings):
        settings.model_dir = tmp_path / "mlx-selection"
        settings.speech_model = "parakeet-v3"

    monkeypatch.setattr(model_catalog, "apply_settings", selection)
    config = tmp_path / "server.toml"
    config.write_text('engine = "sensevoice"\n[sensevoice]\nnum_threads = 2\n')
    captured = []
    monkeypatch.setattr(app, "create_app", lambda settings: captured.append(settings))
    monkeypatch.setattr(app, "accept_prefix", lambda app, prefix: app)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["chirp-server", "--config", str(config)])
    cli.main()
    assert captured[-1].model_dir is None and captured[-1].sensevoice_num_threads == 2
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "chirp-server",
            "--config",
            str(config),
            "--model-dir",
            str(tmp_path / "custom"),
            "--sensevoice-num-threads",
            "3",
        ],
    )
    cli.main()
    assert captured[-1].model_dir == tmp_path / "custom"
    assert captured[-1].sensevoice_num_threads == 3
    monkeypatch.setattr(sys, "argv", ["chirp-server", "--config", str(config), "--engine", "mlx"])
    cli.main()
    assert captured[-1].engine == "mlx"
    assert captured[-1].model_dir == tmp_path / "mlx-selection"
    assert captured[-1].speech_model == "parakeet-v3"


def test_sensevoice_doctor_never_requires_mlx(settings, monkeypatch, capsys):
    from chirp_server import doctor
    from chirp_server.doctor import OK, Check

    settings.engine = "sensevoice"
    monkeypatch.setattr(doctor, "load_settings", lambda _: (settings, Check("config", OK, "valid")))
    monkeypatch.setattr(doctor, "check_mlx", lambda: pytest.fail("MLX required for SenseVoice"))

    async def check(model_dir, threads, quick):
        assert threads == 4 and quick
        return Check("sensevoice", OK, "cached")

    monkeypatch.setattr(doctor, "check_sensevoice", check)
    args = cli.parser().parse_args(["doctor", "--quick"])
    doctor.doctor(args, tailscale_status=(0, "{}"))
    assert "[OK] sensevoice: cached" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments, expected",
    [
        (["model", "fetch"], "mlx"),
        (["--engine", "sensevoice", "model", "fetch"], "sensevoice"),
        (["model", "fetch", "--engine", "sensevoice"], "sensevoice"),
        (["--engine", "onnx", "model", "fetch"], "onnx"),
        (["--engine", "onnx", "model", "fetch", "--engine", "sensevoice"], "sensevoice"),
    ],
)
def test_fetch_engine_option_before_or_after_subcommand(tmp_path, monkeypatch, arguments, expected):
    from chirp_server.engine import models as speech_models

    monkeypatch.setenv("HOME", str(tmp_path))
    requested = []
    monkeypatch.setattr(speech_models, "fetch", lambda engine: requested.append(engine))
    monkeypatch.setattr(sys, "argv", ["chirp-server", *arguments])
    cli.main()
    assert requested == [expected]


@pytest.mark.parametrize("action", ["start", "select"])
def test_mac_setup_reports_sensevoice_scope_without_mlx_or_service_work(
    tmp_path, monkeypatch, capsys, action
):
    from chirp_server import setup

    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / "server.toml"
    config.write_text('engine = "sensevoice"\n')
    monkeypatch.setattr(setup, "start_server", lambda *a: pytest.fail("Mac service action"))
    monkeypatch.setattr(setup.model_catalog, "choose", lambda *a: pytest.fail("MLX selection"))
    monkeypatch.setattr(
        setup.speech_models, "discover", lambda *a: pytest.fail("MLX model required")
    )
    assert setup.run(action, "speech", config, 7831, "parakeet-v3") == 1
    message = capsys.readouterr().out
    assert '"event": "error"' in message
    assert "SenseVoice is configured" in message and "chirp-server directly" in message


@pytest.mark.parametrize("client_name", ["mac", "android"])
def test_existing_socket_protocol_auth_cancellation_and_untimed_finals(settings, client_name):
    engine = stub_engine([(0, RATE)])
    settings.engine = "sensevoice"
    with TestClient(create_app(settings, engine, TOKEN)) as client:
        assert client.get("/v1/health").json() == {"ok": True}
        assert client.post("/v1/transcribe", content=b"invalid").status_code == 401
        with client.websocket_connect("/v1/stream", headers=HEADERS) as ws:
            ws.send_json({**START, "client": client_name, "word_timings": True})
            assert ws.receive_json()["type"] == "ready"
            ws.send_bytes(PCM)
            partial = ws.receive_json()
            assert partial["type"] == "partial" and "words" not in partial
            ws.send_json({"type": "cancel"})
            ws.send_json({**START, "client": client_name})
            assert ws.receive_json()["type"] == "ready"
            ws.send_bytes(PCM)
            ws.send_json({"type": "stop"})
            result = final(ws)
            assert result["type"] == "final" and "words" not in result
            assert result["raw"] == result["text"] == "测试API接口。"
            assert not result["corrected"]
