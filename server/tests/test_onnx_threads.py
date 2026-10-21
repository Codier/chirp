"""Thread tuning stays optional and scoped to the Parakeet ONNX loader."""

import subprocess
import sys
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import HEADERS, TOKEN
from starlette.testclient import TestClient

from chirp_server import app, cli
from chirp_server.config import OnnxConfig, Settings
from chirp_server.engine.onnx import ParakeetONNX


@pytest.mark.parametrize("name", ["intra_op_num_threads", "inter_op_num_threads"])
@pytest.mark.parametrize("value", [-1, True, 1.5, "4", 2**31])
def test_invalid_thread_values(name, value):
    with pytest.raises(ValueError, match=name):
        OnnxConfig(**{name: value})


@pytest.mark.parametrize("value", [None, 0, 1, 4, 2**31 - 1])
def test_valid_thread_values(value):
    assert OnnxConfig(value, value).intra_op_num_threads == value


def test_config_defaults_and_scoped_table(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    path = tmp_path / "server.toml"
    assert Settings.read(path).onnx == OnnxConfig()
    path.write_text("[onnx]\nintra_op_num_threads = 4\ninter_op_num_threads = 1\n")
    assert Settings.read(path).onnx == OnnxConfig(4, 1)
    path.write_text("[onnx]\nintra_op_num_threads = 0\n")
    assert Settings.read(path).onnx == OnnxConfig(0, None)


@pytest.mark.parametrize("value", ["-1", "true", "1.5", '"4"', "2147483648"])
@pytest.mark.parametrize("name", ["intra_op_num_threads", "inter_op_num_threads"])
def test_config_rejects_invalid_threads(tmp_path, value, name):
    path = tmp_path / "server.toml"
    path.write_text(f"[onnx]\n{name} = {value}\n")
    with pytest.raises(ValueError, match=name):
        Settings.read(path)


@pytest.mark.parametrize("flag", ["--onnx-intra-op-num-threads", "--onnx-inter-op-num-threads"])
@pytest.mark.parametrize("value", ["-1", "1.5", "true", "2147483648"])
def test_cli_rejects_invalid_threads(flag, value):
    with pytest.raises(SystemExit) as result:
        cli.parser().parse_args([flag, value])
    assert result.value.code == 2


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ([], OnnxConfig(4, 1)),
        (["--onnx-intra-op-num-threads", "0"], OnnxConfig(0, 1)),
        (["--onnx-inter-op-num-threads", "2"], OnnxConfig(4, 2)),
        (
            ["--onnx-intra-op-num-threads", "2", "--onnx-inter-op-num-threads", "0"],
            OnnxConfig(2, 0),
        ),
    ],
)
def test_cli_precedence(tmp_path, monkeypatch, flags, expected):
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    path = tmp_path / "server.toml"
    path.write_text("[onnx]\nintra_op_num_threads = 4\ninter_op_num_threads = 1\n")
    captured = []
    monkeypatch.setattr(app, "create_app", lambda settings: captured.append(settings))
    monkeypatch.setattr(app, "accept_prefix", lambda application, prefix: application)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        sys, "argv", ["chirp-server", "--config", str(path), "--engine", "onnx", *flags]
    )
    cli.main()
    assert captured[0].onnx == expected


@pytest.mark.parametrize(
    "threads", [OnnxConfig(), OnnxConfig(4, 1), OnnxConfig(0), OnnxConfig(None, 2)]
)
def test_server_options_reach_loader(settings, tmp_path, monkeypatch, threads):
    # Real app selection and startup; only the external model loader is stubbed.
    (tmp_path / "encoder-model.int8.onnx").touch()
    loader = Mock(return_value=SimpleNamespace(recognize=Mock(return_value="")))
    monkeypatch.setitem(sys.modules, "onnx_asr", SimpleNamespace(load_model=loader))

    class SessionOptions:
        def __init__(self):
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0

    constructor = Mock(side_effect=SessionOptions)
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(SessionOptions=constructor))
    settings.engine = "onnx"
    settings.model_dir = tmp_path
    settings.onnx = threads
    with TestClient(app.create_app(settings, token=TOKEN)) as client:
        assert client.get("/v1/health", headers=HEADERS).json()["ready"]
    args, kwargs = loader.call_args
    assert args == ("nemo-conformer-tdt",)
    assert kwargs["path"] == tmp_path
    assert kwargs["quantization"] == "int8"
    assert kwargs["providers"] == ["CPUExecutionProvider"]
    if threads == OnnxConfig():
        constructor.assert_not_called()
        assert set(kwargs) == {"path", "quantization", "providers"}
    else:
        constructor.assert_called_once_with()
        options = kwargs.pop("sess_options")
        assert vars(options) == {key: value or 0 for key, value in asdict(threads).items()}
        assert set(kwargs) == {"path", "quantization", "providers"}


def test_onnx_imports_remain_lazy():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
class BlockOnnx:
    def find_spec(self, fullname, *args):
        if fullname in {'onnx_asr', 'onnxruntime'}:
            raise AssertionError('optional ONNX import: ' + fullname)
sys.meta_path.insert(0, BlockOnnx())
from chirp_server.cli import parser
from chirp_server.app import create_app
from chirp_server.config import OnnxConfig, Settings
from chirp_server.engine.onnx import ParakeetONNX
parser().parse_args([])
ParakeetONNX(intra_op_num_threads=4)
create_app(Settings(onnx=OnnxConfig(4, 1)), token='test')
""",
        ],
        check=True,
    )


def test_direct_engine_rejects_invalid_threads():
    with pytest.raises(ValueError, match="intra_op_num_threads"):
        ParakeetONNX(intra_op_num_threads=-1)
