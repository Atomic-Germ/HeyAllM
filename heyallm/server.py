"""Local HTTP server: REST API, SSE, and the static messaging UI.

Standard library only (`http.server` + threads) -- this is a local app, and a
framework would buy nothing.  The engine runs on its own thread; SSE
subscribers get fan-out through a per-scene queue.
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import shutil
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

from . import __version__, director as director_mod
from .config import Config, Character, Endpoint, blank_character, new_id
from .creative import CreativeToolbox
from .engine import END_NATURAL, SceneEngine
from .llm import LLMClient, LLMError
from .memory import Chronicle, compact as compact_chronicle
from .transcript import (
    Event,
    Transcript,
    delete_scene as delete_scene_files,
    load_scene_meta,
    write_scene_meta,
)

log = logging.getLogger("heyallm.server")

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PORT = 8777


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------
class SceneBusyError(RuntimeError):
    """A scene operation cannot proceed until an in-flight turn finishes."""


class _SceneOperation:
    def __init__(self) -> None:
        self.active = 0
        self.deleting = False


class App:
    """Everything the handlers need, shared across request threads."""

    def __init__(self, data_dir: str | Path | None = None, config: Config | None = None) -> None:
        self.config = config or Config.load(data_dir)
        self.llm = LLMClient()
        self.creative = CreativeToolbox(self.config.data_dir)
        self._scene_creative: dict[str, CreativeToolbox] = {}
        self._scene_ops: dict[str, _SceneOperation] = {}
        self.engines: dict[str, SceneEngine] = {}
        self.active_scene_id: str | None = None
        self._subs: dict[str, list[queue.Queue]] = {}
        self._transcripts: dict[str, Transcript] = {}
        self._compacting: set[str] = set()
        self._memory_lock = threading.Lock()
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()

    # ------------------------------------------------------------- broadcast
    def subscribe(self, scene_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.setdefault(scene_id, []).append(q)
        return q

    def unsubscribe(self, scene_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subs.get(scene_id) or []
            if q in subs:
                subs.remove(q)

    def broadcast(self, scene_id: str, payload: dict) -> None:
        with self._lock:
            subs = list(self._subs.get(scene_id) or [])
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # Never block the engine. Drop the oldest frame and ask the
                # client to merge a fresh transcript snapshot instead.
                try:
                    q.get_nowait()
                    q.put_nowait({"type": "resync", "scene_id": scene_id})
                except (queue.Empty, queue.Full):
                    pass

    # ---------------------------------------------------------------- state
    def state(self) -> dict:
        running = False
        active = self.active_scene_id
        if active and active in self.engines:
            running = self.engines[active].busy
        return {
            "config": self.config.to_dict(),
            "characters": [vars(c) | {"endpoint": vars(c.endpoint)} for c in self.config.characters],
            "scenes": self.scene_summaries(),
            "active_scene": active,
            "running": running,
        }

    def scene_summaries(self) -> list[dict]:
        out = []
        for meta in load_scene_meta(self.config.scenes_dir):
            scene_id = str(meta.get("id") or "")
            engine = self.engines.get(scene_id)
            meta["status"] = "running" if engine and engine.busy else "stopped"
            meta["event_count"] = len(self._transcript(scene_id))
            out.append(meta)
        return out

    @contextmanager
    def _scene_operation(self, scene_id: str) -> Iterator[None]:
        """Register director work so deletion cannot race its persistence."""
        with self._lock:
            operation = self._scene_ops.setdefault(scene_id, _SceneOperation())
            if operation.deleting:
                raise SceneBusyError("Scene is being deleted")
            operation.active += 1
        try:
            yield
        finally:
            with self._lock:
                operation.active -= 1
                if (
                    operation.active == 0
                    and not operation.deleting
                    and self._scene_meta(scene_id) is None
                ):
                    self._scene_ops.pop(scene_id, None)

    def _creative_path(self, scene_id: str) -> Path:
        if (
            not isinstance(scene_id, str)
            or not scene_id
            or scene_id in {".", ".."}
            or "/" in scene_id
            or "\\" in scene_id
            or Path(scene_id).name != scene_id
        ):
            raise ValueError("Invalid scene id")
        base = (self.config.creative_dir / "scenes").resolve()
        target = (base / scene_id).resolve()
        if target.parent != base:
            raise ValueError("Invalid scene id")
        return target

    def _creative_for(self, scene_id: str) -> CreativeToolbox:
        self._creative_path(scene_id)  # validate before constructing a namespace
        with self._lock:
            toolbox = self._scene_creative.get(scene_id)
            if toolbox is None:
                toolbox = CreativeToolbox(self.config.data_dir, namespace=f"scenes/{scene_id}")
                self._scene_creative[scene_id] = toolbox
            return toolbox

    def _director_complete(
        self, endpoint: Endpoint, messages: list[dict], *, scene_id: str | None = None, **kwargs,
    ):
        toolbox = self._creative_for(scene_id) if scene_id else self.creative
        return director_mod.complete(
            self.llm,
            endpoint,
            messages,
            creative=toolbox if self.config.creative.enabled else None,
            max_tool_calls=self.config.creative.max_tool_calls,
            **kwargs,
        )

    # ----------------------------------------------------------- characters
    def create_character(self, body: dict) -> Character:
        seed = str(body.get("prompt_extra") or "")
        endpoint = Endpoint.from_dict(body.get("endpoint"))
        character = blank_character(endpoint)
        character.prompt_extra = seed

        if body.get("generate", True):
            filled = director_mod.create_character(
                self.llm,
                self.config.director,
                seed=seed,
                character_endpoint=endpoint,
                creative=self.creative if self.config.creative.enabled else None,
                max_tool_calls=self.config.creative.max_tool_calls,
            )
            director_mod.apply_to_character(character, filled)

        with self.config.lock:
            self.config.characters.append(character)
            self.config.save()
        return character

    def update_character(self, char_id: str, body: dict) -> Character | None:
        with self.config.lock:
            character = self.config.get_character(char_id)
            if character is None:
                return None
            for key in ("screenname", "real_name", "persona", "prompt_extra"):
                if key in body:
                    setattr(character, key, str(body.get(key) or ""))
            if isinstance(body.get("demographics"), dict):
                character.demographics = {
                    str(k): str(v) for k, v in body["demographics"].items() if v
                }
            if isinstance(body.get("endpoint"), dict):
                character.endpoint = Endpoint.from_dict(body["endpoint"])
            self.config.save()
        return character

    def regenerate_character(self, char_id: str) -> Character | None:
        character = self.config.get_character(char_id)
        if character is None:
            return None
        filled = director_mod.create_character(
            self.llm,
            self.config.director,
            seed=character.prompt_extra,
            character_endpoint=character.endpoint,
            creative=self.creative if self.config.creative.enabled else None,
            max_tool_calls=self.config.creative.max_tool_calls,
        )
        with self.config.lock:
            if self.config.get_character(char_id) is None:
                return None
            director_mod.apply_to_character(character, filled)
            self.config.save()
        return character

    def delete_character(self, char_id: str) -> bool:
        with self.config.lock:
            removed = self.config.remove_character(char_id)
            if removed:
                self.config.save()
        return removed

    def apply_config(self, body: dict) -> None:
        raw = body.get("config") if isinstance(body.get("config"), dict) else body
        if not isinstance(raw, dict):
            return
        with self.config.lock:
            if isinstance(raw.get("director"), dict):
                self.config.director = Endpoint.from_dict(raw["director"])
            for field_name in ("cooldown", "engine", "narration", "memory", "creative"):
                if isinstance(raw.get(field_name), dict):
                    setattr(
                        self.config,
                        field_name,
                        getattr(self.config, field_name).__class__.from_dict(raw[field_name]),
                    )
            self.config.save()

    # --------------------------------------------------------------- scenes
    def _transcript(self, scene_id: str) -> Transcript:
        """Return the one shared transcript for a scene.

        The engine and HTTP request threads must see the same in-memory log;
        separate Transcript instances would each go stale after the other
        appended an event.
        """
        with self._lock:
            transcript = self._transcripts.get(scene_id)
            if transcript is None:
                scene = self._scene_meta(scene_id) or {"id": scene_id}
                transcript = Transcript(self.config.scene_path(scene_id), scene=scene)
                self._transcripts[scene_id] = transcript
            return transcript

    def _scene_meta(self, scene_id: str) -> dict | None:
        for meta in load_scene_meta(self.config.scenes_dir):
            if meta.get("id") == scene_id:
                return meta
        return None

    def _stop_other_engines(self, except_id: str | None = None) -> None:
        for other_id, other in list(self.engines.items()):
            if other_id == except_id or not other.running:
                continue
            other.stop()
            other.join(timeout=5)
            if other.running:
                raise SceneBusyError("Another scene is still stopping; try again in a moment")

    def create_scene(self, body: dict) -> dict:
        with self._lifecycle_lock:
            ids = list(body.get("character_ids") or [])
            if len(ids) != 2 or ids[0] == ids[1]:
                raise ValueError("A scene needs two different characters")
            for char_id in ids:
                if self.config.get_character(char_id) is None:
                    raise ValueError(f"Unknown character: {char_id}")
            self._stop_other_engines()

            scene = {
                "id": new_id("scene"),
                "title": str(body.get("title") or "").strip() or "Untitled scene",
                "setting": str(body.get("setting") or "").strip(),
                "character_ids": ids,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "status": "stopped",
            }
            write_scene_meta(self.config.scenes_dir, scene)
            transcript = self._transcript(scene["id"])
            transcript.add(kind="scene", text=f"{scene['title']} — {scene['setting']}".strip(" —"))
            self.active_scene_id = scene["id"]
            return scene

    def scene_detail(self, scene_id: str) -> dict | None:
        meta = self._scene_meta(scene_id)
        if meta is None:
            return None
        transcript = self._transcript(scene_id)
        chronicle = Chronicle.load(self.config.record_path(scene_id))
        return {
            "scene": meta,
            "events": transcript.dict_events(),
            "memory": chronicle.text,
        }

    def add_narration(self, scene_id: str, text: str) -> Event | None:
        with self._scene_operation(scene_id):
            return self._add_narration(scene_id, text)

    def _add_narration(self, scene_id: str, text: str) -> Event | None:
        if self._scene_meta(scene_id) is None:
            return None
        transcript = self._transcript(scene_id)
        event = transcript.add(kind="narration", text=text.strip())
        self.broadcast(scene_id, {"type": "event", "scene_id": scene_id, "event": event.to_dict()})
        self._schedule_auto_compact(scene_id)
        self._wake_engine(scene_id)
        return event

    def narrate(self, scene_id: str, hint: str = "") -> Event:
        with self._scene_operation(scene_id):
            return self._narrate(scene_id, hint)

    def _narrate(self, scene_id: str, hint: str = "") -> Event:
        """The director takes a turn as the narrator of the scene.

        Anything the third model says arrives as narration, exactly like the
        human's -- the world moving around the characters, never a line of
        dialogue from either of them.
        """
        if self._scene_meta(scene_id) is None:
            raise ValueError("Unknown scene")
        transcript = self._transcript(scene_id)
        recent = [e for e in transcript.events if e.kind != "scene"][-14:]
        body = "\n".join(f"{e.kind}: {e.text}" for e in recent) or "(nothing yet)"

        meta = self._scene_meta(scene_id) or {}
        prompt = (
            "You are the narrator of a scene between two people.\n"
            f"Scene: {meta.get('title') or 'Untitled'}"
            + (f" -- {meta.get('setting')}" if meta.get("setting") else "")
            + "\n\nHere are the most recent events, in order:\n\n"
            + body
            + "\n\nWrite ONE narrative beat: one or two sentences of something that "
            "happens around them -- weather, an object, a sound, someone's body, the "
            "light changing. Third person. Not addressed to anyone, and nobody in "
            "the scene says anything. It must be consistent with what is written "
            "above; do not resolve their situation for them."
        )
        if hint.strip():
            prompt += "\n\nDirection to follow: " + hint.strip()

        result = self._director_complete(
            self.config.director,
            [{"role": "user", "content": prompt}],
            scene_id=scene_id,
            temperature=0.95,
            max_tokens=220,
            timeout=(10, 120),
        )
        text = (result.text or "").strip()
        if not text:
            raise LLMError("Director returned an empty narrative beat")

        event = transcript.add(kind="narration", text=text)
        self.broadcast(scene_id, {"type": "event", "scene_id": scene_id, "event": event.to_dict()})
        self._schedule_auto_compact(scene_id)
        self._wake_engine(scene_id)
        return event

    def _wake_engine(self, scene_id: str) -> None:
        """Narration means the scene still has somewhere to go."""
        with self._lifecycle_lock:
            engine = self.engines.get(scene_id)
            if engine is None:
                return
            if engine.running:
                engine.note_narration()
            elif engine.end_reason == END_NATURAL:
                # The human (or the director) pushed the scene back open.
                engine.note_narration()
                if not engine.start(resume=True):
                    raise SceneBusyError("Scene is busy")
                self.active_scene_id = scene_id
                meta = self._scene_meta(scene_id)
                if meta is not None:
                    meta["status"] = "running"
                    write_scene_meta(self.config.scenes_dir, meta)

    def _engine_for(self, scene_id: str) -> SceneEngine | None:
        with self._lifecycle_lock:
            meta = self._scene_meta(scene_id)
            if meta is None:
                return None
            with self._lock:
                engine = self.engines.get(scene_id)
                if engine is None:
                    engine = SceneEngine(
                        self.config,
                        self.llm,
                        self._transcript(scene_id),
                        meta,
                        on_event=lambda e, sid=scene_id: self._on_engine_event(sid, e),
                        on_status=lambda s, sid=scene_id: self.broadcast(
                            sid, {"type": "status", "scene_id": sid, **s}
                        ),
                        on_end=lambda r, g, sid=scene_id: self._on_scene_end(sid, r, g),
                        on_error=lambda m, sid=scene_id: self.broadcast(
                            sid, {"type": "error", "scene_id": sid, "message": m}
                        ),
                    )
                    self.engines[scene_id] = engine
                return engine

    def _on_engine_event(self, scene_id: str, event: dict) -> None:
        self.broadcast(scene_id, {"type": "event", "scene_id": scene_id, "event": event})
        self._schedule_auto_compact(scene_id)

    def _on_scene_end(self, scene_id: str, reason: str, generation: int) -> None:
        with self._lifecycle_lock:
            engine = self.engines.get(scene_id)
            if engine is None or engine.generation != generation:
                return
            meta = self._scene_meta(scene_id)
            if meta is not None:
                meta["status"] = "stopped"
                write_scene_meta(self.config.scenes_dir, meta)
            self.broadcast(scene_id, {"type": "scene_end", "scene_id": scene_id, "reason": reason})
            if self.active_scene_id == scene_id:
                self.active_scene_id = None
            if reason in (END_NATURAL, "max turns"):
                self._schedule_auto_compact(scene_id)

    def start_scene(self, scene_id: str) -> bool:
        with self._lifecycle_lock:
            engine = self._engine_for(scene_id)
            if engine is None:
                return False
            if engine.running:
                return True
            self._stop_other_engines(except_id=scene_id)
            if not engine.start():
                raise SceneBusyError("Scene is busy")
            meta = self._scene_meta(scene_id)
            if meta is not None:
                meta["status"] = "running"
                write_scene_meta(self.config.scenes_dir, meta)
            self.active_scene_id = scene_id
            return True

    def stop_scene(self, scene_id: str) -> bool:
        engine = self.engines.get(scene_id)
        if engine is None:
            return False
        engine.stop()
        engine.join(timeout=10)
        return True

    def step_scene(self, scene_id: str) -> Event | None:
        # Serialize manual turns with Start/Delete. Stop deliberately does not
        # take this lock, so it can still interrupt the cooldown promptly.
        with self._lifecycle_lock:
            engine = self._engine_for(scene_id)
            if engine is None:
                raise ValueError("Unknown scene")
            if engine.running:
                return None
            return engine.step()

    def delete_scene(self, scene_id: str) -> bool:
        with self._lifecycle_lock:
            # Existence is checked before touching any path derived from the id.
            if self._scene_meta(scene_id) is None:
                return False
            creative_path = self._creative_path(scene_id)
            with self._lock:
                operation = self._scene_ops.setdefault(scene_id, _SceneOperation())
                if operation.active:
                    raise SceneBusyError("A director request is still using this scene")
                operation.deleting = True
            try:
                engine = self.engines.get(scene_id)
                if engine:
                    engine.stop()
                    engine.join(timeout=5)
                    if engine.running:
                        raise SceneBusyError("Scene is still stopping; try again in a moment")
                with self._lock:
                    self.engines.pop(scene_id, None)
                    self._transcripts.pop(scene_id, None)
                    self._scene_creative.pop(scene_id, None)
                if self.active_scene_id == scene_id:
                    self.active_scene_id = None
                with self._memory_lock:
                    removed = delete_scene_files(self.config.scenes_dir, scene_id)
                    try:
                        self.config.record_path(scene_id).unlink()
                    except FileNotFoundError:
                        pass
                    try:
                        shutil.rmtree(creative_path)
                    except FileNotFoundError:
                        pass
                with self._lock:
                    self._scene_ops.pop(scene_id, None)
                return removed
            finally:
                with self._lock:
                    operation.deleting = False

    # --------------------------------------------------------------- memory
    def compact_scene(self, scene_id: str) -> str:
        with self._scene_operation(scene_id):
            return self._compact_scene(scene_id)

    def _compact_scene(self, scene_id: str) -> str:
        with self._memory_lock:
            meta = self._scene_meta(scene_id)
            if meta is None:
                raise ValueError("Unknown scene")
            transcript = self._transcript(scene_id)
            chronicle = Chronicle.load(self.config.record_path(scene_id))
            events = transcript.events
            pending = events[chronicle.compacted_upto:]
            if not pending:
                return chronicle.text

            characters = {c.id: c for c in self.config.characters}
            text = compact_chronicle(
                self.llm, self.config.director, chronicle, pending, characters,
                chat_fn=lambda endpoint, messages, **kwargs: self._director_complete(
                    endpoint, messages, scene_id=scene_id, **kwargs,
                ),
            )
            chronicle.text = text
            chronicle.scene_id = scene_id
            # Only mark the snapshot that was actually sent to the director.
            # Events appended during the request remain pending for next time.
            chronicle.compacted_upto = len(events)
            chronicle.save(self.config.record_path(scene_id))
            log.info("Compacted %d events for %s", len(pending), scene_id)
            return text

    def _schedule_auto_compact(self, scene_id: str) -> None:
        if not self.config.memory.auto_compact:
            return
        with self._lock:
            if scene_id in self._compacting:
                return
            self._compacting.add(scene_id)
        try:
            threading.Thread(
                target=self._auto_compact, args=(scene_id,), daemon=True,
                name=f"compact-{scene_id}",
            ).start()
        except BaseException:
            with self._lock:
                self._compacting.discard(scene_id)
            raise

    def _auto_compact(self, scene_id: str) -> None:
        try:
            with self._scene_operation(scene_id):
                self._auto_compact_locked(scene_id)
        except SceneBusyError:
            return

    def _auto_compact_locked(self, scene_id: str) -> None:
        failed = False
        try:
            while True:
                if self._scene_meta(scene_id) is None:
                    break
                transcript = self._transcript(scene_id)
                chronicle = Chronicle.load(self.config.record_path(scene_id))
                pending = transcript.since(chronicle.compacted_upto)
                if len(pending) < self.config.memory.compact_at_events:
                    break
                self.compact_scene(scene_id)
        except (LLMError, ValueError, OSError) as exc:
            failed = True
            log.warning("Auto-compact failed for %s: %s", scene_id, exc)
        finally:
            with self._lock:
                self._compacting.discard(scene_id)
        if not failed and self._scene_meta(scene_id) is not None:
            # Close the race with an event arriving while this worker exited.
            transcript = self._transcript(scene_id)
            chronicle = Chronicle.load(self.config.record_path(scene_id))
            if len(transcript.since(chronicle.compacted_upto)) >= self.config.memory.compact_at_events:
                self._schedule_auto_compact(scene_id)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "HeyAllM/0.1"
        protocol_version = "HTTP/1.1"

        # ------------------------------------------------------- plumbing
        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, obj: Any, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, message: str, status: int = 400) -> None:
            self._send_json({"error": message}, status)

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise ValueError("Invalid Content-Length") from exc
            if length <= 0:
                return {}
            if length > 1_048_576:
                raise ValueError("Request body is too large")
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Request body must be valid JSON") from exc
            if not isinstance(data, dict):
                raise ValueError("Request body must be a JSON object")
            return data

        # -------------------------------------------------------- static
        def _serve_static(self, rel: str) -> None:
            target = (STATIC_DIR / rel).resolve()
            try:
                target.relative_to(STATIC_DIR.resolve())
            except ValueError:
                self._send_error_json("Not found", 404)
                return
            if not target.is_file():
                self._send_error_json("Not found", 404)
                return
            ctype = "text/plain; charset=utf-8"
            suffix = target.suffix.lower()
            if suffix == ".html":
                ctype = "text/html; charset=utf-8"
            elif suffix == ".css":
                ctype = "text/css; charset=utf-8"
            elif suffix == ".js":
                ctype = "application/javascript; charset=utf-8"
            elif suffix == ".svg":
                ctype = "image/svg+xml"
            elif suffix in (".png", ".jpg", ".jpeg", ".webp"):
                ctype = f"image/{suffix[1:]}"
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ----------------------------------------------------------- SSE
        def _stream(self, scene_id: str) -> None:
            if app._scene_meta(scene_id) is None:
                self._send_error_json("Unknown scene", 404)
                return
            q = app.subscribe(scene_id)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                engine = app.engines.get(scene_id)
                initial = engine.status() if engine else {
                    "scene_id": scene_id, "running": False, "turn": 0,
                    "next_speaker": None, "typing": None, "state": "idle",
                }
                self._write_sse({"type": "status", "scene_id": scene_id, **initial})
                while True:
                    try:
                        payload = q.get(timeout=20)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    self._write_sse(payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.unsubscribe(scene_id, q)

        def _write_sse(self, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False)
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        # ---------------------------------------------------------- routes
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            try:
                if path == "/" or path == "/index.html":
                    self._serve_static("index.html")
                    return
                if path.startswith("/static/"):
                    self._serve_static(path[len("/static/"):])
                    return
                if path == "/api/health":
                    self._send_json({"ok": True})
                    return
                if path == "/api/state":
                    self._send_json(app.state())
                    return
                if path == "/api/characters":
                    self._send_json({"characters": [
                        vars(c) | {"endpoint": vars(c.endpoint)}
                        for c in app.config.characters
                    ]})
                    return

                parts = [p for p in path.split("/") if p]
                if parts[:2] == ["api", "characters"] and len(parts) == 3:
                    character = app.config.get_character(parts[2])
                    if character is None:
                        self._send_error_json("Unknown character", 404)
                    else:
                        self._send_json({"character": vars(character) | {"endpoint": vars(character.endpoint)}})
                    return
                if path == "/api/scenes":
                    self._send_json({"scenes": app.scene_summaries()})
                    return
                if parts[:2] == ["api", "scenes"] and len(parts) == 3:
                    detail = app.scene_detail(parts[2])
                    if detail is None:
                        self._send_error_json("Unknown scene", 404)
                    else:
                        self._send_json(detail)
                    return
                if parts[:2] == ["api", "scenes"] and len(parts) == 4 and parts[3] == "stream":
                    self._stream(parts[2])
                    return
                self._send_error_json("Not found", 404)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/")
            parts = [p for p in path.split("/") if p]
            try:
                body = self._body()
            except ValueError as exc:
                self._send_error_json(str(exc))
                return
            try:
                if path == "/api/characters":
                    character = app.create_character(body)
                    self._send_json(
                        {"character": vars(character) | {"endpoint": vars(character.endpoint)}}, 201
                    )
                    return
                if path == "/api/config":
                    app.apply_config(body)
                    self._send_json({"config": app.config.to_dict()})
                    return
                if path == "/api/scenes":
                    try:
                        scene = app.create_scene(body)
                    except ValueError as exc:
                        self._send_error_json(str(exc))
                        return
                    except SceneBusyError as exc:
                        self._send_error_json(str(exc), 409)
                        return
                    self._send_json({"scene": scene}, 201)
                    return

                if parts[:2] == ["api", "scenes"] and len(parts) == 4:
                    scene_id, action = parts[2], parts[3]
                    if action == "events":
                        text = str(body.get("text") or "").strip()
                        if not text:
                            self._send_error_json("Empty narration")
                            return
                        event = app.add_narration(scene_id, text)
                        if event is None:
                            self._send_error_json("Unknown scene", 404)
                        else:
                            self._send_json({"event": event.to_dict()}, 201)
                        return
                    if action == "narrate":
                        try:
                            event = app.narrate(scene_id, str(body.get("hint") or ""))
                        except ValueError:
                            self._send_error_json("Unknown scene", 404)
                            return
                        except LLMError as exc:
                            self._send_error_json(str(exc), 502)
                            return
                        self._send_json({"event": event.to_dict()}, 201)
                        return
                    if action == "start":
                        try:
                            started = app.start_scene(scene_id)
                        except SceneBusyError as exc:
                            self._send_error_json(str(exc), 409)
                            return
                        if not started:
                            self._send_error_json("Unknown scene", 404)
                        else:
                            self._send_json({"running": True})
                        return
                    if action == "stop":
                        if not app.stop_scene(scene_id):
                            self._send_error_json("Unknown scene", 404)
                        else:
                            engine = app.engines.get(scene_id)
                            self._send_json({"running": bool(engine and engine.busy)})
                        return
                    if action == "step":
                        try:
                            event = app.step_scene(scene_id)
                        except ValueError:
                            self._send_error_json("Unknown scene", 404)
                            return
                        self._send_json({"event": event.to_dict() if event else None})
                        return
                    if action == "compact":
                        try:
                            memory = app.compact_scene(scene_id)
                        except ValueError as exc:
                            self._send_error_json(str(exc), 404)
                            return
                        except LLMError as exc:
                            self._send_error_json(str(exc), 502)
                            return
                        self._send_json({"memory": memory})
                        return

                if parts[:2] == ["api", "characters"] and len(parts) == 4 and parts[3] == "regenerate":
                    try:
                        character = app.regenerate_character(parts[2])
                    except LLMError as exc:
                        self._send_error_json(str(exc), 502)
                        return
                    if character is None:
                        self._send_error_json("Unknown character", 404)
                    else:
                        self._send_json({"character": vars(character) | {"endpoint": vars(character.endpoint)}})
                    return

                self._send_error_json("Not found", 404)
            except SceneBusyError as exc:
                self._send_error_json(str(exc), 409)
            except LLMError as exc:
                self._send_error_json(str(exc), 502)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def do_PUT(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/")
            parts = [p for p in path.split("/") if p]
            try:
                body = self._body()
            except ValueError as exc:
                self._send_error_json(str(exc))
                return
            try:
                if parts[:2] == ["api", "characters"] and len(parts) == 3:
                    character = app.update_character(parts[2], body)
                    if character is None:
                        self._send_error_json("Unknown character", 404)
                    else:
                        self._send_json({"character": vars(character) | {"endpoint": vars(character.endpoint)}})
                    return
                if path == "/api/config":
                    app.apply_config(body)
                    self._send_json({"config": app.config.to_dict()})
                    return
                self._send_error_json("Not found", 404)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def do_DELETE(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/")
            parts = [p for p in path.split("/") if p]
            try:
                if parts[:2] == ["api", "characters"] and len(parts) == 3:
                    ok = app.delete_character(parts[2])
                    self._send_json({"ok": ok}, 200 if ok else 404)
                    return
                if parts[:2] == ["api", "scenes"] and len(parts) == 3:
                    try:
                        ok = app.delete_scene(parts[2])
                    except SceneBusyError as exc:
                        self._send_error_json(str(exc), 409)
                        return
                    except ValueError as exc:
                        self._send_error_json(str(exc), 400)
                        return
                    self._send_json({"ok": ok}, 200 if ok else 404)
                    return
                self._send_error_json("Not found", 404)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    return Handler


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def serve(
    data_dir: str | Path | None = None,
    port: int = DEFAULT_PORT,
    app: App | None = None,
) -> ThreadingHTTPServer:
    application = app or App(data_dir)
    if application.config.load_error:
        raise ValueError(application.config.load_error)
    application.config.scenes_dir.mkdir(parents=True, exist_ok=True)
    application.config.memory_dir.mkdir(parents=True, exist_ok=True)
    application.config.creative_dir.mkdir(parents=True, exist_ok=True)
    application.config.save()
    # A persisted "running" flag has no worker after a restart or crash.
    for meta in load_scene_meta(application.config.scenes_dir):
        if meta.get("status") == "running":
            meta["status"] = "stopped"
            write_scene_meta(application.config.scenes_dir, meta)

    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(application))
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="HeyAllM — two characters, one scene")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", default=None, help="Override the data directory")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    application = App(args.data_dir)
    try:
        server = serve(app=application, port=args.port)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"heyallm: {exc}\n")
    log.info(
        "HeyAllM on http://127.0.0.1:%d  data=%s  characters=%d",
        args.port, application.config.data_dir, len(application.config.characters),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        for engine in application.engines.values():
            engine.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
