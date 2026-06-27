"""Player-facing projection of world_state.

CRITICAL: world_state holds spoilers — the DM bible (secrets, planned
beats, scratchpad), NPC `hides`/`lies_about`, NPC intents, and hidden plot
threads. The pygame client never sent state over a wire, so nothing in the
codebase stops those from leaking. The moment state reaches a browser,
"view source" would defeat the whole mystery.

`to_player_view` is the allow-list filter: only fields the player is meant
to see ever leave the server. This is a DIFFERENT (stricter) filter than the
ROT13 obfuscation in world/bible.py, which only protects the save file.
"""

import os
from pathlib import Path

from world.bible import GAMES_DIR


def media_url(path):
    """Map a generated-image filesystem path to a served URL, with an
    mtime cache-buster (room images are overwritten in place, so the path
    alone is not enough to make the browser reload)."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        rel = p.relative_to(GAMES_DIR)
    except ValueError:
        return None
    try:
        version = int(os.path.getmtime(p))
    except OSError:
        version = 0
    return "/media/" + "/".join(rel.parts) + f"?v={version}"


def to_player_view(session):
    ws = session.world_state
    meta = ws.get("meta", {})
    player = ws.get("player", {})
    dm_inst = ws.get("dm_instructions", {})
    npcs = ws.get("npcs", {})

    loc_id = ws.get("current_location_id")
    loc = ws.get("locations", {}).get(loc_id, {}) if loc_id else {}

    # NPCs visibly present — name + portrait only. Intents/knows/hides/lies
    # are deliberately omitted, and unknown NPCs aren't listed at all.
    present = []
    for nid in loc.get("present_npc_ids", []):
        npc = npcs.get(nid)
        if npc and npc.get("known_to_player", True):
            present.append({
                "id": nid,
                "name": npc.get("name", nid),
                "portrait_url": media_url(npc.get("portrait_path")),
            })

    inventory = []
    for entry in player.get("inventory", []):
        if isinstance(entry, dict):
            inventory.append({
                "item": entry.get("item", "???"),
                "item_id": entry.get("item_id"),
                "provenance": entry.get("provenance", ""),
                "sprite_url": media_url(entry.get("sprite_path")),
            })
        else:
            inventory.append({"item": str(entry), "item_id": None,
                              "provenance": "", "sprite_url": None})

    speaker_id = session.speaker_id
    if speaker_id == "player":
        speaker_name = player.get("name") or "You"
        speaker_portrait = media_url(player.get("portrait_path"))
    else:
        snpc = npcs.get(speaker_id, {})
        speaker_name = snpc.get("name") or speaker_id
        speaker_portrait = media_url(snpc.get("portrait_path"))

    # Only plot threads the player knows about.
    threads = [
        {"summary": t.get("summary", ""), "status": t.get("status", "")}
        for t in ws.get("plot_threads", [])
        if t.get("known_to_player")
    ]

    return {
        "slug": session.slug,
        "phase": session.dm.phase,
        "title": meta.get("title", "Untitled Adventure"),
        "tone": meta.get("tone", ""),
        "visual_style": meta.get("visual_style", ""),
        "clock": ws.get("narrative_clock", ""),
        "location": {
            "id": loc_id,
            "name": loc.get("name", ""),
            "summary": loc.get("summary", ""),
            "image_url": media_url(loc.get("image_path")),
        },
        "speaker": {
            "id": speaker_id,
            "name": speaker_name,
            "portrait_url": speaker_portrait,
        },
        "npcs_present": present,
        "inventory": inventory,
        "threads": threads,
        "transcript": [{"source": s, "text": t} for s, t in session.transcript],
        "pending": session.pending,
        # Setup/interview screen fields (these are what the player is building,
        # so they are safe to surface).
        "player_name": player.get("name") or "",
        "player_description": player.get("description") or "",
        "premise": dm_inst.get("premise") or "",
        "plot_seeds": dm_inst.get("plot_seeds") or [],
    }
