"""Claude Code session management."""
import json
import os
from typing import Optional, List, Dict, Callable

import sublime

from .rpc import JsonRpcClient
from .output import OutputView


BRIDGE_SCRIPT = os.path.join(os.path.dirname(__file__), "bridge", "main.py")
CODEX_BRIDGE_SCRIPT = os.path.join(os.path.dirname(__file__), "bridge", "codex_main.py")
COPILOT_BRIDGE_SCRIPT = os.path.join(os.path.dirname(__file__), "bridge", "copilot_main.py")
SESSIONS_FILE = os.path.join(os.path.dirname(__file__), ".sessions.json")


def load_saved_sessions() -> List[Dict]:
    """Load saved sessions from disk."""
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return []


def save_sessions(sessions: List[Dict]) -> None:
    """Save sessions to disk."""
    try:
        with open(SESSIONS_FILE, "w") as f:
            json.dump(sessions, f, indent=2)
    except Exception as e:
        print(f"[Claude] Failed to save sessions: {e}")


class ContextItem:
    """A pending context item to attach to next query."""
    def __init__(self, kind: str, name: str, content: str):
        self.kind = kind  # "file", "selection"
        self.name = name  # Display name
        self.content = content  # Actual content


class Session:
    def __init__(self, window: sublime.Window, resume_id: Optional[str] = None, fork: bool = False, profile: Optional[Dict] = None, initial_context: Optional[Dict] = None, backend: str = "claude"):
        self.window = window
        self.backend = backend
        self.client: Optional[JsonRpcClient] = None
        self.output = OutputView(window)
        self.initialized = False
        self.working = False
        self.current_tool: Optional[str] = None
        self.spinner_frame = 0
        # Session identity
        # When resuming (not forking), use resume_id as session_id immediately
        # so renames/saves work before first query completes
        self.session_id: Optional[str] = resume_id if resume_id and not fork else None
        self.resume_id: Optional[str] = resume_id  # ID to resume from
        self.fork: bool = fork  # Fork from resume_id instead of continuing it
        self.profile: Optional[Dict] = profile  # Profile config (model, betas, system_prompt, preload_docs)
        self.initial_context: Optional[Dict] = initial_context  # Initial context (subsession_id, parent_view_id, etc.)
        self.name: Optional[str] = None
        self.total_cost: float = 0.0
        self.query_count: int = 0
        self.context_usage: Optional[Dict] = None  # Latest usage/context stats
        # Pending context for next query
        self.pending_context: List[ContextItem] = []
        # Profile docs available for reading (paths only, not content)
        self.profile_docs: List[str] = []
        # Draft prompt (persists across input panel open/close)
        self.draft_prompt: str = ""
        self._pending_resume_at: Optional[str] = None  # Set by undo, consumed by next query
        # Track if we've entered input mode after last query
        self._input_mode_entered: bool = False
        # Callback for channel mode responses
        self._response_callback: Optional[Callable[[str], None]] = None
        # Queue of prompts to send after current query completes
        self._queued_prompts: List[str] = []
        # Track if inject was sent (to skip "done" status until inject query completes)
        self._inject_pending: bool = False

        # Extract subsession_id and parent_view_id if provided
        if initial_context:
            self.subsession_id = initial_context.get("subsession_id")
            self.parent_view_id = initial_context.get("parent_view_id")
        else:
            self.subsession_id = None
            self.parent_view_id = None

        # Persona info (for release on close)
        if profile:
            self.persona_id = profile.get("persona_id")
            self.persona_session_id = profile.get("persona_session_id")
            self.persona_url = profile.get("persona_url")
        else:
            self.persona_id = None
            self.persona_session_id = None
            self.persona_url = None

        # Plan mode state
        self.plan_mode: bool = False
        self.plan_file: Optional[str] = None

        # Pending retain content (set by compact_boundary, sent after interrupt)
        self._pending_retain: Optional[str] = None

    def start(self, resume_session_at: str = None) -> None:
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        python_path = settings.get("python_path", "python3")

        # Build profile docs list early (before init) so we can add to system prompt
        self._build_profile_docs_list()

        # Load environment variables from settings and profile
        env = self._load_env(settings)

        # Sync sublime project retain content to file for hook
        self._sync_project_retain()

        if self.backend == "codex":
            bridge_script = CODEX_BRIDGE_SCRIPT
        elif self.backend == "copilot":
            bridge_script = COPILOT_BRIDGE_SCRIPT
        else:
            bridge_script = BRIDGE_SCRIPT
        self.client = JsonRpcClient(self._on_notification)
        self.client.start([python_path, bridge_script], env=env)
        self._status("connecting...")

        permission_mode = settings.get("permission_mode", "acceptEdits")
        # In default mode, don't auto-allow any tools - prompt for all
        if permission_mode == "default":
            allowed_tools = []
        else:
            allowed_tools = settings.get("allowed_tools", [])

        # Per-backend default model, fallback to legacy default_model
        default_models = settings.get("default_models", {})
        default_model = default_models.get(self.backend) or settings.get("default_model")
        print(f"[Claude] initialize: permission_mode={permission_mode}, allowed_tools={allowed_tools}, resume={self.resume_id}, fork={self.fork}, profile={self.profile}, default_model={default_model}, subsession_id={getattr(self, 'subsession_id', None)}")
        # Get additional working directories from project folders + project settings
        additional_dirs = self.window.folders()[1:] if len(self.window.folders()) > 1 else []
        project_data = self.window.project_data() or {}
        project_settings = project_data.get("settings", {})
        extra_dirs = project_settings.get("claude_additional_dirs", [])
        if extra_dirs:
            expanded = [os.path.expanduser(d) for d in extra_dirs]
            additional_dirs = additional_dirs + expanded
            print(f"[Claude] extra additional_dirs from project: {expanded}")
        init_params = {
            "cwd": self._cwd(),
            "additional_dirs": additional_dirs,
            "allowed_tools": allowed_tools,
            "permission_mode": permission_mode,
            "view_id": str(self.output.view.id()) if self.output and self.output.view else None,
        }
        if self.resume_id:
            init_params["resume"] = self.resume_id
            if self.fork:
                init_params["fork_session"] = True
            if resume_session_at:
                init_params["resume_session_at"] = resume_session_at
        # Pass subsession_id if this is a subsession
        if hasattr(self, 'subsession_id') and self.subsession_id:
            init_params["subsession_id"] = self.subsession_id
        # Apply profile config or default model
        if self.profile:
            if self.profile.get("model"):
                init_params["model"] = self.profile["model"]
            if self.profile.get("betas"):
                init_params["betas"] = self.profile["betas"]
            if self.profile.get("pre_compact_prompt"):
                init_params["pre_compact_prompt"] = self.profile["pre_compact_prompt"]
            # Build system prompt with profile docs info
            system_prompt = self.profile.get("system_prompt", "")
            if self.profile_docs:
                docs_info = f"\n\nProfile Documentation: {len(self.profile_docs)} files available. Use list_profile_docs to see them and read_profile_doc(path) to read their contents."
                system_prompt = system_prompt + docs_info if system_prompt else docs_info.strip()
            if system_prompt:
                init_params["system_prompt"] = system_prompt
        else:
            # No profile - use default_model setting if available
            if default_model:
                init_params["model"] = default_model
        self.client.send("initialize", init_params, self._on_init)

    def _cwd(self) -> str:
        if self.window.folders():
            return self.window.folders()[0]
        view = self.window.active_view()
        if view and view.file_name():
            return os.path.dirname(view.file_name())
        # Fallback: use ~/.claude/scratch for sessions without a project
        # This ensures consistent cwd for session resume
        scratch_dir = os.path.expanduser("~/.claude/scratch")
        os.makedirs(scratch_dir, exist_ok=True)
        return scratch_dir

    def _on_init(self, result: dict) -> None:
        if "error" in result:
            error_msg = result['error'].get('message', str(result['error']))
            print(f"[Claude] init error: {error_msg}")
            self._status("error")

            # Show user-friendly message in view
            is_session_error = (
                "No conversation found" in error_msg or
                "Command failed" in error_msg
            )
            if is_session_error:
                self.output.text("\n*Session expired or not found.*\n\nUse `Claude: Restart Session` (Cmd+Shift+R) to start fresh.\n")
            else:
                self.output.text(f"\n*Failed to connect: {error_msg}*\n\nTry `Claude: Restart Session` (Cmd+Shift+R).\n")
            return
        self.initialized = True
        self.working = False
        self.current_tool = None
        if self._pending_resume_at:
            self._pending_resume_at = None
            self._save_session()  # Clear persisted rewind point
        self._input_mode_entered = False  # Reset for fresh start after init
        # Capture session_id from initialize response (set via --session-id CLI arg)
        if result.get("session_id"):
            self.session_id = result["session_id"]
            print(f"[Claude] session_id={self.session_id}")
        # Show loaded MCP servers and agents
        mcp_servers = result.get("mcp_servers", [])
        agents = result.get("agents", [])
        parts = []
        if mcp_servers:
            print(f"[Claude] MCP servers: {mcp_servers}")
            parts.append(f"MCP: {', '.join(mcp_servers)}")
        if agents:
            print(f"[Claude] Agents: {agents}")
            parts.append(f"agents: {', '.join(agents)}")
        if parts:
            self._status(f"ready ({'; '.join(parts)})")
        else:
            self._status("ready")
        # Auto-enter input mode when ready
        self._enter_input_with_draft()

    def _load_env(self, settings) -> dict:
        """Load environment variables from settings and project profile."""
        import os
        env = {}
        # From user settings (ClaudeCode.sublime-settings)
        settings_env = settings.get("env", {})
        if isinstance(settings_env, dict):
            env.update(settings_env)
        # From sublime project settings (.sublime-project -> settings -> claude_env)
        project_data = self.window.project_data() or {}
        project_settings = project_data.get("settings", {})
        project_env = project_settings.get("claude_env", {})
        if isinstance(project_env, dict):
            env.update(project_env)
        # From project .claude/settings.json
        cwd = self._cwd()
        if cwd:
            project_settings_path = os.path.join(cwd, ".claude", "settings.json")
            if os.path.exists(project_settings_path):
                try:
                    with open(project_settings_path, "r") as f:
                        import json
                        project_settings = json.load(f)
                    claude_env = project_settings.get("env", {})
                    if isinstance(claude_env, dict):
                        env.update(claude_env)
                except Exception as e:
                    print(f"[Claude] Failed to load project env: {e}")
        # From profile (highest priority)
        if self.profile:
            profile_env = self.profile.get("env", {})
            if isinstance(profile_env, dict):
                env.update(profile_env)
        if env:
            print(f"[Claude] Custom env vars: {env}")
        return env

    def _sync_project_retain(self):
        """Sync sublime project retain setting to file for hook."""
        cwd = self._cwd()
        if not cwd:
            return
        project_data = self.window.project_data() or {}
        project_settings = project_data.get("settings", {})
        retain_content = project_settings.get("claude_retain", "")

        retain_path = os.path.join(cwd, ".claude", "sublime_project_retain.md")
        if retain_content:
            os.makedirs(os.path.dirname(retain_path), exist_ok=True)
            with open(retain_path, "w") as f:
                f.write(retain_content)
        elif os.path.exists(retain_path):
            os.remove(retain_path)

    def _get_retain_path(self) -> Optional[str]:
        """Get path to session's dynamic retain file."""
        if not self.session_id:
            return None
        cwd = self._cwd()
        if not cwd:
            return None
        return os.path.join(cwd, ".claude", "sessions", f"{self.session_id}_retain.md")

    def retain(self, content: str = None, append: bool = False) -> Optional[str]:
        """Write to or read session's retain file for compaction.

        Args:
            content: Content to write (None to read current)
            append: If True, append to existing content

        Returns:
            Current retain content if reading, None if writing
        """
        path = self._get_retain_path()
        if not path:
            print("[Claude] Cannot access retain file - no session_id yet")
            return None

        if content is None:
            # Read mode
            if os.path.exists(path):
                with open(path, "r") as f:
                    return f.read()
            return ""

        # Write mode - ensure directory exists
        os.makedirs(os.path.dirname(path), exist_ok=True)

        mode = "a" if append else "w"
        with open(path, mode) as f:
            if append and os.path.exists(path):
                f.write("\n")
            f.write(content)
        print(f"[Claude] Retain file updated: {path}")
        return None

    def clear_retain(self):
        """Clear session's retain file."""
        path = self._get_retain_path()
        if path and os.path.exists(path):
            os.remove(path)
            print(f"[Claude] Retain file cleared: {path}")

    def _strip_comment_only_content(self, content: str) -> str:
        """Strip lines that are only comments or whitespace."""
        lines = content.split('\n')
        filtered = [line for line in lines if line.strip() and not line.strip().startswith('#')]
        return '\n'.join(filtered).strip()

    def _gather_retain_content(self) -> Optional[str]:
        """Gather all retain content from various sources.

        Returns combined retain content string, or None if no content found.
        """
        prompts = []
        cwd = self._cwd()

        # 1. Static retain file (.claude/RETAIN.md)
        if cwd:
            static_path = os.path.join(cwd, ".claude", "RETAIN.md")
            if os.path.exists(static_path):
                try:
                    with open(static_path, "r") as f:
                        content = self._strip_comment_only_content(f.read())
                    if content:
                        prompts.append(content)
                except Exception as e:
                    print(f"[Claude] Error reading static retain: {e}")

        # 2. Sublime project retain file
        if cwd:
            sublime_retain_path = os.path.join(cwd, ".claude", "sublime_project_retain.md")
            if os.path.exists(sublime_retain_path):
                try:
                    with open(sublime_retain_path, "r") as f:
                        content = self._strip_comment_only_content(f.read())
                    if content:
                        prompts.append(content)
                except Exception as e:
                    print(f"[Claude] Error reading sublime project retain: {e}")

        # 3. Session retain file
        session_retain = self._strip_comment_only_content(self.retain() or "")
        if session_retain:
            prompts.append(session_retain)

        # 4. Profile pre_compact_prompt
        if self.profile and self.profile.get("pre_compact_prompt"):
            prompts.append(self.profile["pre_compact_prompt"])

        if prompts:
            return "\n\n---\n\n".join(prompts)
        return None

    def _inject_retain_midquery(self) -> None:
        """Inject retain content by interrupting and restarting with retain prompt."""
        content = self._gather_retain_content()
        if content:
            print(f"[Claude] Interrupting to inject retain content ({len(content)} chars)")
            # Store retain content to send after interrupt completes
            self._pending_retain = f"[retain context]\n\n{content}"
            self.interrupt(break_channel=False)

    def _record_edit(self, tool_name: str):
        """Record an Edit/Write operation to the order table's edit log."""
        from .order_table import get_table, refresh_order_table

        # Get tool input from current conversation
        if not self.output.current:
            return
        tools = self.output.current.tools
        if not tools:
            return

        # Find the most recent tool of this type that's still pending
        tool_input = None
        for tool in reversed(tools):
            if tool.name == tool_name and tool.status == "pending":
                tool_input = tool.tool_input
                break
        if not tool_input:
            return

        file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not file_path:
            return

        # Calculate line number, diff stats, and context
        if tool_name == "Edit":
            old = tool_input.get("old_string", "")
            new = tool_input.get("new_string", "")
            line_num = self._find_edit_line(file_path, new or old)
            lines_added = len(new.splitlines()) if new else 0
            lines_removed = len(old.splitlines()) if old else 0
            context = self._extract_edit_context(new)
        else:  # Write
            content = tool_input.get("content", "")
            line_num = 1
            lines_added = len(content.splitlines())
            lines_removed = 0
            context = os.path.basename(file_path)

        table = get_table(self.window)
        if table:
            agent_name = self.name or f"view_{self.output.view.id()}" if self.output.view else "unknown"
            view_id = self.output.view.id() if self.output.view else 0
            table.add_edit(agent_name, view_id, file_path, line_num or 1,
                          lines_added, lines_removed, tool_name, context)
            refresh_order_table(self.window)

    def _extract_edit_context(self, text: str) -> str:
        """Extract first meaningful line as context."""
        if not text:
            return ""
        for line in text.split('\n'):
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith('//'):
                # Truncate and clean
                return line[:50].strip()
        return ""

    def _find_edit_line(self, file_path: str, search: str) -> int:
        """Find line number where content occurs in file."""
        if not search or not os.path.exists(file_path):
            return None
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            pos = content.find(search)
            if pos >= 0:
                return content[:pos].count('\n') + 1
        except Exception:
            pass
        return None

    def _build_profile_docs_list(self) -> None:
        """Build list of available docs from profile preload_docs patterns (no reading yet)."""
        if not self.profile or not self.profile.get("preload_docs"):
            return

        import glob as glob_module

        patterns = self.profile["preload_docs"]
        if isinstance(patterns, str):
            patterns = [patterns]

        cwd = self._cwd()

        try:
            for pattern in patterns:
                # Make pattern relative to cwd
                full_pattern = os.path.join(cwd, pattern)
                for filepath in glob_module.glob(full_pattern, recursive=True):
                    if os.path.isfile(filepath):
                        rel_path = os.path.relpath(filepath, cwd)
                        self.profile_docs.append(rel_path)

            if self.profile_docs:
                print(f"[Claude] Profile docs available: {len(self.profile_docs)} files")
        except Exception as e:
            print(f"[Claude] preload_docs error: {e}")

    def add_context_file(self, path: str, content: str) -> None:
        """Add a file to pending context."""
        name = os.path.basename(path)
        self.pending_context.append(ContextItem("file", name, f"File: {path}\n```\n{content}\n```"))
        # print(f"[Claude] add_context_file: added {name}, pending_context={[c.name for c in self.pending_context]}")
        self._update_context_display()

    def add_context_selection(self, path: str, content: str) -> None:
        """Add a selection to pending context."""
        name = os.path.basename(path) if path else "selection"
        self.pending_context.append(ContextItem("selection", name, f"Selection from {path}:\n```\n{content}\n```"))
        self._update_context_display()

    def add_context_folder(self, path: str) -> None:
        """Add a folder path to pending context."""
        name = os.path.basename(path) + "/"
        self.pending_context.append(ContextItem("folder", name, f"Folder: {path}"))
        self._update_context_display()

    def add_context_image(self, image_data: bytes, mime_type: str) -> None:
        """Add an image to pending context."""
        import base64
        import tempfile

        # Save to temp file for reference
        ext = ".png" if "png" in mime_type else ".jpg" if "jpeg" in mime_type or "jpg" in mime_type else ".img"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(image_data)
            temp_path = f.name

        # Store base64 encoded image
        encoded = base64.b64encode(image_data).decode('utf-8')
        # Use special format that query() will detect
        name = f"image{ext}"
        self.pending_context.append(ContextItem(
            "image",
            name,
            f"__IMAGE__:{mime_type}:{encoded}"  # Special marker for image data
        ))
        print(f"[Claude] Added image to context: {name} ({len(image_data)} bytes, saved to {temp_path})")
        self._update_context_display()

    def clear_context(self) -> None:
        """Clear pending context."""
        self.pending_context = []
        self._update_context_display()

    def _update_context_display(self) -> None:
        """Update output view with pending context."""
        self.output.set_pending_context(self.pending_context)

    def _build_prompt_with_context(self, prompt: str) -> tuple:
        """Build full prompt with pending context.

        Returns:
            (full_prompt, images) where images is list of {"mime_type": str, "data": str}
        """
        if not self.pending_context:
            return prompt, []

        parts = []
        images = []
        for item in self.pending_context:
            if item.content.startswith("__IMAGE__:"):
                # Extract image data: __IMAGE__:mime_type:base64data
                _, mime_type, data = item.content.split(":", 2)
                images.append({"mime_type": mime_type, "data": data})
            else:
                parts.append(item.content)
        parts.append(prompt)
        return "\n\n".join(parts), images

    def undo_message(self) -> None:
        """Undo last conversation turn by rewinding the CLI session."""
        if not self.session_id:
            return
        # Allow undo even while "working" (bridge reconnecting from previous undo)
        if self.working and self.current_tool != "rewinding...":
            return
        rewind_id, undone_prompt = self._find_rewind_point()
        if not rewind_id:
            print(f"[Claude] undo_message: no rewind point found")
            return
        saved_id = self.session_id
        print(f"[Claude] undo_message: rewinding {saved_id} to {rewind_id}")
        # Exit input mode
        if self.output._input_mode:
            self.output.exit_input_mode(keep_text=False)
        # Erase last prompt turn from view (prompt = "◎ ... ▶", not input marker "◎ ")
        view = self.output.view
        content = view.substr(sublime.Region(0, view.size()))
        # Find last prompt line (contains " ▶")
        import re as _re
        last_prompt = None
        for m in _re.finditer(r'\n◎ .+? ▶', content):
            last_prompt = m
        if not last_prompt and content.startswith("◎ ") and " ▶" in content.split("\n")[0]:
            print(f"[Claude] undo: erasing entire view (first turn)")
            self.output._replace(0, view.size(), "")
        elif last_prompt:
            erase_from = last_prompt.start()
            print(f"[Claude] undo: erasing from {erase_from} to {view.size()}, matched={last_prompt.group()[:40]!r}")
            self.output._replace(erase_from, view.size(), "")
        else:
            print(f"[Claude] undo: no prompt found to erase")
        # Update conversation state
        if self.output.current:
            self.output.current = None
        view.erase_regions("claude_conversation")
        # Kill bridge synchronously (may already be dead from previous undo)
        if self.client:
            self.client.stop()
            self.client = None
        self.initialized = False
        # Restart bridge with rewind
        self.session_id = saved_id
        self.resume_id = saved_id
        self.fork = False
        self.draft_prompt = undone_prompt
        self._input_mode_entered = True  # Block auto input mode until bridge ready
        self._pending_resume_at = rewind_id
        self._save_session()  # Persist rewind point for restart survival
        self.working = True
        self.current_tool = "rewinding..."
        self._animate()
        self.start(resume_session_at=rewind_id)
        # _on_init will reset _input_mode_entered and call _enter_input_with_draft

    def _find_rewind_point(self) -> tuple:
        """Find the assistant entry uuid to rewind to (before last visible turn).
        Respects current _pending_resume_at to support consecutive undos.
        Returns (uuid, undone_prompt) or (None, "") if can't rewind."""
        jsonl_path = self._find_jsonl_path()
        if not jsonl_path:
            return None, ""
        # Collect user prompt turns and their preceding assistant uuid
        turns = []  # [(prompt, prev_assistant_uuid)]
        last_assistant_uuid = None
        try:
            with open(jsonl_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    if entry.get("isSidechain") or entry.get("isMeta"):
                        continue
                    etype = entry.get("type")
                    if etype == "assistant":
                        uuid = entry.get("uuid")
                        if uuid:
                            last_assistant_uuid = uuid
                    elif etype == "user":
                        msg = entry.get("message", {})
                        content = msg.get("content", [])
                        has_tool_result = (
                            isinstance(content, list) and
                            any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                        )
                        if has_tool_result:
                            continue
                        prompt = ""
                        if isinstance(content, str):
                            prompt = content
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    prompt += block.get("text", "")
                        turns.append((prompt, last_assistant_uuid))
        except Exception as e:
            print(f"[Claude] _find_rewind_point error: {e}")
            return None, ""
        # If already rewound, find the turn whose prev_assistant_uuid == current rewind point
        # and rewind one step further back
        if self._pending_resume_at:
            # Find which turn we're currently rewound to
            for i, (prompt, asst_uuid) in enumerate(turns):
                if asst_uuid == self._pending_resume_at:
                    # This turn starts after the current rewind point
                    # We want to undo the turn BEFORE this one
                    if i < 2:
                        return None, ""  # Can't undo further
                    undone_prompt = turns[i - 1][0]
                    rewind_to = turns[i - 1][1]
                    if not rewind_to:
                        return None, ""
                    return rewind_to, undone_prompt
            # Fallback: current rewind point not found in turns
            return None, ""
        # Normal case: undo the last turn
        if len(turns) < 2:
            return None, ""
        undone_prompt = turns[-1][0]
        rewind_to = turns[-1][1]
        if not rewind_to:
            return None, ""
        return rewind_to, undone_prompt

    def _find_jsonl_path(self) -> Optional[str]:
        """Find the JSONL file for this session."""
        if not self.session_id:
            return None
        fname = f"{self.session_id}.jsonl"
        projects_dir = os.path.expanduser("~/.claude/projects")
        # Try exact cwd match first
        cwd = self._cwd()
        project_key = cwd.replace("/", "-").lstrip("-")
        exact = os.path.join(projects_dir, project_key, fname)
        if os.path.exists(exact):
            return exact
        # Search all project directories
        if os.path.isdir(projects_dir):
            for d in os.listdir(projects_dir):
                candidate = os.path.join(projects_dir, d, fname)
                if os.path.exists(candidate):
                    return candidate
        return None

    def query(self, prompt: str, display_prompt: str = None, silent: bool = False) -> None:
        """
        Start a new query.

        Args:
            prompt: The full prompt to send to the agent
            display_prompt: Optional shorter prompt to display in the UI (defaults to prompt)
            silent: If True, skip UI updates (for channel mode)
        """
        if not self.client or not self.initialized:
            sublime.error_message("Claude not initialized")
            return

        self.working = True
        self.query_count += 1
        self.draft_prompt = ""  # Clear draft — query submitted
        self._input_mode_entered = False  # Reset so input mode can be entered when query completes

        # Mark this session as the currently executing session for MCP tools
        # MCP tools should operate on the executing session, not the UI-active session
        # Only set if not already set (don't overwrite parent session when spawning subsessions)
        self._is_executing_session = False  # Track if we set the marker
        if self.output.view and not self.window.settings().has("claude_executing_view"):
            self.window.settings().set("claude_executing_view", self.output.view.id())
            self._is_executing_session = True
        # Build prompt with context (may include images)
        full_prompt, images = self._build_prompt_with_context(prompt)
        context_names = [item.name for item in self.pending_context]
        self.pending_context = []  # Clear after use
        self._update_context_display()

        # Store images for RPC call
        self._pending_images = images

        # Use display_prompt for UI if provided, otherwise use full prompt
        ui_prompt = display_prompt if display_prompt else prompt

        # Check if bridge is alive before sending
        if not self.client.is_alive():
            self._status("error: bridge died")
            if not silent:
                self.output.text("\n\n*Bridge process died. Please restart the session.*\n")
            return

        if not silent:
            self.output.show()
            # Auto-name session from first prompt if not already named
            if not self.name:
                self._set_name(ui_prompt[:30].strip() + ("..." if len(ui_prompt) > 30 else ""))
            self.output.prompt(ui_prompt, context_names)
            self._animate()
        query_params = {"prompt": full_prompt}
        if hasattr(self, '_pending_images') and self._pending_images:
            query_params["images"] = self._pending_images
            self._pending_images = []
        if not self.client.send("query", query_params, self._on_done):
            self._status("error: bridge died")
            self.working = False
            self.output.text("\n\n*Failed to send query. Bridge process died.*\n")

    def send_message_with_callback(self, message: str, callback: Callable[[str], None], silent: bool = False, display_prompt: str = None) -> None:
        """Send message and call callback with Claude's response.

        Used by channel mode for sync request-response communication.

        Args:
            message: The message to send to Claude
            callback: Function to call with the response text when complete
            silent: If True, skip UI updates
            display_prompt: Optional display text for UI (ignored if silent=True)
        """
        # Validate session state before setting callback
        if not self.client or not self.initialized:
            print(f"[Claude] send_message_with_callback: session not initialized")
            callback("Error: session not initialized")
            return
        if not self.client.is_alive():
            print(f"[Claude] send_message_with_callback: bridge not running")
            callback("Error: bridge not running")
            return

        print(f"[Claude] send_message_with_callback: sending message")
        self._response_callback = callback
        ui_prompt = display_prompt if display_prompt else (message[:50] + "..." if len(message) > 50 else message)
        self.query(message, display_prompt=ui_prompt, silent=silent)

        # Check if query() failed (working is False if send failed)
        if not self.working and self._response_callback:
            print(f"[Claude] send_message_with_callback: query failed, calling callback with error")
            cb = self._response_callback
            self._response_callback = None
            cb("Error: failed to send query")

    def _on_done(self, result: dict) -> None:
        self.current_tool = None

        # Clear executing session marker - MCP tools should no longer target this session
        if self.output.view and getattr(self, '_is_executing_session', False):
            self.window.settings().erase("claude_executing_view")
            self._is_executing_session = False

        # 1. Determine completion type
        if "error" in result:
            completion = "error"
        elif result.get("status") == "interrupted":
            completion = "interrupted"
        else:
            completion = "success"

        # 2. Handle UI for each completion type
        if completion == "error":
            error_msg = result['error'].get('message', str(result['error'])) if isinstance(result['error'], dict) else str(result['error'])
            self._status("error")
            self.output.text(f"\n\n*Error: {error_msg}*\n")
            if self.output.current:
                self.output.current.working = False
                self.output._render_current()
        elif completion == "interrupted":
            self._status("interrupted")
            self.output.interrupted()
        else:
            self._status("ready")

        self.output.set_name(self.name or "Claude")
        self.output.clear_all_permissions()

        # 3. Response callback fires for ALL completions (channel mode needs to know)
        if self._response_callback:
            callback = self._response_callback
            self._response_callback = None
            response_text = ""
            if self.output.current:
                response_text = "".join(self.output.current.text_chunks)
            try:
                callback(response_text)
            except Exception as e:
                print(f"[Claude] response callback error: {e}")

        # Notify subsession completion (for notalone2)
        if self.output.view:
            view_id = str(self.output.view.id())
            for session in sublime._claude_sessions.values():
                if session.client:
                    session.client.send("subsession_complete", {"subsession_id": view_id})

        # 4. Check for pending retain (interrupt was triggered by compact_boundary)
        if completion == "interrupted" and self._pending_retain:
            retain_content = self._pending_retain
            self._pending_retain = None
            self.output.text(f"\n◎ [retain] ▶\n\n")
            self.query(retain_content, display_prompt="[retain context]")
            return

        # 5. GATE: Only process deferred actions on success
        if completion != "success":
            self.working = False
            self._clear_deferred_state()
            sublime.set_timeout(lambda: self._enter_input_with_draft() if not self.working else None, 100)
            return

        # 5. Process queued prompts (keep working=True, animation continues)
        if self._queued_prompts:
            prompt = self._queued_prompts.pop(0)
            self.output.text(f"\n**[queued]** {prompt}\n\n")
            self.query(prompt)
            return

        # 6. Clear inject_pending - if inject was mid-query, it's done now
        # If inject was queued, queued_inject notification will start new query
        self._inject_pending = False

        # 7. Now set working=False and enter input mode
        self.working = False
        sublime.set_timeout(lambda: self._enter_input_with_draft() if not self.working else None, 100)

    def _clear_deferred_state(self) -> None:
        """Clear deferred action state. Called on error/interrupt."""
        self._queued_prompts.clear()
        self._inject_pending = False
        self._pending_retain = None
        self._input_mode_entered = False  # Allow re-entry to input mode

    def _enter_input_with_draft(self) -> None:
        """Enter input mode and restore draft with cursor at end."""
        # Skip if already in input mode or session is working
        if self.output.is_input_mode() or self.working:
            return

        # Skip if we've already entered input mode after the last query
        # This prevents duplicate entries from multiple callers (on_activated, _on_done, etc.)
        if self._input_mode_entered:
            return

        self.output.enter_input_mode()

        # Check if enter_input_mode actually succeeded (might have deferred)
        if not self.output.is_input_mode():
            return

        self._input_mode_entered = True

        if self.draft_prompt and self.output.view:
            self.output.view.run_command("append", {"characters": self.draft_prompt})
            end = self.output.view.size()
            self.output.view.sel().clear()
            self.output.view.sel().add(sublime.Region(end, end))

    def queue_prompt(self, prompt: str) -> None:
        """Inject a prompt into the current query stream."""
        self._status(f"injected: {prompt[:30]}...")

        if self.working and self.client:
            # Mid-query: show prompt and inject via bridge
            short = prompt[:100] + "..." if len(prompt) > 100 else prompt
            self.output.text(f"\n◎ [injected] {short} ▶\n\n")
            self._inject_pending = True  # Don't show "done" until inject query completes
            self.client.send("inject_message", {"message": prompt})
        elif self.client:
            # Not working: start query directly (no round-trip delay)
            self.query(prompt)
        else:
            # No client - queue locally for later
            self._queued_prompts.append(prompt)

    def show_queue_input(self) -> None:
        """Show input panel to queue a prompt while session is working."""
        if not self.working:
            # Not working, just enter normal input mode
            self._enter_input_with_draft()
            return

        def on_done(text: str) -> None:
            text = text.strip()
            if text:
                self.queue_prompt(text)

        self.window.show_input_panel(
            "Queue prompt:",
            self.draft_prompt,
            on_done,
            None,  # on_change
            None   # on_cancel
        )

    def interrupt(self, break_channel: bool = True) -> None:
        """Interrupt current query.

        Args:
            break_channel: If True, also breaks any active channel connection.
                          Set to False when interrupt is from channel message.
        """
        if self.client:
            sent = self.client.send("interrupt", {})
            self._status("interrupting...")
            # Don't set working=False here — wait for _on_done to confirm
            # the bridge actually stopped. This prevents input mode race.
            self._queued_prompts.clear()
            # If bridge is dead, _on_done won't fire — force cleanup
            if not sent:
                self.working = False
                self._status("error: bridge died")
                self.output.text("\n\n*Bridge process died. Please restart the session.*\n")
                self._enter_input_with_draft()

        # Break any active channel connection (only for user-initiated interrupts)
        if break_channel and self.output.view:
            from . import notalone
            notalone.interrupt_channel(self.output.view.id())

    def stop(self) -> None:
        # Release persona if acquired
        if self.persona_session_id and self.persona_url:
            self._release_persona()

        if self.client:
            self.client.send("shutdown", {}, lambda _: self.client.stop())
        self._clear_status()

        # Release accumulated state
        if self.output:
            self.output.conversations.clear()
        self.pending_context.clear()
        self._queued_prompts.clear()

    def _release_persona(self) -> None:
        """Release acquired persona."""
        import threading
        from . import persona_client

        session_id = self.persona_session_id
        persona_url = self.persona_url

        def release():
            result = persona_client.release_persona(session_id, base_url=persona_url)
            if "error" not in result:
                print(f"[Claude] Released persona for session {session_id}")

        threading.Thread(target=release, daemon=True).start()

    # ─── Notification Tools ───────────────────────────────────────────────
    # Notification tools are provided by dedicated MCP servers:
    # - notalone2 daemon: timers, session completion, list/unregister
    # - vibekanban MCP server: watch_kanban for ticket state changes

    def _on_notification(self, method: str, params: dict) -> None:
        if method == "permission_request":
            self._handle_permission_request(params)
            return

        if method == "question_request":
            self._handle_question_request(params)
            return

        if method == "plan_mode_enter":
            self._handle_plan_mode_enter(params)
            return

        if method == "plan_mode_exit":
            self._handle_plan_mode_exit(params)
            return

        if method == "plan_response":
            # Response handled via pending_plan_approvals in bridge
            return

        if method == "queued_inject":
            message = params.get("message", "")
            if message:
                self._inject_pending = False
                self.working = True
                self.query(message)
            return

        if method == "notification_wake":
            # Notification fired - start a new query with the wake prompt
            wake_prompt = params.get("wake_prompt", "")
            display_message = params.get("display_message", "")  # User-friendly message
            notification_id = params.get("notification_id", "")

            # Use display_message for user (concise), wake_prompt goes to agent (detailed)
            # If no display_message, extract first line of wake_prompt
            if display_message:
                user_message = display_message
            else:
                # Extract first meaningful line for display
                first_line = wake_prompt.split("\n")[0].strip() if wake_prompt else ""
                user_message = first_line if first_line else "🔔 Notification received"

            # If session is still working, queue the wake query for when it becomes idle
            if self.working:

                def start_wake_query():
                    if not self.working:
                        try:
                            self.query(wake_prompt, display_prompt=user_message)
                        except Exception as e:
                            print(f"[Claude] deferred wake query error: {e}")
                    else:
                        # Still working, try again later
                        sublime.set_timeout(start_wake_query, 500)

                sublime.set_timeout(start_wake_query, 500)
                return

            # Session is idle, start wake query immediately
            try:
                self.query(wake_prompt, display_prompt=user_message)
            except Exception as e:
                print(f"[Claude] wake query error: {e}")
            return

        if method != "message":
            return

        t = params.get("type")
        if t == "tool_use":
            # Mark previous tool as done if any (skip if empty/anonymous)
            if self.current_tool and self.current_tool.strip():
                self.output.tool_done(self.current_tool)
            self.current_tool = params.get("name", "")

            # Skip anonymous/empty tool_use notifications
            if not self.current_tool or not self.current_tool.strip():
                self.current_tool = None
                return

            tool_input = params.get("input", {})
            self.output.tool(self.current_tool, tool_input)
        elif t == "tool_result":
            # Skip anonymous/empty tool results
            if not self.current_tool or not self.current_tool.strip():
                self.current_tool = None
                return

            content = params.get("content", "")
            # Convert content to string if it's a list
            if isinstance(content, list):
                content = "\n".join(str(c) for c in content)
            # Truncate large results (e.g., image base64) — only used for display summaries
            if len(content) > 10000:
                content = content[:10000]
            is_error = params.get("is_error")

            # Track Edit/Write for edit log
            if self.current_tool in ("Edit", "Write") and not is_error:
                self._record_edit(self.current_tool)

            if is_error:
                self.output.tool_error(self.current_tool, content)
            else:
                self.output.tool_done(self.current_tool, content)
            self.current_tool = None
        elif t in ("text_delta", "text"):
            self.output.text(params.get("text", ""))
        elif t == "turn_usage":
            usage = params.get("usage", {})
            if usage:
                self.context_usage = usage
                self._update_status_bar()
        elif t == "result":
            # Capture session ID for resume
            if params.get("session_id"):
                self.session_id = params["session_id"]
                self._save_session()
            cost = params.get("total_cost_usd") or 0
            self.total_cost += cost
            dur = params.get("duration_ms", 0) / 1000
            usage = params.get("usage")
            if usage:
                self.context_usage = usage
            print(f"[Claude] [{dur:.1f}s, ${cost:.4f}]" if cost else f"[Claude] [{dur:.1f}s]")
            if usage:
                print(f"[Claude] usage: {usage}")
            self.output.meta(dur, cost, usage=usage)
            self._update_status_bar()
        elif t == "system":
            subtype = params.get("subtype", "")
            data = params.get("data", {})
            if subtype == "compact_boundary":
                self.context_usage = None
                self._update_status_bar()
                self._inject_retain_midquery()

    def _set_name(self, name: str) -> None:
        """Set session name and update UI."""
        self.name = name
        self.output.set_name(name)
        self._update_status_bar()
        self._save_session()

    def _save_session(self) -> None:
        """Save session info to disk for later resume."""
        if not self.session_id:
            return
        sessions = load_saved_sessions()
        # Update or add this session — always move to front (most recently active)
        entry = None
        for i, s in enumerate(sessions):
            if s.get("session_id") == self.session_id:
                entry = sessions.pop(i)
                break
        if not entry:
            entry = {"session_id": self.session_id}
        entry["name"] = self.name
        entry["project"] = self._cwd()
        entry["total_cost"] = self.total_cost
        entry["query_count"] = self.query_count
        if self._pending_resume_at:
            entry["resume_session_at"] = self._pending_resume_at
        else:
            entry.pop("resume_session_at", None)
        sessions.insert(0, entry)
        # Keep last 200 sessions
        sessions = sessions[:200]
        save_sessions(sessions)

    def _status(self, text: str) -> None:
        """Update status on output view only."""
        if not self.output.view or not self.output.view.is_valid():
            return
        label = self.backend.title() if self.backend != "claude" else "Claude"
        prefix = "[PLAN] " if self.plan_mode else ""
        parts = [f"{prefix}{text}"]
        if self.name:
            parts.append(self.name)
        if self.total_cost > 0:
            parts.append(f"${self.total_cost:.4f}")
        if self.query_count > 0:
            parts.append(f"{self.query_count}q")
        if self.context_usage:
            ctx_k = self._context_tokens_k()
            if ctx_k is not None:
                parts.append(f"ctx:{ctx_k}k")
        self.output.view.set_status("claude", f"{label}: {', '.join(parts)}")

    def _update_status_bar(self) -> None:
        """Update status bar with session info."""
        self._status("ready")

    def _context_tokens_k(self) -> Optional[int]:
        """Get context token count in thousands from latest usage data."""
        if not self.context_usage:
            return None
        u = self.context_usage
        input_t = (u.get("input_tokens", 0)
                 + u.get("cache_read_input_tokens", 0)
                 + u.get("cache_creation_input_tokens", 0))
        if not input_t:
            return None
        return max(1, input_t // 1000)

    def _clear_status(self) -> None:
        if self.output.view and self.output.view.is_valid():
            self.output.view.erase_status("claude")

    def _animate(self) -> None:
        if not self.working:
            # Restore normal title when done
            self.output.set_name(self.name or "Claude")
            return
        chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        s = chars[self.spinner_frame % len(chars)]
        self.spinner_frame += 1
        # Show spinner in status bar only (not title - causes cursor flicker)
        status = self.current_tool or "thinking..."
        self._status(f"{s} {status}")
        # Animate spinner in output view
        self.output.advance_spinner()
        sublime.set_timeout(self._animate, 200)

    def _handle_permission_request(self, params: dict) -> None:
        """Handle permission request from bridge - show in output view."""
        from .output import PERM_ALLOW

        pid = params.get("id")
        tool = params.get("tool", "Unknown")
        tool_input = params.get("input", {})
        def on_response(response: str) -> None:
            if self.client:
                allow = (response == PERM_ALLOW)
                if not allow:
                    # Mark tool as error immediately - SDK won't send tool_result for denied
                    self.output.tool_error(tool)
                    self.current_tool = None
                self.client.send("permission_response", {
                    "id": pid,
                    "allow": allow,
                    "input": tool_input if allow else None,
                    "message": None if allow else "User denied permission",
                })

        # Show permission UI in output view
        self.output.permission_request(pid, tool, tool_input, on_response)

    def _handle_question_request(self, params: dict) -> None:
        """Handle AskUserQuestion from Claude - show inline question UI."""
        qid = params.get("id")
        questions = params.get("questions", [])
        if not questions:
            if self.client:
                self.client.send("question_response", {"id": qid, "answers": {}})
            return

        def on_done(answers):
            if self.client:
                self.client.send("question_response", {"id": qid, "answers": answers})

        self.output.question_request(qid, questions, on_done)

    # ─── Plan Mode ─────────────────────────────────────────────────────

    def _handle_plan_mode_enter(self, params: dict) -> None:
        """Handle entering plan mode."""
        self.plan_mode = True
        self._status("plan mode")

    def _handle_plan_mode_exit(self, params: dict) -> None:
        """Handle exiting plan mode - show inline approval UI."""
        from .output import PLAN_APPROVE
        plan_id = params.get("id")
        tool_input = params.get("tool_input", {})

        # Find the most recent plan file
        plan_file = self._find_plan_file()
        self.plan_file = plan_file
        allowed_prompts = tool_input.get("allowedPrompts", [])

        def on_response(response: str):
            approved = response == PLAN_APPROVE
            self.plan_mode = False

            if self.client:
                self.client.send("plan_response", {
                    "id": plan_id,
                    "approved": approved,
                })

            if approved:
                self._status("implementing...")
            else:
                self._status("ready")

        # Show inline approval block (like permission UI)
        self.output.plan_approval_request(
            plan_id=plan_id,
            plan_file=plan_file or "",
            allowed_prompts=allowed_prompts,
            callback=on_response,
        )

        # Open plan file if found
        if plan_file and os.path.exists(plan_file):
            view = self.window.open_file(plan_file)
            def enable_wrap(v=view):
                if v.is_loading():
                    sublime.set_timeout(lambda: enable_wrap(v), 100)
                    return
                v.settings().set("word_wrap", True)
            enable_wrap()

    def _find_plan_file(self) -> Optional[str]:
        """Find the most recent plan file in ~/.claude/plans/."""
        import glob
        plans_dir = os.path.expanduser("~/.claude/plans")
        if not os.path.exists(plans_dir):
            return None

        plan_files = glob.glob(os.path.join(plans_dir, "*.md"))
        if not plan_files:
            return None

        return max(plan_files, key=os.path.getmtime)

    # ─── Notification API (notalone2) ──────────────────────────────────

    def subscribe_to_service(
        self,
        notification_type: str,
        params: dict,
        wake_prompt: str
    ) -> dict:
        """Subscribe to a service - handles HTTP endpoints for channel services.

        This is synchronous and returns a result dict.
        """
        import urllib.request
        import json as json_mod
        import socket as sock_mod

        # First, get services list synchronously from notalone
        try:
            sock = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(os.path.expanduser("~/.notalone/notalone.sock"))
            sock.sendall((json_mod.dumps({"method": "services"}) + "\n").encode())
            response = sock.recv(65536).decode().strip()
            sock.close()
            services_data = json_mod.loads(response)
            services = services_data.get("services", [])
        except Exception as e:
            print(f"[Claude] Failed to get services: {e}")
            services = []

        # Check if this is a channel service with an endpoint
        endpoint = None
        for svc in services:
            if svc.get("type") == notification_type and svc.get("endpoint"):
                endpoint = svc.get("endpoint")
                break

        # If it has an endpoint, POST to it first
        if endpoint:
            view_id = self.output.view.id() if self.output and self.output.view else 0
            session_id = params.get("session_id", f"sublime.{view_id}")
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=json_mod.dumps({"session_id": session_id}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    result = resp.read().decode()
                    print(f"[Claude] Subscribed to {notification_type}: {result}")
            except Exception as e:
                print(f"[Claude] Failed to subscribe to {notification_type}: {e}")
                return {"error": str(e)}

        # Now register with notalone daemon (simple version, no callback)
        if self.client:
            self.client.send("register_notification", {
                "notification_type": notification_type,
                "params": params,
                "wake_prompt": wake_prompt
            })
        view_id = self.output.view.id() if self.output and self.output.view else 0
        return {"ok": True, "notification_type": notification_type, "session_id": params.get("session_id", f"sublime.{view_id}")}

    def register_notification(
        self,
        notification_type: str,
        params: dict,
        wake_prompt: str,
        notification_id: Optional[str] = None,
        callback: Optional[callable] = None
    ) -> None:
        """Register a notification via notalone2 daemon.

        Args:
            notification_type: 'timer', 'subsession_complete', 'ticket_update', 'channel'
            params: Type-specific parameters
            wake_prompt: Prompt to inject when notification fires
            notification_id: Optional custom notification ID
            callback: Optional callback for result
        """
        if not self.client:
            return

        self.client.send("register_notification", {
            "notification_type": notification_type,
            "params": params,
            "wake_prompt": wake_prompt,
            "notification_id": notification_id
        }, callback)

    def signal_subsession_complete(
        self,
        result_summary: Optional[str] = None,
        callback: Optional[callable] = None
    ) -> None:
        """Signal that this subsession has completed.

        Args:
            result_summary: Optional summary of what was accomplished
            callback: Optional callback for result
        """
        if not self.client:
            return

        self.client.send("signal_subsession_complete", {
            "result_summary": result_summary
        }, callback)
