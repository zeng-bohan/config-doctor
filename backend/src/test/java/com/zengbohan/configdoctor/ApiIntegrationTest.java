package com.zengbohan.configdoctor;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.annotation.DirtiesContext;

import java.math.BigDecimal;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

import static org.junit.jupiter.api.Assertions.*;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {
                "server.address=127.0.0.1",
                "spring.datasource.url=jdbc:h2:mem:configdoctor-test;MODE=PostgreSQL;DB_CLOSE_DELAY=-1;LOCK_TIMEOUT=10000",
                "config-doctor.lease-seconds=30"
        })
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class ApiIntegrationTest {
    private static final String VIEWER = "viewer-token";
    private static final String WORKER = "worker-token";
    private static final String OPERATOR = "operator-token";

    @LocalServerPort
    int port;
    @Autowired ObjectMapper mapper;
    @Autowired JdbcTemplate jdbc;

    private final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

    @Test
    void healthIsAnonymousAndProtectedRoutesRequireBearerToken() throws Exception {
        assertEquals(200, call("GET", "/api/health", null, null).statusCode());
        assertEquals(401, call("GET", "/api/cases", null, null).statusCode());
        assertEquals(401, call("GET", "/api/cases", "wrong-token", null).statusCode());
    }

    @Test
    void createCaseUsesServerDefaultsAndNeverReturnsExpectedRoot() throws Exception {
        String id = id("defaults");
        JsonNode body = node(call("POST", "/api/cases", OPERATOR,
                Map.of("id", id, "symptom", "settlement fails", "config", Map.of())));
        assertEquals(1, body.path("configVersion").asInt());
        assertDecimal("10.00", body.path("config").path("sourcePrice"));
        assertDecimal("10.00", body.path("config").path("capacity"));
        assertDecimal("0.06", body.path("config").path("taxRate"));
        assertFalse(body.has("expectedRoot"));
        JsonNode listed = node(call("GET", "/api/cases", VIEWER, null));
        assertFalse(listed.toString().contains("expectedRoot"));
    }

    @Test
    void duplicateCaseCannotOverwriteExistingConfiguration() throws Exception {
        String caseId = id("case-duplicate");
        assertEquals(200, createCase(caseId, config(20, 70, "12", "10", 10, "0.06")).statusCode());
        assertEquals(409, createCase(caseId, config(20, 70, "99", "10", 10, "0.06")).statusCode());
        JsonNode read = node(call("GET", "/api/cases", VIEWER, null));
        JsonNode found = findById(read, caseId);
        assertDecimal("12.00", found.path("config").path("cachePrice"));
    }

    @Test
    void taskCreationReturnsOriginalForSameIdempotentPayload() throws Exception {
        String caseId = healthyCase();
        String key = id("task-same");
        JsonNode first = node(createTask(caseId, "timeout symptom", key));
        JsonNode second = node(createTask(caseId, "timeout symptom", key));
        assertEquals(first.path("id").asText(), second.path("id").asText());
        assertEquals("CREATED", second.path("status").asText());
        assertEquals("timeout symptom", second.path("symptom").asText());
    }

    @Test
    void taskIdempotencyRejectsDifferentPayload() throws Exception {
        String caseId = healthyCase();
        String key = id("task-mismatch");
        assertEquals(200, createTask(caseId, "first symptom", key).statusCode());
        HttpResponse<String> mismatch = createTask(caseId, "changed symptom", key);
        assertEquals(409, mismatch.statusCode());
        assertTrue(mismatch.body().contains("different request"));
    }

    @Test
    void concurrentTaskCreationUsesDatabaseIdempotencyConstraint() throws Exception {
        String caseId = healthyCase();
        String key = id("task-race");
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<HttpResponse<String>> one = pool.submit(() -> { start.await(); return createTask(caseId, "same", key); });
            Future<HttpResponse<String>> two = pool.submit(() -> { start.await(); return createTask(caseId, "same", key); });
            start.countDown();
            JsonNode first = node(one.get());
            JsonNode second = node(two.get());
            assertEquals(first.path("id").asText(), second.path("id").asText());
        } finally { pool.shutdownNow(); }
    }

    @Test
    void onlyOneConcurrentWorkerCanClaimActiveTask() throws Exception {
        String taskId = newTask(healthyCase());
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<HttpResponse<String>> one = pool.submit(() -> { start.await(); return call("POST", "/api/tasks/" + taskId + "/claim", WORKER, Map.of()); });
            Future<HttpResponse<String>> two = pool.submit(() -> { start.await(); return call("POST", "/api/tasks/" + taskId + "/claim", WORKER, Map.of()); });
            start.countDown();
            int a = one.get().statusCode();
            int b = two.get().statusCode();
            assertTrue((a == 200 && b == 409) || (a == 409 && b == 200), "statuses=" + a + "," + b);
        } finally { pool.shutdownNow(); }
    }

    @Test
    void expiredLeaseCanBeReclaimedAndOldRunTokenIsRejected() throws Exception {
        String taskId = newTask(healthyCase());
        String oldToken = claim(taskId);
        jdbc.update("UPDATE lab_tasks SET lease_until_ms=? WHERE id=?", System.currentTimeMillis() - 1, taskId);
        String newToken = claim(taskId);
        assertNotEquals(oldToken, newToken);
        HttpResponse<String> stale = call("POST", "/api/tasks/" + taskId + "/checkpoint", WORKER,
                Map.of("runToken", oldToken, "messages", List.of(Map.of("role", "assistant", "content", "stale"))));
        assertEquals(409, stale.statusCode());
    }

    @Test
    void checkpointPersistsMessagesAndRenewsLease() throws Exception {
        String taskId = newTask(healthyCase());
        String token = claim(taskId);
        JsonNode result = node(call("POST", "/api/tasks/" + taskId + "/checkpoint", WORKER,
                Map.of("runToken", token, "messages", List.of(Map.of("role", "assistant", "content", "checking probe")))));
        assertTrue(result.path("leaseUntil").asLong() > System.currentTimeMillis());
        JsonNode task = node(call("GET", "/api/tasks/" + taskId, VIEWER, null));
        assertEquals("checking probe", task.path("messages").get(0).path("content").asText());
    }

    @Test
    void probeRunsSettlementContractAndPersistsEvidenceAndLogs() throws Exception {
        String caseId = createFaultCase(config(8, 1, "10", "10", 10, "0.06"));
        String taskId = newTask(caseId);
        String token = claim(taskId);
        JsonNode probe = node(tool(taskId, token, "probe"));
        assertEquals("UPSTREAM_TIMEOUT", probe.path("data").path("code").asText());
        assertEquals(2, probe.path("data").path("quantity").asInt());
        assertTrue(probe.path("data").path("elapsedMs").asLong() >= 1);
        JsonNode task = node(call("GET", "/api/tasks/" + taskId, VIEWER, null));
        assertEquals(1, task.path("evidence").size());
        assertEquals(probe.path("id").asText(), task.path("evidence").get(0).path("id").asText());
        JsonNode logs = node(tool(taskId, token, "logs"));
        assertEquals(1, logs.path("data").path("entries").size());
    }

    @Test
    void configToolReturnsOnlyRealConfigAndNoEvaluationAnswer() throws Exception {
        String taskId = newTask(healthyCase());
        String token = claim(taskId);
        JsonNode configEvidence = node(tool(taskId, token, "config"));
        assertTrue(configEvidence.path("data").path("config").has("sourcePrice"));
        assertFalse(configEvidence.toString().contains("expectedRoot"));
        assertFalse(configEvidence.toString().contains("rootCause"));
    }

    @Test
    void evidenceIdsCannotBeBorrowedAcrossTasks() throws Exception {
        String caseId = createFaultCase(config(8, 1, "10", "10", 10, "0.06"));
        String firstTask = newTask(caseId);
        String firstToken = claim(firstTask);
        String probeId = node(tool(firstTask, firstToken, "probe")).path("id").asText();
        String configId = node(tool(firstTask, firstToken, "config")).path("id").asText();

        String secondTask = newTask(caseId);
        String secondToken = claim(secondTask);
        HttpResponse<String> response = propose(secondTask, secondToken, "TIMEOUT", "RESTORE_TIMEOUT", List.of(probeId, configId));
        assertEquals(422, response.statusCode());
    }

    @Test
    void proposalRequiresMatchingProbeAndConfigEvidence() throws Exception {
        String caseId = createFaultCase(config(8, 1, "10", "10", 10, "0.06"));
        String taskId = newTask(caseId);
        String token = claim(taskId);
        JsonNode observedProbe = node(tool(taskId, token, "probe"));
        assertEquals("UPSTREAM_TIMEOUT", observedProbe.path("data").path("code").asText());
        String probeId = observedProbe.path("id").asText();
        assertEquals(422, propose(taskId, token, "HEALTHY", "NONE", List.of(probeId)).statusCode());
        assertEquals(422, propose(taskId, token, "TIMEOUT", "REFRESH_CACHE", List.of(probeId)).statusCode());
        assertEquals(422, propose(taskId, token, "TIMEOUT", "RESTORE_TIMEOUT", List.of(probeId)).statusCode());
        String configId = node(tool(taskId, token, "config")).path("id").asText();
        JsonNode accepted = node(propose(taskId, token, "CACHE_DRIFT", "REFRESH_CACHE", List.of(probeId, configId)));
        assertEquals("AWAITING_APPROVAL", accepted.path("status").asText());
        assertEquals(1, accepted.path("proposalConfigVersion").asInt());
        assertEquals(10, accepted.path("proposal").path("patch").path("cachePrice").path("before").decimalValue().intValue());
    }

    @Test
    void proposalRejectsEvidenceFromAnOlderCaseConfigVersion() throws Exception {
        String caseId = createFaultCase(config(8, 1, "10", "10", 10, "0.06"));
        String taskId = newTask(caseId);
        String token = claim(taskId);
        String probeId = node(tool(taskId, token, "probe")).path("id").asText();
        String configId = node(tool(taskId, token, "config")).path("id").asText();
        assertEquals(200, call("POST", "/api/cases/" + caseId + "/config", OPERATOR,
                Map.of("expectedVersion", 1, "config", config(8, 80, "10", "10", 10, "0.06"))).statusCode());
        assertEquals(409, propose(taskId, token, "TIMEOUT", "RESTORE_TIMEOUT", List.of(probeId, configId)).statusCode());
    }

    @Test
    void operatorApprovalAppliesServerPatchAndReplaysWithoutSecondMutation() throws Exception {
        String caseId = createFaultCase(config(20, 70, "13", "10", 10, "0.06"));
        String taskId = diagnosedTask(caseId, "CACHE_DRIFT", "REFRESH_CACHE");
        String key = id("approve-cache");
        JsonNode first = node(call("POST", "/api/tasks/" + taskId + "/approve", OPERATOR, Map.of("idempotencyKey", key)));
        assertEquals("COMPLETED", first.path("status").asText());
        assertTrue(first.path("verification").path("passed").asBoolean());
        JsonNode replay = node(call("POST", "/api/tasks/" + taskId + "/approve", OPERATOR, Map.of("idempotencyKey", key)));
        assertEquals(first.path("configVersion").asLong(), replay.path("configVersion").asLong());
        assertEquals(first.path("evidenceId").asText(), replay.path("evidenceId").asText());
        JsonNode cases = node(call("GET", "/api/cases", VIEWER, null));
        JsonNode updated = findById(cases, caseId);
        assertEquals(2, updated.path("configVersion").asInt());
        assertDecimal("10.00", updated.path("config").path("cachePrice"));
        JsonNode task = node(call("GET", "/api/tasks/" + taskId, VIEWER, null));
        assertEquals(3, task.path("evidence").size());
        assertEquals("COMPLETED", task.path("approvalResult").path("status").asText());
    }

    @Test
    void configVersionChangeMakesPendingApprovalStale() throws Exception {
        String caseId = createFaultCase(config(8, 1, "10", "10", 10, "0.06"));
        String taskId = diagnosedTask(caseId, "TIMEOUT", "RESTORE_TIMEOUT");
        HttpResponse<String> update = call("POST", "/api/cases/" + caseId + "/config", OPERATOR,
                Map.of("expectedVersion", 1, "config", config(8, 80, "10", "10", 10, "0.06")));
        assertEquals(200, update.statusCode());
        HttpResponse<String> stale = call("POST", "/api/tasks/" + taskId + "/approve", OPERATOR,
                Map.of("idempotencyKey", id("approve-stale")));
        assertEquals(409, stale.statusCode());
        JsonNode cases = node(call("GET", "/api/cases", VIEWER, null));
        JsonNode current = findById(cases, caseId);
        assertEquals(2, current.path("configVersion").asInt());
        assertEquals(80, current.path("config").path("timeoutMs").asInt());
    }

    @Test
    void workerCannotApproveAndViewerCannotRunWorkerTools() throws Exception {
        String caseId = healthyCase();
        String taskId = newTask(caseId);
        assertEquals(403, call("POST", "/api/tasks/" + taskId + "/claim", VIEWER, Map.of()).statusCode());
        assertEquals(403, call("POST", "/api/tasks/" + taskId + "/approve", WORKER, Map.of("idempotencyKey", id("wrong-role"))).statusCode());
        assertEquals(200, call("GET", "/api/tasks", OPERATOR, null).statusCode());
        assertEquals(200, call("GET", "/api/tasks", WORKER, null).statusCode());
    }

    @Test
    void healthyDiagnosisCompletesOnlyAfterPassingProbe() throws Exception {
        String taskId = newTask(healthyCase());
        String token = claim(taskId);
        String probeId = node(tool(taskId, token, "probe")).path("id").asText();
        JsonNode result = node(propose(taskId, token, "HEALTHY", "NONE", List.of(probeId)));
        assertEquals("COMPLETED", result.path("status").asText());
        assertEquals(200, call("GET", "/api/tasks/" + taskId, VIEWER, null).statusCode());
    }

    @Test
    void timeoutPatchUsesBoundedDelayPlusFiftyAndRejectsDelayAboveTwoHundred() throws Exception {
        HttpResponse<String> invalid = createCase(id("delay-limit"), config(201, 251, "10", "10", 10, "0.06"));
        assertEquals(400, invalid.statusCode());

        String caseId = createFaultCase(config(200, 1, "10", "10", 10, "0.06"));
        String taskId = newTask(caseId);
        String token = claim(taskId);
        String probeId = node(tool(taskId, token, "probe")).path("id").asText();
        String configId = node(tool(taskId, token, "config")).path("id").asText();
        JsonNode proposal = node(propose(taskId, token, "TIMEOUT", "RESTORE_TIMEOUT", List.of(probeId, configId)));
        assertEquals(250, proposal.path("proposal").path("patch").path("timeoutMs").path("after").asInt());
        JsonNode approval = node(call("POST", "/api/tasks/" + taskId + "/approve", OPERATOR,
                Map.of("idempotencyKey", id("approve-timeout"))));
        assertTrue(approval.path("verification").path("passed").asBoolean());
    }

    @Test
    void concurrentApprovalAndDifferentKeyReplayDoNotApplyPatchTwice() throws Exception {
        String caseId = createFaultCase(config(1, 100, "13", "10", 10, "0.06"));
        String taskId = diagnosedTask(caseId, "CACHE_DRIFT", "REFRESH_CACHE");
        String key = id("approve-race");
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        JsonNode one;
        JsonNode two;
        try {
            Future<HttpResponse<String>> first = pool.submit(() -> { start.await(); return call("POST", "/api/tasks/" + taskId + "/approve", OPERATOR, Map.of("idempotencyKey", key)); });
            Future<HttpResponse<String>> second = pool.submit(() -> { start.await(); return call("POST", "/api/tasks/" + taskId + "/approve", OPERATOR, Map.of("idempotencyKey", key)); });
            start.countDown();
            HttpResponse<String> r1 = first.get();
            HttpResponse<String> r2 = second.get();
            assertEquals(200, r1.statusCode(), r1.body());
            assertEquals(200, r2.statusCode(), r2.body());
            one = mapper.readTree(r1.body());
            two = mapper.readTree(r2.body());
        } finally { pool.shutdownNow(); }
        assertEquals(one.path("configVersion").asLong(), two.path("configVersion").asLong());
        assertEquals(one.path("evidenceId").asText(), two.path("evidenceId").asText());
        JsonNode alternateKeyReplay = node(call("POST", "/api/tasks/" + taskId + "/approve", OPERATOR,
                Map.of("idempotencyKey", id("approve-alternate"))));
        assertEquals(one.path("evidenceId").asText(), alternateKeyReplay.path("evidenceId").asText());
        assertEquals(2, findById(node(call("GET", "/api/cases", VIEWER, null)), caseId).path("configVersion").asInt());
    }

    @Test
    void approvalKeyCannotBeReusedForAnotherTask() throws Exception {
        String firstCase = createFaultCase(config(1, 100, "13", "10", 10, "0.06"));
        String firstTask = diagnosedTask(firstCase, "CACHE_DRIFT", "REFRESH_CACHE");
        String key = id("approval-global-key");
        assertEquals(200, call("POST", "/api/tasks/" + firstTask + "/approve", OPERATOR, Map.of("idempotencyKey", key)).statusCode());
        String secondCase = createFaultCase(config(1, 100, "13", "10", 10, "0.06"));
        String secondTask = diagnosedTask(secondCase, "CACHE_DRIFT", "REFRESH_CACHE");
        assertEquals(409, call("POST", "/api/tasks/" + secondTask + "/approve", OPERATOR, Map.of("idempotencyKey", key)).statusCode());
    }

    @Test
    void failureEndpointPersistsReasonAndMakesTaskTerminal() throws Exception {
        String taskId = newTask(healthyCase());
        String token = claim(taskId);
        JsonNode result = node(call("POST", "/api/tasks/" + taskId + "/fail", WORKER,
                Map.of("runToken", token, "reason", "worker stopped after bounded retries")));
        assertEquals("FAILED", result.path("status").asText());
        assertEquals("worker stopped after bounded retries", result.path("failureReason").asText());
    }

    private String healthyCase() throws Exception {
        return createFaultCase(config(1, 100, "10", "10", 10, "0.06"));
    }

    private String createFaultCase(Map<String, Object> config) throws Exception {
        String caseId = id("case");
        HttpResponse<String> response = createCase(caseId, config);
        assertEquals(200, response.statusCode(), response.body());
        return caseId;
    }

    private HttpResponse<String> createCase(String caseId, Map<String, Object> config) throws Exception {
        return call("POST", "/api/cases", OPERATOR,
                Map.of("id", caseId, "symptom", "settlement does not match its contract", "config", config));
    }

    private String newTask(String caseId) throws Exception {
        JsonNode result = node(createTask(caseId, "settlement failed", id("task")));
        return result.path("id").asText();
    }

    private HttpResponse<String> createTask(String caseId, String symptom, String key) throws Exception {
        return call("POST", "/api/tasks", VIEWER,
                Map.of("caseId", caseId, "symptom", symptom, "idempotencyKey", key));
    }

    private String claim(String taskId) throws Exception {
        JsonNode result = node(call("POST", "/api/tasks/" + taskId + "/claim", WORKER, Map.of()));
        assertFalse(result.path("runToken").asText().isBlank());
        return result.path("runToken").asText();
    }

    private HttpResponse<String> tool(String taskId, String token, String name) throws Exception {
        return call("POST", "/api/tasks/" + taskId + "/tools/" + name, WORKER, Map.of("runToken", token));
    }

    private String diagnosedTask(String caseId, String rootCause, String action) throws Exception {
        String taskId = newTask(caseId);
        String token = claim(taskId);
        String probeId = node(tool(taskId, token, "probe")).path("id").asText();
        String configId = node(tool(taskId, token, "config")).path("id").asText();
        JsonNode result = node(propose(taskId, token, rootCause, action, List.of(probeId, configId)));
        assertEquals("AWAITING_APPROVAL", result.path("status").asText());
        return result.path("id").asText().isEmpty() ? taskId : result.path("id").asText();
    }

    private HttpResponse<String> propose(String taskId, String token, String rootCause, String action,
                                         List<String> evidenceIds) throws Exception {
        return call("POST", "/api/tasks/" + taskId + "/proposal", WORKER,
                Map.of("runToken", token, "rootCause", rootCause, "action", action,
                        "evidenceIds", evidenceIds, "summary", "evidence supports the observed configuration fault"));
    }

    private HttpResponse<String> call(String method, String path, String token, Object body) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                .timeout(Duration.ofSeconds(10)).header("Content-Type", "application/json");
        if (token != null) builder.header("Authorization", "Bearer " + token);
        String payload = body == null ? "" : mapper.writeValueAsString(body);
        HttpRequest request = switch (method) {
            case "GET" -> builder.GET().build();
            case "POST" -> builder.POST(HttpRequest.BodyPublishers.ofString(payload)).build();
            default -> throw new IllegalArgumentException("unsupported method");
        };
        return client.send(request, HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode node(HttpResponse<String> response) throws Exception {
        assertTrue(response.statusCode() >= 200 && response.statusCode() < 300,
                "HTTP " + response.statusCode() + ": " + response.body());
        return mapper.readTree(response.body());
    }

    private JsonNode findById(JsonNode list, String id) {
        for (JsonNode item : list) if (id.equals(item.path("id").asText())) return item;
        fail("id not found: " + id);
        return null;
    }

    private void assertDecimal(String expected, JsonNode value) {
        assertEquals(0, new BigDecimal(expected).compareTo(value.decimalValue()), "actual=" + value);
    }

    private String id(String prefix) { return prefix + "-" + UUID.randomUUID(); }

    private Map<String, Object> config(int delay, int timeout, String cache, String source, int capacity, String tax) {
        return Map.of("upstreamDelayMs", delay, "timeoutMs", timeout,
                "cachePrice", new BigDecimal(cache), "sourcePrice", new BigDecimal(source),
                "capacity", capacity, "taxRate", new BigDecimal(tax));
    }
}
