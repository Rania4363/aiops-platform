from fastapi.testclient import TestClient

from main import INCIDENTS, app

client = TestClient(app)


def alert(ns="dev", status="firing"):
    return {"alerts": [{"status": status, "labels": {"alertname": "X", "namespace": ns}}]}


def setup_function():
    INCIDENTS.clear()


def test_firing_alert_creates_incident():
    assert len(client.post("/webhook/alertmanager", json=alert()).json()["created"]) == 1


def test_duplicate_is_ignored():
    client.post("/webhook/alertmanager", json=alert())
    assert client.post("/webhook/alertmanager", json=alert()).json()["created"] == []


def test_other_namespace_ignored():
    assert client.post("/webhook/alertmanager", json=alert(ns="ml-serving")).json()["created"] == []


def test_resolved_ignored():
    assert client.post("/webhook/alertmanager", json=alert(status="resolved")).json()["created"] == []
