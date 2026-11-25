/* ============================================================
   HeyAllM frontend — vanilla JS, no frameworks, no build step.
   Talks to the REST + SSE API documented in docs/API.md.
   ============================================================ */
"use strict";

/* ---------------------------------------------------------- state */
const state = {
  config: null,            // config object from GET /api/state
  characters: [],
  scenes: [],
  activeSceneId: null,
  running: false,
  events: [],              // transcript of the active scene
  seenIds: new Set(),      // event ids already rendered (SSE vs POST dedupe)
  status: { turn: null, typing: null, state: "idle" },
  stickBottom: true,       // autoscroll unless the user scrolled up
  source: null,            // EventSource for the active scene
  catchupSource: null,     // stream that opened after its history snapshot
  reconnectTimer: null,
  buffer: null,            // frames held while the transcript loads (see selectScene)
  sceneLoad: 0,            // ignores stale history responses after rapid switching
  currentTab: null,
};

/* ---------------------------------------------------------- dom helpers */
const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function field(label, inner) {
  return `<label class="field"><span class="field-label">${label}</span>${inner}</label>`;
}
function textInput(f, v, attrs = "") {
  return `<input type="text" data-f="${f}" value="${esc(v ?? "")}" ${attrs}>`;
}
function numInput(f, v, step = "1") {
  return `<input type="number" data-f="${f}" value="${esc(v ?? "")}" step="${step}">`;
}
function checkInput(f, checked, label) {
  return `<label class="check"><input type="checkbox" data-f="${f}" ${checked ? "checked" : ""}><span>${label}</span></label>`;
}

const fnum = (v, d) => { const n = parseFloat(v); return Number.isFinite(n) ? n : d; };
const inum = (v, d) => { const n = parseInt(v, 10); return Number.isFinite(n) ? n : d; };
const DEMO_STD = ["age", "gender", "occupation", "location", "appearance",
  "mannerisms", "speech_style", "history", "wants"];
const AVATAR_COLORS = ["#e06c75", "#98c379", "#e5c07b", "#c678dd",
  "#56b6c2", "#61afef", "#d19a66", "#7ec699"];
const charLetter = (i) => String.fromCharCode(65 + i);

/* ---------------------------------------------------------- api + toast */
async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, {
      method: opts.method || "GET",
      headers: opts.body ? { "Content-Type": "application/json" } : undefined,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
  } catch (_) {
    throw new Error("Cannot reach the HeyAllM server");
  }
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON body */ }
  if (!res.ok) {
    let msg = data && (data.detail || data.error || data.message);
    if (Array.isArray(msg)) msg = msg.map((m) => m.msg || JSON.stringify(m)).join("; ");
    const err = new Error(typeof msg === "string" && msg ? msg : `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data || {};
}

function toast(msg, kind = "info") {
  const box = $("#toasts");
  const t = el("div", `toast ${kind}`, String(msg));
  box.appendChild(t);
  setTimeout(() => {
    t.style.opacity = "0";
    t.style.transition = "opacity .3s";
    setTimeout(() => t.remove(), 320);
  }, 4200);
  while (box.children.length > 5) box.firstChild.remove();
}

/* ---------------------------------------------------------- small formatters */
function toMs(ts) {
  if (ts == null) return 0;
  if (typeof ts === "number") return ts > 1e12 ? ts : ts * 1000;
  const parsed = Date.parse(ts);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** "just now", "3m", "2h", "1d", then a date. */
function relTime(ts) {
  const ms = toMs(ts);
  if (!ms) return "";
  const diff = Math.max(0, (Date.now() - ms) / 1000);
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}d`;
  return new Date(ms).toLocaleDateString();
}

function fullName(ch) {
  if (!ch) return "";
  return ch.real_name || ch.screenname || ch.id || "";
}
function displayName(ch) {
  if (!ch) return "";
  return ch.screenname || ch.real_name || ch.id || "";
}
function initials(ch) {
  const src = (ch && (ch.screenname || ch.real_name)) || "?";
  const parts = src.trim().split(/\s+/);
  const init = parts.length > 1
    ? parts[0][0] + parts[1][0]
    : src.slice(0, 2);
  return init || "?";
}
function colorFor(id) {
  let h = 0;
  for (const c of String(id || "")) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}
function avatarEl(ch, big = false) {
  const a = el("div", "avatar" + (big ? " lg" : ""), initials(ch));
  a.style.background = colorFor(ch ? ch.id : "");
  a.setAttribute("aria-hidden", "true");
  return a;
}

/* ---------------------------------------------------------- lookups */
const charById = (id) => state.characters.find((c) => c.id === id) || null;
const currentScene = () => state.scenes.find((s) => s.id === state.activeSceneId) || null;
function activeCharacterIds() {
  const sc = currentScene();
  return (sc && sc.character_ids) || [];
}
/** Character index -> side: 0 = left, 1 = right. */
function sideOf(charId) {
  const ids = activeCharacterIds();
  return ids.indexOf(charId) === 1 ? "right" : "left";
}

/* ---------------------------------------------------------- message text
   *action* -> italic, "speech" -> quoted styling.
   Action-only messages become centered italic lines (no bubble).          */
function buildBody(text) {
  const frag = document.createDocumentFragment();
  let hasAction = false, hasSpeech = false;
  const parts = String(text || "").split(/(\*[^*\n]+\*)/g);
  for (const part of parts) {
    if (!part) continue;
    if (/^\*[^*\n]+\*$/.test(part)) {
      hasAction = true;
      frag.appendChild(el("span", "action", part.slice(1, -1)));
      continue;
    }
    for (const seg of part.split(/("[^"]*")/g)) {
      if (!seg) continue;
      if (/^"[^"]*"$/.test(seg)) {
        hasSpeech = true;
        frag.appendChild(el("span", "speech", seg));
      } else {
        if (seg.trim()) hasSpeech = true;
        frag.appendChild(document.createTextNode(seg));
      }
    }
  }
  return { frag, hasAction, hasSpeech };
}

/* ---------------------------------------------------------- event rendering */
function renderMessage(ev) {
  const ch = charById(ev.character_id);
  const body = buildBody(ev.text);

  // Action-only: centered italic line instead of a bubble.
  if (body.hasAction && !body.hasSpeech) {
    const line = el("div", "action-line");
    if (ch) line.appendChild(el("span", "who", displayName(ch)));
    line.appendChild(body.frag);
    if (ev.ts) line.title = new Date(toMs(ev.ts)).toLocaleString();
    return line;
  }

  const row = el("div", `msg-row ${sideOf(ev.character_id)}`);
  if (ch) row.appendChild(avatarEl(ch));

  const col = el("div", "msg-col");
  const meta = el("div", "msg-meta");
  if (ch) meta.appendChild(el("span", "msg-who", displayName(ch)));
  meta.appendChild(el("span", "msg-time", relTime(ev.ts)));

  const bubble = el("div", "bubble");
  bubble.appendChild(body.frag);
  col.append(meta, bubble);
  row.appendChild(col);
  if (ev.ts) row.title = new Date(toMs(ev.ts)).toLocaleString();
  return row;
}

function renderNarration(ev) {
  const wrap = el("div", "narration");
  const prefix = state.config && state.config.narration && state.config.narration.prefix;
  if (prefix) wrap.appendChild(el("span", "narration-prefix", prefix));
  wrap.appendChild(el("span", "narration-text", ev.text || ""));
  return wrap;
}

function renderSilence(ev) {
  let t = String(ev.text || "").trim() || "a beat of silence";
  if (!t.startsWith("*")) t = `* ${t} *`;
  return el("div", "silence", t);
}

function renderSceneLine(ev) {
  const sc = currentScene();
  const wrap = el("div", "scene-line");
  const title = (sc && sc.title) || "";
  const setting = (sc && sc.setting) || "";
  if (title) wrap.appendChild(el("span", "scene-name", title));
  if (setting) wrap.appendChild(el("span", "scene-desc", setting));
  if (!title && !setting) wrap.textContent = ev.text || "";
  return wrap;
}

function eventNode(ev) {
  if (ev.kind === "message") return renderMessage(ev);
  if (ev.kind === "narration") return renderNarration(ev);
  if (ev.kind === "silence") return renderSilence(ev);
  if (ev.kind === "scene") return renderSceneLine(ev);
  return null;
}

/** Append one transcript event (deduped: SSE and POST both deliver it). */
function appendEvent(ev, smooth = true) {
  if (!ev || !ev.id || state.seenIds.has(ev.id)) return false;
  state.seenIds.add(ev.id);
  state.events.push(ev);

  const node = eventNode(ev);
  if (node) {
    node.dataset.eventId = ev.id;
    $("#stream").appendChild(node);
    // keep the typing indicator pinned to the bottom while it's still active
    const tr = document.getElementById("typing-row");
    if (tr) $("#stream").appendChild(tr);
  }
  // The posted event ends that character's typing indicator.
  if (state.status.typing && ev.character_id === state.status.typing) {
    state.status.typing = null;
    renderTyping();
  }
  scrollToBottom(smooth);
  return true;
}

/* ---------------------------------------------------------- typing indicator */
function renderTyping() {
  const old = $("#typing-row");
  if (old) old.remove();
  const id = state.status.typing;
  if (!id) return;

  const ch = charById(id);
  const row = el("div", `msg-row ${sideOf(id)} typing-row`);
  row.id = "typing-row";
  row.setAttribute("aria-label", `${displayName(ch) || "Someone"} is typing`);
  if (ch) row.appendChild(avatarEl(ch));

  const col = el("div", "msg-col");
  const meta = el("div", "msg-meta");
  if (ch) meta.appendChild(el("span", "msg-who", displayName(ch)));
  const bubble = el("div", "bubble dots");
  for (let i = 0; i < 3; i++) bubble.appendChild(el("span", "dot"));
  col.append(meta, bubble);
  row.appendChild(col);

  $("#stream").appendChild(row);
  scrollToBottom();
}

/* ---------------------------------------------------------- autoscroll */
function scrollToBottom(smooth = true) {
  if (!state.stickBottom) return;
  const stream = $("#stream");
  stream.scrollTo({ top: stream.scrollHeight, behavior: smooth ? "smooth" : "auto" });
}

function watchScroll() {
  const stream = $("#stream");
  stream.addEventListener("scroll", () => {
    state.stickBottom =
      stream.scrollHeight - stream.scrollTop - stream.clientHeight < 70;
  });
}

/* ---------------------------------------------------------- sidebar */
const ICON_TRASH = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`;

function renderSidebar() {
  const list = $("#scene-list");
  list.innerHTML = "";
  for (const sc of state.scenes) {
    const item = el("div", "scene-item" + (sc.id === state.activeSceneId ? " active" : ""));
    item.setAttribute("role", "button");
    item.tabIndex = 0;
    item.setAttribute("aria-current", sc.id === state.activeSceneId ? "true" : "false");

    item.appendChild(el("span", "item-title", sc.title || "Untitled scene"));
    const sub = el("span", "item-sub");
    const bits = [];
    if (sc.status === "running") bits.push("● running");
    bits.push(`${sc.event_count ?? state.events.length} events`);
    if (sc.created_at) bits.push(relTime(sc.created_at));
    sub.textContent = bits.join(" · ");
    item.appendChild(sub);

    const del = el("button", "icon-btn scene-del");
    del.type = "button";
    del.innerHTML = ICON_TRASH;
    del.title = "Delete scene";
    del.setAttribute("aria-label", `Delete scene ${sc.title || "Untitled"}`);
    del.addEventListener("click", (e) => { e.stopPropagation(); deleteScene(sc.id); });
    item.appendChild(del);

    const open = () => selectScene(sc.id);
    item.addEventListener("click", open);
    item.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
    list.appendChild(item);
  }
}

/* ---------------------------------------------------------- header */
function renderHeaderChars() {
  const box = $("#header-chars");
  box.innerHTML = "";
  const ids = activeCharacterIds();
  const sc = currentScene();

  if (!ids.length) {
    box.appendChild(el("span", "muted", "No scene selected"));
    return;
  }
  ids.slice(0, 2).forEach((id, i) => {
    if (i > 0) box.appendChild(el("span", "vs", "vs"));
    const ch = charById(id);
    const wrap = el("div", "hchar");
    wrap.appendChild(avatarEl(ch, true));
    const names = el("div", "names");
    names.appendChild(el("span", "screenname", displayName(ch) || "unknown"));
    const real = fullName(ch);
    names.appendChild(el("span", "realname",
      (real && real !== displayName(ch) ? real : (ch && ch.demographics && ch.demographics.occupation) || "—")));
    wrap.appendChild(names);
    box.appendChild(wrap);
  });

  if (sc && sc.title) box.title = `${sc.title} — ${sc.setting || ""}`;
}

function updateHeaderStatus() {
  const btn = $("#btn-start-stop");
  btn.classList.toggle("running", state.running);
  $("#start-stop-label").textContent = state.running ? "Stop" : "Start";
  btn.disabled = !state.activeSceneId;
  $("#btn-step").disabled = !state.activeSceneId;
  $("#btn-compact").disabled = !state.activeSceneId;
  $("#btn-delete-scene").disabled = !state.activeSceneId;

  const s = state.status;
  const parts = [state.running ? "running" : "stopped"];
  if (state.running && s.state && s.state !== "idle") parts.push(s.state);
  if (s.turn != null) parts.push(`turn ${s.turn}`);
  $("#status-pill").textContent = parts.join(" · ");
}

/* ---------------------------------------------------------- empty states */
function renderEmpty() {
  const main = $("#main");
  const box = $("#empty-state");
  let spec = null;

  if (state.characters.length < 2) {
    spec = {
      title: "Welcome to HeyAllM",
      body: "HeyAllM runs a conversation between two AI characters in a scene — framed as a messaging app, with you free to interject as narrative. Create at least two characters to get started.",
      btn: "Open settings",
      act: openSettings,
    };
  } else if (!state.activeSceneId) {
    spec = {
      title: "No scene yet",
      body: "Start a new scene and watch two characters talk — or step in yourself at any time.",
      btn: "New scene",
      act: openNewScene,
    };
  }

  if (!spec) {
    main.classList.remove("empty");
    box.classList.add("hidden");
    return;
  }
  main.classList.add("empty");
  box.classList.remove("hidden");
  box.innerHTML = "";
  box.appendChild(el("h2", null, spec.title));
  box.appendChild(el("p", null, spec.body));
  const b = el("button", "btn primary", spec.btn);
  b.type = "button";
  b.addEventListener("click", spec.act);
  box.appendChild(b);
}

function updateComposer() {
  const ok = !!state.activeSceneId && state.characters.length >= 2;
  $("#composer-text").disabled = !ok;
  $("#btn-send").disabled = !ok;
  $("#btn-new-scene").disabled = state.characters.length < 2;
}

function renderAll() {
  renderSidebar();
  renderHeaderChars();
  updateHeaderStatus();
  updateComposer();
  renderEmpty();
}

/* ---------------------------------------------------------- scene loading */
async function selectScene(id, rebuild = false) {
  if (!id) return;
  const load = ++state.sceneLoad;
  if (state.activeSceneId !== id || rebuild) {
    closeStream();                     // close the old source FIRST
    state.activeSceneId = id;
    state.events = [];
    state.seenIds.clear();
    state.status = { turn: null, typing: null, state: "idle" };
    state.stickBottom = true;
    $("#stream").innerHTML = "";
  }
  renderAll();

  // Open the stream BEFORE fetching history so no event can slip through the
  // gap; frames are buffered until the transcript is rendered (dedupe then
  // drops anything the history fetch already contains). The buffer is armed
  // after openStream because that resets it while closing the old source.
  const pending = [];
  openStream(id);
  state.buffer = pending;

  try {
    const data = await api(`/api/scenes/${encodeURIComponent(id)}`);
    if (state.activeSceneId === id && load === state.sceneLoad) {
      for (const ev of data.events || []) appendEvent(ev, false);
    }
  } catch (err) {
    if (state.activeSceneId === id && load === state.sceneLoad) {
      toast(`Could not load scene: ${err.message}`, "error");
    }
  }
  if (state.activeSceneId !== id || load !== state.sceneLoad) return;

  state.buffer = null;
  for (const frame of pending) handleFrame(frame);

  renderSidebar();
  renderHeaderChars();
  updateHeaderStatus();
  // If the SSE connection was not open while history was fetched, opening it
  // later leaves a gap. Re-read the transcript once on that first connection.
  if (state.source && state.source.readyState !== EventSource.OPEN) {
    state.catchupSource = state.source;
  }
  const stream = $("#stream");
  stream.scrollTop = stream.scrollHeight;
  state.stickBottom = true;
  renderTyping();
}

async function refreshScenes() {
  try {
    const data = await api("/api/scenes");
    state.scenes = data.scenes || [];
    renderSidebar();
  } catch (_) { /* non-fatal */ }
}

/* ---------------------------------------------------------- SSE */
function openStream(sceneId) {
  closeStream();
  if (!sceneId || typeof EventSource === "undefined") return;

  const src = new EventSource(`/api/scenes/${encodeURIComponent(sceneId)}/stream`);
  state.source = src;
  let connectedOnce = false;

  src.onopen = () => {
    if (state.source !== src) return;
    if (connectedOnce || state.catchupSource === src) {
      // EventSource does not replay frames missed while disconnected. Select
      // the scene again to merge a fresh history snapshot with the live stream.
      state.catchupSource = null;
      selectScene(sceneId, true);
      return;
    }
    connectedOnce = true;
  };

  src.onmessage = (msg) => {
    let data;
    try { data = JSON.parse(msg.data); } catch (_) { return; }
    // hold frames until the transcript has been rendered (see selectScene)
    if (state.buffer) { state.buffer.push(data); return; }
    handleFrame(data);
  };

  src.onerror = () => {
    // EventSource retries on its own while CONNECTING; if it gave up, reopen
    // after a short delay (as long as this source is still the active one).
    if (src.readyState === EventSource.CLOSED && state.source === src) {
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = setTimeout(() => {
        if (state.activeSceneId === sceneId) selectScene(sceneId, true);
      }, 3000);
    }
  };
}

/** Apply one parsed SSE frame (shared by the live stream and the flush). */
function handleFrame(data) {
  if (!data || (data.scene_id && data.scene_id !== state.activeSceneId)) return;
  switch (data.type) {
    case "event":
      if (data.event) appendEvent(data.event);
      break;
    case "status":
      applyStatus(data);
      break;
    case "scene_end":
      applySceneEnd(data);
      break;
    case "resync":
      if (state.activeSceneId) selectScene(state.activeSceneId, true);
      break;
    case "error":
      toast(data.message || "The scene reported an error", "error");
      break;
    default:
      break;
  }
}

function closeStream() {
  clearTimeout(state.reconnectTimer);
  state.reconnectTimer = null;
  state.buffer = null;
  state.catchupSource = null;
  if (state.source) {
    state.source.close();
    state.source = null;
  }
}

function applyStatus(s) {
  state.running = !!s.running;
  const typingChanged = state.status.typing !== (s.typing || null);
  state.status.typing = s.typing || null;
  state.status.turn = s.turn ?? state.status.turn;
  state.status.state = s.state || "idle";
  updateHeaderStatus();
  if (typingChanged) renderTyping();
}

function applySceneEnd(d) {
  state.running = false;
  if (state.status.typing) {
    state.status.typing = null;
    renderTyping();
  }
  updateHeaderStatus();
  toast(`Scene ended: ${d.reason || "finished"}`);
  refreshScenes();
}

/* ---------------------------------------------------------- composer */
function autoGrow() {
  const ta = $("#composer-text");
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
}

function wireComposer() {
  const form = $("#composer");
  const ta = $("#composer-text");

  ta.addEventListener("input", autoGrow);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = ta.value.trim();
    if (!text || !state.activeSceneId) return;
    ta.value = "";
    autoGrow();
    try {
      const data = await api(`/api/scenes/${encodeURIComponent(state.activeSceneId)}/events`, {
        method: "POST",
        body: { text },
      });
      appendEvent(data.event);          // deduped against the SSE copy
    } catch (err) {
      ta.value = text;                  // give the text back on failure
      autoGrow();
      toast(`Could not send: ${err.message}`, "error");
    }
  });
}

/* ---------------------------------------------------------- header controls */
function wireControls() {
  $("#btn-start-stop").addEventListener("click", async () => {
    if (!state.activeSceneId) return;
    const action = state.running ? "stop" : "start";
    // optimistic: the SSE status will confirm
    state.running = action === "start";
    updateHeaderStatus();
    try {
      const data = await api(`/api/scenes/${encodeURIComponent(state.activeSceneId)}/${action}`,
        { method: "POST" });
      state.running = !!data.running;
    } catch (err) {
      state.running = action === "stop";  // revert
      toast(`${action === "start" ? "Start" : "Stop"} failed: ${err.message}`, "error");
    }
    updateHeaderStatus();
    refreshScenes();
  });

  $("#btn-step").addEventListener("click", async () => {
    if (!state.activeSceneId) return;
    try {
      const data = await api(`/api/scenes/${encodeURIComponent(state.activeSceneId)}/step`,
        { method: "POST" });
      if (data.event) appendEvent(data.event);   // deduped with SSE
    } catch (err) {
      toast(`Step failed: ${err.message}`, "error");
    }
  });

  $("#btn-compact").addEventListener("click", async () => {
    if (!state.activeSceneId) return;
    try {
      await api(`/api/scenes/${encodeURIComponent(state.activeSceneId)}/compact`, { method: "POST" });
      toast("Memory compacted", "ok");
    } catch (err) {
      toast(`Compact failed: ${err.message}`, "error");
    }
  });

  $("#btn-delete-scene").addEventListener("click", () => {
    if (state.activeSceneId) deleteScene(state.activeSceneId);
  });

  $("#btn-new-scene").addEventListener("click", openNewScene);
  $("#btn-settings").addEventListener("click", () => openSettings());
}

async function deleteScene(id) {
  const sc = state.scenes.find((s) => s.id === id);
  const label = (sc && sc.title) || "Untitled scene";
  if (!window.confirm(`Delete "${label}"? The transcript cannot be recovered.`)) return;
  try {
    await api(`/api/scenes/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("Scene deleted");
    if (id === state.activeSceneId) {
      closeStream();
      state.activeSceneId = null;
      state.events = [];
      state.seenIds.clear();
      state.status = { turn: null, typing: null, state: "idle" };
      $("#stream").innerHTML = "";
    }
    await loadState();
    // only pick a new scene if the deleted one was the active one
    if (!state.activeSceneId) {
      const next = state.scenes[0];
      if (next) await selectScene(next.id);
      else renderAll();
    }
  } catch (err) {
    toast(`Delete failed: ${err.message}`, "error");
  }
}

/* ---------------------------------------------------------- modals */
function showModal(sel) {
  const m = $(sel);
  m.classList.remove("hidden");
  // Prefer a field in the visible panel; fall back to the first control.
  const target =
    m.querySelector(".tab-panel:not(.hidden) input, .tab-panel:not(.hidden) select, .tab-panel:not(.hidden) textarea") ||
    m.querySelector("input:not([type=hidden]), select, textarea") ||
    m.querySelector("button");
  if (target) target.focus();
}
function hideModal(sel) {
  $(sel).classList.add("hidden");
}

function wireModals() {
  // close buttons + backdrop click + Escape
  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const modal = btn.closest(".modal");
      if (modal) hideModal("#" + modal.id);
    });
  });
  document.querySelectorAll(".modal").forEach((modal) => {
    modal.addEventListener("mousedown", (e) => {
      if (e.target === modal) hideModal("#" + modal.id);
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    document.querySelectorAll(".modal:not(.hidden)").forEach((m) => hideModal("#" + m.id));
  });
}

/* ---------------------------------------------------------- new scene modal */
function fillCharSelect(select, selectedId) {
  select.innerHTML = "";
  state.characters.forEach((ch, i) => {
    const opt = document.createElement("option");
    opt.value = ch.id;
    opt.textContent = `${charLetter(i)} · ${displayName(ch)}${ch.real_name && ch.real_name !== displayName(ch) ? ` — ${ch.real_name}` : ""}`;
    select.appendChild(opt);
  });
  if (selectedId) select.value = selectedId;
}

function openNewScene() {
  if (state.characters.length < 2) {
    toast("Create two characters first", "error");
    openSettings();
    return;
  }
  fillCharSelect($("#ns-char-a"), state.characters[0].id);
  fillCharSelect($("#ns-char-b"), state.characters[1].id);
  $("#ns-title").value = "";
  $("#ns-setting").value = "";
  $("#ns-error").classList.add("hidden");
  $("#ns-submit").disabled = false;
  $("#ns-submit").textContent = "Create scene";
  showModal("#modal-new-scene");
}

function wireNewScene() {
  $("#form-new-scene").addEventListener("submit", async (e) => {
    e.preventDefault();
    const a = $("#ns-char-a").value;
    const b = $("#ns-char-b").value;
    const errBox = $("#ns-error");
    if (a === b) {
      errBox.textContent = "Character A and Character B must be two different characters.";
      errBox.classList.remove("hidden");
      return;
    }
    errBox.classList.add("hidden");

    const btn = $("#ns-submit");
    btn.disabled = true;
    btn.textContent = "Creating…";
    const body = { character_ids: [a, b] };
    const title = $("#ns-title").value.trim();
    const setting = $("#ns-setting").value.trim();
    if (title) body.title = title;
    if (setting) body.setting = setting;

    try {
      const data = await api("/api/scenes", { method: "POST", body });
      hideModal("#modal-new-scene");
      await refreshScenes();
      if (data.scene) await selectScene(data.scene.id);
      toast("Scene created", "ok");
    } catch (err) {
      toast(`Could not create scene: ${err.message}`, "error");
      btn.disabled = false;
      btn.textContent = "Create scene";
    }
  });
}

/* ---------------------------------------------------------- settings modal */
let snap = null;   // snapshot taken when the modal opens, for change detection

function takeSnapshot() {
  snap = {
    chars: state.characters.map((c) => ({
      screenname: c.screenname || "",
      real_name: c.real_name || "",
      persona: c.persona || "",
      prompt_extra: c.prompt_extra || "",
      demographics: { ...(c.demographics || {}) },
      endpoint: { ...(c.endpoint || {}) },
    })),
    config: JSON.parse(JSON.stringify(state.config || {})),
  };
}

function demoKeys(ch) {
  const keys = [...DEMO_STD];
  for (const k of Object.keys((ch && ch.demographics) || {})) {
    if (!keys.includes(k)) keys.push(k);
  }
  return keys;
}

function charPanelHtml(i) {
  const ch = state.characters[i];
  if (!ch) return `<p class="muted">No character here yet — use <strong>＋ New character</strong> above.</p>`;
  const ep = ch.endpoint || {};
  const demoFields = demoKeys(ch).map((k) =>
    field(k.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
      `<input type="text" data-demo="${esc(k)}" value="${esc((ch.demographics || {})[k] || "")}">`)
  ).join("");

  return `
    <p class="muted">id: ${esc(ch.id)}${ch.created_at ? ` · created ${esc(ch.created_at)}` : ""}</p>
    <div class="panel-grid">
      ${field("Base URL", textInput("base_url", ep.base_url || ""))}
      ${field("Model", textInput("model", ep.model || ""))}
      ${field("API key", `<input type="password" data-f="api_key" value="${esc(ep.api_key || "")}" autocomplete="new-password" placeholder="optional">`)}
      ${field("Timeout (s)", numInput("timeout", ep.timeout ?? 180, "0.1"))}
      ${field("Screenname", textInput("screenname", ch.screenname || ""))}
      ${field("Real name", textInput("real_name", ch.real_name || ""))}
    </div>
    ${field("Persona", `<textarea data-f="persona" rows="5" placeholder="Director-written paragraph(s) used as the system prompt body">${esc(ch.persona || "")}</textarea>`)}
    ${field("Prompt extra", `<textarea data-f="prompt_extra" rows="3" placeholder="Human seed / directive (optional)">${esc(ch.prompt_extra || "")}</textarea>`)}
    <h3>Demographics</h3>
    <div class="panel-grid">${demoFields}</div>
    <div class="panel-actions">
      <button type="button" class="btn" data-act="regenerate" data-char="${esc(ch.id)}">Regenerate details</button>
      <button type="button" class="btn danger" data-act="delete-char" data-char="${esc(ch.id)}">Delete character</button>
    </div>`;
}

function directorPanelHtml() {
  const d = (state.config && state.config.director) || {};
  return `
    <p class="muted">The director fills character details, checks for natural scene endings, and compacts memory.</p>
    <div class="panel-grid">
      ${field("Base URL", textInput("base_url", d.base_url || ""))}
      ${field("Model", textInput("model", d.model || ""))}
      ${field("API key", `<input type="password" data-f="api_key" value="${esc(d.api_key || "")}" autocomplete="new-password" placeholder="optional">`)}
      ${field("Timeout (s)", numInput("timeout", d.timeout ?? 180, "0.1"))}
    </div>`;
}

function pacingPanelHtml() {
  const c = state.config || {};
  const cd = c.cooldown || {}, en = c.engine || {}, na = c.narration || {}, me = c.memory || {};
  const opts = state.characters.map((ch, i) =>
    `<option value="${i}" ${inum(en.first_speaker, 0) === i ? "selected" : ""}>Character ${charLetter(i)} — ${esc(displayName(ch))}</option>`
  ).join("");

  return `
    <div class="subsection">
      <span class="field-label">Pacing — reading-speed cooldown</span>
      <div class="panel-grid">
        ${field("Chars per second", numInput("chars_per_second", cd.chars_per_second ?? 18, "0.1"))}
        ${field("Min seconds", numInput("min_seconds", cd.min_seconds ?? 1.5, "0.1"))}
        ${field("Max seconds", numInput("max_seconds", cd.max_seconds ?? 15, "0.1"))}
      </div>
    </div>
    <div class="subsection">
      <span class="field-label">Engine</span>
      <div class="panel-grid">
        ${field("Max turns", numInput("max_turns", en.max_turns ?? 60))}
        ${field("Max consecutive silences", numInput("max_consecutive_silences", en.max_consecutive_silences ?? 2))}
        ${field("Temperature", numInput("temperature", en.temperature ?? 0.9, "0.05"))}
        ${field("Max tokens", numInput("max_tokens", en.max_tokens ?? 420))}
        ${field("First speaker", `<select data-f="first_speaker">${opts}</select>`)}
      </div>
      ${checkInput("use_silence_tool", en.use_silence_tool !== false, "Use the silence tool (characters may choose not to post)")}
      ${checkInput("director_end_check", !!en.director_end_check, "Ask the director after each post whether to end the scene")}
    </div>
    <div class="subsection">
      <span class="field-label">Narration</span>
      ${field("Prefix label", textInput("prefix", na.prefix ?? "[the scene, unspoken]", 'placeholder="[the scene, unspoken]"'))}
    </div>
    <div class="subsection">
      <span class="field-label">Memory</span>
      ${checkInput("auto_compact", me.auto_compact !== false, "Auto-compact memory during the scene")}
      <div class="panel-grid">
        ${field("Compact at N events", numInput("compact_at_events", me.compact_at_events ?? 12))}
      </div>
    </div>`;
}

function buildSettings() {
  const prev = state.currentTab;
  const tabsEl = $("#settings-tabs");
  const body = $("#settings-body");
  tabsEl.innerHTML = "";
  body.innerHTML = "";

  const tabs = [];
  state.characters.forEach((c, i) =>
    tabs.push({ key: `char-${i}`, label: `Character ${charLetter(i)}`, html: () => charPanelHtml(i) }));
  tabs.push({ key: "director", label: "Director", html: directorPanelHtml });
  tabs.push({ key: "pacing", label: "Pacing", html: pacingPanelHtml });

  for (const t of tabs) {
    const b = el("button", "tab", t.label);
    b.type = "button";
    b.setAttribute("role", "tab");
    b.dataset.tab = t.key;
    b.addEventListener("click", () => selectTab(t.key));
    tabsEl.appendChild(b);

    const panel = el("section", "tab-panel hidden");
    panel.dataset.panel = t.key;
    panel.innerHTML = t.html();
    body.appendChild(panel);
  }
  selectTab(tabs.some((t) => t.key === prev) ? prev : tabs[0].key);
}

function selectTab(key) {
  state.currentTab = key;
  document.querySelectorAll("#settings-tabs .tab").forEach((b) => {
    const on = b.dataset.tab === key;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll("#settings-body .tab-panel").forEach((p) => {
    p.classList.toggle("hidden", p.dataset.panel !== key);
  });
}

function openSettings() {
  takeSnapshot();
  buildSettings();
  $("#settings-status").textContent = "";
  $("#new-char-form").classList.add("hidden");
  showModal("#modal-settings");
}

/* ---- reading form values ---- */
function panelValues(target) {
  const p = typeof target === "string"
    ? document.querySelector(`[data-panel="${target}"]`)
    : target;
  if (!p) return null;
  const out = {};
  p.querySelectorAll("[data-f]").forEach((inp) => {
    out[inp.dataset.f] = inp.type === "checkbox" ? inp.checked : inp.value;
  });
  return out;
}

function readDemos(panel, keepEmpties) {
  const out = {};
  panel.querySelectorAll("[data-demo]").forEach((inp) => {
    const v = inp.value.trim();
    if (v || keepEmpties) out[inp.dataset.demo] = v;
  });
  return out;
}
// order-insensitive view of a demographics object with empty values dropped
const stripEmpty = (obj) => Object.fromEntries(
  Object.entries(obj || {})
    .filter(([, v]) => String(v).trim() !== "")
    .sort((a, b) => a[0].localeCompare(b[0])));

function diffCharPayload(i) {
  const panel = document.querySelector(`[data-panel="char-${i}"]`);
  const s = snap.chars[i];
  if (!panel || !s) return null;
  const v = panelValues(panel);
  const payload = {};

  for (const f of ["screenname", "real_name", "persona", "prompt_extra"]) {
    if ((s[f] || "") !== (v[f] || "")) payload[f] = v[f] || "";
  }
  // Compare effective (non-empty) demographics; if changed, send the full
  // form values so cleared fields are actually cleared server-side.
  const demosNow = readDemos(panel, true);
  if (JSON.stringify(stripEmpty(s.demographics)) !== JSON.stringify(stripEmpty(demosNow))) {
    payload.demographics = demosNow;
  }
  const endpoint = {
    base_url: (v.base_url || "").trim().replace(/\/+$/, ""),
    model: (v.model || "").trim(),
    api_key: v.api_key || null,
    timeout: fnum(v.timeout, 180),
  };
  if (JSON.stringify(s.endpoint || {}) !== JSON.stringify(endpoint)) {
    payload.endpoint = endpoint;
  }
  return payload;
}

function diffConfigPayload() {
  const cfg = snap.config || {};
  const config = {};

  const d = panelValues("director");
  if (d) {
    const director = Object.assign({}, cfg.director || {}, {
      base_url: (d.base_url || "").trim().replace(/\/+$/, ""),
      model: (d.model || "").trim(),
      api_key: d.api_key || null,
      timeout: fnum(d.timeout, 180),
    });
    if (JSON.stringify(cfg.director || {}) !== JSON.stringify(director)) config.director = director;
  }

  const p = panelValues("pacing");
  if (p) {
    const cooldown = Object.assign({}, cfg.cooldown || {}, {
      chars_per_second: fnum(p.chars_per_second, 18),
      min_seconds: fnum(p.min_seconds, 1.5),
      max_seconds: fnum(p.max_seconds, 15),
    });
    const engine = Object.assign({}, cfg.engine || {}, {
      max_turns: inum(p.max_turns, 60),
      max_consecutive_silences: inum(p.max_consecutive_silences, 2),
      temperature: fnum(p.temperature, 0.9),
      max_tokens: inum(p.max_tokens, 420),
      first_speaker: inum(p.first_speaker, 0),
      use_silence_tool: !!p.use_silence_tool,
      director_end_check: !!p.director_end_check,
    });
    const narration = Object.assign({}, cfg.narration || {}, { prefix: p.prefix || "" });
    const memory = Object.assign({}, cfg.memory || {}, {
      auto_compact: !!p.auto_compact,
      compact_at_events: inum(p.compact_at_events, 12),
    });
    if (JSON.stringify(cfg.cooldown || {}) !== JSON.stringify(cooldown)) config.cooldown = cooldown;
    if (JSON.stringify(cfg.engine || {}) !== JSON.stringify(engine)) config.engine = engine;
    if (JSON.stringify(cfg.narration || {}) !== JSON.stringify(narration)) config.narration = narration;
    if (JSON.stringify(cfg.memory || {}) !== JSON.stringify(memory)) config.memory = memory;
  }
  return config;
}

async function saveSettings() {
  const btn = $("#btn-save-settings");
  const status = $("#settings-status");
  btn.disabled = true;
  status.textContent = "Saving…";

  try {
    // 1) characters (only the ones that actually changed; 404 handled gently)
    for (let i = 0; i < state.characters.length; i++) {
      const ch = state.characters[i];
      const payload = diffCharPayload(i);
      if (!payload || !Object.keys(payload).length) continue;
      try {
        await api(`/api/characters/${encodeURIComponent(ch.id)}`, { method: "PUT", body: payload });
      } catch (err) {
        if (err.status === 404) {
          toast(`Character "${displayName(ch)}" no longer exists on the server`, "error");
        } else throw err;
      }
    }

    // 2) director / pacing / narration / memory / engine via POST /api/config
    const config = diffConfigPayload();
    if (Object.keys(config).length) {
      await api("/api/config", { method: "POST", body: { config } });
    }

    await loadState();          // pull the authoritative values back
    takeSnapshot();
    buildSettings();
    status.textContent = "Saved ✓";
    toast("Settings saved", "ok");
  } catch (err) {
    status.textContent = "";
    toast(`Save failed: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

/* ---- settings actions: regenerate / delete / new character ---- */
function wireSettingsActions() {
  $("#btn-save-settings").addEventListener("click", saveSettings);

  $("#settings-body").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const id = btn.dataset.char;
    const idx = state.characters.findIndex((c) => c.id === id);
    const ch = idx >= 0 ? state.characters[idx] : null;
    if (!ch) return;

    if (btn.dataset.act === "regenerate") {
      const label = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Regenerating…";   // director call, can be slow
      try {
        const data = await api(`/api/characters/${encodeURIComponent(id)}/regenerate`, { method: "POST" });
        if (data.character && idx >= 0) state.characters[idx] = data.character;
        takeSnapshot();
        buildSettings();
        toast(`Regenerated ${displayName(data.character || ch)}`, "ok");
      } catch (err) {
        btn.disabled = false;
        btn.textContent = label;
        toast(`Regenerate failed: ${err.message}`, "error");
      }
      return;
    }

    if (btn.dataset.act === "delete-char") {
      if (!window.confirm(`Delete character "${displayName(ch)}"? Scenes using it may break.`)) return;
      try {
        await api(`/api/characters/${encodeURIComponent(id)}`, { method: "DELETE" });
        toast("Character deleted");
        await loadState();
        takeSnapshot();
        buildSettings();
      } catch (err) {
        toast(`Delete failed: ${err.message}`, "error");
      }
    }
  });

  // new character (slow director call -> explicit loading state)
  $("#btn-new-char").addEventListener("click", () => {
    const form = $("#new-char-form");
    form.classList.toggle("hidden");
    if (!form.classList.contains("hidden")) {
      const seed = state.characters[0] && state.characters[0].endpoint;
      $("#nc-base-url").value = (seed && seed.base_url) || "http://127.0.0.1:8080/v1";
      $("#nc-model").value = "";
      $("#nc-timeout").value = (seed && seed.timeout) || 180;
      $("#nc-seed").value = "";
      $("#nc-status").textContent = "";
      $("#nc-seed").focus();
    }
  });
  $("#nc-cancel").addEventListener("click", () => $("#new-char-form").classList.add("hidden"));

  $("#new-char-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#nc-submit");
    const status = $("#nc-status");
    btn.disabled = true;
    btn.textContent = "Generating…";
    status.textContent = "The director is writing this character — this can take a while…";
    try {
      const data = await api("/api/characters", {
        method: "POST",
        body: {
          generate: true,
          prompt_extra: $("#nc-seed").value.trim(),
          endpoint: {
            base_url: ($("#nc-base-url").value.trim() || "http://127.0.0.1:8080/v1").replace(/\/+$/, ""),
            model: $("#nc-model").value.trim(),
            timeout: fnum($("#nc-timeout").value, 180),
          },
        },
      });
      await loadState();
      takeSnapshot();
      buildSettings();
      const newId = data.character && data.character.id;
      const idx = state.characters.findIndex((c) => c.id === newId);
      if (idx >= 0) selectTab(`char-${idx}`);
      $("#new-char-form").classList.add("hidden");
      toast(`Character ${displayName(data.character)} created`, "ok");
    } catch (err) {
      status.textContent = "";
      toast(`Could not create character: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Generate character";
    }
  });
}

/* ---------------------------------------------------------- boot */
async function loadState() {
  const st = await api("/api/state");
  state.config = st.config || null;
  state.characters = st.characters || [];
  state.scenes = st.scenes || [];
  state.running = !!st.running;
  // drop the selection if the active scene disappeared server-side
  if (state.activeSceneId && !state.scenes.some((s) => s.id === state.activeSceneId)) {
    closeStream();
    state.activeSceneId = null;
    state.events = [];
    state.seenIds.clear();
    $("#stream").innerHTML = "";
  }
  renderAll();
  return st;
}

async function boot() {
  let st;
  try {
    st = await loadState();
  } catch (err) {
    toast(`Could not load state: ${err.message}`, "error");
    renderAll();
    return;
  }

  const first = st.active_scene || (state.scenes[0] && state.scenes[0].id) || null;
  if (first) await selectScene(first);
  else renderAll();

  // Fewer than two characters: friendly prompt, settings opens itself.
  if (state.characters.length < 2) {
    openSettings();
    toast("Create at least two characters to start a scene");
  }
}

/* ---------------------------------------------------------- init */
function init() {
  watchScroll();
  wireComposer();
  wireControls();
  wireModals();
  wireNewScene();
  wireSettingsActions();
  boot();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
