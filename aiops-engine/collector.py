"""Collecte compacte du contexte d'un incident (lecture seule). Le LLM lit lentement : chaque token compte."""
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

PROMETHEUS_URL = os.getenv(
    "PROMETHEUS_URL", "http://monitoring-kube-prometheus-prometheus.monitoring.svc:9090")
EVENT_WINDOW_MIN = 10
MAX_EVENTS = 3
LOG_FETCH_LINES = 50
MAX_LOG_ENTRIES = 6
_LOG_PREFIX = re.compile(r"^INFO:\s+\S+ - ")


def _clip(text, n):
    return (text or "")[:n]


def clean_logs(lines: list[str], keep: int = MAX_LOG_ENTRIES) -> list[str]:
    """Retire healthz/metrics et les adresses, puis regroupe les lignes identiques."""
    cleaned = [_clip(_LOG_PREFIX.sub("", ln), 100) for ln in lines
               if "/healthz" not in ln and "/metrics" not in ln]
    return [f"{n}x {msg}" if n > 1 else msg for msg, n in Counter(cleaned).most_common(keep)]


def _event_time(e):
    return e.last_timestamp or e.event_time or e.metadata.creation_timestamp


def recent_warnings(events, now: datetime, minutes: int = EVENT_WINDOW_MIN, keep: int = MAX_EVENTS) -> list[str]:
    """Avertissements récents seulement, un par motif (les plus récents d'abord)."""
    out, seen = [], set()
    for e in sorted((e for e in events if e.type == "Warning"), key=_event_time, reverse=True):
        if _event_time(e) < now - timedelta(minutes=minutes) or e.reason in seen:
            continue
        seen.add(e.reason)
        out.append(f"{e.reason}: {_clip(e.message, 90)}")
    return out[:keep]


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
    ctx = {"deployment": {"replicas": dep.spec.replicas, "image_tag": c.image.rsplit(":", 1)[-1],
                          "memory_limit": limits.get("memory")}}

    pods = core.list_namespaced_pod(namespace, label_selector=f"app={deployment}").items
    ctx["pods"] = []
    for p in pods[:3]:
        cs = (p.status.container_statuses or [None])[0]
        waiting = cs.state.waiting.reason if cs and cs.state and cs.state.waiting else None
        last = cs.last_state.terminated.reason if cs and cs.last_state and cs.last_state.terminated else None
        ctx["pods"].append({"phase": p.status.phase, "restarts": cs.restart_count if cs else 0,
                            "waiting": waiting, "last_termination": last})

    ctx["events"] = recent_warnings(core.list_namespaced_event(namespace).items, datetime.now(timezone.utc))

    ctx["logs"] = []
    if pods:
        try:
            raw = core.read_namespaced_pod_log(pods[0].metadata.name, namespace, tail_lines=LOG_FETCH_LINES)
            ctx["logs"] = clean_logs(raw.splitlines())
        except Exception as exc:
            ctx["logs"] = [f"<logs indisponibles: {exc.__class__.__name__}>"]
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


_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def alert_firing(alertname: str, namespace: str):
    """True si l'alerte est toujours active, False si elle a disparu, None si on ne sait pas."""
    if not _NAME_RE.match(alertname or "") or not _NAME_RE.match(namespace or ""):
        return None
    try:
        q = f'ALERTS{{alertname="{alertname}",namespace="{namespace}",alertstate="firing"}}'
        r = httpx.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": q}, timeout=10)
        return len(r.json()["data"]["result"]) > 0
    except Exception:
        return None
