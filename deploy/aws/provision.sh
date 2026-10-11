#!/usr/bin/env bash
# Provision OnCall on one EC2 t4g.micro with keyless Bedrock access (IAM role).
# Uses only the AWS CLI with your existing login. Idempotent: re-run safely.
#
#   ./deploy/aws/provision.sh            # create or reuse everything, print the host
#   ./deploy/aws/provision.sh --status   # show what exists
#
# Creates (all tagged Project=oncall):
#   IAM role + instance profile  oncall-bedrock   bedrock:InvokeModel on Amazon Nova Pro only
#   Security group               oncall-web       80/443 from anywhere, 22 from your current IP
#   Key pair                     oncall-key       private key saved to ~/.ssh/oncall-key.pem
#   EC2 instance                 oncall           t4g.micro, Ubuntu 24.04 arm64, 10 GB gp3, IMDSv2 required
#   Elastic IP                   oncall-ip        stable public address -> <ip>.sslip.io hostname
# Cost (us-east-1, Pricing API Oct 2026): t4g.micro $0.0084/h, gp3 $0.08/GB-mo, public IPv4 $0.005/h.
# Remove everything with ./deploy/aws/teardown.sh
set -euo pipefail
export MSYS_NO_PATHCONV=1 AWS_PAGER=""

REGION=${AWS_REGION:-us-east-1}
NAME=oncall
ROLE=oncall-bedrock
SG=oncall-web
KEY=oncall-key
KEY_FILE="$HOME/.ssh/$KEY.pem"
TYPE=${ONCALL_INSTANCE_TYPE:-t4g.micro}
MODEL=${ONCALL_BEDROCK_MODEL_ID:-us.amazon.nova-pro-v1:0}
TAGS="Key=Project,Value=oncall"
aws() { command aws --region "$REGION" "$@"; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

if [[ "${1:-}" == "--status" ]]; then
  aws ec2 describe-instances --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].{id:InstanceId,state:State.Name,ip:PublicIpAddress,type:InstanceType}' --output table
  aws ec2 describe-addresses --filters "Name=tag:Name,Values=$NAME-ip" --query 'Addresses[].{ip:PublicIp,instance:InstanceId}' --output table
  exit 0
fi

echo "==> IAM role $ROLE (Nova Pro invoke only, no stored keys)"
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --tags "$TAGS" --description "OnCall EC2: invoke Amazon Nova Pro only" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
fi
# The us.* inference profile routes to Nova Pro in several US regions; allow the
# profile and the underlying foundation model in those regions, nothing else.
POLICY=$(cat <<EOF
{"Version":"2012-10-17","Statement":[{"Sid":"NovaProOnly","Effect":"Allow",
 "Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],
 "Resource":["arn:aws:bedrock:*:$ACCOUNT:inference-profile/us.amazon.nova-pro-v1:0",
             "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0",
             "arn:aws:bedrock:us-east-2::foundation-model/amazon.nova-pro-v1:0",
             "arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-pro-v1:0"]}]}
EOF
)
aws iam put-role-policy --role-name "$ROLE" --policy-name nova-pro-invoke --policy-document "$POLICY"
if ! aws iam get-instance-profile --instance-profile-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$ROLE" --tags "$TAGS" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$ROLE" --role-name "$ROLE"
  sleep 10   # instance profiles take a few seconds to propagate
fi

echo "==> Security group $SG"
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SG_ID=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$SG" "Name=vpc-id,Values=$VPC" --query 'SecurityGroups[0].GroupId' --output text)
if [[ "$SG_ID" == "None" ]]; then
  SG_ID=$(aws ec2 create-security-group --group-name "$SG" --description "OnCall: HTTPS public, SSH admin only" --vpc-id "$VPC" \
    --tag-specifications "ResourceType=security-group,Tags=[{$TAGS}]" --query GroupId --output text)
  aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --ip-permissions \
    'IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0,Description=ACME+redirect}]' \
    'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0,Description=HTTPS}]' >/dev/null
fi
MY_IP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
[[ "$MY_IP" =~ ^[0-9.]+$ ]] || { echo "Could not determine your public IP" >&2; exit 1; }
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
  --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$MY_IP/32,Description=admin-ssh}]" >/dev/null 2>&1 || true

echo "==> Key pair $KEY"
mkdir -p "$HOME/.ssh"
if ! aws ec2 describe-key-pairs --key-names "$KEY" >/dev/null 2>&1; then
  aws ec2 create-key-pair --key-name "$KEY" --key-type ed25519 --tag-specifications "ResourceType=key-pair,Tags=[{$TAGS}]" \
    --query KeyMaterial --output text | tr -d '\r' > "$KEY_FILE"   # Windows AWS CLI emits CRLF; OpenSSH rejects it
  chmod 600 "$KEY_FILE"
fi
[[ -s "$KEY_FILE" ]] || { echo "Key pair $KEY exists in AWS but $KEY_FILE is missing; delete the key pair or restore the file" >&2; exit 1; }

echo "==> Instance $NAME ($TYPE, Ubuntu 24.04 arm64)"
INSTANCE=$(aws ec2 describe-instances --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
if [[ "$INSTANCE" == "None" ]]; then
  AMI=$(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id --query Parameter.Value --output text)
  INSTANCE=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$TYPE" --key-name "$KEY" --security-group-ids "$SG_ID" \
    --iam-instance-profile "Name=$ROLE" \
    --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled \
    --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=10,VolumeType=gp3,DeleteOnTermination=true,Encrypted=true}' \
    --credit-specification CpuCredits=standard \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{$TAGS}]" "ResourceType=volume,Tags=[{$TAGS}]" \
    --query 'Instances[0].InstanceId' --output text)
fi
aws ec2 wait instance-running --instance-ids "$INSTANCE"

echo "==> Elastic IP"
ALLOC=$(aws ec2 describe-addresses --filters "Name=tag:Name,Values=$NAME-ip" --query 'Addresses[0].AllocationId' --output text)
if [[ "$ALLOC" == "None" ]]; then
  ALLOC=$(aws ec2 allocate-address --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$NAME-ip},{$TAGS}]" --query AllocationId --output text)
fi
aws ec2 associate-address --allocation-id "$ALLOC" --instance-id "$INSTANCE" --allow-reassociation >/dev/null
IP=$(aws ec2 describe-addresses --allocation-ids "$ALLOC" --query 'Addresses[0].PublicIp' --output text)
HOST="${IP//./-}.sslip.io"

echo
echo "Instance: $INSTANCE   IP: $IP   Host: https://$HOST"
echo "SSH:      ssh -i $KEY_FILE ubuntu@$IP"
echo "Next:     ./deploy/aws/ship.sh $IP"
echo "$IP" > "$(dirname "$0")/.last-ip"
