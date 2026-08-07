import os

# ── Required — set these before deploying ──────────────────────────────────
# Option 1: export as shell environment variables before running cdk deploy
#   export CDK_ACCOUNT=123456789012
#   export CDK_REGION=us-east-1
#   export VPC_ID=vpc-xxxxxxxxxxxxxxxxx
#   export SUBNET1=subnet-xxxxxxxxxxxxxxxxx
#   export SUBNET2=subnet-xxxxxxxxxxxxxxxxx
#
# Option 2: CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION are set automatically
#   when you run: cdk deploy --profile <your-profile>

ACCOUNT = os.environ.get("CDK_ACCOUNT", os.environ.get("CDK_DEFAULT_ACCOUNT", ""))
REGION  = os.environ.get("CDK_REGION",  os.environ.get("CDK_DEFAULT_REGION",  "us-east-1"))

VPC_ID  = os.environ.get("VPC_ID",  "")
SUBNET1 = os.environ.get("SUBNET1", "")
SUBNET2 = os.environ.get("SUBNET2", "")

# AZs derived from region — override with AZ1/AZ2 env vars if needed
AZ1 = os.environ.get("AZ1", f"{REGION}a")
AZ2 = os.environ.get("AZ2", f"{REGION}b")

CLUSTER_NAME = "ecs-bg-demo-cluster"
FAMILY       = "ecs-bg-demo"

if not all([ACCOUNT, VPC_ID, SUBNET1, SUBNET2]):
    raise ValueError(
        "Missing required configuration. Set these environment variables:\n"
        "  CDK_ACCOUNT (or CDK_DEFAULT_ACCOUNT)\n"
        "  VPC_ID\n"
        "  SUBNET1\n"
        "  SUBNET2"
    )
