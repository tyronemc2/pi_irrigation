"""Rain forecast from Open-Meteo (free, no API key)."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)
URL = "https://api.open-meteo.com/v1/forecast"


class RainForecast:
    def __init__(self, cfg: dict, session=None, clock=time.monotonic, cache_seconds: int = 900):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.clock = clock
        self.cache_seconds = cache_seconds
        self._cache: dict | None = None
        self._cached_at = -1e18

    def _fetch(self) -> dict:
        loc, w = self.cfg["location"], self.cfg["weather"]
        r = self.session.get(URL, timeout=10, params={
            "latitude": loc["latitude"], "longitude": loc["longitude"],
            "hourly": "precipitation_probability,precipitation",
            "forecast_hours": int(w["lookahead_hours"]), "timezone": "auto",
        })
        r.raise_for_status()
        return r.json()

    def check(self, force: bool = False) -> dict:
        """Return {'available', 'rain_expected', 'max_probability', 'total_mm', 'summary'}."""
        if not self.cfg["weather"]["enabled"]:
            return {"available": False, "rain_expected": False, "max_probability": None,
                    "total_mm": None, "summary": "Forecast off"}
        if not force and self._cache and self.clock() - self._cached_at < self.cache_seconds:
            return self._cache
        w = self.cfg["weather"]
        try:
            hourly = self._fetch()["hourly"]
            probs = [p for p in hourly.get("precipitation_probability", []) if p is not None]
            mm = [p for p in hourly.get("precipitation", []) if p is not None]
            max_p = max(probs) if probs else 0
            total = round(sum(mm), 1)
            rain = max_p >= w["rain_probability_threshold"] or total >= w["rain_mm_threshold"]
            hours = w["lookahead_hours"]
            summary = (f"Rain likely in the next {hours} h ({max_p}%, {total} mm)" if rain
                       else f"Dry for the next {hours} h ({max_p}% chance)")
            result = {"available": True, "rain_expected": rain, "max_probability": max_p,
                      "total_mm": total, "summary": summary}
        except Exception as exc:
            log.warning("Forecast unavailable: %s", exc)
            result = {"available": False, "rain_expected": False, "max_probability": None,
                      "total_mm": None, "summary": "Forecast unavailable"}
        self._cache, self._cached_at = result, self.clock()
        return result
