from datetime import datetime

import pytest
from conftest import FakeForecast, run_for

from irrigation.scheduler import due_slot, next_run, validate_schedule

DAILY = {"id": 1, "zone_id": 2, "kind": "daily", "days": [0, 1, 2, 3, 4, 5, 6], "start": "06:30", "minutes": 10, "enabled": True}
CYCLE = {"id": 2, "zone_id": 1, "kind": "cycle", "days": [0, 1, 2, 3, 4, 5, 6], "start": "06:00", "end": "18:00",
         "every_minutes": 60, "minutes": 15, "enabled": True}
MON = datetime(2026, 10, 5)


def at(h, m, day=MON):
    return day.replace(hour=h, minute=m)


def test_daily_window():
    assert due_slot(DAILY, at(6, 29)) is None
    assert due_slot(DAILY, at(6, 30)) == "2026-10-05T06:30"
    assert due_slot(DAILY, at(6, 34)) == "2026-10-05T06:30"  # catch-up window
    assert due_slot(DAILY, at(6, 35)) is None
    assert due_slot(dict(DAILY, days=[1]), at(6, 30)) is None   # Tuesday only
    assert due_slot(dict(DAILY, enabled=False), at(6, 30)) is None


def test_cycle_slots():
    assert due_slot(CYCLE, at(5, 59)) is None
    assert due_slot(CYCLE, at(6, 0)) == "2026-10-05T06:00"
    assert due_slot(CYCLE, at(9, 2)) == "2026-10-05T09:00"
    assert due_slot(CYCLE, at(9, 30)) is None
    assert due_slot(CYCLE, at(17, 0)) == "2026-10-05T17:00"
    assert due_slot(CYCLE, at(18, 0)) is None


def test_next_run():
    assert next_run(DAILY, at(7, 0)) == "2026-10-06T06:30"
    assert next_run(CYCLE, at(9, 10)) == "2026-10-05T10:00"
    assert next_run(CYCLE, at(18, 30)) == "2026-10-06T06:00"
    assert next_run(dict(DAILY, enabled=False), at(7, 0)) is None


@pytest.mark.parametrize("data,msg", [
    ({}, "Choose a zone"),
    ({"zone_id": 9}, "Unknown zone"),
    ({"zone_id": 1, "days": []}, "Pick at least one day"),
    ({"zone_id": 1, "days": [0], "start": "25:00"}, "Start time"),
    ({"zone_id": 1, "days": [0], "start": "06:00", "minutes": 0}, "between"),
    ({"zone_id": 1, "days": [0], "start": "06:00", "minutes": 99}, "between"),
    ({"zone_id": 1, "kind": "cycle", "days": [0], "start": "06:00", "end": "05:00", "minutes": 5, "every_minutes": 60}, "after the start"),
    ({"zone_id": 1, "kind": "cycle", "days": [0], "start": "06:00", "end": "18:00", "minutes": 5, "every_minutes": 2}, "every 5"),
    ({"zone_id": 1, "kind": "cycle", "days": [0], "start": "06:00", "end": "18:00", "minutes": 20, "every_minutes": 15}, "shorter"),
])
def test_validation(data, msg):
    with pytest.raises(ValueError, match=msg):
        validate_schedule(data, {1, 2}, 30)


def test_validation_ok():
    s = validate_schedule({"zone_id": "1", "kind": "cycle", "days": [6, 0, 0], "start": "06:00", "end": "18:00",
                           "minutes": "15", "every_minutes": "60"}, {1, 2}, 30)
    assert s["days"] == [0, 6] and s["minutes"] == 15.0 and s["every_minutes"] == 60


def test_daily_schedule_runs_once(make_system, clock):
    s = make_system(schedules=[dict(DAILY, start="05:01", minutes=1)])
    s.device.soil = [2700.0, 2700.0, 2700.0]  # dry
    run_for(s, clock, 60 * 6)
    ran = [h for h in s.store.history() if h["event"] == "ran"]
    assert len(ran) == 1 and ran[0]["reason"] == "Schedule"


def test_hydro_cycle_runs_each_hour_quietly(make_system, clock):
    s = make_system(schedules=[dict(CYCLE, start="05:00", end="08:00", minutes=2)])
    run_for(s, clock, 3 * 3600)
    ran = [h for h in s.store.history() if h["event"] == "ran"]
    assert len(ran) == 3 and all(h["zone"] == "Hydroponics" for h in ran)
    assert not any("watered" in m["title"] for m in s.notifier.sent)  # cycles don't spam


def test_skips_when_soil_moist(make_system, clock):
    s = make_system(schedules=[dict(DAILY, start="05:01")])
    s.device.soil = [1500.0, 1500.0, 1500.0]  # very wet
    run_for(s, clock, 90)
    skipped = [h for h in s.store.history() if h["event"] == "skipped"]
    assert skipped and "moist" in skipped[0]["reason"]
    assert any("skipped" in m["title"] for m in s.notifier.sent)


def test_skips_when_paused(make_system, clock):
    s = make_system(schedules=[dict(DAILY, start="05:01")])
    s.store.set("paused_until", "2026-10-06T00:00:00")
    run_for(s, clock, 90)
    assert s.store.history()[0]["reason"] == "Watering is paused"


def test_rain_skip_only_when_zone_opts_in(make_system, cfg, clock):
    cfg["zones"][1]["skip_if_rain"] = True
    s = make_system(cfg=cfg, schedules=[dict(DAILY, start="05:01")], forecast=FakeForecast(rain=True))
    s.device.soil = [2700.0] * 3
    run_for(s, clock, 90)
    assert s.store.history()[0]["reason"] == "Rain likely"


def test_rain_ignored_by_default(make_system, clock):
    # covered greenhouse: forecast rain must not stop watering
    s = make_system(schedules=[dict(DAILY, start="05:01", minutes=1)], forecast=FakeForecast(rain=True))
    s.device.soil = [2700.0] * 3
    run_for(s, clock, 150)
    assert any(h["event"] == "ran" for h in s.store.history())


def test_skips_when_tank_low(make_system, clock):
    s = make_system(schedules=[dict(DAILY, start="05:01")])
    s.device.force_low = 1
    s.device.refill_enabled = False
    s.device.soil = [2700.0] * 3
    run_for(s, clock, 90)
    assert s.store.history()[0]["reason"] == "Tank is low"


def test_two_day_soak_with_default_schedules(make_system, clock):
    s = make_system()  # default hydro cycle + bed daily, top-up refill
    s.device.soil = [2700.0] * 3  # bed starts dry
    s.device.tank_pct = 90
    run_for(s, clock, 2 * 24 * 3600, step=5)
    hist = s.store.history(200)
    hydro = [h for h in hist if h["zone"] == "Hydroponics" and h["event"] == "ran"]
    bed = [h for h in hist if h["zone"] == "Veggie bed" and h["event"] in ("ran", "skipped")]
    assert len(hydro) == 24          # 12 cycles a day, never doubled
    assert all(h["minutes"] == 15 for h in hydro)
    assert len(bed) == 2             # one decision per morning
    day1, day2 = bed[::-1]
    assert day1["event"] == "ran" and day1["minutes"] == 10  # full run: top-up kept the tank up
    assert day2["event"] == "skipped" and "moist" in day2["reason"]  # yesterday soaked it
    assert not [h for h in hist if h["event"] in ("stopped", "alert", "error")]
    assert s.controller.current is None and not s.controller.queue


def test_refill_only_at_low_cuts_bed_runs_short(make_system, clock):
    """Why top-up is the default: refilling only at LOW leaves the tank near LOW,
    so a bed watering soon trips the pump-protection cutoff."""
    s = make_system(schedules=[dict(DAILY, start="05:01", minutes=10)])
    s.device.refill_topup = False
    s.device.tank_pct = 50
    s.device.soil = [2700.0] * 3
    run_for(s, clock, 15 * 60)
    stopped = [h for h in s.store.history() if h["event"] == "stopped"]
    assert stopped and "low float" in stopped[0]["reason"]


def test_top_up_keeps_up_with_a_full_bed_run(make_system, clock):
    s = make_system(schedules=[])
    s.device.tank_pct = 86  # just full
    ok, _ = s.controller.request_run(2, 10)
    assert ok
    run_for(s, clock, 11 * 60)
    ran = [h for h in s.store.history() if h["event"] == "ran"]
    assert ran and ran[0]["minutes"] == 10
