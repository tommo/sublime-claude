"""Output data models and constants."""
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Callable, Any

from .constants import BACKEND_ABBREV

import re as _re

# Status-icon chars that prefix a tab title (see ClaudeOutput._update_title).
_TITLE_ICON_RE = _re.compile(r'^(?:[◉◇•❓⏸↻⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*)+')


def _title_abbrev_tokens():
    """The set of backend abbrev tokens _update_title can emit as `ABBR> `,
    resolved the same way (registry abbrev → static map → name[:2]). Used by
    strip_title_decoration so the strip stays in sync with the decoration and
    covers custom providers (As / GM / Km / …), not just the built-ins."""
    toks = set()
    try:
        from . import backends as _b
        for name, spec in _b.all_backends().items():
            tok = spec.abbrev or BACKEND_ABBREV.get(name) or name[:2].upper()
            if tok:
                toks.add(tok)
    except Exception:
        pass
    for tok in BACKEND_ABBREV.values():
        if tok:
            toks.add(tok)
    return toks


def strip_title_decoration(title):
    """Inverse of ClaudeOutput._update_title: peel status icons and any number
    of stacked `ABBR> ` prefixes (+ legacy "[x] " / "Claude: " forms) so that
    re-decorating across reconnects can't accumulate prefixes. Returns the
    clean base name (empty string if nothing left). Does NOT touch a trailing
    truncation ellipsis — callers that need it handle it themselves."""
    if not title:
        return ""
    name = title
    # Legacy forms first.
    if name.startswith("[") and "] " in name:
        name = name[name.index("] ") + 2:]
    if name.startswith("Claude: "):
        name = name[8:]
    toks = sorted(_title_abbrev_tokens(), key=len, reverse=True)  # DSR before DS
    abbr_re = _re.compile(r'^(?:%s)>\s*' % "|".join(_re.escape(t) for t in toks)) if toks else None
    # Loop so any interleaved icon/abbrev stacking from prior reconnects
    # collapses completely in one call.
    while True:
        new = _TITLE_ICON_RE.sub('', name)
        if abbr_re:
            new = abbr_re.sub('', new)
        if new == name:
            break
        name = new
    return name.strip()


# Status constants
PENDING = "pending"
DONE = "done"
ERROR = "error"
BACKGROUND = "background"

# Permission button constants
PERM_ALLOW = "allow"
PERM_DENY = "deny"
PERM_ALLOW_ALL = "allow_all"
PERM_ALLOW_SESSION = "allow_session"  # Allow same tool for 30s


PLAN_APPROVE = "approve"
PLAN_REJECT = "reject"
PLAN_VIEW = "view"


@dataclass
class PlanApproval:
    """A pending plan approval request."""
    id: int
    plan_file: str
    allowed_prompts: List[dict]
    callback: Callable[[str], None]  # Called with PLAN_APPROVE or PLAN_REJECT
    region: tuple = (0, 0)
    button_regions: Dict[str, tuple] = field(default_factory=dict)


@dataclass
class PermissionRequest:
    """A pending permission request."""
    id: int
    tool: str
    tool_input: dict
    callback: Callable[[str], None]  # Called with PERM_ALLOW, PERM_DENY, or PERM_ALLOW_ALL
    region: tuple = (0, 0)  # Region in view
    button_regions: Dict[str, tuple] = field(default_factory=dict)  # button_type -> (start, end)


@dataclass
class QuestionRequest:
    """A pending inline question request."""
    qid: int
    questions: List[dict]     # [{question, options, header, multiSelect}]
    current_idx: int = 0
    answers: Dict[str, str] = field(default_factory=dict)
    callback: Callable = None  # Called with answers dict or None (cancelled)
    region: tuple = (0, 0)
    button_regions: Dict[str, tuple] = field(default_factory=dict)
    selected: set = field(default_factory=set)  # multi-select toggles


@dataclass
class ToolCall:
    """A single tool call."""
    name: str
    tool_input: dict
    status: str = PENDING  # pending, done, error, background
    result: Optional[str] = None  # tool result content
    id: Optional[str] = None  # tool_use_id, for precise matching


@dataclass
class TodoItem:
    """A todo item from TodoWrite or Task* tools."""
    content: str
    status: str  # pending, in_progress, completed, cancelled, deleted
    id: str = ""  # server-assigned id (set by TaskCreate result / TaskList)


# Statuses that mean "no longer open work" — hide from widget + don't carry.
_TODO_CLOSED = frozenset({
    "completed", "cancelled", "canceled", "deleted",
})


def _todo_status_norm(status: str) -> str:
    return (status or "pending").strip().lower().replace("-", "_")


def _todo_is_open(todo: "TodoItem") -> bool:
    """True if the item is still active work (not completed/cancelled/deleted)."""
    if not (todo.content or "").strip():
        return False
    return _todo_status_norm(todo.status) not in _TODO_CLOSED


def _todo_is_active(todo: "TodoItem") -> bool:
    return _todo_status_norm(todo.status) == "in_progress"


def _todo_is_completed(todo: "TodoItem") -> bool:
    return _todo_status_norm(todo.status) == "completed"


def _open_todos(todos: list) -> list:
    """Open items only — used for widget + carry-forward between rounds."""
    return [t for t in (todos or []) if _todo_is_open(t)]


@dataclass
class GoalState:
    """Host-owned goal strip snapshot (from GoalTracker, not tool invention)."""
    status: str = "active"
    message: str = ""
    blocked_reason: str = ""
    objective: str = ""
    phase: str = "idle"
    pause_message: str = ""
    token_budget: Optional[int] = None
    tokens_used: Optional[int] = None
    verify_runs: int = 0
    verify_max: int = 0
    gaps: List[str] = field(default_factory=list)
    verifying: bool = False
    planning: bool = False
    goal_id: str = ""


def _goal_is_open(goal: Optional["GoalState"]) -> bool:
    """Sticky while harness still has an open goal (not complete/cleared)."""
    if not goal:
        return False
    return goal.status in (
        "active", "user_paused", "blocked", "budget_limited", "infra_paused",
    )


def goal_strip_label(goal: Optional["GoalState"]) -> str:
    """Human phase label for sticky ◎ goal strip + status chip."""
    if not goal:
        return ""
    try:
        from .goal_tracker import ui_phase_label
    except ImportError:
        from goal_tracker import ui_phase_label  # type: ignore
    phase = goal.phase or ""
    if goal.verifying:
        phase = "verifying"
    elif goal.planning and phase != "verifying":
        phase = "planning"
    return ui_phase_label(
        status=goal.status or "",
        phase=phase,
        gaps=list(goal.gaps or []),
    )


def goal_strip_body(goal: Optional["GoalState"]) -> str:
    """Secondary text for the strip: claim, pause, or first verify gaps."""
    if not goal:
        return ""
    try:
        from .goal_tracker import ui_phase_body
    except ImportError:
        from goal_tracker import ui_phase_body  # type: ignore
    phase = goal.phase or ""
    if goal.verifying:
        phase = "verifying"
    return ui_phase_body(
        status=goal.status or "",
        phase=phase,
        message=goal.message or "",
        objective=goal.objective or "",
        pause_message=goal.pause_message or "",
        blocked_reason=goal.blocked_reason or "",
        gaps=list(goal.gaps or []),
    )


@dataclass
class Conversation:
    """A single prompt + tools + response + meta."""
    prompt: str = ""
    # Events in time order - either ToolCall or str (text chunk)
    events: List = field(default_factory=list)
    todos: List[TodoItem] = field(default_factory=list)  # current todo state
    todos_all_done: bool = False  # True when all todos completed (don't carry to next)
    goal: Optional[GoalState] = None  # sticky snapshot from host GoalTracker
    working: bool = True  # True while processing, False when done
    duration: float = 0.0
    has_meta: bool = False  # True after meta() — show @done even if duration is 0
    usage: dict = None
    region: tuple = (0, 0)
    context_names: List[str] = field(default_factory=list)  # Context files used

    @property
    def tools(self) -> List[ToolCall]:
        """Get all tool calls (for compatibility)."""
        return [e for e in self.events if isinstance(e, ToolCall)]

    @property
    def text_chunks(self) -> List[str]:
        """Get all text chunks (for compatibility)."""
        return [e for e in self.events if isinstance(e, str)]
