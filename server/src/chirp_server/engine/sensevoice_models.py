"""Pinned SenseVoice assets. Only the explicit model-fetch command uses the network."""

import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

from .models import model_root

REPOSITORY = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
REVISION = "2365baeacb507f821a0c8120fcee3d484dba7a07"
FILES = ("model.onnx", "tokens.txt", "silero_vad.onnx")
VAD_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
VAD_SHA256 = "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6"


def discover(explicit: Path | None = None) -> Path:
    directory = explicit if explicit is not None else model_root() / "sensevoice-small"
    if not all(
        (directory / name).is_file() and (directory / name).stat().st_size for name in FILES
    ):
        raise ValueError(
            "SenseVoice model incomplete; run chirp-server model fetch --engine sensevoice "
            "or use --model-dir with model.onnx, tokens.txt and silero_vad.onnx"
        )
    return directory


def fetch() -> Path:
    from huggingface_hub import snapshot_download

    directory = model_root() / "sensevoice-small"
    snapshot_download(
        REPOSITORY,
        revision=REVISION,
        local_dir=directory,
        allow_patterns=["model.onnx", "tokens.txt", "LICENSE"],
    )
    # The release URL is mutable, so pin its content too. Never install a partial
    # download or an unverified replacement over an existing VAD model.
    temporary = None
    try:
        digest = hashlib.sha256()
        with (
            urlopen(VAD_URL, timeout=60) as response,
            NamedTemporaryFile(dir=directory, prefix=".vad-", delete=False) as output,
        ):
            temporary = Path(output.name)
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != VAD_SHA256:
            raise ValueError("Silero VAD checksum mismatch; model was not installed")
        os.replace(temporary, directory / "silero_vad.onnx")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return discover(directory)
