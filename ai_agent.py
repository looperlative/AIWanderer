# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""
AI state tracker for AIWanderer MUD Client (human-play mode).

Maintains the room graph, character stats, and a rolling text buffer
used to provide context to the LLM advisor.
"""

import re
import time
from collections import deque
from datetime import datetime, timezone

from mud_parser import MUDTextParser


# Lines to suppress from the recent-MUD-text buffer sent to the LLM.
# Stat prompts and command echoes are machine-readable noise, not narrative.
#
# We used to filter command echo, but not anymore: it's useful for the LLM
# to see what commands we're sending.
_MUD_NOISE_RE = re.compile(
    r'^(?:'
    r'\d+[Hh]\w*\s+\d+[Mm]\w*.*[>$]'   # stat prompt: "24H 100M 85V 0%T 0%O >"
    r'|\[AI'                              # agent status: "[AI] ..."
    r')',
    re.IGNORECASE
)

# Directions the agent will try (canonical names)
DIRECTION_ABBREVS = {
    'n': 'north', 'north': 'north',
    's': 'south', 'south': 'south',
    'e': 'east',  'east':  'east',
    'w': 'west',  'west':  'west',
    'u': 'up',    'up':    'up',
    'd': 'down',  'down':  'down',
}

# (dx, dy, dz) offsets for position tracking — used to detect collision zones
DIRECTION_DELTAS = {
    'north': (0,  1, 0), 'south': (0, -1, 0),
    'east':  (1,  0, 0), 'west':  (-1, 0, 0),
    'up':    (0,  0, 1), 'down':  (0,  0, -1),
}

REVERSE_DIRECTION = {
    'north': 'south', 'south': 'north',
    'east':  'west',  'west':  'east',
    'up':    'down',  'down':  'up',
}


def _link_dest(val):
    """Return (dest_hash, assumed) from a room_link value (str or dict)."""
    if isinstance(val, dict):
        return val.get('dest'), val.get('assumed', False)
    return val, False  # legacy plain-string → treat as confirmed


# ---------------------------------------------------------------------------
# ExplorationState — pure data, serialized to profile["ai_state"]
# ---------------------------------------------------------------------------

class ExplorationState:
    """Persisted state for the exploration agent (human-play mode)."""

    def __init__(self):
        self.frontier = deque()       # BFS queue: room hashes to visit
        self.came_from = {}           # hash -> {"from": hash, "direction": str}
        self.danger_rooms = {}        # hash -> {"reason": str, "time": str}
        self.dead_ends = {}           # hash -> set of blocked direction strings
        # Stats tracking (kept current for the LLM advisor)
        self.current_hp = None
        self.current_mp = None
        self.current_mv = None
        self.hp_at_room_entry = None  # HP when we entered the current room
        self.hunger_level = None      # None | 'hungry' | 'starving'
        self.thirst_level = None      # None | 'thirsty' | 'parched'
        self.gold = None
        self.inventory = []           # item names currently carried
        self.equipment = {}           # slot -> item name (currently worn/wielded)
        # Character sheet (from SCORE command)
        self.char_level = None
        self.char_class = None
        self.max_hp = None
        self.max_mp = None
        self.max_mv = None
        self.char_xp = None
        self.char_xp_next = None
        self.char_alignment = None
        # Who list cache — list of {"level": int, "class": str, "name": str}
        self.who_list = []

    def to_dict(self):
        return {
            "frontier": list(self.frontier),
            "came_from": self.came_from,
            "danger_rooms": self.danger_rooms,
            "dead_ends": {k: list(v) for k, v in self.dead_ends.items()},
            "current_hp": self.current_hp,
            "current_mp": self.current_mp,
            "current_mv": self.current_mv,
            "hunger_level": self.hunger_level,
            "thirst_level": self.thirst_level,
            "gold": self.gold,
            "inventory": self.inventory,
            "equipment": self.equipment,
            "char_level": self.char_level,
            "char_class": self.char_class,
            "max_hp": self.max_hp,
            "max_mp": self.max_mp,
            "max_mv": self.max_mv,
            "char_xp": self.char_xp,
            "char_xp_next": self.char_xp_next,
            "char_alignment": self.char_alignment,
        }

    @classmethod
    def from_dict(cls, data):
        s = cls()
        s.frontier = deque(data.get("frontier", []))
        s.came_from = data.get("came_from", {})
        s.danger_rooms = data.get("danger_rooms", {})
        s.dead_ends = {k: set(v) for k, v in data.get("dead_ends", {}).items()}
        s.current_hp = data.get("current_hp")
        s.current_mp = data.get("current_mp")
        s.current_mv = data.get("current_mv")
        s.hunger_level = data.get("hunger_level")
        s.thirst_level = data.get("thirst_level")
        s.gold = data.get("gold")
        s.inventory = data.get("inventory", [])
        s.equipment = data.get("equipment", {})
        s.char_level = data.get("char_level")
        s.char_class = data.get("char_class")
        s.max_hp = data.get("max_hp")
        s.max_mp = data.get("max_mp")
        s.max_mv = data.get("max_mv")
        s.char_xp = data.get("char_xp")
        s.char_xp_next = data.get("char_xp_next")
        s.char_alignment = data.get("char_alignment")
        return s



# ---------------------------------------------------------------------------
# PathFinder — stateless BFS/graph algorithms
# ---------------------------------------------------------------------------

class PathFinder:
    """Stateless pathfinding utilities over the room_links graph."""

    def bfs_path(self, room_links, start, goal):
        """Return list of direction strings from start to goal, or [] if unreachable."""
        if start == goal:
            return []
        visited = {start}
        queue = deque([(start, [])])
        while queue:
            current, path = queue.popleft()
            for direction, link_val in room_links.get(current, {}).items():
                dest, _ = _link_dest(link_val)
                if dest is None:
                    continue
                if dest == goal:
                    return path + [direction]
                if dest not in visited:
                    visited.add(dest)
                    queue.append((dest, path + [direction]))
        return []

    def find_nearest_frontier(self, start, room_links, frontier_set):
        """BFS from start to find the nearest room in frontier_set.
        Returns the destination hash or None."""
        if not frontier_set:
            return None
        visited = {start}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for link_val in room_links.get(current, {}).values():
                dest, _ = _link_dest(link_val)
                if dest is None:
                    continue
                if dest in frontier_set:
                    return dest
                if dest not in visited:
                    visited.add(dest)
                    queue.append(dest)
        return None

    def parse_exits_text(self, exits_text):
        """Parse exit directions from room exits string like '[N, S, E, W, U]'.
        Returns a set of canonical direction names."""
        if not exits_text:
            return set()
        # Match single-letter or full direction words
        tokens = re.findall(r'\b([NnSsEeWwUuDd]|[Nn]orth|[Ss]outh|[Ee]ast|[Ww]est|[Uu]p|[Dd]own)\b', exits_text)
        directions = set()
        for t in tokens:
            canonical = DIRECTION_ABBREVS.get(t.lower())
            if canonical:
                directions.add(canonical)
        return directions

    def reverse_direction(self, direction):
        """Return the opposite direction."""
        return REVERSE_DIRECTION.get(direction)


# ---------------------------------------------------------------------------
# ExplorationAgent — main AI controller
# ---------------------------------------------------------------------------

class ExplorationAgent:
    """
    State tracker and map builder for human-play mode.

    Runs on the main thread (callbacks from MUDClient).  Maintains the room
    graph, character stats, and a rolling text buffer for the LLM advisor.
    """

    def __init__(self, client):
        self.client = client          # MUDClient instance
        self.state = ExplorationState()
        self.pathfinder = PathFinder()
        self.parser = MUDTextParser()
        self.is_running = False
        # Rolling buffer of recent MUD text — fed to the LLM advisor as context
        self._recent_mud_lines = deque(maxlen=30)
        # Position tracking — gives each physical room a unique (x, y, z) identity
        # even when many rooms share the same content hash (chess boards, mazes, etc.)
        self._pos = (0, 0, 0)
        self._pos_visited = set()
        self._pos_links = {}
        self._collision_hashes = set()
        self._hash_positions = {}  # hash -> set of (x,y,z) positions
        self._pos_exits = {}       # (x,y,z) -> set of direction strings
        self._last_direction = None

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def start(self):
        """Load persisted state and mark agent as running."""
        self.load_state()
        self.is_running = True

    def stop(self):
        """Stop the agent and persist state."""
        self.is_running = False
        self.save_state()

    # ------------------------------------------------------------------
    # Callbacks from MUDClient (main thread)
    # ------------------------------------------------------------------

    def on_room_entered(self, room_hash, room_data):
        """Called by MUDClient after a room is successfully parsed."""
        if not self.is_running:
            return

        move_dir = self._last_direction   # capture before clearing
        self._last_direction = None

        # Update (x, y, z) position and position graph
        if move_dir:
            dx, dy, dz = DIRECTION_DELTAS.get(move_dir, (0, 0, 0))
            new_pos = (self._pos[0] + dx, self._pos[1] + dy, self._pos[2] + dz)
            self._pos_links.setdefault(self._pos, {})[move_dir] = new_pos
            rev = self.pathfinder.reverse_direction(move_dir)
            if rev:
                self._pos_links.setdefault(new_pos, {})[rev] = self._pos
            self._pos = new_pos

            # Set the reverse room_link back to where we came from.
            # This lets BFS route through this room even if it was never explicitly
            # explored outward.  Also clears any false dead-end that may have been
            # recorded for this direction in a previous (buggy) session.
            prev_hash = self.client.previous_room_hash
            if rev and prev_hash and room_hash and self.client.current_profile:
                profile = self.client.profiles.get(self.client.current_profile, {})
                room_links = profile.get('room_links', {})
                existing = room_links.get(room_hash, {}).get(rev)
                _, is_assumed = _link_dest(existing)
                if existing is None or is_assumed:
                    room_links.setdefault(room_hash, {})[rev] = {"dest": prev_hash, "assumed": True}
                self.state.dead_ends.get(room_hash, set()).discard(rev)

        self._pos_visited.add(self._pos)

        # Check if HP dropped since we were last in a room (danger detection)
        if (self.state.hp_at_room_entry is not None
                and self.state.current_hp is not None
                and self.state.current_hp < self.state.hp_at_room_entry):
            hp_loss = self.state.hp_at_room_entry - self.state.current_hp
            if room_hash and room_hash not in self.state.danger_rooms:
                self.state.danger_rooms[room_hash] = {
                    "reason": f"hp_loss:{hp_loss}",
                    "time": datetime.now(timezone.utc).isoformat(),
                }
                self.client.append_text(
                    f"[AI] Warning: lost {hp_loss} HP entering this room.\n", "system")

        # Record HP at entry for next comparison
        self.state.hp_at_room_entry = self.state.current_hp

        if room_hash and room_hash not in self.client.profiles.get(
                self.client.current_profile, {}).get('rooms', {}):
            self._enqueue_unvisited_neighbors(room_hash)

        # Remove from frontier if present
        if room_hash in self.state.frontier:
            # deque doesn't support fast remove; rebuild without this hash
            self.state.frontier = deque(
                h for h in self.state.frontier if h != room_hash
            )

        # Collision detection: a hash collision only exists when the SAME hash is
        # observed at two or more DISTINCT positions.  Normal revisits of the same
        # physical room update the same position entry and do NOT trigger this.
        if room_hash:
            pos_set = self._hash_positions.setdefault(room_hash, set())
            pos_set.add(self._pos)
            if len(pos_set) > 1 and room_hash not in self._collision_hashes:
                self._collision_hashes.add(room_hash)
                name = (room_data.get('name', room_hash[:8]) if room_data else room_hash[:8])
                self.client.append_text(
                    f"[AI] Non-unique room detected ('{name}') — switching to "
                    f"position-based navigation for this area.\n", "system")

        # Record which exits exist at the current position (for BFS phantom-route avoidance)
        if room_data:
            exits_text = room_data.get('exits', '')
            self._pos_exits[self._pos] = self.pathfinder.parse_exits_text(exits_text)

        self.save_state()

    def on_text_received(self, clean_text):
        """Called by MUDClient for every block of incoming text (main thread)."""
        if not self.is_running:
            return

        # Maintain rolling text buffer fed to the LLM advisor as context
        for line in clean_text.splitlines():
            line = line.strip()
            if line and not _MUD_NOISE_RE.match(line):
                self._recent_mud_lines.append(line)

        # Parse HP/MP/MV stats (used for danger-room detection on room entry)
        stats = self.parser.parse_prompt_stats(clean_text)
        if stats:
            self.state.current_hp = stats.get('hp', self.state.current_hp)
            self.state.current_mp = stats.get('mp', self.state.current_mp)
            self.state.current_mv = stats.get('mv', self.state.current_mv)

        # SCORE parsing — keeps char stats current for the LLM advisor
        score = self.parser.parse_score(clean_text)
        if score:
            if 'level' in score:     self.state.char_level    = score['level']
            if 'class_name' in score: self.state.char_class   = score['class_name']
            if 'max_hp' in score:    self.state.max_hp        = score['max_hp']
            if 'max_mp' in score:    self.state.max_mp        = score['max_mp']
            if 'max_mv' in score:    self.state.max_mv        = score['max_mv']
            if 'xp' in score:        self.state.char_xp       = score['xp']
            if 'xp_next' in score:   self.state.char_xp_next  = score['xp_next']
            if 'gold' in score:      self.state.gold          = score['gold']
            if 'alignment' in score: self.state.char_alignment = score['alignment']
            if 'hunger' in score:    self.state.hunger_level  = score['hunger']
            if 'thirst' in score:    self.state.thirst_level  = score['thirst']
            self.save_state()

        # WHO list parsing — helps advisor identify players vs. NPCs
        char_name = self.client.current_profile
        players, self_entry = self.parser.parse_who(clean_text, char_name)
        if players:
            self.state.who_list = players
            if self_entry:
                if self_entry.get('level') != self.state.char_level:
                    self.state.char_level = self_entry['level']
                if self_entry.get('class') != self.state.char_class:
                    self.state.char_class = self_entry['class']
            self.save_state()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enqueue_unvisited_neighbors(self, room_hash):
        """Add known unvisited neighbors to the frontier queue."""
        if not self.client.current_profile:
            return
        profile = self.client.profiles.get(self.client.current_profile, {})
        rooms = profile.get('rooms', {})
        room_links = profile.get('room_links', {})
        for direction, link_val in room_links.get(room_hash, {}).items():
            dest, _ = _link_dest(link_val)
            if dest is not None and dest not in rooms:
                if dest not in self.state.frontier:
                    self.state.frontier.append(dest)
                    self.state.came_from.setdefault(dest, {
                        "from": room_hash,
                        "direction": direction,
                    })

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _profile(self):
        if not self.client.current_profile:
            return None
        return self.client.profiles.get(self.client.current_profile)

    def save_state(self):
        profile = self._profile()
        if profile is None:
            return
        profile['ai_state'] = self.state.to_dict()
        self.client.save_profiles()

    def load_state(self):
        profile = self._profile()
        if profile is None:
            return
        data = profile.get('ai_state')
        if data:
            self.state = ExplorationState.from_dict(data)
        else:
            self.state = ExplorationState()
        # Apply previously calibrated MUD time so buff expiry is accurate immediately
        cal = profile.get('mud_time_calibration', {})
        mud_hour_secs = cal.get('mud_hour_secs')
        if mud_hour_secs:
            self.state.TICK_SECS = mud_hour_secs
            self.client.append_text(
                f"[AI] Loaded MUD time calibration: {mud_hour_secs:.1f}s/mud-hour "
                f"({cal.get('sample_count', '?')} samples)\n", "system")
