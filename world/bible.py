import codecs
import copy
import json
import os
import re
from datetime import datetime
from pathlib import Path

GAMES_DIR = Path(__file__).parent.parent / "games"

# Marker placed on saved files once their secret fields have been ROT13'd.
# Saves WITHOUT this flag are treated as plaintext (backwards-compat with
# pre-obfuscation autosaves).
OBFUSCATED_FLAG = "_secrets_obfuscated_v1"


def _rot13(s):
    if not isinstance(s, str):
        return s
    return codecs.encode(s, "rot_13")


def _transform_secrets(ws):
    """Apply ROT13 to fields that would spoil the game if read casually.

    ROT13 is self-inverse, so the same function obfuscates on save and
    deobfuscates on load. Operates IN PLACE on the passed dict — callers
    that need to preserve the original should pass a deep copy.
    """
    bible = ws.get("dm_bible")
    if isinstance(bible, dict):
        for secret in bible.get("secrets") or []:
            if isinstance(secret, dict) and isinstance(secret.get("fact"), str):
                secret["fact"] = _rot13(secret["fact"])
        if isinstance(bible.get("planned_beats"), list):
            bible["planned_beats"] = [_rot13(b) for b in bible["planned_beats"]]
        if isinstance(bible.get("scratchpad"), str):
            bible["scratchpad"] = _rot13(bible["scratchpad"])

    # NPC hidden knowledge
    for npc in (ws.get("npcs") or {}).values():
        if isinstance(npc, dict):
            if isinstance(npc.get("hides"), list):
                npc["hides"] = [_rot13(h) for h in npc["hides"]]
            if isinstance(npc.get("lies_about"), list):
                npc["lies_about"] = [_rot13(l) for l in npc["lies_about"]]

    # Plot threads the player doesn't yet know about
    for thread in ws.get("plot_threads") or []:
        if isinstance(thread, dict) and not thread.get("known_to_player", True):
            if isinstance(thread.get("summary"), str):
                thread["summary"] = _rot13(thread["summary"])
    return ws


def _obfuscate_for_save(ws):
    """Return a deep copy of ws with secret fields ROT13'd and the flag set."""
    out = copy.deepcopy(ws)
    _transform_secrets(out)
    out[OBFUSCATED_FLAG] = True
    return out


def _deobfuscate_after_load(ws):
    """If the loaded state is marked obfuscated, decode in place and drop flag."""
    if ws.pop(OBFUSCATED_FLAG, False):
        _transform_secrets(ws)
    _migrate_inventory(ws)
    _migrate_npcs(ws)
    return ws


def _migrate_npcs(ws):
    """Ensure all NPCs have the fields needed by the NPC agent system."""
    for npc_id, npc in ws.get("npcs", {}).items():
        npc.setdefault("id", npc_id)
        npc.setdefault("voice", "")
        npc.setdefault("knows", [])
        npc.setdefault("hides", [])
        npc.setdefault("lies_about", [])
        npc.setdefault("known_to_player", True)
        npc.setdefault("dialog_summary_with_player", "")
        npc.setdefault("portrait_path", None)


def _migrate_inventory(ws):
    """Upgrade flat string inventory entries to rich dicts."""
    import re
    inv = ws.get("player", {}).get("inventory", [])
    migrated = []
    for entry in inv:
        if isinstance(entry, dict):
            entry.setdefault("item_id", re.sub(r"[^a-z0-9]+", "_", entry.get("item", "item").lower()).strip("_"))
            entry.setdefault("provenance", "")
            entry.setdefault("found_location_id", "")
            entry.setdefault("found_location_name", "")
            entry.setdefault("turn_acquired", 0)
            entry.setdefault("sprite_path", None)
            entry.setdefault("visual_description", "")
            migrated.append(entry)
        elif isinstance(entry, str):
            item_id = re.sub(r"[^a-z0-9]+", "_", entry.lower()).strip("_")
            migrated.append({
                "item": entry,
                "item_id": item_id,
                "provenance": "",
                "found_location_id": "",
                "found_location_name": "",
                "turn_acquired": 0,
                "sprite_path": None,
                "visual_description": "",
            })
        else:
            migrated.append({"item": str(entry), "item_id": "unknown", "provenance": "",
                             "found_location_id": "", "found_location_name": "",
                             "turn_acquired": 0, "sprite_path": None, "visual_description": ""})
    if inv:
        ws["player"]["inventory"] = migrated


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

    filename.parent.mkdir(parents=True, exist_ok=True)
    to_write = _obfuscate_for_save(world_state)

    temp = filename.with_suffix(".json.tmp")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(to_write, f, indent=2, ensure_ascii=False)
    os.replace(temp, filename)


def load_game(game_slug, slot="autosave"):
    if slot == "autosave":
        filename = _game_file(game_slug)
    else:
        filename = _save_dir(game_slug) / f"{slot}.json"
    if not filename.exists():
        return None
    with open(filename, "r", encoding="utf-8") as f:
        ws = json.load(f)
    return _deobfuscate_after_load(ws)


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
    (game_dir / "items").mkdir(exist_ok=True)
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
            "premise": "",
            "starting_location_concept": "",
            "interview_summary": "",
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
