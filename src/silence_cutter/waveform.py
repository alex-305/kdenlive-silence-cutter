"""Cached low-resolution audio envelopes for the Qt waveform preview."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import tempfile
from array import array
from dataclasses import dataclass
from pathlib import Path

from .silence import Interval, detect_silence, parse_silencedetect

SAMPLE_RATE = 8000
FRAMES_PER_BUCKET = 8  # one envelope sample per millisecond
_RECORD = struct.Struct("<hhhh")  # left min/max, right min/max


@dataclass
class WaveformData:
    sample_rate: int
    frames_per_bucket: int
    bucket_count: int
    levels: list[array]

    @property
    def duration(self) -> float:
        return self.bucket_count * self.frames_per_bucket / self.sample_rate


def _cache_path(media: Path) -> Path:
    stat = media.stat()
    identity = f"{media.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{SAMPLE_RATE}:{FRAMES_PER_BUCKET}"
    digest = hashlib.sha256(identity.encode()).hexdigest()
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "kdenlive-silence-cutter" / "waveforms" / f"{digest}.pcmpeaks"


def _make_levels(base: array) -> list[array]:
    """Build a min/max pyramid so zoomed-out painting stays bounded by pixels."""
    levels = [base]
    previous = base
    count = len(previous) // 4
    while count > 1:
        next_level = array("h")
        for index in range(0, count - 1, 2):
            left = index * 4
            right = (index + 1) * 4
            next_level.extend((min(previous[left], previous[right]),
                               max(previous[left + 1], previous[right + 1]),
                               min(previous[left + 2], previous[right + 2]),
                               max(previous[left + 3], previous[right + 3])))
        if count % 2:
            next_level.extend(previous[(count - 1) * 4:count * 4])
        levels.append(next_level)
        previous = next_level
        count = len(previous) // 4
    return levels


def _cache_read(path: Path) -> WaveformData | None:
    try:
        payload = path.read_bytes()
        if len(payload) < 24 or payload[:8] != b"SCWAVE01":
            return None
        sample_rate, frames_per_bucket, count = struct.unpack_from("<IIQ", payload, 8)
        raw = payload[24:]
        if len(raw) != count * _RECORD.size:
            return None
        base = array("h")
        base.frombytes(raw)
        if struct.pack("=h", 1) != struct.pack("<h", 1):
            base.byteswap()
        return WaveformData(sample_rate, frames_per_bucket, count, _make_levels(base))
    except (OSError, ValueError, struct.error):
        return None


def _cache_write(path: Path, base: array, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = array("h", base)
    if struct.pack("=h", 1) != struct.pack("<h", 1):
        values.byteswap()
    header = b"SCWAVE01" + struct.pack("<IIQ", SAMPLE_RATE, FRAMES_PER_BUCKET, count)
    fd, temp_name = tempfile.mkstemp(prefix="waveform-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(header)
            stream.write(values.tobytes())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _decode(media: Path, noise_db: float, min_duration: float) -> tuple[array, list[Interval]]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostats", "-i", str(media),
               "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
               "-ac", "2", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]
    try:
        with tempfile.TemporaryFile() as stderr_file:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr_file)
            if proc.stdout is None:
                raise RuntimeError("Could not read FFmpeg waveform output")
            pending = bytearray()
            peaks = array("h")
            while chunk := proc.stdout.read(1024 * 1024):
                pending.extend(chunk)
                available = len(pending) // (FRAMES_PER_BUCKET * 4)
                offset = 0
                for _ in range(available):
                    samples = struct.unpack_from("<16h", pending, offset)
                    left_values = samples[0::2]
                    right_values = samples[1::2]
                    peaks.extend((min(left_values), max(left_values),
                                  min(right_values), max(right_values)))
                    offset += FRAMES_PER_BUCKET * 4
                if offset:
                    del pending[:offset]
            proc.stdout.close()
            return_code = proc.wait()
            stderr_file.seek(0)
            diagnostic = stderr_file.read().decode("utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg was not found; install ffmpeg and ensure it is on PATH") from exc
    if return_code:
        raise RuntimeError(f"FFmpeg waveform analysis failed: {diagnostic.strip()[-2000:]}")
    # Preserve a final partial bucket rather than dropping the tail of the audio.
    complete_frames = len(pending) // 4
    if complete_frames:
        samples = struct.unpack_from(f"<{complete_frames * 2}h", pending)
        left_values, right_values = samples[0::2], samples[1::2]
        peaks.extend((min(left_values), max(left_values), min(right_values), max(right_values)))
    duration = (len(peaks) // 4) * FRAMES_PER_BUCKET / SAMPLE_RATE
    return peaks, parse_silencedetect(diagnostic, duration=duration)


def analyze_with_waveform(media: str | Path, *, noise_db: float = -40.0,
                          min_duration: float = 0.5) -> tuple[WaveformData, list[Interval]]:
    """Return waveform peaks and silences, decoding the source once on cache miss."""
    media_path = Path(media).expanduser().resolve()
    cache = _cache_path(media_path)
    waveform = _cache_read(cache)
    if waveform is not None:
        return waveform, detect_silence(media_path, noise_db=noise_db,
                                        min_duration=min_duration,
                                        duration=waveform.duration)
    peaks, silences = _decode(media_path, noise_db, min_duration)
    count = len(peaks) // 4
    if not count:
        raise RuntimeError("FFmpeg produced no audio samples for the waveform")
    _cache_write(cache, peaks, count)
    return WaveformData(SAMPLE_RATE, FRAMES_PER_BUCKET, count, _make_levels(peaks)), silences
