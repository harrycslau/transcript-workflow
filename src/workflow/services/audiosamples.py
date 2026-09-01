"""Audio probing and sample extraction for language routing.

All temporary artifacts are created beneath ``<storage.temp>/routing/``
and must be removed by the caller (``finally``). The source WAV is never
modified. No ffmpeg dependency: macOS built-in ``afinfo``/``afconvert``
plus the stdlib ``wave`` module.

Notes validated against macOS tools:
- ``afinfo`` output is plain text (``estimated duration:`` /
  ``Data format:`` lines); ``afinfo -r`` is NOT JSON.
- ``afconvert -f WAVE -d LEI16@16000 -c 1`` produces 16 kHz mono
  little-endian Int16 PCM WAV readable by ``wave``.
"""

from __future__ import annotations

import audioop  # stdlib on Python 3.12; removed in 3.13 (revisit then)
import logging
import re
import shutil
import struct
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

AFINFO_TIMEOUT = 15
AFCONVERT_TIMEOUT = 600
SAMPLE_RATE = 16000
# Silence heuristic: peak amplitude below this fraction of full scale.
SILENCE_PEAK_THRESHOLD = 250  # of 32767

# Sample windows: beginning, middle, end (seconds), clamped to duration.
WINDOW_SECONDS = 15.0


class SampleExtractionError(Exception):
    """Sample extraction failed; message is a stable short reason code."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(detail or reason_code)


@dataclass
class SampleBundle:
    pcm_path: Path
    duration_seconds: float
    windows: list[tuple[float, float]]
    sample_paths: list[Path] = field(default_factory=list)
    composite_path: Path | None = None
    # Per-window silence flags; a recording is only "silent" when EVERY
    # window is silent (a silent head with voiced middle/end is not silent).
    window_silence: list[bool] = field(default_factory=list)
    is_silent: bool = False


def probe_duration(path: Path) -> float | None:
    """Duration via stdlib ``wave``; None when not a readable PCM WAV."""
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            if rate <= 0:
                return None
            return frames / float(rate)
    except (wave.Error, OSError, EOFError):
        return None


def parse_afinfo_duration(output: str) -> float | None:
    """Extract ``estimated duration: X sec`` from plain-text afinfo output."""
    match = re.search(r"estimated duration:\s*([0-9.]+)\s*sec", output)
    return float(match.group(1)) if match else None


def afinfo_duration(path: Path) -> float | None:
    """Duration via macOS ``afinfo`` (plain text). None when unavailable."""
    if shutil.which("afinfo") is None:
        return None
    try:
        result = subprocess.run(
            ["afinfo", str(path)],
            capture_output=True,
            text=True,
            timeout=AFINFO_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return parse_afinfo_duration(result.stdout or "")


def get_duration(path: Path) -> float | None:
    return probe_duration(path) or afinfo_duration(path)


def is_pcm_wav(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getsampwidth() in (1, 2, 4) and handle.getnframes() > 0
    except (wave.Error, OSError, EOFError):
        return False


def convert_to_pcm_16k(source: Path, dest: Path) -> Path:
    """Convert any macOS-supported audio to 16 kHz mono Int16 PCM WAV."""
    afconvert = shutil.which("afconvert")
    if afconvert is None:
        raise SampleExtractionError("afconvert_unavailable")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Expected filesystem failure (permissions, full disk, ...):
        # stable reason, never a raw traceback.
        raise SampleExtractionError("temp_dir_unwritable", str(exc)[:200]) from None
    try:
        result = subprocess.run(
            [afconvert, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(source), str(dest)],
            capture_output=True,
            text=True,
            timeout=AFCONVERT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise SampleExtractionError("afconvert_timeout") from None
    except OSError:
        raise SampleExtractionError("afconvert_failed") from None
    if result.returncode != 0 or not dest.exists():
        raise SampleExtractionError("afconvert_failed", (result.stderr or "").strip()[:200])
    return dest


def _slice_windows(pcm_path: Path, windows: list[tuple[float, float]], out_paths: list[Path] | None = None) -> list[list[bytes]]:
    """Read time windows from a WAV; returns raw frame blobs per window.

    Works for any PCM width/channels; downmixes multi-channel frames.
    Returns frame data in window order (no temp slice files needed).
    """
    blobs: list[list[bytes]] = []
    try:
        with wave.open(str(pcm_path), "rb") as src:
            rate = src.getframerate()
            channels = src.getnchannels()
            width = src.getsampwidth()
            if rate <= 0:
                raise SampleExtractionError("unreadable_audio")
            for (start_s, end_s) in windows:
                start_frame = max(0, int(start_s * rate))
                end_frame = int(end_s * rate)
                src.setpos(min(start_frame, src.getnframes()))
                frames = src.readframes(max(0, end_frame - start_frame))
                if frames and channels > 1:
                    frames = audioop.tomono(frames, width, 0.5, 0.5)
                blobs.append([frames])
    except (wave.Error, OSError) as exc:
        raise SampleExtractionError("slice_failed", str(exc)[:200]) from None
    return blobs


def _write_wav(out_path: Path, frame_blobs: list[bytes], rate: int, width: int = 2) -> Path:
    """Write a WAV from concatenated mono frame blobs (chronological)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(width)
        dst.setframerate(rate)
        for blob in frame_blobs:
            dst.writeframes(blob)
    return out_path


def _frames_peak_silence(frame_blobs: list[bytes], width: int = 2) -> bool:
    """True when the frames' peak amplitude indicates digital silence."""
    peak = 0
    for blob in frame_blobs:
        if width != 2:
            return False
        values = struct.unpack(f"<{len(blob) // 2}h", blob) if blob else ()
        if values:
            peak = max(peak, max(abs(v) for v in values))
    return peak < SILENCE_PEAK_THRESHOLD


def extract_samples(source: Path, attempt_dir: Path) -> SampleBundle:
    """Extract beginning/middle/end samples beneath ``attempt_dir``.

    Builds one composite WAV containing every window in chronological
    order (preserving window order) so routing candidates receive
    evidence from all windows. Silence is evaluated per window; the
    recording is silent only when every window is silent.

    Raises :class:`SampleExtractionError` with a stable reason code when
    the audio is unreadable, silent, too short, or conversion fails.
    All files live under ``attempt_dir``; the caller removes the whole
    directory afterwards.
    """
    attempt_dir.mkdir(parents=True, exist_ok=True)
    duration = get_duration(source)
    if duration is None:
        # Try converting (covers non-PCM WAV variants); the converted
        # file also gives us a reliable duration.
        converted = attempt_dir / "converted.wav"
        try:
            convert_to_pcm_16k(source, converted)
        except SampleExtractionError:
            raise SampleExtractionError("unreadable_audio") from None
        pcm_path = converted
        duration = probe_duration(converted)
    elif is_pcm_wav(source):
        pcm_path = source  # opened read-only; the source is never written
        converted = None
    else:
        converted = attempt_dir / "converted.wav"
        convert_to_pcm_16k(source, converted)
        pcm_path = converted
        duration = probe_duration(converted)

    if duration is None:
        raise SampleExtractionError("unreadable_audio")
    if duration < 3.0:
        raise SampleExtractionError("too_short")

    window = min(WINDOW_SECONDS, duration / 3.0)
    windows = [
        (0.0, window),
        (max(0.0, duration / 2.0 - window / 2.0), duration / 2.0 + window / 2.0),
        (max(0.0, duration - window), duration),
    ]
    # Merge overlapping windows for very short recordings.
    merged: list[tuple[float, float]] = []
    for start_s, end_s in windows:
        if merged and start_s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_s))
        else:
            merged.append((start_s, end_s))

    window_blobs = _slice_windows(pcm_path, merged)
    frame_blobs = [blob for group in window_blobs for blob in group]
    rate = probe_rate(pcm_path) or SAMPLE_RATE
    composite_path = _write_wav(attempt_dir / "composite.wav", frame_blobs, rate)

    window_silence = [_frames_peak_silence(group) for group in window_blobs]
    return SampleBundle(
        pcm_path=pcm_path,
        duration_seconds=duration,
        windows=merged,
        composite_path=composite_path,
        window_silence=window_silence,
        is_silent=all(window_silence) if window_silence else False,
    )


def probe_rate(path: Path) -> int | None:
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getframerate()
    except (wave.Error, OSError, EOFError):
        return None


def cleanup_attempt_dir(attempt_dir: Path) -> None:
    """Remove a routing attempt's temporary directory (best-effort)."""
    shutil.rmtree(attempt_dir, ignore_errors=True)
