"""Application de démo : métriques Prometheus + mode panne activable à chaud."""
import os
import random
import time

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Gauge, Histogram,
                               generate_latest)

app = FastAPI(title="demo-app")

REQUESTS = Counter("demo_http_requests_total", "Requêtes HTTP", ["path", "status"])
LATENCY = Histogram("demo_http_request_seconds", "Latence", ["path"])
FAULT_MODE = Gauge("demo_fault_mode", "1 si un mode panne est actif", ["mode"])

# Modes : none | errors | latency | memleak
state = {"mode": os.getenv("FAULT_MODE", "none"), "leak": []}
VERSION = os.getenv("APP_VERSION", "dev")


def _set_mode(mode: str) -> None:
    state["mode"] = mode
    if mode != "memleak":
        state["leak"].clear()
    for m in ("errors", "latency", "memleak"):
        FAULT_MODE.labels(mode=m).set(1 if m == mode else 0)


_set_mode(state["mode"])


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": VERSION}


@app.get("/")
def index():
    start = time.perf_counter()
    mode = state["mode"]
    try:
        if mode == "errors" and random.random() < 0.7:
            REQUESTS.labels("/", "500").inc()
            raise HTTPException(status_code=500, detail="panne simulée")
        if mode == "latency":
            time.sleep(random.uniform(1.5, 3.0))
        if mode == "memleak":
            state["leak"].append(bytearray(5 * 1024 * 1024))  # +5 Mo par requête
        REQUESTS.labels("/", "200").inc()
        return {"message": "hello", "version": VERSION, "mode": mode}
    finally:
        LATENCY.labels("/").observe(time.perf_counter() - start)


@app.post("/fault/{mode}")
def set_fault(mode: str):
    if mode not in ("none", "errors", "latency", "memleak"):
        raise HTTPException(status_code=400, detail="mode inconnu")
    _set_mode(mode)
    return {"mode": mode}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
