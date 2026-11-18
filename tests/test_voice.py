import struct
import tempfile
import unittest
import wave
from pathlib import Path

from agentcal.voice import SttUnavailable, VoiceGate, VoiceIntake, wav_stats


def write_wav(path: Path, seconds: float, amplitude: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        frames = b"".join(struct.pack("<h", amplitude) for _ in range(int(16000 * seconds)))
        handle.writeframes(frames)


class FakeTelegram:
    def __init__(self, payload: bytes):
        self.payload = payload

    def get_file(self, file_id):
        return {"file_path": "voice/file.wav"}

    def download(self, path):
        return self.payload


class FakeStt:
    def __init__(self, text="明天十点巡检内网"):
        self.text = text
        self.calls = 0

    def transcribe(self, path):
        self.calls += 1
        return self.text


class VoiceTest(unittest.TestCase):
    def test_silence_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "silent.wav"
            write_wav(path, 1.0, 0)
            ok, reason = VoiceGate().check(path)
            self.assertFalse(ok)
            self.assertEqual(reason, "silence")

    def test_short_clip_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.wav"
            write_wav(path, 0.1, 8000)
            ok, reason = VoiceGate().check(path)
            self.assertFalse(ok)
            self.assertEqual(reason, "too_short")

    def test_loud_clip_passes_and_transcribes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.wav"
            write_wav(path, 0.6, 8000)
            duration, dbfs = wav_stats(path)
            self.assertAlmostEqual(duration, 0.6, places=2)
            self.assertGreater(dbfs, -45)
            stt = FakeStt()
            intake = VoiceIntake(FakeTelegram(path.read_bytes()), stt, tmp)
            message = {"voice": {"file_id": "f1"}}
            self.assertEqual(intake.transcribe_message(message), "明天十点巡检内网")
            self.assertEqual(stt.calls, 1)

    def test_gate_rejects_before_stt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "silent.wav"
            write_wav(path, 0.6, 0)
            stt = FakeStt()
            intake = VoiceIntake(FakeTelegram(path.read_bytes()), stt, tmp)
            self.assertIsNone(intake.transcribe_message({"voice": {"file_id": "f1"}}))
            self.assertEqual(stt.calls, 0)

    def test_missing_ffmpeg_is_reported(self):
        from unittest import mock

        from agentcal.voice import ensure_wav

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "a.ogg"
            source.write_bytes(b"fake")
            with mock.patch("agentcal.voice.shutil.which", return_value=None):
                with self.assertRaises(SttUnavailable):
                    ensure_wav(source)
