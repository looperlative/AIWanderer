# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""
AI Exploration Agent for AIWanderer MUD Client.

Implements an autonomous BFS-based exploration agent that navigates the MUD,
builds a map, detects dangers, and learns from outcomes.
"""

import re
import time
from collections import deque
from datetime import datetime, timezone

from mud_parser import MUDTextParser
from llm_advisor import LLMAdvisor


# ---------------------------------------------------------------------------
# Death and combat text patterns (lowercase for case-insensitive matching)
# ---------------------------------------------------------------------------

DEATH_PATTERNS = [
    "you are dead",
    "you have died",
    "you died",
    "you wake up",
    "you have been killed",
    "you fall unconscious",
    "you lose consciousness",
]

COMBAT_START_PATTERNS = [
    "attacks you",
    "hits you",
    "misses you",
    "you begin fighting",
    "you start fighting",
]

COMBAT_ACTIVE_PATTERNS = [
    "you hit",
    "you miss",
    "you dodge",
    "you parry",
    "attacks you",
    "hits you",
    "misses you",
    # Miss/dodge phrases that name the opponent but don't use the above forms
    "avoid the blow",
    "duck under",
    "punch at the air",
    "tries to hit you",
    "ducks under your",
    "missing the",
]

BLOCKED_PATTERNS = [
    "you can't go that way",
    "you cannot go that way",
    "alas, you cannot go",
    "there is no exit",
    "that direction",
    "no exit",
    "invalid direction",
    # Guard / NPC blocking
    "blocks your way",
    "bars your way",
    "bars the way",
    "stands in your way",
    "blocks the way",
    "guards the way",
    "won't let you pass",
    "will not let you pass",
    "prevents you from",
    "humiliates you",
    "turns you away",
    "stops you",
    "refuses to let you",
]

# Otto's known services — pre-populated so he is usable immediately on session
# start without waiting for a 'tell otto help' response.  Dynamic discovery via
# the help response will extend or correct this list at runtime.
OTTO_KNOWN_CAPABILITIES = ['summon', 'heal', 'sanctuary', 'bless', 'armor']

# Lines to suppress from the recent-MUD-text buffer sent to the LLM.
# Stat prompts and command echoes are machine-readable noise, not narrative.
_MUD_NOISE_RE = re.compile(
    r'^(?:'
    r'\d+[Hh]\w*\s+\d+[Mm]\w*.*[>$]'   # stat prompt: "24H 100M 85V 0%T 0%O >"
    r'|>\s*\S'                            # command echo: "> north"
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


# ---------------------------------------------------------------------------
# ExplorationState — pure data, serialized to profile["ai_state"]
# ---------------------------------------------------------------------------

class ExplorationState:
    """Persisted state for the exploration agent."""

    def __init__(self):
        self.visited = set()          # room hashes confirmed visited
        self.frontier = deque()       # BFS queue: room hashes to visit
        self.came_from = {}           # hash -> {"from": hash, "direction": str}
        self.danger_rooms = {}        # hash -> {"reason": str, "time": str}
        self.npc_danger = {}          # npc_name_lower -> {deaths, fastest_death_secs, wins, near_kills, last_room}
        self.dead_ends = {}           # hash -> set of blocked direction strings
        self.current_goal = "explore"
        # Stats tracking
        self.current_hp = None
        self.current_mp = None
        self.current_mv = None
        self.current_tank_pct = None   # tank HP% (0-100)
        self.current_opp_pct = None    # opponent HP% (0-100)
        self.hp_at_room_entry = None   # HP when we entered the current room
        self.total_xp_gained = 0
        self.total_deaths = 0
        # Survival state
        self.hunger_level = None       # None | 'hungry' | 'starving'
        self.thirst_level = None       # None | 'thirsty' | 'parched'
        self.has_light = True          # assume light until told otherwise
        self.dark_rooms = set()        # room hashes known to be dark
        self.dark_directions = {}      # {room_hash: set(directions)} that led to darkness
        # Discovered resource locations (learned during exploration)
        self.water_sources = {}        # {room_hash: drink_command} — all known water sources
        self.water_sources_failed = set()  # room hashes where drinking was tried and failed
        self.food_room = None          # room hash of known food shop
        self.food_rooms_failed = set() # room hashes where 'list' was rejected
        self.food_item = None          # e.g. "loaf of bread" (from shop list)
        self.food_price = None         # cost of food item in gold
        self.seeking_resource = None   # 'water' | 'food' | None
        self.gold = None               # current gold on hand
        self.inventory = []            # item names currently carried
        self.equipment = {}            # slot -> item name (currently worn/wielded)
        self.unequippable = set()      # item names we tried to equip and failed
        # Otto helper-player state — pre-seeded with known capabilities so Otto
        # is usable immediately; tell otto help extends/corrects this at runtime.
        self.otto_capabilities = list(OTTO_KNOWN_CAPABILITIES)
        self.otto_queried = False      # have we sent "tell otto help" yet?
        self.otto_room = None          # room hash where Otto was last seen
        # Buff tracking — cleared on session start (buffs don't survive logout)
        self.buff_expires = {}         # {buff_name: time.monotonic() when buff expires}
        self.BUFF_DURATION_SECS = 1800 # assumed duration when applied via tell (fallback)
        self.BUFF_STACK_COUNT = 2      # ask for protection buffs this many times each
        self.TICK_SECS = 60            # assumed seconds per MUD tick for duration conversion
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
            "visited": list(self.visited),
            "frontier": list(self.frontier),
            "came_from": self.came_from,
            "danger_rooms": self.danger_rooms,
            "npc_danger": self.npc_danger,
            "dead_ends": {k: list(v) for k, v in self.dead_ends.items()},
            "current_goal": self.current_goal,
            "total_xp_gained": self.total_xp_gained,
            "total_deaths": self.total_deaths,
            "current_hp": self.current_hp,
            "current_mp": self.current_mp,
            "current_mv": self.current_mv,
            "hunger_level": self.hunger_level,
            "thirst_level": self.thirst_level,
            "has_light": self.has_light,
            "dark_rooms": list(self.dark_rooms),
            "dark_directions": {k: list(v) for k, v in self.dark_directions.items()},
            "water_sources": self.water_sources,
            "water_sources_failed": list(self.water_sources_failed),
            "food_room": self.food_room,
            "food_rooms_failed": list(self.food_rooms_failed),
            "food_item": self.food_item,
            "food_price": self.food_price,
            "gold": self.gold,
            "inventory": self.inventory,
            "equipment": self.equipment,
            "unequippable": list(self.unequippable),
            "otto_capabilities": self.otto_capabilities,
            "otto_queried": self.otto_queried,
            "otto_room": self.otto_room,
            "buff_expires": {},  # always cleared on save — buffs don't survive logout
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
        s.visited = set(data.get("visited", []))
        s.frontier = deque(data.get("frontier", []))
        s.came_from = data.get("came_from", {})
        s.danger_rooms = data.get("danger_rooms", {})
        s.npc_danger = data.get("npc_danger", {})
        s.dead_ends = {k: set(v) for k, v in data.get("dead_ends", {}).items()}
        s.current_goal = data.get("current_goal", "explore")
        s.total_xp_gained = data.get("total_xp_gained", 0)
        s.total_deaths = data.get("total_deaths", 0)
        s.current_hp = data.get("current_hp")
        s.current_mp = data.get("current_mp")
        s.current_mv = data.get("current_mv")
        s.hunger_level = data.get("hunger_level")
        s.thirst_level = data.get("thirst_level")
        s.has_light = data.get("has_light", True)
        s.dark_rooms = set(data.get("dark_rooms", []))
        s.dark_directions = {k: set(v) for k, v in data.get("dark_directions", {}).items()}
        # Migrate old single water_room/water_command fields to water_sources dict
        water_sources = data.get("water_sources", {})
        if not water_sources:
            old_room = data.get("water_room")
            old_cmd  = data.get("water_command")
            if old_room and old_cmd:
                water_sources = {old_room: old_cmd}
        s.water_sources = water_sources
        s.water_sources_failed = set(data.get("water_sources_failed", []))
        s.food_room = data.get("food_room")
        s.food_rooms_failed = set(data.get("food_rooms_failed", []))
        s.food_item = data.get("food_item")
        s.food_price = data.get("food_price")
        s.seeking_resource = data.get("seeking_resource")
        s.gold = data.get("gold")
        s.inventory = data.get("inventory", [])
        s.equipment = data.get("equipment", {})
        s.unequippable = set(data.get("unequippable", []))
        s.otto_capabilities = data.get("otto_capabilities", [])
        s.otto_queried = data.get("otto_queried", False)
        s.otto_room = data.get("otto_room")
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
            for direction, dest in room_links.get(current, {}).items():
                if dest is None:
                    continue
                if dest == goal:
                    return path + [direction]
                if dest not in visited:
                    visited.add(dest)
                    queue.append((dest, path + [direction]))
        return []

    def get_unvisited_neighbors(self, room_hash, room_links, visited):
        """Return [(direction, dest_hash), ...] for mapped but unvisited neighbors."""
        result = []
        for direction, dest in room_links.get(room_hash, {}).items():
            if dest is not None and dest not in visited:
                result.append((direction, dest))
        return result

    def find_nearest_frontier(self, start, room_links, frontier_set):
        """BFS from start to find the nearest room in frontier_set.
        Returns the destination hash or None."""
        if not frontier_set:
            return None
        visited = {start}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for dest in room_links.get(current, {}).values():
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
    Autonomous BFS exploration agent.

    Scheduling: uses tkinter after() to stay on the main thread, avoiding
    race conditions with the non-threadsafe room state in MUDClient.
    """

    # How long to wait between sending commands (ms)
    COMMAND_DELAY_MS = 1500
    # How long to wait for a room response before treating move as blocked (ms)
    ROOM_TIMEOUT_MS = 4000

    def __init__(self, client):
        self.client = client          # MUDClient instance
        self.state = ExplorationState()
        self.pathfinder = PathFinder()
        self.parser = MUDTextParser()

        self.llm = LLMAdvisor(client)

        self.is_running = False
        self._timer_handle = None     # tkinter after() handle
        self._waiting_for_room = False
        self._wait_started = None     # time.monotonic() when wait began
        self._last_direction = None   # direction of the pending move
        self._waiting_for_llm = False # LLM call in flight
        self._llm_ignored_count = 0  # consecutive times LLM suggestion was ignored
        self._combat_active = False
        self._combat_seen_active = False # True once we've observed actual combat (not just sent kill)
        self._combat_win_counted = False # True once a win has been credited for the current combat
        self._combat_npc = None          # lowercase name of NPC we're fighting
        self._last_combat_npc = None     # persists after combat ends — used by death attribution
        self._combat_start_time = None   # time.monotonic() when combat started
        self._last_combat_start_time = None  # persists after combat ends — used by death attribution
        self._combat_start_hp = None     # our HP when combat started
        self._dead = False
        self._last_shop_list_request = 0.0   # monotonic time of last 'list' command
        self._otto_pending_service = None    # service to request from Otto after summon
        self._otto_in_current_room = False   # True when Otto was detected in this room's live text
        self._otto_summon_sent_at = 0.0      # monotonic time of last 'tell otto summon' — guards spam
        self._buff_sequence_pending = []     # protection buff tells still to send this cycle
        self._waiting_for_otto_help = False  # True after 'tell otto help' sent
        self._auto_pickup_pending = []       # get/drink commands queued from room entry
        self._last_score_request = time.monotonic()  # monotonic time of last 'score' command
        self._score_interval = 120.0         # request SCORE every 2 minutes
        self._last_time_request = 0.0        # monotonic time of last 'time' command
        self._time_interval = 300.0          # request MUD time every 5 minutes
        self._last_who_request = 0.0         # monotonic time of last 'who' command
        self._who_interval = 120.0           # refresh who list every 2 minutes
        self._last_autosave = 0.0            # monotonic time of last periodic save
        self._autosave_interval = 60.0       # save state every 60 seconds
        self._mud_time_obs = []              # [(monotonic, mud_hour), ...] for calibration
        self._last_inv_request = 0.0         # monotonic time of last inventory check
        self._inv_interval = 60.0            # check inventory every 60 seconds
        self._pending_equip = []             # item names queued for equip attempt
        self._exploration_steps = 0          # rooms entered without a fight
        self._hunt_threshold = 15            # explore this many rooms then seek combat
        self._survival_fail_count = 0        # consecutive ticks where survival made no progress
        self._survival_pause_until = 0.0     # monotonic time — suppress survival until then
        self._survival_llm_room = None       # room hash where LLM was last asked for survival
        self._look_retry_count = 0           # startup look retries before giving up
        # Rolling buffer of recent MUD text — fed to the LLM as context
        self._recent_mud_lines = deque(maxlen=30)
        # Position tracking — gives each physical room a unique (x, y, z) identity
        # even when many rooms share the same content hash (chess boards, mazes, etc.)
        self._pos = (0, 0, 0)           # current position relative to session start
        self._pos_visited = set()       # {(x, y, z)} physically confirmed visited
        self._pos_links = {}            # {(x,y,z): {direction: (x2,y2,z2)}}
        self._collision_hashes = set()  # hashes confirmed to have multiple physical rooms
        # hash → set of (x,y,z) positions where that hash has been observed.
        # A real collision only exists when the same hash appears at 2+ DISTINCT positions.
        # Normal room revisits (same hash, same position) do NOT indicate a collision.
        self._hash_positions = {}       # hash -> set of positions
        # Exits actually observed at each (x,y,z) position — used by BFS so it
        # doesn't invent phantom routes through exits that don't exist.
        self._pos_exits = {}            # (x,y,z) -> set of direction strings

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def start(self):
        """Load persisted state and begin the tick loop."""
        self.load_state()
        # If key score values are unknown, request score on first tick
        if self.state.max_hp is None:
            self._last_score_request = 0.0
        # Seed visited with current room if known
        if self.client.current_room_hash:
            current = self.client.current_room_hash
            if current not in self.state.visited:
                self.state.visited.add(current)
                self._enqueue_unvisited_neighbors(current)
            # Re-scan all known rooms for exits that were never traversed.
            # This repairs an empty frontier after a session that ended mid-exploration.
            untried_rooms = self._find_rooms_with_untried_exits()
            for room_hash in untried_rooms:
                if room_hash not in self.state.frontier:
                    self.state.frontier.append(room_hash)
            self.client.append_text(
                f"[AI] Starting in known room. "
                f"Visited: {len(self.state.visited)}, "
                f"Frontier: {len(self.state.frontier)} "
                f"({len(untried_rooms)} with untried exits)\n", "system")
        else:
            # Current room unknown — send 'look' to establish position.
            # Set _waiting_for_room so the tick loop waits for the response.
            self.client.append_text("[AI] Current room unknown — sending 'look' to establish position.\n", "system")
            self._waiting_for_room = True
            self._wait_started = time.monotonic()
            self._look_retry_count = 0
            self.client.send_ai_command('look')
        self.is_running = True
        # Ask Otto what he can do (once per session)
        if not self.state.otto_queried:
            self.client.master.after(
                self.COMMAND_DELAY_MS,
                lambda: self._tell_otto('help') if self.is_running else None)
            self.state.otto_queried = True
        # Extra delay on start to give 'look' response time to arrive
        self._schedule_tick(self.COMMAND_DELAY_MS * 2)

    def stop(self):
        """Stop the agent and persist state."""
        self.is_running = False
        if self._timer_handle is not None:
            self.client.master.after_cancel(self._timer_handle)
            self._timer_handle = None
        self.save_state()

    def pause(self):
        """Pause without saving state."""
        self.is_running = False
        if self._timer_handle is not None:
            self.client.master.after_cancel(self._timer_handle)
            self._timer_handle = None

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------

    def _schedule_tick(self, delay_ms=None):
        if delay_ms is None:
            delay_ms = self.COMMAND_DELAY_MS
        self._timer_handle = self.client.master.after(delay_ms, self._tick)

    def _tick(self):
        """Main decision loop — called from tkinter main thread."""
        self._timer_handle = None
        try:
            self._tick_body()
        except Exception as e:
            import traceback
            self.client.append_text(
                f"[AI] ERROR in tick: {e}\n{traceback.format_exc()}\n", "error")
            # Reschedule so a single error doesn't kill the loop
            self._schedule_tick(self.COMMAND_DELAY_MS)

    def _tick_body(self):
        """Inner tick logic — separated so exceptions can be caught cleanly."""
        if not self.is_running or not self.client.connected:
            return

        # If dead, wait for respawn (on_room_entered will clear this)
        if self._dead:
            self._schedule_tick(2000)
            return

        # If in combat, defer exploration moves
        if self._combat_active:
            self._schedule_tick(1000)
            return

        # If waiting for room confirmation from a previous move (or startup look)
        if self._waiting_for_room:
            elapsed_ms = (time.monotonic() - self._wait_started) * 1000
            if elapsed_ms < self.ROOM_TIMEOUT_MS:
                self._schedule_tick(300)
                return
            # Timed out — if no room hash yet (startup), retry look up to 5 times
            if self.client.current_room_hash is None:
                self._look_retry_count = getattr(self, '_look_retry_count', 0) + 1
                if self._look_retry_count <= 5:
                    self.client.append_text(
                        f"[AI] No room response yet — retrying look ({self._look_retry_count}/5).\n",
                        "system")
                    self._wait_started = time.monotonic()
                    self.client.send_ai_command('look')
                    self._schedule_tick(3000)
                    return
                else:
                    self.client.append_text(
                        "[AI] Could not establish room after 5 attempts — stopping.\n", "error")
                    self.stop()
                    return
            # Timed out on a move — treat as blocked
            self._handle_blocked_move()

        # Periodically fetch character sheet (SCORE) and MUD time for calibration.
        # These are sent alone (not with a move) so their responses can't arrive
        # while expecting_room_data=True and accidentally clear that flag.
        now = time.monotonic()
        need_score = now - self._last_score_request >= self._score_interval
        need_time = (not self._combat_active
                     and now - self._last_time_request >= self._time_interval)
        need_who = now - self._last_who_request >= self._who_interval
        if need_score or need_time or need_who:
            if need_score:
                self._last_score_request = now
                self.client.send_ai_command('score')
            if need_time:
                self._last_time_request = now
                self.client.send_ai_command('time')
            if need_who:
                self._last_who_request = now
                self.client.send_ai_command('who')
            self._schedule_tick(self.COMMAND_DELAY_MS * 2)
            return

        # Periodic auto-save — preserves discovered water/food sources, exploration
        # state, and other incremental data even if the session ends unexpectedly.
        if now - self._last_autosave >= self._autosave_interval:
            self._last_autosave = now
            self.save_state()

        # Survival needs take priority over exploration.
        # If survival has failed to make progress 3+ times, pause it for 60 seconds
        # and fall through to exploration so the agent can discover a resource.
        survival = None
        if time.monotonic() >= self._survival_pause_until:
            survival = self._survival_action()
        if survival:
            self._do_survival_action(survival)
            return

        # Drain auto-pickup queue before moving — pick up gold/food spotted on entry
        if self._auto_pickup_pending:
            cmd = self._auto_pickup_pending.pop(0)
            self.client.send_ai_command(cmd)
            self._schedule_tick(self.COMMAND_DELAY_MS)
            return

        # Try equipping items queued from inventory checks
        if self._pending_equip:
            item = self._pending_equip.pop(0)
            if item not in self.state.unequippable:
                equip_cmd = self.parser.classify_equip_command(item)
                if equip_cmd:
                    self.client.append_text(
                        f"[AI] Trying to equip: {equip_cmd} {item}\n", "system")
                    self.client.send_ai_command(f"{equip_cmd} {item}")
                    self._schedule_tick(self.COMMAND_DELAY_MS)
                    return

        # Periodically check inventory for new loot
        now = time.monotonic()
        if now - self._last_inv_request >= self._inv_interval:
            self._last_inv_request = now
            for cmd in ('inventory', 'equipment'):
                if cmd not in self._auto_pickup_pending:
                    self._auto_pickup_pending.append(cmd)


        # If an LLM call is already in flight, wait for it
        if self._waiting_for_llm:
            self._schedule_tick(500)
            return

        # Periodically divert from exploration to hunt a nearby beatable NPC.
        # Only when frontier is nearly exhausted — don't interrupt early exploration.
        if (self._exploration_steps >= self._hunt_threshold
                and len(self.state.frontier) < 5
                and self._should_fight()
                and not self._needs_gold()):  # gold need already handled by survival
            hunted = self._hunt_nearest_npc()
            if hunted:
                return

        # Choose next action via BFS rules
        direction = self._choose_next_direction()
        if direction is None:
            if not self.client.current_room_hash:
                self.client.append_text("[AI] Waiting for room data...\n", "system")
                self._schedule_tick(2000)
                return
            # BFS exhausted — ask the LLM
            self._request_llm_action()
            return

        self._execute_move(direction)
        self._schedule_tick(self.COMMAND_DELAY_MS)

    def _handle_blocked_move(self):
        """Record the timed-out direction as a dead end for the current room."""
        current = self.client.current_room_hash
        if current and self._last_direction:
            if current not in self.state.dead_ends:
                self.state.dead_ends[current] = set()
            self.state.dead_ends[current].add(self._last_direction)
            # Also mark as None in room_links so we don't try again
            if self.client.current_profile:
                profile = self.client.profiles.get(self.client.current_profile, {})
                room_links = profile.setdefault('room_links', {})
                room_links.setdefault(current, {})[self._last_direction] = None
        self._waiting_for_room = False
        self._last_direction = None

    def _choose_next_direction(self):
        """
        BFS exploration strategy. Returns a direction string or None.

        Priority:
        0. Position-based navigation when in a collision zone (rooms with non-unique hashes)
        1. Unmapped exits in the current room (directions MUD reports but we haven't tried)
        2. Mapped but unvisited neighbors of the current room
        3. BFS path to nearest frontier node (room known but not yet visited)
        4. BFS to nearest room with untried exits (resumes incomplete sessions)
        """
        current = self.client.current_room_hash
        if not current or not self.client.current_profile:
            return None

        profile = self.client.profiles.get(self.client.current_profile, {})
        room_links = profile.get('room_links', {})
        rooms = profile.get('rooms', {})
        dead_ends = self.state.dead_ends.get(current, set())

        # Directions we have already tried (mapped or confirmed dead)
        tried_directions = set(room_links.get(current, {}).keys()) | dead_ends

        # Skip directions known to lead to darkness (unless we now have light)
        dark_dirs = self.state.dark_directions.get(current, set())
        if not self.state.has_light:
            tried_directions |= dark_dirs

        exits_text = rooms.get(current, {}).get('exits', '')
        reported_exits = self.pathfinder.parse_exits_text(exits_text)

        # 0. Collision zone: use position coordinates to distinguish physical rooms
        #    that share the same content hash (chess boards, repetitive mazes, etc.)
        if current in self._collision_hashes:
            direction = self._choose_collision_direction(reported_exits, dead_ends, dark_dirs)
            if direction is not None:
                return direction
            # No unvisited positions reachable from here — BFS through position
            # graph to find a position that still has unexplored neighbours.
            return self._navigate_within_collision_zone(reported_exits, dead_ends, dark_dirs)

        # 1. Exits reported by the MUD that we haven't tried yet
        untried = reported_exits - tried_directions
        if untried:
            return next(iter(untried))

        # Helper: skip collision zones and rooms with deadly NPCs
        npc_danger_rooms = self._npc_danger_rooms()
        def skip_hash(dest_hash):
            if dest_hash is None:
                return False
            return dest_hash in self._collision_hashes or dest_hash in npc_danger_rooms

        # 2. Mapped neighbors that are unvisited (skip collision-zone hashes)
        unvisited_neighbors = self.pathfinder.get_unvisited_neighbors(
            current, room_links, self.state.visited)
        unvisited_neighbors = [(d, h) for d, h in unvisited_neighbors if not skip_hash(h)]
        if unvisited_neighbors:
            return unvisited_neighbors[0][0]

        # 3. BFS to nearest frontier node (skip collision-zone hashes)
        frontier_set = {h for h in self.state.frontier if not skip_hash(h)}
        target = self.pathfinder.find_nearest_frontier(current, room_links, frontier_set)
        if target:
            path = self.pathfinder.bfs_path(room_links, current, target)
            if path:
                return path[0]

        # 4. BFS to nearest room with untried exits (skip collision-zone hashes)
        untried_rooms = {h for h in self._find_rooms_with_untried_exits()
                         if not skip_hash(h)}
        if untried_rooms:
            target = self.pathfinder.find_nearest_frontier(current, room_links, untried_rooms)
            if target:
                if target == current:
                    untried = reported_exits - tried_directions
                    if untried:
                        return next(iter(untried))
                else:
                    path = self.pathfinder.bfs_path(room_links, current, target)
                    if path:
                        return path[0]

        return None

    def _choose_collision_direction(self, reported_exits, dead_ends, dark_dirs):
        """
        Pick an exit whose target (x, y, z) position has never been visited.
        Returns a direction string or None if all reachable positions are known.
        """
        skip = dead_ends | (dark_dirs if not self.state.has_light else set())
        for direction in reported_exits:
            if direction in skip:
                continue
            dx, dy, dz = DIRECTION_DELTAS.get(direction, (0, 0, 0))
            target_pos = (self._pos[0] + dx, self._pos[1] + dy, self._pos[2] + dz)
            if target_pos not in self._pos_visited:
                return direction
        return None

    def _navigate_within_collision_zone(self, reported_exits, dead_ends, dark_dirs):
        """
        BFS through visited positions to find the nearest one that still has
        an unvisited neighbour, then return the first step from the current position.
        Returns a direction string or None if the entire zone is fully explored.
        """
        skip = dead_ends | (dark_dirs if not self.state.has_light else set())

        # BFS: (position, first_direction_taken_from_start)
        from collections import deque as _deque
        queue = _deque([(self._pos, None)])
        seen = {self._pos}
        while queue:
            pos, first_step = queue.popleft()
            # Use exits observed at this specific position (not the starting room's exits)
            # so we don't invent phantom routes through exits that don't exist there.
            exits_here = self._pos_exits.get(pos, reported_exits)
            for direction in exits_here:
                if direction in skip:
                    continue
                dx, dy, dz = DIRECTION_DELTAS.get(direction, (0, 0, 0))
                neighbour = (pos[0] + dx, pos[1] + dy, pos[2] + dz)
                step = first_step if first_step is not None else direction
                if neighbour not in self._pos_visited:
                    # pos has an unvisited neighbour — head toward pos first
                    return step
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append((neighbour, step))
        return None  # collision zone fully explored

    def _execute_move(self, direction):
        """Issue a movement command and start waiting for room confirmation."""
        current = self.client.current_room_hash
        self._last_direction = direction
        self._waiting_for_room = True
        self._wait_started = time.monotonic()
        self.client.send_ai_command(direction)

    # ------------------------------------------------------------------
    # Callbacks from MUDClient (main thread)
    # ------------------------------------------------------------------

    def on_room_entered(self, room_hash, room_data):
        """Called by MUDClient after a room is successfully parsed."""
        if not self.is_running:
            return

        move_dir = self._last_direction   # capture before clearing
        self._waiting_for_room = False
        self._last_direction = None
        self._dead = False  # if we were dead, we've respawned
        self._otto_summon_sent_at = 0.0   # room entry confirms teleport complete

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
                room_links.setdefault(room_hash, {})[rev] = prev_hash
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

        # Scan room for resources — run on EVERY visit so we catch shops/water
        # even when returning after a previous session.
        if room_data:
            description = room_data.get('description', '')
            objects_text = ' '.join(room_data.get('objects', []))
            full_text = room_data.get('name', '') + ' ' + description + ' ' + objects_text

            # Water source discovery — accumulate all known sources
            water_cmd = self.parser.detect_water_source(full_text)
            if (water_cmd
                    and room_hash not in self.state.water_sources
                    and room_hash not in self.state.water_sources_failed):
                self.state.water_sources[room_hash] = water_cmd
                self.client.append_text(
                    f"[AI] Water source found here! Command: '{water_cmd}' "
                    f"({len(self.state.water_sources)} known)\n", "system")
                if self.state.seeking_resource == 'water':
                    self.state.seeking_resource = None

            # Food shop discovery — also re-check if we've returned and still don't know the item
            if not self.state.food_room or (room_hash == self.state.food_room and not self.state.food_item):
                if (room_hash not in self.state.food_rooms_failed
                        and (self.parser.detect_food_shop(full_text) or room_hash == self.state.food_room)):
                    if not self.state.food_room:
                        self.state.food_room = room_hash
                        self.client.append_text("[AI] Food shop found!\n", "system")
                        if self.state.seeking_resource == 'food':
                            self.state.seeking_resource = None
                    # Always request the list when in the shop and item is unknown
                    self._request_shop_list()

            # Mob lines — extract early so Otto presence check can use them
            mob_lines = room_data.get('mob_lines')

            # Otto presence — note his room, use pending service if any
            mob_text = ' '.join(mob_lines) if mob_lines else ''
            self._otto_in_current_room = self.parser.detect_otto_present(
                full_text + ' ' + mob_text)
            if self._otto_in_current_room:
                self.state.otto_room = room_hash
                if self._otto_pending_service:
                    svc = self._otto_pending_service
                    self._otto_pending_service = None
                    if svc == 'buffs':
                        # Arrived at Otto after summon — reset sequence so next
                        # survival tick rebuilds and sends with Otto present.
                        self._buff_sequence_pending = []
                        self.client.append_text(
                            "[AI] Otto is here — starting buff sequence.\n", "system")
                    else:
                        self.client.append_text(
                            f"[AI] Otto is here — requesting '{svc}'.\n", "system")
                        self.client.master.after(
                            self.COMMAND_DELAY_MS,
                            lambda s=svc: self._tell_otto(s) if self.is_running else None)
                else:
                    self.client.append_text("[AI] Otto is here.\n", "system")

            # Ground-item pickup: gold and food every visit (items may have been
            # dropped since the last time we were here).
            ground_items = self.parser.detect_ground_items(full_text)
            for cmd, label in ground_items:
                if cmd not in self._auto_pickup_pending:
                    self._auto_pickup_pending.append(cmd)
                    self.client.append_text(
                        f"[AI] Spotted on ground: {label} — queuing pickup.\n", "system")

            # Proactively drink from a water source when we're standing next to one
            # and have some thirst — avoids spam when we're already hydrated.
            if room_hash in self.state.water_sources and self.state.thirst_level is not None:
                drink_cmd = self.state.water_sources[room_hash]
                if drink_cmd not in self._auto_pickup_pending:
                    self._auto_pickup_pending.append(drink_cmd)

            # Mob/item detection — check every visit for danger
            # Prefer color-parsed mob_lines if available (1A), fall back to regex.
            # (mob_lines already extracted above for Otto presence check)
            color_calibrated = bool(mob_lines is not None)  # mob_lines=[] means calibrated+empty
            if mob_lines:
                # Color-calibrated: use line-aware extractor that handles lowercase names
                mobs = self.parser.detect_mobs_in_lines(mob_lines)
            else:
                mobs = self.parser.detect_mobs(description)

            # Filter PCs out of mobs entirely — before recording in npc_danger/entity_db.
            # Check prefix match so "Otto the Automatic Cleric" is caught by "otto".
            pc_names = {e['name'].lower() for e in self.state.who_list}
            if pc_names:
                mobs = [m for m in mobs
                        if not any(m.lower() == pc or m.lower().startswith(pc + ' ')
                                   for pc in pc_names)]

            # Filter "your X" mobs — guildmasters and other possessive-prefixed NPCs
            # are quest-givers / trainers that can't be killed and shouldn't be recorded.
            mobs = [m for m in mobs if not m.lower().startswith('your ')]

            if mobs:
                if room_hash not in self.state.visited:
                    self.client.append_text(f"[AI] Mobs detected: {', '.join(mobs)}\n", "system")
                # Record every detected mob's location and increment sighting count
                for mob in mobs:
                    rec = self.state.npc_danger.setdefault(mob.lower(), {
                        "deaths": 0, "fastest_death_secs": None,
                        "wins": 0, "near_kills": 0, "last_room": None,
                        "sightings": 0,
                    })
                    rec["last_room"] = room_hash
                    rec["sightings"] = rec.get("sightings", 0) + 1

                # Update profile-level mob_db / entity_db (1E)
                self._update_entity_db(room_hash, mobs)

                # Proactively attack beatable NPCs for XP and loot
                if self._should_fight():
                    for mob in mobs:
                        mob_lower = mob.lower()
                        rec = self.state.npc_danger.get(mob_lower)
                        if not rec or rec.get("deaths", 0) > 0:
                            continue
                        # When color-calibrated, trust mob on first sight.
                        # When using regex fallback, require 2+ sightings to
                        # avoid false positives from room description text.
                        if not color_calibrated and rec.get("sightings", 0) < 2:
                            continue
                        reason = "need gold —" if self._needs_gold() else "attacking for XP —"
                        self.client.append_text(
                            f"[AI] {reason} attacking {mob}.\n", "system")
                        self.client.send_ai_command(f"kill {mob_lower}")
                        self._combat_active = True
                        self._combat_seen_active = False
                        self._combat_win_counted = False
                        self._combat_npc = mob_lower
                        self._last_combat_npc = mob_lower
                        self._combat_start_time = time.monotonic()
                        self._last_combat_start_time = self._combat_start_time
                        self._combat_start_hp = self.state.current_hp
                        self._schedule_tick(self.COMMAND_DELAY_MS * 2)
                        return

                # Check for known-dangerous NPCs and flee if we somehow entered anyway
                for mob in mobs:
                    rec = self.state.npc_danger.get(mob.lower())
                    if rec and rec.get("deaths", 0) > 0:
                        fast = rec.get("fastest_death_secs")
                        self.client.append_text(
                            f"[AI] DANGER: {mob} has killed us {rec['deaths']} time(s)"
                            f"{f' (fastest: {fast}s)' if fast else ''} — fleeing!\n",
                            "system")
                        self.client.send_ai_command("flee")
                        self._schedule_tick(self.COMMAND_DELAY_MS * 2)
                        return
            # Update entity_db even when no mobs — records the empty snapshot (1E)
            if not mobs:
                self._update_entity_db(room_hash, [])

            items = self.parser.detect_items(description)
            if items and room_hash not in self.state.visited:
                self.client.append_text(f"[AI] Items visible: {', '.join(items)}\n", "system")

        self._exploration_steps += 1
        if room_hash and room_hash not in self.state.visited:
            self.state.visited.add(room_hash)
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

        # --- Otto tell responses ---
        otto_msg = self.parser.parse_otto_tell(clean_text)
        if otto_msg:
            self.client.append_text(f"[AI] Otto says: {otto_msg}\n", "system")
            # Parse capabilities from help response — replace list if we asked for help
            caps = self.parser.parse_otto_capabilities(otto_msg)
            if caps:
                # Always merge — never replace — so hardcoded defaults aren't lost
                # if Otto's help response is partial or unparseable.
                new_caps = [c for c in caps if c not in self.state.otto_capabilities]
                if new_caps:
                    self.state.otto_capabilities.extend(new_caps)
                if self._waiting_for_otto_help:
                    self._waiting_for_otto_help = False
                    self.client.append_text(
                        f"[AI] Otto capabilities (updated): {', '.join(self.state.otto_capabilities)}\n",
                        "system")
                elif new_caps:
                    self.client.append_text(
                        f"[AI] Otto capabilities: {', '.join(self.state.otto_capabilities)}\n",
                        "system")
                self.save_state()
            elif self._waiting_for_otto_help:
                # Help response arrived but we couldn't parse capabilities — clear flag anyway
                self._waiting_for_otto_help = False
            # Detect "I don't know" / failed service — remove the last attempted service
            elif re.search(r"(?:don't|do not|cannot|can't)\s+(?:know|understand|do that)", otto_msg, re.I):
                if self._otto_pending_service and self._otto_pending_service in self.state.otto_capabilities:
                    self.state.otto_capabilities.remove(self._otto_pending_service)
                    self.client.append_text(
                        f"[AI] Removed failed Otto capability: {self._otto_pending_service}\n", "system")
                    self.save_state()

        # --- Darkness ---
        if self.parser.detect_darkness(clean_text):
            if not self.state.has_light:
                pass  # already known
            else:
                self.state.has_light = False
            if self._waiting_for_room:
                self._handle_darkness()
                return
            # If we're just sitting in a dark room, note it
            if self.client.current_room_hash:
                self.state.dark_rooms.add(self.client.current_room_hash)

        # --- Light changes ---
        if self.parser.detect_light_gained(clean_text):
            if not self.state.has_light:
                self.state.has_light = True
                # Re-open dark directions for re-exploration now that we have light
                self.state.dark_directions.clear()
                self.state.dark_rooms.clear()
                self.client.append_text(
                    "[AI] Light source acquired — dark areas re-queued for exploration.\n", "system")
        elif self.parser.detect_light_lost(clean_text):
            if self.state.has_light:
                self.state.has_light = False
                self.client.append_text(
                    "[AI] Light lost — will avoid dark areas.\n", "system")

        # --- Hunger ---
        hunger = self.parser.detect_hunger(clean_text)
        if hunger and hunger != self.state.hunger_level:
            self.state.hunger_level = hunger
            self.client.append_text(f"[AI] Hunger detected: {hunger}.\n", "system")
        elif re.search(r'you eat|you consume|you finish eating', clean_text, re.IGNORECASE):
            if self.state.hunger_level:
                self.state.hunger_level = None
                self.state.current_goal = 'explore'
                self.client.append_text("[AI] Hunger satisfied — resuming exploration.\n", "system")


        # --- Thirst ---
        thirst = self.parser.detect_thirst(clean_text)
        if thirst and thirst != self.state.thirst_level:
            self.state.thirst_level = thirst
            self.client.append_text(f"[AI] Thirst detected: {thirst}.\n", "system")
        elif re.search(r'you drink|you quench|you sip|you gulp', clean_text, re.IGNORECASE):
            if self.state.thirst_level:
                self.state.thirst_level = None
                self.state.current_goal = 'explore'
                self.client.append_text("[AI] Thirst quenched — resuming exploration.\n", "system")

        # --- Accumulate recent MUD text for LLM context ---
        # Stat prompts, command echoes, and agent status lines are
        # machine-readable noise — strip them so the LLM sees clean narrative.
        for line in clean_text.splitlines():
            line = line.strip()
            if line and not _MUD_NOISE_RE.match(line):
                self._recent_mud_lines.append(line)

        # --- SCORE / character sheet parsing ---
        score = self.parser.parse_score(clean_text)
        if score:
            if 'level'     in score: self.state.char_level     = score['level']
            if 'class_name'in score: self.state.char_class     = score['class_name']
            if 'max_hp'    in score: self.state.max_hp         = score['max_hp']
            if 'max_mp'    in score: self.state.max_mp         = score['max_mp']
            if 'max_mv'    in score: self.state.max_mv         = score['max_mv']
            if 'xp'        in score: self.state.char_xp        = score['xp']
            if 'xp_next'   in score: self.state.char_xp_next   = score['xp_next']
            if 'gold'      in score: self.state.gold           = score['gold']
            if 'alignment' in score: self.state.char_alignment = score['alignment']
            self.save_state()

        # --- WHO list parsing ---
        char_name = self.client.current_profile
        players, self_entry = self.parser.parse_who(clean_text, char_name)
        if players:
            self.state.who_list = players
            if self_entry:
                if self_entry['level'] != self.state.char_level:
                    self.state.char_level = self_entry['level']
                if self_entry['class'] != self.state.char_class:
                    self.state.char_class = self_entry['class']
                    self.client.append_text(
                        f"[AI] Class identified from who: {self_entry['class']}\n", "system")
            self.save_state()

        # --- Buff application / expiration detection ---
        buff_events = self.parser.detect_buff_events(clean_text)
        if buff_events['applied'] or buff_events['expired']:
            now = time.monotonic()
            for buff in buff_events['applied']:
                ticks = self.parser.BUFF_DEFAULT_TICKS.get(buff, 4)
                self.state.buff_expires[buff] = now + ticks * self.state.TICK_SECS
                self.client.append_text(
                    f"[AI] Buff '{buff}' confirmed applied "
                    f"({ticks} ticks, ~{ticks * self.state.TICK_SECS // 60:.0f} min).\n",
                    "system")
            for buff in buff_events['expired']:
                self.state.buff_expires.pop(buff, None)
                self.client.append_text(
                    f"[AI] Buff '{buff}' expired — will re-request.\n", "system")

        # --- Spell affects / buff duration parsing ---
        affects = self.parser.parse_spell_affects(clean_text)
        if affects:
            now = time.monotonic()
            for buff, ticks in affects.items():
                expires_at = now + ticks * self.state.TICK_SECS
                self.state.buff_expires[buff] = expires_at
                self.client.append_text(
                    f"[AI] Buff '{buff}' active: {ticks} ticks remaining "
                    f"({ticks * self.state.TICK_SECS // 60:.0f} min).\n", "system")

        # --- MUD time parsing (buff duration calibration) ---
        mud_hour = self.parser.parse_mud_time(clean_text)
        if mud_hour is not None:
            self._record_mud_time_obs(mud_hour)

        # --- Stat parsing ---
        stats = self.parser.parse_prompt_stats(clean_text)
        if stats:
            self.state.current_hp = stats.get('hp', self.state.current_hp)
            self.state.current_mp = stats.get('mp', self.state.current_mp)
            self.state.current_mv = stats.get('mv', self.state.current_mv)
            prev_opp = self.state.current_opp_pct
            self.state.current_tank_pct = stats.get('tank', self.state.current_tank_pct)
            self.state.current_opp_pct = stats.get('opp', self.state.current_opp_pct)
            # Opponent just hit 0% — kill confirmed via prompt opp% transition.
            # prev_opp != 0 covers both healthy (>0) and mortally wounded (<0) states.
            if (self._combat_active and prev_opp is not None and prev_opp != 0
                    and self.state.current_opp_pct == 0):
                npc = self._combat_npc
                if npc and self._combat_seen_active:
                    rec = self.state.npc_danger.setdefault(npc, {
                        "deaths": 0, "fastest_death_secs": None,
                        "wins": 0, "near_kills": 0, "last_room": None,
                    })
                    rec["wins"] += 1
                    rec["last_room"] = self.client.current_room_hash
                    self._combat_win_counted = True
                    self.client.append_text(
                        f"[AI] Defeated {npc} (wins: {rec['wins']}).\n", "system")
                    for cmd in ('get all corpse', 'inventory', 'equipment'):
                        if cmd not in self._auto_pickup_pending:
                            self._auto_pickup_pending.append(cmd)
                self._combat_active = False
                self._combat_seen_active = False
                self._last_combat_npc = npc if npc else self._last_combat_npc
                self.client.append_text("[AI] Opponent defeated — resuming exploration.\n", "system")

        # --- XP gain ---
        xp = self.parser.detect_xp_gain(clean_text)
        if xp:
            self.state.total_xp_gained += xp
            self.client.append_text(f"[AI] XP gained: +{xp} (total: {self.state.total_xp_gained})\n", "system")
            # XP gain is a reliable kill signal — queue looting and credit win if the
            # opp%-transition paths missed it (e.g. combat_over fired prematurely).
            for cmd in ('get all corpse', 'inventory', 'equipment'):
                if cmd not in self._auto_pickup_pending:
                    self._auto_pickup_pending.append(cmd)
            if not self._combat_win_counted:
                npc = self._combat_npc or self._last_combat_npc
                if npc:
                    rec = self.state.npc_danger.setdefault(npc, {
                        "deaths": 0, "fastest_death_secs": None,
                        "wins": 0, "near_kills": 0, "last_room": None,
                    })
                    rec["wins"] += 1
                    rec["last_room"] = self.client.current_room_hash
                    self._combat_win_counted = True
                    self.client.append_text(
                        f"[AI] Defeated {npc} (wins: {rec['wins']}).\n", "system")

        # --- Gold tracking ---
        gold_carried = self.parser.detect_gold_carried(clean_text)
        if gold_carried is not None:
            self.state.gold = gold_carried
        else:
            gold_received = self.parser.detect_gold_received(clean_text)
            if gold_received and self.state.gold is not None:
                self.state.gold += gold_received

        # --- "You can't find it!" — drink/eat target not present here ---
        # Clears a false water-source record so we don't keep navigating back.
        if re.search(r"you can.t find it|you don.t see (?:it|that) here",
                     clean_text, re.IGNORECASE):
            current = self.client.current_room_hash
            if current in self.state.water_sources:
                self.client.append_text(
                    "[AI] Drink failed here — blacklisting water source.\n", "system")
                del self.state.water_sources[current]
                self.state.water_sources_failed.add(current)

        # --- "Sorry, you cannot do that here" — MUD rejected a shop command ---
        # This fires when we sent 'list' or 'buy' in a non-shop room.  Clear the
        # false-positive food/water room so we don't keep retrying there.
        if re.search(r"sorry.*cannot do that here|you cannot do that here",
                     clean_text, re.IGNORECASE):
            current = self.client.current_room_hash
            if self.state.food_room == current:
                self.client.append_text(
                    "[AI] 'list' rejected here — blacklisting false food-shop room.\n", "system")
                self.state.food_rooms_failed.add(current)
                self.state.food_room = None
                self.state.food_item = None
                self.state.food_price = None
            if current in self.state.water_sources:
                self.client.append_text(
                    "[AI] Drink rejected here — blacklisting water source.\n", "system")
                del self.state.water_sources[current]
                self.state.water_sources_failed.add(current)

        # --- Shop list: learn food item and price ---
        if self.state.food_room and not self.state.food_item:
            food_items = self.parser.parse_shop_list(clean_text)
            if food_items:
                item_name, price = food_items[0]
                self.state.food_item = item_name
                if price is not None:
                    self.state.food_price = price
                self.client.append_text(
                    f"[AI] Learned food item: '{item_name}'"
                    f"{f' ({price} gold)' if price is not None else ''}\n", "system")

        # --- Inventory parsing ---
        inv = self.parser.parse_inventory(clean_text)
        if inv is not None:
            old_set = set(self.state.inventory)
            self.state.inventory = inv
            new_items = [i for i in inv if i not in old_set and i not in self.state.unequippable]
            for item in new_items:
                if self.parser.classify_equip_command(item) and item not in self._pending_equip:
                    self._pending_equip.append(item)
                    self.client.append_text(f"[AI] New item to try equipping: {item}\n", "system")

        # --- Equipment parsing ---
        eq = self.parser.parse_equipment(clean_text)
        if eq is not None:
            self.state.equipment = eq

        # --- Failed equip detection ---
        if re.search(r"you can't use|you can't wear|you can't wield|you can't hold|"
                     r"you do not have|you don't have|incompatible|wrong type|"
                     r"you are not skilled enough",
                     clean_text, re.IGNORECASE):
            # Look for the item name in the rejection text and mark it unequippable
            for item in list(self.state.inventory):
                if item.lower() in clean_text.lower():
                    self.state.unequippable.add(item)
                    self.client.append_text(
                        f"[AI] Can't equip '{item}' — skipping in future.\n", "system")
                    break

        # --- Death detection ---
        if self.parser.detect_death(clean_text):
            if not self._dead:
                self._dead = True
                self._waiting_for_room = False
                self.state.total_deaths += 1
                current = self.client.current_room_hash
                if current:
                    self.state.danger_rooms[current] = {
                        "reason": "died",
                        "time": datetime.now(timezone.utc).isoformat(),
                    }
                # Record which NPC killed us and how quickly.
                # Use _last_* fallbacks — "combat appears over" may have fired in a
                # previous chunk before the death message arrived, clearing the live vars.
                killer = self._combat_npc or self._last_combat_npc
                start_t = self._combat_start_time or self._last_combat_start_time
                if killer and start_t is not None:
                    elapsed = time.monotonic() - start_t
                    rec = self.state.npc_danger.setdefault(killer, {
                        "deaths": 0, "fastest_death_secs": None,
                        "wins": 0, "near_kills": 0, "last_room": None,
                    })
                    rec["deaths"] += 1
                    rec["last_room"] = current
                    if rec["fastest_death_secs"] is None or elapsed < rec["fastest_death_secs"]:
                        rec["fastest_death_secs"] = round(elapsed, 1)
                    self.client.append_text(
                        f"[AI] Killed by {killer} in {elapsed:.1f}s "
                        f"(deaths: {rec['deaths']}, fastest: {rec['fastest_death_secs']}s).\n",
                        "system")
                self._combat_active = False
                self._combat_seen_active = False
                self._combat_npc = None
                self._last_combat_npc = None
                self._combat_start_time = None
                self._last_combat_start_time = None
                self._combat_start_hp = None
                self.client.append_text(
                    f"[AI] Death detected (#{self.state.total_deaths}) — waiting for respawn.\n", "system")
                self.save_state()
            # Menu may arrive in the same chunk as the death message
            if re.search(r'make your choice\s*:', clean_text, re.IGNORECASE):
                self.client.append_text("[AI] Post-death menu detected — re-entering game.\n", "system")
                self.client.send_ai_command('1')
                self._dead = False
            return

        # --- Post-death main menu arriving in a later chunk ---
        if self._dead and re.search(r'make your choice\s*:', clean_text, re.IGNORECASE):
            self.client.append_text("[AI] Post-death menu detected — re-entering game.\n", "system")
            self.client.send_ai_command('1')
            self._dead = False
            return

        lower = clean_text.lower()

        # --- Blocked movement ---
        if self._waiting_for_room and any(p in lower for p in BLOCKED_PATTERNS):
            self._handle_blocked_move()
            return

        # --- Combat detection ---
        if self.parser.detect_combat_start(clean_text):
            self._combat_seen_active = True   # confirmed real combat
            if not self._combat_active:
                self._combat_active = True
                self._combat_seen_active = True
                self._combat_win_counted = False
                self._reset_exploration_counter()
                self._combat_start_time = time.monotonic()
                self._last_combat_start_time = self._combat_start_time
                self._combat_start_hp = self.state.current_hp
                attacker = self.parser.detect_combat_attacker(clean_text)
                self._combat_npc = attacker.lower() if attacker else None
                if self._combat_npc:
                    self._last_combat_npc = self._combat_npc
                self.client.append_text(
                    f"[AI] Combat detected vs {attacker or 'unknown'} — pausing exploration.\n",
                    "system")
        elif self._combat_active:
            # Also mark active if we see any combat pattern in this chunk
            if any(p in lower for p in COMBAT_ACTIVE_PATTERNS):
                self._combat_seen_active = True
            # Opponent HP going above 0 also confirms real combat
            if self.state.current_opp_pct is not None and self.state.current_opp_pct > 0:
                self._combat_seen_active = True
            round_data = self.parser.detect_combat_round(clean_text)
            if round_data and round_data.get('damage'):
                self._combat_seen_active = True
                self.client.append_text(
                    f"[AI] Combat: {round_data['attacker']} {round_data['verb']} "
                    f"{round_data['target']} for {round_data['damage']} damage.\n", "system")
            fled = self.parser.detect_flee(clean_text)
            opp_pct = self.state.current_opp_pct
            # Non-zero opp_pct means the opponent is still alive (positive = healthy,
            # negative = mortally wounded) — patterns alone cannot declare combat over.
            opp_alive = opp_pct is not None and opp_pct != 0
            combat_over = fled or (
                not any(p in lower for p in COMBAT_ACTIVE_PATTERNS) and not opp_alive
            )
            if combat_over:
                npc = self._combat_npc
                if npc:
                    rec = self.state.npc_danger.setdefault(npc, {
                        "deaths": 0, "fastest_death_secs": None,
                        "wins": 0, "near_kills": 0, "last_room": None,
                    })
                    rec["last_room"] = self.client.current_room_hash
                    if opp_pct == 0 and self._combat_seen_active:
                        # Only credit a win when we actually observed combat rounds —
                        # opp_pct is 0 both when not in combat and when mob is dead.
                        rec["wins"] += 1
                        self._combat_win_counted = True
                        self.client.append_text(
                            f"[AI] Defeated {npc} (wins: {rec['wins']}).\n", "system")
                        # Loot the corpse then check inventory for new items
                        for cmd in ('get all corpse', 'inventory', 'equipment'):
                            if cmd not in self._auto_pickup_pending:
                                self._auto_pickup_pending.append(cmd)
                    elif fled and opp_pct is not None and opp_pct <= 20:
                        rec["near_kills"] += 1
                        self.client.append_text(
                            f"[AI] Near-kill on {npc} at {opp_pct}% HP "
                            f"(near_kills: {rec['near_kills']}).\n", "system")
                self._combat_active = False
                self._combat_seen_active = False
                self._combat_npc = None
                # Keep _last_combat_npc set until either death attribution clears it
                # or the next combat begins — do NOT clear it here.
                self._combat_start_time = None
                self._combat_start_hp = None
                if fled:
                    self.client.append_text("[AI] Fled combat — resuming exploration.\n", "system")
                else:
                    self.client.append_text("[AI] Combat appears over — resuming exploration.\n", "system")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Survival logic
    # ------------------------------------------------------------------

    def _survival_action(self):
        """
        Return a survival action descriptor if the character needs attention,
        or None if exploration can continue.

        Priority: low HP (< 50% Otto present / < 40% Otto available / < 25% no Otto) > buffs > thirst > hunger > gold.
        """
        # HP-based healing — threshold depends on Otto availability:
        #   Otto present in room : 0.50 — proactively heal while he's standing here
        #   Otto can heal remotely: 0.40 — summon him for healing before doing buffs
        #   No Otto available    : 0.25 — rest/wait as last resort
        if (self.state.current_hp is not None and self.state.max_hp is not None
                and self.state.max_hp > 0):
            hp_pct = self.state.current_hp / self.state.max_hp
            if self._otto_in_current_room and self._otto_can('heal'):
                heal_threshold = 0.50
            elif self._otto_can('heal'):
                heal_threshold = 0.40
            else:
                heal_threshold = 0.25
            if hp_pct < heal_threshold:
                return {'need': 'heal'}
        # Protection buffs — apply before adventuring, refresh when expired
        if self._needs_buffs():
            return {'need': 'buffs'}
        if self.state.thirst_level in ('parched', 'thirsty'):
            return {'need': 'water'}
        if self.state.hunger_level in ('starving', 'hungry'):
            if self._needs_gold():
                return {'need': 'gold'}
            return {'need': 'food'}
        return None

    def _needs_gold(self):
        """Return True when we're hungry, know a food shop with a price, and can't afford it."""
        if self.state.hunger_level not in ('starving', 'hungry'):
            return False
        if not self.state.food_room or self.state.food_price is None:
            return False
        gold = self.state.gold
        return gold is None or gold < self.state.food_price

    def _should_fight(self):
        """
        Return True when conditions are good enough to proactively attack an NPC.
        Fights for XP, loot, and gold. Suppressed when HP is too low or thirst is critical.
        """
        # Don't fight when critically low on HP
        if (self.state.current_hp is not None and self.state.max_hp is not None
                and self.state.max_hp > 0
                and self.state.current_hp / self.state.max_hp < 0.40):
            return False
        # Don't fight when parched — water is more urgent
        if self.state.thirst_level == 'parched':
            return False
        return True

    def _hunt_nearest_npc(self):
        """
        Navigate toward the nearest beatable NPC.
        Returns True and schedules the next tick if a path was found, else False.
        Prefers NPCs we have already beaten; falls back to unknown ones.
        Only targets NPCs within a reasonable distance so we don't cross the whole map.
        """
        if not self.client.current_profile:
            return False
        current = self.client.current_room_hash
        if not current:
            return False
        profile = self.client.profiles.get(self.client.current_profile, {})
        room_links = profile.get('room_links', {})

        best_name, best_path = None, None
        for name, room in self._beatable_npcs():
            if room == current:
                # Already here — room-entry handler will attack; just return False
                # so the normal tick proceeds (auto-pickup / movement).
                return False
            path = self.pathfinder.bfs_path(room_links, current, room)
            if path and len(path) <= 10:  # don't hunt across the whole map
                if best_path is None or len(path) < len(best_path):
                    best_name, best_path = name, path

        if best_path:
            self._reset_exploration_counter()  # entering hunt mode
            self.client.append_text(
                f"[AI] Hunt: heading to fight {best_name} ({len(best_path)} steps).\n",
                "system")
            self._execute_move(best_path[0])
            self._schedule_tick(self.COMMAND_DELAY_MS)
            return True
        return False

    def _beatable_npcs(self):
        """
        Return (name, room_hash) pairs for NPCs worth attacking, in priority order:
          1. Known wins (deaths == 0, wins > 0)
          2. Near-kills (deaths == 0, near_kills > 0)
          3. Unknown NPCs (deaths == 0, no combat history)

        Also includes mobs from mob_db (1E) that aren't already in npc_danger —
        e.g. after an ai_state reset — as long as they haven't killed us.
        """
        preferred, near, unknown = [], [], []
        seen_names = set()
        for name, rec in self.state.npc_danger.items():
            seen_names.add(name)
            if rec.get('deaths', 0) > 0:
                continue
            room = rec.get('last_room')
            if not room:
                continue
            if rec.get('wins', 0) > 0:
                preferred.append((name, room))
            elif rec.get('near_kills', 0) > 0:
                near.append((name, room))
            else:
                unknown.append((name, room))

        # Also pull from mob_db for mobs not yet in npc_danger (e.g. after reset)
        profile = self._profile()
        if profile:
            for name, entry in profile.get('mob_db', {}).items():
                if name in seen_names:
                    continue
                room = entry.get('last_room')
                if room:
                    unknown.append((name, room))

        return preferred + near + unknown

    def _do_survival_action(self, action):
        """Execute a survival action — navigate to a known source or seek one."""
        need = action['need']
        current = self.client.current_room_hash

        # --- Earn gold by fighting beatable NPCs ---
        if need == 'gold':
            self.state.current_goal = 'earn_gold'
            profile = self.client.profiles.get(self.client.current_profile, {})
            room_links = profile.get('room_links', {})
            targets = self._beatable_npcs()
            if not targets:
                # No known targets — explore to find some
                self.client.append_text(
                    "[AI] Need gold but no known targets — exploring to find NPCs.\n", "system")
                direction = self._choose_next_direction()
                if direction:
                    self._execute_move(direction)
                    self._schedule_tick(self.COMMAND_DELAY_MS)
                else:
                    self._request_llm_action()
                return
            # Find nearest reachable target
            best_name, best_room, best_path = None, None, None
            for name, room in targets:
                if room == current:
                    best_name, best_room, best_path = name, room, []
                    break
                path = self.pathfinder.bfs_path(room_links, current, room)
                if path and (best_path is None or len(path) < len(best_path)):
                    best_name, best_room, best_path = name, room, path
            if best_name is None:
                # All targets unreachable — explore
                self.client.append_text(
                    "[AI] Known targets unreachable — exploring to find NPCs.\n", "system")
                direction = self._choose_next_direction()
                if direction:
                    self._execute_move(direction)
                    self._schedule_tick(self.COMMAND_DELAY_MS)
                else:
                    self._request_llm_action()
                return
            if best_room == current:
                # Target is here — attack (room entry handler already fires kill,
                # but this covers the case where we were already in the room)
                self.client.append_text(
                    f"[AI] Need gold — attacking {best_name} (already here).\n", "system")
                self.client.send_ai_command(f"kill {best_name}")
                self._combat_active = True
                self._combat_seen_active = False
                self._combat_win_counted = False
                self._combat_npc = best_name
                self._last_combat_npc = best_name
                self._combat_start_time = time.monotonic()
                self._last_combat_start_time = self._combat_start_time
                self._combat_start_hp = self.state.current_hp
                self._schedule_tick(self.COMMAND_DELAY_MS * 2)
            else:
                self.client.append_text(
                    f"[AI] Need gold — heading to fight {best_name} "
                    f"({len(best_path)} steps).\n", "system")
                self._execute_move(best_path[0])
                self._schedule_tick(self.COMMAND_DELAY_MS)
            return

        # --- Protection buffs via Otto ---
        if need == 'buffs':
            self._do_buff_sequence()
            return

        # --- Healing via Otto ---
        if need == 'heal':
            self.state.current_goal = 'get_heal'
            if self._otto_can('heal'):
                if self._otto_present():
                    self.client.append_text("[AI] Survival: asking Otto to heal us.\n", "system")
                    self._tell_otto('heal')
                    self._schedule_tick(self.COMMAND_DELAY_MS * 2)
                else:
                    self._otto_summon_and_use('heal')
            else:
                # No Otto heal available — just rest/wait
                self.client.append_text("[AI] Survival: low HP, resting.\n", "system")
                self._schedule_tick(5000)
            return

        if need == 'water':
            self.state.current_goal = 'get_water'
            # Pick nearest reachable water source
            profile = self.client.profiles.get(self.client.current_profile, {})
            room_links = profile.get('room_links', {})
            source_room = None
            drink_cmd = 'drink fountain'
            best_len = None
            for wroom, wcmd in self.state.water_sources.items():
                if wroom == current:
                    source_room = wroom
                    drink_cmd = wcmd
                    best_len = 0
                    break
                path = self.pathfinder.bfs_path(room_links, current, wroom)
                if path and (best_len is None or len(path) < best_len):
                    source_room = wroom
                    drink_cmd = wcmd
                    best_len = len(path)
        else:
            self.state.current_goal = 'get_food'
            source_room = self.state.food_room
            drink_cmd = None

        # --- Source known: navigate to it ---
        if source_room:
            if current == source_room:
                if need == 'water':
                    self._do_drink(drink_cmd)
                else:
                    self._do_eat()
                return

            profile = self.client.profiles.get(self.client.current_profile, {})
            room_links = profile.get('room_links', {})
            path = self.pathfinder.bfs_path(room_links, current, source_room)
            if path:
                self.client.append_text(
                    f"[AI] Survival: heading to {need} source "
                    f"({len(path)} steps away).\n", "system")
                self._execute_move(path[0])
                self._schedule_tick(self.COMMAND_DELAY_MS)
                return
            # Source known but unreachable — forget it and re-seek
            self.client.append_text(
                f"[AI] Survival: {need} source unreachable — re-seeking.\n", "system")
            if need == 'water':
                self.state.water_sources.pop(source_room, None)
            else:
                self.state.food_room = None

        # --- Source unknown: ask LLM to inspect each room, fall back to BFS ---
        if self.state.seeking_resource != need:
            self.state.seeking_resource = need
            self.client.append_text(
                f"[AI] Survival: {need} source unknown — "
                f"asking LLM each room to find one.\n", "system")

        # Ask the LLM once per room — it won't give a different answer if we stay put.
        # If the LLM already answered in this room and the attempt failed, BFS to
        # the next room first so we're somewhere new before asking again.
        current_for_llm = self.client.current_room_hash
        if (self.llm.is_ready() and not self._waiting_for_llm
                and self._survival_llm_room != current_for_llm):
            self._survival_llm_room = current_for_llm
            self._survival_fail_count = 0
            self._request_llm_action()
            return

        # LLM already asked here, rate-limited, or in-flight — BFS to a new room.
        # Track consecutive no-progress ticks; after 3, pause survival for 60s.
        self._survival_fail_count += 1
        if self._survival_fail_count >= 3:
            self.client.append_text(
                "[AI] Survival stuck — pausing survival checks for 60s to explore.\n",
                "system")
            self._survival_fail_count = 0
            self._survival_pause_until = time.monotonic() + 60.0
        direction = self._choose_next_direction()
        if direction:
            self._execute_move(direction)
            self._schedule_tick(self.COMMAND_DELAY_MS)
        else:
            self._request_llm_action()

    def _do_drink(self, cmd):
        """Send a drink command and optimistically clear thirst."""
        current = self.client.current_room_hash
        self.client.append_text(f"[AI] Survival: sending '{cmd}'\n", "system")
        self.client.send_ai_command(cmd)
        self.state.thirst_level = None
        self.state.seeking_resource = None
        self.state.current_goal = 'explore'
        self._schedule_tick(self.COMMAND_DELAY_MS)

    # ------------------------------------------------------------------
    # Otto helper-player interface
    # ------------------------------------------------------------------

    def _tell_otto(self, command):
        """Send a tell to Otto."""
        self.client.append_text(f"[AI] Telling Otto: {command}\n", "system")
        if command.strip().lower() == 'help':
            self._waiting_for_otto_help = True
        self.client.send_ai_command(f'tell otto {command}')

    def _otto_present(self):
        """Return True if Otto was detected in the current room's live text."""
        return self._otto_in_current_room

    def _otto_can(self, service):
        """Return True if Otto advertised this service in his help response."""
        return service in self.state.otto_capabilities

    def _otto_summon_and_use(self, service):
        """
        Ask Otto to summon us to him, then request the service once there.
        Works from anywhere since 'tell' is remote.
        """
        if self._otto_can('summon'):
            # Guard: only send one summon at a time — wait for room entry to clear flag
            if time.monotonic() - self._otto_summon_sent_at < 30.0:
                self._schedule_tick(self.COMMAND_DELAY_MS * 3)
                return
            self._otto_summon_sent_at = time.monotonic()
            self.client.append_text(
                f"[AI] Asking Otto to summon us, then requesting '{service}'.\n", "system")
            self._tell_otto('summon')
            # After teleport, the next on_room_entered will re-run survival logic
            # and Otto will be present — set a pending service so we use it
            self._otto_pending_service = service
            self._schedule_tick(self.COMMAND_DELAY_MS * 3)
        else:
            self.client.append_text(
                "[AI] Otto summoning not available — continuing normal survival.\n", "system")

    def _request_shop_list(self):
        """Send 'list' to the shop, throttled to once per 20 seconds."""
        now = time.monotonic()
        if now - self._last_shop_list_request >= 20.0:
            self._last_shop_list_request = now
            self.client.append_text("[AI] Survival: checking shop inventory (list).\n", "system")
            self.client.send_ai_command('list')

    def _do_eat(self):
        """Buy food if needed and eat it; check gold first."""
        current = self.client.current_room_hash
        cfg = self._get_ai_config()

        # If we don't know the food item yet, request the shop list and wait
        if not self.state.food_item:
            self._request_shop_list()
            self._schedule_tick(self.COMMAND_DELAY_MS * 2)
            return

        # Check if we can afford it
        if (self.state.food_price is not None
                and self.state.gold is not None
                and self.state.gold < self.state.food_price):
            self.client.append_text(
                f"[AI] Survival: need food but only have {self.state.gold} gold "
                f"(food costs {self.state.food_price}). Need to earn more gold.\n", "system")
            # Fall back to exploration to kill mobs and earn gold
            self.state.current_goal = 'earn_gold'
            direction = self._choose_next_direction()
            if direction:
                self._execute_move(direction)
                self._schedule_tick(self.COMMAND_DELAY_MS)
            else:
                self._request_llm_action()
            return

        buy_cmd = f"buy {self.state.food_item}"
        eat_cmd = cfg.get('eat_command') or f"eat {self.state.food_item}"

        self.client.append_text(
            f"[AI] Survival: buying and eating '{self.state.food_item}'"
            f"{f' for {self.state.food_price} gold' if self.state.food_price else ''}\n", "system")
        self.client.send_ai_command(buy_cmd)
        self.client.master.after(
            1500, lambda: self.client.send_ai_command(eat_cmd))
        self.state.hunger_level = None
        self.state.seeking_resource = None
        self.state.current_goal = 'explore'
        self._schedule_tick(self.COMMAND_DELAY_MS * 3)

    def _needs_buffs(self):
        """Return True if any protection buff is missing or has expired."""
        if self._combat_active:
            return False
        now = time.monotonic()
        for buff in ('sanctuary', 'bless', 'armor'):
            if not self._otto_can(buff):
                continue
            if now >= self.state.buff_expires.get(buff, 0):
                return True
        return False

    def _do_buff_sequence(self):
        """
        Apply protection buffs via Otto in sequence, stacking each twice for duration.
        Sequence: sanctuary x2, bless x2, armor x2.
        On first call builds the pending list; subsequent calls send one tell per tick.
        Summons Otto first if he's not present and summon is available.
        """
        self.state.current_goal = 'get_buffs'

        # Build the sequence on first call (empty list = not yet started)
        if not self._buff_sequence_pending:
            for buff in ('sanctuary', 'bless', 'armor'):
                if self._otto_can(buff):
                    for _ in range(self.state.BUFF_STACK_COUNT):
                        self._buff_sequence_pending.append(buff)
            if not self._buff_sequence_pending:
                # No buff capabilities available — nothing to do
                return

            # Summon Otto first if he's not here and summon is available
            if not self._otto_present() and self._otto_can('summon'):
                self.client.append_text(
                    "[AI] Buffs: summoning Otto for pre-adventure protection.\n", "system")
                self._tell_otto('summon')
                self._otto_pending_service = 'buffs'
                self._schedule_tick(self.COMMAND_DELAY_MS * 3)
                return

        # Send the next buff in the sequence
        buff = self._buff_sequence_pending.pop(0)
        self.client.append_text(
            f"[AI] Buffs: requesting '{buff}' from Otto "
            f"({len(self._buff_sequence_pending)} remaining).\n", "system")
        self._tell_otto(buff)
        # Set a provisional expiry once all stacks of this buff have been sent.
        # This will be overwritten with an accurate value on the next score parse.
        if buff not in self._buff_sequence_pending:
            self.state.buff_expires[buff] = time.monotonic() + self.state.BUFF_DURATION_SECS

        if self._buff_sequence_pending:
            self._schedule_tick(self.COMMAND_DELAY_MS * 2)
        else:
            self.client.append_text("[AI] Buff sequence complete.\n", "system")
            self.state.current_goal = 'explore'
            self._schedule_tick(self.COMMAND_DELAY_MS)

    def _reset_exploration_counter(self):
        """Reset the exploration step counter — called when entering combat or hunt mode."""
        self._exploration_steps = 0

    def _get_ai_config(self):
        if not self.client.current_profile:
            return {}
        return self.client.profiles.get(
            self.client.current_profile, {}).get('ai_config', {})

    # ------------------------------------------------------------------
    # Darkness handling
    # ------------------------------------------------------------------

    def _handle_darkness(self):
        """Called when darkness is detected after a move attempt."""
        current = self.client.current_room_hash
        prev = self.client.previous_room_hash
        direction = self._last_direction

        self._waiting_for_room = False

        if current:
            self.state.dark_rooms.add(current)

        # Record which direction from the previous room leads to darkness
        if prev and direction:
            if prev not in self.state.dark_directions:
                self.state.dark_directions[prev] = set()
            self.state.dark_directions[prev].add(direction)
            # Also mark in room_links as None so BFS skips it
            profile = self.client.profiles.get(self.client.current_profile, {})
            room_links = profile.setdefault('room_links', {})
            room_links.setdefault(prev, {})[direction] = None

        self.client.append_text(
            "[AI] Darkness detected — backing up and marking direction.\n", "system")

        # Back up to the previous room if possible
        if direction and self.pathfinder.reverse_direction(direction):
            back = self.pathfinder.reverse_direction(direction)
            self._last_direction = None
            self.client.send_ai_command(back)

        self.save_state()
        self._schedule_tick(self.COMMAND_DELAY_MS)

    def _request_llm_action(self):
        """Ask the LLM for a command when BFS has nothing to offer."""
        if not self.llm.is_available():
            self.state.current_goal = "idle"
            self.client.append_text(
                "[AI] BFS exploration complete. "
                "Add 'ai_config' to your profile to enable LLM suggestions.\n", "system")
            self.save_state()
            return

        # Check model availability on first use (runs in background)
        if not getattr(self, '_llm_checked', False):
            self._llm_checked = True
            cfg = self.llm._config()
            if cfg and cfg.get('llm_backend', 'ollama') == 'ollama':
                def _check():
                    ok, msg = self.llm.check_ollama_model(cfg)
                    tag = "system" if ok else "error"
                    self.client.master.after(
                        0, lambda: self.client.append_text(f"[AI/LLM] {msg}\n", tag))
                    if not ok:
                        self.client.master.after(0, lambda: setattr(self, '_llm_checked', False))
                import threading
                threading.Thread(target=_check, daemon=True).start()
                self._schedule_tick(3000)  # wait for check to finish
                return

        if not self.llm.is_ready():
            # Rate limited — wait and retry
            self._schedule_tick(3000)
            return

        context = self.llm.build_context()
        if context is None:
            self._schedule_tick(2000)
            return

        self._waiting_for_llm = True
        self.client.append_text("[AI/LLM] Asking LLM for next action...\n", "system")
        self.llm.request_action(context, self._on_llm_result)

    def _on_llm_result(self, command):
        """Callback on main thread when LLM returns a command."""
        self._waiting_for_llm = False
        if not self.is_running:
            return
        if command:
            cmd_lower = command.lower().strip()
            # Reject movement suggestions that are already confirmed dead ends
            current = self.client.current_room_hash
            if current and cmd_lower in self.client.movement_commands:
                profile = self.client.profiles.get(self.client.current_profile, {})
                room_links = profile.get('room_links', {})
                dead_ends = self.state.dead_ends.get(current, set())
                is_dead = (room_links.get(current, {}).get(cmd_lower) is None
                           and cmd_lower in room_links.get(current, {}))
                is_blocked = cmd_lower in dead_ends
                if is_dead or is_blocked:
                    # Don't hard-reject — the LLM may know how to get through
                    # (locked door, item needed, etc.).  Let it try.  If it keeps
                    # failing, the outcome will appear in recent_actions next call.
                    self.client.append_text(
                        f"[AI/LLM] Attempting '{cmd_lower}' (previously impassable).\n",
                        "system")
            self._llm_ignored_count = 0
            self.client.append_text(f"[AI/LLM] Suggested: {command!r}\n", "system")

            # Learn resource locations from LLM-suggested commands
            current = self.client.current_room_hash
            if current:
                if cmd_lower.startswith('drink '):
                    if current not in self.state.water_sources:
                        self.state.water_sources[current] = cmd_lower
                        self.client.append_text(
                            f"[AI] Learned water source from LLM: '{cmd_lower}'\n", "system")
                        if self.state.seeking_resource == 'water':
                            self.state.seeking_resource = None
                elif re.match(r'^(?:eat|buy)\s+\S', cmd_lower):
                    if not self.state.food_room:
                        self.state.food_room = current
                        self.client.append_text(
                            f"[AI] Learned food source from LLM: '{cmd_lower}'\n", "system")
                        if self.state.seeking_resource == 'food':
                            self.state.seeking_resource = None

            # If it's a movement command, use the normal move path
            if cmd_lower in self.client.movement_commands:
                self._execute_move(cmd_lower)
            else:
                # Route 'tell otto <service>' through _tell_otto so capability tracking works
                otto_match = re.match(r'^tell\s+otto\s+(.+)', cmd_lower)
                if otto_match:
                    self._tell_otto(otto_match.group(1).strip())
                else:
                    # Non-movement command (examine, open, say, etc.)
                    self.client.send_ai_command(command)
        else:
            self.client.append_text("[AI/LLM] No command returned — retrying later.\n", "system")
        self._schedule_tick(self.COMMAND_DELAY_MS)

    def _npc_danger_rooms(self):
        """Return set of room hashes known to contain NPCs that have killed us."""
        return {
            rec["last_room"]
            for rec in self.state.npc_danger.values()
            if rec.get("deaths", 0) > 0 and rec.get("last_room")
        }

    def _update_entity_db(self, room_hash, mobs):
        """Update mob_db and entity_db in the profile for the current room visit.

        mob_db  — profile-level, keyed by mob name lower; tracks all rooms a mob
                  has been seen in across sessions.
        entity_db — profile-level, keyed by room hash; snapshot of what's present.
        """
        if not room_hash:
            return
        profile = self._profile()
        if profile is None:
            return

        mob_db = profile.setdefault('mob_db', {})
        entity_db = profile.setdefault('entity_db', {})
        now_iso = datetime.now(timezone.utc).isoformat()

        for mob in mobs:
            mob_lower = mob.lower()
            if mob_lower not in mob_db:
                mob_db[mob_lower] = {
                    "display_name": mob,
                    "rooms_seen": {},
                    "total_sightings": 0,
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                    "last_room": room_hash,
                    "is_wanderer": False,
                }
            entry = mob_db[mob_lower]
            entry["last_seen"] = now_iso
            entry["last_room"] = room_hash
            entry["total_sightings"] = entry.get("total_sightings", 0) + 1
            rooms_seen = entry.setdefault("rooms_seen", {})
            rooms_seen[room_hash] = rooms_seen.get(room_hash, 0) + 1
            if len(rooms_seen) >= 2:
                entry["is_wanderer"] = True

        room_entry = entity_db.setdefault(room_hash, {
            "mob_names": [],
            "items_on_ground": [],
            "features": [],
            "area_theme": None,
        })
        room_entry["mob_names"] = [m.lower() for m in mobs]

    def _find_rooms_with_untried_exits(self):
        """
        Return a set of room hashes that have exits reported by the MUD
        which have never been traversed (absent from room_links for that room).
        These are candidates for further exploration even when the BFS frontier
        appears empty.
        """
        if not self.client.current_profile:
            return set()
        profile = self.client.profiles.get(self.client.current_profile, {})
        rooms = profile.get('rooms', {})
        room_links = profile.get('room_links', {})
        result = set()
        for room_hash, room_data in rooms.items():
            # Collision-zone rooms are handled via position tracking, not hash BFS
            if room_hash in self._collision_hashes:
                continue
            exits_text = room_data.get('exits', '')
            reported = self.pathfinder.parse_exits_text(exits_text)
            tried = (set(room_links.get(room_hash, {}).keys())
                     | self.state.dead_ends.get(room_hash, set())
                     | self.state.dark_directions.get(room_hash, set()))
            if reported - tried:
                result.add(room_hash)
        return result

    def _enqueue_unvisited_neighbors(self, room_hash):
        """Add known unvisited neighbors to the frontier queue."""
        if not self.client.current_profile:
            return
        profile = self.client.profiles.get(self.client.current_profile, {})
        room_links = profile.get('room_links', {})
        npc_danger_rooms = self._npc_danger_rooms()
        for direction, dest in room_links.get(room_hash, {}).items():
            if dest is not None and dest not in self.state.visited:
                if dest in npc_danger_rooms:
                    continue  # don't explore rooms with deadly NPCs
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

    def _record_mud_time_obs(self, mud_hour):
        """
        Record a MUD time observation and recalibrate seconds-per-mud-hour.

        Called each time the 'time' command response is parsed.  Observations
        accumulate within the session; once at least two are separated by real
        time AND the MUD hour has advanced, we calculate a calibrated value and
        persist it so future sessions inherit accurate buff expiry times.
        """
        now = time.monotonic()
        self._mud_time_obs.append((now, mud_hour))
        if len(self._mud_time_obs) > 20:
            self._mud_time_obs = self._mud_time_obs[-20:]

        # Need at least 2 observations to estimate the rate
        if len(self._mud_time_obs) < 2:
            return

        # Collect rate estimates from adjacent pairs where MUD time has advanced
        # and enough real time has elapsed to give a meaningful ratio.
        estimates = []
        for i in range(len(self._mud_time_obs) - 1):
            t1, h1 = self._mud_time_obs[i]
            t2, h2 = self._mud_time_obs[i + 1]
            dt_real = t2 - t1
            dt_mud = (h2 - h1) % 24
            if dt_mud > 0 and dt_real >= 60:
                estimates.append(dt_real / dt_mud)

        if not estimates:
            return

        mud_hour_secs = sum(estimates) / len(estimates)
        self.state.TICK_SECS = mud_hour_secs

        # Persist so the calibration survives logout
        profile = self._profile()
        if profile is not None:
            cal = profile.setdefault('mud_time_calibration', {})
            cal['mud_hour_secs'] = mud_hour_secs
            cal['sample_count'] = len(estimates)
            self.client.save_profiles()
            self.client.append_text(
                f"[AI] MUD time calibrated: {mud_hour_secs:.1f}s/mud-hour "
                f"(~{mud_hour_secs / 60:.1f}min/hour, {len(estimates)} samples)\n",
                "system")

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
