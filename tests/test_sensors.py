from irrigation.sensors import raw_to_percent, soil_readings, tank_state, zone_moisture


def test_percent_capacitive_scale():
    assert raw_to_percent(2900, 2900, 1300) == 0
    assert raw_to_percent(1300, 2900, 1300) == 100
    assert raw_to_percent(2100, 2900, 1300) == 50
    assert raw_to_percent(3500, 2900, 1300) == 0     # clamped
    assert raw_to_percent(900, 2900, 1300) == 100


def test_percent_reversed_scale():
    assert raw_to_percent(75, 0, 100) == 75


def test_unplugged_sensor_ignored(cfg):
    st = {"online": True, "soil_raw": [2100, 0, 1300]}
    r = soil_readings(cfg, st)
    assert r[1]["percent"] == 50 and r[2]["percent"] is None and r[3]["percent"] == 100
    assert zone_moisture(cfg, cfg["zones"][1], r) == 75


def test_tank_states(cfg):
    assert tank_state(cfg, {"online": True, "float_low": 0, "float_high": 0})["label"] == "Full"
    assert tank_state(cfg, {"online": True, "float_low": 0, "float_high": 1})["label"] == "OK"
    t = tank_state(cfg, {"online": True, "float_low": 1, "float_high": 1})
    assert t["label"] == "Low" and t["low_ok"] is False
    assert tank_state(cfg, {"online": False})["low_ok"] is None
    cfg["tank"]["float_wet_value"] = 1  # inverted floats
    assert tank_state(cfg, {"online": True, "float_low": 1, "float_high": 0})["label"] == "OK"
