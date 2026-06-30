"""Oracle access via python-oracledb in THIN mode (no Instant Client needed)."""
import oracledb

# Fetch LOBs as str/bytes directly, and numbers as Decimal for precision.
oracledb.defaults.fetch_lobs = False
oracledb.defaults.fetch_decimals = True


def connect(cfg):
    dsn = f"{cfg['host']}:{cfg['port']}/{cfg['service']}"
    return oracledb.connect(user=cfg["user"], password=cfg["password"], dsn=dsn)


def owner(cfg):
    return (cfg.get("schema") or cfg["user"]).upper()


def test(cfg):
    con = connect(cfg)
    try:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM dual")
        cur.fetchone()
        return True
    finally:
        con.close()


def list_tables(cfg):
    con = connect(cfg)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT table_name FROM all_tables WHERE owner = :o ORDER BY table_name",
            o=owner(cfg),
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        con.close()


def get_columns(con, owner_, table):
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
    cols = []
    for name, dtype, prec, scale, length, clen, nullable in cur.fetchall():
        cols.append(dict(
            name=name, dtype=dtype, prec=prec, scale=scale,
            length=length, clen=clen, nullable=(nullable == "Y"),
        ))
    return cols


def get_pk(con, owner_, table):
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
    return [r[0] for r in cur.fetchall()]


def row_count(con, owner_, table):
    cur = con.cursor()
    cur.execute(f'SELECT COUNT(*) FROM "{owner_}"."{table}"')
    return cur.fetchone()[0]


def fetch_batches(con, owner_, table, columns, arraysize=10000):
    """Yield lists of row-tuples for the given columns."""
    cur = con.cursor()
    cur.arraysize = arraysize
    collist = ", ".join(f'"{c}"' for c in columns)
    cur.execute(f'SELECT {collist} FROM "{owner_}"."{table}"')
    while True:
        rows = cur.fetchmany(arraysize)
        if not rows:
            break
        yield rows
