"""FastAPI application — the transport layer in front of GameSession.

Endpoints are declared with plain `def` (not `async def`), so FastAPI runs
them in its threadpool. That lets the DM's blocking Gemini calls happen
without an event-loop dance, and a per-session lock (inside GameSession)
serializes turns for a given game.

Sessions live in a process-level registry so the DM keeps its in-memory
history and context cache across requests.
"""

import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from world.bible import (
    GAMES_DIR, list_games, create_game, load_game, get_save_slots, delete_game,
)
from server.session import GameSession
from server.view import to_player_view

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

# Ensure the games dir exists before mounting it as static media.
GAMES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="The Holodeck")

_sessions = {}
_sessions_lock = threading.Lock()


def _log(msg):
    print(f"[APP] {msg}", file=sys.stderr, flush=True)


def _get_session(slug, *, load_if_missing=True):
    """Return the live session for slug, loading the autosave if needed."""
    with _sessions_lock:
        sess = _sessions.get(slug)
    if sess is not None:
        return sess
    if not load_if_missing:
        raise HTTPException(status_code=404, detail=f"Session not open: {slug}")

    loaded = load_game(slug)
    if loaded is None:
        raise HTTPException(status_code=404, detail=f"No such game: {slug}")
    ws, session_data = loaded
    session_data = session_data or {}
    sess = GameSession(
        slug, ws,
        play_history=session_data.get("play_history"),
        console_lines=session_data.get("console_lines"),
    )
    with _sessions_lock:
        # Another request may have raced us; keep the first one.
        sess = _sessions.setdefault(slug, sess)
    sess.start()  # narrate opening / resumed scene; no-op if already started
    return sess


class InputBody(BaseModel):
    text: str


class SlotBody(BaseModel):
    slot: str


@app.get("/api/games")
def api_list_games():
    return {"games": list_games()}


@app.post("/api/games")
def api_create_game():
    slug, ws = create_game()
    sess = GameSession(slug, ws)
    with _sessions_lock:
        _sessions[slug] = sess
    sess.start()
    return {"view": to_player_view(sess)}


@app.delete("/api/games/{slug}")
def api_delete_game(slug: str):
    # Evict any live session first so nothing keeps the files open or
    # re-autosaves the directory we're about to remove.
    with _sessions_lock:
        _sessions.pop(slug, None)
    try:
        existed = delete_game(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        # Windows/Dropbox can transiently lock files mid-delete.
        _log(f"delete failed for {slug}: {e}")
        raise HTTPException(status_code=500, detail=f"Could not delete: {e}")
    if not existed:
        raise HTTPException(status_code=404, detail=f"No such game: {slug}")
    _log(f"Deleted game: {slug}")
    return {"deleted": slug}


@app.post("/api/games/{slug}/open")
def api_open_game(slug: str):
    sess = _get_session(slug)
    return {"view": to_player_view(sess)}


@app.get("/api/games/{slug}")
def api_get_view(slug: str):
    sess = _get_session(slug)
    return {"view": to_player_view(sess)}


@app.post("/api/games/{slug}/input")
def api_input(slug: str, body: InputBody):
    sess = _get_session(slug)
    sess.submit(body.text)
    return {"view": to_player_view(sess)}


@app.get("/api/games/{slug}/poll")
def api_poll(slug: str):
    sess = _get_session(slug)
    sess.poll_assets()
    return {"view": to_player_view(sess)}


@app.get("/api/games/{slug}/saves")
def api_saves(slug: str):
    return {"slots": get_save_slots(slug)}


@app.post("/api/games/{slug}/save")
def api_save(slug: str, body: SlotBody):
    sess = _get_session(slug)
    sess.save_to_slot(body.slot)
    return {"slots": get_save_slots(slug)}


@app.post("/api/games/{slug}/load")
def api_load(slug: str, body: SlotBody):
    loaded = load_game(slug, body.slot)
    if loaded is None:
        raise HTTPException(status_code=404, detail="Save not found")
    ws, session_data = loaded
    session_data = session_data or {}
    # Replace the live session wholesale; mark started so we restore the
    # transcript rather than re-narrating an opening.
    sess = GameSession(
        slug, ws,
        play_history=session_data.get("play_history"),
        console_lines=session_data.get("console_lines"),
        started=True,
    )
    with _sessions_lock:
        _sessions[slug] = sess
    return {"view": to_player_view(sess)}


# Static media (generated images) and the frontend.
app.mount("/media", StaticFiles(directory=str(GAMES_DIR)), name="media")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
