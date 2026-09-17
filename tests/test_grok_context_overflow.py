"""DeepSeek Flash 400: shared 1M window vs Grok's 384k completion reservation."""
import asyncio
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from grok_backend import (  # noqa: E402
    DEEPSEEK_V4_CONTEXT_TOKENS,
    DEEPSEEK_V4_MAX_OUTPUT_TOKENS,
    apply_deepseek_shared_window_config,
    is_context_overflow_error,
    parse_input_too_large,
    parse_shared_window_overflow,
    patch_deepseek_shared_window_toml,
    rewrite_grok_query_error,
    usable_prompt_tokens,
)
from grok_main import GrokBridge  # noqa: E402


# Exact host error the user pasted (DeepSeek Flash via Grok ACP).
_USER_ERROR = (
    "grok query failed: Internal error: {'message': \"API error "
    "(status 400 Bad Request): invalid_request_error: This model's "
    "maximum context length is 1048576 tokens. However, you requested "
    "1048664 tokens (664664 in the messages, 384000 in the completion). "
    "Please reduce the length of the messages or completion.\", "
    "'http_status': 400, 'promptUsage': {'inputTokens': 11796739, "
    "'outputTokens': 13024, 'totalTokens': 11809763, "
    "'cachedReadTokens': 11792000, 'cacheCreationTokens': 0, "
    "'reasoningTokens': 9033, 'modelCalls': 18}}"
)

_USER_CONFIG = """
[models]
default = "grok-4.6"

[model.deepseek-v4-flash]
model = "deepseek-v4-flash"
base_url = "https://api.deepseek.com/v1"
name = "DeepSeek V4 Flash"
env_key = "DEEPSEEK_API_KEY"
api_backend = "chat_completions"
context_window = 1000000
max_completion_tokens = 384000

[model.deepseek-v4-pro]
model = "deepseek-v4-pro"
base_url = "https://api.deepseek.com/v1"
context_window = 1000000
max_completion_tokens = 384000

[model."gemini-3.8-flash"]
model = "google/gemini-3.8-flash"
context_window = 1048576
max_completion_tokens = 65536

[privacy]
privacy_banner_acked = "2026-08-16T06:56:12Z"
"""


class TestSharedWindowMath(unittest.TestCase):
    def test_user_error_is_88_over(self):
        # Not "11.7M tokens" — that is cumulative cache. The failing
        # request is 664664 + 384000.
        self.assertEqual(664664 + 384000, 1048664)
        self.assertEqual(1048664 - 1048576, 88)
        usable = usable_prompt_tokens(
            DEEPSEEK_V4_CONTEXT_TOKENS, DEEPSEEK_V4_MAX_OUTPUT_TOKENS)
        self.assertEqual(usable, 664576)
        self.assertGreater(664664, usable)

    def test_parse_user_error(self):
        parsed = parse_shared_window_overflow(_USER_ERROR)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["max_context"], 1048576)
        self.assertEqual(parsed["requested"], 1048664)
        self.assertEqual(parsed["messages"], 664664)
        self.assertEqual(parsed["completion"], 384000)
        self.assertEqual(parsed["over_by"], 88)
        self.assertEqual(parsed["usable_input"], 664576)

    def test_parse_ignores_unrelated(self):
        self.assertIsNone(parse_shared_window_overflow("agent_busy"))
        self.assertIsNone(parse_shared_window_overflow(""))

    def test_rewrite_mentions_shared_window_not_cumulative(self):
        out = rewrite_grok_query_error(_USER_ERROR)
        self.assertIn("664664", out)
        self.assertIn("384000", out)
        self.assertIn("1048576", out)
        self.assertIn("88", out)
        self.assertIn("664576", out)
        self.assertIn("session-cumulative", out)
        self.assertNotIn("11796739", out)
        self.assertNotEqual(out, _USER_ERROR)

    def test_rewrite_passthrough(self):
        msg = "grok query failed: agent_busy"
        self.assertEqual(rewrite_grok_query_error(msg), msg)


_GROK46_TOO_LARGE = (
    "grok query failed: Internal error: {'message': \"API error "
    "(status 400 Bad Request): invalid-argument: Failed to start "
    "sampling: [input_too_large] The prompt is too long for this "
    "model's context window (560448 tokens > 500000 tokens)\", "
    "'http_status': 400, 'promptUsage': {'inputTokens': 248808, "
    "'cachedReadTokens': 248704, 'modelCalls': 1}}"
)


class TestGrok46InputTooLarge(unittest.TestCase):
    def test_parse_packed_vs_window(self):
        parsed = parse_input_too_large(_GROK46_TOO_LARGE)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["prompt"], 560448)
        self.assertEqual(parsed["max_context"], 500000)
        self.assertEqual(parsed["over_by"], 60448)
        # Occupancy ~249k is 50% of 500k — under the 85% trip (425k).
        self.assertLess(248808, int(500000 * 0.85))
        self.assertGreater(560448, 500000)

    def test_rewrite_explains_occupancy_miss(self):
        out = rewrite_grok_query_error(_GROK46_TOO_LARGE)
        self.assertIn("560448", out)
        self.assertIn("500000", out)
        self.assertIn("occupancy", out.lower())
        self.assertNotIn("248808", out)
        self.assertTrue(is_context_overflow_error(_GROK46_TOO_LARGE))
        self.assertFalse(is_context_overflow_error("agent_busy"))


class TestConfigPatch(unittest.TestCase):
    def test_shrinks_deepseek_leaves_gemini(self):
        new = patch_deepseek_shared_window_toml(_USER_CONFIG)
        self.assertNotEqual(new, _USER_CONFIG)
        self.assertIn("context_window = 664576", new)
        self.assertEqual(new.count("context_window = 664576"), 2)
        self.assertNotIn("context_window = 1000000", new)
        # Gemini keeps its own window / completion
        self.assertIn("context_window = 1048576", new)
        self.assertIn("max_completion_tokens = 65536", new)
        self.assertIn('max_completion_tokens = 384000', new)
        self.assertIn("[model.deepseek-v4-flash]", new)
        self.assertIn('[model."gemini-3.8-flash"]', new)
        self.assertIn("[privacy]", new)
        self.assertIn("privacy_banner_acked", new)

    def test_idempotent(self):
        once = patch_deepseek_shared_window_toml(_USER_CONFIG)
        twice = patch_deepseek_shared_window_toml(once)
        self.assertEqual(once, twice)

    def test_apply_writes_temp_file_not_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(_USER_CONFIG)
            self.assertTrue(apply_deepseek_shared_window_config(path))
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("context_window = 664576", text)
            self.assertFalse(apply_deepseek_shared_window_config(path))


class TestGrokBridgeHooks(unittest.TestCase):
    def test_format_query_error_uses_rewrite(self):
        bridge = GrokBridge()
        msg = bridge.format_query_error(RuntimeError(_USER_ERROR))
        self.assertIn("DeepSeek shares", msg)
        self.assertIn("664664", msg)
        self.assertNotIn("11796739", msg)

    def test_format_query_error_passthrough(self):
        bridge = GrokBridge()
        msg = bridge.format_query_error(RuntimeError("agent_busy"))
        self.assertIn("grok query failed", msg)
        self.assertIn("agent_busy", msg)

    def test_spawn_env_calls_config_patch(self):
        path = os.path.join(_BRIDGE, "grok_main.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("apply_deepseek_shared_window_config", src)
        self.assertIn("recover_prompt_error", src)
        self.assertIn("rewrite_grok_query_error", src)

    def test_acp_base_uses_format_query_error(self):
        path = os.path.join(_BRIDGE, "acp_base.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("self.format_query_error(e)", src)
        self.assertIn("recover_prompt_error", src)

    def test_recover_compacts_then_retries_real_method(self):
        err = RuntimeError(_USER_ERROR)
        blocks = [{"type": "text", "text": "continue"}]
        bridge = GrokBridge()
        bridge.session_id = "sess-1"
        calls = []

        async def _send_acp(method, params, timeout=None):
            calls.append(("acp", method, params))
            return {"ok": True}

        async def _send_prompt(prompt_blocks):
            calls.append(("prompt", prompt_blocks))
            return {"stopReason": "end_turn"}

        bridge._send_acp = _send_acp
        bridge._send_prompt = _send_prompt
        result = asyncio.run(bridge.recover_prompt_error(err, blocks))
        self.assertEqual(result, {"stopReason": "end_turn"})
        self.assertEqual(calls[0][0], "acp")
        self.assertIn("compact_conversation", calls[0][1])
        self.assertEqual(calls[0][2], {"sessionId": "sess-1"})
        self.assertEqual(calls[-1], ("prompt", blocks))
        # Second overflow must not loop
        result2 = asyncio.run(bridge.recover_prompt_error(err, blocks))
        self.assertIsNone(result2)

    def test_recover_skips_non_overflow(self):
        bridge = GrokBridge()
        result = asyncio.run(
            bridge.recover_prompt_error(RuntimeError("nope"), []))
        self.assertIsNone(result)

    def test_recover_compacts_grok46_input_too_large(self):
        err = RuntimeError(_GROK46_TOO_LARGE)
        blocks = [{"type": "text", "text": "continue"}]
        bridge = GrokBridge()
        bridge.session_id = "sess-2"
        calls = []

        async def _send_acp(method, params, timeout=None):
            calls.append(("acp", method))
            return {"ok": True}

        async def _send_prompt(prompt_blocks):
            calls.append(("prompt", prompt_blocks))
            return {"stopReason": "end_turn"}

        bridge._send_acp = _send_acp
        bridge._send_prompt = _send_prompt
        result = asyncio.run(bridge.recover_prompt_error(err, blocks))
        self.assertEqual(result, {"stopReason": "end_turn"})
        self.assertIn("compact_conversation", calls[0][1])
        self.assertEqual(calls[-1], ("prompt", blocks))


if __name__ == "__main__":
    unittest.main()
