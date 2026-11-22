import base64
import datetime as dt

import pytest
import yaml

import actions
from actions import (ActionRefused, check_allowed, check_pr_allowed, open_pull_request,
                     patch_kustomization_text, restart_body)
from schemas import Diagnosis

D = {"dev"}

OVERLAY = """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: dev
resources:
  - ../../base
images:
  - name: ghcr.io/rania4363/demo-app
    newName: ghcr.io/rania4363/demo-app
    newTag: sha-1111111
patches:
  - target: {kind: Deployment, name: demo-app}
    patch: |-
      - op: replace
        path: /spec/template/spec/containers/0/env/1/value
        value: dev
"""


def diag(**kw):
    base = dict(cause="x", confidence=0.9, action="restart_deployment")
    base.update(kw)
    return Diagnosis(**base)


# ----- actions directes : le redémarrage seulement -----
def test_restart_allowed_on_dev_demo_app():
    check_allowed(diag(), "dev", "demo-app", D)


@pytest.mark.parametrize("ns,dep", [("prod", "demo-app"), ("dev", "mlflow"), ("ml-serving", "demo-app")])
def test_other_targets_refused(ns, dep):
    with pytest.raises(ActionRefused):
        check_allowed(diag(), ns, dep, {"dev", "prod", "ml-serving"})


def test_namespace_not_in_direct_set_refused():
    with pytest.raises(ActionRefused):
        check_allowed(diag(), "dev", "demo-app", set())


@pytest.mark.parametrize("action,extra", [("scale_deployment", {"replicas": 3}),
                                           ("set_memory_limit", {"memory_limit": "512Mi"}),
                                           ("rollback_image", {"image_tag": "sha-abc1234"}),
                                           ("escalate", {})])
def test_everything_but_restart_refused_in_direct(action, extra):
    with pytest.raises(ActionRefused):
        check_allowed(diag(action=action, **extra), "dev", "demo-app", D)


def test_restart_body():
    ann = restart_body(dt.datetime(2026, 11, 22, tzinfo=dt.timezone.utc))["spec"]["template"]["metadata"]["annotations"]
    assert ann["kubectl.kubernetes.io/restartedAt"].startswith("2026-11-22")


# ----- garde-fous des PR -----
def test_pr_allowed_cases():
    check_pr_allowed(diag(action="scale_deployment", replicas=3), "prod", "demo-app")
    check_pr_allowed(diag(action="set_memory_limit", memory_limit="512Mi"), "dev", "demo-app")
    check_pr_allowed(diag(action="rollback_image", image_tag="sha-abc1234"), "dev", "demo-app")


@pytest.mark.parametrize("kw,ns,dep", [
    (dict(action="restart_deployment"), "dev", "demo-app"),
    (dict(action="scale_deployment", replicas=9), "dev", "demo-app"),
    (dict(action="scale_deployment", replicas=0), "dev", "demo-app"),
    (dict(action="set_memory_limit", memory_limit="64Gi"), "dev", "demo-app"),
    (dict(action="rollback_image", image_tag="latest"), "dev", "demo-app"),
    (dict(action="scale_deployment", replicas=2), "kube-system", "demo-app"),
    (dict(action="scale_deployment", replicas=2), "dev", "mlflow"),
])
def test_pr_refused_cases(kw, ns, dep):
    with pytest.raises(ActionRefused):
        check_pr_allowed(diag(**kw), ns, dep)


# ----- modification minimale du fichier -----
def test_memory_patch_is_valid_yaml_and_idempotent():
    d = diag(action="set_memory_limit", memory_limit="512Mi")
    once = patch_kustomization_text(OVERLAY, d)
    twice = patch_kustomization_text(once, d)
    assert once.startswith(OVERLAY)  # on n'a fait qu'ajouter à la fin
    assert twice.count("512Mi") == 1
    patches = yaml.safe_load(twice)["patches"]
    assert len(patches) == 2 and "limits/memory" in patches[1]["patch"] and "512Mi" in patches[1]["patch"]


def test_new_value_replaces_previous_block():
    first = patch_kustomization_text(OVERLAY, diag(action="set_memory_limit", memory_limit="512Mi"))
    second = patch_kustomization_text(first, diag(action="set_memory_limit", memory_limit="768Mi"))
    assert "768Mi" in second and "512Mi" not in second
    assert len(yaml.safe_load(second)["patches"]) == 2


def test_scale_patch_and_rollback_tag():
    out = patch_kustomization_text(OVERLAY, diag(action="scale_deployment", replicas=3))
    assert "path: /spec/replicas" in out and "value: 3" in out
    assert len(yaml.safe_load(out)["patches"]) == 2
    rb = patch_kustomization_text(OVERLAY, diag(action="rollback_image", image_tag="sha-abc1234"))
    assert "newTag: sha-abc1234" in rb and "sha-1111111" not in rb


def test_missing_sections_raise():
    with pytest.raises(ValueError):
        patch_kustomization_text("kind: Kustomization\n", diag(action="scale_deployment", replicas=2))
    with pytest.raises(ValueError):
        patch_kustomization_text("kind: Kustomization\n", diag(action="rollback_image", image_tag="sha-abc1234"))


# ----- création de la PR (API GitHub simulée) -----
class Resp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class FakeGH:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        self.calls.append(("GET", url, None))
        if url.startswith("/git/ref"):
            return Resp({"object": {"sha": "basesha"}})
        return Resp({"content": base64.b64encode(OVERLAY.encode()).decode(), "sha": "filesha"})

    def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, json))
        return Resp({"html_url": "https://github.com/x/y/pull/1"} if url == "/pulls" else {})

    def put(self, url, json=None, **kw):
        self.calls.append(("PUT", url, json))
        return Resp({})


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGH()
    monkeypatch.setattr(actions, "_gh_client", lambda: fake)
    monkeypatch.setattr(actions, "GITHUB_REPO", "Rania4363/aiops-platform")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    return fake


def test_open_pull_request_full_flow(gh):
    d = diag(action="set_memory_limit", memory_limit="512Mi", cause="fuite @admin `rm` <b>x</b>\nligne 2")
    url = open_pull_request(d, "dev", "demo-app", "abcd1234", "DemoAppMemoryNearLimit")
    assert url.endswith("/pull/1")
    kinds = [(m, u) for m, u, _ in gh.calls]
    assert kinds == [("GET", "/git/ref/heads/main"), ("POST", "/git/refs"),
                     ("GET", "/contents/manifests/overlays/dev/kustomization.yaml"),
                     ("PUT", "/contents/manifests/overlays/dev/kustomization.yaml"), ("POST", "/pulls")]
    assert gh.calls[1][2]["ref"] == "refs/heads/aiops/abcd1234"
    put = gh.calls[3][2]
    assert put["branch"] == "aiops/abcd1234" and put["sha"] == "filesha"
    assert "512Mi" in base64.b64decode(put["content"]).decode()
    body = gh.calls[4][2]["body"]
    assert "@" not in body and "`rm`" not in body and "<b>" not in body and "validation humaine" in body


def test_open_pull_request_refuses_before_any_network_call(gh):
    with pytest.raises(ActionRefused):
        open_pull_request(diag(action="restart_deployment"), "dev", "demo-app", "abcd1234", "X")
    with pytest.raises(ActionRefused):
        open_pull_request(diag(action="scale_deployment", replicas=2), "dev", "demo-app", "../etc", "X")
    assert gh.calls == []


def test_open_pull_request_needs_token(monkeypatch):
    monkeypatch.setattr(actions, "GITHUB_REPO", "Rania4363/aiops-platform")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ActionRefused):
        open_pull_request(diag(action="scale_deployment", replicas=2), "dev", "demo-app", "abcd1234", "X")
