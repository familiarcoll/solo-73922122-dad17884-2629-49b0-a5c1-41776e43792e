"""End-to-end tests for revision-fenced annotations and local corrections."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import SETTINGS


def configure_database(path: Path) -> None:
    object.__setattr__(SETTINGS, "db_path", path)
    object.__setattr__(SETTINGS, "rebuild_batch_delay_ms", 80)
    object.__setattr__(SETTINGS, "rebuild_batch_size", 2)
    object.__setattr__(SETTINGS, "rebuild_lease_seconds", 3)


@pytest.fixture
def client(tmp_path: Path):
    configure_database(tmp_path / "annotations.db")
    from app.main import app
    from app.rebuild import cancel_local_rebuilds

    with TestClient(app) as test_client:
        yield test_client
    cancel_local_rebuilds()


BODY = "Le café est près de la gare; le señor lit un naïve résumé."


def _make_page(client: TestClient, page_id: str = "ocr-001", body: str = BODY) -> dict:
    response = client.put(
        f"/api/pages/{page_id}",
        json={"title": "Test page", "body": body},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _add(client: TestClient, page_id: str, revision: int, label: str, word: str, body: str = BODY):
    start = body.index(word)
    end = start + len(word)
    response = client.post(
        f"/api/pages/{page_id}/annotations",
        json={"revision": revision, "label": label, "start": start, "end": end},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["selected_text"] == word
    assert payload["status"] == "active"
    return payload


def test_annotation_lifecycle_and_selected_text_snapshot(client: TestClient):
    created = _make_page(client)
    revision = created["revision"]

    person = _add(client, "ocr-001", revision, "person", "señor")
    place = _add(client, "ocr-001", revision, "place", "gare")

    snapshot = client.get("/api/pages/ocr-001/annotations").json()
    assert snapshot["revision"] == revision
    assert snapshot["body"] == BODY
    assert snapshot["active_annotation_count"] == 2
    assert snapshot["invalidated_annotation_count"] == 0
    labels = {item["label"] for item in snapshot["annotations"]}
    assert labels == {"person", "place"}
    assert all(item["selected_text"] for item in snapshot["annotations"])

    # Filtering.
    active_only = client.get(
        "/api/pages/ocr-001/annotations", params={"status": "active"}
    ).json()
    assert len(active_only["annotations"]) == 2
    invalid_only = client.get(
        "/api/pages/ocr-001/annotations", params={"status": "invalidated"}
    ).json()
    assert invalid_only["annotations"] == []

    # Bad label, bad span, unknown page.
    bad_label = client.post(
        "/api/pages/ocr-001/annotations",
        json={"revision": revision, "label": "dog", "start": 0, "end": 2},
    )
    assert bad_label.status_code == 422
    bad_span = client.post(
        "/api/pages/ocr-001/annotations",
        json={"revision": revision, "label": "place", "start": 5, "end": len(BODY) + 3},
    )
    assert bad_span.status_code == 422
    inverted = client.post(
        "/api/pages/ocr-001/annotations",
        json={"revision": revision, "label": "place", "start": 9, "end": 4},
    )
    assert inverted.status_code == 422
    missing = client.post(
        "/api/pages/missing/annotations",
        json={"revision": revision, "label": "place", "start": 0, "end": 1},
    )
    assert missing.status_code == 404

    # Annotation does not bump the page revision.
    page = client.get("/api/pages/ocr-001").json()
    assert page["revision"] == revision
    assert page["active_annotation_count"] == 2
    assert person["annotation_id"] and place["annotation_id"]


def test_stale_revision_is_rejected_for_annotation(client: TestClient):
    created = _make_page(client)
    revision = created["revision"]

    stale = client.post(
        "/api/pages/ocr-001/annotations",
        json={"revision": revision - 1, "label": "place", "start": 0, "end": 2},
    )
    assert stale.status_code == 409
    detail = stale.json()
    assert detail["error"] == "stale_revision"
    assert detail["current_revision"] == revision

    # A later full rewrite moves the revision and the old fence is stale again.
    rewritten = client.put(
        "/api/pages/ocr-001", json={"title": "T", "body": "tout nouveau texte"}
    ).json()
    stale2 = client.post(
        "/api/pages/ocr-001/annotations",
        json={"revision": revision, "label": "person", "start": 0, "end": 4},
    )
    assert stale2.status_code == 409
    assert stale2.json()["current_revision"] == rewritten["revision"]

    fresh = client.post(
        "/api/pages/ocr-001/annotations",
        json={"revision": rewritten["revision"], "label": "person", "start": 0, "end": 4},
    )
    assert fresh.status_code == 201


def test_local_correction_translates_disjoint_annotations(client: TestClient):
    created = _make_page(client)
    revision = created["revision"]

    before = _add(client, "ocr-001", revision, "place", "gare")
    after = _add(client, "ocr-001", revision, "person", "señor")
    word_naive = _add(client, "ocr-001", revision, "place", "naïve")

    # Replace "café" (code points [3,7)) with the longer "bistrot".
    start = BODY.index("café")
    response = client.post(
        "/api/pages/ocr-001/corrections",
        json={
            "revision": revision,
            "replacements": [
                {"start": start, "end": start + len("café"), "replacement": "bistrot"}
            ],
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["edits_applied"] == 1
    assert result["annotations_migrated"] == 3
    assert result["annotations_invalidated"] == 0
    new_revision = result["revision"]
    assert new_revision > revision
    assert "bistrot" in result["body"]

    snapshot = client.get("/api/pages/ocr-001/annotations").json()
    assert snapshot["revision"] == new_revision
    assert snapshot["invalidated_annotation_count"] == 0
    by_id = {item["annotation_id"]: item for item in snapshot["annotations"]}

    moved = by_id[before["annotation_id"]]
    shift = len("bistrot") - len("café")
    assert moved["start"] == before["start"] + shift
    assert moved["end"] == before["end"] + shift
    assert snapshot["body"][moved["start"]:moved["end"]] == "gare"
    assert moved["selected_text"] == "gare"
    # The anchor revision stays the revision at which the label was created;
    # only the coordinates were translated.
    assert moved["anchor_revision"] == revision
    assert moved["created_revision"] == revision

    moved_after = by_id[after["annotation_id"]]
    assert moved_after["start"] == after["start"] + shift
    assert snapshot["body"][moved_after["start"]:moved_after["end"]] == "señor"

    moved_naive = by_id[word_naive["annotation_id"]]
    assert snapshot["body"][moved_naive["start"]:moved_naive["end"]] == "naïve"

    # The change log carries the structured correction details.
    from app.db import connect

    conn = connect(query_only=True)
    try:
        log = conn.execute(
            "SELECT op, details FROM changes WHERE page_id = ? ORDER BY seq DESC LIMIT 1",
            ("ocr-001",),
        ).fetchone()
    finally:
        conn.close()
    assert log["op"] == "correct"
    import json

    details = json.loads(log["details"])
    assert details["annotations_migrated"] == 3
    assert details["edits"][0]["replacement"] == "bistrot"


def test_intersecting_replacement_and_inner_insertion_invalidate(client: TestClient):
    created = _make_page(client)
    revision = created["revision"]
    victim = _add(client, "ocr-001", revision, "person", "señor")
    survivor = _add(client, "ocr-001", revision, "place", "gare")
    inner_victim = _add(client, "ocr-001", revision, "place", "naïve")

    # A replacement fully covering the annotated span invalidates it.
    start = BODY.index("señor")
    response = client.post(
        "/api/pages/ocr-001/corrections",
        json={
            "revision": revision,
            "replacements": [
                {"start": start, "end": start + len("señor"), "replacement": "X12345"},
                # An insertion strictly inside another annotation (naïve) too.
                {"start": BODY.index("naïve") + 1, "end": BODY.index("naïve") + 1,
                 "replacement": "-"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["annotations_invalidated"] == 2
    assert response.json()["annotations_migrated"] == 1

    snapshot = client.get("/api/pages/ocr-001/annotations").json()
    by_id = {item["annotation_id"]: item for item in snapshot["annotations"]}
    bad = by_id[victim["annotation_id"]]
    assert bad["status"] == "invalidated"
    assert bad["invalidation_reason"] == "replacement_intersects_annotation"
    # Original selected text is retained even though the body changed.
    assert bad["selected_text"] == "señor"
    good = by_id[survivor["annotation_id"]]
    assert good["status"] == "active"

    invalid_only = client.get(
        "/api/pages/ocr-001/annotations", params={"status": "invalidated"}
    ).json()
    assert len(invalid_only["annotations"]) == 2
    assert all(item["selected_text"] for item in invalid_only["annotations"])


def test_insertion_boundary_semantics_start_before_end_after(client: TestClient):
    body = "abcdefghij"
    created = _make_page(client, body=body)
    revision = created["revision"]
    annotation = _add(
        client, "ocr-001", revision, "person", "def", body=body
    )  # span [3, 6)

    response = client.post(
        "/api/pages/ocr-001/corrections",
        json={
            "revision": revision,
            "replacements": [
                {"start": 3, "end": 3, "replacement": "P"},   # at start -> before
                {"start": 6, "end": 6, "replacement": "Q"},   # at end -> after
            ],
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["annotations_invalidated"] == 0
    snapshot = client.get("/api/pages/ocr-001/annotations").json()
    item = snapshot["annotations"][0]
    assert item["start"] == annotation["start"] + 1
    assert item["end"] == annotation["end"] + 1
    assert snapshot["body"][item["start"]:item["end"]] == "def"
    assert snapshot["body"] == "abcPdefQghij"


def test_correction_rejects_overlaps_coincident_points_and_stale_revision(
    client: TestClient,
):
    created = _make_page(client)
    revision = created["revision"]

    overlap = client.post(
        "/api/pages/ocr-001/corrections",
        json={
            "revision": revision,
            "replacements": [
                {"start": 0, "end": 5, "replacement": "a"},
                {"start": 4, "end": 8, "replacement": "b"},
            ],
        },
    )
    assert overlap.status_code == 422

    coincident = client.post(
        "/api/pages/ocr-001/corrections",
        json={
            "revision": revision,
            "replacements": [
                {"start": 5, "end": 5, "replacement": "a"},
                {"start": 5, "end": 5, "replacement": "b"},
            ],
        },
    )
    assert coincident.status_code == 422

    inside = client.post(
        "/api/pages/ocr-001/corrections",
        json={
            "revision": revision,
            "replacements": [
                {"start": 2, "end": 9, "replacement": "long"},
                {"start": 5, "end": 5, "replacement": "i"},
            ],
        },
    )
    assert inside.status_code == 422

    out_of_range = client.post(
        "/api/pages/ocr-001/corrections",
        json={
            "revision": revision,
            "replacements": [
                {"start": len(BODY), "end": len(BODY) + 4, "replacement": "x"}
            ],
        },
    )
    assert out_of_range.status_code == 422

    # A successful correction bumps the revision; replaying with the old
    # fence must be rejected and change nothing.
    ok = client.post(
        "/api/pages/ocr-001/corrections",
        json={
            "revision": revision,
            "replacements": [{"start": 0, "end": 2, "replacement": "LE"}],
        },
    )
    assert ok.status_code == 200
    new_revision = ok.json()["revision"]

    stale = client.post(
        "/api/pages/ocr-001/corrections",
        json={
            "revision": revision,
            "replacements": [{"start": 0, "end": 2, "replacement": "XX"}],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["current_revision"] == new_revision

    # The stale attempt rolled everything back: the body stays at revision N.
    snapshot = client.get("/api/pages/ocr-001/annotations").json()
    assert snapshot["revision"] == new_revision
    assert snapshot["body"].startswith("LE")


def test_full_rewrite_and_delete_invalidate_without_resurrection(client: TestClient):
    created = _make_page(client)
    revision = created["revision"]
    first = _add(client, "ocr-001", revision, "person", "señor")
    second = _add(client, "ocr-001", revision, "place", "gare")

    rewritten = client.put(
        "/api/pages/ocr-001", json={"title": "T", "body": "tout nouveau texte ici"}
    )
    assert rewritten.status_code == 200
    assert rewritten.json()["annotations_invalidated"] == 2

    snapshot = client.get("/api/pages/ocr-001/annotations").json()
    assert snapshot["active_annotation_count"] == 0
    assert snapshot["invalidated_annotation_count"] == 2
    reasons = {item["invalidation_reason"] for item in snapshot["annotations"]}
    assert reasons == {"page_fully_rewritten"}
    # Original text preserved.
    by_id = {item["annotation_id"]: item for item in snapshot["annotations"]}
    assert by_id[first["annotation_id"]]["selected_text"] == "señor"

    # Re-creating the same page ID must not revive the old labels.
    client.put("/api/pages/ocr-001", json={"title": "T2", "body": "encore du texte"})
    again = client.get("/api/pages/ocr-001/annotations").json()
    assert again["active_annotation_count"] == 0
    assert again["invalidated_annotation_count"] == 2

    # Delete path.
    other = _make_page(client, page_id="ocr-002")
    _add(client, "ocr-002", other["revision"], "place", "gare")
    deleted = client.delete("/api/pages/ocr-002")
    assert deleted.status_code == 200
    assert deleted.json()["annotations_invalidated"] == 1
    assert client.get("/api/pages/ocr-002").status_code == 404
    other_snapshot = client.get("/api/pages/ocr-002/annotations")
    assert other_snapshot.status_code == 404


def test_annotations_added_during_correction_are_never_lost(client: TestClient):
    created = _make_page(client)
    revision = created["revision"]

    # Pre-existing labels spread on both sides of the edited region.
    for word in ("gare", "señor", "résumé", "naïve", "près"):
        _add(client, "ocr-001", revision, "place", word)

    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def add_concurrently(word: str) -> None:
        try:
            barrier.wait()
            response = client.post(
                "/api/pages/ocr-001/annotations",
                json={
                    "revision": revision,
                    "label": "person",
                    "start": BODY.index(word),
                    "end": BODY.index(word) + len(word),
                },
            )
            # Either it lands before the correction commit (201) or the
            # correction wins first and the stale fence is rejected (409).
            assert response.status_code in (201, 409), response.text
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    words = ["café", "est", "lit", "un", "Le"]
    threads = [threading.Thread(target=add_concurrently, args=(word,)) for word in words]

    def correct_concurrently() -> None:
        try:
            barrier.wait()
            start = BODY.index("café")
            response = client.post(
                "/api/pages/ocr-001/corrections",
                json={
                    "revision": revision,
                    "replacements": [
                        {"start": start, "end": start + 4, "replacement": "thé"}
                    ],
                },
            )
            assert response.status_code in (200, 409), response.text
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads.append(threading.Thread(target=correct_concurrently))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors

    snapshot = client.get("/api/pages/ocr-001/annotations").json()
    # Five pre-existing annotations always survive; every concurrent add is
    # either committed and migrated/invalidated, or explicitly rejected.
    assert snapshot["active_annotation_count"] + snapshot[
        "invalidated_annotation_count"
    ] >= 5
    all_ids = {item["annotation_id"] for item in snapshot["annotations"]}
    assert len(all_ids) == len(snapshot["annotations"])

    # Every active span still points at its selected text in the live body.
    for item in snapshot["annotations"]:
        if item["status"] == "active":
            assert snapshot["body"][item["start"]:item["end"]] == item["selected_text"]


def test_concurrent_corrections_serialize_with_one_losing_the_fence(
    client: TestClient,
):
    created = _make_page(client)
    revision = created["revision"]
    _add(client, "ocr-001", revision, "place", "gare")

    barrier = threading.Barrier(2)
    outcomes: list[int] = []
    lock = threading.Lock()

    def worker(replacement: str, start: int) -> None:
        barrier.wait()
        response = client.post(
            "/api/pages/ocr-001/corrections",
            json={
                "revision": revision,
                "replacements": [{"start": start, "end": start + 1, "replacement": replacement}],
            },
        )
        with lock:
            outcomes.append(response.status_code)

    threads = [
        threading.Thread(target=worker, args=("X", 0)),
        threading.Thread(target=worker, args=("Y", 10)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == [200, 409]
    snapshot = client.get("/api/pages/ocr-001/annotations").json()
    # The single winning correction kept the annotation consistent.
    for item in snapshot["annotations"]:
        if item["status"] == "active":
            assert snapshot["body"][item["start"]:item["end"]] == item["selected_text"]


def test_annotations_and_corrections_survive_restart(tmp_path: Path):
    configure_database(tmp_path / "restart.db")
    from app.db import connect, init_db
    from app.main import app

    with TestClient(app) as first:
        created = first.put(
            "/api/pages/ocr-001", json={"title": "T", "body": BODY}
        ).json()
        revision = created["revision"]
        annotation = _add(first, "ocr-001", revision, "person", "señor")
        start = BODY.index("café")
        corrected = first.post(
            "/api/pages/ocr-001/corrections",
            json={
                "revision": revision,
                "replacements": [
                    {"start": start, "end": start + len("café"), "replacement": "bistrot"}
                ],
            },
        ).json()
        new_revision = corrected["revision"]

    # Simulate a full process restart against the same SQLite file.
    with TestClient(app) as second:
        snapshot = second.get("/api/pages/ocr-001/annotations").json()
        assert snapshot["revision"] == new_revision
        assert len(snapshot["annotations"]) == 1
        item = snapshot["annotations"][0]
        assert item["annotation_id"] == annotation["annotation_id"]
        assert item["status"] == "active"
        assert snapshot["body"][item["start"]:item["end"]] == "señor"

    # The durable schema is the migrated one (op='correct' accepted).
    conn = connect()
    try:
        ops = [
            row["op"]
            for row in conn.execute(
                "SELECT op FROM changes WHERE page_id = ? ORDER BY seq", ("ocr-001",)
            ).fetchall()
        ]
    finally:
        conn.close()
    assert ops == ["upsert", "correct"]


def test_correction_during_rebuild_is_replayed_into_new_generation(
    client: TestClient,
):
    import time

    created = _make_page(client)
    for index in range(8):
        _make_page(client, page_id=f"book-{index:02d}", body=f"page {index} café gare")
    revision = created["revision"]
    _add(client, "ocr-001", revision, "place", "gare")

    trigger = client.post("/api/index/rebuild", json={"accent_policy": "fold"})
    assert trigger.status_code == 202
    job_id = trigger.json()["job"]["job_id"]

    # Correct while the background rebuild scan/catch-up is in progress.
    start = BODY.index("café")
    corrected = client.post(
        "/api/pages/ocr-001/corrections",
        json={
            "revision": revision,
            "replacements": [{"start": start, "end": start + len("café"), "replacement": "bistrot"}],
        },
    )
    assert corrected.status_code == 200, corrected.text
    new_revision = corrected.json()["revision"]

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = client.get("/api/index/status").json()
        job = status["current_rebuild_job"] or status["latest_rebuild_job"]
        if job and job["job_id"] == job_id and job["state"] in {
            "succeeded",
            "failed",
            "interrupted",
        }:
            break
        time.sleep(0.03)
    else:  # pragma: no cover
        raise AssertionError("rebuild did not finish")

    status = client.get("/api/index/status").json()
    assert status["active_policy"] == "fold"

    hits = client.get("/api/search", params={"q": "bistrot"}).json()
    assert hits["hits"][0]["page_id"] == "ocr-001"
    assert hits["hits"][0]["revision"] == new_revision
    assert "bistrot" in hits["hits"][0]["body"]

    snapshot = client.get("/api/pages/ocr-001/annotations").json()
    assert snapshot["revision"] == new_revision
    item = snapshot["annotations"][0]
    assert item["status"] == "active"
    assert snapshot["body"][item["start"]:item["end"]] == "gare"
