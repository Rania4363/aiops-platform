"""Génère platform/alerts/dashboard-aiops.yaml (ConfigMap lue par le sidecar Grafana).
Usage : python3 scripts/gen_dashboard.py   (depuis la racine du dépôt)"""
import json

DS = {"type": "prometheus", "uid": "prometheus"}
NS = 'namespace=~"$namespace"'


def panel(pid, title, kind, x, y, w, h, exprs, unit="short", thresholds=None, desc=""):
    steps = [{"color": "green", "value": None}]
    for value, color in (thresholds or []):
        steps.append({"color": color, "value": value})
    p = {"id": pid, "title": title, "type": kind, "description": desc, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": lg}
                     for i, (e, lg) in enumerate(exprs)],
         "fieldConfig": {"defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}},
                         "overrides": []}}
    if kind == "timeseries":
        p["fieldConfig"]["defaults"]["custom"] = {"lineWidth": 2, "fillOpacity": 10,
                                                  "thresholdsStyle": {"mode": "line"}}
        p["options"] = {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}
    else:
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background"}
    return p


panels = [
    panel(1, "Pods demo-app actifs", "stat", 0, 0, 6, 4,
          [(f'sum(up{{job="demo-app",{NS}}})', "")], thresholds=[(1, "green")],
          desc="Nombre de pods scrapés par Prometheus"),
    panel(2, "Alertes AIOps en cours", "stat", 6, 0, 6, 4,
          [('count(ALERTS{project="aiops",alertstate="firing"}) or vector(0)', "")],
          thresholds=[(1, "red")], desc="Alertes DemoApp* au statut firing"),
    panel(3, "Redémarrages de conteneur (1 h)", "stat", 12, 0, 6, 4,
          [(f'sum(increase(kube_pod_container_status_restarts_total{{container="app",{NS}}}[1h])) or vector(0)', "")],
          thresholds=[(1, "orange"), (3, "red")]),
    panel(4, "Réplicas disponibles", "stat", 18, 0, 6, 4,
          [(f'sum by (namespace) (kube_deployment_status_replicas_available{{deployment="demo-app",{NS}}})',
            "{{namespace}}")], desc="Visible après un changement de réplicas par Pull Request"),
    panel(5, "Taux d'erreurs 5xx", "timeseries", 0, 4, 12, 8,
          [(f'sum by (namespace) (rate(demo_http_requests_total{{{NS},status="500"}}[2m]))'
            f' / clamp_min(sum by (namespace) (rate(demo_http_requests_total{{{NS}}}[2m])), 0.001)', "{{namespace}}")],
          unit="percentunit", thresholds=[(0.2, "red")], desc="Seuil d'alerte DemoAppHighErrorRate : 20 %"),
    panel(6, "Latence p95", "timeseries", 12, 4, 12, 8,
          [(f'histogram_quantile(0.95, sum by (le, namespace) (rate(demo_http_request_seconds_bucket{{{NS}}}[2m])))',
            "{{namespace}}")], unit="s", thresholds=[(1, "red")], desc="Seuil d'alerte DemoAppHighLatency : 1 s"),
    panel(7, "Mémoire utilisée / limite", "timeseries", 0, 12, 12, 8,
          [(f'max by (namespace, pod) (container_memory_working_set_bytes{{container="app",{NS}}})'
            f' / max by (namespace, pod) (kube_pod_container_resource_limits{{resource="memory",container="app",{NS}}})',
            "{{namespace}} {{pod}}")], unit="percentunit", thresholds=[(0.85, "red")],
          desc="Seuil d'alerte DemoAppMemoryNearLimit : 85 %"),
    panel(8, "Requêtes par code HTTP", "timeseries", 12, 12, 12, 8,
          [(f'sum by (namespace, status) (rate(demo_http_requests_total{{{NS}}}[2m]))', "{{namespace}} {{status}}")],
          unit="reqps"),
]

dash = {
    "uid": "aiops-demo-app", "title": "AIOps - demo-app (dev / prod)", "tags": ["aiops"],
    "schemaVersion": 39, "version": 1, "editable": True, "refresh": "30s",
    "time": {"from": "now-1h", "to": "now"},
    "templating": {"list": [{
        "name": "namespace", "label": "Namespace", "type": "query", "datasource": DS,
        "query": {"query": "label_values(demo_http_requests_total, namespace)", "refId": "ns"},
        "refresh": 2, "includeAll": True, "multi": True, "current": {"text": "All", "value": "$__all"}}]},
    "panels": panels,
}

body = json.dumps(dash, indent=1, ensure_ascii=False)
indented = "\n".join("    " + ln for ln in body.splitlines())
out = f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: aiops-dashboard
  namespace: aiops
  labels:
    grafana_dashboard: "1"
data:
  aiops-demo-app.json: |
{indented}
"""
open("platform/alerts/dashboard-aiops.yaml", "w", encoding="utf-8").write(out)
print("écrit : platform/alerts/dashboard-aiops.yaml", len(out), "octets")
