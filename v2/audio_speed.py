"""Pass 2: fit each dubbed line to its slot by changing its speed with FFmpeg (pitch preserved).

After ElevenLabs (pass 1), the spoken part of each clip is measured (silence ElevenLabs adds before and
after the voice is not counted) and compared with the scripted slot, TCR OUT - TCR IN. A line more
than FIT_TOLERANCE_MS off is time-stretched by spoken / slot with FFmpeg's ``atempo``, held between
MIN_FIT_SPEED and MAX_FIT_SPEED: too-long lines are sped up, too-short lines are slowed down. No
second ElevenLabs request is made, so the performance is the same take.

Also used by the Dubbing Quality Check for its per-line speed control.
"""

from array import array
import io
import math
import shutil
import subprocess
import sys
import wave

MIN_FIT_SPEED = 0.70
MAX_FIT_SPEED = 1.50
FIT_TOLERANCE_MS = 100     # a line this close to its slot keeps its natural speed
SPEECH_WINDOW_MS = 10      # speech detection resolution
SILENCE_DB = 40            # quieter than this below the clip's loudest point counts as silence
MIN_SPEECH_PEAK = 64       # and never above this absolute level (about -54 dBFS)


def check_ffmpeg():
    """Fail before any ElevenLabs request if FFmpeg, needed for pass 2, is missing."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("FFmpeg must be installed on the server: it fits each line to its slot")


def validate_speed(value):
    error_message = f"Line speed must be a number from {MIN_FIT_SPEED:.2f}x to {MAX_FIT_SPEED:.2f}x"
    try:
        speed = float(value)
    except (TypeError, ValueError):
        raise ValueError(error_message) from None
    if not math.isfinite(speed) or not MIN_FIT_SPEED <= speed <= MAX_FIT_SPEED:
        raise ValueError(error_message)
    return speed


def change_speed(wav_bytes, speed):
    """Return the clip at ``speed`` with unchanged pitch; 1.00x returns the input unchanged."""
    speed = validate_speed(speed)
    if speed == 1.0:
        return wav_bytes
    check_ffmpeg()
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


def speech_span(wav_bytes):
    """(speech start ms, speech end ms, clip ms): where the voice is, ignoring leading/trailing silence."""
    with wave.open(io.BytesIO(wav_bytes)) as wav:
        rate, width = wav.getframerate(), wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError("expected 16-bit PCM audio")
    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    clip_ms = len(samples) * 1000 // rate
    window = max(1, rate * SPEECH_WINDOW_MS // 1000)
    peaks = [max(max(chunk), -min(chunk))
             for chunk in (samples[i:i + window] for i in range(0, len(samples), window))]
    threshold = max(MIN_SPEECH_PEAK, max(peaks, default=0) * 10 ** (-SILENCE_DB / 20))
    voiced = [i for i, peak in enumerate(peaks) if peak >= threshold]
    if not voiced:
        return 0, 0, clip_ms
    return voiced[0] * SPEECH_WINDOW_MS, min(clip_ms, (voiced[-1] + 1) * SPEECH_WINDOW_MS), clip_ms


def needed_speed(wav_bytes, slot_ms):
    """The speed at which the spoken part lasts ``slot_ms``, with no limits or rounding; 1.0 when the
    line is already within the tolerance (or silent). Quality Check shows it when a limit is reached."""
    start, end, _clip = speech_span(wav_bytes)
    spoken = end - start
    if spoken <= 0 or slot_ms <= 0 or abs(slot_ms - spoken) <= FIT_TOLERANCE_MS:
        return 1.0
    return spoken / slot_ms


def limit_speed(speed):
    """Hold a needed speed between the limits, rounded to 0.01x."""
    return round(min(MAX_FIT_SPEED, max(MIN_FIT_SPEED, speed)), 2)


def fit_speed(wav_bytes, slot_ms):
    """The speed that makes the spoken part last ``slot_ms``: spoken / slot, held between the limits,
    rounded to 0.01x; 1.00 when the line is already within the tolerance (or silent)."""
    return limit_speed(needed_speed(wav_bytes, slot_ms))


def fit_to_slot(wav_bytes, slot_ms):
    """Pass 2 for one line: ``(fitted wav, speed applied)``."""
    speed = fit_speed(wav_bytes, slot_ms)
    return change_speed(wav_bytes, speed), speed
