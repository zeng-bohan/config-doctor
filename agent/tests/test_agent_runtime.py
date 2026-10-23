"""Agent 运行时工程测试：退避策略、上下文压缩、空转熔断、运行汇总与畸形响应容错。
全部使用注入的假件，不调用真实模型或 Java 进程。"""
import copy
import io
import json
import os
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from agent.provider import DeepSeekProvider, ModelError
from agent.worker import compact_messages, run_task
from agent.tests.test_worker_protocol import FakeClient, FakeProvider, call_tool


class ProviderBackoffTests(unittest.TestCase):
    """注入 sleep 记录器验证退避节奏；urlopen 全部打桩。"""

    def setUp(self):
        self.environment = patch.dict(os.environ, {"DEEPSEEK_API_KEY": "mock-key-never-sent"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.sleeps = []
        self.provider = DeepSeekProvider(max_calls=6, sleep_fn=self.sleeps.append,
                                         jitter=lambda low, high: 0.0)

    def _http_error(self, status, headers=None):
        return urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions", status, "temporary",
            headers or {}, io.BytesIO(b"temporary test response"))

    def _success(self):
        payload = {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                   "model": "mock-returned-model", "id": "mock-response"}
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    def test_backoff_is_exponential_with_jitter(self):
        with patch("agent.provider.urllib.request.urlopen",
                   side_effect=[self._http_error(503), self._http_error(503), self._success()]):
            message = self.provider.complete([{"role": "user", "content": "synthetic"}])
        self.assertEqual(message["content"], "ok")
        self.assertEqual(len(self.sleeps), 2)
        self.assertGreaterEqual(self.sleeps[0], 0.2)
        self.assertGreaterEqual(self.sleeps[1], 0.4)
        self.assertLessEqual(self.sleeps[1], 8.0)

    def test_retry_after_header_overrides_backoff(self):
        error = self._http_error(429, {"Retry-After": "3"})
        with patch("agent.provider.urllib.request.urlopen",
                   side_effect=[error, self._success()]):
            self.provider.complete([{"role": "user", "content": "synthetic"}])
        self.assertEqual(self.sleeps, [3.0])

    def test_attempts_exhausted_raises_model_error(self):
        provider = DeepSeekProvider(max_calls=5, max_attempts=2, sleep_fn=lambda delay: None)
        with patch("agent.provider.urllib.request.urlopen",
                   side_effect=self._http_error(502)):
            with self.assertRaisesRegex(ModelError, "status=502"):
                provider.complete([{"role": "user", "content": "synthetic"}])
        self.assertEqual(len(provider.network_errors), 2)

    def test_usage_summary_aggregates_tokens_retries_and_latency(self):
        provider = DeepSeekProvider(max_calls=5, sleep_fn=lambda delay: None)
        provider.usage = [
            {"prompt_tokens": 100, "completion_tokens": 20},
            {"prompt_tokens": 50, "completion_tokens": 10},
        ]
        provider.network_errors = [{"type": "HTTPError"}]
        provider.latencies_ms = [900, 200, 700]
        summary = provider.usage_summary()
        self.assertEqual(summary["modelCalls"], 0)
        self.assertEqual(summary["retries"], 1)
        self.assertEqual(summary["promptTokens"], 150)
        self.assertEqual(summary["completionTokens"], 30)
        self.assertEqual(summary["totalTokens"], 180)
        self.assertEqual(summary["latencyMs"], {"min": 200, "median": 700, "max": 900})


class ContextCompactionTests(unittest.TestCase):

    def _messages(self, big="x" * 3000):
        return [
            {"role": "system", "content": "system prompt must survive"},
            {"role": "user", "content": "symptom must survive"},
            {"role": "tool", "tool_call_id": "c1", "content": big},
            {"role": "assistant", "content": "collected evidence"},
            {"role": "tool", "tool_call_id": "c2", "content": big},
        ]

    def test_old_tool_results_compacted_with_hash_marker(self):
        messages = self._messages()
        compacted, count = compact_messages(messages, budget=3000)
        self.assertEqual(count, 2)
        self.assertEqual(compacted[0]["content"], "system prompt must survive")
        self.assertEqual(compacted[1]["content"], "symptom must survive")
        for index in (2, 4):
            self.assertIn("···compact:", compacted[index]["content"])
            self.assertIn("chars omitted···", compacted[index]["content"])
            self.assertLess(len(compacted[index]["content"]), 1000)
        total = sum(len(str(item)) for item in compacted)
        self.assertLessEqual(total, 3000)

    def test_compaction_is_idempotent_and_recent_round_preferred(self):
        messages = self._messages()
        once, _ = compact_messages(messages, budget=3000)
        twice, count = compact_messages(once, budget=3000)
        self.assertEqual(count, 0)
        self.assertEqual(once, twice)

    def test_nothing_compacted_under_budget(self):
        messages = self._messages(big="small")
        compacted, count = compact_messages(messages, budget=100000)
        self.assertEqual(count, 0)
        self.assertEqual(compacted, messages)


class WorkerRuntimeTests(unittest.TestCase):

    def setUp(self):
        self.output_root = Path(__file__).resolve().parents[1] / "evaluation"

    def run_worker(self, provider, client, **kwargs):
        import tempfile
        temporary = tempfile.TemporaryDirectory(dir=self.output_root)
        self.addCleanup(temporary.cleanup)
        return run_task("task-test", provider=provider, client=client,
                        output_dir=temporary.name, **kwargs)

    def test_idle_rounds_breaker_stops_early(self):
        provider = FakeProvider([])
        client = FakeClient()
        trace = self.run_worker(provider, client, max_rounds=6)
        self.assertEqual(trace["status"], "FAILED")
        self.assertIn("no tool progress", trace["error"])
        self.assertEqual(provider.calls, 2)
        self.assertEqual(trace["summary"]["rounds"], 2)

    def test_huge_tool_result_is_compacted_and_event_recorded(self):
        client = FakeClient()
        huge = {"id": "probe-evidence", "tool": "probe", "payload": "z" * 40000}
        client.tool_behaviors["probe"] = huge
        provider = FakeProvider([
            call_tool("probe", {}),
            call_tool("submit_diagnosis", {
                "rootCause": "HEALTHY", "action": "NONE",
                "evidenceIds": ["probe-evidence"], "summary": "probe passed",
            }, "call-submit"),
        ])
        trace = self.run_worker(provider, client, context_budget=4000)
        self.assertEqual(trace["status"], "COMPLETED")
        compactions = [event for event in trace["events"] if event.get("type") == "context_compact"]
        self.assertEqual(len(compactions), 1)
        checkpoint_messages = client.checkpoints[-1]["messages"]
        tool_contents = [m["content"] for m in checkpoint_messages if m.get("role") == "tool"]
        self.assertTrue(any("···compact:" in content for content in tool_contents))

    def test_run_summary_reports_rounds_tokens_and_compactions(self):
        provider = FakeProvider([
            call_tool("probe", {}),
            call_tool("submit_diagnosis", {
                "rootCause": "HEALTHY", "action": "NONE",
                "evidenceIds": ["probe-evidence"], "summary": "ok",
            }, "call-submit"),
        ])
        provider.usage_summary = lambda: {
            "modelCalls": 2, "retries": 1, "promptTokens": 500, "completionTokens": 40,
            "totalTokens": 540, "latencyMs": {"min": 100, "median": 150, "max": 200}}
        trace = self.run_worker(provider, FakeClient())
        summary = trace["summary"]
        self.assertEqual(summary["outcome"], "COMPLETED")
        self.assertEqual(summary["rounds"], 2)
        self.assertEqual(summary["toolCalls"], 2)
        self.assertEqual(summary["contextCompactions"], 0)
        self.assertEqual(summary["model"]["totalTokens"], 540)
        self.assertEqual(summary["model"]["latencyMs"]["median"], 150)
        self.assertIn("wallMs", summary)

    def test_malformed_tool_call_gets_synthesized_id_and_error_observation(self):
        malformed_round = {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "type": "function",
                "function": {"name": "probe", "arguments": {"unexpected": "dict-not-string"}},
            }],
        }
        provider = FakeProvider([
            malformed_round,
            call_tool("submit_diagnosis", {
                "rootCause": "HEALTHY", "action": "NONE",
                "evidenceIds": ["probe-evidence"], "summary": "recovered",
            }, "call-submit"),
        ])
        client = FakeClient()
        trace = self.run_worker(provider, client)
        self.assertEqual(trace["status"], "COMPLETED")
        self.assertFalse(trace["events"][0]["ok"])
        self.assertIn("read tools accept no parameters", trace["events"][0]["error"])
        checkpoint_messages = client.checkpoints[-1]["messages"]
        tool_messages = [m for m in checkpoint_messages if m.get("role") == "tool"]
        self.assertTrue(any(m.get("tool_call_id", "").startswith("call-") for m in tool_messages))


if __name__ == "__main__":
    unittest.main()
