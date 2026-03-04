"""Order table - TODO list for human→agent assignments."""
import os
import json
import time
import sublime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict


# Local subscriptions: {project_root: [(view_id, wake_prompt), ...]}
_order_subscriptions: Dict[str, List[Tuple[int, str]]] = {}


def subscribe_to_orders(project_root: str, view_id: int, wake_prompt: str) -> str:
    """Subscribe a session to order notifications for a project."""
    if project_root not in _order_subscriptions:
        _order_subscriptions[project_root] = []
    # Remove existing subscription for this view
    _order_subscriptions[project_root] = [
        (vid, wp) for vid, wp in _order_subscriptions[project_root] if vid != view_id
    ]
    _order_subscriptions[project_root].append((view_id, wake_prompt))
    return f"order_sub_{view_id}"


CLAIM_TIMEOUT_SECS = 600  # 10 minutes - auto-release if claimed too long


def _add_order_region(view, order_id: str, row: int, col: int, selection_length: int = None, prompt: str = None):
    """Add visual region marker and phantom for an order."""
    key = f"claude_order_{order_id}"
    # Clear existing first
    view.erase_regions(key)
    view.erase_phantoms(key)

    point = view.text_point(row, col or 0)
    end_point = point + selection_length if selection_length else point
    # Subtle outline if selection, otherwise just gutter icon
    flags = sublime.PERSISTENT | sublime.DRAW_NO_FILL
    if not selection_length:
        flags |= sublime.HIDDEN
    view.add_regions(key, [sublime.Region(point, end_point)], "region.bluish", "bookmark", flags)
    # Add phantom above the line - anchor at end of previous line
    if prompt:
        if row > 0:
            prev_line_end = view.text_point(row, 0) - 1  # end of previous line (before \n)
        else:
            prev_line_end = 0
        indent = " " * (col or 0)
        short_prompt = prompt[:120] + "..." if len(prompt) > 120 else prompt
        html = f'<body style="margin:0;padding:0"><span style="color:color(var(--foreground) alpha(0.5));font-style:italic">{indent}📌 {order_id}: {short_prompt}</span></body>'
        view.add_phantom(key, sublime.Region(prev_line_end, prev_line_end), html, sublime.LAYOUT_BELOW)


@dataclass
class Order:
    id: str
    prompt: str
    state: str = "pending"  # pending, done
    file_path: Optional[str] = None
    row: Optional[int] = None
    col: Optional[int] = None
    selection_length: Optional[int] = None  # length of selected text when pinned
    created_at: float = field(default_factory=time.time)
    claimed_by: Optional[str] = None  # view_id of claiming agent
    claimed_at: Optional[float] = None
    done_at: Optional[float] = None
    done_by: Optional[str] = None  # agent_id

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Order":
        return cls(**data)

    def is_claimed(self) -> bool:
        return self.claimed_by is not None and self.state == "pending"

    def is_claim_expired(self) -> bool:
        if not self.claimed_at:
            return False
        return time.time() - self.claimed_at > CLAIM_TIMEOUT_SECS


@dataclass
class EditEntry:
    """Record of an Edit/Write operation by an agent."""
    id: str
    agent_name: str
    agent_view_id: int  # For messaging the agent
    file_path: str
    line_num: int
    lines_added: int
    lines_removed: int
    timestamp: float
    tool: str  # "Edit" or "Write"
    context: str = ""  # First line or function name hint


class OrderTable:
    """Persistent order list for agent assignments."""

    def __init__(self, project_root: str):
        self.project_root = project_root
        self.orders_file = os.path.join(project_root, ".claude", "orders.json")
        self._counter = 0
        self._orders: Dict[str, Order] = {}
        self._edits: List[EditEntry] = []
        self._edit_counter = 0
        self._load()

    def _load(self):
        if os.path.exists(self.orders_file):
            try:
                with open(self.orders_file, "r") as f:
                    data = json.load(f)
                self._counter = data.get("counter", 0)
                for order_data in data.get("orders", []):
                    order = Order.from_dict(order_data)
                    self._orders[order.id] = order
                # Load edits with backwards compatibility
                self._edit_counter = data.get("edit_counter", 0)
                for e in data.get("edits", []):
                    # Add defaults for new fields if missing
                    e.setdefault("agent_view_id", 0)
                    e.setdefault("context", "")
                    self._edits.append(EditEntry(**e))
            except Exception as e:
                print(f"[OrderTable] Load failed: {e}")

    def _save(self):
        os.makedirs(os.path.dirname(self.orders_file), exist_ok=True)
        data = {
            "counter": self._counter,
            "orders": [o.to_dict() for o in self._orders.values()],
            "edit_counter": self._edit_counter,
            "edits": [asdict(e) for e in self._edits]
        }
        try:
            with open(self.orders_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[OrderTable] Save failed: {e}")

    def add(self, prompt: str, file_path: str = None, row: int = None, col: int = None, selection_length: int = None, view=None) -> Order:
        """Add an order."""
        self._counter += 1
        order = Order(
            id=f"order_{self._counter}",
            prompt=prompt,
            file_path=file_path,
            row=row,
            col=col,
            selection_length=selection_length
        )
        self._orders[order.id] = order
        self._save()
        self._notify_order_added(order)
        if view and row is not None:
            _add_order_region(view, order.id, row, col, selection_length, prompt)
        return order

    def _notify_order_added(self, order: Order):
        """Notify subscribed sessions about new order."""
        from . import notalone

        # Build context
        prompt_text = order.prompt if len(order.prompt) <= 500 else order.prompt[:500] + "..."
        loc = f" @ {order.file_path}:{order.row+1}" if order.file_path else ""
        context = {
            "order_id": order.id,
            "prompt": prompt_text,
            "location": loc,
            "file": order.file_path,
            "row": order.row,
            "col": order.col,
            "selection_length": order.selection_length
        }

        # Notify local subscribers via notalone inject
        subs = _order_subscriptions.get(self.project_root, [])
        for view_id, wake_prompt_template in subs:
            try:
                wake_prompt = wake_prompt_template.format(context=context)
            except (KeyError, ValueError):
                wake_prompt = wake_prompt_template
            notalone.inject_local(view_id, wake_prompt, context)

        if subs:
            print(f"[OrderTable] notified {len(subs)} subscribers")

        # Also fire to daemon for external agents
        self._fire_to_daemon(context)

    def _fire_to_daemon(self, context: dict):
        """Fire notification to daemon for external agents."""
        import socket
        from pathlib import Path

        socket_path = str(Path.home() / ".notalone" / "notalone.sock")
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect(socket_path)
            msg = {
                "method": "fire",
                "type": "order_added",
                "params": {"project": self.project_root},
                "context": context
            }
            sock.sendall((json.dumps(msg) + "\n").encode())
            sock.close()
        except Exception:
            pass  # Silently fail - daemon may not support fire

    def list(self, state: str = None) -> List[dict]:
        """List orders as dicts. Auto-releases expired/orphaned claims."""
        self._auto_release_claims()
        orders = list(self._orders.values())
        if state:
            orders = [o for o in orders if o.state == state]
        orders = sorted(orders, key=lambda o: o.created_at)
        return [o.to_dict() for o in orders]

    # --- Edit tracking ---

    def add_edit(self, agent_name: str, agent_view_id: int, file_path: str, line_num: int,
                 lines_added: int, lines_removed: int, tool: str, context: str = "") -> str:
        """Record an edit event."""
        self._edit_counter += 1
        edit = EditEntry(
            id=f"edit_{self._edit_counter}",
            agent_name=agent_name,
            agent_view_id=agent_view_id,
            file_path=file_path,
            line_num=line_num,
            lines_added=lines_added,
            lines_removed=lines_removed,
            timestamp=time.time(),
            tool=tool,
            context=context
        )
        self._edits.append(edit)
        # Keep only last 50 edits
        if len(self._edits) > 50:
            self._edits = self._edits[-50:]
        self._save()
        return edit.id

    def clear_edits(self, file_path: str = None, edit_id: str = None):
        """Clear edit history.

        Args:
            file_path: If provided, only clear edits for this file
            edit_id: If provided, only clear this specific edit
        """
        if edit_id:
            self._edits = [e for e in self._edits if e.id != edit_id]
        elif file_path:
            self._edits = [e for e in self._edits if e.file_path != file_path]
        else:
            self._edits = []
        self._save()

    def list_edits(self) -> List[dict]:
        """List recent edits."""
        return [asdict(e) for e in self._edits]

    def _auto_release_claims(self):
        """Release claims that are expired or from gone agents."""
        released = []
        for order in self._orders.values():
            if not order.is_claimed():
                continue
            # Check timeout
            if order.is_claim_expired():
                released.append((order.id, "timeout"))
                order.claimed_by = None
                order.claimed_at = None
                continue
            # Check if agent session is gone
            if order.claimed_by:
                try:
                    view_id = int(order.claimed_by)
                    if hasattr(sublime, '_claude_sessions') and view_id not in sublime._claude_sessions:
                        released.append((order.id, "agent_gone"))
                        order.claimed_by = None
                        order.claimed_at = None
                except ValueError:
                    pass
        if released:
            self._save()
            for oid, reason in released:
                print(f"[OrderTable] auto-released {oid} ({reason})")

    def claim(self, order_id: str, agent_id: str) -> Tuple[bool, str]:
        """Claim an order for working on it."""
        order = self._orders.get(order_id)
        if not order:
            return False, f"Order {order_id} not found"
        if order.state == "done":
            return False, "Order already done"
        if order.is_claimed() and order.claimed_by != agent_id:
            if not order.is_claim_expired():
                return False, f"Already claimed by {order.claimed_by}"
        order.claimed_by = agent_id
        order.claimed_at = time.time()
        self._save()
        return True, "Claimed"

    def release(self, order_id: str, agent_id: str = None) -> Tuple[bool, str]:
        """Release a claimed order."""
        order = self._orders.get(order_id)
        if not order:
            return False, f"Order {order_id} not found"
        if not order.is_claimed():
            return False, "Not claimed"
        if agent_id and order.claimed_by != agent_id:
            return False, f"Claimed by different agent ({order.claimed_by})"
        order.claimed_by = None
        order.claimed_at = None
        self._save()
        return True, "Released"

    def complete(self, order_id: str, agent_id: str = None) -> Tuple[bool, str]:
        """Mark order done."""
        order = self._orders.get(order_id)
        if not order:
            return False, f"Order {order_id} not found"
        if order.state == "done":
            return False, "Already done"
        order.state = "done"
        order.done_at = time.time()
        order.done_by = agent_id
        order.claimed_by = None  # Clear claim on completion
        order.claimed_at = None
        self._save()
        self._remove_bookmark(order_id)
        return True, "Done"

    def delete(self, order_id: str) -> Tuple[bool, str]:
        """Delete an order (saves to undo stack)."""
        if order_id not in self._orders:
            return False, f"Order {order_id} not found"
        order = self._orders.pop(order_id)
        # Save to undo stack
        if self.project_root not in _undo_stack:
            _undo_stack[self.project_root] = []
        _undo_stack[self.project_root].append(order)
        _undo_stack[self.project_root] = _undo_stack[self.project_root][-20:]
        self._save()
        self._remove_bookmark(order_id)
        return True, "Deleted"

    def undo_delete(self) -> Tuple[bool, str]:
        """Restore last deleted order."""
        if self.project_root not in _undo_stack or not _undo_stack[self.project_root]:
            return False, "Nothing to undo"
        order = _undo_stack[self.project_root].pop()
        self._orders[order.id] = order
        self._save()
        return True, f"Restored {order.id}"

    def _remove_bookmark(self, order_id: str):
        """Remove bookmark and phantom for an order from all views."""
        window = sublime.active_window()
        if window:
            for view in window.views():
                view.erase_regions(f"claude_order_{order_id}")
                view.erase_phantoms(f"claude_order_{order_id}")

    def clear_done(self) -> int:
        """Remove all done orders."""
        done_ids = [oid for oid, o in self._orders.items() if o.state == "done"]
        for oid in done_ids:
            del self._orders[oid]
        self._save()
        return len(done_ids)


def _relative_time(timestamp: float) -> str:
    """Get human-readable relative time."""
    diff = time.time() - timestamp
    if diff < 60:
        return "just now"
    elif diff < 3600:
        return f"{int(diff/60)}m ago"
    elif diff < 86400:
        return f"{int(diff/3600)}h ago"
    else:
        return f"{int(diff/86400)}d ago"


def _relative_path(file_path: str, folders: List[str]) -> str:
    """Get path relative to nearest project folder ancestor."""
    for folder in folders:
        if file_path.startswith(folder + os.sep):
            return file_path[len(folder) + 1:]
    return os.path.basename(file_path)


# Table cache
_tables: Dict[str, OrderTable] = {}

# Undo stack per project: {project_root: [deleted_orders]}
_undo_stack: Dict[str, List[Order]] = {}


def get_table(window) -> Optional[OrderTable]:
    """Get order table for window's project."""
    folders = window.folders()
    if not folders:
        return None
    return get_table_for_cwd(folders[0])


def get_table_for_cwd(cwd: str) -> OrderTable:
    """Get order table for cwd."""
    if cwd not in _tables:
        _tables[cwd] = OrderTable(cwd)
    return _tables[cwd]


def sync_bookmarks(window):
    """Sync order bookmarks with open views."""
    table = get_table(window)
    if not table:
        return

    pending = table.list("pending")
    for order in pending:
        file_path = order.get("file_path")
        if not file_path:
            continue
        row = order.get("row", 0)
        col = order.get("col", 0)
        selection_length = order.get("selection_length")
        prompt = order.get("prompt")
        order_id = order["id"]

        # Find view for this file
        for view in window.views():
            if view.file_name() == file_path:
                _add_order_region(view, order_id, row, col, selection_length, prompt)
                break


# ─── Order Table View ──────────────────────────────────────────────────────

class OrderTableView:
    """Manages a dedicated view for the order table."""

    def __init__(self, window, table: OrderTable):
        self.window = window
        self.table = table
        self.view = None
        self._create_view()

    def _create_view(self):
        for v in self.window.views():
            if v.settings().get("order_table_view"):
                self.view = v
                break

        if not self.view:
            self.view = self.window.new_file()
            self.view.set_name("Order Table")
            self.view.set_scratch(True)
            self.view.settings().set("order_table_view", True)
            self.view.settings().set("word_wrap", False)
            self.view.settings().set("gutter", False)
            self.view.settings().set("line_numbers", False)
            self.view.settings().set("margin", 10)
            self.view.settings().set("font_size", 11)
            self.view.settings().set("edits_grouped", False)  # Toggle for grouped view
            self.view.assign_syntax("Packages/ClaudeCode/OrderTable.sublime-syntax")

        self.refresh()

    def toggle_edits_grouped(self):
        """Toggle between flat and grouped-by-file edit display."""
        if not self.view:
            return
        current = self.view.settings().get("edits_grouped", False)
        self.view.settings().set("edits_grouped", not current)
        self.refresh()
        return not current

    def refresh(self):
        if not self.view or not self.view.is_valid():
            return

        pending = self.table.list("pending")
        done = self.table.list("done")

        lines = ["═══ ORDER TABLE ═══", ""]

        # Get project folders for relative paths
        folders = self.window.folders() if self.window else []

        if pending:
            claimed = [o for o in pending if o.get("claimed_by")]
            unclaimed = [o for o in pending if not o.get("claimed_by")]
            lines.append(f"PENDING ({len(pending)})")
            lines.append("─" * 50)
            for o in unclaimed:
                loc = ""
                if o.get("file_path"):
                    rel_path = _relative_path(o["file_path"], folders)
                    row = o.get('row', 0) + 1
                    sel = f" [{o['selection_length']}ch]" if o.get("selection_length") else ""
                    loc = f" @ {rel_path}:{row}{sel}"
                prompt = o['prompt'][:200] + ("..." if len(o['prompt']) > 200 else "")
                lines.append(f"  [{o['id']}]{loc}  {prompt}")
            if claimed:
                lines.append("")
                lines.append(f"⏳ CLAIMED ({len(claimed)})")
                for o in claimed:
                    loc = ""
                    if o.get("file_path"):
                        rel_path = _relative_path(o["file_path"], folders)
                        row = o.get('row', 0) + 1
                        sel = f" [{o['selection_length']}ch]" if o.get("selection_length") else ""
                        loc = f" @ {rel_path}:{row}{sel}"
                    prompt = o['prompt'][:150] + ("..." if len(o['prompt']) > 150 else "")
                    lines.append(f"  ⏳ [{o['id']}]{loc}  {prompt} <- {o['claimed_by']}")
        else:
            lines.append("No pending orders")

        lines.append("")

        if done:
            lines.append(f"# DONE ({len(done)})")
            for o in done[-5:]:
                by = f" <- {o.get('done_by', '?')}" if o.get("done_by") else ""
                prompt = o['prompt'][:150] + ("..." if len(o['prompt']) > 150 else "")
                lines.append(f"#   [{o['id']}] {prompt}{by}")
            if len(done) > 5:
                lines.append(f"#   ... and {len(done)-5} more")

        # Edits section
        edits = self.table.list_edits()
        grouped = self.view.settings().get("edits_grouped", False)
        if edits:
            lines.append("")
            mode_indicator = "[grouped]" if grouped else "[by time]"
            lines.append(f"📝 RECENT EDITS ({len(edits)}) {mode_indicator}")
            lines.append("─" * 120)

            if grouped:
                # Group by file
                from collections import defaultdict
                by_file = defaultdict(list)
                for e in edits:
                    by_file[e["file_path"]].append(e)

                # Sort files by most recent edit
                sorted_files = sorted(by_file.items(),
                                     key=lambda x: max(e["timestamp"] for e in x[1]),
                                     reverse=True)

                for file_path, file_edits in sorted_files[:8]:  # Top 8 files
                    rel_path = _relative_path(file_path, folders)
                    if len(rel_path) > 70:
                        rel_path = "..." + rel_path[-67:]
                    agent_ids = set(e["agent_view_id"] for e in file_edits)
                    agent_str = ",".join(str(a) for a in agent_ids)

                    lines.append(f"  {rel_path} [{agent_str}]")

                    # Show edits sorted by time (newest first)
                    for e in sorted(file_edits, key=lambda x: x["timestamp"], reverse=True)[:4]:
                        delta = f"+{e['lines_added']}" if e['lines_added'] else ""
                        if e['lines_removed']:
                            delta += f"/-{e['lines_removed']}"
                        ago = _relative_time(e["timestamp"])
                        ctx = e.get("context", "")[:45]
                        lines.append(f"    {rel_path}:{e['line_num']:<5} {delta:<8} {ago:<10} {ctx}")
            else:
                # Flat list sorted by time (newest first), merge same file:line
                sorted_edits = sorted(edits, key=lambda x: x["timestamp"], reverse=True)

                # Merge edits at same location (keep newest)
                seen = set()
                merged_edits = []
                for e in sorted_edits:
                    key = (e["file_path"], e["line_num"])
                    if key not in seen:
                        seen.add(key)
                        merged_edits.append(e)

                for e in merged_edits[:15]:
                    rel_path = _relative_path(e["file_path"], folders)
                    if len(rel_path) > 40:
                        rel_path = "..." + rel_path[-37:]
                    ago = _relative_time(e["timestamp"])
                    delta = f"+{e['lines_added']}" if e['lines_added'] else ""
                    if e['lines_removed']:
                        delta += f"/-{e['lines_removed']}"
                    ctx = e.get("context", "")[:40]
                    agent_id = e.get("agent_view_id", 0)
                    file_loc = f"{rel_path}:{e['line_num']}"
                    lines.append(f"  {file_loc:<45} {delta:<8} {ago:<10} {ctx:<40} [{agent_id}]")

        lines.append("─" * 120)
        lines.append("a add | Enter/g goto | o focus | q msg | c clear | C/Ctrl+C clear all | x clear done | t group")

        content = "\n".join(lines)

        # Update view content - replace entire buffer
        self.view.set_read_only(False)
        self.view.run_command("claude_replace_content", {"content": content})
        self.view.set_read_only(True)

    def show(self):
        if self.view and self.view.is_valid():
            self.window.focus_view(self.view)


# Global view cache
_views: Dict[str, OrderTableView] = {}


def show_order_table(window) -> Optional[OrderTableView]:
    """Show the order table for window."""
    table = get_table(window)
    if not table:
        return None

    # Sync bookmarks with open views
    sync_bookmarks(window)

    key = table.project_root
    if key not in _views or not _views[key].view.is_valid():
        _views[key] = OrderTableView(window, table)
    else:
        _views[key].refresh()
        _views[key].show()
    return _views[key]


def refresh_order_table(window, reload_from_disk=False):
    """Refresh order table if visible."""
    table = get_table(window)
    if not table:
        return
    # Reload from disk only if requested (e.g., external changes)
    if reload_from_disk:
        table._load()
    key = table.project_root

    # Check if we have cached wrapper
    if key in _views:
        view_wrapper = _views[key]
        if view_wrapper.view and view_wrapper.view.is_valid():
            view_wrapper.table = table
            view_wrapper.refresh()
            sync_bookmarks(window)
            return

    # No cached wrapper - check if view exists (e.g., after restart)
    for v in window.views():
        if v.settings().get("order_table_view"):
            # Create wrapper for existing view
            _views[key] = OrderTableView(window, table)
            sync_bookmarks(window)
            return

    # Also sync bookmarks
    sync_bookmarks(window)
