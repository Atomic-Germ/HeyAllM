# AGENTS.md — HeyAllM Development Guide

## Project map

```
heyallm/        Python package: engine, server, models, prompts
  static/       Vanilla HTML/CSS/JS messaging-app UI
tests/          pytest suite (fake LLM, no network)
docs/API.md     HTTP + SSE contract
```

## Conventions

- **One responsibility per module.** `config` owns data shapes, `llm` owns
  transport, `engine` owns turn-taking, `server` owns HTTP. Do not let the
  engine import the server.
- **Thread safety.** `Transcript` guards its list with a lock; the engine runs
  on its own thread. The HTTP layer must not mutate shared state without going
  through `App` methods.
- **Atomic file writes.** Use `atomic_write` for config, memory, and scene
  metadata. Transcripts are append-only JSONL so a crash loses at most one line.
- **No frameworks.** Backend is stdlib `http.server` + `requests`. Frontend is
  vanilla JS with no build step and no CDN dependencies.
- **Environment.** Python 3.11+. Dependencies: `requests`.

## Invariants (do not break)

1. **Nothing is revealed before the cooldown elapses.** The engine holds the
   turn until `last_post_at + cooldown.delay_for(text)`. The UI only renders
   message text from an `event` frame, never from a token stream.
2. **Sides stay straight.** A character's own lines are `assistant`; the other's
   are `user`. Narration is `user` with the narration prefix. Do not introduce
   a third role.
3. **Narration is never dialogue.** Human or director input is always wrapped
   in the narration prefix and described as the world moving.
4. **The chronicle is never first person.** `memory.is_third_person` guards it;
   compaction retries once if it fails.
5. **A character may stay silent.** `stay_silent` tool or `[[SILENT]]`
   sentinel, plus the consecutive-silence heuristic. Never force a post.

## Running

```bash
python -m heyallm --port 8777 --data-dir ./data
pytest tests/ -q
```

## Adding a route

Update `docs/API.md`, `server.py` (handler + `App` method), and the UI's API
calls. Keep the request/response shapes in sync with `docs/API.md`.

## Adding a demographic field

Add it to `DEMOGRAPHIC_FIELDS` in `config.py` and the UI's standard set. The
director will fill it in; the settings form will show it.
