"""Moteur AIOps v2 : alerte -> contexte -> diagnostic LLM -> décision (allowlist). Aucune action exécutée."""
import asyncio
import json
import logging
import os
import time
import uuid

from fastapi import BackgroundTasks, FastAPI

from allowlist import decide
from collector import collect_context
from llm import DiagnosisError, diagnose

log = logging.getLogger("aiops")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="aiops-engine", version="0.3")
INCIDENTS: dict[str, dict] = {}  # en mémoire, suffisant pour la démo
WATCHED_NAMESPACES = {"dev", "prod"}
DIRECT_NAMESPACES = {n for n in os.getenv("DIRECT_ACTION_NAMESPACES", "dev").split(",") if n}
CLOSED = {"resolved", "collect_failed", "diagnosis_failed"}
LLM_LOCK = asyncio.Semaphore(1)  # un seul diagnostic à la fois : le LLM est lent


def _open_incident(key: str):
    return next((i for i in INCIDENTS.values() if i["key"] == key and i["status"] not in CLOSED), None)


async def process(incident_id: str) -> None:
    inc = INCIDENTS[incident_id]
    alert = inc["alert"]
    labels = alert["labels"]
    ns, dep = labels["namespace"], labels.get("app", "demo-app")

    try:
        inc["status"] = "collecting"
        ctx = await asyncio.to_thread(collect_context, ns, dep)
        inc["context"] = ctx
        inc["context_chars"] = len(json.dumps(ctx, ensure_ascii=False))
        inc["timeline"]["collected"] = time.time()
    except Exception as exc:
        inc["status"], inc["error"] = "collect_failed", f"{exc.__class__.__name__}: {exc}"
        log.error("incident %s : collecte échouée : %s", incident_id, inc["error"])
        return

    try:
        inc["status"] = "diagnosing"
        async with LLM_LOCK:
            t0 = time.time()
            diag = await diagnose(labels.get("alertname", "?"),
                                  alert.get("annotations", {}).get("summary", ""), ns, ctx)
            inc["llm_seconds"] = round(time.time() - t0, 1)
        inc["diagnosis"] = diag.model_dump()
        inc["timeline"]["diagnosed"] = time.time()
    except DiagnosisError as exc:
        inc["status"], inc["error"] = "diagnosis_failed", str(exc)
        log.error("incident %s : %s", incident_id, exc)
        return

    decision = decide(diag, ns, dep, DIRECT_NAMESPACES)
    inc["decision"] = decision.model_dump()
    inc["status"] = "escalated" if decision.route in ("escalate", "rejected") else "diagnosed"
    log.info("incident %s : %s -> %s (%s) en %ss", incident_id, diag.action, decision.route,
             decision.reason, inc["llm_seconds"])


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
