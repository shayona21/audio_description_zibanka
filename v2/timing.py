"""Resolve audio collisions independently for each episode and character."""

from collections import defaultdict
from dataclasses import dataclass

from .models import DialogueRow


@dataclass(frozen=True)
class PlacedDialogue:
    row: DialogueRow
    start_ms: int
    end_ms: int

    @property
    def delay_ms(self):
        return self.start_ms - self.row.start_ms


def place_dialogue(rows, clip_lengths):
    """Start each line at max(script start, previous actual audio end).

    Rows are ordered by original start time, with Excel row number breaking ties.
    Only an actual collision delays a line. Available silence absorbs overruns,
    so later lines can return to their original starts without moving earlier.
    """
    groups = defaultdict(list)
    for row in rows:
        groups[row.group].append(row)

    placements = []
    for group in sorted(groups):
        previous_end_ms = 0
        for row in sorted(groups[group], key=lambda row: (row.start_ms, row.row_number)):
            start = max(row.start_ms, previous_end_ms)
            placed = PlacedDialogue(row, start, start + clip_lengths[row.row_number])
            placements.append(placed)
            previous_end_ms = placed.end_ms
    return placements
