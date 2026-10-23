"""Offline protocol tests for the fixed-evidence baseline; no live model or Java calls."""
import json
import tempfile
import unittest
from pathlib import Path

from agent.evaluation import evidence_baseline, run_eval


class FakeOperator:
    def __init__(self):
        self.calls = []

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if path == "/api/cases":
            return {"id": data["id"], "configVersion": 1}
        if path == "/api/tasks":
            return {"id": "task-evidence-test"}
        raise AssertionError(f"unexpected operator request {method} {path}")


class FakeWorker:
    def __init__(self, fail_probe=False):
        self.calls = []
        self.fail_probe = fail_probe

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if path.endswith("/claim"):
            return {"runToken": "test-run-token"}
        if path.endswith("/tools/probe"):
            if self.fail_probe:
                raise RuntimeError("mock probe failure")
            return {"id": "e-probe", "tool": "probe", "data": {"passed": False, "code": "UPSTREAM_TIMEOUT"}}
        if path.endswith("/tools/config"):
            return {"id": "e-config", "tool": "config", "data": {"upstreamDelayMs": 120, "timeoutMs": 50}}
        raise AssertionError(f"unexpected worker request {method} {path}")


class FakeProvider:
    def __init__(self, response):
        self.response = response
        self.model = "requested-test-alias"
        self.max_calls = None
        self.calls = 0
        self.messages = None
        self.tools = "not-called"
        self.responses = [{"model": "actual-test-model", "elapsedMs": 7}]
        self.usage = [{"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}]
        self.network_errors = []

    def complete(self, messages, tools=None):
        self.calls += 1
        self.messages = messages
        self.tools = tools
        return self.response


def model_message(root="TIMEOUT", action="RESTORE_TIMEOUT", evidence_ids=None):
    return {
        "role": "assistant",
        "content": json.dumps({
            "rootCause": root,
            "action": action,
            "evidenceIds": evidence_ids if evidence_ids is not None else ["e-probe", "e-config"],
            "summary": "证据显示响应时间超过配置阈值。",
        }, ensure_ascii=False),
    }


class EvidenceBaselineTests(unittest.TestCase):
    def test_parser_accepts_real_citations_and_rejects_bad_schema_or_pair(self):
        parsed = evidence_baseline.parse_evidence_answer(model_message())
        self.assertEqual(parsed["evidenceIds"], ["e-probe", "e-config"])
        with self.assertRaises(ValueError):
            evidence_baseline.parse_evidence_answer(model_message("TIMEOUT", "REFRESH_CACHE"))
        extra = json.loads(model_message()["content"])
        extra["expected"] = "TIMEOUT"
        with self.assertRaises(ValueError):
            evidence_baseline.parse_evidence_answer({"content": json.dumps(extra)})

    def test_case_uses_fixed_tools_and_exactly_one_model_call_without_truth(self):
        case = run_eval.load_dataset()[0][0]
        operator = FakeOperator()
        worker = FakeWorker()
        provider = FakeProvider(model_message())
        factory_args = []

        def provider_factory(max_calls):
            factory_args.append(max_calls)
            provider.max_calls = max_calls
            return provider

        with tempfile.TemporaryDirectory(dir=run_eval.EVALUATION_DIR) as temporary:
            row, raw = evidence_baseline.run_case(
                case, "test-run", operator, worker, temporary, provider_factory
            )
            self.assertTrue(Path(temporary, f"{case['id']}.json").is_file())

        self.assertEqual(factory_args, [1])
        self.assertEqual(provider.calls, 1)
        self.assertIsNone(provider.tools)
        self.assertEqual([path.rsplit("/", 1)[-1] for _, path, _ in worker.calls], [
            "claim", "probe", "config"
        ])
        self.assertFalse(any(path.endswith("/proposal") or path.endswith("/approve") for _, path, _ in worker.calls + operator.calls))
        case_payload = operator.calls[0][2]
        task_payload = operator.calls[1][2]
        self.assertNotIn(case["id"], case_payload["id"])
        self.assertNotIn("expected", case_payload)
        self.assertNotIn("expected", task_payload)
        user_payload = json.loads(provider.messages[1]["content"])
        self.assertEqual(set(user_payload), {"symptom", "evidence"})
        self.assertEqual(user_payload["symptom"], case["symptom"])
        self.assertNotIn("expected", provider.messages[1]["content"])
        self.assertEqual(raw["status"], "COMPLETED")
        self.assertTrue(row["evidenceValid"])
        self.assertEqual(row["modelId"], "actual-test-model")
        self.assertEqual(row["requestedModelId"], "requested-test-alias")
        self.assertEqual(row["tokens"]["total_tokens"], 120)

    def test_unknown_evidence_ids_are_kept_and_scored_invalid(self):
        case = run_eval.load_dataset()[0][0]
        operator = FakeOperator()
        worker = FakeWorker()
        provider = FakeProvider(model_message(evidence_ids=["fabricated-id"]))
        with tempfile.TemporaryDirectory(dir=run_eval.EVALUATION_DIR) as temporary:
            row, _ = evidence_baseline.run_case(case, "bad-citation", operator, worker, temporary, lambda max_calls: provider)
        self.assertEqual(row["evidenceIds"], ["fabricated-id"])
        self.assertEqual(row["availableEvidenceIds"], ["e-probe", "e-config"])
        self.assertFalse(row["evidenceValid"])

    def test_tool_failure_is_recorded_and_other_fixed_query_still_runs(self):
        case = run_eval.load_dataset()[0][0]
        worker = FakeWorker(fail_probe=True)
        provider = FakeProvider(model_message(evidence_ids=["e-config"]))
        with tempfile.TemporaryDirectory(dir=run_eval.EVALUATION_DIR) as temporary:
            row, raw = evidence_baseline.run_case(
                case, "tool-error", FakeOperator(), worker, temporary, lambda max_calls: provider
            )
        self.assertEqual(row["toolCalls"], 2)
        self.assertEqual(row["toolCallErrors"], 1)
        self.assertEqual(len(raw["evidence"]), 1)
        self.assertEqual(provider.calls, 1)

    def test_summary_tracks_single_call_metrics_and_disables_recovery(self):
        rows = [
            {"family": "TIMEOUT", "diagnosisCorrect": True, "actionCorrect": True,
             "evidenceValid": True, "toolCalls": 2, "toolCallErrors": 0,
             "elapsedMs": 100, "tokens": {"total_tokens": 120}, "modelCalls": 1,
             "networkErrors": [], "actualModelIds": ["actual-test-model"],
             "requestedModelId": "requested-test-alias"},
            {"family": "HEALTHY", "diagnosisCorrect": False, "actionCorrect": False,
             "evidenceValid": False, "toolCalls": 2, "toolCallErrors": 1,
             "elapsedMs": 200, "tokens": {"total_tokens": None}, "modelCalls": 1,
             "networkErrors": [{"status": 503}], "actualModelIds": [],
             "requestedModelId": "requested-test-alias"},
        ]
        summary = evidence_baseline.summarize(rows, "dataset-hash", "prompt-hash", "summary-test")
        self.assertEqual(summary["metrics"]["rootCauseDenominator"], 2)
        self.assertEqual(summary["metrics"]["modelCalls"], 2)
        self.assertEqual(summary["metrics"]["networkErrors"], 1)
        self.assertIsNone(summary["metrics"]["recoveryRate"])
        self.assertEqual(summary["metrics"]["recoveryDenominator"], 0)


if __name__ == "__main__":
    unittest.main()
