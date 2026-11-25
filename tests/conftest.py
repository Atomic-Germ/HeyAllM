"""Shared test fixtures: a fake LLM that never touches the network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from heyallm.config import Character, Config, Endpoint  # noqa: E402
from fakes import FakeLLM  # noqa: E402,F401 - re-exported for tests


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def config(data_dir: Path) -> Config:
    cfg = Config(data_dir=data_dir)
    cfg.cooldown.min_seconds = 0.0
    cfg.cooldown.max_seconds = 0.0
    cfg.cooldown.chars_per_second = 10_000.0
    cfg.engine.director_end_check = False
    return cfg


@pytest.fixture
def alice() -> Character:
    return Character(
        id="char-alice",
        screenname="mara",
        real_name="Mara Ellison",
        demographics={"age": "29", "occupation": "night-shift radio operator"},
        persona="You keep your voice low. You check the door before you answer.",
        endpoint=Endpoint(base_url="http://127.0.0.1:9001/v1", model="alice-model"),
    )


@pytest.fixture
def bob() -> Character:
    return Character(
        id="char-bob",
        screenname="grey",
        real_name="Jonas Grey",
        demographics={"age": "41", "occupation": "harbour master"},
        persona="You never say the first thing you think. You tap twice when lying.",
        endpoint=Endpoint(base_url="http://127.0.0.1:9002/v1", model="bob-model"),
    )


@pytest.fixture
def cast_config(config: Config, alice: Character, bob: Character) -> Config:
    config.characters = [alice, bob]
    config.director = Endpoint(base_url="http://127.0.0.1:9003/v1", model="director-model")
    return config
