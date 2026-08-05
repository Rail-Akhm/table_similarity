"""Database connection helpers."""

import psycopg2


def get_connection(dsn: str):
    """Create a psycopg2 connection."""
    return psycopg2.connect(dsn)


def get_cursor(conn, name: str = None):
    """Create a cursor for querying.

    If name is provided, creates a server-side named cursor for streaming.
    """
    return conn.cursor(name=name)


def iter_cursor(cursor, batch_size: int = 50000):
    """Iterate over cursor in batches.

    For named cursors, fetchmany is used to stream results.
    """
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        for row in rows:
            yield row