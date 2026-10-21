"""
Mock data layer for the storage-test-automation-mcp agent.

Everything here is SIMULATED. There is no physical NVMe device behind any
of these functions — device_health.json is a hand-authored JSON file whose
field names mirror the real NVMe SMART / Health Information Log Page
(0x02) shape, and schedule.json is a small JSON-backed calendar. This
module is intentionally synchronous and free of I/O latency simulation
(no sleep() here) so it stays trivially unit-testable; the MCP server
layer is responsible for simulating any "test execution takes time"
delay before/around calling into this module.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HEALTH_FILE = DATA_DIR / "device_health.json"
SCHEDULE_FILE = DATA_DIR / "schedule.json"

TEST_SUITES = ("read_write_endurance", "latency_benchmark", "power_cycle")

# Wear level (percentage_used) at/above which a device is considered to
# warrant proactive testing per this project's health -> decision chain.
WEAR_WARNING_THRESHOLD = 60
WEAR_CRITICAL_THRESHOLD = 90


class UnknownDeviceError(KeyError):
    """Raised when a device_id has no entry in the mock data files."""


class InvalidTestSuiteError(ValueError):
    """Raised when a test_suite string isn't one of TEST_SUITES."""


def _load_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def list_device_ids() -> list[str]:
    health = _load_json(HEALTH_FILE)
    return [k for k in health.keys() if not k.startswith("_")]


def get_device_health(device_id: str) -> dict[str, Any]:
    """Return the mock SMART-style health record for device_id.

    Raises UnknownDeviceError if device_id isn't in the mock dataset.
    """
    health = _load_json(HEALTH_FILE)
    if device_id not in health:
        raise UnknownDeviceError(device_id)
    record = dict(health[device_id])
    record["device_id"] = device_id

    percentage_used = record.get("percentage_used", 0)
    if percentage_used >= WEAR_CRITICAL_THRESHOLD or record.get("critical_warning", 0) != 0:
        record["health_flag"] = "critical"
    elif percentage_used >= WEAR_WARNING_THRESHOLD:
        record["health_flag"] = "warning"
    else:
        record["health_flag"] = "healthy"

    return record


def get_schedule(device_id: str) -> list[dict[str, Any]]:
    schedule = _load_json(SCHEDULE_FILE)
    if device_id not in schedule:
        raise UnknownDeviceError(device_id)
    return list(schedule[device_id])


def add_schedule_entry(device_id: str, test_suite: str, scheduled_for: str) -> dict[str, Any]:
    if test_suite not in TEST_SUITES:
        raise InvalidTestSuiteError(test_suite)

    schedule = _load_json(SCHEDULE_FILE)
    if device_id not in schedule:
        raise UnknownDeviceError(device_id)

    entry = {"test_suite": test_suite, "scheduled_for": scheduled_for, "status": "pending"}
    schedule[device_id].append(entry)
    _save_json(SCHEDULE_FILE, schedule)
    return entry


def _fail_probability(health: dict[str, Any], test_suite: str) -> float:
    """Derive a failure probability from mock health signals.

    This is what makes run_device_test's pass/fail outcome causally
    connected to check_device_health's output rather than pure noise:
    worn/critical devices fail simulated tests more often.
    """
    base = 0.03
    wear_component = (health.get("percentage_used", 0) / 100) * 0.55
    critical_component = 0.30 if health.get("critical_warning", 0) else 0.0

    suite_component = 0.0
    if test_suite == "power_cycle":
        suite_component = min(health.get("unsafe_shutdowns", 0) / 500, 0.15)
    elif test_suite == "latency_benchmark":
        temp = health.get("composite_temperature_celsius", 0)
        suite_component = 0.15 if temp >= 70 else 0.0
    elif test_suite == "read_write_endurance":
        suite_component = min(health.get("media_and_data_integrity_errors", 0) / 100, 0.20)

    return min(base + wear_component + critical_component + suite_component, 0.97)


def _suite_metrics(test_suite: str, passed: bool, rng: random.Random) -> dict[str, Any]:
    if test_suite == "read_write_endurance":
        return {
            "write_throughput_mb_s": round(rng.uniform(320, 520) if passed else rng.uniform(40, 200), 1),
            "read_throughput_mb_s": round(rng.uniform(480, 620) if passed else rng.uniform(60, 300), 1),
            "errors_detected": 0 if passed else rng.randint(1, 25),
        }
    if test_suite == "latency_benchmark":
        return {
            "avg_latency_us": round(rng.uniform(40, 90) if passed else rng.uniform(200, 900), 1),
            "p99_latency_us": round(rng.uniform(120, 250) if passed else rng.uniform(900, 4000), 1),
        }
    if test_suite == "power_cycle":
        cycles = 20
        completed = cycles if passed else rng.randint(1, cycles - 1)
        return {"cycles_requested": cycles, "cycles_completed": completed}
    return {}


def simulate_test_run(
    device_id: str,
    test_suite: str,
    rng: Optional[random.Random] = None,
) -> dict[str, Any]:
    """Run a simulated test_suite against device_id and return a result.

    Explicitly supports both pass and fail outcomes -- failure likelihood
    is derived from the device's mock health record via _fail_probability,
    so a worn/critical device fails more often than a healthy one. This
    is the required "failure handling" path: a failed result carries a
    failure_reason and a distinct recommended_action the agent is
    expected to act on differently than a pass.
    """
    if test_suite not in TEST_SUITES:
        raise InvalidTestSuiteError(test_suite)

    health = get_device_health(device_id)  # raises UnknownDeviceError if unknown
    rng = rng or random.Random()

    fail_prob = _fail_probability(health, test_suite)
    passed = rng.random() >= fail_prob

    metrics = _suite_metrics(test_suite, passed, rng)
    duration_seconds = round(rng.uniform(1.5, 4.0), 2)

    result: dict[str, Any] = {
        "device_id": device_id,
        "test_suite": test_suite,
        "status": "pass" if passed else "fail",
        "duration_seconds": duration_seconds,
        "metrics": metrics,
    }

    if passed:
        result["summary"] = (
            f"{test_suite} completed successfully on {device_id} in "
            f"{duration_seconds}s. No anomalies detected."
        )
        result["failure_reason"] = None
        result["recommended_action"] = "none"
    else:
        reason = (
            f"{test_suite} failed on {device_id}: health_flag="
            f"{health['health_flag']}, percentage_used={health['percentage_used']}%, "
            f"critical_warning={health['critical_warning']}."
        )
        result["summary"] = f"{test_suite} FAILED on {device_id} after {duration_seconds}s."
        result["failure_reason"] = reason
        result["recommended_action"] = (
            "escalate_replace" if health["health_flag"] == "critical" else "escalate_rerun"
        )

    return result
