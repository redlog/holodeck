# Holodeck — Win States & Endings

> **Status:** Design discussion, parked for later. No code written.
> **Date:** 2026-06-28
> **Relates to:** the open/closed world mode (soft-rails closed worlds) added
> June 2026; the DM phase machine in `agents/dm.py`; plot threads in
> `world/bible.py`.

## The problem

Open-world games have no win state and no end. The player resolves every
thread — "I did all the things and I won" — and then: now what? Play just
continues with nothing left to resolve.

This is not only an unsatisfying-ending problem. **The absence of an end state
is a generative bug.** The DM phase machine is `INTERVIEW → CREATING → PLAY`,
and `PLAY` has no exit. When the story is actually over, the DM's only move to
keep the turn engine fed is to invent new conflict. In the Haunted Mansion
game this produced a literal fabricated threat — *"a faint, insidious magical
hum… persists"* — after every real thread was resolved, and over many turns it
bloated the plot-thread list from a clean handful to 28 entries (duplicates,
summary-as-id debris, contradictory statuses). Completion with no handler
becomes padding; padding becomes data sprawl.

So fixing "now what?" also removes a root cause of thread sprawl: teach the DM
to **recognize arc completion and respond on purpose** instead of padding.

## What we decided

Two world modes want opposite things at completion, and we lean into that:

- **Closed world → full ending.** A bounded story should converge. Seed a real
  `win_condition` at creation, detect when it's met, play a deliberate
  denouement, then end and offer a new game.
- **Open world → ask the player.** A sandbox shouldn't "win and stop."
  Completing the main arc is a *chapter break*, not a terminus. The DM gives
  the accomplishment its due (a genuine closure beat, not a fake new threat),
  then offers the player a choice: wind down here, or press on into a new arc.

These were explicitly chosen (open = "ask the player"; closed = "yes, full
ending").

## Core mechanism (shared by both modes)

**Recognition — hybrid signal.** Pure "all threads resolved" is too mechanical;
the DM is the narrative authority. So:
- Engine pre-check: when there are **zero active threads remaining (known *and*
  hidden)** and a `win_condition` exists, the per-turn digest *nudges* the DM
  to consider whether the arc is genuinely complete. (Hidden active threads
  mean undiscovered story still pending, so they must gate too — this prevents
  premature "you won" calls.)
- The DM makes the call by emitting a new `arc_complete: true` field in its
  `state_changes`. That is the authoritative trigger.
- The play prompt gains an explicit rule: **when the arc is complete, do NOT
  invent filler threats.** This is the line that directly kills the
  manufactured-lingering-threat behavior.

**Seed `win_condition` at creation.** The field already exists in the bible and
is read into the DM digest (`agents/dm.py`, the win-condition section of the
state digest), but the creation pass never populates it — so it's currently
dead. Creation should author it: the concrete world-state that means the core
story is done. In open worlds it is **per-arc and mutable** — when a new
chapter begins, the DM rewrites it.

## Mode branches

**Closed world (full ending):**
`arc_complete` → DM narrates a denouement → session enters a new `PHASE_ENDED`
→ frontend shows "The End" + New Game; input closes.

**Open world (ask the player):**
`arc_complete` → DM narrates a closure/victory beat → poses the fork *in-fiction*
("rest here, or press on?") → session enters `PHASE_AWAITING_DIRECTION`. The
player answers in free text (no new UI needed — it is already a chat):
- *Wind down* → epilogue → `PHASE_ENDED` (+ New Game).
- *Press on* → DM runs a lightweight new-arc pass (fresh `win_condition`, new
  hook/threads, optional time-skip) → back to `PHASE_PLAY`. Each subsequent arc
  completion re-triggers the same fork, so it is naturally repeatable.

## Files this would touch

| Area | Change |
|---|---|
| `agents/prompts.py` | Creation: seed `win_condition`. Play: completion rule + anti-padding + the open-world fork / new-arc instructions. |
| `agents/dm.py` | Parse `arc_complete`; digest nudge when zero active threads remain; phase transitions. |
| `server/session.py` | New phases; route completion → ending vs await-direction vs new-arc. |
| `server/view.py` | Expose ended/awaiting state (phase is already in the view). |
| `static/app.js` | React to `PHASE_ENDED` (show ending + New Game; lock input). |

## Suggested sequencing

Two passes to de-risk:
1. **Foundation** — `win_condition` seeding + `arc_complete` signal +
   anti-padding completion rule + phase plumbing. This pass *also* kills the
   thread-sprawl bug and is testable without UI.
2. **UX** — the open-world fork + new-arc generation + frontend ending screen.

## Open questions (not yet decided)

These were raised but left unresolved when we parked the work. Recommendations
noted for whenever we pick it back up:

1. **`win_condition` representation** — prose statement (DM-judged) vs explicit
   thread-id checklist vs prose-only. *Lean:* prose + the mechanical "zero
   active threads" backstop. Flexible, fits the LLM-narrative engine, degrades
   gracefully.
2. **Ending feel** — brief epilogue (a few low-stakes wind-down turns before
   "The End") vs hard curtain (denouement → end immediately). *Lean:* brief
   epilogue; the epilogue is cheap (normal play turns with a "no new plot"
   constraint) and avoids slamming the curtain after a climax. Applies to both
   modes' wind-down path.
3. **New-arc scope (open "press on")** — lightweight continuation (new
   win_condition + a few threads + maybe one antagonist, reusing the existing
   world, new places appearing as the player explores) vs full new region/cast
   up front. *Lean:* lightweight; escalate through stakes and hooks, not
   geography, to keep "press on" snappy rather than a creation-length stall.

Secondary details still to settle: what "The End" does to the save (remain
loadable/read-only? archive?); whether "New Game" can carry over the character;
exact `PHASE_*` naming.
