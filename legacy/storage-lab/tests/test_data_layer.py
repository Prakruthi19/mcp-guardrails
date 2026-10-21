"""Unit tests for the mock data layer (mcp_server/data_layer.py)."""

import random

import pytest

from mcp_server import data_layer


def test_list_device_ids_excludes_comment_key():
    ids = data_layer.list_device_ids()
    assert "_comment" not in ids
    assert "nvme0n1" in ids


def test_get_device_health_known_device_has_smart_shape():
    health = data_layer.get_device_health("nvme0n1")
    for field in (
        "percentage_used",
        "composite_temperature_celsius",
        "critical_warning",
        "power_on_hours",
        "reallocated_sector_count",
    ):
        assert field in health
    assert health["device_id"] == "nvme0n1"


def test_get_device_health_unknown_device_raises():
    with pytest.raises(data_layer.UnknownDeviceError):
        data_layer.get_device_health("nvme99n1")


@pytest.mark.parametrize(
    "device_id,expected_flag",
    [
        ("nvme3n1", "healthy"),
        ("nvme1n1", "warning"),
        ("nvme2n1", "critical"),
    ],
)
def test_health_flag_reflects_wear_and_critical_warning(device_id, expected_flag):
    assert data_layer.get_device_health(device_id)["health_flag"] == expected_flag


def test_simulate_test_run_unknown_device_raises():
    with pytest.raises(data_layer.UnknownDeviceError):
        data_layer.simulate_test_run("nvme99n1", "latency_benchmark")


def test_simulate_test_run_invalid_suite_raises():
    with pytest.raises(data_layer.InvalidTestSuiteError):
        data_layer.simulate_test_run("nvme0n1", "not_a_real_suite")


def test_simulate_test_run_can_pass_and_fail_explicitly():
    """run_device_test must be able to fail, not just always pass."""
    passed = [
        data_layer.simulate_test_run("nvme0n1", "read_write_endurance", random.Random(seed))
        for seed in range(20)
    ]
    statuses = {r["status"] for r in passed}
    # A healthy device (nvme0n1) should pass overwhelmingly but the
    # mechanism itself must support "fail" -- exercised directly below.
    assert "pass" in statuses

    failed = data_layer.simulate_test_run(
        "nvme2n1", "power_cycle", random.Random(1)
    )
    assert failed["status"] == "fail"
    assert failed["failure_reason"] is not None
    assert failed["recommended_action"] in ("escalate_rerun", "escalate_replace")


def test_failed_result_differs_meaningfully_from_passed_result():
    fail_result = data_layer.simulate_test_run("nvme2n1", "power_cycle", random.Random(1))
    pass_result = data_layer.simulate_test_run("nvme0n1", "read_write_endurance", random.Random(0))

    assert fail_result["failure_reason"] is not None
    assert pass_result["failure_reason"] is None
    assert fail_result["recommended_action"] != "none"
    assert pass_result["recommended_action"] == "none"


def test_critical_device_more_likely_to_fail_than_healthy_device():
    n = 200
    healthy_fails = sum(
        data_layer.simulate_test_run("nvme0n1", "read_write_endurance", random.Random(seed))["status"] == "fail"
        for seed in range(n)
    )
    critical_fails = sum(
        data_layer.simulate_test_run("nvme2n1", "read_write_endurance", random.Random(seed))["status"] == "fail"
        for seed in range(n)
    )
    assert critical_fails > healthy_fails


def test_get_schedule_unknown_device_raises():
    with pytest.raises(data_layer.UnknownDeviceError):
        data_layer.get_schedule("nvme99n1")


def test_add_schedule_entry_appends_and_persists(tmp_path, monkeypatch):
    schedule_copy = tmp_path / "schedule.json"
    schedule_copy.write_text('{"nvme0n1": []}', encoding="utf-8")
    monkeypatch.setattr(data_layer, "SCHEDULE_FILE", schedule_copy)

    entry = data_layer.add_schedule_entry("nvme0n1", "latency_benchmark", "2026-10-01T00:00:00")
    assert entry["status"] == "pending"

    reloaded = data_layer.get_schedule("nvme0n1")
    assert len(reloaded) == 1
    assert reloaded[0]["test_suite"] == "latency_benchmark"


def test_add_schedule_entry_invalid_suite_raises(tmp_path, monkeypatch):
    schedule_copy = tmp_path / "schedule.json"
    schedule_copy.write_text('{"nvme0n1": []}', encoding="utf-8")
    monkeypatch.setattr(data_layer, "SCHEDULE_FILE", schedule_copy)

    with pytest.raises(data_layer.InvalidTestSuiteError):
        data_layer.add_schedule_entry("nvme0n1", "bogus_suite", "2026-10-01T00:00:00")
