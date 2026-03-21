#!/usr/bin/env python3
# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""
Clear Room Data Script
Removes room mapping data from MUD client profiles for fresh testing.
"""

import json
import os
import sys
from datetime import datetime

def main():
    # Profile file location
    profiles_file = os.path.join(os.path.expanduser("~"), ".mud_client_profiles.json")
    
    if not os.path.exists(profiles_file):
        print(f"Profile file not found: {profiles_file}")
        print("No profiles to clean.")
        return
    
    # Load profiles
    try:
        with open(profiles_file, 'r') as f:
            profiles = json.load(f)
    except Exception as e:
        print(f"Error loading profiles: {e}")
        return
    
    # Create backup
    backup_file = profiles_file + f".backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    try:
        with open(backup_file, 'w') as f:
            json.dump(profiles, f, indent=2)
        print(f"✓ Backup created: {backup_file}")
    except Exception as e:
        print(f"Error creating backup: {e}")
        return
    
    # Display profiles and room counts
    print("\nProfiles found:")
    profile_list = []
    for name, data in profiles.items():
        if not name.startswith('_'):
            room_count = len(data.get('rooms', {}))
            link_count = sum(len(links) for links in data.get('room_links', {}).values())
            profile_list.append(name)
            print(f"  {len(profile_list)}. {name} - {room_count} rooms, {link_count} links")
    
    if not profile_list:
        print("No profiles found.")
        return
    
    # Ask user what to clear
    print("\nOptions:")
    print("  a - Clear ALL room data from ALL profiles")
    print("  s - Select specific profile(s)")
    print("  q - Quit without changes")
    
    choice = input("\nYour choice: ").strip().lower()
    
    if choice == 'q':
        print("Cancelled. No changes made.")
        return
    
    profiles_to_clean = []
    
    if choice == 'a':
        profiles_to_clean = profile_list
    elif choice == 's':
        print("\nEnter profile numbers to clear (comma-separated, e.g., 1,3,5):")
        selection = input("Profile numbers: ").strip()
        try:
            indices = [int(x.strip()) - 1 for x in selection.split(',')]
            profiles_to_clean = [profile_list[i] for i in indices if 0 <= i < len(profile_list)]
        except:
            print("Invalid selection.")
            return
    else:
        print("Invalid choice.")
        return
    
    if not profiles_to_clean:
        print("No profiles selected.")
        return
    
    # Confirm
    print(f"\nWill clear room data from: {', '.join(profiles_to_clean)}")
    confirm = input("Are you sure? (yes/no): ").strip().lower()
    
    if confirm != 'yes':
        print("Cancelled. No changes made.")
        return
    
    # Clear room data
    cleared_count = 0
    for profile_name in profiles_to_clean:
        if profile_name in profiles:
            removed_rooms = len(profiles[profile_name].get('rooms', {}))
            removed_links = sum(len(links) for links in profiles[profile_name].get('room_links', {}).values())
            
            # Clear room-related data
            profiles[profile_name]['rooms'] = {}
            profiles[profile_name]['room_links'] = {}
            
            if 'entry_room' in profiles[profile_name]:
                del profiles[profile_name]['entry_room']
            
            if 'room_color' in profiles[profile_name]:
                del profiles[profile_name]['room_color']
            
            if 'room_tracking_enabled' in profiles[profile_name]:
                del profiles[profile_name]['room_tracking_enabled']
            
            print(f"✓ Cleared {profile_name}: {removed_rooms} rooms, {removed_links} links")
            cleared_count += 1
    
    # Save updated profiles
    try:
        with open(profiles_file, 'w') as f:
            json.dump(profiles, f, indent=2)
        print(f"\n✓ Successfully cleared room data from {cleared_count} profile(s)")
        print(f"✓ Profile file updated: {profiles_file}")
    except Exception as e:
        print(f"\n✗ Error saving profiles: {e}")
        print(f"Your data is safe in the backup: {backup_file}")
        return

if __name__ == "__main__":
    main()
