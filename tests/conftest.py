import os
import sys

import pytest
from pytest_postgresql import factories

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import migrate  # noqa: E402

postgresql_proc = factories.postgresql_proc()
postgresql = factories.postgresql("postgresql_proc")


@pytest.fixture
def conn(postgresql):
    i = postgresql.info
    dsn = f"host={i.host} port={i.port} user={i.user} dbname={i.dbname}"
    c = db.connect(dsn)
    migrate.apply(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """Every domain 'has an MX' in tests; test_domain_accepts_mail swaps in its own fake resolver."""
    import signup

    class _AlwaysMX:
        lifetime = None

        def resolve(self, domain, rtype):
            return ["mx"]
    signup._DOMAIN_OK.clear()
    monkeypatch.setattr(signup.dns.resolver, "Resolver", lambda: _AlwaysMX())
