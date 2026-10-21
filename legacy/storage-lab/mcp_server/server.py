"""
MCP server exposing storage-device test-automation tools over stdio.

IMPORTANT — this is all simulated. There is no physical NVMe hardware
behind check_device_health or run_device_test. Health data is read from a
local mock JSON file shaped like a real NVMe SMART / Health Information
Log Page, and test execution is a short async sleep followed by a
pass/fail decision derived from that mock health data. See README.md for
the honesty note on this; do not present this server as talking to real
hardware.

Run directly for a quick manual smoke test:
    python -m mcp_server.server
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from mcp_server import data_layer

mcp = FastMCP("storage-test-automation")

TestSuite = Literal["read_write_endurance", "latency_benchmark", "power_cycle"]


@mcp.tool()
def check_device_health(device_id: str) -> dict[str, Any]:
    """Return simulated SMART-style health telemetry for one NVMe storage device.

    Use this FIRST, before deciding whether to run a test, to check a
    device's current wear level and error signals. The data is mocked
    (no physical device is queried) but the field names mirror a real
    NVMe SMART / Health Information Log Page: percentage_used (wear
    level, 0-100+), composite_temperature_celsius, critical_warning
    (nonzero means the controller is signalling a problem),
    reallocated_sector_count, media_and_data_integrity_errors,
    power_on_hours, and unsafe_shutdowns.

    The response also includes a derived `health_flag` of "healthy",
    "warning" (percentage_used >= 60), or "critical" (percentage_used
    >= 90 or critical_warning is set) to make the test-worthiness
    decision explicit for the caller.

    Args:
        device_id: Device identifier, e.g. "nvme0n1". Call
            get_test_schedule or try a known id if unsure; unknown ids
            raise an error listing that the device was not found.

    Returns:
        A dict of SMART-style fields plus device_id and health_flag.

    Raises:
        ValueError: if device_id is not a known mock device.
    """
    try:
        return data_layer.get_device_health(device_id)
    except data_layer.UnknownDeviceError as e:
        raise ValueError(
            f"Unknown device_id '{device_id}'. Known devices: "
            f"{', '.join(data_layer.list_device_ids())}"
        ) from e


@mcp.tool()
async def run_device_test(device_id: str, test_suite: TestSuite) -> dict[str, Any]:
    """Trigger a simulated test run of test_suite against device_id and return the result.

    This actually executes a short simulated run (a few seconds of async
    delay standing in for real test execution time) and returns pass or
    fail — it does not always return pass. Call check_device_health
    first: a device with health_flag "warning" or "critical" is
    meaningfully more likely to fail here, because failure probability
    is derived from that device's wear level, temperature, and error
    counts, not from independent random chance.

    On failure, the result includes `failure_reason` (why, referencing
    the device's health signals) and `recommended_action`:
    "escalate_rerun" (re-run the test / monitor) or "escalate_replace"
    (device health is critical -- recommend scheduling replacement,
    not just a re-run). Treat a failed result differently from a
    passed one: do not report it as routine success.

    Args:
        device_id: Device identifier, e.g. "nvme0n1".
        test_suite: One of "read_write_endurance" (throughput/error
            stress test), "latency_benchmark" (read/write latency
            under load), or "power_cycle" (repeated power-cycle
            resilience test).

    Returns:
        A dict with status ("pass"/"fail"), duration_seconds, metrics
        (suite-specific), summary, failure_reason (None on pass), and
        recommended_action ("none", "escalate_rerun", or
        "escalate_replace").

    Raises:
        ValueError: if device_id is unknown or test_suite is invalid.
    """
    # Simulate real test execution taking measurable wall-clock time,
    # without blocking the event loop.
    await asyncio.sleep(random.uniform(0.5, 1.5))

    try:
        return data_layer.simulate_test_run(device_id, test_suite)
    except data_layer.UnknownDeviceError as e:
        raise ValueError(
            f"Unknown device_id '{device_id}'. Known devices: "
            f"{', '.join(data_layer.list_device_ids())}"
        ) from e
    except data_layer.InvalidTestSuiteError as e:
        raise ValueError(
            f"Invalid test_suite '{test_suite}'. Valid suites: "
            f"{', '.join(data_layer.TEST_SUITES)}"
        ) from e


@mcp.tool()
def get_test_schedule(
    device_id: str,
    action: Literal["view", "add"] = "view",
    test_suite: TestSuite | None = None,
    scheduled_for: str | None = None,
) -> dict[str, Any]:
    """View or add to the local JSON-backed test schedule for a device.

    With action="view" (default), returns the list of upcoming/past
    test-schedule entries for device_id. With action="add", appends a
    new pending entry (requires test_suite and scheduled_for) -- use
    this to schedule a re-test or follow-up after a failed
    run_device_test call, e.g. after recommending "escalate_rerun".

    Args:
        device_id: Device identifier, e.g. "nvme0n1".
        action: "view" to list the schedule, "add" to append an entry.
        test_suite: Required when action="add". One of
            "read_write_endurance", "latency_benchmark", "power_cycle".
        scheduled_for: Required when action="add". ISO-8601 timestamp
            string, e.g. "2026-09-01T02:00:00".

    Returns:
        {"device_id": ..., "schedule": [...]} listing all entries for
        the device after applying the requested action.

    Raises:
        ValueError: if device_id/test_suite is invalid, or action="add"
            is missing test_suite/scheduled_for.
    """
    try:
        if action == "add":
            if not test_suite or not scheduled_for:
                raise ValueError(
                    "action='add' requires both test_suite and scheduled_for."
                )
            data_layer.add_schedule_entry(device_id, test_suite, scheduled_for)

        schedule = data_layer.get_schedule(device_id)
        return {"device_id": device_id, "schedule": schedule}
    except data_layer.UnknownDeviceError as e:
        raise ValueError(
            f"Unknown device_id '{device_id}'. Known devices: "
            f"{', '.join(data_layer.list_device_ids())}"
        ) from e
    except data_layer.InvalidTestSuiteError as e:
        raise ValueError(
            f"Invalid test_suite '{test_suite}'. Valid suites: "
            f"{', '.join(data_layer.TEST_SUITES)}"
        ) from e


if __name__ == "__main__":
    mcp.run(transport="stdio")
