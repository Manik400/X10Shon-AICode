from __future__ import annotations

from app.security.extension_id import repo_extension_dist, unpacked_extension_id

from .conftest import DASH_HEADERS


def test_unpacked_id_is_deterministic_and_well_formed() -> None:
    a = unpacked_extension_id(repo_extension_dist())
    assert len(a) == 32 and set(a) <= set("abcdefghijklmnop")
    assert a == unpacked_extension_id(str(repo_extension_dist()))
    assert a != unpacked_extension_id(repo_extension_dist().parent)


def test_autopair_only_for_the_path_derived_extension_id(client) -> None:
    expected = unpacked_extension_id(repo_extension_dist())
    good = {"X-LFA-Client": "extension", "Origin": f"chrome-extension://{expected}"}
    bad = {"X-LFA-Client": "extension", "Origin": "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
    assert client.post("/api/pairing/claim", json={"code": "AUTOPAIR"}, headers=bad).status_code == 403
    assert client.post("/api/pairing/claim", json={"code": "AUTOPAIR"}, headers={"X-LFA-Client": "dashboard"}).status_code == 403
    r = client.post("/api/pairing/claim", json={"code": "AUTOPAIR"}, headers=good)
    assert r.status_code == 200
    token = r.json()["token"]
    assert client.get("/api/health", headers={"Authorization": f"Bearer {token}", "Origin": good["Origin"]}).status_code == 200
    # A second auto-claim (lost token, reinstalled extension) replaces the earlier pairing: the old
    # token stops working, the new one works, and only one pairing stays active.
    r2 = client.post("/api/pairing/claim", json={"code": "AUTOPAIR"}, headers=good)
    assert r2.status_code == 200
    assert client.get("/api/health", headers={"Authorization": f"Bearer {token}", "Origin": good["Origin"]}).status_code == 401
    assert client.get("/api/health", headers={"Authorization": f"Bearer {r2.json()['token']}", "Origin": good["Origin"]}).status_code == 200
    listing = client.get("/api/pairing", headers=DASH_HEADERS).json()
    assert listing["unpacked"]["expected_extension_id"] == expected
    active = [p for p in listing["items"] if not p["revoked_at"]]
    assert len(active) == 1 and active[0]["extension_id"] == expected
