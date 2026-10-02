"""Turning raw readings into things people understand."""
from __future__ import annotations


def raw_to_percent(raw: int, dry_raw: int, wet_raw: int) -> float:
    """0% = as dry as the dry calibration, 100% = as wet as the wet one.
    Works whichever way the sensor's scale runs."""
    pct = (raw - dry_raw) / (wet_raw - dry_raw) * 100.0
    return round(max(0.0, min(100.0, pct)), 1)


def soil_readings(cfg: dict, status: dict | None) -> dict[int, dict]:
    out = {}
    for s in cfg["soil_sensors"]:
        raw = None
        if status and status.get("online"):
            raw = status["soil_raw"][s["id"] - 1]
        valid = raw is not None and raw > 50  # 0 means unplugged / no power
        out[s["id"]] = {
            "id": s["id"], "name": s["name"], "raw": raw,
            "percent": raw_to_percent(raw, s["dry_raw"], s["wet_raw"]) if valid else None,
        }
    return out


def zone_moisture(cfg: dict, zone: dict, readings: dict[int, dict]) -> float | None:
    vals = [readings[sid]["percent"] for sid in zone.get("sensors", [])
            if readings.get(sid, {}).get("percent") is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def tank_state(cfg: dict, status: dict | None) -> dict:
    """Return {'low_ok': bool|None, 'high_reached': bool|None, 'label': str}."""
    if not cfg["tank"]["enabled"]:
        return {"enabled": False, "low_ok": None, "high_reached": None, "label": "Not monitored"}
    if not status or not status.get("online"):
        return {"enabled": True, "low_ok": None, "high_reached": None, "label": "Unknown"}
    wet = cfg["tank"]["float_wet_value"]
    low_ok = status["float_low"] == wet
    high = status["float_high"] == wet
    if high:
        label = "Full"
    elif low_ok:
        label = "OK"
    else:
        label = "Low"
    return {"enabled": True, "low_ok": low_ok, "high_reached": high, "label": label}
