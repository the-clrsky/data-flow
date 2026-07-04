"""Small, dependency-free retry-with-backoff for transient DB errors.

Wraps individual DB operations (connect, execute-and-fetch, one COPY chunk)
rather than whole sync runs, so a transient blip mid-transfer only retries
the operation that failed - not the whole table. Reconnecting-and-resuming
a large transfer from an arbitrary row offset is out of scope (a separate,
later checkpoint/resume feature); this just retries the same call after
backing off, which is enough for network blips that resolve within a few
seconds. Non-transient errors (constraint violations, syntax errors, type
mismatches, etc.) are never retried - they fail immediately, as today.

Convention: 4 total attempts (the initial try + up to 3 retries), with
exponential backoff of 1s/2s/4s between them.
"""
import time

MAX_ATTEMPTS = 4  # 1 initial try + 3 retries
BACKOFF_SECONDS = (1, 2, 4)

# Well-known Oracle error codes for lost/unavailable connections. oracledb
# mostly raises DatabaseError for everything, so - unlike psycopg's clean
# OperationalError hierarchy - code matching on the message is the reliable,
# driver-version-agnostic way to tell "connection is gone" apart from
# "your SQL/data was wrong".
_ORACLE_TRANSIENT_CODES = (
    "ORA-03113",  # end-of-file on communication channel
    "ORA-03114",  # not connected to Oracle
    "ORA-03135",  # connection lost contact
    "ORA-12170",  # TNS: connect timeout occurred
    "ORA-12171",  # TNS: could not resolve connect identifier
    "ORA-12541",  # TNS: no listener
    "ORA-12537",  # TNS: connection closed
    "ORA-12545",  # connect failed - host/object does not exist
    "ORA-01012",  # not logged on
    "ORA-25408",  # can not safely replay call
    "DPY-4011",   # the database or network closed the connection
    "DPY-4024",   # timed out
    "DPY-6005",   # cannot connect to database
)


def is_oracle_transient(exc):
    msg = str(exc)
    return any(code in msg for code in _ORACLE_TRANSIENT_CODES)


def is_pg_transient(exc):
    try:
        import psycopg
        if isinstance(exc, psycopg.OperationalError):
            return True
    except ImportError:
        pass
    # Defensive fallback in case a raw socket-level error surfaces unwrapped.
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def call_with_retry(fn, is_transient, label, on_retry=None, max_attempts=MAX_ATTEMPTS):
    """Call fn() (no-arg), retrying on a transient exception with
    exponential backoff. Non-transient exceptions, and transient ones once
    attempts are exhausted, propagate immediately and unchanged - retrying
    never masks or rewrites a real failure, it just gives a momentary blip
    a chance to clear.

    `on_retry(label, attempt, max_retries, exc, delay)` fires right before
    each sleep, letting streaming callers (SSE) surface "retrying..." to
    the user instead of an unexplained pause.
    """
    attempt = 1
    while True:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if attempt >= max_attempts or not is_transient(e):
                raise
            delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
            if on_retry:
                on_retry(label, attempt, max_attempts - 1, e, delay)
            time.sleep(delay)
            attempt += 1
