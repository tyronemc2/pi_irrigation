import pytest

from irrigation.config import ConfigError, load_config


def test_defaults_match_greenhouse():
    c = load_config(data={})
    names = [z["name"] for z in c["zones"]]
    assert names == ["Hydroponics", "Beds & pots"]
    assert c["zones"][1]["sensors"] == [1, 2, 3]
    assert c["zones"][0]["skip_if_rain"] is False and c["zones"][1]["skip_if_rain"] is False


def test_yaml_file(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("safety:\n  max_run_minutes: 12\nzones:\n  - {id: 7, name: Bed, valve: b, sensors: [1]}\n")
    c = load_config(p)
    assert c["safety"]["max_run_minutes"] == 12
    assert c["safety"]["no_flow_timeout_seconds"] == 45  # untouched defaults kept
    assert c["zones"][0]["valve"] == "B"
    assert c["zones"][0]["skip_if_wet"] is True  # has sensors -> default on


@pytest.mark.parametrize("data,msg", [
    ({"zones": [{"id": 1, "name": "a", "valve": "A"}, {"id": 2, "name": "b", "valve": "A"}]}, "Valve A"),
    ({"zones": [{"id": 1, "name": "a", "valve": "E"}]}, "valve must"),
    ({"zones": [{"id": 1, "name": "a", "valve": "A", "sensors": [9]}]}, "unknown soil sensor"),
    ({"controller": {"mode": "magic"}}, "controller.mode"),
    ({"safety": {"max_run_minutes": 0}}, "max_run_minutes"),
    ({"zones": []}, "At least one zone"),
])
def test_invalid(data, msg):
    with pytest.raises(ConfigError, match=msg):
        load_config(data=data)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_broken_yaml_is_readable(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("zones:\n  - id: 1\n bad: [\n")
    with pytest.raises(ConfigError, match="isn't valid YAML near line"):
        load_config(p)
