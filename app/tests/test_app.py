from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_healthz():
    assert client.get("/healthz").json()["status"] == "ok"


def test_index_ok():
    client.post("/fault/none")
    assert client.get("/").status_code == 200


def test_metrics_exposed():
    client.get("/")
    body = client.get("/metrics").text
    assert "demo_http_requests_total" in body


def test_invalid_fault_mode():
    assert client.post("/fault/nimporte").status_code == 400


def test_errors_mode_produces_500s():
    client.post("/fault/errors")
    codes = {client.get("/").status_code for _ in range(40)}
    client.post("/fault/none")
    assert 500 in codes
