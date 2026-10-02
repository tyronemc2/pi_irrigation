import json

import pytest

from irrigation.device import DeviceError, Esp32Client
from irrigation.notifier import Notifier
from irrigation.state import StateStore
from irrigation.weather import RainForecast


class Resp:
    def __init__(self, data=None, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Session:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.exc:
            raise self.exc
        return self.resp

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


ESP = {"fw": "1.0.0", "uptime_s": 50, "rssi": -61, "soil_raw": [2000, 2100, 2200], "temp_c": -127.0,
       "float_low": 0, "float_high": 1, "flow_pulses": 900,
       "valves": {"A": {"on": True, "remaining_s": 30}, "B": {"on": False, "remaining_s": 0}},
       "pump": {"enabled": True, "on": True},
       "refill": {"enabled": True, "running": False, "running_s": 0, "fault": False, "fault_reason": ""}}


def test_esp32_status_parsing():
    sess = Session(Resp(ESP))
    c = Esp32Client("http://esp/", "k", session=sess)
    st = c.status()
    assert st["online"] and st["valves"]["A"]["on"] and st["valves"]["C"] == {"on": False, "remaining_s": 0}
    assert st["temp_c"] is None  # -127 = sensor missing
    assert st["pump"]["on"] and st["refill"]["enabled"]
    method, url, kw = sess.calls[0]
    assert url == "http://esp/api/status" and kw["headers"] == {"X-Api-Key": "k"}


def test_esp32_commands():
    sess = Session(Resp({}))
    c = Esp32Client("http://esp", session=sess)
    c.set_valve("B", 600)
    c.all_off()
    c.refill("reset")
    assert sess.calls[0][1].endswith("/api/valve") and sess.calls[0][2]["params"] == {"valve": "B", "seconds": 600}
    assert sess.calls[1][1].endswith("/api/off")
    assert sess.calls[2][2]["params"] == {"action": "reset"}
    assert sess.calls[0][2]["headers"] == {}


def test_esp32_errors():
    with pytest.raises(DeviceError):
        Esp32Client("http://esp", session=Session(exc=ConnectionError("down"))).status()
    with pytest.raises(DeviceError):
        Esp32Client("http://esp", session=Session(Resp({}, 401))).status()


def test_forecast(cfg, clock):
    data = {"hourly": {"precipitation_probability": [10, 70, 20], "precipitation": [0, 1.0, 0.2]}}
    sess = Session(Resp(data))
    f = RainForecast(cfg, session=sess, clock=clock)
    r = f.check()
    assert r["rain_expected"] and r["max_probability"] == 70 and r["total_mm"] == 1.2
    f.check()
    assert len(sess.calls) == 1  # cached
    params = sess.calls[0][2]["params"]
    assert params["latitude"] == -34.35 and params["forecast_hours"] == 12


def test_forecast_dry_and_failure(cfg, clock):
    dry = RainForecast(cfg, session=Session(Resp({"hourly": {"precipitation_probability": [5], "precipitation": [0]}})), clock=clock)
    assert not dry.check()["rain_expected"]
    bad = RainForecast(cfg, session=Session(exc=TimeoutError()), clock=clock)
    r = bad.check()
    assert not r["available"] and not r["rain_expected"]
    cfg["weather"]["enabled"] = False
    assert RainForecast(cfg, session=Session(exc=AssertionError("should not call"))).check()["summary"] == "Forecast off"


def test_notifier(cfg):
    sess = Session(Resp({}))
    off = Notifier(cfg, session=sess, background=False)
    off.send("t", "m")
    assert sess.calls == [] and off.sent[-1]["title"] == "t"
    cfg["notifications"].update(enabled=True, ntfy_topic="garden-x")
    on = Notifier(cfg, session=sess, background=False)
    on.send("Bed watered", "10 min", priority="high", tags="droplet")
    method, url, kw = sess.calls[0]
    assert url == "https://ntfy.sh/garden-x" and kw["headers"]["Priority"] == "high"
    Notifier(cfg, session=Session(exc=ConnectionError()), background=False).send("x", "y")  # swallowed


def test_state_persists(tmp_path):
    p = tmp_path / "s.json"
    a = StateStore(str(p))
    assert len(a.schedules()) == 2  # default hydro cycle + bed daily
    a.log({"event": "ran"})
    a.set("paused_until", "2030-01-01T00:00:00")
    b = StateStore(str(p))
    assert b.history()[0]["event"] == "ran" and b.get("paused_until") == "2030-01-01T00:00:00"
    sid = b.schedules()[0]["id"]
    assert b.delete_schedule(sid) and not b.delete_schedule(sid)
    assert len(StateStore(str(p)).schedules()) == 1


def test_state_corrupt_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    s = StateStore(str(p))
    assert len(s.schedules()) == 2 and (tmp_path / "s.corrupt").exists()
    json.loads(p.read_text())
