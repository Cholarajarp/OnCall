---
service: payments
title: Payment gateway timeouts
alerts: [PaymentGatewayTimeout, GatewayTimeoutHigh]
---
# Payment gateway timeouts

Use when checkout failures are dominated by gateway timeout errors.

## Steps
1. Compare the configured gateway timeout in the active release with observed gateway latency.
2. Check the gateway provider status page for a declared incident.
3. If a recent release tightened the timeout, propose rolling that release back.
4. If the provider is degraded, tell support and pause marketing traffic; do not roll back healthy code.
5. Verify recovery with fresh checkout traffic, then record the gateway latency seen during the incident.
