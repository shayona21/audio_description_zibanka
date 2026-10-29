"""Fix narration overlaps: speed a line up if that is enough, else play it after the previous line.

Audio description has one narrator, so two lines must never play at once. Lines are handled
in start-time order. For each line:

1. Overlap: the line would still be playing when the next line's start timecode arrives.
2. The speed that fixes it = the line's length / the time it has before the next line starts.
3. If that speed is below MAX_FIX_SPEED (1.5x), the line is sped up with FFmpeg (pitch
   preserved) and keeps its start.
4. If not, the line keeps its normal speed and the next line starts SEQUENTIAL_GAP_MS
   (0.5 s) after it ends.

The fix speed is on top of the speed dial.
"""

from audio_builder import audio_duration_ms
from audio_speed import apply_tempo

MAX_FIX_SPEED = 1.5        # a line is only sped up when the fix needs less than this
SEQUENTIAL_GAP_MS = 500    # gap after a line when the next line has to wait for it


def fit_to_window(wav_bytes, window_ms):
    """Speed the clip up so it ends within ``window_ms``; returns (wav, speed) or None.

    atempo can leave a clip a few ms long, so the speed is re-measured and raised (up to
    three tries), always staying below MAX_FIX_SPEED.
    """
    length_ms = audio_duration_ms(wav_bytes)
    speed = length_ms / window_ms if window_ms > 0 else float("inf")
    for _ in range(3):
        if speed >= MAX_FIX_SPEED:
            return None
        fitted = apply_tempo(wav_bytes, speed)
        fitted_ms = audio_duration_ms(fitted)
        if fitted_ms <= window_ms:
            return fitted, speed
        speed *= (fitted_ms + 1) / window_ms
    return None


def fix_overlaps(ad_rows, audio_clips, progress_callback=print):
    """Apply the rule above to rows sorted by start time; returns (clips, start times in ms)."""
    clips, starts = [], []
    previous_end_ms = None
    for i, (row, clip) in enumerate(zip(ad_rows, audio_clips)):
        # Step 4 for the previous line: wait until it has finished, plus the gap.
        start_ms = row["start_ms"]
        if previous_end_ms is not None and start_ms < previous_end_ms:
            start_ms = previous_end_ms + SEQUENTIAL_GAP_MS
            progress_callback(
                f"OVERLAP — Row {row['row_number']} placed after the previous line: "
                f"starts {(start_ms - row['start_ms']) / 1000:.2f}s later than its start timecode."
            )

        # Steps 1-3: does this line run into the next one, and can a speed-up fix it?
        length_ms = audio_duration_ms(clip)
        if i + 1 < len(ad_rows):
            window_ms = ad_rows[i + 1]["start_ms"] - start_ms
            if length_ms > window_ms:
                needed = length_ms / window_ms if window_ms > 0 else float("inf")
                try:
                    fitted = fit_to_window(clip, window_ms)
                except RuntimeError as exc:  # FFmpeg missing or failed: place sequentially
                    fitted = None
                    progress_callback(f"Row {row['row_number']}: speed-up unavailable ({exc}).")
                if fitted:
                    clip, speed = fitted
                    length_ms = audio_duration_ms(clip)
                    progress_callback(
                        f"OVERLAP FIXED — Row {row['row_number']} sped up {speed:.2f}x "
                        f"to end before the next line (pitch preserved)."
                    )
                else:
                    progress_callback(
                        f"OVERLAP — Row {row['row_number']} would need "
                        + (f"{needed:.2f}x" if needed != float("inf") else "an unlimited speed-up")
                        + f" (limit below {MAX_FIX_SPEED:.1f}x): kept at normal speed; "
                        "the next line is placed after it."
                    )

        clips.append(clip)
        starts.append(start_ms)
        previous_end_ms = start_ms + length_ms
    return clips, starts
