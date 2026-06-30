"""Orchestration: create tables, compare row counts, sync data (streaming)."""
from . import store
from . import oracle_client as ora
from . import pg_client as pg
from . import types_map


def _cfgs():
    ocfg = store.get("oracle")
    pcfg = store.get("pg")
    if not ocfg or not pcfg:
        raise RuntimeError("Oracle and Postgres connections must be saved first.")
    return ocfg, pcfg


def pg_name(oracle_table):
    """Default Postgres name for an Oracle table (rename UI comes in phase 2)."""
    return oracle_table.lower()


def create_tables(table_names):
    ocfg, pcfg = _cfgs()
    schema = pcfg["target_schema"]
    owner = ora.owner(ocfg)
    results = []
    ocon = ora.connect(ocfg)
    pcon = pg.connect(pcfg)
    try:
        pg.ensure_schema(pcon, schema)
        for t in table_names:
            pgt = pg_name(t)
            try:
                cols = ora.get_columns(ocon, owner, t)
                if not cols:
                    raise RuntimeError("no columns found (check schema/owner)")
                pk = [c.lower() for c in ora.get_pk(ocon, owner, t)]
                coldefs = [types_map.coldef(c, c["name"].lower()) for c in cols]
                existed = pg.table_exists(pcon, schema, pgt)
                pg.create_table(pcon, schema, pgt, coldefs, pk)
                results.append(dict(table=t, pg_table=pgt,
                                    status="exists" if existed else "created"))
            except Exception as e:  # noqa: BLE001
                pcon.rollback()
                results.append(dict(table=t, pg_table=pgt, status="error", error=str(e)))
    finally:
        ocon.close()
        pcon.close()
    return results


def compare(table_names):
    ocfg, pcfg = _cfgs()
    schema = pcfg["target_schema"]
    owner = ora.owner(ocfg)
    ocon = ora.connect(ocfg)
    pcon = pg.connect(pcfg)
    out = []
    try:
        for t in table_names:
            pgt = pg_name(t)
            try:
                oc = ora.row_count(ocon, owner, t)
            except Exception as e:  # noqa: BLE001
                out.append(dict(table=t, pg_table=pgt, oracle=None, pg=None,
                                diff=None, error=str(e)))
                continue
            try:
                pc = pg.row_count(pcon, schema, pgt)
            except Exception:  # table not created yet
                pcon.rollback()
                pc = None
            out.append(dict(
                table=t, pg_table=pgt, oracle=oc, pg=pc,
                diff=(None if pc is None else oc - pc),
                match=(pc is not None and oc == pc),
            ))
    finally:
        ocon.close()
        pcon.close()
    return out


def sync_stream(table_names):
    """Generator of progress dict-events for an SSE stream."""
    ocfg, pcfg = _cfgs()
    schema = pcfg["target_schema"]
    owner = ora.owner(ocfg)
    ocon = ora.connect(ocfg)
    pcon = pg.connect(pcfg)
    try:
        for t in table_names:
            pgt = pg_name(t)
            yield dict(type="table_start", table=t, pg_table=pgt)
            try:
                cols = [c["name"] for c in ora.get_columns(ocon, owner, t)]
                pgcols = [c.lower() for c in cols]
                pg.truncate(pcon, schema, pgt)
                batches = ora.fetch_batches(ocon, owner, t, cols)
                last = 0
                for total in pg.copy_load(pcon, schema, pgt, pgcols, batches):
                    last = total
                    yield dict(type="progress", table=t, rows=total)
                oc = ora.row_count(ocon, owner, t)
                pc = pg.row_count(pcon, schema, pgt)
                yield dict(type="table_done", table=t, rows=last,
                           oracle=oc, pg=pc, match=(oc == pc))
            except Exception as e:  # noqa: BLE001
                try:
                    pcon.rollback()
                except Exception:
                    pass
                yield dict(type="table_error", table=t, error=str(e))
        yield dict(type="all_done")
    finally:
        ocon.close()
        pcon.close()
