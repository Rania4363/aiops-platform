import pytest
from fastapi.testclient import TestClient

import main
from llm import DiagnosisError
from main import INCIDENTS, app
from schemas import Diagnosis

client = TestClient(app)


def set_diag(monkeypatch, **kw):
    base = dict(cause="état interne", confidence=0.9, action="restart_deployment")
    base.update(kw)

    async def fake(*a, **k):
        return Diagnosis(**base)
    monkeypatch.setattr(main, "diagnose", fake)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    INCIDENTS.clear()
    monkeypatch.setattr(main, "collect_context", lambda ns, dep: {"fake": True})
    set_diag(monkeypatch)


def alert(ns="dev", status="firing"):
    return {"alerts": [{"status": status, "labels": {"alertname": "X", "namespace": ns}}]}


def post(**kw):
    return client.post("/webhook/alertmanager", json=alert(**kw)).json()["created"]


def last():
    return client.get("/incidents").json()[-1]


def test_restart_in_dev_is_diagnosed_with_direct_route():
    assert len(post()) == 1
    inc = last()
    assert inc["status"] == "diagnosed" and inc["decision"]["route"] == "direct"
    assert inc["diagnosis"]["action"] == "restart_deployment" and "llm_seconds" in inc


def test_low_confidence_is_escalated(monkeypatch):
    set_diag(monkeypatch, confidence=0.2)
    post()
    assert last()["status"] == "escalated"


def test_prod_restart_is_escalated():
    post(ns="prod")
    assert last()["status"] == "escalated"


def test_collect_failure_is_recorded(monkeypatch):
    def boom(ns, dep):
        raise RuntimeError("pas d'accès")
    monkeypatch.setattr(main, "collect_context", boom)
    post()
    assert last()["status"] == "collect_failed" and "pas d'accès" in last()["error"]


def test_diagnosis_failure_is_recorded(monkeypatch):
    async def boom(*a, **k):
        raise DiagnosisError("json invalide")
    monkeypatch.setattr(main, "diagnose", boom)
    post()
    assert last()["status"] == "diagnosis_failed"


def test_duplicate_is_ignored():
    post()
    assert post() == []


def test_other_namespace_ignored():
    assert post(ns="ml-serving") == []


def test_resolved_closes_incident_and_allows_a_new_one():
    post()
    assert post(status="resolved") == []
    assert last()["status"] == "resolved"
    assert len(post()) == 1
