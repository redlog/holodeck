import json
import os
import re
from datetime import datetime
from pathlib import Path

GAMES_DIR = Path(__file__).parent.parent / "games"


def _game_dir(game_slug):
    return GAMES_DIR / game_slug


def _save_dir(game_slug):
    return _game_dir(game_slug) / "saves"


def _game_file(game_slug):
    return _game_dir(game_slug) / "game.json"


def slugify(title):
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "untitled"


def _migrate_if_needed(game_dir):
    """Migrate old directory structure to new layout."""
    old_autosave = game_dir / "saves" / "autosave.json"
    new_game_file = game_dir / "game.json"
    old_cache = game_dir / "cache"

    if old_autosave.exists() and not new_game_file.exists():
        os.replace(old_autosave, new_game_file)

    if old_cache.exists():
        for subdir in ("rooms", "portraits"):
            old_sub = old_cache / subdir
            new_sub = game_dir / subdir
            if old_sub.exists() and not new_sub.exists():
                old_sub.rename(new_sub)
        try:
            old_cache.rmdir()
        except OSError:
            pass


def list_games():
    if not GAMES_DIR.exists():
        return []
    games = []
    for d in sorted(GAMES_DIR.iterdir()):
        if not d.is_dir():
            continue
        _migrate_if_needed(d)
        game_file = d / "game.json"
        if game_file.exists():
            try:
                with open(game_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                title = data.get("meta", {}).get("title", d.name)
                last_played = data.get("meta", {}).get("last_played")
                games.append({"slug": d.name, "title": title, "last_played": last_played})
            except Exception:
                games.append({"slug": d.name, "title": d.name, "last_played": None})
        else:
            games.append({"slug": d.name, "title": d.name, "last_played": None})
    return games


def save_game(world_state, game_slug, slot="autosave"):
    world_state["meta"]["last_saved"] = datetime.now().isoformat()
    world_state["meta"]["last_played"] = datetime.now().isoformat()

    if slot == "autosave":
        filename = _game_file(game_slug)
    else:
        save_dir = _save_dir(game_slug)
        save_dir.mkdir(parents=True, exist_ok=True)
        filename = save_dir / f"{slot}.json"

    temp = filename.with_suffix(".json.tmp")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(world_state, f, indent=2, ensure_ascii=False)
    os.replace(temp, filename)


def load_game(game_slug, slot="autosave"):
    if slot == "autosave":
        filename = _game_file(game_slug)
    else:
        filename = _save_dir(game_slug) / f"{slot}.json"
    if not filename.exists():
        return None
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)


def get_save_slots(game_slug):
    slots = []
    if _game_file(game_slug).exists():
        slots.append("autosave")
    save_dir = _save_dir(game_slug)
    if save_dir.exists():
        for f in save_dir.glob("*.json"):
            if not f.name.endswith(".tmp"):
                slots.append(f.stem)
    return slots


def get_game_dir(game_slug):
    d = _game_dir(game_slug)
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_game(title):
    slug = slugify(title)
    base = slug
    counter = 2
    while _game_dir(slug).exists():
        slug = f"{base}-{counter}"
        counter += 1

    game_dir = _game_dir(slug)
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "rooms").mkdir(exist_ok=True)
    (game_dir / "portraits").mkdir(exist_ok=True)
    (game_dir / "saves").mkdir(exist_ok=True)

    ws = new_game()
    ws["meta"]["title"] = title
    save_game(ws, slug, "autosave")
    return slug, ws


def new_game():
    """Initial empty world_state for a brand-new game.

    Schema is intentionally minimal; the new DM (see
    design/text_adventure_design.md) will extend it with locations, npcs,
    plot_threads, dm_bible, narrative_clock, etc. once the setup
    conversation completes.
    """
    return {
        "meta": {
            "title": "Untitled Adventure",
            "version": "2.0",
            "created": datetime.now().isoformat(),
            "last_played": None,
            "last_saved": None,
            "tone": "",
            "visual_style": "",
        },
        "dm_instructions": {
            "plot_seeds": [],
            "hard_constraints": [],
            "world_rules": [],
        },
        "player": {
            "name": None,
            "description": None,
            "portrait_path": None,
            "inventory": [],
        },
        "current_location_id": None,
        "locations": {},
        "npcs": {},
        "plot_threads": [],
        "dm_bible": {
            "secrets": [],
            "planned_beats": [],
            "scratchpad": "",
        },
    }
