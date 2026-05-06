# Copyright (c) 2026 Bob Amstadt
# SPDX-License-Identifier: MIT
"""Dialog windows for the AIWanderer MUD client."""

import tkinter as tk
from tkinter import messagebox, ttk


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

    # Keys that the "Save to Host" button writes to the local override file.
    _LLM_KEYS = ("llm_backend", "llm_endpoint", "llm_model",
                 "claude_model", "claude_api_key")

    def __init__(self, parent, current_cfg, local_overrides=None):
        self.result = None          # set on "Save to Profile"
        self.local_result = None    # set on "Save to Host"
        cfg = current_cfg or {}
        self._local_overrides = local_overrides or {}

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("AI / LLM Configuration")
        self.dialog.geometry("480x370")
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

        # Indicator when host-local overrides are active
        if self._local_overrides:
            local_note = ttk.Label(
                self.dialog,
                text="* LLM settings shown include host-local overrides.",
                foreground="#e5e510", font=("TkDefaultFont", 8))
            local_note.grid(row=6, column=0, columnspan=2, pady=0)

        btn_frame = ttk.Frame(self.dialog)
        btn_frame.grid(row=7, column=0, columnspan=2, pady=12)
        ttk.Button(btn_frame, text="Save to Profile",
                   command=self._save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Save to Host",
                   command=self._save_local).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel",
                   command=self.dialog.destroy).pack(side=tk.LEFT, padx=5)

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

    def _gather(self):
        return {
            "llm_backend":    self.backend_var.get(),
            "llm_endpoint":   self.endpoint_entry.get().strip(),
            "llm_model":      self.model_entry.get().strip(),
            "claude_model":   self.claude_model_entry.get().strip(),
            "claude_api_key": self.api_key_entry.get().strip(),
        }

    def _save(self):
        self.result = self._gather()
        self.dialog.destroy()

    def _save_local(self):
        self.local_result = self._gather()
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


class SkillsDialog:
    """Manage skills, templates, and targets for the current profile."""

    # Stat keys the user may ask the LLM to watch (from self.char_stats / mud_parser).
    AVAILABLE_STATS = [
        "hp", "max_hp", "mp", "max_mp", "mv", "max_mv",
        "hunger", "thirst", "level", "xp", "gold", "alignment",
        "tank", "opp",
    ]

    def __init__(self, parent, client):
        self.client = client
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(f"Skills — {client.current_profile}")
        self.dialog.geometry("560x440")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        nb = ttk.Notebook(self.dialog)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.skill_list = self._build_tab(
            nb, "Skills",
            new_cb=self._new_skill, edit_cb=self._edit_skill,
            delete_cb=self._delete_skill, reload_cb=self._reload_skills)
        self.template_list = self._build_tab(
            nb, "Templates",
            new_cb=self._new_template, edit_cb=self._edit_template,
            delete_cb=self._delete_template, reload_cb=self._reload_templates)
        self.target_list = self._build_tab(
            nb, "Targets",
            new_cb=self._new_target, edit_cb=self._edit_target,
            delete_cb=self._delete_target, reload_cb=self._reload_targets)

        ttk.Button(self.dialog, text="Close",
                   command=self._close).pack(side=tk.RIGHT, padx=10, pady=(0, 10))
        self.dialog.protocol("WM_DELETE_WINDOW", self._close)

        self._reload_skills()
        self._reload_templates()
        self._reload_targets()

    def _close(self):
        self.dialog.destroy()
        self.client.master.after(0, self.client.input_entry.focus_set)

    def _build_tab(self, nb, label, new_cb, edit_cb, delete_cb, reload_cb):
        frame = ttk.Frame(nb)
        nb.add(frame, text=label)
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 4))
        lb = tk.Listbox(list_frame, height=12)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=lb.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        lb.config(yscrollcommand=sb.set)
        lb.bind("<Double-Button-1>", lambda _e: edit_cb())
        btns = ttk.Frame(frame)
        btns.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(btns, text="New...", command=new_cb).pack(side=tk.LEFT)
        ttk.Button(btns, text="Edit...", command=edit_cb).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Delete", command=delete_cb).pack(side=tk.LEFT, padx=4)
        return lb

    @staticmethod
    def _selected(lb):
        sel = lb.curselection()
        if not sel:
            return None
        return lb.get(sel[0])

    # ---- Skills tab ----
    def _reload_skills(self):
        self.skill_list.delete(0, tk.END)
        for name in sorted(self.client._current_skills().keys()):
            self.skill_list.insert(tk.END, name)

    def _new_skill(self):
        ed = SkillEditDialog(self.dialog, "", {}, self.AVAILABLE_STATS)
        if ed.result is None:
            return
        name, cfg = ed.result
        if not name:
            return
        skills = self.client._current_skills()
        if name in skills:
            if not messagebox.askyesno("Overwrite?",
                                       f"A skill named '{name}' already exists. Replace it?"):
                return
        skills[name] = cfg
        self.client.save_profiles()
        self._reload_skills()

    def _edit_skill(self):
        name = self._selected(self.skill_list)
        if not name:
            return
        skills = self.client._current_skills()
        cfg = skills.get(name, {})
        ed = SkillEditDialog(self.dialog, name, cfg, self.AVAILABLE_STATS)
        if ed.result is None:
            return
        new_name, new_cfg = ed.result
        if not new_name:
            return
        if new_name != name:
            skills.pop(name, None)
        skills[new_name] = new_cfg
        self.client.save_profiles()
        self._reload_skills()

    def _delete_skill(self):
        name = self._selected(self.skill_list)
        if not name:
            return
        if not messagebox.askyesno("Delete skill", f"Delete skill '{name}'?"):
            return
        self.client._current_skills().pop(name, None)
        self.client.save_profiles()
        self._reload_skills()

    # ---- Templates tab ----
    def _reload_templates(self):
        self.template_list.delete(0, tk.END)
        for name in sorted(self.client._current_skill_templates().keys()):
            self.template_list.insert(tk.END, name)

    def _new_template(self):
        ed = TemplateEditDialog(self.dialog, "", {}, self.AVAILABLE_STATS)
        if ed.result is None:
            return
        name, cfg = ed.result
        if not name:
            return
        tmpls = self.client._current_skill_templates()
        if name in tmpls:
            if not messagebox.askyesno("Overwrite?",
                                       f"Template '{name}' already exists. Replace it?"):
                return
        tmpls[name] = cfg
        self.client.save_profiles()
        self._reload_templates()

    def _edit_template(self):
        name = self._selected(self.template_list)
        if not name:
            return
        tmpls = self.client._current_skill_templates()
        cfg = tmpls.get(name, {})
        ed = TemplateEditDialog(self.dialog, name, cfg, self.AVAILABLE_STATS)
        if ed.result is None:
            return
        new_name, new_cfg = ed.result
        if not new_name:
            return
        if new_name != name:
            tmpls.pop(name, None)
        tmpls[new_name] = new_cfg
        self.client.save_profiles()
        self._reload_templates()

    def _delete_template(self):
        name = self._selected(self.template_list)
        if not name:
            return
        if not messagebox.askyesno("Delete template", f"Delete template '{name}'?"):
            return
        self.client._current_skill_templates().pop(name, None)
        self.client.save_profiles()
        self._reload_templates()

    # ---- Targets tab ----
    def _reload_targets(self):
        self.target_list.delete(0, tk.END)
        for name in sorted(self.client._current_skill_targets().keys()):
            tgt = self.client._current_skill_targets()[name]
            self.target_list.insert(tk.END, f"{name}  [{tgt.get('template', '?')}]")

    def _selected_target_name(self):
        sel = self.target_list.curselection()
        if not sel:
            return None
        raw = self.target_list.get(sel[0])
        return raw.split("  [")[0]

    def _new_target(self):
        tmpls = self.client._current_skill_templates()
        if not tmpls:
            messagebox.showwarning(
                "Targets", "Define a template first on the Templates tab.")
            return
        ed = TargetEditDialog(self.dialog, "", {}, tmpls)
        if ed.result is None:
            return
        name, cfg = ed.result
        if not name:
            return
        targets = self.client._current_skill_targets()
        if name in targets:
            if not messagebox.askyesno("Overwrite?",
                                       f"Target '{name}' already exists. Replace it?"):
                return
        targets[name] = cfg
        self.client.save_profiles()
        self._reload_targets()

    def _edit_target(self):
        name = self._selected_target_name()
        if not name:
            return
        targets = self.client._current_skill_targets()
        cfg = targets.get(name, {})
        tmpls = self.client._current_skill_templates()
        ed = TargetEditDialog(self.dialog, name, cfg, tmpls)
        if ed.result is None:
            return
        new_name, new_cfg = ed.result
        if not new_name:
            return
        if new_name != name:
            targets.pop(name, None)
        targets[new_name] = new_cfg
        self.client.save_profiles()
        self._reload_targets()

    def _delete_target(self):
        name = self._selected_target_name()
        if not name:
            return
        if not messagebox.askyesno("Delete target", f"Delete target '{name}'?"):
            return
        self.client._current_skill_targets().pop(name, None)
        self.client.save_profiles()
        self._reload_targets()


class SkillEditDialog:
    """Edit one skill: name, instructions, plan, rescue_restart_step, watched stats."""

    def __init__(self, parent, name, cfg, available_stats):
        self.result = None
        self._original_cfg = dict(cfg or {})
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Edit Skill" if name else "New Skill")
        self.dialog.geometry("620x780")
        self.dialog.transient(parent)
        self.dialog.update_idletasks()
        self.dialog.grab_set()

        pad = {"padx": 10, "pady": 6}

        ttk.Label(self.dialog, text="Name:").grid(row=0, column=0, sticky=tk.W, **pad)
        self.name_entry = ttk.Entry(self.dialog, width=50)
        self.name_entry.insert(0, name)
        self.name_entry.grid(row=0, column=1, sticky=tk.W + tk.E, **pad)

        ttk.Label(self.dialog, text="Instructions\n(what, where, how,\nrescue recovery)") \
            .grid(row=1, column=0, sticky=tk.NW, **pad)
        self.instructions_text = tk.Text(self.dialog, width=60, height=12, wrap=tk.WORD)
        self.instructions_text.insert("1.0", cfg.get("instructions", ""))
        self.instructions_text.grid(row=1, column=1, sticky=tk.NSEW, **pad)

        ttk.Label(self.dialog, text="Plan\n(- [ ] step_id: desc\none per line)") \
            .grid(row=2, column=0, sticky=tk.NW, **pad)
        self.plan_text = tk.Text(self.dialog, width=60, height=10, wrap=tk.WORD)
        self.plan_text.insert("1.0", cfg.get("plan", ""))
        self.plan_text.grid(row=2, column=1, sticky=tk.NSEW, **pad)

        ttk.Label(self.dialog, text="Rescue restart\nstep ID:").grid(row=3, column=0, sticky=tk.W, **pad)
        self.rescue_entry = ttk.Entry(self.dialog, width=40)
        self.rescue_entry.insert(0, cfg.get("rescue_restart_step", ""))
        self.rescue_entry.grid(row=3, column=1, sticky=tk.W, **pad)

        ttk.Label(self.dialog, text="Watched stats:").grid(row=4, column=0, sticky=tk.NW, **pad)
        stats_frame = ttk.Frame(self.dialog)
        stats_frame.grid(row=4, column=1, sticky=tk.W, **pad)
        selected = set(cfg.get("watch_stats", []))
        self._stat_vars = {}
        for i, key in enumerate(available_stats):
            var = tk.BooleanVar(value=(key in selected))
            self._stat_vars[key] = var
            ttk.Checkbutton(stats_frame, text=key, variable=var).grid(
                row=i // 4, column=i % 4, sticky=tk.W, padx=4, pady=2)

        btns = ttk.Frame(self.dialog)
        btns.grid(row=5, column=0, columnspan=2, pady=12)
        ttk.Button(btns, text="Save", command=self._save).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Cancel", command=self.dialog.destroy).pack(side=tk.LEFT, padx=4)

        self.dialog.columnconfigure(1, weight=1)
        self.dialog.rowconfigure(1, weight=1)
        self.dialog.rowconfigure(2, weight=1)
        self.dialog.wait_window()

    def _save(self):
        name = self.name_entry.get().strip()
        if not name:
            messagebox.showwarning("Skill", "Name is required.")
            return
        instructions = self.instructions_text.get("1.0", tk.END).strip()
        plan = self.plan_text.get("1.0", tk.END).strip()
        rescue = self.rescue_entry.get().strip()
        watch = [k for k, v in self._stat_vars.items() if v.get()]
        cfg = dict(self._original_cfg)
        cfg["instructions"] = instructions
        cfg["watch_stats"] = watch
        if plan:
            cfg["plan"] = plan
        elif "plan" in cfg:
            del cfg["plan"]
        if rescue:
            cfg["rescue_restart_step"] = rescue
        elif "rescue_restart_step" in cfg:
            del cfg["rescue_restart_step"]
        self.result = (name, cfg)
        self.dialog.destroy()


class TemplateEditDialog:
    """Edit one skill template: name, instructions (with {{placeholders}}),
    plan (markdown checkboxes), optional reminders, placeholders list, watched stats."""

    def __init__(self, parent, name, cfg, available_stats):
        self.result = None
        self._original_cfg = dict(cfg or {})
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Edit Template" if name else "New Template")
        self.dialog.geometry("680x820")
        self.dialog.transient(parent)
        self.dialog.update_idletasks()
        self.dialog.grab_set()

        pad = {"padx": 10, "pady": 4}

        ttk.Label(self.dialog, text="Name:").grid(row=0, column=0, sticky=tk.W, **pad)
        self.name_entry = ttk.Entry(self.dialog, width=50)
        self.name_entry.insert(0, name)
        self.name_entry.grid(row=0, column=1, sticky=tk.W + tk.E, **pad)

        ttk.Label(self.dialog,
                  text="Placeholders\n(comma-separated,\nused as {{name}}\nin text)") \
            .grid(row=1, column=0, sticky=tk.NW, **pad)
        self.placeholders_entry = ttk.Entry(self.dialog, width=60)
        self.placeholders_entry.insert(0, ", ".join(cfg.get("placeholders", [])))
        self.placeholders_entry.grid(row=1, column=1, sticky=tk.W + tk.E, **pad)

        ttk.Label(self.dialog, text="Instructions").grid(
            row=2, column=0, sticky=tk.NW, **pad)
        self.instructions_text = tk.Text(self.dialog, width=70, height=10, wrap=tk.WORD)
        self.instructions_text.insert("1.0", cfg.get("instructions", ""))
        self.instructions_text.grid(row=2, column=1, sticky=tk.NSEW, **pad)

        ttk.Label(self.dialog,
                  text="Plan\n(markdown,\none step per line:\n- [ ] step_id: desc)").grid(
            row=3, column=0, sticky=tk.NW, **pad)
        self.plan_text = tk.Text(self.dialog, width=70, height=8, wrap=tk.WORD)
        plan_val = cfg.get("plan", "")
        if isinstance(plan_val, list):
            # Legacy JSON array — show as markdown so the user can edit it
            from skill_engine import _parse_plan_steps as _pps  # local import avoids cycle
            plan_val = "\n".join(
                f"- [ ] {s.get('step', '?')}: {s.get('description', '')}"
                for s in plan_val
            )
        self.plan_text.insert("1.0", plan_val)
        self.plan_text.grid(row=3, column=1, sticky=tk.NSEW, **pad)

        ttk.Label(self.dialog, text="Reminders\n(optional)").grid(
            row=4, column=0, sticky=tk.NW, **pad)
        self.reminders_text = tk.Text(self.dialog, width=70, height=4, wrap=tk.WORD)
        self.reminders_text.insert("1.0", cfg.get("reminders", ""))
        self.reminders_text.grid(row=4, column=1, sticky=tk.NSEW, **pad)

        ttk.Label(self.dialog, text="Watched stats:").grid(
            row=5, column=0, sticky=tk.NW, **pad)
        stats_frame = ttk.Frame(self.dialog)
        stats_frame.grid(row=5, column=1, sticky=tk.W, **pad)
        selected = set(cfg.get("watch_stats", []))
        self._stat_vars = {}
        for i, key in enumerate(available_stats):
            var = tk.BooleanVar(value=(key in selected))
            self._stat_vars[key] = var
            ttk.Checkbutton(stats_frame, text=key, variable=var).grid(
                row=i // 4, column=i % 4, sticky=tk.W, padx=4, pady=2)

        btns = ttk.Frame(self.dialog)
        btns.grid(row=6, column=0, columnspan=2, pady=12)
        ttk.Button(btns, text="Save", command=self._save).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Cancel", command=self.dialog.destroy).pack(side=tk.LEFT, padx=4)

        self.dialog.columnconfigure(1, weight=1)
        self.dialog.rowconfigure(2, weight=1)
        self.dialog.rowconfigure(3, weight=1)
        self.dialog.wait_window()

    def _save(self):
        name = self.name_entry.get().strip()
        if not name:
            messagebox.showwarning("Template", "Name is required.")
            return
        raw_ph = self.placeholders_entry.get().strip()
        placeholders = [p.strip() for p in raw_ph.split(",") if p.strip()] if raw_ph else []
        instructions = self.instructions_text.get("1.0", tk.END).strip()
        plan = self.plan_text.get("1.0", tk.END).strip()
        reminders = self.reminders_text.get("1.0", tk.END).strip()
        watch = [k for k, v in self._stat_vars.items() if v.get()]
        # Start from the original cfg to preserve fields not shown here
        # (e.g. rescue_restart_step).
        cfg = dict(self._original_cfg)
        cfg["instructions"] = instructions
        cfg["watch_stats"] = watch
        cfg["placeholders"] = placeholders
        if plan:
            cfg["plan"] = plan
        else:
            cfg.pop("plan", None)
        if reminders:
            cfg["reminders"] = reminders
        else:
            cfg.pop("reminders", None)
        self.result = (name, cfg)
        self.dialog.destroy()


class TargetEditDialog:
    """Edit one skill target: name, bound template, parameter values."""

    def __init__(self, parent, name, cfg, templates):
        self.result = None
        self.templates = templates
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Edit Target" if name else "New Target")
        self.dialog.geometry("620x500")
        self.dialog.transient(parent)
        self.dialog.update_idletasks()
        self.dialog.grab_set()

        pad = {"padx": 10, "pady": 6}

        ttk.Label(self.dialog, text="Name:").grid(row=0, column=0, sticky=tk.W, **pad)
        self.name_entry = ttk.Entry(self.dialog, width=50)
        self.name_entry.insert(0, name)
        self.name_entry.grid(row=0, column=1, sticky=tk.W + tk.E, **pad)

        ttk.Label(self.dialog, text="Template:").grid(row=1, column=0, sticky=tk.W, **pad)
        tmpl_names = sorted(templates.keys())
        initial_tmpl = cfg.get("template") or (tmpl_names[0] if tmpl_names else "")
        self.template_var = tk.StringVar(value=initial_tmpl)
        self.template_combo = ttk.Combobox(
            self.dialog, textvariable=self.template_var,
            values=tmpl_names, state="readonly", width=47)
        self.template_combo.grid(row=1, column=1, sticky=tk.W + tk.E, **pad)
        self.template_combo.bind("<<ComboboxSelected>>",
                                 lambda _e: self._rebuild_params())

        params_outer = ttk.LabelFrame(self.dialog, text="Parameters")
        params_outer.grid(row=2, column=0, columnspan=2, sticky=tk.NSEW, **pad)
        self.params_frame = ttk.Frame(params_outer)
        self.params_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self._param_widgets = {}  # name -> Text widget
        self._existing_params = dict(cfg.get("params", {}))

        btns = ttk.Frame(self.dialog)
        btns.grid(row=3, column=0, columnspan=2, pady=12)
        ttk.Button(btns, text="Save", command=self._save).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Cancel", command=self.dialog.destroy).pack(side=tk.LEFT, padx=4)

        self.dialog.columnconfigure(1, weight=1)
        self.dialog.rowconfigure(2, weight=1)

        self._rebuild_params()
        self.dialog.wait_window()

    def _rebuild_params(self):
        for child in self.params_frame.winfo_children():
            child.destroy()
        self._param_widgets = {}
        tmpl_name = self.template_var.get()
        tmpl = self.templates.get(tmpl_name, {})
        placeholders = tmpl.get("placeholders", [])
        if not placeholders:
            ttk.Label(self.params_frame,
                      text="(selected template declares no placeholders)") \
                .grid(row=0, column=0, sticky=tk.W)
            return
        for i, ph in enumerate(placeholders):
            ttk.Label(self.params_frame, text=f"{ph}:").grid(
                row=i, column=0, sticky=tk.NW, padx=4, pady=3)
            txt = tk.Text(self.params_frame, width=60, height=3, wrap=tk.WORD)
            txt.insert("1.0", str(self._existing_params.get(ph, "")))
            txt.grid(row=i, column=1, sticky=tk.W + tk.E, padx=4, pady=3)
            self._param_widgets[ph] = txt
        self.params_frame.columnconfigure(1, weight=1)

    def _save(self):
        name = self.name_entry.get().strip()
        if not name:
            messagebox.showwarning("Target", "Name is required.")
            return
        tmpl_name = self.template_var.get().strip()
        if not tmpl_name:
            messagebox.showwarning("Target", "Template is required.")
            return
        params = {ph: w.get("1.0", tk.END).strip()
                  for ph, w in self._param_widgets.items()}
        self.result = (name, {"template": tmpl_name, "params": params})
        self.dialog.destroy()
