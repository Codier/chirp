"""Explicit model acquisition. Server startup never downloads anything."""

import json
from pathlib import Path

REPOSITORY = "mlx-community/parakeet-tdt-0.6b-v2"
REVISION = "8ae155301e23d820d82aa60d24817c900e69e487"
FILES = ["config.json", "model.safetensors", "tokenizer.model", "vocab.txt"]


def model_root() -> Path:
    return Path.home() / "Library/Application Support/Chirp/models"


def valid_tdt(directory: Path) -> bool:
    try:
        config = json.loads((directory / "config.json").read_text())
        # Both English v2 and multilingual v3 use this TDT interface.
        return (
            all((directory / name).is_file() for name in FILES)
            and config["sample_rate"] == 16000
            and config["decoder"]["vocab_size"] > 0
            and config["model_defaults"]["tdt_durations"] == [0, 1, 2, 3, 4]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def valid_v2(directory: Path) -> bool:
    return (
        valid_tdt(directory)
        and json.loads((directory / "config.json").read_text())["decoder"]["vocab_size"] == 1024
    )


def discover(explicit: Path | None = None) -> Path:
    candidates = (
        [explicit]
        if explicit
        else [
            Path.home() / ".koi/voice-models/parakeet-mlx",
            model_root() / "parakeet-mlx",
        ]
    )
    for directory in candidates:
        if directory and (valid_tdt(directory) if explicit else valid_v2(directory)):
            return directory
    raise ValueError("English Parakeet v2 model missing; run chirp-server model fetch")


def fetch(engine: str = "mlx") -> Path:
    if engine == "sensevoice":
        from .sensevoice_models import fetch as fetch_sensevoice

        return fetch_sensevoice()
    from huggingface_hub import snapshot_download

    directory = model_root() / f"parakeet-{engine}"
    if engine == "onnx":
        snapshot_download(
            "istupakov/parakeet-tdt-0.6b-v2-onnx",
            revision="0bbb45a3365852604aef28b538a8f066f4ccaa85",
            local_dir=directory,
            allow_patterns=["config.json", "vocab.txt", "*-model.int8.onnx", "nemo128.onnx"],
        )
    else:
        snapshot_download(REPOSITORY, revision=REVISION, local_dir=directory, allow_patterns=FILES)
    return directory
