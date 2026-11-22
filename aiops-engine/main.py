"""Moteur AIOps v0 : reçoit les alertes Alertmanager et les enregistre."""
import logging
import time
import uuid

from fastapi import FastAPI

log = logging.getLogger("aiops")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="aiops-engine")
INCIDENTS: dict[str, dict] = {}  # en mémoire, suffisant pour la démo
WATCHED_NAMESPACES = {"dev", "prod"}


def _open_key_exists(key: str) -> bool:
    return any(i["key"] == key and i["status"] == "received" for i in INCIDENTS.values())


@app.post("/webhook/alertmanager")
def alertmanager_webhook(payload: dict):
    created = []
    for a in payload.get("alerts", []):
        labels = a.get("labels", {})
        if labels.get("namespace") not in WATCHED_NAMESPACES or a.get("status") != "firing":
            continue
        key = f'{labels.get("alertname")}/{labels.get("namespace")}'
        if _open_key_exists(key):
            continue  # déduplication
        iid = uuid.uuid4().hex[:8]
        INCIDENTS[iid] = {"id": iid, "key": key, "status": "received",
                          "detected_at": time.time(), "alert": a}
        log.info("incident %s reçu : %s", iid, key)
        created.append(iid)
    return {"created": created}


@app.get("/incidents")
def list_incidents():
    return list(INCIDENTS.values())


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
