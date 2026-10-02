import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from irrigation.config import load_config  # noqa: E402
from irrigation.device import SimulatedDevice  # noqa: E402
from irrigation.notifier import Notifier  # noqa: E402
from irrigation.state import StateStore  # noqa: E402
from irrigation.system import IrrigationSystem  # noqa: E402


class FakeClock:
    """Monotonic seconds + matching wall clock, advanced by hand."""
    def __init__(self, start=datetime(2026, 10, 5, 5, 0)):  # a Monday
        self.t = 1000.0
        self.start = start

    def __call__(self):
        return self.t

    def now(self):
        return self.start + timedelta(seconds=self.t - 1000.0)

    def advance(self, seconds):
        self.t += seconds


class FakeForecast:
    def __init__(self, rain=False, available=True):
        self.rain, self.available = rain, available

    def check(self, force=False):
        return {"available": self.available, "rain_expected": self.rain, "max_probability": 80 if self.rain else 5,
                "total_mm": 4.0 if self.rain else 0.0,
                "summary": "Rain likely" if self.rain else "Dry"}


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def cfg():
    c = load_config(data={"state_file": None})
    c["controller"]["mode"] = "simulated"
    return c


@pytest.fixture
def make_system(cfg, clock, tmp_path):
    def _make(cfg=cfg, schedules=None, forecast=None):
        store = StateStore(str(tmp_path / "state.json"))
        if schedules is not None:
            for s in store.schedules():
                store.delete_schedule(s["id"])
            for s in schedules:
                store.add_schedule(dict(s))
        mapping = {z["valve"]: z["sensors"] for z in cfg["zones"]}
        dev = SimulatedDevice(mapping, cfg["flow"]["pulses_per_litre"], clock=clock, flow_valves=("A",))
        system = IrrigationSystem(cfg, device=dev, store=store, notifier=Notifier(cfg, background=False),
                                  forecast=forecast or FakeForecast(), clock=clock, now=clock.now)
        system.startup()
        return system
    return _make


def run_for(system, clock, seconds, step=3):
    for _ in range(int(seconds // step)):
        clock.advance(step)
        system.step()
