"""Oracle -> PostgreSQL type translation.

This is the part ora2pg would normally do for you. It covers the common
Oracle types; extend the map for anything exotic in your schema.
`col` is a dict: name, dtype, prec, scale, length, clen, nullable.
"""


def pg_type(col):
    dt = (col["dtype"] or "").upper()
    prec = col["prec"]
    scale = col["scale"] or 0
    clen = col["clen"]
    length = col["length"]

    if dt in ("VARCHAR2", "NVARCHAR2", "VARCHAR", "CHARACTER VARYING"):
        n = clen or length
        return f"varchar({n})" if n else "text"
    if dt in ("CHAR", "NCHAR", "CHARACTER"):
        n = clen or length or 1
        return f"char({n})"
    if dt in ("CLOB", "NCLOB", "LONG"):
        return "text"
    if dt in ("BLOB", "RAW", "LONG RAW", "BFILE"):
        return "bytea"
    if dt == "NUMBER":
        if prec is None:
            return "numeric"
        if scale == 0:
            if prec <= 4:
                return "smallint"
            if prec <= 9:
                return "integer"
            if prec <= 18:
                return "bigint"
            return f"numeric({prec})"
        return f"numeric({prec},{scale})"
    if dt == "FLOAT":
        return "double precision"
    if dt == "BINARY_FLOAT":
        return "real"
    if dt == "BINARY_DOUBLE":
        return "double precision"
    if dt == "DATE":
        return "timestamp"
    if dt.startswith("TIMESTAMP"):
        if "WITH TIME ZONE" in dt or "WITH LOCAL TIME ZONE" in dt:
            return "timestamptz"
        return "timestamp"
    if dt.startswith("INTERVAL"):
        return "interval"
    if dt in ("ROWID", "UROWID"):
        return "text"
    # Safe fallback
    return "text"


def coldef(col, pg_name):
    """Return a single column definition line for CREATE TABLE."""
    null = "" if col["nullable"] else " NOT NULL"
    return f'"{pg_name}" {pg_type(col)}{null}'
