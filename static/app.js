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
  $("topbar").hidden = true;   // menu screen is all you see
  setDrawer(false);
  for (const id of ["btn-layout", "btn-save", "btn-load", "btn-inventory"]) $(id).hidden = true;
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
    li.innerHTML =
      `<span class="title">${escapeHtml(g.title)}</span>` +
      `<span class="when">${when}</span>` +
      `<button class="del" title="Delete this adventure">×</button>`;
    li.onclick = () => openGame(g.slug);
    li.querySelector(".del").onclick = (e) => {
      e.stopPropagation(); // don't open the game we're deleting
      deleteGame(g);
    };
    ul.appendChild(li);
  }
}

async function deleteGame(g) {
  if (!confirm(`Delete "${g.title}" permanently? This cannot be undone.`)) return;
  try {
    await api("DELETE", `/api/games/${g.slug}`);
  } catch (err) {
    alert(`Could not delete the adventure.\n${err.message}`);
    return;
  }
  loadGameList();
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
  $("topbar").hidden = false;
  for (const id of ["btn-layout", "btn-save", "btn-load", "btn-inventory"]) $(id).hidden = false;
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
  $("stage").classList.toggle("no-room", !inPlay);

  // Who's here + visible exits, shown under the scene image.
  renderSceneInfo(v, inPlay);

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

// Bottom-of-scene overlay: present NPCs (portrait thumbnails; name only once
// known) on the left, spoiler-free exit labels on the right.
function renderSceneInfo(v, inPlay) {
  const box = $("scene-info");
  const npcs = (inPlay && v.npcs_present) || [];
  const exits = (inPlay && v.exits) || [];
  if (!npcs.length && !exits.length) { box.hidden = true; return; }
  box.hidden = false;

  const nbox = $("scene-npcs");
  nbox.innerHTML = "";
  for (const n of npcs) {
    const chip = document.createElement("div");
    chip.className = "npc-chip" + (n.known ? "" : " unknown");
    const thumb = n.portrait_url
      ? `<img src="${n.portrait_url}" alt="">`
      : `<span class="npc-ph">?</span>`;
    const cap = n.known ? escapeHtml(n.name || "") : "?";
    chip.innerHTML = `<div class="npc-thumb">${thumb}</div><div class="npc-cap">${cap}</div>`;
    nbox.appendChild(chip);
  }

  const ebox = $("scene-exits");
  if (exits.length) {
    const parts = exits.map((e) => {
      const label = escapeHtml(e.label);
      return e.destination
        ? `${label} <span class="exit-dest">→ ${escapeHtml(e.destination)}</span>`
        : label;
    });
    ebox.innerHTML = `<span class="exits-label">Exits:</span> ` +
      parts.join(`<span class="exit-sep"> · </span>`);
    ebox.hidden = false;
  } else {
    ebox.hidden = true;
    ebox.innerHTML = "";
  }
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
function setDrawer(open) {
  $("drawer").hidden = !open;
  // In columns layout this class adds/removes the inventory grid column;
  // in scene-top layout it's harmless (the drawer is a fixed overlay).
  $("stage").classList.toggle("drawer-open", open);
}
function toggleDrawer() { setDrawer($("drawer").hidden); }

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

// ---------- layout + resizable divider ----------
const LAYOUT_KEY = "holodeck_layout";
const ROOM_H_KEY = "holodeck_room_h";
const TEXT_W_KEY = "holodeck_text_w";

let layout = localStorage.getItem(LAYOUT_KEY) || "scene-top";
let roomH = parseInt(localStorage.getItem(ROOM_H_KEY), 10);
let textW = parseInt(localStorage.getItem(TEXT_W_KEY), 10);
if (isNaN(roomH)) roomH = Math.round(window.innerHeight * 0.48);
if (isNaN(textW)) textW = 380;

function isColumns() { return layout === "columns"; }

function applySizes() {
  const stage = $("stage");
  const minH = 140, maxH = Math.max(minH, window.innerHeight - 260);
  roomH = Math.max(minH, Math.min(maxH, roomH));
  const minW = 220, maxW = Math.max(minW, window.innerWidth - 360);
  textW = Math.max(minW, Math.min(maxW, textW));
  stage.style.setProperty("--room-h", roomH + "px");
  stage.style.setProperty("--text-w", textW + "px");
  localStorage.setItem(ROOM_H_KEY, String(roomH));
  localStorage.setItem(TEXT_W_KEY, String(textW));
}

function applyLayout() {
  const stage = $("stage");
  stage.classList.toggle("layout-columns", isColumns());
  stage.classList.toggle("layout-scene-top", !isColumns());
  $("btn-layout").textContent = isColumns() ? "Layout: Columns" : "Layout: Scene top";
  localStorage.setItem(LAYOUT_KEY, layout);
}

function toggleLayout() {
  layout = isColumns() ? "scene-top" : "columns";
  applyLayout();
}

function initResizer() {
  applyLayout();
  applySizes();
  $("room-resizer").addEventListener("mousedown", (e) => {
    e.preventDefault();
    const columns = isColumns();
    const startX = e.clientX, startY = e.clientY;
    const startW = textW, startH = roomH;
    const onMove = (ev) => {
      if (columns) textW = startW + (ev.clientX - startX);   // drag horizontally
      else roomH = startH + (ev.clientY - startY);           // drag vertically
      applySizes();
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.userSelect = "";
    };
    document.body.style.userSelect = "none";
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
  window.addEventListener("resize", applySizes); // re-clamp to the new viewport
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- wiring ----------
$("btn-new").onclick = newGame;
$("btn-menu").onclick = showMenu;
$("btn-layout").onclick = toggleLayout;
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

initResizer();
showMenu();
