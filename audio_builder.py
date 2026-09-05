# audio_builder.py
# Places each audio clip at its correct timecode position on a
# silent master timeline and exports as a single .wav file

from pydub import AudioSegment
import io

# Total episode duration in milliseconds
# 31 minutes = 31 * 60 * 1000 = 1,860,000 ms
# We read this from the last row's end_time dynamically
SAMPLE_RATE = 24000

def build_master_timeline(
    ad_rows,
    audio_clips,
    episode_duration_ms,
    progress_callback=print
):
    """
    Place each audio clip at its start_ms position on a silent master timeline.
    
    Clips always retain their scheduled starts, even when they overlap.
    The master duration is fixed regardless of individual clip durations.
    """

    progress_callback(
        f"Creating silent master timeline "
        f"({episode_duration_ms / 1000 / 60:.1f} minutes)..."
    )
    master = AudioSegment.silent(
        duration=episode_duration_ms,
        frame_rate=SAMPLE_RATE
    )

    # Track when the previous clip finishes
    previous_clip_end_ms = 0

    for i, (row, audio_bytes) in enumerate(zip(ad_rows, audio_clips)):

        # Convert raw bytes to a pydub AudioSegment
        clip = AudioSegment.from_wav(io.BytesIO(audio_bytes))

        # Get the intended start position from the timecode
        intended_start_ms = row["start_ms"]

        # Check if this clip would overlap with the previous one
        if intended_start_ms < previous_clip_end_ms:
            progress_callback(
                f"Row {row['row_number']}: OVERLAP DETECTED — "
                f"keeping scheduled start at {intended_start_ms}ms"
            )
        actual_start_ms = intended_start_ms

        # Place the clip on the master timeline
        master = master.overlay(clip, position=actual_start_ms)

        # Update where the previous clip ends
        clip_end_ms = actual_start_ms + len(clip)
        previous_clip_end_ms = max(previous_clip_end_ms, clip_end_ms)

        progress_callback(
            f"Placed row {row['row_number']} at {actual_start_ms}ms "
            f"(clip: {len(clip)}ms, ends at: {clip_end_ms}ms): "
            f"{row['text'][:30]}..."
        )

    return master

def export_wav(master, output_path, progress_callback=print):
    """
    Export the master timeline as a .wav file.
    """
    progress_callback(f"Exporting to {output_path}...")
    with master.export(output_path, format="wav"):
        pass
    progress_callback(f"Done! Saved to {output_path}")
