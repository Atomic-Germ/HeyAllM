# HeyAllM

A small app that looks like a messaging client but is, in fact, a stage.

Two LLM characters converse with each other inside a scene. Each one believes
it is a person standing there, doing things with its body and its voice — not
answering chat messages. A human can interject, and those words arrive as
narration: the world moving around them rather than a line of dialogue. A third
model — the director — writes the characters on creation, narrates, and
compacts the history into a third-person record that can later be handed back
to a character as its own past.

## Quick start

Requires Python 3.11 or newer. From this directory:

```bash
python -m pip install .
heyallm                          # http://127.0.0.1:8777
```

The package includes the web UI and installs a `heyallm` command. You can
still run it as a module with `python -m heyallm`.

Options:

```bash
heyallm --port 9000 --data-dir ./data --verbose
heyallm --version
```

Open the URL in a browser. Open Settings, create two characters (the director
fills in the details from an optional seed), create a scene, and press Start.

## Talking to the models

Every character and the director has its own endpoint configuration: base URL,
model name, optional API key, timeout. Point them at the same OpenAI-compatible
server with different `model` values (llama.cpp routing mode) or at different
servers entirely. Ollama, vLLM, LM Studio, and llama.cpp all work unchanged.

## Creative director tools

The director can optionally use a native Python port of the old
`creative-thinking` meditation pipeline:

1. `creative_meditate` collides scene context with seeded random fragments.
2. `creative_insight` extracts themes from a meditation.
3. `creative_ponder` asks the director for a deeper reflection on an insight.

These tools are available during character creation, narration, and memory
compaction. They are inspirations, not facts; only the director's final output
enters the scene. Tool calls and results stay inside the director request and
are never streamed to characters.

The feature is enabled by default and can be disabled under **Settings →
Director** for endpoints without function-calling support. Each director
request has a bounded tool-call budget. Results persist under
`creative/state.json` for character creation and
`creative/scenes/<id>/state.json` for scene work, so state is isolated between
scenes and survives restarts. The Heritage Library, embeddings, and background
daemon are not included in this integration.

## How the scene works

- **Each character sees the other as the human user.** Its own past lines are
  `assistant` messages; the other character's lines are `user` messages. The
  system prompt tells it to keep its side straight.
- **Human and director input arrive as narration** — prefixed with
  `[the scene, unspoken]`, described as the world moving around the characters.
- **Actions, not chat.** Turns are written `*action in asterisks*` and
  `"speech in quotes"`. The prompt insists this is a scene, not an interface.
- **A reading-speed cooldown** between posted messages, computed from the
  previous message's length. Configurable.
- **Characters may stay silent** — via the `stay_silent` tool or the
  `[[SILENT]]` sentinel. Consecutive silences end the scene naturally.
- **Compaction is third person.** The director folds the event log into a
  chronicle that never uses first person for any character, and that chronicle
  is later injected into a character's system prompt as its pre-existing past.
  Auto-compaction runs during a scene once the configured event threshold is
  reached; the chronicle replaces the raw events it covers instead of
  duplicating them.

## Persistence

Everything lives under the data directory (default `~/.local/share/heyallm`):

- `config.json` — characters, endpoints, pacing, engine
- `scenes/<id>.jsonl` — the full event log
- `scenes/<id>.meta.json` — scene metadata
- `memory/<id>.json` — the compacted chronicle
- `creative/state.json` — director creative state for character creation
- `creative/scenes/<id>/state.json` — scene-scoped meditations, insights, and ponderings

If `config.json` cannot be read, the app refuses to start rather than
replacing it with defaults. Scene statuses are reconciled on startup, so a
crash cannot leave a scene displayed as permanently running.

## Development

```bash
python -m pip install -e ".[test]"
pytest
```

Model behavior uses a fake LLM client. HTTP and transport tests use loopback
servers only and never contact external services.

## Layout

```
heyallm/
  config.py        endpoints, characters, pacing, engine config
  llm.py           OpenAI-compatible client with retry + SSE
  director.py      third model: creates characters, narrates, runs tools
  creative.py      native meditation, insight, and pondering tools
  prompts.py       per-character system prompt and role mapping
  transcript.py    append-only JSONL event log
  memory.py        third-person compaction
  engine.py        turn loop, cooldown, silence, natural stop
  server.py        REST + SSE + static files
  static/          the messaging-app UI
```

## API

See `docs/API.md`.
