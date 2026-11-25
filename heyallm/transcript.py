"""Scene transcripts: append-only JSONL, one file per scene."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from .config import atomic_write, new_id

KINDS = {"message", "narration", "silence", "scene"}


@dataclass
class Event:
    kind: str
    text: str = ""
    character_id: str | None = None
    turn: int = 0
    id: str = field(default_factory=lambda: new_id("evt"))
    ts: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        kind = str(data.get("kind") or "message")
        if kind not in KINDS:
            kind = "message"
        ts = float(data.get("ts") or time.time())
        character_id = data.get("character_id")
        return cls(
            kind=kind,
            text=str(data.get("text") or ""),
            character_id=str(character_id) if character_id else None,
            turn=int(data.get("turn") or 0),
            id=str(data.get("id") or new_id("evt")),
            ts=ts if math.isfinite(ts) else time.time(),
        )

    def to_dict(self) -> dict:
        return asdict(self)


class Transcript:
    """A single scene's ordered event log.

    Reads are cheap (the file is small); writes are append-only so a crash
    mid-scene loses at most the last line.  A lock guards the in-memory list
    because the engine thread and the HTTP threads both touch it.
    """

    def __init__(self, path: Path, scene: dict | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scene: dict = scene or {}
        self._events: list[Event] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if not isinstance(data, dict):
                            continue
                        self._events.append(Event.from_dict(data))
                    except (json.JSONDecodeError, TypeError, ValueError, OverflowError):
                        continue  # tolerate a torn or malformed record
        except OSError:
            return

    # ---------------------------------------------------------------- writes
    def append(self, event: Event) -> Event:
        payload = (json.dumps(event.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            with self.path.open("a+b") as fh:
                # A recovered torn tail may not end in a newline. Separate it
                # from the new record without discarding any valid earlier data.
                fh.seek(0, 2)
                if fh.tell():
                    fh.seek(-1, 2)
                    if fh.read(1) != b"\n":
                        fh.write(b"\n")
                fh.write(payload)
            self._events.append(event)
        return event

    def add(
        self,
        kind: str,
        text: str = "",
        character_id: str | None = None,
        turn: int = 0,
    ) -> Event:
        return self.append(Event(kind=kind, text=text, character_id=character_id, turn=turn))

    # ----------------------------------------------------------------- reads
    @property
    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def since(self, index: int) -> list[Event]:
        with self._lock:
            return list(self._events[index:])

    def dict_events(self) -> list[dict]:
        return [e.to_dict() for e in self.events]

    def count_kind(self, kind: str) -> int:
        with self._lock:
            return sum(1 for e in self._events if e.kind == kind)

    def next_turn(self) -> int:
        """The turn number for the next message or silence."""
        with self._lock:
            return sum(e.kind in {"message", "silence"} for e in self._events) + 1

    # ----------------------------------------------------------- convenience
    def last_message(self) -> Event | None:
        """The last posted message, used to pace the following turn."""
        with self._lock:
            for event in reversed(self._events):
                if event.kind == "message":
                    return event
        return None

    def last_message_at(self) -> float:
        """Timestamp of the last posted message -- the cooldown clock starts here."""
        event = self.last_message()
        return event.ts if event else 0.0

    def iter_messages(self) -> Iterator[Event]:
        for event in self.events:
            if event.kind == "message":
                yield event


def load_scene_meta(scenes_dir: Path) -> list[dict]:
    """Read every scene's sidecar metadata file, newest first."""
    out: list[dict] = []
    if not scenes_dir.exists():
        return out
    for path in scenes_dir.glob("*.meta.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    out.sort(key=lambda d: str(d.get("created_at") or ""), reverse=True)
    return out


def write_scene_meta(scenes_dir: Path, scene: dict) -> None:
    scene_id = str(scene.get("id") or "")
    if not scene_id:
        return
    atomic_write(scenes_dir / f"{scene_id}.meta.json", json.dumps(scene, indent=2, ensure_ascii=False))


def delete_scene(scenes_dir: Path, scene_id: str) -> bool:
    removed = False
    for path in (scenes_dir / f"{scene_id}.jsonl", scenes_dir / f"{scene_id}.meta.json"):
        if path.exists():
            try:
                path.unlink()
                removed = True
            except OSError:
                pass
    return removed
