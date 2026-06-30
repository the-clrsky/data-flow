"""PostgreSQL access via psycopg (v3). Uses COPY for fast bulk load."""
import psycopg


def connect(cfg):
    return psycopg.connect(
        host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
        user=cfg["user"], password=cfg["password"],
    )


def test(cfg):
    with connect(cfg) as con:
        with con.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    return True


def ensure_schema(con, schema):
    with con.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    con.commit()


def table_exists(con, schema, table):
    with con.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
        return cur.fetchone() is not None


def create_table(con, schema, table, coldefs, pk):
    cols_sql = ",\n  ".join(coldefs)
    pk_sql = ""
    if pk:
        pk_cols = ", ".join(f'"{c}"' for c in pk)
        pk_sql = f',\n  PRIMARY KEY ({pk_cols})'
    ddl = f'CREATE TABLE IF NOT EXISTS "{schema}"."{table}" (\n  {cols_sql}{pk_sql}\n)'
    with con.cursor() as cur:
        cur.execute(ddl)
    con.commit()
    return ddl


def row_count(con, schema, table):
    with con.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
        return cur.fetchone()[0]


def truncate(con, schema, table):
    with con.cursor() as cur:
        cur.execute(f'TRUNCATE TABLE "{schema}"."{table}"')
    con.commit()


def copy_load(con, schema, table, columns, batch_iter):
    """Stream batches into the table via COPY. Yields running row total."""
    collist = ", ".join(f'"{c}"' for c in columns)
    sql = f'COPY "{schema}"."{table}" ({collist}) FROM STDIN'
    total = 0
    with con.cursor() as cur:
        with cur.copy(sql) as cp:
            for rows in batch_iter:
                for row in rows:
                    cp.write_row(row)
                total += len(rows)
                yield total
    con.commit()
