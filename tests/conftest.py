from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from opsguard.config import Config, parse_config
from opsguard.runtime import Runtime, build_runtime

ROOT = Path(__file__).resolve().parent.parent


def demo_config(tmp_path: Path, **overrides) -> Config:
    with open(ROOT / "opsguard.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw["database"] = str(tmp_path / "test.db")
    raw["llm"] = {"provider": "mock"}
    raw.update(overrides)
    return parse_config(raw)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return demo_config(tmp_path)


@pytest.fixture
async def runtime(config: Config) -> Runtime:
    rt = await build_runtime(config)
    yield rt
    await rt.close()


@pytest.fixture
def users(config: Config):
    return config.users
