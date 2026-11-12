# OCR Accent-Aware Archive Search

FastAPI + SQLite FTS5 service for an OCR archive whose searchable accent
policy can be changed online. Page corrections and deletes continue to commit
while a new index is being built; the old index serves searches until the new
generation has caught up and is switched atomically.

Researchers can also anchor **person/place annotations** to Unicode code-point
spans of a page body. Local text corrections keep the annotation coordinates
valid by translating them, and invalidate only the labels the edit actually
touches — without ever holding a database write lock for the duration of the
coordinate computation.

## Start with Docker Compose

```bash
docker compose up --build -d
```

Open **<http://localhost:8000/docs>**. The container runs exactly two uvicorn
API worker processes against one WAL-mode SQLite file on the named volume
`archive-data`.

```bash
docker compose ps
docker compose exec api ps -ef | grep '[u]vicorn'
```

The `api` container is the single writer-capable deployment; SQLite is not
shared between separate containers. The two processes are uvicorn workers in
this one container.

To make a rebuild visibly long enough to submit edits by hand during it:

```bash
REBUILD_BATCH_DELAY_MS=250 docker compose up --build -d
```

The delay is a demo-only aid and is `0` by default. Reset demo data with:

```bash
docker compose down -v
```

## Swagger demo

Every step is available with **Try it out** on `/docs`. The OpenAPI description
also contains the full demo narrative and accented request examples.

### 1. Write two accented OCR pages under the initial `keep` policy

```bash
BASE=http://localhost:8000

curl -sS -X PUT "$BASE/api/pages/ocr-001" \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Crème brûlée à Paris",
    "body": "Le café est près de la gare; le señor lit un naïve résumé."
  }'

curl -sS -X PUT "$BASE/api/pages/ocr-002" \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "A plain cafe",
    "body": "This page uses cafe and naive without accents."
  }'
```

### 2. Search with the initial exact-accent rule

```bash
curl --get "$BASE/api/search" --data-urlencode 'q=café'
# ocr-001 is returned.

curl --get "$BASE/api/search" --data-urlencode 'q=cafe'
# Only ocr-002 matches. The accented café is not folded under keep.
```

Word queries are literal words. Multiple words are ANDed; FTS query operators
from the request are never interpreted.

### 3. Trigger the background switch to `fold`

```bash
curl -sS -X POST "$BASE/api/index/rebuild" \
  -H 'Content-Type: application/json' \
  -d '{"accent_policy": "fold"}'
```

The response is HTTP `202` and contains a `job.job_id`. A duplicate trigger
while the job is active returns the **same** job ID with
`"reused_existing_rebuild": true`.

### 4. Correct and delete during the rebuild

Run these while `GET /api/index/status` reports `building` or `catching_up`:

```bash
# Correction: replaces the page and becomes visible in both the old live index
# and, after catch-up, the new generation.
curl -sS -X PUT "$BASE/api/pages/ocr-001" \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Crème brûlée — corrected OCR",
    "body": "Le café fermé est près de la gare; le señor relit le résumé corrigé."
  }'

# Deletion must remain deleted; the old snapshot scan must not revive it.
curl -sS -X DELETE "$BASE/api/pages/ocr-002"
```

These writes commit immediately. The rebuild scan does not hold a lock for its
duration, so editing is not blocked by it.

### 5. Watch the live status and old index

```bash
curl -sS "$BASE/api/index/status"
curl --get "$BASE/api/search" --data-urlencode 'q=cafe'
```

While the job is running, `active_policy` is still `keep`,
`rebuild_in_progress` is `true`, and `current_rebuild_job` shows progress:
`queued → building → catching_up → activating → succeeded`.

### 6. Query after the switch

Once `current_rebuild_job` is `null` and `active_policy` is `fold`:

```bash
curl --get "$BASE/api/search" --data-urlencode 'q=cafe'
# Returns ocr-001 with the corrected body: café matches cafe.

curl --get "$BASE/api/search" --data-urlencode 'q=resume'
# Folded résumé matches.
```

Deleted `ocr-002` remains absent, and returned titles/bodies are the corrected
committed versions from the same SQLite snapshot as the hit.

### 7. Anchor a person/place label to the body

Coordinates are **Unicode code-point** half-open intervals `[start, end)` into
the current body; the server stores the exact selected original text and the
revision the label is anchored to. Every write carries the **expected
revision** — a stale revision is rejected with HTTP `409`.

```bash
# Body: "Le café est près de la gare; le señor lit un naïve résumé."
# "señor" starts at code point 35, "gare" at 26.
curl -sS -X POST "$BASE/api/pages/ocr-001/annotations" \
  -H 'Content-Type: application/json' \
  -d '{"revision": 1, "label": "person", "start": 35, "end": 40}'
# 201 -> annotation id, selected_text "señor", status "active"

curl -sS -X POST "$BASE/api/pages/ocr-001/annotations" \
  -H 'Content-Type: application/json' \
  -d '{"revision": 1, "label": "place", "start": 26, "end": 30}'
```

### 8. Apply a local correction and watch coordinates move

`POST /api/pages/{page_id}/corrections` takes the expected revision and up to
**20 mutually non-overlapping** replacements, all interpreted in the
*pre-correction* coordinate space. Insertion points may not coincide or land
inside another replacement.

```bash
# Replace "café" [3,7) with the longer "bistrot". Labels later in the text
# have both endpoints translated; their selected text stays identical.
curl -sS -X POST "$BASE/api/pages/ocr-001/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
    "revision": 1,
    "replacements": [{"start": 3, "end": 7, "replacement": "bistrot"}]
  }'
# -> revision 2, annotations_migrated 2, annotations_invalidated 0
```

Coordinate rules:

* An insertion at an annotation **start** counts as *before* it (both
  endpoints shift); an insertion at the **end** counts as *after* it.
* A replacement that intersects an annotation, or an insertion strictly
  inside one, sets that annotation to `invalidated` and keeps its original
  selected text. Edits that merely touch a boundary translate the label.
* Replaying the same request against the old revision returns `409` and
  changes nothing.

### 9. Query body and labels from one snapshot

```bash
curl -sS "$BASE/api/pages/ocr-001/annotations"
# Optional filter: ?status=active or ?status=invalidated
```

The response contains the committed `body`, its `revision`, and both active
and invalidated annotations from the **same** SQLite snapshot. Every active
span still slices exactly its `selected_text` out of the live body. All rows
are durable in SQLite and survive a container restart.

A full `PUT` rewrite or a `DELETE` invalidates every annotation on the page
(`annotations_invalidated` is returned); re-creating the same page ID never
revives the old labels.

## Accent examples

| Document word | `keep` query | `fold` query |
|---|---|---|
| café | café | cafe or café |
| señor | señor | senor or señor |
| naïve | naïve | naive or naïve |
| résumé | résumé | resume or résumé |
| Crème brûlée | crème / brûlée | creme / brulee |
| İstanbul | İstanbul | Istanbul |

`keep` normalises composed/decomposed spellings to NFC but never removes a
diacritic. `fold` removes combining marks attached to Latin base letters only;
non-Latin combining marks are preserved.

## API summary

| Method | Path | Purpose |
|---|---|---|
| `PUT` | `/api/pages/{page_id}` | Insert/replace an OCR title and body; invalidates all annotations on the page. |
| `DELETE` | `/api/pages/{page_id}` | Idempotent delete; invalidates all annotations on the page. |
| `GET` | `/api/pages/{page_id}` | Read a committed page. |
| `POST` | `/api/pages/{page_id}/annotations` | Anchor a `person`/`place` label to a code-point span at an expected revision. |
| `GET` | `/api/pages/{page_id}/annotations` | Snapshot of the body with active and invalidated annotations (`?status=` filter). |
| `POST` | `/api/pages/{page_id}/corrections` | Revision-fenced local correction (≤20 non-overlapping replacements) that migrates or invalidates annotations. |
| `GET` | `/api/search?q=word` | Word search over the active FTS generation. |
| `POST` | `/api/index/rebuild` | Queue/switch accent policy through a background rebuild. |
| `GET` | `/api/index/status` | Active policy/generation, current job, progress, history. |
| `GET` | `/api/index/jobs/{job_id}` | One rebuild job. |
| `GET` | `/healthz` | Health and active policy. |

`POST /api/index/rebuild` also accepts `?accent_policy=fold` for convenience.
If the body/parameter is omitted, the opposite of the active policy is used.
The alias `POST /api/index/switch` is available but omitted from the Swagger
schema.

## Correctness model

* **Durable source of truth** — `pages` stores the latest committed title/body
  and `changes` is an append-only sequence log containing the full content of
  every write/delete.
* **One active generation** — each generation owns a separate FTS5 table.
  `settings.active_generation_id` and `settings.active_policy` are switched in
  the same short transaction that marks the new generation active.
* **Writes during rebuild** — a write transaction updates `pages`, appends to
  `changes`, and updates the currently active FTS table. It never writes to a
  generation under construction. Therefore no scan-length write lock is needed,
  and a failed worker cannot corrupt the serving index.
* **Snapshot baseline** — the rebuild reads its baseline in a pinned WAL read
  transaction. Writers continue to commit in WAL while that snapshot is open.
* **Catch-up** — the worker applies every change-log row after the snapshot,
  in sequence, to its private shadow FTS table. It does this in small write
  transactions, with heartbeats.
* **Atomic swap** — the final short `BEGIN IMMEDIATE` transaction applies the
  last residual changes, verifies shadow/page document counts, updates the
  pointer/policy, and marks the job succeeded. Only then does a new request see
  the new generation and policy.
* **Snapshot-consistent reads** — a search begins one SQLite read transaction,
  reads the active pointer/policy, the FTS hits and the canonical bodies in that
  same transaction. Hits and returned text therefore come from one committed
  state, even if a swap commits concurrently.
* **One rebuild across processes** — `rebuild_jobs` has a partial unique index
  on active states. Claiming is done under `BEGIN IMMEDIATE`, so two API
  processes cannot start two jobs; the loser returns the current job.
* **Failure/interruption** — the previous active generation is never changed
  until final commit. A failed job marks only its shadow generation failed. A
  new process marks a dead owner's job interrupted at startup (dead PID or a
  different container host), so the rebuild can be retriggered immediately.
  Canonical pages and the change log survive in SQLite.
* **Fencing** — the final activation re-checks the job's claim token and
  active state under the write lock, so a stale worker that resumes cannot
  switch an obsolete generation after a newer job has been claimed.

FTS rowids are taken from the canonical page rowid and every log entry carries
that rowid, making updates/deletes O(index lookup) rather than rescanning the
FTS table by page ID.

Retired FTS tables are deliberately retained rather than dropped in the swap
transaction. This avoids schema-lock surprises for an already-running query;
generation history is visible in `/api/index/status`.

### Annotation and local-correction model

* **Durable labels** — the `annotations` table stores the label, code-point
  half-open interval, selected original text, `active`/`invalidated` status,
  the anchor/creation revisions and an invalidation reason. Nothing lives only
  in process memory, so a restart preserves every label.
* **Revision fencing** — annotation creation and local correction both send
  the revision the caller read. Under the final write lock the current
  revision (and, for corrections, the exact body) is re-checked; a mismatch
  fails with HTTP `409` and the whole transaction rolls back.
* **No scan-length write lock** — a correction first opens a read-only
  snapshot, validates the replacements and computes every annotation
  migration in pure in-memory code. Only then does it take one short
  `BEGIN IMMEDIATE` transaction. Annotations can therefore be added while a
  correction is being calculated.
* **Nothing added during computation is lost** — inside the write
  transaction the active annotation set is re-read; if labels appeared during
  the unlocked computation, their migration is recomputed before commit. A
  concurrently committed annotation either joins the migration or its stale
  fence is rejected explicitly; it is never silently dropped.
* **Atomic commit across both worker processes** — the corrected page body,
  every translated/invalidated annotation row, the `changes` log entry
  (`op='correct'`, with the edit list and migration counts in `details`) and
  the active FTS row update commit in a single SQLite transaction. A failure
  makes none of it visible. The two uvicorn workers are serialised by the
  `BEGIN IMMEDIATE` write lock; concurrent same-revision corrections have
  exactly one winner, the rest receive `409`.
* **Coordinate semantics** — edits are sorted and applied in pre-correction
  coordinates. An insertion at a span start is *before* the span; at the end
  it is *after* it. A half-open intersection with a replacement, or an
  insertion strictly inside the span, invalidates the label while preserving
  its original text.
* **Bulk invalidation** — a full-page `PUT` or a `DELETE` invalidates all
  active annotations in the same write transaction. The rows are retained,
  and creating a page with the same ID afterwards starts with zero active
  labels rather than resurrecting old ones.
* **Rebuild interplay** — `correct` rows in the change log are replayed by a
  background rebuild exactly like `upsert` rows, so the shadow generation
  indexes the corrected body and the final swap stays consistent with the
  canonical pages.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ARCHIVE_DB_PATH` | `./data/archive.db` locally, `/data/archive.db` in Docker | SQLite path. |
| `REBUILD_LEASE_SECONDS` | `20` | Heartbeat lease before a trigger may reclaim a job. |
| `REBUILD_BATCH_SIZE` | `500` | Scan/catch-up batch size. |
| `REBUILD_BATCH_DELAY_MS` | `0` | Demo-only pacing per batch. |
| `DB_BUSY_TIMEOUT_SECONDS` | `30` | SQLite writer wait. |

## Local development and tests

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
ARCHIVE_DB_PATH=/tmp/archive-dev.db \
  uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2
```

```bash
pytest -q
```

The tests exercise exact vs folded search, a rebuild that finishes with a
correction and a deletion submitted during the scan, duplicate job idempotency,
and restart/stale-job recovery. They use temporary SQLite files.

The annotation suite (`tests/test_annotations.py`) covers label creation and
selected-text snapshots, stale-revision `409` fencing, coordinate translation
for disjoint edits, the start-before/end-after insertion rule, invalidation by
intersecting replacements and inner insertions, overlap/coincident-point
rejection, full-rewrite/delete invalidation without resurrection, restart
durability (including the legacy-schema migration), a correction replayed into
a concurrent rebuild, and cross-thread concurrency for annotations racing a
correction and for competing corrections.
