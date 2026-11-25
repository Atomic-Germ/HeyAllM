"""Prompt construction: whose side is whose, and how narration is framed."""

from __future__ import annotations

from heyallm import prompts as P
from heyallm.config import Config
from heyallm.transcript import Event


def _events(alice_id: str, bob_id: str) -> list[dict]:
    return [
        Event(kind="scene", text="The lighthouse").to_dict(),
        Event(kind="message", character_id=alice_id, text='*sets down the lamp* "You came."').to_dict(),
        Event(kind="message", character_id=bob_id, text='"Storm\'s not done."').to_dict(),
        Event(kind="narration", text="The generator coughs and dies.").to_dict(),
        Event(kind="silence", character_id=bob_id).to_dict(),
        Event(kind="message", character_id=alice_id, text='*does not turn around*').to_dict(),
    ]


def test_own_lines_are_assistant_and_theirs_are_user(cast_config: Config, alice, bob):
    messages = P.build_messages(cast_config, alice, bob, "a lighthouse", _events(alice.id, bob.id))

    assert messages[0]["role"] == "system"
    body = [m for m in messages if m["role"] != "system"]

    own = [m for m in body if m["role"] == "assistant"]
    theirs = [m for m in body if m["role"] == "user"]

    assert [m["content"] for m in own] == [
        '*sets down the lamp* "You came."',
        "*does not turn around*",
    ]
    # The counterpart's line, the narration, and the beat of silence all land on
    # the user side -- to Alice, Bob occupies the human's side of the exchange.
    assert '"Storm\'s not done."' in [m["content"] for m in theirs]
    assert len(theirs) == 3


def test_view_is_mirrored_for_the_other_character(cast_config: Config, alice, bob):
    messages = P.build_messages(cast_config, bob, alice, "a lighthouse", _events(alice.id, bob.id))
    body = [m for m in messages if m["role"] != "system"]

    own = [m["content"] for m in body if m["role"] == "assistant"]
    assert own == ['"Storm\'s not done."']
    assert '*sets down the lamp* "You came."' in [
        m["content"] for m in body if m["role"] == "user"
    ]


def test_narration_is_framed_not_quoted(cast_config: Config, alice, bob):
    messages = P.build_messages(cast_config, alice, bob, "s", _events(alice.id, bob.id))
    narration = [m for m in messages if m["role"] == "user" and "generator" in m["content"]]

    assert len(narration) == 1
    assert narration[0]["content"].startswith(cast_config.narration.prefix)
    assert "The generator coughs and dies." in narration[0]["content"]


def test_silence_is_an_unattributed_beat(cast_config: Config, alice, bob):
    messages = P.build_messages(cast_config, alice, bob, "s", _events(alice.id, bob.id))
    beats = [m["content"] for m in messages if "beat of silence" in m["content"]]

    assert len(beats) == 1
    # No name attached -- neither side looks like it spoke.
    assert "Bob" not in beats[0] and "Grey" not in beats[0] and "grey" not in beats[0]


def test_system_prompt_refuses_the_chat_framing(cast_config: Config, alice, bob):
    prompt = P.build_system_prompt(cast_config, alice, bob, "a lighthouse")

    assert "This is not a chat" in prompt
    assert "*Actions go in asterisks.*" in prompt
    assert cast_config.engine.silence_sentinel in prompt
    assert "stay_silent" in prompt
    assert cast_config.narration.prefix in prompt
    # Both identities and the counterpart are present.
    assert "Mara Ellison" in prompt and "mara" in prompt
    assert "Jonas Grey" in prompt and "grey" in prompt
    # Demographics and the character's own persona are folded in.
    assert "night-shift radio operator" in prompt
    assert "check the door before you answer" in prompt
    # The counterpart's inner persona stays private to them.
    assert "tap twice when lying" not in prompt


def test_disabled_silence_tool_is_not_advertised(cast_config: Config, alice, bob):
    cast_config.engine.use_silence_tool = False

    prompt = P.build_system_prompt(cast_config, alice, bob, "s")

    assert "stay_silent" not in prompt
    assert cast_config.engine.silence_sentinel in prompt


def test_counterpart_traits_are_visible_but_inner_life_is_not(cast_config: Config, alice, bob):
    bob.demographics["mannerisms"] = "taps twice when lying"
    bob.demographics["history"] = "raised in the harbour town"
    bob.demographics["wants"] = "to be left alone"

    prompt = P.build_system_prompt(cast_config, alice, bob, "s")

    # Observable in the scene: yes.
    assert "Mannerisms: taps twice when lying" in prompt
    # Inner life: no -- that is Bob's alone.
    assert "raised in the harbour town" not in prompt
    assert "to be left alone" not in prompt


def test_system_prompt_carries_episodic_memory(cast_config: Config, alice, bob):
    prompt = P.build_system_prompt(
        cast_config, alice, bob, "s", "Mara told Grey the lamp was broken."
    )
    assert "Mara told Grey the lamp was broken." in prompt


def test_detect_silence():
    assert P.detect_silence("[[SILENT]]", "[[SILENT]]")
    assert P.detect_silence("   [[SILENT]]  ", "[[SILENT]]")
    assert P.detect_silence("", "[[SILENT]]")
    assert P.detect_silence("I stay put. [[SILENT]]", "[[SILENT]]")
    assert not P.detect_silence('*turns away* "No."', "[[SILENT]]")
    assert P.strip_silence_sentinel("hmm [[SILENT]]", "[[SILENT]]") == "hmm"


def test_segments_split_actions_from_speech():
    text = '*leans against the wall* "You\'re late." *checks his watch*'
    parts = P.segments(text)

    assert parts[0] == ("action", "leans against the wall")
    assert parts[1][0] == "text"
    assert "You're late." in parts[1][1]
    assert parts[2] == ("action", "checks his watch")
    assert P.is_action_only("*sits down* *says nothing*")
    assert not P.is_action_only('*sits down* "hi"')
    assert P.speech_of(text) == ["You're late."]
