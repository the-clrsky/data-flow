"""FastAPI backend for the Oracle -> Postgres migration GUI (MVP)."""
import json
import os

from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict

from . import store
from . import oracle_client as ora
from . import pg_client as pg
from . import engine

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


# ---------- migrate ----------
@app.post("/api/create-tables")
def create_tables(sel: Selection):
    try:
        return {"results": engine.create_tables(sel.tables)}
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
def sync(tables: str = ""):
    names = [t for t in tables.split(",") if t] or store.get("selection", [])

    def event_stream():
        try:
            for ev in engine.sync_stream(names):
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'fatal', 'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- static UI ----------
@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
