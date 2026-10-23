"""仅使用桩对象测试评测编排，不调用模型、Java服务或真实接口。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from agent.evaluation import run_eval


class FakeOperator:
    """评测编排的协议桩，不访问Java服务。"""

    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = []
        self.approved = False

    def request(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        if method == "POST" and path == "/api/cases":
            return {"id": data["id"], "configVersion": 1}
        if method == "POST" and path == "/api/tasks":
            return {"id": "task-mock-eval"}
        if method == "GET" and path == "/api/tasks/task-mock-eval":
            return {
                "id": "task-mock-eval",
                "status": "COMPLETED" if self.approved else "AWAITING_APPROVAL",
                "proposal": copy.deepcopy(self.proposal),
                "evidence": [{"id": "probe-e-1", "tool": "probe", "data": {"passed": False}}],
            }
        if method == "POST" and path == "/api/tasks/task-mock-eval/approve":
            self.approved = True
            return {
                "status": "COMPLETED",
                "configVersion": 2,
                "verification": {"passed": True, "code": "OK"},
                "evidenceId": "approval-e-1",
            }
        raise AssertionError(f"unexpected mocked endpoint {method} {path}")


class EvaluationDatasetTests(unittest.TestCase):
    def test_dataset_has_eight_synthetic_variants_per_family(self):
        records, digest = run_eval.load_dataset()
        self.assertEqual(len(records), 40)
        self.assertEqual(len(digest), 64)
        for family in run_eval.FAMILIES:
            rows = [row for row in records if row["family"] == family]
            self.assertEqual(len(rows), 8)
        for row in records:
            self.assertTrue(row["synthetic"])
            self.assertFalse(row["independentProductionIncident"])
            self.assertEqual(row["quantity"], 2)
            self.assertEqual(set(row["config"]), {
                "upstreamDelayMs", "timeoutMs", "cachePrice", "sourcePrice", "capacity", "taxRate"
            })
            self.assertEqual(row["expected"]["rootCause"], row["family"])
            self.assertEqual(row["expected"]["action"], run_eval.ACTIONS[row["family"]])

    def test_symptoms_include_untrusted_interference_variants(self):
        records, _ = run_eval.load_dataset()
        noisy = [row for row in records if "忽略" in row["symptom"] or "泄露令牌" in row["symptom"]]
        self.assertGreaterEqual(len(noisy), 5)

    def test_each_row_has_only_its_declared_fault_family(self):
        records, _ = run_eval.load_dataset()
        for row in records:
            config = row["config"]
            family = row["family"]
            if family == "TIMEOUT":
                self.assertGreaterEqual(config["upstreamDelayMs"] - config["timeoutMs"], 25)
                self.assertEqual(config["cachePrice"], config["sourcePrice"])
                self.assertEqual(config["capacity"], 10)
                self.assertEqual(config["taxRate"], 0.06)
            elif family == "CACHE_DRIFT":
                self.assertNotEqual(config["cachePrice"], config["sourcePrice"])
                self.assertEqual(config["upstreamDelayMs"], 20)
                self.assertEqual(config["timeoutMs"], 70)
                self.assertEqual(config["capacity"], 10)
                self.assertEqual(config["taxRate"], 0.06)
            elif family == "CAPACITY":
                self.assertLess(config["capacity"], 2)
                self.assertEqual(config["cachePrice"], config["sourcePrice"])
                self.assertEqual(config["taxRate"], 0.06)
            elif family == "TAX":
                self.assertNotEqual(config["taxRate"], 0.06)
                self.assertEqual(config["cachePrice"], config["sourcePrice"])
                self.assertEqual(config["capacity"], 10)
            else:
                self.assertLessEqual(config["upstreamDelayMs"], config["timeoutMs"])
                self.assertGreaterEqual(config["capacity"], 2)
                self.assertEqual(config["cachePrice"], config["sourcePrice"])
                self.assertEqual(config["capacity"], 10)
                self.assertEqual(config["taxRate"], 0.06)

    def test_dataset_validation_rejects_backend_out_of_range_values(self):
        records, _ = run_eval.load_dataset()
        records[0]["config"]["timeoutMs"] = 0
        with self.assertRaisesRegex(run_eval.DatasetError, "timeoutMs"):
            run_eval.validate_dataset(records)

        records, _ = run_eval.load_dataset()
        records[0]["config"]["sourcePrice"] = 0
        with self.assertRaisesRegex(run_eval.DatasetError, "sourcePrice"):
            run_eval.validate_dataset(records)

    def test_baseline_parser_accepts_only_structured_symptom_only_answers(self):
        valid = {
            "role": "assistant",
            "content": (
                '{"rootCause":"TIMEOUT","action":"RESTORE_TIMEOUT",'
                '"evidenceIds":[],"summary":"症状文字提示超时，但没有配置证据。"}'
            ),
        }
        self.assertEqual(run_eval.parse_baseline_answer(valid)["rootCause"], "TIMEOUT")
        invalid = {
            "role": "assistant",
            "content": (
                '{"rootCause":"TIMEOUT","action":"REFRESH_CACHE",'
                '"evidenceIds":["invented"],"summary":"不应接受。"}'
            ),
        }
        with self.assertRaises(ValueError):
            run_eval.parse_baseline_answer(invalid)

    def test_baseline_case_uses_mock_provider_and_records_no_evidence(self):
        class Provider:
            model = "requested-alias"
            calls = 1
            network_errors = []
            usage = [{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}]
            responses = [{"model": "actual-returned-model", "usage": usage[0]}]

            def complete(self, messages, tools=None):
                self.messages = messages
                self.tools = tools
                return {
                    "role": "assistant",
                    "content": (
                        '{"rootCause":"HEALTHY","action":"NONE",'
                        '"evidenceIds":[],"summary":"症状没有显示异常。"}'
                    ),
                }

        row = run_eval.load_dataset()[0][32]
        provider = Provider()
        with tempfile.TemporaryDirectory(dir=run_eval.EVALUATION_DIR) as temporary:
            result = run_eval.run_baseline_case(row, provider, temporary)
            self.assertEqual(provider.tools, None)
            self.assertEqual(provider.messages[1]["content"], row["symptom"])
            self.assertEqual(result["taskId"], None)
            self.assertEqual(result["evidence"], [])
            self.assertEqual(result["tokens"]["total_tokens"], 15)
            self.assertEqual(result["modelId"], "actual-returned-model")
            self.assertEqual(result["requestedModelId"], "requested-alias")
            self.assertEqual(result["actualModelIds"], ["actual-returned-model"])
            self.assertTrue(Path(temporary, f"baseline-{row['id']}.json").is_file())

    def test_baseline_failure_trace_keeps_network_errors_without_fake_usage(self):
        class FailingProvider:
            model = "requested-alias"
            calls = 2
            responses = []
            usage = []
            network_errors = [{"type": "URLError", "status": None, "attempt": 1}]

            def complete(self, messages, tools=None):
                raise RuntimeError("network exhausted")

        row = run_eval.load_dataset()[0][0]
        with tempfile.TemporaryDirectory(dir=run_eval.EVALUATION_DIR) as temporary:
            result = run_eval.run_baseline_case(row, FailingProvider(), temporary)
            trace = json.loads(Path(temporary, f"baseline-{row['id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(result["networkErrors"][0]["type"], "URLError")
        self.assertEqual(result["tokens"]["total_tokens"], None)
        self.assertEqual(trace["usage"], [])

    def test_prompt_hash_is_stable_and_tracks_tools(self):
        first = run_eval.prompt_hash("system", [{"name": "probe"}])
        second = run_eval.prompt_hash("system", [{"name": "probe"}])
        changed = run_eval.prompt_hash("system", [{"name": "config"}])
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_output_directory_is_confined_to_evaluation_tree(self):
        with self.assertRaises(ValueError):
            run_eval._output_dir(run_eval.ROOT)


class EvaluationOrchestrationTests(unittest.TestCase):
    """Only injected Java/worker fakes are used; no local service is contacted."""

    def test_agent_case_does_not_send_evaluator_labels_and_approves_matching_patch(self):
        case = run_eval.load_dataset()[0][0]
        proposal = {
            "rootCause": case["expected"]["rootCause"],
            "action": case["expected"]["action"],
            "evidenceIds": ["probe-e-1"],
            "summary": "mock proposal tied to local evidence",
        }
        operator = FakeOperator(proposal)
        worker = object()
        provider = object()

        def fake_run_task(task_id, provider, client, output_dir):
            self.assertEqual(task_id, "task-mock-eval")
            self.assertIs(client, worker)
            return {
                "taskId": task_id,
                "status": "AWAITING_APPROVAL",
                "elapsedMs": 25,
                "toolCalls": 3,
                "events": [
                    {"tool": "probe", "ok": True},
                    {"tool": "config", "ok": True},
                    {"tool": "submit_diagnosis", "ok": True},
                ],
                "usage": [{"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}],
                "modelResponses": [],
                "networkErrors": [],
                "proposal": {"proposal": proposal},
            }

        with tempfile.TemporaryDirectory(dir=run_eval.EVALUATION_DIR) as temporary:
            result = run_eval.run_agent_case(
                case,
                "mock-run",
                operator,
                worker,
                provider,
                Path(temporary),
                approve_matching=True,
                run_task_fn=fake_run_task,
            )
            case_request = next(call[2] for call in operator.calls if call[1] == "/api/cases")
            task_request = next(call[2] for call in operator.calls if call[1] == "/api/tasks")
            case_identifier = case_request["id"].casefold()
            self.assertNotIn(case["family"].casefold(), case_identifier)
            self.assertNotIn(case["id"].casefold(), case_identifier)
            self.assertNotIn("expected", case_request)
            self.assertNotIn("expected", task_request)
            self.assertEqual(case_request["config"], case["config"])
            self.assertEqual(case_request["symptom"], case["symptom"])
            self.assertEqual(task_request["symptom"], case["symptom"])
            self.assertEqual(result["rootCause"], case["expected"]["rootCause"])
            self.assertTrue(result["evidenceValid"])
            self.assertTrue(result["recovered"])
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(result["taskStatusBeforeApproval"], "AWAITING_APPROVAL")
            self.assertEqual(result["tokens"]["total_tokens"], 120)
            self.assertTrue(any(path.endswith("/approve") for _, path, _ in operator.calls))

    def test_wrong_proposal_is_not_operator_approved(self):
        case = run_eval.load_dataset()[0][0]
        operator = FakeOperator({
            "rootCause": "CACHE_DRIFT",
            "action": "REFRESH_CACHE",
            "evidenceIds": ["probe-e-1"],
            "summary": "wrong mock proposal",
        })

        def fake_run_task(task_id, provider, client, output_dir):
            return {"status": "AWAITING_APPROVAL", "elapsedMs": 1, "events": [], "toolCalls": 0, "usage": []}

        with tempfile.TemporaryDirectory(dir=run_eval.EVALUATION_DIR) as temporary:
            result = run_eval.run_agent_case(
                case,
                "mock-run",
                operator,
                object(),
                object(),
                Path(temporary),
                approve_matching=True,
                run_task_fn=fake_run_task,
            )
        self.assertFalse(result["recovered"])
        self.assertIn("did not match", result["approvalSkippedReason"])
        self.assertFalse(any(path.endswith("/approve") for _, path, _ in operator.calls))

    def test_summary_keeps_recovery_denominator_on_all_injected_faults(self):
        rows = [
            {
                "family": "TIMEOUT",
                "diagnosisCorrect": True,
                "actionCorrect": True,
                "evidenceValid": True,
                "toolCalls": 3,
                "toolCallErrors": 0,
                "elapsedMs": 100,
                "tokens": {"total_tokens": 120},
                "recovered": True,
            },
            {
                "family": "CACHE_DRIFT",
                "diagnosisCorrect": False,
                "actionCorrect": False,
                "evidenceValid": False,
                "toolCalls": 2,
                "toolCallErrors": 1,
                "elapsedMs": 200,
                "tokens": {"total_tokens": None},
                "recovered": False,
            },
            {
                "family": "HEALTHY",
                "diagnosisCorrect": True,
                "actionCorrect": True,
                "evidenceValid": True,
                "toolCalls": 2,
                "toolCallErrors": 0,
                "elapsedMs": 50,
                "tokens": {"total_tokens": 75},
                "recovered": None,
            },
        ]
        summary = run_eval.summarize(rows, "tool-agent", True, "d" * 64, "p" * 64, "test-run")
        metrics = summary["metrics"]
        self.assertEqual(metrics["rootCauseDenominator"], 3)
        self.assertEqual(metrics["recoveryDenominator"], 2)
        self.assertEqual(metrics["recoveredFaults"], 1)
        self.assertEqual(metrics["recoveryRate"], 0.5)
        self.assertEqual(metrics["tokenDenominator"], 2)
        self.assertEqual(metrics["medianElapsedMs"], 100)


if __name__ == "__main__":
    unittest.main()

