# audio_builder.py
# Places each audio clip at its correct timecode position on a
# silent master timeline and exports as a single .wav file

from pydub import AudioSegment
import io

SAMPLE_RATE = 24000

def build_master_timeline(
    ad_rows,
    audio_clips,
    starts_ms,
    episode_duration_ms,
    progress_callback=print
):
    """
    Place each audio clip at its start time (from audio_overlap.fix_overlaps)
    on a silent master timeline.

    The master is episode_duration_ms long, or longer if the last clip ends
    after that, so no speech is cut off.
    """

    clips = [AudioSegment.from_wav(io.BytesIO(audio_bytes)) for audio_bytes in audio_clips]
    duration_ms = max([episode_duration_ms] + [start + len(clip) for start, clip in zip(starts_ms, clips)])

    if duration_ms < 60000:
        duration_label = f"{duration_ms / 1000:.2f} seconds"
    else:
        duration_label = f"{duration_ms / 1000 / 60:.1f} minutes"
    progress_callback(f"Creating silent master timeline ({duration_label})...")
    master = AudioSegment.silent(
        duration=duration_ms,
        frame_rate=SAMPLE_RATE
    )

    for row, clip, start_ms in zip(ad_rows, clips, starts_ms):
        master = master.overlay(clip, position=start_ms)
        progress_callback(
            f"Placed row {row['row_number']} at {start_ms}ms "
            f"(clip: {len(clip)}ms, ends at: {start_ms + len(clip)}ms): "
            f"{row['text'][:30]}..."
        )

    return master


def audio_duration_ms(audio_bytes):
    """Return the duration of a WAV clip in milliseconds."""
    return len(AudioSegment.from_wav(io.BytesIO(audio_bytes)))


def export_wav_bytes(audio):
    """Export an AudioSegment as in-memory WAV bytes."""
    buffer = io.BytesIO()
    audio.export(buffer, format="wav")
    return buffer.getvalue()


def export_wav(master, output_path, progress_callback=print):
    """
    Export the master timeline as a .wav file.
    """
    progress_callback(f"Exporting to {output_path}...")
    with master.export(output_path, format="wav"):
        pass
    progress_callback(f"Done! Saved to {output_path}")
