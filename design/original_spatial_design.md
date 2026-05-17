# The Holodeck — AI-Driven Adventure Game Engine
## Comprehensive Design Document & Claude Code Build Prompt

---

## Project Overview

You are building a Python/pygame application called **The Holodeck** — an AI-driven graphical adventure game in the style of Sierra's SCI engine (late 1980s/early 1990s VGA era). The game is powered by Google's Gemini AI acting as a "Dungeon Master" that dynamically generates the world, characters, dialog, and imagery in real time. The game is not scripted — the story emerges from player choices and the AI DM's responses.

The application is **for personal use on Windows**, running locally.

This document is your complete specification. Build the project systematically, starting with the core infrastructure and building up to the full experience. Ask for clarification if anything is ambiguous, but otherwise use your judgment to fill in implementation details consistent with the design.

---

## Tech Stack

- **Python 3.11+** (do not use 3.9 features that were backported — write clean modern Python)
- **pygame 2.x** — rendering, input, game loop
- **google-genai** (the official Google Generative AI Python SDK) — for all Gemini API calls
- **Pillow (PIL)** — image processing, resizing, color quantization, chroma key
- **tkinter** — OS native file picker only (ships with Python, no installation needed)
- **python-dotenv** — loading API key from .env file
- No other major dependencies unless strictly necessary

### Installation

Create a `requirements.txt`:
```
pygame>=2.5.0
google-genai>=1.0.0
Pillow>=10.0.0
python-dotenv>=1.0.0
```

### API Key

Load from a `.env` file in the project root:
```
GEMINI_API_KEY=your_key_here
```

Never hardcode the API key. Add `.env` to `.gitignore`.

---

## Models

- **DM / World Brain**: `gemini-2.5-pro` (or latest available Gemini Pro thinking model)
- **NPC Dialog**: `gemini-2.5-flash` (fast, cheap, runs per conversational exchange)
- **Image Generation**: `gemini-2.5-flash-image` (Nano Banana — the image generation model)

Use the model string aliases so they auto-update. If a model string is unavailable, fall back gracefully and log the error.

---

## Visual Specifications

- **Internal resolution**: 320×200 pixels (authentic VGA)
- **Display resolution**: Scale up 3× or 4× using `pygame.transform.scale` with nearest-neighbor interpolation (no smoothing). Default to 3× (960×600). Make the scale factor configurable in config.
- **Color**: Design for 256-color VGA aesthetic. When processing AI-generated images, quantize to 256 colors using Pillow's `image.quantize(256)` then convert back to RGB for pygame. This gives authentic VGA palette banding.
- **Font**: Use a bitmap/pixel font. Include a fallback to pygame's default font if no custom font is found. The font should feel era-appropriate.
- **UI Chrome**: Keep UI minimal and period-appropriate. Text boxes, inventory panel, and holodeck console should feel like Sierra games.

---

## Application Modes

The application has exactly **two modes**, toggled with the `H` key (or `~` key as alternative):

### 1. PLAY MODE
The normal game state. The player sees the current room background, their character sprite walking around, and can type commands in a text input bar at the bottom of the screen.

### 2. HOLODECK MODE
Activated any time — including at first launch when the world is empty. The screen gets a visual treatment (desaturate the current scene, overlay a yellow grid pattern reminiscent of TNG holodeck), and a chat console slides up. The player types natural language to the DM to build or adjust the world. Press `H` again or type `resume` to return to play mode.

**Critical design point**: There is no separate "editor application." Creation and editing are both Holodeck Mode. The first time the game launches with no game bible, it opens directly in Holodeck Mode.

---

## File & Folder Structure

```
holodeck/
├── main.py                    # Entry point
├── config.py                  # Configuration constants
├── .env                       # API key (gitignored)
├── requirements.txt
├── README.md
│
├── modes/
│   ├── __init__.py
│   ├── play_mode.py            # Play mode rendering and input
│   └── holodeck_mode.py        # Holodeck console UI
│
├── dm/
│   ├── __init__.py
│   ├── dungeon_master.py       # Gemini Pro DM integration
│   ├── character_ai.py         # Per-NPC dialog (Gemini Flash)
│   └── image_gen.py            # Nano Banana image generation
│
├── world/
│   ├── __init__.py
│   ├── state.py                # World state management (in-memory + disk)
│   ├── room.py                 # Room definition and walkability map
│   ├── character.py            # NPC and player character definitions
│   ├── inventory.py            # Object/item tracking
│   └── bible.py                # Game bible save/load (JSON)
│
├── rendering/
│   ├── __init__.py
│   ├── renderer.py             # Main pygame rendering pipeline
│   ├── sprite.py               # Sprite animation system
│   ├── holodeck_overlay.py     # Grid overlay and console rendering
│   └── ui.py                   # Text input, inventory panel, dialog boxes
│
├── input/
│   ├── __init__.py
│   └── parser.py               # Free-form input → DM action dispatch
│
├── assets/
│   ├── fonts/                  # Pixel fonts (include a default)
│   ├── sounds/                 # Optional: UI sounds
│   └── cache/                  # Generated images stored here (gitignored)
│       ├── rooms/
│       ├── sprites/
│       └── portraits/
│
└── saves/
    ├── autosave.json
    └── (player save slots)
```

---

## The Game Bible

The game bible is the central data structure — a JSON file that contains everything needed to reconstruct the game state. It is created by the DM during holodeck sessions and evolved during gameplay.

### Schema

```json
{
  "meta": {
    "title": "string",
    "version": "1.0",
    "created": "ISO timestamp",
    "last_played": "ISO timestamp",
    "tone": "string — e.g. dark fantasy mystery",
    "visual_style": "string — image generation style prefix applied to all prompts",
    "style_reference_images": ["path/to/image1.jpg"]
  },
  "dm_instructions": {
    "plot_seeds": ["string — things that should eventually happen"],
    "hard_constraints": ["string — things that must never happen"],
    "pacing": "slow | medium | fast",
    "difficulty": "easy | medium | hard",
    "world_rules": ["string — laws of this world, e.g. 'magic is illegal'"]
  },
  "world": {
    "factions": [],
    "lore": []
  },
  "player": {
    "name": "string",
    "description": "string",
    "sprite_sheet_path": "string",
    "starting_room": "room_id",
    "current_room": "room_id",
    "position": {"x": 160, "y": 150},
    "facing": "south",
    "inventory": ["object_id"],
    "known_facts": ["string"],
    "reputation": {}
  },
  "rooms": {
    "room_id": {
      "id": "string",
      "name": "string",
      "description": "string",
      "background_path": "string — path to cached generated image or null",
      "background_prompt": "string — the prompt used to generate it",
      "exits": {
        "north": "room_id or null",
        "south": "room_id or null",
        "east": "room_id or null",
        "west": "room_id or null"
      },
      "exit_zones": {
        "north": {"x": 0, "y": 0, "width": 320, "height": 20},
        "south": {"x": 0, "y": 180, "width": 320, "height": 20}
      },
      "walkable_zone": {
        "type": "lower_percentage",
        "value": 60
      },
      "obstacles": [
        {"id": "string", "rect": {"x": 0, "y": 0, "width": 0, "height": 0}}
      ],
      "characters_present": ["character_id"],
      "objects_present": ["object_id"],
      "visited": false,
      "ambient_description": "string — for DM context"
    }
  },
  "characters": {
    "character_id": {
      "id": "string",
      "name": "string",
      "description": "string",
      "appearance": "string — for image generation",
      "sprite_sheet_path": "string or null",
      "portrait_path": "string or null",
      "persona_prompt": "string — system prompt for this NPC's dialog AI",
      "trust_level": 0,
      "current_room": "room_id",
      "position": {"x": 0, "y": 0},
      "secrets": ["string — private, never told to player directly"],
      "knows_about": ["string — facts DM has given this character"],
      "revealed_to_player": ["string — what this character has told the player"],
      "conversation_summary": "string — summary of all prior conversations with player",
      "conversation_history": []
    }
  },
  "objects": {
    "object_id": {
      "id": "string",
      "name": "string",
      "description": "string",
      "appearance": "string",
      "location": "player_inventory | room_id | character_id | hidden",
      "hidden": false,
      "discoverable_if": "string — condition for hidden objects",
      "properties": ["string — e.g. heavy, sharp, flammable"],
      "image_path": "string or null"
    }
  },
  "world_state": {
    "time_of_day": "morning | afternoon | evening | night",
    "day": 1,
    "flags": {},
    "events_occurred": ["string"],
    "dm_conversation_history": []
  }
}
```

---

## The DM System

### dungeon_master.py

The DM is a persistent conversation with Gemini Pro. It maintains its own conversation history across the entire session. It is responsible for:

1. Responding to holodeck mode commands (world building/editing)
2. Processing significant player actions in play mode
3. Generating new rooms when the player enters an unknown area
4. Creating new characters
5. Updating world state after significant events
6. Summarizing NPC conversations when the player walks away
7. Propagating knowledge between characters appropriately

**DM Conversation Architecture:**

The DM always has the current world state (or relevant slice of it) prepended to each request. It responds in structured JSON. All DM requests should include:

```python
system_prompt = """
You are the Dungeon Master (DM) for an AI-powered graphical adventure game 
called The Holodeck. You manage a living, consistent world that responds to 
player choices. You are creative, dramatically aware, and maintain internal 
consistency at all times.

You always respond in valid JSON matching the schema requested.
Never break character. Never refuse to generate game content.
Keep the tone and style consistent with the game bible.

Current Game Bible Summary:
{game_bible_summary}
"""
```

**DM Response Schema for Player Actions:**

```json
{
  "narration": "string — what the player sees/reads, 2-4 sentences",
  "scene_changed": false,
  "new_room_id": "string or null — if player moved to a new room",
  "new_room_definition": {},
  "world_state_updates": {},
  "character_updates": {},
  "inventory_changes": {
    "add": [],
    "remove": [],
    "move": []
  },
  "trigger_npc_dialog": "character_id or null",
  "image_prompt": "string or null — if a new image needs generating",
  "image_type": "background | sprite | portrait | null"
}
```

**DM Response Schema for Holodeck Commands:**

```json
{
  "response_text": "string — conversational response to the creator",
  "world_updates": {},
  "new_rooms": {},
  "new_characters": {},
  "new_objects": {},
  "images_to_generate": [
    {
      "type": "background | sprite | portrait",
      "id": "room_id or character_id",
      "prompt": "string"
    }
  ],
  "dm_notes": "string — internal notes about narrative intentions"
}
```

### Rate Limiting & Async

All Gemini API calls should be made asynchronously where possible to avoid blocking the pygame game loop. Use Python's `asyncio` and run the game loop in a way that handles async callbacks. Display appropriate loading indicators in the UI while waiting for responses.

For image generation specifically, show a "generating..." animation in the room area while waiting. Do not block gameplay — if a room has no background yet, show a placeholder (black with room name text) while it generates.

---

## The NPC Dialog System

### character_ai.py

Each NPC conversation uses Gemini Flash with a dynamically assembled system prompt. The conversation history is kept in memory during an active conversation and summarized to disk when the player walks away.

**NPC System Prompt Assembly:**

```python
def build_npc_system_prompt(character, world_state, game_bible):
    return f"""
You are {character['name']} in an adventure game. 

PERSONA:
{character['persona_prompt']}

WHAT YOU KNOW:
{chr(10).join(character['knows_about'])}

WHAT YOU'VE ALREADY TOLD THE PLAYER:
{chr(10).join(character['revealed_to_player'])}

CONVERSATION HISTORY SUMMARY:
{character['conversation_summary']}

SECRETS (never reveal these directly, but they may influence your behavior):
{chr(10).join(character['secrets'])}

CURRENT TRUST LEVEL WITH PLAYER: {character['trust_level']} (scale 0-10)

Rules:
- Stay completely in character at all times
- Never mention that you are an AI
- Only know what your character would realistically know
- Your secrets may make you evasive, nervous, or defensive — show this subtly
- If trust is low (0-3), be guarded. If high (7-10), be forthcoming.
- Keep responses to 2-4 sentences unless the player asks a complex question
- Respond only as {character['name']}, never as a narrator
"""
```

**Conversation Flow:**

1. Player approaches/addresses NPC → `trigger_npc_dialog` fires
2. Dialog mode activates — text input now sends to NPC AI, not DM
3. NPC dialog box appears (Sierra-style portrait box upper screen)
4. Each exchange: append to `conversation_history`, send full history to Gemini Flash
5. After each NPC response, check if anything significant was revealed → flag for DM review
6. Player walks away / types `goodbye` / presses Escape → end dialog mode
7. Call DM to generate conversation summary → write to `conversation_summary`, clear `conversation_history`
8. DM decides if any world state updates needed based on what was revealed

---

## Image Generation System

### image_gen.py

All image generation uses Nano Banana (`gemini-2.5-flash-image`).

**Style Consistency:**

Every image prompt is prefixed with the game's visual style string from the game bible. Example:
```
"{visual_style}, {specific_prompt}"

where visual_style might be:
"painterly VGA adventure game style, 320x200 resolution aesthetic, 
256 color palette, Sierra On-Line SCI engine look, slightly desaturated, 
detailed backgrounds, 1990s point-and-click adventure game art"
```

**Image Processing Pipeline:**

All generated images go through this pipeline before being stored and displayed:

```python
from PIL import Image

def process_background(raw_image_bytes):
    img = Image.open(io.BytesIO(raw_image_bytes))
    img = img.resize((320, 200), Image.NEAREST)          # Resize to game resolution
    img = img.quantize(256)                               # Quantize to 256 colors
    img = img.convert('RGB')                              # Back to RGB for pygame
    return img

def process_sprite_sheet(raw_image_bytes, chroma_key=(0, 255, 0)):
    img = Image.open(io.BytesIO(raw_image_bytes))
    img = img.convert('RGBA')
    # Replace chroma key color with transparency
    data = img.getdata()
    new_data = []
    for pixel in data:
        if (abs(pixel[0] - chroma_key[0]) < 30 and 
            abs(pixel[1] - chroma_key[1]) < 30 and 
            abs(pixel[2] - chroma_key[2]) < 30):
            new_data.append((0, 0, 0, 0))
        else:
            new_data.append(pixel)
    img.putdata(new_data)
    return img
```

**Image Caching:**

Never regenerate an image that already exists in the cache. Cache is keyed by a hash of the prompt + any relevant state. Store as PNG files in `assets/cache/`.

```python
import hashlib

def get_cache_path(prompt, image_type):
    key = hashlib.md5(prompt.encode()).hexdigest()[:12]
    return f"assets/cache/{image_type}/{key}.png"
```

**Sprite Sheet Generation:**

For characters, request all directional animation frames in one API call:

```
"Sprite sheet on pure green background (#00FF00). 
[Character description]. 
Row 1: 4 frames walking south (toward viewer).
Row 2: 4 frames walking north (away from viewer). 
Row 3: 4 frames walking west (left).
Row 4: 1 idle/standing frame facing south.
Each frame 40×60 pixels. Consistent character design. 
Flat colors, pixel art style, VGA game aesthetic."
```

Slice the sprite sheet into individual frames programmatically after generation.

---

## Sprite Animation System

### rendering/sprite.py

```python
class AnimatedSprite:
    FACING_SOUTH = 0
    FACING_NORTH = 1
    FACING_WEST = 2
    FACING_EAST = 3  # Mirror of west
    
    def __init__(self, sprite_sheet_path, frame_width=40, frame_height=60):
        self.sheet = pygame.image.load(sprite_sheet_path).convert_alpha()
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.frames = self._slice_frames()
        self.current_loop = self.FACING_SOUTH
        self.current_frame = 0
        self.frame_timer = 0
        self.frame_delay = 150  # ms per frame
        self.x = 160
        self.y = 150
        self.speed = 1  # pixels per frame at 320x200
        self.moving = False
    
    def _slice_frames(self):
        frames = {
            self.FACING_SOUTH: [],
            self.FACING_NORTH: [],
            self.FACING_WEST: [],
            self.FACING_EAST: [],
        }
        for row, direction in enumerate([self.FACING_SOUTH, self.FACING_NORTH, 
                                          self.FACING_WEST]):
            cols = 4 if row < 3 else 1
            for col in range(cols):
                frame = self.sheet.subsurface(
                    col * self.frame_width,
                    row * self.frame_height,
                    self.frame_width,
                    self.frame_height
                )
                frames[direction].append(frame)
        # East is mirror of west
        frames[self.FACING_EAST] = [
            pygame.transform.flip(f, True, False) 
            for f in frames[self.FACING_WEST]
        ]
        return frames
    
    def update(self, dt):
        if self.moving:
            self.frame_timer += dt
            if self.frame_timer >= self.frame_delay:
                self.frame_timer = 0
                self.current_frame = (self.current_frame + 1) % len(
                    self.frames[self.current_loop]
                )
    
    def get_current_frame(self):
        return self.frames[self.current_loop][self.current_frame]
    
    def get_priority(self, screen_height=200):
        """Lower on screen = higher priority = drawn on top"""
        return int((self.y / screen_height) * 15)
```

---

## Room & Walkability System

### world/room.py

```python
class Room:
    def __init__(self, definition):
        self.id = definition['id']
        self.name = definition['name']
        self.exits = definition['exits']
        self.exit_zones = definition.get('exit_zones', {})
        self.obstacles = definition.get('obstacles', [])
        self.walkable_zone = definition.get('walkable_zone', {
            'type': 'lower_percentage', 'value': 65
        })
        self._walkable_mask = None
        self._build_walkable_mask()
    
    def _build_walkable_mask(self):
        """
        Build walkability from room definition — NOT from image analysis.
        The image prompt is generated to MATCH this definition.
        """
        surface = pygame.Surface((320, 200))
        surface.fill((0, 0, 0))  # Start: nothing walkable
        
        if self.walkable_zone['type'] == 'lower_percentage':
            pct = self.walkable_zone['value'] / 100
            top = int(200 * (1 - pct))
            pygame.draw.rect(surface, (255, 255, 255), (0, top, 320, 200 - top))
        
        # Subtract obstacles
        for obs in self.obstacles:
            r = obs['rect']
            pygame.draw.rect(surface, (0, 0, 0), 
                           (r['x'], r['y'], r['width'], r['height']))
        
        # Build mask from white areas
        self._walkable_mask = pygame.mask.from_surface(surface)
    
    def can_walk(self, x, y):
        if 0 <= x < 320 and 0 <= y < 200:
            return bool(self._walkable_mask.get_at((int(x), int(y))))
        return False
    
    def check_exit(self, x, y):
        for direction, zone in self.exit_zones.items():
            r = zone
            if (r['x'] <= x <= r['x'] + r['width'] and 
                r['y'] <= y <= r['y'] + r['height']):
                return direction, self.exits.get(direction)
        return None, None
    
    def get_image_prompt_geometry(self):
        """
        Returns compositional instructions for the image generator
        that match this room's walkability definition.
        """
        pct = self.walkable_zone.get('value', 65)
        prompt_parts = [
            f"Clear open floor in the lower {pct}% of the image.",
            "Walls, ceiling, and furniture in the upper portion.",
        ]
        for direction, zone in self.exit_zones.items():
            if direction == 'west':
                prompt_parts.append("Exit door or opening on the left side of the image.")
            elif direction == 'east':
                prompt_parts.append("Exit door or opening on the right side of the image.")
            elif direction == 'north':
                prompt_parts.append("Exit, door, or passage at the upper portion of the image.")
            elif direction == 'south':
                prompt_parts.append("Exit or opening at the bottom of the image.")
        return ' '.join(prompt_parts)
```

---

## Holodeck Mode UI

### modes/holodeck_mode.py

The holodeck overlay renders on top of the current play mode state. When activated:

1. The current room background desaturates (convert to grayscale, blend back slightly)
2. A yellow dot-grid pattern overlays the screen (think TNG holodeck — black with yellow grid)
3. A console panel appears in the lower 40% of the scaled display
4. The console has a scrolling conversation history and a text input at the bottom
5. An image preview area shows newly generated assets on the right side of the console

**Visual Treatment:**

```python
def apply_holodeck_overlay(surface):
    # Desaturate current frame
    gray = pygame.Surface(surface.get_size())
    gray.fill((20, 20, 30))  # Very dark blue-black
    gray.set_alpha(180)
    surface.blit(gray, (0, 0))
    
    # Draw yellow grid dots
    for x in range(0, surface.get_width(), 20):
        for y in range(0, surface.get_height(), 20):
            pygame.draw.circle(surface, (200, 180, 50), (x, y), 1)
```

**File Upload in Holodeck Mode:**

Include a "📎 Upload Reference Art" button in the holodeck console. Clicking it calls:

```python
import tkinter as tk
from tkinter import filedialog

def open_image_picker():
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes('-topmost', 1)  # Ensure dialog appears on top
    path = filedialog.askopenfilename(
        title="Select Reference Artwork",
        filetypes=[
            ("Image files", "*.png *.jpg *.jpeg *.webp *.bmp"),
            ("All files", "*.*")
        ]
    )
    root.destroy()
    return path if path else None
```

When an image is selected, encode it as base64 and include it in the next DM message so Gemini can actually see and analyze it.

**Watched Inspiration Folder:**

Also watch `assets/inspiration/` folder. Any images dropped there are automatically picked up and mentioned to the DM: "I notice you've added reference images. I'll incorporate their visual style into the world."

---

## Input System

### input/parser.py

In play mode, all text input goes through a simple dispatcher:

```python
class InputParser:
    def __init__(self, world_state, dm, character_ai):
        self.world_state = world_state
        self.dm = dm
        self.character_ai = character_ai
        self.active_npc = None  # currently talking to
    
    def process(self, text):
        text = text.strip()
        if not text:
            return
        
        # System commands first
        if text.lower() in ['quit', 'exit']:
            return {'action': 'quit'}
        
        # If in NPC dialog mode, send to character AI
        if self.active_npc:
            if text.lower() in ['goodbye', 'bye', 'leave', 'exit']:
                self.end_npc_dialog()
                return {'action': 'end_dialog'}
            return {'action': 'npc_dialog', 'npc_id': self.active_npc, 'text': text}
        
        # Otherwise send to DM for interpretation
        return {'action': 'player_command', 'text': text}
```

In holodeck mode, all text goes directly to the DM conversation.

**Movement**: Also handle keyboard arrow keys / WASD for character movement in play mode. The player can type `go north` OR press the arrow key — both work. When the player walks to an exit zone, room transition triggers automatically.

---

## Save System

### world/bible.py

```python
import json
import os
from datetime import datetime

SAVE_DIR = "saves/"

def save_game(world_state, slot='autosave'):
    os.makedirs(SAVE_DIR, exist_ok=True)
    world_state['meta']['last_saved'] = datetime.now().isoformat()
    filename = f"{SAVE_DIR}{slot}.json"
    # Write to temp file first, then rename (atomic write — prevents corruption)
    temp = filename + '.tmp'
    with open(temp, 'w', encoding='utf-8') as f:
        json.dump(world_state, f, indent=2, ensure_ascii=False)
    os.replace(temp, filename)

def load_game(slot='autosave'):
    filename = f"{SAVE_DIR}{slot}.json"
    if not os.path.exists(filename):
        return None
    with open(filename, 'r', encoding='utf-8') as f:
        return json.load(f)

def get_save_slots():
    if not os.path.exists(SAVE_DIR):
        return []
    return [f.replace('.json', '') for f in os.listdir(SAVE_DIR) 
            if f.endswith('.json') and not f.endswith('.tmp')]

def new_game():
    """Return an empty game bible — world starts blank, holodeck mode builds it"""
    return {
        "meta": {
            "title": "Untitled Adventure",
            "version": "1.0",
            "created": datetime.now().isoformat(),
            "last_played": None,
            "tone": "",
            "visual_style": "painterly VGA adventure game style, 320x200 aesthetic, 256 color palette, Sierra On-Line SCI engine look",
            "style_reference_images": []
        },
        "dm_instructions": {
            "plot_seeds": [],
            "hard_constraints": [],
            "pacing": "medium",
            "difficulty": "medium",
            "world_rules": []
        },
        "world": {"factions": [], "lore": []},
        "player": {
            "name": None,
            "description": None,
            "sprite_sheet_path": None,
            "starting_room": None,
            "current_room": None,
            "position": {"x": 160, "y": 150},
            "facing": "south",
            "inventory": [],
            "known_facts": [],
            "reputation": {}
        },
        "rooms": {},
        "characters": {},
        "objects": {},
        "world_state": {
            "time_of_day": "morning",
            "day": 1,
            "flags": {},
            "events_occurred": [],
            "dm_conversation_history": []
        }
    }
```

**Keyboard Shortcuts:**
- `F5` → Save (prompt for slot name or use last slot)
- `F7` → Load (show available slots)
- `F9` → Restart (confirm dialog, then reload original game bible from slot 0)
- Auto-save on every room transition and significant world event

---

## Main Game Loop

### main.py

```python
import pygame
import asyncio
from modes.play_mode import PlayMode
from modes.holodeck_mode import HolodeckMode
from world.bible import load_game, new_game
from dm.dungeon_master import DungeonMaster
from config import DISPLAY_SCALE, INTERNAL_WIDTH, INTERNAL_HEIGHT

def main():
    pygame.init()
    
    # Internal rendering surface (320x200)
    internal_surface = pygame.Surface((INTERNAL_WIDTH, INTERNAL_HEIGHT))
    
    # Display window (scaled up)
    display_width = INTERNAL_WIDTH * DISPLAY_SCALE
    display_height = INTERNAL_HEIGHT * DISPLAY_SCALE
    screen = pygame.display.set_mode((display_width, display_height))
    pygame.display.set_caption("The Holodeck")
    
    clock = pygame.time.Clock()
    
    # Load or create game bible
    world_state = load_game('autosave') or new_game()
    
    # Initialize systems
    dm = DungeonMaster(world_state)
    
    # Start in holodeck mode if world is not set up yet
    has_world = bool(world_state['player']['current_room'])
    current_mode = 'holodeck' if not has_world else 'play'
    
    play_mode = PlayMode(internal_surface, world_state, dm)
    holodeck_mode = HolodeckMode(internal_surface, world_state, dm)
    
    running = True
    while running:
        dt = clock.tick(60)  # Target 60fps
        
        events = pygame.event.get()
        for event in events:
            if event.type == pygame.QUIT:
                running = False
            if event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_h, pygame.K_BACKQUOTE):
                    current_mode = 'holodeck' if current_mode == 'play' else 'play'
                if event.key == pygame.K_F5:
                    # save
                    pass
                if event.key == pygame.K_F7:
                    # load
                    pass
                if event.key == pygame.K_F9:
                    # restart confirm
                    pass
        
        # Update and render current mode
        internal_surface.fill((0, 0, 0))
        
        if current_mode == 'play':
            play_mode.update(dt, events)
            play_mode.render()
        else:
            # Render play mode behind holodeck overlay
            play_mode.render()
            holodeck_mode.update(dt, events)
            holodeck_mode.render()
        
        # Scale up to display
        pygame.transform.scale(internal_surface, (display_width, display_height), screen)
        pygame.display.flip()
    
    pygame.quit()

if __name__ == '__main__':
    main()
```

### config.py

```python
INTERNAL_WIDTH = 320
INTERNAL_HEIGHT = 200
DISPLAY_SCALE = 3  # 960x600 display

GEMINI_DM_MODEL = "gemini-2.5-pro"
GEMINI_NPC_MODEL = "gemini-2.5-flash"
GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"

FRAME_DELAY_MS = 150       # Sprite animation speed
PLAYER_SPEED = 1           # Pixels per frame at internal resolution
AUTOSAVE_ON_ROOM_CHANGE = True

CHROMA_KEY = (0, 255, 0)   # Pure green for sprite transparency
SPRITE_FRAME_WIDTH = 40
SPRITE_FRAME_HEIGHT = 60

HOLODECK_GRID_COLOR = (200, 180, 50)
HOLODECK_GRID_SPACING = 20
```

---

## Build Order

Build in this sequence. Each step should be fully working before moving to the next:

### Phase 1: Skeleton
1. Project structure and all empty files with stubs
2. `config.py` and `.env` loading
3. `main.py` game loop — just a black window that runs at 60fps
4. Mode toggle with `H` key — prints to console which mode is active

### Phase 2: Rendering Foundation  
5. Internal 320×200 surface scaling to display
6. Basic play mode — renders a solid color background with room name text
7. Basic holodeck overlay — grid pattern + console panel (no AI yet)
8. Text input box in holodeck mode (keyboard input, display typed text)

### Phase 3: World State
9. Game bible JSON schema — `new_game()`, `save_game()`, `load_game()`
10. F5/F7/F9 save/load with simple slot selection UI
11. Autosave on room change

### Phase 4: DM Integration (Text Only)
12. `DungeonMaster` class with Gemini Pro connection
13. Holodeck mode sends text to DM, displays response
14. DM can create a basic room definition (text only, no images yet)
15. World state updates from DM responses

### Phase 5: Image Generation
16. `image_gen.py` with Nano Banana integration
17. Background image generation and caching
18. Image processing pipeline (resize → quantize → pygame surface)
19. Display generated background in play mode
20. Placeholder display while image is generating

### Phase 6: Sprite System
21. `AnimatedSprite` class with frame slicing
22. Player sprite generation via Nano Banana (sprite sheet)
23. Chroma key transparency processing
24. Player renders on screen, priority-sorted by Y position

### Phase 7: Movement & Rooms
25. Keyboard movement (arrow keys / WASD)
26. `Room` walkability mask system
27. Collision detection against walkability mask
28. Exit zone detection → room transition
29. Room transition: load/generate new room

### Phase 8: NPC Dialog
30. `character_ai.py` with Gemini Flash integration
31. NPC dialog mode (text input → NPC AI)
32. Sierra-style dialog box with character portrait
33. Conversation summary on dialog end
34. DM world state update after significant conversations

### Phase 9: Inventory
35. Inventory display panel
36. Object interaction through free-form parser
37. DM interprets object use, updates world state

### Phase 10: Polish
38. File picker for reference art upload (tkinter)
39. Inspiration folder watcher
40. Reference image encoding and DM visual analysis
41. Holodeck overlay animation (smooth slide-in)
42. Loading animations while AI generates
43. Sound effects (optional)

---

## Important Implementation Notes

**Never block the game loop.** All AI calls must be non-blocking. Use threading or asyncio. The pygame loop must always be able to render at 60fps even while waiting for Gemini.

**JSON parsing robustness.** Gemini will occasionally return slightly malformed JSON or wrap it in markdown code fences. Always strip markdown fences before parsing, and wrap all JSON parsing in try/except with fallback behavior.

**Image generation is slow.** Always show the player something — a placeholder, a loading animation, or the previous room — while waiting. Never freeze.

**Conversation history size.** For NPC dialog, cap conversation history at 50 exchanges before summarizing to avoid token limit issues. The DM conversation history should be summarized periodically if it grows very long.

**Windows path handling.** Use `pathlib.Path` throughout for all file paths to ensure Windows compatibility. Never hardcode forward slashes.

**Character sprite consistency.** When generating a character's sprite sheet, also generate a portrait (bust shot for dialog boxes) in the same API call if possible, using the same detailed character description to maximize consistency.

**The DM is always right.** If the DM's JSON response conflicts with current world state, trust the DM and update world state accordingly. The DM has the narrative authority.

---

## Sample DM Prompts to Implement

### New World Setup (Holodeck Mode, First Launch)
```
The game is starting for the first time with an empty world. The player 
wants to create a new adventure. Greet them warmly, explain that you are 
their AI Dungeon Master, and ask them the following questions one at a time:

1. What kind of world/setting do you want? (Fantasy, sci-fi, mystery, 
   historical, modern, etc.)
2. What is the general tone? (Dark and serious, lighthearted, horror, 
   comedic, etc.)
3. Who is the player character? (Name, background, why they're here)
4. Is there a specific story seed or scenario you have in mind, 
   or should I create one?

After gathering this information, create the initial world setup including 
the first room, the player character definition, and 2-3 initial characters 
they might meet. Return as structured JSON.
```

### Processing Player Action (Play Mode)
```
Current world state: {world_state_summary}
Current room: {room_name} — {room_description}
Characters present: {characters_present}
Player inventory: {inventory}
Player just typed: "{player_input}"

Interpret the player's action and respond. Consider what is realistically 
possible given the room and inventory. Be creative but consistent with 
established world rules. If the player tries something that has interesting 
consequences (expected or unexpected), make it happen. Keep narration to 
2-4 sentences. Return structured JSON response.
```

---

## Error Handling

- If the Gemini API is unreachable, show a graceful error in the holodeck console: "The holodeck is experiencing technical difficulties. Check your API key and connection."
- If image generation fails, use a placeholder (room name on colored background) and log the error. Do not crash.
- If JSON parsing fails, log the raw response and use a safe fallback (e.g., narration only, no state changes)
- If a save file is corrupted, offer to start fresh and archive the corrupted file

---

## What This Project Is NOT

- Not a web app. Everything runs locally in pygame.
- Not multiplayer.
- Not a general-purpose game engine — it is purpose-built for this AI-DM paradigm.
- Not dependent on any pre-built game content — all content is AI-generated at runtime.
