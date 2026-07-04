"""PostgreSQL access via psycopg (v3). Uses COPY for fast bulk load."""
import psycopg
from . import retry


def _retry(label, fn, on_retry=None):
    return retry.call_with_retry(fn, retry.is_pg_transient, f"Postgres: {label}", on_retry=on_retry)


def connect(cfg, on_retry=None):
    def _do():
        return psycopg.connect(
            host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
            user=cfg["user"], password=cfg["password"],
        )
    return _retry("connect", _do, on_retry)


def test(cfg, on_retry=None):
    with connect(cfg, on_retry=on_retry) as con:
        def _do():
            with con.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        _retry("test query", _do, on_retry)
    return True


def ensure_schema(con, schema, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        con.commit()
    _retry("ensure schema", _do, on_retry)


def table_exists(con, schema, table, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s",
                (schema, table),
            )
            return cur.fetchone() is not None
    return _retry("table exists", _do, on_retry)


def create_table(con, schema, table, coldefs, pk, on_retry=None):
    cols_sql = ",\n  ".join(coldefs)
    pk_sql = ""
    if pk:
        pk_cols = ", ".join(f'"{c}"' for c in pk)
        pk_sql = f',\n  PRIMARY KEY ({pk_cols})'
    ddl = f'CREATE TABLE IF NOT EXISTS "{schema}"."{table}" (\n  {cols_sql}{pk_sql}\n)'

    def _do():
        with con.cursor() as cur:
            cur.execute(ddl)
        con.commit()
    _retry("create table", _do, on_retry)
    return ddl


def row_count(con, schema, table, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
            return cur.fetchone()[0]
    return _retry("row count", _do, on_retry)


def drop_table(con, schema, table, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{schema}"."{table}"')
        con.commit()
    _retry("drop table", _do, on_retry)


def truncate(con, schema, table, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(f'TRUNCATE TABLE "{schema}"."{table}"')
        con.commit()
    _retry("truncate", _do, on_retry)


def check_privileges(con, schema, table, on_retry=None):
    """Best-effort pre-flight permission check for a sync target.

    CREATE on the schema covers CREATE TABLE / DROP TABLE. If the table
    already exists we also require INSERT (needed for COPY) and ownership
    (needed to ALTER/DROP its constraints for fast-load recreate) - Postgres
    has no has_table_privilege() privilege type for DROP, ownership is the
    real gate for that, so this is the closest direct check available.
    Returns (ok, reason-or-None).
    """
    def _do():
        with con.cursor() as cur:
            cur.execute("SELECT has_schema_privilege(current_user, %s, 'CREATE')", (schema,))
            can_create = bool(cur.fetchone()[0])
            cur.execute(
                "SELECT tableowner FROM pg_tables WHERE schemaname=%s AND tablename=%s",
                (schema, table),
            )
            row = cur.fetchone()
            if row is None:
                return can_create, None, None
            cur.execute(
                "SELECT has_table_privilege(current_user, %s, 'INSERT'), current_user",
                (f'"{schema}"."{table}"',),
            )
            can_insert, cur_user = cur.fetchone()
            return can_create, bool(can_insert), (row[0] == cur_user)
    can_create, can_insert, is_owner = _retry("check privileges", _do, on_retry)
    if not can_create:
        return False, f'current user lacks CREATE privilege on schema "{schema}"'
    if can_insert is False:
        return False, f'current user lacks INSERT privilege on "{schema}"."{table}" (required for COPY)'
    if is_owner is False:
        return False, f'current user does not own "{schema}"."{table}" (required to add/drop constraints)'
    return True, None


def copy_chunk(con, schema, table, columns, rows, on_retry=None):
    """COPY exactly one already-fetched chunk of rows, as a single retryable
    unit. Unlike copy_load's batch-iterator form, the caller controls chunk
    boundaries so progress/checkpoints can be recorded between chunks (see
    engine.sync_stream). On any failure the transaction is rolled back
    before propagating, so a retried attempt (or the row-by-row fallback
    the caller uses for non-transient failures) starts from a clean state.
    """
    collist = ", ".join(f'"{c}"' for c in columns)
    sql = f'COPY "{schema}"."{table}" ({collist}) FROM STDIN'

    def _do():
        try:
            with con.cursor() as cur:
                with cur.copy(sql) as cp:
                    for row in rows:
                        cp.write_row(row)
            con.commit()
        except Exception:
            con.rollback()
            raise
    _retry("copy chunk", _do, on_retry)


def insert_rows_capture_errors(con, schema, table, columns, rows, on_row_error, on_retry=None):
    """Row-by-row fallback for a chunk whose bulk COPY failed for a
    non-transient (data-level) reason: insert each row in its own
    subtransaction so one bad row (encoding, constraint violation, type
    mismatch) doesn't lose the rest of the chunk. `on_row_error(index,
    error)` fires for each row that fails, after its subtransaction is
    rolled back to the savepoint - the rest of the chunk keeps going.
    Returns (ok_count, failed_count).
    """
    collist = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f'INSERT INTO "{schema}"."{table}" ({collist}) VALUES ({placeholders})'
    ok = 0
    failed = 0
    for i, row in enumerate(rows):
        try:
            def _do(row=row):
                with con.transaction():
                    with con.cursor() as cur:
                        cur.execute(sql, row)
            _retry("insert row", _do, on_retry)
            ok += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            on_row_error(i, e)
    con.commit()
    return ok, failed


def introspect_constraints(con, schema, table, on_retry=None):
    """Capture ready-to-run DDL for every PK/unique/FK constraint and every
    standalone index on a table, so a drop-then-recreate can restore them
    exactly - column renames/skips mean this can't just be regenerated from
    the Oracle source schema.

    Returns {"pk": [...], "unique": [...], "fk": [...], "index": [...]},
    each item a dict with "name" and "ddl" (the fragment to use after
    ALTER TABLE ... ADD CONSTRAINT <name>, or a full CREATE INDEX statement
    for indexes). FK items also carry "ref_table" for dependency ordering.
    """
    qualified = f'"{schema}"."{table}"'

    def _do_constraints():
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT c.conname, c.contype, pg_get_constraintdef(c.oid),
                       ci.relname, cf.relname
                FROM pg_constraint c
                LEFT JOIN pg_class ci ON ci.oid = c.conindid
                LEFT JOIN pg_class cf ON cf.oid = c.confrelid
                WHERE c.conrelid = %s::regclass
                """,
                (qualified,),
            )
            return cur.fetchall()
    rows = _retry("introspect constraints", _do_constraints, on_retry)

    pk, unique, fk = [], [], []
    backing_index_names = set()
    for conname, contype, condef, index_name, ref_table in rows:
        item = {"name": conname, "ddl": condef}
        if contype == "p":
            pk.append(item)
        elif contype == "u":
            unique.append(item)
        elif contype == "f":
            item["ref_table"] = ref_table
            fk.append(item)
        if index_name:
            backing_index_names.add(index_name)

    def _do_indexes():
        with con.cursor() as cur:
            cur.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
                (schema, table),
            )
            return cur.fetchall()
    idx_rows = _retry("introspect indexes", _do_indexes, on_retry)
    index = [{"name": n, "ddl": d} for n, d in idx_rows if n not in backing_index_names]

    return {"pk": pk, "unique": unique, "fk": fk, "index": index}


def drop_constraint(con, schema, table, name, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(f'ALTER TABLE "{schema}"."{table}" DROP CONSTRAINT "{name}"')
        con.commit()
    _retry("drop constraint", _do, on_retry)


def drop_index(con, schema, name, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(f'DROP INDEX IF EXISTS "{schema}"."{name}"')
        con.commit()
    _retry("drop index", _do, on_retry)


def add_constraint(con, schema, table, name, ddl, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(f'ALTER TABLE "{schema}"."{table}" ADD CONSTRAINT "{name}" {ddl}')
        con.commit()
    _retry("add constraint", _do, on_retry)


def create_index(con, ddl, on_retry=None):
    """`ddl` is a full statement from pg_indexes.indexdef (already
    schema/table-qualified)."""
    def _do():
        with con.cursor() as cur:
            cur.execute(ddl)
        con.commit()
    _retry("create index", _do, on_retry)
