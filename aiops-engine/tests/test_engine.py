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
    main.PR_TIMES.clear()
    monkeypatch.setattr(main, "collect_context", lambda ns, dep: {"fake": True})
    monkeypatch.setattr(main, "AUTO_REMEDIATION", False)
    monkeypatch.setattr(main, "AUTO_PULL_REQUEST", False)
    monkeypatch.setattr(main, "VERIFY_INTERVAL_S", 0)
    monkeypatch.setattr(main, "VERIFY_TIMEOUT_S", 0.2)
    set_diag(monkeypatch)


def alert(ns="dev", status="firing", name="X"):
    return {"alerts": [{"status": status, "labels": {"alertname": name, "namespace": ns}}]}


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


def enable_pr(monkeypatch, fail=False):
    calls = []

    def fake(d, ns, dep, iid, name):
        if fail:
            raise RuntimeError("401 Bad credentials")
        calls.append((d.action, ns, dep, iid, name))
        return "https://github.com/x/y/pull/7"
    monkeypatch.setattr(main, "AUTO_PULL_REQUEST", True)
    monkeypatch.setattr(main.actions, "open_pull_request", fake)
    return calls


# ----- actions directes -----
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
    post()
    assert len(calls) == 1 and last()["status"] == "escalated"
    assert "délai de sécurité" in last()["decision"]["reason"]


def test_prod_is_never_acted_on_directly(monkeypatch):
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


# ----- pull requests -----
def test_pr_switch_off_means_simulation(monkeypatch):
    set_diag(monkeypatch, action="scale_deployment", replicas=3)
    post()
    inc = last()
    assert inc["status"] == "diagnosed" and inc["decision"]["route"] == "pull_request" and "pr_url" not in inc


def test_scale_opens_pull_request(monkeypatch):
    calls = enable_pr(monkeypatch)
    set_diag(monkeypatch, action="scale_deployment", replicas=3)
    post()
    inc = last()
    assert inc["status"] == "pending_review" and inc["pr_url"].endswith("/pull/7")
    assert calls[0][:3] == ("scale_deployment", "dev", "demo-app") and calls[0][4] == "X"


def test_memory_limit_opens_pull_request_even_in_prod(monkeypatch):
    calls = enable_pr(monkeypatch)
    set_diag(monkeypatch, action="set_memory_limit", memory_limit="512Mi")
    post(ns="prod")
    assert last()["status"] == "pending_review" and calls[0][1] == "prod"


def test_pr_failure_is_recorded(monkeypatch):
    enable_pr(monkeypatch, fail=True)
    set_diag(monkeypatch, action="scale_deployment", replicas=3)
    post()
    assert last()["status"] == "pr_failed" and "401" in last()["error"]


def test_pr_rate_limit(monkeypatch):
    calls = enable_pr(monkeypatch)
    set_diag(monkeypatch, action="scale_deployment", replicas=3)
    for i in range(4):
        post(name=f"Alert{i}")
    assert len(calls) == 3
    assert last()["status"] == "escalated" and "pull requests par heure" in last()["decision"]["reason"]


def test_alert_resolving_closes_pending_review(monkeypatch):
    enable_pr(monkeypatch)
    set_diag(monkeypatch, action="scale_deployment", replicas=3)
    post()
    post(status="resolved")
    assert last()["status"] == "resolved" and last()["verified_by"] == "alertmanager"


# ----- cas généraux -----
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


def test_no_signal_skips_llm_and_escalates(monkeypatch):
    called = []

    async def fake(*a, **k):
        called.append(1)
        return Diagnosis(cause="x", confidence=0.9, action="restart_deployment")
    monkeypatch.setattr(main, "diagnose", fake)
    monkeypatch.setattr(main, "collect_context",
                        lambda ns, dep: {"metrics": {"error_rate_5xx": 0.0}, "pods": [], "deployment": {}})
    post(name="DemoAppHighErrorRate")
    inc = last()
    assert inc["status"] == "escalated"
    assert "LLM non appelé" in inc["decision"]["reason"]
    assert called == []
