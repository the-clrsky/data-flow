"""FastAPI backend for the Oracle -> Postgres migration GUI (MVP)."""
import json
import os
from datetime import datetime
from decimal import Decimal

from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict

from . import store
from . import oracle_client as ora
from . import pg_client as pg
from . import engine
from . import history

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Data Flow")


# ---------- models ----------
class OracleCfg(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    host: str
    port: int = 1521
    service: str
    user: str
    password: str
    schema_: str | None = Field(default=None, alias="schema")  # owner; defaults to user


class PgCfg(BaseModel):
    host: str
    port: int = 5432
    dbname: str
    user: str
    password: str
    target_schema: str = "public"


class Selection(BaseModel):
    tables: list[str]


class ColumnMapping(BaseModel):
    name: str
    target: str | None = None
    skip: bool = False
    pg_type: str | None = None


class TableMapping(BaseModel):
    pg_table: str | None = None
    columns: list[ColumnMapping]
    fast_load: bool = False


def _json_default(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _mask(cfg):
    if not cfg:
        return None
    c = dict(cfg)
    if c.get("password"):
        c["password"] = "********"
    return c


# ---------- config ----------
@app.get("/api/config")
def get_config():
    return {
        "oracle": _mask(store.get("oracle")),
        "pg": _mask(store.get("pg")),
        "selection": store.get("selection", []),
    }


@app.post("/api/config/oracle")
def save_oracle(cfg: OracleCfg):
    store.put("oracle", cfg.model_dump(by_alias=True))
    return {"ok": True}


@app.post("/api/config/pg")
def save_pg(cfg: PgCfg):
    store.put("pg", cfg.model_dump())
    return {"ok": True}


@app.post("/api/test/oracle")
def test_oracle():
    cfg = store.get("oracle")
    if not cfg:
        return {"ok": False, "error": "Save Oracle connection first."}
    try:
        ora.test(cfg)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.post("/api/test/pg")
def test_pg():
    cfg = store.get("pg")
    if not cfg:
        return {"ok": False, "error": "Save Postgres connection first."}
    try:
        pg.test(cfg)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ---------- tables / selection ----------
@app.get("/api/oracle/tables")
def oracle_tables():
    cfg = store.get("oracle")
    if not cfg:
        return JSONResponse({"error": "Save Oracle connection first."}, status_code=400)
    try:
        return {"tables": ora.list_tables(cfg)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/selection")
def save_selection(sel: Selection):
    store.put("selection", sel.tables)
    return {"ok": True, "count": len(sel.tables)}


@app.get("/api/oracle/row-count-estimates")
def oracle_row_count_estimates():
    try:
        return {"counts": engine.oracle_row_count_estimates()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------- column mapping ----------
@app.get("/api/table/{table}/columns")
def table_columns(table: str):
    try:
        return engine.table_columns(table)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/table/{table}/mapping")
def save_table_mapping(table: str, m: TableMapping):
    engine.save_mapping(table, m.pg_table, [c.model_dump() for c in m.columns], m.fast_load)
    return {"ok": True}


@app.get("/api/mappings")
def mappings_status(tables: str = ""):
    names = [t for t in tables.split(",") if t] or store.get("selection", [])
    try:
        return {"rows": engine.mapping_status(names)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------- migrate ----------
@app.post("/api/create-tables")
def create_tables(sel: Selection):
    try:
        return {"results": engine.create_tables(sel.tables)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/recreate-tables")
def recreate_tables(sel: Selection):
    try:
        return {"results": engine.recreate_tables(sel.tables)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/compare")
def compare(tables: str = ""):
    names = [t for t in tables.split(",") if t] or store.get("selection", [])
    try:
        return {"rows": engine.compare(names)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/sync")
def sync(tables: str = "", fresh: str = ""):
    names = [t for t in tables.split(",") if t] or store.get("selection", [])
    fresh_tables = {t for t in fresh.split(",") if t}

    def event_stream():
        try:
            for ev in engine.sync_stream(names, fresh_tables=fresh_tables):
                yield f"data: {json.dumps(ev, default=_json_default)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'fatal', 'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- checkpoints / dry run ----------
@app.get("/api/checkpoints")
def checkpoints(tables: str = ""):
    names = [t for t in tables.split(",") if t] or store.get("selection", [])
    return {"rows": engine.checkpoint_status(names)}


@app.get("/api/dry-run")
def dry_run(tables: str = ""):
    names = [t for t in tables.split(",") if t] or store.get("selection", [])
    try:
        return {"rows": engine.dry_run(names)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------- sync history ----------
@app.get("/api/sync-runs")
def sync_runs(limit: int = 200):
    return {"runs": history.list_runs(limit)}


@app.get("/api/sync-runs/{run_id}/errors")
def sync_run_errors(run_id: int):
    return {"errors": history.list_row_errors(run_id)}


@app.get("/api/sync-runs/{run_id}/export")
def sync_run_export(run_id: int):
    run = history.get_run(run_id)
    if not run:
        return JSONResponse({"error": "run not found"}, status_code=404)
    errors = history.list_row_errors(run_id, limit=100000)

    def ts(v):
        return datetime.fromtimestamp(v).isoformat(sep=" ", timespec="seconds") if v else "—"

    lines = [
        f"Sync run #{run['id']} — {run['table']} -> {run['pg_table']}",
        f"Status: {run['status']}",
        f"Started: {ts(run['started_at'])}",
        f"Ended: {ts(run['ended_at'])}",
        f"Duration: {run['duration']:.1f}s" if run["duration"] is not None else "Duration: —",
        f"Rows loaded: {run['rows_loaded']}",
        f"Rows skipped: {run['rows_skipped']}",
        "",
        f"Row errors ({len(errors)}):",
    ]
    for e in errors:
        lines.append(f"  row {e['row_ref']}: {e['error']}")
    body = "\n".join(lines) + "\n"
    filename = f"sync_run_{run_id}_{run['table']}.txt"
    return PlainTextResponse(body, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------- static UI ----------
@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))

@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(HERE, "static", "favicon.ico"))

app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
