from datetime import datetime, timedelta

import pytest

from irrigation.app import create_app
from irrigation.climate import TempLog, validate_rule
from irrigation.state import StateStore

from conftest import run_for


def hist(s, event):
    return [h for h in s.store.history(200) if h["event"] == event]


RULE = {"zone_id": 1, "when": "above", "temp_c": 28, "minutes": 2, "cooldown_minutes": 60,
        "start": "05:00", "end": "08:00", "enabled": True}


# ---------- hourly history ----------
def test_hourly_average_low_high_and_saved_once_per_hour():
    store = StateStore(None)
    log = TempLog(store)
    t = datetime(2026, 10, 5, 10, 0)
    for i in range(60):
        log.record(t + timedelta(minutes=i), 20.0 + (i % 2))  # 20, 21, 20, 21...
    assert store.get("temp_history") == []  # nothing written mid-hour
    for i in range(31):
        log.record(t + timedelta(hours=1, minutes=i), 30.0)
    saved = store.get("temp_history")
    assert [h["t"] for h in saved] == ["2026-10-05T10:00"]
    assert saved[0]["avg"] == 20.5 and saved[0]["min"] == 20.0 and saved[0]["max"] == 21.0
    series = log.series(t + timedelta(hours=1, minutes=30))
    assert series[0]["t"] == "2026-10-02T00:00"  # today plus the 3 days before
    assert len(series) == 3 * 24 + 12
    by = {h["t"]: h for h in series}
    assert by["2026-10-05T10:00"]["avg"] == 20.5
    assert by["2026-10-05T11:00"]["avg"] == 30.0  # hour in progress is shown too
    assert by["2026-10-05T09:00"]["avg"] is None  # no data = gap, not zero


def test_restart_resumes_the_current_hour():
    store = StateStore(None)
    log = TempLog(store)
    t = datetime(2026, 10, 5, 14, 0)
    for i in range(10):
        log.record(t + timedelta(minutes=i), 24.0)
    log.flush()
    log2 = TempLog(store)
    for i in range(10, 20):
        log2.record(t + timedelta(minutes=i), 26.0)
    h = {x["t"]: x for x in log2.series(t + timedelta(minutes=20))}["2026-10-05T14:00"]
    assert h["avg"] == 25.0 and h["min"] == 24.0 and h["max"] == 26.0


def test_history_is_trimmed_to_a_week():
    store = StateStore(None)
    log = TempLog(store)
    t = datetime(2026, 10, 1, 0, 0)
    for i in range(10 * 24 + 1):
        log.record(t + timedelta(hours=i), 20.0)
    assert len(store.get("temp_history")) <= 7 * 24 + 1


def test_smoothed_needs_a_few_minutes_and_fresh_data():
    log = TempLog(StateStore(None))
    t = datetime(2026, 10, 5, 12, 0)
    log.record(t, 30.0)
    assert log.smoothed(t) is None
    for s in range(3, 200, 3):
        log.record(t + timedelta(seconds=s), 30.0)
    assert log.smoothed(t + timedelta(seconds=198)) == 30.0
    assert log.smoothed(t + timedelta(seconds=400)) is None  # sensor stopped reporting


# ---------- rules ----------
def test_hot_rule_runs_then_waits_for_cooldown(make_system, clock):
    s = make_system(schedules=[])
    s.device.temp_c = 31.0
    s.store.add_rule(dict(RULE))
    run_for(s, clock, 150)
    assert s.controller.current is None  # waiting for a steady 10-minute average
    run_for(s, clock, 60)
    assert s.controller.current and s.controller.current.reason == "Temperature 31.0 °C"
    run_for(s, clock, 30 * 60)
    assert len(hist(s, "ran")) == 1
    run_for(s, clock, 35 * 60)
    assert len(hist(s, "ran")) == 2
    assert s.dashboard_state()["temp_rules"][0]["last_triggered"]


def test_rule_outside_its_hours_or_below_threshold_does_nothing(make_system, clock):
    s = make_system(schedules=[])
    s.device.temp_c = 31.0
    s.store.add_rule(dict(RULE, start="09:00", end="17:00"))  # clock is at 05:00
    s.store.add_rule(dict(RULE, temp_c=35))
    run_for(s, clock, 600)
    assert s.controller.current is None and not hist(s, "ran")


def test_cold_rule(make_system, clock):
    s = make_system(schedules=[])
    s.device.temp_c = 3.0
    s.store.add_rule(dict(RULE, when="below", temp_c=5))
    run_for(s, clock, 240)
    assert s.controller.current is not None


def test_moist_soil_skips_once_per_cooldown(make_system, clock):
    s = make_system(schedules=[])
    s.device.temp_c = 31.0
    s.device.soil = [1300.0, 1300.0, 1300.0]  # bed is soaked
    s.store.add_rule(dict(RULE, zone_id=2))
    run_for(s, clock, 20 * 60)
    skips = hist(s, "skipped")
    assert len(skips) == 1 and "moist" in skips[0]["reason"] and "31.0 °C" in skips[0]["reason"]
    assert s.controller.current is None


def test_no_reading_means_no_rule(make_system, clock):
    s = make_system(schedules=[])
    s.device.temp_c = None
    s.device._air_temp = lambda: None  # DS18B20 missing
    s.store.add_rule(dict(RULE))
    run_for(s, clock, 600)
    assert s.controller.current is None


def test_validate_rule():
    zones = {1, 2}
    ok = validate_rule(dict(RULE), zones, 30)
    assert ok["temp_c"] == 28 and ok["when"] == "above"
    for bad, msg in [({"zone_id": 9}, "Unknown zone"), ({"when": "near"}, "above or below"),
                     ({"temp_c": 99}, "between"), ({"minutes": 0}, "Minutes"),
                     ({"cooldown_minutes": 7}, "how often"), ({"end": "04:00"}, "after"),
                     ({"minutes": 30, "cooldown_minutes": 30}, "longer than the run")]:
        with pytest.raises(ValueError, match=msg):
            validate_rule(dict(RULE, **bad), zones, 30)


# ---------- API ----------
@pytest.fixture
def client(make_system):
    system = make_system(schedules=[])
    app = create_app(system)
    app.testing = True
    c = app.test_client()
    c.system = system
    return c


def test_rule_crud_and_temperature_endpoint(client, clock):
    assert client.post("/api/temp-rules", json=dict(RULE, temp_c="hot")).status_code == 400
    r = client.post("/api/temp-rules", json=RULE).get_json()
    rid = r["rule"]["id"]
    upd = client.put(f"/api/temp-rules/{rid}", json=dict(RULE, temp_c=32, enabled=False)).get_json()
    assert upd["rule"]["temp_c"] == 32 and upd["rule"]["enabled"] is False
    assert client.get("/api/status").get_json()["temp_rules"][0]["temp_c"] == 32
    assert client.put("/api/temp-rules/99", json=RULE).status_code == 404
    assert client.delete(f"/api/temp-rules/{rid}").status_code == 200
    assert client.delete(f"/api/temp-rules/{rid}").status_code == 404
    client.system.device.temp_c = 22.5
    run_for(client.system, clock, 240)
    t = client.get("/api/temperature").get_json()
    assert t["current"] == 22.5 and t["average_10min"] == 22.5
    assert t["hours"][-1]["t"] == "2026-10-05T05:00" and t["hours"][-1]["avg"] == 22.5
    assert client.get("/api/status").get_json()["air_temp_c"] == 22.5
