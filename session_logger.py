# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""
Session Logger for AIWanderer.

Writes a timestamped plain-text log of every MUD session to:
    ~/mud_sessions/session_YYYY-MM-DD_HH-MM-SS.log

Log format:
    [HH:MM:SS] TYPE | text

Types:
    MUD     — raw text received from the server (ANSI stripped)
    CMD     — command sent by the human player
    AI_CMD  — command sent by the AI agent
    AI      — AI decision / status message
    SYSTEM  — client system messages
    ERROR   — errors
    LLM_OUT — full prompt sent to the LLM
    LLM_IN  — raw response received from the LLM
    ADVISOR — free-text advice from the LLM advisor
"""

import os
import re
from datetime import datetime


# Map mud_client append_text() msg_type values to log type labels
_TYPE_MAP = {
    "mud":         "MUD",
    "mud_colored": "MUD",
    "user":        "CMD",
    "system":      "SYSTEM",
    "error":       "ERROR",
    "telnet":      None,   # skip telnet negotiation noise
}

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mGKHF]')


def _strip_ansi(text):
    return _ANSI_RE.sub('', text)


class SessionLogger:
    """Writes a session log file for one MUD connection."""

    LOG_DIR = os.path.join(os.path.expanduser("~"), "mud_sessions")

    def __init__(self):
        self._file = None
        self._path = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self):
        """Open a new log file for this session."""
        os.makedirs(self.LOG_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._path = os.path.join(self.LOG_DIR, f"session_{stamp}.log")
        self._file = open(self._path, 'w', encoding='utf-8', buffering=1)
        self._write("SYSTEM", f"Session started — log: {self._path}")

    def close(self):
        """Close the log file."""
        if self._file:
            self._write("SYSTEM", "Session ended.")
            self._file.close()
            self._file = None

    @property
    def path(self):
        return self._path

    # ------------------------------------------------------------------
    # Logging API
    # ------------------------------------------------------------------

    def log_received(self, text):
        """Log text received from the MUD server."""
        self._write("MUD", _strip_ansi(text))

    def log_command(self, command):
        """Log a command sent by the human player."""
        self._write("CMD", command)

    def log_ai_command(self, command):
        """Log a command sent by the AI agent."""
        self._write("AI_CMD", command)

    def log_system(self, text):
        """Log a client system message."""
        self._write("SYSTEM", text)

    def log_ai(self, text):
        """Log an AI decision or status message."""
        self._write("AI", text)

    def log_error(self, text):
        """Log an error."""
        self._write("ERROR", text)

    def log_llm_prompt(self, text):
        """Log the full prompt sent to the LLM."""
        self._write("LLM_OUT", text)

    def log_llm_response(self, text):
        """Log the raw response received from the LLM."""
        self._write("LLM_IN", text)

    def log_advisor(self, text):
        """Log LLM advisor response."""
        self._write("ADVISOR", text)

    def log_session_summary(self, text):
        """Log the end-of-session summary generated for cross-session persistence."""
        self._write("SUMMARY", text)

    def log_append(self, text, msg_type):
        """
        Route an append_text() call to the appropriate log method.
        msg_type matches the values used in MUDClient.append_text().
        """
        # Detect AI messages by their prefix even when typed as "system"
        if msg_type == "system" and text.startswith("[AI"):
            self.log_ai(text.rstrip())
            return

        log_type = _TYPE_MAP.get(msg_type)
        if log_type is None:
            return   # skip telnet noise and unknown types

        # mud_colored is a list of (text, color) tuples — join the text parts
        if msg_type == "mud_colored":
            text = "".join(seg for seg, _ in text)

        clean = _strip_ansi(text).rstrip()
        if not clean:
            return

        if log_type == "MUD":
            self.log_received(clean)
        elif log_type == "CMD":
            # strip the "> " prefix append_text adds
            self.log_command(clean.lstrip("> "))
        elif log_type == "SYSTEM":
            self.log_system(clean)
        elif log_type == "ERROR":
            self.log_error(clean)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write(self, log_type, text):
        if not self._file:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        # Write each line separately so multi-line blocks are readable
        for line in text.splitlines():
            if line.strip():
                self._file.write(f"[{ts}] {log_type:7s} | {line}\n")
