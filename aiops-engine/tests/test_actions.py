import datetime as dt

import pytest

from actions import ActionRefused, check_allowed, restart_body, scale_body
from schemas import Diagnosis

D = {"dev"}


def diag(**kw):
    base = dict(cause="x", confidence=0.9, action="restart_deployment")
    base.update(kw)
    return Diagnosis(**base)


def test_restart_and_scale_allowed_on_dev_demo_app():
    check_allowed(diag(), "dev", "demo-app", D)
    check_allowed(diag(action="scale_deployment", replicas=3), "dev", "demo-app", D)


@pytest.mark.parametrize("ns,dep", [("prod", "demo-app"), ("dev", "mlflow"), ("ml-serving", "demo-app")])
def test_other_targets_refused(ns, dep):
    with pytest.raises(ActionRefused):
        check_allowed(diag(), ns, dep, {"dev", "prod", "ml-serving"})


def test_namespace_not_in_direct_set_refused():
    with pytest.raises(ActionRefused):
        check_allowed(diag(), "dev", "demo-app", set())


@pytest.mark.parametrize("action,extra", [("set_memory_limit", {"memory_limit": "512Mi"}),
                                           ("rollback_image", {"image_tag": "sha-abc1234"}),
                                           ("escalate", {})])
def test_non_direct_actions_refused(action, extra):
    with pytest.raises(ActionRefused):
        check_allowed(diag(action=action, **extra), "dev", "demo-app", D)


@pytest.mark.parametrize("n", [0, 6, 50])
def test_scale_out_of_range_refused(n):
    with pytest.raises(ActionRefused):
        check_allowed(diag(action="scale_deployment", replicas=n), "dev", "demo-app", D)


def test_patch_bodies():
    now = dt.datetime(2026, 11, 22, tzinfo=dt.timezone.utc)
    ann = restart_body(now)["spec"]["template"]["metadata"]["annotations"]
    assert ann["kubectl.kubernetes.io/restartedAt"].startswith("2026-11-22")
    assert scale_body(3) == {"spec": {"replicas": 3}}
