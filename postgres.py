"""Postgres storage adapter for the existing, unchanged reporting Backend."""
import re
from contextlib import contextmanager
from server import Backend


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def row_factory(cursor):
    names = [column.name for column in cursor.description]
    return lambda values: Row(zip(names, values))


def translate(sql):
    if sql == 'BEGIN IMMEDIATE':
        return 'SELECT 1'  # db() already holds the transaction-wide advisory lock.
    sql = sql.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'BIGSERIAL PRIMARY KEY')
    sql = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', sql)
    # SQLite identifiers that are reserved words in PostgreSQL.
    sql = re.sub(r'\buser\b', '"user"', sql)
    sql = sql.replace("json_extract(payload,'$.alertType')", "(payload::jsonb->>'alertType')")
    if 'INSERT OR IGNORE INTO' in sql:
        sql = sql.replace('INSERT OR IGNORE INTO', 'INSERT INTO') + ' ON CONFLICT DO NOTHING'
    if 'INSERT OR REPLACE INTO members' in sql:
        sql = sql.replace('INSERT OR REPLACE INTO', 'INSERT INTO') + ' ON CONFLICT("user",business,branch) DO UPDATE SET role=excluded.role'
    return sql.replace('?', '%s')


class Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        return self.connection.execute(translate(sql), params)

    def executescript(self, script):
        for statement in script.split(';'):
            if statement.strip():
                self.execute(statement)


class PostgresBackend(Backend):
    @contextmanager
    def db(self):
        import psycopg
        # Session pooler URL, TLS required. No credentials are written to files.
        with psycopg.connect(self.path, sslmode='require', connect_timeout=15,
                             row_factory=row_factory, prepare_threshold=None) as connection:
            connection.execute('SET LOCAL search_path TO laundry, pg_catalog')
            connection.execute("SET LOCAL statement_timeout = '30s'")
            # One lock for this small deployment makes read-modify-write aggregates
            # and retries safe across gunicorn workers and separate POS devices.
            connection.execute('SELECT pg_advisory_xact_lock(716283940)')
            yield Connection(connection)
