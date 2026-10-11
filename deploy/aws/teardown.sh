#!/usr/bin/env bash
# Delete every AWS resource provision.sh created. Charges stop when this finishes.
# Destroys the server and its data (incidents, audit). Take a backup first if needed:
#   scp -i ~/.ssh/oncall-key.pem ubuntu@<ip>:/var/backups/oncall/* .
#
#   ./deploy/aws/teardown.sh --yes
set -euo pipefail
export MSYS_NO_PATHCONV=1 AWS_PAGER=""
[[ "${1:-}" == "--yes" ]] || { echo "This permanently deletes the OnCall server and its data. Re-run with --yes." >&2; exit 1; }
REGION=${AWS_REGION:-us-east-1}
aws() { command aws --region "$REGION" "$@"; }

INSTANCE=$(aws ec2 describe-instances --filters Name=tag:Name,Values=oncall Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
ALLOC=$(aws ec2 describe-addresses --filters Name=tag:Name,Values=oncall-ip --query 'Addresses[0].AllocationId' --output text)
ASSOC=$(aws ec2 describe-addresses --filters Name=tag:Name,Values=oncall-ip --query 'Addresses[0].AssociationId' --output text)
[[ "$ASSOC" != "None" ]] && aws ec2 disassociate-address --association-id "$ASSOC"
[[ "$ALLOC" != "None" ]] && aws ec2 release-address --allocation-id "$ALLOC" && echo "released Elastic IP"
if [[ "$INSTANCE" != "None" ]]; then
  aws ec2 terminate-instances --instance-ids "$INSTANCE" >/dev/null
  aws ec2 wait instance-terminated --instance-ids "$INSTANCE" && echo "terminated $INSTANCE"
fi
SG_ID=$(aws ec2 describe-security-groups --filters Name=group-name,Values=oncall-web --query 'SecurityGroups[0].GroupId' --output text)
[[ "$SG_ID" != "None" ]] && aws ec2 delete-security-group --group-id "$SG_ID" && echo "deleted security group"
aws ec2 delete-key-pair --key-name oncall-key >/dev/null 2>&1 && echo "deleted key pair (local ~/.ssh/oncall-key.pem kept)"
if aws iam get-instance-profile --instance-profile-name oncall-bedrock >/dev/null 2>&1; then
  aws iam remove-role-from-instance-profile --instance-profile-name oncall-bedrock --role-name oncall-bedrock 2>/dev/null || true
  aws iam delete-instance-profile --instance-profile-name oncall-bedrock
fi
aws iam delete-role-policy --role-name oncall-bedrock --policy-name nova-pro-invoke 2>/dev/null || true
aws iam delete-role --role-name oncall-bedrock 2>/dev/null && echo "deleted IAM role"
echo "Done. Nothing billable remains from OnCall."
