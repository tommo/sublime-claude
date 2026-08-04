"""Selection geometry for sticky ◎ composer vs transcript history.

`input_mode` only means the composer chrome exists. Interaction policy must
use caret/selection position relative to `_input_start`.

Pure functions — no Sublime imports — so unit tests can drive them.
"""
from __future__ import annotations

from typing import Iterable, Sequence, Tuple, Union

# (begin, end) inclusive-exclusive ST-style region; begin may be > end.
RegionLike = Union[Tuple[int, int], Sequence[int]]


def _norm_region(r: RegionLike) -> Tuple[int, int]:
    a, b = int(r[0]), int(r[1])
    if a <= b:
        return a, b
    return b, a


def classify_regions(
        regions: Iterable[RegionLike],
        input_start: int,
        eof: int,
) -> str:
    """Classify selection set relative to the draft boundary.

    Returns one of:
      - "draft"    — every region wholly in [input_start, eof]
      - "history"  — every region wholly in [0, input_start)  (empty sel at
                     input_start-1 etc.); no region enters the draft
      - "crossing" — any region straddles input_start, or multi-caret mix
                     of history and draft
      - "none"     — empty regions list

    Notes:
      - A caret at exactly input_start is *in the draft* (draft starts there).
      - Crossing counts as protected for mutations (treat like history).
    """
    regs = [_norm_region(r) for r in regions]
    if not regs:
        return "none"
    start = max(0, int(input_start))
    end = max(start, int(eof))

    wholly_draft = True
    wholly_history = True
    for a, b in regs:
        # empty caret: a == b
        in_draft = a >= start and b <= end
        in_history = b <= start  # wholly before draft (caret at start is draft)
        if not in_draft:
            wholly_draft = False
        if not in_history:
            wholly_history = False
        # straddles: begins before start and ends after start
        if a < start < b:
            return "crossing"

    if wholly_draft:
        return "draft"
    if wholly_history:
        return "history"
    # multi-caret: some in history, some in draft
    return "crossing"


def wholly_in_draft(
        regions: Iterable[RegionLike], input_start: int, eof: int) -> bool:
    return classify_regions(regions, input_start, eof) == "draft"


def wholly_in_history(
        regions: Iterable[RegionLike], input_start: int, eof: int) -> bool:
    return classify_regions(regions, input_start, eof) == "history"


def crosses_draft_boundary(
        regions: Iterable[RegionLike], input_start: int, eof: int) -> bool:
    return classify_regions(regions, input_start, eof) == "crossing"


def mutation_allowed_in_draft(
        regions: Iterable[RegionLike], input_start: int, eof: int) -> bool:
    """True only when every selection is wholly inside the draft."""
    return wholly_in_draft(regions, input_start, eof)


def clamp_region_to_draft(
        begin: int, end: int, input_start: int, eof: int) -> Tuple[int, int]:
    """Clamp a single region into the draft (for explicit draft-only ops)."""
    start = max(0, int(input_start))
    end_pt = max(start, int(eof))
    a, b = _norm_region((begin, end))
    a = min(max(a, start), end_pt)
    b = min(max(b, start), end_pt)
    if a > b:
        a = b = end_pt
    return a, b


def history_select_range(input_start: int, eof: int) -> Tuple[int, int]:
    """Full history span [0, input_start) for Cmd+A while browsing."""
    start = max(0, int(input_start))
    return (0, start)


def draft_select_range(input_start: int, eof: int) -> Tuple[int, int]:
    """Full draft span [input_start, eof] for Cmd+A while composing."""
    start = max(0, int(input_start))
    end_pt = max(start, int(eof))
    return (start, end_pt)
