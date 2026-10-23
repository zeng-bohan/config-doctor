"""验证真实模型工具调用在 Java/H2 进程重启后的恢复能力。

只访问本脚本自己启动的 127.0.0.1:18082 服务。DeepSeek 凭据必须预先存在于
DEEPSEEK_API_KEY 环境变量；脚本不会读取密钥文件、打印密钥或将密钥写入结果。
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
EVIDENCE_DIR = PROJECT_ROOT.parent / "evidence"
JAR_PATH = PROJECT_ROOT / "backend" / "target" / "config-doctor-backend-0.1.0-SNAPSHOT.jar"
PORT = 18082
BASE_URL = f"http://127.0.0.1:{PORT}"
MAX_MODEL_REQUESTS = 4
MAX_OUTPUT_TOKENS = 800
PRIOR_MODEL_REQUESTS = int(os.environ.get("CONFIG_DOCTOR_RESTART_PRIOR_REQUESTS", "0"))
REMAINING_MODEL_REQUESTS = MAX_MODEL_REQUESTS - PRIOR_MODEL_REQUESTS
if REMAINING_MODEL_REQUESTS < 2 or REMAINING_MODEL_REQUESTS > MAX_MODEL_REQUESTS:
    raise RuntimeError("the remaining model request budget must allow both pre- and post-restart calls")
INITIAL_PROVIDER_REQUESTS = min(2, REMAINING_MODEL_REQUESTS - 1)
RESUME_PROVIDER_REQUESTS = REMAINING_MODEL_REQUESTS - INITIAL_PROVIDER_REQUESTS
LEASE_SECONDS = 30
H2_WRITE_DELAY_MS = 0
PRIOR_EVIDENCE_PATH = os.environ.get("CONFIG_DOCTOR_RESTART_PRIOR_EVIDENCE")
H2_WRITE_DELAY_DOCS = [
    "https://h2database.com/html/commands.html#SET_WRITE_DELAY",
    "https://h2database.com/html/advanced.html#durability_problems",
]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import worker  # noqa: E402
from agent.provider import DeepSeekProvider  # noqa: E402
from agent.worker import ApiError, JavaClient, run_task  # noqa: E402


def _java_executable() -> tuple[pathlib.Path, str]:
    java_home = os.environ.get("JAVA_HOME") or os.environ.get("JAVA21_HOME")
    if not java_home:
        raise RuntimeError("JAVA_HOME must point to the installed JDK 21")
    executable = pathlib.Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java")
    if not executable.is_file():
        raise RuntimeError("JAVA_HOME does not contain bin/java")
    check = subprocess.run([str(executable), "-version"], capture_output=True, text=True, timeout=10)
    version_text = (check.stdout or "") + (check.stderr or "")
    match = re.search(r'version\s+"(\d+)', version_text)
    if check.returncode != 0 or not match or match.group(1) != "21":
        raise RuntimeError("the configured JAVA_HOME is not a working JDK 21")
    return executable, version_text.strip()


def _port_is_available() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", PORT))
        except OSError:
            return False
    return True


def _http_health() -> dict:
    request = urllib.request.Request(BASE_URL + "/api/health", method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=2) as response:
        return json.load(response)


def _log_text(log_file, max_chars: int = 10000) -> str:
    log_file.flush()
    log_file.seek(0)
    data = log_file.read()
    if isinstance(data, bytes):
        text = data.decode("utf-8", errors="replace")
    else:
        text = data
    return text[-max_chars:]


def _start_java(java_executable: pathlib.Path, run_id: str, process_records: list[dict]) -> dict:
    if not _port_is_available():
        raise RuntimeError("127.0.0.1:18082 is already occupied; no existing process was changed")

    data_dir = PROJECT_ROOT / "data" / f"restart-{run_id}"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_url = f"jdbc:h2:file:./data/restart-{run_id}/config-doctor;MODE=PostgreSQL;DB_CLOSE_ON_EXIT=FALSE;WRITE_DELAY={H2_WRITE_DELAY_MS}"
    java_env = os.environ.copy()
    java_env.pop("DEEPSEEK_API_KEY", None)
    java_env["CONFIG_DOCTOR_DB_URL"] = db_url
    java_env["CONFIG_DOCTOR_VIEWER_TOKEN"] = "viewer-token"
    java_env["CONFIG_DOCTOR_WORKER_TOKEN"] = "worker-token"
    java_env["CONFIG_DOCTOR_OPERATOR_TOKEN"] = "operator-token"
    java_env["SERVER_ADDRESS"] = "127.0.0.1"
    java_env["SERVER_PORT"] = str(PORT)

    command = [
        str(java_executable), "-jar", str(JAR_PATH),
        "--server.address=127.0.0.1",
        f"--server.port={PORT}",
        f"--spring.datasource.url={db_url}",
        f"--config-doctor.lease-seconds={LEASE_SECONDS}",
    ]
    log_file = tempfile.TemporaryFile(mode="w+b")
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=java_env,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    record = {"pid": process.pid, "exitCode": None, "terminationMode": None, "logFile": log_file}
    process_records.append(record)
    instance = {"process": process, "logFile": log_file, "dataDir": data_dir, "dbUrl": db_url}

    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"the owned Java process exited during startup with code {process.returncode}")
            try:
                health = _http_health()
                if health.get("status") == "UP":
                    instance["health"] = health
                    return instance
            except Exception:
                pass
            time.sleep(0.2)
        raise RuntimeError("the owned Java process did not become healthy within 45 seconds")
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        record["terminationMode"] = "cleanup-owned-process-after-startup-failure"
        record["exitCode"] = process.poll()
        raise


def _stop_owned_java(instance: dict, process_records: list[dict], mode: str) -> None:
    process = instance["process"]
    record = next(item for item in process_records if item["pid"] == process.pid)
    if process.poll() is None:
        record["terminationMode"] = mode
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            record["terminationMode"] = mode + "-then-kill-owned-pid"
            process.kill()
            process.wait(timeout=10)
    record["exitCode"] = process.poll()


def _run_fixed_preflight(java_executable: pathlib.Path) -> dict:
    test_env = os.environ.copy()
    test_env.pop("DEEPSEEK_API_KEY", None)
    java_home = pathlib.Path(os.environ.get("JAVA_HOME") or os.environ["JAVA21_HOME"])
    test_env["JAVA_HOME"] = str(java_home)
    test_env["PATH"] = str(java_home / "bin") + os.pathsep + test_env.get("PATH", "")

    maven = shutil.which("mvn.cmd") or shutil.which("mvn")
    if not maven:
        fallback = pathlib.Path(r"C:\tools\maven\bin\mvn.cmd")
        if fallback.is_file():
            maven = str(fallback)
    if not maven:
        raise RuntimeError("Maven was not found; fixed preflight tests were not run")

    maven_command = f'"{maven}" -B -f backend/pom.xml test'
    maven_result = subprocess.run(
        maven_command,
        cwd=PROJECT_ROOT,
        env=test_env,
        shell=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    maven_output = (maven_result.stdout or "") + (maven_result.stderr or "")
    maven_tests = re.findall(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)", maven_output)

    python_result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "agent/tests", "-v"],
        cwd=PROJECT_ROOT,
        env=test_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    python_output = (python_result.stdout or "") + (python_result.stderr or "")
    python_match = re.search(r"Ran (\d+) tests? in [0-9.]+s", python_output)

    preflight = {
        "jdk": str(java_executable),
        "maven": {
            "command": "mvn -B -f backend/pom.xml test",
            "exitCode": maven_result.returncode,
            "suiteSummary": maven_tests[-1] if maven_tests else None,
            "outputTail": maven_output[-2500:],
        },
        "python": {
            "command": "python -m unittest discover -s agent/tests -v",
            "exitCode": python_result.returncode,
            "testsRun": int(python_match.group(1)) if python_match else None,
            "outputTail": python_output[-2500:],
        },
    }
    if maven_result.returncode != 0 or not maven_tests:
        raise RuntimeError("Java fixed preflight tests did not pass")
    if python_result.returncode != 0 or not python_match:
        raise RuntimeError("Python fixed preflight tests did not pass")
    return preflight


def _checkpoint_reads(task_id: str, client: JavaClient, run_token: str,
                      messages: list[dict], provider: DeepSeekProvider) -> tuple[list[dict], dict, list[dict], int]:
    evidence_by_tool: dict[str, dict] = {}
    phase_steps: list[dict] = []
    total_tool_calls = 0

    for step in range(2):
        message = provider.complete(messages, worker.TOOLS)
        calls = message.get("tool_calls") or []
        step_record = {"step": step + 1, "modelMessage": copy.deepcopy(message), "executedTools": []}
        if calls:
            if not isinstance(calls, list) or len(calls) > 4:
                raise RuntimeError("initial model response exceeded the read-tool call limit")
            messages.append({key: value for key, value in message.items()
                             if key in ("role", "content", "tool_calls")})
            for call in calls:
                total_tool_calls += 1
                call_id = call.get("id")
                name = call.get("function", {}).get("name", "")
                if not isinstance(call_id, str) or not call_id:
                    raise RuntimeError("model tool call did not include a valid call id")
                try:
                    arguments = json.loads(call["function"]["arguments"])
                    worker.validate_call(name, arguments, total_tool_calls)
                    if name in ("probe", "config", "logs"):
                        result = client.request(
                            "POST", f"/api/tasks/{task_id}/tools/{name}", {"runToken": run_token}
                        )
                        evidence_by_tool[name] = copy.deepcopy(result)
                        step_record["executedTools"].append({"name": name, "arguments": arguments, "result": result})
                    elif name == "submit_diagnosis":
                        result = {"error": "proposal submission is deferred until the resumed worker after restart"}
                        step_record["executedTools"].append({"name": name, "arguments": arguments,
                                                             "result": result, "deferred": True})
                    else:
                        raise ValueError("only read tools are permitted before the restart")
                except (ValueError, KeyError, TypeError, ApiError) as error:
                    result = {"error": str(error)}
                    step_record["executedTools"].append({"name": name, "result": result, "ok": False})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
        else:
            messages.append({"role": "assistant", "content": message.get("content") or ""})

        if not {"probe", "config"}.issubset(evidence_by_tool):
            messages.append({
                "role": "user",
                "content": "暂停提交诊断；请继续使用只读工具，至少运行真实 probe 并读取当前 config，再给出判断。",
            })
        checkpoint = client.request(
            "POST", f"/api/tasks/{task_id}/checkpoint", {"runToken": run_token, "messages": messages}
        )
        step_record["checkpoint"] = checkpoint
        step_record["messageCount"] = len(messages)
        phase_steps.append(step_record)
        if {"probe", "config"}.issubset(evidence_by_tool):
            return messages, checkpoint, phase_steps, total_tool_calls

    raise RuntimeError("initial model calls did not produce both probe and config evidence")


def _assert_no_secret(payload: str) -> None:
    secret = os.environ.get("DEEPSEEK_API_KEY", "")
    if secret and secret in payload:
        raise RuntimeError("evidence payload unexpectedly contains the provider credential; refusing to save")


def _summarize_prior_attempt(path: pathlib.Path) -> dict:
    prior = json.loads(path.read_text(encoding="utf-8"))
    before = prior.get("beforeRestart") or {}
    after_value = prior.get("afterRestartBeforeClaim")
    after = after_value or {}
    before_task = before.get("taskSnapshot") or {}
    jdbc_url = (prior.get("database") or {}).get("jdbcUrl")
    write_delay = re.search(r"(?:^|;)WRITE_DELAY=(\d+)(?:;|$)", jdbc_url or "", re.IGNORECASE)
    request_count = prior.get("modelRequestCount")
    if request_count is None:
        request_count = before.get("providerHttpAttempts")
    if request_count is None:
        request_count = (prior.get("initialProvider") or {}).get("httpAttempts")
    if request_count is None:
        request_count = (prior.get("resume") or {}).get("providerHttpAttempts")
    return {
        "evidencePath": str(path),
        "runId": prior.get("runId"),
        "outcome": prior.get("outcome"),
        "failure": (prior.get("error") or {}).get("message"),
        "jdbcUrl": jdbc_url,
        "writeDelaySetting": (
            f"{write_delay.group(1)} ms explicitly configured" if write_delay else
            "unspecified in URL; H2 default is 500 ms"
        ),
        "requests": request_count,
        "messagesBeforeRestart": len(before.get("messages") or []),
        "evidenceBeforeRestart": len(before_task.get("evidence") or []),
        "afterRestartSnapshotRecorded": isinstance(after_value, dict),
        "messagesAfterRestart": len(after.get("messages") or []) if isinstance(after_value, dict) else None,
        "evidenceAfterRestart": len(after.get("evidence") or []) if isinstance(after_value, dict) else None,
        "checkpointEventAfterRestart": any(
            item.get("event") == "CHECKPOINT_SAVED" for item in after.get("events") or []
        ) if isinstance(after_value, dict) else None,
    }


def _prior_attempt_comparison() -> list[dict]:
    if not PRIOR_EVIDENCE_PATH:
        return []
    paths = [pathlib.Path(item) for item in PRIOR_EVIDENCE_PATH.split("|") if item]
    return [_summarize_prior_attempt(path) for path in paths]


def main() -> int:
    run_id = str(uuid.uuid4())
    evidence_path = EVIDENCE_DIR / f"config-restart-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{run_id[:8]}.json"
    result: dict = {
        "schemaVersion": 1,
        "runId": run_id,
        "startedAtUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "outcome": "incomplete",
        "scope": {"host": "127.0.0.1", "port": PORT, "existingPortsTouched": [],
                  "maxModelHttpRequests": MAX_MODEL_REQUESTS,
                  "priorAttemptsAlreadyUsed": PRIOR_MODEL_REQUESTS,
                  "remainingBudgetForThisRun": REMAINING_MODEL_REQUESTS,
                  "maxOutputTokensPerRequest": MAX_OUTPUT_TOKENS,
                  "credentialSource": "DEEPSEEK_API_KEY process environment only",
                  "credentialWritten": False},
        "priorAttempts": [],
    }
    process_instances: list[dict] = []
    process_records: list[dict] = []
    current_instance = None

    try:
        result["priorAttempts"] = _prior_attempt_comparison()
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise RuntimeError("DEEPSEEK_API_KEY is not present in the process environment")
        if not JAR_PATH.is_file():
            raise RuntimeError("the required existing backend/target jar was not found")
        java_executable, java_version = _java_executable()
        result["environment"] = {
            "javaExecutable": str(java_executable),
            "javaVersion": java_version,
            "jar": str(JAR_PATH.relative_to(PROJECT_ROOT)),
            "jarSha256": hashlib.sha256(JAR_PATH.read_bytes()).hexdigest(),
            "jarModifiedAt": dt.datetime.fromtimestamp(JAR_PATH.stat().st_mtime, dt.timezone.utc).isoformat(),
            "pythonVersion": sys.version.split()[0],
            "javaBaseUrl": BASE_URL,
            "leaseSeconds": LEASE_SECONDS,
        }

        preflight = _run_fixed_preflight(java_executable)
        result["preflight"] = preflight

        current_instance = _start_java(java_executable, run_id, process_records)
        process_instances.append(current_instance)
        result["firstStart"] = {"pid": current_instance["process"].pid, "health": current_instance["health"]}
        result["database"] = {
            "directory": str((PROJECT_ROOT / "data" / f"restart-{run_id}").relative_to(PROJECT_ROOT)),
            "jdbcUrl": current_instance["dbUrl"],
            "writeDelayMs": H2_WRITE_DELAY_MS,
            "h2WriteDelayDocs": H2_WRITE_DELAY_DOCS,
            "leftInPlace": True,
        }

        operator = JavaClient(token="operator-token", base=BASE_URL)
        worker_client = JavaClient(token="worker-token", base=BASE_URL)
        case_id = f"restart-{run_id}"
        symptom = "用户反馈结算金额与商品当前金额对不上，请查询运行证据后定位原因，不要预设答案。"
        config = {
            "upstreamDelayMs": 10,
            "timeoutMs": 200,
            "sourcePrice": 25.75,
            "cachePrice": 19.50,
            "capacity": 10,
            "taxRate": 0.06,
        }
        created_case = operator.request("POST", "/api/cases", {
            "id": case_id,
            "config": config,
            "symptom": symptom,
        })
        task = worker_client.request("POST", "/api/tasks", {
            "caseId": case_id,
            "symptom": symptom,
            "idempotencyKey": f"restart-task-{run_id}",
        })
        task_id = task["id"]
        result["case"] = {"request": {"id": case_id, "config": config, "symptom": symptom},
                          "response": created_case}
        result["task"] = {"id": task_id, "created": task}

        claim = worker_client.request("POST", f"/api/tasks/{task_id}/claim", {})
        old_run_token = claim["runToken"]
        messages = [
            {"role": "system", "content": worker.SYSTEM},
            {"role": "user", "content": symptom},
        ]
        result["firstClaim"] = {key: value for key, value in claim.items() if key != "runToken"}
        result["firstClaim"]["runTokenSha256"] = hashlib.sha256(old_run_token.encode()).hexdigest()

        first_provider = DeepSeekProvider(max_calls=INITIAL_PROVIDER_REQUESTS)
        try:
            messages, checkpoint, first_steps, initial_tool_calls = _checkpoint_reads(
                task_id, worker_client, old_run_token, messages, first_provider
            )
        except Exception:
            result["initialProvider"] = {
                "model": first_provider.model,
                "httpAttempts": first_provider.calls,
                "responses": first_provider.responses,
                "usage": first_provider.usage,
                "networkErrors": first_provider.network_errors,
                "messagesAtFailure": messages,
            }
            raise

        successful_read_tools = [
            tool for step in first_steps for tool in step.get("executedTools", [])
            if tool.get("name") in ("probe", "config", "logs") and tool.get("result", {}).get("tool")
        ]
        first_tools = {tool.get("name") for tool in successful_read_tools}
        if not {"probe", "config"}.issubset(first_tools):
            raise RuntimeError("the initial real-model phase did not execute both probe and config tools")
        last_lease_until = int(checkpoint["leaseUntil"])
        result["beforeRestart"] = {
            "model": first_provider.model,
            "providerHttpAttempts": first_provider.calls,
            "responses": first_provider.responses,
            "usage": first_provider.usage,
            "networkErrors": first_provider.network_errors,
            "steps": first_steps,
            "toolCalls": initial_tool_calls,
            "messages": copy.deepcopy(messages),
            "checkpoint": checkpoint,
            "evidenceTools": sorted(first_tools),
            "expectedEvidenceCount": len(successful_read_tools),
            "terminationAfterCheckpointWaitMs": 0,
        }

        _stop_owned_java(current_instance, process_records, "terminate-owned-java-for-restart")
        db_file = PROJECT_ROOT / "data" / f"restart-{run_id}" / "config-doctor.mv.db"
        result["database"]["fileImmediatelyAfterTermination"] = {
            "exists": db_file.is_file(),
            "sizeBytes": db_file.stat().st_size if db_file.is_file() else None,
        }
        if not db_file.is_file() or db_file.stat().st_size == 0:
            raise RuntimeError("the isolated H2 database file was not created")
        result["interruption"] = {
            "pid": current_instance["process"].pid,
            "exitCode": current_instance["process"].poll(),
            "method": "Popen.terminate on the process created by this script (Windows TerminateProcess)",
            "afterCheckpointWaitMs": 0,
            "lastLeaseUntilEpochMs": last_lease_until,
        }
        current_instance = None

        current_instance = _start_java(java_executable, run_id, process_records)
        process_instances.append(current_instance)
        result["secondStart"] = {"pid": current_instance["process"].pid, "health": current_instance["health"],
                                  "sameDatabaseUrl": current_instance["dbUrl"] == result["database"]["jdbcUrl"]}
        if current_instance["dbUrl"] != result["database"]["jdbcUrl"]:
            raise RuntimeError("the restarted Java process did not use the original database URL")

        resumed_client = JavaClient(token="worker-token", base=BASE_URL)
        after_restart = resumed_client.request("GET", f"/api/tasks/{task_id}")
        result["afterRestartBeforeClaim"] = after_restart
        restored_evidence = after_restart.get("evidence") or []
        restored_checkpoint_event = any(
            item.get("event") == "CHECKPOINT_SAVED" for item in after_restart.get("events") or []
        )
        result["afterRestartValidation"] = {
            "restoredMessageCount": len(after_restart.get("messages") or []),
            "expectedMessageCount": len(messages),
            "restoredEvidenceCount": len(restored_evidence),
            "expectedEvidenceCount": len(successful_read_tools),
            "checkpointSavedEventPresent": restored_checkpoint_event,
        }
        if after_restart.get("messages") != messages:
            raise RuntimeError("saved messages did not survive Java/H2 process restart")
        if len(restored_evidence) < len(successful_read_tools):
            raise RuntimeError("saved evidence did not survive Java/H2 process restart")
        if not restored_checkpoint_event:
            raise RuntimeError("the CHECKPOINT_SAVED audit event did not survive Java/H2 process restart")

        remaining_lease_ms = last_lease_until - int(time.time() * 1000)
        if remaining_lease_ms > 0:
            time.sleep((remaining_lease_ms + 150) / 1000)
        stale_result = {"expectedStatus": 409, "oldRunTokenSha256": result["firstClaim"]["runTokenSha256"]}
        try:
            resumed_client.request("POST", f"/api/tasks/{task_id}/checkpoint", {
                "runToken": old_run_token,
                "messages": messages,
            })
            stale_result["rejected"] = False
            stale_result["actualStatus"] = 200
        except ApiError as error:
            stale_result["rejected"] = error.status == 409
            stale_result["actualStatus"] = error.status
            stale_result["error"] = str(error)
        result["oldRunTokenCheck"] = stale_result
        if not stale_result["rejected"]:
            raise RuntimeError("the expired pre-restart runToken was not rejected with HTTP 409")

        resume_provider = DeepSeekProvider(max_calls=RESUME_PROVIDER_REQUESTS)
        with tempfile.TemporaryDirectory(prefix="config-doctor-restart-traces-") as trace_dir:
            resume_trace = run_task(
                task_id,
                provider=resume_provider,
                client=resumed_client,
                output_dir=trace_dir,
                max_rounds=RESUME_PROVIDER_REQUESTS,
            )
            trace_path = pathlib.Path(trace_dir) / f"{task_id}.json"
            if trace_path.is_file():
                resume_trace = json.loads(trace_path.read_text(encoding="utf-8"))
        result["resume"] = {
            "model": resume_provider.model,
            "providerHttpAttempts": resume_provider.calls,
            "responses": resume_provider.responses,
            "usage": resume_provider.usage,
            "networkErrors": resume_provider.network_errors,
            "trace": resume_trace,
            "temporaryTraceRemoved": True,
        }

        total_model_requests = first_provider.calls + resume_provider.calls
        cumulative_model_requests = PRIOR_MODEL_REQUESTS + total_model_requests
        result["modelRequestCount"] = total_model_requests
        result["cumulativeModelRequestCount"] = cumulative_model_requests
        if total_model_requests > REMAINING_MODEL_REQUESTS or cumulative_model_requests > MAX_MODEL_REQUESTS:
            raise RuntimeError("the total DeepSeek HTTP request limit of four was exceeded")
        if any(item.get("usage", {}).get("completion_tokens", 0) > MAX_OUTPUT_TOKENS
               for item in first_provider.responses + resume_provider.responses):
            raise RuntimeError("a provider response exceeded the configured 800 output-token limit")
        if resume_trace.get("status") != "AWAITING_APPROVAL":
            raise RuntimeError(f"resumed real worker did not produce a pending proposal: {resume_trace.get('status')}")

        pending_task = resumed_client.request("GET", f"/api/tasks/{task_id}")
        proposal = pending_task.get("proposal") or {}
        if proposal.get("rootCause") != "CACHE_DRIFT" or proposal.get("action") != "REFRESH_CACHE":
            raise RuntimeError("the resumed proposal did not match the synthetic CACHE_DRIFT evidence")
        proposal_evidence = set(proposal.get("evidenceIds") or [])
        persisted_evidence = {item.get("id") for item in pending_task.get("evidence") or []}
        if not proposal_evidence or not proposal_evidence.issubset(persisted_evidence):
            raise RuntimeError("the resumed proposal did not cite persisted task evidence")
        claim_events = [event for event in pending_task.get("events", [])
                        if event.get("event") == "TASK_CLAIMED"]
        if len(claim_events) < 2:
            raise RuntimeError("the resumed worker did not claim a fresh post-restart lease")
        result["resume"]["claimEventCount"] = len(claim_events)
        result["pendingProposal"] = pending_task

        approval_client = JavaClient(token="operator-token", base=BASE_URL)
        approval = approval_client.request("POST", f"/api/tasks/{task_id}/approve", {
            "idempotencyKey": f"restart-approval-{run_id}",
        })
        result["approval"] = approval
        final_task = approval_client.request("GET", f"/api/tasks/{task_id}")
        result["finalTask"] = final_task
        if approval.get("status") != "COMPLETED" or not approval.get("verification", {}).get("passed"):
            raise RuntimeError("operator approval did not complete the actual post-repair contract probe")
        if final_task.get("status") != "COMPLETED":
            raise RuntimeError("the task did not reach COMPLETED after approval")

        result["database"]["fileAfterApproval"] = {
            "exists": db_file.is_file(),
            "sizeBytes": db_file.stat().st_size if db_file.is_file() else None,
        }
        result["outcome"] = "passed"

    except Exception as error:
        result["outcome"] = "failed"
        result["error"] = {"type": type(error).__name__, "message": str(error)[:2000]}
    finally:
        if current_instance is not None:
            try:
                _stop_owned_java(current_instance, process_records, "final-stop-owned-java-process")
            except Exception as stop_error:
                result["cleanupError"] = {"type": type(stop_error).__name__, "message": str(stop_error)[:1000]}
        result["javaProcesses"] = [
            {key: value for key, value in record.items() if key != "logFile"}
            | {"logTail": _log_text(record["logFile"])}
            for record in process_records
        ]
        for record in process_records:
            try:
                record["logFile"].close()
            except Exception:
                pass
        result["finishedAtUtc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(result, ensure_ascii=False, indent=2)
        try:
            _assert_no_secret(encoded)
        except RuntimeError:
            result = {
                "schemaVersion": 1,
                "runId": run_id,
                "outcome": "failed",
                "error": {"type": "CredentialRedaction", "message": "evidence contained the provider secret and was replaced"},
                "finishedAtUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            encoded = json.dumps(result, ensure_ascii=False, indent=2)
        evidence_path.write_text(encoded + "\n", encoding="utf-8")

    summary = {
        "outcome": result.get("outcome"),
        "evidence": str(evidence_path),
        "taskId": (result.get("task") or {}).get("id"),
        "modelRequestCount": result.get("modelRequestCount"),
        "javaPids": [item.get("pid") for item in result.get("javaProcesses", [])],
        "error": result.get("error"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.get("outcome") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
