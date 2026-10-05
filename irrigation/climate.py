"""Greenhouse air temperature: an hourly history and temperature-triggered watering.

The DS18B20 hangs in the greenhouse air. Every poll (a few seconds) its reading is
added to the current hour's running average. When the hour ends, that hour's
average, low and high are saved, so the state file is written about once an hour.

Temperature rules ("water the bed for 5 minutes when it's above 30 °C, at most
every 2 hours, between 09:00 and 17:00") use a 10-minute average, so a brief
spike, e.g. sun on the shield for a moment, doesn't start a run.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timedelta

from .scheduler import parse_hhmm

log = logging.getLogger(__name__)

KEEP_DAYS = 7              # hourly history kept in the state file
SHOW_DAYS = 4              # shown on the dashboard: today + the 3 days before
SMOOTH_MINUTES = 10        # rules act on this rolling average
MIN_SMOOTH_SECONDS = 180   # ...once it covers at least this much time
STALE_SECONDS = 60         # no reading for this long = sensor not reporting
COOLDOWN_CHOICES = (30, 60, 120, 180, 240, 360, 720, 1440)


def hour_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:00")


class TempLog:
    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._hours: dict[str, dict] = {h["t"]: h for h in (store.get("temp_history") or [])}
        self._cur: list | None = None  # [hour key, sum, count, low, high]
        self._recent: deque[tuple[datetime, float]] = deque()

    def record(self, now: datetime, temp: float | None) -> None:
        with self._lock:
            key = hour_key(now)
            if self._cur and self._cur[0] != key:
                self._close(now)
            if temp is not None:
                if not self._cur:
                    prev = self._hours.get(key)  # resume an hour saved before a restart
                    self._cur = ([key, prev["avg"] * prev["n"], prev["n"], prev["min"], prev["max"]]
                                 if prev else [key, 0.0, 0, temp, temp])
                c = self._cur
                c[1] += temp
                c[2] += 1
                c[3], c[4] = min(c[3], temp), max(c[4], temp)
                self._recent.append((now, temp))
            cutoff = now - timedelta(minutes=SMOOTH_MINUTES)
            while self._recent and self._recent[0][0] < cutoff:
                self._recent.popleft()

    def _entry(self) -> dict | None:
        key, total, n, lo, hi = self._cur
        if not n:
            return None
        return {"t": key, "avg": round(total / n, 2), "min": round(lo, 1), "max": round(hi, 1), "n": n}

    def _close(self, now: datetime) -> None:
        entry = self._entry()
        self._cur = None
        if entry:
            self._hours[entry["t"]] = entry
        oldest = hour_key(now - timedelta(days=KEEP_DAYS))
        self._hours = {k: v for k, v in self._hours.items() if k >= oldest}
        self.store.set("temp_history", [self._hours[k] for k in sorted(self._hours)])

    def flush(self) -> None:
        """Save the hour in progress too (called on shutdown)."""
        with self._lock:
            if self._cur and (entry := self._entry()):
                self._hours[entry["t"]] = entry
                self.store.set("temp_history", [self._hours[k] for k in sorted(self._hours)])

    def smoothed(self, now: datetime) -> float | None:
        """10-minute average, or None while there isn't enough fresh data."""
        with self._lock:
            if not self._recent:
                return None
            if (now - self._recent[-1][0]).total_seconds() > STALE_SECONDS:
                return None
            if (now - self._recent[0][0]).total_seconds() < MIN_SMOOTH_SECONDS:
                return None
            return round(sum(t for _, t in self._recent) / len(self._recent), 1)

    def series(self, now: datetime, days: int = SHOW_DAYS) -> list[dict]:
        """One entry per hour from midnight `days - 1` days ago up to the current hour."""
        with self._lock:
            hours = dict(self._hours)
            if self._cur and (entry := self._entry()):
                hours[entry["t"]] = entry
        start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        out, t = [], start
        while t <= now:
            h = hours.get(hour_key(t))
            out.append({"t": hour_key(t),
                        "avg": round(h["avg"], 1) if h else None,
                        "min": h["min"] if h else None,
                        "max": h["max"] if h else None})
            t += timedelta(hours=1)
        return out


def validate_rule(data: dict, zone_ids, max_minutes: float) -> dict:
    """Clean a temperature rule from the dashboard. Raises ValueError with a readable message."""
    try:
        zone_id = int(data["zone_id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Choose a zone")
    if zone_id not in zone_ids:
        raise ValueError("Unknown zone")
    when = data.get("when", "above")
    if when not in ("above", "below"):
        raise ValueError("Choose above or below")
    try:
        temp_c = float(data["temp_c"])
    except Exception:
        raise ValueError("Temperature must be a number")
    if not -10 <= temp_c <= 60:
        raise ValueError("Temperature must be between -10 and 60 °C")
    try:
        minutes = float(data["minutes"])
    except Exception:
        raise ValueError("Minutes must be a number")
    if not 0 < minutes <= max_minutes:
        raise ValueError(f"Minutes must be between 1 and {max_minutes:g}")
    try:
        cooldown = int(data.get("cooldown_minutes", 120))
    except (TypeError, ValueError):
        raise ValueError("Choose how often it may run")
    if cooldown not in COOLDOWN_CHOICES:
        raise ValueError("Choose how often it may run")
    if cooldown <= minutes:
        raise ValueError("The wait between runs must be longer than the run")
    for key, example in (("start", "09:00"), ("end", "17:00")):
        try:
            parse_hhmm(data[key])
        except Exception:
            raise ValueError(f"Times must look like {example}")
    if data["end"] <= data["start"]:
        raise ValueError("'Until' must be after 'From'")
    return {"zone_id": zone_id, "when": when, "temp_c": round(temp_c, 1), "minutes": minutes,
            "cooldown_minutes": cooldown, "start": data["start"], "end": data["end"],
            "enabled": bool(data.get("enabled", True))}


class Climate:
    def __init__(self, controller, store, scheduler):
        self.controller, self.store, self.scheduler = controller, store, scheduler
        self.log = TempLog(store)

    def air_temp(self) -> float | None:
        st = self.controller.status or {}
        return st.get("temp_c") if st.get("online") else None

    def step(self, now: datetime) -> None:
        self.log.record(now, self.air_temp())
        self.check(now)

    def check(self, now: datetime) -> None:
        temp = self.log.smoothed(now)
        if temp is None:
            return
        last = self.store.get("temp_rule_last", {})
        changed = False
        clock = now.strftime("%H:%M")
        for rule in self.store.rules():
            if not rule.get("enabled", True) or not rule["start"] <= clock < rule["end"]:
                continue
            hit = temp >= rule["temp_c"] if rule["when"] == "above" else temp <= rule["temp_c"]
            prev = last.get(str(rule["id"]))
            if not hit or (prev and now - datetime.fromisoformat(prev)
                           < timedelta(minutes=rule["cooldown_minutes"])):
                continue
            last[str(rule["id"])] = now.isoformat(timespec="seconds")
            changed = True
            zone = self.controller.zones.get(rule["zone_id"])
            if not zone:
                continue
            label = f"Temperature {temp:.1f} °C"
            reason = self.scheduler.skip_reason(zone, now)
            if reason:
                self.controller._event(zone, "skipped", f"{reason} (air {temp:.1f} °C)")
                continue
            log.info("Temperature rule %s: %s at %.1f °C", rule["id"], zone["name"], temp)
            ok, msg = self.controller.request_run(zone["id"], rule["minutes"], label, notify=True)
            if not ok:
                self.controller._event(zone, "skipped", f"{msg} (air {temp:.1f} °C)")
        if changed:
            self.store.set("temp_rule_last", last)

    def rules_view(self) -> list[dict]:
        last = self.store.get("temp_rule_last", {})
        return [dict(r, last_triggered=last.get(str(r["id"]))) for r in self.store.rules()]
