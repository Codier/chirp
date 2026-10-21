"""Local CPU SenseVoice with automatic language detection and phrase-sized decodes."""

import re

import numpy as np

from chirp_server.audio import RATE, silent

from .base import Partial, Transcript
from .sensevoice_models import discover

MAX_SAMPLES = 20 * RATE
MIN_CONTEXT = 4 * RATE
CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def join_text(left: str, right: str) -> str:
    left, right = left.strip(), right.strip()
    if not left or not right:
        return left or right
    separator = "" if CJK.search(left[-1] + right[0]) else " "
    return left + separator + right


class SenseVoice:
    name = "sensevoice-small:fp32-cpu:auto"

    def __init__(self, model_dir=None, num_threads=4):
        if type(num_threads) is not int or not 1 <= num_threads <= 256:
            raise ValueError("SenseVoice num_threads must be an integer between 1 and 256")
        self.model_dir = model_dir
        self.num_threads = num_threads
        self.model = self.vad = None

    def load(self):
        directory = discover(self.model_dir)
        try:
            import sherpa_onnx
        except ImportError:
            raise ValueError(
                "SenseVoice requires the optional chirp-server[sensevoice] extra"
            ) from None

        self.model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(directory / "model.onnx"),
            tokens=str(directory / "tokens.txt"),
            num_threads=self.num_threads,
            provider="cpu",
            language="auto",
            use_itn=True,
            debug=False,
        )
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(directory / "silero_vad.onnx")
        config.silero_vad.min_silence_duration = 0.25
        config.silero_vad.min_speech_duration = 0.1
        config.silero_vad.max_speech_duration = 20.0
        config.sample_rate = RATE
        config.num_threads = 1
        config.provider = "cpu"
        self.window_size = config.silero_vad.window_size
        self.vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)

    def decode(self, pcm):
        stream = self.model.create_stream()
        stream.accept_waveform(RATE, pcm)
        self.model.decode_stream(stream)
        return stream.result.text.strip()

    def warm(self):
        self.decode(np.zeros(8000, dtype=np.float32))

    def cuts(self, pcm):
        """Use pauses for cuts, retaining every original sample exactly once.

        Short phrases share language-detection context. A hard 20 s limit also
        bounds uninterrupted speech, where a cut may bisect a word.
        """
        self.vad.reset()
        speech = []

        def drain():
            while not self.vad.empty():
                segment = self.vad.front
                speech.append((segment.start, segment.start + len(segment.samples)))
                self.vad.pop()

        for start in range(0, len(pcm), self.window_size):
            chunk = pcm[start : start + self.window_size]
            if len(chunk) < self.window_size:
                chunk = np.pad(chunk, (0, self.window_size - len(chunk)))
            self.vad.accept_waveform(chunk)
            drain()
        self.vad.flush()
        drain()
        if not speech:
            return []
        pauses = [
            (previous[1] + following[0]) // 2
            for previous, following in zip(speech, speech[1:], strict=False)
        ]
        cuts = [0]
        for cut in pauses:
            if cut - cuts[-1] >= MIN_CONTEXT and len(pcm) - cut >= 2 * RATE:
                while cut - cuts[-1] > MAX_SAMPLES:
                    cuts.append(cuts[-1] + MAX_SAMPLES)
                cuts.append(cut)
        while len(pcm) - cuts[-1] > MAX_SAMPLES:
            cuts.append(cuts[-1] + MAX_SAMPLES)
        return cuts + [len(pcm)]

    def render(self, pcm, cache):
        if len(pcm) < 1600 or silent(pcm):
            return ""
        cuts = self.cuts(pcm)
        text = ""
        for start, end in zip(cuts, cuts[1:], strict=False):
            key = (start, end)
            if key not in cache:
                chunk = pcm[start:end]
                decoded = "" if silent(chunk) else self.decode(chunk)
                # The last window changes with each feed. Cache only completed
                # windows; no text-based deduplication can erase repeated phrases.
                if end != len(pcm):
                    cache[key] = decoded
            else:
                decoded = cache[key]
            text = join_text(text, decoded)
        # Keep only the current partition if a provisional VAD boundary moved.
        live = set(zip(cuts, cuts[1:], strict=False))
        for key in list(cache):
            if key not in live:
                del cache[key]
        return text

    def transcribe(self, pcm):
        return self.render(pcm, {})

    def open_stream(self):
        return SenseVoiceStream(self)

    def close(self):
        self.model = self.vad = None


class SenseVoiceStream:
    """Chronological phrase previews without matching overlapping text windows."""

    def __init__(self, engine):
        self.engine = engine
        self.parts = []
        self.cache = {}

    def buffer(self, pcm):
        self.parts.append(pcm)

    def feed(self, pcm):
        self.buffer(pcm)
        return Partial(self.engine.render(np.concatenate(self.parts), self.cache))

    def finish(self):
        audio = np.concatenate(self.parts) if self.parts else np.zeros(0, dtype=np.float32)
        try:
            # Always re-decode the retained audio for the authoritative final.
            return Transcript(self.engine.transcribe(audio))
        finally:
            self.cancel()

    def cancel(self):
        self.parts.clear()
        self.cache.clear()
