"""Load/save/validate for the flat TOML method document, and its provenance stamp."""

import datetime

import pytest

from clockwork import method

SAMPLE = """\
schema_version = 1

[metadata]
name = "smoke-test"
created = 2026-09-06
description = "A minimal two-box method for tests."

[acquisition]
frames = 1
scans = 100
accumulations = 10
file_stem = "smoke-test"

[[boxes]]
name = "box1"
port = "COM3"
strings = ["STBLCLK,EXT", "STBLDAT;25:[A:10,10:A:1,25:A:0,100:];"]

[[boxes]]
name = "box2"
port = "COM4"
strings = ["STBLCLK,EXT", "STBLDAT;0:[A:10,50:A:1,100:];"]
"""


def test_loads_sample_method() -> None:
    m = method.loads(SAMPLE)
    assert m.schema_version == method.SCHEMA_VERSION
    assert m.metadata.name == "smoke-test"
    assert m.metadata.created == datetime.date(2026, 9, 6)
    assert m.acquisition == method.Acquisition(
        frames=1, scans=100, accumulations=10, file_stem="smoke-test"
    )
    assert [box.name for box in m.boxes] == ["box1", "box2"]
    assert m.boxes[0].port == "COM3"
    assert m.boxes[0].strings[0] == "STBLCLK,EXT"


def test_save_then_load_round_trips(tmp_path) -> None:
    m = method.loads(SAMPLE)
    path = tmp_path / "method.toml"
    method.save(m, str(path))
    assert method.load(str(path)) == m


def test_dumps_is_deterministic() -> None:
    m = method.loads(SAMPLE)
    assert method.dumps(m) == method.dumps(m)


def test_stamp_fields() -> None:
    m = method.loads(SAMPLE)
    s = method.stamp(m, console_version="1.2.3")
    assert s["method_name"] == "smoke-test"
    assert s["method_text"] == method.dumps(m)
    assert s["method_hash"] == method.stamp(m, console_version="1.2.3")["method_hash"]
    assert len(s["method_hash"]) == 64
    assert s["console_version"] == "1.2.3"

    import clockwork

    assert s["clockwork_version"] == clockwork.__version__


def test_stamp_hash_changes_with_content() -> None:
    m = method.loads(SAMPLE)
    other = SAMPLE.replace('name = "smoke-test"', 'name = "different"', 1)
    m2 = method.loads(other)
    assert method.stamp(m)["method_hash"] != method.stamp(m2)["method_hash"]


def test_stamp_console_version_defaults_to_none() -> None:
    m = method.loads(SAMPLE)
    assert method.stamp(m)["console_version"] is None


@pytest.mark.parametrize(
    "broken,expected_fragment",
    [
        (SAMPLE.replace("schema_version = 1", "schema_version = 2"), "schema_version"),
        (SAMPLE.replace('name = "smoke-test"\n', "", 1), "metadata.name"),
        (SAMPLE.replace("frames = 1", "frames = 0"), "acquisition.frames"),
        (SAMPLE.replace('file_stem = "smoke-test"', 'file_stem = "a/b"'), "file_stem"),
        (SAMPLE.replace('name = "box2"', 'name = "box1"'), "duplicate box name"),
        (SAMPLE.replace('strings = ["STBLCLK,EXT", "STBLDAT;25:[A:10,10:A:1,25:A:0,100:];"]',
                         "strings = []"), "boxes[0].strings"),
    ],
)
def test_validation_rejects(broken: str, expected_fragment: str) -> None:
    with pytest.raises(method.MethodError) as exc_info:
        method.loads(broken)
    assert expected_fragment in str(exc_info.value)


def test_no_boxes_is_rejected() -> None:
    text = SAMPLE.split("[[boxes]]")[0]
    with pytest.raises(method.MethodError) as exc_info:
        method.loads(text)
    assert "boxes" in str(exc_info.value)


def test_invalid_toml_reports_as_method_error() -> None:
    with pytest.raises(method.MethodError):
        method.loads("this is not [ valid toml")
