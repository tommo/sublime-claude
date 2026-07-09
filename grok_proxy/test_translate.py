"""Unit tests for grok_proxy translation — runnable as `python -m grok_proxy.test_translate`.

Uses stdlib unittest only (no pytest dependency). Cases mirror the conformance
fixtures in CLIProxyAPI's codex_claude_request_test.go / codex_claude_response_test.go.
"""
import base64
import json
import unittest

from . import translate_request as tr
from . import translate_response as rr


def _valid_gpt_sig():
    """A signature that passes the GPT/Fernet transport-shape check."""
    dec = bytes([0x80]) + b"\x00" * 72  # len 73 -> ciphertext 16 (mult of 16)
    return base64.urlsafe_b64encode(dec).rstrip(b"=").decode()


class HelpersTest(unittest.TestCase):
    def test_budget_levels(self):
        self.assertEqual(tr.convert_budget_to_level(0), "none")
        self.assertEqual(tr.convert_budget_to_level(512), "minimal")
        self.assertEqual(tr.convert_budget_to_level(1024), "low")
        self.assertEqual(tr.convert_budget_to_level(8192), "medium")
        self.assertEqual(tr.convert_budget_to_level(24576), "high")
        self.assertEqual(tr.convert_budget_to_level(99999), "xhigh")

    def test_call_id_shortening(self):
        self.assertEqual(tr.shorten_call_id("short"), "short")
        self.assertEqual(len(tr.shorten_call_id("x" * 100)), 64)

    def test_mcp_name_shortening(self):
        nm = "mcp__server__sub__" + "t" * 80
        s = tr.shorten_name_if_needed(nm)
        self.assertTrue(s.startswith("mcp__"))
        self.assertLessEqual(len(s), 64)

    def test_gpt_signature_check(self):
        self.assertFalse(tr.is_valid_gpt_reasoning_signature(""))
        self.assertFalse(tr.is_valid_gpt_reasoning_signature("garbage"))
        self.assertTrue(tr.is_valid_gpt_reasoning_signature(_valid_gpt_sig()))

    def test_grok_thinking_is_dropped(self):
        # Grok's encrypted_content is not GPT-shaped (gAAAA) and is NOT replayable
        # via the transcript (xAI rejects it as a compaction blob). Such thinking
        # blocks must be dropped from the upstream request.
        import os
        raw = os.urandom(60)
        grok_sig = base64.b64encode(raw).rstrip(b"=").decode()
        while grok_sig[:1] in ("E", "R") or grok_sig.startswith("gAAAA"):
            raw = os.urandom(60)
            grok_sig = base64.b64encode(raw).rstrip(b"=").decode()
        self.assertFalse(tr.is_valid_gpt_reasoning_signature(grok_sig))
        req = {"model": "grok-4.5", "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hm", "signature": grok_sig},
                {"type": "text", "text": "answer"}]},
            {"role": "user", "content": "next"}]}
        body, _ = tr.translate_request("grok-4.5", req)
        self.assertFalse(any(i.get("type") == "reasoning" for i in body["input"]))


class RequestTest(unittest.TestCase):
    def _full_request(self, sig=None):
        msg_content = [{"type": "text", "text": "ok"}]
        if sig:
            msg_content.insert(0, {"type": "thinking", "thinking": "hm", "signature": sig})
        return {
            "model": "grok-4",
            # attribution as its own array element (real Claude Code shape);
            # only blocks that *start* with the prefix are stripped.
            "system": [
                {"type": "text", "text": "You are helpful."},
                {"type": "text", "text": "x-anthropic-billing-header: fingerprint-data"},
            ],
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": msg_content + [
                    {"type": "tool_use", "id": "toolu_123", "name": "read_file",
                     "input": {"path": "/a"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_123", "content": "file body"}]},
            ],
            "tools": [{"name": "read_file", "description": "d",
                       "input_schema": {"type": "object",
                                        "properties": {"path": {"type": "string"}}}}],
            "thinking": {"type": "enabled", "budget_tokens": 8192},
            "tool_choice": {"type": "auto"},
        }

    def test_system_to_developer_and_attribution_strip(self):
        body, _ = tr.translate_request("grok-4", self._full_request())
        dev = body["input"][0]
        self.assertEqual(dev["role"], "developer")
        self.assertEqual(body["instructions"], "")
        self.assertEqual(dev["content"][0]["text"], "You are helpful.")  # attribution stripped

    def test_tool_use_arguments_is_json_string(self):
        body, _ = tr.translate_request("grok-4", self._full_request())
        fc = [i for i in body["input"] if i.get("type") == "function_call"][0]
        self.assertIsInstance(fc["arguments"], str)
        self.assertEqual(json.loads(fc["arguments"]), {"path": "/a"})

    def test_tool_result_output(self):
        body, _ = tr.translate_request("grok-4", self._full_request())
        fco = [i for i in body["input"] if i.get("type") == "function_call_output"][0]
        self.assertEqual(fco["output"], "file body")

    def test_valid_signature_becomes_reasoning_item(self):
        sig = _valid_gpt_sig()
        body, _ = tr.translate_request("grok-4", self._full_request(sig=sig))
        types = [i.get("type") for i in body["input"]]
        self.assertIn("reasoning", types)
        ri = [i for i in body["input"] if i.get("type") == "reasoning"][0]
        self.assertEqual(ri["encrypted_content"], sig)

    def test_invalid_signature_dropped(self):
        body, _ = tr.translate_request("grok-4", self._full_request(sig="not-a-fernet-sig"))
        self.assertFalse(any(i.get("type") == "reasoning" for i in body["input"]))

    def test_thinking_budget_maps_to_effort(self):
        body, _ = tr.translate_request("grok-4", self._full_request())
        self.assertEqual(body["reasoning"]["effort"], "medium")
        self.assertEqual(body["reasoning"]["summary"], "auto")

    def test_forced_fields(self):
        body, _ = tr.translate_request("grok-4", self._full_request())
        self.assertTrue(body["stream"])
        self.assertFalse(body["store"])
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])

    def test_tools_normalized(self):
        body, _ = tr.translate_request("grok-4", self._full_request())
        t = body["tools"][0]
        self.assertEqual(t["type"], "function")
        self.assertFalse(t["strict"])
        self.assertNotIn("input_schema", t)
        self.assertEqual(t["parameters"]["type"], "object")

    def test_tool_choice_mapping(self):
        for tc, expect in [
            ({"type": "auto"}, "auto"),
            ({"type": "any"}, "required"),
            ({"type": "none"}, "none"),
        ]:
            req = {"model": "grok-4", "messages": [{"role": "user", "content": "x"}],
                   "tools": [{"name": "f", "input_schema": {}}], "tool_choice": tc}
            body, _ = tr.translate_request("grok-4", req)
            self.assertEqual(body["tool_choice"], expect)


class ResponseTest(unittest.TestCase):
    def _run(self, lines, short_to_original=None):
        p = rr.StreamingPipeline(short_to_original)
        out = b""
        for d in lines:
            out += p.feed_data_line(json.dumps(d))
        return out.decode()

    def test_plain_text_sequence(self):
        lines = [
            {"type": "response.created", "response": {"id": "r", "model": "grok-4"}},
            {"type": "response.output_text.delta", "delta": "Hello"},
            {"type": "response.completed", "response": {
                "id": "r", "model": "grok-4", "stop_reason": "stop",
                "usage": {"input_tokens": 10, "output_tokens": 5}}},
        ]
        s = self._run(lines)
        self.assertIn("event: message_start", s)
        self.assertIn("text_delta", s)
        self.assertIn("Hello", s)
        self.assertIn('"stop_reason":"end_turn"', s)
        self.assertIn("event: message_stop", s)

    def test_tool_use_and_args_reconstruct(self):
        _, rev = tr.translate_request("grok-4", {"tools": [{"name": "read_file", "input_schema": {}}]})
        lines = [
            {"type": "response.created", "response": {"id": "r", "model": "grok-4"}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"type": "function_call", "call_id": "call_abc", "name": "read_file", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "{\""},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "path\": \"/x\"}"},
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"type": "function_call", "call_id": "call_abc", "name": "read_file", "arguments": "{\"path\":\"/x\"}"}},
            {"type": "response.completed", "response": {"id": "r", "model": "grok-4",
                "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 2}, "output": []}},
        ]
        s = self._run(lines, rev)
        self.assertIn('"tool_use"', s)
        self.assertEqual([b for b in _blocks(s, "content_block_start") if "tool_use" in b][0].count("call_abc"), 1)
        # reconstruct streamed args
        import re
        parts = re.findall(r'"partial_json":"((?:[^"\\]|\\.)*)"', s)
        joined = "".join(json.loads('"' + pp + '"') for pp in parts)
        self.assertEqual(json.loads(joined), {"path": "/x"})
        self.assertIn('"stop_reason":"tool_use"', s)

    def test_reasoning_emits_thinking_and_signature(self):
        sig = _valid_gpt_sig()
        lines = [
            {"type": "response.created", "response": {"id": "r", "model": "grok-4"}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"type": "reasoning", "encrypted_content": sig}},
            {"type": "response.reasoning_summary_part.added", "output_index": 0, "summary_index": 0,
             "part": {"type": "summary_text", "text": ""}},
            {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 0, "delta": "hmm"},
            {"type": "response.reasoning_summary_part.done", "output_index": 0, "summary_index": 0,
             "part": {"type": "summary_text", "text": "hmm"}},
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"type": "reasoning", "encrypted_content": sig,
                      "summary": [{"type": "summary_text", "text": "hmm"}]}},
            {"type": "response.output_text.delta", "delta": "Answer"},
            {"type": "response.completed", "response": {"id": "r", "model": "grok-4",
                "stop_reason": "stop", "usage": {"input_tokens": 1, "output_tokens": 1}}},
        ]
        s = self._run(lines)
        self.assertIn("thinking_delta", s)
        self.assertIn("signature_delta", s)
        self.assertIn(sig, s)
        self.assertIn("text_delta", s)

    def test_non_stream_synthesis_with_cached_tokens(self):
        _, rev = tr.translate_request("grok-4", {"tools": [{"name": "read_file", "input_schema": {}}]})
        ns = rr.non_stream_response({
            "type": "response.completed",
            "response": {"id": "r2", "model": "grok-4", "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 3,
                          "input_tokens_details": {"cached_tokens": 4}},
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "sure"}]},
                    {"type": "function_call", "call_id": "call_z", "name": "read_file",
                     "arguments": "{\"path\":\"/y\"}"}]}}, rev)
        self.assertEqual(ns["stop_reason"], "tool_use")
        self.assertEqual(ns["usage"]["input_tokens"], 6)   # 10 - 4 cached
        self.assertEqual(ns["usage"]["cache_read_input_tokens"], 4)
        types = {b["type"] for b in ns["content"]}
        self.assertIn("text", types)
        self.assertIn("tool_use", types)


def _blocks(text, event):
    """Split an SSE text into per-frame strings for the given event."""
    frames = []
    for chunk in text.split("event: "):
        if chunk.startswith(event):
            frames.append(chunk)
    return frames


if __name__ == "__main__":
    unittest.main(verbosity=2)
