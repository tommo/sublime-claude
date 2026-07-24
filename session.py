"""Claude Code session management."""
import datetime
import json
import os
import time
from typing import Optional, List, Dict, Callable, Any

import sublime

from .rpc import JsonRpcClient
from .output import OutputView
from .constants import BACKGROUND_PREFIX
from . import backends
from .context_manager import ContextManager, ContextItem  # ContextItem re-exported for back-compat
from . import cc_launch


SESSIONS_FILE = os.path.join(os.path.dirname(__file__), ".sessions.json")

# PhantomSet keys owned by ClaudeCode.
#
# Critical: PhantomSet objects must stay alive. On package reload, Session
# drops its refs → PhantomSet GC → ST can *orphan* LAYOUT_BLOCK phantoms on
# the view (sleep banner stacks once per reload). Keys alone are not enough
# if the set is collected. Registry lives on the `sublime` module so it
# survives soft package reload.
CLAUDE_PHANTOM_KEYS = (
    "claude_overlay",        # sleep / connecting / terminal banner
    "claude_queue",          # sticky queue hairline
    "claude_permmode",       # permission-mode banner
    "claude_wakeup",         # cron / loop wakeup ticker
    "claude_workflow",       # workflow strip
    "claude_media",          # inline image previews (output_view)
    "claude_context",        # 📎 chips (output_view)
    "claude_pad",            # legacy pad key
    "claude_composer_pad",   # composer pad under ◎ (output_view real key)
)


def _phantom_registry() -> dict:
    reg = getattr(sublime, "_claude_phantom_registry", None)
    if not isinstance(reg, dict):
        reg = {}
        sublime._claude_phantom_registry = reg  # type: ignore[attr-defined]
    return reg


def keyed_phantom_set(view: sublime.View, key: str):
    """Return the single long-lived PhantomSet for (view, key).

    Always reuse the same object so update([]) / update([…]) replace phantoms
    instead of stacking orphans across package reloads.
    """
    if not view or not view.is_valid() or not key:
        return None
    reg = _phantom_registry()
    k = (view.id(), key)
    ps = reg.get(k)
    if ps is not None:
        return ps
    try:
        ps = sublime.PhantomSet(view, key)
    except Exception:
        return None
    reg[k] = ps
    return ps


def clear_claude_view_phantoms(view: sublime.View, keys=None) -> int:
    """Empty Claude phantoms on a view (registry set + erase_phantoms).

    ``view.erase_phantoms(key)`` is required for orphans left after PhantomSet
    GC on package reload — ``PhantomSet.update([])`` alone cannot touch them
    once the original set is gone, which produced stacked sleep banners.
    """
    if not view or not view.is_valid():
        return 0
    n = 0
    for key in (keys or CLAUDE_PHANTOM_KEYS):
        try:
            if hasattr(view, "erase_phantoms"):
                view.erase_phantoms(key)
            ps = keyed_phantom_set(view, key)
            if ps is not None:
                ps.update([])
            n += 1
        except Exception:
            pass
    return n


def clear_all_claude_phantoms() -> int:
    """Clear phantoms on every Claude output sheet (plugin load / unload)."""
    total = 0
    try:
        for w in sublime.windows():
            for v in w.views():
                if not v.settings().get("claude_output"):
                    continue
                total += clear_claude_view_phantoms(v)
    except Exception as e:
        print(f"[Claude] clear_all_claude_phantoms: {e}")
    return total


def prune_phantom_registry() -> None:
    """Drop registry entries for closed views (keep sets for live sheets)."""
    reg = _phantom_registry()
    live = set()
    try:
        for w in sublime.windows():
            for v in w.views():
                live.add(v.id())
    except Exception:
        return
    dead = [k for k in reg if k[0] not in live]
    for k in dead:
        try:
            reg[k].update([])
        except Exception:
            pass
        reg.pop(k, None)


# @suffix → max context tokens (stripped from model ID before sending to bridge)
_CONTEXT_LIMITS = {
    "@400k": 400000,
    "@200k": 200000,
}

def _resolve_model_id(model_id: str):
    """Resolve virtual model ID. Returns (real_model_id, max_context_tokens or None)."""
    if not model_id:
        return model_id, None
    for suffix, tokens in _CONTEXT_LIMITS.items():
        if model_id.endswith(suffix):
            return model_id[:-len(suffix)], tokens
    return model_id, None


def _bookmarks_path(project_path: str = None) -> str:
    if project_path:
        return os.path.join(project_path, ".claude", "bookmarks.json")
    return os.path.expanduser("~/.claude/bookmarks.json")


def load_bookmarks(project_path: str = None) -> set:
    """Load starred session IDs for a project."""
    path = _bookmarks_path(project_path)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return set(json.load(f).get("starred", []))
        except Exception:
            pass
    return set()


def save_bookmarks(starred: set, project_path: str = None) -> None:
    path = _bookmarks_path(project_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w") as f:
            json.dump({"starred": list(starred)}, f, indent=2)
    except Exception as e:
        print(f"[Claude] Failed to save bookmarks: {e}")


def toggle_bookmark(session_id: str, project_path: str = None) -> bool:
    """Toggle star for a session. Returns True if now starred."""
    starred = load_bookmarks(project_path)
    if session_id in starred:
        starred.discard(session_id)
        now_starred = False
    else:
        starred.add(session_id)
        now_starred = True
    save_bookmarks(starred, project_path)
    return now_starred


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


# NOTE: ContextItem moved to context_manager.py and re-exported above for callers


class Session:
    def __init__(self, window: sublime.Window, resume_id: Optional[str] = None, fork: bool = False, profile: Optional[Dict] = None, initial_context: Optional[Dict] = None, backend: str = "claude"):
        self.window = window
        self.backend = backend
        self.client: Optional[JsonRpcClient] = None
        self.output = OutputView(window)
        self.initialized = False
        self.working = False
        self.is_looping = False  # agent armed a self-wake/cron → title shows ↻ until manual takeover
        self.next_wake_at: Optional[float] = None  # epoch of the pending self-wake (for the wakeup banner)
        # Quick Agent: lives in a bottom panel (not a doc tab). Lightweight
        # trivia/Q&A — no session resume list, no inline input region.
        self.quick_mode: bool = False
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
        self.effort: Optional[str] = None  # Resolved reasoning effort for this session
        self.name: Optional[str] = None
        self.total_cost: float = 0.0
        self.query_count: int = 0
        self.context_usage: Optional[Dict] = None  # Latest usage/context stats
        # Plugin-level auto-retry on is_error turn results (opt-in via
        # auto_retry_turns). _auto_retry_count resets on each user-submitted
        # query; _auto_retry_pending lets query() cancel a scheduled retry.
        self._auto_retry_count: int = 0
        self._auto_retry_pending: bool = False
        # Monotonic turn id: stale bridge _on_done (late interrupt after a
        # queued follow-up already started) must not clobber the new turn.
        self._query_gen: int = 0
        # Pending context for next query (delegated to ContextManager)
        self.context = ContextManager(self)
        # Profile docs available for reading (paths only, not content)
        self.profile_docs: List[str] = []
        # Draft prompt (persists across input panel open/close)
        self.draft_prompt: str = ""
        # Last path the user previewed / pinned for edits (image_edit, code).
        # Set from media enlarge/edit links and from opening paths in the transcript.
        self.edit_target: Optional[str] = None
        self._pending_resume_at: Optional[str] = None  # Set by undo, consumed by next query
        # Background task tracking: task_id → tool_use_id
        self._task_tool_map: Dict[str, str] = {}
        # Tool-use IDs we know were started with run_in_background=true,
        # mapped to the ToolCall object. Authoritative for "should this
        # task_notification fire a wake?" — survives conversation-history
        # truncation (HISTORY_CAP), and the held ToolCall reference keeps
        # the object alive so the ⚙ → ✓/✗ symbol patch can still happen
        # after the owning Conversation has been dropped.
        self._bg_tools: Dict[str, Any] = {}
        # Tool-use ids known to be run_in_background — the agent-wake gate. Kept
        # separate from _bg_tools (the visual registry) so the reliable
        # task_updated cleanup can finalize the ⚙ line without suppressing the
        # later task_notification wake.
        self._bg_task_ids: set = set()
        # Task ids we've seen in the bridge's running set — so reconciliation
        # only finalizes a bg task that was observed running and then vanished
        # (a missed terminal event), never one that simply hasn't started yet.
        self._seen_running: set = set()
        # Workflow (ultracode) live state: task_id -> {wp, summary, sig}. The
        # bridge forwards task_progress every tick with a workflow_progress[]
        # tree; we render it as a live panel.
        self._workflows: Dict[str, Any] = {}
        self._workflow_phantom_set = None
        self._workflow_views: Dict[str, int] = {}  # task_id -> dedicated detail view id
        self._workflow_view_ps: Dict[str, Any] = {}  # task_id -> PhantomSet for the detail view
        # Track if we've entered input mode after last query
        self._input_mode_entered: bool = False
        # Callback for channel mode responses
        self._response_callback: Optional[Callable[[str], None]] = None
        # Queue of prompts to send after current query completes
        self._queued_prompts: List[str] = []
        # Track if inject was sent (to skip "done" status until inject query completes)
        self._inject_pending: bool = False
        # Buffer for coalescing background-task notifications. Multiple bg tasks
        # finishing close together are combined into a single wake to avoid
        # spamming the conversation and racing with user input.
        self._pending_bg_notifications: List[str] = []
        self._bg_flush_scheduled: bool = False
        self._bg_poll_timer = None  # sublime.set_timeout handle for between-query bg task polling

        # Extract subsession_id and parent_view_id if provided
        if initial_context:
            self.subsession_id = initial_context.get("subsession_id")
            self.parent_view_id = initial_context.get("parent_view_id")
            # Host goal skeptic subsession (never owns the goal tracker)
            self.goal_role = (initial_context.get("goal_role") or "") or None
        else:
            self.subsession_id = None
            self.parent_view_id = None
            self.goal_role = None

        # Persona info (for release on close)
        if profile:
            self.persona_id = profile.get("persona_id")
            self.persona_session_id = profile.get("persona_session_id")
            self.persona_url = profile.get("persona_url")
        else:
            self.persona_id = None
            self.persona_session_id = None
            self.persona_url = None

        # Activity tracking for auto-sleep
        self.last_activity: float = time.time()
        self.last_idle_at: float = 0  # set when session enters input mode (truly idle)
        self.sleep_disabled: bool = False  # per-session auto-sleep disable toggle
        # Turn/bridge failed while idle — tab shows ⚠ until next query.
        self.error_halted: bool = False
        self.error_halt_message: str = ""

        # Terminal mode state
        self.terminal_mode: bool = False
        self._terminal_tag: Optional[str] = None
        self._terminal_poll_active: bool = False

        # Plan mode state
        self.plan_mode: bool = False
        self.plan_file: Optional[str] = None

        # Host-owned goal harness (native /goal — all backends)
        from .goal_tracker import GoalTracker
        self.goal_tracker = GoalTracker()
        self._goal_verify_turn: bool = False  # legacy same-session flag (unused for host skeptic)
        self._goal_planning_turn: bool = False  # planner writing plan.md
        self._goal_harness_continue: bool = False  # suppress user-queue steal? no
        self._goal_skip_continue_once: bool = False  # after user interrupt
        # Verify mode: "task" (in-session Task POC) | "session" (legacy sheet)
        self._goal_verify_mode: Optional[str] = None
        self._goal_skeptic_view_id: Optional[int] = None  # legacy sheet only
        self._goal_verify_awaiting: bool = False

        # Composer allowed only after live init/wake — never during restore/sleep.
        # Prevents enter_input_mode from flashing ◎ on package load / ST restart.
        self._composer_allowed: bool = True

        # Pending retain content (set by compact_boundary, sent after interrupt)
        self._pending_retain: Optional[str] = None

    def start(self, resume_session_at: str = None) -> None:
        # Live bridge starting — still no ◎ until _on_init succeeds
        self._composer_allowed = False
        self._show_connecting_phantom()

        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        python_path = settings.get("python_path", "python3")

        # Build profile docs list early (before init) so we can add to system prompt
        self._build_profile_docs_list()

        # Load environment variables from settings and profile
        env = self._load_env(settings)

        # Resolve backend spec — single source of truth for bridge script,
        # fallback model, env overrides, etc.
        spec = backends.get(self.backend)

        # Resolve virtual model ID (e.g. @400k suffix) → real model + context limit
        default_models = settings.get("default_models", {})
        default_model = default_models.get(self.backend) or spec.fallback_model or settings.get("default_model")
        model_for_env = (self.profile.get("model") if self.profile else None) or default_model
        if model_for_env:
            _, ctx = _resolve_model_id(model_for_env)
            if ctx:
                env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(ctx)

        # Stash provider label + resolved model on the view so the output renderer
        # can show them on the @done(…) meta line without reaching into the session.
        # Also stamp session_id early so ST restart can reconnect before next save.
        if self.output and self.output.view:
            self.output.view.settings().set("claude_backend", self.backend)
            self.output.view.settings().set("claude_provider_label", spec.label or self.backend)
            if model_for_env:
                self.output.view.settings().set("claude_model", model_for_env)
            # Effort may be refined below; placeholder until init_params resolve.
            if getattr(self, "effort", None):
                self.output.view.settings().set("claude_effort", self.effort)
            try:
                self._persist_view_identity()
            except Exception:
                pass

        # Diagnostic: log resolved spawn config (subsession-vs-standalone matters here)
        _is_subsession = bool(getattr(self, "subsession_id", None))
        print(f"[Claude session] backend={self.backend} bridge={spec.bridge_script} "
              f"subsession={'yes' if _is_subsession else 'no'} "
              f"resume={self.resume_id!r} fork={self.fork} "
              f"default_model={default_model!r} model_for_env={model_for_env!r}")

        # Sync sublime project retain content to file for hook
        self._sync_project_retain()

        # Apply backend-specific env. static_env is always defaults; dynamic_env
        # returns (overwrite, defaults) — overwrite always wins, defaults use setdefault.
        for k, v in spec.static_env.items():
            env.setdefault(k, v)
        if spec.dynamic_env is not None:
            # Pass a full settings snapshot so custom providers can read their
            # own config (custom_providers[name]); deepseek_api_key kept for
            # back-compat with any dynamic_env that still reads it.
            settings_dict = {
                "custom_providers": settings.get("custom_providers", {}) or {},
                "deepseek_api_key": settings.get("deepseek_api_key"),
            }
            overwrite, defaults = spec.dynamic_env(settings_dict)
            env.update(overwrite)
            for k, v in defaults.items():
                env.setdefault(k, v)
            # Diagnostic: which env vars the bridge will receive (mask secrets)
            def _mask(k, v):
                if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                    return f"<set:{len(str(v))}b>" if v else "<empty>"
                return v
            shown = {k: _mask(k, v) for k, v in env.items() if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "DEEPSEEK_"))}
            print(f"[Claude session] env (masked) for {self.backend}: {shown}")

        bridge_script = os.path.join(os.path.dirname(__file__), "bridge", spec.bridge_script)
        self.client = JsonRpcClient(self._on_notification)
        self.client.start([python_path, bridge_script], env=env)
        self._status("connecting...")

        # Quick Agent may pin permission / tools; otherwise same as normal session.
        qa = (settings.get("quick_agent") or {}) if getattr(self, "quick_mode", False) else {}
        if not isinstance(qa, dict):
            qa = {}
        permission_mode = (
            qa.get("permission_mode")
            if qa.get("permission_mode")
            else settings.get("permission_mode", "acceptEdits")
        )
        self.permission_mode = permission_mode
        # In default mode, don't auto-allow any tools - prompt for all
        if isinstance(qa.get("allowed_tools"), list):
            allowed_tools = list(qa.get("allowed_tools") or [])
        elif getattr(self, "quick_mode", False):
            # Narrow default: full MCP made Quick thrash on get_window_summary.
            try:
                from .quick_agent import DEFAULT_QUICK_ALLOWED_TOOLS
                allowed_tools = list(DEFAULT_QUICK_ALLOWED_TOOLS)
            except Exception:
                allowed_tools = [
                    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                    "mcp__sublime__quick_done",
                ]
        elif permission_mode == "default":
            allowed_tools = []
        else:
            allowed_tools = settings.get("allowed_tools", [])

        print(f"[Claude] initialize: permission_mode={permission_mode}, allowed_tools={allowed_tools}, resume={self.resume_id}, fork={self.fork}, profile={self.profile}, default_model={default_model}, subsession_id={getattr(self, 'subsession_id', None)} quick={getattr(self, 'quick_mode', False)}")
        # Get additional working directories from project folders + project settings
        all_folders = self.window.folders()
        secondary_folders = all_folders[1:] if len(all_folders) > 1 else []
        additional_dirs = list(secondary_folders)
        project_data = self.window.project_data() or {}
        project_settings = project_data.get("settings", {})
        extra_dirs = project_settings.get("claude_additional_dirs", [])
        expanded_extras = []
        if extra_dirs:
            expanded_extras = [os.path.expanduser(d) for d in extra_dirs]
            additional_dirs = additional_dirs + expanded_extras
        print(f"[Claude] additional_dirs sources: cwd={all_folders[0] if all_folders else None!r} "
              f"secondary_folders={secondary_folders} "
              f"claude_additional_dirs={expanded_extras} "
              f"→ sending {len(additional_dirs)} dirs: {additional_dirs}")
        init_params = {
            "cwd": self._cwd(),
            "additional_dirs": additional_dirs,
            "allowed_tools": allowed_tools,
            "permission_mode": permission_mode,
            "view_id": str(self.output.view.id()) if self.output and self.output.view else None,
            # Vision MCP tool (mcp__sublime__read_image): auto = Grok ACP only.
            "mcp_enable_read_image": self._mcp_enable_read_image(settings),
        }
        if self.resume_id:
            init_params["resume"] = self.resume_id
            if self.fork:
                init_params["fork_session"] = True
            # Use saved session's project dir as cwd (session may belong to different project)
            # Also restore lightweight UI state (context_usage, plan_file) so it survives sleep
            for saved in load_saved_sessions():
                if saved.get("session_id") == self.resume_id:
                    saved_project = saved.get("project", "")
                    if saved_project and saved_project != init_params["cwd"]:
                        print(f"[Claude] resume: using saved project {saved_project}")
                        init_params["cwd"] = saved_project
                    # Restore optional state — best-effort, ignore parse errors
                    try:
                        if saved.get("context_usage"):
                            self.context_usage = saved.get("context_usage")
                        if saved.get("plan_file"):
                            self.plan_file = saved.get("plan_file")
                    except Exception:
                        pass  # benign: restored state is purely cosmetic
                    break
            if resume_session_at:
                init_params["resume_session_at"] = resume_session_at
        # Pass subsession_id if this is a subsession
        if hasattr(self, 'subsession_id') and self.subsession_id:
            init_params["subsession_id"] = self.subsession_id
        # Effort resolution. Order: profile → provider override → env → global.
        # Claude: on resume omit unless profile/provider pins (keep CLI session).
        # Grok: always pass — agent spawn uses --reasoning-effort (not configurable
        # via session/load alone).
        effort = self._resolve_effort(settings, env, spec)
        self.effort = effort
        if self.backend == "grok" or not self.resume_id:
            init_params["effort"] = effort
        elif self.profile and self.profile.get("effort"):
            init_params["effort"] = effort
        elif getattr(spec, "effort", None):
            init_params["effort"] = effort
        if self.output and self.output.view and effort:
            self.output.view.settings().set("claude_effort", effort)

        # Apply profile config or default model
        if self.profile:
            if self.profile.get("model"):
                real_model, _ = _resolve_model_id(self.profile["model"])
                init_params["model"] = real_model
            if self.profile.get("betas"):
                init_params["betas"] = self.profile["betas"]
            if self.profile.get("pre_compact_prompt"):
                init_params["pre_compact_prompt"] = self.profile["pre_compact_prompt"]
            # Build system prompt with profile docs info
            system_prompt = self.profile.get("system_prompt", "")
            append_system = (self.profile.get("append_system_prompt") or "").strip()
            if self.profile_docs:
                docs_info = f"\n\nProfile Documentation: {len(self.profile_docs)} files available. Use list_profile_docs to see them and read_profile_doc(path) to read their contents."
                system_prompt = system_prompt + docs_info if system_prompt else docs_info.strip()
            if system_prompt:
                # Full replacement (profiles that intentionally override Claude).
                init_params["system_prompt"] = system_prompt
            elif append_system:
                # Quick Agent: keep claude_code preset + CLAUDE.md, append contract.
                init_params["append_system_prompt"] = append_system
            if getattr(self, "quick_mode", False):
                init_params["quick_mode"] = True
        else:
            # No profile - use default_model setting if available
            if default_model:
                real_model, _ = _resolve_model_id(default_model)
                init_params["model"] = real_model
        sent = self.client.send("initialize", init_params, self._on_init)
        if not sent:
            # Bridge died before we could send — simulate an error so _on_init cleans up
            sublime.set_timeout(
                lambda: self._on_init({"error": {"message": "Bridge process died before initialization. Check that the backend CLI is installed and authenticated."}}),
                50
            )

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
            self._clear_overlay_phantom()
            error_msg = result['error'].get('message', str(result['error']))
            print(f"[Claude] init error: {error_msg}")
            self._mark_error_halt(error_msg)
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
            try:
                self.output.set_name(self.name or "Claude")
            except Exception:
                pass
            # Still enter input mode so the user can interact with the error view
            self.working = False
            self._input_mode_entered = False
            self._composer_allowed = True
            if self.output and self.output.view:
                self.output.view.settings().erase("claude_sleeping")
            sublime.set_timeout(lambda: self._enter_input_with_draft() if not self.working else None, 200)
            return
        self._clear_overlay_phantom()
        self.initialized = True
        self.working = False
        self.current_tool = None
        self.last_activity = time.time()
        # Keep _pending_resume_at alive for consecutive undo support
        self._input_mode_entered = False  # Reset for fresh start after init
        # Capture session_id from initialize response (set via --session-id CLI arg).
        # ACP bridges may also send camelCase sessionId — accept both.
        sid = result.get("session_id") or result.get("sessionId")
        if sid:
            self.session_id = sid
            print(f"[Claude] session_id={self.session_id}")
            try:
                self._persist_view_identity()
            except Exception:
                pass
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
        if result.get("resumed"):
            parts.append("resumed")
        if result.get("resume_fallback"):
            parts.append("fresh (load miss)")
            # Soft notice — agent context was not restored (common for ACP
            # agents that only persist named REPL sessions to disk).
            try:
                self.output.text(
                    "\n*Session reopened without agent transcript "
                    "(UI history kept; model starts fresh).*\n"
                )
            except Exception:
                pass
        # Prefer bridge-reported effort (actual agent) when present.
        bridge_effort = result.get("effort")
        if bridge_effort:
            self.effort = str(bridge_effort)
            if self.output and self.output.view:
                self.output.view.settings().set("claude_effort", self.effort)
        if self.effort and self.backend in ("claude", "grok"):
            parts.append(f"effort:{self.effort}")
        if parts:
            self._status(f"ready ({'; '.join(parts)})")
        else:
            self._status("ready")
        # Effort lives on status bar + @done only. Transcript hints stacked on
        # every reconnect — strip any leftovers from earlier plugin versions.
        try:
            self.output.strip_trailing_status_hints()
        except Exception:
            pass
        # Persist "open" state (so plugin_loaded can track which sessions had views).
        # Quick Agent is ephemeral — don't pollute the resume list.
        if not getattr(self, "quick_mode", False):
            self._save_session()
        if getattr(self, "quick_mode", False) and self.output:
            self.output.set_name(self.name or "⚡ Quick")
        # Submit-created session: run the message that started us
        pending = getattr(self, "_quick_pending_prompt", None)
        if getattr(self, "quick_mode", False) and pending:
            self._quick_pending_prompt = None
            self._composer_allowed = True
            if self.output and self.output.view:
                self.output.view.settings().erase("claude_sleeping")
            # Don't open empty ◎ first — query will re-arm sticky composer
            sublime.set_timeout(lambda p=pending: self.query(p), 0)
            return
        # Live agent ready — composer is allowed now (not during restore/sleep)
        self._composer_allowed = True
        if self.output and self.output.view:
            self.output.view.settings().erase("claude_sleeping")
        # Same inline input UX as a normal session
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
        except Exception as e:
            print(f"[Claude] _find_edit_line({file_path!r}) failed: {e}")
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

    # ── Context API delegated to ContextManager (back-compat shims) ──────
    @property
    def pending_context(self) -> List[ContextItem]:
        return self.context.items

    @pending_context.setter
    def pending_context(self, items: List[ContextItem]) -> None:
        # Used by code that does `self.pending_context = []` to reset
        self.context.items = list(items)
        self.context._refresh_display()

    def add_context_file(self, path: str, content: str) -> None:
        self.context.add_file(path, content)

    def add_context_selection(self, path: str, content: str) -> None:
        self.context.add_selection(path, content)

    def add_context_folder(self, path: str) -> None:
        self.context.add_folder(path)

    def add_context_path(self, path: str) -> None:
        """Paste/attach a filesystem path (image → path ref, code → content)."""
        self.context.add_path(path)

    def add_context_image(self, image_data: bytes, mime_type: str) -> None:
        self.context.add_image(image_data, mime_type)

    def set_edit_target(
        self,
        path: str,
        *,
        as_context: bool = True,
        line: int = None,
        announce: bool = True,
    ) -> bool:
        """Pin a path as the session editing target (from media/code preview).

        Used when the user previews a generated image or opens a transcript
        path — next image_edit / edits can target this without re-finding it.
        """
        import os
        path = os.path.abspath(os.path.expanduser((path or "").strip()))
        if not path:
            return False
        self.edit_target = path
        if as_context:
            try:
                # Image → path ref for image_edit/read_image; code → content chip
                self.add_context_path(path)
            except Exception as e:
                print(f"[Claude] edit target context: {e}")
        if announce:
            base = os.path.basename(path)
            loc = f"{base}:{line}" if line and line > 0 else base
            sublime.status_message(f"Edit target: {loc}")
        return True

    def clear_context(self) -> None:
        self.context.clear()

    def remove_context_at(self, index: int) -> bool:
        return self.context.remove_at(index)

    def _update_context_display(self) -> None:
        self.context._refresh_display()

    def _build_prompt_with_context(self, prompt: str) -> tuple:
        return self.context.build_prompt(prompt)

    def undo_message(self) -> None:
        """Undo last conversation turn (message round).

        Claude: JSONL resume_session_at. Grok: async rewind (never blocks UI).
        """
        if not self.session_id:
            return
        if self.working and self.current_tool != "rewinding...":
            return
        if self.backend == "grok":
            # Async: list points then execute last — must not send_wait on UI.
            self._grok_undo_async(prompt_index=None, draft_prompt="")
            return
        rewind_id, undone_prompt = self._find_rewind_point()
        if not rewind_id:
            print(f"[Claude] undo_message: no rewind point found")
            sublime.status_message("No rewind point found")
            return
        self._apply_undo(rewind_id, undone_prompt)

    def _apply_undo(self, rewind_id, undone_prompt: str) -> None:
        """Rewind agent + UI; restore undone_prompt as draft.

        rewind_id is a Claude assistant uuid, or for Grok an int prompt_index
        (or str digits).
        """
        if self.backend == "grok":
            self._grok_undo_async(prompt_index=rewind_id, draft_prompt=undone_prompt or "")
            return
        saved_id = self.session_id
        print(f"[Claude] undo: rewinding {saved_id} to {rewind_id}")
        self._strip_view_from_last_prompt()
        if self.client:
            self.client.stop()
            self.client = None
        self.initialized = False
        self.session_id = saved_id
        self.resume_id = saved_id
        self.fork = False
        self.draft_prompt = undone_prompt or ""
        self._input_mode_entered = True
        self._pending_resume_at = rewind_id
        self._save_session()
        self.working = True
        self.current_tool = "rewinding..."
        self._animate()
        self.start(resume_session_at=rewind_id)

    def _strip_view_from_prompt_index(self, prompt_index: int) -> None:
        """Remove rendered turns from prompt_index through end (◎ markers)."""
        if not self.output or not self.output.view:
            return
        if self.output._input_mode:
            self.output.exit_input_mode(keep_text=False)
        view = self.output.view
        content = view.substr(sublime.Region(0, view.size()))
        import re as _re
        # Match prompt lines: start of file or after newline, ◎ … ▶
        matches = list(_re.finditer(r'(?m)^◎ .+? ▶', content))
        if not matches:
            return
        if prompt_index < 0 or prompt_index >= len(matches):
            # Fall back to last prompt
            m = matches[-1]
        else:
            m = matches[prompt_index]
        start = m.start()
        # Include leading newline if present so we don't leave a blank gap wrong
        if start > 0 and content[start - 1] == "\n":
            start = start - 1
        self.output._replace(start, view.size(), "")
        if self.output.current:
            self.output.current = None
        view.erase_regions("claude_conversation")

    def _strip_view_from_last_prompt(self) -> None:
        """Claude path: drop the last ◎ … ▶ block."""
        if not self.output or not self.output.view:
            return
        if self.output._input_mode:
            self.output.exit_input_mode(keep_text=False)
        view = self.output.view
        content = view.substr(sublime.Region(0, view.size()))
        import re as _re
        last_prompt = None
        for m in _re.finditer(r'\n◎ .+? ▶', content):
            last_prompt = m
        if not last_prompt and content.startswith("◎ ") and " ▶" in content.split("\n")[0]:
            self.output._replace(0, view.size(), "")
        elif last_prompt:
            self.output._replace(last_prompt.start(), view.size(), "")
        if self.output.current:
            self.output.current = None
        view.erase_regions("claude_conversation")

    def _grok_undo_async(self, prompt_index=None, draft_prompt: str = "") -> None:
        """Grok message-round undo without blocking the UI thread.

        Uses send() callbacks (main-thread), never send_wait. Flow:
          optional rewind_points → rewind_execute → strip UI → restart session.
        """
        if not self.client or not self.client.is_alive():
            sublime.status_message("Bridge not ready for rewind")
            return
        if self.working and self.current_tool != "rewinding...":
            return

        self.working = True
        self.current_tool = "rewinding..."
        self._animate()
        sublime.status_message("Rewinding…")

        def _fail(msg: str) -> None:
            print(f"[Claude] grok undo failed: {msg}")
            sublime.status_message(f"Rewind failed: {msg}")
            self.working = False
            self.current_tool = None
            self._update_title_idle()
            self._enter_input_with_draft()

        def _execute(idx: int, draft: str) -> None:
            print(f"[Claude] grok undo: session={self.session_id} → prompt_index={idx}")
            if not self.client or not self.client.is_alive():
                _fail("bridge died")
                return

            state = {"done": False}

            def on_exec(resp: dict) -> None:
                if state["done"]:
                    return
                state["done"] = True
                # Main thread (send callback via set_timeout).
                if not isinstance(resp, dict):
                    resp = {}
                if "error" in resp:
                    err = resp["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    _fail(msg or "execute error")
                    return
                # Bare result shape from rpc._handle
                result = resp if "draft_prompt" in resp or "ok" in resp else (
                    resp.get("result") or resp)
                if not isinstance(result, dict):
                    result = {}
                d = (result.get("draft_prompt") or draft or "").strip()
                self._finish_grok_undo(idx, d)

            ok = self.client.send(
                "rewind_execute",
                {
                    "prompt_index": idx,
                    # Conversation only — never project file_snapshots.
                    "mode": "conversation_only",
                },
                on_exec,
            )
            if not ok:
                _fail("bridge send failed")
                return

            def _timeout():
                if state["done"]:
                    return
                if self.current_tool != "rewinding...":
                    return
                state["done"] = True
                _fail("timeout waiting for bridge (45s)")

            sublime.set_timeout(_timeout, 45000)

        # Index already known (quick panel) — skip points fetch.
        if prompt_index is not None:
            try:
                idx = int(prompt_index)
            except (TypeError, ValueError):
                _fail(f"bad prompt_index {prompt_index!r}")
                return
            _execute(idx, draft_prompt or "")
            return

        # Undo last turn: list points first.
        def on_points(resp: dict) -> None:
            if not isinstance(resp, dict):
                resp = {}
            if "error" in resp:
                err = resp["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                _fail(msg or "points error")
                return
            result = resp if "points" in resp else (resp.get("result") or resp)
            if not isinstance(result, dict):
                result = {}
            points = result.get("points") or []
            if not points:
                _fail("no rewind points")
                return
            # Prefer highest prompt_index (last user turn).
            best = None
            for p in points:
                if not isinstance(p, dict):
                    continue
                try:
                    i = int(p.get("prompt_index"))
                except (TypeError, ValueError):
                    continue
                if best is None or i > best[0]:
                    best = (i, (p.get("prompt_preview") or "").strip())
            if best is None:
                _fail("no valid rewind points")
                return
            _execute(best[0], best[1])

        ok = self.client.send("rewind_points", {}, on_points)
        if not ok:
            _fail("bridge send failed")

    def _finish_grok_undo(self, idx: int, draft: str) -> None:
        """After successful rewind_execute: trim UI and reload session."""
        self._strip_view_from_prompt_index(idx)
        saved_id = self.session_id
        if self.client:
            self.client.stop()
            self.client = None
        self.initialized = False
        self.session_id = saved_id
        self.resume_id = saved_id
        self.fork = False
        self.draft_prompt = draft or ""
        self._input_mode_entered = True
        self._pending_resume_at = None  # Grok uses session/load
        self._save_session()
        self.working = True
        self.current_tool = "rewinding..."
        self._animate()
        self.start()
        sublime.status_message(
            f"Rewound to turn {idx}"
            + (f": {draft[:40]}…" if len(draft) > 40 else (f": {draft}" if draft else ""))
        )

    def _update_title_idle(self) -> None:
        try:
            if self.output:
                self.output._update_title()
        except Exception:
            pass

    @staticmethod
    def _is_synthetic_turn(prompt: str) -> bool:
        """Detect prompts that aren't real user messages (bg-task wakes, retain
        injections, interruption markers, channel/subsession events, …) so the
        undo quick panel doesn't list them as rewind targets."""
        if not prompt:
            return True
        first = prompt.lstrip().split("\n", 1)[0]
        # XML-tagged synthetic blocks: <task-notification>, <channel>, <wake>,
        # <subsession>, <inject>, <timer>, etc.
        if first.startswith("<") and ">" in first:
            tag = first[1:first.index(">")].split()[0].lstrip("/")
            if tag in {
                "task-notification", "channel", "subsession",
                "wake", "inject", "timer", "notification", "retain",
            }:
                return True
        # Bracketed synthetic markers
        synthetic_brackets = (
            "[Request interrupted",
            "[retain context]",
            "[Loop]",
        )
        if any(first.startswith(p) for p in synthetic_brackets):
            return True
        return False

    def get_turns_for_undo(self) -> list:
        """Return [(label, rewind_id, draft_prompt)] for all undoable turns, newest first.

        Claude: rewind_id = prior assistant uuid.
        Grok: rewind_id = prompt_index (int) from x.ai/rewind/points.
        """
        if self.backend == "grok":
            return self._get_grok_turns_for_undo()
        turns = self._read_turns()
        result = []
        for i, (prompt, prev_asst_uuid) in enumerate(turns):
            if not prev_asst_uuid:
                continue  # first turn with no prior assistant — can't rewind here
            if self._is_synthetic_turn(prompt):
                continue  # bg notifications / interrupts / retain injects: not useful as rewind targets
            first_line = prompt.split("\n")[0][:72]
            label = f"{i + 1} — {first_line}" if first_line else f"{i + 1} — (empty)"
            result.append((label, prev_asst_uuid, prompt))
        result.reverse()
        return result

    def _get_grok_turns_for_undo(self) -> list:
        """Sync list for Claude-style callers — Grok must not block the UI.

        Returns [] and kicks an async panel fetch if a window is available.
        Prefer show_grok_undo_panel() from the command palette path.
        """
        # Never send_wait here — freezes Sublime (main-thread deadlock).
        return []

    def show_grok_undo_panel(self, window=None) -> None:
        """Fetch rewind points async and show a quick panel (Grok)."""
        win = window or self.window
        if not win or not self.client or not self.client.is_alive():
            sublime.status_message("Bridge not ready for rewind")
            return
        if self.working and self.current_tool != "rewinding...":
            sublime.status_message("Busy — wait for the turn to finish")
            return
        sublime.status_message("Loading rewind points…")

        def on_points(resp: dict) -> None:
            if not isinstance(resp, dict):
                resp = {}
            if "error" in resp:
                err = resp["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                sublime.status_message(f"Rewind points failed: {msg}")
                return
            result = resp if "points" in resp else (resp.get("result") or resp)
            if not isinstance(result, dict):
                result = {}
            points = result.get("points") or []
            turns = []
            for p in points:
                if not isinstance(p, dict):
                    continue
                try:
                    idx = int(p.get("prompt_index"))
                except (TypeError, ValueError):
                    continue
                preview = (p.get("prompt_preview") or "").strip()
                first = preview.split("\n")[0][:72] if preview else "(empty)"
                files = " · files" if p.get("has_file_changes") else ""
                label = f"{idx} — {first}{files}"
                turns.append((label, idx, preview))
            turns.reverse()
            if not turns:
                sublime.status_message("No rewind points")
                return
            labels = [t[0] for t in turns]

            def on_pick(i, _turns=turns):
                if i < 0:
                    return
                _, rid, draft = _turns[i]
                self._apply_undo(rid, draft)

            win.show_quick_panel(labels, on_pick, placeholder="Rewind to…")

        if not self.client.send("rewind_points", {}, on_points):
            sublime.status_message("Bridge send failed")

    def _read_turns(self) -> list:
        """Read JSONL and return [(prompt, prev_assistant_uuid)] for each user turn."""
        jsonl_path = self._find_jsonl_path()
        if not jsonl_path:
            return []
        turns = []
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
            print(f"[Claude] _read_turns error: {e}")
        return turns

    def _find_rewind_point(self) -> tuple:
        """Find the assistant entry uuid to rewind to (before last visible turn).
        Respects current _pending_resume_at to support consecutive undos.
        Skips synthetic turns (bg-task wakes, interrupts, retain injects) so
        undo lands on the user's last real message.
        Returns (uuid, undone_prompt) or (None, "") if can't rewind."""
        turns = self._read_turns()
        if not turns:
            return None, ""
        if self._pending_resume_at:
            for i, (prompt, asst_uuid) in enumerate(turns):
                if asst_uuid == self._pending_resume_at:
                    # Walk back over synthetic turns to find the previous real one.
                    j = i - 1
                    while j >= 0 and self._is_synthetic_turn(turns[j][0]):
                        j -= 1
                    if j < 1:
                        return None, ""
                    undone_prompt = turns[j][0]
                    rewind_to = turns[j][1]
                    if not rewind_to:
                        return None, ""
                    return rewind_to, undone_prompt
            return None, ""
        # Pick the most recent non-synthetic turn as the undo target.
        idx = len(turns) - 1
        while idx >= 0 and self._is_synthetic_turn(turns[idx][0]):
            idx -= 1
        if idx < 1:
            return None, ""
        undone_prompt = turns[idx][0]
        rewind_to = turns[idx][1]
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

    def query(self, prompt: str, display_prompt: str = None, silent: bool = False,
              _auto_retry: bool = False) -> None:
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

        # A user-initiated query (or any non-auto-retry) cancels any pending
        # auto-retry and resets the retry budget. Auto-retry calls pass
        # _auto_retry=True to keep the budget counting.
        if not _auto_retry:
            self._auto_retry_pending = False
            self._auto_retry_count = 0

        self.working = True
        self.query_count += 1
        self._query_gen = int(getattr(self, "_query_gen", 0) or 0) + 1
        query_gen = self._query_gen
        # New turn clears error-halt tab mark
        self._clear_error_halt()
        self._goal_last_update_sig = None  # allow same progress text next turn
        # Preserve sticky draft whenever the user is typing mid-stream — including
        # when a *queued* turn fires. Never wipe ◎ content just because the queue
        # advanced. Only clear draft on a normal user submit (no sticky open,
        # not silent, not queue-driven).
        firing_queue = bool(getattr(self, "_firing_queue", False))
        if self.output and self.output.is_input_mode():
            self.draft_prompt = self.output.get_input_text()
        elif silent or firing_queue:
            # Keep existing draft_prompt (may already hold sticky text)
            pass
        else:
            self.draft_prompt = ""  # User-initiated submit already emptied ◎
        self._pending_resume_at = None  # New query advances past any rewind point
        self._input_mode_entered = False  # Reset so input mode can be entered when query completes

        # Mark this session as the currently executing session for MCP tools
        # MCP tools should operate on the executing session, not the UI-active session
        # Only set if not already set (don't overwrite parent session when spawning subsessions)
        self._is_executing_session = False  # Track if we set the marker
        if self.output.view and not self.window.settings().has("claude_executing_view"):
            self.window.settings().set("claude_executing_view", self.output.view.id())
            self._is_executing_session = True
        # Multi-slot Quick host: all slots share one view_id — pin the slot id
        # so quick_done stops *this* agent, not whatever tab is focused.
        # Set even when output.view is None (inactive slot still streaming).
        if getattr(self, "quick_mode", False) and self.window:
            sid = getattr(self, "_quick_slot_id", None)
            if sid:
                try:
                    self.window.settings().set("claude_executing_quick_slot", sid)
                except Exception:
                    pass
        # Build prompt with context (may include images), then consume
        full_prompt, images = self._build_prompt_with_context(prompt)
        _, context_names, context_refs = self.context.take()

        # Store images for RPC call
        self._pending_images = images

        # Use display_prompt for UI if provided, otherwise use full prompt
        ui_prompt = display_prompt if display_prompt else prompt
        # Quick host may auto-dismiss when user said "close yourself" etc.
        if getattr(self, "quick_mode", False) and not silent:
            self._last_user_prompt = (ui_prompt or prompt or "").strip()

        # Check if bridge is alive before sending
        if not self.client.is_alive():
            self.working = False
            self._mark_error_halt("bridge died")
            self._status("error: bridge died")
            if not silent:
                self.output.text("\n\n*Bridge process died. Please restart the session.*\n")
            try:
                self.output.set_name(self.name or "Claude")
            except Exception:
                pass
            return

        # New query supersedes any prior interrupt debounce.
        self._interrupting = False

        if not silent:
            # Never steal focus on every turn — user may be editing another file
            # while the agent streams / goal continues.
            self.output.show(focus=False)
            # Auto-name session from first prompt if not already named
            if not self.name:
                self._set_name(ui_prompt[:30].strip() + ("..." if len(ui_prompt) > 30 else ""))
            self.output.prompt(ui_prompt, context_names, context_refs=context_refs)
        # Always show busy indicator: flip tab title now and start the spinner
        # loop. Silent queries (bg-task wakes, retain injects, …) still need a
        # visible cue that the session is processing, even though the user
        # prompt itself isn't rendered. The previous turn's meta() flipped
        # self.output.current.working=False; re-arm it so advance_spinner
        # actually re-renders the ⠋ line at the bottom of the conversation.
        if self.output.current is not None:
            self.output.current.working = True
            if silent:
                # Hide the previous turn's @done(…) line while this wake processes;
                # meta() of the wake result will set a fresh duration on completion.
                self.output.current.duration = 0
                self.output.current.has_meta = False
                self.output._render_current()
        self.output._update_title()
        self._animate()
        self._query_start = time.time()
        query_params = {"prompt": full_prompt}
        if hasattr(self, '_pending_images') and self._pending_images:
            query_params["images"] = self._pending_images
            self._pending_images = []

        def _on_done_for_gen(result, _gen=query_gen):
            self._on_done(result, _expected_gen=_gen)

        if not self.client.send("query", query_params, _on_done_for_gen):
            self.working = False
            self._mark_error_halt("bridge died")
            self._status("error: bridge died")
            self.output.text("\n\n*Failed to send query. Bridge process died.*\n")
            try:
                self.output.set_name(self.name or "Claude")
            except Exception:
                pass
            return

        # Sticky EOF composer: re-open ◎ so the next message can be typed
        # (queued via inject) while this turn streams. Silent wakes keep any
        # draft the user already had.
        if not silent:
            def _sticky():
                if not self.output or self.output.is_input_mode():
                    return
                # Don't reopen ◎ under question / permission / plan UI
                if getattr(self.output, "has_turn_modal_ui", None) and self.output.has_turn_modal_ui():
                    return
                if getattr(self.output, "_question_input_mode", False):
                    return
                self._input_mode_entered = False
                self._enter_input_with_draft()
            sublime.set_timeout(_sticky, 40)

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

    def _on_done(self, result: dict, _expected_gen: Optional[int] = None) -> None:
        # Stale completion: user Esc + queue already started a newer turn, but
        # the cancelled turn's interrupt/error still arrives. Ignoring prevents
        # *[interrupted]* spam and killing the live turn (working=False + re-fire).
        if (_expected_gen is not None
                and _expected_gen != getattr(self, "_query_gen", None)):
            print(
                f"[Claude] ignore stale turn done "
                f"gen={_expected_gen} current={getattr(self, '_query_gen', None)} "
                f"status={result.get('status') if isinstance(result, dict) else '?'}"
            )
            return

        self.current_tool = None

        # Clear executing session marker - MCP tools should no longer target this session
        if self.output.view and getattr(self, '_is_executing_session', False):
            self.window.settings().erase("claude_executing_view")
            self._is_executing_session = False
        if getattr(self, "quick_mode", False) and self.window:
            if self.window.settings().get("claude_executing_quick_slot") == getattr(
                    self, "_quick_slot_id", None):
                self.window.settings().erase("claude_executing_quick_slot")

        # 1. Determine completion type
        if "error" in result:
            completion = "error"
        elif result.get("status") == "interrupted":
            completion = "interrupted"
        else:
            completion = "success"

        # Interrupt already restored idle UI in interrupt(). Late bridge ack
        # only settles flags + flushes queue — never paints *[interrupted]* again
        # and never clobbers a newer live turn (queue/send_now may already run).
        if completion == "interrupted":
            # Critical: if settle timers / send_now already started the next
            # turn, working=True while _interrupting may still be True. The
            # old check required "working and not interrupting" so it missed
            # that case, forced working=False, and re-fired the queue —
            # superseding the turn we just started (Grok looks "weird").
            if self.working:
                print("[Claude] ignore interrupt done — newer turn already live")
                self._interrupting = False
                return
            self._interrupting = False
            if self._response_callback:
                cb = self._response_callback
                self._response_callback = None
                try:
                    cb("")
                except Exception:
                    pass
            # Host already set working=False + *[interrupted]*; just flush queue.
            self.working = False
            if self.output and self.output.current:
                self.output.current.working = False
            self._clear_deferred_state(clear_queue=False)
            if not getattr(self, "_quick_finished", False):
                if self._fire_next_queued():
                    return
                self._ensure_idle_input(reason="late interrupt ack")
            return

        # 2. Handle UI for each completion type.
        # If Task-mode verify finish hands off to a new turn (continuation),
        # we must return before the non-success gate forces working=False.
        goal_handoff = False
        if completion == "error":
            error_msg = result['error'].get('message', str(result['error'])) if isinstance(result['error'], dict) else str(result['error'])
            # Print to the Sublime console (with backend context) so provider API
            # errors are diagnosable at a glance — the chat view text alone is hard
            # to copy/inspect, and transient providers (e.g. Astron) hit these often.
            print(f"[Claude] query error [backend={self.backend}]: {error_msg}")
            self._mark_error_halt(error_msg)
            self._status("error")
            self.output.text(f"\n\n*Error: {error_msg}*\n")
            if self.output.current:
                self.output.current.working = False
                self.output._render_current()
            try:
                # Goal skeptic is a subagent — surface failure to parent, never
                # pause the (empty) child goal tracker.
                if self._goal_is_skeptic():
                    self._goal_notify_parent_skeptic_done(
                        error=f"Skeptic turn error: {error_msg}"[:200])
                elif (getattr(self, "_goal_verify_awaiting", False)
                      and (getattr(self, "_goal_verify_mode", None) or "task") == "task"):
                    goal_handoff = self._goal_abort_task_verify(
                        f"Host: verify turn error: {error_msg}"[:180],
                        message="verify error",
                    )
                else:
                    gt = getattr(self, "goal_tracker", None)
                    if gt and gt.is_active():
                        gt.pause("infra", f"Turn error: {error_msg}"[:200])
                        self._goal_verify_turn = False
                        self._goal_verify_awaiting = False
                        self._goal_skeptic_view_id = None
                        self.sync_goal_ui()
            except Exception:
                pass
        elif completion == "interrupted":
            self._status("interrupted")
            self.output.interrupted()
            try:
                if self._goal_is_skeptic():
                    self._goal_notify_parent_skeptic_done(
                        error="Skeptic interrupted")
                elif (getattr(self, "_goal_verify_awaiting", False)
                      and (getattr(self, "_goal_verify_mode", None) or "task") == "task"):
                    goal_handoff = self._goal_abort_task_verify(
                        "Host: verify turn interrupted",
                        message="interrupted",
                    )
            except Exception:
                pass
        else:
            self._clear_error_halt()
            self._status("ready")

        self._interrupting = False
        self.output.set_name(self.name or "Claude")
        self.output.clear_all_permissions()
        # Sticky composer may already be open mid-turn — leave it.

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

        # 4b. Goal finish already started the next turn — do not clobber working
        if goal_handoff:
            return

        # 5. GATE: deferred queue only on success — except interrupt keeps
        # user-queued messages (clear_queue=False) and flushes the next one.
        if completion != "success":
            self.working = False
            if self.output and self.output.current:
                self.output.current.working = False
            # Reset idle clock when the turn ends (long ACP runs + sticky ◎).
            now = time.time()
            self.last_activity = now
            self.last_idle_at = now
            if completion == "interrupted":
                self._clear_deferred_state(clear_queue=False)
                if not getattr(self, "_quick_finished", False):
                    if self._fire_next_queued():
                        return
                    self._ensure_idle_input(reason="completion=interrupted")
                return
            self._clear_deferred_state(clear_queue=True)
            if not getattr(self, "_quick_finished", False):
                self._ensure_idle_input(reason=f"completion={completion}")
            return

        # Quick was explicitly closed (status=closed) — no ◎, no queued turns
        if getattr(self, "_quick_finished", False):
            self.working = False
            self._queued_prompts.clear()
            try:
                self._update_queue_phantom()
            except Exception:
                pass
            self._inject_pending = False
            return

        # Safety net: user said "close yourself" but model never called
        # quick_done(closed). Dismiss after the goodbye turn.
        if (getattr(self, "quick_mode", False)
                and completion == "success"
                and not getattr(self, "_quick_finished", False)):
            try:
                from . import quick_agent as qa
                last = getattr(self, "_last_user_prompt", "") or ""
                if qa.user_wants_close(last):
                    host = qa.get_host(self.window) if self.window else None
                    if host:
                        host.complete_slot(self, status="closed", message="")
                    else:
                        self._quick_finished = True
                        self.working = False
                        qa.stop_session_bridge(self)
                    return
            except Exception as e:
                print(f"[Claude] quick auto-close: {e}")

        # 5a. Task-mode goal verify must close before user queue steals the turn
        # (otherwise phase stays verifying forever with pending tool verdict).
        if (getattr(self, "_goal_verify_awaiting", False)
                and (getattr(self, "_goal_verify_mode", None) or "task") == "task"):
            if self._goal_on_turn_success():
                return

        # 5b. Process plugin-side queued prompts (sticky composer / queue_prompt).
        # Keep working=True so the next query() owns the busy state.
        # Does not clear ◎ draft — user may still be typing the next one.
        # User queue wins over goal harness continuation (after verify closed).
        if self._fire_next_queued():
            return

        # 5c. Host goal harness: verify deferred complete and/or continue.
        if self._goal_on_turn_success():
            return

        # 6. Clear inject_pending - Claude mid-stream inject / bridge queued_inject
        self._inject_pending = False

        # 7. Now set working=False and enter input mode
        self.working = False
        now = time.time()
        self.last_activity = now
        # Idle clock starts when the turn ends — not when sticky ◎ opened
        # mid-stream (that left last_idle_at stale after long ACP runs).
        self.last_idle_at = now
        sublime.set_timeout(lambda: self._enter_input_with_draft() if not self.working else None, 100)

    def _clear_deferred_state(self, clear_queue: bool = True) -> None:
        """Clear deferred action state. Called on error/interrupt.

        clear_queue=False on interrupt so user-queued messages survive cancel.
        """
        if clear_queue:
            self._queued_prompts.clear()
        self._pending_bg_notifications.clear()
        self._bg_flush_scheduled = False
        self._inject_pending = False
        self._pending_retain = None
        self._input_mode_entered = False  # Allow re-entry to input mode
        try:
            self._update_queue_phantom()
        except Exception:
            pass

    def resume_input_mode(self) -> None:
        """Re-enter input mode after a non-query action (errors, cancellation, etc.)
        consumed the input. Idempotent: bails if already in input mode or working.
        """
        if self.working or not self.output:
            return
        self._input_mode_entered = False
        sublime.set_timeout(lambda: self._enter_input_with_draft() if not self.working else None, 100)

    def _ensure_idle_input(self, reason: str = "") -> None:
        """Force ◎ back after interrupt/error — even if a prior enter was wiped.

        Late session updates after cancel can re-render without sticky input and
        leave `_input_mode_entered=True` so re-entry was skipped (dead UI: idle,
        no busy mark, no composer — only sleep/reconnect recovered).

        Also flushes plugin-side queued prompts when idle — sticky ◎ was often
        already open mid-turn, so the old early-return never reached
        `_enter_input_with_draft` (which is what used to fire the queue).
        """
        if getattr(self, "_quick_finished", False):
            return
        if self.working:
            return
        if not self.output:
            return
        # Held across Esc: run next queued turn before re-opening ◎.
        if self._fire_next_queued():
            if reason:
                print(f"[Claude] flushed queue after idle ({reason})")
            return
        # Clear stuck spinner from post-interrupt text drips
        try:
            if self.output.current and self.output.current.working:
                self.output.current.working = False
                self.output._render_pending = False
                self.output._do_render()
        except Exception:
            pass
        self._input_mode_entered = False
        if self.output.is_input_mode():
            return

        def _go(tries=0):
            if self.working or getattr(self, "_quick_finished", False):
                return
            if not self.output:
                return
            # Queue may have been filled between tries
            if self._fire_next_queued():
                return
            if self.output.is_input_mode():
                return
            if getattr(self.output, "has_turn_modal_ui", None) and self.output.has_turn_modal_ui():
                return
            self._input_mode_entered = False
            self._enter_input_with_draft()
            if self.output.is_input_mode():
                if reason:
                    print(f"[Claude] idle input restored ({reason})")
                return
            if tries < 6:
                sublime.set_timeout(lambda: _go(tries + 1), 80 + tries * 40)

        sublime.set_timeout(lambda: _go(), 50)

    def _enter_input_with_draft(self) -> None:
        """Enter sticky composer and restore draft with cursor at end.

        Allowed while working — user can queue the next message mid-stream.
        """
        if not self.output:
            return
        # Restore/sleep: never open ◎ (would flash then strip — wrong design)
        if not getattr(self, "_composer_allowed", True):
            return
        if self.is_sleeping:
            return
        if self.output.view and self.output.view.settings().get("claude_sleeping"):
            return
        # Explicit close — host is dismissing; do not reopen ◎
        if getattr(self, "_quick_finished", False):
            return
        # Question / permission / plan owns the tail
        if getattr(self.output, "has_turn_modal_ui", None) and self.output.has_turn_modal_ui():
            return
        # Already in input mode — re-pin scroll only if this sheet is focused
        # (never focus_view: that stole the editor from other files).
        if self.output.is_input_mode():
            self._input_mode_entered = True
            # Critical: sticky ◎ is often already open mid-turn. Without this,
            # last_idle_at stays at the *previous* idle stamp and auto-sleep
            # fires immediately after a long ACP run ends.
            if not self.working:
                self.last_idle_at = time.time()
                self.last_activity = time.time()
            try:
                if self.output._view_is_focused():
                    # Re-pin scroll only — never yank mid-draft caret to EOF
                    self.output.focus_composer(
                        force_show=True, steal_focus=False, preserve_caret=True)
            except Exception:
                pass
            return

        # Skip if we've already entered input mode after the last query
        # This prevents duplicate entries from multiple callers (on_activated, _on_done, etc.)
        # Still allow re-entry while working after submit cleared the strip.
        # Exception: if ◎ is gone but flag is set, allow re-entry (post-interrupt drip).
        if self._input_mode_entered and not self.working:
            try:
                if self.output.is_input_mode() and self.output._view_is_focused():
                    self.output.focus_composer(
                        force_show=True, steal_focus=False, preserve_caret=True)
            except Exception:
                pass
            return

        # Queued prompts (e.g. notalone inject while sleeping) take priority
        # only when idle — fire without wiping a draft the user is typing.
        if not self.working and self._fire_next_queued():
            return

        # Stale conversation.working after interrupt: clear when session is idle.
        if self.output.current and self.output.current.working and not self.working:
            self.output.current.working = False

        self.output.enter_input_mode()  # ends with focus_composer → scroll bottom

        # Check if enter_input_mode actually succeeded (might have deferred)
        if not self.output.is_input_mode():
            # Retry once after pending render / race settles.
            def _retry():
                if not self.output:
                    return
                if self.output.is_input_mode():
                    if self.output._view_is_focused():
                        self.output.focus_composer(force_show=True, steal_focus=False)
                    return
                if self.output.current and self.output.current.working and not self.working:
                    self.output.current.working = False
                self.output.enter_input_mode()
                if self.output.is_input_mode():
                    self._input_mode_entered = True
                    if not self.working:
                        self.last_idle_at = time.time()
            sublime.set_timeout(_retry, 50)
            return

        self._input_mode_entered = True
        if not self.working:
            self.last_idle_at = time.time()

        if self.draft_prompt and self.output.view:
            # Insert draft on the ◎ line; drop whitespace-only legacy spare ``\\n``
            draft = self.draft_prompt
            if not str(draft).strip():
                draft = ""
                self.draft_prompt = ""
            try:
                self.output.set_composer_text(draft)
            except Exception:
                if draft:
                    self.output.view.run_command("append", {
                        "characters": draft,
                    })
                if hasattr(self.output, "ensure_composer_spare_line"):
                    self.output.ensure_composer_spare_line()
            try:
                if self.output._view_is_focused():
                    self.output.focus_composer(force_show=True, steal_focus=False)
            except Exception:
                pass
        elif self.output and self.output.is_input_mode():
            try:
                self.output.collapse_empty_composer_tail()
            except Exception:
                pass


    def queue_prompt(self, prompt: str) -> None:
        """Queue a prompt for the next turn (or run immediately if idle).

        Sticky-composer / mid-stream: plugin-side queue + phantom chrome above
        ◎ (not transcript lines). Fires from `_on_done` when the turn ends.
        Does not touch the sticky draft — caller may leave ◎ as-is for more typing.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return
        self._status(f"queued: {prompt[:30]}...")

        if self.working and self.client and self.client.is_alive():
            self._queued_prompts.append(prompt)
            self._update_queue_phantom()
            # Optional Claude mid-stream inject (same turn). If it works, drop
            # the plugin queue entry so we do not double-run on turn end.
            if self.backend == "claude":
                def _on_inj(r, p=prompt):
                    if not isinstance(r, dict) or r.get("error"):
                        return  # keep local queue entry
                    res = r.get("result") if isinstance(r.get("result"), dict) else {}
                    if res.get("status") in ("ok", "queued"):
                        try:
                            self._queued_prompts.remove(p)
                        except ValueError:
                            pass
                        if res.get("status") == "queued":
                            self._inject_pending = True
                        self._update_queue_phantom()
                self.client.send("inject_message", {"message": prompt}, _on_inj)
            return

        if self.client and self.client.is_alive():
            self._fire_queued_now(prompt)
            return

        # Quick: dead bridge → submit starts a new session (same as Enter)
        if getattr(self, "quick_mode", False):
            try:
                from . import quick_agent as qa
                host = qa.get_host(self.window) if self.window else None
                if host and host.submit_prompt(self, prompt):
                    return
            except Exception as e:
                print(f"[Claude] quick queue→start: {e}")

        self._queued_prompts.append(prompt)
        self._update_queue_phantom()

    def send_now(self, prompt: str = "") -> bool:
        """Cancel-and-send: stop the live turn, run this prompt next.

        Grok/Kimi TUI "send now" semantics — not mid-stream steer. Existing
        queued follow-ups stay (this message is prepended and fires first after
        interrupt settles). Empty prompt with a non-empty queue flushes the top
        of the queue after cancel. Idle + prompt runs immediately.

        Returns True if a send/interrupt was started.
        """
        prompt = (prompt or "").strip()

        if not self.working:
            if prompt:
                self._fire_queued_now(prompt)
                return True
            if self._queued_prompts:
                return self._fire_next_queued()
            return False

        if prompt:
            # Prefer this message first; drop exact dupes already queued.
            self._queued_prompts = [p for p in self._queued_prompts if p != prompt]
            self._queued_prompts.insert(0, prompt)
            self._update_queue_phantom()
        elif not self._queued_prompts:
            sublime.status_message("Nothing to send now")
            return False

        print(f"[Claude] send now: interrupt → {self._queued_prompts[0][:60]!r}")
        self._status(f"send now: {self._queued_prompts[0][:40]}…")
        sublime.status_message("Send now — interrupting current turn…")
        # Quiet interrupt: no *[interrupted]* banner (next turn replaces it).
        self._send_now_pending = True
        self.interrupt()
        return True

    def _fire_next_queued(self) -> bool:
        """Pop and run the next queued prompt. Preserves ◎ draft. True if fired."""
        if not self._queued_prompts:
            return False
        prompt = self._queued_prompts.pop(0)
        self._update_queue_phantom()
        self._fire_queued_now(prompt)
        return True

    def _fire_queued_now(self, prompt: str) -> None:
        """Run a queued/follow-up prompt without wiping sticky user input."""
        # Snapshot sticky draft before query/prompt path
        if self.output and self.output.is_input_mode():
            try:
                self.draft_prompt = self.output.get_input_text()
            except Exception:
                pass
        self._firing_queue = True
        try:
            if self._is_synthetic_turn(prompt):
                first = prompt.lstrip().split("\n", 1)[0][:60]
                display = f"{BACKGROUND_PREFIX}{first}"
                self.query(prompt, display_prompt=display, silent=True)
            else:
                self.query(prompt, display_prompt=prompt)
        finally:
            self._firing_queue = False

    def _clear_queue_phantom(self) -> None:
        """Remove queue chips + hairline split (safe if never created)."""
        try:
            ps = getattr(self, "_queue_phantom_set", None)
            if ps is not None:
                ps.update([])
            # Also clear via rebound set in case of view swap / stale attr
            ps2 = self._phantom_set_for("_queue_phantom_set", "claude_queue")
            if ps2 is not None and ps2 is not ps:
                ps2.update([])
        except Exception:
            pass

    def _update_queue_phantom(self) -> None:
        """Composer chrome *above* ◎: optional queue chips + a hairline split.

        Layout (top → bottom, phantoms only):
          ⏳ queued msg  ×   (only when queued)
          ─ hairline ─
          ◎ input…

        Only while sticky ◎ is open. Conversation rewrites must not leave an
        orphaned hairline mid-history (ST remaps LAYOUT_BLOCK into the turn).
        Anchor on the line *above* ◎ with LAYOUT_BLOCK.
        """
        import html as _html
        if not self.output or not self.output.view:
            return
        view = self.output.view
        if not view.is_valid():
            return
        # Never draw over history / sleep / modal / free-text Other
        in_input = getattr(self.output, "_input_mode", False)
        if (
            not in_input
            or getattr(self.output, "_question_input_mode", False)
            or view.settings().get("claude_sleeping")
            or (getattr(self.output, "has_turn_modal_ui", None)
                and self.output.has_turn_modal_ui()
                and not in_input)
        ):
            self._clear_queue_phantom()
            return

        ps = self._phantom_set_for("_queue_phantom_set", "claude_queue")
        if not ps:
            return

        # Peel = start of composer (◎). Park phantom on the previous line so
        # LAYOUT_BLOCK draws *below* that line and *above* ◎.
        peel = getattr(self.output, "_input_area_start", None)
        if peel is None or peel < 0:
            peel = getattr(self.output, "_input_start", None)
        if peel is None or peel < 0:
            peel = view.size()
        peel = min(max(0, int(peel)), view.size())
        # If peel is 0 there is no "above ◎" line — skip rather than pin at 0
        # (that used to leave a hairline at the top of the buffer).
        if peel <= 0:
            self._clear_queue_phantom()
            return
        pt = peel - 1

        rows = []
        q = list(self._queued_prompts or [])
        if q:
            rows.append(
                '<div style="margin:0 0 2px 0;font-size:10px;'
                'color:color(var(--foreground) alpha(0.4));">'
                'queue · '
                '<a href="send_now" style="color:var(--orangish);'
                'text-decoration:none;" title="Cancel turn and send top now">'
                'send now</a>'
                '</div>'
            )
            for i, msg in enumerate(q):
                short = msg.replace("\n", " ").strip()
                if len(short) > 72:
                    short = short[:72] + "…"
                safe = _html.escape(short)
                rows.append(
                    f'<div style="margin:1px 0;padding:1px 6px;'
                    f'background-color:color(var(--foreground) alpha(0.06));'
                    f'color:var(--bluish);font-size:11px;">'
                    f'⏳ {safe}'
                    f'&nbsp;<a href="send:{i}" style="color:var(--orangish);'
                    f'text-decoration:none;" title="send now">↵</a>'
                    f'&nbsp;<a href="drop:{i}" style="color:var(--redish);'
                    f'text-decoration:none;" title="remove">×</a>'
                    f'</div>'
                )
        # Hairline flush above ◎ — no bottom margin (avoids empty-line gap)
        rows.append(
            '<div style="margin:1px 0 0 0;padding:0;line-height:1;'
            'font-size:1px;border-top:1px solid '
            'color(var(--foreground) alpha(0.14));">&nbsp;</div>'
        )
        html = (
            f'<body id="claude-queue" style="margin:0;padding:0;">'
            f'{"".join(rows)}</body>'
        )
        try:
            ps.update([sublime.Phantom(
                sublime.Region(pt, pt),
                html,
                sublime.LAYOUT_BLOCK,
                on_navigate=self._on_queue_phantom_navigate,
            )])
        except Exception:
            try:
                self._clear_queue_phantom()
            except Exception:
                pass

    def _on_queue_phantom_navigate(self, href: str) -> None:
        if href == "send_now":
            # Top of queue → cancel-and-send (empty composer path).
            self.send_now("")
            return
        if href.startswith("send:"):
            try:
                idx = int(href.split(":", 1)[1])
            except (TypeError, ValueError):
                return
            if 0 <= idx < len(self._queued_prompts):
                msg = self._queued_prompts.pop(idx)
                self.send_now(msg)
            return
        if not href.startswith("drop:"):
            return
        try:
            idx = int(href.split(":", 1)[1])
        except (TypeError, ValueError):
            return
        if 0 <= idx < len(self._queued_prompts):
            self._queued_prompts.pop(idx)
            self._update_queue_phantom()
            sublime.status_message("Removed queued message")

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
        # Debounce Esc spam — multiple cancels after the turn ends kill Grok
        # (session/cancel on a dead prompt → ChatStateActor dead).
        if getattr(self, "_interrupting", False):
            return
        if not self.working and not getattr(self, "_inject_pending", False):
            # Idle: only clear input is handled by the command; nothing to cancel.
            if break_channel and self.output and self.output.view:
                from . import notalone
                notalone.interrupt_channel(self.output.view.id())
            return

        self._interrupting = True
        # Keep user-queued messages — interrupt only cancels the active turn.
        # Do NOT flush the queue here: bridge cancel is still in flight, and
        # starting a new turn immediately races late interrupt _on_done (which
        # used to mark the new turn *[interrupted]* and re-fire). Queue flushes
        # from late interrupt ack / post-settle rearm once idle.

        # Goal harness: Esc pauses Active goal so we don't auto-continue.
        try:
            gt = getattr(self, "goal_tracker", None)
            if gt and gt.is_active():
                gt.pause("user", "Interrupted by user")
                self._goal_skip_continue_once = True
                self._goal_verify_turn = False
                # Stop planner auto-replan after Esc (otherwise accept fails
                # "not active" and host loops re-plan forever).
                self._goal_planning_turn = False
                self.sync_goal_ui()
        except Exception as e:
            print(f"[Claude] goal interrupt pause: {e}")

        # Immediate UI: idle. Do NOT leave session.working=True with
        # conversation.working=False (dead UI after a hung cancel).
        send_now = bool(getattr(self, "_send_now_pending", False))
        self._send_now_pending = False
        self.working = False
        self.current_tool = None
        self._clear_deferred_state(clear_queue=False)
        try:
            if self.output:
                if send_now:
                    # Cancel-and-send: no *[interrupted]* banner (next turn
                    # starts immediately — banner looks broken on Grok).
                    self.output.interrupted(show_banner=False)
                else:
                    self.output.interrupted()
        except Exception:
            pass
        self._status("send now…" if send_now else "interrupted")
        try:
            self._update_queue_phantom()
        except Exception:
            pass
        # Restore ◎ only — queue flush waits for settle (see rearm / late ack).
        # Still allow flush via _ensure_idle_input once idle, but delay the
        # first attempt so cancel lands before a new session/prompt.
        gen = getattr(self, "_interrupt_gen", 0) + 1
        self._interrupt_gen = gen

        def _rearm_after_drip(_gen=gen, fire_queue=True):
            if getattr(self, "_interrupt_gen", 0) != _gen:
                return
            # Newer turn already live (from an earlier settle or late ack) —
            # do not re-fire the rest of the queue on this interrupt gen.
            if self.working:
                return
            # After cancel settles: flush held queue once, else restore ◎.
            if fire_queue:
                self._ensure_idle_input(reason="post-interrupt settle")
            else:
                # Composer only (no queue) — used for very early re-paint
                if not self.output:
                    return
                if self.output.is_input_mode():
                    return
                self._input_mode_entered = False
                try:
                    self._enter_input_with_draft()
                except Exception:
                    pass

        # Composer quickly; queue only after cancel has had time to settle
        # (avoids Grok/Kimi agent_busy + late interrupt clobbering the new turn).
        sublime.set_timeout(lambda: _rearm_after_drip(fire_queue=False), 50)
        # Prefer waiting for bridge cancel ACK (late _on_done) for queue fire;
        # these are backups if the ACK is slow/missing.
        sublime.set_timeout(lambda: _rearm_after_drip(fire_queue=True), 450)
        sublime.set_timeout(lambda: _rearm_after_drip(fire_queue=True), 900)
        sublime.set_timeout(lambda: _rearm_after_drip(fire_queue=True), 1600)

        if self.client:
            sent = self.client.send("interrupt", {})
            if not sent:
                self._interrupting = False
                self._mark_error_halt("bridge died")
                self._status("error: bridge died")
                try:
                    self.output.text(
                        "\n\n*Bridge process died. Please restart the session.*\n")
                    self.output.set_name(self.name or "Claude")
                except Exception:
                    pass
            else:
                # Late _on_done from the cancelled query is a no-op for UI;
                # clear interrupting when it arrives (or after a short grace).
                def _clear_interrupting(_gen=gen):
                    if getattr(self, "_interrupt_gen", 0) == _gen:
                        self._interrupting = False
                        # One last re-arm in case a drip wiped ◎ after settle
                        if not self.working:
                            self._ensure_idle_input(reason="interrupt flag clear")

                sublime.set_timeout(_clear_interrupting, 3000)

        # Break any active channel connection (only for user-initiated interrupts)
        if break_channel and self.output and self.output.view:
            from . import notalone
            notalone.interrupt_channel(self.output.view.id())

    def stop(self) -> None:
        # Persist closed state before cleanup
        self._abort_background_tools(reason="session stopped")
        self._task_tool_map.clear()
        self._bg_poll_timer = None  # cancel pending poll (map cleared → _bg_poll will bail)
        self._persist_state("closed")

        # Clean up terminal mode if active
        if self.terminal_mode:
            self._terminal_poll_active = False
            tv = self._find_terminal_view()
            if tv and tv.is_valid():
                tv.close()
            self.terminal_mode = False
            self._terminal_tag = None

        # Release persona if acquired
        if self.persona_session_id and self.persona_url:
            self._release_persona()

        if self.client:
            client = self.client
            client.send("shutdown", {}, lambda _: client.stop())
        self._clear_status()

        # Release accumulated state
        if self.output:
            self.output.conversations.clear()
        self.context.clear()
        self._queued_prompts.clear()

    @property
    def is_sleeping(self) -> bool:
        return bool(self.session_id) and self.client is None and not self.initialized

    @property
    def display_name(self) -> str:
        from .output import strip_title_decoration
        base = strip_title_decoration(self.name or "Claude")
        return base or "Claude"

    def sleep(self, force: bool = False) -> bool:
        """Put session to sleep — kill bridge, keep view.

        Returns True if the session was put to sleep, False if refused.
        Refuses (returns False) when background tools are running unless
        force=True. Caller can re-invoke with force=True to abort + sleep.
        """
        if not self.session_id:
            return False
        if self.working:
            self.interrupt()
            sublime.set_timeout(self.sleep, 500)
            return False
        # Refuse to sleep if background processes are alive — they'd be killed
        # silently with the bridge subprocess. force=True overrides.
        if not force and self.output:
            bg = self.output.active_background_tools()
            if bg:
                names = ", ".join(t.name for t in bg[:3])
                more = f" (+{len(bg) - 3} more)" if len(bg) > 3 else ""
                msg = f"refusing to sleep: {len(bg)} background tool(s) running: {names}{more}"
                print(f"[Claude] {msg}")
                sublime.status_message(f"Claude: {msg}")
                return False
        # If we got here with force=True and bg tools, abort their UI state.
        self._abort_background_tools(reason="session slept")
        # Clear pending background-task ID map; bridge restart loses these mappings.
        self._task_tool_map.clear()
        self._bg_poll_timer = None  # cancel pending poll (map cleared → _bg_poll will bail)
        if self.client:
            client = self.client
            self.client = None
            client.send("shutdown", {}, lambda _: client.stop())
        self.initialized = False
        self._persist_state("sleeping")
        self._apply_sleep_ui()
        return True

    def _abort_background_tools(self, reason: str) -> None:
        """Drop all in-flight background tools — their subprocess is gone with the
        bridge, so their outcome is unknowable and a leftover ✘ line is just
        noise. Remove the lines rather than mark them errored."""
        if not self.output:
            return
        try:
            from .output import BACKGROUND
            # Union of currently-visible bg tools and any we've tracked across
            # history truncation. Use object identity to avoid double-handling.
            seen = set()
            bg = []
            for tool in self.output.active_background_tools():
                if id(tool) not in seen:
                    seen.add(id(tool))
                    bg.append(tool)
            for tool in self._bg_tools.values():
                if tool is not None and id(tool) not in seen:
                    seen.add(id(tool))
                    bg.append(tool)
            for tool in bg:
                if tool.status != BACKGROUND:
                    continue
                self.output.remove_tool(tool)
            self._bg_tools.clear()
            self._bg_task_ids.clear()
            if bg:
                print(f"[Claude] dropped {len(bg)} aborted background tool(s): {reason}")
        except Exception as e:
            print(f"[Claude] _abort_background_tools error: {e}")

    def _strip_sticky_composer(self) -> None:
        """Remove leftover ◎ draft strip after restart + clear input-mode flags.

        ST session restore leaves buffer text and often setting.claude_input_mode
        still true — that looks like a live composer on a sleeping sheet.
        """
        if not self.output or not self.output.view:
            return
        try:
            if self.output.is_input_mode():
                try:
                    self.draft_prompt = self.output.get_input_text().strip()
                except Exception:
                    pass
            # Always clear flags + strip ◎…/📎 tail (not only bare marker)
            self.output.reset_input_mode()
        except Exception as e:
            print(f"[Claude] strip sticky composer: {e}")
            try:
                self.output.view.settings().set("claude_input_mode", False)
                self.output._input_mode = False
            except Exception:
                pass

    def _apply_sleep_ui(self, *, touch_buffer: bool = True) -> None:
        """Apply sleeping state: no composer, ⏸ title, optional pause overlay.

        Always strips sticky ◎ residue (restart leaves it in the buffer).
        touch_buffer / focus only gates the heavy overlay phantom.
        """
        if not self.output or not self.output.view:
            return
        if not self.session_id:
            return
        view = self.output.view
        view.settings().set("claude_sleeping", True)
        view.settings().set("claude_input_mode", False)
        self.output.set_name(self.display_name)
        # Always remove ◎ strip — this is what looked like "input on restart"
        self._strip_sticky_composer()
        # Composer pad + queue only make sense with ◎; wrong pad key used to
        # leave LAYOUT_BELOW height. Also drop spare blank lines under @done
        # that made a huge void above the sleep banner.
        try:
            clear_claude_view_phantoms(
                view,
                keys=("claude_composer_pad", "claude_pad", "claude_queue"),
            )
            if getattr(self.output, "_pad_phantom_set", None) is not None:
                self.output._pad_phantom_set.update([])
        except Exception:
            pass
        try:
            self._update_queue_phantom()
        except Exception:
            pass
        try:
            self._update_permission_banner(show=False)
        except Exception:
            pass
        if touch_buffer:
            try:
                from .output_view import OutputView
                OutputView.collapse_trailing_blank_lines(view, keep=1)
            except Exception:
                pass

        win = view.window()
        is_focused = bool(
            win and win.active_view() and win.active_view().id() == view.id()
        )
        if is_focused:
            self._status("sleeping")
        # Overlay can jostle focus on background tabs — only when painting
        if not touch_buffer and not is_focused:
            return
        # Single banner only (registry set replaces; no stack per reload).
        self._show_overlay_phantom(
            "\u23f8 Session paused \u2014 press Enter to wake",
            color="var(--yellowish)",
            strong=True,
        )

    def _phantom_set_for(self, attr: str, key: str):
        """PhantomSet for this view+key via process-global registry (reload-safe)."""
        view = self.output.view if self.output else None
        if not view or not view.is_valid():
            return None
        ps = keyed_phantom_set(view, key)
        # Keep session attr as alias so older call sites still see a set
        setattr(self, attr, ps)
        setattr(self, attr + "_view_id", view.id())
        return ps

    def reset_phantoms_for_new_view(self) -> None:
        """Clear session-local aliases (registry keeps the real PhantomSets)."""
        for attr in (
            "_overlay_phantom_set", "_permmode_phantom_set",
            "_wakeup_phantom_set", "_workflow_phantom_set",
            "_queue_phantom_set",
        ):
            setattr(self, attr, None)
            setattr(self, attr + "_view_id", None)
        if self.output:
            self.output._media_phantom_set = None
            self.output._context_phantom_set = None
            self.output._pad_phantom_set = None

    def _get_overlay_phantom_set(self):
        return self._phantom_set_for("_overlay_phantom_set", "claude_overlay")

    def _show_overlay_phantom(
        self,
        html_body: str,
        color: str = "color(var(--foreground) alpha(0.5))",
        *,
        strong: bool = False,
    ) -> None:
        """Session-status block at EOF (sleep / connecting / terminal).

        strong=True: full-width split rule + bold banner (session paused).
        Always replaces via the long-lived registry set — never stacks.
        """
        if not self.output or not self.output.view:
            return
        view = self.output.view
        ps = keyed_phantom_set(view, "claude_overlay")
        if not ps:
            return
        self._overlay_phantom_set = ps
        self._overlay_phantom_set_view_id = view.id()
        # Anchor on last non-empty line (not EOF after blank run) so LAYOUT_BLOCK
        # does not sit under a tall void of spare newlines / pad leftovers.
        content = view.substr(sublime.Region(0, view.size()))
        pt = view.size()
        try:
            stripped = content.rstrip("\n")
            if stripped:
                # Point at end of last non-empty line
                pt = len(stripped)
                # Prefer start of that line for BLOCK layout (below the line)
                pt = view.line(pt - 1 if pt > 0 else 0).begin()
            else:
                pt = 0
        except Exception:
            last_nl = content.rfind("\n")
            pt = last_nl if last_nl >= 0 else 0
        if strong:
            # High-contrast banner. Avoid width:200em / min-width:40em — those
            # inflate layout_extent and create a huge horizontal scroll range.

            try:
                vw = int(float(view.viewport_extent()[0])) - 8
                w = max(80, vw)
            except Exception:
                w = 400
            line = (
                f'<div style="margin:0 0 6px 0;padding:0;line-height:1;'
                f'font-size:1px;height:0;max-width:{w}px;width:{w}px;'
                f'border-top:1px solid color-mix(in srgb, {color} 65%, transparent);'
                f'">&nbsp;</div>'
            )
            html = (
                f'<body id="claude-overlay" style="margin:10px 0 8px 0;padding:0;'
                f'max-width:{w}px;">'
                f'{line}'
                f'<div style="'
                f'padding:5px 10px;'
                f'font-size:13px;font-weight:bold;'
                f'letter-spacing:0.02em;'
                f'color:{color};'
                f'background-color:color-mix(in srgb, {color} 14%, transparent);'
                f'border-left:3px solid {color};'
                f'">'
                f'{html_body}'
                f'</div>'
                f'</body>'
            )
        else:
            html = (
                f'<body id="claude-overlay" style="margin:8px 0;padding:0;'
                f'color:{color};">{html_body}</body>'
            )
        # erase_phantoms kills stacked orphans from prior reloads; then one banner.
        try:
            if hasattr(view, "erase_phantoms"):
                view.erase_phantoms("claude_overlay")
            ps.update([])
        except Exception:
            pass
        ps.update([sublime.Phantom(sublime.Region(pt, pt), html, sublime.LAYOUT_BLOCK)])
        # Selection / show on inactive sheets steals focus during multi-tab restore.
        win = view.window()
        if win and win.active_view() and win.active_view().id() == view.id():
            view.sel().clear()
            view.sel().add(sublime.Region(view.size(), view.size()))
            view.show(view.size())

    def _clear_overlay_phantom(self) -> None:
        """Remove sleep/connecting banner via long-lived registry set."""
        view = self.output.view if self.output else None
        if view and view.is_valid():
            clear_claude_view_phantoms(view, keys=("claude_overlay",))
        self._overlay_phantom_set = None
        self._overlay_phantom_set_view_id = None

    # Permission-mode banner: a persistent, color-coded line pinned at the input
    # area whenever the agent runs more autonomously than baseline, so "it's
    # acting without asking" is visible right where you type. default/acceptEdits
    # are the baseline and intentionally get no banner.
    _PERMMODE_BANNER = {
        "auto": ("⏵ auto · runs safe actions without asking, prompts on risky", "var(--yellowish)"),
        "dontAsk": ("⏵ don't-ask · never prompts; denies anything not pre-approved", "var(--orangish)"),
        "bypassPermissions": ("⏵ bypass · ALL actions run without asking — caution", "var(--redish)"),
    }

    def _get_permmode_phantom_set(self):
        return self._phantom_set_for("_permmode_phantom_set", "claude_permmode")

    def _update_permission_banner(self, show: bool = True) -> None:
        ps = self._get_permmode_phantom_set()
        if not ps or not self.output or not self.output.view:
            return
        in_input = getattr(self.output, '_input_mode', False)
        info = self._PERMMODE_BANNER.get(getattr(self, 'permission_mode', None)) if (show and in_input) else None
        if not info:
            ps.update([])
            return
        label, color = info
        view = self.output.view
        # Anchor above ◎ (peel-1), not last ``\\n`` — trailing draft blanks used
        # to push this banner under empty rows below the marker.
        peel = getattr(self.output, "_input_area_start", None)
        if peel is None or peel < 0:
            peel = getattr(self.output, "_input_start", None)
        if peel is None or peel < 0:
            peel = view.size()
        peel = min(max(0, int(peel)), view.size())
        pt = max(0, peel - 1) if peel > 0 else 0
        html = f'<body style="margin: 2px 0; color: {color};">{label}</body>'
        ps.update([sublime.Phantom(sublime.Region(pt, pt), html, sublime.LAYOUT_BLOCK)])

    def _get_wakeup_phantom_set(self):
        return self._phantom_set_for("_wakeup_phantom_set", "claude_wakeup")

    def _wakeup_armed(self) -> bool:
        nxt = getattr(self, "next_wake_at", None)
        return bool(nxt and nxt > time.time())

    def _update_wakeup_banner(self, show: bool = True) -> None:
        """Pin cron/loop bar with ETA + Stop while a self-wake is armed.

        Always visible when next_wake_at is in the future (input *and* mid-turn).
        Stop cancels bridge crons / ScheduleWakeup / Grok client schedule backups.
        """
        ps = self._get_wakeup_phantom_set()
        if not ps or not self.output or not self.output.view:
            return
        if not (show and self._wakeup_armed()):
            ps.update([])
            self._stop_wakeup_ticker()
            return
        nxt = self.next_wake_at
        remain = max(0, int(nxt - time.time()))
        when = datetime.datetime.fromtimestamp(nxt).strftime("%H:%M")
        if remain >= 3600:
            eta = f"~{remain // 3600}h{(remain % 3600) // 60:02d}m"
        elif remain >= 60:
            eta = f"~{remain // 60}m{remain % 60:02d}s"
        else:
            eta = f"~{remain}s"
        view = self.output.view
        # Prefer just above the input marker when present; else last newline.
        content = view.substr(sublime.Region(0, view.size()))
        pt = None
        if getattr(self.output, "_input_mode", False) and getattr(self.output, "_input_start", 0):
            pt = max(0, self.output._input_start - 1)
        if pt is None:
            last_nl = content.rfind("\n")
            pt = last_nl if last_nl >= 0 else 0
        # Stop is the primary control (red chip) — cancel cron / loop.
        html = (
            f'<body id="claude-wakeup" style="margin:6px 0 4px 0;padding:0;'
            f'font-size:12px;color:var(--bluish);">'
            f'<span style="color:var(--bluish);">↻ cron · next at {when} · {eta}</span>'
            f'&nbsp;&nbsp;'
            f'<a href="stop" style="'
            f'color:var(--background);'
            f'background-color:var(--redish);'
            f'padding:1px 10px;'
            f'text-decoration:none;'
            f'font-weight:bold;'
            f'border-radius:3px;'
            f'">Stop</a>'
            f'</body>'
        )
        ps.update([sublime.Phantom(
            sublime.Region(pt, pt),
            html,
            sublime.LAYOUT_BLOCK,
            on_navigate=self._on_wakeup_banner_navigate,
        )])
        self._ensure_wakeup_ticker()

    def _ensure_wakeup_ticker(self) -> None:
        """Refresh ETA / re-pin Stop bar every few seconds while armed."""
        if getattr(self, "_wakeup_ticker_on", False):
            return
        self._wakeup_ticker_on = True

        def _tick():
            self._wakeup_ticker_on = False
            if not self._wakeup_armed():
                self._update_wakeup_banner(show=False)
                return
            self._update_wakeup_banner(show=True)
            # _update_wakeup_banner re-arms the ticker via _ensure_wakeup_ticker

        sublime.set_timeout(_tick, 5000)

    def _stop_wakeup_ticker(self) -> None:
        self._wakeup_ticker_on = False

    def _on_wakeup_banner_navigate(self, href: str) -> None:
        if href in ("cancel", "stop", "remove"):
            self.cancel_scheduled_loop()

    def cancel_scheduled_loop(self) -> None:
        """User-initiated cancel of cron / loop / scheduler wakeups."""
        print(f"[Claude] cancel_scheduled_loop backend={self.backend}")
        self.next_wake_at = None
        self.is_looping = False
        self._stop_wakeup_ticker()
        if self.output:
            self.output._update_title()
            self._update_wakeup_banner(show=False)
        # Tell bridge to drop cron jobs / wake timers / Grok client backups.
        if self.client and self.client.is_alive():
            self.client.send("cancel_loop", {}, lambda r: print(
                f"[Claude] cancel_loop → {r}"))
        sublime.status_message("Cron / scheduled wakeup stopped")

    def _show_connecting_phantom(self) -> None:
        self._show_overlay_phantom("◎ Connecting...")

    def restart(self) -> None:
        """Restart session — sleep then immediately wake.

        Restart is an explicit user action (typically used to fix a stuck
        session), so background tools are aborted via force=True.
        """
        def do_wake():
            if self.output and self.output.view and self.output.view.settings().get("claude_sleeping"):
                self.wake()
        if self.sleep(force=True):
            sublime.set_timeout(do_wake, 600)

    def wake(self) -> None:
        """Wake a sleeping session — re-spawn bridge with resume."""
        # Always strip sleep/connect chrome first. After soft package reload the
        # old PhantomSet is gone but the HTML banner can still sit on the view;
        # early-return paths must not leave "Session paused" over a live sheet.
        if self.output and self.output.view:
            clear_claude_view_phantoms(
                self.output.view,
                keys=("claude_overlay", "claude_queue", "claude_permmode"),
            )
            self._overlay_phantom_set = None
            self._overlay_phantom_set_view_id = None
        if self.client or self.initialized:
            # Stale sleep chrome after reload while already live
            if self.output and self.output.view:
                self.output.view.settings().erase("claude_sleeping")
                try:
                    self.output.set_name(self.display_name)
                except Exception:
                    pass
            return
        if not self.session_id:
            return
        self.terminal_mode = False
        self._terminal_poll_active = False
        self._terminal_tag = None
        self._clear_overlay_phantom()
        if self.output and self.output.view:
            view = self.output.view
            view.settings().erase("claude_sleeping")
            end = view.size()
            view.sel().clear()
            view.sel().add(end)
            view.show(end)
        # Composer opens only after bridge init succeeds (_on_init)
        self._composer_allowed = False
        self.resume_id = self.session_id
        self.fork = False
        resume_at = self._pending_resume_at
        self.current_tool = "waking..."
        self.start(resume_session_at=resume_at)
        self._persist_state("open")
        if self.output and self.output.view:
            self.output.set_name(self.display_name)

    def change_backend(self, new_backend: str) -> bool:
        """Change this session's provider on the fly to a different Claude-bridge
        backend, resuming the same session so history is preserved.

        Only backends sharing the Claude bridge (main.py) are eligible: the
        built-in 'claude' plus any custom Anthropic-compatible provider. Codex /
        Copilot / Pi / DSR use their own bridges and can't be reached this way.

        History is preserved by resuming the existing session_id: Claude Code
        stores the transcript locally (~/.claude/projects/<cwd>/<id>.jsonl) and
        the Anthropic API is stateless, so the id ports across Anthropic-
        compatible endpoints — the new provider just receives the full context
        each turn. This is exactly what `--resume <id>` does.
        """
        from . import backends
        if new_backend == self.backend:
            sublime.status_message("Already on provider '{}'".format(self.backend))
            return False
        cur_spec = backends.get(self.backend)
        new_spec = backends.get(new_backend)
        # Claude-bridge family only.
        if cur_spec.bridge_script != "main.py" or new_spec.bridge_script != "main.py":
            sublime.error_message(
                "Can't change provider: '{}' ↔ '{}' crosses bridge families. Changing "
                "only works between Claude-bridge backends (claude + custom Anthropic-"
                "compatible providers).".format(self.backend, new_backend))
            return False
        if not backends.is_available(new_backend):
            sublime.error_message(
                "Provider '{}' is not usable (missing base_url or auth).\n"
                "Run 'Claude: Manage Anthropic Providers' → Test config.".format(new_backend))
            return False
        if self.working:
            sublime.status_message("Interrupt the current task before changing provider")
            return False
        if not self.session_id:
            sublime.status_message("Nothing to change — session not started")
            return False

        # Swap the backend before restart so wake()'s start() rebuilds env for
        # the new provider. resume_id = session_id makes the bridge resume the
        # local transcript on the new endpoint (see start → init_params resume).
        self.backend = new_backend
        if self.output and self.output.view:
            self.output.view.settings().set("claude_backend", new_backend)
            self.output.view.settings().set("claude_provider_label", new_spec.label or new_backend)
            # Refresh the title now so the new provider's abbrev shows immediately.
            self.output.set_name(self.display_name)
        sublime.status_message("Changing provider to '{}'…".format(new_backend))
        self.restart()  # sleep(force=True) → wake() resumes session_id on new backend
        return True

    # ─── Terminal Mode ─────────────────────────────────────────────────

    def enter_terminal_mode(self) -> bool:
        """Switch from bridge mode to CLI terminal mode."""
        if not self.session_id or self.terminal_mode:
            return False
        if self.working:
            sublime.status_message("Can't switch to terminal mode while working")
            return False
        cli_cmd = self._resolve_cli_command()
        if not cli_cmd:
            sublime.status_message("No CLI available for this backend")
            return False

        # Record JSONL position so we can replay new entries on return
        jsonl_path = self._find_jsonl_path()
        self._terminal_jsonl_pos = os.path.getsize(jsonl_path) if jsonl_path else 0

        self.sleep()
        self.terminal_mode = True
        self._terminal_tag = f"claude-terminal-{self.session_id[:12]}"
        self._show_overlay_phantom("\u2b1b Terminal mode \u2014 CLI running in terminal")
        self._persist_state("terminal")
        self._open_terminal(cli_cmd)
        self._poll_terminal_exit()
        return True

    def _resolve_cli_command(self) -> list:
        import shutil
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        if self.backend == "codex":
            cli = settings.get("codex_cli_path") or shutil.which("codex") or "codex"
            return [cli, "resume", self.session_id]
        elif self.backend == "copilot":
            return None  # No CLI available
        elif self.backend == "pi":
            return None  # No direct terminal resume for pi RPC
        elif self.backend == "dsr":
            # dsr ACP sessions: drop into repl when a named transcript exists;
            # otherwise plain repl in the project cwd.
            cli = os.environ.get("DSR_BIN") or shutil.which("dsr") or "dsr"
            if self.session_id:
                return [cli, "repl", f"--session={self.session_id}"]
            return [cli, "repl"]
        elif self.backend == "grok":
            cli = os.environ.get("GROK_BIN") or shutil.which("grok") or "grok"
            if self.session_id:
                return [cli, "--resume", self.session_id]
            return [cli]
        else:
            cli = (cc_launch.resolve_claude(settings)
                   or settings.get("claude_cli_path") or "claude")
            argv = [cli, "--resume", self.session_id]
            # Same permission posture as the hidden-PTY engine.
            perm = settings.get("pty_permission_mode", "acceptEdits")
            argv += ["--permission-mode", perm]
            return argv

    def _open_terminal(self, cli_cmd: list) -> None:
        """Run the CLI in our embedded terminal view, injecting the sublime MCP
        server (scoped to the terminal view's id) and the same env as SDK
        sessions — so the in-terminal Claude has the editor tools."""
        from .terminal.terminal import Terminal
        from .terminal.commands import new_terminal_view
        cwd = self._cwd()
        tag = self._terminal_tag
        name = self.display_name
        window_id = self.window.id()
        backend = self.backend
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        env = cc_launch.load_env(self.window, settings, cwd)

        def do_open():
            win = next((w for w in sublime.windows() if w.id() == window_id), None)
            if not win:
                return
            view = new_terminal_view(win, "CLI: {}".format(name), tag)
            argv = list(cli_cmd)
            if backend == "claude":
                argv += cc_launch.add_dir_args(win)  # extra working dirs
                mcp_cfg = cc_launch.build_sublime_mcp_config(settings, view.id())
                if mcp_cfg:
                    argv += ["--mcp-config", mcp_cfg]
            Terminal(view).start(cmd=argv, cwd=cwd, env=env, tag=tag,
                                 default_title="CLI: {}".format(name))
            win.focus_view(view)
        sublime.set_timeout(do_open, 50)

    def _poll_terminal_exit(self) -> None:
        self._terminal_poll_active = True

        def check():
            if not self.terminal_mode or not self._terminal_poll_active:
                return
            tv = self._find_terminal_view()
            if tv is None or tv.settings().get("claude_terminal_view.finished"):
                self._on_terminal_exit()
                return
            sublime.set_timeout(check, 500)

        sublime.set_timeout(check, 500)

    def _find_terminal_view(self):
        if not self._terminal_tag:
            return None
        for view in self.window.views():
            if view.settings().get("claude_terminal_tag") == self._terminal_tag:
                return view
        return None

    def _on_terminal_exit(self) -> None:
        self._terminal_poll_active = False
        self.terminal_mode = False
        tv = self._find_terminal_view()
        if tv and tv.settings().get("claude_terminal_view.finished"):
            sublime.set_timeout(lambda: tv.close() if tv.is_valid() else None, 200)
        self._terminal_tag = None
        if self.output and self.output.view and self.output.view.is_valid():
            self._replay_terminal_history()
            self.wake()
        else:
            self._persist_state("closed")

    def _replay_terminal_history(self) -> None:
        """Render conversation entries added during terminal mode."""
        jsonl_path = self._find_jsonl_path()
        start_pos = getattr(self, '_terminal_jsonl_pos', 0)
        if not jsonl_path or not os.path.exists(jsonl_path):
            return
        try:
            with open(jsonl_path, "r") as f:
                f.seek(start_pos)
                new_lines = f.readlines()
        except Exception as e:
            print(f"[Claude] terminal sync skipped (jsonl read failed): {e}")
            return
        if not new_lines:
            return
        # Parse and render new conversation turns
        for line in new_lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("isSidechain") or entry.get("isMeta"):
                continue
            etype = entry.get("type")
            if etype == "user":
                msg = entry.get("message", {})
                content = msg.get("content", [])
                # Skip tool_result messages
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                ):
                    continue
                prompt = ""
                if isinstance(content, str):
                    prompt = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            prompt += block.get("text", "")
                if prompt:
                    self.output.text(f"\n◎ {prompt} ▶\n\n")
            elif etype == "assistant":
                msg = entry.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                self.output.text(text)
                elif isinstance(content, str) and content:
                    self.output.text(content)
        self.output.text("\n")

    def _persist_state(self, state: str) -> None:
        """Save session with explicit state override."""
        if not self.session_id:
            return
        sessions = load_saved_sessions()
        for i, s in enumerate(sessions):
            if s.get("session_id") == self.session_id:
                sessions[i]["state"] = state
                save_sessions(sessions)
                return
        # Entry doesn't exist yet — create it
        self._save_session()

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

    # ── Notification dispatch ────────────────────────────────────────────
    # Method-level handlers (top-level RPC notifications from the bridge).
    # For "message" notifications, we then dispatch on params["type"] via
    # the _MESSAGE_HANDLERS table; for "system" messages, on params["subtype"]
    # via _SYSTEM_HANDLERS. Each handler is small and isolated; the giant
    # if/elif chain it replaced lived right here.

    def _on_notification(self, method: str, params: dict) -> None:
        # Pre-built handler set for top-level methods (one-time lookup is fine)
        method_handler = self._notification_method_handlers().get(method)
        if method_handler is not None:
            method_handler(params)
            return
        if method != "message":
            return
        t = params.get("type")
        msg_handler = self._notification_message_handlers().get(t)
        if msg_handler is not None:
            msg_handler(params)

    def _notification_method_handlers(self):
        return {
            "permission_request": self._handle_permission_request,
            "question_request": self._handle_question_request,
            "plan_mode_enter": self._handle_plan_mode_enter,
            "plan_mode_exit": self._handle_plan_mode_exit,
            "plan_response": lambda _p: None,  # handled via pending_plan_approvals in bridge
            "queued_inject": self._on_queued_inject,
            "notification_wake": self._on_notification_wake,
            "loop_scheduled": self._on_loop_scheduled,
        }

    def _notification_message_handlers(self):
        return {
            "tool_use": self._on_msg_tool_use,
            "tool_result": self._on_msg_tool_result,
            "text_delta": self._on_msg_text,
            "text": self._on_msg_text,
            "turn_usage": self._on_msg_turn_usage,
            "result": self._on_msg_result,
            "system": self._on_msg_system,
            # Kimi TodoWrite → ACP plan entries (title/status)
            "plan_todos": self._on_msg_plan_todos,
        }

    # ── method-level handlers ────────────────────────────────────────────

    def _on_queued_inject(self, params: dict) -> None:
        """Bridge-held inject (Claude SDK) after turn end — run if not already queued."""
        message = (params.get("message") or "").strip()
        if not message:
            return
        self._inject_pending = False
        # Dedupe: sticky composer may already have this in _queued_prompts and
        # _on_done will fire it; only run here if we're idle with an empty queue.
        if message in self._queued_prompts:
            return
        if self.working:
            self._queued_prompts.append(message)
            return
        self.working = True
        self.query(message)

    def _on_loop_scheduled(self, params: dict) -> None:
        """Bridge reports the exact next self-wake time (cron or ScheduleWakeup
        or Grok scheduler_create) so the wakeup hint is accurate.
        fire_at None = cleared."""
        self.next_wake_at = params.get("fire_at")
        if self.next_wake_at:
            self.is_looping = True
        else:
            # Cleared (user remove / one-shot done / no remaining jobs).
            self.is_looping = False
        if self.output:
            self.output._update_title()
            self._update_wakeup_banner(show=True)

    @staticmethod
    def _parse_schedule_interval(interval: str) -> Optional[float]:
        """Parse 60s / 5m / 2h / 1d → seconds (min 60)."""
        import re
        s = (interval or "").strip().lower()
        m = re.fullmatch(r"(\d+)\s*([smhd])?", s)
        if not m:
            return None
        n = int(m.group(1))
        unit = m.group(2) or "s"
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        sec = float(n * mult)
        return max(60.0, sec) if sec > 0 else None

    def _on_notification_wake(self, params: dict) -> None:
        """Fire a new query from a notification wake event (timer, channel, etc)."""
        wake_prompt = params.get("wake_prompt", "")
        display_message = params.get("display_message", "")
        if display_message:
            user_message = display_message
        else:
            first_line = wake_prompt.split("\n")[0].strip() if wake_prompt else ""
            user_message = first_line if first_line else "🔔 Notification received"

        # If still working, defer until idle (poll every 500ms)
        if self.working:
            def start_wake_query():
                if not self.working:
                    try:
                        self.query(wake_prompt, display_prompt=user_message)
                    except Exception as e:
                        print(f"[Claude] deferred wake query error: {e}")
                else:
                    sublime.set_timeout(start_wake_query, 500)
            sublime.set_timeout(start_wake_query, 500)
            return

        try:
            self.query(wake_prompt, display_prompt=user_message)
        except Exception as e:
            print(f"[Claude] wake query error: {e}")

    # ── message-level handlers ───────────────────────────────────────────

    def _on_msg_plan_todos(self, params: dict) -> None:
        """ACP plan sessionUpdate (Kimi TodoWrite display blocks) → Tasks UI."""
        if not self.output or not self.working:
            return
        entries = params.get("entries") or []
        if not isinstance(entries, list) or not entries:
            return
        try:
            self.output.apply_plan_todos(entries)
        except Exception as e:
            print(f"[Claude] plan_todos: {e}")

    def _on_msg_tool_use(self, params: dict) -> None:
        name = params.get("name", "")
        tool_input = params.get("input", {})
        background = params.get("background", False)
        tool_id = params.get("id")
        if not name or not name.strip():
            return
        # Drop late tool starts after interrupt / turn end (☐ rows + busy UI)
        if not self.working:
            return
        # A real content event arrived → the retry hint no longer applies.
        self._clear_api_retry_hint()
        # Agent armed a self-wake / cron / Grok scheduler → looping session.
        if name in (
            "ScheduleWakeup", "CronCreate", "scheduler_create",
            "SchedulerCreate",
        ):
            self.is_looping = True
            if name == "ScheduleWakeup":
                try:
                    d = float(tool_input.get("delaySeconds") or 0)
                except (TypeError, ValueError):
                    d = 0
                # mirror the bridge clamp so the displayed time matches the timer
                self.next_wake_at = time.time() + max(60.0, min(d, 3600.0)) if d > 0 else None
            elif name in ("CronCreate", "scheduler_create", "SchedulerCreate"):
                # Prefer bridge loop_scheduled; estimate from interval as fallback.
                interval = (
                    tool_input.get("interval")
                    or tool_input.get("cron")
                    or ""
                )
                if interval and not self.next_wake_at:
                    sec = self._parse_schedule_interval(str(interval))
                    if sec:
                        self.next_wake_at = time.time() + sec
            self.output._update_title()
            self._update_wakeup_banner(show=True)
        if background:
            # Background tools don't take over current_tool (spinner stays on foreground)
            self.output.tool(name, tool_input, tool_id=tool_id, background=True)
            if tool_id:
                # Hold the ToolCall reference so the symbol patch can still fire
                # after this turn's Conversation is dropped from history.
                tc = self.output.find_tool_by_id(tool_id)
                if tc is not None:
                    self._bg_tools[tool_id] = tc
                self._bg_task_ids.add(tool_id)
            self._update_status_bar()
            return
        # Serial Claude path: auto-close previous nameless tool. Concurrent ACP
        # batches all carry ids — auto-done would mark the wrong Read/Bash done
        # early and leave a later same-name ☐ forever pending.
        if (not tool_id and self.current_tool and self.current_tool.strip()
                and self.current_tool != name):
            self.output.tool_done(self.current_tool)
        self.current_tool = name
        self.output.tool(name, tool_input, tool_id=tool_id, background=False)

    def _on_msg_tool_result(self, params: dict) -> None:
        tool_use_id = params.get("tool_use_id")
        content = params.get("content", "")
        if isinstance(content, list):
            content = "\n".join(str(c) for c in content)
        if len(content) > 10000:
            content = content[:10000]
        is_error = params.get("is_error")

        matched = self.output.find_tool_by_id(tool_use_id) if tool_use_id else None
        was_background = matched is not None and matched.status == "background"
        tool_name = matched.name if matched else self.current_tool

        if not tool_name or not str(tool_name).strip():
            self.current_tool = None
            return
        if was_background:
            # Background tool_result is just an ack; final status comes via task_notification
            return
        if tool_name in ("Edit", "Write") and not is_error:
            self._record_edit(tool_name)
        if is_error:
            self.output.tool_error(tool_name, content, tool_id=tool_use_id)
        else:
            self.output.tool_done(tool_name, content, tool_id=tool_use_id)
        if tool_name == self.current_tool:
            self.current_tool = None
        self._update_status_bar()

    def _on_msg_text(self, params: dict) -> None:
        self._clear_api_retry_hint()
        # Late post-interrupt tokens still paint (text() won't re-arm spinner
        # when session.working is False). Composer re-arm is handled by
        # _ensure_idle_input settle timers on interrupt.
        self.output.text(params.get("text", ""))

    def _on_msg_turn_usage(self, params: dict) -> None:
        usage = params.get("usage", {})
        if usage:
            self.context_usage = usage
            self._update_status_bar()

    def _on_msg_result(self, params: dict) -> None:
        # Capture session ID for resume
        if params.get("session_id"):
            self.session_id = params["session_id"]
            self._save_session()
        cost = params.get("total_cost_usd") or 0
        self.total_cost += cost
        try:
            dur = float(params.get("duration_ms") or 0) / 1000.0
        except (TypeError, ValueError):
            dur = 0.0
        # ACP bridges often omit/zero duration_ms — fall back to local elapsed.
        if dur <= 0 and getattr(self, "_query_start", None):
            try:
                dur = max(0.0, time.time() - self._query_start)
            except Exception:
                pass
        usage = params.get("usage")
        if usage:
            self.context_usage = usage
        print(f"[Claude] [{dur:.1f}s, ${cost:.4f}]" if cost else f"[Claude] [{dur:.1f}s]")
        if usage:
            print(f"[Claude] usage: {usage}")
        stop = params.get("stop_reason") or params.get("stopReason") or ""
        if (
            params.get("status") == "interrupted"
            or stop in ("interrupted", "cancelled", "canceled")
        ):
            # Manual interrupt — _on_done renders *[interrupted]*. Skip @done
            # and "turn failed" (and auto-retry). ACP sends stop_reason without
            # status on the message notification.
            if self.output.current:
                self.output.current.working = False
            return
        if params.get("is_error"):
            # Turn ended in error (e.g. provider 503 retries exhausted). Don't
            # write the normal @done meta — that falsely signals success. Mark
            # the turn idle and surface a brief error note; _on_done adds the
            # detailed message if the bridge also returns an error response.
            stop = stop or "error"
            if self.output.current:
                self.output.current.working = False
            if self._maybe_schedule_auto_retry(stop):
                return  # retry scheduled; don't finalize as a hard failure yet
            retries = self._auto_retry_count
            suffix = f" after {retries} auto-retr{'y' if retries == 1 else 'ies'}" if retries else ""
            self.output.text(f"\n\n*⚠ turn failed ({stop}){suffix}.*\n")
            self._mark_error_halt(f"turn failed ({stop}){suffix}")
            self._status("error")
            try:
                self.output.set_name(self.name or "Claude")
            except Exception:
                pass
        else:
            self.output.meta(dur, cost, usage=usage)
        self._update_status_bar()
        # Workflows run in the background past turn-end — their redirect/detail
        # are persistent now (no turn-end clear).

    def _maybe_schedule_auto_retry(self, stop: str) -> bool:
        """On a failed turn, optionally schedule a plugin-level re-issue of the
        same prompt (opt-in via `auto_retry_turns`). The SDK already exhausted
        its in-request retries; this is one level up, with a backoff so the
        provider/rate-limit can recover. Returns True if a retry was scheduled."""
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        max_turns = settings.get("auto_retry_turns", 0) or 0
        if max_turns <= 0:
            return False
        if self._auto_retry_count >= max_turns:
            return False
        # Capture the prompt to re-issue. self.current is the just-failed turn.
        prompt = self.current.prompt if self.current and self.current.prompt else None
        if not prompt or not self.client or not self.initialized:
            return False
        self._auto_retry_count += 1
        backoff = settings.get("auto_retry_backoff_seconds", 20) or 20
        attempt = self._auto_retry_count
        self._auto_retry_pending = True
        self.output.text(
            f"\n*⚠ turn failed ({stop}) — auto-retry {attempt}/{max_turns} "
            f"in {backoff}s…*\n")
        self._status(f"⚠ auto-retry {attempt}/{max_turns} in {backoff}s")
        sublime.set_timeout(
            lambda _p=prompt, _a=attempt, _m=max_turns: self._do_auto_retry(_p, _a, _m),
            int(backoff * 1000))
        return True

    def _do_auto_retry(self, prompt: str, attempt: int, max_turns: int) -> None:
        """Fire a scheduled auto-retry (cancels if the user submitted/interrupted
        in the meantime — query() clears _auto_retry_pending for non-retry calls)."""
        if not self._auto_retry_pending:
            return  # cancelled by a user query / interrupt
        self._auto_retry_pending = False
        if not self.client or not self.initialized:
            return
        print(f"[Claude] auto-retry {attempt}/{max_turns}: re-issuing turn")
        self.query(prompt, _auto_retry=True)

    def _on_msg_system(self, params: dict) -> None:
        """Dispatch system messages by subtype."""
        handlers = {
            "compact_boundary": self._on_sys_compact_boundary,
            "task_started": self._on_sys_task_started,
            "task_updated": self._on_sys_task_updated,
            "task_notification": self._on_sys_task_notification,
            "task_progress": self._on_sys_task_progress,
            "api_retry": self._on_sys_api_retry,
        }
        h = handlers.get(params.get("subtype", ""))
        if h is not None:
            h(params.get("data", {}) or {})

    # ── system message subtypes ──────────────────────────────────────────

    def _on_sys_compact_boundary(self, _data: dict) -> None:
        self.context_usage = None
        self._update_status_bar()
        self._inject_retain_midquery()

    def _on_sys_api_retry(self, data: dict) -> None:
        """Surface provider API retries (429/5xx) so a busy/failing provider
        isn't a silent hang. Transient only: the hint lives in the status bar /
        spinner (current_tool) while retrying and is cleared as soon as content
        resumes (see _clear_api_retry_hint) — no permanent transcript line.
        """
        attempt = data.get("attempt")
        max_retries = data.get("max_retries")
        status = data.get("error_status")
        exhausted = attempt is not None and max_retries and attempt >= max_retries
        tag = str(status) if status else "error"
        hint = "⚠ {} retry {}/{}{}".format(
            tag, attempt, max_retries,
            " · exhausted" if exhausted else "")
        self.current_tool = hint
        self._api_retry_hint = hint  # _clear_api_retry_hint drains this on resume
        self._status(hint)

    def _clear_api_retry_hint(self) -> None:
        """Drop the retry hint from current_tool once real content arrives, so it
        doesn't outlive the retry (and so the tool_use handler's tool_done doesn't
        fire on the hint string)."""
        if getattr(self, "_api_retry_hint", None):
            self._api_retry_hint = None
            if self.current_tool and self.current_tool.startswith("⚠"):
                self.current_tool = None

    # task_updated.patch.status / task_notification.status values that mean the
    # task is over (the SDK schema dropped the old `is_backgrounded` patch flag
    # in favour of a status patch — see docs/… and the bridge log).
    _TASK_TERMINAL = ("completed", "failed", "cancelled", "canceled",
                      "error", "errored", "aborted", "timeout", "crashed")

    def _finalize_bg_tool(self, tool_use_id: str, keep: bool) -> None:
        """Visually finalize a background tool line — idempotent. `keep` → flip
        ⚙ to ✓ (a real result worth keeping); else remove the line as noise.
        Safe to call from both task_updated and task_notification."""
        from .output import BACKGROUND, DONE
        tool = self._bg_tools.get(tool_use_id) or self.output.find_tool_by_id(tool_use_id)
        if tool is None or tool.status != BACKGROUND:
            self._bg_tools.pop(tool_use_id, None)  # drop a stale registry entry
            return  # already finalized (or never a live ⚙) — nothing to do
        if keep:
            tool.status = DONE
            if self.output._is_in_current(tool):
                self.output._render_current()
            else:
                self.output._patch_tool_symbol(tool, BACKGROUND)
        else:
            self.output.remove_tool(tool)
        self._bg_tools.pop(tool_use_id, None)
        # Do NOT exit+re-enter sticky ◎ here. That rebuilt the queue hairline /
        # pad phantoms every bg-task finish and made the composer jump (and
        # inflated scroll range). Stale ⚙ hints above ◎ clear on next natural
        # enter_input / turn end.

    def _on_sys_task_started(self, data: dict) -> None:
        task_id = data.get("task_id", "")
        tool_use_id = data.get("tool_use_id", "")
        if task_id and tool_use_id:
            self._task_tool_map[task_id] = tool_use_id
            self._schedule_bg_poll()

    def _on_sys_task_updated(self, data: dict) -> None:
        # The SDK task schema changed: `patch` now carries {status, end_time}
        # (no more `is_backgrounded`). A terminal status here is the RELIABLE
        # completion signal — it fires even when a task_notification doesn't
        # (e.g. a task killed out-of-band), so it's the primary cleanup path.
        task_id = data.get("task_id", "")
        status = (data.get("patch") or {}).get("status", "")
        if not task_id or status not in self._TASK_TERMINAL:
            return
        # Read (don't pop) the map: a task_notification may still arrive and
        # needs it to resolve the id (to wake + discard _bg_task_ids). It pops
        # the entry; reconcile cleans it if no notification ever comes.
        tool_use_id = self._task_tool_map.get(task_id)
        if not tool_use_id:
            return
        # No output_file on task_updated → keep a completed line as ✓, drop the
        # rest. A leading task_notification (which has output) may already have
        # finalized it; _finalize_bg_tool is idempotent. Leave _bg_task_ids so
        # the notification can still wake the agent.
        self._finalize_bg_tool(tool_use_id, keep=(status == "completed"))

    def _on_sys_task_notification(self, data: dict) -> None:
        task_id = data.get("task_id", "")
        status = data.get("status", "")
        # Use tool_use_id from the SDK directly; clean up _task_tool_map.
        tool_use_id = data.get("tool_use_id") or self._task_tool_map.get(task_id)
        self._task_tool_map.pop(task_id, None)
        if not status or not tool_use_id:
            return
        # Wake gate. run_in_background tools always wake (their tool_result was
        # just an ack; this notification carries the real result). For anything
        # else, only wake when the session is IDLE — that's the orphaned-subagent
        # case: the SDK backgrounded a task without run_in_background (e.g. a
        # Task/Agent subagent) and the parent turn already ended expecting a
        # wake that would otherwise never come. When mid-turn (self.working),
        # skip: the blocking tool_result drives continuation and a wake here
        # would duplicate it.
        is_bg = tool_use_id in self._bg_task_ids
        self._bg_task_ids.discard(tool_use_id)
        if not is_bg and self.working:
            return
        if not is_bg:
            print(f"[Claude] orphan task_notification (idle session) — waking parent: {tool_use_id}")
        # Read output first — it decides whether the tool line is worth keeping.
        # Cap size: bridge logs show 15–30k task dumps (GLM agent reports) which
        # bloat wake prompts and the session scroll range when echoed.
        output = ""
        output_file = data.get("output_file", "")
        if output_file:
            try:
                with open(output_file, "r") as f:
                    output = f.read().strip()
            except Exception as e:
                print(f"[Claude] task notification output read failed ({output_file}): {e}")
        _MAX_BG_OUT = 8000
        if len(output) > _MAX_BG_OUT:
            output = (
                output[:_MAX_BG_OUT]
                + f"\n…[truncated {len(output) - _MAX_BG_OUT} chars; full: {output_file}]"
            )
        # Keep ✓ only for a completed task with a real surfaced result.
        self._finalize_bg_tool(tool_use_id, keep=(status == "completed" and bool(output)))
        self._bg_tools.pop(tool_use_id, None)
        summary = data.get("summary", "")
        # Prefer short summary for header; long summary alone also blows scroll
        if summary and len(summary) > 400:
            summary = summary[:400] + "…"
        header = f"{summary} [{status}]" if status != "completed" else summary
        block = (
            f"<task-notification>{header}\n{output}</task-notification>"
            if output else f"<task-notification>{header}</task-notification>"
        )
        # Coalesce: append to buffer and schedule a debounced flush. Multiple
        # bg tasks finishing within the window are sent as a single wake.
        self._pending_bg_notifications.append(block)
        if not self._bg_flush_scheduled:
            self._bg_flush_scheduled = True
            sublime.set_timeout(self._flush_bg_notifications, 400)

    def _flush_bg_notifications(self) -> None:
        """Flush coalesced bg-task notifications into a single wake prompt."""
        self._bg_flush_scheduled = False
        if not self._pending_bg_notifications:
            return
        blocks = self._pending_bg_notifications
        self._pending_bg_notifications = []
        wake_prompt = "\n".join(blocks)
        # Display: short single-line summary regardless of how many merged
        n = len(blocks)
        display = f"{BACKGROUND_PREFIX}{n} task notification{'s' if n != 1 else ''}"
        if self.working:
            self._queued_prompts.append(wake_prompt)
        else:
            self.query(wake_prompt, display_prompt=display, silent=True)

    # ── workflow (ultracode) live panel ──────────────────────────────────────
    # state → glyph. start/queued/progress confirmed from the live bridge log;
    # the failure/cancel set is defensive (real strings still unconfirmed — a
    # succeeding run never emits them), unknown → neutral '?'.
    _WF_GLYPH = {"start": "○", "queued": "○", "progress": "◐", "running": "◐",
                 "done": "✔", "completed": "✔", "success": "✔",
                 "failed": "✘", "error": "✘", "errored": "✘", "crashed": "✘",
                 "cancelled": "⊘", "canceled": "⊘", "aborted": "⊘", "timeout": "⊘"}
    _WF_DONE = ("done", "completed", "success")

    @staticmethod
    def _wf_tokens(n) -> str:
        try:
            n = int(n or 0)
        except Exception:
            return "0"
        return f"{n/1000:.1f}k" if n >= 1000 else str(n)

    @staticmethod
    def _wf_model(m: str) -> str:
        if not m:
            return ""
        for k in ("opus", "sonnet", "haiku", "fable"):
            if k in m:
                return k
        return m.split("-")[0][:8]

    def _on_sys_task_progress(self, data: dict) -> None:
        """Live ultracode workflow progress. The workflow_progress[] tree mixes
        `workflow_phase` ({index,title}) and `workflow_agent` entries; render a
        live per-phase/per-agent panel. High-frequency + full re-snapshot each
        tick, so suppress no-op ticks (only timers moved)."""
        task_id = data.get("task_id", "")
        wp = data.get("workflow_progress") or []
        if not task_id or not wp:
            return
        # task_progress ticks are PARTIAL (only the agents that changed this
        # tick), so accumulate per-agent state keyed by (phaseIndex, index)
        # rather than treating each tick as the full set — otherwise counts jump
        # (6→1) and the panel never coheres.
        wf = self._workflows.get(task_id)
        if not wf or "agents" not in wf:
            wf = {"agents": {}, "phases": {}, "summary": "", "sig": None}
            self._workflows[task_id] = wf
        wf["summary"] = data.get("summary", "") or wf["summary"]
        touched = False
        for e in wp:
            if not isinstance(e, dict):
                continue
            if e.get("type") == "workflow_phase":
                wf["phases"][e.get("index")] = e.get("title") or wf["phases"].get(e.get("index"), "")
            elif e.get("type") == "workflow_agent":
                key = (e.get("phaseIndex"), e.get("index"))
                wf["agents"][key] = {**wf["agents"].get(key, {}), **e}
                touched = True
        if not touched:
            return
        agents = list(wf["agents"].values())
        sig = tuple(sorted(
            (str(a.get("phaseIndex")), a.get("index") or 0, a.get("state"),
             a.get("toolCalls") or 0, a.get("tokens") or 0, a.get("lastToolName") or "")
            for a in agents))
        if wf.get("sig") == sig:
            return  # truly nothing changed
        wf["sig"] = sig
        wf["done"] = sum(1 for a in agents if a.get("state") in self._WF_DONE)
        wf["total"] = len(agents)
        wf["completed"] = bool(agents) and all(a.get("state") in self._WF_DONE for a in agents)
        # Clickable redirect in the conversation (persists past turn-end) + live
        # detail in the workflow's own view if the user opened it.
        self._render_all_workflow_redirects()
        self._render_workflow_detail(task_id)

    def _get_workflow_phantom_set(self):
        return self._phantom_set_for("_workflow_phantom_set", "claude_workflow")

    def _workflow_anchor_key(self, task_id: str) -> str:
        # Region key must be a valid sublime region name (no spaces).
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (task_id or "wf"))[:48]
        return f"claude_workflow_anchor_{safe}"

    def _ensure_workflow_anchor(self, task_id: str) -> int:
        """Stable phantom anchor: prefer related Task tool line, else sticky HIDDEN region.

        Avoids the old EOF-only placement that jumped every render and clobbered
        multi-workflow phantoms into one last-newline point.
        """
        view = self.output.view if self.output else None
        if not view:
            return 0
        key = self._workflow_anchor_key(task_id)
        existing = view.get_regions(key)
        if existing:
            return existing[0].begin()

        pt = None
        # 1) tool_use_id from task_started map → find that tool's line in the buffer
        tool_use_id = self._task_tool_map.get(task_id)
        if tool_use_id and self.output:
            tool = self.output.find_tool_by_id(tool_use_id)
            if tool is None:
                # also search DONE tools — find_tool_by_id only checks pending/bg
                for conv in list(self.output.conversations) + (
                        [self.output.current] if self.output.current else []):
                    if not conv:
                        continue
                    for e in conv.events:
                        if getattr(e, "id", None) == tool_use_id:
                            tool = e
                            break
            if tool is not None:
                content = view.substr(sublime.Region(0, view.size()))
                import re
                from .output import BACKGROUND, DONE, ERROR
                sym = self.output.SYMBOLS.get(getattr(tool, "status", BACKGROUND), "⚙")
                name = getattr(tool, "name", "Task") or "Task"
                prefix = f"  {sym} {name}"
                m = re.search(re.escape(prefix), content)
                if m is not None:
                    # place just after the tool line
                    line = view.line(m.start())
                    pt = min(line.end() + 1, view.size())

        # 2) fallback: before input area if known, else last content newline
        if pt is None:
            if getattr(self.output, "_input_mode", False) and getattr(self.output, "_input_start", 0):
                pt = max(0, self.output._input_start - 1)
            else:
                content = view.substr(sublime.Region(0, view.size()))
                last_nl = content.rfind("\n")
                pt = last_nl if last_nl >= 0 else 0

        # Stable region so later ticks re-use the same point even as buffer grows
        view.add_regions(key, [sublime.Region(pt, pt)], "", "", sublime.HIDDEN)
        return pt

    def _render_all_workflow_redirects(self) -> None:
        """Rebuild redirect phantoms for every tracked workflow (no clobber)."""
        ps = self._get_workflow_phantom_set()
        if not ps or not self.output or not self.output.view:
            return
        if not self._workflows:
            ps.update([])
            return
        esc = lambda s: str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        phantoms = []
        # Stable order so multi-workflow layout doesn't thrash
        for task_id in sorted(self._workflows.keys()):
            wf = self._workflows.get(task_id)
            if not wf or "agents" not in wf:
                continue
            pt = self._ensure_workflow_anchor(task_id)
            g = "✓" if wf.get("completed") else "⚙"
            label = (f'{g} workflow: {esc(wf.get("summary"))[:32]} · '
                     f'{wf.get("done", 0)}/{wf.get("total", 0)} agents · '
                     f'<a href="open">open ↗</a>')
            html = (f'<body style="margin:4px 0; padding:1px 8px; '
                    f'color:color(var(--foreground) alpha(0.65)); '
                    f'background-color:color(var(--background) blend(var(--foreground) 96%));">'
                    f'{label}</body>')
            phantoms.append(sublime.Phantom(
                sublime.Region(pt, pt), html, sublime.LAYOUT_BLOCK,
                lambda href, tid=task_id: self._open_workflow_view(tid)))
        ps.update(phantoms)

    def _render_workflow_redirect(self, task_id: str) -> None:
        """Compat: single-id entry → full multi-workflow redraw."""
        self._render_all_workflow_redirects()

    def _find_view_by_id(self, vid: int):
        for w in sublime.windows():
            for v in w.views():
                if v.id() == vid:
                    return v
        return None

    def _open_workflow_view(self, task_id: str) -> None:
        """Create or focus the dedicated detail view for a workflow."""
        if not hasattr(self, "_workflow_views"):
            self._workflow_views = {}
        vid = self._workflow_views.get(task_id)
        if vid:
            v = self._find_view_by_id(vid)
            if v:
                v.window().focus_view(v)
                return
            self._workflow_views.pop(task_id, None)
        if not self.window:
            return
        wf = self._workflows.get(task_id) or {}
        v = self.window.new_file()
        v.set_scratch(True)
        v.set_read_only(True)
        v.set_name(f"⚙ {(wf.get('summary') or 'workflow')[:24]}")
        v.settings().set("claude_workflow_view", task_id)
        if self.output and self.output.view:
            v.settings().set("claude_workflow_parent", self.output.view.id())
        # Mirror the session view's chrome so the detail view looks consistent
        # (font size incl. zoom, zero-gutter, no line numbers, color scheme).
        if self.output and self.output.view:
            src = self.output.view.settings()
            dst = v.settings()
            for key in ("font_size", "line_numbers", "gutter", "margin", "word_wrap",
                        "draw_indent_guides", "draw_white_space", "highlight_line",
                        "fold_buttons", "fade_fold_buttons", "rulers", "scroll_past_end",
                        "color_scheme"):
                val = src.get(key)
                if val is not None:
                    dst.set(key, val)
        v.settings().set("auto_indent", False)
        v.settings().set("is_widget", False)
        self._workflow_views[task_id] = v.id()
        self._render_workflow_detail(task_id)

    def _render_workflow_detail(self, task_id: str) -> None:
        """Render the rich live panel into the workflow's detail view (if open)."""
        if not hasattr(self, "_workflow_views"):
            self._workflow_views = {}
        if not hasattr(self, "_workflow_view_ps"):
            self._workflow_view_ps = {}
        vid = self._workflow_views.get(task_id)
        if not vid:
            return
        view = self._find_view_by_id(vid)
        if view is None:
            self._workflow_views.pop(task_id, None)
            self._workflow_view_ps.pop(task_id, None)
            return
        wf = self._workflows.get(task_id)
        if not wf or "agents" not in wf:
            return
        html = self._build_workflow_html(list(wf["agents"].values()), wf["phases"],
                                         wf["summary"], wf["done"], wf["total"], wf["completed"])
        if view.size() == 0:  # anchor for the block phantom
            view.set_read_only(False)
            view.run_command("append", {"characters": "\n"})
            view.set_read_only(True)
        ps = self._workflow_view_ps.get(task_id)
        if ps is None:
            ps = sublime.PhantomSet(view, "wf_detail")
            self._workflow_view_ps[task_id] = ps
        ps.update([sublime.Phantom(sublime.Region(0, 0), html, sublime.LAYOUT_BLOCK)])

    # state -> (glyph, minihtml colour)
    _WF_STATE_STYLE = {
        "queued":   ("○", "color(var(--foreground) alpha(0.45))"),
        "start":    ("◔", "color(var(--foreground) alpha(0.65))"),
        "progress": ("◐", "var(--yellowish)"),
        "done":     ("✔", "var(--greenish)"),
        "success":  ("✔", "var(--greenish)"),
        "error":    ("✘", "var(--redish)"),
        "failed":   ("✘", "var(--redish)"),
    }

    @classmethod
    def _build_workflow_html(cls, agents: list, phases: dict, summary: str,
                             done: int, total: int, completed: bool) -> str:
        import time as _t
        esc = lambda s: str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        dim = "color:color(var(--foreground) alpha(0.5))"
        now = _t.time() * 1000

        def fmt(ms_start):
            if not ms_start:
                return ""
            s = max(0, int(now - ms_start) // 1000)
            return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"

        tok = sum(int(a.get("tokens") or 0) for a in agents)
        starts = [a.get("startedAt") for a in agents if a.get("startedAt")]
        elapsed = fmt(min(starts)) if starts else ""
        filled = int(round((done / total) * 12)) if total else 0
        bar = "▰" * filled + "▱" * (12 - filled)
        bar_col = "var(--greenish)" if completed else "var(--accent)"
        hglyph = "✓" if completed else "⚡"

        out = ['<body id="wf" style="margin:0; padding:10px 14px; line-height:1.45;">']
        out.append(f'<div style="font-size:1.2rem; font-weight:bold;">{hglyph} {esc(summary)[:72]}</div>')
        out.append(f'<div style="margin:5px 0 12px 0;">'
                   f'<span style="color:{bar_col}; font-size:1.05rem;">{bar}</span>'
                   f'&nbsp;&nbsp;<span style="font-weight:bold;">{done}/{total}</span> '
                   f'<span style="{dim}">agents&nbsp;·&nbsp;{cls._wf_tokens(tok)} tok&nbsp;·&nbsp;{elapsed}</span></div>')

        # Completion: collapse to header only (proposal §2 / G3). Keeps history short;
        # full per-agent state still lives in _workflows if needed later.
        if completed:
            previews = []
            for a in sorted(agents, key=lambda x: (x.get("phaseIndex") or 0, x.get("index") or 0)):
                lab = esc(a.get("label"))[:24]
                rp = esc(a.get("resultPreview") or "")[:40]
                previews.append(f'✔ {lab}' + (f' — {rp}' if rp else ""))
            if previews:
                out.append(f'<div style="{dim}; margin-top:2px;">' +
                           '<br>'.join(previews[:12]) + '</div>')
            out.append('</body>')
            return "".join(out)

        by_phase = {}
        for a in agents:
            by_phase.setdefault(a.get("phaseIndex"), []).append(a)
        for pidx in sorted(by_phase, key=lambda x: (x is None, x)):
            pa = by_phase[pidx]
            pdone = sum(1 for a in pa if a.get("state") in cls._WF_DONE)
            pg = "✔" if pdone == len(pa) else ("◐" if any(a.get("state") == "progress" for a in pa) else "○")
            title = phases.get(pidx, "") or (f"Phase {pidx}" if pidx is not None else "")
            if title or len(by_phase) > 1:
                out.append(f'<div style="margin:10px 0 3px 0; font-weight:bold; color:var(--bluish);">'
                           f'{pg}&nbsp;{esc(title)} <span style="{dim}; font-weight:normal;">'
                           f'({pdone}/{len(pa)})</span></div>')
            for a in sorted(pa, key=lambda x: x.get("index") or 0):
                glyph, col = cls._WF_STATE_STYLE.get(a.get("state"), ("·", dim))
                model = esc(cls._wf_model(a.get("model")))
                label = esc(a.get("label"))[:30]
                attempt = a.get("attempt") or 1
                retry = f' <span style="color:var(--redish);">↻{attempt}</span>' if attempt and attempt > 1 else ""
                if a.get("durationMs"):
                    s = int(a["durationMs"]) // 1000
                    ael = f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"
                elif a.get("state") not in cls._WF_DONE:
                    ael = fmt(a.get("startedAt"))
                else:
                    ael = ""
                if a.get("state") in cls._WF_DONE:
                    rp = esc(a.get("resultPreview"))[:64]
                    act = f'<span style="{dim}">{rp}</span>' if rp else ""
                else:
                    tn, tsum = esc(a.get("lastToolName")), esc(a.get("lastToolSummary"))[:46]
                    act = (f'<span style="color:var(--cyanish, var(--bluish));">{tn}</span> '
                           f'<span style="{dim}">{tsum}</span>') if tn else ""
                meta = f'{a.get("toolCalls") or 0}t&nbsp;·&nbsp;{cls._wf_tokens(a.get("tokens"))}'
                if ael:
                    meta += f'&nbsp;·&nbsp;{ael}'
                out.append(f'<div style="margin:2px 0 2px 10px;">'
                           f'<span style="color:{col};">{glyph}</span>&nbsp;'
                           f'<span style="font-weight:bold;">{label}</span>{retry}&nbsp;'
                           f'<span style="{dim}">{model}</span>&nbsp;&nbsp;{act}'
                           f'&nbsp;&nbsp;<span style="{dim}">{meta}</span></div>')
        out.append('</body>')
        return "".join(out)

    def _schedule_bg_poll(self) -> None:
        """Start bg-task poll timer if not already running."""
        if self._bg_poll_timer is not None or not self._task_tool_map:
            return
        self._bg_poll_timer = sublime.set_timeout(self._bg_poll, 5000)

    def _bg_poll(self) -> None:
        """Periodically poll bridge for buffered task_notification messages."""
        self._bg_poll_timer = None
        if not self._task_tool_map or not self.client or not self.initialized:
            return
        if self.working:
            # Active query — run_query() handles it; retry after it ends
            self._bg_poll_timer = sublime.set_timeout(self._bg_poll, 3000)
            return
        self.client.send("poll_bg_tasks", {}, self._on_bg_poll_result)

    def _on_bg_poll_result(self, result: dict) -> None:
        checked = result.get("checked", 0)
        pending = result.get("pending", 0)
        if checked:
            print(f"[Claude] bg_poll: checked={checked} pending_bridge={pending} pending_plugin={len(self._task_tool_map)}")
        self._reconcile_bg_tools(result.get("running"))
        if self._task_tool_map:
            self._bg_poll_timer = sublime.set_timeout(self._bg_poll, 8000)

    def _reconcile_bg_tools(self, running=None) -> None:
        """Memory hygiene + missed-event recovery.

        Always drops registry entries whose ⚙ line was already finalized. When
        the bridge reports its live-task set (`running`), also finalizes any
        tracked background task that we *saw* running and which has since
        vanished — i.e. it ended without a terminal event reaching us (the only
        case the task_updated path can't catch)."""
        from .output import BACKGROUND
        for tid in list(self._bg_tools):
            tool = self._bg_tools.get(tid)
            if tool is None or tool.status != BACKGROUND:
                self._bg_tools.pop(tid, None)
        if running is None:
            return
        live = set(running)
        self._seen_running |= live
        for task_id, tool_use_id in list(self._task_tool_map.items()):
            if (tool_use_id in self._bg_task_ids
                    and task_id in self._seen_running and task_id not in live):
                self._finalize_bg_tool(tool_use_id, keep=False)
                self._bg_task_ids.discard(tool_use_id)
                self._task_tool_map.pop(task_id, None)
                self._bg_task_ids.discard(tool_use_id)
                self._bg_tools.pop(tool_use_id, None)
                self._seen_running.discard(task_id)

    def _set_name(self, name: str) -> None:
        """Set session name and update UI."""
        self.name = name
        self.output.set_name(name)
        self._update_status_bar()
        self._save_session()

    def _persist_view_identity(self) -> None:
        """Stamp session_id + backend on the view so ST restart can resume.

        Tab-title matching is fragile (truncation, newlines, GM> prefix). View
        settings survive session restore and are the reliable reconnect key.
        """
        sid = self.session_id or self.resume_id
        if not sid or not self.output or not self.output.view:
            return
        try:
            view = self.output.view
            if not view.is_valid():
                return
            view.settings().set("claude_session_id", sid)
            if self.backend:
                view.settings().set("claude_backend", self.backend)
        except Exception:
            pass

    def _save_session(self) -> None:
        """Save session info to disk for later resume."""
        if not self.session_id:
            return
        # Ephemeral panel agent — never pollute the resume list.
        if getattr(self, "quick_mode", False):
            return
        # Always re-stamp view identity (covers init + rename + sleep)
        self._persist_view_identity()
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
        entry["backend"] = self.backend
        entry["last_activity"] = self.last_activity
        # Derive state from current session state
        if self.client is not None and self.initialized:
            entry["state"] = "open"
        elif self.session_id and self.client is None and not self.initialized:
            entry["state"] = "sleeping"
        else:
            entry.setdefault("state", "closed")
        if self._pending_resume_at:
            entry["resume_session_at"] = self._pending_resume_at
        else:
            entry.pop("resume_session_at", None)
        # Persist optional state for post-resume continuity
        if self.context_usage:
            entry["context_usage"] = self.context_usage
        else:
            entry.pop("context_usage", None)
        if self.plan_file:
            entry["plan_file"] = self.plan_file
        else:
            entry.pop("plan_file", None)
        # First-line prompt hint for restore when session_id missing on view
        if self.name:
            entry["first_prompt"] = str(self.name).split("\n", 1)[0].strip()[:200]
        sessions.insert(0, entry)
        # Keep last 200 sessions
        sessions = sessions[:200]
        save_sessions(sessions)

    def _resolve_effort(self, settings=None, env=None, spec=None) -> str:
        """profile → provider → CLAUDE_CODE_EFFORT_LEVEL → settings (default high)."""
        if settings is None:
            settings = sublime.load_settings("ClaudeCode.sublime-settings")
        if env is None:
            env = os.environ
        if spec is None:
            try:
                spec = backends.get(self.backend)
            except Exception:
                spec = None
        if self.profile and self.profile.get("effort"):
            return str(self.profile["effort"]).strip()
        pe = getattr(spec, "effort", None) if spec is not None else None
        if pe:
            return str(pe).strip()
        return str(
            env.get("CLAUDE_CODE_EFFORT_LEVEL")
            or settings.get("effort", "high")
            or "high"
        ).strip()

    def _mcp_enable_read_image(self, settings=None) -> bool:
        """Whether sublime MCP should advertise read_image for this session.

        settings.mcp_enable_read_image:
          true / false — force on/off for all backends
          "auto" (default) — on only for Grok ACP (needs vision over binary FS)
        """
        if settings is None:
            settings = sublime.load_settings("ClaudeCode.sublime-settings")
        raw = settings.get("mcp_enable_read_image", "auto")
        if isinstance(raw, str):
            low = raw.strip().lower()
            if low in ("1", "true", "yes", "on"):
                return True
            if low in ("0", "false", "no", "off"):
                return False
            # "auto" / empty / unknown → Grok ACP only
            return self.backend == "grok"
        if raw is True:
            return True
        if raw is False:
            return False
        return self.backend == "grok"

    # ── Host goal harness ────────────────────────────────────────────────

    def sync_goal_ui(self) -> None:
        """Mirror GoalTracker → Conversation.goal and re-render strip."""
        from .output_models import GoalState, _goal_is_open
        gt = getattr(self, "goal_tracker", None)
        if not self.output:
            return
        if not gt or not gt.is_open():
            # Clear sticky on complete/cleared
            if self.output.current:
                st = gt.status if gt else "cleared"
                if st in ("complete", "cleared") or not gt or not gt.goal_id:
                    self.output.current.goal = None
                elif gt.status == "complete":
                    self.output.current.goal = None
            try:
                if self.output.current:
                    if self.output.is_input_mode():
                        self.output.refresh_preserving_input()
                    else:
                        self.output._render_current()
            except Exception:
                pass
            self._update_status_bar()
            return
        snap = gt.to_ui_dict()
        gs = GoalState(
            status=snap["status"],
            message=snap.get("message") or "",
            blocked_reason=snap.get("blocked_reason") or "",
            objective=snap.get("objective") or "",
            phase=snap.get("phase") or "idle",
            pause_message=snap.get("pause_message") or "",
            token_budget=snap.get("token_budget"),
            tokens_used=snap.get("tokens_used"),
            verify_runs=int(snap.get("verify_runs") or 0),
            verify_max=int(snap.get("verify_max") or 0),
            gaps=list(snap.get("gaps") or []),
            verifying=bool(snap.get("verifying")),
            planning=bool(snap.get("planning")),
            goal_id=snap.get("goal_id") or "",
        )
        if self.output.current:
            self.output.current.goal = gs if _goal_is_open(gs) else None
        try:
            if self.output.current:
                if self.output.is_input_mode():
                    self.output.refresh_preserving_input()
                else:
                    self.output._render_current()
        except Exception as e:
            print(f"[Claude] sync_goal_ui: {e}")
        self._update_status_bar()

    def apply_goal_update(
        self,
        message: str = "",
        completed: bool = False,
        blocked_reason: str = "",
    ) -> dict:
        """MCP / mirrored native update_goal drain (mid-turn)."""
        gt = getattr(self, "goal_tracker", None)
        if gt is None:
            from .goal_tracker import GoalTracker
            self.goal_tracker = GoalTracker()
            gt = self.goal_tracker
        # Dedup: MCP tool + stream tool_use can both fire for one call
        sig = (
            (message or "").strip(),
            bool(completed),
            (blocked_reason or "").strip(),
            int(getattr(self, "query_count", 0) or 0),
        )
        if getattr(self, "_goal_last_update_sig", None) == sig:
            self.sync_goal_ui()
            return {
                "ok": True,
                "deduped": True,
                "status": gt.status,
                "summary": "already applied",
            }
        self._goal_last_update_sig = sig
        # Skeptic subagent / verifier turns must not complete the goal
        if self._goal_is_skeptic():
            return {
                "ok": False,
                "error": "Goal skeptic cannot call update_goal; use goal_verdict.",
                "rejected": True,
            }
        if getattr(self, "_goal_verify_turn", False) and completed:
            return {
                "ok": False,
                "error": "Verifier turn cannot call update_goal(completed=true).",
                "rejected": True,
            }
        result = gt.apply_update(
            message=message or "",
            completed=bool(completed),
            blocked_reason=blocked_reason or "",
            mid_turn=True,
        )
        try:
            self._goal_refresh_tokens()
        except Exception:
            pass
        self.sync_goal_ui()
        return result

    def handle_goal_command(self, args: str) -> None:
        """Handle /goal … (plugin-owned; never forward raw /goal to agent)."""
        from .goal_tracker import parse_goal_slash
        from . import goal_prompts

        if getattr(self, "quick_mode", False):
            self.output.text("\n*Goal mode is not available in Quick Agent.*\n")
            self._ensure_idle_input(reason="goal/quick")
            return

        action, payload = parse_goal_slash(args)
        gt = self.goal_tracker

        if action == "status":
            self.output.text("\n" + gt.status_summary() + "\n")
            self.sync_goal_ui()
            self._ensure_idle_input(reason="goal/status")
            return

        if action == "pause":
            if gt.is_active():
                gt.pause("user", "Paused via /goal pause")
                self.output.text("\n*Goal paused.*\n")
            else:
                self.output.text("\n*" + gt.status_summary() + "*\n")
            self.sync_goal_ui()
            self._ensure_idle_input(reason="goal/pause")
            return

        if action == "clear":
            # Stale plan must not remain "active" after clear
            old_path = (gt.plan_path or "").strip()
            gt.clear()
            self._goal_verify_turn = False
            self.output.text("\n*Goal cleared.*\n")
            if old_path:
                self.output.text(f"  (plan was: {old_path})\n")
            self.sync_goal_ui()
            self._ensure_idle_input(reason="goal/clear")
            return

        if action == "resume":
            if not gt.is_open():
                self.output.text("\n*No goal to resume. /goal <objective>*\n")
                self._ensure_idle_input(reason="goal/resume-empty")
                return
            if gt.status in ("complete", "cleared"):
                self.output.text("\n*Goal already finished. Start a new /goal.*\n")
                self._ensure_idle_input(reason="goal/resume-done")
                return
            gt.resume()
            # Still planning (no plan) → resume planner, not a fake template plan
            if gt.phase == "planning" or not gt.has_plan():
                self._goal_planning_turn = True
                try:
                    from . import goal_plan
                    if not (gt.plan_path or "").strip():
                        gt.plan_path = goal_plan.default_plan_path(
                            self._goal_project_root(), gt.goal_id)
                except Exception:
                    pass
                self.sync_goal_ui()
                self.query(
                    goal_prompts.planner_kickoff(gt, plan_path=gt.plan_path or ""),
                    display_prompt="/goal resume (plan)",
                )
                return
            self.sync_goal_ui()
            recap = goal_prompts.resume_recap(gt)
            self.query(recap, display_prompt="/goal resume")
            return

        # set objective → planning → host plan materialization → execute
        objective, budget = payload
        baseline = 0
        try:
            k = self._context_tokens_k()
            if k is not None:
                baseline = k * 1000
        except Exception:
            pass
        gt.create(objective, token_budget=budget, tokens_baseline=baseline)
        self._goal_verify_turn = False
        self._goal_skip_continue_once = False
        self._goal_planning_turn = True
        # Reserve plan path; model must write a real plan (not host template).
        try:
            from . import goal_plan
            root = self._goal_project_root()
            gt.plan_path = goal_plan.default_plan_path(root, gt.goal_id)
        except Exception:
            pass
        self.sync_goal_ui()
        disp = f"/goal {objective}"
        if budget:
            disp += f" --budget {budget}"
        self.output.text(
            f"\n*Goal planning* — write a concrete plan to "
            f"`{gt.plan_path or '.claude/goals/…/plan.md'}` "
            f"(host rejects templates).\n"
        )
        body = goal_prompts.planner_kickoff(gt, plan_path=gt.plan_path or "")
        self.query(body, display_prompt=disp)

    def _goal_refresh_tokens(self) -> None:
        gt = getattr(self, "goal_tracker", None)
        if not gt or not gt.is_open():
            return
        k = self._context_tokens_k()
        if k is not None:
            gt.set_tokens_used(k * 1000)
        gt.enforce_budget()

    def _goal_project_root(self) -> Optional[str]:
        try:
            folders = self.window.folders() if self.window else None
            if folders:
                return folders[0]
        except Exception:
            pass
        try:
            return self._cwd()
        except Exception:
            return None

    def _goal_try_accept_plan_from_disk(self) -> dict:
        """Load plan.md written by planner; accept if quality gate passes."""
        from . import goal_plan
        gt = self.goal_tracker
        if not gt or not gt.goal_id:
            return {"ok": False, "error": "no goal"}
        path = (gt.plan_path or "").strip() or goal_plan.default_plan_path(
            self._goal_project_root(), gt.goal_id)
        gt.plan_path = path
        body = ""
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    body = f.read()
            except Exception as e:
                return {"ok": False, "error": f"read plan failed: {e}", "path": path}
        if not body.strip():
            return {
                "ok": False,
                "error": f"Plan file missing or empty: {path}",
                "path": path,
                "issues": ["write plan.md with required sections"],
            }
        issues = goal_plan.plan_quality_issues(body, gt.objective)
        if issues:
            return {"ok": False, "error": "quality", "issues": issues, "path": path}
        ack = gt.accept_plan(body, plan_path=path)
        if not ack.get("ok"):
            return {
                "ok": False,
                "error": ack.get("error") or "accept_plan failed",
                "issues": ack.get("issues") or [ack.get("error") or "accept failed"],
                "path": path,
                "host_state": bool(ack.get("host_state")),
            }
        return {"ok": True, "path": path}

    def _goal_is_skeptic(self) -> bool:
        try:
            from .goal_skeptic import is_goal_skeptic
        except ImportError:
            from goal_skeptic import is_goal_skeptic  # type: ignore
        return is_goal_skeptic(self)

    def _goal_abort_task_verify(self, gap: str, message: str = "") -> bool:
        """Fail-closed Task-mode verify on main (error/interrupt).

        Records a not-achieved tool verdict if missing, then runs the finish
        cycle. Returns True when finish started the next turn (continuation) —
        caller must return early and must not force ``working=False``.
        """
        try:
            from .goal_skeptic import (
                prepare_task_verify_abort,
                preserve_working_after_verify_finish,
            )
        except ImportError:
            from goal_skeptic import (  # type: ignore
                prepare_task_verify_abort,
                preserve_working_after_verify_finish,
            )
        gt = getattr(self, "goal_tracker", None)
        if not prepare_task_verify_abort(gt, gap, message=message):
            return False
        self._goal_verify_awaiting = False
        started = bool(self._goal_finish_verify_cycle())
        try:
            self.sync_goal_ui()
        except Exception:
            pass
        return preserve_working_after_verify_finish(started)

    def _goal_on_turn_success(self) -> bool:
        """After successful turn: verify and/or continue. True = started next turn."""
        # Host skeptic subagent: report back to parent; never drive goal loop.
        if self._goal_is_skeptic():
            try:
                self._goal_notify_parent_skeptic_done()
            except Exception as e:
                print(f"[Claude] goal skeptic notify failed: {e}")
            return False

        gt = getattr(self, "goal_tracker", None)
        if not gt or getattr(self, "quick_mode", False):
            return False

        # Verify turn in progress
        if getattr(self, "_goal_verify_awaiting", False):
            mode = getattr(self, "_goal_verify_mode", None) or "task"
            if mode == "task":
                # Main-session executor turn ended (Task skeptic should have run)
                self._goal_verify_awaiting = False
                return self._goal_finish_verify_cycle()
            # Legacy sheet: wait for child notify; keep parent busy
            return True

        # Recovery: phase stuck verifying without await flag (error/race)
        if gt.phase == "verifying" and gt.is_active():
            return self._goal_finish_verify_cycle()

        # Esc / skip: do not accept or re-plan while user paused mid-flight
        if getattr(self, "_goal_skip_continue_once", False):
            self._goal_skip_continue_once = False
            self._goal_planning_turn = False
            return False

        # Planning turn: accept real plan from disk, else re-prompt planner
        if gt.phase == "planning" or getattr(self, "_goal_planning_turn", False):
            self._goal_planning_turn = False
            # Paused mid-plan but plan.md may already be valid — resume first
            if gt.is_open() and not gt.is_active() and not gt.has_plan():
                if gt.status in ("user_paused", "infra_paused"):
                    gt.resume()
                    self.sync_goal_ui()
            result = self._goal_try_accept_plan_from_disk()
            self.sync_goal_ui()
            if result.get("ok"):
                self.output.text(
                    f"\n*Plan accepted* — `{result.get('path')}`\n"
                    f"*Executing…*\n"
                )
                from . import goal_prompts
                self.working = True
                self.query(
                    goal_prompts.implementer_kickoff(gt),
                    display_prompt="↻ goal execute",
                )
                return True
            issues = result.get("issues") or [result.get("error") or "plan invalid"]
            err = str(result.get("error") or "")
            host_state = bool(result.get("host_state")) or any(
                "not active" in str(i).lower()
                or "host state" in str(i).lower()
                or "no open goal" in str(i).lower()
                for i in issues
            ) or "not active" in err.lower() or "no open goal" in err.lower()
            detail = "; ".join(str(i) for i in issues[:6])
            path = result.get("path") or gt.plan_path or ""
            if host_state:
                # Do NOT loop re-plan — that was the Esc/pause friction disaster
                self.output.text(
                    f"\n*Plan accept blocked* (host state, not schema) — {detail}\n"
                    f"  path: `{path}`\n"
                    "  Use `/goal resume` (or start `/goal …` again), then continue.\n"
                )
                self.sync_goal_ui()
                return False
            # Real quality failures → re-plan with schema
            self.output.text(
                f"\n*Plan rejected* (schema/quality gate) — {detail}\n"
                f"  path: `{path}`\n"
                "  Host re-sends full plan.md schema to the planner.\n"
            )
            from . import goal_prompts
            self._goal_planning_turn = True
            self.working = True
            self.query(
                goal_prompts.planner_revise(
                    gt, issues, plan_path=gt.plan_path or ""),
                display_prompt="↻ goal re-plan",
            )
            return True

        # Legacy same-session verify turn (should not run once host skeptic is used)
        if getattr(self, "_goal_verify_turn", False):
            self._goal_verify_turn = False
            return self._goal_finish_verify_cycle()

        try:
            self._goal_refresh_tokens()
        except Exception:
            pass
        if gt.enforce_budget():
            self.sync_goal_ui()
            self.output.text("\n*Goal paused: token budget reached.*\n")
            return False

        if not gt.is_active():
            self.sync_goal_ui()
            return False

        # Deferred complete → host-spawned skeptic subagent
        if gt.has_pending_complete():
            claim = gt.begin_verify()
            self.sync_goal_ui()
            if claim is None:
                # cap hit
                self.output.text("\n*Goal paused: verification cap.*\n")
                self.sync_goal_ui()
                return False
            return self._goal_fire_verifier(claim)

        if gt.should_continue():
            return self._goal_fire_continuation()
        return False

    def _goal_spawn_skeptic_session(self):
        """Create a background subsession for host verification (not implementer)."""
        from .core import create_session
        try:
            from .goal_skeptic import make_skeptic_context, skeptic_display_name
        except ImportError:
            from goal_skeptic import make_skeptic_context, skeptic_display_name  # type: ignore

        parent_vid = self.output.view.id() if self.output and self.output.view else None
        if parent_vid is None:
            raise RuntimeError("parent session has no view")
        gt = self.goal_tracker
        ctx = make_skeptic_context(parent_vid, verify_run=gt.verify_runs)
        child = create_session(
            self.window,
            profile=None,
            initial_context=ctx,
            backend=self.backend,
            focus=False,
        )
        name = skeptic_display_name(gt.verify_runs)
        child.name = name
        child.goal_role = "skeptic"
        child.sleep_disabled = True
        try:
            child.output.set_name(name)
        except Exception:
            pass
        return child

    def _goal_fire_verifier(self, claim: str) -> bool:
        """Start host verification.

        Default (``goal_skeptic_mode: task``): same main session stays the
        goal *executor*; it must fan out a Task/Agent reviewer (no new sheet).

        Legacy (``goal_skeptic_mode: session``): create_session skeptic sheet.
        """
        from . import goal_prompts
        try:
            from .goal_skeptic import resolve_skeptic_mode, MODE_SESSION, MODE_TASK
        except ImportError:
            from goal_skeptic import resolve_skeptic_mode, MODE_SESSION, MODE_TASK  # type: ignore

        gt = self.goal_tracker
        mode = resolve_skeptic_mode()
        self._goal_verify_mode = mode
        self._goal_verify_turn = False
        self._goal_skeptic_view_id = None
        self.working = True

        if mode == MODE_SESSION:
            return self._goal_fire_verifier_session_sheet(claim)

        # In-session Task under main executor (single sheet)
        prompt = goal_prompts.executor_verify_prompt(
            gt.objective,
            claim,
            gaps_prior=gt.gaps or None,
            plan_body=getattr(gt, "plan_body", "") or "",
        )
        self._goal_verify_awaiting = True
        n = gt.verify_runs
        cap = gt.verify_max
        self.output.text(
            f"\n*Goal · verifying* ({n}/{cap}) — stay the **executor** on this "
            "sheet; spawn one Task/Agent **reviewer** (not a new session). "
            "Complete unlocks only via `goal_verdict`.\n"
        )
        self.sync_goal_ui()
        self.query(prompt, display_prompt="↻ goal verify")
        return True

    def _goal_fire_verifier_session_sheet(self, claim: str) -> bool:
        """Legacy: separate ST session for skeptic (discouraged)."""
        from . import goal_prompts
        gt = self.goal_tracker
        prompt = goal_prompts.verifier_prompt(
            gt.objective,
            claim,
            gaps_prior=gt.gaps or None,
            plan_body=getattr(gt, "plan_body", "") or "",
        )
        try:
            child = self._goal_spawn_skeptic_session()
        except Exception as e:
            print(f"[Claude] goal skeptic sheet spawn failed: {e}")
            gt.record_tool_verdict(
                achieved=False,
                evidence=[],
                gaps=[f"Host: skeptic session spawn failed: {e}"[:200]],
                message="spawn failed",
            )
            return self._goal_finish_verify_cycle()

        child_vid = (
            child.output.view.id()
            if child.output and child.output.view else None
        )
        self._goal_skeptic_view_id = child_vid
        self._goal_verify_awaiting = True
        self.output.text(
            f"\n*Goal · verifying* (legacy sheet `{getattr(child, 'name', 'goal-skeptic')}` "
            f"view {child_vid}) — prefer `goal_skeptic_mode=task` for in-sheet verify.\n"
        )
        self.sync_goal_ui()
        self._goal_kick_skeptic_when_ready(child, prompt, attempt=0)
        return True

    def _goal_kick_skeptic_when_ready(self, child, prompt: str, attempt: int = 0) -> None:
        """Poll child init, then fire verifier prompt (MCP spawn pattern)."""
        try:
            if child is None or not getattr(child, "output", None):
                self._goal_skeptic_failed("skeptic session missing")
                return
            view = child.output.view
            if view is None or not view.is_valid():
                self._goal_skeptic_failed("skeptic view closed before init")
                return
            if getattr(child, "initialized", False) and not getattr(child, "working", False):
                child.goal_role = "skeptic"
                child.sleep_disabled = True
                child.query(prompt, display_prompt="↻ goal skeptic")
                return
            if attempt >= 150:  # ~30s @ 200ms
                self._goal_skeptic_failed("skeptic init timeout")
                return
            sublime.set_timeout(
                lambda: self._goal_kick_skeptic_when_ready(child, prompt, attempt + 1),
                200,
            )
        except Exception as e:
            self._goal_skeptic_failed(f"skeptic kick error: {e}")

    def _goal_skeptic_failed(self, reason: str) -> None:
        """Fail-closed when host cannot run the skeptic subagent."""
        gt = getattr(self, "goal_tracker", None)
        self._goal_verify_awaiting = False
        self._goal_skeptic_view_id = None
        if gt is None:
            self.working = False
            return
        if gt.phase == "verifying" and not gt.pending_tool_verdict:
            gt.record_tool_verdict(
                achieved=False,
                evidence=[],
                gaps=[f"Host: {reason}"],
                message=reason[:200],
            )
        self._goal_finish_verify_cycle()

    def _goal_notify_parent_skeptic_done(self, error: str = "") -> None:
        """Skeptic child → parent: apply recorded verdict (or fail-closed)."""
        try:
            from .goal_skeptic import parent_view_id_for_skeptic
        except ImportError:
            from goal_skeptic import parent_view_id_for_skeptic  # type: ignore
        pvid = parent_view_id_for_skeptic(self)
        parent = None
        if pvid is not None and hasattr(sublime, "_claude_sessions"):
            parent = sublime._claude_sessions.get(pvid)
        if parent is None:
            print(f"[Claude] goal skeptic: parent {pvid} gone")
            return
        # If child never called goal_verdict (routed to parent), inject fail-closed
        gt = getattr(parent, "goal_tracker", None)
        if error and gt is not None and gt.phase == "verifying":
            if not getattr(gt, "pending_tool_verdict", None):
                gt.record_tool_verdict(
                    achieved=False,
                    evidence=[],
                    gaps=[error],
                    message=error[:200],
                )
        parent._goal_on_skeptic_finished(self)

    def _goal_on_skeptic_finished(self, child_session=None) -> None:
        """Parent: skeptic subagent finished — apply verdict and continue."""
        if not getattr(self, "_goal_verify_awaiting", False) and \
                getattr(self.goal_tracker, "phase", None) != "verifying":
            return
        self._goal_verify_awaiting = False
        self._goal_skeptic_view_id = None
        self._goal_verify_turn = False
        # Keep parent busy while we apply + maybe continue
        self.working = True
        self._goal_finish_verify_cycle()

    def _goal_finish_verify_cycle(self) -> bool:
        """Apply skeptic verdict on parent and continue or stop. True = next turn started.

        Emits exactly one host banner (complete XOR not-verified) — never both.
        """
        gt = self.goal_tracker
        self._goal_verify_awaiting = False
        self._goal_skeptic_view_id = None
        self._goal_verify_turn = False
        already_complete = gt.status == "complete"
        self._goal_apply_verifier_result()
        # Mode cleared after apply so logs still know path during apply
        self._goal_verify_mode = None
        self.sync_goal_ui()
        if gt.status == "complete":
            try:
                if self.is_looping:
                    self.cancel_scheduled_loop()
            except Exception:
                pass
            # Clear busy *before* host status text — otherwise text() can
            # re-arm conversation.working while session.working is still True
            # and leave a stuck ⠼ after verified complete.
            self.working = False
            if self.output and self.output.current:
                self.output.current.working = False
            self.output.text(
                "\n*Goal · complete* — verified via structured verdict.\n")
            if self.output and self.output.current:
                self.output.current.working = False
                try:
                    self.output._render_pending = False
                    self.output._do_render()
                except Exception:
                    self.output._render_current()
            self.sync_goal_ui()
            try:
                self.output.set_name(self.display_name)
            except Exception:
                pass
            return False
        # Not achieved only — never print this if status is complete
        if gt.phase == "verifying":
            gt.phase = "executing"
        gaps = list(gt.gaps or [])
        if gaps:
            shown = "; ".join(gaps[:3])
            more = f" (+{len(gaps) - 3} more)" if len(gaps) > 3 else ""
            self.output.text(
                f"\n*Goal · not verified* — back to **executing**. Gaps: {shown}{more}\n"
            )
        else:
            self.output.text(
                "\n*Goal · not verified* — back to **executing** "
                "(no `goal_verdict` or empty evidence).\n"
            )
        self.sync_goal_ui()
        if not gt.should_continue():
            self.working = False
            return False
        return self._goal_fire_continuation()

    def apply_goal_verdict(
        self,
        achieved: bool = False,
        evidence=None,
        gaps=None,
        message: str = "",
    ) -> dict:
        """MCP goal_verdict drain — structured skeptic result (verify phase only).

        When called from a skeptic subsession, MCP layer routes to the parent
        first; this method always runs on the goal owner.
        """
        # Defense in depth: if a skeptic somehow lands here, bounce to parent
        if self._goal_is_skeptic():
            try:
                from .goal_skeptic import parent_view_id_for_skeptic
            except ImportError:
                from goal_skeptic import parent_view_id_for_skeptic  # type: ignore
            pvid = parent_view_id_for_skeptic(self)
            parent = (
                sublime._claude_sessions.get(pvid)
                if pvid is not None and hasattr(sublime, "_claude_sessions")
                else None
            )
            if parent is None:
                return {
                    "ok": False,
                    "error": "Skeptic parent session not found for goal_verdict.",
                    "rejected": True,
                }
            return parent.apply_goal_verdict(
                achieved=achieved,
                evidence=evidence,
                gaps=gaps,
                message=message,
            )

        gt = getattr(self, "goal_tracker", None)
        if gt is None:
            return {
                "ok": False,
                "error": "No goal tracker.",
                "rejected": True,
            }
        if getattr(self, "quick_mode", False):
            return {
                "ok": False,
                "error": "goal_verdict is not for Quick Agent.",
                "rejected": True,
            }
        if not hasattr(gt, "record_tool_verdict"):
            return {
                "ok": False,
                "error": (
                    "GoalTracker.record_tool_verdict missing — soft-reload left a "
                    "stale tracker class. Soft/hard reload ClaudeCode package and "
                    "re-open the goal."
                ),
                "rejected": True,
            }
        # Coerce evidence/gaps from string or list
        def _as_list(v):
            if v is None:
                return []
            if isinstance(v, str):
                lines = [ln.strip().lstrip("-* ") for ln in v.splitlines()]
                return [x for x in lines if x]
            if isinstance(v, (list, tuple)):
                return [str(x).strip() for x in v if str(x).strip()]
            return [str(v).strip()] if str(v).strip() else []

        return gt.record_tool_verdict(
            achieved=bool(achieved),
            evidence=_as_list(evidence),
            gaps=_as_list(gaps),
            message=message or "",
        )

    def _goal_apply_verifier_result(self) -> None:
        gt = self.goal_tracker
        # Already complete (e.g. apply_verdict via sublime_eval mid-turn) —
        # do not fail-closed and demote with a not-achieved apply.
        if gt.status == "complete":
            try:
                gt._push("verify_applied", "already_complete")
            except Exception:
                pass
            return

        source = "none"
        achieved = False
        gaps = []
        detail = gt.message

        # Structured unlock only: MCP goal_verdict, else evidence/VERDICT.json
        # (Task children often lack MCP — they write the file; host ingests).
        # Prose never unlocks complete.
        tool_v = None
        source = "none"
        if hasattr(gt, "take_tool_verdict"):
            tool_v = gt.take_tool_verdict()
            if tool_v:
                source = "tool"
        if not tool_v and hasattr(gt, "try_load_verdict_file"):
            try:
                file_rec = gt.try_load_verdict_file()
                if file_rec and file_rec.get("ok"):
                    # record_tool_verdict left pending; consume it
                    tool_v = (
                        gt.take_tool_verdict()
                        if hasattr(gt, "take_tool_verdict") else None
                    )
                    if not tool_v and file_rec.get("recorded") is not False:
                        tool_v = {
                            "achieved": file_rec.get("achieved"),
                            "evidence": [],
                            "gaps": file_rec.get("gaps") or [],
                            "message": file_rec.get("message") or "",
                        }
                    if tool_v:
                        source = "verdict_file"
            except Exception as e:
                print(f"[Claude] goal VERDICT.json ingest: {e}")
        if tool_v:
            achieved = bool(tool_v.get("achieved"))
            gaps = list(tool_v.get("gaps") or [])
            detail = (tool_v.get("message") or detail or "").strip() or detail
        else:
            source = "no_tool"
            achieved = False
            gaps = list(gt.gaps or [])
            if not gaps:
                gaps = [
                    "Host: no goal_verdict / VERDICT.json — complete stays locked "
                    "(MCP goal_verdict or evidence/VERDICT.json next to plan.md)",
                ]

        gt.apply_verdict(achieved, gaps=gaps, detail=detail or gt.message)
        # Transcript banners live in _goal_finish_verify_cycle (single place).
        try:
            gt._push(
                "verify_applied",
                ("achieved" if achieved else "not_achieved") + f" via {source}",
            )
        except Exception:
            pass

    def _goal_fire_continuation(self) -> bool:
        from . import goal_prompts
        gt = self.goal_tracker
        # Never continue while still planning / without a plan contract
        if gt.phase == "planning" or not gt.has_plan():
            return False
        if not gt.should_continue():
            if gt.is_active() and gt.continue_count >= gt.continue_max:
                gt.pause(
                    "user",
                    f"Continue cap ({gt.continue_max}) without verified complete.",
                )
                self.sync_goal_ui()
                self.output.text("\n*Goal paused: continue cap.*\n")
            return False
        gt.note_continue()
        if not gt.is_active():
            self.sync_goal_ui()
            return False
        self.sync_goal_ui()
        prompt = goal_prompts.continuation_directive(gt)
        self.working = True
        self.query(prompt, display_prompt="↻ goal")
        return True

    def _mark_error_halt(self, message: str = "") -> None:
        """Flag session as error-halted so the tab shows ⚠ until next query."""
        self.error_halted = True
        self.error_halt_message = (message or "").strip()[:200]
        try:
            if self.output and self.output.view and self.output.view.is_valid():
                self.output.view.settings().set("claude_error_halted", True)
        except Exception:
            pass

    def _clear_error_halt(self) -> None:
        self.error_halted = False
        self.error_halt_message = ""
        try:
            if self.output and self.output.view and self.output.view.is_valid():
                self.output.view.settings().erase("claude_error_halted")
        except Exception:
            pass

    def _status(self, text: str) -> None:
        """Update status on output view only."""
        if not self.output.view or not self.output.view.is_valid():
            return
        label = backends.get(self.backend).label
        prefix = "[PLAN] " if self.plan_mode else ""
        parts = [f"{prefix}{text}"]
        if self.backend in ("claude", "grok"):
            effort = getattr(self, "effort", None) or self._resolve_effort()
            if effort:
                parts.append(f"effort:{effort}")
        if self.total_cost > 0:
            parts.append(f"${self.total_cost:.4f}")
        if self.query_count > 0:
            parts.append(f"{self.query_count}q")
        if self.context_usage:
            ctx_k = self._context_tokens_k()
            if ctx_k is not None:
                parts.append(f"ctx:{ctx_k}k")
        gt = getattr(self, "goal_tracker", None)
        if gt and gt.is_open():
            try:
                chip = gt.ui_phase_label() or gt.status
            except Exception:
                chip = "verifying" if gt.phase == "verifying" else gt.status
            parts.append(f"goal:{chip}")
        self.output.view.set_status("claude", f"{label}: {', '.join(parts)}")

    def _update_status_bar(self) -> None:
        """Update status bar with session info."""
        if self.is_sleeping:
            self._status("sleeping")
        else:
            self._status("ready")

    def _context_tokens_k(self) -> Optional[int]:
        """Get context token count in thousands from latest usage data."""
        if not self.context_usage:
            return None
        u = self.context_usage
        # Usage fields may be explicitly null (Grok turn_usage mid-stream).
        def _n(key: str) -> int:
            v = u.get(key, 0)
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0
        input_t = (
            _n("input_tokens")
            + _n("cache_read_input_tokens")
            + _n("cache_creation_input_tokens")
        )
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
        from .output import PERM_ALLOW, PERM_ALLOW_ALL, PERM_ALLOW_SESSION

        pid = params.get("id")
        tool = params.get("tool", "Unknown")
        tool_input = params.get("input", {})
        def on_response(response: str) -> None:
            if self.client:
                # ALLOW_ALL / ALLOW_SESSION are normalized to allow in output.py
                # before the callback for Claude UX, but we still receive the
                # original token when output keeps it — accept both.
                allow = response in (PERM_ALLOW, PERM_ALLOW_ALL, PERM_ALLOW_SESSION)
                always = response == PERM_ALLOW_ALL
                if not allow:
                    # Mark tool as error immediately - SDK won't send tool_result for denied
                    self.output.tool_error(tool)
                    self.current_tool = None
                self.client.send("permission_response", {
                    "id": pid,
                    "allow": allow,
                    "always": always,
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
        tool_input = params.get("tool_input", {}) or {}

        # Prefer path from bridge; else scan disk (Kimi/Claude/Grok plans).
        plan_file = (
            tool_input.get("planFilePath")
            or tool_input.get("plan_file")
            or self._find_plan_file()
        )
        if plan_file and not os.path.isfile(plan_file):
            plan_file = self._find_plan_file() or plan_file
        self.plan_file = plan_file
        allowed_prompts = tool_input.get("allowedPrompts", [])

        def on_response(response: str):
            approved = response == PLAN_APPROVE
            self.plan_mode = False

            # Claude Code: only *saved* plan file content is sent back on
            # ExitPlanMode (unsaved buffer edits are ignored).
            plan_text = self._read_plan_content(plan_file) if plan_file else (
                tool_input.get("plan") or "")

            if self.client:
                self.client.send("plan_response", {
                    "id": plan_id,
                    "approved": approved,
                    "plan": plan_text,
                    "planFilePath": plan_file or "",
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
        """Find the newest on-disk plan (Kimi / Claude / Grok)."""
        import glob
        # Kimi: ~/.kimi-code/sessions/<wd>/<session_id>/agents/*/plans/*.md
        root = os.path.expanduser("~/.kimi-code/sessions")
        sid = str(self.session_id or "")
        if os.path.isdir(root):
            if sid:
                cands = glob.glob(os.path.join(
                    root, "*", sid, "agents", "*", "plans", "*.md"))
            else:
                cands = []
            cands = [p for p in cands if os.path.isfile(p)]
            if cands:
                return max(cands, key=os.path.getmtime)
        plans_dir = os.path.expanduser("~/.claude/plans")
        if os.path.exists(plans_dir):
            plan_files = glob.glob(os.path.join(plans_dir, "*.md"))
            if plan_files:
                return max(plan_files, key=os.path.getmtime)
        if self.plan_file and os.path.isfile(self.plan_file):
            return self.plan_file
        return None

    def _read_plan_content(self, plan_file: str) -> str:
        """Read plan text from disk only (saved content; ignore unsaved buffer)."""
        if not plan_file or not os.path.isfile(plan_file):
            return ""
        try:
            with open(plan_file, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            print(f"[Claude] _read_plan_content: {e}")
        return ""

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
