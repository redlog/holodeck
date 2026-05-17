# Holodeck Text Adventure — Design

> **Status:** Design in progress. About to rip out the spatial/sprite/walking
> code and rebuild as a visualizable text adventure.
> **Date:** 2026-05-17
> **Replaces:** the sprite-based game; supersedes much of `design.md`.

## What this is

A graphical text adventure with AI-driven characters. The player converses
with a DM through free-text input. Each room has a beautiful background
image; characters have portraits; conversations happen in a single text
stream styled by speaker.

The goal is to play to what AI does well (open-ended narrative reasoning,
character voice, atmospheric image generation) and stop fighting the things
it does poorly (spatially-precise game world, animated sprites,
pixel-accurate priority maps).

## What we're ripping out

Everything that exists to support walking, collision, and sprite animation:

- `world/priority_map.py`
- `world/scene3d.py` (just built, ack)
- `agents/layout.py` (just built, ack)
- The sprite-sheet and walk-frame generation in `agents/character_imagery.py`
  (keep the portrait-generation paths — we need those for player and NPCs)
- `modes/play_mode.py`'s movement, collision, and exit-zone code
- Any character-sprite rendering, foreground masks, depth banding
- `scripts/test_scene3d.py`, `scripts/test_layout.py`, `scripts/test_paint.py`

What survives:

- Holodeck mode (renamed and merged with the DM; see below)
- Portrait generation for characters (player + NPCs)
- Background image generation (simplified — just paint atmospheric scenes,
  no priority maps, no spatial constraints)
- Save/load plumbing (changes format but autosave concept is fine)
- Gemini agent base class and API plumbing

## Player experience

The screen shows a beautiful background image of where the player currently
is, an active-speaker portrait, and a scrolling text panel. The player
types free-form English. The DM responds in narrative prose. NPCs respond
in their own voice with their portrait shown.

A typical exchange:

```
[Room image: a candlelit tavern interior]

The candles flicker as you push the door open. Three patrons look up — a
hooded woman at a corner table, an old man nursing a tankard, and the
bartender, who stops polishing a glass mid-motion.

> approach the bartender
[Portrait swaps to: the bartender]

You step up to the bar. He gives you a slow, measured look.

  Bartender: "Don't see your face around here. What'll it be?"

> ask about the murder
  Bartender: "What murder?"

The hooded woman in the corner has gone very still.
```

## Architecture overview

```
┌────────────────────────────────────────────────────────────────┐
│  Holodeck Mode (game setup)                                    │
│    user ↔ DM converses to define world, player char, opening   │
│    DM writes down secret prep (plot threads, NPC truths)       │
└────────────────────────────────────────────────────────────────┘
                              ↓
                       (player declares ready)
                              ↓
┌────────────────────────────────────────────────────────────────┐
│  Play Mode                                                     │
│                                                                │
│   ┌────────────────────┐   ┌──────────────────────────────┐    │
│   │  UI                │   │  DM agent                    │    │
│   │  - room image      │←──│  - intent parser (1st step)  │    │
│   │  - portrait        │   │  - world state (structured)  │    │
│   │  - text panel      │   │  - hidden GM bible           │    │
│   │  - inventory panel │   │  - narrates everything       │    │
│   │  - input box       │   │  - dispatches to NPCs        │    │
│   └────────────────────┘   └───────────────┬──────────────┘    │
│           ↑                                ↓                   │
│           │      ┌──────────────────────────────────────┐      │
│           │      │  NPC agents (one per active NPC)     │      │
│           │      │  - public + hidden persona           │      │
│           │      │  - dialog memory                     │      │
│           │      │  - current intent / mood             │      │
│           │      │  - speaks in own voice               │      │
│           │      └──────────────────────────────────────┘      │
│           │                                                    │
│           │      ┌──────────────────────────────────────┐      │
│           └──────│  Image agents                        │      │
│                  │  - room background (on scene_dirty)  │      │
│                  │  - portrait (on first appearance)    │      │
│                  └──────────────────────────────────────┘      │
└────────────────────────────────────────────────────────────────┘
```

## State model

The world state is structured JSON the DM owns and mutates. It contains
everything we need to be coherent across long sessions. The recent
transcript is short and gets summarized into state as it ages out.

```
world_state = {
    "title": "The Salt Marsh Mystery",
    "tone": "noir, slightly comic, low-fantasy",
    "visual_style": "moody painterly art, candlelight and fog",
    "narrative_clock": "1888-10-14, evening",   # in-fiction time
    "player": {
        "name": "Vesper Hale",
        "portrait_path": "...",
        "backstory_summary": "...",           # public — what the player chose
        "inventory": [...]                    # see below
    },
    "current_location_id": "tavern",
    "locations": {
        "tavern": {
            "name": "The Bent Tankard",
            "summary": "A low-ceilinged dockside tavern...",
            "image_path": "cache/rooms/tavern.png",
            "image_dirty": false,             # set true when visual changes
            "present_npc_ids": ["bartender", "hooded_woman"],
            "discovered_features": ["bar", "fireplace", "back stairs"],
            "events_log_summary": "Player arrived, asked bartender about murder."
        },
        # ... other defined locations
    },
    "npcs": {
        "bartender": {
            "name": "Old Tom",
            "portrait_path": "...",
            "color": "#c8884d",
            "public_persona": "Gruff dockside bartender, sees everything.",
            "current_location_id": "tavern",
            "current_intent": "Keep an eye on the hooded woman. Avoid trouble.",
            "mood_toward_player": "wary",
            "dialog_summary_with_player": "Denied knowledge of any murder.",
        },
        # ...
    },
    "plot_threads": [
        {
            "id": "murder",
            "summary": "Saltworks foreman found dead in the marsh.",
            "status": "active",
            "known_to_player": true,
        },
        # ...
    ],
    "dm_bible": {
        # HIDDEN from any prompt to NPC agents. Visible only to DM.
        "secrets": [
            {"id": "murderer", "fact": "The hooded woman did it.",
             "revealed": false},
            {"id": "motive", "fact": "Foreman knew about her smuggling.",
             "revealed": false},
        ],
        "planned_beats": [
            "If player presses bartender hard, he hints at the back stairs.",
            "Hooded woman leaves town if confronted publicly."
        ],
        "scratchpad": "...free-form DM notes..."
    }
}
```

The full transcript still gets spilled to disk for debugging and replay,
but the DM's prompt only ever sees the structured state plus the last few
turns. Old turns are folded into `events_log_summary` etc. by a
summarization pass when the recent buffer exceeds some threshold.

## DM agent

**Single agent for the whole session.** One personality, one consistent voice.
The DM is the merger of what we used to call Author + DM.

**Per-turn flow:**

1. **Intent parse.** Free-text input → structured intent:
   ```
   {action: "talk", target_npc: "bartender", topic: "the murder"}
   {action: "move", destination: "the back stairs"}
   {action: "look", target: "the hooded woman"}
   {action: "take", target: "the brass key"}
   {action: "use", target: "key", on: "the office door"}
   {action: "wait", duration: "until morning"}
   {action: "freeform"}   # for "I scream and throw the chair"
   ```
   This is structured-output reasoning on the LLM. The DM has the location
   summary, present NPCs, recent events, and inventory available when parsing.

2. **Resolve.** Based on intent, the DM either:
   - Narrates directly (look, move, take, wait, freeform action)
   - Delegates to an NPC agent (talk, ask, give)
   - Refuses with narration ("you can't pick up the piano")
   - Updates inventory / location / state as needed

3. **Side effects.** The DM emits a small structured diff against world_state:
   ```
   {
       "narration": "You climb the back stairs into a dusty hallway...",
       "state_changes": {
           "current_location_id": "upstairs_hallway",
           "create_location": {...},   # if newly defined
           "image_dirty": ["upstairs_hallway"],
           "inventory_add": [...],
           "inventory_remove": [...],
           "npc_state_changes": {...},
           "advance_clock": "00:05",
           "reveal_secret": ["upstairs_exists"],
       }
   }
   ```
   Engine applies the diff and renders.

4. **Image / portrait triggers.** Any location with `image_dirty=true` queues
   a re-render. Any newly introduced NPC queues portrait generation. Both are
   async; UI shows a placeholder while they finish.

**The DM bible** is the GM-side hidden state. It's never sent to NPC agents
or to the player. It is sent to the DM on every turn so the DM stays
consistent with its own past decisions. When the DM makes new private
decisions ("the brass key is from her smuggling ring"), it appends to the
bible.

**Refusal vs nudge.** When the player asks for something impossible:
- Physically absurd: refuse with in-fiction narration. "The piano is bolted
  to the stage."
- Player asks "am I stuck?": DM offers gentle direction, possibly via NPC
  suggestion or environmental hint. **In life are we ever truly stuck
  unless we're dead?** — there's always at least one path forward; the DM
  finds it. (This is a design rule, not a hard constraint.)

## NPC agents

Each persistent NPC is its own agent (its own system prompt, its own
short-term dialog memory). The DM dispatches conversation by calling the
NPC agent with appropriate context.

**Per-NPC state on every dispatch:**

```
{
    "persona": "Gruff dockside bartender, 50s, ex-sailor.",
    "voice": "Terse. Drops articles. Speaks like he's tired of everyone.",
    "knows": ["the foreman drank here", "saw a hooded figure leave"],
    "hides": ["he was paid to forget"],
    "lies_about": ["whether he saw the hooded figure"],
    "current_intent": "Get the player to leave without trouble.",
    "mood_toward_player": "wary",
    "recent_dialog": [...last 3-4 exchanges with player...],
    "scene_context": "Player just asked about the murder. Hooded woman is
                      across the room, listening."
}
```

The NPC responds with their line of dialog plus optional **side signals**
the DM can use:

```
{
    "speech": "What murder? Don't know what you're talking about.",
    "tells": ["briefly glances at the hooded woman", "fidgets with cloth"],
    "internal_state_change": {
        "mood_toward_player": "more guarded",
        "current_intent": "Get rid of this person."
    }
}
```

`tells` are physical-tell hints the DM weaves into surrounding narration
("…his eyes flick toward the corner for half a second"). Player picks up
or misses these.

**Eavesdropping.** When the player addresses NPC A but other NPCs are
present, the DM asks itself (or runs a quick pass on the other NPCs) whether
each one would care, hear, and react. Reactions can be:
- Silent tell (woven into narration)
- Interjection (NPC speaks unprompted)
- Departure (NPC leaves the room)
- Internal-state change only

Distance, social context, and the NPC's `current_intent` shape this.

**NPC autonomy between scenes.** Each persistent NPC has a `current_intent`
that the DM updates when in-fiction time passes. When the player re-enters
a room, the DM resolves any updates for present NPCs ("Old Tom has gone
home; his daughter is tending the bar now").

## Inventory

Always-visible panel listing items by name. Click an item to see its
**provenance paragraph** — a short narrative of where it came from.

Example:

```
INVENTORY
─────────
brass key       ← click expands ──→ "Found in the top drawer of the desk
                                     in Marta's study. The drawer was
                                     unlocked. You took it while she was
                                     in the kitchen. She doesn't know
                                     yet."
folded note
dockworker's coin
oil lamp (lit)
```

Provenance is generated by the DM at the moment the player acquires the
item — it's a paragraph, not just a fact. Items can change state ("oil
lamp" → "oil lamp (lit)") and the provenance can grow ("…you later used
this to break the lock on the trunk in the cellar").

**No weight or volume mechanic.** Common-sense limit only: the DM refuses
to let the player carry obvious things (piano, large statue, body of an
adult NPC). Borderline cases the DM judges narratively.

## Navigation

**Freeform**, DM-defined just-in-time. The player says "I head down to
the docks" — if the docks have never been defined, the DM defines them
right now (writes a new entry in `locations`, picks a tone-consistent
description, queues an image render).

**Sticky decisions.** Once a place exists in `locations`, it stays. The
tavern is always a tavern with a bar and back stairs. The DM cannot
quietly retcon physical layout. (Mood, NPCs present, lighting, time of
day — those change. The bones don't.)

**Room images** are generated on first definition, cached, and re-rendered
only when `image_dirty` is set by a meaningful state change.

## Time and NPC autonomy

In-fiction time advances **only when narrative warrants it**. Real-world
idle never advances the game clock.

Time advancement is triggered by:
- Explicit player action ("I sleep", "I wait", "I travel to the next city")
- Implied passage (walking from room to room → minutes; significant travel
  → hours or days)
- The DM may also fast-forward when nothing interesting is happening
  ("you spend the afternoon researching at the library; by evening…")

When time passes, the DM runs an NPC tick:
- For each persistent NPC, advance their `current_intent` and
  `current_location_id` according to their persona + the elapsed time.
- For each location, summarize any off-screen events into
  `events_log_summary`.

This isn't a fine-grained simulation. It's a coarse "what would each of
these characters reasonably be doing N hours/days later." The DM may also
trigger plot beats in the bible during this tick ("after two days the
hooded woman attempts to flee town").

## Holodeck (setup) mode

Renamed from holodeck; merged with the DM. The setup conversation IS just
the DM running before play has begun.

**Goal of the setup:** produce a complete enough initial world_state to
start play. Specifically:

- Setting & tone
- Visual style (drives both rooms and portraits)
- Player character: name, backstory, portrait (generated from description)
- Opening situation (where they are, why, what's at stake)
- The DM's hidden prep — initial bible entries (the mystery, who did it,
  who knows what)

The setup ends when the user says "I'm ready" (or similar). The DM commits
the state and the UI swaps to play mode, opening on the first scene.

**Setup conversation is itself a chat with the same DM persona.** No
mode-switching for the player — they're already talking to "their" DM.
The DM just behaves differently before play: asking questions, suggesting
options, hidden-prepping the world.

## UI / rendering

**Layout: image-dominant, text along bottom.**

```
┌────────────────────────────────────────────────────────────┐
│                                                            │
│                                                            │
│           [Room background image — generated]              │
│                                                            │
│                                                            │
├────────┬─────────────────────────────────┬─────────────────┤
│        │ Narration text scrolls here...  │ INVENTORY       │
│ [Por-  │ > player input                  │ - brass key     │
│ trait] │ ┃ NPC: dialog with color bar    │ - folded note   │
│  120×  │                                 │ - oil lamp      │
│  150   │ > player input                  │ (click=details) │
│        │ Narration continues...          │                 │
└────────┴─────────────────────────────────┴─────────────────┘
[ input box here ──────────────────────────────────────────  ]
```

**Portrait slot:** shows whichever character is currently speaking. When
the DM is narrating (no specific NPC voice), it shows the player's portrait
(or a generic icon — TBD). When an NPC speaks, their portrait swaps in.

**Text styling:**
- Narration: default parchment color, regular weight
- Player commands (echoed): amber, prefixed with `>`
- NPC speech: character's assigned color, name prefix, optional left bar
- Player's inner observations: italic, dimmer

Each NPC gets a consistent color picked at portrait-generation time.

**Image generation:** room images are 16:10 painterly scenes. No spatial
constraints — purely atmospheric. Painted by the image model from a text
description that the DM provides at definition time. Portraits are
character close-ups in matching visual style.

## Save / load

Save = full `world_state` JSON + the spilled transcript log + cached
images & portraits. Already autosaving after every response per
[feedback_commit_immediately.md](#) — keep that pattern.

Load: read JSON, restore UI from current location + present NPCs +
inventory. Replay only happens if the user explicitly asks; default is
"resume mid-scene."

## Components / new file layout

Proposed:

```
agents/
  dm.py                 # the merged author + DM agent
  npc.py                # per-NPC agent (instantiated per active NPC)
  imagery.py            # rooms + portraits only (no sprites)

world/
  state.py              # WorldState dataclass, load/save, diff/apply
  inventory.py          # inventory model with provenance
  locations.py          # locations dict, freeform definitions

modes/
  setup_mode.py         # was holodeck_mode; setup conversation
  play_mode.py          # play loop — replaces sprite-based one entirely

rendering/
  layout.py             # the image / text / portrait / inventory layout
  text_styles.py        # narration/speech/input styling
  inventory_panel.py    # inventory UI

input/
  text_input.py         # the chat input box (multi-line, history, etc.)
```

Out:

```
world/priority_map.py        # DELETE
world/scene3d.py             # DELETE
agents/layout.py             # DELETE
agents/scenery.py            # SUBSUMED into imagery.py (background-only paths)
agents/character_imagery.py  # SUBSUMED into imagery.py (portrait-only paths)
modes/play_mode.py           # REWRITTEN
scripts/test_scene3d.py      # DELETE
scripts/test_layout.py       # DELETE
scripts/test_paint.py        # DELETE
```

## Open questions / things to figure out as we go

- **Conversation "exit" gesture.** When does the player stop talking to an
  NPC and return to narration mode? Probably implicit — they say something
  not-directed-at-the-NPC and the DM resumes narrating. Worth testing.
- **Length & pacing of narration.** Too short feels thin, too long is
  overwhelming. DM prompt likely needs explicit guidance ("3-5 sentences
  unless the player asks for more detail").
- **Player physicality.** Do we ever describe what the player does in third
  person ("Vesper crosses the room") or always in second person ("You cross
  the room")? Probably second-person, classic.
- **Companion NPCs.** Can an NPC travel with the player long-term?
  Mechanically: yes, just `current_location_id` tracks the player. Worth
  flagging since it complicates conversation context.
- **Multiple players?** Not now. Single player throughout.
- **Modding / user content.** Out of scope.

## Implementation order (rough)

A reasonable build sequence — not committing to all of this yet:

1. **Strip out the old.** Delete spatial code, sprite generation, priority
   maps, etc. Keep what's reusable (Gemini plumbing, base agent, portrait
   path of `character_imagery`).
2. **WorldState skeleton.** Dataclass, JSON ser/deser, diff/apply.
3. **DM agent v0.** Intent parse → narration. No NPCs yet, no inventory,
   no images. Pure text loop to verify the conversational loop works.
4. **Inventory + provenance.** Items can be acquired and inspected.
5. **Room images.** Wire up the existing background-painting path with the
   new "purely atmospheric, no priority map" prompt.
6. **NPC agents.** First with one NPC, then with eavesdropping.
7. **Portraits + speaker UI.** Portrait slot, NPC colors, text styling.
8. **Time and NPC ticks.** Narrative time + offscreen progression.
9. **Setup mode polish.** Make the setup conversation feel like its own
   thing.
10. **Inventory panel polish, transitions, etc.**

## TL;DR

We're rebuilding holodeck as a graphical text adventure. The DM is a
single LLM agent that owns the world state, parses free-text input,
narrates, and dispatches to per-NPC agents. NPCs are autonomous between
scenes; time only advances when the narrative warrants it. Rooms are
generated on first entry and only re-rendered when their visual state
changes meaningfully. Inventory is always visible with provenance
paragraphs. Setup is just the DM running in pre-play mode.

This plays to AI's strengths and stops trying to do pixel-precise gameplay
that AI image models can't deliver.
