"""The third model: writes characters, and compacts memory.

This is the configurable non-player model.  A human supplies at most a seed
line; the director decides who the character actually is.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .config import Character, DEMOGRAPHIC_FIELDS, Endpoint
from .creative import CREATIVE_TOOLS, CreativeToolbox
from .llm import LLMClient, LLMError, LLMResult

log = logging.getLogger("heyallm.director")

CREATION_SYSTEM = """\
You create people for a scene. You will be given an optional seed and an
endpoint that says what kind of model will play this character.

Return ONLY a JSON object, no prose, no code fences, with exactly these keys:

{
  "screenname": "the name they go by -- short, lowercase, casual, the way it appears in a messenger",
  "real_name": "their full name as it appears on documents",
  "demographics": {
    "age": "...",
    "gender": "...",
    "occupation": "...",
    "location": "...",
    "appearance": "...",
    "mannerisms": "...",
    "speech_style": "...",
    "history": "...",
    "wants": "what they want right now, in this scene"
  },
  "persona": "..."
}

Rules:
- Make them specific and a little imperfect. No idealised people. Give them a
  habit, a pressure, a way of holding themselves.
- "mannerisms" must be physical and observable: what their hands do, how they
  stand, what they do when they are lying.
- "speech_style" must be concrete enough to imitate: sentence length, what
  they avoid saying, what they call things.
- "persona" is 4-6 sentences addressed to the character in the second person
  ("You ...") covering temperament, voice, posture, the flaw they carry, and
  what they want from the person they are about to be with. It is pasted
  straight into their system prompt.
- screenname must not be their real name. Real people use handles.
"""

CREATIVE_GUIDANCE = """\
You have optional creative meditation tools. Use creative_meditate when an
unexpected image would help, creative_insight to extract its themes, and
creative_ponder for a deeper second pass when useful. They are inspirations,
not facts. Do not mention tool names or tool machinery in your final answer.
"""

PONDER_SYSTEM = """\
You are a philosophical contemplator reflecting on a creative insight from a
scene. Find a surprising but grounded connection, ask one useful question, and
keep the reflection concise. Do not mention tools or systems.
"""

SEED_TEMPLATE = """\
Create a character.

Seed (may be empty): {seed}

The character will be played by this model:
  endpoint: {base_url}
  model: {model}

Return only the JSON object."""


def _extract_json(raw: str) -> dict[str, Any]:
    """Pull the first JSON object out of a possibly chatty response."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError(f"Director did not return JSON: {text[:200]!r}")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"Director returned unparseable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("Director returned a non-object JSON value")
    return data


def _tool_arguments(call: dict) -> dict:
    function = call.get("function")
    if not isinstance(function, dict):
        raise ValueError("Malformed tool call")
    raw = function.get("arguments", {})
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Tool arguments must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Tool arguments must be a JSON object")
    return parsed


def _director_reflection(client: LLMClient, endpoint: Endpoint, source: str) -> str:
    result = client.chat(
        endpoint,
        [
            {"role": "system", "content": PONDER_SYSTEM},
            {"role": "user", "content": source},
        ],
        temperature=0.65,
        max_tokens=600,
        timeout=(10, 120),
    )
    return result.text


def complete(
    client: LLMClient,
    endpoint: Endpoint,
    messages: list[dict],
    *,
    creative: CreativeToolbox | None = None,
    max_tool_calls: int = 4,
    temperature: float = 0.9,
    max_tokens: int = 420,
    timeout: float | tuple[float, float] | None = None,
) -> LLMResult:
    """Run a director completion, including an optional bounded tool loop."""
    if creative is None:
        return client.chat(
            endpoint, messages,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout,
        )
    if max_tool_calls < 1:
        raise ValueError("max_tool_calls must be at least 1")

    conversation = [dict(message) for message in messages]
    guidance = {"role": "system", "content": CREATIVE_GUIDANCE}
    if conversation and conversation[0].get("role") == "system":
        conversation[0]["content"] = str(conversation[0].get("content") or "") + "\n" + CREATIVE_GUIDANCE
    else:
        conversation.insert(0, guidance)

    calls_made = 0
    while True:
        result = client.chat(
            endpoint,
            conversation,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=CREATIVE_TOOLS,
            timeout=timeout,
        )
        if not result.tool_calls:
            return result
        if calls_made >= max_tool_calls:
            raise LLMError(f"Director exceeded the {max_tool_calls}-call creative tool limit")

        conversation.append({
            "role": "assistant",
            "content": result.text or "",
            "tool_calls": result.tool_calls,
        })
        for index, call in enumerate(result.tool_calls):
            if calls_made >= max_tool_calls:
                raise LLMError(f"Director exceeded the {max_tool_calls}-call creative tool limit")
            calls_made += 1
            call = call if isinstance(call, dict) else {}
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            name = str(function.get("name") or call.get("name") or "")
            call_id = str(call.get("id") or f"creative-{calls_made}-{index}")
            try:
                args = _tool_arguments(call)
                output = creative.call(
                    name,
                    args,
                    ponder=lambda source: _director_reflection(client, endpoint, source),
                )
            except Exception as exc:  # noqa: BLE001 - return tool errors to the model
                log.warning("Creative tool %s failed: %s", name or "(unknown)", exc)
                output = {"error": str(exc)}
            conversation.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": json.dumps(output, ensure_ascii=False),
            })


def create_character(
    client: LLMClient,
    endpoint: Endpoint,
    *,
    seed: str = "",
    character_endpoint: Endpoint | None = None,
    creative: CreativeToolbox | None = None,
    max_tool_calls: int = 4,
) -> dict:
    """Ask the director for a full character description.

    Returns the raw dict (screenname / real_name / demographics / persona).
    """
    ce = character_endpoint or Endpoint()
    prompt = SEED_TEMPLATE.format(
        seed=seed.strip() or "(none -- invent someone entirely)",
        base_url=ce.base_url,
        model=ce.model or "(whatever the server has loaded)",
    )
    result = complete(
        client,
        endpoint,
        [
            {"role": "system", "content": CREATION_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        creative=creative,
        max_tool_calls=max_tool_calls,
        temperature=1.0,
        max_tokens=1200,
        timeout=(10, 240),
    )
    data = _extract_json(result.text)
    return _normalise(data)


def _normalise(data: dict[str, Any]) -> dict:
    demo_raw = data.get("demographics")
    demographics: dict[str, str] = {}
    if isinstance(demo_raw, dict):
        for key, value in demo_raw.items():
            if value:
                demographics[str(key)] = str(value).strip()
    # Guarantee the standard fields exist so the settings form always has a
    # stable shape, even when the director left one blank.
    for key in DEMOGRAPHIC_FIELDS:
        demographics.setdefault(key, "")

    return {
        "screenname": str(data.get("screenname") or "").strip(),
        "real_name": str(data.get("real_name") or "").strip(),
        "demographics": demographics,
        "persona": str(data.get("persona") or "").strip(),
    }


def apply_to_character(character: Character, filled: dict) -> Character:
    character.screenname = filled.get("screenname") or character.screenname
    character.real_name = filled.get("real_name") or character.real_name
    demographics = filled.get("demographics")
    if isinstance(demographics, dict):
        # Normalisation guarantees standard keys, sometimes with blank values.
        # Missing director details must not erase demographics edited by hand.
        character.demographics.update(
            {str(k): str(v).strip() for k, v in demographics.items() if str(v).strip()}
        )
    if filled.get("persona"):
        character.persona = filled["persona"]
    if not character.created_at:
        character.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    return character
