"""Opt-in word timings: token grouping, stream offsets and the additive protocol field."""

import numpy as np
import pytest
from conftest import HEADERS, PCM, START, TOKEN
from starlette.testclient import TestClient
from test_protocol import final

from chirp_server.app import create_app
from chirp_server.config import CorrectionConfig
from chirp_server.correction import CorrectionResult
from chirp_server.engine import FakeEngine, Transcript, Word
from chirp_server.engine.mlx import RollingStream
from chirp_server.engine.words import shift, tokens_to_words

TIMED = {**START, "word_timings": True}


def piece(text, start, duration=0.08, id=0):
    alignment = pytest.importorskip("parakeet_mlx.alignment")
    return alignment.AlignedToken(id, text, start, duration)


def test_pieces_group_into_words_at_sentencepiece_boundaries():
    tokens = [
        piece(" hel", 0.0),
        piece("lo", 0.08),
        piece(",", 0.16, 0.0),
        piece(" world", 0.4, 0.2),
        piece(".", 0.6, 0.0),
    ]
    words = tokens_to_words(tokens)
    assert [w.text for w in words] == ["hello,", "world."]
    assert words[0] == Word("hello,", 0.0, 0.16)
    assert words[1].text == "world." and words[1].start == 0.4
    assert words[1].end == pytest.approx(0.6)
    assert " ".join(w.text for w in words) == "".join(t.text for t in tokens).strip()


def test_lone_space_piece_and_leading_punctuation_do_not_make_empty_words():
    tokens = [piece(" ", 0.0, 0.0), piece("a", 0.0), piece(" ", 0.2, 0.0), piece("b", 0.24)]
    assert [w.text for w in tokens_to_words(tokens)] == ["a", "b"]
    assert tokens_to_words([piece(" ", 0.0, 0.0)]) == []
    assert tokens_to_words([]) == []


def test_offset_and_stable_threshold():
    tokens = [piece(" one", 0.0, 0.2), piece(" two", 1.0, 0.2), piece(" three", 2.0, 0.2)]
    words = tokens_to_words(tokens, offset=10.0, stable_before=11.5)
    assert [(w.start, w.end, w.stable) for w in words] == [
        (10.0, 10.2, True),
        (11.0, 11.2, True),
        (12.0, 12.2, False),
    ]
    assert tokens_to_words(tokens, stable_before=None)[0].stable is False
    assert shift(words, 1.0, stable=False)[0] == Word("one", 11.0, 11.2, False)


def test_payload_rounds_to_milliseconds():
    assert Word("hi", 0.123456, 0.2, True).payload() == {
        "text": "hi",
        "start": 0.123,
        "end": 0.2,
        "stable": True,
    }


def test_zero_duration_piece_never_ends_before_it_starts():
    words = tokens_to_words([piece(" x", 1.0, 0.0)])
    assert words == [Word("x", 1.0, 1.0)]


class TimedEngine:
    """A segments-mode engine whose final decode times each word at 100 ms per word."""

    finalization = "segments"

    def transcribe(self, pcm):
        return self.transcribe_timed(pcm).text

    def transcribe_timed(self, pcm):
        if len(pcm) == 0:
            return Transcript("", ())
        text = "first sentence." if np.max(pcm) < 0.2 else "second one"
        words = tuple(Word(w, i * 0.1, i * 0.1 + 0.08) for i, w in enumerate(text.split()))
        return Transcript(text, words)


def test_segment_words_keep_utterance_relative_times():
    stream = RollingStream(TimedEngine())
    stream.feed(np.full(16000, 0.1, dtype=np.float32))
    committed = stream.feed(np.zeros(6400, dtype=np.float32))
    assert committed.text == "first sentence."
    # Committed words are frozen for the rest of the utterance.
    assert [w.stable for w in committed.words] == [True, True]
    assert stream.base == 22400
    live = stream.feed(np.full(8000, 0.3, dtype=np.float32))
    # The text-only preview path has no timings for the tail, so no words at all.
    assert live.words is None
    result = stream.finish()
    assert result.text == "first sentence. second one"
    # The tail's words are shifted by the 22400 samples cut off the front.
    assert [(w.text, w.start, w.stable) for w in result.words] == [
        ("first", 0.0, True),
        ("sentence.", 0.1, True),
        ("second", 1.4, True),
        ("one", 1.5, True),
    ]
    stream.cancel()
    assert stream.committed_words == [] and stream.base == 0


def test_untimed_engine_yields_no_words():
    stream = RollingStream(FakeEngine(timed=False))
    assert stream.finish().words is None


def test_fake_engine_words_follow_its_text():
    engine = FakeEngine("a b c")
    assert [w.text for w in engine.words] == ["a", "b", "c"]
    assert engine.words[2] == Word("c", 0.6, 0.85)
    assert FakeEngine(timed=False).words is None


def test_partials_and_final_carry_words_only_when_asked(client, engine):
    with client.websocket_connect("/v1/stream", headers=HEADERS) as ws:
        ws.send_json(TIMED)
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(PCM)
        partial = ws.receive_json()
        assert partial["type"] == "partial" and partial["seq"] == 1
        assert partial["words"] == [w.payload() for w in engine.words]
        assert [w["text"] for w in partial["words"]] == partial["text"].split()
        assert all(w["stable"] is False for w in partial["words"])
        ws.send_json({"type": "stop"})
        result = final(ws)
        assert result["raw"] == engine.text
        assert [w["text"] for w in result["words"]] == result["raw"].split()
        assert all(w["stable"] is True for w in result["words"])

        # The same socket without the flag: byte-for-byte the old messages.
        ws.send_json(START)
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(PCM)
        assert ws.receive_json() == {"type": "partial", "seq": 1, "text": engine.text, "stable": ""}
        ws.send_json({"type": "stop"})
        assert "words" not in final(ws)


def test_flag_off_is_the_default_and_post_never_has_words(client, wav):
    with client.websocket_connect("/v1/stream", headers=HEADERS) as ws:
        ws.send_json({**START, "word_timings": False})
        ws.receive_json()
        ws.send_bytes(PCM)
        assert "words" not in ws.receive_json()
        ws.send_json({"type": "stop"})
        assert "words" not in final(ws)
    assert "words" not in client.post("/v1/transcribe", headers=HEADERS, content=wav).json()


@pytest.mark.parametrize("value", ["true", 1, None, [True]])
def test_non_boolean_flag_is_a_bad_request(client, value):
    with client.websocket_connect("/v1/stream", headers=HEADERS) as ws:
        ws.send_json({**START, "word_timings": value})
        assert ws.receive_json()["code"] == "bad_audio"


def test_engine_without_timings_omits_words_even_when_asked(settings):
    with TestClient(create_app(settings, FakeEngine(timed=False), TOKEN)) as client:
        with client.websocket_connect("/v1/stream", headers=HEADERS) as ws:
            ws.send_json(TIMED)
            ws.receive_json()
            ws.send_bytes(PCM)
            assert "words" not in ws.receive_json()
            ws.send_json({"type": "stop"})
            assert "words" not in final(ws)


def test_empty_final_has_an_empty_word_list(client):
    with client.websocket_connect("/v1/stream", headers=HEADERS) as ws:
        ws.send_json(TIMED)
        ws.receive_json()
        ws.send_json({"type": "stop"})
        assert final(ws)["words"] == []


def test_correcting_carries_the_raw_words_and_the_final_keeps_them(settings):
    class Corrector:
        async def process(self, raw, profile="default", instructions="", prompt=None):
            return CorrectionResult("Synthetic dictation test!", corrected=True)

    settings.correction = CorrectionConfig(mode="endpoint")
    engine = FakeEngine()
    with TestClient(create_app(settings, engine, TOKEN, Corrector())) as client:
        with client.websocket_connect("/v1/stream", headers=HEADERS) as ws:
            ws.send_json({**TIMED, "correction": True})
            ws.receive_json()
            ws.send_bytes(PCM)
            ws.receive_json()
            ws.send_json({"type": "stop"})
            correcting = ws.receive_json()
            assert correcting["type"] == "correcting"
            assert [w["text"] for w in correcting["words"]] == correcting["raw"].split()
            result = final(ws)
            assert result["corrected"] and result["text"] != result["raw"]
            # Timings always describe `raw`, never the cleaned text.
            assert [w["text"] for w in result["words"]] == result["raw"].split()
