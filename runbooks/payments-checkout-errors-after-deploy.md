---
service: payments
title: Payments checkout errors after a deploy
alerts: [CheckoutErrorRateHigh, HighErrorRate, CheckoutErrors]
default: true
---
# Payments checkout errors after a deploy

Use when checkout 5xx rises above 5% and a release went out shortly before.
Timing is a lead, not proof: confirm before and after any change.

## Steps
1. Confirm the error rate is measured and fresh; treat missing telemetry as unknown, not healthy.
2. Check whether a release went live in the 30 minutes before the spike and note its commit.
3. Search recent error logs for the dominant failure (timeouts, gateway errors, config parse failures).
4. If the active release lines up with the spike, propose a rollback of exactly that release and get explicit approval.
5. After the rollback, watch fresh checkout traffic for the full verification window before resolving.
6. Save the handoff report and open a follow-up to fix the release before it ships again.

## Escalate when
- Errors continue after rolling back to the previous release.
- No deployment lines up with the spike (look at dependencies, traffic and configuration).
