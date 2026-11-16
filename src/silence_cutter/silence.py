"""FFmpeg-backed silence analysis."""

from __future__ import annotations

import re
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Interval:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def parse_silencedetect(stderr: str, duration: float | None = None) -> list[Interval]:
    """Parse FFmpeg silencedetect output into clamped silent intervals."""
    starts: list[float] = []
    result: list[Interval] = []
    for line in stderr.splitlines():
        start = _START.search(line)
        end = _END.search(line)
        if start:
            starts.append(max(0.0, float(start.group(1))))
        if end:
            right = float(end.group(1))
            if starts:
                left = starts.pop(0)
                right = max(left, right)
                if duration is not None:
                    left, right = min(left, duration), min(right, duration)
                if right > left:
                    result.append(Interval(left, right))
    if starts and duration is not None:
        left = min(starts[0], duration)
        if duration > left:
            result.append(Interval(left, duration))
    return result


def detect_silence(media: str | Path, *, noise_db: float = -40.0,
                   min_duration: float = 0.5,
                   duration: float | None = None) -> list[Interval]:
    """Run ffmpeg silencedetect on a media file; errors include ffmpeg diagnostics."""
    command = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(media), "-vn",
               "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
               "-f", "null", "-"]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg was not found; install ffmpeg and ensure it is on PATH") from exc
    if proc.returncode:
        raise RuntimeError(f"FFmpeg analysis failed: {proc.stderr.strip()[-2000:]}")
    if duration is None:
        duration = probe_duration(media)
    return parse_silencedetect(proc.stderr, duration=duration)


def probe_duration(media: str | Path) -> float:
    """Return media duration using ffprobe."""
    command = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "json", str(media)]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe was not found; install ffmpeg and ensure it is on PATH") from exc
    if proc.returncode:
        raise RuntimeError(f"ffprobe failed: {proc.stderr.strip()[-2000:]}")
    try:
        duration = float(json.loads(proc.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("ffprobe did not return a valid media duration") from exc
    if duration <= 0:
        raise RuntimeError("Media duration is not positive")
    return duration
