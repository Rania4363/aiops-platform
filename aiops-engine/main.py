"""Moteur AIOps v1 : réception des alertes, collecte du contexte."""
import asyncio
import json
import logging
import time
import uuid

from fastapi import BackgroundTasks, FastAPI

from collector import collect_context

log = logging.getLogger("aiops")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="aiops-engine", version="0.2")
INCIDENTS: dict[str, dict] = {}  # en mémoire, suffisant pour la démo
WATCHED_NAMESPACES = {"dev", "prod"}
CLOSED = {"resolved", "collect_failed"}


def _open_incident(key: str):
    return next((i for i in INCIDENTS.values() if i["key"] == key and i["status"] not in CLOSED), None)


async def process(incident_id: str) -> None:
    inc = INCIDENTS[incident_id]
    labels = inc["alert"]["labels"]
    try:
        inc["status"] = "collecting"
        ctx = await asyncio.to_thread(collect_context, labels["namespace"], labels.get("app", "demo-app"))
        inc["context"] = ctx
        inc["context_chars"] = len(json.dumps(ctx, ensure_ascii=False))
        inc["timeline"]["collected"] = time.time()
        inc["status"] = "collected"
        log.info("incident %s : contexte collecté (%d caractères)", incident_id, inc["context_chars"])
    except Exception as exc:
        inc["status"], inc["error"] = "collect_failed", f"{exc.__class__.__name__}: {exc}"
        log.error("incident %s : collecte échouée : %s", incident_id, inc["error"])


@app.post("/webhook/alertmanager")
async def alertmanager_webhook(payload: dict, bg: BackgroundTasks):
    created = []
    for a in payload.get("alerts", []):
        labels = a.get("labels", {})
        if labels.get("namespace") not in WATCHED_NAMESPACES:
            continue
        key = f'{labels.get("alertname")}/{labels.get("namespace")}'
        existing = _open_incident(key)
        if a.get("status") == "resolved":
            if existing:
                existing["status"] = "resolved"
                existing["timeline"]["resolved"] = time.time()
                log.info("incident %s résolu", existing["id"])
            continue
        if existing:
            continue  # déduplication
        iid = uuid.uuid4().hex[:8]
        INCIDENTS[iid] = {"id": iid, "key": key, "status": "received", "alert": a,
                          "timeline": {"detected": time.time()}}
        log.info("incident %s reçu : %s", iid, key)
        bg.add_task(process, iid)
        created.append(iid)
    return {"created": created}


@app.get("/incidents")
def list_incidents():
    return list(INCIDENTS.values())


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
