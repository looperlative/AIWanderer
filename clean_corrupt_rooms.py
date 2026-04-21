#!/usr/bin/env python3
# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""
Clean Corrupt Room Entries
Removes malformed room data (spell/buff output, character stat output) from
MUD client profiles without touching legitimate room entries.
"""

import json
import os
import re
import sys
from datetime import datetime

SPELL_KEYWORDS = re.compile(r'\b(armor|bless|sanctuary)\b', re.IGNORECASE)
STAT_OUTPUT    = re.compile(
    r'\b(HITROLL|SAVING_SPELL|SAVING_PARA|SAVING_ROD|SAVING_PETRI|SAVING_BREATH)\b'
)
ABILITY_SCORES = re.compile(r'\d+/?\d*(?:\s+\d+){4,}')
CORRUPT_EXITS  = re.compile(r'Int:|Str:|Dex:|Con:|Wis:|Cha:')
MUD_PROMPT     = re.compile(r'^\d+[Hh]\w*\s+\d+[Mm]\w*.*[>$]', re.IGNORECASE)


def _link_dest(val):
    """Return (dest_hash, assumed) from a room_link value (str or dict)."""
    if isinstance(val, dict):
        return val.get('dest'), val.get('assumed', False)
    return val, False  # legacy plain-string → treat as confirmed


def is_corrupt_room(room):
    name  = room.get('name', '')
    desc  = room.get('description', '')
    exits = room.get('exits', '')

    # Entire name is just spell/buff keywords
    words = name.split()
    if words and all(w.lower() in ('armor', 'bless', 'sanctuary') for w in words):
        return True, 'spell-buff name'

    # Game stat output in description or name
    if STAT_OUTPUT.search(desc) or STAT_OUTPUT.search(name):
        return True, 'stat output in description'

    # Ability score sequence in name (e.g. "18/0 13 13 16 14 9")
    if ABILITY_SCORES.search(name):
        return True, 'ability scores in name'

    # Stat labels leaked into exits field (e.g. "]  Int: [")
    if CORRUPT_EXITS.search(exits):
        return True, 'stat labels in exits field'

    return False, None


def clean_objects(room):
    """Remove MUD prompt lines from a room's objects list. Returns True if changed."""
    objects = room.get('objects')
    if not objects:
        return False
    cleaned = [o for o in objects if not MUD_PROMPT.match(o)]
    if len(cleaned) != len(objects):
        room['objects'] = cleaned
        return True
    return False


def clean_profile(profile_data):
    rooms      = profile_data.get('rooms', {})
    room_links = profile_data.get('room_links', {})

    corrupt_hashes = set()
    reasons = []
    objects_cleaned = 0
    dangling_removed = 0

    for room_hash, room in rooms.items():
        bad, reason = is_corrupt_room(room)
        if bad:
            corrupt_hashes.add(room_hash)
            reasons.append((room_hash[:12], reason, room.get('name', '')))
        elif clean_objects(room):
            objects_cleaned += 1

    # Remove corrupt rooms
    for h in corrupt_hashes:
        del rooms[h]

    # Valid room set after corruption removal
    valid_rooms = set(rooms.keys())

    # Remove room_links whose source or any destination is not in valid_rooms
    for src in list(room_links.keys()):
        if src not in valid_rooms:
            del room_links[src]
            dangling_removed += 1
            continue
        links = room_links[src]
        for direction in list(links.keys()):
            dest, _ = _link_dest(links[direction])
            if dest not in valid_rooms:
                del links[direction]
                dangling_removed += 1
        if not links:
            del room_links[src]

    # Clear named room-hash references that no longer exist
    ai_cfg = profile_data.get('ai_config', {})
    for key in ('entry_room',):
        h = profile_data.get(key)
        if h and h not in valid_rooms:
            del profile_data[key]
            dangling_removed += 1
    for key in ('fountain_room', 'food_store_room'):
        h = ai_cfg.get(key)
        if h and h not in valid_rooms:
            del ai_cfg[key]
            dangling_removed += 1

    if not corrupt_hashes and not objects_cleaned and not dangling_removed:
        return 0, [], 0, 0

    return len(corrupt_hashes), reasons, objects_cleaned, dangling_removed


def main():
    profiles_file = os.path.join(os.path.expanduser("~"), ".mud_client_profiles.json")

    if not os.path.exists(profiles_file):
        print(f"Profile file not found: {profiles_file}")
        return

    try:
        with open(profiles_file, 'r') as f:
            profiles = json.load(f)
    except Exception as e:
        print(f"Error loading profiles: {e}")
        return

    # Dry-run scan first
    print("\nScanning for corrupt room entries...\n")
    total_corrupt = 0
    total_prompt_objects = 0
    total_dangling = 0
    scan_results = {}

    for name, data in profiles.items():
        if name.startswith('_'):
            continue
        rooms      = data.get('rooms', {})
        room_links = data.get('room_links', {})
        ai_cfg     = data.get('ai_config', {})
        valid      = set(rooms.keys())
        found = []
        prompt_obj_count = 0
        dangling = 0
        for room_hash, room in rooms.items():
            bad, reason = is_corrupt_room(room)
            if bad:
                found.append((room_hash[:12], reason, room.get('name', '')))
            else:
                objects = room.get('objects') or []
                prompt_obj_count += sum(1 for o in objects if MUD_PROMPT.match(o))
        # Count dangling room_links (approximate — corrupt rooms not yet removed)
        corrupt_set = {rh for rh, _ in [(rh, r) for rh, r in rooms.items()
                                        if is_corrupt_room(r)[0]]}
        surviving = valid - corrupt_set
        for src, links in room_links.items():
            if src not in surviving:
                dangling += 1
            else:
                dangling += sum(1 for dest in links.values() if dest not in surviving)
        for key in ('entry_room',):
            h = data.get(key)
            if h and h not in surviving:
                dangling += 1
        for key in ('fountain_room', 'food_store_room'):
            h = ai_cfg.get(key)
            if h and h not in surviving:
                dangling += 1
        scan_results[name] = (len(rooms), found, prompt_obj_count, dangling)
        total_corrupt += len(found)
        total_prompt_objects += prompt_obj_count
        total_dangling += dangling

    for profile_name, (total, found, prompt_obj_count, dangling) in scan_results.items():
        print(f"  {profile_name}: {total} total rooms, {len(found)} corrupt, "
              f"{prompt_obj_count} rooms with prompt lines in objects, "
              f"{dangling} dangling references")
        for short_hash, reason, rname in found:
            print(f"    [{short_hash}] {reason}: {rname!r}")

    if total_corrupt == 0 and total_prompt_objects == 0 and total_dangling == 0:
        print("\nNo issues found. Nothing to do.")
        return

    print(f"\nTotal corrupt rooms: {total_corrupt}")
    print(f"Total prompt lines in objects fields: {total_prompt_objects}")
    print(f"Total dangling room references: {total_dangling}")

    confirm = input("\nCreate backup and clean corrupt entries? (yes/no): ").strip().lower()
    if confirm != 'yes':
        print("Cancelled. No changes made.")
        return

    # Backup
    backup_file = profiles_file + f".backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    try:
        with open(backup_file, 'w') as f:
            json.dump(profiles, f, indent=2)
        print(f"\n✓ Backup created: {backup_file}")
    except Exception as e:
        print(f"Error creating backup: {e}")
        return

    # Clean
    for name, data in profiles.items():
        if name.startswith('_'):
            continue
        result = clean_profile(data)
        removed, _, obj_cleaned, dangling = result
        if removed or obj_cleaned or dangling:
            remaining = len(data.get('rooms', {}))
            print(f"✓ {name}: removed {removed} corrupt rooms, "
                  f"cleaned prompt objects from {obj_cleaned} rooms, "
                  f"removed {dangling} dangling references "
                  f"({remaining} rooms remain)")

    try:
        with open(profiles_file, 'w') as f:
            json.dump(profiles, f, indent=2)
        print(f"\n✓ Profile file updated: {profiles_file}")
    except Exception as e:
        print(f"\n✗ Error saving profiles: {e}")
        print(f"Your data is safe in the backup: {backup_file}")


if __name__ == "__main__":
    main()
