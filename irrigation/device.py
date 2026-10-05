"""Talking to the ESP32 that does the real switching and sensing.

The ESP32 firmware (firmware/irrigation_esp32) exposes a tiny HTTP API:

    GET  /api/status                 -> JSON sensor + valve state
    POST /api/valve?valve=A&seconds=N -> open valve A for N seconds (0 closes it)
    POST /api/off                    -> close every valve
    POST /api/refill?action=reset|start|stop -> tank refill control

The firmware closes a valve on its own when its timer expires, so a crashed Pi
or dropped WiFi can never leave water running.

SimulatedDevice has the same interface and a toy model of a garden, used for
tests and for trying the dashboard without hardware.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
import threading
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)

VALVES = ("A", "B", "C", "D")


class DeviceError(RuntimeError):
    pass


def _normalise(data: dict) -> dict:
    valves = {}
    for v in VALVES:
        info = (data.get("valves") or {}).get(v, {})
        valves[v] = {"on": bool(info.get("on", False)), "remaining_s": int(info.get("remaining_s", 0))}
    temp = data.get("temp_c")
    if temp is not None and (temp <= -100 or temp >= 125):
        temp = None  # -127 means "DS18B20 not detected"
    return {
        "online": True,
        "soil_raw": [int(x) for x in data.get("soil_raw", [0, 0, 0])][:3],
        "temp_c": temp,
        "float_low": int(data.get("float_low", 1)),
        "float_high": int(data.get("float_high", 1)),
        "flow_pulses": int(data.get("flow_pulses", 0)),
        "valves": valves,
        "uptime_s": int(data.get("uptime_s", 0)),
        "fw": str(data.get("fw", "?")),
        "rssi": data.get("rssi"),
        "pump": {"enabled": bool((data.get("pump") or {}).get("enabled", False)),
                 "on": bool((data.get("pump") or {}).get("on", False))},
        "refill": {
            "enabled": bool((data.get("refill") or {}).get("enabled", False)),
            "running": bool((data.get("refill") or {}).get("running", False)),
            "running_s": int((data.get("refill") or {}).get("running_s", 0)),
            "fault": bool((data.get("refill") or {}).get("fault", False)),
            "fault_reason": str((data.get("refill") or {}).get("fault_reason", "")),
        },
    }


class Esp32Client:
    def __init__(self, url: str, api_key: str = "", timeout: float = 5.0, session=None):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.headers = {"X-Api-Key": api_key} if api_key else {}

    def _call(self, method: str, path: str, **params) -> dict:
        try:
            r = self.session.request(method, self.url + path, params=params or None,
                                     headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            raise DeviceError(f"ESP32 not reachable at {self.url}: {exc}") from exc

    def status(self) -> dict:
        return _normalise(self._call("GET", "/api/status"))

    def set_valve(self, valve: str, seconds: int) -> None:
        self._call("POST", "/api/valve", valve=valve, seconds=int(seconds))

    def all_off(self) -> None:
        self._call("POST", "/api/off")

    def refill(self, action: str) -> None:
        self._call("POST", "/api/refill", action=action)


class SimulatedDevice:
    """A fake ESP32 + garden. Soil dries slowly; watering wets the sensors
    belonging to that valve; open valves produce flow pulses."""

    def __init__(self, valve_sensors: dict[str, list[int]] | None = None,
                 pulses_per_litre: float = 450, litres_per_min: float = 3.0,
                 clock: Callable[[], float] = time.monotonic,
                 flow_valves: tuple[str, ...] = ("A",)):
        self.clock = clock
        self.flow_valves = flow_valves  # valves whose water passes the flow sensor
        self.valve_sensors = valve_sensors or {"A": [1, 2], "B": [3]}
        self.pps = pulses_per_litre * litres_per_min / 60.0
        self.soil = [2300.0, 2400.0, 2200.0]
        self.temp_c: float | None = None  # None = follow a daily curve; tests can set a value
        # 250 L tank: LOW float at 40%, HIGH float at 85%. Floats read 0 when submerged.
        self.tank_pct = 70.0
        self.force_low: int | None = None   # tests can force float readings
        self.force_high: int | None = None
        self.rain_tank_empty = False
        self.refill_enabled, self.pump_enabled = True, True
        self.refilling, self.refill_fault, self.refill_reason = False, False, ""
        self._refill_started = 0.0
        self.refill_max_s, self.refill_lpm = 1200, 30.0
        self.bed_valve, self.bed_lpm = "B", 12.0
        self.refill_topup, self.topup_delay = True, 120
        self._high_dry_since: float | None = None
        self.leak_pps = 0.0  # set >0 to simulate a leak
        self.offline = False
        self._pulses = 0.0
        self._until = {v: 0.0 for v in VALVES}
        self._last = clock()
        self._start = self._last
        self._lock = threading.Lock()

    def _advance(self) -> None:
        now = self.clock()
        dt = max(0.0, now - self._last)
        if dt == 0:
            return
        self._last = now
        for v in VALVES:
            t_on = max(0.0, min(dt, self._until[v] - (now - dt)))
            if t_on > 0:
                if v in self.flow_valves:
                    self._pulses += self.pps * t_on
                for sid in self.valve_sensors.get(v, []):
                    self.soil[sid - 1] = max(1300.0, self.soil[sid - 1] - 6.0 * t_on)
        self._pulses += self.leak_pps * dt
        # bed watering leaves the system; hydro water returns to the tank
        t_bed = max(0.0, min(dt, self._until.get(self.bed_valve, 0) - (now - dt)))
        self.tank_pct -= self.bed_lpm * t_bed / 60 / 250 * 100
        if self.refilling and not self.rain_tank_empty:
            self.tank_pct += self.refill_lpm * dt / 60 / 250 * 100
        self.tank_pct = max(0.0, min(100.0, self.tank_pct))
        # refill logic mirrors the firmware
        if self.refill_enabled:
            if self.refilling:
                if self._float(85) == 0:
                    self.refilling = False
                elif now - self._refill_started > self.refill_max_s:
                    self.refilling, self.refill_fault = False, True
                    self.refill_reason = "Tank didn't reach HIGH in time"
            elif not self.refill_fault:
                if self._float(85) == 1:
                    self._high_dry_since = self._high_dry_since or now
                else:
                    self._high_dry_since = None
                top_up = (self.refill_topup and self._high_dry_since is not None
                          and now - self._high_dry_since > self.topup_delay)
                if self._float(40) == 1 or top_up:
                    self.refilling, self._refill_started = True, now
        for i in range(3):
            self.soil[i] = min(2900.0, self.soil[i] + 0.002 * dt)

    def _air_temp(self) -> float:
        if self.temp_c is not None:
            return self.temp_c
        now = datetime.now()  # a warm afternoon, cool night: 12 °C at 03:00, 28 °C at 15:00
        h = now.hour + now.minute / 60
        return round(20 + 8 * math.sin((h - 9) / 24 * 2 * math.pi), 2)

    def _float(self, level: float) -> int:
        forced = self.force_high if level > 50 else self.force_low
        if forced is not None:
            return forced
        return 0 if self.tank_pct >= level else 1

    def status(self) -> dict:
        with self._lock:
            if self.offline:
                raise DeviceError("simulated ESP32 offline")
            self._advance()
            now = self.clock()
            valves = {v: {"on": self._until[v] > now,
                          "remaining_s": max(0, int(round(self._until[v] - now)))} for v in VALVES}
            return _normalise({
                "soil_raw": [round(x) for x in self.soil], "temp_c": self._air_temp(),
                "float_low": self._float(40), "float_high": self._float(85),
                "flow_pulses": int(self._pulses), "valves": valves,
                "uptime_s": int(now - self._start), "fw": "simulated", "rssi": -58,
                "pump": {"enabled": self.pump_enabled,
                         "on": self.pump_enabled and self._float(40) == 0
                         and any(v["on"] for v in valves.values())},
                "refill": {"enabled": self.refill_enabled, "running": self.refilling,
                           "running_s": int(now - self._refill_started) if self.refilling else 0,
                           "fault": self.refill_fault, "fault_reason": self.refill_reason},
            })

    def set_valve(self, valve: str, seconds: int) -> None:
        with self._lock:
            if self.offline:
                raise DeviceError("simulated ESP32 offline")
            self._advance()
            self._until[valve] = self.clock() + max(0, min(int(seconds), 3600))

    def all_off(self) -> None:
        with self._lock:
            if self.offline:
                raise DeviceError("simulated ESP32 offline")
            self._advance()
            for v in VALVES:
                self._until[v] = 0.0


    def refill(self, action: str) -> None:
        with self._lock:
            if self.offline:
                raise DeviceError("simulated ESP32 offline")
            self._advance()
            if action == "reset":
                self.refill_fault, self.refill_reason = False, ""
            elif action == "stop":
                self.refilling = False
            elif action == "start" and not self.refill_fault:
                self.refilling, self._refill_started = True, self.clock()


def build_device(cfg: dict, clock=time.monotonic):
    c = cfg["controller"]
    if c["mode"] == "simulated":
        mapping = {z["valve"]: z.get("sensors", []) for z in cfg["zones"]}
        flow = tuple(z["valve"] for z in cfg["zones"] if z.get("monitor_flow"))
        bed = [z["valve"] for z in cfg["zones"] if z.get("sensors")]
        log.info("Using simulated ESP32")
        dev = SimulatedDevice(mapping, cfg["flow"]["pulses_per_litre"], clock=clock, flow_valves=flow)
        if bed:
            dev.bed_valve = bed[0]
        return dev
    log.info("Using ESP32 at %s", c["url"])
    return Esp32Client(c["url"], c.get("api_key", ""), c["timeout_seconds"])
