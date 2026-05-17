import sys
from config import INTERNAL_WIDTH, INTERNAL_HEIGHT


def _log(msg):
    print(f"[ROOM] {msg}", file=sys.stderr, flush=True)


class Room:
    def __init__(self, definition):
        self.id = definition["id"]
        self.name = definition.get("name", self.id)
        self.exits = definition.get("exits", {})
        self.exit_zones = definition.get("exit_zones", {})
        self.obstacles = definition.get("obstacles", [])
        self.walkable_zone = definition.get("walkable_zone", {
            "type": "lower_percentage", "value": 65
        })
        self.entry_points = definition.get("entry_points", {})
        self._build_default_exit_zones()

    def _build_default_exit_zones(self):
        for direction, target in self.exits.items():
            if not target:
                continue
            if direction in self.exit_zones:
                continue
            if direction == "north":
                self.exit_zones[direction] = {"x": INTERNAL_WIDTH // 4, "y": 0, "width": INTERNAL_WIDTH // 2, "height": 30}
            elif direction == "south":
                self.exit_zones[direction] = {"x": INTERNAL_WIDTH // 4, "y": INTERNAL_HEIGHT - 30, "width": INTERNAL_WIDTH // 2, "height": 30}
            elif direction == "west":
                self.exit_zones[direction] = {"x": 0, "y": INTERNAL_HEIGHT // 4, "width": 30, "height": INTERNAL_HEIGHT // 2}
            elif direction == "east":
                self.exit_zones[direction] = {"x": INTERNAL_WIDTH - 30, "y": INTERNAL_HEIGHT // 4, "width": 30, "height": INTERNAL_HEIGHT // 2}

    def can_walk(self, x, y):
        if not (0 <= x < INTERNAL_WIDTH and 0 <= y < INTERNAL_HEIGHT):
            return False

        wz = self.walkable_zone
        if wz.get("type") == "lower_percentage":
            pct = wz.get("value", 65) / 100
            top = int(INTERNAL_HEIGHT * (1 - pct))
            if y < top:
                return False

        for obs in self.obstacles:
            r = obs.get("rect", {})
            try:
                ox, oy = int(r.get("x", 0)), int(r.get("y", 0))
                ow, oh = int(r.get("width", 0)), int(r.get("height", 0))
            except (TypeError, ValueError):
                continue
            if ox <= x <= ox + ow and oy <= y <= oy + oh:
                return False

        return True

    def check_exit(self, x, y):
        for direction, zone in self.exit_zones.items():
            target = self.exits.get(direction)
            if not target:
                continue
            try:
                zx = int(zone.get("x", 0))
                zy = int(zone.get("y", 0))
                zw = int(zone.get("width", 0))
                zh = int(zone.get("height", 0))
            except (TypeError, ValueError):
                continue
            if zx <= x <= zx + zw and zy <= y <= zy + zh:
                return direction, target
        return None, None

    def get_entry_position(self, from_direction):
        if from_direction in self.entry_points:
            ep = self.entry_points[from_direction]
            return ep.get("x", INTERNAL_WIDTH // 2), ep.get("y", INTERNAL_HEIGHT // 2 + 80)

        if from_direction == "north":
            return INTERNAL_WIDTH // 2, INTERNAL_HEIGHT - 60
        elif from_direction == "south":
            return INTERNAL_WIDTH // 2, self._walkable_top() + 40
        elif from_direction == "west":
            return INTERNAL_WIDTH - 80, INTERNAL_HEIGHT // 2 + 80
        elif from_direction == "east":
            return 80, INTERNAL_HEIGHT // 2 + 80
        return INTERNAL_WIDTH // 2, INTERNAL_HEIGHT // 2 + 80

    def _walkable_top(self):
        pct = self.walkable_zone.get("value", 65) / 100
        return int(INTERNAL_HEIGHT * (1 - pct))
