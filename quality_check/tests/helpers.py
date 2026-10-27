"""Shared fixtures: workbooks, and real AD version 2 output ZIPs built with a fake voice."""

import io
import math
import struct
from pathlib import Path
import wave

from openpyxl import Workbook

HEADERS = ["EP NO", "SR NO", "TCR IN", "TCR OUT", "CHARACTERS", "VOICE ID", "TARGET DIALOGUE"]

# KUHU's second line (30.0 s) is long enough to overrun into her third (31.0 s), so the dub
# delays it: this exercises V2's collision rule. AMRITA overlaps KUHU, which is allowed.
ONE_EPISODE = [
    ["1", 1, "00:00:12:22", "00:00:14:09", "KUHU", "voiceA", "[Worried] Are you okay?"],
    ["1", 2, "00:00:22:02", "00:00:26:06", "AMRITA", "voiceB", "[Angry] You beggar, get out of my sight!"],
    ["1", 3, "00:00:30:00", "00:00:31:00", "KUHU", "voiceA", "[Gasps] This line is much too long for its slot."],
    ["1", 4, "00:00:31:00", "00:00:32:00", "KUHU", "voiceA", "Stop!"],
    ["1", 5, "00:00:23:00", "00:00:24:00", "AMRITA", "voiceB", "[Shouting] Guards!"],
]


def workbook_bytes(rows, headers=HEADERS):
    book = Workbook()
    sheet = book.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def tone_wav(duration_ms, freq=220.0, rate=24000, amplitude=8000):
    frames = duration_ms * rate // 1000
    data = b"".join(struct.pack("<h", int(amplitude * math.sin(2 * math.pi * freq * i / rate)))
                    for i in range(frames))
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(data)
    return out.getvalue()


def fake_voice(text, voice_id):
    """Deterministic stand-in for ElevenLabs: 45 ms per character, pitch per voice."""
    return tone_wav(max(300, 45 * len(text)), freq=180.0 + 40 * (sum(map(ord, voice_id)) % 7))


# A second episode, so an AD version 2 run holds more than the episode under review.
SECOND_EPISODE = [
    ["2", 1, "00:00:02:00", "00:00:04:00", "KUHU", "voiceA", "Good morning."],
    ["2", 2, "00:00:05:00", "00:00:06:00", "RAVI", "voiceC", "[Laughs] Not today, not ever, not in a hundred years."],
]


def dub_zip(tmp, rows, name="dub.zip", fps=25):
    """Run the real V2 pipeline (needs pydub and FFmpeg) and return the output ZIP's bytes."""
    from v2.pipeline import run_pipeline

    tmp = Path(tmp)
    script = tmp / f"{name}.xlsx"
    script.write_bytes(workbook_bytes(rows))
    out = tmp / name
    run_pipeline(script, out, fps=fps, synthesize=fake_voice, progress=lambda _m: None)
    return out.read_bytes()


def have_pydub():
    try:
        import pydub  # noqa: F401
        return True
    except ImportError:
        return False
