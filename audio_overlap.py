"""Repair small dialogue overlaps without changing pitch or cutting speech."""

from dataclasses import dataclass

from audio_builder import audio_duration_ms
from audio_speed import MAX_SPEED_RATE, adjust_audio_speed


@dataclass
class OverlapResult:
    audio: bytes
    duration_ms: int
    speed: float
    repaired: bool = False
    reason: str | None = None


def repair_overlap(output_wav, available_ms):
    """Fit eligible output with additional speed strictly below 1.20x.

    Each attempt uses the same clip after the user's speed dial adjustment.
    If repair fails, retain that clip in full for editing.
    """
    duration_ms = audio_duration_ms(output_wav)
    unchanged = OverlapResult(output_wav, duration_ms, 1.0)
    if available_ms is None or duration_ms <= available_ms:
        return unchanged

    overlap_ms = duration_ms - available_ms
    if duration_ms <= 0 or available_ms <= 0 or overlap_ms * 10 >= duration_ms:
        unchanged.reason = "Overlap is at least 10% of the clip length."
        return unchanged

    required_speed = duration_ms / available_ms
    for _ in range(4):
        if required_speed >= MAX_SPEED_RATE:
            unchanged.reason = "Required additional speed is not below 1.20x."
            return unchanged
        try:
            repaired_wav = adjust_audio_speed(output_wav, required_speed)
            repaired_ms = audio_duration_ms(repaired_wav)
        except RuntimeError as exc:
            unchanged.reason = f"Automatic speed adjustment unavailable: {exc}"
            return unchanged
        if repaired_ms <= 0:
            unchanged.reason = "Speed adjustment produced an empty clip."
            return unchanged
        if repaired_ms <= available_ms:
            return OverlapResult(repaired_wav, repaired_ms, required_speed, repaired=True)

        # Tempo processing can leave a small residual overlap. Recalculate
        # against the measured result, with a 1ms margin, without trimming it.
        required_speed = max(
            required_speed + 0.001,
            required_speed * (repaired_ms + 1) / available_ms,
        )

    unchanged.reason = "Audio still overlaps after bounded speed adjustments."
    return unchanged
