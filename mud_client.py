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
from tkinter import scrolledtext, messagebox, ttk, simpledialog
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

        # Session logging
        from session_logger import SessionLogger
        self.session_logger = SessionLogger()

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
        # Connection frame
        connection_frame = ttk.LabelFrame(self.master, text="Connection", padding=10)
        connection_frame.pack(fill=tk.X, padx=10, pady=5)
        
        # Profile selection (row 0)
        ttk.Label(connection_frame, text="Profile:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(connection_frame, textvariable=self.profile_var, width=20, state="readonly")
        self.profile_combo.grid(row=0, column=1, padx=5)
        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)
        
        ttk.Button(connection_frame, text="New", command=self.new_profile, width=8).grid(row=0, column=2, padx=2)
        ttk.Button(connection_frame, text="Edit", command=self.edit_profile, width=8).grid(row=0, column=3, padx=2)
        ttk.Button(connection_frame, text="Delete", command=self.delete_profile, width=8).grid(row=0, column=4, padx=2)
        
        # Host input (row 1)
        ttk.Label(connection_frame, text="Host:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.host_entry = ttk.Entry(connection_frame, width=30)
        self.host_entry.grid(row=1, column=1, padx=5, pady=5)
        self.host_entry.insert(0, "mud.example.com")
        
        # Port input
        ttk.Label(connection_frame, text="Port:").grid(row=1, column=2, sticky=tk.W, padx=5)
        self.port_entry = ttk.Entry(connection_frame, width=10)
        self.port_entry.grid(row=1, column=3, padx=5)
        self.port_entry.insert(0, "4000")
        
        # Connect button
        self.connect_btn = ttk.Button(connection_frame, text="Connect", command=self.toggle_connection)
        self.connect_btn.grid(row=1, column=4, padx=5)
        
        # Status label
        self.status_label = ttk.Label(connection_frame, text="Disconnected", foreground="red")
        self.status_label.grid(row=1, column=5, padx=5)
        
        # Room tracking checkbox (row 2)
        self.room_tracking_var = tk.BooleanVar(value=False)
        self.room_tracking_check = ttk.Checkbutton(
            connection_frame, 
            text="Enable Room Tracking", 
            variable=self.room_tracking_var,
            command=self.toggle_room_tracking
        )
        self.room_tracking_check.grid(row=2, column=0, columnspan=2, sticky=tk.W, padx=5, pady=5)
        
        # Room tracking status label
        self.room_tracking_status = ttk.Label(connection_frame, text="", foreground="gray")
        self.room_tracking_status.grid(row=2, column=2, columnspan=3, sticky=tk.W, padx=5)

        # AI speed control (row 3)
        ttk.Label(connection_frame, text="AI Speed:").grid(
            row=3, column=0, sticky=tk.W, padx=5, pady=3)
        self._ai_speed_var = tk.IntVar(value=1500)
        self._ai_speed_label = ttk.Label(connection_frame, text="1.5s", width=4)
        self._ai_speed_label.grid(row=3, column=2, sticky=tk.W, padx=2)
        ai_speed_slider = ttk.Scale(
            connection_frame,
            from_=500, to=6000,
            orient=tk.HORIZONTAL,
            variable=self._ai_speed_var,
            command=self._on_speed_change,
            length=200,
        )
        ai_speed_slider.grid(row=3, column=1, sticky=tk.W, padx=5)

        # Session log indicator
        self._log_label = ttk.Label(connection_frame, text="Log: off", foreground="gray")
        self._log_label.grid(row=3, column=3, columnspan=3, sticky=tk.W, padx=5)

        # Text display area
        display_frame = ttk.LabelFrame(self.master, text="MUD Output", padding=10)
        display_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        self.text_area = scrolledtext.ScrolledText(
            display_frame,
            wrap=tk.WORD,
            width=80,
            height=20,
            font=("Courier", 10),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white"
        )
        self.text_area.pack(fill=tk.BOTH, expand=True)
        self.text_area.config(state=tk.DISABLED)
        
        # Input frame
        input_frame = ttk.Frame(self.master, padding=10)
        input_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(input_frame, text="Send:").pack(side=tk.LEFT, padx=5)
        
        self.input_entry = ttk.Entry(input_frame)
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.input_entry.bind('<Return>', self.send_message)
        
        self.send_btn = ttk.Button(input_frame, text="Send", command=self.send_message)
        self.send_btn.pack(side=tk.LEFT, padx=5)
        self.send_btn.config(state=tk.DISABLED)
        
        # Send Character Name button
        self.send_char_btn = ttk.Button(input_frame, text="Send Char Name", command=self.send_character_name)
        self.send_char_btn.pack(side=tk.LEFT, padx=5)
        self.send_char_btn.config(state=tk.DISABLED)
        
        # Send Password button
        self.send_pass_btn = ttk.Button(input_frame, text="Send Password", command=self.send_password)
        self.send_pass_btn.pack(side=tk.LEFT, padx=5)
        self.send_pass_btn.config(state=tk.DISABLED)
        
        # Send & Remember button
        self.send_remember_btn = ttk.Button(input_frame, text="Send & Remember", command=self.send_and_remember)
        self.send_remember_btn.pack(side=tk.LEFT, padx=5)
        self.send_remember_btn.config(state=tk.DISABLED)
        
        # Quit button
        self.quit_btn = ttk.Button(input_frame, text="Quit", command=self.start_quit_sequence)
        self.quit_btn.pack(side=tk.LEFT, padx=5)
        self.quit_btn.config(state=tk.DISABLED)

        # AI Mode button
        self.ai_mode_btn = ttk.Button(input_frame, text="AI Mode: OFF", command=self.toggle_ai_mode)
        self.ai_mode_btn.pack(side=tk.LEFT, padx=5)
        self.ai_mode_btn.config(state=tk.DISABLED)

        # AI Config button
        self.ai_config_btn = ttk.Button(input_frame, text="AI Config", command=self.open_ai_config)
        self.ai_config_btn.pack(side=tk.LEFT, padx=5)

        # Room Colors calibration button
        self.room_colors_btn = ttk.Button(input_frame, text="Room Colors",
                                          command=self.open_color_calibration)
        self.room_colors_btn.pack(side=tk.LEFT, padx=5)

        # Clear button
        clear_btn = ttk.Button(input_frame, text="Clear", command=self.clear_output)
        clear_btn.pack(side=tk.LEFT, padx=5)
        
        # Initialize profile list after all UI elements are created
        self.update_profile_list()
    
    def update_profile_list(self):
        """Update the profile dropdown list"""
        # Filter out the _settings key from profile names
        profile_names = [name for name in self.profiles.keys() if not name.startswith('_')]
        self.profile_combo['values'] = profile_names
        
        if profile_names and not self.profile_var.get():
            # Try to load the last connected profile
            last_profile = self.get_last_profile()
            if last_profile and last_profile in profile_names:
                self.profile_var.set(last_profile)
                self.on_profile_selected(None)
            else:
                # Default to the first profile if no last profile
                self.profile_combo.current(0)
                self.on_profile_selected(None)
    
    def on_profile_selected(self, event):
        """Handle profile selection"""
        profile_name = self.profile_var.get()
        if profile_name and profile_name in self.profiles:
            profile = self.profiles[profile_name]
            self.host_entry.delete(0, tk.END)
            self.host_entry.insert(0, profile.get('host', ''))
            self.port_entry.delete(0, tk.END)
            self.port_entry.insert(0, profile.get('port', '4000'))
            self.current_profile = profile_name
            
            # Load room tracking settings from profile
            self.room_color = profile.get('room_color', None)
            tracking_enabled = profile.get('room_tracking_enabled', False)
            self.room_tracking_var.set(tracking_enabled)
            self.room_tracking_enabled = tracking_enabled
            
            if tracking_enabled:
                room_count = len(profile.get('rooms', {}))
                self.room_tracking_status.config(
                    text=f"Tracking enabled ({room_count} rooms mapped)",
                    foreground="green"
                )
            else:
                self.room_tracking_status.config(text="", foreground="gray")
    
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
            self.profile_var.set(name)
            self.on_profile_selected(None)
    
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
            self.profile_var.set(name)
            self.on_profile_selected(None)
    
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
            self.update_profile_list()
        
    def toggle_connection(self):
        """Toggle connection state"""
        if self.connected:
            self.disconnect()
        else:
            self.connect()
    
    def connect(self):
        """Establish SSL connection to MUD server"""
        host = self.host_entry.get().strip()
        try:
            port = int(self.port_entry.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Invalid port number")
            return
        
        if not host:
            messagebox.showerror("Error", "Please enter a host address")
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
            self.status_label.config(text="Connected", foreground="green")
            self.connect_btn.config(text="Disconnect")
            self.send_btn.config(state=tk.NORMAL)
            self.send_char_btn.config(state=tk.NORMAL)
            self.send_pass_btn.config(state=tk.NORMAL)
            self.send_remember_btn.config(state=tk.NORMAL)
            self.quit_btn.config(state=tk.NORMAL)
            self.ai_mode_btn.config(state=tk.NORMAL)
            self.host_entry.config(state=tk.DISABLED)
            self.port_entry.config(state=tk.DISABLED)
            
            self.append_text(f"Connected successfully!\n", "system")
            self.session_logger.open()
            self._log_label.config(
                text=f"Log: {os.path.basename(self.session_logger.path)}",
                foreground="green")
            
            # Save this as the last connected profile
            if self.current_profile:
                self.save_last_profile(self.current_profile)
            
            # Check if autologin should be performed
            if self.current_profile and self.current_profile in self.profiles:
                profile = self.profiles[self.current_profile]
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
        self.status_label.config(text="Disconnected", foreground="red")
        self.connect_btn.config(text="Connect")
        self.send_btn.config(state=tk.DISABLED)
        self.send_char_btn.config(state=tk.DISABLED)
        self.send_pass_btn.config(state=tk.DISABLED)
        self.send_remember_btn.config(state=tk.DISABLED)
        self.quit_btn.config(state=tk.DISABLED)
        self.ai_mode_btn.config(state=tk.DISABLED)
        if self.ai_agent and self.ai_agent.is_running:
            self.ai_agent.stop()
        self.session_logger.close()
        self._log_label.config(text="Log: off", foreground="gray")
        self.quit_pending = False
        self.quit_stage = 0
        self.quit_prompts_seen = []
        self.host_entry.config(state=tk.NORMAL)
        self.port_entry.config(state=tk.NORMAL)
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
                
                # Track last non-empty line for prompt learning
                lines = clean_text.strip().split('\n')
                for line in reversed(lines):
                    if line.strip():
                        self.last_line = line.strip()
                        break
                
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
                        self.autologin_pending = False
                        self.message_queue.put(("system", "[Autologin] Login sequence completed\n"))
                        
                        # Enable entry room detection if room tracking is enabled
                        if self.room_tracking_enabled:
                            self.detect_entry_room = True
                            self.expecting_room_data = True
                            self.message_queue.put(("system", "[Room tracking] Detecting entry room...\n"))
                    except Exception as e:
                        self.message_queue.put(("error", f"Autologin failed: {e}\n"))
                        self.autologin_pending = False
                    break
    
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
                
                room_count = len(self.profiles[self.current_profile].get('rooms', {}))
                self.room_tracking_status.config(
                    text=f"Tracking enabled ({room_count} rooms mapped)",
                    foreground="green"
                )
                self.append_text("[Room tracking enabled - will detect and map rooms]\n", "system")
                
                if self.room_color:
                    self.append_text(f"[Room color detected: {self.room_color}]\n", "system")
                else:
                    self.append_text("[Room color not yet detected - will identify on first movement]\n", "system")
            else:
                self.room_tracking_status.config(text="No profile selected", foreground="red")
                self.room_tracking_var.set(False)
                self.room_tracking_enabled = False
        else:
            self.room_tracking_status.config(text="", foreground="gray")
            if self.current_profile and self.current_profile in self.profiles:
                self.profiles[self.current_profile]['room_tracking_enabled'] = False
                self.save_profiles()
            self.append_text("[Room tracking disabled]\n", "system")
    
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
                    room_count = len(self.profiles[self.current_profile].get('rooms', {}))
                    self.room_tracking_status.config(
                        text=f"Tracking enabled ({room_count} rooms mapped)",
                        foreground="green"
                    )
        
        if not self.room_color:
            return None
        
        # Extract room name (first line with room color)
        room_name = ""
        description_parts = []
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
                # Description text (non-room-color, before exits)
                if in_description and not exits_found and text_stripped:
                    description_parts.append(text_stripped)
        
        description = ' '.join(description_parts)
        
        if room_name:
            # Normalize description for stable hashing
            # Remove extra whitespace, normalize line breaks
            normalized_description = ' '.join(description.split())
            
            return {
                'name': room_name,
                'description': description,  # Keep original for display
                'normalized_description': normalized_description,  # Use for hashing
                'exits': exits
            }
        
        return None
    
    def process_room_data(self, segments):
        """Process and store room data"""
        if not self.room_tracking_enabled or not self.expecting_room_data:
            return
        
        if not self.current_profile or self.current_profile not in self.profiles:
            return
        
        # Parse room data
        room_data = self.parse_room_data(segments)

        if not room_data:
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
                    room_count = len(rooms)
                    self.room_tracking_status.config(
                        text=f"Tracking enabled ({room_count} rooms mapped)",
                        foreground="green"
                    )
                    self.append_text(f"[New room mapped: {room_data['name']} (Total: {room_count})]\n", "system")
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
                    room_count = len(rooms)
                    self.room_tracking_status.config(
                        text=f"Tracking enabled ({room_count} rooms mapped)",
                        foreground="green"
                    )
                    self.append_text(f"[New room mapped: {room_data['name']} (Total: {room_count})]\n", "system")
            
            self.expecting_room_data = False

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
                    
                    self.append_text(msg_data, "mud_colored")
                elif msg_type == "ai_text":
                    if self.ai_agent:
                        self.ai_agent.on_text_received(msg_data)
                elif msg_type == "telnet":
                    self.append_text(msg_data + "\n", "telnet")
                elif msg_type == "error":
                    self.append_text(msg_data, "error")
                    self.disconnect()
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

        try:
            # If in quit sequence and user manually sends something, learn it
            if self.quit_pending and self.last_line:
                self.learn_quit_response(self.last_line, message)
            
            # Track movement/look commands for room tracking
            message_lower = message.lower().strip()
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

    def _on_speed_change(self, value):
        """Update AI agent command delay from the speed slider."""
        ms = int(float(value))
        self._ai_speed_label.config(text=f"{ms/1000:.1f}s")
        if self.ai_agent:
            self.ai_agent.COMMAND_DELAY_MS = ms

    def toggle_ai_mode(self):
        """Toggle the autonomous AI exploration agent on or off."""
        if self.ai_agent and self.ai_agent.is_running:
            self.ai_agent.stop()
            self.ai_mode_btn.config(text="AI Mode: OFF")
            self.append_text("[AI mode disabled]\n", "system")
        else:
            if not self.room_tracking_enabled:
                messagebox.showwarning("AI Mode", "Please enable Room Tracking before starting AI Mode.")
                return
            from ai_agent import ExplorationAgent
            if not self.ai_agent:
                self.ai_agent = ExplorationAgent(self)
            self.ai_agent.start()
            self.ai_mode_btn.config(text="AI Mode: ON")
            self.append_text("[AI mode enabled — autonomous exploration started]\n", "system")

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
        note = ttk.Label(self.dialog, text="Note: Use 'Send Char Name' and 'Send Password' buttons\nto automatically learn login prompts", 
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
