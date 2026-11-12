"""Transactional page writes, annotations and snapshot-consistent reading."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .accents import build_match_expression, normalize_for_policy
from .annotations import (
    VALID_LABELS,
    AnnotationSpan,
    Edit,
    PageNotFoundError,
    StaleRevisionError,
    AnnotationError,
    apply_edits,
    migrate_annotation,
    normalize_edits,
    validate_span,
)
from .db import (
    active_index,
    begin_immediate,
    connect,
    quote_identifier,
    utc_now,
)


def _fts_ident(generation: sqlite3.Row) -> str:
    return quote_identifier(str(generation["fts_table"]))


def _invalidate_all_annotations(
    conn: sqlite3.Connection, page_id: str, now: str, reason: str
) -> int:
    cur = conn.execute(
        """
        UPDATE annotations
           SET status = 'invalidated',
               invalidation_reason = COALESCE(invalidation_reason, ?),
               invalidated_at = ?,
               updated_at = ?
         WHERE page_id = ? AND status = 'active'
        """,
        (reason, now, now, page_id),
    )
    return int(cur.rowcount or 0)


def _annotation_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "annotation_id": row["annotation_id"],
        "page_id": row["page_id"],
        "label": row["label"],
        "start": int(row["start_cp"]),
        "end": int(row["end_cp"]),
        "selected_text": row["selected_text"],
        "status": row["status"],
        "anchor_revision": int(row["anchor_rev"]),
        "created_revision": int(row["created_rev"]),
        "invalidation_reason": row["invalidation_reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "invalidated_at": row["invalidated_at"],
    }


def upsert_page(page_id: str, title: str, body: str) -> dict[str, Any]:
    conn = connect()
    try:
        begin_immediate(conn)
        generation = active_index(conn)
        policy = str(generation["policy"])
        now = utc_now()

        existing = conn.execute(
            "SELECT rowid AS page_ref FROM pages WHERE page_id = ?", (page_id,)
        ).fetchone()
        page_ref = int(existing["page_ref"]) if existing is not None else None

        cur = conn.execute(
            """
            INSERT INTO changes(page_id, page_ref, op, title, body, rev, changed_at)
            VALUES (?, ?, 'upsert', ?, ?, 0, ?)
            """,
            (page_id, page_ref, title, body, now),
        )
        seq = int(cur.lastrowid)

        conn.execute(
            """
            INSERT INTO pages(page_id, title, body, rev, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(page_id) DO UPDATE SET
                title = excluded.title,
                body = excluded.body,
                rev = excluded.rev,
                updated_at = excluded.updated_at
            """,
            (page_id, title, body, seq, now),
        )
        # A full rewrite replaces the whole text: every active annotation is
        # invalidated.  Re-creating the same page ID later never revives them.
        invalidated = _invalidate_all_annotations(
            conn, page_id, now, "page_fully_rewritten"
        )
        if page_ref is None:
            page_ref = int(
                conn.execute(
                    "SELECT rowid AS page_ref FROM pages WHERE page_id = ?", (page_id,)
                ).fetchone()["page_ref"]
            )
            conn.execute(
                "UPDATE generations SET doc_count = doc_count + 1, applied_seq = ? WHERE id = ?",
                (seq, int(generation["id"])),
            )
        else:
            conn.execute(
                "UPDATE generations SET applied_seq = ? WHERE id = ?",
                (seq, int(generation["id"])),
            )
        conn.execute(
            "UPDATE changes SET rev = ?, page_ref = ? WHERE seq = ?",
            (seq, page_ref, seq),
        )

        ident = _fts_ident(generation)
        conn.execute(
            f"""
            INSERT OR REPLACE INTO {ident}(rowid, page_id, title, body)
            VALUES (?, ?, ?, ?)
            """,
            (
                page_ref,
                page_id,
                normalize_for_policy(title, policy),
                normalize_for_policy(body, policy),
            ),
        )
        conn.commit()
        return {
            "page_id": page_id,
            "revision": seq,
            "accent_policy": policy,
            "generation_id": int(generation["id"]),
            "indexed": True,
            "upserted": True,
            "annotations_invalidated": invalidated,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_page(page_id: str) -> dict[str, Any]:
    conn = connect()
    try:
        begin_immediate(conn)
        generation = active_index(conn)
        policy = str(generation["policy"])
        now = utc_now()

        existing = conn.execute(
            "SELECT rowid AS page_ref FROM pages WHERE page_id = ?", (page_id,)
        ).fetchone()
        page_ref = int(existing["page_ref"]) if existing is not None else None

        cur = conn.execute(
            """
            INSERT INTO changes(page_id, page_ref, op, title, body, rev, changed_at)
            VALUES (?, ?, 'delete', NULL, NULL, 0, ?)
            """,
            (page_id, page_ref, now),
        )
        seq = int(cur.lastrowid)
        conn.execute(
            "UPDATE changes SET rev = ?, page_ref = ? WHERE seq = ?",
            (seq, page_ref, seq),
        )

        conn.execute("DELETE FROM pages WHERE page_id = ?", (page_id,))
        # Deleting the page invalidates every active annotation.  The rows are
        # retained (with their original selected text) so the history survives.
        invalidated = _invalidate_all_annotations(
            conn, page_id, now, "page_deleted"
        )
        if page_ref is not None:
            conn.execute(
                "UPDATE generations SET doc_count = doc_count - 1, applied_seq = ? WHERE id = ?",
                (seq, int(generation["id"])),
            )
        else:
            conn.execute(
                "UPDATE generations SET applied_seq = ? WHERE id = ?",
                (seq, int(generation["id"])),
            )

        ident = _fts_ident(generation)
        if page_ref is not None:
            conn.execute(f"DELETE FROM {ident} WHERE rowid = ?", (page_ref,))
        else:
            # Idempotent delete of an absent page.  This fallback also cleans
            # up any pre-existing orphan from an older schema version.
            conn.execute(f"DELETE FROM {ident} WHERE page_id = ?", (page_id,))

        conn.commit()
        return {
            "page_id": page_id,
            "revision": seq,
            "accent_policy": policy,
            "generation_id": int(generation["id"]),
            "deleted": True,
            "existed": existing is not None,
            "annotations_invalidated": invalidated,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def search(raw_query: str, *, limit: int, offset: int) -> dict[str, Any]:
    conn = connect()
    try:
        # A single read transaction pins both the active-generation pointer and
        # the page rows.  Hits and returned bodies therefore come from the same
        # committed database state, even if a rebuild swap commits concurrently.
        conn.execute("BEGIN")
        generation = active_index(conn)
        policy = str(generation["policy"])
        expression, terms = build_match_expression(raw_query, policy)
        ident = _fts_ident(generation)

        total = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                  FROM {ident} AS f
                  JOIN pages AS p ON p.page_id = f.page_id
                 WHERE {ident} MATCH ?
                """,
                (expression,),
            ).fetchone()[0]
        )
        rows = conn.execute(
            f"""
            SELECT p.page_id, p.title, p.body, p.rev, p.updated_at,
                   bm25({ident}) AS score
              FROM {ident} AS f
              JOIN pages AS p ON p.page_id = f.page_id
             WHERE {ident} MATCH ?
             ORDER BY score ASC, p.page_id ASC
             LIMIT ? OFFSET ?
            """,
            (expression, limit, offset),
        ).fetchall()
        conn.execute("COMMIT")
        return {
            "query": raw_query,
            "query_terms": terms,
            "accent_policy": policy,
            "generation_id": int(generation["id"]),
            "total": total,
            "limit": limit,
            "offset": offset,
            "hits": [
                {
                    "page_id": row["page_id"],
                    "title": row["title"],
                    "body": row["body"],
                    "revision": int(row["rev"]),
                    "updated_at": row["updated_at"],
                    "score": float(row["score"]),
                }
                for row in rows
            ],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_page(page_id: str) -> dict[str, Any] | None:
    conn = connect()
    try:
        conn.execute("BEGIN")
        generation = active_index(conn)
        row = conn.execute(
            "SELECT page_id, title, body, rev, updated_at FROM pages WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        counts = conn.execute(
            """
            SELECT status, COUNT(*) AS n
              FROM annotations
             WHERE page_id = ?
             GROUP BY status
            """,
            (page_id,),
        ).fetchall()
        conn.execute("COMMIT")
        by_status = {str(item["status"]): int(item["n"]) for item in counts}
        return {
            "page_id": row["page_id"],
            "title": row["title"],
            "body": row["body"],
            "revision": int(row["rev"]),
            "updated_at": row["updated_at"],
            "accent_policy": str(generation["policy"]),
            "generation_id": int(generation["id"]),
            "active_annotation_count": by_status.get("active", 0),
            "invalidated_annotation_count": by_status.get("invalidated", 0),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_annotation(
    *,
    page_id: str,
    expected_revision: int,
    label: str,
    start: int,
    end: int,
) -> dict[str, Any]:
    """Create one annotation against an exact, revision-fenced page state."""

    if label not in VALID_LABELS:
        raise AnnotationError(f"unsupported label: {label!r}")

    # Phase 1: read-only validation.  No write lock is held while the
    # coordinate and selected-text checks run.
    conn = connect()
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT body, rev FROM pages WHERE page_id = ?", (page_id,)
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            raise PageNotFoundError(page_id)
        revision = int(row["rev"])
        if revision != int(expected_revision):
            conn.execute("COMMIT")
            raise StaleRevisionError(
                page_id=page_id, expected=int(expected_revision), current=revision
            )
        body = str(row["body"])
        validate_span(start, end, len(body))
        selected_text = body[start:end]
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()

    # Phase 2: the short write transaction re-checks the revision so an
    # annotation created concurrently with a correction is never written
    # against unvalidated coordinates.
    conn = connect()
    try:
        begin_immediate(conn)
        row = conn.execute(
            "SELECT body, rev FROM pages WHERE page_id = ?", (page_id,)
        ).fetchone()
        if row is None:
            raise PageNotFoundError(page_id)
        revision = int(row["rev"])
        if revision != int(expected_revision):
            raise StaleRevisionError(
                page_id=page_id, expected=int(expected_revision), current=revision
            )
        body = str(row["body"])
        if start > len(body) or end > len(body) or body[start:end] != selected_text:
            raise AnnotationError(
                "the page changed between validation and commit; retry on the "
                "current revision"
            )

        annotation_id = uuid.uuid4().hex
        now = utc_now()
        conn.execute(
            """
            INSERT INTO annotations
                (annotation_id, page_id, label, start_cp, end_cp, selected_text,
                 status, anchor_rev, created_rev, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                annotation_id,
                page_id,
                label,
                int(start),
                int(end),
                selected_text,
                revision,
                revision,
                now,
                now,
            ),
        )
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM annotations WHERE annotation_id = ?", (annotation_id,)
        ).fetchone()
        return _annotation_dict(stored)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def correct_page(
    *,
    page_id: str,
    expected_revision: int,
    raw_edits: list[dict],
) -> dict[str, Any]:
    """Apply up to 20 non-overlapping replacements and migrate annotations.

    Coordinate computation happens outside any write transaction; the final
    short ``BEGIN IMMEDIATE`` transaction commits the corrected body, the FTS
    row, the change-log entry and every annotation migration together.
    """

    # Phase 1: read-only snapshot and all expensive coordinate work.
    conn = connect()
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT body, rev FROM pages WHERE page_id = ?", (page_id,)
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            raise PageNotFoundError(page_id)
        revision = int(row["rev"])
        if revision != int(expected_revision):
            conn.execute("COMMIT")
            raise StaleRevisionError(
                page_id=page_id, expected=int(expected_revision), current=revision
            )
        original_body = str(row["body"])
        spans = [
            AnnotationSpan(
                annotation_id=str(item["annotation_id"]),
                start=int(item["start_cp"]),
                end=int(item["end_cp"]),
            )
            for item in conn.execute(
                """
                SELECT annotation_id, start_cp, end_cp
                  FROM annotations
                 WHERE page_id = ? AND status = 'active'
                 ORDER BY start_cp, end_cp, annotation_id
                """,
                (page_id,),
            ).fetchall()
        ]
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()

    edits: list[Edit] = normalize_edits(raw_edits, len(original_body))
    new_body = apply_edits(original_body, edits)
    migrations = [migrate_annotation(span, edits) for span in spans]

    # Phase 2: one short atomic write transaction.
    conn = connect()
    try:
        begin_immediate(conn)
        generation = active_index(conn)
        policy = str(generation["policy"])
        now = utc_now()

        row = conn.execute(
            "SELECT rowid AS page_ref, title, body, rev FROM pages WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        if row is None:
            raise PageNotFoundError(page_id)
        current_revision = int(row["rev"])
        if current_revision != int(expected_revision):
            raise StaleRevisionError(
                page_id=page_id,
                expected=int(expected_revision),
                current=current_revision,
            )
        if str(row["body"]) != original_body:
            # Another writer changed the body without changing the revision
            # number; refuse rather than apply coordinates to a different text.
            raise StaleRevisionError(
                page_id=page_id,
                expected=int(expected_revision),
                current=current_revision,
            )

        page_ref = int(row["page_ref"])
        title = str(row["title"])

        # Re-read the active annotations inside the write lock so any
        # annotation added during phase 1 is migrated as well — none are lost.
        locked_spans = [
            AnnotationSpan(
                annotation_id=str(item["annotation_id"]),
                start=int(item["start_cp"]),
                end=int(item["end_cp"]),
            )
            for item in conn.execute(
                """
                SELECT annotation_id, start_cp, end_cp
                  FROM annotations
                 WHERE page_id = ? AND status = 'active'
                 ORDER BY start_cp, end_cp, annotation_id
                """,
                (page_id,),
            ).fetchall()
        ]
        locked_ids = {span.annotation_id for span in locked_spans}
        planned_ids = {span.annotation_id for span in spans}
        if locked_ids != planned_ids:
            # New annotations appeared during the computation; recompute their
            # migration still without holding this write lock any longer than
            # the pure in-memory function requires.
            migrations = [migrate_annotation(span, edits) for span in locked_spans]

        invalidated = 0
        migrated = 0
        for result in migrations:
            if result.active:
                # Coordinates move with the text; the anchor revision stays
                # the revision at which the label was created.
                conn.execute(
                    """
                    UPDATE annotations
                       SET start_cp = ?, end_cp = ?, updated_at = ?
                     WHERE annotation_id = ? AND status = 'active'
                    """,
                    (
                        int(result.new_start),
                        int(result.new_end),
                        now,
                        result.annotation_id,
                    ),
                )
                migrated += 1
            else:
                conn.execute(
                    """
                    UPDATE annotations
                       SET status = 'invalidated',
                           invalidation_reason = ?,
                           invalidated_at = ?,
                           updated_at = ?
                     WHERE annotation_id = ? AND status = 'active'
                    """,
                    (result.reason, now, now, result.annotation_id),
                )
                invalidated += 1

        details = {
            "edits": [
                {"start": edit.start, "end": edit.end, "replacement": edit.replacement}
                for edit in edits
            ],
            "annotations_migrated": migrated,
            "annotations_invalidated": invalidated,
        }
        cur = conn.execute(
            """
            INSERT INTO changes
                (page_id, page_ref, op, title, body, rev, details, changed_at)
            VALUES (?, ?, 'correct', ?, ?, 0, ?, ?)
            """,
            (
                page_id,
                page_ref,
                title,
                new_body,
                json.dumps(details, ensure_ascii=False),
                now,
            ),
        )
        seq = int(cur.lastrowid)
        conn.execute(
            "UPDATE changes SET rev = ? WHERE seq = ?",
            (seq, seq),
        )
        conn.execute(
            "UPDATE pages SET body = ?, rev = ?, updated_at = ? WHERE page_id = ?",
            (new_body, seq, now, page_id),
        )
        conn.execute(
            "UPDATE generations SET applied_seq = ? WHERE id = ?",
            (seq, int(generation["id"])),
        )

        ident = _fts_ident(generation)
        conn.execute(
            f"""
            INSERT OR REPLACE INTO {ident}(rowid, page_id, title, body)
            VALUES (?, ?, ?, ?)
            """,
            (
                page_ref,
                page_id,
                normalize_for_policy(title, policy),
                normalize_for_policy(new_body, policy),
            ),
        )
        conn.commit()
        return {
            "page_id": page_id,
            "revision": seq,
            "previous_revision": current_revision,
            "accent_policy": policy,
            "generation_id": int(generation["id"]),
            "body": new_body,
            "edits_applied": len(edits),
            "annotations_migrated": migrated,
            "annotations_invalidated": invalidated,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_annotations(page_id: str, status_filter: str | None = None) -> dict[str, Any]:
    """Return the body and all annotations from one committed snapshot."""

    if status_filter is not None and status_filter not in ("active", "invalidated"):
        raise AnnotationError(
            f"unsupported annotation status filter: {status_filter!r}"
        )

    conn = connect()
    try:
        conn.execute("BEGIN")
        page = conn.execute(
            "SELECT page_id, title, body, rev, updated_at FROM pages WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        if page is None:
            conn.execute("COMMIT")
            raise PageNotFoundError(page_id)

        if status_filter is None:
            rows = conn.execute(
                """
                SELECT * FROM annotations
                 WHERE page_id = ?
                 ORDER BY status DESC, start_cp, end_cp, annotation_id
                """,
                (page_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM annotations
                 WHERE page_id = ? AND status = ?
                 ORDER BY start_cp, end_cp, annotation_id
                """,
                (page_id, status_filter),
            ).fetchall()
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()

    annotations = [_annotation_dict(row) for row in rows]
    active = [item for item in annotations if item["status"] == "active"]
    invalid = [item for item in annotations if item["status"] == "invalidated"]
    return {
        "page_id": page["page_id"],
        "title": page["title"],
        "body": page["body"],
        "revision": int(page["rev"]),
        "updated_at": page["updated_at"],
        "active_annotation_count": len(active),
        "invalidated_annotation_count": len(invalid),
        "annotations": annotations,
    }
