"""Kimi-only background bash tracking (ACP + ~/.kimi-code tasks/*.json).

Not on AcpBridge — Grok/other ACP backends must not walk Kimi session files.

Real wire (see sandbox/kimi_bg): every detached bash-* also has terminal/create
+ wait_for_exit; titles are `Running:` not `Starting background`. Detection of
that pair is a follow-up; this module only owns the Kimi registries + poll.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from typing import Any, Dict, Optional

from rpc_helpers import send_notification


_KIMI_BG_TASK_RE = re.compile(
    r"task_id:\s*(bash-[\w-]+)\s*\n"
    r"(?:pid:\s*\S+\s*\n)?"
    r"description:\s*(.*)\n"
    r"status:\s*(running|completed|failed|cancelled|canceled)",
    re.IGNORECASE,
)
_KIMI_BG_TERM = frozenset({
    "completed", "failed", "cancelled", "canceled", "error", "timeout", "lost",
})
_EXEC_NAMES = frozenset({
    "Bash", "Shell", "execute", "run_terminal_command",
})


class KimiBgMixin:
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._init_kimi_bg()

    def _init_kimi_bg(self) -> None:
        self._kimi_bg: Dict[str, Dict[str, Any]] = {}
        self._bg_poll_task = None

    def _live_kimi_task_ids(self) -> set:
        return set(getattr(self, "_kimi_bg", {}) or {})

    @staticmethod
    def parse_kimi_bg_result_text(text: str) -> Optional[dict]:
        """Parse detached-Bash tool_result text → {task_id, description, status}.

        Live ACP often has no such blob (task id lives in TaskOutput title +
        bash-*.json). Kept for older result shapes.
        """
        if not text or "task_id:" not in text:
            return None
        m = _KIMI_BG_TASK_RE.search(text)
        if m:
            return {
                "task_id": m.group(1),
                "description": (m.group(2) or "").strip(),
                "status": (m.group(3) or "running").lower(),
                "auto": "automatic_notification" in text.lower(),
            }
        tid_m = re.search(r"task_id:\s*(bash-[\w-]+)", text, re.I)
        if not tid_m:
            return None
        st_m = re.search(r"status:\s*(\w+)", text, re.I)
        desc_m = re.search(r"description:\s*(.+)", text, re.I)
        return {
            "task_id": tid_m.group(1),
            "description": (desc_m.group(1).strip() if desc_m else ""),
            "status": (st_m.group(1) if st_m else "running").lower(),
            "auto": "automatic_notification" in text.lower(),
        }

    # AcpBridge called this as a classmethod — keep the name for call sites.
    _parse_kimi_bg_result_text = parse_kimi_bg_result_text

    def kimi_tasks_dir(self) -> Optional[str]:
        sid = (getattr(self, "session_id", None) or "").strip()
        if not sid:
            return None
        root = os.path.expanduser("~/.kimi-code/sessions")
        if not os.path.isdir(root):
            return None
        try:
            for wd in os.listdir(root):
                p = os.path.join(root, wd, sid, "agents", "main", "tasks")
                if os.path.isdir(p):
                    return p
        except OSError:
            return None
        return None

    def _kimi_tasks_dir(self) -> Optional[str]:
        return self.kimi_tasks_dir()

    def read_kimi_task_meta(self, task_id: str) -> Optional[dict]:
        tdir = self.kimi_tasks_dir()
        if not tdir or not task_id:
            return None
        path = os.path.join(tdir, f"{task_id}.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.file_log(f"kimi task json read {path}: {e}")
            return None

    def _read_kimi_task_meta(self, task_id: str) -> Optional[dict]:
        return self.read_kimi_task_meta(task_id)

    def _ensure_bg_poller(self) -> None:
        if self._bg_poll_task is not None and not self._bg_poll_task.done():
            return
        if not self._kimi_bg and not getattr(self, "_terminal_bg", None):
            return

        async def _loop():
            try:
                while self._kimi_bg or getattr(self, "_terminal_bg", None):
                    try:
                        self._poll_kimi_bg_tasks()
                    except Exception as e:
                        self.file_log(f"bg poller: {e}")
                    await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                pass
            finally:
                self._bg_poll_task = None

        try:
            self._bg_poll_task = asyncio.create_task(_loop())
        except Exception as e:
            self.file_log(f"bg poller start: {e}")

    def _register_kimi_native_bg(
            self, tool_use_id: str, task_id: str, description: str = "",
            command: str = "") -> None:
        if not task_id or not tool_use_id:
            return
        if task_id in self._kimi_bg:
            return
        name = self._tool_names_by_id.get(tool_use_id) or "Bash"
        if name not in _EXEC_NAMES:
            name = "Bash"
            tool_use_id = f"bg-{task_id}"
        self._bg_tool_ids.add(tool_use_id)
        self._kimi_bg[task_id] = {
            "tool_use_id": tool_use_id,
            "description": description or task_id,
            "command": command or description or "",
        }
        inp = {
            "command": (command or description or task_id)[:500],
            "run_in_background": True,
            "task_id": task_id,
        }
        self._tool_inputs_by_id[tool_use_id] = inp
        self._tool_names_by_id[tool_use_id] = "Bash"
        self._tool_ids_emitted.add(tool_use_id)
        send_notification("message", {
            "type": "tool_use",
            "id": tool_use_id,
            "name": "Bash",
            "input": inp,
            "background": True,
        })
        self._emit_system("task_started", {
            "task_id": task_id,
            "tool_use_id": tool_use_id,
        })
        self._ensure_bg_poller()

    def _emit_kimi_native_bg_complete(self, task_id: str, meta: dict = None) -> None:
        info = self._kimi_bg.pop(task_id, None)
        if not info:
            return
        tool_use_id = info.get("tool_use_id") or f"bg-{task_id}"
        if self._should_skip_bg_notify(task_id, tool_use_id):
            return
        meta = meta or self.read_kimi_task_meta(task_id) or {}
        raw_status = str(meta.get("status") or "completed").lower()
        if raw_status in ("cancelled", "canceled", "lost"):
            status = "failed"
        elif raw_status == "completed":
            status = "completed"
        elif raw_status in _KIMI_BG_TERM:
            status = "failed"
        else:
            status = "completed"
        code = meta.get("exitCode")
        output_file = ""
        tdir = self.kimi_tasks_dir()
        if tdir:
            cand = os.path.join(tdir, task_id, "output.log")
            if os.path.isfile(cand):
                output_file = cand
        if not output_file:
            output_file = self._write_bg_output_file(
                "kimi-bg-",
                self._format_kimi_bg_log(task_id, status, code, meta, info),
            )
        summary = str(
            meta.get("description") or info.get("description") or task_id
        ).strip()
        summary = self._clip_bg_summary(summary, code)
        self._emit_bg_finished(
            task_id, tool_use_id, status, summary, output_file)
        for term_id, tinfo in list(self._terminal_bg.items()):
            if tinfo.get("task_id") == task_id or tinfo.get("tool_use_id") == tool_use_id:
                self._terminal_bg.pop(term_id, None)

    @staticmethod
    def _format_kimi_bg_log(task_id, status, code, meta, info) -> str:
        body = meta.get("command") or (info or {}).get("command") or ""
        lines = [f"task_id: {task_id}", f"status: {status}"]
        if code is not None:
            lines.append(f"exitCode: {code}")
        if body:
            lines.extend(["", str(body)])
        return "\n".join(lines) + "\n"

    def _poll_kimi_bg_tasks(self) -> int:
        checked = 0
        for term_id, info in list(self._terminal_bg.items()):
            if info.get("kimi_native"):
                continue
            tool_use_id = info.get("tool_use_id") or ""
            if not tool_use_id or tool_use_id not in self._bg_tool_ids:
                continue
            meta = self.find_matching_kimi_task(str(info.get("cmd") or ""))
            if meta and meta.get("taskId"):
                self._link_terminal_to_kimi_task(term_id, tool_use_id, meta)
                checked += 1

        ids = set(self._kimi_bg.keys())
        tdir = self.kimi_tasks_dir()
        if tdir:
            try:
                for name in os.listdir(tdir):
                    if name.startswith("bash-") and name.endswith(".json"):
                        tid = name[:-5]
                        if tid in self._kimi_bg:
                            ids.add(tid)
            except OSError:
                pass
        for task_id in list(ids):
            if task_id not in self._kimi_bg:
                continue
            meta = self.read_kimi_task_meta(task_id)
            checked += 1
            if not meta:
                continue
            st = str(meta.get("status") or "").lower()
            if st in _KIMI_BG_TERM:
                self._emit_kimi_native_bg_complete(task_id, meta)
        return checked

    def find_matching_kimi_task(self, cmd: str = "") -> Optional[dict]:
        """Running bash-*.json whose command overlaps `cmd`. No fuzzy fallback.

        Picking the newest unmatched running task linked the wrong job.
        """
        tdir = self.kimi_tasks_dir()
        if not tdir:
            return None
        cmd = " ".join((cmd or "").split())
        if not cmd:
            return None
        needle = cmd[:80]
        best = None
        best_score = 0
        best_t = 0
        try:
            for name in os.listdir(tdir):
                if not (name.startswith("bash-") and name.endswith(".json")):
                    continue
                meta = self.read_kimi_task_meta(name[:-5])
                if not meta or str(meta.get("status") or "").lower() != "running":
                    continue
                mcmd = " ".join(str(meta.get("command") or "").split())
                if not mcmd:
                    continue
                score = 0
                if needle in mcmd or mcmd[:80] in cmd:
                    score = 10
                elif len(cmd) > 40 and cmd[20:60] in mcmd:
                    score = 5
                if score <= 0:
                    continue
                started = int(meta.get("startedAt") or 0)
                if score > best_score or (score == best_score and started >= best_t):
                    best_score = score
                    best_t = started
                    best = meta
        except OSError:
            return None
        return best if best_score > 0 else None

    def _find_matching_kimi_task(self, cmd: str = "") -> Optional[dict]:
        return self.find_matching_kimi_task(cmd)

    def _link_terminal_to_kimi_task(
            self, terminal_id: str, tool_use_id: str, meta: dict) -> None:
        kid = str(meta.get("taskId") or "")
        if not kid:
            return
        self._register_kimi_native_bg(
            tool_use_id,
            kid,
            description=str(meta.get("description") or ""),
            command=str(meta.get("command") or "")[:500],
        )
        self._terminal_bg[terminal_id] = {
            "task_id": kid,
            "tool_use_id": tool_use_id,
            "cmd": str(meta.get("command") or meta.get("description") or "")[:200],
            "kimi_native": True,
        }
        self.file_log(
            f"bg terminal linked to kimi task={kid} term={terminal_id} "
            f"tool={tool_use_id}")
        self._ensure_bg_poller()

    def _schedule_kimi_relink(self, terminal_id: str, tool_use_id: str, cmd: str) -> None:
        delays = (0.3, 1.0, 2.5, 5.0)

        async def _retries():
            for d in delays:
                try:
                    await asyncio.sleep(d)
                except asyncio.CancelledError:
                    return
                info = self._terminal_bg.get(terminal_id)
                if not info or info.get("kimi_native"):
                    return
                if tool_use_id not in self._bg_tool_ids:
                    return
                meta = self.find_matching_kimi_task(cmd)
                if meta and meta.get("taskId"):
                    self._link_terminal_to_kimi_task(
                        terminal_id, tool_use_id, meta)
                    return
            self.file_log(
                f"bg kimi relink gave up term={terminal_id} tool={tool_use_id}")

        try:
            asyncio.create_task(_retries())
        except Exception as e:
            self.file_log(f"bg kimi relink schedule: {e}")

    def _kimi_handle_tool_result(
            self, tid, tool_name, text, enriched, is_task_poll, status, upd) -> bool:
        """True if this completion was consumed as a Kimi bg ack (no close row)."""
        if status != "completed":
            return False
        kg = self.parse_kimi_bg_result_text(text or "") if text else None
        if is_task_poll and kg and kg.get("task_id") in self._kimi_bg:
            st = kg.get("status") or ""
            if st in _KIMI_BG_TERM or (
                    "retrieval_status: ready" in (text or "").lower()):
                self._emit_kimi_native_bg_complete(
                    kg["task_id"],
                    self.read_kimi_task_meta(kg["task_id"]) or {
                        "status": st or "completed",
                        "description": kg.get("description"),
                    },
                )
            return False  # still emit normal poll tool_result
        if is_task_poll or not kg:
            return False
        if kg.get("status") != "running":
            return False
        if not (kg.get("auto") or "detached: true" in (text or "").lower()):
            return False
        if not tid or tool_name not in _EXEC_NAMES:
            return False
        cmd = (enriched or {}).get("command") or (
            self._tool_inputs_by_id.get(tid) or {}
        ).get("command") or ""
        self._register_kimi_native_bg(
            tid, kg["task_id"],
            description=kg.get("description") or "",
            command=cmd,
        )
        send_notification("message", {
            "type": "tool_result",
            "tool_use_id": tid,
            "content": text or "background",
            "is_error": False,
        })
        return True

    def _bind_terminal_to_bg_tool(self, terminal_id: str, tool_use_id: str) -> None:
        if not terminal_id or not tool_use_id:
            return
        if tool_use_id not in self._bg_tool_ids:
            return
        cmd = (self._tool_inputs_by_id.get(tool_use_id) or {}).get("command", "")
        if terminal_id in self._terminal_bg:
            info = self._terminal_bg[terminal_id]
            if not info.get("kimi_native"):
                meta = self.find_matching_kimi_task(
                    str(info.get("cmd") or cmd))
                if meta and meta.get("taskId"):
                    self._link_terminal_to_kimi_task(
                        terminal_id, tool_use_id, meta)
            return
        native = self.find_matching_kimi_task(str(cmd))
        if native and native.get("taskId"):
            self._link_terminal_to_kimi_task(terminal_id, tool_use_id, native)
            return
        super()._bind_terminal_to_bg_tool(terminal_id, tool_use_id)
        self._schedule_kimi_relink(terminal_id, tool_use_id, str(cmd))

    def _emit_bg_terminal_complete(self, terminal_id: str) -> None:
        info = self._terminal_bg.get(terminal_id)
        if not info:
            return
        tool_use_id = info.get("tool_use_id") or ""
        kid = info.get("task_id") if info.get("kimi_native") else None
        if not kid:
            for bid, kinfo in list(self._kimi_bg.items()):
                if kinfo.get("tool_use_id") == tool_use_id:
                    kid = bid
                    break
        if not kid:
            meta_match = self.find_matching_kimi_task(str(info.get("cmd") or ""))
            if meta_match and meta_match.get("taskId"):
                kid = str(meta_match.get("taskId"))
        if not kid:
            super()._emit_bg_terminal_complete(terminal_id)
            return
        self._terminal_bg.pop(terminal_id, None)
        meta = self.read_kimi_task_meta(str(kid)) or {}
        if str(meta.get("status") or "").lower() not in _KIMI_BG_TERM:
            meta = dict(meta)
            meta["status"] = "completed"
            slot = self._terminals.get(terminal_id) or {}
            es = slot.get("exit_status") or {}
            if es.get("exitCode") is not None:
                meta["exitCode"] = es.get("exitCode")
        if kid not in self._kimi_bg and tool_use_id:
            self._kimi_bg[kid] = {
                "tool_use_id": tool_use_id,
                "description": str(meta.get("description") or kid),
                "command": str(meta.get("command") or info.get("cmd") or ""),
            }
        self._emit_kimi_native_bg_complete(str(kid), meta)
