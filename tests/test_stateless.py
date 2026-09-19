"""Stateless-flow tests: the behaviour Vercel depends on.

On Vercel each request may land on a brand-new, empty function instance. The
old design stored the session in module globals, so "Load sample data" on one
instance and "Run migration" on another would see no data and fail.

These tests simulate that by wiping the server's in-memory state between every
request and passing the session the way the browser does. If these pass, the
app works on a platform that keeps no state.
"""

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path, raising=False)
    main.state.__init__()
    return TestClient(main.app)


def fresh_instance():
    """Simulate landing on a different, empty serverless instance."""
    main.state.__init__()


def load_session(client):
    """Load sample data and return the session the browser would hold."""
    fresh_instance()
    response = client.post("/api/load-samples")
    assert response.status_code == 200
    payload = response.json()
    assert "session" in payload, "every response must return a replayable session"
    return payload["session"]


class TestSessionIsReturned:
    def test_load_samples_returns_a_replayable_session(self, client):
        session = load_session(client)
        assert len(session["source_accounts"]) == 46
        assert len(session["target_accounts"]) == 47
        assert len(session["transactions"]) == 130
        assert session["transactions_filename"] == "transactions.csv"

    def test_session_carries_confirmed_decisions(self, client):
        session = load_session(client)
        fresh_instance()
        response = client.post(
            "/api/approve-top",
            json={"source_number": "1000", "session": session},
        )
        assert response.status_code == 200
        assert response.json()["session"]["confirmed"] == {"1000": "10000"}


class TestWorksWithNoServerState:
    """Each test wipes server state before the call it cares about."""

    def test_migrate_on_a_cold_instance(self, client):
        session = load_session(client)
        fresh_instance()  # cold start: server remembers nothing

        response = client.post("/api/migrate", json={"session": session})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["report"]["passed"] is True
        assert payload["summary"]["migrated_lines"] == 130
        # The migrated journal must be returned so later calls can export it.
        assert payload["session"]["transactions"]

    def test_mappings_on_a_cold_instance(self, client):
        session = load_session(client)
        fresh_instance()

        response = client.post("/api/mappings", json={"session": session})
        assert response.status_code == 200
        payload = response.json()
        assert payload["summary"]["mappings_total"] == 46
        assert payload["summary"]["needs_review"] == 7

    def test_source_accounts_on_a_cold_instance(self, client):
        session = load_session(client)
        fresh_instance()

        response = client.post("/api/source-accounts", json={"session": session})
        assert response.status_code == 200
        assert len(response.json()["accounts"]) == 46

    def test_target_accounts_on_a_cold_instance(self, client):
        session = load_session(client)
        fresh_instance()

        response = client.post("/api/target-accounts", json={"session": session})
        assert response.status_code == 200
        assert len(response.json()["accounts"]) == 47

    def test_transactions_on_a_cold_instance(self, client):
        session = load_session(client)
        fresh_instance()

        response = client.post("/api/transactions", json={"session": session})
        assert response.status_code == 200
        assert len(response.json()["transactions"]) == 130

    def test_confirmed_mapping_survives_a_cold_instance(self, client):
        """A human decision made on one instance must apply on the next."""
        session = load_session(client)
        fresh_instance()

        # Pin 1000 -> a deliberately different target than the matcher suggested.
        response = client.post(
            "/api/confirm",
            json={"source_number": "1000", "target_number": "10200", "session": session},
        )
        assert response.status_code == 200
        updated = response.json()["session"]

        fresh_instance()  # yet another instance
        mappings = client.post("/api/mappings", json={"session": updated}).json()
        mapping = next(m for m in mappings["mappings"] if m["source_number"] == "1000")
        assert mapping["target_number"] == "10200"

    def test_approve_top_on_a_cold_instance(self, client):
        session = load_session(client)
        fresh_instance()

        response = client.post(
            "/api/approve-top", json={"source_number": "1000", "session": session}
        )
        assert response.status_code == 200
        mapping = response.json()["session"]["confirmed"]
        assert "1000" in mapping

    def test_explain_on_a_cold_instance(self, client):
        session = load_session(client)
        fresh_instance()

        response = client.post(
            "/api/explain", json={"source_number": "1000", "session": session}
        )
        assert response.status_code == 200
        assert "%" in response.json()["explanation"]


class TestExports:
    def test_migrated_export_rebuilds_from_session(self, client):
        """Export must work even on an instance that never ran the migration."""
        session = load_session(client)
        fresh_instance()

        response = client.post("/api/export/migrated.csv", json={"session": session})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        lines = response.text.strip().splitlines()
        assert len(lines) == 131  # header + 130 rows
        assert "target_account" in lines[0]

    def test_mappings_export_rebuilds_from_session(self, client):
        session = load_session(client)
        fresh_instance()

        response = client.post("/api/export/mappings.csv", json={"session": session})
        assert response.status_code == 200
        assert response.text.count("\n") >= 46


class TestVoiceStatusIsStateless:
    def test_health_needs_no_session(self, client):
        fresh_instance()
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
