"""Oracle access via python-oracledb in THIN mode (no Instant Client needed)."""
import oracledb
from . import retry

# Fetch LOBs as str/bytes directly, and numbers as Decimal for precision.
oracledb.defaults.fetch_lobs = False
oracledb.defaults.fetch_decimals = True


def _retry(label, fn, on_retry=None):
    return retry.call_with_retry(fn, retry.is_oracle_transient, f"Oracle: {label}", on_retry=on_retry)


def connect(cfg, on_retry=None):
    def _do():
        dsn = f"{cfg['host']}:{cfg['port']}/{cfg['service']}"
        return oracledb.connect(user=cfg["user"], password=cfg["password"], dsn=dsn)
    return _retry("connect", _do, on_retry)


def owner(cfg):
    return (cfg.get("schema") or cfg["user"]).upper()


def test(cfg, on_retry=None):
    con = connect(cfg, on_retry=on_retry)
    try:
        def _do():
            cur = con.cursor()
            cur.execute("SELECT 1 FROM dual")
            cur.fetchone()
        _retry("test query", _do, on_retry)
        return True
    finally:
        con.close()


def list_tables(cfg, on_retry=None):
    con = connect(cfg, on_retry=on_retry)
    try:
        def _do():
            cur = con.cursor()
            cur.execute(
                "SELECT table_name FROM all_tables WHERE owner = :o ORDER BY table_name",
                o=owner(cfg),
            )
            return cur.fetchall()
        rows = _retry("list tables", _do, on_retry)
        return [r[0] for r in rows]
    finally:
        con.close()


def row_count_estimates(cfg, on_retry=None):
    """Approximate row count for every table under the owner, from
    optimizer statistics (ALL_TABLES.NUM_ROWS) in one query - not a live
    COUNT(*) per table. On schemas with thousands of tables, running a
    COUNT(*) for each one is both too slow and (via a "one giant table list
    in the URL" request) prone to hitting URL length limits; a single
    dictionary query has neither problem. Counts reflect the last time
    stats were gathered and can be stale; None means stats were never
    gathered for that table.
    """
    con = connect(cfg, on_retry=on_retry)
    try:
        def _do():
            cur = con.cursor()
            cur.execute(
                "SELECT table_name, num_rows FROM all_tables WHERE owner = :o",
                o=owner(cfg),
            )
            return cur.fetchall()
        rows = _retry("row count estimates", _do, on_retry)
        return {name: (int(n) if n is not None else None) for name, n in rows}
    finally:
        con.close()


def get_columns(con, owner_, table, on_retry=None):
    def _do():
        cur = con.cursor()
        cur.execute(
            """
            SELECT column_name, data_type, data_precision, data_scale,
                   data_length, char_length, nullable
            FROM all_tab_columns
            WHERE owner = :o AND table_name = :t
            ORDER BY column_id
            """,
            o=owner_, t=table,
        )
        return cur.fetchall()
    rows = _retry("get columns", _do, on_retry)
    cols = []
    for name, dtype, prec, scale, length, clen, nullable in rows:
        cols.append(dict(
            name=name, dtype=dtype, prec=prec, scale=scale,
            length=length, clen=clen, nullable=(nullable == "Y"),
        ))
    return cols


def get_pk(con, owner_, table, on_retry=None):
    def _do():
        cur = con.cursor()
        cur.execute(
            """
            SELECT cc.column_name
            FROM all_constraints c
            JOIN all_cons_columns cc
              ON c.owner = cc.owner AND c.constraint_name = cc.constraint_name
            WHERE c.owner = :o AND c.table_name = :t AND c.constraint_type = 'P'
            ORDER BY cc.position
            """,
            o=owner_, t=table,
        )
        return cur.fetchall()
    rows = _retry("get primary key", _do, on_retry)
    return [r[0] for r in rows]


def row_count(con, owner_, table, on_retry=None):
    def _do():
        cur = con.cursor()
        cur.execute(f'SELECT COUNT(*) FROM "{owner_}"."{table}"')
        return int(cur.fetchone()[0])
    return _retry("row count", _do, on_retry)


def check_read_access(con, owner_, table, on_retry=None):
    """Cheap read-only probe used by pre-flight: raises if the current
    Oracle user can't SELECT from the table (missing grant, wrong owner,
    typo'd name, etc.) rather than failing deep inside the sync loop."""
    def _do():
        cur = con.cursor()
        cur.execute(f'SELECT 1 FROM "{owner_}"."{table}" WHERE ROWNUM = 1')
        cur.fetchone()
    _retry("check read access", _do, on_retry)


def get_order_columns(con, owner_, table, on_retry=None):
    """Deterministic ordering for OFFSET-based chunked paging: the table's
    primary key if it has one, else ROWID. Needed so each chunk fetch (and
    a resumed run's fetch from a saved offset) sees rows in a stable order."""
    pk = get_pk(con, owner_, table, on_retry=on_retry)
    return pk if pk else ["ROWID"]


def fetch_chunk(con, owner_, table, columns, order_cols, offset, chunk_size, on_retry=None):
    """Fetch one deterministically-ordered page of rows. Each chunk is its
    own retryable unit and its own query (rather than one long-lived
    cursor), so a resumed sync can restart a chunk fetch from an arbitrary
    saved offset instead of only from the top of the table."""
    collist = ", ".join(f'"{c}"' for c in columns)
    orderby = "ROWID" if order_cols == ["ROWID"] else ", ".join(f'"{c}"' for c in order_cols)
    sql = (
        f'SELECT {collist} FROM "{owner_}"."{table}" ORDER BY {orderby} '
        f'OFFSET :chunk_offset ROWS FETCH NEXT :chunk_size ROWS ONLY'
    )

    def _do():
        cur = con.cursor()
        cur.execute(sql, chunk_offset=offset, chunk_size=chunk_size)
        return cur.fetchall()
    return _retry("fetch chunk", _do, on_retry)
