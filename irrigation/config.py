"""Configuration loading with sensible defaults."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "location": {"latitude": -34.35, "longitude": 18.83, "name": "Pringle Bay"},
    "web": {"host": "0.0.0.0", "port": 8080, "password": ""},
    "controller": {
        # "simulated" runs a fake garden so you can try everything without hardware.
        "mode": "esp32",
        "url": "http://irrigation-esp32.local",
        "api_key": "",
        "timeout_seconds": 5,
        "poll_seconds": 3,
    },
    "safety": {
        "max_run_minutes": 30,
        "one_zone_at_a_time": True,
        # Stop a run if no water flows within this many seconds of opening a valve.
        "no_flow_timeout_seconds": 45,
        "no_flow_min_litres_per_min": 0.3,
        # Flow while all valves are closed for this long counts as a leak.
        "leak_alert_seconds": 120,
        "leak_min_litres_per_min": 0.3,
    },
    "flow": {"enabled": True, "pulses_per_litre": 450},
    "tank": {
        "enabled": True,
        # Float switches read 0 or 1. Set which value means "water is at this level"
        # after testing your floats in the tank (manual step 12).
        "float_wet_value": 0,
        # Don't start (and stop) watering when the LOW float says the tank is low.
        "lockout_when_low": True,
    },
    "zones": [
        {"id": 1, "name": "Hydroponics", "valve": "A", "sensors": [],
         "skip_if_wet": False, "skip_if_rain": False, "monitor_flow": True},
        {"id": 2, "name": "Veggie bed", "valve": "B", "sensors": [1, 2, 3],
         "skip_if_wet": True, "skip_if_rain": False, "monitor_flow": False},
    ],
    "soil_sensors": [
        {"id": 1, "name": "Soil 1", "dry_raw": 2900, "wet_raw": 1300},
        {"id": 2, "name": "Soil 2", "dry_raw": 2900, "wet_raw": 1300},
        {"id": 3, "name": "Soil 3", "dry_raw": 2900, "wet_raw": 1300},
    ],
    "moisture": {"enabled": True, "skip_above_percent": 60},
    "weather": {
        "enabled": True,
        "lookahead_hours": 12,
        "rain_probability_threshold": 60,
        "rain_mm_threshold": 2.0,
        "water_if_unavailable": True,
    },
    "notifications": {"enabled": False, "ntfy_server": "https://ntfy.sh", "ntfy_topic": ""},
    "state_file": "/var/lib/pi_irrigation/state.json",
}

VALID_VALVES = ("A", "B", "C", "D")


class ConfigError(ValueError):
    pass


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def validate(cfg: dict) -> dict:
    mode = cfg["controller"]["mode"]
    if mode not in ("esp32", "simulated"):
        raise ConfigError("controller.mode must be 'esp32' or 'simulated'")
    sensor_ids = set()
    for s in cfg["soil_sensors"]:
        if not 1 <= s["id"] <= 3:
            raise ConfigError(f"Soil sensor id must be 1, 2 or 3: {s}")
        if s["dry_raw"] == s["wet_raw"]:
            raise ConfigError(f"Soil sensor {s['id']}: dry_raw and wet_raw must differ")
        sensor_ids.add(s["id"])
    zones = cfg["zones"]
    if not zones:
        raise ConfigError("At least one zone must be configured.")
    ids, valves = set(), set()
    for z in zones:
        for key in ("id", "name", "valve"):
            if key not in z:
                raise ConfigError(f"Zone is missing '{key}': {z}")
        z["valve"] = str(z["valve"]).upper()
        if z["valve"] not in VALID_VALVES:
            raise ConfigError(f"Zone {z['id']}: valve must be one of {VALID_VALVES}")
        if z["id"] in ids:
            raise ConfigError(f"Duplicate zone id {z['id']}")
        if z["valve"] in valves:
            raise ConfigError(f"Valve {z['valve']} is used by more than one zone")
        ids.add(z["id"])
        valves.add(z["valve"])
        z.setdefault("sensors", [])
        z.setdefault("skip_if_wet", bool(z["sensors"]))
        z.setdefault("skip_if_rain", False)
        z.setdefault("monitor_flow", True)
        for sid in z["sensors"]:
            if sid not in sensor_ids:
                raise ConfigError(f"Zone {z['id']} uses unknown soil sensor {sid}")
    if cfg["safety"]["max_run_minutes"] <= 0:
        raise ConfigError("safety.max_run_minutes must be positive")
    if cfg["flow"]["pulses_per_litre"] <= 0:
        raise ConfigError("flow.pulses_per_litre must be positive")
    return cfg


def load_config(path: str | Path | None = None, data: dict | None = None) -> dict:
    """Load config from YAML (or a dict, for tests) merged over defaults."""
    user: dict = {}
    if data is not None:
        user = data
    elif path is not None:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"Config file not found: {p}")
        try:
            user = yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" near line {mark.line + 1}" if mark else ""
            raise ConfigError(f"{p} isn't valid YAML{where}. Check indentation and colons.") from exc
        if not isinstance(user, dict):
            raise ConfigError(f"{p} should contain settings like 'zones:' at the top level")
    cfg = _merge(DEFAULTS, user)
    for key in ("zones", "soil_sensors"):  # lists replace rather than merge
        if key in user:
            cfg[key] = copy.deepcopy(user[key])
    return validate(cfg)
