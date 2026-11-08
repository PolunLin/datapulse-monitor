from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.realtime.devices import configured_devices
from app.schemas.record import RecordCreate, RecordFilters, RecordUpdate

CONTRACTS = [
    pytest.param(
        RecordCreate,
        {"title": "temperature", "value": "23.5", "category": "device-001"},
        "timestamp",
        id="create",
    ),
    pytest.param(RecordUpdate, {}, "timestamp", id="update"),
    pytest.param(RecordFilters, {}, "start_time", id="filter-start"),
    pytest.param(RecordFilters, {}, "end_time", id="filter-end"),
]


@pytest.mark.parametrize("model,payload,field", CONTRACTS)
@pytest.mark.parametrize(
    "timestamp",
    [
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59-12:00",
        "0001-01-01T00:00:00Z",
        "1000-01-01T00:00:00+14:00",
    ],
)
def test_unrepresentable_timestamp_is_a_validation_error(model, payload, field, timestamp):
    with pytest.raises(ValidationError):
        model.model_validate({**payload, field: timestamp})


@pytest.mark.parametrize("model,payload,field", CONTRACTS)
@pytest.mark.parametrize(
    "timestamp,expected",
    [
        ("1000-01-01T14:00:00+14:00", datetime(1000, 1, 1, tzinfo=UTC)),
        (
            "9999-12-31T11:59:59.999999-12:00",
            datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        ),
    ],
)
def test_supported_boundary_timestamp_is_normalized_to_utc(
    model, payload, field, timestamp, expected
):
    result = model.model_validate({**payload, field: timestamp})
    assert getattr(result, field) == expected
    assert getattr(result, field).tzinfo is UTC


@pytest.mark.parametrize("category", [device.id for device in configured_devices()])
def test_record_device_categories_match_simulator_catalog(category):
    created = RecordCreate(title="設備回報", value="25.123456", category=category)
    assert created.category == category
    assert RecordUpdate(category=category).category == category


@pytest.mark.parametrize("category", ["cpu", "temperature", "device-004", "legacy", "設備 A", ""])
def test_new_or_updated_category_must_be_a_configured_device(category):
    with pytest.raises(ValidationError):
        RecordCreate(title="device", value="25", category=category)
    with pytest.raises(ValidationError):
        RecordUpdate(category=category)
    # Existing free-form categories remain readable/searchable.
    if category:
        assert RecordFilters(category=category).category == category


@pytest.mark.parametrize("value", ["abc", "25°C", "NaN", "Infinity", True, "0.0000001"])
def test_create_and_edit_values_are_finite_numbers_with_supported_precision(value):
    with pytest.raises(ValidationError):
        RecordCreate(title="device", value=value, category="device-001")
    with pytest.raises(ValidationError):
        RecordUpdate(value=value)


@pytest.mark.parametrize("timestamp", [1794096000, "1794096000", True, "2026-11-08T12:00:00"])
def test_unzoned_or_numeric_input_cannot_be_interpreted_as_a_record_time(timestamp):
    with pytest.raises(ValidationError):
        RecordCreate(title="device", value="1", category="device-001", timestamp=timestamp)
