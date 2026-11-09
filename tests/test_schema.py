import migrate


def test_migrations_apply_once(conn):
    assert migrate.apply(conn) == []  # fixture already applied everything
    tables = {r["tablename"] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert {"profiles", "consent", "suppressions", "coupons", "events", "enrolments", "flow_steps", "messages", "clicks", "opens"} <= tables
