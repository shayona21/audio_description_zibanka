"""Render aligned tracks while retaining all speech, including overruns."""

import io
from pydub import AudioSegment

from .tts_client import SAMPLE_RATE


def read_clip(wav_bytes):
    clip = AudioSegment.from_wav(io.BytesIO(wav_bytes))
    if len(clip) <= 0:
        raise ValueError("Speech provider returned an empty WAV")
    return clip.set_frame_rate(SAMPLE_RATE).set_channels(1).set_sample_width(2)


def build_track(rows_and_paths, duration_ms, output_path):
    master = AudioSegment.silent(duration=duration_ms, frame_rate=SAMPLE_RATE)
    for row, path in rows_and_paths:
        clip = read_clip(path.read_bytes())
        master = master.overlay(clip, position=row.start_ms)
    with master.export(str(output_path), format="wav"):
        pass
