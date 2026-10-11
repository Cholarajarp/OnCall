---
service: payments
title: Payments latency high
alerts: [CheckoutLatencyHigh, PaymentsLatencyHigh, PaymentsDown]
---
# Payments latency high

Use when checkout p95 latency rises or the service stops being scraped.

## Steps
1. Confirm the service is being scraped; a missing target means health is unknown.
2. Check host CPU, memory and disk on the payments host.
3. Look for a traffic surge versus the normal baseline for this time of day.
4. Check whether a release changed timeouts, retries or pool sizes.
5. If a release is the likely trigger, propose a rollback; otherwise scale or shed load and escalate.
