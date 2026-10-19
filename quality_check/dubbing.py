"""Dub one episode with the AD version 2 engine, keeping every line as its own clip.

This is V2's pipeline (``v2/pipeline.py``) step for step: ElevenLabs per row, the speed dial,
``place_dialogue`` for collisions, ``build_track`` per character. Differences, all needed by the
review screen:

* Each line's clip is kept in ``clips/<excel row>.wav`` (V2 discards them).
* The run is resumable: a clip already on disk is reused, so retrying after an ElevenLabs error
  carries on from the failed line instead of paying for the earlier lines again.
* One line can be regenerated later (``regenerate_line``) or sped up on its own
  (``set_line_speed``): only that clip changes, the episode is re-placed with V2's rule, and only
  the tracks that actually changed are rebuilt.

Each line has two files: ``clips/<row>.take.wav`` is the take as dubbed (ElevenLabs plus the
episode speed dial) and ``clips/<row>.wav`` is what plays (the take, sped up if the reviewer chose a
line speed). Line speed is always applied to the take, so changes never stack.
"""

from array import array
from collections import defaultdict
import json
from pathlib import Path
import shutil
import sys
import wave

from v2.audio_builder import build_track, read_clip
from v2.audio_speed import adjust_audio_speed
from v2.exporter import format_tcr
from v2.timing import place_dialogue

PEAKS_PER_SECOND = 100  # 10 ms waveform resolution for the lane view


class DubCancelled(Exception):
    """The session was closed while dubbing."""


class DubError(RuntimeError):
    """A line could not be dubbed. ``row`` is its Excel row number."""

    def __init__(self, message, row=None):
        super().__init__(message)
        self.row = row


def clip_path(folder, row_number):
    return Path(folder) / "clips" / f"{row_number}.wav"


def take_path(folder, row_number):
    return Path(folder) / "clips" / f"{row_number}.take.wav"


def peaks_for(track_path):
    """Absolute peak per 10 ms bin, scaled 0..255, for drawing the waveform."""
    with wave.open(str(track_path), "rb") as wav:
        rate, width = wav.getframerate(), wav.getsampwidth()
        samples = array("h")
        samples.frombytes(wav.readframes(wav.getnframes()))
    if width != 2:
        raise ValueError("expected 16-bit PCM")
    if sys.byteorder == "big":
        samples.byteswap()
    size = max(1, rate // PEAKS_PER_SECOND)
    out = bytearray()
    for start in range(0, len(samples), size):
        chunk = samples[start:start + size]
        out.append(min(255, max(max(chunk), -min(chunk)) * 255 // 32767))
    return list(out)


def synthesize_line(folder, row, fps, speed, synthesize):
    """One ElevenLabs request for one row, speed-adjusted. Returns the WAV bytes (not yet saved)."""
    try:
        wav = adjust_audio_speed(synthesize(row.text, row.voice_id), speed)
        read_clip(wav)  # rejects empty or unreadable audio before anything is replaced
        return wav
    except DubCancelled:
        raise
    except Exception as exc:
        raise DubError(f"Row {row.row_number} ({row.character} at {format_tcr(row.start_ms, fps)}): {exc}",
                       row=row.row_number) from exc


def _write(path, data):
    temp = path.with_name(path.name + ".partial")
    temp.write_bytes(data)
    temp.replace(path)  # a file on disk is always complete, which makes resuming safe


def save_clip(folder, row_number, wav):
    """Store a fresh take: it is both the take and (at line speed 1.00x) what plays."""
    _write(take_path(folder, row_number), wav)
    _write(clip_path(folder, row_number), wav)


def clip_lengths(folder, rows):
    """Clip lengths in ms, measured exactly as V2 measures them (pydub length)."""
    return {row.row_number: len(read_clip(clip_path(folder, row.row_number).read_bytes())) for row in rows}


def assemble(folder, rows, summary, fps, speed, lengths, rebuild=None):
    """Place every line with V2's rule and (re)build character tracks; return the audio section.

    ``rebuild`` is the set of character names whose track must be rewritten; None means all.
    Tracks not rebuilt keep their file and their waveform peaks.
    """
    folder = Path(folder)
    (folder / "tracks").mkdir(exist_ok=True)
    placed = place_dialogue(rows, lengths)
    episode_end = max(max(p.row.end_ms, p.end_ms) for p in placed)

    index = {c["name"]: i for i, c in enumerate(summary["characters"])}
    by_character = defaultdict(list)
    for p in placed:
        by_character[p.row.character].append(p)

    peaks_file = folder / "peaks.json"
    peaks = json.loads(peaks_file.read_text())["tracks"] if rebuild is not None and peaks_file.is_file() else {}
    tracks = []
    for name, i in index.items():
        items = sorted(by_character[name], key=lambda p: p.start_ms)
        target = folder / "tracks" / f"{i}.wav"
        if rebuild is None or name in rebuild or not target.is_file():
            temp = target.with_suffix(".partial")
            build_track([(p, clip_path(folder, p.row.row_number)) for p in items], episode_end, temp)
            temp.replace(target)
            peaks[str(i)] = peaks_for(target)
        with wave.open(str(target), "rb") as wav:
            rate = wav.getframerate()
            duration_ms = wav.getnframes() * 1000 // rate
        tracks.append({"index": i, "character": name, "voice_id": items[0].row.voice_id,
                       "duration_ms": duration_ms, "lines": len(items)})
    temp = peaks_file.with_suffix(".partial")
    temp.write_text(json.dumps({"per_second": PEAKS_PER_SECOND, "tracks": peaks}), encoding="utf-8")
    temp.replace(peaks_file)

    lines = []
    for p in sorted(placed, key=lambda p: (p.start_ms, p.row.row_number)):
        row = p.row
        lines.append({
            "row": row.row_number,
            "character": row.character,
            "char_index": index[row.character],
            "text": row.text,
            "tcr_in": format_tcr(row.start_ms, fps),
            "tcr_out": format_tcr(row.end_ms, fps),
            "start_ms": row.start_ms,
            "end_ms": row.end_ms,
            "audio_start_ms": p.start_ms,
            "audio_end_ms": p.end_ms,
            "delay_ms": p.delay_ms,
            "overrun_ms": max(0, (p.end_ms - p.start_ms) - (row.end_ms - row.start_ms)),
        })

    over = sum(1 for line in lines if line["overrun_ms"])
    late = sum(1 for line in lines if line["delay_ms"])
    warnings = []
    if over:
        warnings.append(f"{over} line runs past its TCR OUT." if over == 1 else f"{over} lines run past their TCR OUT.")
    if late:
        warnings.append(f"{late} line{'s start' if late != 1 else ' starts'} late because the same character's "
                        "previous line ran long.")
    return {
        "sample_rate": rate,
        "speed": speed,
        "duration_ms": episode_end,
        "tracks": tracks,
        "lines": lines,
        "warnings": warnings,
        "overrun_lines": over,
        "late_lines": late,
    }


def dub_episode(folder, rows, summary, fps, speed, synthesize, report=lambda **_: None,
                cancelled=lambda: False):
    """Generate (or resume) the episode's dub in ``folder``; return the session's audio section.

    ``report(**state)`` receives progress: phase, done, total, current.
    """
    folder = Path(folder)
    (folder / "clips").mkdir(parents=True, exist_ok=True)
    total = len(rows)

    # One ElevenLabs request per row, in script order, sequential like V2 (no automatic
    # retries: a retried request may be billed twice).
    for done, row in enumerate(rows):
        if cancelled():
            raise DubCancelled()
        report(phase="dubbing", done=done, total=total, current=f"{row.character}, {format_tcr(row.start_ms, fps)}")
        if not clip_path(folder, row.row_number).is_file():
            save_clip(folder, row.row_number, synthesize_line(folder, row, fps, speed, synthesize))

    report(phase="building", done=total, total=total, current="Building character tracks")
    return assemble(folder, rows, summary, fps, speed, clip_lengths(folder, rows))


def _replace_and_rebuild(folder, rows, summary, fps, speed, row_number, write, previous):
    """Swap one line's audio (``write()`` does it) and rebuild only the tracks that changed.

    On any failure the line's files are restored, so clip and tracks stay consistent.
    Returns ``(audio, changed_tracks, moved)``.
    """
    folder = Path(folder)
    row = next(r for r in rows if r.row_number == row_number)
    saved = {}
    for path in (clip_path(folder, row_number), take_path(folder, row_number)):
        if path.is_file():
            saved[path] = path.read_bytes()
    try:
        write()
        lengths = clip_lengths(folder, rows)
        # All tracks share one length (V2 rule); if the episode end moves, every track changes.
        end = max(max(p.row.end_ms, p.end_ms) for p in place_dialogue(rows, lengths))
        rebuild = None if end != previous["duration_ms"] else {row.character}
        audio = assemble(folder, rows, summary, fps, speed, lengths, rebuild=rebuild)
    except BaseException:
        for path, data in saved.items():
            _write(path, data)
        raise

    before = {line["row"]: line["audio_start_ms"] for line in previous["lines"]}
    moved = [{"row": line["row"], "character": line["character"],
              "shift_ms": line["audio_start_ms"] - before[line["row"]]}
             for line in audio["lines"]
             if line["row"] != row_number and line["audio_start_ms"] != before.get(line["row"])]
    changed = [t["index"] for t in audio["tracks"] if rebuild is None or t["character"] in rebuild]
    return audio, changed, moved


def regenerate_line(folder, rows, summary, fps, speed, synthesize, row_number, previous):
    """Re-dub one row (``rows`` already carries its new text) as a new take at line speed 1.00x."""
    row = next(r for r in rows if r.row_number == row_number)
    wav = synthesize_line(folder, row, fps, speed, synthesize)  # before touching any file
    return _replace_and_rebuild(folder, rows, summary, fps, speed, row_number,
                                lambda: save_clip(folder, row_number, wav), previous)


def set_line_speed(folder, rows, summary, fps, speed, row_number, factor, previous):
    """Speed one line up from its take (1.00x restores the take exactly). No ElevenLabs request."""
    folder = Path(folder)
    take = take_path(folder, row_number)
    if not take.is_file():  # sessions dubbed before line speed existed: the clip is the take
        _write(take, clip_path(folder, row_number).read_bytes())
    wav = adjust_audio_speed(take.read_bytes(), factor)  # validates the range; FFmpeg above 1.00x
    return _replace_and_rebuild(folder, rows, summary, fps, speed, row_number,
                                lambda: _write(clip_path(folder, row_number), wav), previous)
