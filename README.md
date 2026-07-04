# Data Flow

Selective **Oracle → PostgreSQL** table & data migration with a small web GUI.

Pick a subset of Oracle tables, map/rename columns, create them in Postgres,
compare row counts, and run a live, resumable, chunked sync with retry/backoff,
row-level error capture, and a full history log — all from the browser. Pure
Python, so it runs identically on Windows and on the Ubuntu server next to
Postgres. **No ora2pg / Perl / Oracle Instant Client required**
(`python-oracledb` runs in thin mode; `psycopg` v3 does the bulk load via
`COPY`).

## Install & run
From the project root:

**Linux / Ubuntu server**
```bash
chmod +x run.sh
./run.sh
```
**Windows**
```bat
run.bat
```
Both create a venv, install the package, and start the app via the `data-flow`
command. Then open `http://localhost:8000` (or `http://<server-ip>:8000` on the
LAN).

To install manually:
```bash
pip install -e .
data-flow            # launches the server
```
Environment overrides: `DATAFLOW_HOST`, `DATAFLOW_PORT`, `DATAFLOW_DB`
(path to the SQLite state file; defaults to `./data-flow.db`).

## What it does

1. **Connections** — enter + test Oracle and Postgres connections.
2. **Tables** — list Oracle tables, filter, pick which to migrate, save the
   selection. A "Show row counts (approx.)" button pulls a fast, single-query
   row-count estimate per table from Oracle's optimizer statistics — safe to
   use even on schemas with thousands of tables, since it never does a live
   `COUNT(*)` per table.
3. **Columns** — per-table column mapping: rename columns, rename the target
   table, skip columns, override the Postgres type, and flag a table for
   "fast load" (drop constraints/indexes/FKs before sync, recreate after).
   Detects **schema drift** — if the mapping changes after a table was
   created, it's flagged before the next sync so you can recreate deliberately
   rather than get a silent surprise.
4. **Migrate & Sync**
   - Create the selected tables in Postgres (or recreate them to apply a
     changed mapping) and compare row counts side by side.
   - **Pre-flight checks** before every sync: both connections reachable,
     Oracle read access on each source table, Postgres CREATE/INSERT/ownership
     on the target — a failing check blocks the sync before it touches
     anything, with the specific check and reason reported.
   - **Dry run** — preview the DDL a create/recreate would issue, current row
     counts on both sides, and (for fast-load tables) exactly which
     constraints/indexes would be dropped and recreated — without executing
     anything.
   - **Chunked, resumable sync** — data loads in fixed 10,000-row chunks via
     `COPY`, with a checkpoint recorded in SQLite after each chunk commits. An
     interrupted sync resumes from its last checkpoint instead of reloading
     the whole table (with a "start fresh" override if you want a normal
     truncate+reload instead).
   - **Row-level error capture** — if a chunk's bulk `COPY` fails for a
     data-level reason (bad encoding, a constraint violation not caught by
     pre-flight, a type mismatch), it falls back to inserting that chunk
     row-by-row so one bad row doesn't lose the rest of the table. Skipped
     rows are logged with their row reference and error.
   - **Retry with backoff** on transient connection errors (3 retries,
     exponential 1s/2s/4s) for both Oracle and Postgres calls.
   - **Live progress over SSE**: rows loaded, rows/sec throughput, and an ETA,
     in addition to retry/constraint-drop/restore events.
5. **History** — every sync run (table, start/end time, duration, rows
   loaded/skipped, status) is logged to SQLite and browsable here, so you can
   spot which tables are slow or fail often. Row-level error detail is
   viewable inline and the full run log (summary + row errors) can be
   downloaded as a text file for sharing outside the app.

A minimal footer with attribution appears on every page.

State (connections, column mappings, checkpoints, sync history) lives in
`data-flow.db` (SQLite, gitignored).

## Project layout
```
data-flow/
├─ pyproject.toml          # packaging + the `data-flow` entry point
├─ run.sh / run.bat
├─ LICENSE                 # Apache 2.0
├─ README.md
└─ src/dataflow/
   ├─ cli.py               # console command -> launches uvicorn
   ├─ main.py              # FastAPI routes (config, test, tables, mapping, migrate, sync/SSE, history)
   ├─ engine.py            # orchestration: create/recreate, compare, dry run, chunked sync stream
   ├─ oracle_client.py     # Oracle connect + metadata + chunked/ordered reads (thin mode)
   ├─ pg_client.py         # Postgres connect + DDL + COPY/row-insert load + constraint introspection (psycopg3)
   ├─ mapping.py           # per-table column rename/skip/type overrides + drift fingerprinting
   ├─ types_map.py         # Oracle -> Postgres type translation
   ├─ depgraph.py          # topological sort for FK-safe constraint recreation order
   ├─ retry.py             # transient-error retry/backoff, shared by both DB clients
   ├─ preflight.py         # connectivity + privilege checks run before a sync starts
   ├─ history.py           # SQLite: sync checkpoints, run history, row-level error log
   ├─ store.py             # SQLite key/value settings store (connections, selection, mappings)
   └─ static/              # UI (index.html, app.js, style.css, favicon.ico)
```

## Notes / limits
- Single-user, no auth — **bind to localhost or a trusted LAN only.** Connection
  passwords are stored in plaintext in the SQLite state file; don't expose publicly.
- `types_map.py` covers common Oracle types; extend it for exotic ones.
- Row-count estimates on the Tables tab come from Oracle's optimizer
  statistics, not a live count — they can be stale on tables with heavy recent
  DML but stay fast even on schemas with thousands of tables. Compare/sync
  elsewhere in the app always use exact counts.
- Disk space estimation before a sync is intentionally out of scope.

## Roadmap
- Optional **ora2pg-backed DDL** path as an alternative to `types_map.py`.
- Parallel per-table sync (currently sequential within one run).

## License
Apache License 2.0 — see [LICENSE](LICENSE).

© 2026 Sadman Rahamat Bishal
