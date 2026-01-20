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


@dataclass
class Order:
    id: str
    prompt: str
    state: str = "pending"  # pending, done
    file_path: Optional[str] = None
    row: Optional[int] = None
    col: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    done_at: Optional[float] = None
    done_by: Optional[str] = None  # agent_id

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Order":
        return cls(**data)


class OrderTable:
    """Persistent order list for agent assignments."""

    def __init__(self, project_root: str):
        self.project_root = project_root
        self.orders_file = os.path.join(project_root, ".claude", "orders.json")
        self._counter = 0
        self._orders: Dict[str, Order] = {}
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
            except Exception as e:
                print(f"[OrderTable] Load failed: {e}")

    def _save(self):
        os.makedirs(os.path.dirname(self.orders_file), exist_ok=True)
        data = {
            "counter": self._counter,
            "orders": [o.to_dict() for o in self._orders.values()]
        }
        try:
            with open(self.orders_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[OrderTable] Save failed: {e}")

    def add(self, prompt: str, file_path: str = None, row: int = None, col: int = None, view=None) -> Order:
        """Add an order."""
        self._counter += 1
        order = Order(
            id=f"order_{self._counter}",
            prompt=prompt,
            file_path=file_path,
            row=row,
            col=col
        )
        self._orders[order.id] = order
        self._save()
        self._notify_order_added(order)
        # Add visual bookmark marker
        if view and row is not None:
            point = view.text_point(row, col or 0)
            view.add_regions(
                f"claude_order_{order.id}",
                [sublime.Region(point, point)],
                "region.bluish",
                "bookmark",
                sublime.HIDDEN | sublime.PERSISTENT
            )
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
            "row": order.row
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
        """List orders as dicts."""
        orders = list(self._orders.values())
        if state:
            orders = [o for o in orders if o.state == state]
        orders = sorted(orders, key=lambda o: o.created_at)
        return [o.to_dict() for o in orders]

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
        # Keep only last 10 deletions
        _undo_stack[self.project_root] = _undo_stack[self.project_root][-10:]
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
        """Remove bookmark for an order from all views."""
        window = sublime.active_window()
        if window:
            for view in window.views():
                view.erase_regions(f"claude_order_{order_id}")

    def clear_done(self) -> int:
        """Remove all done orders."""
        done_ids = [oid for oid, o in self._orders.items() if o.state == "done"]
        for oid in done_ids:
            del self._orders[oid]
        self._save()
        return len(done_ids)


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
        order_id = order["id"]

        # Find view for this file
        for view in window.views():
            if view.file_name() == file_path:
                point = view.text_point(row, col)
                view.add_regions(
                    f"claude_order_{order_id}",
                    [sublime.Region(point, point)],
                    "region.bluish",
                    "bookmark",
                    sublime.HIDDEN | sublime.PERSISTENT
                )
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
            self.view.assign_syntax("Packages/ClaudeCode/OrderTable.sublime-syntax")

        self.refresh()

    def refresh(self):
        if not self.view or not self.view.is_valid():
            return

        pending = self.table.list("pending")
        done = self.table.list("done")

        lines = ["═══ ORDER TABLE ═══", ""]

        # Get project folders for relative paths
        folders = self.window.folders() if self.window else []

        if pending:
            lines.append(f"PENDING ({len(pending)})")
            lines.append("─" * 50)
            for o in pending:
                loc = ""
                if o.get("file_path"):
                    rel_path = _relative_path(o["file_path"], folders)
                    row = o.get('row', 0) + 1
                    loc = f" @ {rel_path}:{row}"
                prompt = o['prompt'][:60] + ("..." if len(o['prompt']) > 60 else "")
                lines.append(f"  [{o['id']}]{loc}  {prompt}")
        else:
            lines.append("No pending orders")

        lines.append("")

        if done:
            lines.append(f"# DONE ({len(done)})")
            for o in done[-5:]:
                by = f" <- {o.get('done_by', '?')}" if o.get("done_by") else ""
                prompt = o['prompt'][:50] + ("..." if len(o['prompt']) > 50 else "")
                lines.append(f"#   [{o['id']}] {prompt}{by}")
            if len(done) > 5:
                lines.append(f"#   ... and {len(done)-5} more")

        lines.append("─" * 50)
        lines.append("a add | Cmd+Shift+O add@cursor | Enter goto | d del | u undo")

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
