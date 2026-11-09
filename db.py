import psycopg
from psycopg.rows import dict_row


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row)
