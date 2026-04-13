#!/usr/bin/env python3
# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""
Export a skill template from a profiles JSON file as human-readable text.

Usage:
    python3 export_skill_template.py [--profiles PATH] [--profile NAME]
                                     [--template NAME] [--output FILE]
                                     [--render-target NAME]

Defaults:
    --profiles      ~/.mud_client_profiles.json
    --profile       first profile that defines skill_templates
    --template      first template in that profile
    --output        stdout

When --render-target is given, placeholders are substituted using that
target's params (useful for reviewing the final prompt the LLM will see).
Otherwise placeholders are shown verbatim as {{name}}.
"""

import argparse
import json
import os
import sys


def pick_profile(profiles, explicit):
    if explicit:
        if explicit not in profiles:
            sys.exit(f"Profile not found: {explicit}")
        return explicit
    for name, prof in profiles.items():
        if isinstance(prof, dict) and prof.get("skill_templates"):
            return name
    sys.exit("No profile with skill_templates found.")


def pick_template(templates, explicit):
    if not templates:
        sys.exit("Profile has no skill_templates.")
    if explicit:
        if explicit not in templates:
            sys.exit(f"Template not found: {explicit}. "
                     f"Available: {', '.join(sorted(templates))}")
        return explicit
    return sorted(templates)[0]


def format_template(name, tmpl, rendered_from=None):
    lines = []
    lines.append("=" * 72)
    lines.append(f"Skill Template: {name}")
    if rendered_from:
        lines.append(f"Rendered with target: {rendered_from}")
    lines.append("=" * 72)
    lines.append("")

    placeholders = tmpl.get("placeholders", [])
    lines.append("Placeholders:")
    if placeholders:
        for ph in placeholders:
            lines.append(f"  - {{{{{ph}}}}}")
    else:
        lines.append("  (none declared)")
    lines.append("")

    watch = tmpl.get("watch_stats", [])
    lines.append("Watched stats: " + (", ".join(watch) if watch else "(none)"))
    lines.append("")

    lines.append("-" * 72)
    lines.append("Instructions")
    lines.append("-" * 72)
    lines.append(tmpl.get("instructions", "").rstrip())
    lines.append("")

    reminders = tmpl.get("reminders", "")
    if reminders:
        lines.append("-" * 72)
        lines.append("Reminders")
        lines.append("-" * 72)
        lines.append(reminders.rstrip())
        lines.append("")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profiles",
                    default=os.path.expanduser("~/.mud_client_profiles.json"))
    ap.add_argument("--profile", default=None)
    ap.add_argument("--template", default=None)
    ap.add_argument("--render-target", default=None,
                    help="Name of a skill_target whose params should be substituted.")
    ap.add_argument("--output", default=None,
                    help="Write to this file instead of stdout.")
    args = ap.parse_args()

    with open(args.profiles) as f:
        profiles = json.load(f)

    profile_name = pick_profile(profiles, args.profile)
    prof = profiles[profile_name]
    templates = prof.get("skill_templates", {})
    template_name = pick_template(templates, args.template)
    tmpl = templates[template_name]

    rendered_from = None
    if args.render_target:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from skill_engine import render_skill
        targets = prof.get("skill_targets", {})
        if args.render_target not in targets:
            sys.exit(f"Target not found: {args.render_target}. "
                     f"Available: {', '.join(sorted(targets))}")
        target = targets[args.render_target]
        if target.get("template") != template_name:
            print(f"Warning: target '{args.render_target}' is bound to template "
                  f"'{target.get('template')}', not '{template_name}'.",
                  file=sys.stderr)
        tmpl = render_skill(tmpl, target.get("params", {}))
        rendered_from = args.render_target

    out = format_template(template_name, tmpl, rendered_from=rendered_from)
    if args.output:
        with open(args.output, "w") as f:
            f.write(out)
    else:
        sys.stdout.write(out)


if __name__ == "__main__":
    main()
