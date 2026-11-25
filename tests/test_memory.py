"""Chronicle compaction: third-person voice, no first-person narrator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heyallm.config import Character, Endpoint
from heyallm.llm import LLMError, LLMResult
from heyallm.memory import (
    Chronicle,
    compact,
    format_for_character,
    is_third_person,
    render_events,
)
from heyallm.transcript import Event
from fakes import FakeLLM


def test_third_person_voice_passes():
    assert is_third_person("Mara told Grey that the lamp was broken. He did not answer.")
    assert is_third_person('Mara said, "I\'ll fix it myself." He turned away.')


def test_first_person_voice_fails():
    assert not is_third_person("I walked to the door and waited for him.")
    assert not is_third_person("We had agreed to meet. My hands were shaking.")
    # Even with quotes present elsewhere, the narrator's own "I" still counts.
    assert not is_third_person('"Fine," she said. I did not believe her.')


def test_render_events_names_the_speakers(alice, bob):
    events = [
        Event(kind="scene", text="Lighthouse", ts=0),
        Event(kind="message", text='*waves* "hi"', character_id=alice.id, ts=0),
        Event(kind="narration", text="The generator dies.", ts=0),
        Event(kind="silence", character_id=bob.id, ts=0),
    ]
    body = render_events(events, {alice.id: alice, bob.id: bob})

    assert "Mara Ellison says *waves* \"hi\"" in body
    assert "(narration) The generator dies." in body
    assert "Jonas Grey stays silent." in body
    assert "(scene opens) Lighthouse" in body


def test_render_events_handles_unknown_character():
    body = render_events(
        [Event(kind="message", text="hello", character_id="char-ghost", ts=0)], {}
    )
    assert "char-ghost says hello" in body


def test_compact_returns_director_text(alice, bob):
    llm = FakeLLM(["Mara left the lamp on the step. Grey never knocked."])
    chronicle = Chronicle(scene_id="scene-1")
    pending = [Event(kind="message", text="hi", character_id=alice.id)]

    text = compact(
        llm, Endpoint(base_url="http://x/v1", model="d"), chronicle, pending,
        {alice.id: alice, bob.id: bob},
    )

    assert text == "Mara left the lamp on the step. Grey never knocked."
    assert is_third_person(text)
    # The existing chronicle and the new events are both in the prompt.
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "EXISTING CHRONICLE" in prompt
    assert "NEW EVENTS" in prompt
    assert "Mara Ellison says hi" in prompt


def test_compact_retries_when_director_goes_first_person(alice):
    llm = FakeLLM([
        "I walked to the door and waited.",
        "Mara walked to the door and waited.",
    ])
    chronicle = Chronicle()
    text = compact(
        llm, Endpoint(), chronicle, [Event(kind="message", text="x", character_id=alice.id)],
        {alice.id: alice},
    )

    assert text == "Mara walked to the door and waited."
    assert len(llm.calls) == 2
    assert "strictly third-person voice" in llm.calls[1]["messages"][-1]["content"]


def test_compact_raises_on_empty_response(alice):
    llm = FakeLLM([""])
    with pytest.raises(LLMError):
        compact(
            llm, Endpoint(), Chronicle(), [Event(kind="message", text="x")], {alice.id: alice}
        )


def test_compact_rejects_first_person_after_retry(alice):
    llm = FakeLLM(["I waited by the door.", "We stayed in the dark."])
    with pytest.raises(LLMError, match="third-person"):
        compact(
            llm, Endpoint(), Chronicle(), [Event(kind="message", text="x")], {alice.id: alice}
        )
    assert len(llm.calls) == 2


def test_compact_short_circuits_when_nothing_pending(alice):
    llm = FakeLLM(["should not be used"])
    chronicle = Chronicle(text="Already written.")
    assert compact(llm, Endpoint(), chronicle, [], {alice.id: alice}) == "Already written."
    assert llm.calls == []


def test_chronicle_persistence(tmp_path: Path):
    path = tmp_path / "scene-1.json"
    chronicle = Chronicle(scene_id="scene-1", text="Mara waited.", compacted_upto=4)
    chronicle.save(path)

    again = Chronicle.load(path)
    assert again.text == "Mara waited."
    assert again.compacted_upto == 4

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["scene_id"] == "scene-1"


def test_missing_chronicle_is_empty(tmp_path: Path):
    assert Chronicle.load(tmp_path / "nope.json").text == ""
    assert Chronicle.load(tmp_path / "nope.json").compacted_upto == 0


@pytest.mark.parametrize("data", [[], None, "not-an-object", 42])
def test_chronicle_load_tolerates_non_object_json(tmp_path: Path, data):
    path = tmp_path / "scene-1.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    chronicle = Chronicle.load(path)
    assert chronicle.scene_id == ""
    assert chronicle.text == ""
    assert chronicle.compacted_upto == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("compacted_upto", "not-an-int"),
        ("compacted_upto", [1]),
        ("compacted_upto", float("inf")),
        ("updated_at", {"invalid": True}),
        ("updated_at", "not-a-float"),
    ],
)
def test_chronicle_load_tolerates_malformed_numeric_fields(tmp_path: Path, field, value):
    path = tmp_path / "scene-1.json"
    data = {"scene_id": "scene-1", "text": "Mara waited.", field: value}
    path.write_text(json.dumps(data), encoding="utf-8")

    chronicle = Chronicle.load(path)
    assert chronicle.scene_id == ""
    assert chronicle.text == ""
    assert chronicle.compacted_upto == 0


def test_non_finite_chronicle_timestamp_is_normalized(tmp_path: Path):
    path = tmp_path / "scene.json"
    path.write_text(json.dumps({"text": "Mara waited.", "updated_at": float("nan")}), encoding="utf-8")

    chronicle = Chronicle.load(path)

    assert chronicle.text == "Mara waited."
    assert chronicle.updated_at > 0


def test_format_for_character_joins_records(alice):
    records = [Chronicle(text="One."), Chronicle(text="Two."), Chronicle(text="   ")]
    assert format_for_character(records, alice) == "One.\n\nTwo."
    assert format_for_character([], alice) == ""
