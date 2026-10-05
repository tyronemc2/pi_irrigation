"""Wires everything together and runs the background loop."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from .climate import Climate
from .controller import Controller
from .device import DeviceError, build_device
from .notifier import Notifier
from .scheduler import Scheduler, next_run
from .sensors import soil_readings
from .state import StateStore
from .weather import RainForecast

log = logging.getLogger(__name__)


class IrrigationSystem:
    def __init__(self, cfg, device=None, store=None, notifier=None, forecast=None,
                 clock=time.monotonic, now=datetime.now):
        self.cfg, self.now = cfg, now
        self.device = device or build_device(cfg, clock=clock)
        self.store = store or StateStore(cfg["state_file"])
        self.notifier = notifier or Notifier(cfg)
        self.forecast = forecast or RainForecast(cfg, clock=clock)
        self.controller = Controller(cfg, self.device, self.store, self.notifier, clock, now)
        self.scheduler = Scheduler(cfg, self.controller, self.store, self.forecast, self.notifier)
        self.climate = Climate(self.controller, self.store, self.scheduler)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def startup(self) -> None:
        try:
            self.device.all_off()  # start from a known-safe state
        except DeviceError as exc:
            log.warning("Couldn't reach ESP32 at startup: %s", exc)
        self.controller.poll()

    def step(self) -> None:
        self.controller.tick()
        now = self.now()
        self.scheduler.check(now)
        self.climate.step(now)

    def _loop(self) -> None:
        interval = self.cfg["controller"]["poll_seconds"]
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:  # never let one bad tick kill the loop
                log.exception("Loop error")
            self._stop.wait(interval)

    def start(self) -> None:
        self.startup()
        self._thread = threading.Thread(target=self._loop, name="irrigation-loop", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.climate.log.flush()
        try:
            self.device.all_off()
        except DeviceError:
            pass

    def dashboard_state(self) -> dict:
        now = self.now()
        st = self.controller.status or {}
        readings = soil_readings(self.cfg, st)
        snap = self.controller.snapshot()
        zones = []
        for z in self.cfg["zones"]:
            v = st.get("valves", {}).get(z["valve"], {}) if st.get("online") else {}
            scheds = [s for s in self.store.schedules() if s["zone_id"] == z["id"]]
            nexts = sorted(filter(None, (next_run(s, now) for s in scheds)))
            zones.append({
                "id": z["id"], "name": z["name"], "valve": z["valve"],
                "on": bool(v.get("on")), "moisture": self.controller.moisture(z),
                "sensors": [readings[s] for s in z["sensors"]],
                "skip_if_wet": z["skip_if_wet"], "skip_if_rain": z["skip_if_rain"],
                "next_run": nexts[0] if nexts else None,
            })
        paused = self.store.get("paused_until")
        return {
            "now": now.isoformat(timespec="seconds"),
            "controller": {"online": snap["online"], "mode": self.cfg["controller"]["mode"],
                           "fw": st.get("fw"), "uptime_s": st.get("uptime_s"),
                           "error": st.get("error")},
            "zones": zones,
            "running": snap["running"], "queue": snap["queue"],
            "air_temp_c": self.climate.air_temp(),
            "temp_rules": self.climate.rules_view(),
            "pump": st.get("pump"), "refill": st.get("refill"), "rssi": st.get("rssi"),
            "flow_lpm": snap["flow_lpm"],
            "tank": self.controller.tank(),
            "soil": list(readings.values()),
            "weather": self.forecast.check() if self.cfg["weather"]["enabled"] else None,
            "paused_until": paused if paused and datetime.fromisoformat(paused) > now else None,
            "schedules": self.store.schedules(),
            "history": self.store.history(40),
            "limits": {"max_run_minutes": self.cfg["safety"]["max_run_minutes"],
                       "moist_threshold": self.cfg["moisture"]["skip_above_percent"]},
        }
