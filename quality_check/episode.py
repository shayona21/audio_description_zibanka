"""Summarise one episode's parsed script for the quality check screen."""

from collections import OrderedDict

from v2.exporter import format_tcr


class EpisodeError(ValueError):
    """The workbook is valid for dubbing but not usable for a one-episode review."""


def summarize_episode(rows, fps):
    """Return the episode summary, rejecting scripts that contain several episodes."""
    episodes = sorted({row.episode for row in rows})
    if len(episodes) != 1:
        listed = ", ".join(episodes[:10]) + (" …" if len(episodes) > 10 else "")
        raise EpisodeError(
            f"This script contains {len(episodes)} episodes ({listed}). "
            "Quality check works on one episode at a time: upload a script with a single EP NO."
        )

    characters = OrderedDict()
    for row in sorted(rows, key=lambda r: (r.start_ms, r.row_number)):
        entry = characters.setdefault(row.character, {"name": row.character,
                                                      "voice_id": row.voice_id, "lines": 0})
        entry["lines"] += 1

    order = {name: index for index, name in enumerate(characters)}
    # Each cue is [start_ms, end_ms, character index]; drawn as the episode timeline strip.
    cues = [[row.start_ms, row.end_ms, order[row.character]]
            for row in sorted(rows, key=lambda r: (r.start_ms, r.row_number))]

    first_start = min(row.start_ms for row in rows)
    last_end = max(row.end_ms for row in rows)
    return {
        "episode": episodes[0],
        "fps": fps,
        "lines": len(rows),
        "characters": list(characters.values()),
        "first_start_ms": first_start,
        "first_start_tcr": format_tcr(first_start, fps),
        "last_end_ms": last_end,
        "last_end_tcr": format_tcr(last_end, fps),
        "cues": cues,
    }
