package com.zengbohan.configdoctor.api;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

@Component
public class ApiAuthFilter extends OncePerRequestFilter {
    private final String viewerToken;
    private final String workerToken;
    private final String operatorToken;

    public ApiAuthFilter(@Value("${config-doctor.auth.viewer-token}") String viewerToken,
                         @Value("${config-doctor.auth.worker-token}") String workerToken,
                         @Value("${config-doctor.auth.operator-token}") String operatorToken) {
        this.viewerToken = viewerToken;
        this.workerToken = workerToken;
        this.operatorToken = operatorToken;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        if ("/api/health".equals(request.getRequestURI())) {
            chain.doFilter(request, response);
            return;
        }
        if (!isLoopback(request.getRemoteAddr())) {
            writeError(response, 403, "loopback_only", "API is available only on loopback");
            return;
        }
        String header = request.getHeader("Authorization");
        if (header == null || !header.startsWith("Bearer ")) {
            writeError(response, 401, "unauthorized", "Bearer token required");
            return;
        }
        String token = header.substring("Bearer ".length());
        Role role = roleFor(token);
        if (role == null) {
            writeError(response, 401, "unauthorized", "invalid bearer token");
            return;
        }
        Role required = requiredRole(request.getMethod(), request.getRequestURI());
        if (!role.canAccess(required)) {
            writeError(response, 403, "forbidden", "token role cannot access this endpoint");
            return;
        }
        chain.doFilter(request, response);
    }

    private Role roleFor(String token) {
        if (constantTimeEquals(token, operatorToken)) return Role.OPERATOR;
        if (constantTimeEquals(token, workerToken)) return Role.WORKER;
        if (constantTimeEquals(token, viewerToken)) return Role.VIEWER;
        return null;
    }

    private Role requiredRole(String method, String path) {
        if ("GET".equals(method) && ("/api/cases".equals(path) || "/api/tasks".equals(path)
                || path.matches("/api/tasks/[^/]+"))) return Role.VIEWER;
        if ("POST".equals(method) && ("/api/tasks".equals(path))) return Role.VIEWER;
        if ("POST".equals(method) && ("/api/cases".equals(path)
                || path.matches("/api/cases/[^/]+/config")
                || path.matches("/api/tasks/[^/]+/approve"))) return Role.OPERATOR;
        if ("POST".equals(method) && (path.matches("/api/tasks/[^/]+/claim")
                || path.matches("/api/tasks/[^/]+/checkpoint")
                || path.matches("/api/tasks/[^/]+/tools/[^/]+")
                || path.matches("/api/tasks/[^/]+/proposal")
                || path.matches("/api/tasks/[^/]+/fail"))) return Role.WORKER;
        return Role.VIEWER;
    }

    private boolean isLoopback(String remoteAddress) {
        try { return InetAddress.getByName(remoteAddress).isLoopbackAddress(); }
        catch (Exception e) { return false; }
    }

    private boolean constantTimeEquals(String left, String right) {
        if (left == null || right == null) return false;
        return MessageDigest.isEqual(left.getBytes(StandardCharsets.UTF_8), right.getBytes(StandardCharsets.UTF_8));
    }

    private void writeError(HttpServletResponse response, int status, String error, String message) throws IOException {
        response.setStatus(status);
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        response.setContentType("application/json");
        response.getWriter().write("{\"error\":\"" + error + "\",\"message\":\"" + message + "\"}");
    }

    private enum Role {
        VIEWER, WORKER, OPERATOR;
        boolean canAccess(Role required) {
            return this == required || (required == VIEWER && (this == WORKER || this == OPERATOR));
        }
    }
}
