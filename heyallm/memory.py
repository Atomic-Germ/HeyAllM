"""Episodic memory: the third-person chronicle of a scene.

The director model compacts raw events into a running chronicle that is
*deliberately never first person for any character*.  That matters because the
chronicle is later handed straight back to a character as their pre-existing
past -- it has to read as a record of things that happened to them, not as
someone's diary.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .config import Character, Endpoint, atomic_write
from .llm import LLMClient, LLMError, LLMResult
from .transcript import Event

log = logging.getLogger("heyallm.memory")

_FIRST_PERSON = re.compile(r"\b(I|I'm|I've|I'd|me|my|mine|myself|we|we're|our|ours|us)\b", re.I)
# Quoted speech legitimately contains first person; the *voice* must not.
_QUOTED = re.compile(r'"[^"]*"|“[^”]*”|' + r"'[^']*'")

_COMPACT_RULES = """You are the chronicler of these events. Fold the NEW EVENTS into the existing chronicle.

Rules:
- Third person throughout. The chronicle's own voice never uses "I", "me", "my", "we", "our". First person may appear ONLY inside a character's quoted speech.
- Do not address anyone, do not comment, do not evaluate. Write only what happened.
- Render actions and reported speech: who did what, who said what, what changed.
- Keep names exactly as given. Never invent characters or events that are not in the source.
- Dense and factual -- roughly one paragraph per dozen events. It must stand alone, because a character will later read it as their own past.
- Output ONLY the full updated chronicle (existing + new). No preamble, no headings, no commentary.
"""

_EVENT_LABEL = {
    "message": "says",
    "narration": "(narration)",
    "silence": "(silence)",
    "scene": "(scene opens)",
}


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def render_events(events: list[Event], characters: dict[str, Character]) -> str:
    """Flatten events into the plain lines the chronicler works from."""
    lines: list[str] = []
    for event in events:
        stamp = _clock(event.ts)
        if event.kind == "scene":
            lines.append(f"[{stamp}] (scene opens) {event.text}")
            continue
        if event.kind == "narration":
            lines.append(f"[{stamp}] (narration) {event.text}")
            continue
        if event.kind == "silence":
            name = _name(characters, event.character_id)
            lines.append(f"[{stamp}] {name} stays silent.")
            continue
        name = _name(characters, event.character_id)
        lines.append(f"[{stamp}] {name} {_EVENT_LABEL['message']} {event.text}")
    return "\n".join(lines)


def _name(characters: dict[str, Character], char_id: str | None) -> str:
    if not char_id:
        return "Someone"
    char = characters.get(char_id)
    if not char:
        return char_id
    return char.real_name or char.display_name()


def is_third_person(text: str) -> bool:
    """Heuristic: does the chronicle's *voice* use first person?

    Quoted speech is stripped first, so `"I'm fine," Mara said` passes while
    `I walked to the door` fails.
    """
    without_quotes = _QUOTED.sub(" ", text or "")
    return not _FIRST_PERSON.search(without_quotes)


@dataclass
class Chronicle:
    scene_id: str = ""
    text: str = ""
    compacted_upto: int = 0
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def load(cls, path: Path) -> "Chronicle":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        try:
            updated_at = float(data.get("updated_at") or time.time())
            return cls(
                scene_id=str(data.get("scene_id") or ""),
                text=str(data.get("text") or ""),
                compacted_upto=max(0, int(data.get("compacted_upto") or 0)),
                updated_at=updated_at if math.isfinite(updated_at) else time.time(),
            )
        except (TypeError, ValueError, OverflowError):
            return cls()

    def save(self, path: Path) -> None:
        self.updated_at = time.time()
        atomic_write(path, json.dumps(asdict(self), indent=2, ensure_ascii=False))


def compact(
    client: LLMClient,
    endpoint: Endpoint,
    chronicle: Chronicle,
    pending: list[Event],
    characters: dict[str, Character],
    *,
    temperature: float = 0.4,
    max_tokens: int = 1400,
    chat_fn: Callable[..., LLMResult] | None = None,
) -> str:
    """Fold `pending` into the chronicle. Returns the updated text.

    Raises LLMError when the director cannot be reached; the caller decides
    whether that is fatal (it should not be -- memory compaction is best-effort).
    """
    if not pending:
        return chronicle.text

    new_lines = render_events(pending, characters)
    existing = chronicle.text.strip() or "(nothing recorded yet)"

    prompt = (
        _COMPACT_RULES
        + "\nEXISTING CHRONICLE:\n"
        + existing
        + "\n\nNEW EVENTS:\n"
        + new_lines
        + "\n\nUPDATED CHRONICLE:"
    )

    messages = [{"role": "user", "content": prompt}]
    chat = chat_fn or client.chat
    result = chat(
        endpoint,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=(10, 240),
    )
    text = (result.text or "").strip()

    if text and not is_third_person(text):
        # One corrective pass; the voice has to be right for episodic memory.
        log.warning("Chronicle came back first-person -- retrying once")
        messages.append({"role": "assistant", "content": text})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Rewrite that chronicle in a strictly third-person voice. "
                    "Remove first-person narration entirely; first person may remain "
                    "only inside quoted speech. Output only the rewritten chronicle."
                ),
            }
        )
        retry = chat(
            endpoint, messages, temperature=temperature, max_tokens=max_tokens, timeout=(10, 240)
        )
        candidate = (retry.text or "").strip()
        if not candidate:
            raise LLMError("Director returned an empty chronicle on retry")
        if not is_third_person(candidate):
            raise LLMError("Director could not produce a third-person chronicle")
        text = candidate

    if not text:
        raise LLMError("Director returned an empty chronicle")
    return text


def format_for_character(records: list[Chronicle], character: Character) -> str:
    """Chronicles this character was present for, ready for a system prompt."""
    parts = [r.text.strip() for r in records if r.text.strip()]
    if not parts:
        return ""
    return "\n\n".join(parts)
