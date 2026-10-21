"""Opt-in cached-model checks. No downloads, microphones or private fixtures."""

import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from conftest import HEADERS, START, TOKEN
from starlette.testclient import TestClient

from chirp_server.app import create_app
from chirp_server.audio import decode_wav, samples, wav_bytes
from chirp_server.engine.sensevoice import SenseVoice

pytestmark = pytest.mark.model


@pytest.fixture(scope="module")
def assets():
    model = os.environ.get("CHIRP_SENSEVOICE_MODEL_DIR")
    fixtures = os.environ.get("CHIRP_SENSEVOICE_FIXTURES")
    if not model or not fixtures:
        pytest.skip(
            "set CHIRP_SENSEVOICE_MODEL_DIR and CHIRP_SENSEVOICE_FIXTURES for cached public assets"
        )
    # Explicitly supplied model assets mean this backend must work. An inherited
    # native-library override must not hide an incomplete optional installation.
    environment = {
        k: v for k, v in os.environ.items() if k not in {"LD_LIBRARY_PATH", "LD_PRELOAD"}
    }
    subprocess.run(
        [sys.executable, "-I", "-c", "import sherpa_onnx"],
        env=environment,
        check=True,
        capture_output=True,
    )
    paths = [Path(fixtures) / name for name in ("zh.wav", "jfk.wav")]
    expected = [
        "b77f1794fe374a0ba1ee1dc458bfaf9349496cbbfc32780c50ba3c5a7ad8e373",
        "59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e",
    ]
    for path, digest in zip(paths, expected, strict=True):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, (
            "use documented public fixtures"
        )
    return Path(model), [decode_wav(path.read_bytes()) for path in paths]


def normalized(text):
    return re.sub(r"[\W_]", "", text).lower()


@pytest.mark.parametrize("client_name", ["mac", "android"])
def test_public_chinese_english_and_language_switching_socket(settings, assets, client_name):
    model, (zh, en) = assets
    gap = bytes(16000)  # half a second, PCM16
    clips = [zh, en, zh + gap + en + gap + zh]
    engine = SenseVoice(model)
    settings.engine = "sensevoice"
    settings.idle_seconds = 60
    settings.auth_seconds = 5
    settings.queue_timeout = 10
    with TestClient(create_app(settings, engine, TOKEN)) as client:
        for index, pcm in enumerate(clips):
            response = client.post("/v1/transcribe", headers=HEADERS, content=wav_bytes(pcm))
            assert response.status_code == 200
            reference = response.json()["raw"]
            with client.websocket_connect("/v1/stream", headers=HEADERS) as ws:
                ws.send_json({**START, "client": client_name, "word_timings": True})
                assert ws.receive_json()["type"] == "ready"
                for offset in range(0, len(pcm), 6400):
                    ws.send_bytes(pcm[offset : offset + 6400])
                    time.sleep(0.2)
                ws.send_json({"type": "stop"})
                partials = []
                while True:
                    message = ws.receive_json()
                    if message["type"] == "partial":
                        partials.append(message)
                    elif message["type"] == "final":
                        break
                    else:
                        pytest.fail("unexpected socket message")
            # Transport consistency is separate from recognition quality below.
            assert normalized(message["raw"]) == normalized(reference)
            assert message["text"] == message["raw"] and not message["corrected"]
            assert "words" not in message
            assert partials and all(p["stable"] == "" and "words" not in p for p in partials)
            assert all(a["seq"] < b["seq"] for a, b in zip(partials, partials[1:], strict=False))
            text = normalized(message["raw"])
            if index != 1:
                # Mandarin sample: Simplified 时/间/点 and two occurrences in the
                # mixed clip. This does not accept an English-only final.
                assert text.count("时间早上9点至下午5点") == (2 if index == 2 else 1)
                assert "時" not in text and "點" not in text
            if index != 0:
                assert "myfellowamericans" in text and "yourcountry" in text
        # Silence and a new session after cancellation exercise the real detector
        # reset and native decoder, not only a text-engine mock.
        assert engine.transcribe(np.zeros(32000, dtype=np.float32)) == ""
        stream = engine.open_stream()
        stream.feed(samples(zh))
        stream.cancel()
        stream.buffer(samples(en))
        assert "myfellowamericans" in normalized(stream.finish().text)
