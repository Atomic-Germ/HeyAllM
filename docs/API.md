# HeyAllM HTTP API

All bodies are JSON. Base path is the local server root (default `http://127.0.0.1:8777`).

## Endpoints

### `GET /api/state`
Everything the UI needs to boot in one call.

```json
{
  "config": { "version": 1, "director": {...}, "cooldown": {...}, "engine": {...}, "narration": {...}, "memory": {...} },
  "characters": [ Character ],
  "scenes": [ SceneSummary ],
  "active_scene": "scene-abc" | null,
  "running": false
}
```

### Characters

`Character`:

```json
{
  "id": "char-1a2b3c4d5e",
  "screenname": "mara",
  "real_name": "Mara Ellison",
  "demographics": { "age": "29", "gender": "she/her", "occupation": "night-shift radio operator", "location": "a coastal town", "appearance": "...", "mannerisms": "...", "speech_style": "...", "history": "...", "wants": "..." },
  "persona": "director-written paragraph(s) used as the body of the system prompt",
  "prompt_extra": "human seed, may be empty",
  "endpoint": { "base_url": "http://127.0.0.1:8080/v1", "model": "qwen2.5-7b-instruct", "api_key": null, "timeout": 180 },
  "created_at": "2026-11-23T11:00:00"
}
```

- `GET /api/characters` → `{ "characters": [Character] }`
- `GET /api/characters/<id>` → `{ "character": Character }`
- `POST /api/characters` — body `{ "prompt_extra"?, "endpoint"?, "generate"? }`.
  Creates a shell character; when `generate` is true (default) the **director**
  model fills screenname / real name / demographics / persona before responding.
  Response `{ "character": Character }`. Slow (one director call).
- `PUT /api/characters/<id>` — partial update of any Character field
  (`screenname`, `real_name`, `demographics`, `persona`, `prompt_extra`, `endpoint`).
  Response `{ "character": Character }`.
- `POST /api/characters/<id>/regenerate` — director re-fills the details,
  preserving `id` and `endpoint`. Response `{ "character": Character }`.
- `DELETE /api/characters/<id>` → `{ "ok": true }`

### Scenes

`SceneSummary`:

```json
{
  "id": "scene-9f8e7d6c5b",
  "title": "The lighthouse",
  "setting": "A lighthouse stairwell, 3am, storm outside",
  "character_ids": ["char-...", "char-..."],
  "created_at": "2026-11-23T11:05:00",
  "event_count": 14,
  "status": "stopped"
}
```

`Event` (transcript entries, in order):

```json
{
  "id": "evt-...",
  "ts": 1764000000.12,
  "kind": "message" | "narration" | "silence" | "scene",
  "character_id": "char-..." | null,
  "text": "...",
  "turn": 3
}
```

`kind` meanings:
- `message` — a character posted; `character_id` set.
- `narration` — human (or director) spoke; `character_id` null; shown framed as narrative.
- `silence` — a character chose not to post; `character_id` set.
- `scene` — scene opened; `character_id` null.

- `GET /api/scenes` → `{ "scenes": [SceneSummary] }`
- `POST /api/scenes` — body `{ "character_ids": ["char-a","char-b"], "title"?, "setting"? }`
  → `{ "scene": SceneSummary }`. Creates the scene and appends the `scene` event.
- `GET /api/scenes/<id>` → `{ "scene": SceneSummary, "events": [Event], "memory": "third-person chronicle text or empty" }`
- `POST /api/scenes/<id>/events` — body `{ "text": "..." }` → appends a `narration`
  event (works while running or stopped). Response `{ "event": Event }`.
- `POST /api/scenes/<id>/narrate` — body `{ "hint": "..." }` (optional) → the
  director writes one narrative beat and appends it as a `narration` event.
  Response `{ "event": Event }`. Works while running or stopped.
- `POST /api/scenes/<id>/start` → `{ "running": true }`. Starts at most one
  scene at a time. If another scene has an in-flight request that has not
  stopped yet, the response is `409` with an error message.
- `POST /api/scenes/<id>/stop` → `{ "running": true | false }`, reflecting the
  actual state after the stop request. An in-flight model request or cooldown
  may still be finishing, in which case `running` remains `true` until the
  final status event arrives.
- `POST /api/scenes/<id>/step` — run exactly one turn (for debugging) →
  `{ "event": Event | null }`. A step is serialized with Start and Delete.
- `POST /api/scenes/<id>/compact` — director compacts uncompacted events into
  the third-person chronicle → `{ "memory": "..." }`
- `DELETE /api/scenes/<id>` → `{ "ok": true }`. Removes the transcript,
  metadata, and compacted chronicle. Returns `409` if the scene cannot finish
  stopping yet.

Unknown scene resources return `404`; malformed JSON request bodies return
`400`. Model and director failures return `502`.

### `GET /api/scenes/<id>/stream` — Server-Sent Events

Each message is `data: {json}\n\n`. Kinds:

```json
{ "type": "event",  "scene_id": "scene-...", "event": Event }
{ "type": "status", "scene_id": "scene-...", "running": true, "turn": 4, "next_speaker": "char-..." | null, "typing": "char-..." | null, "state": "idle" | "generating" | "cooldown" }
{ "type": "scene_end", "scene_id": "scene-...", "reason": "natural stop" | "max turns" | "stopped" }
{ "type": "error",  "scene_id": "scene-...", "message": "..." }
{ "type": "resync", "scene_id": "scene-..." }
```

`typing` is non-null from the moment a character starts composing until their
message (or silence) is posted — this covers both generation time and the
reading-speed cooldown. **The UI must not reveal message text before the
`event` arrives**; the cooldown is the point. A `resync` frame means the
subscriber fell behind; the client reopens the stream and rebuilds from scene
history. It also refetches history after a dropped connection, because SSE does
not replay missed frames.

## UI requirements

The screen should read as a **messaging app**: scene list sidebar, header with
both characters' avatars/names, message stream, composer at the bottom.

- Character A renders on the **left**, character B on the **right**.
- `narration` events render as **centered, dimmed, full-width** lines (narrative,
  not a bubble) — prefix is `config.narration.prefix`.
- `silence` events render as a centered dimmed line (e.g. *a beat of silence*).
- Inside a message, `*text*` is an **action**: italic. `"text"` is **speech**.
  Actions may also be rendered outside the bubble as centered italic lines when
  the message is action-only.
- Typing indicator (animated dots) in the correct-side bubble while `typing` is
  set.
- Composer: textarea + send button → `POST /api/scenes/<id>/events`.
- Controls: Start / Stop, Step, Compact memory, New scene, Settings.
- Settings modal edits both characters (endpoint, model, api key, screenname,
  real name, demographics, persona), the director endpoint, and cooldown /
  engine numbers, then `PUT`s them back.
- No frameworks — vanilla HTML/CSS/JS, served from `/static/`.
