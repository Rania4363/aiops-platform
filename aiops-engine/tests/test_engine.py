import pytest
from fastapi.testclient import TestClient

import main
from main import INCIDENTS, app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    INCIDENTS.clear()
    monkeypatch.setattr(main, "collect_context", lambda ns, dep: {"fake": True})


def alert(ns="dev", status="firing"):
    return {"alerts": [{"status": status, "labels": {"alertname": "X", "namespace": ns}}]}


def post(**kw):
    return client.post("/webhook/alertmanager", json=alert(**kw)).json()["created"]


def test_firing_alert_creates_incident_and_collects_context():
    assert len(post()) == 1
    inc = client.get("/incidents").json()[0]
    assert inc["status"] == "collected" and inc["context"] == {"fake": True}


def test_duplicate_is_ignored():
    post()
    assert post() == []


def test_other_namespace_ignored():
    assert post(ns="ml-serving") == []


def test_resolved_closes_incident_and_allows_a_new_one():
    post()
    assert post(status="resolved") == []
    assert client.get("/incidents").json()[0]["status"] == "resolved"
    assert len(post()) == 1


def test_collect_failure_is_recorded(monkeypatch):
    def boom(ns, dep):
        raise RuntimeError("pas d'accès")
    monkeypatch.setattr(main, "collect_context", boom)
    post()
    inc = client.get("/incidents").json()[0]
    assert inc["status"] == "collect_failed" and "pas d'accès" in inc["error"]
