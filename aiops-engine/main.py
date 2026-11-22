"""Moteur AIOps v4 : alerte -> contexte -> diagnostic LLM -> décision -> restart direct ou Pull Request -> vérification."""
import asyncio
import json
import logging
import os
import time
import uuid

from fastapi import BackgroundTasks, FastAPI

import actions
from allowlist import decide
from collector import alert_firing, collect_context
from llm import DiagnosisError, diagnose

log = logging.getLogger("aiops")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="aiops-engine", version="0.5")
INCIDENTS: dict[str, dict] = {}  # en mémoire, suffisant pour la démo
WATCHED_NAMESPACES = {"dev", "prod"}
DIRECT_NAMESPACES = {n for n in os.getenv("DIRECT_ACTION_NAMESPACES", "dev").split(",") if n}
AUTO_REMEDIATION = os.getenv("AUTO_REMEDIATION", "false").lower() == "true"  # interrupteur : actions directes
AUTO_PULL_REQUEST = os.getenv("AUTO_PULL_REQUEST", "false").lower() == "true"  # interrupteur : création de PR
PR_LIMIT_PER_HOUR = int(os.getenv("PR_LIMIT_PER_HOUR", "3"))
COOLDOWN_S = int(os.getenv("COOLDOWN_S", "600"))  # une action directe par cible et par période
VERIFY_TIMEOUT_S = float(os.getenv("VERIFY_TIMEOUT_S", "420"))
VERIFY_INTERVAL_S = float(os.getenv("VERIFY_INTERVAL_S", "15"))
CLOSED = {"resolved", "collect_failed", "diagnosis_failed"}
LLM_LOCK = asyncio.Semaphore(1)  # un seul diagnostic à la fois : le LLM est lent
LAST_ACTION: dict[tuple[str, str], float] = {}
PR_TIMES: list[float] = []


def _open_incident(key: str):
    return next((i for i in INCIDENTS.values() if i["key"] == key and i["status"] not in CLOSED), None)


def _set(inc: dict, status: str) -> None:
    if inc["status"] != "resolved":  # une alerte déjà résolue ne repasse jamais en cours
        inc["status"] = status


async def verify(incident_id: str) -> None:
    inc = INCIDENTS[incident_id]
    labels = inc["alert"]["labels"]
    _set(inc, "verifying")
    deadline = time.time() + VERIFY_TIMEOUT_S
    while time.time() < deadline:
        if inc["status"] == "resolved":  # déjà fermé par Alertmanager
            return
        firing = await asyncio.to_thread(alert_firing, labels.get("alertname", ""), labels["namespace"])
        if firing is False:
            inc["status"], inc["verified_by"] = "resolved", "prometheus"
            inc["timeline"]["resolved"] = time.time()
            log.info("incident %s : résolu et vérifié", incident_id)
            return
        await asyncio.sleep(VERIFY_INTERVAL_S)
    _set(inc, "unresolved")
    log.warning("incident %s : alerte toujours active après vérification", incident_id)


async def open_pr(incident_id: str, diag, ns: str, dep: str) -> None:
    inc = INCIDENTS[incident_id]
    if not AUTO_PULL_REQUEST:
        _set(inc, "diagnosed")  # simulation : interrupteur coupé
        return
    PR_TIMES[:] = [t for t in PR_TIMES if time.time() - t < 3600]
    if len(PR_TIMES) >= PR_LIMIT_PER_HOUR:
        inc["decision"] = {"route": "escalate",
                           "reason": f"limite de {PR_LIMIT_PER_HOUR} pull requests par heure atteinte"}
        _set(inc, "escalated")
        return
    try:
        _set(inc, "opening_pr")
        PR_TIMES.append(time.time())
        inc["pr_url"] = await asyncio.to_thread(
            actions.open_pull_request, diag, ns, dep, incident_id, inc["alert"]["labels"].get("alertname", "?"))
        inc["timeline"]["acted"] = time.time()
        _set(inc, "pending_review")  # un humain valide ; l'incident se clôt quand l'alerte disparaît
        log.info("incident %s : pull request ouverte : %s", incident_id, inc["pr_url"])
    except Exception as exc:
        inc["error"] = f"{exc.__class__.__name__}: {exc}"
        _set(inc, "pr_failed")
        log.error("incident %s : pull request échouée : %s", incident_id, inc["error"])


async def process(incident_id: str) -> None:
    inc = INCIDENTS[incident_id]
    alert = inc["alert"]
    labels = alert["labels"]
    ns, dep = labels["namespace"], labels.get("app", "demo-app")

    try:
        _set(inc, "collecting")
        ctx = await asyncio.to_thread(collect_context, ns, dep)
        inc["context"] = ctx
        inc["context_chars"] = len(json.dumps(ctx, ensure_ascii=False))
        inc["timeline"]["collected"] = time.time()
    except Exception as exc:
        inc["status"], inc["error"] = "collect_failed", f"{exc.__class__.__name__}: {exc}"
        log.error("incident %s : collecte échouée : %s", incident_id, inc["error"])
        return

    try:
        _set(inc, "diagnosing")
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
    log.info("incident %s : %s -> %s (%s) en %ss", incident_id, diag.action, decision.route,
             decision.reason, inc["llm_seconds"])

    if decision.route in ("escalate", "rejected"):
        _set(inc, "escalated")
        return
    if decision.route == "pull_request":
        await open_pr(incident_id, diag, ns, dep)
        return
    if not AUTO_REMEDIATION:
        _set(inc, "diagnosed")  # simulation : interrupteur coupé
        return

    since = time.time() - LAST_ACTION.get((ns, dep), 0)
    if since < COOLDOWN_S:
        inc["decision"] = {"route": "escalate",
                           "reason": f"délai de sécurité : dernière action il y a {int(since)}s (< {COOLDOWN_S}s)"}
        _set(inc, "escalated")
        return

    try:
        _set(inc, "remediating")
        LAST_ACTION[(ns, dep)] = time.time()
        inc["result"] = await asyncio.to_thread(actions.run_direct, diag, ns, dep, DIRECT_NAMESPACES)
        inc["timeline"]["acted"] = time.time()
        log.info("incident %s : action exécutée : %s", incident_id, inc["result"])
    except Exception as exc:
        inc["error"] = f"{exc.__class__.__name__}: {exc}"
        _set(inc, "action_failed")
        log.error("incident %s : action échouée : %s", incident_id, inc["error"])
        return
    await verify(incident_id)


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
                existing.setdefault("verified_by", "alertmanager")
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
