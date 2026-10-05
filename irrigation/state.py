"""Small JSON-file store for schedules, history and settings changed in the UI."""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_STATE = {
    "schedules": [],
    "last_runs": {},      # schedule id -> last trigger key
    "history": [],        # newest first
    "paused_until": None, # ISO timestamp
    "next_id": 1,
    "temp_rules": [],     # temperature-triggered watering (see climate.py)
    "temp_rule_last": {}, # rule id -> when it last fired
    "next_rule_id": 1,
    "temp_history": [],   # hourly air temperature, oldest first
}

DEFAULT_SCHEDULES = [
    # Hydroponics: 15 minutes at the top of every hour, 06:00-18:00
    {"zone_id": 1, "kind": "cycle", "days": [0, 1, 2, 3, 4, 5, 6], "start": "06:00",
     "end": "18:00", "every_minutes": 60, "minutes": 15, "enabled": True},
    # Beds & pots: 10 minutes at 06:30 daily (skipped if every bed is already moist)
    {"zone_id": 2, "kind": "daily", "days": [0, 1, 2, 3, 4, 5, 6], "start": "06:30",
     "minutes": 10, "enabled": True},
]


class StateStore:
    def __init__(self, path: str | None, history_limit: int = 200):
        self.path = Path(path) if path else None
        self.history_limit = history_limit
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULT_STATE)
        self._load()

    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                self.data.update(loaded)
                return
            except Exception as exc:
                log.error("State file unreadable (%s); starting fresh", exc)
                self.path.rename(self.path.with_suffix(".corrupt"))
        for s in DEFAULT_SCHEDULES:
            self.add_schedule(dict(s))

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state")
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)

    # schedules
    def add_schedule(self, sched: dict) -> dict:
        with self._lock:
            sched["id"] = self.data["next_id"]
            self.data["next_id"] += 1
            self.data["schedules"].append(sched)
            self.save()
            return sched

    def update_schedule(self, sid: int, sched: dict) -> dict | None:
        with self._lock:
            for i, s in enumerate(self.data["schedules"]):
                if s["id"] == sid:
                    sched["id"] = sid
                    self.data["schedules"][i] = sched
                    self.save()
                    return sched
            return None

    def delete_schedule(self, sid: int) -> bool:
        with self._lock:
            before = len(self.data["schedules"])
            self.data["schedules"] = [s for s in self.data["schedules"] if s["id"] != sid]
            self.data["last_runs"].pop(str(sid), None)
            self.save()
            return len(self.data["schedules"]) < before

    def schedules(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self.data["schedules"])

    # temperature rules
    def add_rule(self, rule: dict) -> dict:
        with self._lock:
            rule["id"] = self.data.get("next_rule_id", 1)
            self.data["next_rule_id"] = rule["id"] + 1
            self.data.setdefault("temp_rules", []).append(rule)
            self.save()
            return rule

    def update_rule(self, rid: int, rule: dict) -> dict | None:
        with self._lock:
            for i, r in enumerate(self.data.get("temp_rules", [])):
                if r["id"] == rid:
                    rule["id"] = rid
                    self.data["temp_rules"][i] = rule
                    self.save()
                    return rule
            return None

    def delete_rule(self, rid: int) -> bool:
        with self._lock:
            rules = self.data.get("temp_rules", [])
            self.data["temp_rules"] = [r for r in rules if r["id"] != rid]
            self.data.get("temp_rule_last", {}).pop(str(rid), None)
            self.save()
            return len(self.data["temp_rules"]) < len(rules)

    def rules(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self.data.get("temp_rules", []))

    # history
    def log(self, entry: dict) -> None:
        with self._lock:
            self.data["history"].insert(0, entry)
            del self.data["history"][self.history_limit:]
            self.save()

    def history(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self.data["history"][:limit])

    def get(self, key, default=None):
        with self._lock:
            return copy.deepcopy(self.data.get(key, default))

    def set(self, key, value) -> None:
        with self._lock:
            self.data[key] = value
            self.save()
