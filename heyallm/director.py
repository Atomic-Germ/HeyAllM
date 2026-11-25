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
from .llm import LLMClient, LLMError

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


def create_character(
    client: LLMClient,
    endpoint: Endpoint,
    *,
    seed: str = "",
    character_endpoint: Endpoint | None = None,
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
    result = client.chat(
        endpoint,
        [
            {"role": "system", "content": CREATION_SYSTEM},
            {"role": "user", "content": prompt},
        ],
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
