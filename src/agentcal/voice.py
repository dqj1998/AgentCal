"""Voice intake: Telegram voice -> ffmpeg -> VAD gate -> whisper.cpp -> text."""
from __future__ import annotations

import logging
import math
import shutil
import subprocess
import wave
from pathlib import Path

log = logging.getLogger(__name__)


class SttUnavailable(RuntimeError):
    pass


def wav_stats(path: str | Path) -> tuple[float, float]:
    """Return (duration_s, rms_dbfs). Raises ValueError for non-PCM input."""
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate() or 1
        width = handle.getsampwidth()
        raw = handle.readframes(frames)
    if width != 2:
        raise ValueError("expected 16-bit PCM wav")
    samples = memoryview(raw).cast("h")
    if len(samples) == 0:
        return 0.0, -120.0
    acc = sum(int(value) * int(value) for value in samples) / len(samples)
    rms = acc ** 0.5
    return frames / rate, (20 * math.log10(rms / 32768.0) if rms > 0 else -120.0)


class VoiceGate:
    def __init__(self, min_duration_s: float = 0.4, min_rms_dbfs: float = -45.0):
        self.min_duration_s = min_duration_s
        self.min_rms_dbfs = min_rms_dbfs

    def check(self, path: str | Path) -> tuple[bool, str]:
        try:
            duration, dbfs = wav_stats(path)
        except (ValueError, wave.Error) as exc:
            return False, f"unreadable_audio:{exc}"
        if duration < self.min_duration_s:
            return False, "too_short"
        if dbfs < self.min_rms_dbfs:
            return False, "silence"
        return True, "ok"


class WhisperCpp:
    def __init__(self, binary: str = "whisper-cli", model: str | None = None, timeout_s: float = 120):
        self.binary = binary
        self.model = model
        self.timeout_s = timeout_s

    def available(self) -> bool:
        return bool(shutil.which(self.binary) and self.model and Path(self.model).exists())

    def transcribe(self, wav_path: str | Path) -> str:
        if not self.available():
            raise SttUnavailable("whisper.cpp binary or model missing")
        out_base = str(Path(wav_path).with_suffix(""))
        command = [self.binary, "-m", self.model, "-f", str(wav_path), "-nt", "-otxt", "-of", out_base]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=self.timeout_s)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise SttUnavailable(f"whisper failed: {type(exc).__name__}") from exc
        text_path = Path(out_base + ".txt")
        if not text_path.exists():
            raise SttUnavailable("whisper produced no text")
        return text_path.read_text(encoding="utf-8", errors="ignore").strip()


def ensure_wav(source: str | Path, target: str | Path | None = None) -> Path:
    source = Path(source)
    target = Path(target) if target else source.with_suffix(".wav")
    if source.suffix.lower() == ".wav":
        return source
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SttUnavailable("ffmpeg missing; cannot convert telegram voice to wav")
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(source), "-ar", "16000", "-ac", "1", str(target)],
        check=True,
        timeout=120,
    )
    return target


class VoiceIntake:
    def __init__(self, client, stt: WhisperCpp, workdir: str | Path, gate: VoiceGate | None = None):
        self.client = client
        self.stt = stt
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.gate = gate or VoiceGate()

    def transcribe_message(self, message: dict) -> str | None:
        voice = message.get("voice") or message.get("audio")
        if not voice:
            return None
        info = self.client.get_file(voice["file_id"])
        file_path = info.get("file_path")
        if not file_path:
            raise SttUnavailable("telegram getFile returned no path")
        suffix = Path(file_path).suffix or ".oga"
        raw = self.workdir / f"{voice['file_id']}{suffix}"
        raw.write_bytes(self.client.download(file_path))
        wav = ensure_wav(raw)
        ok, reason = self.gate.check(wav)
        if not ok:
            log.info("voice rejected: %s", reason)
            return None
        return self.stt.transcribe(wav)
