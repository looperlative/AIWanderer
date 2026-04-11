#!/usr/bin/env python3
# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""
MUD Client with SSL Support
A simple GUI-based MUD client that connects via SSL and displays received text.
"""

import socket
import ssl
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
from mud_parser import MUDTextParser


class MUDClient:
    def __init__(self, master):
        self.master = master
        self.master.title("MUD Client - SSL Connection")
        self.master.geometry("900x600")  # default; overridden below if saved
        
        # Shared parser instance
        self.mud_parser = MUDTextParser()

        # Connection state
        self.socket = None
        self.ssl_socket = None
        self.connected = False
        self.receive_thread = None
        self.message_queue = queue.Queue()
        
        # Profile management
        self.profiles_file = os.path.join(os.path.expanduser("~"), ".mud_client_profiles.json")
        self.profiles = self.load_profiles()
        self.current_profile = None
        
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
        self.room_color = None  # Will store the detected room name color
        # Rolling buffer of raw decoded text (ANSI codes intact) for the
        # color calibration UI — lets the dialog show actual MUD colors.
        self._raw_ansi_lines = deque(maxlen=500)
        self.current_room_hash = None  # Hash of the current room
        self.previous_room_hash = None  # Hash of the previous room
        self.detect_entry_room = False  # Flag to detect entry room after login
        self.movement_commands = ['n', 'north', 's', 'south', 'e', 'east', 
                                   'w', 'west', 'u', 'up', 'd', 'down', 'l', 'look']
        # Map short commands to directions
        self.direction_map = {
            'n': 'north', 'north': 'north',
            's': 'south', 'south': 'south',
            'e': 'east', 'east': 'east',
            'w': 'west', 'west': 'west',
            'u': 'up', 'up': 'up',
            'd': 'down', 'down': 'down',
            'l': 'look', 'look': 'look'
        }

        # AI agent state
        self.ai_agent = None

        # LLM advisor state
        self.llm_advisor = None
        self._pending_command = None   # last human command sent (awaiting MUD response)
        self._response_buffer = []     # MUD lines received since last command
        self._advisor_active = True    # LLM advisor on/off
        self._advisor_busy = False     # True while an LLM call is in flight
        self._advisor_queue = []       # events queued while LLM is busy: list of {'command','mud_lines'}
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

        # Mob combat stat tracking (receive-thread state)
        self._combat_mob = None           # Normalised mob name currently fighting us
        self._kill_cmd_pending = False    # Player sent kill/k command
        self._kill_cmd_target = None      # Target typed by player
        self._last_kill_cmd_time = 0.0
        self._prev_combat_hp = None       # HP before last round for damage calc
        self._last_killed_mob = None      # Mob key awaiting XP attribution

        self.setup_ui()

        # Restore saved window geometry (position + size)
        saved_geometry = self.profiles.get('_settings', {}).get('window_geometry')
        if saved_geometry:
            try:
                self.master.geometry(saved_geometry)
            except tk.TclError:
                pass  # ignore invalid saved geometry
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
        # TELNET commands start with IAC (0xFF)
        IAC = 0xFF  # Interpret As Command
        WILL = 0xFB
        WONT = 0xFC
        DO = 0xFD
        DONT = 0xFE
        SB = 0xFA  # Subnegotiation Begin
        SE = 0xF0  # Subnegotiation End
        
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
        }
        
        result = bytearray()
        i = 0
        
        while i < len(data):
            if data[i] == IAC and i + 1 < len(data):
                cmd = data[i + 1]
                
                # Handle subnegotiation
                if cmd == SB and i + 2 < len(data):
                    # Find the end of subnegotiation (IAC SE)
                    end = i + 2
                    while end < len(data) - 1:
                        if data[end] == IAC and data[end + 1] == SE:
                            break
                        end += 1
                    
                    option = data[i + 2] if i + 2 < len(data) else 0
                    option_name = telnet_options.get(option, f"UNKNOWN({option})")
                    sb_data = data[i+3:end]
                    self.message_queue.put(("telnet", f"TELNET: Subnegotiation for {option_name} (data length: {len(sb_data)})"))
                    i = end + 2
                    continue
                
                # Handle 3-byte commands (WILL, WONT, DO, DONT)
                if cmd in [WILL, WONT, DO, DONT] and i + 2 < len(data):
                    option = data[i + 2]
                    cmd_name = telnet_commands.get(cmd, f"UNKNOWN({cmd})")
                    option_name = telnet_options.get(option, f"UNKNOWN({option})")
                    self.message_queue.put(("telnet", f"TELNET: {cmd_name} {option_name}"))
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
                print(f"Error loading profiles: {e}")
                return {'_settings': {}}
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
        self._advisor_var = tk.BooleanVar(value=True)
        settings_menu.add_checkbutton(label="Room Tracking",
                                      variable=self.room_tracking_var,
                                      command=self.toggle_room_tracking)
        settings_menu.add_checkbutton(label="LLM Advisor",
                                      variable=self._advisor_var,
                                      command=self.toggle_advisor)
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

        self.advisor_area = scrolledtext.ScrolledText(
            paned,
            wrap=tk.WORD,
            font=self._font_main,
            bg="#1a1a2e",
            fg="#d4d4d4",
            insertbackground="white"
        )
        self.advisor_area.config(state=tk.DISABLED)
        paned.add(self.advisor_area, stretch="always", minsize=60)

        # Set initial sash position after window draws
        self.master.after(100, lambda: paned.sash_place(0, 0,
            int(self.master.winfo_height() * 0.70)))

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
        self._sv_align.set(str(s.get('alignment', '?')))
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
                tk.Label(row, text=f"{ticks}t", bg="#1a1a1a", fg=BLUE,
                         font=self._font_status, anchor='e').pack(side=tk.RIGHT)
        else:
            tk.Label(self._spells_frame, text="none", bg="#1a1a1a", fg=DIM,
                     font=self._font_status, anchor='w').pack(fill=tk.X)


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
            self._status_log_label.config(
                text=f"Log: {os.path.basename(self.session_logger.path)}",
                foreground="green")

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
            
            # Start receiving thread
            self.receive_thread = threading.Thread(target=self.receive_data, daemon=True)
            self.receive_thread.start()
            
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
        if self.llm_advisor and self.llm_advisor.is_available():
            self.llm_advisor.generate_session_summary(self._on_session_summary)
        self.session_logger.close()
        self._status_log_label.config(text="Log: off", foreground="gray")
        self._cancel_auto_score()
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
        self._advisor_busy = False
        self._advisor_queue.clear()
        self.triggered_once_responses.clear()  # Reset run-once tracking
        self.append_text("Disconnected from server\n", "system")
    
    def cleanup_connection(self):
        """Clean up socket resources"""
        if self.ssl_socket:
            try:
                self.ssl_socket.close()
            except:
                pass
            self.ssl_socket = None
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
    
    def receive_data(self):
        """Receive data from MUD server (runs in separate thread)"""
        buffer = []
        while self.connected:
            try:
                data = self.ssl_socket.recv(4096)
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

                # Accumulate MUD response for LLM advisor
                if self._pending_command is not None:
                    for ln in clean_text.splitlines():
                        if ln.strip():
                            self._response_buffer.append(ln.rstrip())
                    # Detect MUD command prompt — trigger advisor when prompt arrives
                    if self.last_line.rstrip().endswith('>') and self._advisor_active \
                            and self.llm_advisor:
                        self.master.after(0, self._trigger_advisor)

                # Parse character stats and queue status panel update
                self._parse_and_queue_stats(clean_text)

                # Autoloot on kill
                self._handle_autoloot(clean_text)

                # Per-mob combat stat tracking
                self._update_mob_combat_stats(clean_text)

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

        # Seed tick interval from saved profile value (keeps limit across sessions)
        saved_tick = (self.profiles.get(self.current_profile, {})
                      .get('tick_interval'))
        if saved_tick and int(saved_tick) > 0:
            self._tick_interval = int(saved_tick)
        self._tick_count = None
        self.message_queue.put(("system", "[Autologin] Login sequence completed\n"))
        if self.room_tracking_enabled:
            self.detect_entry_room = True
            self.expecting_room_data = True
            self.message_queue.put(("system", "[Room tracking] Detecting entry room...\n"))
        # Issue first score fetch after a short settle delay, then every 60 s
        self.master.after(1500, self._send_auto_score)

    def _send_auto_score(self):
        """Send score command silently and reschedule."""
        if not self.connected:
            self._auto_score_job = None
            return
        try:
            self._suppress_score_output = True
            self.ssl_socket.sendall(b"score\n")
            # Safety: clear suppression if prompt never arrives
            self.master.after(5000, self._clear_score_suppression)
        except Exception:
            self._suppress_score_output = False
        self._auto_score_job = self.master.after(60000, self._send_auto_score)

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

    def toggle_advisor(self):
        """Toggle the LLM advisor on/off from the Settings menu."""
        self._advisor_active = self._advisor_var.get()
        state = "enabled" if self._advisor_active else "disabled"
        self.append_text(f"[LLM Advisor {state}]\n", "system")

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
        """BFS through room_links; returns list of short direction strings or None."""
        if from_hash == to_hash:
            return []
        profile = self.profiles.get(self.current_profile, {})
        links = profile.get('room_links', {})
        queue = deque([(from_hash, [])])
        visited = {from_hash}
        while queue:
            room, path = queue.popleft()
            for direction, neighbor in links.get(room, {}).items():
                abbrev = self._DIR_ABBREV.get(direction, direction)
                new_path = path + [abbrev]
                if neighbor == to_hash:
                    return new_path
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, new_path))
        return None   # no path found

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
        if self._survival_state != 'inv_wait':
            return
        self._survival_inv_text += text
        # Wait for a prompt line before processing
        if self.last_line.rstrip().endswith('>'):
            full_text = self._survival_inv_text
            self._survival_inv_text = ''
            self.message_queue.put(('survival_inv', full_text))

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

        rounded = round(self._tick_count / 5) * 5
        if rounded > 0 and (self._tick_interval is None or rounded < self._tick_interval):
            self._tick_interval = rounded
            if self.current_profile and self.current_profile in self.profiles:
                self.profiles[self.current_profile]['tick_interval'] = rounded
                self.save_profiles()

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

        # Rescue check — evaluated on every HP prompt update during combat,
        # independent of the AI agent.  Dispatched to the main thread via after().
        if new_hp is not None and self._combat_mob:
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
                    msg = f"[Rescue] {reason} — sending: {rescue_cmd}\n"
                    cmd = rescue_cmd
                    self.master.after(0, lambda m=msg, c=cmd: (
                        self.append_text(m, "error"),
                        self.send_ai_command(c)
                    ))

        # Kill confirmed — note which mob was killed so we can attach XP
        if self._KILL_RE.search(text):
            self._last_killed_mob = self._combat_mob
            self._kill_cmd_pending = False
            self._kill_cmd_target = None
            self._combat_mob = None

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
            spells = self.mud_parser.parse_spell_affects(text)
            updates['spells'] = spells  # replace entirely from score output
        else:
            # Not a score block — check for spontaneous "You are thirsty/hungry"
            # messages the MUD sends between auto-score calls.
            ht = self.mud_parser.detect_hunger_thirst(text)
            updates.update(ht)

        # Buff expiration messages
        buff_events = self.mud_parser.detect_buff_events(text)
        if buff_events['expired']:
            updates['spells_expired'] = buff_events['expired']

        # Combat end detection
        if re.search(r'\b(?:is dead|has fled|you flee|you stop fighting)\b',
                     text, re.IGNORECASE):
            updates.setdefault('fighting', False)
            updates.setdefault('tank', None)
            updates.setdefault('opp', None)

        if updates:
            self.message_queue.put(("stats", updates))

    def process_room_data(self, segments):
        """Process and store room data"""
        if not self.room_tracking_enabled or not self.expecting_room_data:
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
                # Log why parsing failed so aidebug can show it
                colors_seen = list(dict.fromkeys(c for _, c in segments if _.strip()))
                self.append_text(
                    f"[Room parse failed] room_color={self.room_color!r} "
                    f"colors_in_segments={colors_seen}\n", "system")
                # Clear the flag so subsequent colored text (e.g. guard messages)
                # doesn't keep re-triggering the parser.
                self.expecting_room_data = False

        if room_data:
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
                if self.llm_advisor:
                    self.llm_advisor.send_initial_context()
            
            # Handle directional links (if we moved from another room)
            elif self.previous_room_hash and self.last_movement_direction:
                # Create link from previous room to current room
                room_links = self.profiles[self.current_profile]['room_links']
                
                # Initialize previous room's links if needed
                if self.previous_room_hash not in room_links:
                    room_links[self.previous_room_hash] = {}
                
                # Store the directional link
                direction = self.last_movement_direction
                room_links[self.previous_room_hash][direction] = room_hash
                
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

            # Survival hook — fountain fill and buy-food walk progression
            self._survival_on_room_entered(self.current_room_hash)

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
                            self.expecting_room_data = False
                            self.last_movement_direction = None
                        elif self.mud_parser.detect_darkness(plain):
                            # Dark room — we moved here but can't parse it.
                            # Clear expecting flag and let AI know we're stuck.
                            self.expecting_room_data = False
                            self.last_movement_direction = None
                            if self.ai_agent and self.ai_agent.is_running:
                                self.ai_agent.on_text_received(plain)

                    # Process room data if we're expecting it
                    if self.room_tracking_enabled and self.expecting_room_data:
                        self.process_room_data(msg_data)
                    
                    filtered = self._filter_display_segments(msg_data)
                    if filtered:
                        self.append_text(filtered, "mud_colored")
                elif msg_type == "ai_text":
                    if self.ai_agent:
                        self.ai_agent.on_text_received(msg_data)
                elif msg_type == "telnet":
                    self.append_text(msg_data + "\n", "telnet")
                elif msg_type == "error":
                    self.append_text(msg_data, "error")
                    self.disconnect()
                elif msg_type == "stats":
                    expired = msg_data.pop('spells_expired', [])
                    self.char_stats.update(msg_data)
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

        # Direct LLM prompt — starts with \
        if message.startswith('\\'):
            self.input_entry.delete(0, tk.END)
            self._send_direct_llm_prompt(message[1:].strip())
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
    # LLM Advisor
    # ------------------------------------------------------------------

    def _trigger_advisor(self):
        """Called on the main thread when a MUD prompt is detected after a command."""
        if not self._pending_command or not self.llm_advisor:
            return
        command = self._pending_command
        lines = list(self._response_buffer)
        self._pending_command = None
        self._response_buffer = []

        event = {'command': command, 'mud_lines': lines}

        if self._advisor_busy:
            # LLM still processing — queue this event for the next call
            self._advisor_queue.append(event)
            return

        self._advisor_busy = True
        self._fire_advisor([event])

    def _fire_advisor(self, events):
        """Start an LLM advisor call for the given list of events."""
        room_data = None
        if self.current_room_hash and self.current_profile:
            rooms = self.profiles.get(self.current_profile, {}).get('rooms', {})
            room_data = rooms.get(self.current_room_hash)
        self.llm_advisor.request_advice(events, room_data, self._on_advisor_result)

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
        """Called on the main thread when the LLM advisor responds."""
        if advice:
            if not self._advisor_streamed:
                self.append_advisor_text(advice)
            self.session_logger.log_advisor(advice)
        self._advisor_streamed = False

        if self._advisor_queue:
            queued = list(self._advisor_queue)
            self._advisor_queue.clear()
            # Split into leading MUD events and everything else.
            # Process the first contiguous run of MUD events as one batch,
            # then re-queue the remainder (which may start with a direct prompt).
            mud_events = []
            remainder = []
            hit_direct = False
            for item in queued:
                if not hit_direct and item.get('direct_prompt') is None:
                    mud_events.append(item)
                else:
                    hit_direct = True
                    remainder.append(item)
            self._advisor_queue.extend(remainder)
            if mud_events:
                self._fire_advisor(mud_events)
            else:
                # First item is a direct prompt
                direct = self._advisor_queue.pop(0)
                self.llm_advisor.request_direct(
                    direct['direct_prompt'], self._on_advisor_result)
        else:
            # Nothing queued — go idle; next prompt will re-arm naturally
            self._advisor_busy = False

    def _send_direct_llm_prompt(self, prompt):
        """Send a freeform prompt directly to the LLM advisor."""
        if not prompt:
            return
        if not self.llm_advisor or not self.llm_advisor.is_available():
            self.append_advisor_text("[No LLM configured]")
            return
        self.append_text(f"\\ {prompt}\n", "user")
        self.session_logger.log_command(f"\\ {prompt}")
        if self._advisor_busy:
            self._advisor_queue.append({'direct_prompt': prompt})
            return
        self._advisor_busy = True
        self.llm_advisor.request_direct(prompt, self._on_advisor_result)

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
        self.advisor_area.insert(tk.END, text.strip() + "\n\n", "advisor_body")
        self.advisor_area.tag_config("advisor_prefix", foreground="#89d185",
                                     font=("Courier", 10, "bold"))
        self.advisor_area.tag_config("advisor_body", foreground="#c8c8c8")
        self.advisor_area.see(tk.END)
        self.advisor_area.config(state=tk.DISABLED)

    def clear_advisor(self):
        """Clear the advisor output pane."""
        self.advisor_area.config(state=tk.NORMAL)
        self.advisor_area.delete(1.0, tk.END)
        self.advisor_area.config(state=tk.DISABLED)

    def _on_speed_change(self, value):
        """Update AI agent command delay from the speed slider (legacy)."""
        if self.ai_agent:
            self.ai_agent.COMMAND_DELAY_MS = int(float(value))

    def toggle_ai_mode(self):
        """Toggle the autonomous AI exploration agent on or off (legacy — autonomous disabled)."""
        pass

    def open_ai_config(self):
        """Open the AI configuration dialog for the current profile."""
        profile_name = self.profile_var.get()
        if not profile_name or profile_name not in self.profiles:
            messagebox.showwarning("AI Config", "Please select a profile first.")
            return
        profile = self.profiles[profile_name]
        current_cfg = profile.get('ai_config', {})
        dialog = AIConfigDialog(self.master, current_cfg)
        if dialog.result is not None:
            profile['ai_config'] = dialog.result
            self.save_profiles()
            self.append_text("[AI Config saved to profile.]\n", "system")

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
            out.append(f"agent._waiting_room:  {a._waiting_for_room}")
            out.append(f"agent._waiting_llm:   {a._waiting_for_llm}")
            out.append(f"agent._combat:        {a._combat_active}")
            out.append(f"agent._dead:          {a._dead}")
            out.append(f"agent._last_dir:      {a._last_direction}")
            out.append(f"agent.COMMAND_DELAY:  {a.COMMAND_DELAY_MS}ms")
            out.append(f"state.current_goal:   {s.current_goal}")
            out.append(f"state.visited:        {len(s.visited)} rooms")
            out.append(f"state.frontier:       {len(s.frontier)} rooms")
            out.append(f"state.danger_rooms:   {len(s.danger_rooms)}")
            out.append(f"state.dead_ends:      {dict((k[:8], list(v)) for k,v in s.dead_ends.items())}")
            out.append(f"state.hp/mp/mv:       {s.current_hp}/{s.current_mp}/{s.current_mv}")
            out.append(f"state.hunger/thirst:  {s.hunger_level} / {s.thirst_level}")
            out.append(f"state.has_light:      {s.has_light}")
            out.append(f"state.water_room:     {s.water_room}")
            out.append(f"state.food_room:      {s.food_room}")
            out.append(f"state.gold:           {s.gold}")
            out.append(f"state.seeking:        {s.seeking_resource}")
            # Show what choose_next_direction would do right now
            direction = a._choose_next_direction()
            out.append(f"next_direction:       {direction!r}")
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
        # Save window geometry for next session
        if '_settings' not in self.profiles:
            self.profiles['_settings'] = {}
        self.profiles['_settings']['window_geometry'] = self.master.geometry()
        self.save_profiles()
        self.master.destroy()


class RunOnceDialog:
    """Dialog for choosing if response runs once or always"""
    def __init__(self, parent):
        self.result = None
        
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Response Frequency")
        self.dialog.geometry("350x150")
        self.dialog.transient(parent)
        self.dialog.grab_set()
        
        # Message
        message = ttk.Label(self.dialog, 
                           text="How often should this response be sent?",
                           wraplength=300)
        message.pack(pady=20)
        
        # Buttons
        button_frame = ttk.Frame(self.dialog)
        button_frame.pack(pady=10)
        
        ttk.Button(button_frame, text="Once Per Connection", 
                  command=self.once_clicked, width=20).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Always", 
                  command=self.always_clicked, width=20).pack(side=tk.LEFT, padx=5)
        
        self.dialog.wait_window()
    
    def once_clicked(self):
        self.result = True  # run_once = True
        self.dialog.destroy()
    
    def always_clicked(self):
        self.result = False  # run_once = False
        self.dialog.destroy()


class AIConfigDialog:
    """Dialog for configuring the LLM backend used by the AI agent."""

    def __init__(self, parent, current_cfg):
        self.result = None
        cfg = current_cfg or {}

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("AI / LLM Configuration")
        self.dialog.geometry("480x320")
        self.dialog.transient(parent)
        self.dialog.grab_set()
        self.dialog.resizable(False, False)

        pad = {"padx": 10, "pady": 6}

        # Backend selector
        ttk.Label(self.dialog, text="Backend:").grid(row=0, column=0, sticky=tk.W, **pad)
        self.backend_var = tk.StringVar(value=cfg.get("llm_backend", "ollama"))
        backend_combo = ttk.Combobox(self.dialog, textvariable=self.backend_var,
                                     values=["ollama", "claude"], state="readonly", width=18)
        backend_combo.grid(row=0, column=1, sticky=tk.W, **pad)
        backend_combo.bind("<<ComboboxSelected>>", self._on_backend_change)

        # Ollama endpoint
        ttk.Label(self.dialog, text="Ollama Endpoint:").grid(row=1, column=0, sticky=tk.W, **pad)
        self.endpoint_entry = ttk.Entry(self.dialog, width=34)
        self.endpoint_entry.insert(0, cfg.get("llm_endpoint", "http://localhost:11434"))
        self.endpoint_entry.grid(row=1, column=1, sticky=tk.W, **pad)

        # Ollama model
        ttk.Label(self.dialog, text="Ollama Model:").grid(row=2, column=0, sticky=tk.W, **pad)
        self.model_entry = ttk.Entry(self.dialog, width=34)
        self.model_entry.insert(0, cfg.get("llm_model", "gemma2:27b"))
        self.model_entry.grid(row=2, column=1, sticky=tk.W, **pad)

        # Claude model
        ttk.Label(self.dialog, text="Claude Model:").grid(row=3, column=0, sticky=tk.W, **pad)
        self.claude_model_entry = ttk.Entry(self.dialog, width=34)
        self.claude_model_entry.insert(0, cfg.get("claude_model", "claude-haiku-4-5-20251001"))
        self.claude_model_entry.grid(row=3, column=1, sticky=tk.W, **pad)

        # Claude API key
        ttk.Label(self.dialog, text="Claude API Key:").grid(row=4, column=0, sticky=tk.W, **pad)
        self.api_key_entry = ttk.Entry(self.dialog, width=34, show="*")
        self.api_key_entry.insert(0, cfg.get("claude_api_key", ""))
        self.api_key_entry.grid(row=4, column=1, sticky=tk.W, **pad)

        note = ttk.Label(self.dialog,
                         text="Ollama must be running locally ('ollama serve').\n"
                              "Claude requires an Anthropic API key.",
                         foreground="gray", font=("TkDefaultFont", 8))
        note.grid(row=5, column=0, columnspan=2, pady=4)

        btn_frame = ttk.Frame(self.dialog)
        btn_frame.grid(row=6, column=0, columnspan=2, pady=12)
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.dialog.destroy).pack(side=tk.LEFT, padx=5)

        self._on_backend_change()
        self.dialog.wait_window()

    def _on_backend_change(self, _event=None):
        is_ollama = self.backend_var.get() == "ollama"
        state_ollama = tk.NORMAL if is_ollama else tk.DISABLED
        state_claude = tk.NORMAL if not is_ollama else tk.DISABLED
        self.endpoint_entry.config(state=state_ollama)
        self.model_entry.config(state=state_ollama)
        self.claude_model_entry.config(state=state_claude)
        self.api_key_entry.config(state=state_claude)

    def _save(self):
        backend = self.backend_var.get()
        self.result = {
            "llm_backend":    backend,
            "llm_endpoint":   self.endpoint_entry.get().strip(),
            "llm_model":      self.model_entry.get().strip(),
            "claude_model":   self.claude_model_entry.get().strip(),
            "claude_api_key": self.api_key_entry.get().strip(),
        }
        self.dialog.destroy()


class ColorCalibrationDialog:
    """
    Dialog for assigning MUD room section roles to ANSI colors.

    Shows one representative line per distinct color seen in recent MUD output.
    The user assigns each a role: Room Title, Description, Objects, Mobs/PCs,
    Exits, or Ignore.  On OK the result is written to ai_config["mud_structure"].
    """

    ROLES = ['(unassigned)', 'room_title', 'description', 'objects', 'mobs', 'exits']
    ROLE_LABELS = {
        '(unassigned)': '(unassigned)',
        'room_title':   'Room Title',
        'description':  'Description',
        'objects':      'Objects',
        'mobs':         'Mobs/PCs',
        'exits':        'Exits',
    }

    def __init__(self, parent, color_samples, current_structure):
        """
        color_samples   : list of (hex_color, sample_text) tuples
        current_structure: dict {role: hex_color} from existing mud_structure
        """
        self.result = None

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Configure Room Colors")
        self.dialog.transient(parent)
        self.dialog.grab_set()
        self.dialog.resizable(False, False)

        # Reverse map: color → current role (for pre-populating dropdowns)
        color_to_role = {v.lower(): k for k, v in current_structure.items() if v}

        tk.Label(self.dialog,
                 text="Assign a role to each color seen in recent MUD output:",
                 anchor='w').grid(row=0, column=0, columnspan=3,
                                  sticky='w', padx=10, pady=(10, 4))
        tk.Label(self.dialog, text="Sample text", anchor='w',
                 font=('TkDefaultFont', 9, 'bold')).grid(
            row=1, column=1, sticky='w', padx=4)
        tk.Label(self.dialog, text="Role", anchor='w',
                 font=('TkDefaultFont', 9, 'bold')).grid(
            row=1, column=2, sticky='w', padx=4)

        self._vars = {}   # color_hex -> StringVar
        role_label_list = [self.ROLE_LABELS[r] for r in self.ROLES]

        for row_idx, (color, sample) in enumerate(color_samples, start=2):
            # Colour swatch
            swatch = tk.Label(self.dialog, bg=color, width=3, relief='raised')
            swatch.grid(row=row_idx, column=0, padx=(10, 4), pady=3, sticky='ns')

            # Sample text rendered in the actual color
            sample_text = sample[:60] + ('…' if len(sample) > 60 else '')
            tk.Label(self.dialog, text=sample_text, fg=color,
                     anchor='w', width=50,
                     bg=self.dialog.cget('bg')).grid(
                row=row_idx, column=1, sticky='w', padx=4)

            # Role dropdown — pre-select from existing mud_structure
            current_role = color_to_role.get(color.lower(), '(unassigned)')
            current_label = self.ROLE_LABELS.get(current_role, '(unassigned)')
            var = tk.StringVar(value=current_label)
            self._vars[color] = var
            ttk.OptionMenu(self.dialog, var, current_label,
                           *role_label_list).grid(
                row=row_idx, column=2, sticky='w', padx=(4, 10), pady=3)

        # Buttons
        btn_frame = tk.Frame(self.dialog)
        n_rows = 2 + len(color_samples)
        btn_frame.grid(row=n_rows, column=0, columnspan=3, pady=10)
        ttk.Button(btn_frame, text="OK",     command=self._ok).pack(side=tk.LEFT,  padx=6)
        ttk.Button(btn_frame, text="Cancel", command=self._cancel).pack(side=tk.LEFT, padx=6)

        self.dialog.bind('<Return>', lambda _: self._ok())
        self.dialog.bind('<Escape>', lambda _: self._cancel())
        self.dialog.wait_window()

    def _ok(self):
        # Build label → role reverse map
        label_to_role = {v: k for k, v in self.ROLE_LABELS.items()}
        structure = {}
        for color, var in self._vars.items():
            role = label_to_role.get(var.get(), '(unassigned)')
            if role != '(unassigned)':
                structure[role] = color
        self.result = structure
        self.dialog.destroy()

    def _cancel(self):
        self.dialog.destroy()


class ProfileDialog:
    """Dialog for creating/editing profiles"""
    def __init__(self, parent, title, name="", host="", port="4000", character="", password=""):
        self.result = None
        
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        self.dialog.geometry("450x340")
        self.dialog.transient(parent)
        self.dialog.grab_set()
        
        # Profile name
        ttk.Label(self.dialog, text="Profile Name:").grid(row=0, column=0, sticky=tk.W, padx=10, pady=8)
        self.name_entry = ttk.Entry(self.dialog, width=30)
        self.name_entry.grid(row=0, column=1, padx=10, pady=8)
        self.name_entry.insert(0, name)
        
        # Host
        ttk.Label(self.dialog, text="Host:").grid(row=1, column=0, sticky=tk.W, padx=10, pady=8)
        self.host_entry = ttk.Entry(self.dialog, width=30)
        self.host_entry.grid(row=1, column=1, padx=10, pady=8)
        self.host_entry.insert(0, host)
        
        # Port
        ttk.Label(self.dialog, text="Port:").grid(row=2, column=0, sticky=tk.W, padx=10, pady=8)
        self.port_entry = ttk.Entry(self.dialog, width=30)
        self.port_entry.grid(row=2, column=1, padx=10, pady=8)
        self.port_entry.insert(0, port)
        
        # Character name
        ttk.Label(self.dialog, text="Character Name:").grid(row=3, column=0, sticky=tk.W, padx=10, pady=8)
        self.character_entry = ttk.Entry(self.dialog, width=30)
        self.character_entry.grid(row=3, column=1, padx=10, pady=8)
        self.character_entry.insert(0, character)
        
        # Password
        ttk.Label(self.dialog, text="Password:").grid(row=4, column=0, sticky=tk.W, padx=10, pady=8)
        self.password_entry = ttk.Entry(self.dialog, width=30, show="*")
        self.password_entry.grid(row=4, column=1, padx=10, pady=8)
        self.password_entry.insert(0, password)
        
        # Note about prompts
        note = ttk.Label(self.dialog, text="Note: Use Connection > 'Send Character Name' and 'Send Password'\nto automatically learn login prompts",
                        foreground="gray", font=("TkDefaultFont", 8))
        note.grid(row=5, column=0, columnspan=2, pady=8)
        
        # Buttons
        button_frame = ttk.Frame(self.dialog)
        button_frame.grid(row=6, column=0, columnspan=2, pady=20)
        
        ttk.Button(button_frame, text="OK", command=self.ok_clicked).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Cancel", command=self.cancel_clicked).pack(side=tk.LEFT, padx=5)
        
        self.dialog.wait_window()
    
    def ok_clicked(self):
        name = self.name_entry.get().strip()
        host = self.host_entry.get().strip()
        port = self.port_entry.get().strip()
        character = self.character_entry.get().strip()
        password = self.password_entry.get()
        
        if not name:
            messagebox.showerror("Error", "Profile name is required")
            return
        if not host:
            messagebox.showerror("Error", "Host is required")
            return
        if not port:
            messagebox.showerror("Error", "Port is required")
            return
        
        self.result = (name, host, port, character, password)
        self.dialog.destroy()
    
    def cancel_clicked(self):
        self.dialog.destroy()


def main():
    root = tk.Tk()
    app = MUDClient(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
