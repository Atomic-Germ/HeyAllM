"""How each character sees the scene.

Two rules drive everything here:

1. **Sides stay straight.**  A character's own past turns are `assistant`
   messages; everything the *other* character said or did is a `user` message.
   To each model, the other character occupies the human's side of the
   exchange.
2. **Narration is not chat.**  Input from the human operator or the director
   arrives wrapped in `narration.prefix` so it reads as the world moving
   around them rather than as a line of dialogue.

The system prompt itself insists this is not a chat at all -- the characters
believe they are people in a scene, using the classic `*action*` convention to
actually do things.
"""

from __future__ import annotations

import re
from typing import Iterable

from .config import Character, Config, Endpoint

# Matches *action* spans. Deliberately single-line: an action is one beat.
_ACTION_RE = re.compile(r"(\*[^*\n]+\*)")

# The model may reach for either quote style when speaking.
_SPEECH_RE = re.compile(r'"([^"\n]*)"|“([^"\n]*)”')


def render_demographics(character: Character) -> str:
    block = character.demographics_block()
    return block or "(nothing recorded)"


# What a person in the scene can legitimately notice about the other one.
# History, wants and persona stay private -- they are the other's inner life.
OBSERVABLE_FIELDS = ("appearance", "mannerisms", "speech_style")


def render_observable(character: Character) -> str:
    lines = []
    for key in OBSERVABLE_FIELDS:
        value = (character.demographics.get(key) or "").strip()
        if value:
            lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    return "\n".join(lines) or "(nothing yet -- you have not paid attention)"


def build_system_prompt(
    config: Config,
    character: Character,
    counterpart: Character,
    setting: str,
    memory_text: str = "",
) -> str:
    """The full system prompt for one character's view of a scene."""

    narration_prefix = config.narration.prefix
    persona = character.persona.strip() or "(no persona written yet)"
    setting = setting.strip() or "an unspecified place"

    memory_block = memory_text.strip() or (
        "Nothing yet. This is where the record of what has already happened "
        "will be written."
    )
    silence_instruction = (
        f"Call the stay_silent tool, or reply with nothing but {config.engine.silence_sentinel}."
        if config.engine.use_silence_tool
        else f"Reply with nothing but {config.engine.silence_sentinel}."
    )

    lines = [
        f"You are {character.real_name or character.display_name()}. "
        f"People know you as {character.screenname or character.display_name()}.",
        "",
        "WHO YOU ARE",
        render_demographics(character),
        "",
        persona,
        "",
        "THE SITUATION",
        f"You are here: {setting}.",
        "",
        "This is not a chat, not an interface, not a transcript. You are a person, "
        "in a place, with a body. Things happen around you and you are one of them. "
        "You have no knowledge of any screen, model, prompt, or system, and you "
        "never refer to any of them.",
        "",
        "THE PERSON WITH YOU",
        f"{counterpart.real_name or counterpart.display_name()} "
        f"({counterpart.screenname or counterpart.display_name()}) is here with you. "
        "They are a person, not a participant. Whatever they do or say reaches you "
        "directly. You never write their words or their actions for them.",
        "",
        "WHAT YOU CAN SEE ABOUT THEM",
        render_observable(counterpart),
        "",
        "HOW YOU TAKE YOUR TURN",
        "Act. *Actions go in asterisks.* You are actually doing the thing you write -- "
        "reaching, turning, sitting down, leaving. You do not describe messaging anyone.",
        'Speak, if you have something to say. "Words go in quotation marks."',
        "You may act and speak in the same turn. A turn that is only an action is "
        "normal, and often better than one that is only words.",
        "",
        "Do nothing: if there is nothing left for you to do or say, stay silent. "
        + silence_instruction + " Silence is a real option. Use it when "
        "the scene has run out of somewhere to go -- it is how a conversation "
        "ends naturally.",
        "",
        "KEEPING YOUR SIDE STRAIGHT",
        "Your own past words and actions are in the assistant role. Everything in the "
        "user role comes from "
        f"{counterpart.screenname or counterpart.display_name()} or from the world "
        "around you. Never confuse the two, and never speak for them.",
        "",
        "NARRATION",
        f"Lines that begin {narration_prefix} are narration: the world moving, "
        "something arriving, something you perceive. They are not spoken by anyone "
        "present. They are true.",
        "",
        "WHAT HAS ALREADY HAPPENED",
        "A record, kept in the third person, of the events of this scene and of your "
        "life before it:",
        memory_block,
        "",
        "Reply with your turn only -- your action and/or your words. Nothing else.",
    ]
    return "\n".join(lines)


def _narration_text(config: Config, text: str) -> str:
    return f"{config.narration.prefix} {text}"


def build_messages(
    config: Config,
    character: Character,
    counterpart: Character,
    setting: str,
    events: Iterable[dict],
    memory_text: str = "",
) -> list[dict]:
    """Turn the transcript into this character's private view of it.

    Own lines -> assistant.  The other character's lines -> user.  Narration
    and beat-of-silence markers -> user, framed as narrative.
    """
    messages: list[dict] = [
        {
            "role": "system",
            "content": build_system_prompt(config, character, counterpart, setting, memory_text),
        }
    ]

    for event in events:
        kind = event.get("kind")
        text = (event.get("text") or "").strip()
        speaker = event.get("character_id")

        if kind == "scene":
            continue
        if kind == "silence":
            # A beat passes. No attribution, so neither side looks like it spoke.
            messages.append(
                {
                    "role": "user",
                    "content": _narration_text(
                        config, "A beat of silence. Nothing is offered in return."
                    ),
                }
            )
            continue
        if not text:
            continue

        if kind == "message" and speaker == character.id:
            messages.append({"role": "assistant", "content": text})
        elif kind == "message":
            # The other character's line, seen from the human's side of the table.
            messages.append({"role": "user", "content": text})
        elif kind == "narration":
            messages.append({"role": "user", "content": _narration_text(config, text)})

    return messages


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def detect_silence(text: str, sentinel: str) -> bool:
    """True when the model declined to take a turn."""
    stripped = (text or "").strip()
    if not stripped:
        return True
    return sentinel in stripped


def strip_silence_sentinel(text: str, sentinel: str) -> str:
    return (text or "").replace(sentinel, "").strip()


def segments(text: str) -> list[tuple[str, str]]:
    """Split a turn into (kind, payload) where kind is 'action' or 'speech'.

    Anything that is neither falls out as 'text'.  Used for rendering only --
    the stored transcript keeps the model's raw output.
    """
    if not text:
        return []
    out: list[tuple[str, str]] = []
    pos = 0
    for match in _ACTION_RE.finditer(text):
        before = text[pos:match.start()]
        if before.strip():
            out.append(("text", before.strip()))
        out.append(("action", match.group(1)[1:-1].strip()))
        pos = match.end()
    tail = text[pos:]
    if tail.strip():
        out.append(("text", tail.strip()))
    return out


def is_action_only(text: str) -> bool:
    """True when the turn is nothing but *actions*."""
    parts = segments(text)
    if not parts:
        return False
    return all(kind == "action" for kind, _ in parts)


def speech_of(text: str) -> list[str]:
    """Every quoted line in a turn."""
    return [m.group(1) or m.group(2) for m in _SPEECH_RE.finditer(text or "")]


def endpoint_label(endpoint: Endpoint) -> str:
    model = endpoint.model or "default"
    return f"{endpoint.base_url} · {model}"
