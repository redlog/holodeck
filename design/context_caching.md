# Context Caching for DM Calls

> **Status:** Design only — not yet implemented.
> **Date:** 2026-05-18
> **Depends on:** Gemini context caching API (available today, not wired up)

## Problem

Every DM play-turn call sends the full system prompt + world state context.
The system prompt (`PLAY_SYSTEM`) is ~2k tokens and never changes mid-session.
The world state changes incrementally — most of it (bible, tone, visual style,
interview summary, known locations the player isn't in) is stable across many
turns.

Even with history compaction (implemented), the per-call overhead is dominated
by this repeated prefix. With NPC dispatch adding 2-3 DM calls per talk turn,
the cost multiplies.

## Gemini Context Caching

Gemini's API supports explicit context caching:
https://ai.google.dev/gemini-api/docs/caching

The idea: you upload a "cached content" blob (system instruction + any prefix
content) and get back a cache handle with a TTL. Subsequent `generate_content`
calls reference the cache handle instead of resending the prefix. You pay
storage cost for the cache but drastically reduce per-call input tokens.

### API shape (Python SDK)

```python
from google.genai import types

# Create a cache
cache = client.caches.create(
    model="gemini-2.5-pro",
    config=types.CreateCachedContentConfig(
        system_instruction=PLAY_SYSTEM,
        contents=[
            # The stable world-state prefix could go here
            {"role": "user", "parts": [{"text": stable_context}]},
        ],
        ttl="600s",  # 10 minutes
    ),
)

# Use the cache on subsequent calls
response = client.models.generate_content(
    model="gemini-2.5-pro",
    contents=recent_history,  # only the delta
    config=types.GenerateContentConfig(
        cached_content=cache.name,
    ),
)
```

### What goes in the cache vs. the delta

**Cached (stable across many turns):**
- System prompt (`PLAY_SYSTEM`) — never changes
- Game identity (title, tone, visual style)
- DM bible (secrets, planned beats, scratchpad) — changes rarely
- Interview summary — never changes
- Known locations not currently occupied — changes rarely
- All NPC definitions (persona, voice, knows, hides) — changes rarely

**Per-call delta (changes every turn):**
- Current location details (summary, image prompt, discovered features, events)
- Present NPCs' mutable state (mood, intent, dialog summary)
- Player inventory (changes on take/drop)
- Recent compacted history
- Player input

### Cache invalidation

The cache needs to be rebuilt when the stable prefix changes. Triggers:
- New location created (added to known locations)
- Bible scratchpad appended to
- Secret revealed (changes revealed flag)
- New NPC created
- NPC knowledge updated (knows/hides/lies_about changed)

A simple approach: hash the stable prefix content. If the hash differs from
the last cached version, rebuild the cache. Otherwise reuse it. In practice,
the cache would survive many turns and only rebuild on significant world
changes.

### TTL management

Gemini caches have a configurable TTL (default varies by model). For a game
session where turns happen every 30-120 seconds, a 10-minute TTL is
reasonable — if the player goes AFK, the cache expires and the next turn
rebuilds it cheaply.

## Cost math (rough)

Assumptions for a mid-game turn:
- System prompt: ~2k tokens
- Stable world state: ~3k tokens (bible, locations, NPCs, interview summary)
- Per-turn delta: ~1k tokens (current location, present NPCs, inventory, input)
- Compacted history: ~1.5k tokens

**Without caching:** ~7.5k input tokens per call, 2-3 calls per talk turn = ~20k tokens/turn
**With caching:** ~2.5k input tokens per call + cache storage = ~7.5k tokens/turn

Roughly 60% input token reduction, plus faster response times since the
cached prefix doesn't need to be re-processed.

## Implementation sketch

1. Split `_build_play_context` into `_build_stable_context` and `_build_turn_context`.
2. Add `_cached_content` and `_cache_hash` to `DungeonMaster`.
3. At the start of each turn, compute the stable context and hash it. If the
   hash matches `_cache_hash`, reuse the cached handle. Otherwise, create a
   new cache and update the handle.
4. Modify `_call_text` (or add `_call_text_cached`) to accept a cache handle
   and send only the delta as contents.
5. Handle cache expiry gracefully — if the API returns a cache-miss error,
   rebuild and retry.

## Risks and unknowns

- **Minimum token threshold:** Gemini requires cached content to exceed a
  minimum token count (varies by model, currently 4096 for 2.5-pro). Early
  game states with sparse world data might not meet this threshold.
- **Cache creation latency:** Creating a new cache takes a round trip. If
  invalidation happens frequently (player rapidly creating locations), this
  could add latency rather than save it.
- **Cost model:** Cache storage is billed per hour. For a typical single-player
  session (1-3 hours), this is negligible. Worth verifying pricing.
- **NPC agent calls:** NPC agents use `gemini-2.5-flash` and are stateless
  single-call agents. Caching the NPC system prompt might not meet the minimum
  token threshold. Probably not worth caching unless we add more NPC context.
