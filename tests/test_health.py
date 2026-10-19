"""Health endpoint used by Docker and future orchestrators.

The endpoint must keep answering while the database is down, because a health
check that silently reports success on a broken database is worse than no check
at all.  These tests cover both directions.
"""
from __future__ import annotations


def test_healthz_reports_ok_with_working_database(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "database": True}


def test_healthz_reports_503_when_database_fails(client, monkeypatch):
    import app as app_package

    monkeypatch.setattr(app_package, "_database_probe", lambda: False)
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.get_json() == {"status": "unavailable", "database": False}


def test_healthz_discloses_nothing_sensitive(client):
    body = client.get("/healthz").get_data(as_text=True).lower()
    for forbidden in ("secret", "password", "mysql", "sqlite", "traceback", "version"):
        assert forbidden not in body


def test_healthz_needs_no_authentication(client):
    # The ``client`` fixture is anonymous (it never logs in), so a 200 here
    # proves the endpoint is reachable without a session.
    assert client.get("/healthz").status_code == 200
