"""FastAPI application: OCR page writes, accent-aware search and live rebuild."""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .accents import AccentPolicy
from .annotations import (
    MAX_EDITS_PER_CORRECTION,
    VALID_LABELS,
    AnnotationError,
    PageNotFoundError,
    StaleRevisionError,
)
from .archive import (
    add_annotation,
    correct_page,
    delete_page,
    get_page,
    list_annotations,
    search,
    upsert_page,
)
from .config import SETTINGS
from .db import claim_rebuild, connect, fail_job, get_job, get_status, init_db
from .rebuild import cancel_local_rebuilds, start_rebuild_thread


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(process)d %(message)s",
)
logger = logging.getLogger("archive.api")


APP_DESCRIPTION = """
Accent-aware OCR archive search backed by **SQLite FTS5**, with transactional
page corrections, **person/place text annotations** and an online, background
index rebuild.

### Annotations that survive local corrections

Researchers label names and places with half-open **Unicode code-point**
intervals into a page body. The server stores the exact selected original
text and the revision the label was anchored to:

* `POST /api/pages/{page_id}/annotations` creates one label against an
  **expected revision**. A stale revision is rejected with HTTP `409`.
* `POST /api/pages/{page_id}/corrections` applies up to **20 mutually
  non-overlapping** replacements, all interpreted in the *pre-correction*
  coordinate space. Insertion points may not coincide or land inside another
  replacement.
* Annotations fully outside the edited spans have their coordinates
  **translated**: an insertion at an annotation start counts as *before* it
  (both endpoints shift); an insertion at the end counts as *after* it.
* A replacement intersecting an annotation, or an insertion strictly inside
  one, **invalidates** that annotation while its original selected text is
  retained.
* A full page rewrite or a delete invalidates every annotation; re-creating
  the same page ID never revives old labels.
* The corrected body, every annotation migration, the change-log row and the
  FTS update commit in one short transaction. Coordinate computation happens
  *before* the write lock is taken, so annotations can still be added while a
  correction is being calculated, and none of those new labels are lost.
* `GET /api/pages/{page_id}/annotations` returns the body, revision and both
  active and invalidated annotations from one snapshot. All state is durable
  in SQLite and survives a restart.

### Index generations and accent policy

* Every index generation has its own FTS5 table.
* `keep` indexes accented Latin letters exactly (`café` != `cafe`). This is the
  initial policy.
* `fold` removes combining accents from Latin letters (`café` == `cafe`,
  `señor` == `senor`, `naïve` == `naive`).
* `POST /api/index/rebuild` creates a **new generation** in the background.
  The old generation remains active, and page writes/deletes continue to
  commit normally. Writes are never blocked by a lock covering the scan.
* The new generation snapshots the canonical pages and then replays every
  committed write from the durable change log. The active pointer is switched
  in one short transaction only after the shadow generation has caught up.
* Duplicate rebuild requests while a job is active return that same job; the
  database has a unique constraint that permits only one active rebuild across
  both API worker processes.
* A failed or interrupted rebuild leaves the previous generation active. The
  committed pages and change log are durable, so a restart can rebuild again.

### Two-minute Swagger demo

Use the **Try it out** controls below (or `curl`):

1. **Initial accented write (keep policy)** —
   `PUT /api/pages/ocr-001` with title `Crème brûlée à Paris` and body
   `Le café est près de la gare; le señor lit un naïve résumé.`
2. **Exact search** — `GET /api/search?q=café` returns the page.
   `GET /api/search?q=cafe` does not match under `keep`.
3. **Start a folding rebuild** — `POST /api/index/rebuild` with
   `{"accent_policy": "fold"}`. A second identical call returns the same
   `job_id` (HTTP 202).
4. **Correct and delete while it rebuilds** — send
   `PUT /api/pages/ocr-001` with replacement content, and
   `DELETE /api/pages/ocr-002`. Both commit immediately; the old index stays
   queryable.
5. **Watch status** — `GET /api/index/status` shows `active_policy`, the
   current job state (`queued`, `building`, `catching_up`, `activating`,
   `succeeded`), progress and both generations.
6. **After success** — `GET /api/search?q=cafe` now finds `café`-spelled text,
   deleted pages stay deleted, and returned titles/bodies are the corrected
   committed versions.

For a visible on-screen demonstration with a tiny dataset, run the API with
`REBUILD_BATCH_DELAY_MS=250` (or increase the page count). The delay is only a
demo aid and defaults to zero.

### Accent examples

| Document word | `keep` query | `fold` query |
|---|---|---|
| café | café | cafe or café |
| señor | señor | senor or señor |
| naïve | naïve | naive or naïve |
| résumé | résumé | resume or résumé |
"""


class PageUpsert(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "summary": "Initial accented OCR page (keep policy)",
                    "value": {
                        "title": "Crème brûlée à Paris",
                        "body": (
                            "Le café est près de la gare. Le señor lit un naïve "
                            "résumé sous la pluie."
                        ),
                    },
                },
                {
                    "summary": "Correction submitted while a rebuild is running",
                    "value": {
                        "title": "Crème brûlée — corrected OCR",
                        "body": (
                            "Le café fermé est près de la gare. Le señor relit "
                            "le résumé corrigé; l'élève est déjà parti."
                        ),
                    },
                },
            ]
        },
    )

    title: str = Field(default="", description="OCR page title (UTF-8, accents preserved)")
    body: str = Field(default="", description="OCR page body (UTF-8, accents preserved)")


class RebuildRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "summary": "Switch from keep to fold in the background",
                    "value": {"accent_policy": "fold"},
                },
                {
                    "summary": "Switch back to exact accents",
                    "value": {"accent_policy": "keep"},
                },
            ]
        },
    )

    accent_policy: AccentPolicy | None = Field(
        default=None,
        description="Target policy. Omit to choose the opposite of the active policy.",
    )
    # A friendly alias for clients that call the setting "policy".
    policy: AccentPolicy | None = Field(
        default=None,
        description="Alias of accent_policy.",
        exclude=True,
    )


class AnnotationCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "summary": "Label a person name",
                    "value": {
                        "revision": 1,
                        "label": "person",
                        "start": 44,
                        "end": 49,
                    },
                },
                {
                    "summary": "Label a place name",
                    "value": {
                        "revision": 1,
                        "label": "place",
                        "start": 23,
                        "end": 27,
                    },
                },
            ]
        },
    )

    revision: int = Field(
        ge=0,
        description="The page revision the caller read. A mismatched revision is rejected with HTTP 409.",
    )
    label: str = Field(
        description="Annotation label. Allowed values: `person` or `place`.",
        examples=list(VALID_LABELS),
    )
    start: int = Field(
        ge=0,
        description="Inclusive start Unicode code point offset into the current body.",
    )
    end: int = Field(
        ge=0,
        description="Exclusive end Unicode code point offset; must be greater than `start`.",
    )

    @model_validator(mode="after")
    def _check_span(self) -> "AnnotationCreate":
        if self.end < self.start:
            raise ValueError("end must be greater than or equal to start")
        return self


class Replacement(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "summary": "Replace one misread word",
                    "value": {"start": 3, "end": 7, "replacement": "café"},
                },
                {
                    "summary": "Pure insertion at a code-point boundary",
                    "value": {"start": 12, "end": 12, "replacement": " — note"},
                },
            ]
        },
    )

    start: int = Field(ge=0, description="Inclusive start code point in the pre-correction body.")
    end: int = Field(
        ge=0,
        description=(
            "Exclusive end code point. Equal start/end is a pure insertion. "
            "Intervals are half-open and all replacements are mutually "
            "non-overlapping."
        ),
    )
    replacement: str = Field(description="Replacement text; may be empty to delete the span.")

    @model_validator(mode="after")
    def _check_span(self) -> "Replacement":
        if self.end < self.start:
            raise ValueError("end must be greater than or equal to start")
        return self


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "summary": "Two disjoint local fixes at the current revision",
                    "value": {
                        "revision": 1,
                        "replacements": [
                            {"start": 3, "end": 7, "replacement": "café"},
                            {"start": 40, "end": 44, "replacement": "gare"},
                        ],
                    },
                }
            ]
        },
    )

    revision: int = Field(
        ge=0,
        description="Expected current revision. Stale revisions are rejected with HTTP 409.",
    )
    replacements: list[Replacement] = Field(
        min_length=1,
        max_length=MAX_EDITS_PER_CORRECTION,
        description=(
            f"At most {MAX_EDITS_PER_CORRECTION} mutually non-overlapping "
            "replacements, all interpreted in the pre-correction coordinates."
        ),
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    logger.info("database ready at startup")
    yield
    cancel_local_rebuilds()


app = FastAPI(
    title="OCR Accent-Aware Archive Search",
    version="1.0.0",
    description=APP_DESCRIPTION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "pages", "description": "Transactional OCR page writes and deletes."},
        {
            "name": "annotations",
            "description": (
                "Person/place labels anchored to Unicode code-point spans, with "
                "revision-fenced local corrections that preserve or invalidate "
                "the annotation coordinates."
            ),
        },
        {"name": "search", "description": "Word search over the active FTS5 generation."},
        {
            "name": "index",
            "description": "Background generation rebuild and atomic accent-policy switch.",
        },
        {"name": "ops", "description": "Health and documentation helpers."},
    ],
)


@app.exception_handler(StaleRevisionError)
async def stale_revision_handler(_request: Request, exc: StaleRevisionError):
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "error": "stale_revision",
            "page_id": exc.page_id,
            "expected_revision": exc.expected,
            "current_revision": exc.current,
        },
    )


@app.exception_handler(PageNotFoundError)
async def annotation_page_missing_handler(_request: Request, exc: PageNotFoundError):
    return JSONResponse(
        status_code=404,
        content={"detail": str(exc), "error": "page_not_found", "page_id": exc.page_id},
    )


@app.exception_handler(AnnotationError)
async def annotation_error_handler(_request: Request, exc: AnnotationError):
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc), "error": "annotation_validation"},
    )


@app.exception_handler(sqlite3.OperationalError)
async def sqlite_busy_handler(_request: Request, exc: sqlite3.OperationalError):
    logger.exception("sqlite operational error: %s", exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "SQLite is temporarily busy; the write lock is not held for "
            "the duration of a rebuild. Retry the request.",
            "error": str(exc),
        },
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/healthz", tags=["ops"], summary="Process and database health")
def health() -> dict:
    conn = connect(query_only=True)
    try:
        conn.execute("SELECT 1")
        generation = conn.execute(
            "SELECT value FROM settings WHERE key = 'active_policy'"
        ).fetchone()
        return {
            "status": "ok",
            "active_policy": generation[0] if generation else None,
            "pid": os.getpid(),
            "database": str(SETTINGS.db_path),
        }
    finally:
        conn.close()


@app.put(
    "/api/pages/{page_id}",
    tags=["pages"],
    summary="Insert or fully replace an OCR page by page ID",
    response_description="The page, current live index generation and change sequence.",
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "accented_page": {
                            "summary": "Accented initial OCR text",
                            "value": {
                                "title": "Crème brûlée à Paris",
                                "body": "Le café est près de la gare; le señor lit un naïve résumé.",
                            },
                        },
                        "correction_during_rebuild": {
                            "summary": "Correction to submit while a rebuild runs",
                            "value": {
                                "title": "Crème brûlée — corrected",
                                "body": "Le café fermé est près de la gare; le señor relit le résumé corrigé.",
                            },
                        },
                    }
                }
            }
        }
    },
)
def put_page(
    page_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=512,
            description="Stable OCR page ID, e.g. `ocr-001`.",
            examples=["ocr-001", "scan-berlin-042"],
        ),
    ],
    payload: Annotated[
        PageUpsert,
        Body(
            description="Title and body are stored with their original accents. "
            "The active policy only affects the index/query representation."
        ),
    ],
) -> dict:
    try:
        return upsert_page(page_id, payload.title, payload.body)
    except Exception:
        logger.exception("page upsert failed for %s", page_id)
        raise


@app.delete(
    "/api/pages/{page_id}",
    tags=["pages"],
    summary="Delete an OCR page by ID (idempotent)",
    response_description="Deletion committed to the canonical store and live index.",
)
def remove_page(
    page_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=512,
            description="Page ID to delete, e.g. `ocr-002`.",
            examples=["ocr-002", "scan-berlin-051"],
        ),
    ],
) -> dict:
    try:
        return delete_page(page_id)
    except Exception:
        logger.exception("page delete failed for %s", page_id)
        raise


@app.get(
    "/api/pages/{page_id}",
    tags=["pages"],
    summary="Read a committed OCR page by ID",
)
def read_page(
    page_id: Annotated[str, Path(min_length=1, max_length=512, examples=["ocr-001"])],
) -> dict:
    page = get_page(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")
    return page


@app.post(
    "/api/pages/{page_id}/annotations",
    tags=["annotations"],
    status_code=201,
    summary="Anchor a person/place label to a body code-point span",
    response_description=(
        "The stored annotation with the selected original text. Stale revisions "
        "return 409; coordinates outside the current body return 422."
    ),
)
def create_annotation(
    page_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=512,
            description="Page the label belongs to, e.g. `ocr-001`.",
            examples=["ocr-001"],
        ),
    ],
    payload: Annotated[
        AnnotationCreate,
        Body(
            description="Revision fence plus a half-open Unicode code-point interval. "
            "The server records the exact selected original text."
        ),
    ],
) -> dict:
    return add_annotation(
        page_id=page_id,
        expected_revision=payload.revision,
        label=payload.label,
        start=payload.start,
        end=payload.end,
    )


@app.post(
    "/api/pages/{page_id}/corrections",
    tags=["annotations"],
    summary="Apply a revision-fenced local correction and migrate annotation coordinates",
    response_description=(
        "The new revision and body plus the counts of migrated and invalidated "
        "annotations. Body, annotations, change log and the FTS index commit in "
        "one transaction."
    ),
)
def submit_correction(
    page_id: Annotated[
        str,
        Path(min_length=1, max_length=512, description="Page to correct.", examples=["ocr-001"]),
    ],
    payload: Annotated[
        CorrectionRequest,
        Body(
            description="Expected revision plus up to 20 non-overlapping replacements, "
            "all interpreted in pre-correction coordinates."
        ),
    ],
) -> dict:
    return correct_page(
        page_id=page_id,
        expected_revision=payload.revision,
        raw_edits=[item.model_dump() for item in payload.replacements],
    )


@app.get(
    "/api/pages/{page_id}/annotations",
    tags=["annotations"],
    summary="Read the body with its active and invalidated annotations in one snapshot",
    response_description=(
        "Body, revision and annotations come from the same committed state. "
        "Use `?status=active` or `?status=invalidated` to filter."
    ),
)
def read_annotations(
    page_id: Annotated[
        str,
        Path(min_length=1, max_length=512, description="Page to inspect.", examples=["ocr-001"]),
    ],
    status: Annotated[
        str | None,
        Query(
            description="Optional annotation status filter.",
            examples=["active", "invalidated"],
        ),
    ] = None,
) -> dict:
    return list_annotations(page_id, status)


@app.get(
    "/api/search",
    tags=["search"],
    summary="Find pages whose title/body contains all queried words",
    response_description=(
        "Hits and titles/bodies are read in one SQLite snapshot from the same "
        "active generation and committed page state."
    ),
)
def search_pages(
    q: Annotated[
        str,
        Query(
            min_length=1,
            max_length=2048,
            description="One or more words. Matching is per word; all words must occur. "
            "Accents follow the active policy (`keep`, then `fold` after a switch).",
            openapi_examples={
                "exact_accent_keep": {
                    "summary": "Exact accent (keep policy)",
                    "value": "café",
                },
                "folded_spelling_after_switch": {
                    "summary": "Unaccented spelling (fold policy)",
                    "value": "cafe",
                },
                "two_words": {
                    "summary": "All words must match",
                    "value": "señor naïve",
                },
            },
        ),
    ],
    limit: Annotated[int, Query(ge=1, le=100, description="Maximum hits to return.")] = 20,
    offset: Annotated[int, Query(ge=0, description="Number of ordered hits to skip.")] = 0,
) -> dict:
    try:
        return search(q, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("search failed for query %r", q)
        raise


def _trigger_rebuild(
    request: RebuildRequest | None,
    accent_policy: AccentPolicy | None,
    policy: AccentPolicy | None,
) -> dict:
    body_policy = None
    if request is not None:
        if (
            request.accent_policy is not None
            and request.policy is not None
            and request.accent_policy != request.policy
        ):
            raise HTTPException(
                status_code=422, detail="accent_policy and policy must not conflict"
            )
        body_policy = request.accent_policy or request.policy
    if accent_policy is not None and policy is not None and accent_policy != policy:
        raise HTTPException(status_code=422, detail="accent_policy and policy must not conflict")
    query_policy = accent_policy or policy
    if body_policy is not None and query_policy is not None and body_policy != query_policy:
        raise HTTPException(
            status_code=422,
            detail="body accent_policy and query accent_policy must not conflict",
        )
    target_policy = body_policy or query_policy

    try:
        result = claim_rebuild(target_policy)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    worker_token = result.pop("worker_token", None)
    if result["created"] and worker_token:
        job = result["job"]
        try:
            start_rebuild_thread(
                job_id=job["job_id"],
                generation_id=job["generation_id"],
                policy=job["to_policy"],
                claim_token=worker_token,
            )
        except Exception as exc:
            fail_job(
                job["job_id"],
                worker_token,
                state="failed",
                error=f"could not start rebuild worker: {exc}",
                generation_id=job["generation_id"],
            )
            raise HTTPException(
                status_code=503, detail="could not start background rebuild worker"
            ) from exc
    return result


@app.post(
    "/api/index/rebuild",
    tags=["index"],
    status_code=202,
    summary="Switch accent policy by rebuilding an index generation in the background",
    response_description=(
        "The newly queued job or the already-running job. Duplicate triggers while "
        "a rebuild is active return the same job ID."
    ),
    openapi_extra={
        "requestBody": {
            "required": False,
            "content": {
                "application/json": {
                    "examples": {
                        "fold": {
                            "summary": "Fold Latin accents after rebuild",
                            "value": {"accent_policy": "fold"},
                        },
                        "keep": {
                            "summary": "Restore exact accents after rebuild",
                            "value": {"accent_policy": "keep"},
                        },
                    }
                }
            },
        }
    },
)
def trigger_rebuild(
    request: Annotated[
        RebuildRequest | None,
        Body(description="Target accent policy. The body may be omitted; then the "
        "opposite of the active policy is used."),
    ] = None,
    accent_policy: Annotated[
        AccentPolicy | None,
        Query(description="Query-string alternative to the JSON body.", examples=["fold"]),
    ] = None,
    policy: Annotated[
        AccentPolicy | None,
        Query(description="Alias of accent_policy.", include_in_schema=False),
    ] = None,
) -> dict:
    return _trigger_rebuild(request, accent_policy, policy)


@app.post(
    "/api/index/switch",
    tags=["index"],
    status_code=202,
    include_in_schema=False,
)
def switch_alias(
    request: Annotated[RebuildRequest | None, Body()] = None,
    accent_policy: Annotated[AccentPolicy | None, Query()] = None,
    policy: Annotated[AccentPolicy | None, Query()] = None,
) -> dict:
    return _trigger_rebuild(request, accent_policy, policy)


@app.get(
    "/api/index/status",
    tags=["index"],
    summary="Inspect active generation, running rebuild and recent history",
    response_description=(
        "`active_policy` is the rule used by the very next query. "
        "`current_rebuild_job` is the single cross-process job, if any."
    ),
)
def index_status() -> dict:
    try:
        return get_status()
    except Exception:
        logger.exception("status query failed")
        raise


@app.get(
    "/api/index/jobs/{job_id}",
    tags=["index"],
    summary="Get one rebuild job by ID",
)
def index_job(
    job_id: Annotated[str, Path(min_length=1, description="Rebuild job UUID.")],
) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="rebuild job not found")
    return job
