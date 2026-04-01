# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""
MUD Text Parser for AIWanderer.

Stateless utilities for extracting structured data from raw MUD text.
All methods are pure functions — no side effects, no state.
"""

import re


class MUDTextParser:
    """
    Parses common MUD text patterns into structured data.

    Designed to be tolerant of variation — MUDs differ widely in their
    output formats, so patterns are tried in priority order and the first
    match wins.
    """

    # ------------------------------------------------------------------
    # Prompt / stat parsing
    # ------------------------------------------------------------------

    # Patterns are tried in order; first match wins.
    # Each pattern must have named groups for the stats it captures.
    # All stat values are integers.
    PROMPT_PATTERNS = [
        # "24H 100M 85V >"  or  "24H 100M 85V 0%T 0%O >"  or  "24H 100M 85MV >"
        re.compile(
            r'(?P<hp>\d+)\s*[Hh]\w*\s+(?P<mp>\d+)\s*[Mm]\w*'
            r'(?:\s+(?P<mv>\d+)\s*(?:[Mm][Vv]\w*|[Vv]\w*))?'
            r'(?:\s+(?P<tank>\d+)%\s*[Tt]\w*)?'
            r'(?:\s+(?P<opp>\d+)%\s*[Oo]\w*)?',
            re.IGNORECASE
        ),
        # "[24/100hp 100/100mana 85mv]"  or  "[24/100hp 100/100mana 85/150mv]"
        re.compile(
            r'\[(?P<hp>\d+)(?:/\d+)?\s*hp\s+(?P<mp>\d+)(?:/\d+)?\s*m(?:ana)?\s*'
            r'(?P<mv>\d+)(?:/\d+)?\s*mv\]',
            re.IGNORECASE
        ),
        # "<24hp 100mana 85mv>"
        re.compile(
            r'<\s*(?P<hp>\d+)\s*hp\s+(?P<mp>\d+)\s*m(?:ana)?\s+(?P<mv>\d+)\s*mv\s*>',
            re.IGNORECASE
        ),
        # "Hp: 24/100  Mana: 100/100  Mv: 85/150"
        re.compile(
            r'[Hh]p\s*:\s*(?P<hp>\d+)(?:/\d+)?\s+[Mm]ana?\s*:\s*(?P<mp>\d+)(?:/\d+)?\s+[Mm]v\s*:\s*(?P<mv>\d+)',
            re.IGNORECASE
        ),
        # Bare "24/100 100/100 85/150" (three current/max pairs)
        re.compile(
            r'\b(?P<hp>\d+)/\d+\s+(?P<mp>\d+)/\d+\s+(?P<mv>\d+)/\d+\b'
        ),
    ]

    def parse_prompt_stats(self, text):
        """
        Extract HP/MP/MV from a prompt or stat line.

        Returns a dict like {"hp": 24, "mp": 100, "mv": 85} with any
        stats found, or None if nothing matched. Missing stats are omitted.
        """
        for pattern in self.PROMPT_PATTERNS:
            for m in pattern.finditer(text):
                result = {}
                for key in ('hp', 'mp', 'mv', 'tank', 'opp'):
                    val = m.group(key) if key in pattern.groupindex else None
                    if val is not None:
                        try:
                            result[key] = int(val)
                        except ValueError:
                            pass
                if result:
                    return result
        return None

    # ------------------------------------------------------------------
    # SCORE / character sheet parsing
    # ------------------------------------------------------------------
    #
    # MUDs vary enormously in SCORE format.  We try to pull out the most
    # useful fields using flexible patterns rather than expecting a fixed layout.
    #
    # Fields extracted (all optional, returned as a dict):
    #   level, class_name, race, max_hp, max_mp, max_mv,
    #   xp, xp_next, gold, alignment

    _SCORE_PATTERNS = {
        'level': [
            re.compile(r'\blev(?:el)?\s*[:\-]?\s*(?P<v>\d+)\b', re.IGNORECASE),
            re.compile(r'\blv\.?\s*(?P<v>\d+)\b', re.IGNORECASE),
        ],
        'class_name': [
            re.compile(r'\bclass\s*[:\-]\s*(?P<v>[A-Za-z][A-Za-z \-]+?)(?:\s{2,}|\n|$)', re.IGNORECASE),
            # "Level 5 Warrior" — word after the level number
            re.compile(r'\blev(?:el)?\s+\d+\s+(?P<v>[A-Z][a-z]+)\b'),
        ],
        'max_hp': [
            # CircleMUD: "7(25) hit"
            re.compile(r'\b\d+\s*\(\s*(?P<v>\d+)\s*\)\s+hit\b', re.IGNORECASE),
            re.compile(r'\bhp\s*[:\-]?\s*\d+\s*/\s*(?P<v>\d+)\b', re.IGNORECASE),
            re.compile(r'\bhp\s*[:\-]?\s*\d+\s*\(\s*(?P<v>\d+)\s*\)', re.IGNORECASE),
            re.compile(r'\bmax\s*hp\s*[:\-]?\s*(?P<v>\d+)\b', re.IGNORECASE),
            re.compile(r'\bhit\s*points?\s*[:\-]?\s*\d+\s*/\s*(?P<v>\d+)\b', re.IGNORECASE),
        ],
        'max_mp': [
            # CircleMUD: "100(100) mana"
            re.compile(r'\b\d+\s*\(\s*(?P<v>\d+)\s*\)\s+mana\b', re.IGNORECASE),
            re.compile(r'\bm(?:ana|p)\s*[:\-]?\s*\d+\s*/\s*(?P<v>\d+)\b', re.IGNORECASE),
            re.compile(r'\bmana\s*[:\-]?\s*\d+\s*\(\s*(?P<v>\d+)\s*\)', re.IGNORECASE),
            re.compile(r'\bmax\s*m(?:ana|p)\s*[:\-]?\s*(?P<v>\d+)\b', re.IGNORECASE),
        ],
        'max_mv': [
            # CircleMUD: "84(84) movement points"
            re.compile(r'\b\d+\s*\(\s*(?P<v>\d+)\s*\)\s+movement\b', re.IGNORECASE),
            re.compile(r'\bmv\s*[:\-]?\s*\d+\s*/\s*(?P<v>\d+)\b', re.IGNORECASE),
            re.compile(r'\bmove(?:ment)?\s*[:\-]?\s*\d+\s*/\s*(?P<v>\d+)\b', re.IGNORECASE),
            re.compile(r'\bmax\s*mv\s*[:\-]?\s*(?P<v>\d+)\b', re.IGNORECASE),
        ],
        'xp': [
            # CircleMUD: "You have scored 16 exp"
            re.compile(r'\bscored\s+(?P<v>[\d,]+)\s+exp\b', re.IGNORECASE),
            re.compile(r'\bexp(?:erience)?\s*[:\-]?\s*(?P<v>[\d,]+)\s*/\s*[\d,]+\b', re.IGNORECASE),
            re.compile(r'\bexp(?:erience)?\s*[:\-]?\s*(?P<v>[\d,]+)\b', re.IGNORECASE),
            re.compile(r'\bxp\s*[:\-]?\s*(?P<v>[\d,]+)\b', re.IGNORECASE),
        ],
        'xp_next': [
            # CircleMUD: "You need 1984 exp to reach your next level"
            re.compile(r'\bneed\s+(?P<v>[\d,]+)\s+exp\b', re.IGNORECASE),
            re.compile(r'\bexp(?:erience)?\s*[:\-]?\s*[\d,]+\s*/\s*(?P<v>[\d,]+)\b', re.IGNORECASE),
            re.compile(r'\bnext\s*(?:level)?\s*[:\-]?\s*(?P<v>[\d,]+)\b', re.IGNORECASE),
            re.compile(r'\bto\s*level\s*[:\-]?\s*(?P<v>[\d,]+)\b', re.IGNORECASE),
        ],
        'gold': [
            # CircleMUD: "have 0 gold coins"
            re.compile(r'\b(?P<v>[\d,]+)\s+gold\s+coin', re.IGNORECASE),
            re.compile(r'\bgold\s*[:\-]?\s*(?P<v>[\d,]+)\b', re.IGNORECASE),
            re.compile(r'\bcoins?\s*[:\-]?\s*(?P<v>[\d,]+)\b', re.IGNORECASE),
            re.compile(r'\bgp\s*[:\-]?\s*(?P<v>[\d,]+)\b', re.IGNORECASE),
        ],
        'alignment': [
            # CircleMUD: "your alignment is 0" (numeric)
            re.compile(r'\balignment\s+is\s+(?P<v>-?\d+)\b', re.IGNORECASE),
            re.compile(r'\balign(?:ment)?\s*[:\-]\s*(?P<v>-?\d+)\b', re.IGNORECASE),
            re.compile(r'\balign(?:ment)?\s*[:\-]?\s*(?P<v>[A-Za-z]+)\b', re.IGNORECASE),
            re.compile(r'\b(?P<v>good|neutral|evil|lawful|chaotic)\b', re.IGNORECASE),
        ],
        'hunger': [
            # Detect hunger status in score output
            re.compile(r'\b(?P<v>starving|famished|dying of hunger)\b', re.IGNORECASE),
            re.compile(r'\byou are (?P<v>hungry)\b', re.IGNORECASE),
            re.compile(r'\b(?P<v>hungry)\b', re.IGNORECASE),
        ],
        'thirst': [
            # Detect thirst status in score output
            re.compile(r'\b(?P<v>parched|dying of thirst)\b', re.IGNORECASE),
            re.compile(r'\byou are (?P<v>thirsty)\b', re.IGNORECASE),
            re.compile(r'\b(?P<v>thirsty)\b', re.IGNORECASE),
        ],
    }

    def parse_score(self, text):
        """
        Extract character sheet data from a SCORE / STAT command response.

        Returns a dict with any fields found (level, class_name, race,
        max_hp, max_mp, max_mv, xp, xp_next, gold, alignment, hunger, thirst).
        Returns None if nothing at all was matched.
        """
        result = {}
        for field, patterns in self._SCORE_PATTERNS.items():
            for pat in patterns:
                m = pat.search(text)
                if m:
                    raw = m.group('v').strip()
                    # Integer fields (alignment may be numeric or string)
                    if field not in ('class_name', 'race', 'hunger', 'thirst'):
                        try:
                            result[field] = int(raw.replace(',', ''))
                        except ValueError:
                            if field == 'alignment':
                                result[field] = raw  # accept named alignment
                    else:
                        result[field] = raw
                    break  # first matching pattern wins for this field
        
        # Normalize hunger/thirst to canonical levels
        if 'hunger' in result:
            h = result['hunger'].lower()
            if 'starv' in h or 'famish' in h or 'dying' in h:
                result['hunger'] = 'starving'
            else:
                result['hunger'] = 'hungry'
        
        if 'thirst' in result:
            t = result['thirst'].lower()
            if 'parch' in t or 'dying' in t:
                result['thirst'] = 'parched'
            else:
                result['thirst'] = 'thirsty'
        
        return result if result else None

    # ------------------------------------------------------------------
    # Spell affects / buff duration parsing
    # ------------------------------------------------------------------
    #
    # MUDs report active spells in score output, e.g.:
    #   "Sanctuary        :  47 ticks"
    #   "You are affected by bless for 23 more ticks."
    #   "armor            [12 ticks remaining]"
    #
    # We care about the protection buffs Otto provides.
    _BUFF_NAMES = {
        'sanctuary': 'sanctuary',
        'bless':     'bless',
        'armor':     'armor',
        'armour':    'armor',   # British spelling variant
    }

    _SPELL_AFFECT_RE = re.compile(
        r'\b(?P<spell>sanctuary|bless|armo(?:u?)r)\b'
        r'[^0-9]{0,30}'        # separator: colon, space, "for", ":", "[", etc.
        r'(?P<ticks>\d+)'
        r'[^a-z]{0,10}ticks?', # "tick" or "ticks"
        re.IGNORECASE
    )

    # CircleMUD score SPL line: "SPL: (  3hr) sanctuary             sets SANCT"
    # Hours here are MUD hours; 1 MUD hour ≈ 1 tick for spell duration purposes.
    _SPL_LINE_RE = re.compile(
        r'SPL:\s*\(\s*(?P<hours>\d+)\s*hr\)\s*(?P<spell>\S+)',
        re.IGNORECASE
    )

    def parse_spell_affects(self, text):
        """
        Extract active buff durations from score output.

        Handles two formats:
          - Legacy "N ticks" format
          - CircleMUD SPL line: "SPL: ( Xhr) spell_name ..."

        Returns a dict of {canonical_buff_name: ticks_remaining} for any
        protection buffs found.  Empty dict if none detected.
        In both cases the value is in ticks (1 tick ≈ 1 MUD hour).
        """
        result = {}
        # SPL lines take priority — most accurate source
        for m in self._SPL_LINE_RE.finditer(text):
            spell_raw = m.group('spell').lower()
            canonical = self._BUFF_NAMES.get(spell_raw)
            if canonical:
                hours = int(m.group('hours'))
                if canonical not in result or hours > result[canonical]:
                    result[canonical] = hours
        # Fall back to old "N ticks" format if SPL lines didn't cover everything
        for m in self._SPELL_AFFECT_RE.finditer(text):
            spell_raw = m.group('spell').lower()
            canonical = self._BUFF_NAMES.get(spell_raw)
            if canonical and canonical not in result:
                ticks = int(m.group('ticks'))
                result[canonical] = ticks
        return result

    # ------------------------------------------------------------------
    # Buff application / expiration detection
    # ------------------------------------------------------------------
    #
    # CircleMUD sends confirmation messages when a spell takes effect and
    # expiration messages when it wears off.  These let the agent update
    # buff_expires immediately without waiting for the next score parse.
    #
    # Known CircleMUD tick durations (used as default expiry when confirmed
    # by message rather than by SPL output from score):
    #   bless:     6 ticks
    #   sanctuary: 4 ticks
    #   armor:    24 ticks  (no distinct "You feel..." message — rely on SPL)

    BUFF_DEFAULT_TICKS = {
        'bless':     6,
        'sanctuary': 4,
        'armor':     24,
    }

    _BUFF_APPLIED_PATTERNS = {
        'bless':     re.compile(r'you feel righteous', re.IGNORECASE),
        'sanctuary': re.compile(r'you feel someone protecting you', re.IGNORECASE),
    }

    _BUFF_EXPIRED_PATTERNS = {
        'bless':     re.compile(r'you feel less righteous', re.IGNORECASE),
        'sanctuary': re.compile(r'you feel less protected', re.IGNORECASE),
        'armor':     re.compile(r'you feel less (?:armored|armoured)', re.IGNORECASE),
    }

    def detect_buff_events(self, text):
        """
        Detect buff application and expiration messages.

        Returns a dict:
            {
                "applied": [buff_name, ...],   # buffs just confirmed applied
                "expired": [buff_name, ...],   # buffs just confirmed expired
            }
        """
        applied = []
        expired = []
        for buff, pat in self._BUFF_APPLIED_PATTERNS.items():
            if pat.search(text):
                applied.append(buff)
        for buff, pat in self._BUFF_EXPIRED_PATTERNS.items():
            if pat.search(text):
                expired.append(buff)
        return {"applied": applied, "expired": expired}

    # ------------------------------------------------------------------
    # MUD time parsing (for buff duration calibration)
    # ------------------------------------------------------------------
    #
    # The 'time' command reports current in-game time.  We extract the
    # MUD hour (0-23) so the agent can calibrate real seconds per MUD hour
    # and convert buff tick durations to accurate real-world expiry times.
    #
    # Common MUD time formats:
    #   "It is 3 o'clock in the morning."       -> 3
    #   "It is 1 o'clock in the afternoon."     -> 13
    #   "It is noon."                           -> 12
    #   "It is midnight."                       -> 0
    #   "It is 14:30 (game time)."              -> 14
    #   "The current time is 9 AM."             -> 9

    _MUD_TIME_MORNING_RE = re.compile(
        r"it is\s+(?P<h>\d{1,2})\s+o'?clock\s+in\s+the\s+morning",
        re.IGNORECASE
    )
    _MUD_TIME_AFTERNOON_RE = re.compile(
        r"it is\s+(?P<h>\d{1,2})\s+o'?clock\s+in\s+the\s+(?:afternoon|evening|night)",
        re.IGNORECASE
    )
    _MUD_TIME_24H_RE = re.compile(
        r'(?:it is|time is|current time:?)\s+(?P<h>\d{1,2}):(?P<m>\d{2})',
        re.IGNORECASE
    )
    _MUD_TIME_AMPM_RE = re.compile(
        r'(?:it is|time:?)\s+(?P<h>\d{1,2})\s*(?P<ampm>am|pm)',
        re.IGNORECASE
    )

    _MUD_TIME_NOON_RE = re.compile(r'\bit is\s+noon\b', re.IGNORECASE)
    _MUD_TIME_MIDNIGHT_RE = re.compile(r'\bmidnight\b', re.IGNORECASE)

    def parse_mud_time(self, text):
        """
        Extract current MUD hour (0-23) from 'time' command output.

        Returns an int 0-23, or None if no time found.
        """
        if self._MUD_TIME_MIDNIGHT_RE.search(text):
            return 0
        if self._MUD_TIME_NOON_RE.search(text):
            return 12
        # Try o'clock patterns before 24h — more specific to MUD format
        m = self._MUD_TIME_MORNING_RE.search(text)
        if m:
            return int(m.group('h')) % 12   # 12 AM -> 0
        m = self._MUD_TIME_AFTERNOON_RE.search(text)
        if m:
            return (int(m.group('h')) % 12) + 12   # 1 PM -> 13
        m = self._MUD_TIME_24H_RE.search(text)
        if m:
            return int(m.group('h')) % 24
        m = self._MUD_TIME_AMPM_RE.search(text)
        if m:
            h = int(m.group('h'))
            return h % 12 if m.group('ampm').lower() == 'am' else (h % 12) + 12
        return None

    # ------------------------------------------------------------------
    # XP / experience gain
    # ------------------------------------------------------------------

    XP_PATTERNS = [
        # "You receive 150 experience points."
        re.compile(r'you receive\s+(?P<xp>[\d,]+)\s+exp', re.IGNORECASE),
        # "You gain 150 experience."
        re.compile(r'you gain\s+(?P<xp>[\d,]+)\s+exp', re.IGNORECASE),
        # "Exp: +150"  or  "Experience: 150"
        re.compile(r'exp(?:erience)?\s*[:\+]\s*(?P<xp>[\d,]+)', re.IGNORECASE),
        # "+150 xp"
        re.compile(r'\+\s*(?P<xp>[\d,]+)\s+xp\b', re.IGNORECASE),
    ]

    def detect_xp_gain(self, text):
        """
        Return the XP gained as an int if the text mentions a gain, else None.
        Handles comma-separated numbers like "1,500".
        """
        for pattern in self.XP_PATTERNS:
            m = pattern.search(text)
            if m:
                try:
                    return int(m.group('xp').replace(',', ''))
                except ValueError:
                    pass
        return None

    # ------------------------------------------------------------------
    # Combat detection
    # ------------------------------------------------------------------

    COMBAT_START_RE = re.compile(
        r'(?:attacks? you|begins? fighting you|lunges? at you|'
        r'charges? at you|engages? you|strikes? at you)',
        re.IGNORECASE
    )

    COMBAT_ROUND_RE = re.compile(
        r'(?P<attacker>.+?)\s+(?P<verb>hits?|misses?|slashes?|slays?|'
        r'pierces?|bashes?|crushes?|stabs?|claws?|bites?|kicks?|punches?)\s+'
        r'(?P<target>.+?)(?:\s+for\s+(?P<damage>\d+)\s+damage)?[.!]',
        re.IGNORECASE
    )

    FLEE_RE = re.compile(
        r'(?:you flee|you run|you escape|you back away|you retreat)',
        re.IGNORECASE
    )

    COMBAT_ATTACKER_RE = re.compile(
        r'^(.+?)\s+(?:attacks?|begins?\s+fighting|lunges?\s+at|'
        r'charges?\s+at|engages?|strikes?\s+at)\s+you',
        re.IGNORECASE | re.MULTILINE
    )

    def detect_combat_start(self, text):
        """Return True if the text signals the start of combat."""
        return bool(self.COMBAT_START_RE.search(text))

    def detect_combat_attacker(self, text):
        """
        Return the name of the NPC that initiated combat, or None.
        e.g. "The Black Knight attacks you!" -> "The Black Knight"
        """
        m = self.COMBAT_ATTACKER_RE.search(text)
        if m:
            return m.group(1).strip()
        return None

    def detect_combat_round(self, text):
        """
        Parse a combat round line.

        Returns a dict like:
            {"attacker": str, "target": str, "verb": str, "damage": int|None}
        or None if no combat round is found.
        """
        m = self.COMBAT_ROUND_RE.search(text)
        if not m:
            return None
        damage = None
        if m.group('damage'):
            try:
                damage = int(m.group('damage'))
            except ValueError:
                pass
        return {
            "attacker": m.group('attacker').strip(),
            "target": m.group('target').strip(),
            "verb": m.group('verb').strip(),
            "damage": damage,
        }

    def detect_flee(self, text):
        """Return True if the text indicates the character fled combat."""
        return bool(self.FLEE_RE.search(text))

    OPPONENT_FLEE_RE = re.compile(
        r'(?:the\s+)?(?P<mob>[\w\s]+?)\s+panics,?\s+and\s+attempts\s+to\s+flee',
        re.IGNORECASE
    )

    def detect_opponent_flee(self, text):
        """
        Detect when an opponent flees from combat.
        Returns the mob name if detected, None otherwise.
        
        Example: "The beastly fido panics, and attempts to flee!" -> "beastly fido"
        """
        match = self.OPPONENT_FLEE_RE.search(text)
        if match:
            return match.group('mob').strip()
        return None

    MOVE_FAIL_RE = re.compile(
        r'(?:alas,?\s+you cannot go that way|you cannot go that way'
        r'|there is no exit|that direction does not exist'
        r'|you bump into|the door is closed|it is closed'
        r'|the guard humiliates you.*blocks your way'
        r'|you can\'t find it'
        r'|sorry,?\s+but you cannot do that here)',
        re.IGNORECASE
    )

    def detect_move_fail(self, text):
        """Return True if the MUD rejected a movement command."""
        return bool(self.MOVE_FAIL_RE.search(text))

    MOB_DEPARTURE_RE = re.compile(
        r'^(.+?)\s+(?:leaves?|departs?|walks?|wanders?|runs?|flees?|exits?)\s+'
        r'(?:north|south|east|west|up|down|in|out|the\s+\w+)',
        re.IGNORECASE | re.MULTILINE
    )
    MOB_ARRIVAL_RE = re.compile(
        r'^(.+?)\s+has\s+arrived',
        re.IGNORECASE | re.MULTILINE
    )

    def detect_mob_departures(self, text):
        """Return a list of lowercase mob names that left the room in this text."""
        return [m.group(1).strip().lower() for m in self.MOB_DEPARTURE_RE.finditer(text)]

    def detect_mob_arrivals(self, text):
        """Return a list of lowercase mob names that arrived in this text."""
        return [m.group(1).strip().lower() for m in self.MOB_ARRIVAL_RE.finditer(text)]

    KILL_TARGET_MISSING_RE = re.compile(
        r"they don.t seem to be here|"
        r"i see no \S+ here|"
        r"your victim is not here|"
        r"that person is not here",
        re.IGNORECASE
    )

    def detect_kill_target_missing(self, text):
        """Return True if the MUD couldn't find the kill target by that name."""
        return bool(self.KILL_TARGET_MISSING_RE.search(text))

    MURDER_NEEDED_RE = re.compile(
        r"use 'murder' to hit another player",
        re.IGNORECASE
    )

    def detect_murder_needed(self, text):
        """Return True if the target is a player (MUD rejected kill with 'use murder')."""
        return bool(self.MURDER_NEEDED_RE.search(text))

    # ------------------------------------------------------------------
    # Death detection
    # ------------------------------------------------------------------

    DEATH_RE = re.compile(
        r'(?:you are dead|you have died|you died|you fall unconscious|'
        r'you have been (?:killed|slain)|you lose consciousness|'
        r'you wake up (?:in|at)|your vision fades)',
        re.IGNORECASE
    )

    def detect_death(self, text):
        """Return True if the text indicates the character has died."""
        return bool(self.DEATH_RE.search(text))

    # ------------------------------------------------------------------
    # Color-based room block parsing
    # ------------------------------------------------------------------
    #
    # Requires a calibrated mud_structure dict mapping role names to hex
    # color strings, as produced by the Room Colors calibration dialog:
    #   {
    #     "room_title":  "#hex",   # required
    #     "description": "#hex",   # optional — inferred positionally if absent
    #     "objects":     "#hex",   # optional — inferred positionally if absent
    #     "mobs":        "#hex",   # optional — inferred positionally if absent
    #     "exits":       "#hex",   # optional — detected by [bracket] pattern
    #   }
    #
    # Positional inference rules (applied to unassigned-color text):
    #   - Text before the [Exits:] bracket → description
    #   - Text after the [Exits:] bracket  → objects (Phase 1C will separate mobs)
    #
    # 'segments' is the list of (text, color) tuples from parse_ansi_text().

    def parse_room_block(self, segments, mud_structure):
        """
        Parse room structure from ANSI color segments using calibrated color
        assignments.

        Returns a dict:
            {
                "name":                   str,
                "description":            str,
                "normalized_description": str,  # whitespace-normalised, for hashing
                "exits":                  str,
                "objects":                [str], # lines assigned to objects color
                "mob_lines":              [str], # lines assigned to mobs color
            }
        Returns None if no room title segment is found.
        """
        # Build reverse map: lowercase hex → role
        color_to_role = {v.lower(): k for k, v in mud_structure.items() if v}

        title_parts = []
        desc_parts = []
        object_lines = []
        mob_lines = []
        exits = ""
        past_exits = False

        for text, color in segments:
            stripped = text.strip()
            if not stripped:
                continue

            # Exits bracket [N S E W] — detect by content regardless of color.
            # This must come before the color check because exits and title share
            # the same color on this MUD.
            if not past_exits and '[' in stripped and ']' in stripped:
                exits = stripped
                past_exits = True
                continue

            role = color_to_role.get(color.lower())
            if role == 'room_title':
                title_parts.append(stripped)
            elif role == 'description':
                desc_parts.append(stripped)
                past_exits = False  # reset if explicit description color seen
            elif role == 'objects':
                object_lines.append(stripped)
            elif role == 'mobs':
                mob_lines.append(stripped)
            elif role == 'exits':
                exits = stripped
                past_exits = True
            else:
                # No color role assigned — use positional inference.
                # Description is default-colored on many MUDs; text before exits
                # is description, text after exits is objects/mobs.
                if not past_exits:
                    if title_parts:
                        desc_parts.append(stripped)
                    # else: pre-title noise, ignore
                else:
                    object_lines.append(stripped)

        if not title_parts:
            return None

        name = ' '.join(title_parts)
        description = ' '.join(desc_parts)
        return {
            'name': name,
            'description': description,
            'normalized_description': ' '.join(description.split()),
            'exits': exits,
            'objects': object_lines,
            'mob_lines': mob_lines,
        }

    # ------------------------------------------------------------------
    # Mob / NPC detection
    # ------------------------------------------------------------------

    # Words that commonly appear capitalized but are NOT mob names
    MOB_STOP_WORDS = {
        'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to',
        'of', 'for', 'with', 'by', 'from', 'is', 'are', 'was', 'were',
        'it', 'its', 'this', 'that', 'these', 'those', 'here', 'there',
        'north', 'south', 'east', 'west', 'up', 'down',
        'you', 'your', 'he', 'she', 'they', 'his', 'her', 'their',
        'exits', 'obvious', 'door', 'gate',
    }

    # Last words that mark a place/building name, not an NPC name.
    # If a captured noun phrase ends with one of these, it's a location, not a mob.
    MOB_PLACE_SUFFIXES = {
        'inn', 'tavern', 'bar', 'pub', 'hall', 'office', 'shop', 'store',
        'market', 'guild', 'bank', 'stable', 'stables', 'temple', 'church',
        'shrine', 'tower', 'keep', 'castle', 'palace', 'manor', 'estate',
        'road', 'street', 'alley', 'lane', 'path', 'bridge', 'gate', 'arch',
        'square', 'plaza', 'courtyard', 'chamber', 'room', 'corridor',
        'passage', 'tunnel', 'cave', 'cavern', 'grotto', 'clearing',
        'forest', 'swamp', 'marsh', 'desert', 'plains', 'mountains',
        'staircase', 'stairs', 'entrance', 'exit', 'reception', 'lobby',
        'post', 'fort', 'fortress', 'garrison', 'barracks', 'docks', 'dock',
        'harbor', 'harbour', 'port', 'wharf', 'pier', 'crossing',
    }

    # Pattern: a/an/the + optional adjective(s) + capitalized noun
    MOB_PATTERN = re.compile(
        r'\b(?:a|an|the|[Aa]n?)\s+'          # article
        r'(?:[a-z]+\s+){0,2}'                 # optional adjectives
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)',  # capitalized noun phrase
    )

    # Also match lines like "A Goblin is here."
    MOB_IS_HERE_RE = re.compile(
        r'([A-Z][a-zA-Z\s]+?)\s+(?:is|are)\s+here',
        re.IGNORECASE
    )

    # For color-calibrated mob lines — strip article, capture name before is/are verb
    _MOB_LINE_RE = re.compile(
        r'^(?:(?:a|an|the)\s+)?(.+?)\s+(?:is|are)\s+'
        r'(?:here|standing|sitting|lying|lurking|hovering|waiting|guarding|watching)',
        re.IGNORECASE
    )
    # Fallback: anything before "is/are" if no trailing verb matched
    _MOB_LINE_BARE_RE = re.compile(
        r'^(?:(?:a|an|the)\s+)?(.+?)\s+(?:is|are)\b',
        re.IGNORECASE
    )

    def _is_place_name(self, name):
        """Return True if the last word of name is a known place-type suffix."""
        last_word = name.rsplit(None, 1)[-1].lower()
        return last_word in self.MOB_PLACE_SUFFIXES

    def detect_mobs_in_lines(self, mob_lines):
        """Extract mob names from color-calibrated mob lines.

        Each line is guaranteed to be a mob/PC presence line (color-confirmed).
        Handles lowercase mob names (e.g. 'A beastly fido is here.') that the
        general detect_mobs() regex misses because it requires a capital letter.
        """
        found = []
        for line in mob_lines:
            line = line.strip()
            if not line:
                continue
            m = self._MOB_LINE_RE.match(line) or self._MOB_LINE_BARE_RE.match(line)
            if m:
                name = m.group(1).strip()
                if name.lower() not in self.MOB_STOP_WORDS and len(name) > 2:
                    found.append(name)
        return found

    def detect_mobs(self, description):
        """
        Extract likely mob/NPC names from a room description.

        Returns a list of name strings. May include false positives for
        location names etc., but errs toward inclusion.
        """
        found = set()
        for m in self.MOB_PATTERN.finditer(description):
            name = m.group(1).strip()
            if (name.lower() not in self.MOB_STOP_WORDS
                    and len(name) > 2
                    and not self._is_place_name(name)):
                found.add(name)
        for m in self.MOB_IS_HERE_RE.finditer(description):
            name = m.group(1).strip()
            if (name.lower() not in self.MOB_STOP_WORDS
                    and len(name) > 2
                    and not self._is_place_name(name)):
                found.add(name)
        return sorted(found)

    # ------------------------------------------------------------------
    # Inventory / equipment parsing
    # ------------------------------------------------------------------

    # Keywords that indicate an item is a weapon (use 'wield')
    _WIELD_RE = re.compile(
        r'\b(sword|dagger|mace|axe|staff|club|blade|knife|spear|lance|'
        r'bow|crossbow|hammer|flail|whip|trident|rapier|scimitar|'
        r'quarterstaff|glaive|halberd|pike|falchion|broadsword|'
        r'longsword|shortsword|claymore|cutlass|saber|sabre|stiletto|'
        r'dirk|cleaver|cudgel|morningstar|mattock)\b',
        re.IGNORECASE
    )
    # Keywords that indicate a held item (use 'hold')
    _HOLD_RE = re.compile(
        r'\b(torch|lantern|light|orb|wand|rod|talisman|idol|token|'
        r'sceptre|scepter|scroll|book|candle|crystal ball|gem)\b',
        re.IGNORECASE
    )

    def classify_equip_command(self, item_name):
        """
        Return the MUD command to equip this item: 'wield', 'hold', or 'wear'.
        Returns None for items that probably shouldn't be equipped (food, coins, containers).
        """
        name = item_name.lower()
        # Skip obvious non-equipment
        if re.search(r'\b(bread|ration|meat|food|coin|gold|pouch|bag|pack|'
                     r'canteen|flask|bottle|potion|scroll of)\b', name):
            return None
        if self._WIELD_RE.search(name):
            return 'wield'
        if self._HOLD_RE.search(name):
            return 'hold'
        return 'wear'

    def parse_inventory(self, text):
        """
        Parse 'inventory' command output.
        Returns a list of item name strings, or None if text is not inventory output.
        """
        lines = text.splitlines()
        in_inv = False
        items = []
        for line in lines:
            stripped = line.strip()
            if re.match(r'you are carrying', stripped, re.IGNORECASE):
                in_inv = True
                continue
            if re.match(r'you are not carrying anything', stripped, re.IGNORECASE):
                return []
            if not in_inv:
                continue
            if not stripped:
                break
            # Skip lines that look like equipment slots or prompts
            if stripped.startswith('<') or re.match(r'\d+[HhMmVv]', stripped):
                break
            items.append(stripped)
        return items if in_inv else None

    def parse_equipment(self, text):
        """
        Parse 'equipment' command output.
        Returns dict of {slot: item_name} or None if text is not equipment output.
        e.g. {'wielded': 'a broad sword', 'worn on body': 'some leather armor'}
        """
        lines = text.splitlines()
        in_eq = False
        slots = {}
        slot_re = re.compile(r'<([^>]+)>\s+(.*)', re.IGNORECASE)
        for line in lines:
            stripped = line.strip()
            if re.match(r'you are using', stripped, re.IGNORECASE):
                in_eq = True
                continue
            if not in_eq:
                continue
            if not stripped:
                break
            m = slot_re.match(stripped)
            if m:
                slot = m.group(1).strip().lower()
                item = m.group(2).strip()
                if item.lower() != '(nothing)':
                    slots[slot] = item
            elif re.match(r'\d+[HhMmVv]', stripped):
                break
        return slots if in_eq else None

    # ------------------------------------------------------------------
    # Item detection
    # ------------------------------------------------------------------

    ITEM_PATTERN = re.compile(
        r'\b(?:a|an|the|[Aa]n?)\s+'
        r'(?:[a-z]+\s+){0,2}'
        r'([a-z][a-z\s\-\']+?)'
        r'\s+(?:lies?|sits?|rests?|is)\s+(?:here|on the ground|nearby)',
        re.IGNORECASE
    )

    def detect_items(self, description):
        """
        Extract item names from a room description.

        Returns a list of item name strings.
        """
        found = set()
        for m in self.ITEM_PATTERN.finditer(description):
            name = m.group(1).strip()
            if len(name) > 2:
                found.add(name)
        return sorted(found)

    # Ground pickup detection — items worth collecting automatically.
    #
    # Gold: "A small pile of gold coins lies here.",  "23 gold coins are here."
    GOLD_GROUND_RE = re.compile(
        r'(?:'
        r'(?:\d+|a|an|some|pile\s+of|small\s+pile\s+of)\s+gold\s+coins?\b|'
        r'\bgold\s+coins?\b.{0,50}(?:here|ground|floor)|'
        r'\bpile\s+of\s+gold\b'
        r')',
        re.IGNORECASE
    )

    # Food items visibly lying on the ground (not in a shop listing, not carried).
    _FOOD_WORDS = '|'.join([
        'bread', 'ration', 'meat', 'jerky', 'apple', 'cheese',
        'biscuit', 'hardtack', 'pie', 'loaf', 'berry', 'berries',
        'fruit', 'fish', 'mushroom', 'soup', 'waybread', 'tack',
        'snack', 'cake', 'cracker', 'grain', 'egg',
    ])
    FOOD_GROUND_RE = re.compile(
        r'\b(' + _FOOD_WORDS + r')\b.{0,60}(?:here|lies?|sits?|rests?|ground|floor)',
        re.IGNORECASE
    )

    def detect_ground_items(self, text):
        """
        Return a list of (get_command, label) pairs for useful items visible
        on the ground that the agent should pick up.

        Checks for gold coins and food items.  Does not detect weapons, armour,
        or other items to avoid picking up something dangerous or cursed.
        Excludes food with negative adjectives (dubious, rotten, poisoned, etc).
        """
        items = []
        if self.GOLD_GROUND_RE.search(text):
            items.append(('get coins', 'gold coins'))
        seen_food = set()
        for m in self.FOOD_GROUND_RE.finditer(text):
            word = m.group(1).lower()
            # Get context around the match to check for negative adjectives
            start = max(0, m.start() - 50)
            end = min(len(text), m.end() + 10)
            context = text[start:end]
            # Skip if dubious/rotten/etc appears before the food word
            if re.search(r'\b(?:dubious|rotten|moldy|mouldy|spoiled|spoilt|poisoned|tainted|contaminated|bad)\b',
                        context, re.IGNORECASE):
                continue
            if word not in seen_food:
                seen_food.add(word)
                items.append((f'get {word}', word))
        return items

    # ------------------------------------------------------------------
    # Darkness detection
    # ------------------------------------------------------------------

    DARKNESS_RE = re.compile(
        r'(?:it is pitch[- ]black|too dark to see|you cannot see anything|'
        r'you can\'t see anything|darkness surrounds you|'
        r'it\'s too dark|pitch black darkness|'
        r'you are blind|you cannot see|light would help)',
        re.IGNORECASE
    )

    def detect_darkness(self, text):
        """Return True if the text indicates the area is too dark to navigate."""
        return bool(self.DARKNESS_RE.search(text))

    # ------------------------------------------------------------------
    # Incapacitated / Death detection
    # ------------------------------------------------------------------

    INCAPACITATED_RE = re.compile(
        r'(?:you are incapacitated|you are mortally wounded|'
        r'you lie on the ground suffering|you will slowly die|'
        r'you will die soon)',
        re.IGNORECASE
    )

    def detect_incapacitated(self, text):
        """
        Detect if the player is incapacitated (HP <= 0).
        Returns True if incapacitated messages are found.
        """
        return bool(self.INCAPACITATED_RE.search(text))

    # ------------------------------------------------------------------
    # Hunger detection
    # ------------------------------------------------------------------

    # Returns: None | "hungry" | "starving"
    STARVING_RE = re.compile(
        r'(?:you are starving|you are famished|you are dying of hunger|'
        r'your stomach is growling loudly|you feel very hungry|'
        r'you are extremely hungry)',
        re.IGNORECASE
    )
    HUNGRY_RE = re.compile(
        r'(?:you are hungry|you feel hungry|your stomach growls|'
        r'you could eat|you need food|you are getting hungry)',
        re.IGNORECASE
    )

    def detect_hunger(self, text):
        """
        Return hunger level detected in text.
        Returns 'starving', 'hungry', or None.
        """
        if self.STARVING_RE.search(text):
            return 'starving'
        if self.HUNGRY_RE.search(text):
            return 'hungry'
        return None

    # ------------------------------------------------------------------
    # Thirst detection
    # ------------------------------------------------------------------

    PARCHED_RE = re.compile(
        r'(?:you are parched|you are dying of thirst|you are extremely thirsty|'
        r'you are very thirsty|your throat is parched|your mouth is dry)',
        re.IGNORECASE
    )
    THIRSTY_RE = re.compile(
        r'(?:you are thirsty|you feel thirsty|you could use a drink|'
        r'you need water|you are getting thirsty)',
        re.IGNORECASE
    )

    def detect_thirst(self, text):
        """
        Return thirst level detected in text.
        Returns 'parched', 'thirsty', or None.
        """
        if self.PARCHED_RE.search(text):
            return 'parched'
        if self.THIRSTY_RE.search(text):
            return 'thirsty'
        return None

    # ------------------------------------------------------------------
    # Water source detection
    # ------------------------------------------------------------------

    # Each tuple is (regex, drink_command)
    WATER_SOURCES = [
        (re.compile(r'\bfountain\b', re.IGNORECASE),   'drink fountain'),
        (re.compile(r'(?:(?:a|an|the|stone|old|deep|wooden|draw|wishing|coin)\s+well\b|\bwell\s+(?:here|is here|stands?|bubbles?|gurgles?|sits?|flows?|trickles?))',
                    re.IGNORECASE),   'drink well'),
        (re.compile(r'\bstream\b',   re.IGNORECASE),   'drink stream'),
        (re.compile(r'\briver\b',    re.IGNORECASE),   'drink river'),
        (re.compile(r'\bpool\b',     re.IGNORECASE),   'drink pool'),
        (re.compile(r'\bspring\b',   re.IGNORECASE),   'drink spring'),
        (re.compile(r'\bpond\b',     re.IGNORECASE),   'drink pond'),
        (re.compile(r'\bcreek\b',    re.IGNORECASE),   'drink creek'),
        (re.compile(r'\blake\b',     re.IGNORECASE),   'drink lake'),
    ]

    def detect_water_source(self, text):
        """
        Return the drink command if a natural water source is mentioned, else None.
        e.g. "A fountain bubbles in the center." -> "drink fountain"
        """
        for pattern, command in self.WATER_SOURCES:
            if pattern.search(text):
                return command
        return None

    # ------------------------------------------------------------------
    # Food / shop detection
    # ------------------------------------------------------------------

    SHOP_RE = re.compile(
        r'(?:'
        # Room name keywords — short nouns that name a shop type
        # NOTE: "inn"/"tavern"/"alehouse" are excluded because they frequently
        # appear in descriptions as *references* to neighbouring rooms
        # (e.g. "the Grunting Boar Inn is to the east") and create false positives.
        r'\b(?:bakery|grocery|grocer(?:y|\'s)?|general store|trading post|'
        r'provisions?|supply store|commissary|food stall|food store|'
        r'pantry|larder)\b|'
        # NPC / mob names that sell food
        r'\b(?:shopkeeper|merchant|grocer|baker|innkeeper|vendor|'
        r'provisioner|trader|peddler|hawker|purveyor|monger|sutler)\b|'
        # Possessive room names: "baker's", "grocer's", "merchant's"
        r'\b(?:baker|grocer|merchant|vendor|trader)\'s\b|'
        # Signs and notices in descriptions
        r'(?:a sign|sign reads|notice reads|you see a sign).{0,60}'
        r'(?:food|bread|ration|meal|grocer|provision|supply|supplies|for sale)|'
        # "sells", "buy", "for sale" near food words
        r'(?:sells?|selling|buy|for sale)\b.{0,40}'
        r'\b(?:food|bread|ration|meal|provision|supplies)\b'
        r')',
        re.IGNORECASE
    )

    FOOD_ITEM_RE = re.compile(
        r'\b(?:bread|ration|meat|jerky|apple|cheese|biscuit|hardtack|'
        r'pie|loaf|meal|food|berry|fruit|vegetable|fish|mushroom|soup|'
        r'waybread|tack|provision|snack|cake|cracker|grain|corn|egg)\b',
        re.IGNORECASE
    )

    # Multiple shop list formats tried in order:
    #   "  1)  a loaf of bread         5 gold"   (numbered with paren)
    #   "  1.  a loaf of bread         5 gold"   (numbered with period)
    #   "  [1] a loaf of bread         5 gold"   (bracketed)
    #   "  a loaf of bread ......... 5 gold"     (dot-filled)
    #   "  a loaf of bread (5 gold)"             (parenthesised price)
    #   "  a loaf of bread   - 5 gold"           (dash-separated)
    _SHOP_PATTERNS = [
        re.compile(r'^\s*\d+[.)]\s+(.+?)\s{2,}([\d,]+)', re.MULTILINE),
        re.compile(r'^\s*\[\s*\d+\]\s+(.+?)\s{2,}([\d,]+)', re.MULTILINE),
        re.compile(r'^(.+?)[.]{2,}\s*([\d,]+)', re.MULTILINE),
        re.compile(r'^(.+?)\s*\(\s*([\d,]+)\s*(?:gold|gp|coins?)?\s*\)', re.MULTILINE | re.IGNORECASE),
        re.compile(r'^(.+?)\s+-+\s*([\d,]+)', re.MULTILINE),
    ]

    def is_food_item(self, item_name):
        """
        Return True if the item name appears to be food.
        Checks against common food keywords (meat, bread, ration, etc.).
        """
        if not item_name:
            return False
        return bool(self.FOOD_ITEM_RE.search(item_name))

    def get_item_keyword(self, item_name):
        """
        Extract the keyword from an item name for use in MUD commands.
        For food items, extracts the actual food word (e.g., 'meat' from 'a piece of meat').
        For other items, strips leading articles.
        
        Examples:
            "a piece of meat" -> "meat"
            "loaf of bread" -> "bread"
            "an apple" -> "apple"
            "the broad sword" -> "broad sword"
        """
        if not item_name:
            return item_name
        
        # For food items, try to extract the actual food keyword
        if self.is_food_item(item_name):
            # Find all food keywords in the item name
            matches = list(self.FOOD_ITEM_RE.finditer(item_name))
            if matches:
                # Return the last matched food keyword (most specific)
                # e.g., "loaf of bread" has both "loaf" and "bread", prefer "bread"
                return matches[-1].group(0).lower()
        
        # For non-food items, just strip leading articles
        stripped = re.sub(r'^\s*(?:a|an|the)\s+', '', item_name, flags=re.IGNORECASE)
        return stripped.strip()

    def detect_food_shop(self, text):
        """Return True if the room description suggests a food vendor is present."""
        return bool(self.SHOP_RE.search(text))

    def parse_shop_list(self, text):
        """
        Parse a shop inventory listing — tries several common MUD formats.
        Returns a list of (item_name, price_or_None) tuples for food items found.
        """
        found = {}  # item_name -> price (deduplicate)
        for pattern in self._SHOP_PATTERNS:
            for m in pattern.finditer(text):
                item = m.group(1).strip(' \t-_')
                if not item or len(item) > 60:
                    continue
                if self.FOOD_ITEM_RE.search(item):
                    try:
                        price = int(m.group(2).replace(',', ''))
                    except (IndexError, ValueError):
                        price = None
                    if item not in found:
                        found[item] = price
        return [(item, price) for item, price in found.items()]

    # ------------------------------------------------------------------
    # Gold / currency
    # ------------------------------------------------------------------

    GOLD_CARRIED_RE = re.compile(
        r'(?:you have|you are carrying|you carry|gold:\s*)'
        r'\s*(?P<gold>[\d,]+)\s*(?:gold|coins?|gp)\b',
        re.IGNORECASE
    )
    GOLD_RECEIVED_RE = re.compile(
        r'(?:you (?:get|receive|find|pick up|loot)|'
        r'you are rewarded with)\s+(?P<gold>[\d,]+)\s*(?:gold|coins?|gp)\b',
        re.IGNORECASE
    )
    # "You gain 50 gold." or "50 gold coins."  (drop/loot line)
    GOLD_DROP_RE = re.compile(
        r'\b(?P<gold>[\d,]+)\s*(?:gold|coins?|gp)\b',
        re.IGNORECASE
    )
    SHOP_PRICE_RE = re.compile(
        r'^\s*\d+\)\s+.+?\s{2,}(?P<price>[\d,]+)',
        re.MULTILINE
    )

    def detect_gold_carried(self, text):
        """Return total gold currently carried, or None if not mentioned."""
        m = self.GOLD_CARRIED_RE.search(text)
        if m:
            try:
                return int(m.group('gold').replace(',', ''))
            except ValueError:
                pass
        return None

    def detect_gold_received(self, text):
        """Return gold received in this text block, or None."""
        m = self.GOLD_RECEIVED_RE.search(text)
        if m:
            try:
                return int(m.group('gold').replace(',', ''))
            except ValueError:
                pass
        return None

    def parse_item_price(self, text, item_name):
        """
        Given a shop listing, return the price of item_name, or None.
        Matches the first line whose text contains item_name.
        """
        for line in text.splitlines():
            if item_name.lower() in line.lower():
                m = self.SHOP_PRICE_RE.match(line)
                if m:
                    try:
                        return int(m.group('price').replace(',', ''))
                    except ValueError:
                        pass
        return None

    # ------------------------------------------------------------------
    # Light source / time of day
    # ------------------------------------------------------------------

    LIGHT_GAINED_RE = re.compile(
        r'(?:you light|the torch flickers to life|a warm glow|'
        r'you hold up|it glows brightly|dawn breaks|the sun rises|'
        r'day breaks)',
        re.IGNORECASE
    )
    LIGHT_LOST_RE = re.compile(
        r'(?:your torch goes out|the light fades|darkness falls|'
        r'the sun sets|night falls|it grows dark)',
        re.IGNORECASE
    )

    def detect_light_gained(self, text):
        """Return True if text suggests the character now has light."""
        return bool(self.LIGHT_GAINED_RE.search(text))

    def detect_light_lost(self, text):
        """Return True if text suggests light has been lost (torch out, nightfall)."""
        return bool(self.LIGHT_LOST_RE.search(text))

    # ------------------------------------------------------------------
    # Otto helper-player parsing
    # ------------------------------------------------------------------

    # Otto's tell back: "Otto tells you 'I can heal, summon'"
    # or "Otto tells you, 'Tell me heal, sanctuary, bless, armor or summon'"
    _OTTO_TELL_RE = re.compile(
        r"otto\s+tells?\s+you[,\s]+'([^']+)'",
        re.IGNORECASE
    )
    # ------------------------------------------------------------------
    # WHO list parsing
    # ------------------------------------------------------------------
    #
    # Format: "[ 1 Wa] Ollyama the Swordpupil"
    #         "[34 Cl] Otto the Automatic Cleric. Tell me HELP"

    _WHO_LINE_RE = re.compile(
        r'^\[\s*(?P<level>\d+)\s+(?P<class>[A-Za-z]{2})\]\s+(?P<name>\S+)',
        re.MULTILINE
    )

    def parse_who(self, text, char_name=None):
        """
        Parse the who list.

        Returns a list of dicts: [{"level": int, "class": str, "name": str}, ...]

        If char_name is provided, also returns the matching entry as
        {"self": {...}} so the caller can update char_level and char_class.
        """
        players = []
        self_entry = None
        for m in self._WHO_LINE_RE.finditer(text):
            entry = {
                "level": int(m.group("level")),
                "class": m.group("class").upper(),
                "name": m.group("name"),
            }
            players.append(entry)
            if char_name and entry["name"].lower() == char_name.lower():
                self_entry = entry
        return players, self_entry

    # Otto visible in room: "Otto is here.", "Otto stands here.", etc.
    _OTTO_PRESENT_RE = re.compile(r'\botto\b', re.IGNORECASE)

    def parse_otto_tell(self, text):
        """
        Extract the content of a tell from Otto.
        Returns the inner message string, or None if not an Otto tell.
        """
        m = self._OTTO_TELL_RE.search(text)
        return m.group(1).strip() if m else None

    def parse_otto_capabilities(self, tell_text):
        """
        Given the text of Otto's help response, return a list of capability
        names (lowercase) that Otto advertised.
        Looks for word tokens that match known Otto service names.
        """
        known = {
            'summon', 'heal',
            'mana', 'restore', 'rescue', 'protect', 'buff', 'haste',
            'bless', 'armor', 'sanctuary', 'identify', 'uncurse',
            'cure', 'remove', 'light', 'invis', 'invisible',
        }
        found = []
        for word in re.findall(r'[a-z]+', tell_text.lower()):
            if word in known and word not in found:
                found.append(word)
        return found

    def detect_otto_present(self, room_text):
        """Return True if Otto appears to be in the room description."""
        return bool(self._OTTO_PRESENT_RE.search(room_text))

    _OTTO_SUMMON_SUCCESS_RE = re.compile(
        r'otto\s+has\s+summoned\s+you',
        re.IGNORECASE
    )

    def detect_otto_summon_success(self, text):
        """Return True if Otto successfully summoned the player."""
        return bool(self._OTTO_SUMMON_SUCCESS_RE.search(text))

    # ------------------------------------------------------------------
    # Unrecognized message tracking
    # ------------------------------------------------------------------

    # Lines matching this are considered "known" and won't be tracked as
    # unrecognized.  The pattern is intentionally broad — false negatives
    # (recognized messages accidentally flagged) are fine; false positives
    # (truly unknown messages silently dropped) are what we want to avoid.
    _RECOGNIZED_RE = re.compile(
        r"""
        # MUD prompt (HP/MP/MV line)
        \d+\s*[Hh]\w*\s+\d+\s*[Mm]\w*
        # Room exit line
        | \[\s*Exits?:
        # Common movement / positioning
        | \b(?:you (?:flee|leave|enter|arrive|go|move|walk|run|stand|sit|rest|sleep|wake)
             |you (?:are (?:already|standing|sitting|resting|sleeping|riding))
             |there is no (?:exit|way)|alas|you cannot go that way
             |you bump into|you are too|you can't go)
        # Combat messages
        | \b(?:you (?:miss|hit|dodge|parry|block|attack|kick|bash|stab|slice|pierce|crush|pound)
             |you flee head over heels
             |you are (?:stunned|paralyzed|blinded|poisoned|cursed)
             |you feel (?:better|worse|stronger|weaker|lighter|heavier)
             |your (?:wounds|injuries|body)
             |\w+ misses you|\w+ (?:hits?|slices?|pierces?|crushes?|bashes?|kicks?) you)
        # Death / resurrection
        | \b(?:you (?:die|are dead|have died|wake up|fall unconscious|lose consciousness)
             |you have been (?:killed|slain))
        # Hunger / thirst
        | \byou (?:are (?:hungry|starving|thirsty|parched)|feel (?:hungry|thirsty))
        | \byou (?:eat|drink|consume|finish eating|quench)
        # Gold / shop
        | \b(?:you (?:buy|sell|get|drop|give|receive|pick up|put|remove|wear|wield|hold|grab)
             |\w+ gives? you|\w+ (?:pays?|sells?|buys?)
             |you now have|balance:)
        # Tells / communication
        | \b(?:you tell|\w+ tells? you|\w+ says?|\w+ shouts?|\w+ yells?
             |\w+ whispers?|someone (?:tells?|says?))
        # Score / who / time output
        | \b(?:you are \d+ years old|you have scored|you need \d+ exp
             |this ranks you|you have been playing
             |players\s*$|-------\s*$|\[\s*\d+\s+[A-Z][a-z]\])
        # Shop interaction
        | \b(?:the shopkeeper|a small sign|you can't afford|you don't have enough
             |item not available|out of stock)
        # Buff / spell messages
        | \b(?:you feel (?:protected|blessed|armored|sanctified|holy|invincible)
             |a white aura|a (?:blue|red|green|yellow|white|black) glow
             |you (?:glow|shimmer|radiate))
        # Welcome / login / system
        | \b(?:welcome to|goodbye|press return|make your choice|by what name
             |password:|enter the game)
        # Otto
        | \botto\b
        # Common game messages
        | \b(?:you (?:are summonable|are (?:not )?(?:affected|immune|resistant))
             |your armor class|your alignment
             |it is \d+ o'clock|the \d+(?:st|nd|rd|th) day)
        # Exits / dark / light
        | \b(?:it is pitch black|you see nothing|a (?:torch|lantern|light))
        # Empty or whitespace-only
        | ^\s*$
        """,
        re.IGNORECASE | re.VERBOSE
    )

    def looks_unrecognized(self, line):
        """
        Return True if *line* is a single non-trivial MUD line that doesn't
        match any pattern we already handle.  Call once per stripped line.
        """
        line = line.strip()
        if not line or len(line) < 8:
            return False
        # Skip room-description indented lines and title-case room names
        if line.startswith('   '):
            return False
        return not bool(self._RECOGNIZED_RE.search(line))
