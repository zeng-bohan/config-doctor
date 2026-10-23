package com.zengbohan.configdoctor.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zengbohan.configdoctor.api.ApiException;
import com.zengbohan.configdoctor.api.ApiModels;
import com.zengbohan.configdoctor.api.ApiModels.Action;
import com.zengbohan.configdoctor.api.ApiModels.RootCause;
import com.zengbohan.configdoctor.domain.LabConfig;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.dao.EmptyResultDataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

@Service
public class ConfigDoctorService {
    private final JdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final SettlementLab settlementLab;
    private final long leaseMillis;

    public ConfigDoctorService(JdbcTemplate jdbc, ObjectMapper mapper, SettlementLab settlementLab,
                               @Value("${config-doctor.lease-seconds:30}") long leaseSeconds) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.settlementLab = settlementLab;
        this.leaseMillis = Math.max(1, leaseSeconds) * 1000L;
    }

    @Transactional
    public Map<String, Object> createCase(ApiModels.CaseCreateRequest request) {
        LabConfig config = request.config().toConfig();
        long now = now();
        try {
            jdbc.update("INSERT INTO lab_cases(id,symptom,config_json,config_version,created_at_ms) VALUES(?,?,?,?,?)",
                    request.id(), request.symptom(), json(config), 1L, now);
        } catch (DuplicateKeyException e) {
            throw ApiException.conflict("case id already exists");
        }
        audit(null, request.id(), "CASE_CREATED", Map.of("configVersion", 1));
        return caseView(caseById(request.id()));
    }

    @Transactional(readOnly = true)
    public List<Map<String, Object>> listCases() {
        return jdbc.query("SELECT id,symptom,config_json,config_version,created_at_ms FROM lab_cases ORDER BY created_at_ms DESC",
                (rs, row) -> caseView(caseRow(rs)));
    }

    @Transactional
    public Map<String, Object> updateConfig(String caseId, ApiModels.CaseConfigUpdateRequest request) {
        LabConfig next = request.config().toConfig();
        long now = now();
        int changed = jdbc.update("UPDATE lab_cases SET config_json=?,config_version=config_version+1 WHERE id=? AND config_version=?",
                json(next), caseId, request.expectedVersion());
        if (changed == 0) {
            if (!existsCase(caseId)) throw ApiException.notFound("case not found");
            throw ApiException.conflict("configuration version is stale");
        }
        CaseRow updated = caseById(caseId);
        audit(null, caseId, "CONFIG_UPDATED", Map.of("configVersion", updated.configVersion));
        return caseView(updated);
    }

    @Transactional
    public Map<String, Object> createTask(ApiModels.TaskCreateRequest request) {
        if (!existsCase(request.caseId())) throw ApiException.notFound("case not found");
        String requestHash = sha256(request.caseId() + "\n" + request.symptom());
        Map<String, Object> existing = taskByIdempotencyKey(request.idempotencyKey());
        if (existing != null) return idempotentTask(existing, requestHash);

        String id = UUID.randomUUID().toString();
        long now = now();
        try {
            jdbc.update("INSERT INTO lab_tasks(id,case_id,symptom,status,version,idempotency_key,request_hash,run_token,lease_until_ms,messages_json,proposal_json,proposal_config_version,approval_result_json,failure_reason,created_at_ms,updated_at_ms) VALUES(?,?,?, 'CREATED',1,?,?,NULL,NULL,?,NULL,NULL,NULL,NULL,?,?)",
                    id, request.caseId(), request.symptom(), request.idempotencyKey(), requestHash, json(List.of()), now, now);
        } catch (DuplicateKeyException e) {
            Map<String, Object> raced = taskByIdempotencyKey(request.idempotencyKey());
            if (raced == null) throw ApiException.conflict("task idempotency key collision");
            return idempotentTask(raced, requestHash);
        }
        audit(id, request.caseId(), "TASK_CREATED", Map.of("status", "CREATED"));
        return getTask(id);
    }

    @Transactional(readOnly = true)
    public List<Map<String, Object>> listTasks() {
        List<String> ids = jdbc.query("SELECT id FROM lab_tasks ORDER BY created_at_ms DESC",
                (rs, row) -> rs.getString("id"));
        return ids.stream().map(this::taskSummary).toList();
    }

    @Transactional(readOnly = true)
    public Map<String, Object> getTask(String taskId) {
        TaskRow task = taskById(taskId);
        List<Map<String, Object>> events = jdbc.query(
                "SELECT event,payload_json,created_at_ms FROM lab_audit WHERE task_id=? ORDER BY id",
                (rs, row) -> Map.of("event", rs.getString("event"), "data", readMap(rs.getString("payload_json")),
                        "createdAt", rs.getLong("created_at_ms")), taskId);
        List<Map<String, Object>> evidence = evidenceRows(taskId).stream().map(this::evidenceView).toList();
        Map<String, Object> view = new LinkedHashMap<>();
        view.put("id", task.id);
        view.put("caseId", task.caseId);
        view.put("symptom", task.symptom);
        view.put("status", task.status);
        view.put("version", task.version);
        view.put("messages", readListOfMaps(task.messagesJson));
        view.put("proposal", task.proposalJson == null ? null : readMap(task.proposalJson));
        view.put("proposalConfigVersion", task.proposalConfigVersion);
        view.put("approvalResult", task.approvalResultJson == null ? null : readMap(task.approvalResultJson));
        view.put("failureReason", task.failureReason);
        view.put("events", events);
        view.put("evidence", evidence);
        view.put("createdAt", task.createdAt);
        view.put("updatedAt", task.updatedAt);
        return view;
    }

    @Transactional
    public Map<String, Object> claim(String taskId) {
        String token = UUID.randomUUID().toString();
        long now = now();
        long expires = now + leaseMillis;
        int changed = jdbc.update("UPDATE lab_tasks SET status='RUNNING',version=version+1,run_token=?,lease_until_ms=?,updated_at_ms=? WHERE id=? AND (status='CREATED' OR (status='RUNNING' AND lease_until_ms<?))",
                token, expires, now, taskId, now);
        if (changed == 0) {
            if (!existsTask(taskId)) throw ApiException.notFound("task not found");
            throw ApiException.conflict("task is not claimable or its worker lease is active");
        }
        TaskRow task = taskById(taskId);
        audit(taskId, task.caseId, "TASK_CLAIMED", Map.of("leaseUntil", expires, "version", task.version));
        return Map.of("taskId", taskId, "runToken", token, "leaseUntil", expires, "version", task.version);
    }

    @Transactional
    public Map<String, Object> checkpoint(String taskId, ApiModels.CheckpointRequest request) {
        TaskRow task = requireLiveWorker(taskId, request.runToken());
        long now = now();
        long expires = now + leaseMillis;
        int changed = jdbc.update("UPDATE lab_tasks SET messages_json=?,lease_until_ms=?,version=version+1,updated_at_ms=? WHERE id=? AND status='RUNNING' AND run_token=? AND lease_until_ms>=?",
                json(request.messages()), expires, now, taskId, request.runToken(), now);
        if (changed == 0) throw ApiException.conflict("worker lease expired");
        audit(taskId, task.caseId, "CHECKPOINT_SAVED", Map.of("messageCount", request.messages().size(), "leaseUntil", expires));
        return Map.of("taskId", taskId, "status", "RUNNING", "leaseUntil", expires, "version", task.version + 1);
    }

    @Transactional
    public Map<String, Object> runTool(String taskId, String tool, ApiModels.ToolRequest request) {
        TaskRow task = requireLiveWorker(taskId, request.runToken());
        CaseRow caseRow = caseById(task.caseId);
        LabConfig config = readConfig(caseRow.configJson);
        Map<String, Object> data;
        switch (tool) {
            case "probe" -> {
                data = new LinkedHashMap<>(settlementLab.probe(config));
                data.put("caseId", caseRow.id);
                data.put("configVersion", caseRow.configVersion);
                insertAudit(taskId, caseRow.id, "LAB_PROBE", data);
            }
            case "config" -> {
                data = new LinkedHashMap<>();
                data.put("config", config);
                data.put("configVersion", caseRow.configVersion);
            }
            case "logs" -> {
                List<Map<String, Object>> logs = jdbc.query(
                        "SELECT payload_json,created_at_ms FROM lab_audit WHERE case_id=? AND event='LAB_PROBE' ORDER BY id DESC LIMIT 10",
                        (rs, row) -> Map.of("data", readMap(rs.getString("payload_json")), "createdAt", rs.getLong("created_at_ms")), caseRow.id);
                data = new LinkedHashMap<>();
                data.put("entries", logs);
                data.put("configVersion", caseRow.configVersion);
            }
            default -> throw ApiException.notFound("unknown tool");
        }
        String evidenceId = UUID.randomUUID().toString();
        long createdAt = now();
        jdbc.update("INSERT INTO lab_evidence(id,task_id,tool,data_json,config_version,created_at_ms) VALUES(?,?,?,?,?,?)",
                evidenceId, taskId, tool, json(data), caseRow.configVersion, createdAt);
        audit(taskId, caseRow.id, "EVIDENCE_CAPTURED", Map.of("evidenceId", evidenceId, "tool", tool, "configVersion", caseRow.configVersion));
        return Map.of("id", evidenceId, "tool", tool, "data", data);
    }

    @Transactional
    public Map<String, Object> submitProposal(String taskId, ApiModels.ProposalRequest request) {
        TaskRow task = requireLiveWorker(taskId, request.runToken());
        Set<String> requestedIds = new HashSet<>(request.evidenceIds());
        if (requestedIds.size() != request.evidenceIds().size()) throw ApiException.unprocessable("evidenceIds must be unique");
        List<EvidenceRow> evidence = evidenceRows(taskId).stream().filter(e -> requestedIds.contains(e.id)).toList();
        if (evidence.size() != requestedIds.size()) throw ApiException.unprocessable("evidence must belong to this task");

        EvidenceRow probe = evidence.stream().filter(e -> "probe".equals(e.tool)).findFirst()
                .orElseThrow(() -> ApiException.unprocessable("proposal requires probe evidence"));
        JsonNode probeData = readTree(probe.dataJson);
        CaseRow currentCase = caseByIdForUpdate(task.caseId);
        if (currentCase.configVersion != probe.configVersion) {
            throw ApiException.conflict("probe evidence uses a stale configuration version");
        }
        Action expectedAction = switch (request.rootCause()) {
            case TIMEOUT -> Action.RESTORE_TIMEOUT;
            case CACHE_DRIFT -> Action.REFRESH_CACHE;
            case CAPACITY -> Action.RESTORE_CAPACITY;
            case TAX -> Action.RESTORE_TAX;
            case HEALTHY -> Action.NONE;
        };
        if (request.action() != expectedAction) throw ApiException.unprocessable("action does not match rootCause");
        Map<String, Object> patch = Map.of();
        if (request.rootCause() != RootCause.HEALTHY) {
            EvidenceRow config = evidence.stream().filter(e -> "config".equals(e.tool)).findFirst()
                    .orElseThrow(() -> ApiException.unprocessable("fault proposals require config evidence"));
            if (config.configVersion != probe.configVersion) throw ApiException.unprocessable("probe and config evidence versions differ");
            JsonNode configData = readTree(config.dataJson).path("config");
            LabConfig observedConfig = mapper.convertValue(configData, LabConfig.class).validated();
            patch = patchView(observedConfig, request.action());
        } else if (!probeData.path("passed").asBoolean(false)) {
            throw ApiException.unprocessable("healthy proposal requires a passing probe");
        }

        Map<String, Object> proposal = new LinkedHashMap<>();
        proposal.put("rootCause", request.rootCause());
        proposal.put("action", request.action());
        proposal.put("evidenceIds", request.evidenceIds());
        proposal.put("summary", request.summary());
        proposal.put("configVersion", probe.configVersion);
        proposal.put("patch", patch);
        String status = request.rootCause() == RootCause.HEALTHY ? "COMPLETED" : "AWAITING_APPROVAL";
        long now = now();
        int changed = jdbc.update("UPDATE lab_tasks SET status=?,proposal_json=?,proposal_config_version=?,run_token=NULL,lease_until_ms=NULL,version=version+1,updated_at_ms=? WHERE id=? AND status='RUNNING' AND run_token=? AND lease_until_ms>=?",
                status, json(proposal), probe.configVersion, now, taskId, request.runToken(), now);
        if (changed == 0) throw ApiException.conflict("worker lease expired");
        audit(taskId, task.caseId, "PROPOSAL_SUBMITTED", Map.of("rootCause", request.rootCause(), "action", request.action(), "status", status));
        return getTask(taskId);
    }

    @Transactional
    public Map<String, Object> failTask(String taskId, ApiModels.FailRequest request) {
        TaskRow task = requireLiveWorker(taskId, request.runToken());
        long now = now();
        int changed = jdbc.update("UPDATE lab_tasks SET status='FAILED',failure_reason=?,run_token=NULL,lease_until_ms=NULL,version=version+1,updated_at_ms=? WHERE id=? AND status='RUNNING' AND run_token=? AND lease_until_ms>=?",
                request.reason(), now, taskId, request.runToken(), now);
        if (changed == 0) throw ApiException.conflict("worker lease expired");
        audit(taskId, task.caseId, "TASK_FAILED", Map.of("reason", request.reason()));
        return getTask(taskId);
    }

    @Transactional
    public Map<String, Object> approve(String taskId, ApiModels.ApprovalRequest request) {
        Map<String, Object> prior = approvalByKey(request.idempotencyKey());
        if (prior != null) {
            if (!taskId.equals(prior.get("taskId"))) throw ApiException.conflict("approval idempotency key belongs to another task");
            return readMap((String) prior.get("resultJson"));
        }

        TaskRow task = taskById(taskId);
        Map<String, Object> completedForTask = approvalByTask(taskId);
        if (completedForTask != null) return readMap((String) completedForTask.get("resultJson"));
        if (!"AWAITING_APPROVAL".equals(task.status) || task.proposalJson == null) {
            throw ApiException.conflict("task has no pending proposal");
        }
        Map<String, Object> proposal = readMap(task.proposalJson);
        CaseRow currentCase = caseById(task.caseId);
        if (currentCase.configVersion != task.proposalConfigVersion) {
            throw ApiException.conflict("proposal configuration version is stale");
        }
        LabConfig before = readConfig(currentCase.configJson);
        LabConfig after = applyApprovedPatch(before, Action.valueOf(proposal.get("action").toString()));
        long now = now();
        int updated = jdbc.update("UPDATE lab_cases SET config_json=?,config_version=config_version+1 WHERE id=? AND config_version=?",
                json(after), task.caseId, task.proposalConfigVersion);
        if (updated == 0) {
            Map<String, Object> raced = approvalByKey(request.idempotencyKey());
            if (raced != null && taskId.equals(raced.get("taskId"))) return readMap((String) raced.get("resultJson"));
            throw ApiException.conflict("proposal configuration version is stale");
        }
        long newConfigVersion = task.proposalConfigVersion + 1;
        Map<String, Object> verification = settlementLab.probe(after);
        verification.put("caseId", task.caseId);
        verification.put("configVersion", newConfigVersion);
        String finalStatus = Boolean.TRUE.equals(verification.get("passed")) ? "COMPLETED" : "FAILED";
        String evidenceId = UUID.randomUUID().toString();
        jdbc.update("INSERT INTO lab_evidence(id,task_id,tool,data_json,config_version,created_at_ms) VALUES(?,?,?,?,?,?)",
                evidenceId, taskId, "approval-reprobe", json(verification), newConfigVersion, now);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("taskId", taskId);
        result.put("status", finalStatus);
        result.put("configVersion", newConfigVersion);
        result.put("verification", verification);
        result.put("evidenceId", evidenceId);
        result.put("idempotentReplay", false);
        String resultJson = json(result);
        jdbc.update("UPDATE lab_tasks SET status=?,approval_result_json=?,version=version+1,updated_at_ms=? WHERE id=? AND status='AWAITING_APPROVAL'",
                finalStatus, resultJson, now, taskId);
        jdbc.update("INSERT INTO lab_approvals(idempotency_key,task_id,result_json,created_at_ms) VALUES(?,?,?,?)",
                request.idempotencyKey(), taskId, resultJson, now);
        audit(taskId, task.caseId, "APPROVAL_APPLIED", Map.of("action", proposal.get("action"), "configVersion", newConfigVersion, "status", finalStatus));
        audit(taskId, task.caseId, "LAB_PROBE", verification);
        return result;
    }

    private LabConfig applyApprovedPatch(LabConfig config, Action action) {
        return switch (action) {
            case RESTORE_TIMEOUT -> config.withTimeout(config.upstreamDelayMs() + 50);
            case REFRESH_CACHE -> config.withCachePrice(config.sourcePrice());
            case RESTORE_CAPACITY -> config.withCapacity(LabConfig.DEFAULT_CAPACITY);
            case RESTORE_TAX -> config.withTaxRate(LabConfig.DEFAULT_TAX_RATE);
            case NONE -> config;
        };
    }

    private Map<String, Object> patchView(LabConfig before, Action action) {
        Map<String, Object> patch = new LinkedHashMap<>();
        switch (action) {
            case RESTORE_TIMEOUT -> patch.put("timeoutMs", Map.of("before", before.timeoutMs(), "after", before.upstreamDelayMs() + 50));
            case REFRESH_CACHE -> patch.put("cachePrice", Map.of("before", before.cachePrice(), "after", before.sourcePrice()));
            case RESTORE_CAPACITY -> patch.put("capacity", Map.of("before", before.capacity(), "after", LabConfig.DEFAULT_CAPACITY));
            case RESTORE_TAX -> patch.put("taxRate", Map.of("before", before.taxRate(), "after", LabConfig.DEFAULT_TAX_RATE));
            case NONE -> { }
        }
        return patch;
    }

    private Map<String, Object> idempotentTask(Map<String, Object> existing, String requestHash) {
        if (!requestHash.equals(existing.get("requestHash"))) throw ApiException.conflict("idempotency key was reused with a different request");
        return getTask((String) existing.get("id"));
    }

    private TaskRow requireLiveWorker(String taskId, String token) {
        TaskRow task = taskById(taskId);
        if (!"RUNNING".equals(task.status) || task.runToken == null || !task.runToken.equals(token)
                || task.leaseUntil == null || task.leaseUntil < now()) {
            throw ApiException.conflict("worker lease is missing, expired, or owned by another worker");
        }
        return task;
    }

    private Map<String, Object> caseView(CaseRow row) {
        Map<String, Object> view = new LinkedHashMap<>();
        view.put("id", row.id);
        view.put("symptom", row.symptom);
        view.put("config", readConfig(row.configJson));
        view.put("configVersion", row.configVersion);
        view.put("createdAt", row.createdAt);
        return view;
    }

    private Map<String, Object> taskSummary(String taskId) {
        TaskRow task = taskById(taskId);
        return Map.of("id", task.id, "caseId", task.caseId, "status", task.status,
                "version", task.version, "createdAt", task.createdAt, "updatedAt", task.updatedAt);
    }

    private Map<String, Object> evidenceView(EvidenceRow e) {
        return Map.of("id", e.id, "taskId", e.taskId, "tool", e.tool,
                "data", readMap(e.dataJson), "configVersion", e.configVersion, "createdAt", e.createdAt);
    }

    private CaseRow caseById(String id) {
        try {
            return jdbc.queryForObject("SELECT id,symptom,config_json,config_version,created_at_ms FROM lab_cases WHERE id=?",
                    (rs, row) -> caseRow(rs), id);
        } catch (EmptyResultDataAccessException e) {
            throw ApiException.notFound("case not found");
        }
    }

    private CaseRow caseByIdForUpdate(String id) {
        try {
            return jdbc.queryForObject("SELECT id,symptom,config_json,config_version,created_at_ms FROM lab_cases WHERE id=? FOR UPDATE",
                    (rs, row) -> caseRow(rs), id);
        } catch (EmptyResultDataAccessException e) {
            throw ApiException.notFound("case not found");
        }
    }

    private CaseRow caseRow(ResultSet rs) throws SQLException {
        return new CaseRow(rs.getString("id"), rs.getString("symptom"), rs.getString("config_json"),
                rs.getLong("config_version"), rs.getLong("created_at_ms"));
    }

    private TaskRow taskById(String id) {
        try {
            return jdbc.queryForObject("SELECT id,case_id,symptom,status,version,request_hash,run_token,lease_until_ms,messages_json,proposal_json,proposal_config_version,approval_result_json,failure_reason,created_at_ms,updated_at_ms FROM lab_tasks WHERE id=?",
                    (rs, row) -> taskRow(rs), id);
        } catch (EmptyResultDataAccessException e) {
            throw ApiException.notFound("task not found");
        }
    }

    private TaskRow taskRow(ResultSet rs) throws SQLException {
        long lease = rs.getLong("lease_until_ms");
        Long leaseUntil = rs.wasNull() ? null : lease;
        long proposalVersion = rs.getLong("proposal_config_version");
        Long proposalConfigVersion = rs.wasNull() ? null : proposalVersion;
        return new TaskRow(rs.getString("id"), rs.getString("case_id"), rs.getString("symptom"), rs.getString("status"), rs.getLong("version"),
                rs.getString("request_hash"), rs.getString("run_token"), leaseUntil, rs.getString("messages_json"),
                rs.getString("proposal_json"), proposalConfigVersion, rs.getString("approval_result_json"), rs.getString("failure_reason"),
                rs.getLong("created_at_ms"), rs.getLong("updated_at_ms"));
    }

    private List<EvidenceRow> evidenceRows(String taskId) {
        return jdbc.query("SELECT id,task_id,tool,data_json,config_version,created_at_ms FROM lab_evidence WHERE task_id=? ORDER BY created_at_ms,id",
                (rs, row) -> new EvidenceRow(rs.getString("id"), rs.getString("task_id"), rs.getString("tool"),
                        rs.getString("data_json"), rs.getLong("config_version"), rs.getLong("created_at_ms")), taskId);
    }

    private Map<String, Object> taskByIdempotencyKey(String key) {
        try {
            return jdbc.queryForObject("SELECT id,request_hash FROM lab_tasks WHERE idempotency_key=?",
                    (rs, row) -> Map.of("id", rs.getString("id"), "requestHash", rs.getString("request_hash")), key);
        } catch (EmptyResultDataAccessException e) { return null; }
    }

    private Map<String, Object> approvalByKey(String key) {
        try {
            return jdbc.queryForObject("SELECT task_id,result_json FROM lab_approvals WHERE idempotency_key=?",
                    (rs, row) -> Map.of("taskId", rs.getString("task_id"), "resultJson", rs.getString("result_json")), key);
        } catch (EmptyResultDataAccessException e) { return null; }
    }

    private Map<String, Object> approvalByTask(String taskId) {
        try {
            return jdbc.queryForObject("SELECT idempotency_key,task_id,result_json FROM lab_approvals WHERE task_id=?",
                    (rs, row) -> Map.of("idempotencyKey", rs.getString("idempotency_key"), "taskId", rs.getString("task_id"), "resultJson", rs.getString("result_json")), taskId);
        } catch (EmptyResultDataAccessException e) { return null; }
    }

    private boolean existsCase(String id) {
        return Boolean.TRUE.equals(jdbc.queryForObject("SELECT EXISTS(SELECT 1 FROM lab_cases WHERE id=?)", Boolean.class, id));
    }

    private boolean existsTask(String id) {
        return Boolean.TRUE.equals(jdbc.queryForObject("SELECT EXISTS(SELECT 1 FROM lab_tasks WHERE id=?)", Boolean.class, id));
    }

    private void audit(String taskId, String caseId, String event, Object payload) {
        insertAudit(taskId, caseId, event, payload);
    }

    private void insertAudit(String taskId, String caseId, String event, Object payload) {
        jdbc.update("INSERT INTO lab_audit(task_id,case_id,event,payload_json,created_at_ms) VALUES(?,?,?,?,?)",
                taskId, caseId, event, json(payload), now());
    }

    private LabConfig readConfig(String value) {
        try { return mapper.readValue(value, LabConfig.class); }
        catch (JsonProcessingException e) { throw new IllegalStateException("stored case config is invalid", e); }
    }

    private JsonNode readTree(String value) {
        try { return mapper.readTree(value); }
        catch (JsonProcessingException e) { throw new IllegalStateException("stored evidence is invalid", e); }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> readMap(String value) {
        try { return mapper.readValue(value, Map.class); }
        catch (JsonProcessingException e) { throw new IllegalStateException("stored JSON is invalid", e); }
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> readListOfMaps(String value) {
        try { return mapper.readValue(value, mapper.getTypeFactory().constructCollectionType(List.class, Map.class)); }
        catch (JsonProcessingException e) { throw new IllegalStateException("stored messages are invalid", e); }
    }

    private String json(Object value) {
        try { return mapper.writeValueAsString(value); }
        catch (JsonProcessingException e) { throw new IllegalStateException("could not serialize API data", e); }
    }

    private String sha256(String value) {
        try {
            byte[] bytes = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder out = new StringBuilder(bytes.length * 2);
            for (byte b : bytes) out.append(String.format("%02x", b));
            return out.toString();
        } catch (NoSuchAlgorithmException e) { throw new IllegalStateException(e); }
    }

    private long now() { return Instant.now().toEpochMilli(); }

    private record CaseRow(String id, String symptom, String configJson, long configVersion, long createdAt) {}
    private record TaskRow(String id, String caseId, String symptom, String status, long version, String requestHash,
                           String runToken, Long leaseUntil, String messagesJson, String proposalJson,
                           Long proposalConfigVersion, String approvalResultJson, String failureReason, long createdAt, long updatedAt) {}
    private record EvidenceRow(String id, String taskId, String tool, String dataJson,
                               long configVersion, long createdAt) {}
}
