"""The turn engine: alternation, silence, cooldown, and natural stops."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from heyallm.engine import END_CAST, END_NATURAL, END_STOPPED, END_TURNS, SceneEngine
from heyallm.llm import LLMResult
from heyallm.memory import Chronicle
from heyallm.transcript import Transcript
from fakes import FakeLLM


def _scene(config) -> dict:
    return {
        "id": "scene-1",
        "title": "The lighthouse",
        "setting": "A lighthouse stairwell, 3am",
        "character_ids": ["char-alice", "char-bob"],
        "created_at": "2026-11-23T11:00:00",
        "status": "stopped",
    }


def make_engine(config, llm):
    scene = _scene(config)
    transcript = Transcript(config.scene_path("scene-1"), scene=scene)
    return SceneEngine(config, llm, transcript, scene), transcript


def test_turns_alternate_between_the_two_characters(cast_config, alice, bob):
    llm = FakeLLM(['*sets down the lamp* "You came."', '"Storm\'s not done."'])
    engine, transcript = make_engine(cast_config, llm)

    first = engine.step()
    second = engine.step()

    assert first.character_id == alice.id
    assert second.character_id == bob.id
    assert [e.text for e in transcript.iter_messages()] == [
        '*sets down the lamp* "You came."',
        '"Storm\'s not done."',
    ]
    # Each call saw its own endpoint.
    assert llm.calls[0]["endpoint"].model == "alice-model"
    assert llm.calls[1]["endpoint"].model == "bob-model"


def test_first_speaker_is_configurable(cast_config, alice, bob):
    cast_config.engine.first_speaker = 1
    llm = FakeLLM(['"hi"', '"hello"'])
    engine, _ = make_engine(cast_config, llm)

    assert engine.step().character_id == bob.id
    assert engine.step().character_id == alice.id


def test_each_character_sees_the_other_on_the_user_side(cast_config, alice, bob):
    llm = FakeLLM(['*nods* "A."', '"B."', '"C."'])
    engine, _ = make_engine(cast_config, llm)
    engine.step()
    engine.step()
    engine.step()

    # Third call is Alice again; Bob's line must be a user message for her.
    alice_view = llm.calls[2]["messages"]
    user_contents = [m["content"] for m in alice_view if m["role"] == "user"]
    assistant_contents = [m["content"] for m in alice_view if m["role"] == "assistant"]

    assert '"B."' in user_contents
    assert '*nods* "A."' in assistant_contents
    assert '"C."' not in user_contents
    # And the system prompt is the first message.
    assert alice_view[0]["role"] == "system"


def test_silence_sentinel_stops_posting(cast_config):
    cast_config.engine.max_consecutive_silences = 2
    llm = FakeLLM(["[[SILENT]]", "[[SILENT]]", "[[SILENT]]"])
    engine, transcript = make_engine(cast_config, llm)

    engine.start()
    engine.join(timeout=10)

    assert engine.end_reason == END_NATURAL
    assert transcript.count_kind("message") == 0
    assert transcript.count_kind("silence") == 2
    assert len(llm.calls) == 2  # the third check breaks before another call


def test_silence_tool_stops_posting(cast_config):
    cast_config.engine.max_consecutive_silences = 1
    silent = LLMResult(text="", tool_calls=[{"function": {"name": "stay_silent"}}])
    llm = FakeLLM([silent])
    engine, transcript = make_engine(cast_config, llm)

    engine.start()
    engine.join(timeout=10)

    assert engine.end_reason == END_NATURAL
    assert transcript.count_kind("silence") == 1
    # The tool was offered on the wire.
    assert "tools" in llm.calls[0]
    assert llm.calls[0]["tools"][0]["function"]["name"] == "stay_silent"


def test_silence_then_speech_resets_the_counter(cast_config):
    cast_config.engine.max_consecutive_silences = 2
    llm = FakeLLM(["[[SILENT]]", '*looks up* "Yes?"', "[[SILENT]]", "[[SILENT]]"])
    engine, transcript = make_engine(cast_config, llm)

    engine.start()
    engine.join(timeout=10)

    assert engine.end_reason == END_NATURAL
    assert transcript.count_kind("message") == 1
    assert transcript.count_kind("silence") == 3


def test_narration_reopens_a_naturally_stopped_scene(cast_config, alice, bob):
    cast_config.engine.max_consecutive_silences = 1
    llm = FakeLLM(["[[SILENT]]", '*stirs* "Still here."', "[[SILENT]]"])
    engine, transcript = make_engine(cast_config, llm)

    engine.start()
    engine.join(timeout=10)
    assert engine.end_reason == END_NATURAL

    transcript.add(kind="narration", text="The wind shifts.")
    engine.note_narration()
    engine.start()
    engine.join(timeout=10)

    assert transcript.count_kind("message") == 1
    narrated = [
        m["content"]
        for m in llm.calls[1]["messages"]
        if m["role"] == "user" and "wind shifts" in m["content"]
    ]
    assert len(narrated) == 1
    assert narrated[0].startswith(cast_config.narration.prefix)


def test_cooldown_spaces_out_posted_messages(cast_config):
    cast_config.cooldown.min_seconds = 0.15
    cast_config.cooldown.max_seconds = 5.0
    cast_config.cooldown.chars_per_second = 100_000.0

    llm = FakeLLM(['*first* "one."', '*second* "two."', '*third* "three."'])
    engine, transcript = make_engine(cast_config, llm)

    engine.step()  # first message: nothing preceding it, no wait
    first_ts = transcript.last_message_at()

    engine.step()
    second_ts = transcript.last_message_at()

    engine.step()
    third_ts = transcript.last_message_at()

    assert second_ts - first_ts >= 0.12
    assert third_ts - second_ts >= 0.12


def test_first_message_is_not_delayed(cast_config):
    cast_config.cooldown.min_seconds = 5.0
    cast_config.cooldown.max_seconds = 5.0
    llm = FakeLLM(['*nods* "One."'])
    engine, transcript = make_engine(cast_config, llm)

    start = time.time()
    engine.step()
    assert time.time() - start < 1.0
    assert transcript.count_kind("message") == 1


def test_longer_messages_read_longer(cast_config):
    cast_config.cooldown.chars_per_second = 10.0
    cast_config.cooldown.min_seconds = 0.0
    cast_config.cooldown.max_seconds = 100.0

    assert cast_config.cooldown.delay_for("x" * 100) == pytest.approx(10.0)
    assert cast_config.cooldown.delay_for("x" * 10) == pytest.approx(1.0)
    # Bounded on both ends.
    assert cast_config.cooldown.delay_for("x" * 10_000) == pytest.approx(100.0)


def test_turn_cap_ends_the_scene(cast_config):
    cast_config.engine.max_turns = 3
    llm = FakeLLM(['*a* "1."', '*b* "2."', '*c* "3."', '*d* "4."'])
    engine, transcript = make_engine(cast_config, llm)

    engine.start()
    engine.join(timeout=10)

    assert engine.end_reason == END_TURNS
    assert transcript.count_kind("message") == 3


def test_missing_character_ends_the_scene(cast_config):
    scene = _scene(cast_config)
    scene["character_ids"] = ["char-alice", "char-gone"]
    transcript = Transcript(cast_config.scene_path("scene-1"), scene=scene)
    engine = SceneEngine(cast_config, FakeLLM(), transcript, scene)

    engine.start()
    engine.join(timeout=10)

    assert engine.end_reason == END_CAST
    assert len(transcript) == 0


def test_status_reflects_running_state(cast_config):
    llm = FakeLLM()
    engine, _ = make_engine(cast_config, llm)

    assert engine.status()["running"] is False
    assert engine.status()["next_speaker"] == "char-alice"

    engine.start()
    engine.join(timeout=10)
    assert engine.status()["running"] is False
    assert engine.end_reason is not None


def test_episodic_memory_is_injected_into_the_prompt(cast_config, alice, bob):
    chronicle = Chronicle(
        scene_id="scene-1",
        text="Mara told Grey the lamp was broken. He counted the stairs instead.",
        compacted_upto=3,
    )
    chronicle.save(cast_config.record_path("scene-1"))

    llm = FakeLLM(['*checks the lamp* "Still dead."'])
    engine, _ = make_engine(cast_config, llm)
    engine.step()

    system = llm.calls[0]["messages"][0]["content"]
    assert "Mara told Grey the lamp was broken." in system


def test_memory_from_unrelated_scenes_is_excluded(cast_config, alice, bob):
    other = {"id": "scene-other", "title": "T", "setting": "", "character_ids": ["char-bob"]}
    from heyallm.transcript import write_scene_meta

    write_scene_meta(cast_config.scenes_dir, other)
    Chronicle(scene_id="scene-other", text="SECRET CHRONICLE.").save(
        cast_config.record_path("scene-other")
    )

    llm = FakeLLM(['*nods* "Right."'])
    engine, _ = make_engine(cast_config, llm)
    engine.step()

    assert "SECRET CHRONICLE." not in llm.calls[0]["messages"][0]["content"]


def test_empty_model_output_is_recorded_as_silence(cast_config):
    cast_config.engine.max_consecutive_silences = 1
    llm = FakeLLM(["   "])
    engine, transcript = make_engine(cast_config, llm)

    engine.start()
    engine.join(timeout=10)

    assert transcript.count_kind("silence") == 1
    assert transcript.count_kind("message") == 0


def test_current_chronicle_replaces_compacted_raw_events(cast_config):
    scene = _scene(cast_config)
    transcript = Transcript(cast_config.scene_path("scene-1"), scene=scene)
    transcript.add(kind="scene", text="opening")
    transcript.add(kind="message", text="RAW OLD EVENT", character_id="char-alice")
    Chronicle(
        scene_id="scene-1", text="SUMMARY OF OLD EVENT", compacted_upto=2,
    ).save(cast_config.record_path("scene-1"))
    llm = FakeLLM(['"Next."'])
    engine = SceneEngine(cast_config, llm, transcript, scene)

    engine.step()

    messages = llm.calls[0]["messages"]
    assert "SUMMARY OF OLD EVENT" in messages[0]["content"]
    assert all("RAW OLD EVENT" not in m["content"] for m in messages)


def test_cooldown_uses_the_previous_messages_length(cast_config, monkeypatch):
    delays = []
    monkeypatch.setattr(
        cast_config.cooldown,
        "delay_for",
        lambda text: delays.append(text) or 0.0,
    )
    first = '*a long first turn* "' + ("x" * 100) + '"'
    llm = FakeLLM([first, '"short"'])
    engine, _ = make_engine(cast_config, llm)

    engine.step()
    engine.step()

    assert delays == [first]


def test_explicit_restart_resets_the_turn_cap(cast_config):
    cast_config.engine.max_turns = 1
    llm = FakeLLM(['"one"', '"two"'])
    engine, transcript = make_engine(cast_config, llm)

    engine.start()
    engine.join(timeout=10)
    assert engine.end_reason == END_TURNS

    engine.start()
    engine.join(timeout=10)

    assert engine.end_reason == END_TURNS
    assert transcript.count_kind("message") == 2
    assert len(llm.calls) == 2


def test_stop_during_a_turn_is_reported_as_stopped(cast_config):
    llm = FakeLLM()
    engine, transcript = make_engine(cast_config, llm)
    engine.step()

    def stop_then_reply(*_args, **_kwargs):
        engine.stop()
        return '"never posted"'

    llm.responses = [stop_then_reply]
    engine.start()
    engine.join(timeout=10)

    assert engine.end_reason == END_STOPPED
    assert transcript.count_kind("message") == 1


def test_director_requested_stop_is_a_natural_stop(cast_config):
    cast_config.engine.director_end_check = True
    llm = FakeLLM(['"Goodnight."', "YES"])
    engine, transcript = make_engine(cast_config, llm)
    for i in range(3):
        transcript.add(kind="message", text=f"earlier {i}", character_id="char-alice")

    engine.start()
    engine.join(timeout=10)

    assert engine.end_reason == END_NATURAL
    assert transcript.count_kind("message") == 4


def test_concurrent_manual_steps_do_not_take_two_turns(cast_config):
    started = threading.Event()
    release = threading.Event()
    results = []

    def slow_reply(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=5)
        return '"one"'

    engine, transcript = make_engine(cast_config, FakeLLM([slow_reply]))
    first = threading.Thread(target=lambda: results.append(engine.step()))
    first.start()
    assert started.wait(timeout=5)

    assert engine.step() is None
    release.set()
    first.join(timeout=5)

    assert len(results) == 1
    assert transcript.count_kind("message") == 1
