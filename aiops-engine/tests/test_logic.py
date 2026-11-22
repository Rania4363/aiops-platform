import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError

from allowlist import decide
from collector import clean_logs, recent_warnings
from llm import build_messages
from schemas import Diagnosis


def diag(**kw):
    base = dict(cause="x", confidence=0.9, action="restart_deployment")
    base.update(kw)
    return Diagnosis(**base)


D = {"dev"}


def test_restart_dev_direct():
    assert decide(diag(), "dev", "demo-app", D).route == "direct"


def test_restart_prod_escalated():
    assert decide(diag(), "prod", "demo-app", D).route == "escalate"


def test_low_confidence_escalates():
    assert decide(diag(confidence=0.3), "dev", "demo-app", D).route == "escalate"


def test_target_outside_allowlist_rejected():
    assert decide(diag(), "dev", "kube-apiserver", D).route == "rejected"
    assert decide(diag(), "ml-serving", "demo-app", D).route == "rejected"


def test_scale_bounds_and_routes():
    assert decide(diag(action="scale_deployment", replicas=3), "dev", "demo-app", D).route == "direct"
    assert decide(diag(action="scale_deployment", replicas=3), "prod", "demo-app", D).route == "pull_request"
    assert decide(diag(action="scale_deployment", replicas=50), "dev", "demo-app", D).route == "rejected"
    assert decide(diag(action="scale_deployment", replicas=0), "dev", "demo-app", D).route == "rejected"


def test_memory_limit_validation():
    ok = decide(diag(action="set_memory_limit", memory_limit="512Mi"), "dev", "demo-app", D)
    assert ok.route == "pull_request"
    for bad in ("64Gi", "5Mi", "abc", ""):
        assert decide(diag(action="set_memory_limit", memory_limit=bad), "dev", "demo-app", D).route == "rejected"
    with pytest.raises(ValidationError):  # première barrière : le schéma refuse les chaînes trop longues
        diag(action="set_memory_limit", memory_limit="; rm -rf /")


def test_rollback_tag_validation():
    assert decide(diag(action="rollback_image", image_tag="sha-abc1234"), "dev", "demo-app", D).route == "pull_request"
    assert decide(diag(action="rollback_image", image_tag="latest"), "dev", "demo-app", D).route == "rejected"


def test_schema_rejects_unknown_action_and_extra_fields():
    with pytest.raises(ValidationError):
        diag(action="delete_namespace")
    with pytest.raises(ValidationError):
        Diagnosis(cause="x", confidence=0.9, action="escalate", namespace="kube-system")


def test_json_schema_is_flat_for_llama_cpp():
    raw = json.dumps(Diagnosis.model_json_schema())
    assert "$ref" not in raw and "$defs" not in raw


def test_clean_logs_drops_noise_and_groups():
    lines = ['INFO:     1.2.3.4:5 - "GET /healthz HTTP/1.1" 200 OK',
             'INFO:     1.2.3.4:5 - "GET /metrics HTTP/1.1" 200 OK'] + \
            ['INFO:     127.0.0.1:1 - "GET / HTTP/1.1" 200 OK'] * 3 + \
            ['INFO:     127.0.0.1:2 - "GET / HTTP/1.1" 500 Internal Server Error'] * 2
    assert clean_logs(lines) == ['3x "GET / HTTP/1.1" 200 OK', '2x "GET / HTTP/1.1" 500 Internal Server Error']


def ev(reason, minutes_ago, now, type_="Warning"):
    ts = now - timedelta(minutes=minutes_ago)
    return NS(type=type_, reason=reason, message="m" * 200, last_timestamp=ts, event_time=None,
              metadata=NS(creation_timestamp=ts))


def test_recent_warnings_filters_old_and_duplicates():
    now = datetime.now(timezone.utc)
    events = [ev("Unhealthy", 1, now), ev("Unhealthy", 2, now), ev("BackOff", 30, now),
              ev("Pulled", 1, now, type_="Normal")]
    out = recent_warnings(events, now)
    assert len(out) == 1 and out[0].startswith("Unhealthy:") and len(out[0]) < 110


def test_prompt_stays_small():
    ctx = {"deployment": {"replicas": 1, "image_tag": "sha-5f33639", "memory_limit": "256Mi"},
           "pods": [{"phase": "Running", "restarts": 0, "waiting": None, "last_termination": None}],
           "events": [], "logs": ['3x "GET / HTTP/1.1" 200 OK', '3x "GET / HTTP/1.1" 500 Internal Server Error'],
           "metrics": {"error_rate_5xx": 0.71, "latency_p95_s": 0.005, "memory_mb": 37.4}}
    msgs = build_messages("DemoAppHighErrorRate", "Taux d'erreurs 5xx élevé dans dev", "dev", ctx)
    assert len(msgs[1]["content"]) < 700


def test_system_prompt_covers_all_actions_and_forbids_zero_replicas():
    from llm import SYSTEM_PROMPT
    for action in ("restart_deployment", "scale_deployment", "set_memory_limit", "rollback_image", "escalate"):
        assert action in SYSTEM_PROMPT
    assert "jamais replicas à 0" in SYSTEM_PROMPT
