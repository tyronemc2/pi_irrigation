import base64

import pytest

from irrigation.app import create_app


@pytest.fixture
def client(make_system):
    system = make_system()
    app = create_app(system)
    app.testing = True
    c = app.test_client()
    c.system = system
    return c


def test_dashboard_and_status(client):
    r = client.get("/")
    assert r.status_code == 200 and b'rel="manifest"' in r.data and b"apple-touch-icon" in r.data
    d = client.get("/api/status").get_json()
    assert d["controller"]["online"] and len(d["zones"]) == 2
    assert d["tank"]["label"] == "OK" and d["refill"]["enabled"]
    assert d["zones"][1]["moisture"] is not None and d["zones"][0]["next_run"]


def test_run_and_stop(client):
    r = client.post("/api/zones/1/run", json={"minutes": 5})
    assert r.status_code == 200 and r.get_json()["ok"]
    client.system.step()
    assert client.get("/api/status").get_json()["running"]["zone"] == "Hydroponics"
    assert client.post("/api/zones/1/run", json={"minutes": 5}).status_code == 409
    assert client.post("/api/stop").get_json()["ok"]
    assert client.get("/api/status").get_json()["running"] is None


def test_schedule_crud(client):
    bad = client.post("/api/schedules", json={"zone_id": 1, "days": []})
    assert bad.status_code == 400 and "day" in bad.get_json()["message"]
    new = client.post("/api/schedules", json={"zone_id": 2, "kind": "daily", "days": [0, 2, 4], "start": "18:00", "minutes": 8})
    sid = new.get_json()["schedule"]["id"]
    upd = client.put(f"/api/schedules/{sid}", json={"zone_id": 2, "kind": "daily", "days": [1], "start": "19:00", "minutes": 4, "enabled": False})
    assert upd.get_json()["schedule"]["enabled"] is False
    assert client.put("/api/schedules/999", json={"zone_id": 2, "days": [1], "start": "19:00", "minutes": 4}).status_code == 404
    assert client.delete(f"/api/schedules/{sid}").status_code == 200
    assert client.delete(f"/api/schedules/{sid}").status_code == 404


def test_pause_resume(client):
    assert client.post("/api/pause", json={"days": 99}).status_code == 400
    assert client.post("/api/pause", json={"days": 3}).get_json()["message"] == "Schedules paused for 3 days"
    assert client.get("/api/status").get_json()["paused_until"]
    client.post("/api/resume")
    assert client.get("/api/status").get_json()["paused_until"] is None


def test_refill_and_notify_endpoints(client):
    assert client.post("/api/refill", json={"action": "reset"}).status_code == 200
    assert client.post("/api/refill", json={"action": "nope"}).status_code == 409
    assert client.post("/api/notify-test").status_code == 409  # not configured


def test_pwa_files(client):
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.mimetype == "application/manifest+json"
    j = m.get_json(force=True)
    assert j["display"] == "standalone" and any(i["purpose"] == "maskable" for i in j["icons"])
    for icon in j["icons"]:
        assert client.get(icon["src"]).status_code == 200
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.mimetype and sw.headers["Cache-Control"] == "no-cache"
    assert client.get("/static/icons/apple-touch-icon.png").status_code == 200


def test_password(make_system, cfg):
    cfg["web"]["password"] = "s3cret"
    app = create_app(make_system(cfg=cfg))
    c = app.test_client()
    assert c.get("/api/status").status_code == 401
    assert c.get("/").status_code == 401
    good = {"Authorization": "Basic " + base64.b64encode(b"admin:s3cret").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
    assert c.get("/api/status", headers=good).status_code == 200
    assert c.get("/api/status", headers=bad).status_code == 401
    # install files stay public so phones can fetch them
    assert c.get("/manifest.webmanifest").status_code == 200 and c.get("/sw.js").status_code == 200
    assert c.get("/healthz").status_code == 200
