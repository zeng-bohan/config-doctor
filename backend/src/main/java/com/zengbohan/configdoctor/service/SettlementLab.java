package com.zengbohan.configdoctor.service;

import com.zengbohan.configdoctor.domain.LabConfig;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.LinkedHashMap;
import java.util.Map;

@Component
public class SettlementLab {
    private static final BigDecimal CONTRACT_TAX_RATE = new BigDecimal("0.06");
    private static final int CONTRACT_QUANTITY = 2;

    public Map<String, Object> probe(LabConfig config) {
        long started = System.nanoTime();
        try {
            Thread.sleep(config.upstreamDelayMs());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("settlement probe interrupted", e);
        }
        long elapsedMs = Math.max(0, (System.nanoTime() - started) / 1_000_000L);

        String code;
        String detail;
        BigDecimal expected = config.sourcePrice()
                .multiply(BigDecimal.valueOf(CONTRACT_QUANTITY))
                .multiply(BigDecimal.ONE.add(CONTRACT_TAX_RATE))
                .setScale(2, RoundingMode.HALF_UP);
        BigDecimal actual = config.cachePrice()
                .multiply(BigDecimal.valueOf(CONTRACT_QUANTITY))
                .multiply(BigDecimal.ONE.add(config.taxRate()))
                .setScale(2, RoundingMode.HALF_UP);

        if (elapsedMs > config.timeoutMs()) {
            code = "UPSTREAM_TIMEOUT";
            detail = "upstream response exceeded timeoutMs";
        } else if (config.capacity() < CONTRACT_QUANTITY) {
            code = "QUANTITY_REJECTED";
            detail = "requested quantity exceeds capacity";
        } else if (actual.compareTo(expected) != 0) {
            code = "AMOUNT_MISMATCH";
            detail = "settlement amount differs from contract amount";
        } else {
            code = "OK";
            detail = "settlement contract passed";
        }

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("passed", "OK".equals(code));
        result.put("code", code);
        result.put("detail", detail);
        result.put("quantity", CONTRACT_QUANTITY);
        result.put("sourcePrice", config.sourcePrice());
        result.put("actualAmount", actual);
        result.put("expectedAmount", expected);
        result.put("upstreamDelayMs", config.upstreamDelayMs());
        result.put("timeoutMs", config.timeoutMs());
        result.put("elapsedMs", elapsedMs);
        return result;
    }
}
