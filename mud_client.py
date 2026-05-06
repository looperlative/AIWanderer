#!/usr/bin/env python3
# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""
MUD Client with SSL Support
A simple GUI-based MUD client that connects via SSL and displays received text.
"""

import socket
import ssl
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk, simpledialog, font as tkfont
import queue
import re
import json
import os
import time
import hashlib
from collections import deque
from ai_agent import DIRECTION_ABBREVS


def _link_dest(val):
    """Return (dest_hash, assumed) from a room_link value (str or dict)."""
    if isinstance(val, dict):
        return val.get('dest'), val.get('assumed', False)
    return val, False  # legacy plain-string → treat as confirmed

def _mark_death_trap(profile, from_room, direction):
    """Record a death-trap flag on the room_links entry for from_room→direction."""
    room_links = profile.setdefault('room_links', {})
    existing = room_links.setdefault(from_room, {}).get(direction)
    if isinstance(existing, dict):
        existing['death_trap'] = True
    else:
        room_links[from_room][direction] = {'death_trap': True}


_EXIT_DIR_RE = re.compile(r'\[ Exits: (.*?) \]')

_REVERSE_DIR = {
    'north': 'south', 'south': 'north',
    'east':  'west',  'west':  'east',
    'up':    'down',  'down':  'up',
}

from mud_parser import MUDTextParser


DEFAULT_SKILL_CFG = {
    "instructions": (
        "You are running as the default ambient agent for this character.\n"
        "You have no fixed plan — use your judgment each turn based on the MUD output and stats.\n\n"
        "Priorities (in order):\n"
        "1. If a character addresses you by name in a tell or say, respond appropriately and\n"
        "   carry out any reasonable request they make.\n"
        "2. If a user instruction is provided (prefixed [User instruction:]), follow it.\n"
        "3. If thirst is high (>= 8) or hunger is high (>= 8), find and consume food or water.\n"
        "4. If HP is below 80% of max and you are not in combat, seek healing (e.g. tell otto heal,\n"
        "   or use a healing skill/item).\n"
        "5. If you are not in town and not in combat, return to town (use the appropriate speedwalk\n"
        "   or 'recall' command if available).\n"
        "6. If everything is fine, do nothing — return an empty commands list.\n\n"
        "Never set complete to true. This skill runs indefinitely until the user starts another skill."
    ),
    "watch_stats": ["hp", "max_hp", "hunger", "thirst"],
}


class MUDClient:
    def __init__(self, master):
        self.master = master
        self.master.title("MUD Client - SSL Connection")

        # Shared parser instance
        self.mud_parser = MUDTextParser()

        # Connection state
        self.socket = None
        self.ssl_socket = None
        self.connected = False
        self.receive_thread = None
        self.message_queue = queue.Queue()
        self._keepalive_job = None

        # Profile management
        self.profiles_file = os.path.join(os.path.expanduser("~"), ".mud_client_profiles.json")
        self.profiles = self.load_profiles()
        self.current_profile = None

        # Apply saved window geometry (or default) before widgets are built so
        # the WM honors the requested position on first map. Geometry lives in
        # the host-local UI config since positions/sizes are display-specific.
        ui_local = self._load_ui_local()
        saved_geometry = ui_local.get('window_geometry')
        # Migrate legacy value from shared profiles._settings if present.
        legacy = self.profiles.get('_settings', {}).pop('window_geometry', None)
        if saved_geometry is None and legacy:
            saved_geometry = legacy
            ui_local['window_geometry'] = legacy
            self._save_ui_local(ui_local)
        if legacy:
            self.save_profiles()
        try:
            self.master.geometry(saved_geometry or "900x600")
        except tk.TclError:
            self.master.geometry("900x600")

        # Autologin state
        self.autologin_pending = False
        self.autologin_stage = 0
        self.last_line = ""  # Track last non-empty line for prompt learning
        self.triggered_once_responses = set()  # Track which run_once responses have fired this session

        # Quit sequence state
        self.quit_pending = False
        self.quit_stage = 0
        self.quit_prompts_seen = []  # Track prompts seen during this quit sequence

        # Room mapping state
        self.room_tracking_enabled = False
        self.last_command = ""
        self.last_movement_direction = None  # Track the direction of movement
        self.expecting_room_data = False
        self._expecting_room_description = False  # set by GMCP after identity is known; cleared by text parser
        self._pending_desc_segments = []          # accumulates text chunks while waiting for description
        self._skill_trigger_pending = False   # defer skill turn until room data lands
        self._room_wait_timeout_id = None     # after() handle for the room-wait safety timer
        self._refused_direction: str | None = None  # set when a move direction is rejected
        self._explore_arrived_info: dict | None = None  # persists arrive msg until exit walked
        self.room_color = None  # Will store the detected room name color
        # Rolling buffer of raw decoded text (ANSI codes intact) for the
        # color calibration UI — lets the dialog show actual MUD colors.
        self._raw_ansi_lines = deque(maxlen=500)
        self.current_room_hash = None  # Hash of the current room
        self.previous_room_hash = None  # Hash of the previous room
        self.detect_entry_room = False  # Flag to detect entry room after login
        self.gmcp_active = False  # True after IAC WILL GMCP / IAC DO GMCP handshake
        self._telnet_recv_buf = bytearray()  # incomplete telnet sequences carried across recv() calls
        self.movement_commands = ['n', 'north', 's', 'south', 'e', 'east',
                                   'w', 'west', 'u', 'up', 'd', 'down', 'l', 'look']
        # Map short commands to directions (canonical names from ai_agent)
        self.direction_map = dict(DIRECTION_ABBREVS)
        self.direction_map.update({'l': 'look', 'look': 'look'})

        # AI agent state
        self.ai_agent = None

        # LLM advisor state
        self.llm_advisor = None

        # LLM skill engine state
        self.skill_engine = None
        self._skill_rescue_flag = False  # set by rescue path, consumed on next prompt
        self._skill_target_killed = False  # set when a kill line fires during active skill
        self._pending_command = None   # last human command sent (awaiting MUD response)
        self._response_buffer = []     # MUD lines received since last command
        self._active_goto = None       # {"target": str, "dest": room_key} while navigating
        self._active_explore = None    # {"dest": room_key, "exit_info": dict} while exploring
        self._advisor_streamed = False # True if current response was already streamed to UI
        self._advisor_stream_start = None  # text index where streaming body began

        # Session logging
        from session_logger import SessionLogger
        self.session_logger = SessionLogger()

        # Command history (within session)
        self._cmd_history = []
        self._cmd_history_pos = -1   # -1 = not browsing

        # Command frequency scores (persistent, exponential decay per session)
        self._cmd_scores = self.profiles.get('_settings', {}).get('cmd_scores', {})
        self._decay_cmd_scores()

        # Font size (loaded from settings, default 11)
        self._font_size = self.profiles.get('_settings', {}).get('font_size', 11)

        # Character status (updated from MUD output)
        self.char_stats = {}

        # Auto-score state
        self._suppress_score_output = False
        self._auto_score_job = None

        # Tick timer state
        self._tick_interval = None  # known tick period (seconds, multiple of 5)
        self._tick_count    = None  # seconds counted since last reset (None = not started)
        self._tick_countdown_job = None      # after() job id for 1-s countdown updates

        # Survival (food/drink) automation state
        self._prev_hunger = None          # Last hunger level seen (for transition detection)
        self._prev_thirst = None          # Last thirst level seen
        self._survival_state = None       # None | 'walking' | 'inv_wait' | 'buying'
        self._survival_path = []          # Remaining direction steps when walking to store
        self._survival_buy_count = 0      # Buy commands still to issue
        self._survival_inv_text = ''      # Buffered inventory output

        # Group member tracking (receive-thread state; used by skill engine)
        self.group_members = set()         # Lowercase names of PCs currently in our group
        self._group_leader = None          # Lowercase name of the PC who leads our group

        # Mob combat stat tracking (receive-thread state)
        self._combat_mob = None           # Normalised mob name currently fighting us
        self._rescue_sent = False         # True after rescue command sent this combat
        self._kill_cmd_pending = False    # Player sent kill/k command
        self._kill_cmd_target = None      # Target typed by player
        self._last_kill_cmd_time = 0.0
        self._prev_combat_hp = None       # HP before last round for damage calc
        self._last_killed_mob = None      # Mob key awaiting XP attribution

        self.setup_ui()

        self.master.after(100, self.process_queue)

    def strip_ansi_codes(self, text):
        """Remove ANSI escape sequences from text (for prompt detection)"""
        ansi_pattern = re.compile(r'\x1b\[[0-9;]*m')
        return ansi_pattern.sub('', text)

    def normalize_prompt(self, text):
        """Normalize prompts by replacing numeric values with wildcards

        This allows prompts with stats like '24H 100M 85V 0%T 0%O >' to match
        regardless of the actual stat values.
        """
        # Replace sequences of digits with '#' to create a pattern
        # E.g., "24H 100M 85V 0%T 0%O >" becomes "#H #M #V #%T #%O >"
        normalized = re.sub(r'\d+', '#', text)
        return normalized.strip()

    # ANSI foreground color map: basic 8 + bright 8
    _ANSI_COLORS = {
        30: "#000000",  # Black
        31: "#cd3131",  # Red
        32: "#0dbc79",  # Green
        33: "#e5e510",  # Yellow
        34: "#2472c8",  # Blue
        35: "#bc3fbc",  # Magenta
        36: "#11a8cd",  # Cyan
        37: "#e5e5e5",  # White
        90: "#666666",  # Bright Black (Gray)
        91: "#f14c4c",  # Bright Red
        92: "#23d18b",  # Bright Green
        93: "#f5f543",  # Bright Yellow
        94: "#3b8eea",  # Bright Blue
        95: "#d670d6",  # Bright Magenta
        96: "#29b8db",  # Bright Cyan
        97: "#ffffff",  # Bright White
    }

    _ANSI_DEFAULT_COLOR = "#d4d4d4"

    @staticmethod
    def _ansi256_to_hex(n):
        """Convert a 256-color palette index (0-255) to a hex color string."""
        if n < 8:
            palette = [0x000000, 0xcd3131, 0x0dbc79, 0xe5e510,
                       0x2472c8, 0xbc3fbc, 0x11a8cd, 0xe5e5e5]
            v = palette[n]
        elif n < 16:
            palette = [0x666666, 0xf14c4c, 0x23d18b, 0xf5f543,
                       0x3b8eea, 0xd670d6, 0x29b8db, 0xffffff]
            v = palette[n - 8]
            return f'#{v:06x}'
        elif n < 232:
            # 6×6×6 colour cube
            n -= 16
            r = (n // 36) * 51
            g = ((n // 6) % 6) * 51
            b = (n % 6) * 51
            return f'#{r:02x}{g:02x}{b:02x}'
        else:
            # Grayscale ramp
            v = (n - 232) * 10 + 8
            return f'#{v:02x}{v:02x}{v:02x}'
        return f'#{v:06x}'

    def parse_ansi_text(self, text):
        """
        Parse ANSI escape sequences and return list of (text, color) tuples.

        Handles:
        - Basic 16 colors (codes 30-37, 90-97)
        - 256-color foreground (ESC[38;5;Nm)
        - Truecolor / 24-bit foreground (ESC[38;2;R;G;Bm)
        - Reset (ESC[0m or ESC[m)
        - Bold and other non-color attributes are ignored
        """
        result = []
        current_color = self._ANSI_DEFAULT_COLOR
        current_text = ""

        ansi_pattern = re.compile(r'\x1b\[([0-9;]*)m')
        last_pos = 0

        for match in ansi_pattern.finditer(text):
            # Accumulate text before this escape sequence
            if match.start() > last_pos:
                current_text += text[last_pos:match.start()]

            codes_str = match.group(1)
            # Parse code list; filter out empty strings from trailing semicolons
            codes = [int(c) for c in codes_str.split(';') if c.isdigit()]
            if not codes:
                codes = [0]  # bare ESC[m = reset

            i = 0
            while i < len(codes):
                code = codes[i]
                if code == 0:
                    # Reset
                    if current_text:
                        result.append((current_text, current_color))
                        current_text = ""
                    current_color = self._ANSI_DEFAULT_COLOR
                    i += 1
                elif code in (38, 48):
                    # Extended color: 38=foreground, 48=background
                    if i + 1 < len(codes) and codes[i + 1] == 5:
                        # 256-color: 38;5;N
                        if i + 2 < len(codes):
                            if code == 38:
                                if current_text:
                                    result.append((current_text, current_color))
                                    current_text = ""
                                current_color = self._ansi256_to_hex(codes[i + 2])
                            i += 3
                        else:
                            i += 1
                    elif i + 1 < len(codes) and codes[i + 1] == 2:
                        # Truecolor: 38;2;R;G;B
                        if i + 4 < len(codes):
                            if code == 38:
                                if current_text:
                                    result.append((current_text, current_color))
                                    current_text = ""
                                r, g, b = codes[i+2], codes[i+3], codes[i+4]
                                current_color = f'#{r:02x}{g:02x}{b:02x}'
                            i += 5
                        else:
                            i += 1
                    else:
                        i += 1
                elif code in self._ANSI_COLORS:
                    if current_text:
                        result.append((current_text, current_color))
                        current_text = ""
                    current_color = self._ANSI_COLORS[code]
                    i += 1
                else:
                    # Bold, italic, underline, background colors, etc. — ignore
                    i += 1

            last_pos = match.end()

        # Remaining text after the last escape sequence
        if last_pos < len(text):
            current_text += text[last_pos:]
        if current_text:
            result.append((current_text, current_color))

        return result

    def filter_telnet_sequences(self, data):
        """Filter TELNET protocol sequences and log them"""
        # Prepend any bytes left over from a split sequence in the previous chunk
        if self._telnet_recv_buf:
            data = bytes(self._telnet_recv_buf) + data
            self._telnet_recv_buf.clear()

        # TELNET commands start with IAC (0xFF)
        IAC = 0xFF  # Interpret As Command
        WILL = 0xFB
        WONT = 0xFC
        DO = 0xFD
        DONT = 0xFE
        SB = 0xFA  # Subnegotiation Begin
        SE = 0xF0  # Subnegotiation End
        GMCP = 0xC9  # GMCP option (201)

        telnet_commands = {
            0xFB: "WILL",
            0xFC: "WONT",
            0xFD: "DO",
            0xFE: "DONT",
            0xFA: "SB",
            0xF0: "SE",
            0xF1: "NOP",
            0xF2: "DATA_MARK",
            0xF3: "BREAK",
            0xF4: "IP",
            0xF5: "AO",
            0xF6: "AYT",
            0xF7: "EC",
            0xF8: "EL",
            0xF9: "GA",
        }

        telnet_options = {
            0: "BINARY",
            1: "ECHO",
            3: "SUPPRESS_GO_AHEAD",
            5: "STATUS",
            6: "TIMING_MARK",
            24: "TERMINAL_TYPE",
            31: "WINDOW_SIZE",
            32: "TERMINAL_SPEED",
            33: "REMOTE_FLOW_CONTROL",
            34: "LINEMODE",
            36: "ENVIRONMENT_VARIABLES",
            85: "COMPRESS",
            86: "COMPRESS2",
            200: "MCCP",
            201: "GMCP",
        }

        result = bytearray()
        i = 0

        while i < len(data):
            if data[i] == IAC and i + 1 >= len(data):
                # IAC is the very last byte — save it and wait for the next chunk
                self._telnet_recv_buf.extend(data[i:])
                break

            if data[i] == IAC and i + 1 < len(data):
                cmd = data[i + 1]

                # Handle subnegotiation
                if cmd == SB and i + 2 >= len(data):
                    # SB without option byte yet — save and wait
                    self._telnet_recv_buf.extend(data[i:])
                    break

                if cmd == SB and i + 2 < len(data):
                    # Find the end of subnegotiation (IAC SE)
                    end = i + 2
                    found_se = False
                    while end < len(data) - 1:
                        if data[end] == IAC and data[end + 1] == SE:
                            found_se = True
                            break
                        end += 1

                    if not found_se:
                        # Packet is split — save from IAC SB onward and wait for next chunk
                        self._telnet_recv_buf.extend(data[i:])
                        break

                    option = data[i + 2] if i + 2 < len(data) else 0
                    sb_data = data[i+3:end]

                    if option == GMCP:
                        # Parse GMCP: "<Module.Name> <JSON>"
                        try:
                            payload = sb_data.decode('utf-8', errors='replace')
                            sp = payload.find(' ')
                            if sp != -1:
                                module = payload[:sp].strip()
                                json_str = payload[sp+1:].strip()
                            else:
                                module = payload.strip()
                                json_str = '{}'
                            self._handle_gmcp_packet(module, json_str)
                        except Exception as e:
                            self.message_queue.put(("telnet", f"GMCP parse error: {e}"))
                    else:
                        option_name = telnet_options.get(option, f"UNKNOWN({option})")
                        self.message_queue.put(("telnet", f"TELNET: Subnegotiation for {option_name} (data length: {len(sb_data)})"))
                    i = end + 2
                    continue

                # Handle 3-byte commands (WILL, WONT, DO, DONT)
                if cmd in [WILL, WONT, DO, DONT] and i + 2 >= len(data):
                    self._telnet_recv_buf.extend(data[i:])
                    break

                if cmd in [WILL, WONT, DO, DONT] and i + 2 < len(data):
                    option = data[i + 2]
                    cmd_name = telnet_commands.get(cmd, f"UNKNOWN({cmd})")
                    option_name = telnet_options.get(option, f"UNKNOWN({option})")
                    self.message_queue.put(("telnet", f"TELNET: {cmd_name} {option_name}"))
                    # GMCP negotiation: server offers GMCP, we accept
                    if cmd == WILL and option == GMCP:
                        try:
                            self.ssl_socket.sendall(bytes([IAC, DO, GMCP]))
                            self.gmcp_active = True
                            self.message_queue.put(("telnet", "[GMCP] Negotiated"))
                        except Exception as e:
                            self.message_queue.put(("telnet", f"[GMCP] Negotiate error: {e}"))
                    i += 3
                    continue

                # Handle 2-byte commands
                cmd_name = telnet_commands.get(cmd, f"UNKNOWN({cmd})")
                if cmd in telnet_commands:
                    self.message_queue.put(("telnet", f"TELNET: {cmd_name}"))
                i += 2
            else:
                result.append(data[i])
                i += 1

        return bytes(result)

    def _handle_gmcp_packet(self, module, json_str):
        """Dispatch an incoming GMCP packet (called from receive thread).

        Handles Room.Info, Char.Vitals, and Char.Status.  All other modules
        are silently ignored — they are already stripped from the display stream.
        """
        try:
            data = json.loads(json_str)
        except Exception:
            return

        if module == "Room.Info":
            # Convert exits dict {"n": true, "s": false, ...} to "[ Exits: n s ]"
            exits_dict = data.get("exits", {})
            open_dirs = [d for d, open_ in exits_dict.items() if open_]
            exits_str = "[ Exits: " + " ".join(open_dirs) + " ]" if open_dirs else "[ Exits: none ]"
            room_info = {
                "num":     data.get("num"),
                "name":    data.get("name", ""),
                "zone":    data.get("zone", ""),
                "terrain": data.get("terrain", ""),
                "exits":   exits_str,
            }
            self.message_queue.put(("gmcp_room", room_info))

        elif module == "Char.Vitals":
            updates = {}
            for gmcp_key, stat_key in (
                ("hp",    "hp"),
                ("hpmax", "max_hp"),
                ("mp",    "mp"),
                ("mpmax", "max_mp"),
                ("mv",    "mv"),
                ("mvmax", "max_mv"),
                ("gold",  "gold"),
            ):
                if gmcp_key in data:
                    try:
                        updates[stat_key] = int(data[gmcp_key])
                    except (TypeError, ValueError):
                        pass
            if "hungry" in data:
                try:
                    v = int(data["hungry"])
                    updates["hunger"] = 'OK' if v >= 8 else ('starving' if v == 0 else 'hungry')
                except (TypeError, ValueError):
                    pass
            if "thirsty" in data:
                try:
                    v = int(data["thirsty"])
                    updates["thirst"] = 'OK' if v >= 8 else ('parched' if v == 0 else 'thirsty')
                except (TypeError, ValueError):
                    pass
            if updates:
                self.message_queue.put(("stats", updates))

        elif module == "Char.Status":
            updates = {}
            for gmcp_key, stat_key in (
                ("level",   "level"),
                ("xp",      "xp"),
                ("xp_next", "xp_next"),
                ("ac",      "ac"),
            ):
                if gmcp_key in data:
                    try:
                        updates[stat_key] = int(data[gmcp_key])
                    except (TypeError, ValueError):
                        pass
            # GMCP xp_next is the total XP threshold; convert to additional XP needed
            if "xp_next" in updates and "xp" in updates:
                updates["xp_next"] = max(0, updates["xp_next"] - updates["xp"])
            if "class" in data:
                updates["class_name"] = data["class"]
            if "align" in data:
                updates["alignment"] = data["align"]
            if "alignnum" in data:
                updates["alignment_num"] = data["alignnum"]
            if updates:
                self.message_queue.put(("stats", updates))

        elif module == "Char.Defences.List":
            # Full snapshot of active spell affects at login/reconnect.
            # data is a list; each element has "name" and "remaining" (mud hours, -1=permanent).
            if isinstance(data, list):
                spells = {e["name"]: e.get("remaining", 0)
                          for e in data if isinstance(e, dict) and "name" in e}
                self.message_queue.put(("stats", {"spells": spells}))

        elif module == "Char.Defences.Add":
            # Single spell just applied (first instance of that spell type).
            if isinstance(data, dict) and "name" in data:
                self.message_queue.put(("stats", {
                    "spells_add": {data["name"]: data.get("remaining", 0)}
                }))

        elif module == "Char.Defences.Remove":
            # Spell removed — payload is a plain JSON string (e.g. "armor").
            if isinstance(data, str) and data:
                self.message_queue.put(("stats", {"spells_expired": [data]}))

        # Core.Hello, Char.StatusVars, Char.Items.*, Comm.Channel.Text — ignored

    def load_profiles(self):
        """Load profiles from JSON file"""
        if os.path.exists(self.profiles_file):
            try:
                with open(self.profiles_file, 'r') as f:
                    data = json.load(f)
                    # Ensure the structure has a _settings key for app settings
                    if '_settings' not in data:
                        data['_settings'] = {}
                    return data
            except Exception as e:
                messagebox.showerror(
                    "Profile Load Error",
                    f"Could not load profiles from:\n{self.profiles_file}\n\n"
                    f"Error: {e}\n\n"
                    "Fix or remove the file and restart the application."
                )
                sys.exit(1)
        return {'_settings': {}}

    def save_profiles(self):
        """Save profiles to JSON file"""
        try:
            with open(self.profiles_file, 'w') as f:
                json.dump(self.profiles, f, indent=2)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save profiles: {e}")

    def save_last_profile(self, profile_name):
        """Save the last connected profile name"""
        if '_settings' not in self.profiles:
            self.profiles['_settings'] = {}
        self.profiles['_settings']['last_profile'] = profile_name
        self.save_profiles()

    def get_last_profile(self):
        """Get the last connected profile name"""
        return self.profiles.get('_settings', {}).get('last_profile', None)

    def setup_ui(self):
        """Setup the user interface"""
        # ── Shared font objects (updating these resizes all widgets at once) ──
        self._font_main   = tkfont.Font(family="Courier", size=self._font_size)
        self._font_status = tkfont.Font(family="Courier", size=self._font_size - 1)
        self._font_status_hdr = tkfont.Font(family="Courier", size=self._font_size - 1,
                                            weight="bold")

        # ── Menu bar ──────────────────────────────────────────────────
        menubar = tk.Menu(self.master)
        self.master.config(menu=menubar)

        # Connection menu
        self._conn_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Connection", menu=self._conn_menu)

        # Profile sub-menu
        self._profile_menu = tk.Menu(self._conn_menu, tearoff=0)
        self._conn_menu.add_cascade(label="Profile", menu=self._profile_menu)
        self._conn_menu.add_separator()
        self._conn_menu.add_command(label="Connect", command=self.toggle_connection,
                                    state=tk.NORMAL)
        self._conn_menu.add_separator()
        self._conn_menu.add_command(label="Send Character Name",
                                    command=self.send_character_name, state=tk.DISABLED)
        self._conn_menu.add_command(label="Send Password",
                                    command=self.send_password, state=tk.DISABLED)
        self._conn_menu.add_command(label="Send & Remember",
                                    command=self.send_and_remember, state=tk.DISABLED)
        self._conn_menu.add_command(label="Quit MUD",
                                    command=self.start_quit_sequence, state=tk.DISABLED)
        self._conn_menu.add_separator()
        self._conn_menu.add_command(label="Exit", command=self.master.quit)

        # Settings menu
        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        self.room_tracking_var = tk.BooleanVar(value=False)
        settings_menu.add_checkbutton(label="Room Tracking",
                                      variable=self.room_tracking_var,
                                      command=self.toggle_room_tracking)
        settings_menu.add_separator()

        # Autoloot submenu
        self._autoloot_var = tk.StringVar(value=self.profiles.get('_settings', {}).get('autoloot', 'off'))
        autoloot_menu = tk.Menu(settings_menu, tearoff=0)
        settings_menu.add_cascade(label="Autoloot", menu=autoloot_menu)
        for opt in ('off', 'gold', 'all'):
            autoloot_menu.add_radiobutton(label=opt.capitalize(),
                                          variable=self._autoloot_var,
                                          value=opt,
                                          command=self._save_autoloot)

        settings_menu.add_separator()

        # Survival submenu
        survival_menu = tk.Menu(settings_menu, tearoff=0)
        settings_menu.add_cascade(label="Survival", menu=survival_menu)
        survival_menu.add_command(label="Set Drink Container...",
                                  command=self._survival_set_drink_container)
        survival_menu.add_command(label="Set Food Item...",
                                  command=self._survival_set_food_item)
        survival_menu.add_separator()
        survival_menu.add_command(label="Set Fountain Room",
                                  command=self._survival_set_fountain_room)
        survival_menu.add_command(label="Set Food Store Room",
                                  command=self._survival_set_food_store_room)
        survival_menu.add_separator()
        survival_menu.add_command(label="Buy Food Now",
                                  command=self._survival_buy_food)
        survival_menu.add_separator()
        survival_menu.add_command(label="Rescue Settings...",
                                  command=self._rescue_settings_dialog)

        settings_menu.add_separator()
        settings_menu.add_command(label="AI Config...", command=self.open_ai_config)
        settings_menu.add_command(label="Room Colors...", command=self.open_color_calibration)

        # Skills submenu
        self._skills_menu = tk.Menu(settings_menu, tearoff=0,
                                    postcommand=self._rebuild_skills_menu)
        settings_menu.add_cascade(label="Skills", menu=self._skills_menu)

        # Commands menu (top 20 by frequency score)
        self._cmd_menu = tk.Menu(menubar, tearoff=0,
                                 postcommand=self._rebuild_cmd_menu)
        menubar.add_cascade(label="Commands", menu=self._cmd_menu)

        # View menu
        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="View", menu=view_menu)
        view_menu.add_command(label="Clear MUD Output", command=self.clear_output)
        view_menu.add_command(label="Clear Advisor", command=self.clear_advisor)
        view_menu.add_separator()
        view_menu.add_command(label="Mob Stats...", command=self._show_mob_stats_dialog)
        view_menu.add_separator()
        view_menu.add_command(label="Larger Text  (Ctrl++)", command=self._zoom_in)
        view_menu.add_command(label="Smaller Text (Ctrl+-)", command=self._zoom_out)
        view_menu.add_command(label="Reset Text Size (Ctrl+0)", command=self._zoom_reset)

        # Profile vars (profile combo is now in a menu)
        self.profile_var = tk.StringVar()

        # ── Main content area (MUD pane left, status panel right) ─────
        content_frame = tk.Frame(self.master)
        content_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=(2, 0))

        # ── PanedWindow: MUD output (top) + Advisor (bottom) ──────────
        paned = tk.PanedWindow(content_frame, orient=tk.VERTICAL, sashrelief=tk.RAISED,
                               sashwidth=6, bg="#3c3c3c")
        paned.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.text_area = scrolledtext.ScrolledText(
            paned,
            wrap=tk.WORD,
            font=self._font_main,
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white"
        )
        self.text_area.config(state=tk.DISABLED)
        paned.add(self.text_area, stretch="always", minsize=80)

        # ── Bottom pane: advisor (left) + navigation (right) ─────────
        bottom_paned = tk.PanedWindow(paned, orient=tk.HORIZONTAL, sashrelief=tk.RAISED,
                                      sashwidth=5, bg="#3c3c3c")
        paned.add(bottom_paned, stretch="always", minsize=60)

        self.advisor_area = scrolledtext.ScrolledText(
            bottom_paned,
            wrap=tk.WORD,
            font=self._font_main,
            bg="#1a1a2e",
            fg="#d4d4d4",
            insertbackground="white"
        )
        self.advisor_area.config(state=tk.DISABLED)
        bottom_paned.add(self.advisor_area, stretch="always", minsize=60)

        nav_frame = tk.Frame(bottom_paned, bg="#1a1a1a")
        bottom_paned.add(nav_frame, stretch="always", minsize=80)
        self._bottom_paned = bottom_paned
        self._build_nav_panel(nav_frame)

        # Set initial sash positions after window draws
        self.master.after(100, lambda: paned.sash_place(0, 0,
            int(self.master.winfo_height() * 0.70)))
        self.master.after(150, self._init_nav_sash)

        # ── Right-hand column: tick timer (top) + status panel (bottom) ──
        right_col = tk.Frame(content_frame, bg="#1a1a1a", width=190)
        right_col.pack(side=tk.RIGHT, fill=tk.Y, padx=(3, 0))
        right_col.pack_propagate(False)

        tick_frame = tk.Frame(right_col, bg="#1a1a1a")
        tick_frame.pack(fill=tk.X)
        self._build_tick_panel(tick_frame)

        tk.Frame(right_col, bg="#3a3a3a", height=1).pack(fill=tk.X, pady=(4, 0))

        status_frame = tk.Frame(right_col, bg="#1a1a1a")
        status_frame.pack(fill=tk.BOTH, expand=True)
        self._build_status_panel(status_frame)

        # ── Status bar ────────────────────────────────────────────────
        status_bar = ttk.Frame(self.master, relief=tk.SUNKEN)
        status_bar.pack(fill=tk.X, padx=4, pady=(0, 2))
        self._status_conn_label = ttk.Label(status_bar, text="Disconnected",
                                            foreground="red", width=14)
        self._status_conn_label.pack(side=tk.LEFT, padx=6)
        ttk.Separator(status_bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, pady=2)
        self._status_rooms_label = ttk.Label(status_bar, text="", foreground="gray")
        self._status_rooms_label.pack(side=tk.LEFT, padx=6)
        ttk.Separator(status_bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, pady=2)
        self._status_log_label = ttk.Label(status_bar, text="Log: off", foreground="gray")
        self._status_log_label.pack(side=tk.LEFT, padx=6)
        self._status_profile_label = ttk.Label(status_bar, text="", foreground="#4ec9b0")
        self._status_profile_label.pack(side=tk.RIGHT, padx=6)

        # ── Input frame ───────────────────────────────────────────────
        input_frame = ttk.Frame(self.master)
        input_frame.pack(fill=tk.X, padx=4, pady=(0, 4))

        self.input_entry = ttk.Entry(input_frame, font=self._font_main)
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self.input_entry.bind('<Return>', self.send_message)
        self.input_entry.bind('<Up>',   self._history_prev)
        self.input_entry.bind('<Down>', self._history_next)

        # Keep keyboard focus in the entry whenever the main window is active
        self.master.bind_all('<FocusIn>', self._redirect_focus_to_entry)
        self.master.after(100, self.input_entry.focus_set)

        # Font size keyboard shortcuts
        self.master.bind_all('<Control-equal>', lambda e: self._zoom_in())
        self.master.bind_all('<Control-plus>',  lambda e: self._zoom_in())
        self.master.bind_all('<Control-minus>', lambda e: self._zoom_out())
        self.master.bind_all('<Control-0>',     lambda e: self._zoom_reset())

        self.send_btn = ttk.Button(input_frame, text="Send", command=self.send_message,
                                   width=8)
        self.send_btn.pack(side=tk.LEFT)
        self.send_btn.config(state=tk.DISABLED)

        # Initialize profile list after all UI elements are created
        self.update_profile_list()
        self._rebuild_profile_menu()

    def _build_status_panel(self, parent):
        """Build the right-hand character status panel."""
        BG      = "#1a1a1a"
        HDR_FG  = "#4ec9b0"
        VAL_FG  = "#d4d4d4"
        F_HDR   = self._font_status_hdr
        F_LBL   = self._font_status

        def section(text):
            tk.Label(parent, text=f"\u2500 {text} \u2500", bg=BG, fg=HDR_FG,
                     font=F_HDR, anchor='w').pack(fill=tk.X, padx=4, pady=(6, 1))

        def stat_row(label, width=6):
            row = tk.Frame(parent, bg=BG)
            row.pack(fill=tk.X, padx=4, pady=1)
            tk.Label(row, text=f"{label}:", bg=BG, fg=VAL_FG,
                     font=F_LBL, anchor='w', width=width).pack(side=tk.LEFT)
            var = tk.StringVar(value="?")
            lbl = tk.Label(row, textvariable=var, bg=BG, fg=VAL_FG,
                           font=F_LBL, anchor='w')
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
            return var, lbl

        section("VITALS")
        self._sv_hp,   self._lbl_hp   = stat_row("HP")
        self._sv_mana, self._lbl_mana = stat_row("Mana")
        self._sv_mv,   self._lbl_mv   = stat_row("Move")

        section("CHARACTER")
        self._sv_level, _ = stat_row("Level")
        self._sv_ac,    _ = stat_row("AC")
        self._sv_align, _ = stat_row("Align")
        self._sv_xp,    _ = stat_row("XP")
        self._sv_xp_next, _ = stat_row("To Lvl")
        self._sv_gold,  _ = stat_row("Gold")

        section("CONDITION")
        self._sv_hunger, self._lbl_hunger = stat_row("Hunger")
        self._sv_thirst, self._lbl_thirst = stat_row("Thirst")

        section("COMBAT")
        self._sv_fighting, self._lbl_fighting = stat_row("Fight")
        self._sv_tank,     self._lbl_tank     = stat_row("Tank")
        self._sv_opp,      self._lbl_opp      = stat_row("Opp")

        section("SPELLS")
        self._spells_frame = tk.Frame(parent, bg=BG)
        self._spells_frame.pack(fill=tk.X, padx=4)

        self._update_status_panel()

    def _update_status_panel(self):
        """Refresh all status panel labels from self.char_stats."""
        s = self.char_stats
        WARN  = "#f48771"
        OK_FG = "#d4d4d4"
        DIM   = "#666666"
        BLUE  = "#9cdcfe"

        def fmt_stat(cur_key, max_key=None):
            cur = s.get(cur_key)
            if max_key:
                mx = s.get(max_key)
                if cur is None and mx is None:
                    return "?"
                if mx is not None:
                    return f"{cur if cur is not None else '?'}/{mx}"
            return str(cur) if cur is not None else "?"

        # Vitals
        self._sv_hp.set(fmt_stat('hp', 'max_hp'))
        if s.get('max_hp') and s.get('hp') is not None:
            pct = s['hp'] / s['max_hp']
            color = WARN if pct < 0.25 else "#dcdcaa" if pct < 0.5 else OK_FG
        else:
            color = OK_FG
        self._lbl_hp.config(fg=color)

        self._sv_mana.set(fmt_stat('mp', 'max_mp'))
        self._sv_mv.set(fmt_stat('mv', 'max_mv'))

        # Character
        self._sv_level.set(str(s.get('level', '?')))
        self._sv_ac.set(str(s.get('ac', '?')))
        align_text = str(s.get('alignment', '?'))
        align_num = s.get('alignment_num')
        self._sv_align.set(f"{align_text} ({align_num})" if align_num is not None else align_text)
        xp = s.get('xp')
        self._sv_xp.set(f"{xp:,}" if xp is not None else "?")
        xp_next = s.get('xp_next')
        self._sv_xp_next.set(f"{xp_next:,}" if xp_next is not None else "?")
        gold = s.get('gold')
        self._sv_gold.set(f"{gold:,}" if gold is not None else "?")

        # Condition
        hunger = s.get('hunger', 'OK')
        self._sv_hunger.set(hunger)
        self._lbl_hunger.config(fg=WARN if hunger != 'OK' else OK_FG)
        thirst = s.get('thirst', 'OK')
        self._sv_thirst.set(thirst)
        self._lbl_thirst.config(fg=WARN if thirst != 'OK' else OK_FG)

        # Combat
        fighting = s.get('fighting', False)
        self._sv_fighting.set("YES" if fighting else "No")
        self._lbl_fighting.config(fg=WARN if fighting else DIM)
        tank = s.get('tank')
        self._sv_tank.set(f"{tank}%" if tank is not None else "-")
        self._lbl_tank.config(fg=(WARN if tank is not None and tank < 30 else OK_FG)
                               if tank is not None else DIM)
        opp = s.get('opp')
        self._sv_opp.set(f"{opp}%" if opp is not None else "-")
        self._lbl_opp.config(fg=OK_FG if opp is not None else DIM)

        # Spells
        for w in self._spells_frame.winfo_children():
            w.destroy()
        spells = s.get('spells', {})
        if spells:
            for spell, ticks in sorted(spells.items()):
                row = tk.Frame(self._spells_frame, bg="#1a1a1a")
                row.pack(fill=tk.X)
                tk.Label(row, text=spell, bg="#1a1a1a", fg=OK_FG,
                         font=self._font_status, anchor='w', width=10).pack(side=tk.LEFT)
                tick_str = "perm" if ticks == -1 else f"{ticks}t"
                tk.Label(row, text=tick_str, bg="#1a1a1a", fg=BLUE,
                         font=self._font_status, anchor='e').pack(side=tk.RIGHT)
        else:
            tk.Label(self._spells_frame, text="none", bg="#1a1a1a", fg=DIM,
                     font=self._font_status, anchor='w').pack(fill=tk.X)


    # ── Navigation panel ─────────────────────────────────────────────────

    def _build_nav_panel(self, parent):
        BG     = "#1a1a1a"
        HDR_FG = "#4ec9b0"
        self._nav_parent = parent

        tk.Label(parent, text="─ NAVIGATION ─", bg=BG, fg=HDR_FG,
                 font=self._font_status_hdr, anchor='w').pack(fill=tk.X, padx=4, pady=(4, 2))

        self._nav_room_var = tk.StringVar(value="")
        self._nav_room_label = tk.Label(parent, textvariable=self._nav_room_var, bg=BG, fg="#dcdcaa",
                 font=self._font_status, anchor='w')
        self._nav_room_label.pack(fill=tk.X, padx=6, pady=(0, 4))
        parent.bind('<Configure>', self._on_nav_resize)

        tk.Frame(parent, bg="#3a3a3a", height=1).pack(fill=tk.X, pady=(0, 3))

        self._nav_exits_frame = tk.Frame(parent, bg=BG)
        self._nav_exits_frame.pack(fill=tk.BOTH, expand=True, padx=4)

        tk.Frame(parent, bg="#3a3a3a", height=1).pack(fill=tk.X, pady=(4, 2))
        tk.Label(parent, text="─ EXPLORE ─", bg=BG, fg=HDR_FG,
                 font=self._font_status_hdr, anchor='w').pack(fill=tk.X, padx=4, pady=(2, 1))

        self._nav_explore_status_var = tk.StringVar(value="Idle")
        self._nav_explore_status_lbl = tk.Label(parent, textvariable=self._nav_explore_status_var,
                                                bg=BG, fg="#666666", font=self._font_status, anchor='w')
        self._nav_explore_status_lbl.pack(fill=tk.X, padx=6)

        self._nav_explore_target_var = tk.StringVar(value="")
        self._nav_explore_target_lbl = tk.Label(parent, textvariable=self._nav_explore_target_var,
                                                bg=BG, fg="#888888", font=self._font_status, anchor='w',
                                                wraplength=140, justify=tk.LEFT)
        self._nav_explore_target_lbl.pack(fill=tk.X, padx=6)

        self._nav_explore_zone_var = tk.StringVar(value="")
        tk.Label(parent, textvariable=self._nav_explore_zone_var,
                 bg=BG, fg="#666666", font=self._font_status, anchor='w').pack(fill=tk.X, padx=6)

        self._update_nav_panel()

    def _on_nav_resize(self, event):
        wrap = event.width - 12
        self._nav_room_label.config(wraplength=wrap)
        if hasattr(self, '_nav_explore_target_lbl'):
            self._nav_explore_target_lbl.config(wraplength=wrap)

    def _init_nav_sash(self):
        """Set the initial sash for the bottom horizontal pane after the window is drawn."""
        try:
            w = self.advisor_area.winfo_width() + self._nav_parent.winfo_width()
            if w > 10:
                frac = self._load_ui_local().get('nav_sash_fraction')
                x = int(frac * w) if frac is not None else w // 2
                self._bottom_paned.sash_place(0, x, 0)
        except Exception:
            pass

    def _update_nav_panel(self):
        """Refresh the navigation panel with current room name and exits."""
        BG          = "#1a1a1a"
        DIR_FG      = "#9cdcfe"
        ROOM_FG     = "#d4d4d4"
        UNK_FG      = "#666666"
        ASSUMED_FG  = "#888888"
        BLOCKED_FG  = "#c07060"
        F       = self._font_status

        if not hasattr(self, '_nav_room_var'):
            return

        profile = self.profiles.get(self.current_profile, {}) if self.current_profile else {}
        rooms      = profile.get('rooms', {})
        room_links = profile.get('room_links', {})
        room_hash  = self.current_room_hash

        if room_hash and room_hash in rooms:
            room = rooms[room_hash]
            self._nav_room_var.set(room.get('name', ''))
            exits_str = room.get('exits', '')
            # Exits are stored as the raw MUD line, e.g. "[ Exits: n e s w u ]"
            # or "[North, South, East]".  Strip brackets, drop "Exits:" label,
            # then split on whitespace and commas.
            exits_str = exits_str.strip().lstrip('[').rstrip(']').strip()
            if exits_str.lower().startswith('exits:'):
                exits_str = exits_str[6:].strip()
            tokens = [t.strip(',.') for t in exits_str.replace(',', ' ').split() if t.strip(',.')]
            # Expand abbreviations to full direction names (keys used in room_links)
            full_dirs = [self.direction_map.get(t.lower(), t.lower()) for t in tokens
                         if t.lower() in self.direction_map and
                            self.direction_map.get(t.lower()) != 'look']
        else:
            self._nav_room_var.set('(unknown)')
            full_dirs = []

        links = room_links.get(room_hash, {}) if room_hash else {}

        for w in self._nav_exits_frame.winfo_children():
            w.destroy()

        for direction in full_dirs:
            row = tk.Frame(self._nav_exits_frame, bg=BG)
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=direction.capitalize(), bg=BG, fg=DIR_FG, font=F,
                     anchor='w', width=6).pack(side=tk.LEFT)
            link_val = links.get(direction)
            if isinstance(link_val, dict) and link_val.get('blocked'):
                tk.Label(row, text="(blocked)", bg=BG, fg=BLOCKED_FG, font=F,
                         anchor='w').pack(side=tk.LEFT, fill=tk.X, expand=True)
                continue
            neighbor_hash, is_assumed = _link_dest(link_val)
            if neighbor_hash and neighbor_hash in rooms:
                dest_name = rooms[neighbor_hash].get('name', neighbor_hash[:8])
                if is_assumed:
                    label_text = dest_name + " (assumed)"
                    label_fg   = ASSUMED_FG
                else:
                    label_text = dest_name
                    label_fg   = ROOM_FG
                tk.Label(row, text=label_text, bg=BG, fg=label_fg, font=F,
                         anchor='w').pack(side=tk.LEFT, fill=tk.X, expand=True)
            else:
                tk.Label(row, text="(unknown)", bg=BG, fg=UNK_FG, font=F,
                         anchor='w').pack(side=tk.LEFT, fill=tk.X, expand=True)

        if not full_dirs:
            tk.Label(self._nav_exits_frame, text="No exits", bg=BG, fg=UNK_FG,
                     font=F, anchor='w').pack(fill=tk.X)

        if not hasattr(self, '_nav_explore_status_var'):
            return

        ACTIVE_FG = "#4ec9b0"
        # Explore status
        if self._active_explore:
            dest_key  = self._active_explore["dest"]
            dest_name = rooms.get(dest_key, {}).get('name', dest_key[:12])
            self._nav_explore_status_var.set("Active")
            self._nav_explore_status_lbl.config(fg=ACTIVE_FG)
            self._nav_explore_target_var.set(f"→ {dest_name}")
            self._nav_explore_target_lbl.config(fg="#d4d4d4")
        else:
            self._nav_explore_status_var.set("Idle")
            self._nav_explore_status_lbl.config(fg=UNK_FG)
            self._nav_explore_target_var.set("")

        # Zone explore stats
        if room_hash and room_hash in rooms:
            current_zone = rooms[room_hash].get('zone', '')
            zone_total = zone_unexplored = 0
            for rk, rd in rooms.items():
                if rd.get('zone', '') == current_zone:
                    zone_total += 1
                    ei = self._classify_room_exits(rk)
                    if ei["unknown"] or ei["assumed"]:
                        zone_unexplored += 1
            zone_label = current_zone or "(unknown zone)"
            self._nav_explore_zone_var.set(
                f"{zone_label}: {zone_unexplored}/{zone_total} unexplored"
            )
        else:
            self._nav_explore_zone_var.set("")

    def update_profile_list(self):
        """Update the profile list and rebuild the profile menu"""
        # Filter out the _settings key from profile names
        profile_names = [name for name in self.profiles.keys() if not name.startswith('_')]

        if profile_names and not self.profile_var.get():
            # Try to load the last connected profile
            last_profile = self.get_last_profile()
            if last_profile and last_profile in profile_names:
                self.profile_var.set(last_profile)
                self.on_profile_selected(None)
            else:
                self.profile_var.set(profile_names[0])
                self.on_profile_selected(None)

        self._rebuild_profile_menu()

    def _rebuild_profile_menu(self):
        """Rebuild the Profile sub-menu from the current profile list."""
        self._profile_menu.delete(0, tk.END)
        profile_names = [n for n in self.profiles if not n.startswith('_')]
        for name in profile_names:
            self._profile_menu.add_radiobutton(
                label=name,
                variable=self.profile_var,
                value=name,
                command=lambda n=name: self._select_profile(n)
            )
        self._profile_menu.add_separator()
        self._profile_menu.add_command(label="New Profile...", command=self.new_profile)
        self._profile_menu.add_command(label="Edit Profile...", command=self.edit_profile)
        self._profile_menu.add_command(label="Delete Profile...", command=self.delete_profile)

    def _select_profile(self, name):
        """Select a profile by name (called from menu radiobutton)."""
        self.profile_var.set(name)
        self.on_profile_selected(None)

    def on_profile_selected(self, event):
        """Handle profile selection"""
        profile_name = self.profile_var.get()
        if profile_name and profile_name in self.profiles:
            profile = self.profiles[profile_name]
            self.current_profile = profile_name

            # Load room tracking settings from profile
            self.room_color = profile.get('room_color', None)
            tracking_enabled = profile.get('room_tracking_enabled', False)
            self.room_tracking_var.set(tracking_enabled)
            self.room_tracking_enabled = tracking_enabled

            self._update_status_bar()
            self._status_profile_label.config(text=profile_name)

    def new_profile(self):
        """Create a new profile"""
        dialog = ProfileDialog(self.master, "New Profile")
        if dialog.result:
            name, host, port, character, password = dialog.result
            if name in self.profiles:
                messagebox.showerror("Error", "Profile name already exists")
                return

            self.profiles[name] = {
                'host': host,
                'port': port,
                'character': character,
                'password': password
            }
            self.save_profiles()
            self.update_profile_list()
            self._select_profile(name)

    def edit_profile(self):
        """Edit the selected profile"""
        profile_name = self.profile_var.get()
        if not profile_name or profile_name not in self.profiles:
            messagebox.showwarning("Warning", "Please select a profile to edit")
            return

        profile = self.profiles[profile_name]
        dialog = ProfileDialog(self.master, "Edit Profile",
                              profile_name, profile['host'], profile['port'],
                              profile.get('character', ''), profile.get('password', ''))
        if dialog.result:
            name, host, port, character, password = dialog.result

            # If name changed, delete old and create new
            if name != profile_name:
                if name in self.profiles:
                    messagebox.showerror("Error", "Profile name already exists")
                    return
                del self.profiles[profile_name]

            # Preserve existing learned prompts when editing
            self.profiles[name] = {
                'host': host,
                'port': port,
                'character': character,
                'password': password,
                'login_prompt': profile.get('login_prompt', ''),
                'password_prompt': profile.get('password_prompt', '')
            }
            self.save_profiles()
            self.update_profile_list()
            self._select_profile(name)

    def delete_profile(self):
        """Delete the selected profile"""
        profile_name = self.profile_var.get()
        if not profile_name or profile_name not in self.profiles:
            messagebox.showwarning("Warning", "Please select a profile to delete")
            return

        if messagebox.askyesno("Confirm Delete", f"Delete profile '{profile_name}'?"):
            del self.profiles[profile_name]
            self.save_profiles()
            self.profile_var.set('')
            self.current_profile = None
            self.update_profile_list()
            self._status_profile_label.config(text="")

    def toggle_connection(self):
        """Toggle connection state"""
        if self.connected:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        """Establish SSL connection to MUD server"""
        if not self.current_profile or self.current_profile not in self.profiles:
            messagebox.showerror("Error", "Please select a profile before connecting.")
            return
        profile = self.profiles[self.current_profile]
        host = profile.get('host', '').strip()
        try:
            port = int(profile.get('port', '4000'))
        except ValueError:
            messagebox.showerror("Error", "Invalid port number in profile")
            return

        if not host:
            messagebox.showerror("Error", "No host configured in profile")
            return

        try:
            self.append_text(f"Connecting to {host}:{port}...\n", "system")

            # Create a regular socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(10)

            # Wrap it with SSL
            context = ssl.create_default_context()
            # Allow self-signed certificates (MUDs often use them)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

            self.ssl_socket = context.wrap_socket(self.socket, server_hostname=host)
            self.ssl_socket.connect((host, port))

            self.connected = True
            self._status_conn_label.config(text="Connected", foreground="green")
            self._conn_menu.entryconfig("Connect", label="Disconnect")
            self.send_btn.config(state=tk.NORMAL)
            self._conn_menu.entryconfig("Send Character Name", state=tk.NORMAL)
            self._conn_menu.entryconfig("Send Password", state=tk.NORMAL)
            self._conn_menu.entryconfig("Send & Remember", state=tk.NORMAL)
            self._conn_menu.entryconfig("Quit MUD", state=tk.NORMAL)

            self.append_text(f"Connected successfully!\n", "system")
            self.session_logger.open()
            if self.session_logger.path:
                self._status_log_label.config(
                    text=f"Log: {os.path.basename(self.session_logger.path)}",
                    foreground="green")
            else:
                self._status_log_label.config(text="Log: disabled", foreground="red")
                self.append_text(
                    f"[Warning: session logging disabled — {self.session_logger._open_error}]\n",
                    "system")

            # Initialize AI agent (for room collection) and LLM advisor
            from ai_agent import ExplorationAgent
            from llm_advisor import LLMAdvisor
            if not self.ai_agent:
                self.ai_agent = ExplorationAgent(self)
            self.ai_agent.start()
            if not self.llm_advisor:
                self.llm_advisor = LLMAdvisor(self)
            self.llm_advisor.reset_history()
            # Save this as the last connected profile
            if self.current_profile:
                self.save_last_profile(self.current_profile)

            # Check if autologin should be performed
            if profile.get('character') and profile.get('password'):
                self.autologin_pending = True
                self.autologin_stage = 0
                self.append_text("Autologin enabled for this profile\n", "system")
            else:
                # No autologin — start default skill after a short delay
                self.master.after(500, self._start_default_skill)

            # Start receiving thread
            self.receive_thread = threading.Thread(target=self.receive_data, daemon=True)
            self.receive_thread.start()
            self._keepalive_job = self.master.after(60_000, self._send_keepalive)

        except socket.timeout:
            messagebox.showerror("Error", "Connection timed out")
            self.cleanup_connection()
        except socket.gaierror:
            messagebox.showerror("Error", f"Could not resolve host: {host}")
            self.cleanup_connection()
        except ConnectionRefusedError:
            messagebox.showerror("Error", "Connection refused by server")
            self.cleanup_connection()
        except Exception as e:
            messagebox.showerror("Error", f"Connection failed: {str(e)}")
            self.cleanup_connection()

    def disconnect(self):
        """Disconnect from MUD server"""
        self.connected = False
        self.cleanup_connection()
        self._status_conn_label.config(text="Disconnected", foreground="red")
        self._conn_menu.entryconfig("Disconnect", label="Connect")
        self.send_btn.config(state=tk.DISABLED)
        self._conn_menu.entryconfig("Send Character Name", state=tk.DISABLED)
        self._conn_menu.entryconfig("Send Password", state=tk.DISABLED)
        self._conn_menu.entryconfig("Send & Remember", state=tk.DISABLED)
        self._conn_menu.entryconfig("Quit MUD", state=tk.DISABLED)
        if self.ai_agent and self.ai_agent.is_running:
            self.ai_agent.stop()
        self.session_logger.close()
        self._status_log_label.config(text="Log: off", foreground="gray")
        self._cancel_auto_score()
        if self._keepalive_job:
            self.master.after_cancel(self._keepalive_job)
            self._keepalive_job = None
        # Reset tick sync state (keep _tick_interval — it's seeded from saved profile)
        self._tick_count = None
        if self._tick_countdown_job:
            self.master.after_cancel(self._tick_countdown_job)
            self._tick_countdown_job = None
        self.quit_pending = False
        self.quit_stage = 0
        self.quit_prompts_seen = []
        self._pending_command = None
        self._response_buffer = []
        self.triggered_once_responses.clear()  # Reset run-once tracking
        self.append_text("Disconnected from server\n", "system")

    def _send_keepalive(self):
        """Send IAC NOP to detect silently dropped TCP connections."""
        if not self.connected or self.ssl_socket is None:
            return
        try:
            self.ssl_socket.sendall(bytes([0xFF, 0xF1]))  # IAC NOP
        except OSError:
            self.message_queue.put(("disconnect", "Connection lost\n"))
            return
        self._keepalive_job = self.master.after(60_000, self._send_keepalive)

    def cleanup_connection(self):
        """Clean up socket resources"""
        if self.ssl_socket:
            try:
                self.ssl_socket.close()
            except OSError:
                pass
            self.ssl_socket = None
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None

    def receive_data(self):
        """Receive data from MUD server (runs in separate thread)"""
        buffer = []
        while self.connected:
            try:
                sock = self.ssl_socket
                if sock is None:
                    break
                data = sock.recv(4096)
                if not data:
                    self.message_queue.put(("disconnect", "Connection closed by server\n"))
                    break

                # Filter TELNET sequences first (works on bytes)
                data = self.filter_telnet_sequences(data)

                # Decode received data
                try:
                    text = data.decode('utf-8')
                except UnicodeDecodeError:
                    # Try latin-1 as fallback
                    text = data.decode('latin-1', errors='replace')
                text = text.replace('\r', '')  # strip CR from CRLF line endings

                # Parse ANSI color codes
                parsed_segments = self.parse_ansi_text(text)

                # Store raw decoded text (ANSI codes intact) for the color
                # calibration UI — split into lines so the dialog can scan them.
                for raw_line in text.split('\n'):
                    if raw_line.strip():
                        self._raw_ansi_lines.append(raw_line)

                # Add to buffer (we'll pass the parsed segments)
                buffer = parsed_segments

                # For prompt learning, we need text without ANSI codes
                clean_text = self.strip_ansi_codes(text)
                self.session_logger.log_received(clean_text)

                # Track last non-empty line for prompt learning and advisor trigger
                lines = clean_text.strip().split('\n')
                for line in reversed(lines):
                    if line.strip():
                        self.last_line = line.strip()
                        break

                # Accumulate MUD response for LLM advisor / skill engine.
                # Always append so the skill engine (which fires on every prompt,
                # not just after user-typed commands) has fresh context. The
                # advisor path still gates on _pending_command below.
                for ln in clean_text.splitlines():
                    if ln.strip():
                        self._response_buffer.append(ln.rstrip())

                # Detect MUD command prompt — trigger skill on every prompt.
                if self.last_line.rstrip().endswith('>'):
                    # Skill goes first so it sees _response_buffer before the
                    # advisor consumes and clears it.
                    if self.skill_engine and self.skill_engine.is_active():
                        if self.expecting_room_data:
                            # A directional move is in-flight; defer until the
                            # room description (or a move-fail) arrives so the
                            # LLM sees the correct room annotation, not the
                            # stale one from before the move.
                            self._skill_trigger_pending = True
                            if self._room_wait_timeout_id is None:
                                self._room_wait_timeout_id = self.master.after(
                                    3000, self._room_wait_timeout)
                        else:
                            self.master.after(0, self._trigger_skill)


                # Parse character stats and queue status panel update
                self._parse_and_queue_stats(clean_text)

                # Autoloot on kill
                self._handle_autoloot(clean_text)

                # Per-mob combat stat tracking
                self._update_mob_combat_stats(clean_text)

                # Group membership tracking (used by skill engine PC detection)
                self._update_group_members(clean_text)

                # Survival automation (inventory collection)
                self._survival_handle_text(clean_text)

                # Tick synchronisation
                if self.mud_parser.detect_tick_event(clean_text):
                    self.message_queue.put(('tick_event', None))

                # Handle autologin (use clean text without ANSI codes)
                if self.autologin_pending and self.current_profile:
                    self.handle_autologin(clean_text)

                # Handle quit sequence (use clean text without ANSI codes)
                if self.quit_pending and self.current_profile:
                    self.handle_quit_sequence(clean_text)

                # Handle custom auto-responses (use clean text without ANSI codes)
                # Don't process custom responses during quit sequence
                if self.current_profile and not self.quit_pending:
                    self.handle_custom_responses(clean_text)

                # Notify AI agent of incoming text
                if self.ai_agent and self.ai_agent.is_running:
                    self.message_queue.put(("ai_text", clean_text))

                # Send to queue for display
                self.message_queue.put(("data", buffer))
                buffer = []

            except socket.timeout:
                continue
            except Exception as e:
                if self.connected:
                    self.message_queue.put(("error", f"Error receiving data: {str(e)}\n"))
                break

    def handle_autologin(self, text):
        """Handle autologin sequence based on received text"""
        if not self.current_profile or self.current_profile not in self.profiles:
            return

        profile = self.profiles[self.current_profile]
        character = profile.get('character', '')
        password = profile.get('password', '')
        login_prompt = profile.get('login_prompt', '').lower()
        password_prompt = profile.get('password_prompt', '').lower()

        if not character or not password or not login_prompt or not password_prompt:
            return

        # Check each line to see if prompt is at the start
        lines = text.split('\n')

        # Stage 0: Wait for name/login prompt
        if self.autologin_stage == 0:
            for line in lines:
                line_lower = line.strip().lower()
                if line_lower.startswith(login_prompt):
                    time.sleep(0.5)  # Small delay to ensure prompt is complete
                    try:
                        self.ssl_socket.sendall((character + "\n").encode('utf-8'))
                        self.message_queue.put(("system", f"[Autologin] Sent character name: {character}\n"))
                        self.autologin_stage = 1
                    except Exception as e:
                        self.message_queue.put(("error", f"Autologin failed: {e}\n"))
                        self.autologin_pending = False
                    break

        # Stage 1: Wait for password prompt
        elif self.autologin_stage == 1:
            for line in lines:
                line_lower = line.strip().lower()
                if line_lower.startswith(password_prompt):
                    time.sleep(0.5)
                    try:
                        self.ssl_socket.sendall((password + "\n").encode('utf-8'))
                        self.message_queue.put(("system", "[Autologin] Sent password\n"))
                        self.autologin_stage = 2
                        # Stay pending — may still need to clear MOTD and main menu
                    except Exception as e:
                        self.message_queue.put(("error", f"Autologin failed: {e}\n"))
                        self.autologin_pending = False
                    break

        # Stage 2+: Handle post-password prompts (MOTD, main menu) or detect game entry
        elif self.autologin_stage >= 2:
            for line in lines:
                line_lower = line.strip().lower()

                # MOTD "press return" prompt
                if '*** press return' in line_lower or 'press return' in line_lower:
                    time.sleep(0.3)
                    try:
                        self.ssl_socket.sendall(b"\n")
                        self.message_queue.put(("system", "[Autologin] Cleared MOTD\n"))
                        self.autologin_stage += 1
                    except Exception as e:
                        self.message_queue.put(("error", f"Autologin failed: {e}\n"))
                        self.autologin_pending = False
                    break

                # CircleMUD main menu — enter the game
                if 'make your choice' in line_lower:
                    time.sleep(0.3)
                    try:
                        self.ssl_socket.sendall(b"1\n")
                        self.message_queue.put(("system", "[Autologin] Selected 'Enter the game'\n"))
                        self.autologin_stage += 1
                    except Exception as e:
                        self.message_queue.put(("error", f"Autologin failed: {e}\n"))
                        self.autologin_pending = False
                    break

                # Game prompt detected — we're in
                if line_lower.rstrip().endswith('>') and ('h ' in line_lower or 'hp' in line_lower):
                    self._complete_autologin()
                    break

    def _apply_font_size(self):
        """Update all font objects and persist the size to settings."""
        self._font_main.configure(size=self._font_size)
        self._font_status.configure(size=self._font_size - 1)
        self._font_status_hdr.configure(size=self._font_size - 1)
        if '_settings' not in self.profiles:
            self.profiles['_settings'] = {}
        self.profiles['_settings']['font_size'] = self._font_size
        self.save_profiles()

    def _zoom_in(self):
        self._font_size = min(self._font_size + 1, 32)
        self._apply_font_size()

    def _zoom_out(self):
        self._font_size = max(self._font_size - 1, 6)
        self._apply_font_size()

    def _zoom_reset(self):
        self._font_size = 11
        self._apply_font_size()

    def _history_prev(self, event):
        """Up arrow — go back in command history."""
        if not self._cmd_history:
            return 'break'
        if self._cmd_history_pos == -1:
            self._cmd_history_pos = len(self._cmd_history) - 1
        elif self._cmd_history_pos > 0:
            self._cmd_history_pos -= 1
        self._set_entry(self._cmd_history[self._cmd_history_pos])
        return 'break'

    def _history_next(self, event):
        """Down arrow — go forward in command history."""
        if self._cmd_history_pos == -1:
            return 'break'
        if self._cmd_history_pos < len(self._cmd_history) - 1:
            self._cmd_history_pos += 1
            self._set_entry(self._cmd_history[self._cmd_history_pos])
        else:
            self._cmd_history_pos = -1
            self._set_entry('')
        return 'break'

    def _set_entry(self, text):
        self.input_entry.delete(0, tk.END)
        self.input_entry.insert(0, text)

    def _redirect_focus_to_entry(self, event):
        """Keep keyboard focus in the input entry whenever the main window is active."""
        # event.widget can be a bare string (Tk path) for destroyed widgets
        if not isinstance(event.widget, tk.Misc):
            return
        # Allow focus in dialog boxes (Toplevel windows other than master)
        if event.widget.winfo_toplevel() is not self.master:
            return
        # Don't fight the entry with itself
        if event.widget is self.input_entry:
            return
        self.input_entry.focus_set()

    def _complete_autologin(self):
        """Finalize autologin: clear pending flag and enable room tracking if needed."""
        self.autologin_pending = False
        self._prev_combat_hp = None  # Reset damage baseline on fresh login
        self._kill_cmd_pending = False
        self._combat_mob = None
        self._last_killed_mob = None
        self.group_members = set()
        self._group_leader = None

        # Seed tick interval from saved profile value (keeps limit across sessions)
        saved_tick = (self.profiles.get(self.current_profile, {})
                      .get('tick_interval'))
        if saved_tick and int(saved_tick) > 0:
            self._tick_interval = int(saved_tick)
        self._tick_count = None
        self.message_queue.put(("system", "[Autologin] Login sequence completed\n"))
        self.master.after(500, self._start_default_skill)
        if self.room_tracking_enabled:
            self.detect_entry_room = True
            self.expecting_room_data = True
            self.message_queue.put(("system", "[Room tracking] Detecting entry room...\n"))
        # Issue first score fetch after a short settle delay (no-op if GMCP is active)
        self.master.after(1500, self._send_auto_score)

    def _send_auto_score(self):
        """Send score command silently and reschedule.

        Not used when GMCP is active — all fields previously requiring score
        (XP, xp_next, AC, gold, hunger, thirst) are now provided by GMCP.
        Without GMCP the 60-second cadence keeps buff durations current via
        text parsing.
        """
        if not self.connected or self.gmcp_active:
            self._auto_score_job = None
            return
        try:
            self._suppress_score_output = True
            self.ssl_socket.sendall(b"score\n")
            # Safety: clear suppression if prompt never arrives
            self.master.after(5000, self._clear_score_suppression)
        except Exception:
            self._suppress_score_output = False
        self._auto_score_job = self.master.after(60_000, self._send_auto_score)

    def _clear_score_suppression(self):
        self._suppress_score_output = False

    def _cancel_auto_score(self):
        if self._auto_score_job:
            self.master.after_cancel(self._auto_score_job)
            self._auto_score_job = None
        self._suppress_score_output = False

    def _filter_display_segments(self, segments):
        """Remove prompt lines and suppressed score output from display segments."""
        # Rebuild segment list line by line so we can inspect each line independently
        lines = []
        current = []
        for seg_text, color in segments:
            parts = seg_text.split('\n')
            for i, piece in enumerate(parts):
                if piece:
                    current.append((piece, color))
                if i < len(parts) - 1:
                    lines.append(current)
                    current = []
        if current:
            lines.append(current)

        result = []
        for line_pieces in lines:
            plain = ''.join(t for t, _ in line_pieces).strip()

            if not plain:
                result.append(('\n', '#d4d4d4'))
                continue

            # Prompt line — update stats panel, never display, clear suppression
            if self.mud_parser.parse_prompt_stats(plain) is not None:
                self._suppress_score_output = False
                continue

            if self._suppress_score_output:
                continue

            result.extend(line_pieces)
            result.append(('\n', '#d4d4d4'))

        return result

    def handle_custom_responses(self, text):
        """Handle custom learned prompt-response pairs"""
        if not self.current_profile or self.current_profile not in self.profiles:
            return

        profile = self.profiles[self.current_profile]
        custom_responses = profile.get('custom_responses', {})

        if not custom_responses:
            return

        # Check each line to see if it matches a learned prompt
        lines = text.split('\n')

        for line in lines:
            line_lower = line.strip().lower()
            line_normalized = self.normalize_prompt(line_lower)

            # Check if this line starts with any learned prompt
            for prompt, response_data in custom_responses.items():
                prompt_normalized = self.normalize_prompt(prompt)

                if line_normalized.startswith(prompt_normalized):
                    # Handle both old format (string) and new format (dict)
                    if isinstance(response_data, str):
                        response = response_data
                        run_once = False
                    else:
                        response = response_data.get('response', '')
                        run_once = response_data.get('run_once', False)

                    # Check if this is a run_once response that has already fired
                    if run_once and prompt_normalized in self.triggered_once_responses:
                        continue  # Skip this response

                    try:
                        time.sleep(0.3)  # Small delay
                        self.ssl_socket.sendall((response + "\n").encode('utf-8'))
                        self.message_queue.put(("system", f"[Auto-response] Sent: {response}\n"))

                        # Mark as triggered if run_once
                        if run_once:
                            self.triggered_once_responses.add(prompt_normalized)
                    except Exception as e:
                        self.message_queue.put(("error", f"Auto-response failed: {e}\n"))
                    break  # Only respond once per line

    def start_quit_sequence(self):
        """Start the quit sequence"""
        if not self.connected or not self.current_profile:
            return

        profile = self.profiles.get(self.current_profile, {})
        quit_sequence = profile.get('quit_sequence', [])

        self.quit_pending = True
        self.quit_stage = 0
        self.quit_prompts_seen = []

        # Stop the AI agent immediately so no further commands reach the MUD.
        # In-flight LLM callbacks are also blocked via the quit_pending guard in
        # send_ai_command, preventing them from polluting the login menu.
        if self.ai_agent and self.ai_agent.is_running:
            self.ai_agent.stop()
            self.append_text("[Quit] AI agent stopped.\n", "system")

        # Send the quit command to the MUD — this triggers the confirmation prompts
        # that the quit sequence event/action pairs are designed to respond to.
        try:
            self.ssl_socket.sendall("quit\n".encode('utf-8'))
            self.append_text("[Quit] Sent: quit\n", "system")
        except Exception as e:
            self.append_text(f"Quit failed to send: {e}\n", "error")
            self.quit_pending = False
            return

        if quit_sequence:
            self.append_text("[Quit sequence started - will auto-respond to known prompts]\n", "system")
        else:
            self.append_text("[Quit sequence started - learning mode (manually respond to each prompt)]\n", "system")
            self.append_text("[The quit sequence will be complete when connection closes]\n", "system")

    def handle_quit_sequence(self, text):
        """Handle quit sequence by auto-responding to learned prompts"""
        if not self.current_profile or self.current_profile not in self.profiles:
            return

        profile = self.profiles[self.current_profile]
        quit_sequence = profile.get('quit_sequence', [])

        if not quit_sequence:
            # Learning mode - just track prompts, user will respond manually
            return

        # Check each line to see if it matches a remaining prompt in sequence.
        # We scan forward from the current stage rather than requiring strict
        # sequential order — this handles cases where earlier prompts are skipped
        # (e.g. the in-game '>' prompt is absent when quit is sent programmatically).
        lines = text.split('\n')

        for line in lines:
            line_normalized = self.normalize_prompt(line.strip().lower())

            # Check if we've seen this prompt already in this quit session
            if line_normalized in self.quit_prompts_seen:
                continue

            # Scan forward from current stage to find the first matching prompt
            matched_stage = None
            for stage_idx in range(self.quit_stage, len(quit_sequence)):
                expected_prompt = quit_sequence[stage_idx]['prompt']
                if line_normalized.startswith(expected_prompt):
                    matched_stage = stage_idx
                    break

            if matched_stage is not None:
                    response = quit_sequence[matched_stage]['response']

                    try:
                        time.sleep(0.3)  # Small delay
                        self.ssl_socket.sendall((response + "\n").encode('utf-8'))
                        self.message_queue.put(("system", f"[Quit {matched_stage + 1}/{len(quit_sequence)}] Sent: {response}\n"))

                        # Mark this prompt as seen and advance past this stage
                        self.quit_prompts_seen.append(line_normalized)
                        self.quit_stage = matched_stage + 1

                        if self.quit_stage >= len(quit_sequence):
                            self.message_queue.put(("system", "[Quit sequence complete - waiting for disconnect]\n"))
                    except Exception as e:
                        self.message_queue.put(("error", f"Quit sequence failed: {e}\n"))
                        self.quit_pending = False
                    break

    def learn_quit_response(self, prompt, response):
        """Learn a prompt-response pair as part of the quit sequence"""
        if not self.current_profile or self.current_profile not in self.profiles:
            return

        # Normalize prompt to handle varying stats (e.g., "24H 100M >" becomes "#H #M >")
        prompt_normalized = self.normalize_prompt(prompt.lower())

        # Don't learn if we already responded to this prompt in this quit sequence
        if prompt_normalized in self.quit_prompts_seen:
            return

        # Initialize quit_sequence if it doesn't exist
        if 'quit_sequence' not in self.profiles[self.current_profile]:
            self.profiles[self.current_profile]['quit_sequence'] = []

        # Add this prompt-response to the quit sequence (store normalized version)
        self.profiles[self.current_profile]['quit_sequence'].append({
            'prompt': prompt_normalized,
            'response': response
        })
        self.quit_prompts_seen.append(prompt_normalized)
        self.quit_stage += 1

        self.save_profiles()
        seq_len = len(self.profiles[self.current_profile]['quit_sequence'])
        self.append_text(f"[Learned quit step {seq_len}: '{prompt}' -> '{response}']\n", "system")

    def toggle_room_tracking(self):
        """Toggle room tracking feature"""
        self.room_tracking_enabled = self.room_tracking_var.get()

        if self.room_tracking_enabled:
            # Initialize room data in profile if it doesn't exist
            if self.current_profile and self.current_profile in self.profiles:
                if 'rooms' not in self.profiles[self.current_profile]:
                    self.profiles[self.current_profile]['rooms'] = {}
                if 'room_links' not in self.profiles[self.current_profile]:
                    self.profiles[self.current_profile]['room_links'] = {}

                # Save tracking enabled state to profile
                self.profiles[self.current_profile]['room_tracking_enabled'] = True
                self.save_profiles()
                self._update_status_bar()
                self.append_text("[Room tracking enabled - will detect and map rooms]\n", "system")

                if self.room_color:
                    self.append_text(f"[Room color detected: {self.room_color}]\n", "system")
                else:
                    self.append_text("[Room color not yet detected - will identify on first movement]\n", "system")
            else:
                self.room_tracking_var.set(False)
                self.room_tracking_enabled = False
                self.append_text("[Room tracking: no profile selected]\n", "system")
        else:
            self._update_status_bar()
            if self.current_profile and self.current_profile in self.profiles:
                self.profiles[self.current_profile]['room_tracking_enabled'] = False
                self.save_profiles()
            self.append_text("[Room tracking disabled]\n", "system")

    def _update_status_bar(self):
        """Refresh all status bar labels from current state."""
        if self.room_tracking_enabled and self.current_profile and \
                self.current_profile in self.profiles:
            room_count = len(self.profiles[self.current_profile].get('rooms', {}))
            self._status_rooms_label.config(
                text=f"Rooms: {room_count}", foreground="green")
        else:
            self._status_rooms_label.config(text="", foreground="gray")

    # ------------------------------------------------------------------
    # Command frequency / quick-commands menu
    # ------------------------------------------------------------------

    _SCORE_DECAY    = 0.85   # multiplied into every score at each app start
    _SCORE_PRUNE    = 0.05   # scores below this are removed
    _CMD_MENU_SIZE  = 20     # entries shown in the Commands menu

    def _decay_cmd_scores(self):
        """Apply per-session exponential decay and prune near-zero scores."""
        self._cmd_scores = {
            cmd: score * self._SCORE_DECAY
            for cmd, score in self._cmd_scores.items()
            if score * self._SCORE_DECAY >= self._SCORE_PRUNE
        }

    def _record_cmd_score(self, command):
        """Increment score for a command and persist."""
        self._cmd_scores[command] = self._cmd_scores.get(command, 0.0) + 1.0
        if '_settings' not in self.profiles:
            self.profiles['_settings'] = {}
        self.profiles['_settings']['cmd_scores'] = self._cmd_scores
        self.save_profiles()

    def _rebuild_cmd_menu(self):
        """Populate the Commands menu with the top scored commands."""
        self._cmd_menu.delete(0, tk.END)
        top = sorted(self._cmd_scores.items(), key=lambda x: x[1], reverse=True)
        top = top[:self._CMD_MENU_SIZE]
        if not top:
            self._cmd_menu.add_command(label="(no commands yet)", state=tk.DISABLED)
            return
        for cmd, score in top:
            self._cmd_menu.add_command(
                label=cmd,
                command=lambda c=cmd: self._send_quick_command(c)
            )

    def _send_quick_command(self, command):
        """Send a command chosen from the Commands menu."""
        self._set_entry(command)
        self.send_message()

    def _save_autoloot(self):
        """Persist the autoloot setting."""
        if '_settings' not in self.profiles:
            self.profiles['_settings'] = {}
        self.profiles['_settings']['autoloot'] = self._autoloot_var.get()
        self.save_profiles()

    # ------------------------------------------------------------------
    # Survival automation (food / drink)
    # ------------------------------------------------------------------

    _SURVIVAL_MAX_FOOD = 5
    _DIR_ABBREV = {'north': 'n', 'south': 's', 'east': 'e',
                   'west': 'w', 'up': 'u', 'down': 'd'}

    def _fd_config(self):
        """Return the food_drink config dict for the current profile (may be empty)."""
        if not self.current_profile or self.current_profile not in self.profiles:
            return {}
        return self.profiles[self.current_profile].setdefault('food_drink', {})

    def _rescue_config(self):
        """Return the rescue config dict for the current profile (may be empty)."""
        if not self.current_profile or self.current_profile not in self.profiles:
            return {}
        return self.profiles[self.current_profile].setdefault('rescue', {})

    def _survival_save(self):
        self.save_profiles()

    # ── Settings setters ──────────────────────────────────────────────

    def _survival_set_drink_container(self):
        val = simpledialog.askstring(
            "Drink Container",
            "Enter the name of your drink container (e.g. waterskin):",
            initialvalue=self._fd_config().get('drink_container', ''),
            parent=self.master)
        if val is not None:
            self._fd_config()['drink_container'] = val.strip()
            self._survival_save()
            self.append_text(f"[Survival] Drink container set to: {val.strip()}\n", "system")

    def _survival_set_food_item(self):
        val = simpledialog.askstring(
            "Food Item",
            "Enter the name of your food item (e.g. bread):",
            initialvalue=self._fd_config().get('food_item', ''),
            parent=self.master)
        if val is not None:
            self._fd_config()['food_item'] = val.strip()
            self._survival_save()
            self.append_text(f"[Survival] Food item set to: {val.strip()}\n", "system")

    def _survival_set_fountain_room(self):
        if not self.current_room_hash:
            messagebox.showwarning("Survival",
                "Not in a known room. Enable room tracking and enter a room first.")
            return
        cfg = self._fd_config()
        cfg['fountain_room'] = self.current_room_hash
        self._survival_save()
        room_name = (self.profiles.get(self.current_profile, {})
                     .get('rooms', {})
                     .get(self.current_room_hash, {})
                     .get('name', self.current_room_hash[:8]))
        self.append_text(f"[Survival] Fountain room set: {room_name}\n", "system")

    def _survival_set_food_store_room(self):
        if not self.current_room_hash:
            messagebox.showwarning("Survival",
                "Not in a known room. Enable room tracking and enter a room first.")
            return
        cfg = self._fd_config()
        cfg['food_store_room'] = self.current_room_hash
        self._survival_save()
        room_name = (self.profiles.get(self.current_profile, {})
                     .get('rooms', {})
                     .get(self.current_room_hash, {})
                     .get('name', self.current_room_hash[:8]))
        self.append_text(f"[Survival] Food store room set: {room_name}\n", "system")

    # ── Pathfinding ───────────────────────────────────────────────────

    def _survival_find_path(self, from_hash, to_hash):
        """BFS through room_links; returns list of short direction strings or None.

        Links marked death_trap or blocked are skipped.  Only directions that
        appear in the room's actual MUD exits string are traversed — assumed
        reverse-links that aren't physically listed are ignored.
        """
        if from_hash == to_hash:
            return []
        profile = self.profiles.get(self.current_profile, {})
        rooms = profile.get('rooms', {})
        links = profile.get('room_links', {})
        queue = deque([(from_hash, [])])
        visited = {from_hash}
        while queue:
            room, path = queue.popleft()
            m = _EXIT_DIR_RE.search(rooms.get(room, {}).get('exits', ''))
            raw = m.group(1).split() if m and m.group(1) != 'none' else []
            listed_dirs = {self.direction_map.get(d.lower(), d.lower()) for d in raw}
            for direction, link_val in links.get(room, {}).items():
                if direction not in listed_dirs:
                    continue
                if isinstance(link_val, dict) and (link_val.get('death_trap') or link_val.get('blocked')):
                    continue
                neighbor, _ = _link_dest(link_val)
                if neighbor is None:
                    continue
                abbrev = self._DIR_ABBREV.get(direction, direction)
                new_path = path + [abbrev]
                if neighbor == to_hash:
                    return new_path
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, new_path))
        return None   # no path found

    def _classify_room_exits(self, room_key):
        """Return exit classification for a room.

        Keys: "known", "assumed", "unknown", "blocked", "dangerous".
        death_trap exits are placed in "dangerous" (not "known") so the LLM
        sees them as a distinct category it must never walk.
        """
        profile = self.profiles.get(self.current_profile, {})
        rooms  = profile.get('rooms', {})
        links  = profile.get('room_links', {})
        room      = rooms.get(room_key, {})
        exits_str = room.get('exits', '')
        m = _EXIT_DIR_RE.search(exits_str)
        raw_dirs = m.group(1).split() if m and m.group(1) != 'none' else []
        # Exits are stored as MUD abbreviations (n/e/s/w/u/d); room_links uses full names.
        listed_dirs = [self.direction_map.get(d.lower(), d.lower()) for d in raw_dirs]
        room_links = links.get(room_key, {})
        result = {"known": {}, "assumed": {}, "unknown": [], "blocked": [], "dangerous": []}
        for d in listed_dirs:
            link_val = room_links.get(d)
            if link_val is None:
                result["unknown"].append(d)
            elif isinstance(link_val, dict) and link_val.get('death_trap'):
                result["dangerous"].append(d)
            elif isinstance(link_val, dict) and link_val.get('blocked'):
                result["blocked"].append(d)
            else:
                dest, assumed = _link_dest(link_val)
                dest_name = rooms.get(dest, {}).get('name', dest) if dest else '?'
                if assumed:
                    result["assumed"][d] = dest_name
                else:
                    result["known"][d] = dest_name
        return result

    def _find_nearest_explore_target(self):
        """Find the nearest reachable room with unknown or assumed exits.

        Combines target selection and pathfinding in a single BFS so only
        reachable targets are ever returned.  Prefers rooms in the current
        zone; falls back to any reachable room in the map.
        Returns (path, room_key, exit_info) or None if nothing is reachable.
        path is [] when the current room itself has unexplored exits.
        exit_info is {"known": {dir: name}, "assumed": {dir: name}, "unknown": [dir]}.
        """
        if not self.current_room_hash or not self.current_profile:
            return None
        profile = self.profiles.get(self.current_profile, {})
        rooms   = profile.get('rooms', {})
        links   = profile.get('room_links', {})
        current_zone = rooms.get(self.current_room_hash, {}).get('zone', '')

        def _unexplored_ei(room_key):
            ei = self._classify_room_exits(room_key)
            return ei if (ei["unknown"] or ei["assumed"]) else None

        # Current room may itself have unexplored exits.
        ei = _unexplored_ei(self.current_room_hash)
        if ei is not None:
            return [], self.current_room_hash, ei

        def _bfs(zone_filter):
            """BFS outward; return (path, room_key, ei) for the nearest
            reachable room with unexplored exits in zone_filter, or in any
            zone when zone_filter is None."""
            bfs = deque([(self.current_room_hash, [])])
            visited = {self.current_room_hash}
            while bfs:
                room_key, path = bfs.popleft()
                m = _EXIT_DIR_RE.search(rooms.get(room_key, {}).get('exits', ''))
                raw = m.group(1).split() if m and m.group(1) != 'none' else []
                listed_dirs = {self.direction_map.get(d.lower(), d.lower())
                               for d in raw}
                for direction, link_val in links.get(room_key, {}).items():
                    if direction not in listed_dirs:
                        continue
                    if isinstance(link_val, dict) and (
                            link_val.get('death_trap') or link_val.get('blocked')):
                        continue
                    neighbor, _ = _link_dest(link_val)
                    if not neighbor or neighbor not in rooms or neighbor in visited:
                        continue
                    visited.add(neighbor)
                    new_path = path + [self._DIR_ABBREV.get(direction, direction)]
                    if (zone_filter is None
                            or rooms.get(neighbor, {}).get('zone', '') == zone_filter):
                        ei = _unexplored_ei(neighbor)
                        if ei is not None:
                            return new_path, neighbor, ei
                    bfs.append((neighbor, new_path))
            return None

        return _bfs(current_zone) or _bfs(None)

    def _resolve_goto(self, target: str):
        """Resolve a goto:<target> string to a direction list via BFS, or None."""
        profile = self.profiles.get(self.current_profile, {})
        rooms   = profile.get('rooms', {})
        t       = target.strip()
        dest    = None

        # 1. Exact vnum (e.g. "vnum:3001")
        if t.lower().startswith('vnum:'):
            key = t.lower()
            if key in rooms:
                dest = key

        # 2. Landmark lookup (case-insensitive)
        if dest is None:
            tl = t.lower()
            for name, key in profile.get('landmarks', {}).items():
                if name.lower() == tl:
                    dest = key
                    break

        # 3. Mob name — optional "mob:" prefix; search mob_combat_stats then room mob_lines
        if dest is None:
            mob_query = t[4:].strip() if t.lower().startswith('mob:') else t
            q = mob_query.lower()
            for mob_name, entry in profile.get('mob_combat_stats', {}).items():
                if q in mob_name.lower():
                    mob_rooms = entry.get('rooms', [])
                    if mob_rooms and mob_rooms[-1] in rooms:
                        dest = mob_rooms[-1]
                        break
            if dest is None:
                for room_key, rdata in rooms.items():
                    if any(q in line.lower() for line in rdata.get('mob_lines', [])):
                        dest = room_key
                        break

        # 4. Room name substring
        if dest is None:
            q = t.lower()
            for room_key, rdata in rooms.items():
                if q in rdata.get('name', '').lower():
                    dest = room_key
                    break

        if dest is None or not self.current_room_hash:
            return None
        path = self._survival_find_path(self.current_room_hash, dest)
        if path is None:
            return None
        return path, dest

    def _cmd_setlandmark(self, name: str):
        """Handle the setlandmark <name> console command."""
        if not name:
            self.append_text("[Landmark] Usage: setlandmark <name>\n", "error")
            return
        if not self.current_room_hash:
            self.append_text("[Landmark] No current room known yet.\n", "error")
            return
        profile = self.profiles.get(self.current_profile, {})
        landmarks = profile.setdefault('landmarks', {})
        landmarks[name] = self.current_room_hash
        room_name = profile.get('rooms', {}).get(self.current_room_hash, {}).get('name', self.current_room_hash)
        self.append_text(f"[Landmark] '{name}' → {room_name} ({self.current_room_hash})\n", "system")
        self.save_profiles()

    # ── Death trap handling ───────────────────────────────────────────

    def _handle_player_death(self):
        """Record the link that led to death as a death trap and halt navigation."""
        if not self.current_profile:
            return
        profile = self.profiles.get(self.current_profile, {})
        rooms   = profile.get('rooms', {})
        if self.previous_room_hash and self.last_movement_direction:
            _mark_death_trap(profile, self.previous_room_hash, self.last_movement_direction)
            self.save_profiles()
            prev_name = rooms.get(self.previous_room_hash, {}).get('name', self.previous_room_hash)
            self.append_text(
                f"[Harness: death trap recorded — {self.last_movement_direction} from {prev_name}]\n",
                "system")
        self._active_goto    = None
        self._active_explore = None
        self._update_nav_panel()

    # ── Core survival commands ────────────────────────────────────────

    def _survival_send_cmd(self, cmd):
        """Send a survival command directly (shown with [Survival] prefix)."""
        if not self.connected:
            return
        try:
            self.ssl_socket.sendall((cmd + '\n').encode('utf-8'))
            self.append_text(f"[Survival] {cmd}\n", "system")
        except Exception:
            pass

    # ── Auto-fill fountain ────────────────────────────────────────────

    def _survival_on_room_entered(self, room_hash):
        """Called from process_room_data (main thread) whenever current_room_hash changes."""
        cfg = self._fd_config()

        # Auto-fill drink container when entering fountain room
        fountain = cfg.get('fountain_room')
        container = cfg.get('drink_container')
        if fountain and container and room_hash == fountain:
            self.master.after(400, lambda: self._survival_send_cmd(
                f"fill {container} fountain"))

        # Advance buy-food walk state
        if self._survival_state == 'walking':
            store_room = cfg.get('food_store_room')
            if room_hash == store_room:
                # Arrived — send inventory to count what we have
                self._survival_state = 'inv_wait'
                self._survival_inv_text = ''
                self.master.after(400, lambda: self._survival_send_cmd('inventory'))
            elif self._survival_path:
                # Send next direction step
                direction = self._survival_path.pop(0)
                self.master.after(300, lambda d=direction: self.send_ai_command(d))
            else:
                # Path exhausted but not at destination — map may be stale
                self._survival_state = None
                self.append_text(
                    "[Survival] Walk complete but food store room not reached. "
                    "Is the map up to date?\n", "system")

    # ── Inventory parsing (receive thread) ───────────────────────────

    def _survival_handle_text(self, text):
        """Called from the receive thread to collect inventory output."""
        if self._survival_state not in ('inv_wait', 'eat_inv_wait'):
            return
        self._survival_inv_text += text
        # Wait for a prompt line before processing
        if self.last_line.rstrip().endswith('>'):
            full_text = self._survival_inv_text
            self._survival_inv_text = ''
            msg_type = 'survival_inv' if self._survival_state == 'inv_wait' else 'eat_inv'
            self.message_queue.put((msg_type, full_text))

    # ── Auto-eat / auto-drink ─────────────────────────────────────────

    def _check_hunger_thirst_transitions(self):
        """Detect hunger/thirst state changes and auto-eat/drink."""
        cfg = self._fd_config()
        hunger = self.char_stats.get('hunger')
        thirst = self.char_stats.get('thirst')

        bad_hunger = hunger in ('hungry', 'starving')
        bad_thirst = thirst in ('thirsty', 'parched')
        prev_bad_hunger = self._prev_hunger in ('hungry', 'starving')
        prev_bad_thirst = self._prev_thirst in ('thirsty', 'parched')

        # Trigger on transition into a bad state (not every update)
        if bad_hunger and not prev_bad_hunger:
            food = cfg.get('food_item')
            if food:
                self._survival_send_cmd(f"eat {food}")
                if self._survival_state is None:
                    self._survival_state = 'eat_inv_wait'
                    self._survival_inv_text = ''
                    self.master.after(600, lambda: self._survival_send_cmd('inventory'))

        if bad_thirst and not prev_bad_thirst:
            container = cfg.get('drink_container')
            if container:
                self._survival_send_cmd(f"drink {container}")

        self._prev_hunger = hunger
        self._prev_thirst = thirst

    # ── Buy food flow ─────────────────────────────────────────────────

    def _survival_buy_food(self):
        """Start the buy-food flow: pathfind to store, check inventory, buy."""
        if self._survival_state is not None:
            messagebox.showinfo("Survival", "A survival action is already in progress.")
            return

        cfg = self._fd_config()
        food_item  = cfg.get('food_item', '').strip()
        store_room = cfg.get('food_store_room')

        if not food_item:
            messagebox.showwarning("Survival",
                "Food item not set. Use Settings → Survival → Set Food Item.")
            return
        if not store_room:
            messagebox.showwarning("Survival",
                "Food store room not set. Stand in the store and use "
                "Settings → Survival → Set Food Store Room.")
            return
        if not self.current_room_hash:
            messagebox.showwarning("Survival",
                "Current room unknown. Enable room tracking and move around first.")
            return

        # Already in the store?
        if self.current_room_hash == store_room:
            self._survival_state = 'inv_wait'
            self._survival_inv_text = ''
            self.master.after(300, lambda: self._survival_send_cmd('inventory'))
            return

        path = self._survival_find_path(self.current_room_hash, store_room)
        if path is None:
            messagebox.showwarning("Survival",
                "No mapped path to the food store. Walk there at least once "
                "with room tracking enabled to build the map.")
            return

        self.append_text(
            f"[Survival] Walking to food store ({len(path)} steps)...\n", "system")
        self._survival_state = 'walking'
        self._survival_path = list(path)

        # Send the first step; subsequent steps are triggered by room-entry events
        first = self._survival_path.pop(0)
        self.master.after(300, lambda d=first: self.send_ai_command(d))

    def _survival_start_buying(self, current_count):
        """Begin issuing buy commands after inventory check."""
        cfg = self._fd_config()
        food_item = cfg.get('food_item', '').strip()
        needed = max(0, self._SURVIVAL_MAX_FOOD - current_count)
        if needed == 0:
            self.append_text(
                f"[Survival] Already carrying {current_count} {food_item}. Nothing to buy.\n",
                "system")
            self._survival_state = None
            return
        self.append_text(
            f"[Survival] Carrying {current_count}, buying {needed} {food_item}.\n", "system")
        self._survival_state = 'buying'
        self._survival_buy_count = needed
        self._survival_do_buy()

    def _survival_do_buy(self):
        """Send one buy command and schedule the next."""
        if self._survival_buy_count <= 0 or not self.connected:
            self._survival_state = None
            self.append_text("[Survival] Buy complete.\n", "system")
            return
        cfg = self._fd_config()
        food_item = cfg.get('food_item', '').strip()
        self._survival_send_cmd(f"buy {food_item}")
        self._survival_buy_count -= 1
        self.master.after(350, self._survival_do_buy)

    def _rescue_settings_dialog(self):
        """Open a dialog to configure the rescue command and thresholds."""
        cfg = self._rescue_config()
        dlg = tk.Toplevel(self.master)
        dlg.title("Rescue Settings")
        dlg.resizable(False, False)
        dlg.transient(self.master)
        dlg.grab_set()

        pad = {'padx': 8, 'pady': 4}

        tk.Label(dlg, text="Rescue command:").grid(row=0, column=0, sticky='e', **pad)
        cmd_var = tk.StringVar(value=cfg.get('rescue_command', ''))
        tk.Entry(dlg, textvariable=cmd_var, width=30).grid(row=0, column=1, **pad)

        tk.Label(dlg, text="Fixed HP threshold (0 = off):").grid(row=1, column=0, sticky='e', **pad)
        hp_var = tk.StringVar(value=str(cfg.get('rescue_hp_threshold', 0)))
        tk.Entry(dlg, textvariable=hp_var, width=10).grid(row=1, column=1, sticky='w', **pad)

        tk.Label(dlg, text="Damage multiplier (0 = off):").grid(row=2, column=0, sticky='e', **pad)
        mult_var = tk.StringVar(value=str(cfg.get('rescue_damage_multiplier', 0.0)))
        tk.Entry(dlg, textvariable=mult_var, width=10).grid(row=2, column=1, sticky='w', **pad)
        tk.Label(dlg, text="(rescue if HP < multiplier × opponent's max single hit)",
                 fg='gray').grid(row=3, column=0, columnspan=2, **pad)

        def on_ok():
            try:
                hp_thresh = int(hp_var.get().strip())
            except ValueError:
                messagebox.showerror("Rescue Settings", "HP threshold must be an integer.", parent=dlg)
                return
            try:
                mult = float(mult_var.get().strip())
            except ValueError:
                messagebox.showerror("Rescue Settings", "Damage multiplier must be a number.", parent=dlg)
                return
            cfg['rescue_command'] = cmd_var.get().strip()
            cfg['rescue_hp_threshold'] = hp_thresh
            cfg['rescue_damage_multiplier'] = mult
            self.save_profiles()
            self.append_text(
                f"[Rescue] Settings saved — command: '{cfg['rescue_command']}', "
                f"HP threshold: {hp_thresh}, damage multiplier: {mult}\n", "system")
            dlg.destroy()

        btn_frame = tk.Frame(dlg)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=6)
        tk.Button(btn_frame, text="OK", width=8, command=on_ok).pack(side=tk.LEFT, padx=4)
        tk.Button(btn_frame, text="Cancel", width=8, command=dlg.destroy).pack(side=tk.LEFT, padx=4)

    # ------------------------------------------------------------------
    # Tick timer
    # ------------------------------------------------------------------

    def _on_tick_event(self):
        """Called on the main thread when a tick-boundary message is received.

        Maintains a seconds counter (_tick_count) that starts at 0 on the first
        tick event.  On each subsequent event the counter value is rounded to the
        nearest multiple of 5; if that rounded value is positive and smaller than
        the current limit it becomes the new limit.  The counter always resets to
        0 after the computation.  Duplicate same-tick messages produce a count
        near 0 which rounds to 0 and are therefore ignored automatically.
        """
        if self._tick_count is None:
            # First tick this session — start the counter.
            self._tick_count = 0
            self._start_tick_countdown()
            return

        self._tick_count = 0
        self._start_tick_countdown()

    def _build_tick_panel(self, parent):
        """Build the tick timer section into the top of the right column."""
        BG     = "#1a1a1a"
        FG     = "#d4d4d4"
        HDR_FG = "#4ec9b0"

        tk.Label(parent, text="\u2500 TICK TIMER \u2500", bg=BG, fg=HDR_FG,
                 font=self._font_status_hdr, anchor='w').pack(fill=tk.X, padx=4, pady=(6, 1))

        self._sv_tick_interval = tk.StringVar(value="--")
        self._sv_tick_next     = tk.StringVar(value="--")

        for label, var in (("Interval:", self._sv_tick_interval),
                           ("Next tick:", self._sv_tick_next)):
            row = tk.Frame(parent, bg=BG)
            row.pack(fill=tk.X, padx=4, pady=1)
            tk.Label(row, text=label, bg=BG, fg=FG,
                     font=self._font_status, anchor='w', width=9).pack(side=tk.LEFT)
            tk.Label(row, textvariable=var, bg=BG, fg=FG,
                     font=self._font_status, anchor='w').pack(side=tk.LEFT)

    def _start_tick_countdown(self):
        """Cancel any running countdown and start a fresh one."""
        if self._tick_countdown_job:
            self.master.after_cancel(self._tick_countdown_job)
            self._tick_countdown_job = None
        self._tick_countdown_update()

    def _tick_countdown_update(self):
        """Increment _tick_count every second and update the tick window."""
        if self._tick_count is not None:
            self._tick_count += 1
            if self._tick_interval and self._tick_interval > 0:
                if self._tick_count >= self._tick_interval:
                    self._tick_count = 0
                remaining = self._tick_interval - self._tick_count
                self._sv_tick_interval.set(f"{self._tick_interval}s")
                self._sv_tick_next.set(f"{remaining}s" if remaining > 0 else "now")
            else:
                self._sv_tick_interval.set("--")
                self._sv_tick_next.set("--")
            self._tick_countdown_job = self.master.after(1000, self._tick_countdown_update)
        else:
            self._sv_tick_interval.set("--")
            self._sv_tick_next.set("--")
            self._tick_countdown_job = None

    _KILL_RE = re.compile(r'^.+ is dead!\s+R\.I\.P\.$', re.MULTILINE)

    def _handle_autoloot(self, text):
        """Send loot command after a kill if autoloot is enabled.
        Called from the receive thread — uses after() to send on the main thread."""
        mode = self._autoloot_var.get()
        if mode == 'off':
            return
        if not self._KILL_RE.search(text):
            return
        cmd = 'get gold corpse' if mode == 'gold' else 'get all corpse'
        self.master.after(300, lambda: self._send_autoloot_cmd(cmd))

    def _send_autoloot_cmd(self, cmd):
        """Send the autoloot command on the main thread."""
        if not self.connected:
            return
        try:
            self.ssl_socket.sendall((cmd + '\n').encode('utf-8'))
            self.append_text(f"[Autoloot] {cmd}\n", "system")
        except Exception:
            pass

    # Group join: "Cotu is now a member of Bob's group."
    _GROUP_JOIN_RE = re.compile(
        r'^([A-Z][a-z]+)\s+is\s+now\s+a\s+member\s+of\s+([A-Z][a-z]+)(?:\'s)?\s+group',
        re.IGNORECASE
    )
    # Group leave: "Cotu has left the group."
    _GROUP_LEAVE_RE = re.compile(
        r'^([A-Z][a-z]+)\s+has\s+left\s+(?:the\s+)?group',
        re.IGNORECASE
    )
    # Group disbanded: "Your group has been disbanded."
    _GROUP_DISBAND_RE = re.compile(
        r'your\s+group\s+has\s+been\s+disbanded',
        re.IGNORECASE
    )
    # Player quit: "Cotu has left the game."
    _PLAYER_QUIT_RE = re.compile(
        r'^([A-Z][a-z]+)\s+has\s+left\s+the\s+game',
        re.IGNORECASE
    )

    def _update_group_members(self, text):
        """Parse group join/leave events and maintain self.group_members.
        Called from the receive thread — must not touch UI directly."""
        char_name = (self.profiles.get(self.current_profile, {})
                     .get('character', '')).lower()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = self._GROUP_JOIN_RE.match(line)
            if m:
                joiner = m.group(1).lower()
                leader = m.group(2).lower()
                if joiner not in ('you', char_name):
                    self.group_members.add(joiner)
                if self._group_leader is None:
                    self._group_leader = leader
                continue
            m = self._GROUP_LEAVE_RE.match(line)
            if m:
                name = m.group(1).lower()
                self.group_members.discard(name)
                if self._group_leader and name == self._group_leader:
                    self.message_queue.put(("group_event", "disbanded"))
                    self._group_leader = None
                    self.group_members.clear()
                continue
            if self._GROUP_DISBAND_RE.search(line):
                self.message_queue.put(("group_event", "disbanded"))
                self._group_leader = None
                self.group_members.clear()
                continue
            m = self._PLAYER_QUIT_RE.match(line)
            if m:
                name = m.group(1).lower()
                if self._group_leader and name == self._group_leader:
                    self.message_queue.put(("group_event", "disbanded"))
                    self._group_leader = None
                    self.group_members.clear()

    def _update_mob_combat_stats(self, text):
        """Track per-mob hit/miss/damage/room/aggression stats.
        Called from the receive thread — must not touch UI directly."""
        if not self.current_profile or self.current_profile not in self.profiles:
            return

        # Parse HP from this text chunk for damage calculation
        new_prompt = self.mud_parser.parse_prompt_stats(text)
        new_hp = new_prompt.get('hp') if new_prompt else None

        # Detect hit and miss in this chunk
        hit_mob = self.mud_parser.detect_mob_hit(text)
        miss_mob = self.mud_parser.detect_mob_miss(text)
        active_mob = hit_mob or miss_mob

        if active_mob:
            mob_key = active_mob.lower()
            if mob_key != self._combat_mob:
                self._rescue_sent = False  # new fight — allow rescue again
            self._combat_mob = mob_key

            # Aggression: mob hit/missed us without a recent player kill command
            is_aggressive = False
            if not self._kill_cmd_pending:
                is_aggressive = True
            elif self._kill_cmd_target and self._kill_cmd_target not in mob_key:
                # Kill target doesn't match this mob's name
                is_aggressive = True
            elif (time.time() - self._last_kill_cmd_time) > 5.0:
                # Kill command was too long ago
                is_aggressive = True
                self._kill_cmd_pending = False

            # Get or create mob stats entry
            profile = self.profiles[self.current_profile]
            mob_stats = profile.setdefault('mob_combat_stats', {})
            entry = mob_stats.setdefault(mob_key, {
                'max_hit': 0,
                'hits': 0,
                'misses': 0,
                'rooms': [],
                'aggressive': False,
            })

            if is_aggressive:
                entry['aggressive'] = True

            # Track current room (keep last 10 unique hashes)
            if self.current_room_hash:
                if self.current_room_hash not in entry['rooms']:
                    entry['rooms'].append(self.current_room_hash)
                    if len(entry['rooms']) > 10:
                        entry['rooms'].pop(0)

            if hit_mob:
                entry['hits'] += 1
                # Compute damage from HP delta
                if self._prev_combat_hp is not None and new_hp is not None:
                    damage = self._prev_combat_hp - new_hp
                    if damage > 0 and damage > entry['max_hit']:
                        entry['max_hit'] = damage
            else:
                entry['misses'] += 1

        # Combat-end detection: flee or summon clears fight state so rescue stops.
        if self._combat_mob:
            if self.mud_parser.detect_flee(text):
                self._combat_mob = None
                self._rescue_sent = False
            elif re.search(
                r'(?:has summoned you|you are (?:transported|teleported|summoned))',
                text, re.IGNORECASE
            ):
                self._combat_mob = None
                self._rescue_sent = False

        # Rescue check — evaluated on every HP prompt update during combat,
        # independent of the AI agent.  Dispatched to the main thread via after().
        if new_hp is not None and self._combat_mob and not self._rescue_sent:
            cfg = self._rescue_config()
            rescue_cmd = cfg.get('rescue_command', '').strip()
            if rescue_cmd:
                triggered = False
                reason = ''
                fixed = cfg.get('rescue_hp_threshold', 0)
                if fixed and new_hp < fixed:
                    triggered = True
                    reason = f"HP {new_hp} < fixed threshold {fixed}"
                if not triggered:
                    mult = cfg.get('rescue_damage_multiplier', 0.0)
                    if mult:
                        mob_stats = self.profiles.get(self.current_profile, {}).get('mob_combat_stats', {})
                        max_hit = mob_stats.get(self._combat_mob, {}).get('max_hit', 0)
                        if max_hit > 0 and new_hp < mult * max_hit:
                            triggered = True
                            reason = f"HP {new_hp} < {mult}x max_hit {max_hit} ({mult * max_hit:.0f})"
                if triggered:
                    self._rescue_sent = True
                    self._skill_rescue_flag = True
                    msg = f"[Rescue] {reason} — sending: {rescue_cmd}\n"
                    cmd = rescue_cmd
                    self.master.after(0, lambda m=msg, c=cmd: (
                        self.append_text(m, "error"),
                        self.send_ai_command(c)
                    ))

        # Kill confirmed — note which mob was killed so we can attach XP
        if self._KILL_RE.search(text):
            if self.skill_engine and self.skill_engine.is_active():
                self._skill_target_killed = True
            self._last_killed_mob = self._combat_mob
            self._kill_cmd_pending = False
            self._kill_cmd_target = None
            self._combat_mob = None
            self._rescue_sent = False

        # XP gain — attribute to the most recently killed mob
        xp_gained = self.mud_parser.detect_xp_gain(text)
        if xp_gained and self._last_killed_mob:
            mob_key = self._last_killed_mob
            self._last_killed_mob = None
            profile = self.profiles.get(self.current_profile, {})
            mob_stats = profile.get('mob_combat_stats', {})
            if mob_key in mob_stats:
                entry = mob_stats[mob_key]
                entry['xp_total'] = entry.get('xp_total', 0) + xp_gained
                entry['xp_kills'] = entry.get('xp_kills', 0) + 1
            self.master.after(0, self.save_profiles)
        elif self._KILL_RE.search(text):
            # Kill without XP message in same chunk — save now; XP may arrive next chunk
            self.master.after(0, self.save_profiles)

        # Update prev HP for next round's damage calculation
        if new_hp is not None:
            self._prev_combat_hp = new_hp

    def detect_room_color(self, segments):
        """Detect the color used for room names by looking for bluish colors"""
        # Look for bluish colors in the segments (blues and cyans)
        bluish_colors = []
        for text, color in segments:
            # Check if color is bluish (blue or cyan variants)
            if color.lower() in ['#2472c8', '#3b8eea', '#11a8cd', '#29b8db']:  # Blues and cyans
                if text.strip() and not text.strip().startswith('['):  # Not just whitespace or exit bracket
                    bluish_colors.append(color)

        # Return the most common bluish color found
        if bluish_colors:
            return max(set(bluish_colors), key=bluish_colors.count)
        return None

    _SPELL_KEYWORDS = re.compile(r'\b(armor|bless|sanctuary)\b', re.IGNORECASE)
    _STAT_OUTPUT    = re.compile(
        r'\b(HITROLL|SAVING_SPELL|SAVING_PARA|SAVING_ROD|SAVING_PETRI|SAVING_BREATH)\b'
    )
    _ABILITY_SCORES = re.compile(r'\d+/?\d*(?:\s+\d+){4,}')
    _CORRUPT_EXITS  = re.compile(r'Int:|Str:|Dex:|Con:|Wis:|Cha:')
    _MUD_PROMPT     = re.compile(r'^\d+[Hh]\w*\s+\d+[Mm]\w*.*[>$]', re.IGNORECASE)

    def _is_valid_room_data(self, room_data):
        """Return (True, None) for legitimate rooms; (False, reason) for corrupt ones."""
        name  = room_data.get('name', '')
        desc  = room_data.get('description', '')
        exits = room_data.get('exits', '')

        words = name.split()
        if words and all(w.lower() in ('armor', 'bless', 'sanctuary') for w in words):
            return False, 'spell-buff name'

        if self._STAT_OUTPUT.search(desc) or self._STAT_OUTPUT.search(name):
            return False, 'stat output in description'

        if self._ABILITY_SCORES.search(name):
            return False, 'ability scores in name'

        if self._CORRUPT_EXITS.search(exits):
            return False, 'stat labels in exits field'

        return True, None

    def parse_room_data(self, segments):
        """Parse room data from colored text segments.

        If the profile has a calibrated mud_structure, uses color-based section
        parsing via MUDTextParser.parse_room_block().  Falls back to the legacy
        room-color heuristic otherwise.
        """
        # --- Calibrated path ---
        if self.current_profile and self.current_profile in self.profiles:
            mud_structure = (self.profiles[self.current_profile]
                             .get('ai_config', {})
                             .get('mud_structure', {}))
            if mud_structure.get('room_title'):
                result = self.mud_parser.parse_room_block(segments, mud_structure)
                if result:
                    return result

        # --- Legacy heuristic path ---
        if not self.room_color:
            # Try to detect room color
            detected = self.detect_room_color(segments)
            if detected:
                self.room_color = detected
                self.append_text(f"[Room color detected: {self.room_color}]\n", "system")

                # Save room color to profile
                if self.current_profile and self.current_profile in self.profiles:
                    self.profiles[self.current_profile]['room_color'] = self.room_color
                    self.save_profiles()

                if self.room_tracking_enabled:
                    self._update_status_bar()

        if not self.room_color:
            return None

        # Extract room name (first line with room color)
        room_name = ""
        description_parts = []
        object_parts = []
        exits = ""
        in_description = False
        exits_found = False

        for text, color in segments:
            text_stripped = text.strip()

            # Exit line: contains [...] — capture regardless of color
            if not exits_found and '[' in text and ']' in text:
                exits = text_stripped
                exits_found = True

            # Room-colored text: name or (fallback) description
            elif color == self.room_color:
                if not room_name and text_stripped and not text_stripped.startswith('['):
                    room_name = text_stripped
                    in_description = True

            else:
                if in_description and text_stripped:
                    if not exits_found:
                        # Before exits line: room description
                        description_parts.append(text_stripped)
                    else:
                        # After exits line: objects / mobs on the ground
                        if not self._MUD_PROMPT.match(text_stripped):
                            object_parts.append(text_stripped)

        description = ' '.join(description_parts)

        if room_name:
            # Normalize description for stable hashing
            # Remove extra whitespace, normalize line breaks
            normalized_description = ' '.join(description.split())

            return {
                'name': room_name,
                'description': description,  # Keep original for display
                'normalized_description': normalized_description,  # Use for hashing
                'exits': exits,
                'objects': object_parts,
            }

        return None

    def _parse_and_queue_stats(self, text):
        """Parse stat/combat data from MUD text and queue a status panel update.
        Called from the receive thread — must not touch UI directly."""
        updates = {}

        # Prompt stats: current HP/MP/MV, tank%, opp%
        prompt = self.mud_parser.parse_prompt_stats(text)
        if prompt:
            for k in ('hp', 'mp', 'mv'):
                if k in prompt:
                    updates[k] = prompt[k]
            if 'tank' in prompt:
                updates['tank'] = prompt['tank']
            if 'opp' in prompt:
                updates['opp'] = prompt['opp']
                updates['fighting'] = prompt['opp'] > 0

        # Score block: max values, level, AC, alignment, xp, gold, hunger, thirst, spells
        if self.mud_parser.is_score_block(text):
            score = self.mud_parser.parse_score(text)
            if score:
                for k in ('level', 'ac', 'alignment', 'xp', 'xp_next',
                          'gold', 'max_hp', 'max_mp', 'max_mv'):
                    if k in score:
                        updates[k] = score[k]
                # Score always gives a definitive hunger/thirst state (OK if absent)
                updates['hunger'] = score.get('hunger', 'OK')
                updates['thirst'] = score.get('thirst', 'OK')
            # When GMCP Defences are active, let GMCP own the spells dict — score's
            # SPL lines are less accurate and would overwrite real-time GMCP data.
            if not self.gmcp_active:
                spells = self.mud_parser.parse_spell_affects(text)
                updates['spells'] = spells  # replace entirely from score output
        else:
            # Not a score block — check for spontaneous "You are thirsty/hungry"
            # messages the MUD sends between auto-score calls.
            ht = self.mud_parser.detect_hunger_thirst(text)
            updates.update(ht)

        # Buff application and expiration messages (text-based).
        # Skip when GMCP Defences are active: Char.Defences.Add/Remove provide
        # accurate durations; text heuristics would apply wrong default tick counts.
        if not self.gmcp_active:
            buff_events = self.mud_parser.detect_buff_events(text)
            if buff_events['applied']:
                updates['spells_applied'] = buff_events['applied']
            if buff_events['expired']:
                updates['spells_expired'] = buff_events['expired']

        # Combat end detection
        if re.search(r'\b(?:is dead|has fled|you flee|you stop fighting)\b',
                     text, re.IGNORECASE):
            updates.setdefault('fighting', False)
            updates.setdefault('tank', None)
            updates.setdefault('opp', None)

        # Death detection — queue separate event so main thread can record the trap
        if re.search(r'\byou are dead\b|good-bye cruel world', text, re.IGNORECASE):
            self.message_queue.put(("player_died", None))

        if updates:
            self.message_queue.put(("stats", updates))

    def _migrate_room_to_vnum(self, vnum_key, name):
        """Re-key a hash-keyed room to vnum_key when exactly one name match is found.

        Scans profile rooms for hash-keyed entries (keys not starting with 'vnum:')
        whose stored name matches the given name.  If exactly one such entry exists,
        it is moved to vnum_key and every room_link destination pointing to the old
        hash is updated to point to vnum_key instead.
        """
        if not self.current_profile or self.current_profile not in self.profiles:
            return
        profile = self.profiles[self.current_profile]
        rooms = profile.get('rooms', {})
        room_links = profile.get('room_links', {})

        # Find hash-keyed rooms with matching name
        matches = [k for k, v in rooms.items()
                   if not k.startswith('vnum:') and v.get('name') == name]
        if len(matches) != 1:
            return  # ambiguous or no match — leave unchanged

        old_key = matches[0]
        # Move room record
        rooms[vnum_key] = rooms.pop(old_key)
        # Move link record for this room (outgoing links from old_key)
        if old_key in room_links:
            room_links[vnum_key] = room_links.pop(old_key)
        # Update all incoming links (destinations) pointing to old_key
        for src_links in room_links.values():
            for direction, val in list(src_links.items()):
                dest, assumed = _link_dest(val)
                if dest == old_key:
                    src_links[direction] = {"dest": vnum_key, "assumed": assumed}
        # Update entry_room pointer if it pointed to the old hash
        if profile.get('entry_room') == old_key:
            profile['entry_room'] = vnum_key
        self.append_text(f"[GMCP] Migrated room '{name}' from hash to {vnum_key}\n", "system")

    def _process_gmcp_room(self, room_info):
        """Handle a GMCP Room.Info packet (called from process_queue, main thread).

        Uses vnum as the room key (format 'vnum:N').  Mirrors the room-entry
        logic in process_room_data but uses GMCP data as the authoritative source.
        """
        if not self.room_tracking_enabled:
            return
        if not self.current_profile or self.current_profile not in self.profiles:
            return

        vnum = room_info.get('num')
        if vnum is None:
            return
        vnum_key = f"vnum:{vnum}"
        name    = room_info.get('name', '')
        exits   = room_info.get('exits', '')
        zone    = room_info.get('zone', '')
        terrain = room_info.get('terrain', '')

        profile = self.profiles[self.current_profile]
        if 'rooms' not in profile:
            profile['rooms'] = {}
        if 'room_links' not in profile:
            profile['room_links'] = {}

        rooms      = profile['rooms']
        room_links = profile['room_links']

        is_new_room = vnum_key not in rooms
        if is_new_room:
            # Attempt to migrate an existing hash-keyed room with the same name
            self._migrate_room_to_vnum(vnum_key, name)
            is_new_room = vnum_key not in rooms  # re-check after migration
            if is_new_room:
                rooms[vnum_key] = {
                    'name':    name,
                    'exits':   exits,
                    'zone':    zone,
                    'terrain': terrain,
                }
        else:
            # Update exits/zone/terrain in case they changed
            rooms[vnum_key]['exits']   = exits
            rooms[vnum_key]['zone']    = zone
            rooms[vnum_key]['terrain'] = terrain

        # Handle entry room detection (first room seen after login/reconnect)
        if self.detect_entry_room:
            profile['entry_room'] = vnum_key
            self.save_profiles()
            self.append_text(f"[GMCP] Entry room: {name} ({vnum_key})\n", "system")
            self.detect_entry_room = False
            self.current_room_hash = vnum_key

        # Handle directional links when we moved from another room
        elif self.previous_room_hash and self.last_movement_direction:
            direction = self.last_movement_direction
            if self.previous_room_hash not in room_links:
                room_links[self.previous_room_hash] = {}

            existing = room_links[self.previous_room_hash].get(direction)
            _, assumed = _link_dest(existing)
            if existing is None or assumed:
                room_links[self.previous_room_hash][direction] = {"dest": vnum_key, "assumed": False}

            rev = _REVERSE_DIR.get(direction)
            if rev:
                rev_links = room_links.setdefault(vnum_key, {})
                rev_existing = rev_links.get(rev)
                _, rev_assumed = _link_dest(rev_existing)
                if rev_existing is None or rev_assumed:
                    rev_links[rev] = {"dest": self.previous_room_hash, "assumed": True}

            self.current_room_hash = vnum_key

            if is_new_room:
                self.save_profiles()
                self._update_status_bar()
                self.append_text(
                    f"[GMCP] New room: {name} ({vnum_key}, Total: {len(rooms)})\n", "system")
            else:
                self.save_profiles()
                self.append_text(f"[GMCP] Moved {direction} to: {name} ({vnum_key})\n", "system")

            self.last_movement_direction = None

        else:
            # Look or teleport — update current room but don't create a directional link
            self.current_room_hash = vnum_key
            if is_new_room:
                self.save_profiles()
                self._update_status_bar()
                self.append_text(
                    f"[GMCP] New room: {name} ({vnum_key}, Total: {len(rooms)})\n", "system")

        self._pending_desc_segments = []
        self._expecting_room_description = True
        self.expecting_room_data = False
        self._fire_deferred_skill()

        self._survival_on_room_entered(self.current_room_hash)
        self._update_nav_panel()

        if self.ai_agent and self.ai_agent.is_running:
            self.ai_agent.on_room_entered(self.current_room_hash, {
                'name':    name,
                'exits':   exits,
                'zone':    zone,
                'terrain': terrain,
            })

    def process_room_data(self, segments):
        """Process and store room data"""
        if not self.room_tracking_enabled:
            return
        if self.gmcp_active:
            # GMCP has already updated current_room_hash; just grab the description
            # from the text and write it directly to the current room.
            if not self._expecting_room_description:
                return
            if not self.current_room_hash or not self.current_profile:
                return
            # Accumulate segments across multiple recv() chunks (GMCP and text can
            # arrive in separate TCP packets, so the first call may have empty data).
            self._pending_desc_segments.extend(segments)
            room_data = self.parse_room_data(self._pending_desc_segments)
            if not room_data or not room_data.get('name'):
                return
            # Got a full parse — commit and clear
            self._expecting_room_description = False
            self._pending_desc_segments = []
            desc = room_data.get('description', '')
            rooms = self.profiles[self.current_profile].get('rooms', {})
            stored = rooms.get(self.current_room_hash)
            stored_name = stored.get('name', '') if stored else None
            if stored and stored_name == room_data.get('name', '') and desc:
                stored['description'] = desc
            return

        if not self.expecting_room_data:
            return
        if not self.current_profile or self.current_profile not in self.profiles:
            return

        # Parse room data
        room_data = self.parse_room_data(segments)

        if not room_data:
            if self.detect_entry_room:
                # Still in the login flow — parse failure is expected (MOTD, banners,
                # etc.).  Stay armed so the actual entry room is caught when it arrives.
                pass
            else:
                # Only give up if the batch contains both a MUD prompt AND an exits
                # bracket.  A prompt alone (no exits) means a pager page finished or
                # some other intermediate text arrived — keep armed so the actual room
                # block that follows is still caught.  An exits bracket signals the
                # server genuinely tried to send room data that we failed to parse.
                batch_plain = ' '.join(t for t, _ in segments)
                has_prompt = self.mud_parser.parse_prompt_stats(batch_plain) is not None
                has_exits  = any('[' in t and ']' in t for t, _ in segments)
                if has_prompt and has_exits:
                    colors_seen = list(dict.fromkeys(c for _, c in segments if _.strip()))
                    self.append_text(
                        f"[Room parse failed] room_color={self.room_color!r} "
                        f"colors_in_segments={colors_seen}\n", "system")
                    self.expecting_room_data = False

        if room_data:
            valid, reason = self._is_valid_room_data(room_data)
            if not valid:
                self.append_text(
                    f"[Room rejected ({reason}): {room_data.get('name', '')!r}]\n", "system")
                self.expecting_room_data = False
                return

            # Create hash of room data using normalized description for stability
            room_string = f"{room_data['name']}|{room_data['normalized_description']}|{room_data['exits']}"
            room_hash = hashlib.sha256(room_string.encode()).hexdigest()

            # Initialize rooms dict if needed
            if 'rooms' not in self.profiles[self.current_profile]:
                self.profiles[self.current_profile]['rooms'] = {}
            if 'room_links' not in self.profiles[self.current_profile]:
                self.profiles[self.current_profile]['room_links'] = {}

            # Store or update room data
            rooms = self.profiles[self.current_profile]['rooms']
            is_new_room = room_hash not in rooms

            if is_new_room:
                # Store room data (without the normalized version - we only need that for hashing)
                rooms[room_hash] = {
                    'name': room_data['name'],
                    'description': room_data['description'],
                    'exits': room_data['exits']
                }

            # Always update description so rooms mapped before description capture
            # was added get backfilled as the character re-enters them.
            if room_data.get('description'):
                rooms[room_hash]['description'] = room_data['description']

            # Always update volatile fields (mob presence and objects change each visit)
            if room_data.get('mob_lines'):
                rooms[room_hash]['mob_lines'] = room_data['mob_lines']
            elif 'mob_lines' in rooms[room_hash]:
                rooms[room_hash]['mob_lines'] = []
            if room_data.get('objects'):
                rooms[room_hash]['objects'] = room_data['objects']
            elif 'objects' in rooms[room_hash]:
                rooms[room_hash]['objects'] = []

            # Handle entry room detection
            if self.detect_entry_room:
                self.profiles[self.current_profile]['entry_room'] = room_hash
                self.save_profiles()
                self.append_text(f"[Entry room detected: {room_data['name']}]\n", "system")
                self.detect_entry_room = False
                self.current_room_hash = room_hash

            # Handle directional links (if we moved from another room)
            elif self.previous_room_hash and self.last_movement_direction:
                # Create link from previous room to current room
                room_links = self.profiles[self.current_profile]['room_links']

                # Initialize previous room's links if needed
                if self.previous_room_hash not in room_links:
                    room_links[self.previous_room_hash] = {}

                # Store the directional link (confirm it; promote assumed→confirmed)
                direction = self.last_movement_direction
                existing = room_links[self.previous_room_hash].get(direction)
                _, assumed = _link_dest(existing)
                if existing is None or assumed:
                    room_links[self.previous_room_hash][direction] = {"dest": room_hash, "assumed": False}

                # Create assumed reverse link so nav pane can show it immediately
                rev = _REVERSE_DIR.get(direction)
                if rev:
                    rev_links = room_links.setdefault(room_hash, {})
                    rev_existing = rev_links.get(rev)
                    _, rev_assumed = _link_dest(rev_existing)
                    if rev_existing is None or rev_assumed:
                        rev_links[rev] = {"dest": self.previous_room_hash, "assumed": True}

                # Update current room
                self.current_room_hash = room_hash

                if is_new_room:
                    self.save_profiles()
                    self._update_status_bar()
                    self.append_text(f"[New room mapped: {room_data['name']} (Total: {len(rooms)})]\n", "system")
                else:
                    self.save_profiles()
                    self.append_text(f"[Moved {direction} to: {room_data['name']}]\n", "system")

                # Reset movement tracking
                self.last_movement_direction = None

            else:
                # Just looking at current room
                self.current_room_hash = room_hash
                if is_new_room:
                    self.save_profiles()
                    self._update_status_bar()
                    self.append_text(f"[New room mapped: {room_data['name']} (Total: {len(rooms)})]\n", "system")

            self.expecting_room_data = False
            self._fire_deferred_skill()

            # Survival hook — fountain fill and buy-food walk progression
            self._survival_on_room_entered(self.current_room_hash)
            self._update_nav_panel()

            if self.ai_agent and self.ai_agent.is_running:
                self.ai_agent.on_room_entered(self.current_room_hash, room_data)

    def process_queue(self):
        """Process messages from the receive thread"""
        try:
            while True:
                msg_type, msg_data = self.message_queue.get_nowait()

                if msg_type == "data":
                    # Detect teleport/summon — set expecting_room_data so the
                    # incoming room is parsed and on_room_entered fires normally.
                    if self.room_tracking_enabled and not self.expecting_room_data:
                        plain = ' '.join(t for t, _ in msg_data)
                        if re.search(
                            r'(?:has summoned you|you are (?:transported|teleported|summoned))',
                            plain, re.IGNORECASE
                        ):
                            self.expecting_room_data = True
                            # No last_movement_direction — parsed as a look/teleport,
                            # so no room link is recorded but on_room_entered still fires.

                    # Cancel room-parse expectation on movement failure messages
                    # ("Alas, you cannot go that way...") before attempting parse.
                    if self.expecting_room_data:
                        plain = ' '.join(t for t, _ in msg_data)
                        if self.mud_parser.detect_move_fail(plain):
                            refused = self.last_movement_direction
                            self.expecting_room_data = False
                            self.last_movement_direction = None
                            # Check whether the refused direction is a phantom
                            # link (exists in room_links but not in the room's
                            # actual MUD exits string).  If so, clean it up
                            # automatically rather than asking the LLM to act.
                            if (refused and self.current_room_hash
                                    and self.current_profile):
                                profile = self.profiles.get(
                                    self.current_profile, {})
                                room = (profile.get('rooms', {})
                                        .get(self.current_room_hash, {}))
                                m = _EXIT_DIR_RE.search(
                                    room.get('exits', ''))
                                raw = (m.group(1).split()
                                       if m and m.group(1) != 'none' else [])
                                listed = {self.direction_map.get(
                                    d.lower(), d.lower()) for d in raw}
                                if refused not in listed:
                                    # Direction not in the exits string.
                                    # Only remove assumed links — a confirmed
                                    # link to a known room may be a closed door
                                    # and should be left for the LLM to handle.
                                    room_links = profile.get('room_links', {})
                                    link_val = room_links.get(
                                        self.current_room_hash, {}).get(refused)
                                    _, is_assumed = _link_dest(link_val) if link_val is not None else (None, True)
                                    if link_val is not None and is_assumed:
                                        del room_links[
                                            self.current_room_hash][refused]
                                        self.save_profiles()
                                        room_name = room.get(
                                            'name', self.current_room_hash)
                                        self.append_text(
                                            f"[Harness: removed phantom"
                                            f" '{refused}' link from"
                                            f" {room_name}]\n", "system")
                                        self._active_goto = None
                                        self._active_explore = None
                                        self._update_nav_panel()
                                    else:
                                        self._refused_direction = refused
                                else:
                                    self._refused_direction = refused
                            else:
                                self._refused_direction = refused
                            self._fire_deferred_skill()
                        elif self.mud_parser.detect_darkness(plain):
                            # Dark room — we moved here but can't parse it.
                            # Clear expecting flag and let AI know we're stuck.
                            self.expecting_room_data = False
                            self.last_movement_direction = None
                            self._fire_deferred_skill()
                            if self.ai_agent and self.ai_agent.is_running:
                                self.ai_agent.on_text_received(plain)

                    # Process room data if we're expecting it (or expecting description via GMCP)
                    if self.room_tracking_enabled and (self.expecting_room_data or self._expecting_room_description):
                        self.process_room_data(msg_data)

                    filtered = self._filter_display_segments(msg_data)
                    if filtered:
                        self.append_text(filtered, "mud_colored")
                elif msg_type == "ai_text":
                    if self.ai_agent:
                        self.ai_agent.on_text_received(msg_data)
                elif msg_type == "telnet":
                    self.append_text(msg_data + "\n", "telnet")
                elif msg_type == "gmcp_room":
                    self._process_gmcp_room(msg_data)
                elif msg_type == "error":
                    self.append_text(msg_data, "error")
                    self.disconnect()
                elif msg_type == "stats":
                    applied     = msg_data.pop('spells_applied', [])
                    expired     = msg_data.pop('spells_expired', [])
                    spells_add  = msg_data.pop('spells_add', {})
                    self.char_stats.update(msg_data)
                    if applied:
                        spells = self.char_stats.setdefault('spells', {})
                        for s in applied:
                            spells[s] = self.mud_parser.BUFF_DEFAULT_TICKS.get(s, 4)
                    if spells_add:
                        spells = self.char_stats.setdefault('spells', {})
                        spells.update(spells_add)
                    if expired:
                        spells = self.char_stats.get('spells', {})
                        for s in expired:
                            spells.pop(s, None)
                        self.char_stats['spells'] = spells
                    self._update_status_panel()
                    self._check_hunger_thirst_transitions()
                elif msg_type == "survival_inv":
                    # Inventory text collected after arriving at food store
                    food_item = self._fd_config().get('food_item', '').strip()
                    if food_item and self._survival_state == 'inv_wait':
                        count = self.mud_parser.parse_inventory_count(msg_data, food_item)
                        self._survival_state = None
                        if count is None:
                            self.append_text(
                                "[Survival] Could not parse inventory. "
                                "Try buying manually.\n", "system")
                        else:
                            self._survival_start_buying(count)
                elif msg_type == "eat_inv":
                    food_item = self._fd_config().get('food_item', '').strip()
                    if food_item and self._survival_state == 'eat_inv_wait':
                        count = self.mud_parser.parse_inventory_count(msg_data, food_item)
                        self._survival_state = None
                        if count is not None and count <= 2:
                            if count == 0:
                                self.append_text(
                                    f"[Survival] No {food_item} left in inventory! "
                                    "Visit the food store.\n", "system")
                            else:
                                self.append_text(
                                    f"[Survival] Only {count} {food_item} left in "
                                    "inventory.\n", "system")
                elif msg_type == "player_died":
                    self._handle_player_death()
                elif msg_type == "group_event":
                    if msg_data == "disbanded":
                        if (self.skill_engine and
                                self.skill_engine.is_active() and
                                self.skill_engine.active_name() == "group_tank"):
                            self.skill_engine.stop()
                            self.append_text(
                                "[Skill] Group disbanded; returning to _default.\n", "system")
                            self.master.after(100, self._start_default_skill)
                elif msg_type == "tick_event":
                    self._on_tick_event()
                elif msg_type == "disconnect":
                    self.append_text(msg_data, "system")
                    self.disconnect()

        except queue.Empty:
            pass
        finally:
            self.master.after(100, self.process_queue)

    def send_message(self, event=None):
        """Send message to MUD server"""
        if not self.connected:
            return

        message = self.input_entry.get()
        if not message:
            return

        # Debug command — doesn't send to server
        if message.lower().strip() == 'aidebug':
            self.input_entry.delete(0, tk.END)
            self._dump_ai_debug()
            return

        # setlandmark <name> — save current room as a named goto: landmark
        if message.lower().startswith('setlandmark '):
            self.input_entry.delete(0, tk.END)
            self._cmd_setlandmark(message[len('setlandmark '):].strip())
            return

        # Direct LLM prompt — starts with \
        if message.startswith('\\'):
            self.input_entry.delete(0, tk.END)
            text = message[1:].strip()
            if not text:
                return
            self.append_text(f"\\ {text}\n", "user")
            self.session_logger.log_command(f"\\ {text}")
            engine = self._ensure_skill_engine()
            if engine.is_active():
                engine.inject_user_message(text)
            else:
                self._start_default_skill()
                self.master.after(100, lambda t=text: self.skill_engine.inject_user_message(t))
            return

        try:
            message_lower = message.lower().strip()

            # Record in command history (skip duplicates of the immediately previous entry)
            if not self._cmd_history or self._cmd_history[-1] != message:
                self._cmd_history.append(message)
            self._cmd_history_pos = -1

            # Update persistent frequency scores (exclude movement commands)
            if message_lower not in self.movement_commands:
                self._record_cmd_score(message)

            # If in quit sequence and user manually sends something, learn it
            if self.quit_pending and self.last_line:
                self.learn_quit_response(self.last_line, message)

            # Track kill commands for aggression detection
            kill_match = re.match(r'^(?:kill|k)\s+(.+)', message_lower)
            if kill_match:
                self._kill_cmd_pending = True
                self._kill_cmd_target = kill_match.group(1).strip()
                self._last_kill_cmd_time = time.time()

            # Speedwalk: a string of direction tokens (2+ steps) expands to one
            # command per step.  Each token is an optional repeat count followed
            # by a direction letter, e.g. "3n2ew" -> n n n e w
            _sw_tokens = re.fullmatch(r'(\d*[nsewud])+', message_lower)
            if _sw_tokens:
                steps = [m.group(2)
                         for m in re.finditer(r'(\d*)([nsewud])', message_lower)
                         for _ in range(int(m.group(1)) if m.group(1) else 1)]
                if len(steps) > 1 or (len(steps) == 1 and steps[0] != message_lower):
                    self.input_entry.delete(0, tk.END)
                    payload = "".join(s + "\n" for s in steps)
                    self.ssl_socket.sendall(payload.encode('utf-8'))
                    self.append_text(f"> {message_lower}  [speedwalk: {len(steps)} steps]\n", "user")
                    self._pending_command = steps[-1]
                    self._response_buffer = []
                    return

            # Track movement/look commands for room tracking
            if self.room_tracking_enabled and message_lower in self.movement_commands:
                self.last_command = message_lower
                self.expecting_room_data = True

                # Track direction if it's a movement command (not look)
                if message_lower in self.direction_map and message_lower not in ['l', 'look']:
                    self.previous_room_hash = self.current_room_hash
                    self.last_movement_direction = self.direction_map[message_lower]
                else:
                    # Just looking, not moving
                    self.last_movement_direction = None

            # Send message with newline
            self.ssl_socket.sendall((message + "\n").encode('utf-8'))
            self.append_text(f"> {message}\n", "user")
            # Record for LLM advisor trigger on next MUD prompt
            self._pending_command = message
            self._response_buffer = []
            self.input_entry.delete(0, tk.END)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to send message: {str(e)}")
            self.disconnect()

    def send_ai_command(self, command):
        """Send a command programmatically from the AI agent."""
        if not self.connected:
            return False
        if self.quit_pending:
            return False
        command_lower = command.lower().strip()
        if self.room_tracking_enabled and command_lower in self.movement_commands:
            self.last_command = command_lower
            self.expecting_room_data = True
            if command_lower in self.direction_map and command_lower not in ['l', 'look']:
                self.previous_room_hash = self.current_room_hash
                self.last_movement_direction = self.direction_map[command_lower]
            else:
                self.last_movement_direction = None
        try:
            self.ssl_socket.sendall((command + "\n").encode('utf-8'))
            self.session_logger.log_ai_command(command)
            self.append_text(f"[AI] > {command}\n", "system")
            return True
        except Exception as e:
            self.append_text(f"[AI] Send failed: {str(e)}\n", "error")
            return False

    # ------------------------------------------------------------------
    # LLM Advisor (streaming UI helpers used by llm_advisor.py backend)
    # ------------------------------------------------------------------

    def begin_advisor_stream(self):
        """Open a new streaming advisor entry in the advisor pane."""
        self._advisor_streamed = True
        self.advisor_area.config(state=tk.NORMAL)
        self.advisor_area.insert(tk.END, "[Advisor] ", "advisor_prefix")
        self.advisor_area.tag_config("advisor_prefix", foreground="#89d185",
                                     font=("Courier", 10, "bold"))
        self._advisor_stream_start = self.advisor_area.index(tk.END)

    def append_advisor_token(self, text):
        """Append a streaming token to the advisor pane."""
        self.advisor_area.insert(tk.END, text)
        self.advisor_area.see(tk.END)

    def end_advisor_stream(self):
        """Close the current streaming advisor entry."""
        if self._advisor_stream_start:
            end_idx = self.advisor_area.index(tk.END)
            self.advisor_area.tag_add("advisor_body",
                                      self._advisor_stream_start, end_idx)
            self.advisor_area.tag_config("advisor_body", foreground="#c8c8c8")
            self._advisor_stream_start = None
        self.advisor_area.insert(tk.END, "\n\n")
        self.advisor_area.see(tk.END)
        self.advisor_area.config(state=tk.DISABLED)

    def cancel_advisor_stream(self):
        """Remove a started-but-failed streaming entry from the advisor pane."""
        if self._advisor_stream_start:
            # _advisor_stream_start points just after "[Advisor] " (10 chars),
            # so walk back to delete the prefix too.
            row, col = self._advisor_stream_start.split('.')
            prefix_col = max(0, int(col) - 10)
            self.advisor_area.delete(f"{row}.{prefix_col}", tk.END)
            self._advisor_stream_start = None
        self._advisor_streamed = False
        self.advisor_area.config(state=tk.DISABLED)

    def _on_advisor_result(self, advice):
        """Called on the main thread when the LLM advisor streams a response."""
        if advice:
            if not self._advisor_streamed:
                self.append_advisor_text(advice)
            self.session_logger.log_advisor(advice)
        self._advisor_streamed = False

    # ------------------------------------------------------------------
    # LLM Skills
    # ------------------------------------------------------------------

    def _ensure_skill_engine(self):
        if self.skill_engine is None:
            from skill_engine import SkillEngine
            self.skill_engine = SkillEngine(self)
        return self.skill_engine

    def _start_default_skill(self):
        """Start the default ambient skill if no other skill is running."""
        if not self.connected:
            return
        if not self.llm_advisor or not self.llm_advisor.is_available():
            return
        if self.skill_engine and self.skill_engine.is_active():
            return
        profile = self.profiles.get(self.current_profile, {})
        skills = profile.setdefault("skills", {})
        if "_default" not in skills:
            skills["_default"] = DEFAULT_SKILL_CFG
            self.save_profiles()
        self._start_skill_core("_default", skills["_default"])

    def _current_skills(self):
        """Return the skills dict for the current profile (creating it if needed)."""
        if not self.current_profile or self.current_profile not in self.profiles:
            return {}
        return self.profiles[self.current_profile].setdefault('skills', {})

    def _current_skill_templates(self):
        """Return the skill_templates dict for the current profile."""
        if not self.current_profile or self.current_profile not in self.profiles:
            return {}
        return self.profiles[self.current_profile].setdefault('skill_templates', {})

    def _current_skill_targets(self):
        """Return the skill_targets dict for the current profile."""
        if not self.current_profile or self.current_profile not in self.profiles:
            return {}
        return self.profiles[self.current_profile].setdefault('skill_targets', {})

    def _rebuild_skills_menu(self):
        """Populate the Settings → Skills submenu based on the current profile."""
        menu = self._skills_menu
        menu.delete(0, tk.END)
        menu.add_command(label="Manage Skills...", command=self._open_skills_dialog)
        menu.add_separator()
        skills = self._current_skills()
        targets = self._current_skill_targets()
        active = self.skill_engine.active_name() if self.skill_engine else None
        if skills or targets:
            run_menu = tk.Menu(menu, tearoff=0)
            menu.add_cascade(label="Run", menu=run_menu)
            for name in sorted(skills.keys()):
                run_menu.add_command(
                    label=name,
                    command=lambda n=name: self._start_skill(n)
                )
            if targets:
                if skills:
                    run_menu.add_separator()
                for tname in sorted(targets.keys()):
                    run_menu.add_command(
                        label=f"{tname}  (target)",
                        command=lambda n=tname: self._start_skill_target(n)
                    )
        else:
            menu.add_command(label="Run  (no skills defined)", state=tk.DISABLED)
        menu.add_separator()
        if active:
            menu.add_command(label=f"Stop: {active}", command=self._stop_skill)
        else:
            menu.add_command(label="Stop", state=tk.DISABLED)

    def _open_skills_dialog(self):
        if not self.current_profile or self.current_profile not in self.profiles:
            messagebox.showwarning("Skills", "Please select a profile first.")
            return
        SkillsDialog(self.master, self)

    def _start_skill(self, name):
        skills = self._current_skills()
        cfg = skills.get(name)
        if not cfg:
            messagebox.showwarning("Skills", f"Skill '{name}' not found.")
            return
        self._start_skill_core(name, cfg)

    def _start_skill_target(self, target_name):
        """Render a skill_target through its template and start it."""
        from skill_engine import render_skill
        targets = self._current_skill_targets()
        templates = self._current_skill_templates()
        target = targets.get(target_name)
        if not target:
            messagebox.showwarning("Skills", f"Target '{target_name}' not found.")
            return
        tmpl_name = target.get("template")
        tmpl = templates.get(tmpl_name) if tmpl_name else None
        if not tmpl:
            messagebox.showwarning(
                "Skills",
                f"Target '{target_name}' references missing template "
                f"'{tmpl_name}'.")
            return
        try:
            cfg = render_skill(tmpl, target.get("params", {}))
        except KeyError as e:
            messagebox.showwarning("Skills", str(e))
            return
        self._start_skill_core(target_name, cfg)

    def _start_skill_core(self, name, cfg):
        if not self.connected:
            messagebox.showwarning("Skills", "Connect to the MUD before starting a skill.")
            return
        if not self.llm_advisor or not self.llm_advisor.is_available():
            messagebox.showwarning("Skills", "Configure the LLM (AI Config) first.")
            return
        engine = self._ensure_skill_engine()
        if engine.is_active():
            if engine.active_name() == "_default":
                engine.stop()
                self.append_text("[Skill] _default stopped; starting new skill.\n", "system")
            else:
                messagebox.showwarning("Skills",
                                       f"A skill is already active: {engine.active_name()}.\n"
                                       "Stop it first.")
                return
        engine.start(name, cfg)
        self._skill_rescue_flag = False
        self._skill_target_killed = False
        self.append_text(f"[Skill] started: {name}\n", "system")
        self.session_logger.log_command(f"[Skill start] {name}")
        # Fire an immediate first turn so the LLM can plan, even before the
        # next MUD prompt arrives.
        self.master.after(50, self._trigger_skill)

    def _stop_skill(self):
        if self.skill_engine and self.skill_engine.is_active():
            name = self.skill_engine.active_name()
            self.skill_engine.stop()
            self.append_text(f"[Skill] stopped: {name}\n", "system")
            self.session_logger.log_command(f"[Skill stop] {name}")
            if name != "_default":
                self.master.after(100, self._start_default_skill)

    _SPEEDWALK_RE = re.compile(r'^\s*(?:\d*[nsewudNSEWUD]\s*)+$')
    _SPEEDWALK_STEP_RE = re.compile(r'(\d*)([nsewudNSEWUD])')

    def _expand_speedwalk(self, cmd):
        """If cmd is a speedwalk string like '5n3w4s', return a list of single-
        direction commands. Return None if cmd is not a speedwalk.

        Only expand if the whole command consists of (optional count + direction)
        tokens and at least one token has a count > 1 OR there are multiple
        tokens. A plain 'n' is returned as None so it dispatches unchanged.
        """
        if not isinstance(cmd, str) or not self._SPEEDWALK_RE.match(cmd):
            return None
        steps = []
        tokens = 0
        has_count = False
        for m in self._SPEEDWALK_STEP_RE.finditer(cmd):
            tokens += 1
            n = int(m.group(1)) if m.group(1) else 1
            if m.group(1) and n > 1:
                has_count = True
            steps.extend([m.group(2).lower()] * n)
        if tokens <= 1 and not has_count:
            return None
        return steps

    def _fire_deferred_skill(self):
        """Fire a skill turn that was deferred while expecting room data."""
        if self._room_wait_timeout_id is not None:
            self.master.after_cancel(self._room_wait_timeout_id)
            self._room_wait_timeout_id = None
        if self._skill_trigger_pending:
            self._skill_trigger_pending = False
            if self.skill_engine and self.skill_engine.is_active():
                self.master.after(0, self._trigger_skill)

    def _room_wait_timeout(self):
        """Safety valve: room data never arrived after a movement command.

        Clear the expectation flag and fire the deferred skill turn so the
        LLM isn't silently stuck waiting for a room description that will
        never come (e.g. MUD rejected the move with an unrecognised message).
        """
        self._room_wait_timeout_id = None
        if self._skill_trigger_pending:
            self.expecting_room_data = False
            self.last_movement_direction = None
            self.append_text(
                "[Harness: room-wait timeout — move response unrecognised, "
                "resuming skill turn]\n", "system")
            self._fire_deferred_skill()

    def _trigger_skill(self):
        """Feed the current skill engine one turn of context."""
        engine = self.skill_engine
        if not engine or not engine.is_active():
            return
        mud_lines = list(self._response_buffer)
        # Drain the buffer every turn — the advisor no longer runs, so the
        # skill engine is the sole consumer.  Also clear _pending_command so
        # the stale human command doesn't linger between turns.
        self._response_buffer = []
        self._pending_command = None
        # If a direction was refused since the last turn, inject a harness hint
        # so the LLM knows exactly which direction to markblocked.
        if self._refused_direction:
            mud_lines.insert(0,
                f"[Harness: '{self._refused_direction}' was refused by MUD"
                f" — use markblocked:{self._refused_direction} if this exit"
                f" is permanently blocked]")
            self._refused_direction = None
        # Annotate goto: navigation state so the LLM knows whether it's
        # still in transit or has arrived.
        if self._active_goto:
            dest   = self._active_goto["dest"]
            target = self._active_goto["target"]
            if self.current_room_hash == dest:
                room_name = (self.profiles.get(self.current_profile, {})
                             .get('rooms', {}).get(dest, {}).get('name', dest))
                mud_lines.insert(0, f"[Harness: goto:{target} arrived — {room_name}]")
                self._active_goto = None
            else:
                mud_lines.insert(0, f"[Harness: goto:{target} in progress]")

        # Annotate explore: state and per-turn room exit summary.
        if self.current_room_hash and self.current_profile:
            profile   = self.profiles.get(self.current_profile, {})
            rooms     = profile.get('rooms', {})
            room_name = rooms.get(self.current_room_hash, {}).get('name', self.current_room_hash)
            ei        = self._classify_room_exits(self.current_room_hash)
            parts     = []
            if ei["known"]:
                parts.append("known: " + ", ".join(f"{d}→{n}" for d, n in ei["known"].items()))
            if ei["assumed"]:
                parts.append("assumed: " + ", ".join(f"{d}→{n}" for d, n in ei["assumed"].items()))
            if ei["unknown"]:
                parts.append("unknown: " + ", ".join(ei["unknown"]))
            if ei["blocked"]:
                parts.append("blocked: " + ", ".join(ei["blocked"]))
            if ei["dangerous"]:
                parts.append("DANGEROUS(death-trap): " + ", ".join(ei["dangerous"]))
            exit_summary = "; ".join(parts) if parts else "none"
            mud_lines.insert(0, f"[Room: {room_name} ({self.current_room_hash}) — {exit_summary}]")

            if self._active_explore:
                edest = self._active_explore["dest"]
                if self.current_room_hash == edest:
                    edest_name = rooms.get(edest, {}).get('name', edest)
                    arrival_msg = f"[Harness: explore: arrived at {edest_name} — {exit_summary}]"
                    mud_lines.insert(0, arrival_msg)
                    self._active_explore = None
                    self._explore_arrived_info = {"message": arrival_msg}
                    if "walk_exit" in (engine._plan_steps or []):
                        engine._plan_step = "walk_exit"
                    self._update_nav_panel()
                else:
                    edest_name = rooms.get(edest, {}).get('name', edest)
                    mud_lines.insert(0, f"[Harness: explore: navigating to {edest_name}]")
            elif self._explore_arrived_info:
                # LLM hasn't walked an exit yet — re-inject the arrival message
                # so it has context on every re-triggered turn until it acts.
                mud_lines.insert(0, self._explore_arrived_info["message"])
        stats = dict(self.char_stats)
        # Overlay with the freshest prompt line in the buffer so hp/mp/mv/tank/opp
        # are current even before the message_queue stats update reaches the main thread.
        for line in reversed(mud_lines):
            prompt_update = self.mud_parser.parse_prompt_stats(line)
            if prompt_update:
                stats.update(prompt_update)
                break
        combat_mob = self._combat_mob
        rescue_flag = self._skill_rescue_flag
        self._skill_rescue_flag = False
        target_killed = self._skill_target_killed
        self._skill_target_killed = False
        engine.on_prompt(mud_lines, stats, combat_mob, rescue_flag,
                         self._on_skill_result, target_killed=target_killed)

    def _on_skill_result(self, result, skill_name):
        """Called on the main thread when the skill LLM replies."""
        engine = self.skill_engine
        if not engine or engine.active_name() != skill_name:
            return  # user stopped or swapped skills mid-flight
        if result is None:
            self.append_text("[Skill] LLM reply could not be parsed; starting default skill.\n",
                             "error")
            engine.stop()
            self.master.after(100, self._start_default_skill)
            return
        note = result.get("note", "")
        if note:
            self.append_advisor_text(note)
        commands = result.get("commands", [])
        if commands:
            # Record what the LLM emitted (speedwalk strings stay intact in the
            # ledger so the LLM reasons at that level on the next turn).
            engine.record_dispatched(commands)
            # Expand any speedwalk commands into their constituent directions
            # for dispatch, since the MUD itself does not understand speedwalks.
            dispatch = []
            # If the LLM issued commands that don't include a goto: or explore:,
            # clear any pending navigation state.
            cl = [c.lower() for c in commands]
            if not any(x.startswith('goto:') for x in cl):
                self._active_goto = None
            if not any(x in ('explore:', 'explore') for x in cl):
                self._active_explore = None
                self._update_nav_panel()
            for c in commands:
                cl_c = c.lower().strip()
                if cl_c.startswith('goto:'):
                    tgt = c[5:].strip()
                    goto_result = self._resolve_goto(tgt)
                    if goto_result is not None:
                        path, dest_room = goto_result
                        self._active_goto = {"target": tgt, "dest": dest_room}
                        dispatch.extend(path)
                    else:
                        self._active_goto = None
                        self.append_text(f"[Skill] goto: no path found for '{tgt}'\n", "error")
                elif cl_c in ('explore:', 'explore'):
                    self._explore_arrived_info = None  # starting a new cycle
                    result_ex = self._find_nearest_explore_target()
                    if result_ex is None:
                        self._active_explore = None
                        self.append_text("[Harness: explore: map appears complete — no unmapped exits found]\n", "system")
                        self._update_nav_panel()
                    else:
                        ex_path, ex_dest, ex_ei = result_ex
                        self._active_explore = {"dest": ex_dest, "exit_info": ex_ei}
                        self._update_nav_panel()
                        dispatch.extend(ex_path)
                        if not ex_path:
                            # Already in the target room — fire _trigger_skill so it
                            # detects arrival without waiting for an ambient MUD event.
                            self.master.after(50, self._trigger_skill)
                elif cl_c.startswith('setlandmark:'):
                    lm_name = c[len('setlandmark:'):].strip().lower()
                    if lm_name and self.current_room_hash and self.current_profile:
                        prof = self.profiles.get(self.current_profile, {})
                        prof.setdefault('landmarks', {})[lm_name] = self.current_room_hash
                        self.save_profiles()
                        rn = prof.get('rooms', {}).get(self.current_room_hash, {}).get('name', self.current_room_hash)
                        self.append_text(f"[Harness: landmark '{lm_name}' set → {rn}]\n", "system")
                elif cl_c.startswith('unsetlandmark:'):
                    lm_name = c[len('unsetlandmark:'):].strip().lower()
                    if lm_name and self.current_profile:
                        prof = self.profiles.get(self.current_profile, {})
                        removed = prof.get('landmarks', {}).pop(lm_name, None)
                        if removed:
                            self.save_profiles()
                            self.append_text(f"[Harness: landmark '{lm_name}' removed]\n", "system")
                elif cl_c in ('markdangerous:', 'markdangerous'):
                    if self.previous_room_hash and self.last_movement_direction and self.current_profile:
                        prof = self.profiles.get(self.current_profile, {})
                        _mark_death_trap(prof, self.previous_room_hash, self.last_movement_direction)
                        self.save_profiles()
                        rooms_d = prof.get('rooms', {})
                        prev_n  = rooms_d.get(self.previous_room_hash, {}).get('name', self.previous_room_hash)
                        self.append_text(
                            f"[Harness: marked {self.last_movement_direction} from {prev_n} as dangerous]\n",
                            "system")
                elif cl_c.startswith('markblocked:'):
                    dir_arg = c[len('markblocked:'):].strip().lower()
                    full_dir = self.direction_map.get(dir_arg, dir_arg)
                    if full_dir and self.current_room_hash and self.current_profile:
                        prof    = self.profiles.get(self.current_profile, {})
                        rooms_d = prof.get('rooms', {})
                        room    = rooms_d.get(self.current_room_hash, {})
                        exits_str = room.get('exits', '')
                        m = _EXIT_DIR_RE.search(exits_str)
                        raw_dirs = m.group(1).split() if m and m.group(1) != 'none' else []
                        listed = {self.direction_map.get(d.lower(), d.lower()) for d in raw_dirs}
                        cur_name = rooms_d.get(self.current_room_hash, {}).get('name', self.current_room_hash)
                        if full_dir not in listed:
                            self.append_text(
                                f"[Harness: markblocked:{full_dir} ignored — not a listed exit]\n",
                                "error")
                        else:
                            # Refuse if this is the only non-blocked exit — would strand pathfinding.
                            room_links_chk = prof.get('room_links', {}).get(self.current_room_hash, {})
                            non_blocked = [d for d in listed
                                           if not (isinstance(room_links_chk.get(d), dict)
                                                   and room_links_chk[d].get('blocked'))]
                            if non_blocked == [full_dir]:
                                self.append_text(
                                    f"[Harness: markblocked:{full_dir} refused — it is the only"
                                    f" non-blocked exit from {cur_name};"
                                    f" marking it would strand pathfinding]\n",
                                    "error")
                            else:
                                room_links_prof = prof.setdefault('room_links', {})
                                existing = room_links_prof.setdefault(self.current_room_hash, {}).get(full_dir)
                                if isinstance(existing, dict):
                                    existing['blocked'] = True
                                else:
                                    room_links_prof[self.current_room_hash][full_dir] = {'blocked': True}
                                self.save_profiles()
                                self.append_text(
                                    f"[Harness: marked {full_dir} from {cur_name} as blocked]\n",
                                    "system")
                                self._update_nav_panel()
                else:
                    expanded = self._expand_speedwalk(c)
                    if expanded is not None:
                        dispatch.extend(expanded)
                    else:
                        dispatch.append(c)
            # Clear arrived context once a movement command is actually dispatched.
            if self._explore_arrived_info and any(
                    d.lower() in self.movement_commands for d in dispatch):
                self._explore_arrived_info = None
            # Send sequentially with a small stagger so the MUD has time to
            # respond between steps.
            def send_at(idx):
                if not engine.is_active() or engine.active_name() != skill_name:
                    return
                if idx >= len(dispatch):
                    return
                self.send_ai_command(dispatch[idx])
                self.master.after(300, lambda: send_at(idx + 1))
            send_at(0)
        switch_to = result.get("switch_skill")
        if switch_to:
            engine.stop()
            self.append_text(f"[Skill] switching: {skill_name} \u2192 {switch_to}\n", "system")
            self.session_logger.log_command(f"[Skill switch] {skill_name} -> {switch_to}")
            skills = self._current_skills()
            if switch_to in skills:
                self._start_skill_core(switch_to, skills[switch_to])
            elif switch_to in self._current_skill_targets():
                self._start_skill_target(switch_to)
            else:
                self.append_text(f"[Skill] switch_skill '{switch_to}' not found; resuming default.\n", "error")
                self.master.after(100, self._start_default_skill)
            return
        if result.get("complete"):
            self.append_text(f"[Skill] complete: {skill_name} — {note}\n", "system")
            self.session_logger.log_command(f"[Skill complete] {skill_name}")
            engine.stop()
            if skill_name != "_default":
                self.master.after(100, self._start_default_skill)

    def _on_session_summary(self, summary):
        """Called on the main thread when the end-of-session summary is ready."""
        if not summary or not self.current_profile:
            return
        from datetime import datetime
        profile = self.profiles.get(self.current_profile, {})
        ctx = profile.setdefault('advisor_context', {})
        ctx['session_summary'] = summary
        ctx['session_summary_ts'] = datetime.now().isoformat()
        ctx['total_sessions'] = ctx.get('total_sessions', 0) + 1
        self.save_profiles()
        self.session_logger.log_session_summary(summary)

    def append_advisor_text(self, text):
        """Display advisor text in the advisor pane."""
        self.advisor_area.config(state=tk.NORMAL)
        self.advisor_area.insert(tk.END, "[Advisor] ", "advisor_prefix")
        self.advisor_area.insert(tk.END, text.strip() + "\n", "advisor_body")
        self.advisor_area.tag_config("advisor_prefix", foreground="#89d185",
                                     font=("Courier", 10, "bold"))
        self.advisor_area.tag_config("advisor_body", foreground="#c8c8c8")
        self.advisor_area.see(tk.END)
        self.advisor_area.config(state=tk.DISABLED)

    def append_battle_snapshot(self, text):
        """Display a battle snapshot in the advisor pane with a distinct amber prefix."""
        self.advisor_area.config(state=tk.NORMAL)
        self.advisor_area.insert(tk.END, "[Battle] ", "battle_prefix")
        self.advisor_area.tag_config("battle_prefix", foreground="#ce9178",
                                     font=("Courier", 10, "bold"))
        self.advisor_area.insert(tk.END, text.strip() + "\n", "advisor_body")
        self.advisor_area.see(tk.END)
        self.advisor_area.config(state=tk.DISABLED)

    def clear_advisor(self):
        """Clear the advisor output pane."""
        self.advisor_area.config(state=tk.NORMAL)
        self.advisor_area.delete(1.0, tk.END)
        self.advisor_area.config(state=tk.DISABLED)

    @staticmethod
    def _load_llm_local():
        """Load host-local LLM overrides from ~/.mud_client_llm_local.json."""
        path = os.path.join(os.path.expanduser("~"), ".mud_client_llm_local.json")
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def _save_llm_local(data):
        """Write host-local LLM overrides to ~/.mud_client_llm_local.json."""
        path = os.path.join(os.path.expanduser("~"), ".mud_client_llm_local.json")
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

    @staticmethod
    def _ui_local_path():
        return os.path.join(os.path.expanduser("~"), ".mud_client_ui_local.json")

    @classmethod
    def _load_ui_local(cls):
        """Load host-local UI settings (window geometry, etc.)."""
        try:
            with open(cls._ui_local_path(), 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    @classmethod
    def _save_ui_local(cls, data):
        """Write host-local UI settings."""
        with open(cls._ui_local_path(), 'w') as f:
            json.dump(data, f, indent=2)

    def open_ai_config(self):
        """Open the AI configuration dialog for the current profile."""
        profile_name = self.profile_var.get()
        if not profile_name or profile_name not in self.profiles:
            messagebox.showwarning("AI Config", "Please select a profile first.")
            return
        profile = self.profiles[profile_name]
        base_cfg = profile.get('ai_config', {})
        local_all = self._load_llm_local()
        local_overrides = local_all.get(profile_name, {})
        # Show merged config so the dialog reflects what's actually in effect
        merged_cfg = dict(base_cfg)
        merged_cfg.update(local_overrides)
        dialog = AIConfigDialog(self.master, merged_cfg, local_overrides)
        if dialog.result is not None:
            profile['ai_config'] = dialog.result
            # Clear host-local overrides for this profile since user chose
            # "Save to Profile" (shared config)
            if profile_name in local_all:
                del local_all[profile_name]
                self._save_llm_local(local_all)
            self.save_profiles()
            self.append_text("[AI Config saved to profile.]\n", "system")
        elif dialog.local_result is not None:
            local_all[profile_name] = dialog.local_result
            self._save_llm_local(local_all)
            self.append_text("[AI Config saved to this host only.]\n", "system")

    def open_color_calibration(self):
        """Open the Room Colors calibration dialog."""
        if not self.current_profile or self.current_profile not in self.profiles:
            messagebox.showwarning("Room Colors",
                                   "Please select a profile and connect first.")
            return
        if not self._raw_ansi_lines:
            messagebox.showwarning("Room Colors",
                                   "No MUD output buffered yet.\n"
                                   "Connect and walk around a bit, then try again.")
            return

        color_samples = self._extract_color_samples()
        if not color_samples:
            messagebox.showwarning("Room Colors",
                                   "No distinct colors detected in recent output.\n"
                                   "Walk around some more and try again.")
            return

        profile = self.profiles[self.current_profile]
        current_structure = profile.get('ai_config', {}).get('mud_structure', {})

        dialog = ColorCalibrationDialog(self.master, color_samples, current_structure)
        if dialog.result is not None:
            ai_config = profile.setdefault('ai_config', {})
            ai_config['mud_structure'] = dialog.result
            self.save_profiles()
            roles = ', '.join(f"{r}={c}" for r, c in dialog.result.items())
            self.append_text(f"[Room Colors saved: {roles}]\n", "system")

    def _show_mob_stats_dialog(self):
        """Open a table showing collected per-mob combat statistics."""
        profile_name = self.current_profile or self.profile_var.get()
        if not profile_name or profile_name not in self.profiles:
            messagebox.showinfo("Mob Stats", "No active profile.")
            return

        mob_combat_stats = self.profiles[profile_name].get('mob_combat_stats', {})
        if self._mob_stats_cleanup(mob_combat_stats):
            self.save_profiles()

        win = tk.Toplevel(self.master)
        win.title("Mob Combat Stats")
        win.geometry("860x400")
        win.resizable(True, True)

        # Toolbar frame
        toolbar = tk.Frame(win)
        toolbar.pack(fill=tk.X, padx=6, pady=(6, 2))
        tk.Label(toolbar, text="Profile: " + profile_name).pack(side=tk.LEFT)
        tk.Button(toolbar, text="Delete Selected",
                  command=lambda: self._mob_stats_delete(win, tree, profile_name)
                  ).pack(side=tk.RIGHT, padx=4)
        tk.Button(toolbar, text="Refresh",
                  command=lambda: self._mob_stats_refresh(tree, profile_name)
                  ).pack(side=tk.RIGHT)

        # Treeview with scrollbar
        frame = tk.Frame(win)
        frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        cols = ('mob', 'hits', 'misses', 'hit_pct', 'max_hit',
                'xp_total', 'xp_avg', 'rooms', 'aggressive')
        tree = ttk.Treeview(frame, columns=cols, show='headings', selectmode='extended')

        tree.heading('mob',        text='Mob Name')
        tree.heading('hits',       text='Hits')
        tree.heading('misses',     text='Misses')
        tree.heading('hit_pct',    text='Hit %')
        tree.heading('max_hit',    text='Max Dmg')
        tree.heading('xp_total',   text='XP Total')
        tree.heading('xp_avg',     text='XP Avg')
        tree.heading('rooms',      text='Rooms')
        tree.heading('aggressive', text='Aggressive')

        tree.column('mob',        width=200, anchor='w')
        tree.column('hits',       width=50,  anchor='center')
        tree.column('misses',     width=50,  anchor='center')
        tree.column('hit_pct',    width=55,  anchor='center')
        tree.column('max_hit',    width=65,  anchor='center')
        tree.column('xp_total',   width=75,  anchor='center')
        tree.column('xp_avg',     width=65,  anchor='center')
        tree.column('rooms',      width=50,  anchor='center')
        tree.column('aggressive', width=75,  anchor='center')

        vsb = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Populate rows
        self._mob_stats_populate(tree, mob_combat_stats)

        tk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 6))

    # Adverb suffixes that old regex versions leaked into mob names.
    _MOB_NAME_BAD_SUFFIXES = (
        ' tries to', ' hopelessly', ' desperately', ' frantically', ' feebly',
        ' weakly', ' wildly', ' furiously', ' savagely', ' viciously',
        ' awkwardly', ' clumsily', ' forcefully', ' powerfully',
    )

    @staticmethod
    def _mob_stats_cleanup(mob_combat_stats):
        """Remove stale artefact suffixes from mob_combat_stats in place.

        Old regex versions captured trailing adverbs or 'tries to' as part of
        the mob name (e.g. 'white knight hopelessly', 'beastly fido tries to').
        Finds such keys, merges their stats into the clean base-name entry if
        one exists, then deletes the bad key.  Returns True if anything changed.
        """
        bad_keys = [
            k for k in mob_combat_stats
            if any(k.endswith(s) for s in MUDClient._MOB_NAME_BAD_SUFFIXES)
            or k == 'tries to'
        ]
        if not bad_keys:
            return False
        for bad in bad_keys:
            suffix = next((s for s in MUDClient._MOB_NAME_BAD_SUFFIXES if bad.endswith(s)), None)
            base = bad[:-len(suffix)].strip() if suffix and bad != suffix.strip() else None
            if base and base in mob_combat_stats:
                good = mob_combat_stats[base]
                stale = mob_combat_stats[bad]
                good['hits']    = good.get('hits', 0)    + stale.get('hits', 0)
                good['misses']  = good.get('misses', 0)  + stale.get('misses', 0)
                good['xp_total']= good.get('xp_total', 0)+ stale.get('xp_total', 0)
                good['xp_kills']= good.get('xp_kills', 0)+ stale.get('xp_kills', 0)
                good['max_hit'] = max(good.get('max_hit', 0), stale.get('max_hit', 0))
                if stale.get('aggressive'):
                    good['aggressive'] = True
                for room in stale.get('rooms', []):
                    if room not in good.setdefault('rooms', []):
                        good['rooms'].append(room)
                        if len(good['rooms']) > 10:
                            good['rooms'].pop(0)
            del mob_combat_stats[bad]
        return True

    def _mob_stats_populate(self, tree, mob_combat_stats):
        """Fill the treeview from mob_combat_stats dict."""
        for row in tree.get_children():
            tree.delete(row)
        for mob_name, s in sorted(mob_combat_stats.items()):
            hits      = s.get('hits', 0)
            misses    = s.get('misses', 0)
            total     = hits + misses
            hit_pct   = f"{hits * 100 // total}%" if total else "—"
            max_hit   = s.get('max_hit', 0) or "—"
            xp_total  = s.get('xp_total', 0)
            xp_kills  = s.get('xp_kills', 0)
            xp_avg    = f"{xp_total // xp_kills}" if xp_kills else "—"
            xp_total_s = str(xp_total) if xp_total else "—"
            rooms     = len(s.get('rooms', []))
            agg       = "Yes" if s.get('aggressive') else "No"
            tree.insert('', 'end', iid=mob_name,
                        values=(mob_name, hits, misses, hit_pct, max_hit,
                                xp_total_s, xp_avg, rooms, agg))

    def _mob_stats_refresh(self, tree, profile_name):
        """Reload stats from the current profile into the treeview."""
        mob_combat_stats = self.profiles.get(profile_name, {}).get('mob_combat_stats', {})
        self._mob_stats_populate(tree, mob_combat_stats)

    def _mob_stats_delete(self, win, tree, profile_name):
        """Delete selected mob entries from the profile."""
        selected = tree.selection()
        if not selected:
            return
        mob_combat_stats = self.profiles.get(profile_name, {}).get('mob_combat_stats', {})
        for mob_key in selected:
            mob_combat_stats.pop(mob_key, None)
            tree.delete(mob_key)
        self.save_profiles()

    def _extract_color_samples(self):
        """
        Scan _raw_ansi_lines and return a list of (hex_color, sample_text) tuples —
        one entry per distinct non-default color, using the longest clean line seen
        in that color as the sample.
        """
        best = {}  # color_hex -> best sample text so far
        for raw_line in self._raw_ansi_lines:
            segments = self.parse_ansi_text(raw_line)
            for text, color in segments:
                stripped = text.strip()
                if not stripped:
                    continue
                if color == self._ANSI_DEFAULT_COLOR:
                    continue
                # Skip stat prompts, command echoes, and AI status lines
                if re.match(r'^(?:\d+[Hh]|\s*>|\[AI)', stripped):
                    continue
                if color not in best or len(stripped) > len(best[color]):
                    best[color] = stripped
        return sorted(best.items())   # sorted by color hex for stable ordering

    def _dump_ai_debug(self):
        """Print a full diagnostic snapshot of AI and room tracking state."""
        sep = "=" * 50
        out = [sep, "[AI DEBUG SNAPSHOT]", sep]

        # --- Client state ---
        out.append(f"connected:            {self.connected}")
        out.append(f"room_tracking:        {self.room_tracking_enabled}")
        out.append(f"room_color:           {self.room_color}")
        out.append(f"current_room_hash:    {self.current_room_hash}")
        out.append(f"previous_room_hash:   {self.previous_room_hash}")
        out.append(f"expecting_room_data:  {self.expecting_room_data}")
        out.append(f"last_movement_dir:    {self.last_movement_direction}")

        # --- Room data for current room ---
        if self.current_profile and self.current_room_hash:
            profile = self.profiles.get(self.current_profile, {})
            rooms = profile.get('rooms', {})
            room_links = profile.get('room_links', {})
            room = rooms.get(self.current_room_hash, {})
            out.append(f"current_room_name:    {room.get('name', '(unknown)')}")
            out.append(f"current_room_exits:   {room.get('exits', '(empty)')!r}")
            out.append(f"current_room_links:   {room_links.get(self.current_room_hash, {})}")
            out.append(f"total_rooms_mapped:   {len(rooms)}")
            out.append(f"total_links:          {sum(len(v) for v in room_links.values())}")
        else:
            out.append("current_room_data:    (no profile or no room hash)")

        # --- AI agent state ---
        if self.ai_agent:
            a = self.ai_agent
            s = a.state
            out.append(f"agent.is_running:     {a.is_running}")
            out.append(f"agent._last_dir:      {a._last_direction}")
            out.append(f"state.visited:        {len(s.visited)} rooms")
            out.append(f"state.frontier:       {len(s.frontier)} rooms")
            out.append(f"state.danger_rooms:   {len(s.danger_rooms)}")
            out.append(f"state.dead_ends:      {dict((k[:8], list(v)) for k,v in s.dead_ends.items())}")
            out.append(f"state.hp/mp/mv:       {s.current_hp}/{s.current_mp}/{s.current_mv}")
            out.append(f"state.hunger/thirst:  {s.hunger_level} / {s.thirst_level}")
            out.append(f"state.gold:           {s.gold}")
        else:
            out.append("ai_agent:             (not created)")

        out.append(sep)
        self.append_text("\n".join(out) + "\n", "system")

    def send_character_name(self):
        """Send character name and learn the login prompt"""
        if not self.connected:
            return

        if not self.current_profile or self.current_profile not in self.profiles:
            messagebox.showwarning("Warning", "Please select a profile with character name")
            return

        profile = self.profiles[self.current_profile]
        character = profile.get('character', '')

        if not character:
            messagebox.showwarning("Warning", "No character name set in profile")
            return

        try:
            # Save the last line as the login prompt
            if self.last_line:
                self.profiles[self.current_profile]['login_prompt'] = self.last_line.lower()
                self.save_profiles()
                self.append_text(f"[Learned login prompt: {self.last_line}]\n", "system")

            # Send character name
            self.ssl_socket.sendall((character + "\n").encode('utf-8'))
            self.append_text(f"> {character}\n", "user")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to send character name: {str(e)}")
            self.disconnect()

    def send_password(self):
        """Send password and learn the password prompt"""
        if not self.connected:
            return

        if not self.current_profile or self.current_profile not in self.profiles:
            messagebox.showwarning("Warning", "Please select a profile with password")
            return

        profile = self.profiles[self.current_profile]
        password = profile.get('password', '')

        if not password:
            messagebox.showwarning("Warning", "No password set in profile")
            return

        try:
            # Save the last line as the password prompt
            if self.last_line:
                self.profiles[self.current_profile]['password_prompt'] = self.last_line.lower()
                self.save_profiles()
                self.append_text(f"[Learned password prompt: {self.last_line}]\n", "system")

            # Send password
            self.ssl_socket.sendall((password + "\n").encode('utf-8'))
            self.append_text("> [password hidden]\n", "user")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to send password: {str(e)}")
            self.disconnect()

    def send_and_remember(self):
        """Send message and remember the prompt-response pair"""
        if not self.connected:
            return

        if not self.current_profile or self.current_profile not in self.profiles:
            messagebox.showwarning("Warning", "Please select a profile")
            return

        message = self.input_entry.get()
        if not message:
            messagebox.showwarning("Warning", "Please enter a response to send")
            return

        # Ask user if this should run once or always
        dialog = RunOnceDialog(self.master)
        if dialog.result is None:
            return  # User closed dialog without choosing
        run_once = dialog.result

        try:
            # Save the last line as a prompt with this response
            if self.last_line:
                # Initialize custom_responses if it doesn't exist
                if 'custom_responses' not in self.profiles[self.current_profile]:
                    self.profiles[self.current_profile]['custom_responses'] = {}

                # Normalize prompt to handle varying stats (e.g., "24H 100M >" becomes "#H #M >")
                prompt_normalized = self.normalize_prompt(self.last_line.lower())
                self.profiles[self.current_profile]['custom_responses'][prompt_normalized] = {
                    'response': message,
                    'run_once': run_once
                }
                self.save_profiles()

                frequency = "once per connection" if run_once else "every time"
                self.append_text(f"[Learned: '{self.last_line}' -> '{message}' ({frequency})]\n", "system")

            # Send message
            self.ssl_socket.sendall((message + "\n").encode('utf-8'))
            self.session_logger.log_command(message)
            self.append_text(f"> {message}\n", "user")
            self.input_entry.delete(0, tk.END)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to send message: {str(e)}")
            self.disconnect()

    def append_text(self, text, msg_type="mud"):
        """Append text to the display area"""
        self.session_logger.log_append(text, msg_type)
        self.text_area.config(state=tk.NORMAL)

        # Apply color tags based on message type
        if msg_type == "system":
            self.text_area.insert(tk.END, text, "system")
        elif msg_type == "error":
            self.text_area.insert(tk.END, text, "error")
        elif msg_type == "user":
            self.text_area.insert(tk.END, text, "user")
        elif msg_type == "telnet":
            self.text_area.insert(tk.END, text, "telnet")
        elif msg_type == "mud_colored":
            # Handle colored text segments
            for segment_text, color in text:
                # Create a unique tag name for this color
                tag_name = f"color_{color.replace('#', '')}"
                self.text_area.insert(tk.END, segment_text, tag_name)
                self.text_area.tag_config(tag_name, foreground=color)
        else:
            self.text_area.insert(tk.END, text)

        # Keep the widget memory-bounded; drop the oldest half when over the limit.
        line_count = int(self.text_area.index(tk.END).split('.')[0])
        if line_count > 5000:
            self.text_area.delete("1.0", f"{line_count - 2500}.0")

        self.text_area.see(tk.END)
        self.text_area.config(state=tk.DISABLED)

        # Configure tags for colors
        self.text_area.tag_config("system", foreground="#4ec9b0")
        self.text_area.tag_config("error", foreground="#f48771")
        self.text_area.tag_config("user", foreground="#dcdcaa")
        self.text_area.tag_config("telnet", foreground="#ce9178")

    def clear_output(self):
        """Clear the output text area"""
        self.text_area.config(state=tk.NORMAL)
        self.text_area.delete(1.0, tk.END)
        self.text_area.config(state=tk.DISABLED)

    def on_closing(self):
        """Handle window closing"""
        if self.connected:
            self.disconnect()
        # Flush AI agent state before saving profiles
        if self.ai_agent:
            self.ai_agent.save_state()
        # Save window geometry and pane sizes for next session (host-local, not shared)
        ui_local = self._load_ui_local()
        ui_local['window_geometry'] = self.master.geometry()
        try:
            x, _ = self._bottom_paned.sash_coord(0)
            w = self._bottom_paned.winfo_width()
            if w > 0:
                ui_local['nav_sash_fraction'] = x / w
        except Exception:
            pass
        self._save_ui_local(ui_local)
        self.save_profiles()
        self.master.destroy()


from ui_dialogs import (
    RunOnceDialog, AIConfigDialog, ColorCalibrationDialog, ProfileDialog,
    SkillsDialog, SkillEditDialog, TemplateEditDialog, TargetEditDialog,
)



def main():
    root = tk.Tk()
    app = MUDClient(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
