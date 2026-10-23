package com.zengbohan.configdoctor.api;

import com.zengbohan.configdoctor.service.ConfigDoctorService;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api")
public class ApiController {
    private final ConfigDoctorService service;

    public ApiController(ConfigDoctorService service) { this.service = service; }

    @GetMapping("/health")
    public Map<String, String> health() { return Map.of("status", "UP"); }

    @PostMapping("/cases")
    public Map<String, Object> createCase(@Valid @RequestBody ApiModels.CaseCreateRequest request) {
        return service.createCase(request);
    }

    @GetMapping("/cases")
    public List<Map<String, Object>> listCases() { return service.listCases(); }

    @PostMapping("/cases/{id}/config")
    public Map<String, Object> updateConfig(@PathVariable String id,
                                            @Valid @RequestBody ApiModels.CaseConfigUpdateRequest request) {
        return service.updateConfig(id, request);
    }

    @PostMapping("/tasks")
    public Map<String, Object> createTask(@Valid @RequestBody ApiModels.TaskCreateRequest request) {
        return service.createTask(request);
    }

    @GetMapping("/tasks")
    public List<Map<String, Object>> listTasks() { return service.listTasks(); }

    @GetMapping("/tasks/{id}")
    public Map<String, Object> getTask(@PathVariable String id) { return service.getTask(id); }

    @PostMapping("/tasks/{id}/claim")
    public Map<String, Object> claim(@PathVariable String id, @Valid @RequestBody ApiModels.ClaimRequest request) {
        return service.claim(id);
    }

    @PostMapping("/tasks/{id}/checkpoint")
    public Map<String, Object> checkpoint(@PathVariable String id, @Valid @RequestBody ApiModels.CheckpointRequest request) {
        return service.checkpoint(id, request);
    }

    @PostMapping("/tasks/{id}/tools/{tool}")
    public Map<String, Object> tool(@PathVariable String id, @PathVariable String tool,
                                    @Valid @RequestBody ApiModels.ToolRequest request) {
        return service.runTool(id, tool, request);
    }

    @PostMapping("/tasks/{id}/proposal")
    public Map<String, Object> proposal(@PathVariable String id, @Valid @RequestBody ApiModels.ProposalRequest request) {
        return service.submitProposal(id, request);
    }

    @PostMapping("/tasks/{id}/fail")
    public Map<String, Object> fail(@PathVariable String id, @Valid @RequestBody ApiModels.FailRequest request) {
        return service.failTask(id, request);
    }

    @PostMapping("/tasks/{id}/approve")
    public Map<String, Object> approve(@PathVariable String id, @Valid @RequestBody ApiModels.ApprovalRequest request) {
        return service.approve(id, request);
    }
}
