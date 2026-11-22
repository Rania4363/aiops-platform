"""Collecte compacte du contexte d'un incident (lecture seule)."""
import os

import httpx

PROMETHEUS_URL = os.getenv(
    "PROMETHEUS_URL", "http://monitoring-kube-prometheus-prometheus.monitoring.svc:9090")
MAX_EVENTS = 5
MAX_LOG_LINES = 10


def _clip(text, n):
    return (text or "")[:n]


def _clients():
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api()


def collect_k8s(namespace: str, deployment: str) -> dict:
    core, apps = _clients()
    dep = apps.read_namespaced_deployment(deployment, namespace)
    c = dep.spec.template.spec.containers[0]
    limits = (c.resources.limits or {}) if c.resources else {}
    ctx = {"deployment": {
        "replicas": dep.spec.replicas,
        "image_tag": c.image.rsplit(":", 1)[-1],
        "memory_limit": limits.get("memory"),
    }}

    pods = core.list_namespaced_pod(namespace, label_selector=f"app={deployment}").items
    ctx["pods"] = []
    for p in pods[:3]:
        cs = (p.status.container_statuses or [None])[0]
        waiting = cs.state.waiting.reason if cs and cs.state and cs.state.waiting else None
        last = cs.last_state.terminated.reason if cs and cs.last_state and cs.last_state.terminated else None
        ctx["pods"].append({"phase": p.status.phase, "restarts": cs.restart_count if cs else 0,
                            "waiting": waiting, "last_termination": last})

    events = [e for e in core.list_namespaced_event(namespace).items if e.type == "Warning"]
    events.sort(key=lambda e: e.last_timestamp or e.event_time or e.metadata.creation_timestamp,
                reverse=True)
    ctx["events"] = [f"{e.reason}: {_clip(e.message, 120)}" for e in events[:MAX_EVENTS]]

    ctx["logs_tail"] = []
    if pods:
        try:
            raw = core.read_namespaced_pod_log(pods[0].metadata.name, namespace,
                                               tail_lines=MAX_LOG_LINES)
            ctx["logs_tail"] = [_clip(ln, 150) for ln in raw.splitlines()[-MAX_LOG_LINES:]]
        except Exception as exc:
            ctx["logs_tail"] = [f"<logs indisponibles: {exc.__class__.__name__}>"]
    return ctx


def _prom(query: str):
    try:
        r = httpx.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
        res = r.json()["data"]["result"]
        return round(float(res[0]["value"][1]), 3) if res else None
    except Exception:
        return None


def collect_metrics(namespace: str) -> dict:
    ns = namespace
    return {
        "error_rate_5xx": _prom(
            f'sum(rate(demo_http_requests_total{{namespace="{ns}",status="500"}}[2m]))'
            f' / clamp_min(sum(rate(demo_http_requests_total{{namespace="{ns}"}}[2m])), 0.001)'),
        "latency_p95_s": _prom(
            f'histogram_quantile(0.95, sum by (le) '
            f'(rate(demo_http_request_seconds_bucket{{namespace="{ns}"}}[2m])))'),
        "memory_mb": _prom(
            f'max(container_memory_working_set_bytes{{namespace="{ns}",container="app"}})/1024/1024'),
    }


def collect_context(namespace: str, deployment: str) -> dict:
    ctx = collect_k8s(namespace, deployment)
    ctx["metrics"] = collect_metrics(namespace)
    return ctx
