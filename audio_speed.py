"""Pitch-preserving tempo adjustment for individual Gemini WAV clips."""

import math
import shutil
import subprocess

# Shared upper limit for backend validation and the website speed dial.
MAX_SPEED_RATE = 1.20


def validate_speed(value):
    error_message = f"Speed dial must be a number from 1.00x to {MAX_SPEED_RATE:.2f}x"
    try:
        speed = float(value)
    except (TypeError, ValueError):
        raise ValueError(error_message) from None
    if not math.isfinite(speed) or not 1.0 <= speed <= MAX_SPEED_RATE:
        raise ValueError(error_message)
    return speed


def check_speed_support(speed):
    """Fail before generating speech if the required executable is missing."""
    speed = validate_speed(speed)
    if speed > 1.0 and shutil.which("ffmpeg") is None:
        raise RuntimeError("Speed adjustment requires FFmpeg installed on the server")
    return speed


def adjust_audio_speed(wav_bytes, speed=1.0):
    """Return a faster WAV with unchanged pitch; 1.00x is an exact bypass."""
    speed = check_speed_support(speed)
    if speed == 1.0:
        return wav_bytes
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", "pipe:0", "-af", f"atempo={speed}",
             "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le",
             "-f", "wav", "pipe:1"],
            input=wav_bytes, capture_output=True, check=True, timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Audio speed adjustment timed out") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("FFmpeg could not adjust the dialogue speed") from exc
    if not result.stdout:
        raise RuntimeError("FFmpeg returned no audio after speed adjustment")
    return result.stdout
