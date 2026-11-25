"""The director: parsing and applying what the third model writes."""

from __future__ import annotations

import pytest

from heyallm.config import Character, Endpoint, blank_character
from heyallm.director import _extract_json, _normalise, apply_to_character, create_character
from heyallm.llm import LLMResult, LLMError
from fakes import FakeLLM


PAYLOAD = """{
  "screenname": "maraonair",
  "real_name": "Mara Ellison",
  "demographics": {"age": "29", "occupation": "night-shift radio operator", "wants": "the truth"},
  "persona": "You keep your voice low. You check the door before you answer."
}"""


def test_extract_json_from_clean_output():
    assert _extract_json(PAYLOAD)["screenname"] == "maraonair"


def test_extract_json_strips_code_fences():
    fenced = "```json\n" + PAYLOAD + "\n```"
    assert _extract_json(fenced)["real_name"] == "Mara Ellison"


def test_extract_json_survives_chatty_wrapping():
    chatty = "Sure! Here is the character:\n" + PAYLOAD + "\nHope that helps."
    assert _extract_json(chatty)["screenname"] == "maraonair"


def test_extract_json_rejects_non_json():
    with pytest.raises(LLMError):
        _extract_json("I cannot do that.")
    with pytest.raises(LLMError):
        _extract_json('{"half":')


def test_normalise_guarantees_the_standard_fields():
    out = _normalise({"screenname": "grey", "real_name": "Jonas Grey",
                      "demographics": {"age": "41", "junk": ""}})
    assert out["screenname"] == "grey"
    assert out["demographics"]["age"] == "41"
    assert "junk" not in out["demographics"]  # blank junk from the model is dropped

    # The standard set is always present, even when the director omits it.
    filled = _normalise({})
    for key in ("age", "gender", "occupation", "location", "appearance",
                "mannerisms", "speech_style", "history", "wants"):
        assert key in filled["demographics"]
    assert filled["persona"] == ""


def test_create_character_calls_the_director():
    llm = FakeLLM([PAYLOAD])
    endpoint = Endpoint(base_url="http://127.0.0.1:9999/v1", model="director-model")

    filled = create_character(
        llm, endpoint, seed="a barista who owes money",
        character_endpoint=Endpoint(model="play-model"),
    )

    assert filled["screenname"] == "maraonair"
    assert filled["real_name"] == "Mara Ellison"
    assert filled["demographics"]["occupation"] == "night-shift radio operator"

    call = llm.calls[0]
    assert call["endpoint"] is endpoint
    assert call["temperature"] == 1.0
    system = call["messages"][0]["content"]
    assert system.startswith("You create people for a scene.")
    user = call["messages"][1]["content"]
    assert "a barista who owes money" in user
    assert "play-model" in user  # the director knows what will play the part


def test_create_character_with_no_seed():
    llm = FakeLLM([PAYLOAD])
    create_character(llm, Endpoint(), seed="")
    assert "invent someone entirely" in llm.calls[0]["messages"][1]["content"]


def test_apply_to_character_keeps_existing_values():
    char = blank_character()
    char.screenname = "keepme"
    apply_to_character(char, {"screenname": "newname", "real_name": "", "persona": "", "demographics": {}})

    assert char.screenname == "newname"
    # Empty director output never wipes what was already there.
    char2 = blank_character()
    char2.real_name = "Original"
    apply_to_character(char2, {"screenname": "", "real_name": "", "persona": ""})
    assert char2.real_name == "Original"


def test_apply_to_character_replaces_demographics():
    char = Character(id="c", demographics={"age": "99"})
    apply_to_character(char, {"demographics": {"age": "29", "wants": "home"}})
    assert char.demographics == {"age": "29", "wants": "home"}


def test_apply_to_character_preserves_omitted_demographics():
    char = Character(id="c", demographics={"age": "99", "history": "kept"})
    apply_to_character(char, _normalise({"screenname": "newhandle"}))

    assert char.demographics["age"] == "99"
    assert char.demographics["history"] == "kept"
    assert char.screenname == "newhandle"
