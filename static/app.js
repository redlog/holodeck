"use strict";

// ---------- tiny helpers ----------
const $ = (id) => document.getElementById(id);
const api = async (method, path, body) => {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status}`);
  return res.json();
};

// ---------- state ----------
let slug = null;
let view = null;
let pollTimer = null;
let busy = false; // a DM turn is in flight

// ---------- screens ----------
function showMenu() {
  stopPolling();
  slug = null;
  view = null;
  $("menu").hidden = false;
  $("stage").hidden = true;
  $("drawer").hidden = true;
  for (const id of ["btn-save", "btn-load", "btn-inventory"]) $(id).hidden = true;
  $("title").textContent = "The Holodeck";
  $("clock").textContent = "";
  loadGameList();
}

async function loadGameList() {
  const { games } = await api("GET", "/api/games");
  const ul = $("game-list");
  ul.innerHTML = "";
  if (!games.length) {
    ul.innerHTML = '<li class="muted">No saved adventures yet.</li>';
    return;
  }
  for (const g of games) {
    const li = document.createElement("li");
    const when = g.last_played ? new Date(g.last_played).toLocaleString() : "never";
    li.innerHTML = `<span>${escapeHtml(g.title)}</span><span class="when">${when}</span>`;
    li.onclick = () => openGame(g.slug);
    ul.appendChild(li);
  }
}

// ---------- game lifecycle ----------
async function newGame() {
  const res = await api("POST", "/api/games");
  enterStage(res.view);
}

async function openGame(s) {
  slug = s;
  const res = await api("POST", `/api/games/${s}/open`);
  enterStage(res.view);
}

function enterStage(v) {
  $("menu").hidden = true;
  $("stage").hidden = false;
  for (const id of ["btn-save", "btn-load", "btn-inventory"]) $(id).hidden = false;
  applyView(v);
  $("input").focus();
}

// ---------- rendering ----------
function applyView(v) {
  view = v;
  slug = v.slug;

  $("title").textContent = v.title || "The Holodeck";
  $("clock").textContent = v.clock || "";
  $("inv-count").textContent = v.inventory.length;

  const inPlay = v.phase === "play";

  // Room image
  const room = $("room");
  if (inPlay && v.location.image_url) {
    room.style.backgroundImage = `url("${v.location.image_url}")`;
    room.classList.add("has-image");
    $("room-placeholder").hidden = true;
  } else {
    room.style.backgroundImage = "";
    room.classList.remove("has-image");
    const ph = $("room-placeholder");
    ph.hidden = false;
    ph.textContent = v.pending ? "Painting the scene…"
      : inPlay ? "(no scene image)" : "";
  }
  room.style.display = inPlay ? "flex" : "none";

  // Portrait + speaker
  const portrait = $("portrait");
  const url = v.speaker.portrait_url;
  if (inPlay && url) {
    portrait.src = url;
    portrait.hidden = false;
    $("portrait-placeholder").hidden = true;
  } else {
    portrait.hidden = true;
    $("portrait-placeholder").hidden = false;
    $("portrait-placeholder").textContent = v.pending ? "Painting…" : "(portrait)";
  }
  $("speaker-name").textContent = inPlay ? (v.speaker.name || "") : "";

  // Setup status sidebar (interview/creating only)
  const status = $("setup-status");
  if (!inPlay) {
    status.hidden = false;
    status.innerHTML = [
      ["Title", v.title],
      ["Tone", v.tone],
      ["Style", v.visual_style],
      ["Player", v.player_name],
      ["Seeds", (v.plot_seeds || []).join(", ")],
    ].map(([k, val]) =>
      `<div class="row"><span class="label">${k}:</span> ${escapeHtml(val || "—")}</div>`
    ).join("");
  } else {
    status.hidden = true;
  }

  renderTranscript(v.transcript);
  renderDrawer(v.inventory);

  // Input availability
  const input = $("input");
  const creating = v.phase === "creating";
  input.disabled = busy || creating;
  input.placeholder = creating ? "The DM is preparing the world…"
    : v.phase === "interview" ? "Answer the DM…"
    : "What do you do?";

  // Keep polling while images are still rendering.
  if (v.pending) startPolling(); else stopPolling();
}

function renderTranscript(lines) {
  const el = $("transcript");
  el.innerHTML = "";
  for (const { source, text } of lines) {
    const div = document.createElement("div");
    div.className = `line ${source}`;
    div.textContent = text;
    el.appendChild(div);
  }
  el.scrollTop = el.scrollHeight;
}

function renderDrawer(inv) {
  const ul = $("drawer-list");
  ul.innerHTML = "";
  if (!inv.length) {
    ul.innerHTML = '<li class="muted">(empty)</li>';
    return;
  }
  for (const entry of inv) {
    const li = document.createElement("li");
    const img = entry.sprite_url
      ? `<img src="${entry.sprite_url}" alt="">`
      : `<span class="sprite-ph">?</span>`;
    li.innerHTML = `${img}<span>${escapeHtml(entry.item)}</span>`;
    li.onclick = () => openModal(entry);
    ul.appendChild(li);
  }
}

// ---------- input ----------
async function submitInput(text) {
  if (!slug || busy) return;
  busy = true;
  $("input").disabled = true;
  // Optimistically echo the player's line.
  if (view && view.phase !== "creating") {
    view.transcript.push({ source: "user", text });
    renderTranscript(view.transcript);
  }
  try {
    const res = await api("POST", `/api/games/${slug}/input`, { text });
    busy = false;
    applyView(res.view);
  } catch (e) {
    busy = false;
    console.error(e);
    if (view) {
      view.transcript.push({ source: "system", text: `[error: ${e.message}]` });
      applyView(view);
    }
  }
  $("input").focus();
}

// ---------- asset polling ----------
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    if (!slug || busy) return;
    try {
      const res = await api("GET", `/api/games/${slug}/poll`);
      applyView(res.view); // applyView stops the timer once pending clears
    } catch (e) {
      console.error(e);
    }
  }, 1500);
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// ---------- inventory drawer ----------
function toggleDrawer() {
  const d = $("drawer");
  d.hidden = !d.hidden;
}

// ---------- item modal ----------
function openModal(entry) {
  $("modal").hidden = false;
  const img = $("modal-img");
  if (entry.sprite_url) {
    img.src = entry.sprite_url;
    img.hidden = false;
    $("modal-noimg").hidden = true;
  } else {
    img.hidden = true;
    $("modal-noimg").hidden = false;
  }
  $("modal-name").textContent = entry.item;
  $("modal-prov").textContent = entry.provenance || "";
}
function closeModal() { $("modal").hidden = true; }

// ---------- save / load ----------
async function doSave() {
  const name = prompt("Save name:", "quicksave");
  if (!name) return;
  await api("POST", `/api/games/${slug}/save`, { slot: name.trim() });
  flashSystem(`Saved to slot: ${name.trim()}`);
}
async function doLoad() {
  const { slots } = await api("GET", `/api/games/${slug}/saves`);
  if (!slots.length) { flashSystem("No saves found."); return; }
  const name = prompt(`Load which save?\n${slots.join(", ")}`, slots[0]);
  if (!name) return;
  try {
    const res = await api("POST", `/api/games/${slug}/load`, { slot: name.trim() });
    applyView(res.view);
    flashSystem(`Loaded save: ${name.trim()}`);
  } catch (e) {
    flashSystem(`Load failed: ${e.message}`);
  }
}
function flashSystem(text) {
  if (!view) return;
  view.transcript.push({ source: "system", text });
  renderTranscript(view.transcript);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- wiring ----------
$("btn-new").onclick = newGame;
$("btn-menu").onclick = showMenu;
$("btn-save").onclick = doSave;
$("btn-load").onclick = doLoad;
$("btn-inventory").onclick = toggleDrawer;
$("modal").onclick = closeModal;
$("input-form").onsubmit = (e) => {
  e.preventDefault();
  const input = $("input");
  const text = input.value.trim();
  if (text) { input.value = ""; submitInput(text); }
};
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("modal").hidden) closeModal();
});

showMenu();
