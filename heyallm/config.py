"""Configuration and character records.

Everything is plain dataclasses that round-trip to JSON.  File writes are
atomic (write to a sibling .tmp, then replace) so a crash mid-save never
truncates the config -- same rule the Bubble daemon follows.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Fields the director is asked to fill in when a character is created.
# Extra keys are allowed and preserved; these are just the guaranteed set.
DEMOGRAPHIC_FIELDS = [
    "age",
    "gender",
    "occupation",
    "location",
    "appearance",
    "mannerisms",
    "speech_style",
    "history",
    "wants",
]

SILENCE_SENTINEL = "[[SILENT]]"


def default_data_dir() -> Path:
    """XDG-ish data directory, overridable with HEYALLM_DATA_DIR."""
    env = os.environ.get("HEYALLM_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "heyallm"


def atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def _as_float(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return number if math.isfinite(number) else fallback


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


@dataclass
class Endpoint:
    """One OpenAI-compatible server.

    Two characters pointing at the same base_url with different `model`
    values is the single-llama.cpp-routing-server case.
    """

    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = ""
    api_key: str | None = None
    timeout: float = 180.0

    @classmethod
    def from_dict(cls, data: dict | None) -> "Endpoint":
        data = _as_dict(data)
        key = str(data.get("api_key") or "").strip() or None
        base_url = str(data.get("base_url") or "").strip().rstrip("/") or cls.base_url
        return cls(
            base_url=base_url,
            model=str(data.get("model") or "").strip(),
            api_key=key,
            timeout=max(0.1, _as_float(data.get("timeout"), 180.0)),
        )

    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"


@dataclass
class Character:
    id: str
    screenname: str = ""
    real_name: str = ""
    demographics: dict[str, str] = field(default_factory=dict)
    persona: str = ""          # director-written body of the system prompt
    prompt_extra: str = ""     # human-supplied seed / directive
    endpoint: Endpoint = field(default_factory=Endpoint)
    created_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Character":
        data = _as_dict(data)
        demo = _as_dict(data.get("demographics"))
        return cls(
            id=str(data.get("id") or new_id("char")),
            screenname=str(data.get("screenname") or ""),
            real_name=str(data.get("real_name") or ""),
            demographics={str(k): str(v) for k, v in demo.items()},
            persona=str(data.get("persona") or ""),
            prompt_extra=str(data.get("prompt_extra") or ""),
            endpoint=Endpoint.from_dict(data.get("endpoint")),
            created_at=str(data.get("created_at") or ""),
        )

    def display_name(self) -> str:
        return self.screenname or self.real_name or self.id

    def demographics_block(self) -> str:
        lines = []
        for key, value in self.demographics.items():
            if value:
                label = key.replace("_", " ").strip().capitalize()
                lines.append(f"{label}: {value}")
        return "\n".join(lines)


@dataclass
class Cooldown:
    """Pacing between posted messages, modelled on human reading speed."""

    chars_per_second: float = 18.0   # ~215 wpm, a brisk reader
    min_seconds: float = 1.5
    max_seconds: float = 15.0

    @classmethod
    def from_dict(cls, data: dict | None) -> "Cooldown":
        data = _as_dict(data)
        minimum = max(0.0, _as_float(data.get("min_seconds"), 1.5))
        maximum = max(0.0, _as_float(data.get("max_seconds"), 15.0))
        return cls(
            chars_per_second=max(0.1, _as_float(data.get("chars_per_second"), 18.0)),
            min_seconds=minimum,
            max_seconds=max(minimum, maximum),
        )

    def delay_for(self, text: str) -> float:
        if self.chars_per_second <= 0:
            return self.min_seconds
        raw = len(text) / self.chars_per_second
        return max(self.min_seconds, min(self.max_seconds, raw))


@dataclass
class EngineCfg:
    max_turns: int = 60
    max_consecutive_silences: int = 2
    use_silence_tool: bool = True
    silence_sentinel: str = SILENCE_SENTINEL
    temperature: float = 0.9
    max_tokens: int = 420
    first_speaker: int = 0            # index into the scene's character list
    director_end_check: bool = False  # ask the director after each post

    @classmethod
    def from_dict(cls, data: dict | None) -> "EngineCfg":
        data = _as_dict(data)
        return cls(
            max_turns=max(1, _as_int(data.get("max_turns"), 60)),
            max_consecutive_silences=max(1, _as_int(data.get("max_consecutive_silences"), 2)),
            use_silence_tool=bool(data.get("use_silence_tool", True)),
            silence_sentinel=str(data.get("silence_sentinel") or SILENCE_SENTINEL),
            temperature=_as_float(data.get("temperature"), 0.9),
            max_tokens=max(1, _as_int(data.get("max_tokens"), 420)),
            first_speaker=max(0, _as_int(data.get("first_speaker"), 0)),
            director_end_check=bool(data.get("director_end_check", False)),
        )


@dataclass
class NarrationCfg:
    """How human / director speech is dressed up as narrative."""

    prefix: str = "[the scene, unspoken]"

    @classmethod
    def from_dict(cls, data: dict | None) -> "NarrationCfg":
        data = _as_dict(data)
        return cls(prefix=str(data.get("prefix") or cls.prefix))


@dataclass
class MemoryCfg:
    auto_compact: bool = True
    compact_at_events: int = 12

    @classmethod
    def from_dict(cls, data: dict | None) -> "MemoryCfg":
        data = _as_dict(data)
        return cls(
            auto_compact=bool(data.get("auto_compact", True)),
            compact_at_events=max(2, _as_int(data.get("compact_at_events"), 12)),
        )


@dataclass
class Config:
    characters: list[Character] = field(default_factory=list)
    director: Endpoint = field(default_factory=Endpoint)
    cooldown: Cooldown = field(default_factory=Cooldown)
    engine: EngineCfg = field(default_factory=EngineCfg)
    narration: NarrationCfg = field(default_factory=NarrationCfg)
    memory: MemoryCfg = field(default_factory=MemoryCfg)
    data_dir: Path = field(default_factory=default_data_dir)
    load_error: str | None = field(default=None, init=False, repr=False, compare=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    # ---------------------------------------------------------------- paths
    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def scenes_dir(self) -> Path:
        return self.data_dir / "scenes"

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memory"

    def scene_path(self, scene_id: str) -> Path:
        return self.scenes_dir / f"{scene_id}.jsonl"

    def record_path(self, scene_id: str) -> Path:
        return self.memory_dir / f"{scene_id}.json"

    # ------------------------------------------------------------ characters
    def get_character(self, char_id: str) -> Character | None:
        for char in self.characters:
            if char.id == char_id:
                return char
        return None

    def remove_character(self, char_id: str) -> bool:
        before = len(self.characters)
        self.characters = [c for c in self.characters if c.id != char_id]
        return len(self.characters) != before

    @property
    def lock(self) -> threading.RLock:
        """Serialize mutations with their persistence in HTTP request threads."""
        return self._lock

    # ----------------------------------------------------------------- save
    def to_dict(self) -> dict:
        return {
            "version": 1,
            "characters": [asdict(c) for c in self.characters],
            "director": asdict(self.director),
            "cooldown": asdict(self.cooldown),
            "engine": asdict(self.engine),
            "narration": asdict(self.narration),
            "memory": asdict(self.memory),
        }

    def save(self) -> None:
        with self._lock:
            text = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
            atomic_write(self.config_path, text)

    @classmethod
    def load(cls, data_dir: Path | str | None = None) -> "Config":
        cfg = cls(data_dir=Path(data_dir).expanduser() if data_dir else default_data_dir())
        path = cfg.config_path
        if not path.exists():
            return cfg
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            cfg.load_error = f"Could not read {path}: {exc}"
            return cfg
        if not isinstance(raw, dict):
            cfg.load_error = f"Could not read {path}: expected a JSON object"
            return cfg
        characters = raw.get("characters")
        if isinstance(characters, list):
            cfg.characters = [Character.from_dict(c) for c in characters if isinstance(c, dict)]
        cfg.director = Endpoint.from_dict(raw.get("director"))
        cfg.cooldown = Cooldown.from_dict(raw.get("cooldown"))
        cfg.engine = EngineCfg.from_dict(raw.get("engine"))
        cfg.narration = NarrationCfg.from_dict(raw.get("narration"))
        cfg.memory = MemoryCfg.from_dict(raw.get("memory"))
        return cfg


def blank_character(endpoint: Endpoint | None = None) -> Character:
    """A shell character, ready for the director to fill in."""
    return Character(
        id=new_id("char"),
        endpoint=endpoint or Endpoint(),
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
