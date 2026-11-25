"""Config loading, saving, and the pacing maths."""

from __future__ import annotations

import json
from pathlib import Path

from heyallm.config import (
    Character,
    Cooldown,
    Config,
    CreativeCfg,
    Endpoint,
    EngineCfg,
    blank_character,
)


def test_endpoint_urls():
    ep = Endpoint(base_url="http://127.0.0.1:9931/v1/", model="m")
    assert ep.chat_url() == "http://127.0.0.1:9931/v1/chat/completions"
    assert ep.models_url() == "http://127.0.0.1:9931/v1/models"


def test_endpoint_from_dict_defaults_and_trailing_slash():
    ep = Endpoint.from_dict({"base_url": "http://localhost:9000/v1/", "model": "  "})
    assert ep.base_url == "http://localhost:9000/v1"
    assert ep.model == ""  # blank falls back to server default
    assert Endpoint.from_dict(None).base_url == "http://127.0.0.1:9931/v1"
    assert Endpoint.from_dict({"base_url": "   "}).base_url == "http://127.0.0.1:9931/v1"


def test_cooldown_bounds_reading_speed():
    cd = Cooldown(chars_per_second=20.0, min_seconds=1.0, max_seconds=10.0)
    assert cd.delay_for("x" * 40) == 2.0
    assert cd.delay_for("") == 1.0            # never zero -- there is always a beat
    assert cd.delay_for("x" * 1000) == 10.0   # capped


def test_cooldown_ignores_garbage():
    cd = Cooldown.from_dict({"chars_per_second": "abc", "min_seconds": None})
    assert cd.chars_per_second > 0
    assert cd.min_seconds >= 0


def test_engine_cfg_validation():
    cfg = EngineCfg.from_dict({"max_turns": -4, "max_consecutive_silences": 0, "temperature": "hot"})
    assert cfg.max_turns >= 1
    assert cfg.max_consecutive_silences >= 1
    assert cfg.temperature == 0.9


def test_config_round_trip(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    cfg.characters = [
        Character(
            id="char-a",
            screenname="mara",
            real_name="Mara Ellison",
            demographics={"age": "29", "occupation": "operator"},
            persona="Low voice.",
            endpoint=Endpoint(base_url="http://a/v1", model="ma", api_key="sk-secret"),
        )
    ]
    cfg.director = Endpoint(base_url="http://d/v1", model="director")
    cfg.cooldown.chars_per_second = 12.5
    cfg.engine.max_turns = 7
    cfg.narration.prefix = "[[nar]]"
    cfg.memory.compact_at_events = 5
    cfg.creative = CreativeCfg(enabled=False, max_tool_calls=6)
    cfg.save()

    again = Config.load(tmp_path)
    assert again.config_path.exists()
    assert len(again.characters) == 1
    char = again.characters[0]
    assert char.screenname == "mara"
    assert char.demographics["occupation"] == "operator"
    assert char.endpoint.api_key == "sk-secret"
    assert again.director.model == "director"
    assert again.cooldown.chars_per_second == 12.5
    assert again.engine.max_turns == 7
    assert again.narration.prefix == "[[nar]]"
    assert again.memory.compact_at_events == 5
    assert again.creative.enabled is False
    assert again.creative.max_tool_calls == 6


def test_creative_tool_config_is_bounded_and_enabled_by_default():
    assert CreativeCfg().enabled is True
    cfg = CreativeCfg.from_dict({"enabled": False, "max_tool_calls": 99})
    assert cfg.enabled is False
    assert cfg.max_tool_calls == 12


def test_load_tolerates_a_corrupt_file(tmp_path: Path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    cfg = Config.load(tmp_path)
    assert cfg.characters == []


def test_load_tolerates_a_missing_file(tmp_path: Path):
    assert Config.load(tmp_path).characters == []


def test_character_lookup_and_removal():
    cfg = Config(data_dir=Path("/tmp/never-used"))
    a = Character(id="char-a")
    b = Character(id="char-b")
    cfg.characters = [a, b]

    assert cfg.get_character("char-a") is a
    assert cfg.get_character("nope") is None
    assert cfg.remove_character("char-a") is True
    assert cfg.remove_character("char-a") is False
    assert [c.id for c in cfg.characters] == ["char-b"]


def test_blank_character_is_ready_for_the_director():
    char = blank_character(Endpoint(model="m"))
    assert char.id.startswith("char-")
    assert char.created_at
    assert char.screenname == ""
    assert char.display_name() == char.id


def test_demographics_block_formats_labels():
    char = Character(id="c", demographics={"speech_style": "clipped", "age": "29"})
    block = char.demographics_block()
    assert "Age: 29" in block
    assert "Speech style: clipped" in block


def test_saved_config_is_valid_json(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    cfg.save()
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert "characters" in data and "cooldown" in data


def test_non_finite_numbers_fall_back_to_defaults():
    ep = Endpoint.from_dict({"timeout": "nan"})
    assert ep.timeout == 180.0
    cfg = EngineCfg.from_dict({"temperature": float("inf"), "max_turns": float("nan")})
    assert cfg.temperature == 0.9
    assert cfg.max_turns == 60


def test_malformed_sections_do_not_crash_config_loading(tmp_path: Path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "config.json").write_text(json.dumps({
        "characters": [{"id": "char-a", "demographics": []}, "not a character"],
        "director": [],
        "cooldown": "not an object",
        "engine": ["not an object"],
    }), encoding="utf-8")

    cfg = Config.load(tmp_path)

    assert cfg.load_error is None
    assert [c.id for c in cfg.characters] == ["char-a"]
    assert cfg.characters[0].demographics == {}
    assert cfg.director.model == ""
    assert cfg.cooldown.chars_per_second == 18.0


def test_cooldown_max_is_at_least_min():
    cfg = Cooldown.from_dict({"min_seconds": 5, "max_seconds": 1})
    assert cfg.max_seconds == 5
    assert cfg.delay_for("short") == 5


def test_non_object_config_is_reported_without_being_replaced(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text("[]", encoding="utf-8")

    cfg = Config.load(tmp_path)

    assert cfg.load_error
    assert path.read_text(encoding="utf-8") == "[]"
