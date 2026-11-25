"""The HTTP surface: REST endpoints and the SSE stream."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from heyallm.server import App, STATIC_DIR, serve


@pytest.fixture
def app(cast_config):
    return App(config=cast_config)


@pytest.fixture
def server(app):
    httpd = serve(app=app, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    app.base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield app
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def request(app, method: str, path: str, body=None, expect: int = 200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        app.base_url + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status, payload = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    parsed = json.loads(payload) if payload else {}
    assert status == expect, f"{method} {path} -> {status}: {payload[:300]!r}"
    return parsed


# --------------------------------------------------------------------- basics
def test_health(server):
    assert request(server, "GET", "/api/health") == {"ok": True}


def test_unknown_route_is_404(server):
    request(server, "GET", "/api/nope", expect=404)
    request(server, "POST", "/api/nope", {}, expect=404)


def test_state_contains_config_characters_and_scenes(server, alice, bob):
    state = request(server, "GET", "/api/state")

    assert state["running"] is False
    assert state["active_scene"] is None
    assert {c["id"] for c in state["characters"]} == {"char-alice", "char-bob"}
    assert state["config"]["cooldown"]["min_seconds"] == 0.0
    # Endpoint fields survive serialization as plain objects.
    assert state["characters"][0]["endpoint"]["model"] in ("alice-model", "bob-model")
    assert isinstance(state["scenes"], list)


def test_serves_the_ui_when_present(server):
    index = STATIC_DIR / "index.html"
    if not index.exists():
        pytest.skip("UI not built yet")
    req = urllib.request.Request(server.base_url + "/")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        assert b"<html" in resp.read(200).lower()


def test_static_traversal_is_blocked(server):
    request(server, "GET", "/static/../config.json", expect=404)


# ----------------------------------------------------------------- characters
def test_create_character_without_the_director(server):
    created = request(
        server, "POST", "/api/characters",
        {"generate": False, "prompt_extra": "a barista", "endpoint": {"model": "new-model"}},
        expect=201,
    )["character"]

    assert created["id"].startswith("char-")
    assert created["prompt_extra"] == "a barista"
    assert created["endpoint"]["model"] == "new-model"

    listed = request(server, "GET", "/api/characters")["characters"]
    assert created["id"] in [c["id"] for c in listed]


def test_create_character_asks_the_director(server, app):
    from fakes import FakeLLM

    app.llm = FakeLLM([
        json.dumps({
            "screenname": "harbourgrey",
            "real_name": "Jonas Grey",
            "demographics": {"age": "41"},
            "persona": "You tap twice when lying.",
        })
    ])
    character = request(
        server, "POST", "/api/characters",
        {"generate": True, "endpoint": {"model": "played-by"}},
        expect=201,
    )["character"]

    assert character["screenname"] == "harbourgrey"
    assert character["real_name"] == "Jonas Grey"
    assert character["persona"] == "You tap twice when lying."


def test_update_character(server):
    target = request(server, "GET", "/api/characters/char-alice")["character"]
    updated = request(
        server, "PUT", "/api/characters/char-alice",
        {"screenname": "marae", "endpoint": {"model": "renamed"}},
    )["character"]

    assert updated["screenname"] == "marae"
    assert updated["endpoint"]["model"] == "renamed"
    assert updated["real_name"] == target["real_name"]  # untouched fields stay


def test_update_unknown_character_is_404(server):
    request(server, "PUT", "/api/characters/char-nope", {"screenname": "x"}, expect=404)


def test_delete_character(server):
    assert request(server, "DELETE", "/api/characters/char-bob")["ok"] is True
    assert request(server, "GET", "/api/characters/char-bob", expect=404)
    request(server, "DELETE", "/api/characters/char-bob", expect=404)


def test_regenerate_uses_the_director(server, app):
    from fakes import FakeLLM

    app.llm = FakeLLM([json.dumps({
        "screenname": "onair", "real_name": "Mara Ellison",
        "demographics": {"age": "30"}, "persona": "Quieter now.",
    })])
    character = request(server, "POST", "/api/characters/char-alice/regenerate")["character"]

    assert character["id"] == "char-alice"          # identity preserved
    assert character["screenname"] == "onair"
    assert character["endpoint"]["model"] == "alice-model"  # endpoint preserved


def test_config_updates(server):
    saved = request(server, "POST", "/api/config", {
        "config": {
            "director": {"base_url": "http://127.0.0.1:5000/v1", "model": "dir-2"},
            "cooldown": {"chars_per_second": 30.0},
            "engine": {"max_turns": 9},
            "narration.prefix": "ignored",
            "narration": {"prefix": "[[world]]"},
            "memory": {"auto_compact": False},
        }
    })["config"]

    assert saved["director"]["model"] == "dir-2"
    assert saved["cooldown"]["chars_per_second"] == 30.0
    assert saved["engine"]["max_turns"] == 9
    assert saved["narration"]["prefix"] == "[[world]]"
    assert saved["memory"]["auto_compact"] is False


# -------------------------------------------------------------------- scenes
def test_create_scene_needs_two_distinct_characters(server):
    request(server, "POST", "/api/scenes", {"character_ids": ["char-alice"]}, expect=400)
    request(
        server, "POST", "/api/scenes",
        {"character_ids": ["char-alice", "char-alice"]}, expect=400,
    )
    request(
        server, "POST", "/api/scenes",
        {"character_ids": ["char-alice", "char-ghost"]}, expect=400,
    )


def test_scene_lifecycle(server):
    scene = request(
        server, "POST", "/api/scenes",
        {"character_ids": ["char-alice", "char-bob"],
         "title": "The lighthouse", "setting": "3am, storm"},
        expect=201,
    )["scene"]

    detail = request(server, "GET", f"/api/scenes/{scene['id']}")
    assert detail["scene"]["title"] == "The lighthouse"
    assert len(detail["events"]) == 1          # the opening scene event
    assert detail["events"][0]["kind"] == "scene"
    assert detail["memory"] == ""

    summaries = request(server, "GET", "/api/scenes")["scenes"]
    assert summaries[0]["event_count"] == 1
    assert summaries[0]["status"] == "stopped"

    assert request(server, "GET", "/api/scenes/scene-nope", expect=404) is not None


def test_narration_is_added(server):
    scene = request(
        server, "POST", "/api/scenes", {"character_ids": ["char-alice", "char-bob"]},
        expect=201,
    )["scene"]

    event = request(
        server, "POST", f"/api/scenes/{scene['id']}/events",
        {"text": "The generator coughs and dies."},
        expect=201,
    )["event"]
    assert event["kind"] == "narration"
    assert event["character_id"] is None

    detail = request(server, "GET", f"/api/scenes/{scene['id']}")
    assert detail["events"][-1]["text"] == "The generator coughs and dies."


def test_director_narrates_as_narrative(server, app):
    from fakes import FakeLLM

    scene = request(
        server, "POST", "/api/scenes", {"character_ids": ["char-alice", "char-bob"]},
        expect=201,
    )["scene"]
    app.llm = FakeLLM(["The rain finds a way under the door."])

    event = request(server, "POST", f"/api/scenes/{scene['id']}/narrate", {}, expect=201)["event"]

    assert event["kind"] == "narration"
    assert event["character_id"] is None
    assert event["text"] == "The rain finds a way under the door."
    prompt = app.llm.calls[0]["messages"][0]["content"]
    assert "narrator of a scene" in prompt
    assert "Scene:" in prompt   # scene heading reaches the director

    detail = request(server, "GET", f"/api/scenes/{scene['id']}")
    assert detail["events"][-1]["kind"] == "narration"


def test_director_narrates_with_a_hint(server, app):
    from fakes import FakeLLM

    scene = request(
        server, "POST", "/api/scenes", {"character_ids": ["char-alice", "char-bob"]},
        expect=201,
    )["scene"]
    app.llm = FakeLLM(["A gull lands on the railing."])

    request(server, "POST", f"/api/scenes/{scene['id']}/narrate",
            {"hint": "something outside the window"}, expect=201)

    prompt = app.llm.calls[0]["messages"][0]["content"]
    assert "something outside the window" in prompt


def test_director_narrate_empty_response_is_502(server, app):
    from fakes import FakeLLM

    scene = request(
        server, "POST", "/api/scenes", {"character_ids": ["char-alice", "char-bob"]},
        expect=201,
    )["scene"]
    app.llm = FakeLLM(["   "])

    request(server, "POST", f"/api/scenes/{scene['id']}/narrate", {}, expect=502)


def test_empty_narration_is_rejected(server):
    scene = request(
        server, "POST", "/api/scenes", {"character_ids": ["char-alice", "char-bob"]},
        expect=201,
    )["scene"]
    request(server, "POST", f"/api/scenes/{scene['id']}/events", {"text": "   "}, expect=400)


def test_step_posts_exactly_one_turn(server):
    from fakes import FakeLLM

    scene = request(
        server, "POST", "/api/scenes", {"character_ids": ["char-alice", "char-bob"]},
        expect=201,
    )["scene"]
    server.llm = FakeLLM(['*sets down the lamp* "You came."'])

    event = request(server, "POST", f"/api/scenes/{scene['id']}/step")["event"]
    assert event["kind"] == "message"
    assert event["character_id"] == "char-alice"

    detail = request(server, "GET", f"/api/scenes/{scene['id']}")
    assert detail["events"][-1]["kind"] == "message"
    assert detail["events"][-1]["character_id"] == "char-alice"


def test_step_is_refused_while_running(server, app):
    from fakes import FakeLLM

    scene = request(
        server, "POST", "/api/scenes", {"character_ids": ["char-alice", "char-bob"]},
        expect=201,
    )["scene"]
    # First turn is slow so the engine is still mid-run when we try to step.
    server.llm = FakeLLM([lambda *_a, **_k: (time.sleep(0.6), '*nods* "One."')[1]])

    app.start_scene(scene["id"])
    engine = app.engines[scene["id"]]
    try:
        assert engine.running is True
        assert request(server, "POST", f"/api/scenes/{scene['id']}/step")["event"] is None
    finally:
        app.stop_scene(scene["id"])
        engine.join(timeout=10)


def test_delete_scene_removes_files(server, app):
    scene = request(
        server, "POST", "/api/scenes", {"character_ids": ["char-alice", "char-bob"]},
        expect=201,
    )["scene"]
    assert app.config.scene_path(scene["id"]).exists()

    assert request(server, "DELETE", f"/api/scenes/{scene['id']}")["ok"] is True
    assert not app.config.scene_path(scene["id"]).exists()
    assert request(server, "DELETE", f"/api/scenes/{scene['id']}", expect=404) is not None


def test_compaction_endpoint(server, app):
    from fakes import FakeLLM

    scene = request(
        server, "POST", "/api/scenes", {"character_ids": ["char-alice", "char-bob"]},
        expect=201,
    )["scene"]
    request(server, "POST", f"/api/scenes/{scene['id']}/events",
            {"text": "The storm lifts."}, expect=201)
    app.llm = FakeLLM(["Mara waited out the storm. Grey stayed below."])

    result = request(server, "POST", f"/api/scenes/{scene['id']}/compact")
    assert "Grey stayed below" in result["memory"]
    assert request(server, "GET", f"/api/scenes/{scene['id']}")["memory"] == result["memory"]

    # Compacting again with nothing pending is a no-op, not a second call.
    again = request(server, "POST", f"/api/scenes/{scene['id']}/compact")
    assert again["memory"] == result["memory"]
    assert len(app.llm.calls) == 1


# -------------------------------------------------------------- regressions
def test_engine_sees_narration_added_through_http_state(server, app):
    from fakes import FakeLLM

    scene = request(
        server, "POST", "/api/scenes",
        {"character_ids": ["char-alice", "char-bob"]}, expect=201,
    )["scene"]
    app.llm = FakeLLM(['"Hello."', '"I heard that."'])

    app.step_scene(scene["id"])
    request(server, "POST", f"/api/scenes/{scene['id']}/events",
            {"text": "The floorboards creak."}, expect=201)
    app.step_scene(scene["id"])

    second_prompt = app.llm.calls[1]["messages"]
    assert any("floorboards creak" in m["content"] for m in second_prompt)


def test_narration_reopens_a_naturally_stopped_scene(server, app):
    from fakes import FakeLLM
    from heyallm.engine import END_NATURAL

    app.config.engine.max_turns = 2
    app.config.engine.max_consecutive_silences = 1
    scene = request(
        server, "POST", "/api/scenes",
        {"character_ids": ["char-alice", "char-bob"]}, expect=201,
    )["scene"]
    app.llm = FakeLLM(["[[SILENT]]", '"I am still here."'])

    app.start_scene(scene["id"])
    engine = app.engines[scene["id"]]
    engine.join(timeout=10)
    assert engine.end_reason == END_NATURAL

    request(server, "POST", f"/api/scenes/{scene['id']}/events",
            {"text": "The wind changes."}, expect=201)
    engine.join(timeout=10)

    assert len(app.llm.calls) == 2
    assert app.scene_detail(scene["id"])["events"][-1]["kind"] == "message"


def test_auto_compaction_runs_during_a_scene(server, app, monkeypatch):
    import threading
    from fakes import FakeLLM
    from heyallm.memory import Chronicle

    app.config.memory.compact_at_events = 2
    scene = request(
        server, "POST", "/api/scenes",
        {"character_ids": ["char-alice", "char-bob"]}, expect=201,
    )["scene"]
    app.llm = FakeLLM(["Mara listened to the storm."])
    done = threading.Event()
    original = app._auto_compact

    def compact_and_signal(scene_id):
        try:
            original(scene_id)
        finally:
            done.set()

    monkeypatch.setattr(app, "_auto_compact", compact_and_signal)
    request(server, "POST", f"/api/scenes/{scene['id']}/events",
            {"text": "The storm intensifies."}, expect=201)

    assert done.wait(timeout=5)
    chronicle = Chronicle.load(app.config.record_path(scene["id"]))
    assert chronicle.text == "Mara listened to the storm."
    assert chronicle.compacted_upto == 2


def test_compaction_does_not_mark_events_appended_during_request(server, app):
    from fakes import FakeLLM
    from heyallm.memory import Chronicle

    scene = request(
        server, "POST", "/api/scenes",
        {"character_ids": ["char-alice", "char-bob"]}, expect=201,
    )["scene"]
    request(server, "POST", f"/api/scenes/{scene['id']}/events",
            {"text": "The lamp goes out."}, expect=201)

    def add_during_compaction(*_args, **_kwargs):
        app.add_narration(scene["id"], "A door opens.")
        return "Mara sat in the dark."

    app.llm = FakeLLM([add_during_compaction, "Mara heard the door open."])
    app.compact_scene(scene["id"])
    chronicle = Chronicle.load(app.config.record_path(scene["id"]))
    assert chronicle.compacted_upto == 2

    app.compact_scene(scene["id"])
    chronicle = Chronicle.load(app.config.record_path(scene["id"]))
    assert chronicle.compacted_upto == 3
    assert "A door opens." in app.llm.calls[1]["messages"][0]["content"]


def test_malformed_json_is_a_400(server):
    req = urllib.request.Request(
        server.base_url + "/api/config",
        data=b"{not json",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req, timeout=5)
    assert caught.value.code == 400
    assert b"valid JSON" in caught.value.read()


def test_unknown_scene_actions_are_404(server):
    for action in ("start", "stop", "step", "compact"):
        request(server, "POST", f"/api/scenes/scene-nope/{action}", {}, expect=404)


def test_delete_scene_also_removes_its_chronicle(server, app):
    from fakes import FakeLLM

    scene = request(
        server, "POST", "/api/scenes",
        {"character_ids": ["char-alice", "char-bob"]}, expect=201,
    )["scene"]
    app.llm = FakeLLM(["Mara waited."])
    app.compact_scene(scene["id"])
    record = app.config.record_path(scene["id"])
    assert record.exists()

    request(server, "DELETE", f"/api/scenes/{scene['id']}")
    assert not record.exists()


def test_stale_scene_end_cannot_clobber_a_restart(server, app, monkeypatch):
    import threading
    from fakes import FakeLLM

    app.config.engine.max_turns = 1
    scene = request(
        server, "POST", "/api/scenes",
        {"character_ids": ["char-alice", "char-bob"]}, expect=201,
    )["scene"]
    first_end = threading.Event()
    release_first_end = threading.Event()
    first_end_done = threading.Event()
    second_call = threading.Event()
    release_second_call = threading.Event()

    def slow_second(*_args, **_kwargs):
        second_call.set()
        assert release_second_call.wait(timeout=5)
        return '"second run"'

    app.llm = FakeLLM(['"first run"', slow_second])
    original = app._on_scene_end

    def delayed_end(scene_id, reason, generation):
        if generation == 1:
            first_end.set()
            assert release_first_end.wait(timeout=5)
        original(scene_id, reason, generation)
        if generation == 1:
            first_end_done.set()

    monkeypatch.setattr(app, "_on_scene_end", delayed_end)
    app.start_scene(scene["id"])
    engine = app.engines[scene["id"]]
    assert first_end.wait(timeout=5)

    assert app.start_scene(scene["id"])
    assert second_call.wait(timeout=5)
    release_first_end.set()
    assert first_end_done.wait(timeout=5)

    assert app.active_scene_id == scene["id"]
    assert app._scene_meta(scene["id"])["status"] == "running"
    release_second_call.set()
    engine.join(timeout=10)


def test_start_waits_for_a_manual_step_instead_of_reporting_false_success(server, app):
    import threading
    from fakes import FakeLLM

    app.config.engine.max_turns = 1
    scene = request(
        server, "POST", "/api/scenes",
        {"character_ids": ["char-alice", "char-bob"]}, expect=201,
    )["scene"]
    step_started = threading.Event()
    release_step = threading.Event()
    start_done = threading.Event()
    results = []

    def slow_step(*_args, **_kwargs):
        step_started.set()
        assert release_step.wait(timeout=5)
        return '"manual"'

    app.llm = FakeLLM([slow_step, '"automatic"'])
    step = threading.Thread(target=lambda: results.append(app.step_scene(scene["id"])))

    def start():
        results.append(app.start_scene(scene["id"]))
        start_done.set()

    step.start()
    assert step_started.wait(timeout=5)
    starting = threading.Thread(target=start)
    starting.start()
    assert not start_done.wait(timeout=0.1)

    release_step.set()
    step.join(timeout=5)
    starting.join(timeout=5)

    assert results[0].kind == "message"
    assert results[1] is True
    assert len(app.llm.calls) == 2
    app.engines[scene["id"]].join(timeout=10)


def test_stop_reports_the_actual_state_if_a_request_is_still_in_flight(server, app, monkeypatch):
    import threading
    from fakes import FakeLLM

    scene = request(
        server, "POST", "/api/scenes",
        {"character_ids": ["char-alice", "char-bob"]}, expect=201,
    )["scene"]
    started = threading.Event()
    release = threading.Event()

    def slow_reply(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=5)
        return '"too late"'

    app.llm = FakeLLM([slow_reply])
    app.start_scene(scene["id"])
    engine = app.engines[scene["id"]]
    assert started.wait(timeout=5)
    original_join = engine.join
    monkeypatch.setattr(engine, "join", lambda timeout=None: None)

    assert request(server, "POST", f"/api/scenes/{scene['id']}/stop")["running"] is True

    monkeypatch.setattr(engine, "join", original_join)
    release.set()
    original_join(timeout=10)
    assert not engine.running


def test_persisted_running_status_is_reconciled_on_startup(server, app):
    from heyallm.transcript import load_scene_meta, write_scene_meta

    scene = request(
        server, "POST", "/api/scenes",
        {"character_ids": ["char-alice", "char-bob"]}, expect=201,
    )["scene"]
    scene["status"] = "running"
    write_scene_meta(app.config.scenes_dir, scene)
    assert app.scene_summaries()[0]["status"] == "stopped"

    restarted = serve(data_dir=app.config.data_dir, port=0)
    try:
        assert load_scene_meta(app.config.scenes_dir)[0]["status"] == "stopped"
    finally:
        restarted.server_close()


def test_server_refuses_to_overwrite_a_corrupt_config(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="Could not read"):
        serve(data_dir=tmp_path, port=0)
    assert path.read_text(encoding="utf-8") == "{broken"


# ----------------------------------------------------------------------- SSE
def test_sse_stream_delivers_the_initial_status(server):
    scene = request(
        server, "POST", "/api/scenes", {"character_ids": ["char-alice", "char-bob"]},
        expect=201,
    )["scene"]
    url = server.base_url + f"/api/scenes/{scene['id']}/stream"

    with urllib.request.urlopen(url, timeout=5) as resp:
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        line = resp.readline().decode("utf-8").strip()
        assert line.startswith("data: ")
        payload = json.loads(line[len("data: "):])
        assert payload["type"] == "status"
        assert payload["scene_id"] == scene["id"]
        assert payload["running"] is False


def test_broadcast_reaches_subscribers(server):
    scene = request(
        server, "POST", "/api/scenes", {"character_ids": ["char-alice", "char-bob"]},
        expect=201,
    )["scene"]
    q = server.subscribe(scene["id"])
    server.broadcast(scene["id"], {"type": "event", "scene_id": scene["id"]})

    assert q.get(timeout=2)["type"] == "event"
    server.unsubscribe(scene["id"], q)
    server.broadcast(scene["id"], {"type": "event", "scene_id": scene["id"]})
    assert q.empty()

    # A dead consumer must never block the engine.
    from queue import Queue, Full

    full: Queue = Queue(maxsize=1)
    full.put_nowait({"type": "x"})
    server._subs.setdefault(scene["id"], []).append(full)
    server.broadcast(scene["id"], {"type": "event"})  # must not raise
    assert full.get_nowait()["type"] == "resync"
    server.unsubscribe(scene["id"], full)
    time.sleep(0.01)


def test_stream_unknown_scene_is_404(server):
    try:
        urllib.request.urlopen(server.base_url + "/api/scenes/scene-nope/stream", timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:  # pragma: no cover
        pytest.fail("expected a 404")
