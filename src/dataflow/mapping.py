"""Per-table column/table name mapping (rename + skip + Postgres type
override) and drift fingerprinting.

No value transforms live here - a type override just changes the declared
Postgres column type; Postgres parses the incoming COPY text into it.
"""
import hashlib
import json

from . import store
from . import types_map

_KEY = "mappings"


def _all():
    return store.get(_KEY, {})


def default_table_name(oracle_table):
    return oracle_table.lower()


def get(table):
    """Raw stored mapping for a table, with defaults filled in."""
    m = _all().get(table, {})
    return {
        "pg_table": m.get("pg_table"),
        "columns": m.get("columns", {}),
        "created_fingerprint": m.get("created_fingerprint"),
        "created_pg_table": m.get("created_pg_table"),
        "fast_load": bool(m.get("fast_load", False)),
    }


def save(table, pg_table, columns, fast_load=False):
    """Persist overrides for a table.

    columns: iterable of {name, target, skip, pg_type} for Oracle column
    names. pg_type is a manual Postgres type override, or falsy to use the
    computed default. fast_load toggles drop/recreate of constraints and
    indexes around each sync for this table (see engine.sync_stream) - it
    doesn't affect DDL shape, so it isn't part of the drift fingerprint.
    """
    all_m = _all()
    entry = all_m.get(table, {})
    entry["pg_table"] = (pg_table or "").strip() or None
    col_map = {}
    for c in columns:
        name = c["name"]
        target = (c.get("target") or "").strip() or None
        pg_type_override = (c.get("pg_type") or "").strip() or None
        col_map[name] = {
            "target": target, "skip": bool(c.get("skip")), "pg_type": pg_type_override,
        }
    entry["columns"] = col_map
    entry["fast_load"] = bool(fast_load)
    all_m[table] = entry
    store.put(_KEY, all_m)
    return get(table)


def resolve(table, oracle_columns):
    """Apply stored overrides + defaults to a live Oracle column list.

    Returns (pg_table_name, [oracle_column_dict with target/skip/pg_type/
    pg_type_override/default_pg_type added]).
    """
    m = get(table)
    overrides = m["columns"]
    resolved = []
    for c in oracle_columns:
        ov = overrides.get(c["name"], {})
        target = ov.get("target") or c["name"].lower()
        skip = bool(ov.get("skip"))
        default_pg_type = types_map.pg_type(c)
        pg_type_override = ov.get("pg_type")
        pg_type = pg_type_override or default_pg_type
        resolved.append(dict(
            c, target=target, skip=skip,
            default_pg_type=default_pg_type,
            pg_type_override=pg_type_override,
            pg_type=pg_type,
        ))
    pg_table = m["pg_table"] or default_table_name(table)
    return pg_table, resolved


def fingerprint(pg_table, resolved_columns):
    """Hash of everything that affects DDL shape: target table + per-column
    (oracle name, target name, skip, pg type) in Oracle column order."""
    payload = {
        "pg_table": pg_table,
        "columns": [
            {"name": c["name"], "target": c["target"], "skip": c["skip"], "pg_type": c["pg_type"]}
            for c in resolved_columns
        ],
    }
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def mark_created(table, pg_table, fp):
    """Record the mapping fingerprint (and resulting pg table name) that a
    table was actually created with, so drift can be detected before sync."""
    all_m = _all()
    entry = all_m.setdefault(table, {})
    entry["created_fingerprint"] = fp
    entry["created_pg_table"] = pg_table
    all_m[table] = entry
    store.put(_KEY, all_m)


def clear_created(table):
    all_m = _all()
    if table in all_m:
        all_m[table]["created_fingerprint"] = None
        all_m[table]["created_pg_table"] = None
        store.put(_KEY, all_m)
