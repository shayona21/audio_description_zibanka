"""Leading-silence trim, line measurements, and pass 2 (FFmpeg speed, pitch preserved).

Every take has its leading silence trimmed, then is measured against its slot (TCR OUT - TCR IN):

  audio_duration   the meter: from the first sound to the end of the spoken part (trailing silence
                   is not counted). The take is already trimmed, so it starts on its first sound.
  overrun_ms       audio_duration - tcr_duration; negative when the line is too short
  percent_overrun  overrun_ms / tcr_duration * 100
  desired_speed    audio_duration / tcr_duration, no limits

Speeds and percentages are kept to SIG_FIGS significant figures.

  normal row       |percent_overrun| <= NORMAL_TOLERANCE_PERCENT: no speed change in any pass
  needs rewrite    desired_speed outside MIN_SPEED..MAX_SPEED: pass 1.5 rewrites it with Gemini
  pass 2           normal rows and rows flagged for manual review stay at 1.0x; every other row
                   gets its desired speed, held between MIN_SPEED and MAX_SPEED
  within target    percent_overrun <= NORMAL_TOLERANCE_PERCENT (too-short lines count as within)
  success          at least SUCCESS_TARGET_PERCENT of rows within target after pass 2

Also used by the Dubbing Quality Check for its per-line speed control.
"""

from array import array
from dataclasses import dataclass
import io
import math
import shutil
import subprocess
import sys
import wave

# Universal speed limits: pass 1.5 rewrites lines outside them, pass 2 never goes beyond them.
MIN_SPEED = 0.80
MAX_SPEED = 1.20
NORMAL_TOLERANCE_PERCENT = 2       # within +/- 2% of the slot: a normal row
SUCCESS_TARGET_PERCENT = 90        # share of rows that must be within target after pass 2
SIG_FIGS = 8

MIN_FIT_SPEED, MAX_FIT_SPEED = MIN_SPEED, MAX_SPEED   # older names, still used by Quality Check

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


LEADING_SILENCE_DBFS = -60  # same cut-off as measure_leading_silence.py: a breath or click counts as sound


def trim_leading_silence(wav_bytes):
    """Remove the blank audio ElevenLabs puts before the first sound, so the line starts exactly at
    TCR IN. Returns ``(trimmed wav, ms removed)``.

    FFmpeg's ``silenceremove`` finds where the first sound starts (the first 10 ms window louder than
    LEADING_SILENCE_DBFS). The cut itself is made here, on a whole-millisecond boundary just before
    that point, so the speech samples stay exactly as ElevenLabs delivered them."""
    check_ffmpeg()
    with wave.open(io.BytesIO(wav_bytes)) as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        before = wav.getnframes()
        frames = wav.readframes(before)
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", "pipe:0", "-af",
             f"silenceremove=start_periods=1:start_threshold={LEADING_SILENCE_DBFS}dB"
             ":start_silence=0:start_duration=0:detection=rms:window=0.01",
             "-ar", str(rate), "-ac", str(channels), "-f", "s16le", "pipe:1"],
            input=wav_bytes, capture_output=True, check=True, timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Leading silence trim timed out") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("FFmpeg could not trim the leading silence") from exc
    after = len(result.stdout) // (2 * channels)
    frames_per_ms = max(1, rate // 1000)
    cut = (before - after) // frames_per_ms * frames_per_ms   # whole ms, never past the first sound
    if after == 0 or cut == 0:  # all blank, or under 1 ms to cut: keep as delivered
        return wav_bytes, 0
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(frames[cut * width * channels:])
    return output.getvalue(), cut // frames_per_ms


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


# ---- measurements ------------------------------------------------------------------------------

def sig(value):
    """``value`` kept to SIG_FIGS significant figures."""
    return float(f"{value:.{SIG_FIGS}g}")


def format_sig(value):
    """Report text with SIG_FIGS significant figures and its trailing zeros kept:
    1.4523810, 93.181818, -2.0000000."""
    return f"{value:#.{SIG_FIGS}g}".rstrip(".")   # '#' would leave a bare '.' on 8-digit whole numbers


@dataclass(frozen=True)
class Measurement:
    tcr_duration_ms: int
    audio_duration_ms: int

    @property
    def overrun_ms(self):
        return self.audio_duration_ms - self.tcr_duration_ms

    @property
    def percent_overrun(self):
        return sig(self.overrun_ms / self.tcr_duration_ms * 100)

    @property
    def desired_speed(self):
        return sig(self.audio_duration_ms / self.tcr_duration_ms)

    @property
    def is_normal(self):
        return abs(self.percent_overrun) <= NORMAL_TOLERANCE_PERCENT

    @property
    def needs_rewrite(self):
        return not MIN_SPEED <= self.desired_speed <= MAX_SPEED

    @property
    def within_target(self):
        return self.percent_overrun <= NORMAL_TOLERANCE_PERCENT


def audio_duration(wav_bytes):
    """The meter: first sound to the end of the spoken part, in ms. The take has had its leading
    silence trimmed, so this is measured from the start of the clip; trailing silence is not counted."""
    _start, end, _clip = speech_span(wav_bytes)
    return end


def measure(wav_bytes, tcr_duration_ms):
    if tcr_duration_ms <= 0:
        raise ValueError("TCR OUT must be later than TCR IN")
    return Measurement(tcr_duration_ms, audio_duration(wav_bytes))


def limit_speed(speed):
    """Hold a speed between MIN_SPEED and MAX_SPEED."""
    return min(MAX_SPEED, max(MIN_SPEED, speed))


def pass2_speed(measurement, manual_review=False):
    """1.0 for normal rows, rows flagged for manual review and silent takes; otherwise the desired
    speed, held between the limits."""
    if manual_review or measurement.is_normal or measurement.audio_duration_ms == 0:
        return 1.0
    return limit_speed(measurement.desired_speed)


def apply_pass2(wav_bytes, measurement, manual_review=False):
    """Pass 2 for one line: ``(fitted wav, speed applied)``."""
    speed = pass2_speed(measurement, manual_review)
    return change_speed(wav_bytes, speed), speed


def success_rate(measurements):
    """``(percent of rows within target, met SUCCESS_TARGET_PERCENT)`` for the pass 2 measurements."""
    measurements = list(measurements)
    if not measurements:
        return 100.0, True
    percent = sig(sum(m.within_target for m in measurements) / len(measurements) * 100)
    return percent, percent >= SUCCESS_TARGET_PERCENT


# Shorthands kept for Quality Check, which works from a clip and its slot.
def needed_speed(wav_bytes, slot_ms):
    """The desired speed, no limits."""
    return measure(wav_bytes, slot_ms).desired_speed


def fit_speed(wav_bytes, slot_ms):
    """The speed pass 2 would apply to this clip."""
    return pass2_speed(measure(wav_bytes, slot_ms))


def fit_to_slot(wav_bytes, slot_ms):
    return apply_pass2(wav_bytes, measure(wav_bytes, slot_ms))
