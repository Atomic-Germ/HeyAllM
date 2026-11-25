"""Transcript persistence and scene metadata."""

from __future__ import annotations

import json
from pathlib import Path

from heyallm.config import atomic_write
from heyallm.transcript import (
    Event,
    Transcript,
    delete_scene,
    load_scene_meta,
    write_scene_meta,
)


def test_append_then_reload(tmp_path: Path):
    path = tmp_path / "scene.jsonl"
    first = Transcript(path)
    first.add(kind="scene", text="opening")
    first.add(kind="message", text='*waves* "hi"', character_id="char-a", turn=1)
    first.add(kind="silence", character_id="char-b", turn=2)

    reloaded = Transcript(path)
    assert len(reloaded) == 3
    assert [e.kind for e in reloaded.events] == ["scene", "message", "silence"]
    assert reloaded.events[1].character_id == "char-a"
    assert reloaded.events[1].turn == 1


def test_torn_final_line_is_tolerated(tmp_path: Path):
    path = tmp_path / "scene.jsonl"
    Transcript(path).add(kind="message", text="hello", character_id="a")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "message", "text": "trunc')  # crash mid-write

    reloaded = Transcript(path)
    assert len(reloaded) == 1
    assert reloaded.events[0].text == "hello"

    reloaded.add(kind="message", text="after recovery", character_id="b", turn=2)
    again = Transcript(path)
    assert [e.text for e in again.events] == ["hello", "after recovery"]


def test_load_skips_malformed_records_without_losing_valid_events(tmp_path: Path):
    path = tmp_path / "scene.jsonl"
    first = Event(kind="message", text="before", character_id="a", turn=1)
    last = Event(kind="message", text="after", character_id="b", turn=2)
    malformed = [
        [],
        {"kind": "message", "text": "bad turn type", "turn": [1]},
        {"kind": "message", "text": "bad turn value", "turn": "not-an-int"},
        {"kind": "message", "text": "overflowing turn", "turn": float("inf")},
        {"kind": "message", "text": "bad timestamp type", "ts": {"invalid": True}},
        {"kind": "message", "text": "bad timestamp value", "ts": "not-a-float"},
    ]
    records = [first.to_dict(), *malformed, last.to_dict()]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    reloaded = Transcript(path)
    assert [e.text for e in reloaded.events] == ["before", "after"]


def test_malformed_identity_and_non_finite_timestamp_are_normalized():
    event = Event.from_dict({
        "kind": "message", "text": "hello", "character_id": ["bad"], "ts": float("nan"),
    })

    assert event.character_id == "['bad']"
    assert event.ts > 0


def test_event_ids_and_timestamps_survive_round_trip(tmp_path: Path):
    path = tmp_path / "scene.jsonl"
    original = Transcript(path).add(kind="message", text="x", character_id="a")
    again = Transcript(path).events[0]
    assert again.id == original.id
    assert again.ts == original.ts


def test_counters(tmp_path: Path):
    t = Transcript(tmp_path / "s.jsonl")
    t.add(kind="scene", text="start")
    t.add(kind="message", text="one", character_id="a", turn=1)
    t.add(kind="silence", character_id="b", turn=2)
    t.add(kind="message", text="two", character_id="b", turn=3)

    assert t.count_kind("message") == 2
    assert t.next_turn() == 4
    assert [e.text for e in t.iter_messages()] == ["one", "two"]
    assert t.last_message_at() > 0


def test_next_turn_counts_silence_but_not_scene_or_narration(tmp_path: Path):
    t = Transcript(tmp_path / "s.jsonl")
    t.add(kind="scene", text="start")
    t.add(kind="narration", text="The wind rises.")
    assert t.next_turn() == 1

    t.add(kind="message", text="one", character_id="a", turn=1)
    t.add(kind="narration", text="A door closes.")
    assert t.next_turn() == 2

    t.add(kind="silence", character_id="b", turn=2)
    assert t.next_turn() == 3


def test_last_message_at_is_zero_when_empty(tmp_path: Path):
    t = Transcript(tmp_path / "s.jsonl")
    t.add(kind="scene", text="start")
    assert t.last_message_at() == 0.0


def test_since_returns_only_new_events(tmp_path: Path):
    t = Transcript(tmp_path / "s.jsonl")
    for i in range(5):
        t.add(kind="message", text=str(i), character_id="a")
    assert len(t.since(3)) == 2
    assert len(t.since(0)) == 5
    assert len(t.since(99)) == 0


def test_scene_metadata_round_trip(tmp_path: Path):
    scene = {"id": "scene-1", "title": "T", "character_ids": ["a", "b"], "created_at": "now"}
    write_scene_meta(tmp_path, scene)

    metas = load_scene_meta(tmp_path)
    assert len(metas) == 1
    assert metas[0]["id"] == "scene-1"

    assert delete_scene(tmp_path, "scene-1")
    assert load_scene_meta(tmp_path) == []
    # The transcript file goes too.
    atomic_write(tmp_path / "scene-1.jsonl", "{}\n")
    assert delete_scene(tmp_path, "scene-1")
    assert not (tmp_path / "scene-1.jsonl").exists()


def test_unknown_event_kind_falls_back_to_message():
    assert Event.from_dict({"kind": "shout", "text": "x"}).kind == "message"
