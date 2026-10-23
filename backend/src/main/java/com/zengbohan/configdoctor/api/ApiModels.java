package com.zengbohan.configdoctor.api;

import com.zengbohan.configdoctor.domain.LabConfig;
import jakarta.validation.Valid;
import jakarta.validation.constraints.DecimalMax;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;

public final class ApiModels {
    private ApiModels() {}

    public enum RootCause { TIMEOUT, CACHE_DRIFT, CAPACITY, TAX, HEALTHY }
    public enum Action { RESTORE_TIMEOUT, REFRESH_CACHE, RESTORE_CAPACITY, RESTORE_TAX, NONE }

    public record ConfigInput(
            @Min(0) @Max(200) Integer upstreamDelayMs,
            @Min(1) @Max(1000) Integer timeoutMs,
            @DecimalMin("0.01") @DecimalMax("100000") BigDecimal cachePrice,
            @DecimalMin("0.01") @DecimalMax("100000") BigDecimal sourcePrice,
            @Min(0) @Max(10000) Integer capacity,
            @DecimalMin("0") @DecimalMax("1") BigDecimal taxRate) {
        public LabConfig toConfig() {
            LabConfig defaults = LabConfig.defaults();
            BigDecimal source = sourcePrice == null ? defaults.sourcePrice() : sourcePrice;
            int delay = upstreamDelayMs == null ? defaults.upstreamDelayMs() : upstreamDelayMs;
            return new LabConfig(delay,
                    timeoutMs == null ? delay + 50 : timeoutMs,
                    cachePrice == null ? source : cachePrice,
                    source,
                    capacity == null ? defaults.capacity() : capacity,
                    taxRate == null ? defaults.taxRate() : taxRate).validated();
        }
    }

    public record CaseCreateRequest(
            @NotBlank @Size(max = 100) String id,
            @NotNull @Valid ConfigInput config,
            @NotBlank @Size(max = 2000) String symptom) {}

    public record CaseConfigUpdateRequest(
            @NotNull @Valid ConfigInput config,
            @NotNull @Min(1) Long expectedVersion) {}

    public record TaskCreateRequest(
            @NotBlank @Size(max = 100) String caseId,
            @NotBlank @Size(max = 2000) String symptom,
            @NotBlank @Size(max = 200) String idempotencyKey) {}

    public record ClaimRequest() {}

    public record CheckpointRequest(
            @NotBlank String runToken,
            @NotNull @Size(max = 128) List<Map<String, Object>> messages) {}

    public record ToolRequest(@NotBlank String runToken) {}

    public record ProposalRequest(
            @NotBlank String runToken,
            @NotNull RootCause rootCause,
            @NotNull Action action,
            @NotEmpty @Size(max = 8) List<@NotBlank String> evidenceIds,
            @NotBlank @Size(max = 1000) String summary) {}

    public record FailRequest(@NotBlank String runToken, @NotBlank @Size(max = 1000) String reason) {}

    public record ApprovalRequest(@NotBlank @Size(max = 200) String idempotencyKey) {}
}
