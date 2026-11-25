"""The turn engine.

Strictly alternating: character, character, character... Each turn is

    compose (LLM)  ->  reading-speed cooldown  ->  post or stay silent

The cooldown is measured from the moment the *previous* message was posted, so
a long message is followed by a long pause -- the app paces itself the way a
person reads.  Nothing is revealed before the cooldown elapses.

The scene ends when the characters stop wanting to carry on: either one calls
the `stay_silent` tool (or writes the silence sentinel) often enough in a row,
or the turn cap is reached, or the human stops it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .config import Character, Config, EngineCfg
from .llm import LLMClient, LLMError, SILENT_TOOL
from . import prompts as P
from .transcript import Event, Transcript

log = logging.getLogger("heyallm.engine")

StatusCB = Callable[[dict], None]
EventCB = Callable[[dict], None]
EndCB = Callable[[str, int], None]
ErrorCB = Callable[[str], None]

END_NATURAL = "natural stop"
END_TURNS = "max turns"
END_STOPPED = "stopped"
END_ERROR = "error"
END_CAST = "a character is missing"


class SceneEngine:
    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        transcript: Transcript,
        scene: dict,
        *,
        on_event: EventCB | None = None,
        on_status: StatusCB | None = None,
        on_end: EndCB | None = None,
        on_error: ErrorCB | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.transcript = transcript
        self.scene = scene
        self.on_event = on_event or (lambda _e: None)
        self.on_status = on_status or (lambda _s: None)
        self.on_end = on_end or (lambda _r, _generation: None)
        self.on_error = on_error or (lambda _m: None)

        self._stop = threading.Event()
        self._natural_stop = False
        self._generation = 0
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._stepping = False
        self._turns_taken = 0
        self._consecutive_silences = 0
        self._running = False
        self._state = "idle"
        self._typing: str | None = None
        self._end_reason: str | None = None

    # ------------------------------------------------------------ introspection
    @property
    def running(self) -> bool:
        return self._running

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._running or self._stepping

    @property
    def end_reason(self) -> str | None:
        return self._end_reason

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def status(self) -> dict:
        with self._lock:
            return {
                "scene_id": self.scene.get("id"),
                "running": self._running,
                "turn": self._turns_taken,
                "next_speaker": self._next_speaker_id(),
                "typing": self._typing,
                "state": self._state,
            }

    # ---------------------------------------------------------------- control
    def start(self, *, resume: bool = False) -> bool:
        """Start the turn loop.

        An explicit Start begins a fresh run against the existing transcript.
        Narration that reopens a naturally stopped scene uses ``resume`` so the
        turn cap and speaker position are preserved.
        """
        with self._lock:
            if self._running:
                return True
            if self._stepping:
                return False
            if not resume:
                self._turns_taken = 0
                self._consecutive_silences = 0
            self._generation += 1
            generation = self._generation
            self._running = True
            self._end_reason = None
            self._natural_stop = False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, args=(generation,),
                name=f"engine-{self.scene.get('id')}", daemon=True,
            )
            self._thread.start()
        self._emit_status()
        return True

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._typing = None
            self._state = "idle"
        self._emit_status()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread:
            thread.join(timeout)

    def step(self) -> Event | None:
        """Run exactly one turn synchronously. Used by the /step endpoint."""
        with self._lock:
            if self._running or self._stepping:
                return None
            self._stepping = True
            self._end_reason = None
            self._natural_stop = False
            self._stop.clear()
        try:
            return self._take_turn()
        finally:
            with self._lock:
                self._stepping = False

    def note_narration(self) -> None:
        """A human (or the director) added narration: the scene has fresh interest."""
        with self._lock:
            self._consecutive_silences = 0

    def wait_idle(self, timeout: float | None = None) -> None:
        self.join(timeout)

    # ------------------------------------------------------------------ cast
    def _cast(self) -> tuple[Character, Character] | None:
        ids = list(self.scene.get("character_ids") or [])
        if len(ids) != 2:
            return None
        a = self.config.get_character(ids[0])
        b = self.config.get_character(ids[1])
        if a is None or b is None:
            return None
        return a, b

    def _next_speaker_id(self) -> str | None:
        cast = self._cast()
        if not cast:
            return None
        first = max(0, min(1, self.config.engine.first_speaker))
        index = (first + self._turns_taken) % 2
        return cast[index].id

    # ------------------------------------------------------------------ loop
    def _run(self, generation: int) -> None:
        reason = END_STOPPED
        try:
            while not self._stop.is_set():
                if self._cast() is None:
                    reason = END_CAST
                    break
                cfg = self.config.engine
                if self._turns_taken >= cfg.max_turns:
                    reason = END_TURNS
                    break
                with self._lock:
                    if self._consecutive_silences >= cfg.max_consecutive_silences:
                        reason = END_NATURAL
                        break
                event = self._take_turn()
                if self._stop.is_set():
                    reason = END_NATURAL if self._natural_stop else END_STOPPED
                    break
                if event is None:
                    reason = END_ERROR
                    break
        except LLMError as exc:
            log.error("Engine failed: %s", exc)
            self.on_error(str(exc))
            reason = END_ERROR
        except Exception as exc:  # noqa: BLE001 - never let the thread die silently
            log.exception("Engine crashed")
            self.on_error(str(exc))
            reason = END_ERROR
        finally:
            with self._lock:
                current = generation == self._generation
                if current:
                    self._running = False
                    self._typing = None
                    self._state = "idle"
                    self._end_reason = reason
            if current:
                self._emit_status()
                # The generation lets the HTTP layer ignore a completion that
                # races with a newly started run.
                self.on_end(reason, generation)
                log.info("Scene %s ended (%s)", self.scene.get("id"), reason)

    def _take_turn(self) -> Event | None:
        cast = self._cast()
        if cast is None:
            self.on_error(END_CAST)
            with self._lock:
                self._running = False
                self._state = "idle"
            return None

        first = max(0, min(1, self.config.engine.first_speaker))
        index = (first + self._turns_taken) % 2
        speaker = cast[index]
        listener = cast[1 - index]

        with self._lock:
            self._typing = speaker.id
            self._state = "generating"
        self._emit_status()

        memory_text, compacted_upto = self._memory_for(speaker)
        messages = P.build_messages(
            self.config,
            speaker,
            listener,
            str(self.scene.get("setting") or ""),
            [e.to_dict() for e in self.transcript.since(compacted_upto)],
            memory_text=memory_text,
        )

        cfg: EngineCfg = self.config.engine
        tools = [SILENT_TOOL] if cfg.use_silence_tool else None
        result = self.llm.chat(
            speaker.endpoint,
            messages,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            tools=tools,
        )

        silent = result.called_stay_silent or P.detect_silence(
            result.text, cfg.silence_sentinel
        )
        text = P.strip_silence_sentinel(result.text, cfg.silence_sentinel)

        # Reading-speed cooldown: hold the turn until the previous message has
        # been on screen long enough to be read. The delay belongs to that
        # previous message, not to the response that was just composed.
        previous_message = self.transcript.last_message()
        with self._lock:
            self._state = "cooldown"
        self._emit_status()
        self._wait_for_cooldown(previous_message)

        if self._stop.is_set():
            with self._lock:
                self._typing = None
                self._state = "idle"
            self._emit_status()
            return None

        if silent:
            event = self.transcript.add(
                kind="silence",
                character_id=speaker.id,
                turn=self.transcript.next_turn(),
            )
            with self._lock:
                self._consecutive_silences += 1
            log.info("%s chose silence (%d in a row)", speaker.display_name(),
                     self._consecutive_silences)
        else:
            if not text.strip():
                # Nothing usable came back; treat it as silence rather than
                # posting an empty bubble.
                event = self.transcript.add(
                    kind="silence",
                    character_id=speaker.id,
                    turn=self.transcript.next_turn(),
                )
                with self._lock:
                    self._consecutive_silences += 1
            else:
                event = self.transcript.add(
                    kind="message",
                    text=text.strip(),
                    character_id=speaker.id,
                    turn=self.transcript.next_turn(),
                )
                with self._lock:
                    self._consecutive_silences = 0
                log.info("%s posted %d chars", speaker.display_name(), len(text))

        with self._lock:
            self._turns_taken += 1
            self._typing = None
            self._state = "idle"
        self._emit_status()
        self.on_event(event.to_dict())

        if (
            not silent
            and text.strip()
            and self.config.engine.director_end_check
            and self._director_says_stop()
        ):
            log.info("Director called a natural stop")
            with self._lock:
                self._natural_stop = True
            self._stop.set()

        return event

    # ---------------------------------------------------------------- helpers
    def _wait_for_cooldown(self, previous: Event | None) -> None:
        if previous is None:
            return
        delay = self.config.cooldown.delay_for(previous.text)
        target = previous.ts + delay
        remaining = target - time.time()
        while remaining > 0 and not self._stop.is_set():
            # Wake at most every 0.25s so a stop request lands promptly.
            self._stop.wait(timeout=min(0.25, remaining))
            remaining = target - time.time()

    def _memory_for(self, speaker: Character) -> tuple[str, int]:
        """Return relevant chronicles and the current scene's compacted index.

        The current chronicle replaces the raw events it covers; feeding both
        to the model duplicates history and defeats compaction.
        """
        from .memory import Chronicle, format_for_character

        records: list[Chronicle] = []
        compacted_upto = 0
        if not self.config.memory_dir.exists():
            return "", 0
        for path in sorted(self.config.memory_dir.glob("*.json")):
            chronicle = Chronicle.load(path)
            if not chronicle.text:
                continue
            scene = self._scene_meta_for(chronicle.scene_id)
            cast_ids = set(scene.get("character_ids") or [])
            if speaker.id in cast_ids:
                records.append(chronicle)
                if chronicle.scene_id == self.scene.get("id"):
                    compacted_upto = chronicle.compacted_upto
        return format_for_character(records, speaker), compacted_upto

    def _scene_meta_for(self, scene_id: str) -> dict:
        if scene_id and scene_id == self.scene.get("id"):
            return self.scene
        from .transcript import load_scene_meta

        for meta in load_scene_meta(self.config.scenes_dir):
            if meta.get("id") == scene_id:
                return meta
        return {}

    def _director_says_stop(self) -> bool:
        """Cheap optional check: has this exchange run its course?"""
        recent = [e for e in self.transcript.events if e.kind in ("message", "narration")][-8:]
        if len(recent) < 4:
            return False
        body = "\n".join(f"{e.kind}: {e.text}" for e in recent)
        try:
            result = self.llm.chat(
                self.config.director,
                [
                    {
                        "role": "user",
                        "content": (
                            "Two characters are performing a scene. Here are the most "
                            "recent lines.\n\n"
                            + body
                            + "\n\nHas this exchange reached a natural stopping point -- "
                            "would a person here reasonably let it end? Answer with exactly "
                            "one word: YES or NO."
                        ),
                    }
                ],
                temperature=0.0,
                max_tokens=5,
                timeout=(10, 40),
            )
        except LLMError as exc:
            log.warning("Director end-check failed: %s", exc)
            return False
        return (result.text or "").strip().upper().startswith("Y")

    def _emit_status(self) -> None:
        self.on_status(self.status())
