"""Native creative-meditation tools for the director.

The meditation -> insight -> ponder flow is adapted from the old
``creative-thinking`` MCP service. It runs in-process: no Node runtime, MCP
server, embedding model, or network service is required.

Copyright (c) 2025 MCP Creative Meditation Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

from __future__ import annotations

import copy
import json
import logging
import random
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import atomic_write, new_id

HISTORY_LIMIT = 100
logger = logging.getLogger(__name__)

WORD_POOLS = [
    ("quantum flux essence void nexus spiral echo shimmer threshold weave fractal "
     "resonance entropy harmony paradox catalyst emergence confluence infinity "
     "lattice dimension singularity cascade ripple prism spectrum coherence"),
    ("spoon doorknob elevator receipt coffee staircase notebook window umbrella "
     "keyboard mirror towel lamp chair socket cable battery ticket coin train"),
    ("packet latency buffer kernel cursor firmware console cache syntax runtime "
     "daemon protocol hash opcode heap pointer overflow checksum sandbox pipeline"),
    ("melts oscillates flickers loops spills dissolves folds reboots mutates drifts "
     "erases splices reverses decompresses scrambles fractures remixes"),
    ("longing quiet-joy melancholy irritation awe tension relief nostalgia "
     "anticipation dread gratitude tenderness restlessness confusion clarity curiosity"),
    ("corridor rooftop subway waiting-room attic crosswalk forest-edge parking-lot "
     "kitchen-sink airport-gate empty-theatre hotel-lobby data-center harbor"),
    ("4183 0000 13 1010 404 3.1415 0xDEAD 2049 infinity 11:11 7e3 001101 5/8 9.81"),
    ("#? therefore approximately /dev/null ctrl-c percent-temp psi delta star"),
]
POOLS = [pool.split() for pool in WORD_POOLS]

TEMPLATES = [
    "Between {a} and {b}, {c} {d} around {e}.",
    "{a} drifts through {b}, annotated by {c} and forgotten {d}.",
    "In the corridor between {a} and {b}, {c} quietly {d}.",
    "{a} remembers {b} while {c} glitches beside {d}.",
    "{a} is a note scribbled in the margin of {b} {c}.",
]

CREATIVE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "creative_meditate",
            "description": (
                "Generate a surreal emergent sentence by colliding random conceptual "
                "fragments with context from the scene. Use when inventing a character, "
                "looking for an unexpected narrative beat, or breaking a creative block. "
                "A seed makes the result reproducible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "context_words": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Scene words or short phrases to use as creative anchors (3-10 work well).",
                    },
                    "num_random_words": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 32,
                        "default": 12,
                    },
                    "seed": {
                        "type": "string",
                        "description": "Optional reproducible random seed.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "creative_insight",
            "description": (
                "Extract themes and patterns from a meditation. Defaults to the latest "
                "meditation; pass meditation_id to revisit an earlier one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "meditation_id": {"type": "string"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "creative_ponder",
            "description": (
                "Ask the director for a deeper reflection on an insight, then save the "
                "result. Defaults to the latest insight. Use after creative_insight when "
                "a second perspective would help; do not call it by default."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "insight_text": {"type": "string"},
                },
                "required": [],
            },
        },
    },
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_context(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("context_words must be an array of strings")
    words = []
    for item in value[:12]:
        if not isinstance(item, str):
            raise ValueError("context_words must contain only strings")
        word = item.strip()
        if word:
            words.append(word[:120])
    return words


def _random_count(value: Any) -> int:
    if value is None:
        return 12
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("num_random_words must be an integer") from exc
    if isinstance(value, float) and value != count:
        raise ValueError("num_random_words must be an integer")
    if not 1 <= count <= 32:
        raise ValueError("num_random_words must be between 1 and 32")
    return count


def _sentence(rng: random.Random, words: list[str], context: list[str]) -> str:
    def pick() -> str:
        if context and rng.random() < 0.4:
            return rng.choice(context)
        return rng.choice(words)

    template = rng.choice(TEMPLATES)
    first = rng.choice(context) if context else pick()
    return template.format(a=first, b=pick(), c=pick(), d=pick(), e=pick())


def _interpretation(random_words: list[str], context: list[str], sentence: str) -> str:
    sample = ", ".join(random_words[:7]) or "(none)"
    anchors = ", ".join(context) or "(none)"
    lowered = sentence.lower()
    if "void" in lowered:
        theme = "emptiness and potential"
    elif "resonance" in lowered or "harmony" in lowered:
        theme = "connections that appear without an obvious cause"
    else:
        theme = "the shapes that emerge when unrelated things share a frame"
    context_note = (
        "The supplied context acts as a partial key, biasing the collision toward the scene."
        if context
        else "With no explicit context, noise itself becomes both cipher and key."
    )
    return (
        f"From the interplay of random ({sample}) and contextual elements ({anchors}), "
        f'emerges: "{sentence}"\n\n'
        f"This suggests a contemplation on {theme}. {context_note} "
        "Treat it as a creative invitation, not a factual statement."
    )


def _insights_for(meditation: dict) -> list[str]:
    sentence = str(meditation.get("emergent_sentence") or "")
    context = meditation.get("context_words") or []
    words = {word.lower() for word in meditation.get("random_words") or []}
    insights = [f'Core emergence: "{sentence}"'] if sentence else []
    if context:
        insights.append("Contextual anchors gave the collision structure: " + ", ".join(context))
    if words & {"void", "infinity", "eternal"}:
        insights.append("The meditation touched on boundlessness and transcendence.")
    if words & {"harmony", "resonance", "coherence"}:
        insights.append("Disparate elements suggested alignment or synchronization.")
    if words & {"paradox", "entropy", "flux"}:
        insights.append("The image held tension between order and disorder.")
    if len(insights) == 1:
        insights.append("The meditation exposed the gap between randomness and meaning-making.")
    return insights


def _fallback_ponder(source: str) -> str:
    return (
        "The insight becomes more interesting when its opposites are allowed into view:\n\n"
        f"{source}\n\n"
        "What would this image look like if its quietest detail were the most important? "
        "Which connection feels surprising rather than merely decorative? The useful "
        "material may be the tension between the literal fragments and the story they imply."
    )


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _require_fields(record: dict, fields: tuple[str, ...], location: str) -> None:
    missing = [field for field in fields if field not in record]
    if missing:
        raise ValueError(f"{location} is missing fields: {', '.join(missing)}")


def _validate_state(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise ValueError("state root must be a JSON object")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError("state version must be 1")

    fields = (
        "last_meditation_id",
        "last_insight_id",
        "meditations",
        "insights",
        "ponderings",
    )
    _require_fields(raw, fields, "state")
    meditations = raw["meditations"]
    insights = raw["insights"]
    ponderings = raw["ponderings"]
    if not isinstance(meditations, dict):
        raise ValueError("meditations must be a JSON object")
    if not isinstance(insights, dict):
        raise ValueError("insights must be a JSON object")
    if not isinstance(ponderings, list):
        raise ValueError("ponderings must be a JSON array")

    for record_id, record in meditations.items():
        location = f"meditations[{record_id!r}]"
        if not isinstance(record_id, str) or not record_id or not isinstance(record, dict):
            raise ValueError(f"{location} must be an object with a non-empty string id")
        _require_fields(record, (
            "id", "random_words", "context_words", "emergent_sentence",
            "interpretation", "seed", "created_at",
        ), location)
        if record["id"] != record_id:
            raise ValueError(f"{location}.id must match its dictionary key")
        if not _is_string_list(record["random_words"]):
            raise ValueError(f"{location}.random_words must be an array of strings")
        if not _is_string_list(record["context_words"]):
            raise ValueError(f"{location}.context_words must be an array of strings")
        if not isinstance(record["emergent_sentence"], str):
            raise ValueError(f"{location}.emergent_sentence must be a string")
        if not isinstance(record["interpretation"], str):
            raise ValueError(f"{location}.interpretation must be a string")
        if record["seed"] is not None and not isinstance(record["seed"], str):
            raise ValueError(f"{location}.seed must be a string or null")
        if not isinstance(record["created_at"], str):
            raise ValueError(f"{location}.created_at must be a string")

    for record_id, record in insights.items():
        location = f"insights[{record_id!r}]"
        if not isinstance(record_id, str) or not record_id or not isinstance(record, dict):
            raise ValueError(f"{location} must be an object with a non-empty string id")
        _require_fields(record, (
            "id", "meditation_id", "insights", "created_at",
        ), location)
        if record["id"] != record_id:
            raise ValueError(f"{location}.id must match its dictionary key")
        if not isinstance(record["meditation_id"], str) or not record["meditation_id"]:
            raise ValueError(f"{location}.meditation_id must be a non-empty string")
        if not _is_string_list(record["insights"]):
            raise ValueError(f"{location}.insights must be an array of strings")
        if not isinstance(record["created_at"], str):
            raise ValueError(f"{location}.created_at must be a string")

    for index, record in enumerate(ponderings):
        location = f"ponderings[{index}]"
        if not isinstance(record, dict):
            raise ValueError(f"{location} must be a JSON object")
        _require_fields(record, (
            "id", "insight_text", "reflection", "method", "created_at",
        ), location)
        if not isinstance(record["id"], str) or not record["id"]:
            raise ValueError(f"{location}.id must be a non-empty string")
        for field in ("insight_text", "reflection", "method", "created_at"):
            if not isinstance(record[field], str):
                raise ValueError(f"{location}.{field} must be a string")

    for pointer, records in (
        ("last_meditation_id", meditations),
        ("last_insight_id", insights),
    ):
        value = raw[pointer]
        if value is not None and (
            not isinstance(value, str) or not value or value not in records
        ):
            raise ValueError(f"{pointer} must be null or reference an existing record")


class CreativeToolbox:
    """Thread-safe, atomically persisted creative tool state."""

    def __init__(self, data_dir: Path | str, *, namespace: str | None = None) -> None:
        root = Path(data_dir) / "creative"
        if namespace:
            root = root / namespace
        self.state_path = root / "state.json"
        self._lock = threading.RLock()
        self._meditations: dict[str, dict] = {}
        self._insights: dict[str, dict] = {}
        self._ponderings: list[dict] = []
        self._last_meditation_id: str | None = None
        self._last_insight_id: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for attribute, key in (("_meditations", "meditations"), ("_insights", "insights")):
            records = raw.get(key)
            loaded = {}
            if isinstance(records, dict):
                for record_id, record in records.items():
                    if isinstance(record_id, str) and isinstance(record, dict):
                        loaded[record_id] = record
            setattr(self, attribute, loaded)
        ponderings = raw.get("ponderings")
        if isinstance(ponderings, list):
            self._ponderings = [record for record in ponderings if isinstance(record, dict)]
        last_meditation = raw.get("last_meditation_id")
        last_insight = raw.get("last_insight_id")
        self._last_meditation_id = (
            last_meditation
            if isinstance(last_meditation, str) and last_meditation in self._meditations
            else None
        )
        self._last_insight_id = (
            last_insight
            if isinstance(last_insight, str) and last_insight in self._insights
            else None
        )

    def _save(self) -> None:
        atomic_write(self.state_path, json.dumps({
            "version": 1,
            "last_meditation_id": self._last_meditation_id,
            "last_insight_id": self._last_insight_id,
            "meditations": self._meditations,
            "insights": self._insights,
            "ponderings": self._ponderings,
        }, indent=2, ensure_ascii=False))

    @staticmethod
    def _store(records: dict[str, dict], record: dict) -> None:
        records[record["id"]] = record
        while len(records) > HISTORY_LIMIT:
            records.pop(next(iter(records)))

    def meditate(self, args: dict) -> dict:
        context = _clean_context(args.get("context_words"))
        count = _random_count(args.get("num_random_words"))
        seed = args.get("seed")
        if seed is not None and not isinstance(seed, str):
            raise ValueError("seed must be a string")
        rng = random.Random((seed or "").strip()[:200] or None)
        random_words = [rng.choice(rng.choice(POOLS)) for _ in range(count)]
        words = random_words + context
        if not words:
            sentence = "The fragments resist coalescence; meaning remains diffuse."
        else:
            candidates = [_sentence(rng, words, context) for _ in range(3)]
            sentence = max(
                candidates,
                key=lambda candidate: sum(word.lower() in candidate.lower() for word in context),
            )
        record = {
            "id": new_id("meditation"),
            "random_words": random_words,
            "context_words": context,
            "emergent_sentence": sentence,
            "interpretation": _interpretation(random_words, context, sentence),
            "seed": seed,
            "created_at": _now(),
        }
        with self._lock:
            self._store(self._meditations, record)
            self._last_meditation_id = record["id"]
            self._save()
        return copy.deepcopy(record)

    def insight(self, args: dict) -> dict:
        meditation_id = args.get("meditation_id") or self._last_meditation_id
        if not meditation_id:
            raise ValueError("No meditation found. Call creative_meditate first.")
        with self._lock:
            meditation = self._meditations.get(str(meditation_id))
            if meditation is None:
                raise ValueError(f"Unknown meditation: {meditation_id}")
            record = {
                "id": new_id("insight"),
                "meditation_id": meditation["id"],
                "insights": _insights_for(meditation),
                "created_at": _now(),
            }
            self._store(self._insights, record)
            self._last_insight_id = record["id"]
            self._save()
        return copy.deepcopy(record)

    def insight_source(self, args: dict) -> str:
        explicit = args.get("insight_text")
        if explicit is not None:
            if not isinstance(explicit, str) or not explicit.strip():
                raise ValueError("insight_text must be a non-empty string")
            return explicit.strip()[:4000]
        with self._lock:
            if not self._last_insight_id:
                raise ValueError("No insight found. Call creative_insight first.")
            insight = self._insights[self._last_insight_id]
            return "\n".join(insight["insights"])

    def save_ponder(self, source: str, reflection: str, method: str) -> dict:
        reflection = (reflection or "").strip()
        if not reflection:
            raise ValueError("The director returned an empty reflection")
        record = {
            "id": new_id("ponder"),
            "insight_text": source,
            "reflection": reflection,
            "method": method,
            "created_at": _now(),
        }
        with self._lock:
            self._ponderings.append(record)
            del self._ponderings[:-HISTORY_LIMIT]
            self._save()
        return copy.deepcopy(record)

    def call(
        self,
        name: str,
        args: dict,
        *,
        ponder: Callable[[str], str] | None = None,
    ) -> dict:
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be a JSON object")
        if name == "creative_meditate":
            return self.meditate(args)
        if name == "creative_insight":
            return self.insight(args)
        if name == "creative_ponder":
            source = self.insight_source(args)
            if ponder is None:
                return self.save_ponder(source, _fallback_ponder(source), "internal")
            return self.save_ponder(source, ponder(source), "director")
        raise ValueError(f"Unknown creative tool: {name}")
