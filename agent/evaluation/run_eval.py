"""运行真实本地实验室评测；导入模块不触发外部操作。"""
import argparse
import hashlib
import json
import os
import statistics
import time
import uuid
from collections import Counter
from pathlib import Path

from agent.provider import DeepSeekProvider
from agent.worker import JavaClient, SYSTEM, TOOLS, run_task

ROOT = Path(__file__).resolve().parents[2]
EVALUATION_DIR = Path(__file__).resolve().parent
DATASET_PATH = EVALUATION_DIR / "dataset.jsonl"
FAMILIES = ("TIMEOUT", "CACHE_DRIFT", "CAPACITY", "TAX", "HEALTHY")
ACTIONS = {
    "TIMEOUT": "RESTORE_TIMEOUT",
    "CACHE_DRIFT": "REFRESH_CACHE",
    "CAPACITY": "RESTORE_CAPACITY",
    "TAX": "RESTORE_TAX",
    "HEALTHY": "NONE",
}
BASELINE_SYSTEM = """You classify a synthetic local checkout symptom from the symptom text alone.
You receive no configuration, logs, evidence, or tools. Do not claim you inspected data.
Return only one JSON object with exactly these keys:
rootCause (TIMEOUT, CACHE_DRIFT, CAPACITY, TAX, HEALTHY, or UNKNOWN),
action (RESTORE_TIMEOUT, REFRESH_CACHE, RESTORE_CAPACITY, RESTORE_TAX, NONE),
evidenceIds (always an empty array), summary (short Chinese explanation).
Use UNKNOWN and NONE when the symptom alone does not support a root cause.
Never request approval or claim that a repair was applied."""


class DatasetError(ValueError):
    pass


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_dataset(records):
    if len(records) != 40:
        raise DatasetError(f"expected 40 synthetic rows, got {len(records)}")
    counts = Counter()
    seen_ids = set()
    expected_config = {
        "upstreamDelayMs", "timeoutMs", "cachePrice", "sourcePrice", "capacity", "taxRate"
    }
    for line_number, row in enumerate(records, start=1):
        if not isinstance(row, dict):
            raise DatasetError(f"line {line_number}: row must be an object")
        case_id = row.get("id")
        family = row.get("family")
        if not isinstance(case_id, str) or not case_id or case_id in seen_ids:
            raise DatasetError(f"line {line_number}: id must be unique and non-empty")
        seen_ids.add(case_id)
        if family not in FAMILIES:
            raise DatasetError(f"line {line_number}: unsupported family")
        counts[family] += 1
        if row.get("synthetic") is not True or row.get("independentProductionIncident") is not False:
            raise DatasetError(f"line {line_number}: synthetic scope declaration missing")
        if row.get("quantity") != 2:
            raise DatasetError(f"line {line_number}: quantity must be 2")
        if not isinstance(row.get("symptom"), str) or not row["symptom"].strip():
            raise DatasetError(f"line {line_number}: symptom is required")
        config = row.get("config")
        if not isinstance(config, dict) or set(config) != expected_config:
            raise DatasetError(f"line {line_number}: config fields do not match the lab contract")
        for name in ("upstreamDelayMs", "timeoutMs", "capacity"):
            if not isinstance(config[name], int) or isinstance(config[name], bool):
                raise DatasetError(f"line {line_number}: {name} must be an integer")
        for name in ("cachePrice", "sourcePrice", "taxRate"):
            if not _is_number(config[name]):
                raise DatasetError(f"line {line_number}: {name} must be numeric")
        if not 0 <= config["upstreamDelayMs"] <= 200:
            raise DatasetError(f"line {line_number}: upstreamDelayMs is outside the lab contract")
        if not 1 <= config["timeoutMs"] <= 1000:
            raise DatasetError(f"line {line_number}: timeoutMs is outside the lab contract")
        if not 0.01 <= config["cachePrice"] <= 100000:
            raise DatasetError(f"line {line_number}: cachePrice is outside the lab contract")
        if not 0.01 <= config["sourcePrice"] <= 100000:
            raise DatasetError(f"line {line_number}: sourcePrice is outside the lab contract")
        if not 0 <= config["capacity"] <= 10000:
            raise DatasetError(f"line {line_number}: capacity is outside the lab contract")
        if not 0 <= config["taxRate"] <= 1:
            raise DatasetError(f"line {line_number}: taxRate is outside the lab contract")
        expected = row.get("expected")
        if not isinstance(expected, dict) or expected.get("rootCause") != family:
            raise DatasetError(f"line {line_number}: expected root cause must match family")
        if expected.get("action") != ACTIONS[family]:
            raise DatasetError(f"line {line_number}: expected action does not match root cause")
    if counts != Counter({family: 8 for family in FAMILIES}):
        raise DatasetError(f"expected 8 rows per family, got {dict(counts)}")


def load_dataset(path=DATASET_PATH):
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DatasetError(f"invalid JSONL: {type(error).__name__}") from None
    validate_dataset(records)
    return records, hashlib.sha256(raw).hexdigest()


def prompt_hash(system_prompt, tools=None):
    payload = {"system": system_prompt, "tools": tools}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def token_totals(usage_rows):
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    if not isinstance(usage_rows, list) or not usage_rows:
        return {key: None for key in keys}
    totals = {}
    for key in keys:
        values = [row.get(key) for row in usage_rows if isinstance(row, dict)]
        if len(values) != len(usage_rows) or any(not isinstance(value, int) for value in values):
            totals[key] = None
        else:
            totals[key] = sum(values)
    return totals


def model_identity(provider, responses=None):
    requested = getattr(provider, "model", None)
    if responses is None:
        responses = getattr(provider, "responses", [])
    actual_ids = sorted({
        response.get("model")
        for response in responses or []
        if isinstance(response, dict) and isinstance(response.get("model"), str) and response["model"]
    })
    return {
        "modelId": actual_ids[0] if len(actual_ids) == 1 else None,
        "requestedModelId": requested,
        "actualModelIds": actual_ids,
    }


def parse_baseline_answer(message):
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("baseline response must contain text content")
    try:
        answer = json.loads(message["content"])
    except json.JSONDecodeError:
        raise ValueError("baseline response is not JSON") from None
    required = {"rootCause", "action", "evidenceIds", "summary"}
    if not isinstance(answer, dict) or set(answer) != required:
        raise ValueError("baseline response schema mismatch")
    root = answer["rootCause"]
    action = answer["action"]
    allowed_roots = set(ACTIONS) | {"UNKNOWN"}
    expected_action = ACTIONS.get(root, "NONE")
    if root not in allowed_roots or action != expected_action:
        raise ValueError("baseline root cause/action mismatch")
    if answer["evidenceIds"] != []:
        raise ValueError("symptom-only baseline cannot claim evidence")
    if not isinstance(answer["summary"], str) or not answer["summary"].strip():
        raise ValueError("baseline summary is required")
    return answer


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run_baseline_case(case, provider, trace_dir):
    started = time.monotonic()
    messages = [
        {"role": "system", "content": BASELINE_SYSTEM},
        {"role": "user", "content": case["symptom"]},
    ]
    trace = {
        "mode": "baseline-no-tools",
        "datasetId": case["id"],
        "promptSha256": prompt_hash(BASELINE_SYSTEM, None),
        "messages": messages,
        "status": "FAILED",
        "responses": [],
        "usage": [],
        "networkErrors": [],
    }
    try:
        message = provider.complete(messages, tools=None)
        trace["answer"] = message
        prediction = parse_baseline_answer(message)
        trace["prediction"] = prediction
        trace["status"] = "COMPLETED"
    except Exception as error:
        trace["error"] = f"{type(error).__name__}: {error}"[:500]
        prediction = {}
    trace["responses"] = getattr(provider, "responses", [])
    trace["usage"] = getattr(provider, "usage", [])
    trace["networkErrors"] = getattr(provider, "network_errors", [])
    trace["modelCalls"] = getattr(provider, "calls", None)
    trace.update(model_identity(provider, trace["responses"]))
    elapsed_ms = round((time.monotonic() - started) * 1000)
    trace["elapsedMs"] = elapsed_ms
    trace_path = Path(trace_dir) / f"baseline-{case['id']}.json"
    _write_json(trace_path, trace)
    expected = case["expected"]
    predicted_root = prediction.get("rootCause")
    predicted_action = prediction.get("action")
    usage = token_totals(trace.get("usage", []))
    return {
        "datasetId": case["id"],
        "family": case["family"],
        "expectedRootCause": expected["rootCause"],
        "expectedAction": expected["action"],
        "taskId": None,
        "status": trace["status"],
        "traceFile": str(trace_path.relative_to(ROOT)),
        "promptSha256": trace["promptSha256"],
        "modelId": trace.get("modelId"),
        "requestedModelId": trace.get("requestedModelId"),
        "actualModelIds": trace.get("actualModelIds", []),
        "modelCalls": trace.get("modelCalls"),
        "elapsedMs": elapsed_ms,
        "tokens": usage,
        "rootCause": predicted_root,
        "action": predicted_action,
        "evidenceIds": [],
        "evidence": [],
        "evidenceValid": None,
        "diagnosisCorrect": predicted_root == expected["rootCause"],
        "actionCorrect": predicted_action == expected["action"],
        "toolCalls": 0,
        "toolCallErrors": 0,
        "networkErrors": trace.get("networkErrors", []),
        "approval": None,
        "recovered": None,
    }


def run_agent_case(case, run_id, operator, worker, provider, trace_dir, approve_matching=False, run_task_fn=run_task):
    started = time.monotonic()
    dataset_id = case["id"]
    # 工具可见caseId必须与评分标签无关，不能泄露timeout/tax等族名。
    case_id = f"eval-{run_id}-{uuid.uuid4().hex[:12]}"
    symptom = case["symptom"]
    case_payload = {"id": case_id, "symptom": symptom, "config": case["config"]}
    operator.request("POST", "/api/cases", case_payload)
    task_payload = {
        "caseId": case_id,
        "symptom": symptom,
        "idempotencyKey": f"{run_id}-{dataset_id}",
    }
    created_task = operator.request("POST", "/api/tasks", task_payload)
    task_id = created_task.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError("Java task response omitted id")

    trace = run_task_fn(
        task_id,
        provider=provider,
        client=worker,
        output_dir=str(Path(trace_dir) / "traces"),
    )
    trace["datasetId"] = dataset_id
    trace["promptSha256"] = prompt_hash(SYSTEM, TOOLS)
    trace["modelCalls"] = getattr(provider, "calls", None)
    trace["networkErrors"] = getattr(provider, "network_errors", [])
    trace.update(model_identity(provider, getattr(provider, "responses", [])))
    trace_path = Path(trace_dir) / "traces" / f"{task_id}.json"
    _write_json(trace_path, trace)

    task_detail = operator.request("GET", f"/api/tasks/{task_id}")
    proposal = task_detail.get("proposal") or {}
    evidence = task_detail.get("evidence") or []
    evidence_ids = proposal.get("evidenceIds") or []
    available_ids = {item.get("id") for item in evidence if isinstance(item, dict)}
    evidence_valid = bool(evidence_ids) and all(item in available_ids for item in evidence_ids)
    expected = case["expected"]
    root_cause = proposal.get("rootCause")
    action = proposal.get("action")
    approval = None
    approval_skipped = None
    approval_error = None
    fault_case = case["family"] != "HEALTHY"
    recovered = False if approve_matching and fault_case else None
    status_before_approval = task_detail.get("status")
    final_status = status_before_approval

    if approve_matching and fault_case:
        if (
            task_detail.get("status") == "AWAITING_APPROVAL"
            and root_cause == expected["rootCause"]
            and action == expected["action"]
        ):
            approval_started = time.monotonic()
            try:
                approval = operator.request(
                    "POST",
                    f"/api/tasks/{task_id}/approve",
                    {"idempotencyKey": f"approve-{run_id}-{dataset_id}"},
                )
                approval["elapsedMs"] = round((time.monotonic() - approval_started) * 1000)
                verification = approval.get("verification") or {}
                recovered = verification.get("passed") is True
            except Exception as error:
                approval_error = f"{type(error).__name__}: {error}"[:500]
                recovered = False
            if approval is not None:
                try:
                    final_task = operator.request("GET", f"/api/tasks/{task_id}")
                    final_status = final_task.get("status", approval.get("status", status_before_approval))
                except Exception as error:
                    approval_error = f"final task read: {type(error).__name__}: {error}"[:500]
        elif task_detail.get("status") != "AWAITING_APPROVAL":
            approval_skipped = "task has no pending repair proposal"
        else:
            approval_skipped = "proposal did not match the evaluator label; not approved"

    events = trace.get("events") or []
    tool_errors = sum(1 for event in events if event.get("ok") is False)
    elapsed_ms = trace.get("elapsedMs")
    return {
        "datasetId": dataset_id,
        "family": case["family"],
        "caseId": case_id,
        "taskId": task_id,
        "status": final_status or trace.get("status"),
        "taskStatusBeforeApproval": status_before_approval,
        "agentStatus": trace.get("status"),
        "traceFile": str(trace_path.relative_to(ROOT)),
        "promptSha256": trace["promptSha256"],
        "modelId": trace.get("modelId"),
        "requestedModelId": trace.get("requestedModelId"),
        "actualModelIds": trace.get("actualModelIds", []),
        "modelCalls": trace.get("modelCalls"),
        "elapsedMs": elapsed_ms,
        "evalElapsedMs": round((time.monotonic() - started) * 1000),
        "tokens": token_totals(trace.get("usage", [])),
        "rootCause": root_cause,
        "expectedRootCause": expected["rootCause"],
        "action": action,
        "expectedAction": expected["action"],
        "evidenceIds": evidence_ids,
        "evidence": evidence,
        "evidenceValid": evidence_valid,
        "diagnosisCorrect": root_cause == expected["rootCause"],
        "actionCorrect": action == expected["action"],
        "toolCalls": trace.get("toolCalls", len(events)),
        "toolCallErrors": tool_errors,
        "networkErrors": trace.get("networkErrors", []),
        "approval": approval,
        "approvalError": approval_error,
        "approvalSkippedReason": approval_skipped,
        "recovered": recovered,
    }


def _ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def summarize(rows, mode, approval_enabled, dataset_hash, prompt_digest, run_id):
    count = len(rows)
    root_correct = sum(row.get("diagnosisCorrect") is True for row in rows)
    action_correct = sum(row.get("actionCorrect") is True for row in rows)
    evidence_applicable = [row for row in rows if row.get("evidenceValid") is not None]
    evidence_valid = sum(row.get("evidenceValid") is True for row in evidence_applicable)
    fault_rows = [row for row in rows if row.get("family") != "HEALTHY"]
    recovered = sum(row.get("recovered") is True for row in fault_rows)
    tool_events = sum(row.get("toolCalls", 0) for row in rows)
    tool_errors = sum(row.get("toolCallErrors", 0) for row in rows)
    elapsed = [row["elapsedMs"] for row in rows if isinstance(row.get("elapsedMs"), int)]
    tokens = [
        row["tokens"]["total_tokens"]
        for row in rows
        if isinstance(row.get("tokens"), dict) and isinstance(row["tokens"].get("total_tokens"), int)
    ]
    family_counts = Counter(row.get("family") for row in rows)
    model_ids = sorted({model_id for row in rows for model_id in row.get("actualModelIds", [])})
    requested_model_ids = sorted({row.get("requestedModelId") for row in rows if row.get("requestedModelId")})
    model_call_counts = [row["modelCalls"] for row in rows if isinstance(row.get("modelCalls"), int)]
    network_error_count = sum(len(row.get("networkErrors", [])) for row in rows)
    return {
        "runId": run_id,
        "mode": mode,
        "datasetSha256": dataset_hash,
        "promptSha256": prompt_digest,
        "modelIds": model_ids,
        "requestedModelIds": requested_model_ids,
        "modelIdentityCaseCount": sum(bool(row.get("actualModelIds")) for row in rows),
        "selectedCases": count,
        "familyCounts": dict(family_counts),
        "metrics": {
            "rootCauseCorrect": root_correct,
            "rootCauseDenominator": count,
            "rootCauseAccuracy": _ratio(root_correct, count),
            "actionCorrect": action_correct,
            "actionDenominator": count,
            "actionAccuracy": _ratio(action_correct, count),
            "evidenceValid": evidence_valid if evidence_applicable else None,
            "evidenceDenominator": len(evidence_applicable),
            "evidenceValidityRate": _ratio(evidence_valid, len(evidence_applicable)),
            "toolCalls": tool_events,
            "toolCallErrors": tool_errors,
            "toolCallErrorRate": _ratio(tool_errors, tool_events),
            "medianElapsedMs": statistics.median(elapsed) if elapsed else None,
            "elapsedDenominator": len(elapsed),
            "medianTotalTokens": statistics.median(tokens) if tokens else None,
            "tokenDenominator": len(tokens),
            "modelCalls": sum(model_call_counts),
            "modelCallDenominator": len(model_call_counts),
            "networkErrors": network_error_count,
            "recoveredFaults": recovered if approval_enabled else None,
            "recoveryDenominator": len(fault_rows) if approval_enabled else 0,
            "recoveryRate": _ratio(recovered, len(fault_rows)) if approval_enabled else None,
        },
    }


def _output_dir(value):
    path = Path(value).resolve()
    try:
        path.relative_to(EVALUATION_DIR.resolve())
    except ValueError:
        raise ValueError("output must stay under agent/evaluation") from None
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate the local config-doctor lab.")
    parser.add_argument("--limit", type=int, help="run the first N rows; the dataset remains 40 rows")
    parser.add_argument("--baseline", action="store_true", help="symptom-only structured answer with no tools or Java calls")
    parser.add_argument("--approve-matching", action="store_true", help="operator-approve only proposals matching the evaluator label")
    parser.add_argument("--output-dir", help="output folder under agent/evaluation; defaults to a unique results folder")
    args = parser.parse_args(argv)

    records, dataset_hash = load_dataset()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    selected = records[: args.limit] if args.limit is not None else records
    run_id = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    default_output = EVALUATION_DIR / "results" / run_id
    try:
        output_dir = _output_dir(args.output_dir or default_output)
    except ValueError as error:
        parser.error(str(error))
    trace_dir = output_dir / "traces"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.baseline:
        if args.approve_matching:
            parser.error("--approve-matching is not applicable to the no-tool baseline")
        if not os.environ.get("DEEPSEEK_API_KEY"):
            parser.error("DEEPSEEK_API_KEY is required for model evaluation")
        mode = "baseline-no-tools"
        prompt_digest = prompt_hash(BASELINE_SYSTEM, None)
        rows = []
        for case in selected:
            provider = DeepSeekProvider(max_calls=1)
            try:
                rows.append(run_baseline_case(case, provider, trace_dir))
            except Exception as error:
                requested_id = getattr(provider, "model", None)
                rows.append({
                    "datasetId": case["id"],
                    "family": case["family"],
                    "expectedRootCause": case["expected"]["rootCause"],
                    "expectedAction": case["expected"]["action"],
                    "status": "FAILED_TO_RUN",
                    "error": f"{type(error).__name__}: {error}"[:500],
                    "modelId": None,
                    "requestedModelId": requested_id,
                    "actualModelIds": [],
                    "modelCalls": getattr(provider, "calls", None),
                    "elapsedMs": None,
                    "tokens": token_totals(getattr(provider, "usage", [])),
                    "rootCause": None,
                    "action": None,
                    "evidenceIds": [],
                    "evidence": [],
                    "evidenceValid": None,
                    "diagnosisCorrect": False,
                    "actionCorrect": False,
                    "toolCalls": 0,
                    "toolCallErrors": 0,
                    "networkErrors": getattr(provider, "network_errors", []),
                    "approval": None,
                    "recovered": None,
                })
    else:
        operator_token = os.environ.get("CONFIG_DOCTOR_OPERATOR_TOKEN")
        if not operator_token:
            parser.error("CONFIG_DOCTOR_OPERATOR_TOKEN is required for local case creation")
        if not os.environ.get("DEEPSEEK_API_KEY"):
            parser.error("DEEPSEEK_API_KEY is required for model evaluation")
        mode = "tool-agent"
        prompt_digest = prompt_hash(SYSTEM, TOOLS)
        base = os.environ.get("JAVA_BASE_URL", "http://127.0.0.1:18080")
        operator = JavaClient(token=operator_token, base=base)
        worker = JavaClient(base=base)
        rows = []
        for case in selected:
            provider = DeepSeekProvider(max_calls=6)
            try:
                row = run_agent_case(
                    case, run_id, operator, worker, provider, output_dir,
                    approve_matching=args.approve_matching,
                )
            except Exception as error:
                row = {
                    "datasetId": case["id"],
                    "family": case["family"],
                    "status": "FAILED_TO_START",
                    "error": f"{type(error).__name__}: {error}"[:500],
                    "modelId": None,
                    "requestedModelId": getattr(provider, "model", None),
                    "actualModelIds": model_identity(provider)["actualModelIds"],
                    "modelCalls": getattr(provider, "calls", None),
                    "elapsedMs": None,
                    "tokens": token_totals(getattr(provider, "usage", [])),
                    "rootCause": None,
                    "expectedRootCause": case["expected"]["rootCause"],
                    "action": None,
                    "expectedAction": case["expected"]["action"],
                    "evidenceIds": [],
                    "evidence": [],
                    "evidenceValid": False,
                    "diagnosisCorrect": False,
                    "actionCorrect": False,
                    "toolCalls": 0,
                    "toolCallErrors": 0,
                    "networkErrors": getattr(provider, "network_errors", []),
                    "approval": None,
                    "recovered": False if args.approve_matching and case["family"] != "HEALTHY" else None,
                }
            rows.append(row)

    summary = summarize(
        rows,
        mode=mode,
        approval_enabled=args.approve_matching and not args.baseline,
        dataset_hash=dataset_hash,
        prompt_digest=prompt_digest,
        run_id=run_id,
    )
    summary["approvalPolicy"] = (
        "exact-match-only" if args.approve_matching and not args.baseline else "disabled"
    )
    summary["limit"] = args.limit
    summary_path = output_dir / "summary.json"
    rows_path = output_dir / "results.jsonl"
    _write_json(summary_path, summary)
    rows_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(json.dumps({
        "summary": str(summary_path.relative_to(ROOT)),
        "results": str(rows_path.relative_to(ROOT)),
        "metrics": summary["metrics"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

