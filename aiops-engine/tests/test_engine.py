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
    main.LAST_ACTION.clear()
    monkeypatch.setattr(main, "collect_context", lambda ns, dep: {"fake": True})
    monkeypatch.setattr(main, "AUTO_REMEDIATION", False)
    monkeypatch.setattr(main, "VERIFY_INTERVAL_S", 0)
    monkeypatch.setattr(main, "VERIFY_TIMEOUT_S", 0.2)
    set_diag(monkeypatch)


def alert(ns="dev", status="firing"):
    return {"alerts": [{"status": status, "labels": {"alertname": "X", "namespace": ns}}]}


def post(**kw):
    return client.post("/webhook/alertmanager", json=alert(**kw)).json()["created"]


def last():
    return client.get("/incidents").json()[-1]


def enable_actions(monkeypatch, firing):
    calls = []
    monkeypatch.setattr(main, "AUTO_REMEDIATION", True)
    monkeypatch.setattr(main.actions, "run_direct", lambda d, ns, dep, dn: calls.append((ns, dep)) or "fait")
    monkeypatch.setattr(main, "alert_firing", lambda name, ns: firing)
    return calls


def test_switch_off_means_simulation_only():
    post()
    inc = last()
    assert inc["status"] == "diagnosed" and inc["decision"]["route"] == "direct" and "result" not in inc


def test_action_then_verified_resolution(monkeypatch):
    calls = enable_actions(monkeypatch, firing=False)
    post()
    inc = last()
    assert calls == [("dev", "demo-app")]
    assert inc["status"] == "resolved" and inc["verified_by"] == "prometheus" and inc["result"] == "fait"


def test_unresolved_when_alert_keeps_firing(monkeypatch):
    enable_actions(monkeypatch, firing=True)
    post()
    assert last()["status"] == "unresolved"


def test_cooldown_blocks_second_action(monkeypatch):
    calls = enable_actions(monkeypatch, firing=False)
    post()
    post()  # nouvel incident après résolution
    assert len(calls) == 1 and last()["status"] == "escalated"
    assert "délai de sécurité" in last()["decision"]["reason"]


def test_prod_is_never_acted_on(monkeypatch):
    calls = enable_actions(monkeypatch, firing=False)
    post(ns="prod")
    assert calls == [] and last()["status"] == "escalated"


def test_action_failure_is_recorded(monkeypatch):
    enable_actions(monkeypatch, firing=False)

    def boom(*a):
        raise RuntimeError("403 Forbidden")
    monkeypatch.setattr(main.actions, "run_direct", boom)
    post()
    assert last()["status"] == "action_failed" and "403" in last()["error"]


def test_low_confidence_is_escalated(monkeypatch):
    set_diag(monkeypatch, confidence=0.2)
    post()
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
