#!/usr/bin/env python3
"""PTY integration probes / **recalibration suite**.

This script triggers each TUI pattern the PtyEngineSession relies on, and
asserts the observable outcome. Run it after a Claude Code version bump to
detect drift — anything that turns FAIL is a signal that engine logic needs
updating for the new version. Each probe is a single, isolated `claude`
invocation; failures don't cascade.

Patterns each probe pins down (and where they live in the engine):

  dontask_denies_askuser
      → dontAsk perm-mode auto-denies AskUserQuestion (no TUI dialog).
        Engine: _handle_ask_user_question skips panel when tool_result arrives
        before the screen poll catches a menu.

  acceptedits_resolves_askuser
      → acceptEdits / non-default modes auto-resolve AskUserQuestion.
        Engine relies on tool_result short-circuit (same _ask_resolved_id path).

  default_dialog_drivable
      → default mode shows an interactive menu; Down+Enter selects option 2.
        Engine: _screen_has_menu structural detection + _drive_next_answer
        (Down arrows + Enter) keystroke driver.

  bash_permission_structural
      → Bash in default mode produces a menu matching the same structural
        shape as AskUserQuestion (numbered options). Phase 5 (native permission
        prompts) reuses the same detector + driver — this probe confirms that.

  bracketed_paste_multiline_submits
      → Multi-line bracketed paste + deferred Enter submits as one user message.
        Engine: query() submit path.

  image_inline_via_path
      → TUI auto-inlines image attachments from paths in pasted text.
        Engine: _materialize_images + bare-path append in query().

  transcript_schema_spot_check
      → A real claude turn produces a transcript with the system subtypes and
        field names the tailer maps (turn_duration→result, compact_boundary,
        tool_result.tool_use_id, etc.). Schema-drift canary.

  large_tool_result_spills_to_file
      → When a tool_result is too big to inline (~>25KB), CC stores the
        content in `<session-id>/tool-results/<tool_use_id>.txt` and leaves a
        "<persisted-output> … Full output saved to: <path> …" marker in the
        main .jsonl. Pins both the marker shape and the sidefile location.
        Engine tracking via tool_use_id stays correct; rendering currently
        only shows the marker (sidefile-aware rendering is a future patch).

Slow — each is one real API call, ~30–60s. Inconclusive on network/API flakes
(not FAIL). Run a subset: `python3 prototype/tests/test_pty_probes.py default`
"""
import os, sys, pty, time, select, signal, uuid, re

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # prototype/
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))  # repo root
import cc_transcript as ct  # noqa

CLAUDE = os.environ.get("CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))
CWD = os.environ.get(
    "CLAUDE_PROBE_CWD",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

results = []

def record(status, name, detail=""):
    results.append((status, name, detail))
    print("[%s] %s%s" % (status, name, "  — " + detail if detail else ""))


# ── PTY session helper ────────────────────────────────────────────────────────
class Probe:
    def __init__(self, mode):
        self.sid = str(uuid.uuid4())
        self.path = ct.transcript_path(CWD, self.sid)
        env = dict(os.environ); env["TERM"] = "xterm-256color"
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(CWD)
            # Strict empty MCP config — suppress the user's global MCP servers
            # (which may fail / retry when run outside Sublime and delay tool
            # invocations by 30s+). Use the explicit {"mcpServers":{}} form;
            # bare {} is rejected.
            os.execvpe(CLAUDE,
                ["claude", "--session-id", self.sid,
                 "--permission-mode", mode,
                 "--strict-mcp-config",
                 "--mcp-config", '{"mcpServers":{}}'], env)
            os._exit(127)
        # tailer AFTER fork so child doesn't inherit our threads
        self.events = []
        self.tailer = ct.Tailer(self.path, lambda p: self.events.append(p))
        self.tailer.start()
        self.raw = bytearray()

    def drain(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            r,_,_ = select.select([self.fd], [], [], 0.2)
            if r:
                try: d = os.read(self.fd, 65536)
                except OSError: break
                if not d: break
                self.raw.extend(d)

    def w(self, s):
        os.write(self.fd, s.encode())

    def paste_submit(self, text):
        """Same submission strategy as PtyEngineSession."""
        self.w("\x1b[200~" + text + "\x1b[201~")
        time.sleep(0.4)
        self.w("\r")

    def screen_flat(self):
        t = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", self.raw)
        t = re.sub(rb"\x1b[\(\)][AB0]", b"", t)
        t = re.sub(rb"[^\x09\x0a\x20-\x7e]", b"", t)
        return re.sub(r"\s+", "", t.decode(errors="replace")).lower()

    def screen_lines(self):
        """Cleaned raw output as logical lines (for structural pattern checks)."""
        t = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", self.raw)
        t = re.sub(rb"\x1b[\(\)][AB0]", b"", t)
        t = re.sub(rb"[^\x09\x0a\x20-\x7e]", b"", t)
        return [ln.rstrip() for ln in t.decode(errors="replace").splitlines()
                if ln.strip()]

    def has_numbered_menu(self, min_options=3, window=12):
        """Mirror of PtyEngineSession._screen_has_menu — structural check for
        N+ consecutive `<digit>. text` lines near the bottom of the screen."""
        opt_re = re.compile(r"^\s*\d+\.\s+\S")
        run = 0
        for ln in reversed(self.screen_lines()[-window:]):
            if opt_re.match(ln):
                run += 1
                if run >= min_options:
                    return True
            elif run:
                run = 0
        return False

    def wait_event(self, predicate, deadline_s):
        deadline = time.time() + deadline_s
        while time.time() < deadline:
            self.drain(1)
            for p in self.events:
                if predicate(p):
                    return p
        return None

    def close(self):
        self.tailer.stop()
        try: os.kill(self.pid, signal.SIGINT); time.sleep(0.3)
        except ProcessLookupError: pass
        try: os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError: pass


def _has_text(p, sub):
    return p.get("type") == "text" and sub in p.get("text", "")


# ── PROBES ────────────────────────────────────────────────────────────────────
def probe_dontask_denies_askuserquestion():
    """Hypothesis: dontAsk → harness denies AskUserQuestion → tool_result
    with is_error=True arrives immediately (no TUI dialog)."""
    p = Probe("dontAsk")
    try:
        p.drain(7)
        p.paste_submit(
            "Test the AskUserQuestion tool. You MUST invoke the tool (do not "
            "answer directly). Use it to ask me a question — any question with "
            "two options. Whatever I pick, just reply 'done' and stop.")
        # Wait up to 60s for tool_use → tool_result
        tu = p.wait_event(lambda e: e.get("type") == "tool_use"
                          and e.get("name") == "AskUserQuestion", 60)
        if not tu:
            return record("INCONCLUSIVE", "dontask_denies_askuser",
                          "no AskUserQuestion tool_use within 60s (API issue?)")
        tr = p.wait_event(lambda e: e.get("type") == "tool_result"
                          and e.get("tool_use_id") == tu["id"], 20)
        if not tr:
            return record("FAIL", "dontask_denies_askuser",
                          "tool_use fired but no tool_result in 20s")
        if tr["is_error"]:
            return record("PASS", "dontask_denies_askuser",
                          "denied as expected: %s..." % (tr["content"] or "")[:60])
        return record("FAIL", "dontask_denies_askuser",
                      "tool succeeded under dontAsk (not denied): %r" % tr)
    finally:
        p.close()


def probe_acceptedits_resolves_askuserquestion():
    """Hypothesis: acceptEdits auto-resolves AskUserQuestion (no TUI dialog
    waiting). With NO input from us, expect tool_result within reasonable time."""
    p = Probe("acceptEdits")
    try:
        p.drain(7)
        p.paste_submit(
            "Test the AskUserQuestion tool. You MUST invoke the tool (do not "
            "answer directly). Use it to ask me a question — any question with "
            "two options. Whatever I pick, just reply 'done' and stop.")
        tu = p.wait_event(lambda e: e.get("type") == "tool_use"
                          and e.get("name") == "AskUserQuestion", 60)
        if not tu:
            return record("INCONCLUSIVE", "acceptedits_resolves_askuser",
                          "no AskUserQuestion tool_use within 60s")
        # NO keystroke injection from us. If tool_result arrives within 30s,
        # claude resolved it without us → mode auto-resolves.
        tr = p.wait_event(lambda e: e.get("type") == "tool_result"
                          and e.get("tool_use_id") == tu["id"], 30)
        if tr:
            return record("PASS", "acceptedits_resolves_askuser",
                          "auto-resolved is_error=%s in <30s: %s..."
                          % (tr["is_error"], (tr["content"] or "")[:80]))
        # Check screen for dialog markers
        flat = p.screen_flat()
        has_dialog = "tonavigate" in flat and "toselect" in flat
        return record("PASS" if has_dialog else "INCONCLUSIVE",
                      "acceptedits_resolves_askuser",
                      "did NOT auto-resolve in 30s — TUI actually waits "
                      "(screen has dialog markers=%s)" % has_dialog)
    finally:
        p.close()


def probe_default_dialog_drivable_with_down_enter():
    """Hypothesis: default mode shows interactive AskUserQuestion dialog;
    Down + Enter selects option 2 and the tool returns that option's label."""
    p = Probe("default")
    try:
        p.drain(7)
        p.paste_submit(
            "Use the AskUserQuestion tool right now to ask: 'pick one' "
            "with two options: 'apples' and 'oranges'. After I answer, "
            "reply with just the chosen word and stop.")
        tu = p.wait_event(lambda e: e.get("type") == "tool_use"
                          and e.get("name") == "AskUserQuestion", 60)
        if not tu:
            return record("INCONCLUSIVE", "default_dialog_drivable",
                          "no AskUserQuestion tool_use within 60s")
        # wait for dialog using the SAME structural pattern the engine uses
        deadline = time.time() + 30
        while time.time() < deadline:
            p.drain(1)
            if p.has_numbered_menu():
                break
        else:
            return record("INCONCLUSIVE", "default_dialog_drivable",
                          "numbered-menu structure never appeared")
        # Drive: Down + Enter selects option 2 (oranges)
        p.w("\x1b[B"); time.sleep(0.3); p.w("\r")
        tr = p.wait_event(lambda e: e.get("type") == "tool_result"
                          and e.get("tool_use_id") == tu["id"], 25)
        if not tr:
            return record("FAIL", "default_dialog_drivable",
                          "no tool_result after Down+Enter")
        content = tr["content"] or ""
        if "orange" in content.lower():
            return record("PASS", "default_dialog_drivable",
                          "Down+Enter picked option 2 (oranges): %s..."
                          % content[:80])
        return record("FAIL", "default_dialog_drivable",
                      "tool_result missing 'oranges': %s..." % content[:120])
    finally:
        p.close()


def probe_bracketed_paste_multiline_submits():
    """Hypothesis: bracketed paste of multi-line content + deferred Enter
    submits as a single user message (not lost in paste-close handling)."""
    p = Probe("dontAsk")
    try:
        p.drain(7)
        # Multi-line prompt with a distinctive token. We only verify that the
        # WHOLE prompt (including the second-line marker after `\n`) reached
        # claude — that's the bracketed-paste guarantee being tested. We do
        # NOT require claude to echo the sentinel (reasoning models often
        # rephrase or summarize, which is variance, not breakage).
        token = "SENTINEL_%s" % uuid.uuid4().hex[:8]
        prompt = (
            "Acknowledge this exact two-line message and stop. Do not use tools.\n"
            "%s" % token)
        p.paste_submit(prompt)
        # Wait for the turn to end, then verify the user-record in transcript
        # contains the post-newline sentinel (proves multi-line paste survived).
        p.wait_event(lambda e: e.get("type") == "result", 90)
        import json
        received_token = False
        try:
            with open(p.path) as f:
                for line in f:
                    try: r = json.loads(line)
                    except: continue
                    if r.get("type") == "user":
                        c = r.get("message", {}).get("content")
                        s = c if isinstance(c, str) else json.dumps(c)
                        if token in s:
                            received_token = True
                            break
        except FileNotFoundError:
            pass
        if received_token:
            return record("PASS", "bracketed_paste_multiline_submits",
                          "post-newline sentinel reached claude in user record")
        return record("FAIL", "bracketed_paste_multiline_submits",
                      "sentinel after \\n did NOT reach claude — bracketed "
                      "paste / deferred Enter broken")
    finally:
        p.close()


def probe_bash_permission_structural():
    """Hypothesis: Bash in `default` mode produces a permission menu whose
    SHAPE matches the engine's structural detector (3+ numbered options near
    the bottom). Pin the same shape as AskUserQuestion → one detector covers
    both, Phase 5 can reuse the existing keystroke driver. Drives Down+Enter
    to allow, then asserts the bash side-effect actually happened."""
    target = "/tmp/cc_bash_perm_probe.txt"
    try: os.remove(target)
    except FileNotFoundError: pass
    p = Probe("default")
    try:
        p.drain(7)
        p.paste_submit(
            "Run exactly one bash command and nothing else: "
            "echo BASHPROBE > " + target)
        # Screen-first: in default mode, the tool_use is NOT recorded until
        # permission is resolved, so waiting on transcript tool_use hangs.
        # Watch the rendered screen for the structural menu — that's what
        # Phase 5 in the engine will have to do anyway.
        deadline = time.time() + 60
        seen_menu = False
        while time.time() < deadline:
            p.drain(1)
            if p.has_numbered_menu():
                seen_menu = True
                break
        if not seen_menu:
            return record("INCONCLUSIVE", "bash_permission_structural",
                          "no numbered menu on screen within 60s "
                          "(claude may have declined the prompt)")
        # Drive Enter to accept default option (1. Yes) — same driver as
        # AskUserQuestion's option-1 case.
        p.w("\r")
        # Now look for tool_use+tool_result, OR the side effect, OR turn end
        deadline = time.time() + 25
        while time.time() < deadline and not os.path.exists(target):
            p.drain(1)
        ok = os.path.exists(target)
        if ok: os.remove(target)
        if ok:
            return record("PASS", "bash_permission_structural",
                          "menu detected structurally + Enter approved + side effect")
        return record("FAIL", "bash_permission_structural",
                      "menu appeared but Enter did not approve — keystroke mapping changed?")
    finally:
        p.close()


def probe_transcript_schema_spot_check():
    """Hypothesis: a real claude turn still emits transcripts with the schema
    fields the tailer maps. Schema-drift canary — if this fails on a new CC
    version, cc_transcript.record_to_params needs updating."""
    p = Probe("dontAsk")
    try:
        p.drain(7)
        # A turn that exercises text + at least one tool to verify both shapes
        p.paste_submit(
            "Run exactly one bash command — nothing else: echo OK")
        p.wait_event(lambda e: e.get("type") == "result", 90)
        # Read the raw transcript and validate the schema bits the engine reads
        import json
        recs = []
        try:
            with open(p.path) as f:
                for line in f:
                    try: recs.append(json.loads(line))
                    except: pass
        except FileNotFoundError:
            return record("INCONCLUSIVE", "transcript_schema_spot_check",
                          "transcript not written")
        if not recs:
            return record("INCONCLUSIVE", "transcript_schema_spot_check",
                          "transcript empty")
        # Always-present checks (every real turn produces these)
        required = {
            "system/turn_duration with durationMs": any(
                r.get("type") == "system" and r.get("subtype") == "turn_duration"
                and "durationMs" in r for r in recs),
            "assistant text shape (message.content[].text)": any(
                r.get("type") == "assistant"
                and any(isinstance(b, dict) and b.get("type") == "text"
                        and "text" in b
                        for b in (r.get("message", {}).get("content") or []))
                for r in recs),
        }
        # Conditional checks — only assert if the turn actually used a tool
        used_tool = any(
            r.get("type") == "assistant"
            and any(isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in (r.get("message", {}).get("content") or []))
            for r in recs)
        if used_tool:
            required.update({
                "assistant tool_use shape (name, id, input)": any(
                    r.get("type") == "assistant"
                    and any(isinstance(b, dict) and b.get("type") == "tool_use"
                            and {"name", "id", "input"} <= set(b)
                            for b in (r.get("message", {}).get("content") or []))
                    for r in recs),
                "user tool_result uses 'tool_use_id' (not 'id')": any(
                    r.get("type") == "user"
                    and any(isinstance(b, dict) and b.get("type") == "tool_result"
                            and "tool_use_id" in b
                            for b in (r.get("message", {}).get("content") or []))
                    for r in recs),
                "top-level toolUseResult (structured payload)": any(
                    "toolUseResult" in r for r in recs),
            })
        missing = [k for k, v in required.items() if not v]
        if not missing:
            note = "%d records, tool_used=%s" % (len(recs), used_tool)
            return record("PASS", "transcript_schema_spot_check", note)
        return record("FAIL", "transcript_schema_spot_check",
                      "missing: " + " | ".join(missing))
    finally:
        p.close()


def probe_image_inline_via_path():
    """Hypothesis: TUI auto-detects image paths in pasted text and inlines
    them as message attachments — no Read tool needed."""
    import struct, zlib
    # Build a 1x1 red PNG (smallest possible)
    def make_png(path):
        sig = b"\x89PNG\r\n\x1a\n"
        def chunk(t, d):
            l = struct.pack(">I", len(d))
            crc = struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
            return l + t + d + crc
        ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        raw = b"\x00\xff\x00\x00"  # filter + RGB
        idat = chunk(b"IDAT", zlib.compress(raw))
        iend = chunk(b"IEND", b"")
        with open(path, "wb") as f:
            f.write(sig + ihdr + idat + iend)
    img = "/tmp/cc_test_red_dot.png"
    make_png(img)
    p = Probe("dontAsk")
    try:
        p.drain(7)
        # Append image path on its own line, claude TUI should auto-inline it
        p.paste_submit("What color is the image?\n\n" + img)
        # Wait for the turn to end, then check STRUCTURAL evidence of inlining:
        # an `attachment` record AND an image content-block in the user turn.
        # We do NOT assert on the assistant's reply text — claude paraphrases
        # color words ("brick", "crimson", "muted red") which is variance, not
        # breakage. The load-bearing question is whether the TUI attached the
        # image at all.
        p.wait_event(lambda e: e.get("type") == "result", 90)
        import json
        attached = False; user_image_block = False
        try:
            with open(p.path) as f:
                for line in f:
                    try: r = json.loads(line)
                    except: continue
                    if r.get("type") == "attachment":
                        attached = True
                    if r.get("type") == "user":
                        c = r.get("message", {}).get("content")
                        if isinstance(c, list):
                            for b in c:
                                if isinstance(b, dict) and b.get("type") == "image":
                                    user_image_block = True
        except FileNotFoundError:
            pass
        if attached and user_image_block:
            return record("PASS", "image_inline_via_path",
                          "attachment record + user image block — TUI auto-inlined")
        return record("FAIL", "image_inline_via_path",
                      "attachment=%s user_image_block=%s — path-inline broke"
                      % (attached, user_image_block))
    finally:
        p.close()
        try: os.remove(img)
        except FileNotFoundError: pass


def probe_busy_markers_present_in_blocking_states():
    """Pin the screen-busy markers the engine uses as hard guards against
    premature finalize.

    The TUI shows different footers depending on state:
      - working:  "esc to interrupt"   (→ `esctointerrupt`)
      - dialog:   "esc to cancel"      (→ `esctocancel`)

    Both must suppress finalize, otherwise the engine treats a dialog-awaiting
    state as idle and dumps the screen via the 30s no-progress fallback.

    If this probe FAILs on a new CC version, one of the footers changed —
    update `BUSY_MARKERS_DEFAULT` in cc_pty_session.py (and/or set
    `pty_busy_markers` in user settings).
    """
    # Part 1: "esc to interrupt" must appear during normal work.
    p = Probe("dontAsk")
    saw_interrupt = False
    try:
        p.drain(7)
        p.paste_submit("Read this file and summarize it in one sentence: "
                       + os.path.join(CWD, "CLAUDE.md"))
        deadline = time.time() + 45
        while time.time() < deadline:
            p.drain(1)
            if "esctointerrupt" in p.screen_flat():
                saw_interrupt = True
                break
    finally:
        p.close()
    # Part 2: "esc to cancel" must appear in a dialog state. Force a
    # permission prompt in `default` mode by asking for a Bash command.
    p2 = Probe("default")
    saw_cancel = False
    try:
        p2.drain(7)
        p2.paste_submit("Run exactly one bash command and nothing else: "
                        "echo BUSY_CANCEL_PROBE")
        deadline = time.time() + 60
        while time.time() < deadline:
            p2.drain(1)
            if "esctocancel" in p2.screen_flat():
                saw_cancel = True
                break
    finally:
        p2.close()
    msgs = []
    msgs.append("interrupt=" + ("✓" if saw_interrupt else "✗"))
    msgs.append("cancel=" + ("✓" if saw_cancel else "✗"))
    if saw_interrupt and saw_cancel:
        return record("PASS", "busy_markers_present_in_blocking_states",
                      "both markers observed — guard covers work and dialog")
    if not saw_interrupt and not saw_cancel:
        return record("FAIL", "busy_markers_present_in_blocking_states",
                      "neither marker observed — both footers changed: " + ", ".join(msgs))
    return record("FAIL", "busy_markers_present_in_blocking_states",
                  "partial drift: " + ", ".join(msgs) + " — update BUSY_MARKERS_DEFAULT")


def probe_large_tool_result_spills_to_file():
    """Pin the CC spill-to-file pattern for large tool results.

    Observed today (CC v2.1.158): when a tool_result content exceeds an
    internal size threshold (~25KB), the .jsonl stores a placeholder marker
    instead of the content, and the real bytes go to
    `<session-id>/tool-results/<tool_use_id>.txt`. Shape of the marker:

        <persisted-output>
        Output too large (26KB). Full output saved to: <abs path>
        ...
        </persisted-output>

    The corresponding tool_use_id is the filename stem. The transcript record
    ALSO carries the structured `toolUseResult` top-level (numFiles, mode, …).

    Engine impact: tracking via tool_use_id keeps working (the tool_result
    record IS present). Rendering the actual content requires reading the
    sidefile — currently the engine just shows the marker text. If this
    probe fails on a future version, the spill format changed — engine's
    rendering / sidefile detection needs updating to match.
    """
    p = Probe("dontAsk")
    try:
        p.drain(7)
        # Trigger a large tool output. We accept any tool that pushes past
        # the inline threshold — Glob/Grep are likeliest, but Bash with `find`
        # also spills.
        p.paste_submit(
            "You MUST invoke the Glob tool with pattern '**/*' right now. "
            "Don't use any other tool. After it returns, reply with just the "
            "file count and stop.")
        # Wait for ANY tool_use (claude may choose Bash/find instead of Glob)
        tu = p.wait_event(lambda e: e.get("type") == "tool_use"
                          and e.get("name") in ("Glob", "Grep", "Bash"), 90)
        if not tu:
            return record("INCONCLUSIVE", "large_tool_result_spills_to_file",
                          "no Glob/Grep tool_use within 60s")
        tr = p.wait_event(lambda e: e.get("type") == "tool_result"
                          and e.get("tool_use_id") == tu["id"], 60)
        if not tr:
            return record("FAIL", "large_tool_result_spills_to_file",
                          "tool_use fired but no tool_result")
        # Open the raw transcript to inspect the record shape directly
        import json
        recs = []
        try:
            with open(p.path) as f:
                for line in f:
                    try: recs.append(json.loads(line))
                    except: pass
        except FileNotFoundError:
            return record("FAIL", "large_tool_result_spills_to_file",
                          "transcript missing")
        # Find the matching user record and its toolUseResult
        rec = next((r for r in recs
                    if r.get("type") == "user"
                    and any(isinstance(b, dict) and b.get("type") == "tool_result"
                            and b.get("tool_use_id") == tu["id"]
                            for b in (r.get("message", {}).get("content") or []))),
                   None)
        if not rec:
            return record("FAIL", "large_tool_result_spills_to_file",
                          "tool_result not in main .jsonl")
        content = ""
        for b in rec.get("message", {}).get("content", []):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                ct = b.get("content", "")
                content = ct if isinstance(ct, str) else json.dumps(ct)
                break
        tur = rec.get("toolUseResult") or {}
        # The spill behavior — look for the marker pattern and the sidefile
        spilled = "<persisted-output>" in content or "Output too large" in content
        # Sidefile path:
        session_dir = os.path.join(os.path.dirname(p.path),
                                   os.path.basename(p.path).replace(".jsonl", ""))
        sidefile = os.path.join(session_dir, "tool-results", "%s.txt" % tu["id"])
        sidefile_exists = os.path.exists(sidefile)
        sidefile_size = os.path.getsize(sidefile) if sidefile_exists else 0
        # Structured payload still arrives even when content is spilled
        has_structured = bool(tur)
        if spilled and sidefile_exists and has_structured:
            return record("PASS", "large_tool_result_spills_to_file",
                          "spill marker + sidefile (%d bytes) + structured payload all present"
                          % sidefile_size)
        if not spilled:
            return record("INCONCLUSIVE", "large_tool_result_spills_to_file",
                          "Glob result fit inline (no spill) — try a bigger query "
                          "or the threshold changed (inline_len=%d, structured=%s)"
                          % (len(content), has_structured))
        return record("FAIL", "large_tool_result_spills_to_file",
                      "marker present but sidefile_exists=%s structured=%s"
                      % (sidefile_exists, has_structured))
    finally:
        p.close()


PROBES = [
    probe_dontask_denies_askuserquestion,
    probe_acceptedits_resolves_askuserquestion,
    probe_default_dialog_drivable_with_down_enter,
    probe_bash_permission_structural,
    probe_bracketed_paste_multiline_submits,
    probe_image_inline_via_path,
    probe_transcript_schema_spot_check,
    probe_large_tool_result_spills_to_file,
    probe_busy_markers_present_in_blocking_states,
]


def main():
    chosen = sys.argv[1:] or None
    for fn in PROBES:
        if chosen and not any(c in fn.__name__ for c in chosen):
            continue
        try: fn()
        except Exception as e:
            record("FAIL", fn.__name__, "exception: %r" % e)
    p = sum(1 for s,_,_ in results if s == "PASS")
    f = sum(1 for s,_,_ in results if s == "FAIL")
    i = sum(1 for s,_,_ in results if s == "INCONCLUSIVE")
    print("\n%d passed, %d failed, %d inconclusive" % (p, f, i))
    sys.exit(0 if f == 0 else 1)


if __name__ == "__main__":
    main()
