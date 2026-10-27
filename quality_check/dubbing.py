"""Dub one episode with the AD version 2 engine (or import AD version 2's audio), keeping every
line as its own clip.

Generating is V2's pipeline (``v2/pipeline.py``) step for step: ElevenLabs per row, pass 2 (each
line fitted to its slot with FFmpeg, 0.70x to 1.50x), ``place_dialogue`` for collisions,
``build_track`` per character. Importing takes the fitted line clips from an AD version 2 ZIP
instead and goes straight to placing and building. Differences from V2, all needed by the review:

* Generating is resumable: a clip already on disk is reused, so retrying after an ElevenLabs error
  carries on from the failed line instead of paying for the earlier lines again.
* One line can be regenerated later (``regenerate_line``) or given another speed
  (``set_line_speed``): only that clip changes, the episode is re-placed with V2's rule, and only
  the tracks that actually changed are rebuilt.

Each line has two files: ``clips/<row>.take.wav`` is the take (ElevenLabs as delivered when
generated here; V2's fitted clip when imported) and ``clips/<row>.wav`` is what plays (the take at
the line's speed). Speeds are always measured against the natural ElevenLabs delivery and applied
to the take, so changes never stack. Per line, the session records a ``take`` dict:
``take_speed`` (speed the take file is already at), ``fit_speed`` (pass 2's choice) and
``needed_speed`` (spoken length / slot with no limits, to tell the reviewer when even the limit
does not make the line fit).
"""

from array import array
from collections import defaultdict
import json
from pathlib import Path
import sys
import wave

from v2.audio_builder import build_track, read_clip
from v2.audio_speed import limit_speed, needed_speed, retime
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


def synthesize_line(row, fps, synthesize):
    """One ElevenLabs request for one row. Returns the WAV bytes as delivered (not yet saved)."""
    try:
        wav = synthesize(row.text, row.voice_id)
        read_clip(wav)  # rejects empty or unreadable audio before anything is replaced
        return wav
    except DubCancelled:
        raise
    except Exception as exc:
        raise DubError(f"Row {row.row_number} ({row.character} at {format_tcr(row.start_ms, fps)}): {exc}",
                       row=row.row_number) from exc


def describe_take(wav, row, take_speed=1.0, fit=None):
    """Speeds for a take. ``fit`` is pass 2's speed when already known (an imported V2 clip)."""
    needed = needed_speed(wav, row.end_ms - row.start_ms, clip_speed=take_speed)
    return {"take_speed": take_speed, "fit_speed": limit_speed(needed) if fit is None else fit,
            "needed_speed": round(needed, 2)}


def prepare_take(row, fps, wav, take_speed=1.0, fit=None):
    """Pass 2 for a take: ``(take info, clip that plays)``; the clip is the take at its fit speed."""
    try:
        info = describe_take(wav, row, take_speed, fit)
        clip = retime(wav, info["fit_speed"] / take_speed)
        read_clip(clip)
        return info, clip
    except Exception as exc:
        raise DubError(f"Row {row.row_number} ({row.character} at {format_tcr(row.start_ms, fps)}): "
                       f"fitting the line to its slot failed: {exc}", row=row.row_number) from exc


def _write(path, data):
    temp = path.with_name(path.name + ".partial")
    temp.write_bytes(data)
    temp.replace(path)  # a file on disk is always complete, which makes resuming safe


def save_take(folder, row_number, take, clip):
    """Store a take and what plays. The clip is written last: its presence marks the line done."""
    _write(take_path(folder, row_number), take)
    _write(clip_path(folder, row_number), clip)


def clip_lengths(folder, rows):
    """Clip lengths in ms, measured exactly as V2 measures them (pydub length)."""
    return {row.row_number: len(read_clip(clip_path(folder, row.row_number).read_bytes())) for row in rows}


def assemble(folder, rows, summary, fps, lengths, rebuild=None):
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
        "duration_ms": episode_end,
        "tracks": tracks,
        "lines": lines,
        "warnings": warnings,
        "overrun_lines": over,
        "late_lines": late,
    }


def dub_episode(folder, rows, summary, fps, synthesize, report=lambda **_: None, cancelled=lambda: False):
    """Generate (or resume) the episode's dub in ``folder``.

    Returns ``(audio section, {row: take info})``. ``report(**state)`` receives progress: phase,
    done, total, current.
    """
    folder = Path(folder)
    (folder / "clips").mkdir(parents=True, exist_ok=True)
    total = len(rows)
    takes = {}

    # One ElevenLabs request per row, in script order, sequential like V2 (no automatic
    # retries: a retried request may be billed twice). Each take is fitted to its slot at once.
    for done, row in enumerate(rows):
        if cancelled():
            raise DubCancelled()
        report(phase="dubbing", done=done, total=total, current=f"{row.character}, {format_tcr(row.start_ms, fps)}")
        if clip_path(folder, row.row_number).is_file():  # done before a retry: reuse, never pay twice
            take = take_path(folder, row.row_number)
            takes[row.row_number] = describe_take((take if take.is_file() else clip_path(folder, row.row_number))
                                                  .read_bytes(), row)
            continue
        wav = synthesize_line(row, fps, synthesize)
        takes[row.row_number], clip = prepare_take(row, fps, wav)
        save_take(folder, row.row_number, wav, clip)

    report(phase="building", done=total, total=total, current="Building character tracks")
    return assemble(folder, rows, summary, fps, clip_lengths(folder, rows)), takes


def import_clips(folder, rows, fps, clips, fits):
    """Store AD version 2's fitted line clips as the takes; returns ``{row: take info}``.

    ``clips`` maps each row to its WAV bytes and ``fits`` to the speed V2 fitted it at, so the
    take is already at that speed and plays as it is.
    """
    folder = Path(folder)
    (folder / "clips").mkdir(parents=True, exist_ok=True)
    takes = {}
    for row in rows:
        fit = fits[row.row_number]
        takes[row.row_number], clip = prepare_take(row, fps, clips[row.row_number], take_speed=fit, fit=fit)
        save_take(folder, row.row_number, clips[row.row_number], clip)
    return takes


def _replace_and_rebuild(folder, rows, summary, fps, row_number, write, previous):
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
        audio = assemble(folder, rows, summary, fps, lengths, rebuild=rebuild)
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


def regenerate_line(folder, rows, summary, fps, synthesize, row_number, previous):
    """Re-dub one row (``rows`` already carries its new text) as a new take, fitted to its slot.

    Returns ``(audio, changed_tracks, moved, take info)``.
    """
    row = next(r for r in rows if r.row_number == row_number)
    wav = synthesize_line(row, fps, synthesize)       # before touching any file
    info, clip = prepare_take(row, fps, wav)
    audio, changed, moved = _replace_and_rebuild(folder, rows, summary, fps, row_number,
                                                 lambda: save_take(folder, row_number, wav, clip), previous)
    return audio, changed, moved, info


def set_line_speed(folder, rows, summary, fps, row_number, speed, take_speed, previous):
    """Play one line at ``speed`` (relative to the natural take) from its take; the take's own
    speed gives exactly the take back. No ElevenLabs request."""
    folder = Path(folder)
    take = take_path(folder, row_number)
    if not take.is_file():  # sessions dubbed before takes were kept: the clip is the take
        _write(take, clip_path(folder, row_number).read_bytes())
    wav = retime(take.read_bytes(), speed / take_speed)  # FFmpeg unless the ratio is 1
    return _replace_and_rebuild(folder, rows, summary, fps, row_number,
                                lambda: _write(clip_path(folder, row_number), wav), previous)
