"""Apply migrations/*.sql in filename order, each once. Usage: python migrate.py <dsn>"""
import pathlib
import sys

import db

MIGRATIONS = pathlib.Path(__file__).parent / "migrations"


def apply(conn) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
    done = {r["filename"] for r in conn.execute("SELECT filename FROM schema_migrations")}
    applied = []
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name in done:
            continue
        conn.execute(f.read_text())
        conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING", (f.name,))
        applied.append(f.name)
    conn.commit()
    return applied


if __name__ == "__main__":
    with db.connect(sys.argv[1]) as c:
        print(apply(c))
