from __future__ import annotations

from .conftest import DASH_HEADERS, EXT_ORIGIN


def test_requires_client_header(client) -> None:
    assert client.get("/api/health").status_code == 401


def test_dashboard_same_origin_ok(client) -> None:
    r = client.get("/api/health", headers=DASH_HEADERS)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_foreign_origin_rejected(client) -> None:
    r = client.get("/api/health", headers={**DASH_HEADERS, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_cors_preflight_from_foreign_origin_has_no_allow_origin(client) -> None:
    r = client.options("/api/health", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-lfa-client"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_cors_preflight_from_dashboard_dev_origin_allowed(client) -> None:
    r = client.options("/api/health", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-lfa-client"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_pairing_flow_and_extension_auth(client) -> None:
    code = client.post("/api/pairing/code", headers=DASH_HEADERS).json()["code"]
    assert len(code) == 8
    bad = client.post("/api/pairing/claim", json={"code": "WRONGXYZ"}, headers={"X-LFA-Client": "extension", "Origin": EXT_ORIGIN})
    assert bad.status_code == 400
    ok = client.post("/api/pairing/claim", json={"code": code}, headers={"X-LFA-Client": "extension", "Origin": EXT_ORIGIN})
    assert ok.status_code == 200
    token = ok.json()["token"]
    # Code is single use.
    assert client.post("/api/pairing/claim", json={"code": code}, headers={"X-LFA-Client": "extension", "Origin": EXT_ORIGIN}).status_code == 400
    # Token works from the paired extension origin only.
    assert client.get("/api/health", headers={"Authorization": f"Bearer {token}", "Origin": EXT_ORIGIN}).status_code == 200
    assert client.get("/api/health", headers={"Authorization": f"Bearer {token}", "Origin": "chrome-extension://zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"}).status_code == 403
    assert client.get("/api/health", headers={"Authorization": "Bearer nottoken", "Origin": EXT_ORIGIN}).status_code == 401
    # Extension cannot issue pairing codes.
    assert client.post("/api/pairing/code", headers={"Authorization": f"Bearer {token}", "Origin": EXT_ORIGIN}).status_code == 403
    # Revoke → token stops working.
    pid = client.get("/api/pairing", headers=DASH_HEADERS).json()["items"][0]["id"]
    assert client.delete(f"/api/pairing/{pid}", headers=DASH_HEADERS).json()["ok"] is True
    assert client.get("/api/health", headers={"Authorization": f"Bearer {token}", "Origin": EXT_ORIGIN}).status_code == 401


def test_pairing_claim_rate_limited(client) -> None:
    client.post("/api/pairing/code", headers=DASH_HEADERS)
    for _ in range(5):
        client.post("/api/pairing/claim", json={"code": "WRONGXYZ"}, headers={"X-LFA-Client": "extension", "Origin": EXT_ORIGIN})
    # Even the right code is refused once the failure budget is exhausted.
    code = client.post("/api/pairing/code", headers=DASH_HEADERS).json()["code"]
    assert client.post("/api/pairing/claim", json={"code": code}, headers={"X-LFA-Client": "extension", "Origin": EXT_ORIGIN}).status_code == 400


def test_websocket_requires_valid_token_and_origin(client) -> None:
    from starlette.websockets import WebSocketDisconnect

    code = client.post("/api/pairing/code", headers=DASH_HEADERS).json()["code"]
    token = client.post("/api/pairing/claim", json={"code": code}, headers={"X-LFA-Client": "extension", "Origin": EXT_ORIGIN}).json()["token"]
    try:
        with client.websocket_connect("/ws/extension?token=bad", headers={"Origin": EXT_ORIGIN}) as ws:
            ws.receive_json()
        raise AssertionError("bad token accepted")
    except WebSocketDisconnect as exc:
        assert exc.code == 4001
    with client.websocket_connect(f"/ws/extension?token={token}", headers={"Origin": EXT_ORIGIN}) as ws:
        assert ws.receive_json()["type"] == "welcome"
        assert ws.receive_json()["type"] == "status_update"
        assert client.get("/api/health", headers=DASH_HEADERS).json()["extension"]["connected"] is True
    assert client.get("/api/health", headers=DASH_HEADERS).json()["extension"]["connected"] is False


def test_request_size_limit(client) -> None:
    r = client.post("/api/tasks", content=b"x" * 10, headers={**DASH_HEADERS, "Content-Length": str(3 * 1024 * 1024), "Content-Type": "application/json"})
    assert r.status_code == 413


def test_data_delete_requires_confirmation(client) -> None:
    assert client.post("/api/data/delete", json={"confirm": "no"}, headers=DASH_HEADERS).status_code == 400
    assert client.post("/api/data/delete", json={"confirm": "DELETE"}, headers=DASH_HEADERS).status_code == 200
