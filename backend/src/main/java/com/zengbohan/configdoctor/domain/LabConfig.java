package com.zengbohan.configdoctor.domain;

import java.math.BigDecimal;
import java.math.RoundingMode;

public record LabConfig(
        int upstreamDelayMs,
        int timeoutMs,
        BigDecimal cachePrice,
        BigDecimal sourcePrice,
        int capacity,
        BigDecimal taxRate) {

    public static final BigDecimal DEFAULT_PRICE = new BigDecimal("10.00");
    public static final BigDecimal DEFAULT_TAX_RATE = new BigDecimal("0.06");
    public static final int DEFAULT_DELAY_MS = 20;
    public static final int DEFAULT_CAPACITY = 10;

    public static LabConfig defaults() {
        return new LabConfig(DEFAULT_DELAY_MS, DEFAULT_DELAY_MS + 50,
                DEFAULT_PRICE, DEFAULT_PRICE, DEFAULT_CAPACITY, DEFAULT_TAX_RATE);
    }

    public LabConfig validated() {
        if (upstreamDelayMs < 0 || upstreamDelayMs > 200) {
            throw new IllegalArgumentException("upstreamDelayMs must be between 0 and 200");
        }
        if (timeoutMs < 1 || timeoutMs > 1000) {
            throw new IllegalArgumentException("timeoutMs must be between 1 and 1000");
        }
        validateMoney(cachePrice, "cachePrice");
        validateMoney(sourcePrice, "sourcePrice");
        if (capacity < 0 || capacity > 10000) {
            throw new IllegalArgumentException("capacity must be between 0 and 10000");
        }
        if (taxRate == null || taxRate.compareTo(BigDecimal.ZERO) < 0 || taxRate.compareTo(BigDecimal.ONE) > 0) {
            throw new IllegalArgumentException("taxRate must be between 0 and 1");
        }
        return new LabConfig(upstreamDelayMs, timeoutMs,
                cachePrice.setScale(2, RoundingMode.HALF_UP),
                sourcePrice.setScale(2, RoundingMode.HALF_UP), capacity,
                taxRate.setScale(4, RoundingMode.HALF_UP));
    }

    private static void validateMoney(BigDecimal value, String name) {
        if (value == null || value.compareTo(BigDecimal.ZERO) <= 0 || value.compareTo(new BigDecimal("100000")) > 0) {
            throw new IllegalArgumentException(name + " must be greater than 0 and at most 100000");
        }
    }

    public LabConfig withTimeout(int value) {
        return new LabConfig(upstreamDelayMs, value, cachePrice, sourcePrice, capacity, taxRate).validated();
    }

    public LabConfig withCachePrice(BigDecimal value) {
        return new LabConfig(upstreamDelayMs, timeoutMs, value, sourcePrice, capacity, taxRate).validated();
    }

    public LabConfig withCapacity(int value) {
        return new LabConfig(upstreamDelayMs, timeoutMs, cachePrice, sourcePrice, value, taxRate).validated();
    }

    public LabConfig withTaxRate(BigDecimal value) {
        return new LabConfig(upstreamDelayMs, timeoutMs, cachePrice, sourcePrice, capacity, value).validated();
    }
}
