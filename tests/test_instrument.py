"""Load/save/validate for the instrument document, and the two-form calibration."""

import datetime

import pytest

from clockwork import instrument

SAMPLE = """\
schema_version = 1

[instrument]
name = "SLIM3"
description = "20 dB after the preamplifier."

[calibration]
slope = 0.738123
intercept = 0.07690495
measured = 2026-09-09

[vertical]
full_scale_v = 0.5
offset_v = 0.251
"""


def test_loads_sample_document() -> None:
    machine = instrument.loads(SAMPLE)
    assert machine.schema_version == instrument.SCHEMA_VERSION == 1
    assert machine.name == "SLIM3"
    assert machine.description == "20 dB after the preamplifier."
    assert machine.calibration == instrument.Calibration(
        slope=0.738123, intercept=0.07690495, measured=datetime.date(2026, 9, 9)
    )
    assert machine.vertical == instrument.Vertical(full_scale_v=0.5, offset_v=0.251)


def test_round_trips_through_dumps() -> None:
    machine = instrument.loads(SAMPLE)
    assert instrument.loads(instrument.dumps(machine)) == machine


def test_saves_and_loads_a_file(tmp_path) -> None:
    path = tmp_path / "instrument.toml"
    machine = instrument.loads(SAMPLE)
    instrument.save(machine, str(path))
    assert instrument.load(str(path)) == machine


def test_an_empty_document_is_the_uncalibrated_instrument() -> None:
    """The document is optional and so is every table in it."""
    assert instrument.loads("") == instrument.UNCALIBRATED
    assert instrument.loads("schema_version = 1\n") == instrument.UNCALIBRATED


def test_uncalibrated_states_nothing() -> None:
    assert not instrument.UNCALIBRATED.calibration.usable
    assert not instrument.UNCALIBRATED.vertical.stated


def test_dumps_leaves_out_a_table_with_nothing_in_it() -> None:
    """A rig with no mass axis and no configured window writes neither table."""
    text = instrument.dumps(instrument.UNCALIBRATED)
    assert "calibration" not in text
    assert "vertical" not in text
    assert instrument.loads(text) == instrument.UNCALIBRATED


def test_a_partial_vertical_keeps_the_field_it_has() -> None:
    machine = instrument.loads("[vertical]\nfull_scale_v = 2.5\n")
    assert machine.vertical == instrument.Vertical(full_scale_v=2.5, offset_v=None)
    assert machine.vertical.stated
    assert instrument.loads(instrument.dumps(machine)) == machine


def test_a_calibration_with_only_a_date_survives_the_round_trip() -> None:
    """Zeros plus a date says the pair was looked at and there was none, which is not
    the same as never having asked."""
    machine = instrument.loads("[calibration]\nmeasured = 2026-09-09\n")
    assert machine.calibration.measured == datetime.date(2026, 9, 9)
    assert not machine.calibration.usable
    assert instrument.loads(instrument.dumps(machine)) == machine


def test_the_two_forms_of_one_calibration_agree() -> None:
    """`CalibrationA` and `CalibrationT0` off a digitizer properties file, in tenths of a
    nanosecond, are the pair the golden files store in microseconds (lab record, task 25)."""
    converted = instrument.Calibration.from_tenths_of_ns(7.38123e-05, 769.0495)
    assert converted.slope == pytest.approx(0.738123)
    assert converted.intercept == pytest.approx(0.07690495)
    assert converted.usable


def test_the_conversion_carries_its_date() -> None:
    when = datetime.date(2026, 9, 9)
    converted = instrument.Calibration.from_tenths_of_ns(7.38123e-05, 769.0495, when)
    assert converted.measured == when


def test_a_zero_slope_is_unusable() -> None:
    assert not instrument.Calibration().usable
    assert not instrument.Calibration(slope=0.0, intercept=1.0).usable


def test_rejects_a_schema_it_does_not_know() -> None:
    with pytest.raises(instrument.InstrumentError) as excinfo:
        instrument.loads("schema_version = 2\n")
    assert "schema_version" in str(excinfo.value)


def test_rejects_a_negative_slope() -> None:
    with pytest.raises(instrument.InstrumentError) as excinfo:
        instrument.loads("[calibration]\nslope = -1.0\n")
    assert "calibration.slope" in str(excinfo.value)


def test_rejects_a_full_scale_that_is_not_positive() -> None:
    with pytest.raises(instrument.InstrumentError) as excinfo:
        instrument.loads("[vertical]\nfull_scale_v = 0.0\n")
    assert "vertical.full_scale_v" in str(excinfo.value)


def test_accepts_a_negative_offset() -> None:
    """The offset is a position within the window, not a size."""
    machine = instrument.loads("[vertical]\noffset_v = -0.2\n")
    assert machine.vertical.offset_v == -0.2


def test_rejects_an_unknown_key() -> None:
    """A misspelled key would otherwise leave the file claiming a window nobody set."""
    with pytest.raises(instrument.InstrumentError) as excinfo:
        instrument.loads("[vertical]\nfull_scale = 0.5\n")
    assert "vertical.full_scale" in str(excinfo.value)


def test_rejects_a_measured_that_is_not_a_date() -> None:
    with pytest.raises(instrument.InstrumentError) as excinfo:
        instrument.loads('[calibration]\nmeasured = "yesterday"\n')
    assert "calibration.measured" in str(excinfo.value)


def test_reports_every_problem_at_once() -> None:
    text = "[calibration]\nslope = -1.0\n\n[vertical]\nfull_scale_v = -0.5\n"
    with pytest.raises(instrument.InstrumentError) as excinfo:
        instrument.loads(text)
    assert len(excinfo.value.problems) == 2


def test_rejects_invalid_toml() -> None:
    with pytest.raises(instrument.InstrumentError) as excinfo:
        instrument.loads("[calibration")
    assert "invalid TOML" in str(excinfo.value)
