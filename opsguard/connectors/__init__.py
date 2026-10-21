"""Connectors: one module per kind of system the assistant can reach.

Each connector exposes `build(backend, ...) -> list[Tool]`. The `demo`
backend runs against the simulated environment in demo_infra.py; the
`local` backend runs against the machine OpsGuard is running on.
"""
