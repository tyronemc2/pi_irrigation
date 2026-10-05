"""Decides when schedules are due and whether to skip them."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)
CATCH_UP_MINUTES = 5  # a run still starts if the Pi was busy/restarting for a few minutes


def parse_hhmm(value: str) -> tuple[int, int]:
    h, m = str(value).split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError
    return h, m


def validate_schedule(data: dict, zone_ids, max_minutes: float) -> dict:
    """Clean user input from the dashboard. Raises ValueError with a readable message."""
    try:
        zone_id = int(data["zone_id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Choose a zone")
    if zone_id not in zone_ids:
        raise ValueError("Unknown zone")
    kind = data.get("kind", "daily")
    if kind not in ("daily", "cycle"):
        raise ValueError("Schedule type must be daily or cycle")
    days = sorted({int(d) for d in data.get("days", [])})
    if not days or any(d < 0 or d > 6 for d in days):
        raise ValueError("Pick at least one day")
    try:
        parse_hhmm(data["start"])
    except Exception:
        raise ValueError("Start time must look like 06:30")
    try:
        minutes = float(data["minutes"])
    except Exception:
        raise ValueError("Minutes must be a number")
    if not 0 < minutes <= max_minutes:
        raise ValueError(f"Minutes must be between 1 and {max_minutes:g}")
    out = {"zone_id": zone_id, "kind": kind, "days": days, "start": data["start"],
           "minutes": minutes, "enabled": bool(data.get("enabled", True))}
    if kind == "cycle":
        try:
            parse_hhmm(data["end"])
        except Exception:
            raise ValueError("End time must look like 18:00")
        if data["end"] <= data["start"]:
            raise ValueError("End time must be after the start time")
        try:
            every = int(data["every_minutes"])
        except Exception:
            raise ValueError("Repeat interval must be a whole number of minutes")
        if every < 5:
            raise ValueError("Repeat at most every 5 minutes")
        if minutes >= every:
            raise ValueError("Run time must be shorter than the repeat interval")
        out.update(end=data["end"], every_minutes=every)
    return out


def due_slot(sched: dict, now: datetime) -> str | None:
    """Return a unique key for the slot that is due now, or None."""
    if not sched.get("enabled", True) or now.weekday() not in sched["days"]:
        return None
    h, m = parse_hhmm(sched["start"])
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if sched["kind"] == "daily":
        slot = start
    else:
        eh, em = parse_hhmm(sched["end"])
        end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        if not start <= now < end + timedelta(minutes=CATCH_UP_MINUTES):
            return None
        every = sched["every_minutes"]
        n = int((now - start).total_seconds() // 60 // every)
        slot = start + timedelta(minutes=n * every)
        if slot >= end:
            return None
    if timedelta(0) <= now - slot < timedelta(minutes=CATCH_UP_MINUTES):
        return slot.strftime("%Y-%m-%dT%H:%M")
    return None


def next_run(sched: dict, now: datetime) -> str | None:
    """Human-friendly next start time (for the dashboard)."""
    if not sched.get("enabled", True):
        return None
    probe = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(8 * 24 * 60 // 5):
        if probe.weekday() in sched["days"]:
            h, m = parse_hhmm(sched["start"])
            start = probe.replace(hour=h, minute=m)
            if sched["kind"] == "daily":
                if start >= probe:
                    return start.isoformat(timespec="minutes")
            else:
                eh, em = parse_hhmm(sched["end"])
                end = probe.replace(hour=eh, minute=em)
                t = start
                while t < end:
                    if t >= probe:
                        return t.isoformat(timespec="minutes")
                    t += timedelta(minutes=sched["every_minutes"])
        probe = (probe + timedelta(days=1)).replace(hour=0, minute=0)
    return None


class Scheduler:
    def __init__(self, cfg, controller, store, forecast, notifier):
        self.cfg, self.controller, self.store = cfg, controller, store
        self.forecast, self.notifier = forecast, notifier

    def paused(self, now: datetime) -> bool:
        until = self.store.get("paused_until")
        return bool(until and datetime.fromisoformat(until) > now)

    def skip_reason(self, zone: dict, now: datetime) -> str | None:
        if self.paused(now):
            return "Watering is paused"
        if self.controller.tank_blocks_watering():
            return "Tank is low"
        if zone.get("skip_if_wet") and self.cfg["moisture"]["enabled"]:
            d = self.controller.moisture_detail(zone)
            if d and d["percent"] >= self.cfg["moisture"]["skip_above_percent"]:
                if d["driest"]:
                    return f"Every bed is already moist (driest: {d['driest']} {d['percent']:g}%)"
                return f"Soil is already moist ({d['percent']:g}%)"
        if zone.get("skip_if_rain") and self.cfg["weather"]["enabled"]:
            f = self.forecast.check()
            if f["rain_expected"]:
                return f["summary"]
            if not f["available"] and not self.cfg["weather"]["water_if_unavailable"]:
                return "Forecast unavailable"
        return None

    def check(self, now: datetime) -> None:
        last = self.store.get("last_runs", {})
        changed = False
        for sched in self.store.schedules():
            key = due_slot(sched, now)
            if not key or last.get(str(sched["id"])) == key:
                continue
            last[str(sched["id"])] = key
            changed = True
            zone = self.controller.zones.get(sched["zone_id"])
            if not zone:
                continue
            label = "Cycle" if sched["kind"] == "cycle" else "Schedule"
            reason = self.skip_reason(zone, now)
            notify = sched["kind"] == "daily"  # cycles only notify on problems
            if reason:
                self.controller._event(zone, "skipped", reason)
                if notify:
                    self.notifier.send(f"{zone['name']} skipped", reason + ".", tags="fast_forward")
                continue
            ok, msg = self.controller.request_run(zone["id"], sched["minutes"], label, notify)
            if not ok:
                self.controller._event(zone, "skipped", msg)
                self.notifier.send(f"{zone['name']} didn't run", msg + ".", priority="high",
                                   tags="warning")
        if changed:
            self.store.set("last_runs", last)
