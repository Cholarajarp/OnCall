#!/usr/bin/env bash
# Free AWS Budget that emails you at 50%, 80% and 100% of a monthly limit.
#   ./deploy/aws/budget.sh you@example.com [limit-usd, default 20]
set -euo pipefail
export MSYS_NO_PATHCONV=1 AWS_PAGER=""
EMAIL=${1:?email required}
[[ "$EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || { echo "Invalid email" >&2; exit 1; }
LIMIT=${2:-20}
[[ "$LIMIT" =~ ^[0-9]+$ ]] || { echo "Limit must be whole dollars" >&2; exit 1; }
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
NOTIFS=""
for pct in 50 80 100; do
  NOTIFS+="{\"Notification\":{\"NotificationType\":\"ACTUAL\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":$pct,\"ThresholdType\":\"PERCENTAGE\"},\"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$EMAIL\"}]},"
done
aws budgets delete-budget --account-id "$ACCOUNT" --budget-name oncall-monthly >/dev/null 2>&1 || true
aws budgets create-budget --account-id "$ACCOUNT" \
  --budget "{\"BudgetName\":\"oncall-monthly\",\"BudgetLimit\":{\"Amount\":\"$LIMIT\",\"Unit\":\"USD\"},\"TimeUnit\":\"MONTHLY\",\"BudgetType\":\"COST\"}" \
  --notifications-with-subscribers "[${NOTIFS%,}]"
echo "Budget oncall-monthly: \$$LIMIT/month, alerts to $EMAIL at 50/80/100%."
