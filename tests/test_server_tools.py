"""
Unit tests for the MCP tool functions in mcp_server/server.py.

FastMCP's @mcp.tool() decorator leaves the underlying function directly
callable, so these tests call check_device_health / run_device_test /
get_test_schedule exactly as written -- no MCP client/transport needed.
"""

import asyncio

import pytest

from mcp_server import server


def test_check_device_health_known_device():
    result = server.check_device_health("nvme0n1")
    assert result["device_id"] == "nvme0n1"
    assert result["health_flag"] in ("healthy", "warning", "critical")


def test_check_device_health_unknown_device_raises_value_error():
    with pytest.raises(ValueError, match="Unknown device_id"):
        server.check_device_health("does_not_exist")


def test_run_device_test_returns_pass_or_fail():
    result = asyncio.run(server.run_device_test("nvme0n1", "read_write_endurance"))
    assert result["status"] in ("pass", "fail")
    assert result["device_id"] == "nvme0n1"
    assert result["test_suite"] == "read_write_endurance"


def test_run_device_test_unknown_device_raises_value_error():
    with pytest.raises(ValueError, match="Unknown device_id"):
        asyncio.run(server.run_device_test("does_not_exist", "power_cycle"))


def test_run_device_test_invalid_suite_raises_value_error():
    with pytest.raises(ValueError, match="Invalid test_suite"):
        asyncio.run(server.run_device_test("nvme0n1", "not_a_suite"))


def test_get_test_schedule_view_returns_list():
    result = server.get_test_schedule("nvme1n1")
    assert result["device_id"] == "nvme1n1"
    assert isinstance(result["schedule"], list)


def test_get_test_schedule_add_requires_suite_and_date():
    with pytest.raises(ValueError, match="requires both"):
        server.get_test_schedule("nvme0n1", action="add")


def test_get_test_schedule_unknown_device_raises_value_error():
    with pytest.raises(ValueError, match="Unknown device_id"):
        server.get_test_schedule("does_not_exist")
