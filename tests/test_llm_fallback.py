"""Tests for the Groq->Ollama fallback in llm.py.

These tests do NOT hit the network or Ollama directly for the unit cases;
the fallback path is exercised with a stub Ollama response. The integration
case (real Ollama) renders a clean message-history round trip.
"""

import json
import time
import unittest

import llm


class FakeResp:
    status_code = 429
    text = "rate limited"
    headers = {}

    def raise_for_status(self):
        return None


class TestGroqFallback(unittest.TestCase):

    def test_rate_limited_falls_back(self):
        import requests
        real_post = requests.post
        blocked_called = []
        orig_ollama = llm._ollama_chat
        orig_ts = llm._last_groq_ts
        orig_blocked = llm._groq_blocked_until
        llm._last_groq_ts = 0.0
        llm._groq_blocked_until = 0.0

        def fake_post(*a, **k):
            return FakeResp()

        def fake_ollama(messages, tools, model):
            blocked_called.append(model)
            return {"message": {"role": "assistant",
                                "content": "fallback worked",
                                "tool_calls": [
                                    {"id": "call_0",
                                     "function": {"name": "get_weather",
                                                  "arguments": {},
                                                  "arguments_raw": "{}"}}]}}

        requests.post = fake_post
        llm._ollama_chat = fake_ollama
        try:
            out = llm._groq_chat([{"role": "user", "content": "hi"}],
                                 None, "qwen2.5:7b-instruct")
            self.assertEqual(out["message"]["content"], "fallback worked")
            self.assertEqual(blocked_called, ["qwen2.5:7b-instruct"])
            self.assertGreater(llm._groq_blocked_until, 0.0)
        finally:
            requests.post = real_post
            llm._ollama_chat = orig_ollama
            llm._last_groq_ts = orig_ts
            llm._groq_blocked_until = orig_blocked

    def test_groq_messages_tool_call_without_id(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "",
             "tool_calls": [
                 {"function": {"name": "get_weather",
                               "arguments": {},
                               "arguments_raw": "{}"}}]},
            {"role": "tool", "content": "ok", "name": "get_weather"},
        ]
        out = llm._to_groq_messages(msgs)
        self.assertEqual(out[1]["tool_calls"][0]["id"], "call_0")
        self.assertEqual(out[2]["tool_call_id"], "call_0")

    def test_normalize_tool_calls_missing_id(self):
        tcs = llm._normalize_tool_calls(
            {"tool_calls": [
                {"function": {"name": "geocode_city",
                              "arguments": json.dumps({"city": "Paris"})}}]})
        self.assertEqual(tcs[0]["id"], "call_0")

    def test_reset_seconds_reads_header(self):
        class R:
            headers = {"x-ratelimit-reset-tokens": "1m30s"}
        self.assertEqual(llm._reset_seconds(R()), 90.0)

    def test_reset_seconds_retry_after(self):
        class R:
            headers = {"Retry-After": "42"}
        self.assertEqual(llm._reset_seconds(R()), 42.0)

    def test_reset_seconds_default(self):
        class R:
            headers = {}
        self.assertEqual(llm._reset_seconds(R()), 60.0)

    def test_parse_reset_seconds_ms(self):
        self.assertEqual(llm._parse_reset_seconds("185ms"), 0.185)
        self.assertEqual(llm._parse_reset_seconds("500ms"), 0.5)

    def test_parse_reset_seconds_min_sec(self):
        self.assertEqual(llm._parse_reset_seconds("1m30s"), 90.0)
        self.assertEqual(llm._parse_reset_seconds("1m26.4s"), 86.4)
        self.assertEqual(llm._parse_reset_seconds("1m"), 60.0)
        self.assertEqual(llm._parse_reset_seconds("42s"), 42.0)
        self.assertEqual(llm._parse_reset_seconds("10"), 10.0)
        self.assertIsNone(llm._parse_reset_seconds("garbage"))

    def test_short_reset_not_a_fallback(self):
        class R:
            headers = {"x-ratelimit-reset-tokens": "185ms"}
        self.assertLess(llm._reset_seconds(R()), 1.0)
        self.assertLess(llm._retry_after(R()), 1.0)


class TestToolResultTruncation(unittest.TestCase):

    def test_long_result_trimmed_in_history(self):
        from orchestrator import _trim_tool_result
        long_result = "HISTORICAL weather for Udaipur, EXACT DATE=2025-09-15 " * 30
        self.assertTrue(len(long_result) > 300)
        trimmed = _trim_tool_result(long_result)
        self.assertEqual(len(trimmed), 300)
        self.assertTrue(trimmed.endswith("..."))
        self.assertNotEqual(trimmed, long_result)

    def test_short_result_untouched(self):
        from orchestrator import _trim_tool_result
        short = "Udaipur, India -> lat=24.58, lon=73.71"
        self.assertEqual(_trim_tool_result(short), short)


if __name__ == "__main__":
    unittest.main()