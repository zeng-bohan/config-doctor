"""Mock-only protocol tests; no DeepSeek call, Java process, or live HTTP request."""
import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from agent.provider import DeepSeekProvider, ModelError
from agent.worker import ApiError, JavaClient, TOOLS, run_task, validate_call


def call_tool(name, arguments, call_id="call-1"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }


class FakeProvider:
    """Protocol fake for worker tests; never calls the model provider."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.seen = []
        self.usage = []
        self.responses = []
        self.network_errors = []
        self.calls = 0
        self.model = "mock-provider-protocol-test"

    def complete(self, messages, tools=None):
        self.seen.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        self.calls += 1
        if self.messages:
            return self.messages.pop(0)
        return {"role": "assistant", "content": "no tool call"}


class FakeClient:
    """Protocol fake for worker tests; no Java endpoint is contacted."""

    def __init__(self, task=None):
        self.task = task or {
            "id": "task-test",
            "status": "CREATED",
            "symptom": "synthetic test symptom",
            "messages": [],
        }
        self.calls = []
        self.checkpoints = []
        self.failures = []
        self.tool_behaviors = {}
        self.proposal_error = None
        self.proposal_response = {
            "id": "task-test",
            "status": "COMPLETED",
            "proposal": {
                "rootCause": "HEALTHY",
                "action": "NONE",
                "evidenceIds": ["probe-evidence"],
                "summary": "mock protocol result",
            },
        }

    def request(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        if method == "GET" and path == "/api/tasks/task-test":
            return copy.deepcopy(self.task)
        if path.endswith("/claim"):
            return {"runToken": "run-token-test"}
        if path.endswith("/checkpoint"):
            self.checkpoints.append(copy.deepcopy(data))
            return {"status": "RUNNING"}
        if "/tools/" in path:
            name = path.rsplit("/", 1)[-1]
            behavior = self.tool_behaviors.get(name)
            if isinstance(behavior, Exception):
                raise behavior
            if behavior is not None:
                return copy.deepcopy(behavior)
            return {"id": f"{name}-evidence", "tool": name, "data": {"passed": True}}
        if path.endswith("/proposal"):
            if self.proposal_error is not None:
                raise self.proposal_error
            return copy.deepcopy(self.proposal_response)
        if path.endswith("/fail"):
            self.failures.append(copy.deepcopy(data))
            return {"status": "FAILED"}
        raise AssertionError(f"unexpected fake endpoint {method} {path}")


class JavaClientContractTests(unittest.TestCase):
    """The HTTP adapter is tested with patched urllib only; no network is used."""

    def test_only_strict_loopback_urls_are_accepted(self):
        accepted = JavaClient(base="http://127.0.0.1:18080")
        self.assertEqual(accepted.base, "http://127.0.0.1:18080")
        rejected = [
            "http://127.0.0.1:18080@attacker.example",
            "http://localhost:18080",
            "https://127.0.0.1:18080",
            "http://127.0.0.1",
            "http://127.0.0.1:70000",
            "http://127.0.0.1:18080/api",
            "http://127.0.0.1:18080?next=outside",
            "http://127.0.0.1:18080#fragment",
        ]
        for base in rejected:
            with self.subTest(base=base):
                with self.assertRaises(ValueError):
                    JavaClient(base=base)

    def test_request_accepts_only_fixed_api_paths(self):
        client = JavaClient(base="http://127.0.0.1:18080")
        with self.assertRaises(ValueError):
            client.request("GET", "//attacker.example/path")
        with self.assertRaises(ValueError):
            client.request("GET", "/api/tasks?id=external")

    def test_proxy_disabled_and_redirect_handler_installed(self):
        client = JavaClient(token="worker-secret-test", base="http://127.0.0.1:18080")
        opener = unittest.mock.Mock()
        opener.open.return_value = io.BytesIO(b'{"status":"UP"}')
        with patch("agent.worker.urllib.request.build_opener", return_value=opener) as build:
            result = client.request("GET", "/api/health")
        self.assertEqual(result, {"status": "UP"})
        handlers = build.call_args.args
        self.assertTrue(any(isinstance(item, urllib.request.ProxyHandler) for item in handlers))
        proxy = next(item for item in handlers if isinstance(item, urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {})
        self.assertTrue(any(type(item).__name__ == "NoRedirect" for item in handlers))

    def test_http_403_is_preserved_as_api_error(self):
        client = JavaClient(base="http://127.0.0.1:18080")
        error = urllib.error.HTTPError(
            "http://127.0.0.1:18080/api/tasks",
            403,
            "forbidden",
            {},
            io.BytesIO(b'{"error":"forbidden"}'),
        )
        opener = unittest.mock.Mock()
        opener.open.side_effect = error
        with patch("agent.worker.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ApiError) as caught:
                client.request("POST", "/api/tasks", {})
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("forbidden", str(caught.exception))


class WorkerProtocolTests(unittest.TestCase):
    """Fake Java/provider responses exercise the worker protocol, not model quality."""

    def setUp(self):
        self.output_root = Path(__file__).resolve().parents[1] / "evaluation"

    def run_worker(self, provider, client, max_rounds=6):
        temporary = tempfile.TemporaryDirectory(dir=self.output_root)
        self.addCleanup(temporary.cleanup)
        return run_task(
            "task-test",
            provider=provider,
            client=client,
            output_dir=temporary.name,
            max_rounds=max_rounds,
        )

    def test_schema_errors_and_root_action_mismatch_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_call("submit_diagnosis", {
                "rootCause": "TIMEOUT",
                "action": "REFRESH_CACHE",
                "evidenceIds": ["e-1"],
                "summary": "mismatch",
            }, 1)
        with self.assertRaises(ValueError):
            validate_call("submit_diagnosis", {
                "rootCause": "HEALTHY",
                "action": "NONE",
                "evidenceIds": ["e-1"],
                "summary": "ok",
                "unexpected": "field",
            }, 1)
        with self.assertRaises(ValueError):
            validate_call("probe", {"unexpected": True}, 1)

    def test_untrusted_tool_name_never_becomes_an_api_path(self):
        provider = FakeProvider([call_tool("approve", {})])
        client = FakeClient()
        trace = self.run_worker(provider, client, max_rounds=1)
        self.assertEqual(trace["status"], "FAILED")
        self.assertIn("tool not allowed", trace["events"][0]["error"])
        self.assertFalse(any("/approve" in path for _, path, _ in client.calls))
        self.assertEqual(len(client.failures), 1)

    def test_network_error_fails_closed_without_fake_evidence(self):
        provider = FakeProvider([call_tool("probe", {})])
        client = FakeClient()
        client.tool_behaviors["probe"] = urllib.error.URLError("offline")
        trace = self.run_worker(provider, client)
        self.assertEqual(trace["status"], "FAILED")
        self.assertIn("URLError", trace["error"])
        self.assertEqual(trace["events"], [])
        self.assertFalse(any(path.endswith("/proposal") for _, path, _ in client.calls))
        self.assertEqual(len(client.failures), 1)

    def test_round_budget_stops_provider_calls_and_records_failure(self):
        provider = FakeProvider([])
        client = FakeClient()
        trace = self.run_worker(provider, client, max_rounds=2)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(trace["status"], "FAILED")
        self.assertIn("round budget exhausted", trace["error"])
        self.assertEqual(len(client.failures), 1)

    def test_persisted_messages_resume_and_tool_feedback_is_checkpointed(self):
        saved = [
            {"role": "system", "content": "persisted system"},
            {"role": "user", "content": "persisted symptom"},
        ]
        client = FakeClient(task={
            "id": "task-test",
            "status": "CREATED",
            "symptom": "should not replace saved messages",
            "messages": saved,
        })
        provider = FakeProvider([
            call_tool("probe", {}),
            call_tool("submit_diagnosis", {
                "rootCause": "HEALTHY",
                "action": "NONE",
                "evidenceIds": ["probe-evidence"],
                "summary": "probe passed",
            }, "call-submit"),
        ])
        trace = self.run_worker(provider, client)
        self.assertEqual(trace["status"], "COMPLETED")
        self.assertEqual(provider.seen[0]["messages"], saved)
        self.assertEqual(client.checkpoints[0]["messages"], saved)
        saved_tool_feedback = [
            message
            for message in client.checkpoints[1]["messages"]
            if message.get("role") == "tool"
        ]
        self.assertEqual(len(saved_tool_feedback), 1)
        self.assertIn("probe-evidence", saved_tool_feedback[0]["content"])

    def test_tool_api_failure_is_reported_and_later_evidence_can_continue(self):
        client = FakeClient()
        client.tool_behaviors["config"] = ApiError(503, "temporary")
        provider = FakeProvider([
            call_tool("config", {}),
            call_tool("probe", {}),
            call_tool("submit_diagnosis", {
                "rootCause": "HEALTHY",
                "action": "NONE",
                "evidenceIds": ["probe-evidence"],
                "summary": "later probe passed",
            }, "call-submit"),
        ])
        trace = self.run_worker(provider, client)
        self.assertEqual(trace["status"], "COMPLETED")
        self.assertFalse(trace["events"][0]["ok"])
        self.assertIn("HTTP 503", trace["events"][0]["error"])
        self.assertTrue(trace["events"][1]["ok"])
        self.assertFalse(any(path.endswith("/approve") for _, path, _ in client.calls))

    def test_permission_error_does_not_approve_or_report_success(self):
        client = FakeClient()
        client.proposal_error = ApiError(403, "worker cannot approve")
        provider = FakeProvider([
            call_tool("submit_diagnosis", {
                "rootCause": "HEALTHY",
                "action": "NONE",
                "evidenceIds": ["probe-evidence"],
                "summary": "mock proposal",
            }),
            {"role": "assistant", "content": "no tools"},
        ])
        trace = self.run_worker(provider, client, max_rounds=2)
        self.assertEqual(trace["status"], "FAILED")
        self.assertFalse(trace["events"][0]["ok"])
        self.assertIn("HTTP 403", trace["events"][0]["error"])
        self.assertFalse(any(path.endswith("/approve") for _, path, _ in client.calls))
        self.assertEqual(len(client.failures), 1)


class ProviderRetryProtocolTests(unittest.TestCase):
    """Patched urllib tests verify retry policy without contacting DeepSeek."""

    def setUp(self):
        self.environment = patch.dict(os.environ, {"DEEPSEEK_API_KEY": "mock-key-never-sent"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def _temporary_http_error(self, status):
        return urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions",
            status,
            "temporary",
            {},
            io.BytesIO(b"temporary test response"),
        )

    def _success_response(self):
        payload = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            "model": "mock-returned-model",
            "id": "mock-response",
        }
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    def test_transient_503_retries_once_and_failed_attempt_has_no_usage(self):
        provider = DeepSeekProvider(max_calls=2)
        with patch(
            "agent.provider.urllib.request.urlopen",
            side_effect=[self._temporary_http_error(503), self._success_response()],
        ) as urlopen, patch("agent.provider.time.sleep"):
            result = provider.complete([{"role": "user", "content": "synthetic"}])
        self.assertEqual(result["content"], "ok")
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(provider.network_errors), 1)
        self.assertEqual(provider.network_errors[0]["status"], 503)
        self.assertEqual(provider.usage, [{"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}])

    def test_transient_statuses_and_network_timeout_retry_only_once(self):
        for status in (429, 502, 503, 504):
            with self.subTest(status=status):
                provider = DeepSeekProvider(max_calls=2)
                with patch(
                    "agent.provider.urllib.request.urlopen",
                    side_effect=[self._temporary_http_error(status), self._success_response()],
                ) as urlopen, patch("agent.provider.time.sleep"):
                    provider.complete([{"role": "user", "content": "synthetic"}])
                self.assertEqual(urlopen.call_count, 2)
                self.assertEqual(provider.calls, 2)
                self.assertEqual(len(provider.network_errors), 1)

        provider = DeepSeekProvider(max_calls=2)
        with patch(
            "agent.provider.urllib.request.urlopen",
            side_effect=[urllib.error.URLError("timeout"), self._success_response()],
        ) as urlopen, patch("agent.provider.time.sleep"):
            provider.complete([{"role": "user", "content": "synthetic"}])
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(provider.network_errors[0]["status"], None)

    def test_401_is_not_retried(self):
        provider = DeepSeekProvider(max_calls=3)
        with patch("agent.provider.urllib.request.urlopen", side_effect=self._temporary_http_error(401)) as urlopen:
            with self.assertRaises(ModelError):
                provider.complete([{"role": "user", "content": "synthetic"}])
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.usage, [])
        self.assertEqual(provider.network_errors[0]["status"], 401)

    def test_retry_attempt_consumes_model_call_budget(self):
        provider = DeepSeekProvider(max_calls=1)
        with patch("agent.provider.urllib.request.urlopen", side_effect=self._temporary_http_error(429)) as urlopen, patch(
            "agent.provider.time.sleep"
        ):
            with self.assertRaisesRegex(ModelError, "budget exhausted"):
                provider.complete([{"role": "user", "content": "synthetic"}])
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(provider.network_errors), 1)

    def test_malformed_model_response_is_not_retried(self):
        provider = DeepSeekProvider(max_calls=3)
        malformed = io.BytesIO(b'{"choices":[]}')
        with patch("agent.provider.urllib.request.urlopen", return_value=malformed) as urlopen:
            with self.assertRaisesRegex(ModelError, "malformed"):
                provider.complete([{"role": "user", "content": "synthetic"}])
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.network_errors, [])


if __name__ == "__main__":
    unittest.main()

