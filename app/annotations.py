"""Annotation labels and Unicode code-point coordinate migration.

All coordinates are **Unicode code point** half-open intervals ``[start, end)``
into a Python ``str``; Python string indices are already code-point indices, so
a selected span is exactly ``body[start:end]``.

A local correction is an ordered list of non-overlapping replacements, each one
also interpreted in the *pre-correction* coordinate space.  The rules
implemented here are:

* an insertion at an annotation's **start** counts as *before* the annotation
  (it shifts both coordinates);
* an insertion at an annotation's **end** counts as *after* it (it shifts
  neither coordinate);
* a replacement that intersects an annotation, or an insertion that lands
  strictly inside it, invalidates that annotation while its original text is
  retained.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

AnnotationLabel = Literal["person", "place"]
VALID_LABELS: tuple[str, ...] = ("person", "place")

MAX_EDITS_PER_CORRECTION = 20


class AnnotationError(ValueError):
    """Base class for annotation/correction request validation failures."""


class StaleRevisionError(AnnotationError):
    """The supplied expected revision no longer matches the committed page."""

    def __init__(self, *, page_id: str, expected: int, current: int):
        super().__init__(
            f"stale revision for page {page_id!r}: expected {expected}, "
            f"current revision is {current}"
        )
        self.page_id = page_id
        self.expected = expected
        self.current = current


class PageNotFoundError(AnnotationError):
    def __init__(self, page_id: str):
        super().__init__(f"page not found: {page_id!r}")
        self.page_id = page_id


class OverlappingEditsError(AnnotationError):
    pass


@dataclass(frozen=True)
class Edit:
    """One half-open interval replacement in pre-correction coordinates."""

    start: int
    end: int
    replacement: str

    @property
    def is_insertion(self) -> bool:
        return self.start == self.end

    @property
    def delta(self) -> int:
        return len(self.replacement) - (self.end - self.start)


def validate_span(start: int, end: int, text_length: int) -> None:
    if not isinstance(start, int) or not isinstance(end, int):
        raise AnnotationError("start and end must be integers")
    if start < 0 or end < 0 or start > end or end > text_length:
        raise AnnotationError(
            f"span [{start}, {end}) is outside the body of length {text_length}"
        )


def normalize_edits(
    raw_edits: Sequence[dict], body_length: int
) -> list[Edit]:
    """Validate at most 20 mutually non-overlapping replacements.

    Insertion points must not coincide with another insertion point, and an
    insertion point may not fall strictly inside another replacement.
    Edits touching only at their boundaries are allowed.
    """

    if not raw_edits:
        raise AnnotationError("at least one replacement is required")
    if len(raw_edits) > MAX_EDITS_PER_CORRECTION:
        raise AnnotationError(
            f"a correction accepts at most {MAX_EDITS_PER_CORRECTION} replacements"
        )

    edits: list[Edit] = []
    for index, raw in enumerate(raw_edits):
        try:
            start = int(raw["start"])
            end = int(raw["end"])
            replacement = str(raw["replacement"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AnnotationError(f"replacement #{index} is malformed") from exc
        validate_span(start, end, body_length)
        edits.append(Edit(start=start, end=end, replacement=replacement))

    edits.sort(key=lambda item: (item.start, item.end))

    for earlier, later in zip(edits, edits[1:]):
        if later.start < earlier.end:
            raise OverlappingEditsError(
                f"replacements [{earlier.start}, {earlier.end}) and "
                f"[{later.start}, {later.end}) overlap"
            )
        if earlier.is_insertion and later.is_insertion and later.start == earlier.start:
            raise OverlappingEditsError(
                f"insertion points at code point {earlier.start} coincide"
            )
        if earlier.is_insertion and earlier.start > later.start and earlier.start < later.end:
            raise OverlappingEditsError(
                f"insertion point {earlier.start} falls inside another replacement"
            )
    return edits


def apply_edits(body: str, edits: Sequence[Edit]) -> str:
    """Apply validated edits, all interpreted in pre-correction coordinates."""

    pieces: list[str] = []
    cursor = 0
    for edit in edits:
        pieces.append(body[cursor:edit.start])
        pieces.append(edit.replacement)
        cursor = edit.end
    pieces.append(body[cursor:])
    return "".join(pieces)


@dataclass(frozen=True)
class AnnotationSpan:
    annotation_id: str
    start: int
    end: int


@dataclass(frozen=True)
class MigrationResult:
    annotation_id: str
    active: bool
    new_start: int | None
    new_end: int | None
    reason: str | None


def _shift_before(position: int, edits: Sequence[Edit], *, at_end: bool) -> int:
    """Shift one coordinate by every edit that belongs before it.

    An insertion exactly at the coordinate counts as before it when the
    coordinate is a span start, but after it when the coordinate is a span end.
    """

    shifted = position
    for edit in edits:
        if edit.is_insertion:
            before = edit.start < position if at_end else edit.start <= position
        else:
            before = edit.start < position
        if before:
            shifted += edit.delta
    return shifted


def migrate_annotation(span: AnnotationSpan, edits: Sequence[Edit]) -> MigrationResult:
    """Decide the post-correction fate of one active annotation."""

    for edit in edits:
        if edit.is_insertion:
            # An insertion strictly inside the annotation invalidates it.
            if span.start < edit.start < span.end:
                return MigrationResult(span.annotation_id, False, None, None,
                                       "insertion_inside_annotation")
        else:
            # Half-open intersection; boundary touching is harmless.
            if edit.start < span.end and edit.end > span.start:
                return MigrationResult(span.annotation_id, False, None, None,
                                       "replacement_intersects_annotation")

    new_start = _shift_before(span.start, edits, at_end=False)
    new_end = _shift_before(span.end, edits, at_end=True)
    return MigrationResult(span.annotation_id, True, new_start, new_end, None)
