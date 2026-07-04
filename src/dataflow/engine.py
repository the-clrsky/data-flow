"""Orchestration: create tables, compare row counts, sync data (streaming)."""
import time

from . import store
from . import oracle_client as ora
from . import pg_client as pg
from . import types_map
from . import mapping
from . import depgraph
from . import retry
from . import history
from . import preflight

CHUNK_SIZE = 10_000  # rows per COPY chunk during sync; adjust here if needed


def _retry_hook():
    """A small self-contained event queue so retry attempts happening inside
    a plain (non-generator) DB call can still surface as SSE events at the
    next point the caller yields. Retries themselves happen synchronously
    (including the backoff sleep) inside the call, so events show up as a
    burst right after it resolves rather than truly live during the wait -
    still enough to explain a multi-second pause instead of an unexplained
    stall."""
    queue = []

    def on_retry(label, attempt, max_retries, exc, delay):
        queue.append(dict(type="retry", label=label, attempt=attempt,
                           max_retries=max_retries, delay=delay, error=str(exc)))

    def drain():
        while queue:
            yield queue.pop(0)

    return on_retry, drain


def _cfgs():
    ocfg = store.get("oracle")
    pcfg = store.get("pg")
    if not ocfg or not pcfg:
        raise RuntimeError("Oracle and Postgres connections must be saved first.")
    return ocfg, pcfg


def _ora_cfg():
    ocfg = store.get("oracle")
    if not ocfg:
        raise RuntimeError("Save Oracle connection first.")
    return ocfg


def pg_name(oracle_table):
    """Default Postgres name for an Oracle table (overridden by mapping.py)."""
    return mapping.default_table_name(oracle_table)


def table_columns(table):
    """Live Oracle columns for a table, merged with the stored mapping
    overrides. Used by the column-mapping page."""
    ocfg = _ora_cfg()
    owner = ora.owner(ocfg)
    ocon = ora.connect(ocfg)
    try:
        cols = ora.get_columns(ocon, owner, table)
        if not cols:
            raise RuntimeError("no columns found (check schema/owner/table name)")
        pgt, resolved = mapping.resolve(table, cols)
        return dict(
            table=table,
            pg_table=pgt,
            pg_type_choices=types_map.PG_TYPE_CHOICES,
            fast_load=mapping.get(table)["fast_load"],
            columns=[
                dict(name=c["name"], dtype=c["dtype"], prec=c["prec"], scale=c["scale"],
                     nullable=c["nullable"], target=c["target"], skip=c["skip"],
                     pg_type=c["pg_type"], pg_type_override=c["pg_type_override"],
                     default_pg_type=c["default_pg_type"])
                for c in resolved
            ],
        )
    finally:
        ocon.close()


def save_mapping(table, pg_table, columns, fast_load=False):
    return mapping.save(table, pg_table, columns, fast_load)


def mapping_status(table_names):
    """Per-table summary for the "at a glance" UI: rename/skip counts and
    whether the live mapping has drifted from what the table was created with."""
    if not table_names:
        return []
    ocfg = _ora_cfg()
    owner = ora.owner(ocfg)
    ocon = ora.connect(ocfg)
    out = []
    try:
        for t in table_names:
            m = mapping.get(t)
            try:
                cols = ora.get_columns(ocon, owner, t)
                if not cols:
                    raise RuntimeError("no columns found (check schema/owner)")
                pgt, resolved = mapping.resolve(t, cols)
                renamed = sum(1 for c in resolved if not c["skip"] and c["target"] != c["name"].lower())
                skipped = sum(1 for c in resolved if c["skip"])
                current_fp = mapping.fingerprint(pgt, resolved)
                stored_fp = m["created_fingerprint"]
                out.append(dict(
                    table=t, pg_table=pgt, column_count=len(cols),
                    renamed=renamed, skipped=skipped,
                    created=bool(stored_fp),
                    drift=bool(stored_fp and stored_fp != current_fp),
                    fast_load=m["fast_load"],
                ))
            except Exception as e:  # noqa: BLE001
                out.append(dict(
                    table=t, pg_table=m["pg_table"] or pg_name(t),
                    error=str(e),
                ))
    finally:
        ocon.close()
    return out


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
            pgt = mapping.get(t)["pg_table"] or pg_name(t)
            try:
                cols = ora.get_columns(ocon, owner, t)
                if not cols:
                    raise RuntimeError("no columns found (check schema/owner)")
                pk = ora.get_pk(ocon, owner, t)
                pgt, resolved = mapping.resolve(t, cols)
                kept = [c for c in resolved if not c["skip"]]
                if not kept:
                    raise RuntimeError("all columns skipped - nothing to create")
                target_by_name = {c["name"]: c["target"] for c in kept}
                coldefs = [types_map.coldef(c, c["target"], c["pg_type"]) for c in kept]
                pk_targets = [target_by_name[p] for p in pk if p in target_by_name]
                existed = pg.table_exists(pcon, schema, pgt)
                pg.create_table(pcon, schema, pgt, coldefs, pk_targets)
                fp = mapping.fingerprint(pgt, resolved)
                mapping.mark_created(t, pgt, fp)
                results.append(dict(table=t, pg_table=pgt,
                                    status="exists" if existed else "created"))
            except Exception as e:  # noqa: BLE001
                pcon.rollback()
                results.append(dict(table=t, pg_table=pgt, status="error", error=str(e)))
    finally:
        ocon.close()
        pcon.close()
    return results


def recreate_tables(table_names):
    """Drop the previously-created Postgres table (if any) for each name,
    clear its drift fingerprint, then recreate with the current mapping.
    Data reload is left to a follow-up sync, mirroring the normal
    create -> sync flow."""
    ocfg, pcfg = _cfgs()
    schema = pcfg["target_schema"]
    pcon = pg.connect(pcfg)
    try:
        for t in table_names:
            old_pg_table = mapping.get(t)["created_pg_table"]
            if old_pg_table:
                pg.drop_table(pcon, schema, old_pg_table)
            mapping.clear_created(t)
    finally:
        pcon.close()
    return create_tables(table_names)


def oracle_row_count_estimates():
    """Approximate row counts (from optimizer stats) for every table in the
    Oracle schema, for the Tables tab - before any Postgres connection or
    column mapping exists yet. One dictionary query regardless of how many
    thousands of tables the schema has, unlike a live COUNT(*) per table."""
    ocfg = _ora_cfg()
    return ora.row_count_estimates(ocfg)


def compare(table_names):
    ocfg, pcfg = _cfgs()
    schema = pcfg["target_schema"]
    owner = ora.owner(ocfg)
    ocon = ora.connect(ocfg)
    pcon = pg.connect(pcfg)
    out = []
    try:
        for t in table_names:
            pgt = mapping.get(t)["pg_table"] or pg_name(t)
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


def checkpoint_status(table_names):
    """Tables in this batch that have a pending checkpoint from an
    interrupted sync, for the "resume?" panel shown before a sync starts."""
    out = []
    for t in table_names:
        cp = history.get_checkpoint(t)
        if cp:
            out.append(dict(table=t, last_offset=cp["last_offset"], updated_at=cp["updated_at"]))
    return out


def dry_run(table_names):
    """Preview mode: compute the DDL a create/recreate would run and row
    counts on both sides, without executing any writes on either database
    (no CREATE/TRUNCATE/COPY/ALTER). Fast-load tables also show the
    constraint/index DDL that would be dropped and recreated, so the impact
    of a recreate can be reviewed before committing to it."""
    ocfg, pcfg = _cfgs()
    schema = pcfg["target_schema"]
    owner = ora.owner(ocfg)
    ocon = ora.connect(ocfg)
    pcon = pg.connect(pcfg)
    out = []
    try:
        for t in table_names:
            entry = dict(table=t)
            try:
                cols = ora.get_columns(ocon, owner, t)
                if not cols:
                    raise RuntimeError("no columns found (check schema/owner)")
                pk = ora.get_pk(ocon, owner, t)
                pgt, resolved = mapping.resolve(t, cols)
                entry["pg_table"] = pgt
                kept = [c for c in resolved if not c["skip"]]
                if not kept:
                    raise RuntimeError("all columns skipped - nothing to create")
                target_by_name = {c["name"]: c["target"] for c in kept}
                coldefs = [types_map.coldef(c, c["target"], c["pg_type"]) for c in kept]
                pk_targets = [target_by_name[p] for p in pk if p in target_by_name]
                cols_sql = ",\n  ".join(coldefs)
                pk_sql = ""
                if pk_targets:
                    pk_cols = ", ".join(f'"{c}"' for c in pk_targets)
                    pk_sql = f',\n  PRIMARY KEY ({pk_cols})'
                entry["create_ddl"] = f'CREATE TABLE IF NOT EXISTS "{schema}"."{pgt}" (\n  {cols_sql}{pk_sql}\n)'

                fp = mapping.fingerprint(pgt, resolved)
                stored_fp = mapping.get(t)["created_fingerprint"]
                entry["drift"] = bool(stored_fp and stored_fp != fp)

                entry["oracle_rows"] = ora.row_count(ocon, owner, t)
                entry["exists"] = pg.table_exists(pcon, schema, pgt)
                if entry["exists"]:
                    try:
                        entry["pg_rows"] = pg.row_count(pcon, schema, pgt)
                    except Exception:  # noqa: BLE001
                        pcon.rollback()
                        entry["pg_rows"] = None
                    if mapping.get(t)["fast_load"]:
                        constraints = pg.introspect_constraints(pcon, schema, pgt)
                        entry["fast_load_drop"] = {
                            kind: [c["name"] for c in items] for kind, items in constraints.items()
                        }
                else:
                    entry["pg_rows"] = None

                ckpt = history.get_checkpoint(t)
                entry["resumable_from"] = ckpt["last_offset"] if ckpt else None
            except Exception as e:  # noqa: BLE001
                entry["error"] = str(e)
            out.append(entry)
    finally:
        ocon.close()
        pcon.close()
    return out


def _fast_load_targets(table_names):
    """Oracle table -> pg table name, for every table in this batch that has
    the drop/recreate-for-speed toggle on."""
    out = {}
    for t in table_names:
        m = mapping.get(t)
        if m["fast_load"]:
            out[t] = m["pg_table"] or pg_name(t)
    return out


def _capture_and_drop_constraints(pcon, schema, fast_pgt):
    """Introspect then drop FK/unique/PK/index for every table in fast_pgt
    (mutated in place - tables whose introspection fails are removed so the
    rest of the sync treats them as if the toggle were off this run).

    Global drop order across the WHOLE batch: FK -> unique -> PK -> plain
    index. This must complete for every toggled table before any of them is
    truncated/loaded, or a still-standing FK on table B referencing table
    A's PK would block dropping A's PK once we get to A (see roadmap notes
    on cross-table ordering). Yields SSE-style event dicts; returns the
    captured DDL map via `yield from`'s generator return value.
    """
    on_retry, drain = _retry_hook()
    captured = {}
    for t, pgt in list(fast_pgt.items()):
        try:
            captured[t] = pg.introspect_constraints(pcon, schema, pgt, on_retry=on_retry)
            yield from drain()
        except Exception as e:  # noqa: BLE001
            yield from drain()
            try:
                pcon.rollback()
            except Exception:
                pass
            yield dict(type="constraint_error", table=t, pg_table=pgt,
                       kind="introspect", error=str(e))
            del fast_pgt[t]

    tally = {t: {"ok": 0, "failed": 0} for t in fast_pgt}

    def _phase(kind, drop_fn):
        # Prune captured[t][kind] down to only what was actually dropped, so
        # the restore phase never retries re-adding something that failed
        # to drop (it's still there - retrying would just fail again with a
        # confusing "already exists" on top of the real error).
        for t, pgt in fast_pgt.items():
            kept = []
            for item in captured[t][kind]:
                try:
                    drop_fn(pgt, item)
                    yield from drain()
                    tally[t]["ok"] += 1
                    kept.append(item)
                except Exception as e:  # noqa: BLE001
                    yield from drain()
                    try:
                        pcon.rollback()
                    except Exception:
                        pass
                    tally[t]["failed"] += 1
                    yield dict(type="constraint_error", table=t, pg_table=pgt,
                               kind=kind, name=item["name"], error=str(e))
            captured[t][kind] = kept

    yield from _phase("fk", lambda pgt, item: pg.drop_constraint(pcon, schema, pgt, item["name"], on_retry=on_retry))
    yield from _phase("unique", lambda pgt, item: pg.drop_constraint(pcon, schema, pgt, item["name"], on_retry=on_retry))
    yield from _phase("pk", lambda pgt, item: pg.drop_constraint(pcon, schema, pgt, item["name"], on_retry=on_retry))
    yield from _phase("index", lambda pgt, item: pg.drop_index(pcon, schema, item["name"], on_retry=on_retry))

    for t, pgt in fast_pgt.items():
        yield dict(type="constraints_dropped", table=t, pg_table=pgt,
                   ok=tally[t]["ok"], failed=tally[t]["failed"])

    return captured


def _restore_constraints(pcon, schema, fast_pgt, captured):
    """Recreate PK -> unique -> FK (parent-before-child) -> index across
    every table in fast_pgt, from the DDL captured before the drop.

    Always attempted for every table regardless of whether that table's own
    load succeeded, so a failed load never leaves a table silently
    unconstrained - failures are surfaced as constraint_error events instead.
    """
    on_retry, drain = _retry_hook()
    tally = {t: {"ok": 0, "failed": 0} for t in fast_pgt}

    def _phase(kind, get_items, add_fn):
        for t, pgt in fast_pgt.items():
            for item in get_items(captured[t]):
                try:
                    add_fn(pgt, item)
                    yield from drain()
                    tally[t]["ok"] += 1
                except Exception as e:  # noqa: BLE001
                    yield from drain()
                    try:
                        pcon.rollback()
                    except Exception:
                        pass
                    tally[t]["failed"] += 1
                    yield dict(type="constraint_error", table=t, pg_table=pgt,
                               kind=kind, name=item["name"], error=str(e))

    yield from _phase("pk", lambda c: c["pk"],
                       lambda pgt, item: pg.add_constraint(pcon, schema, pgt, item["name"], item["ddl"], on_retry=on_retry))
    yield from _phase("unique", lambda c: c["unique"],
                       lambda pgt, item: pg.add_constraint(pcon, schema, pgt, item["name"], item["ddl"], on_retry=on_retry))

    # Order FK recreation parent-before-child across the toggled tables -
    # belt-and-suspenders on top of the PK/unique phases already having run
    # for every table first, per the roadmap's cross-table ordering ask.
    pgt_to_t = {pgt: t for t, pgt in fast_pgt.items()}
    edges = []
    for t in fast_pgt:
        for item in captured[t]["fk"]:
            parent_t = pgt_to_t.get(item.get("ref_table"))
            if parent_t:
                edges.append((t, parent_t))
    order = depgraph.topo_sort(list(fast_pgt), edges)

    def _fk_phase():
        for t in order:
            pgt = fast_pgt[t]
            for item in captured[t]["fk"]:
                try:
                    pg.add_constraint(pcon, schema, pgt, item["name"], item["ddl"], on_retry=on_retry)
                    yield from drain()
                    tally[t]["ok"] += 1
                except Exception as e:  # noqa: BLE001
                    yield from drain()
                    try:
                        pcon.rollback()
                    except Exception:
                        pass
                    tally[t]["failed"] += 1
                    yield dict(type="constraint_error", table=t, pg_table=pgt,
                               kind="fk", name=item["name"], error=str(e))

    yield from _fk_phase()
    yield from _phase("index", lambda c: c["index"],
                       lambda pgt, item: pg.create_index(pcon, item["ddl"], on_retry=on_retry))

    for t, pgt in fast_pgt.items():
        yield dict(type="constraints_restored", table=t, pg_table=pgt,
                   ok=tally[t]["ok"], failed=tally[t]["failed"])


def sync_stream(table_names, fresh_tables=None):
    """Generator of progress dict-events for an SSE stream.

    `fresh_tables`: table names to force a normal truncate+reload for even
    if a checkpoint exists (the "start fresh" override) - everything else
    resumes automatically from its checkpoint if one is present, so an
    interrupted sync doesn't silently reload from scratch.
    """
    fresh_tables = set(fresh_tables or ())
    ocfg, pcfg = _cfgs()
    schema = pcfg["target_schema"]
    owner = ora.owner(ocfg)

    failures = preflight.run(ocfg, pcfg, table_names)
    if failures:
        for f in failures:
            yield dict(type="preflight_error", check=f["check"], table=f["table"], error=f["error"])
        yield dict(type="fatal", error="Pre-flight checks failed; sync aborted before making any changes.")
        return

    on_retry, drain = _retry_hook()
    ocon = ora.connect(ocfg, on_retry=on_retry)
    yield from drain()
    pcon = pg.connect(pcfg, on_retry=on_retry)
    yield from drain()
    try:
        fast_pgt = _fast_load_targets(table_names)
        captured = {}
        if fast_pgt:
            captured = yield from _capture_and_drop_constraints(pcon, schema, fast_pgt)

        for t in table_names:
            run_id = None
            rows_loaded = 0
            rows_skipped = 0
            try:
                cols = [c for c in ora.get_columns(ocon, owner, t, on_retry=on_retry)]
                yield from drain()
                if not cols:
                    raise RuntimeError("no columns found (check schema/owner)")
                pgt, resolved = mapping.resolve(t, cols)
                current_fp = mapping.fingerprint(pgt, resolved)
                stored_fp = mapping.get(t)["created_fingerprint"]
                if stored_fp and stored_fp != current_fp:
                    yield dict(
                        type="table_error", table=t, mapping_drift=True,
                        error=(f"Mapping for {t} has changed since it was created. "
                               "Recreate table to apply changes? This will drop and reload all data."),
                    )
                    continue
                yield dict(type="table_start", table=t, pg_table=pgt)
                kept = [c for c in resolved if not c["skip"]]
                oracle_cols = [c["name"] for c in kept]
                pg_cols = [c["target"] for c in kept]

                checkpoint = None if t in fresh_tables else history.get_checkpoint(t)
                if checkpoint:
                    offset = checkpoint["last_offset"]
                    yield dict(type="resuming", table=t, offset=offset)
                else:
                    pg.truncate(pcon, schema, pgt, on_retry=on_retry)
                    yield from drain()
                    history.clear_checkpoint(t)
                    offset = 0

                order_cols = ora.get_order_columns(ocon, owner, t, on_retry=on_retry)
                yield from drain()
                oracle_total = ora.row_count(ocon, owner, t, on_retry=on_retry)
                yield from drain()

                run_id = history.start_run(t, pgt)
                start_time = time.monotonic()

                while True:
                    chunk = ora.fetch_chunk(ocon, owner, t, oracle_cols, order_cols,
                                             offset, CHUNK_SIZE, on_retry=on_retry)
                    yield from drain()
                    if not chunk:
                        break
                    try:
                        pg.copy_chunk(pcon, schema, pgt, pg_cols, chunk, on_retry=on_retry)
                        yield from drain()
                        ok, failed = len(chunk), 0
                    except Exception as e:  # noqa: BLE001
                        if retry.is_pg_transient(e):
                            raise  # outage, not a bad row - abort the table, keep the checkpoint
                        yield from drain()
                        chunk_offset = offset

                        def _on_row_error(i, err, chunk_offset=chunk_offset):
                            history.log_row_error(run_id, t, str(chunk_offset + i), str(err))

                        ok, failed = pg.insert_rows_capture_errors(
                            pcon, schema, pgt, pg_cols, chunk, _on_row_error, on_retry=on_retry)
                        yield from drain()

                    rows_loaded += ok
                    rows_skipped += failed
                    offset += len(chunk)
                    history.save_checkpoint(t, offset)

                    elapsed = time.monotonic() - start_time
                    rate = rows_loaded / elapsed if elapsed > 0 else 0
                    remaining = max(oracle_total - offset, 0)
                    eta_seconds = (remaining / rate) if rate > 0 else None
                    yield dict(type="progress", table=t, rows=offset, rows_loaded=rows_loaded,
                               rows_skipped=rows_skipped, rows_per_sec=rate, eta_seconds=eta_seconds)

                    if len(chunk) < CHUNK_SIZE:
                        break

                history.clear_checkpoint(t)
                oc = ora.row_count(ocon, owner, t, on_retry=on_retry)
                pc = pg.row_count(pcon, schema, pgt, on_retry=on_retry)
                yield from drain()
                status = "success" if rows_skipped == 0 else "partial"
                history.finish_run(run_id, rows_loaded, rows_skipped, status)
                yield dict(type="table_done", table=t, rows=offset, rows_loaded=rows_loaded,
                           rows_skipped=rows_skipped, run_id=run_id,
                           oracle=oc, pg=pc, match=(oc == pc))
            except Exception as e:  # noqa: BLE001
                yield from drain()
                if run_id is not None:
                    history.finish_run(run_id, rows_loaded, rows_skipped, "failed")
                try:
                    pcon.rollback()
                except Exception:
                    pass
                yield dict(type="table_error", table=t, error=str(e))

        if fast_pgt:
            yield from _restore_constraints(pcon, schema, fast_pgt, captured)

        yield dict(type="all_done")
    finally:
        ocon.close()
        pcon.close()
