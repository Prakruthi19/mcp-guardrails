"""The assistant: Claude when an API key is configured, a scripted mock otherwise."""

from __future__ import annotations

import os

from opsguard.config import Config
from opsguard.guard import Guard


def make_agent(config: Config, guard: Guard):
    provider = config.llm_provider
    if provider == "auto":
        provider = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "mock"
    if provider == "anthropic":
        from opsguard.agent.claude import ClaudeAgent

        return ClaudeAgent(guard, config.llm_model)
    from opsguard.agent.mock import MockAgent

    return MockAgent(guard)
