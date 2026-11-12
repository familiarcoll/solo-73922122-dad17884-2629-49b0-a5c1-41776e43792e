"""Unit tests for the pure coordinate-migration logic."""

from __future__ import annotations

import pytest

from app.annotations import (
    MAX_EDITS_PER_CORRECTION,
    AnnotationSpan,
    AnnotationError,
    OverlappingEditsError,
    apply_edits,
    migrate_annotation,
    normalize_edits,
)


def test_normalize_edits_accepts_touching_boundaries_and_sorts():
    edits = normalize_edits(
        [
            {"start": 8, "end": 10, "replacement": "z"},
            {"start": 2, "end": 5, "replacement": "ab"},
            {"start": 5, "end": 8, "replacement": ""},
        ],
        20,
    )
    assert [(e.start, e.end) for e in edits] == [(2, 5), (5, 8), (8, 10)]


def test_normalize_edits_rejects_bad_inputs():
    with pytest.raises(AnnotationError):
        normalize_edits([], 10)
    with pytest.raises(AnnotationError):
        normalize_edits([{"start": 0, "end": 11, "replacement": "x"}], 10)
    with pytest.raises(AnnotationError):
        normalize_edits(
            [{"start": i, "end": i + 1, "replacement": "x"}
             for i in range(MAX_EDITS_PER_CORRECTION + 1)],
            100,
        )
    with pytest.raises(OverlappingEditsError):
        normalize_edits(
            [{"start": 0, "end": 5, "replacement": "a"},
             {"start": 4, "end": 6, "replacement": "b"}],
            10,
        )
    with pytest.raises(OverlappingEditsError):
        normalize_edits(
            [{"start": 3, "end": 3, "replacement": "a"},
             {"start": 3, "end": 3, "replacement": "b"}],
            10,
        )
    with pytest.raises(OverlappingEditsError):
        normalize_edits(
            [{"start": 2, "end": 8, "replacement": "a"},
             {"start": 5, "end": 5, "replacement": "b"}],
            10,
        )


def test_apply_edits_handles_deletion_insertion_and_replacement():
    body = "abcdefghij"
    edits = normalize_edits(
        [
            {"start": 0, "end": 2, "replacement": ""},     # delete "ab"
            {"start": 5, "end": 5, "replacement": "XY"},   # insert
            {"start": 8, "end": 10, "replacement": "Z"},   # replace tail
        ],
        len(body),
    )
    assert apply_edits(body, edits) == "cdeXYfghZ"


def test_insertion_at_start_shifts_both_endpoints():
    result = migrate_annotation(
        AnnotationSpan("a", 4, 7),
        normalize_edits([{"start": 4, "end": 4, "replacement": "kk"}], 10),
    )
    assert result.active and (result.new_start, result.new_end) == (6, 9)


def test_insertion_at_end_shifts_neither_endpoint():
    result = migrate_annotation(
        AnnotationSpan("a", 4, 7),
        normalize_edits([{"start": 7, "end": 7, "replacement": "kk"}], 10),
    )
    assert result.active and (result.new_start, result.new_end) == (4, 7)


def test_inner_insertion_invalidates():
    result = migrate_annotation(
        AnnotationSpan("a", 4, 7),
        normalize_edits([{"start": 5, "end": 5, "replacement": "k"}], 10),
    )
    assert not result.active
    assert result.reason == "insertion_inside_annotation"


def test_intersecting_replacement_invalidates_but_boundary_touch_translates():
    intersect = migrate_annotation(
        AnnotationSpan("a", 4, 7),
        normalize_edits([{"start": 6, "end": 9, "replacement": "q"}], 10),
    )
    assert not intersect.active
    assert intersect.reason == "replacement_intersects_annotation"

    touching = migrate_annotation(
        AnnotationSpan("a", 4, 7),
        normalize_edits([{"start": 7, "end": 9, "replacement": "qq"}], 10),
    )
    assert touching.active


def test_multiple_disjoint_edits_compose_offsets():
    body = "0123456789"
    edits = normalize_edits(
        [
            {"start": 0, "end": 0, "replacement": "AA"},     # +2 before span
            {"start": 2, "end": 4, "replacement": "XYZ"},    # +1 before span
            {"start": 9, "end": 10, "replacement": ""},      # after span
        ],
        len(body),
    )
    result = migrate_annotation(AnnotationSpan("a", 5, 8), edits)
    assert result.active and (result.new_start, result.new_end) == (8, 11)
    new_body = apply_edits(body, edits)
    assert new_body[8:11] == "567"


def test_unicode_code_points_are_indices_not_utf16_or_bytes():
    # "é" and "🙂" are each one Python code point (though multi-byte/multi-utf16).
    body = "aé🙂b"
    assert len(body) == 4
    assert body[1:2] == "é"
    edits = normalize_edits([{"start": 0, "end": 0, "replacement": "x"}], len(body))
    result = migrate_annotation(AnnotationSpan("u", 1, 2), edits)
    assert result.active and (result.new_start, result.new_end) == (2, 3)
    assert apply_edits(body, edits)[2:3] == "é"
