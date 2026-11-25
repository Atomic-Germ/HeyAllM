"""Native creative-meditation tools: deterministic generation and persistence."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from heyallm.creative import CREATIVE_TOOLS, CreativeToolbox


def test_tool_schemas_expose_the_core_pipeline():
    assert [tool["function"]["name"] for tool in CREATIVE_TOOLS] == [
        "creative_meditate", "creative_insight", "creative_ponder",
    ]
    assert all(tool["function"]["parameters"]["type"] == "object" for tool in CREATIVE_TOOLS)


def test_seeded_meditation_is_reproducible_and_uses_context(tmp_path: Path):
    toolbox = CreativeToolbox(tmp_path)
    args = {"context_words": ["lighthouse", "salt"], "num_random_words": 8, "seed": "storm"}

    first = toolbox.call("creative_meditate", args)
    second = toolbox.call("creative_meditate", args)

    assert first["random_words"] == second["random_words"]
    assert first["emergent_sentence"] == second["emergent_sentence"]
    assert first["id"] != second["id"]
    assert "lighthouse" in first["emergent_sentence"].lower() or "salt" in first["emergent_sentence"].lower()
    assert len(first["random_words"]) == 8


def test_meditation_validates_model_arguments(tmp_path: Path):
    toolbox = CreativeToolbox(tmp_path)
    for args in (
        {"context_words": "not an array"},
        {"context_words": [1]},
        {"num_random_words": 0},
        {"num_random_words": 33},
        {"num_random_words": 1.5},
        {"seed": 42},
    ):
        with pytest.raises(ValueError):
            toolbox.call("creative_meditate", args)


def test_insight_requires_a_meditation_and_can_select_an_earlier_one(tmp_path: Path):
    toolbox = CreativeToolbox(tmp_path)
    with pytest.raises(ValueError, match="creative_meditate"):
        toolbox.call("creative_insight", {})

    first = toolbox.call("creative_meditate", {"context_words": ["harbor"], "seed": "one"})
    second = toolbox.call("creative_meditate", {"context_words": ["attic"], "seed": "two"})
    insight = toolbox.call("creative_insight", {"meditation_id": first["id"]})

    assert insight["meditation_id"] == first["id"]
    assert insight["insights"]
    assert any("harbor" in item for item in insight["insights"])
    assert second["id"] != first["id"]
    with pytest.raises(ValueError, match="Unknown meditation"):
        toolbox.call("creative_insight", {"meditation_id": "missing"})


def test_ponder_uses_latest_insight_and_persists_reflection(tmp_path: Path):
    toolbox = CreativeToolbox(tmp_path)
    toolbox.call("creative_meditate", {"context_words": ["radio"], "seed": "signal"})
    insight = toolbox.call("creative_insight", {})

    pondered = toolbox.call("creative_ponder", {}, ponder=lambda source: f"Reflection on {source}")

    assert pondered["method"] == "director"
    assert pondered["insight_text"] == "\n".join(insight["insights"])
    assert pondered["reflection"].startswith("Reflection on ")
    reloaded = CreativeToolbox(tmp_path)
    assert reloaded.insight_source({}) == pondered["insight_text"]


def test_ponder_has_an_internal_fallback_and_accepts_explicit_text(tmp_path: Path):
    toolbox = CreativeToolbox(tmp_path)

    pondered = toolbox.call("creative_ponder", {"insight_text": "A key remembers the sea."})

    assert pondered["method"] == "internal"
    assert "A key remembers the sea." in pondered["reflection"]


def test_state_survives_reload_and_corruption_is_tolerated(tmp_path: Path):
    path = tmp_path / "creative" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text("[]", encoding="utf-8")
    toolbox = CreativeToolbox(tmp_path)
    meditation = toolbox.call("creative_meditate", {"seed": "persist"})
    toolbox.call("creative_insight", {})

    reloaded = CreativeToolbox(tmp_path)
    insight = reloaded.call("creative_insight", {})
    assert insight["meditation_id"] == meditation["id"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["last_insight_id"] == insight["id"]


def test_concurrent_meditations_persist_without_losing_records(tmp_path: Path):
    toolbox = CreativeToolbox(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(
            lambda seed: toolbox.call("creative_meditate", {"seed": str(seed)}),
            range(8),
        ))

    reloaded = CreativeToolbox(tmp_path)
    raw = json.loads((tmp_path / "creative" / "state.json").read_text(encoding="utf-8"))
    assert len(raw["meditations"]) == 8
    assert raw["last_meditation_id"] in {record["id"] for record in records}
    assert reloaded._last_meditation_id == raw["last_meditation_id"]


def test_unknown_tool_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="Unknown creative tool"):
        CreativeToolbox(tmp_path).call("not_a_tool", {})
