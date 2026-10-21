"""Doctor checks against synthetic state only: no real tailscale, token or model calls."""

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest
from conftest import PCM

from chirp_server import doctor
from chirp_server.audio import wav_bytes
from chirp_server.config import CorrectionConfig, Settings
from chirp_server.doctor import FAIL, OK, WARN, Check

PUBLISHED_JSON = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {
        "mainframe.tail676ff2.ts.net:443": {
            "Handlers": {
                "/koi": {"Proxy": "http://127.0.0.1:7710"},
                "/chirp": {"Proxy": "http://127.0.0.1:7821"},
            }
        }
    },
}


def test_render_includes_fix_hint():
    line = doctor.render(Check("model", FAIL, "missing", "fetch it"))
    assert line.startswith("[FAIL] model: missing")
    assert "fix: fetch it" in line


def test_environment_reports_interpreter():
    check = doctor.check_environment()
    assert check.status in {OK, WARN}
    assert "python 3.13" in check.detail


def test_mlx_available():
    pytest.importorskip("mlx.core")
    assert doctor.check_mlx().status == OK


def test_token_missing_is_ok(tmp_path):
    check = doctor.check_token(tmp_path)
    assert check.status == OK
    assert "generated on first server run" in check.detail


def test_token_mode_and_type(tmp_path):
    path = tmp_path / "token"
    path.write_text("synthetic-credential\n")
    path.chmod(0o600)
    assert doctor.check_token(tmp_path).status == OK
    path.chmod(0o644)
    check = doctor.check_token(tmp_path)
    assert check.status == WARN and "0o644" in check.detail
    path.unlink()
    (tmp_path / "real").write_text("target")
    path.symlink_to(tmp_path / "real")
    assert doctor.check_token(tmp_path).status == FAIL


def test_retention_states(tmp_path):
    settings = Settings(config_dir=tmp_path, debug_dir=tmp_path / "debug")
    assert doctor.check_retention(settings).status == OK
    settings.debug_dir.mkdir()
    (settings.debug_dir / "a.wav").write_bytes(b"\0" * 10)
    leftover = doctor.check_retention(settings)
    assert leftover.status == WARN
    assert "1 retained file(s)" in leftover.detail
    settings.debug_retain = True
    enabled = doctor.check_retention(settings)
    assert enabled.status == WARN
    assert "enabled" in enabled.detail and "kept on disk" in enabled.detail


def test_correction_states():
    assert doctor.check_correction(Settings()).status == OK
    loopback = Settings(
        correction=CorrectionConfig(enabled=True, base_url="http://localhost:1234/v1", model="m")
    )
    assert doctor.check_correction(loopback).status == OK
    external = Settings(
        correction=CorrectionConfig(enabled=True, base_url="https://llm.example/v1", model="m")
    )
    check = doctor.check_correction(external)
    assert check.status == WARN and "llm.example" in check.detail
    assert "leaves this machine" in check.detail


def test_correction_managed_reports_keep_loaded_and_memory(monkeypatch, tmp_path):
    monkeypatch.setattr("chirp_server.cleanup_models.discover", lambda model_id="": tmp_path)
    state = tmp_path / "state/server-state.json"
    settings = Settings(
        config_dir=tmp_path,
        debug_dir=tmp_path / "debug",
        state_path=state,
        correction=CorrectionConfig(mode="managed", keep_loaded=True),
    )
    check = doctor.check_correction(settings)
    assert check.status == OK
    assert "keep_loaded on (server.toml)" in check.detail
    assert "MiB" in check.detail
    from chirp_server.config import persist_keep_loaded

    persist_keep_loaded(state, False)
    overridden = doctor.check_correction(settings)
    assert overridden.status == OK
    assert "keep_loaded off (state override)" in overridden.detail


def test_config_defaults_when_file_missing(tmp_path):
    settings, check = doctor.load_settings(tmp_path / "server.toml")
    assert check.status == OK and settings.retain_days == 3


def test_config_valid_parse(tmp_path):
    path = tmp_path / "server.toml"
    path.write_text('[correction]\nenabled = true\nmodel = "m"\n')
    settings, check = doctor.load_settings(path)
    assert check.status == OK and settings.correction.enabled and settings.correction.model == "m"


def test_config_invalid_without_quoting_content(tmp_path):
    path = tmp_path / "server.toml"
    for content in (
        "not = [valid",
        "[correction]\nunknown_key = 1\n",
        "[debug]\nretain_days = 0\n",
    ):
        path.write_text(content)
        _, check = doctor.load_settings(path)
        assert check.status == FAIL and content not in check.detail


def test_tailscale_published_tailnet_only():
    check = doctor.check_tailscale((0, json.dumps(PUBLISHED_JSON)))
    assert check.status == OK
    assert "tailnet-only" in check.detail and "mainframe.tail676ff2.ts.net" in check.detail


def test_tailscale_funnel_warns():
    data = json.loads(json.dumps(PUBLISHED_JSON))
    data["Web"]["mainframe.tail676ff2.ts.net:443"]["Funnel"] = True
    check = doctor.check_tailscale((0, json.dumps(data)))
    assert check.status == WARN and "Funnel" in check.detail


def test_tailscale_wrong_proxy_warns():
    data = json.loads(json.dumps(PUBLISHED_JSON))
    data["Web"]["mainframe.tail676ff2.ts.net:443"]["Handlers"]["/chirp"] = {
        "Proxy": "http://127.0.0.1:9999"
    }
    check = doctor.check_tailscale((0, json.dumps(data)))
    assert check.status == WARN and "127.0.0.1:9999" in check.detail


def test_tailscale_not_published():
    other = {"Web": {"x.ts.net:443": {"Handlers": {"/koi": {"Proxy": "http://127.0.0.1:7710"}}}}}
    check = doctor.check_tailscale((0, json.dumps(other)))
    assert check.status == OK and "not published" in check.detail


def test_tailscale_failure_shapes(monkeypatch):
    assert doctor.check_tailscale((1, "")).status == WARN
    assert doctor.check_tailscale((0, "not json")).status == WARN
    assert doctor.check_tailscale((0, "{bad")).status == WARN

    def missing(command, **kwargs):
        raise FileNotFoundError("tailscale")

    monkeypatch.setattr(doctor.subprocess, "run", missing)
    assert doctor.check_tailscale().status == WARN


def test_port_free():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert doctor.check_port(port).status == OK


def test_port_held_by_chirp(monkeypatch):
    monkeypatch.setattr(doctor, "_listening", lambda port: True)
    monkeypatch.setattr(doctor, "_health_ok", lambda port: True)
    check = doctor.check_port(7821)
    assert check.status == OK and "chirp-server" in check.detail


def test_port_held_by_other_process(monkeypatch):
    monkeypatch.setattr(doctor, "_listening", lambda port: True)
    monkeypatch.setattr(doctor, "_health_ok", lambda port: False)
    check = doctor.check_port(7821)
    assert check.status == WARN and "lsof" in check.fix


def test_warm_decode_missing_fixture(tmp_path):
    check = asyncio.run(doctor.check_warm_decode(fixture=tmp_path / "absent.wav"))
    assert check.status == WARN and "generate_fixtures" in check.fix


class DoctorFakeMLX:
    name = "fake"

    def __init__(self, model_dir=None):
        self.text = "The quick brown fox jumps over the lazy dog."

    def load(self):
        pass

    def warm(self):
        pass

    def transcribe(self, pcm):
        return self.text


def test_warm_decode_keyword_ok(monkeypatch, tmp_path):
    fixture = tmp_path / "synthetic.wav"
    fixture.write_bytes(wav_bytes(PCM))
    monkeypatch.setattr("chirp_server.engine.mlx.ParakeetMLX", DoctorFakeMLX)
    check = asyncio.run(doctor.check_warm_decode(fixture=fixture))
    assert check.status == OK and "not printed" in check.detail


def test_warm_decode_rejects_unexpected_transcript(monkeypatch, tmp_path):
    fixture = tmp_path / "synthetic.wav"
    fixture.write_bytes(wav_bytes(PCM))

    class Wrong(DoctorFakeMLX):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.text = "unrelated words"

    monkeypatch.setattr("chirp_server.engine.mlx.ParakeetMLX", Wrong)
    check = asyncio.run(doctor.check_warm_decode(fixture=fixture))
    assert check.status == FAIL


def test_warm_decode_engine_failure(monkeypatch, tmp_path):
    fixture = tmp_path / "synthetic.wav"
    fixture.write_bytes(wav_bytes(PCM))

    class Broken(DoctorFakeMLX):
        def load(self):
            raise RuntimeError("sensitive internal detail")

    monkeypatch.setattr("chirp_server.engine.mlx.ParakeetMLX", Broken)
    check = asyncio.run(doctor.check_warm_decode(fixture=fixture))
    assert check.status == FAIL and "sensitive" not in check.detail


def test_doctor_quick_reports_every_check(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(doctor, "check_environment", lambda: Check("environment", OK, "Python"))
    monkeypatch.setattr(doctor, "check_mlx", lambda: Check("mlx", OK, "available"))
    monkeypatch.setattr(doctor, "check_model", lambda model_dir=None: Check("model", OK, "cached"))
    args = SimpleNamespace(quick=True, config=None, model_dir=None, port=0)
    code = doctor.doctor(args, tailscale_status=(0, "{}"))
    out = capsys.readouterr().out
    assert code == 0
    assert "[SKIP] warm decode: skipped (--quick)" in out
    for name in (
        "environment",
        "mlx",
        "model",
        "config",
        "token",
        "port",
        "debug retention",
        "correction",
        "tailscale",
        "launchagent",
    ):
        assert f"{name}:" in out
    assert "10 checks: 10 OK, 0 WARN, 0 FAIL" in out


def test_doctor_exits_1_only_on_fail(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        doctor, "check_model", lambda model_dir=None: Check("model", FAIL, "missing", "fetch")
    )
    args = SimpleNamespace(quick=True, config=None, model_dir=None, port=0)
    assert doctor.doctor(args, tailscale_status=(0, "{}")) == 1
    assert "[FAIL]" in capsys.readouterr().out
