# Data Flow

Selective **Oracle → PostgreSQL** table & data migration with a small web GUI.

Pick a subset of Oracle tables, create them in Postgres, compare row counts, and
run a live truncate-and-reload sync — all from the browser. Pure Python, so it
runs identically on Windows and on the Ubuntu server next to Postgres.
**No ora2pg / Perl / Oracle Instant Client required** (`python-oracledb` runs in
thin mode; `psycopg` v3 does the bulk load via `COPY`).

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
2. **Tables** — list Oracle tables, pick which to migrate, save the selection.
3. **Migrate & Sync** — create the selected tables in Postgres, compare row
   counts side by side, and run a streamed sync with live per-table progress.

State (connections, selection) lives in `data-flow.db` (SQLite, gitignored).

## Project layout
```
data-flow/
├─ pyproject.toml          # packaging + the `data-flow` entry point
├─ run.sh / run.bat
├─ README.md
└─ src/dataflow/
   ├─ cli.py               # console command -> launches uvicorn
   ├─ main.py              # FastAPI routes (config, test, tables, create, compare, sync/SSE)
   ├─ engine.py            # orchestration: create tables, compare, stream sync
   ├─ oracle_client.py     # Oracle connect + metadata + streaming reads (thin mode)
   ├─ pg_client.py         # Postgres connect + DDL + COPY load + counts (psycopg3)
   ├─ types_map.py         # Oracle -> Postgres type translation
   ├─ store.py             # SQLite settings store
   └─ static/              # UI (index.html, app.js, style.css)
```

## MVP scope & roadmap
Done: connections, table browser, table creation (PK only), row-count compare,
selective/all sync over SSE.

Next:
- **Per-table rename + column select/rename page** (currently 1:1, lowercased names).
- **FK / index handling** — drop-before-load, recreate-after, for faster big-table syncs.
- Optional **ora2pg-backed DDL** path as an alternative to `types_map.py`.

## Notes / limits
- Single-user, no auth — **bind to localhost or a trusted LAN only.** Connection
  passwords are stored in plaintext in the SQLite state file; don't expose publicly.
- Tables are created with **PK only** (no FKs/indexes yet), which keeps the
  truncate+reload loop simple.
- `types_map.py` covers common Oracle types; extend it for exotic ones.

## License
Not yet decided.
