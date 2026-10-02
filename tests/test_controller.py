from conftest import run_for


def hist(system, event=None):
    return [h for h in system.store.history(100) if event is None or h["event"] == event]


def test_manual_run_completes_and_logs_litres(make_system, clock):
    s = make_system(schedules=[])
    ok, msg = s.controller.request_run(1, 2)
    assert ok, msg
    s.step()
    assert s.device.status()["valves"]["A"]["on"]
    run_for(s, clock, 125)
    assert s.controller.current is None
    assert not s.device.status()["valves"]["A"]["on"]
    ran = hist(s, "ran")[0]
    assert ran["zone"] == "Hydroponics" and ran["minutes"] == 2
    assert 5.5 <= ran["litres"] <= 6.5  # 3 L/min for 2 min
    assert any("watered" in m["title"] for m in s.notifier.sent)


def test_run_is_capped(make_system):
    s = make_system(schedules=[])
    ok, _ = s.controller.request_run(1, 999)
    assert ok and s.controller.current.minutes == s.cfg["safety"]["max_run_minutes"]


def test_bad_requests(make_system):
    s = make_system(schedules=[])
    assert s.controller.request_run(9, 5) == (False, "No zone 9")
    assert not s.controller.request_run(1, 0)[0]
    assert not s.controller.request_run(1, "x")[0]
    s.controller.request_run(1, 5)
    assert "already running" in s.controller.request_run(1, 5)[1]


def test_one_zone_at_a_time_queue(make_system, clock):
    s = make_system(schedules=[])
    s.controller.request_run(1, 1)
    ok, msg = s.controller.request_run(2, 1)
    assert ok and "after" in msg
    assert "already waiting" in s.controller.request_run(2, 1)[1]
    s.step()
    st = s.device.status()["valves"]
    assert st["A"]["on"] and not st["B"]["on"]
    run_for(s, clock, 63)
    st = s.device.status()["valves"]
    assert not st["A"]["on"] and st["B"]["on"]
    run_for(s, clock, 63)
    assert [h["zone"] for h in hist(s, "ran")] == ["Veggie bed", "Hydroponics"]


def test_stop_clears_everything(make_system, clock):
    s = make_system(schedules=[])
    s.controller.request_run(1, 10)
    s.controller.request_run(2, 10)
    run_for(s, clock, 30)
    s.controller.stop()
    assert s.controller.current is None and s.controller.queue == []
    assert not any(v["on"] for v in s.device.status()["valves"].values())
    assert hist(s, "stopped")[0]["reason"] == "Stopped from dashboard"


def test_tank_low_blocks_start(make_system):
    s = make_system(schedules=[])
    s.device.force_low = 1
    s.device.refill_enabled = False
    s.controller.poll()
    ok, msg = s.controller.request_run(2, 5)
    assert not ok and "tank is low" in msg.lower()


def test_tank_dropping_stops_run(make_system, clock):
    s = make_system(schedules=[])
    s.controller.request_run(2, 10)
    run_for(s, clock, 30)
    s.device.force_low = 1
    run_for(s, clock, 6)
    assert s.controller.current is None
    assert "low float" in hist(s, "stopped")[0]["reason"]
    assert not s.device.status()["valves"]["B"]["on"]


def test_no_flow_stops_hydro(make_system, clock):
    s = make_system(schedules=[])
    s.device.pps = 0  # pump not delivering
    s.controller.request_run(1, 10)
    run_for(s, clock, 60)
    assert s.controller.current is None
    assert "No water flowed" in hist(s, "stopped")[0]["reason"]
    assert any(m["title"] == "Hydroponics stopped" for m in s.notifier.sent)


def test_bed_has_no_flow_check(make_system, clock):
    s = make_system(schedules=[])  # flow sensor is on the hydro branch only
    s.controller.request_run(2, 2)
    run_for(s, clock, 126)
    ran = hist(s, "ran")[0]
    assert ran["zone"] == "Veggie bed" and ran["litres"] is None  # not metered, so no false "0 L"


def test_leak_alert(make_system, clock):
    s = make_system(schedules=[])
    s.device.leak_pps = 10  # ~1.3 L/min with every valve closed
    run_for(s, clock, 150)
    assert any(m["title"] == "Possible leak" for m in s.notifier.sent)
    assert sum(m["title"] == "Possible leak" for m in s.notifier.sent) == 1


def test_offline_alert_and_recovery(make_system, clock):
    s = make_system(schedules=[])
    s.device.offline = True
    run_for(s, clock, 66)
    assert not s.controller.online
    assert any("offline" in m["title"] for m in s.notifier.sent)
    assert not s.controller.request_run(1, 5)[0]
    s.device.offline = False
    run_for(s, clock, 3)
    assert s.controller.online
    assert any("back online" in m["title"] for m in s.notifier.sent)


def test_esp32_closing_early_is_detected(make_system, clock):
    s = make_system(schedules=[])
    s.controller.request_run(1, 10)
    run_for(s, clock, 30)
    s.device._until["A"] = 0  # ESP32 rebooted
    run_for(s, clock, 3)
    assert s.controller.current is None
    assert "closed the valve early" in hist(s, "stopped")[0]["reason"]


def test_refill_fault_notifies_and_resets(make_system, clock):
    s = make_system(schedules=[])
    s.device.rain_tank_empty = True
    s.device.refill_max_s = 60
    s.device.tank_pct = 30  # below LOW -> refill starts
    run_for(s, clock, 90)
    assert any(m["title"] == "Tank refill fault" for m in s.notifier.sent)
    ok, msg = s.controller.refill("reset")
    assert ok and not s.controller.status["refill"]["fault"]
    assert not s.controller.refill("explode")[0]


def test_refill_completes(make_system, clock):
    s = make_system(schedules=[])
    s.device.tank_pct = 35
    run_for(s, clock, 600)
    assert s.device.tank_pct >= 85
    assert any(h["reason"] == "Tank refilled" for h in hist(s, "info"))
