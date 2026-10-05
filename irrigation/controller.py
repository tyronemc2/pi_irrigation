"""Runs zones: starts valves, watches flow and tank level, logs and notifies.

All timing goes through injectable clocks so behaviour can be tested quickly.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .device import DeviceError
from .sensors import soil_readings, tank_state, zone_moisture, zone_moisture_detail

log = logging.getLogger(__name__)
GRACE_SECONDS = 8  # time allowed for the ESP32 to report a valve as open


@dataclass
class Run:
    zone: dict
    minutes: float
    reason: str
    notify: bool
    started: float
    ends: float
    started_wall: datetime
    start_pulses: int | None
    flow_warned: bool = False


@dataclass
class Request:
    zone_id: int
    minutes: float
    reason: str
    notify: bool = True


class Controller:
    def __init__(self, cfg, device, store, notifier,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = datetime.now):
        self.cfg, self.device, self.store, self.notifier = cfg, device, store, notifier
        self.clock, self.now = clock, now
        self.zones = {z["id"]: z for z in cfg["zones"]}
        self.status: dict | None = None
        self.current: Run | None = None
        self.queue: list[Request] = []
        self.flow_lpm: float | None = None
        self._lock = threading.RLock()
        self._last_flow: tuple[float, int] | None = None
        self._offline_since: float | None = None
        self._offline_notified = False
        self._leak_since: float | None = None
        self._leak_notified = False
        self._refill_prev: dict | None = None

    # ---------- helpers ----------
    @property
    def online(self) -> bool:
        return bool(self.status and self.status.get("online"))

    def _litres(self, pulses: int) -> float:
        return pulses / self.cfg["flow"]["pulses_per_litre"]

    def tank(self) -> dict:
        return tank_state(self.cfg, self.status)

    def tank_blocks_watering(self) -> bool:
        t = self.tank()
        return bool(t["enabled"] and self.cfg["tank"]["lockout_when_low"] and t["low_ok"] is False)

    def moisture(self, zone: dict) -> float | None:
        return zone_moisture(self.cfg, zone, soil_readings(self.cfg, self.status))

    def moisture_detail(self, zone: dict) -> dict | None:
        return zone_moisture_detail(zone, soil_readings(self.cfg, self.status))

    def _event(self, zone: dict | None, event: str, reason: str, **extra) -> None:
        entry = {"time": self.now().isoformat(timespec="seconds"), "event": event,
                 "zone_id": zone["id"] if zone else None,
                 "zone": zone["name"] if zone else "System", "reason": reason}
        entry.update(extra)
        self.store.log(entry)

    # ---------- device polling ----------
    def poll(self) -> None:
        try:
            st = self.device.status()
        except DeviceError as exc:
            if self._offline_since is None:
                self._offline_since = self.clock()
                log.warning("%s", exc)
            self.status = {"online": False, "error": str(exc)}
            self.flow_lpm, self._last_flow = None, None
            if not self._offline_notified and self.clock() - self._offline_since >= 60:
                self._offline_notified = True
                self.notifier.send("Greenhouse controller offline",
                                   "The Pi can't reach the ESP32. Check its power and WiFi. "
                                   "Valves close on their own when their timers run out.",
                                   priority="high", tags="warning")
                self._event(None, "alert", "Controller offline")
            return
        if self._offline_notified:
            self.notifier.send("Greenhouse controller back online", "The ESP32 is responding again.")
            self._event(None, "info", "Controller back online")
        self._offline_since, self._offline_notified = None, False
        now, pulses = self.clock(), st["flow_pulses"]
        if self._last_flow and now > self._last_flow[0]:
            dp = pulses - self._last_flow[1]
            if dp >= 0:  # a reboot resets the counter; skip that sample
                self.flow_lpm = round(self._litres(dp) / ((now - self._last_flow[0]) / 60), 2)
        self._last_flow = (now, pulses)
        self.status = st
        self._watch_refill(st.get("refill") or {})

    def _watch_refill(self, r: dict) -> None:
        prev, self._refill_prev = self._refill_prev, r
        if not prev or not r.get("enabled"):
            return
        if r.get("fault") and not prev.get("fault"):
            msg = r.get("fault_reason") or "Refill stopped"
            self.notifier.send("Tank refill fault", msg + ". Check the 2,000 L tank and transfer "
                               "pump, then reset the refill on the dashboard.",
                               priority="high", tags="warning")
            self._event(None, "alert", "Tank refill fault", detail=msg)
        elif prev.get("running") and not r.get("running") and not r.get("fault"):
            self._event(None, "info", "Tank refilled", detail=f"{prev.get('running_s', 0) // 60} min")

    def refill(self, action: str) -> tuple[bool, str]:
        if action not in ("reset", "start", "stop"):
            return False, "Unknown refill action"
        try:
            self.device.refill(action)
        except DeviceError:
            return False, "The greenhouse controller is offline"
        self.poll()
        return True, {"reset": "Refill fault cleared", "start": "Tank refill started",
                      "stop": "Tank refill stopped"}[action]

    # ---------- commands ----------
    def request_run(self, zone_id: int, minutes: float, reason: str = "Manual",
                    notify: bool = True) -> tuple[bool, str]:
        with self._lock:
            zone = self.zones.get(zone_id)
            if not zone:
                return False, f"No zone {zone_id}"
            try:
                minutes = float(minutes)
            except (TypeError, ValueError):
                return False, "Minutes must be a number"
            if minutes <= 0:
                return False, "Minutes must be more than zero"
            cap = self.cfg["safety"]["max_run_minutes"]
            minutes = min(minutes, cap)
            if not self.online:
                return False, "The greenhouse controller is offline"
            if self.tank_blocks_watering():
                return False, "The tank is low, so watering is locked out"
            if self.current and self.current.zone["id"] == zone_id:
                return False, f"{zone['name']} is already running"
            if any(r.zone_id == zone_id for r in self.queue):
                return False, f"{zone['name']} is already waiting to run"
            if self.current and self.cfg["safety"]["one_zone_at_a_time"]:
                self.queue.append(Request(zone_id, minutes, reason, notify))
                return True, f"{zone['name']} will run after {self.current.zone['name']}"
            return self._start(Request(zone_id, minutes, reason, notify))

    def _start(self, req: Request) -> tuple[bool, str]:
        zone = self.zones[req.zone_id]
        try:
            self.device.set_valve(zone["valve"], int(round(req.minutes * 60)))
        except DeviceError as exc:
            self._event(zone, "error", req.reason, detail=str(exc))
            return False, "Couldn't reach the greenhouse controller"
        now = self.clock()
        metered = zone.get("monitor_flow") and self.cfg["flow"]["enabled"]
        pulses = self.status["flow_pulses"] if self.online and metered else None
        self.current = Run(zone, req.minutes, req.reason, req.notify, now,
                           now + req.minutes * 60, self.now(), pulses)
        log.info("Started %s for %.1f min (%s)", zone["name"], req.minutes, req.reason)
        return True, f"{zone['name']} started for {req.minutes:g} min"

    def stop(self, reason: str = "Stopped from dashboard") -> None:
        with self._lock:
            self.queue.clear()
            try:
                self.device.all_off()
            except DeviceError as exc:
                log.error("Stop failed: %s", exc)
            if self.current:
                self._finish("stopped", reason)

    def _finish(self, event: str, reason: str, priority: str = "default") -> None:
        run, self.current = self.current, None
        try:
            self.device.set_valve(run.zone["valve"], 0)
        except DeviceError:
            pass  # the ESP32's own timer closes it
        mins = round(min(run.minutes, (self.clock() - run.started) / 60), 1)
        litres = None
        if self.cfg["flow"]["enabled"] and run.start_pulses is not None and self.online:
            dp = self.status["flow_pulses"] - run.start_pulses
            litres = round(self._litres(dp), 1) if dp >= 0 else None
        self._event(run.zone, event, run.reason if event == "ran" else reason,
                    minutes=mins, litres=litres)
        if run.notify or event != "ran":
            amount = f", {litres} L" if litres is not None else ""
            title = {"ran": f"{run.zone['name']} watered",
                     "stopped": f"{run.zone['name']} stopped"}.get(event, f"{run.zone['name']}: {event}")
            body = f"{mins:g} min{amount}." + ("" if event == "ran" else f" {reason}.")
            self.notifier.send(title, body, priority=priority,
                               tags="droplet" if event == "ran" else "warning")

    # ---------- main loop step ----------
    def tick(self) -> None:
        with self._lock:
            self.poll()
            run = self.current
            if run:
                elapsed = self.clock() - run.started
                valve_state = (self.status or {}).get("valves", {}).get(run.zone["valve"], {})
                safety = self.cfg["safety"]
                if self.online and self.tank_blocks_watering():
                    self.device_safe_off(run)
                    self._finish("stopped", "Tank level dropped below the low float", priority="high")
                elif (self.online and run.zone.get("monitor_flow") and self.cfg["flow"]["enabled"]
                      and run.start_pulses is not None
                      and elapsed >= safety["no_flow_timeout_seconds"]
                      and elapsed < safety["no_flow_timeout_seconds"] + 30 and not run.flow_warned):
                    run.flow_warned = True
                    used = self._litres(self.status["flow_pulses"] - run.start_pulses)
                    if used / (elapsed / 60) < safety["no_flow_min_litres_per_min"]:
                        self._finish("stopped", "No water flowed. Check the pump, tank and filter",
                                     priority="high")
                if self.current is run:
                    if self.clock() >= run.ends:
                        self._finish("ran", run.reason)
                    elif self.online and elapsed > GRACE_SECONDS and not valve_state.get("on", False):
                        self._finish("stopped", "The controller closed the valve early (restart?)")
            if not self.current and self.queue:
                req = self.queue.pop(0)
                if self.tank_blocks_watering():
                    self._event(self.zones[req.zone_id], "skipped", "Tank low")
                else:
                    self._start(req)
            self._check_leak()

    def device_safe_off(self, run: Run) -> None:
        try:
            self.device.set_valve(run.zone["valve"], 0)
        except DeviceError:
            pass

    def _check_leak(self) -> None:
        if not (self.online and self.cfg["flow"]["enabled"]) or self.flow_lpm is None:
            return
        any_open = self.current is not None or any(v["on"] for v in self.status["valves"].values())
        safety = self.cfg["safety"]
        if any_open or self.flow_lpm < safety["leak_min_litres_per_min"]:
            self._leak_since, self._leak_notified = None, False
            return
        if self._leak_since is None:
            self._leak_since = self.clock()
        elif not self._leak_notified and self.clock() - self._leak_since >= safety["leak_alert_seconds"]:
            self._leak_notified = True
            self.notifier.send("Possible leak", f"Water is flowing ({self.flow_lpm} L/min) "
                               "while every valve is closed.", priority="high", tags="warning")
            self._event(None, "alert", "Water flowing with all valves closed",
                        detail=f"{self.flow_lpm} L/min")

    def snapshot(self) -> dict:
        with self._lock:
            run = self.current
            return {
                "online": self.online,
                "running": None if not run else {
                    "zone_id": run.zone["id"], "zone": run.zone["name"], "reason": run.reason,
                    "minutes": run.minutes,
                    "remaining_s": max(0, int(run.ends - self.clock())),
                    "litres": (round(self._litres(self.status["flow_pulses"] - run.start_pulses), 1)
                               if self.online and run.start_pulses is not None else None),
                },
                "queue": [{"zone_id": r.zone_id, "zone": self.zones[r.zone_id]["name"],
                           "minutes": r.minutes} for r in self.queue],
                "flow_lpm": self.flow_lpm,
            }
