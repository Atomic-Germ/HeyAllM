"""The director: parsing and applying what the third model writes."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from heyallm.config import Character, Endpoint, blank_character
from heyallm.creative import CreativeToolbox
from heyallm.director import _extract_json, _normalise, apply_to_character, create_character
from heyallm.llm import LLMClient, LLMResult, LLMError
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


def _tool_call(name, args, call_id="call-1"):
    return LLMResult(text="", tool_calls=[{
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }])


def test_director_can_run_the_full_creative_pipeline(tmp_path):
    toolbox = CreativeToolbox(tmp_path)
    llm = FakeLLM([
        _tool_call("creative_meditate", {"context_words": ["lighthouse"], "seed": "storm"}, "m1"),
        _tool_call("creative_insight", {}, "i1"),
        _tool_call("creative_ponder", {}, "p1"),
        "The quiet image opens onto the sea.",
        PAYLOAD,
    ])

    filled = create_character(
        llm, Endpoint(), creative=toolbox, max_tool_calls=3, character_endpoint=Endpoint(model="player"),
    )

    assert filled["screenname"] == "maraonair"
    assert len(llm.calls) == 5  # three tool rounds, one pondering reflection, final JSON
    assert llm.calls[0]["tools"][0]["function"]["name"] == "creative_meditate"
    assert any(message["role"] == "tool" for message in llm.calls[1]["messages"])
    assert "creative meditation tools" in llm.calls[0]["messages"][0]["content"]
    state = json.loads((tmp_path / "creative" / "state.json").read_text(encoding="utf-8"))
    assert state["ponderings"][-1]["method"] == "director"


def test_tool_errors_are_returned_to_the_director_for_recovery(tmp_path):
    llm = FakeLLM([
        LLMResult(text="", tool_calls=[{
            "id": "bad", "function": {"name": "creative_meditate", "arguments": "{bad"},
        }]),
        PAYLOAD,
    ])

    filled = create_character(llm, Endpoint(), creative=CreativeToolbox(tmp_path))

    assert filled["screenname"] == "maraonair"
    tool_message = next(m for m in llm.calls[1]["messages"] if m["role"] == "tool")
    assert "valid JSON" in tool_message["content"]


def test_director_tool_loop_is_bounded(tmp_path):
    llm = FakeLLM([
        _tool_call("creative_meditate", {"seed": "one"}, "m1"),
        _tool_call("creative_meditate", {"seed": "two"}, "m2"),
    ])

    with pytest.raises(LLMError, match="1-call creative tool limit"):
        create_character(llm, Endpoint(), creative=CreativeToolbox(tmp_path), max_tool_calls=1)

    assert len(llm.calls) == 2
    state = json.loads((tmp_path / "creative" / "state.json").read_text(encoding="utf-8"))
    assert len(state["meditations"]) == 1


def test_tool_loop_works_through_the_real_openai_compatible_transport(tmp_path):
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):  # noqa: N802
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            payloads.append(payload)
            has_tool_result = any(m["role"] == "tool" for m in payload["messages"])
            if has_tool_result:
                message = {"role": "assistant", "content": PAYLOAD}
                finish = "stop"
            else:
                message = {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "http-call", "type": "function",
                        "function": {
                            "name": "creative_meditate",
                            "arguments": json.dumps({"context_words": ["harbor"], "seed": "http"}),
                        },
                    }],
                }
                finish = "tool_calls"
            body = json.dumps({
                "model": "director", "choices": [{"message": message, "finish_reason": finish}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = Endpoint(base_url=f"http://127.0.0.1:{httpd.server_address[1]}/v1", model="director")
        filled = create_character(LLMClient(), endpoint, creative=CreativeToolbox(tmp_path))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    assert filled["screenname"] == "maraonair"
    assert len(payloads) == 2
    assert payloads[0]["tools"][0]["function"]["name"] == "creative_meditate"
    assert payloads[1]["messages"][-1]["role"] == "tool"
    assert payloads[1]["messages"][-1]["tool_call_id"] == "http-call"
