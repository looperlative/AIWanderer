import json
import os
import sys

def _link_dest(val):
    """Helper to extract destination hash from room link value."""
    if isinstance(val, dict):
        return val.get('dest')
    return val

def find_zone_entries(profiles_path):
    if not os.path.exists(profiles_path):
        print(f"Error: File {profiles_path} not found.")
        return

    try:
        with open(profiles_path, 'r') as f:
            profiles = json.load(f)
    except Exception as e:
        print(f"Error loading JSON: {e}")
        return

    # Profiles file can have a top-level structure where keys are profile names
    # or it might just be a single profile if it's a specific config.
    # According to mud_client.py, it's a dict of profiles.
    
    # We need to identify which keys are profiles. 
    # Usually, they are keys that have 'rooms' and 'room_links'.
    # '_settings' is a special key.
    
    for profile_name, profile_data in profiles.items():
        if profile_name == '_settings' or not isinstance(profile_data, dict):
            continue
        
        rooms = profile_data.get('rooms', {})
        room_links = profile_data.get('room_links', {})
        
        if not rooms or not room_links:
            continue

        print(f"Processing profile: {profile_name}")
        zone_exits_map = {}

        for from_hash, exits in room_links.items():
            if not isinstance(exits, dict):
                continue
                
            from_room = rooms.get(from_hash, {})
            from_zone = from_room.get('zone', '')

            for direction, target in exits.items():
                dest_hash = _link_dest(target)
                if not dest_hash:
                    continue
                
                dest_room = rooms.get(dest_hash, {})
                dest_zone = dest_room.get('zone', '')

                # Extract room number: check if key is 'vnum:N' or if 'num' is in dict
                dest_num = 'N/A'
                if dest_hash.startswith('vnum:'):
                    dest_num = dest_hash.split(':', 1)[1]
                elif 'num' in dest_room:
                    dest_num = dest_room['num']

                if from_zone != dest_zone:
                    # Use dest_hash as key to eliminate duplicate destinations
                    zone_exits_map[dest_hash] = {
                        'dest_room': dest_room.get('name', dest_hash),
                        'dest_num': dest_num,
                        'dest_zone': dest_zone,
                        'dest_hash': dest_hash
                    }

        zone_exits = list(zone_exits_map.values())

        if zone_exits:
            # Sort by room number (numeric sort)
            def sort_key(entry):
                try:
                    return int(entry['dest_num'])
                except (ValueError, TypeError):
                    return float('inf')

            zone_exits.sort(key=sort_key)

            print(f"{'Destination Room':<50} | {'Room #':<10} | {'To Zone'}")
            print("-" * 80)
            for entry in zone_exits:
                print(f"{entry['dest_room']:<50} | {entry['dest_num']:<10} | {entry['dest_zone']}")
        else:
            print("No cross-zone exits found.")
        print("\n")

if __name__ == "__main__":
    # Default to ~/.mud_client_profiles.json
    default_path = os.path.join(os.path.expanduser("~"), ".mud_client_profiles.json")
    
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        path = default_path
        
    find_zone_entries(path)
