# ECS Blue/Green Migration Demo

## CodeDeploy → ECS Native (CDK Python)

Demonstrates two approaches to migrating ECS CodeDeploy Blue/Green to ECS Native Blue/Green.
Both use CDK Python, single stack, no direct CLI infrastructure commands, no drift.

**The core constraint:** `DeploymentController` is an **immutable property** on `AWS::ECS::Service`.
You cannot update it in-place — CloudFormation must replace the service. The two scenarios
differ in *how* they handle that replacement.

**AWS references:**

- [Migrate CodeDeploy BG to ECS BG](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/migrate-codedeploy-to-ecs-bluegreen.html)
- [AWS blog: Migrating from CodeDeploy to ECS for blue/green deployments](https://aws.amazon.com/blogs/containers/migrating-from-aws-codedeploy-to-amazon-ecs-for-blue-green-deployments/)

---

## Scenario 1 — Parallel Services, Port Swap ⭐ Recommended for Production

**Location:** `cdk/scenario1-single-stack-portswap/`

**Best for:** Production. Validates the native service in parallel before any production traffic
moves. Mirrors AWS blog Option 2.

### The key difference vs Scenario 2

Both services **run simultaneously** — CodeDeploy on port 80, native ECS on port 8080.
You validate the native service is healthy before touching production traffic. Cutover is
a listener port change (metadata update only — no task replacement, no health check wait).

```
Phase 1:  port 80   → CodeDeploy service      (production)
          [only one service running]

Phase 2:  port 80   → CodeDeploy service      (production — completely untouched)
          port 8080 → Native ECS service      (NEW — both services running in parallel)
          ↑ Validate here. Port 8080 must return 200 before proceeding.

Phase 2b: port 9999 → CodeDeploy service      (moved off port 80 — frees it)
          port 8080 → Native ECS service      (still test)

Phase 3b: port 80   → Native ECS service      (production — cutover complete)
          CodeDeploy service + all its resources deleted
```

**Why Phase 2b?** ALB rejects two simultaneous port changes when both ports are occupied.
Phase 2b moves CodeDeploy to a temp port 9999 first (frees port 80), then Phase 3b moves
native to port 80. One listener port change per deploy — no conflict.

### Steps

```bash
cd cdk/scenario1-single-stack-portswap
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk bootstrap aws://760221989975/us-west-2 --profile 9975

# Phase 1 — Deploy CodeDeploy BG (initial state)
cdk deploy EcsMigrationStack --profile 9975 --require-approval never

# Phase 2 — Deploy native ECS service alongside on port 8080
cdk deploy EcsMigrationStack -c phase=2 --profile 9975 --require-approval never
curl http://<ALBDNSName>:8080   # ← MUST return 200 before proceeding

# Phase 2b — Free port 80 (move CodeDeploy to temp port 9999)
cdk deploy EcsMigrationStack -c phase=2b --profile 9975 --require-approval never

# Phase 3b — Cutover: native ECS takes over port 80
cdk deploy EcsMigrationStack -c phase=3b --profile 9975 --require-approval never
curl http://<ALBDNSName>        # ← native ECS serving production
```

### Rollback

| Point          | Rollback action                                               |
| -------------- | ------------------------------------------------------------- |
| After Phase 2  | `cdk deploy -c phase=1` — removes native service           |
| After Phase 2b | `cdk deploy -c phase=2` — moves CodeDeploy back to port 80 |
| After Phase 3b | CodeDeploy deleted — revert via ECS service revision         |

See [`cdk/scenario1-single-stack-portswap/README.md`](cdk/scenario1-single-stack-portswap/README.md) for full details including cleanup instructions.

---

## Scenario 2 — In-Place Service Replacement (~30 sec downtime)

**Location:** `cdk/scenario2-inplace/`

**Best for:** Dev/staging, or production where a brief downtime window is acceptable.
Simplest approach — single context flag triggers the migration.

### The key difference vs Scenario 1

Only **one service exists at any time** — no parallel validation. The stack has two states
controlled by `deployment_type`. Switching from `codedeploy` to `native` causes CloudFormation
to replace the ECS service: creates the new native service, waits for health checks (~30 sec),
then deletes the old CodeDeploy service. That window is the downtime.

```
State 1 — deployment_type=codedeploy (default)
  Resources: CfnService (EXTERNAL) + CfnTaskSet + PrimaryTaskSet
  Green deploys: CFN transform intercepts stack updates, CodeDeploy orchestrates shift

State 2 — deployment_type=native
  Resources: FargateService (ECS controller) + Strategy: BLUE_GREEN
  ServiceName omitted → CFN replaces service → ~30 sec gap
  Green deploys: cdk deploy with new image → ECS handles shift via UpdateService
```

### Steps

```bash
cd cdk/scenario2-inplace
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk bootstrap aws://760221989975/us-west-2 --profile 9975

# Step 1 — Deploy initial CodeDeploy BG state
cdk deploy EcsBgStack --profile 9975 --require-approval never

# Step 2 — Demo: green deploy via CodeDeploy (optional, shows CodeDeploy BG in action)
cdk deploy EcsBgStack -c image=nginxdemos/hello:plain-text --profile 9975 --require-approval never

# Step 3 — MIGRATION: switch to ECS native (~30 sec downtime)
cdk deploy EcsBgStack -c deployment_type=native --profile 9975 --require-approval never

# Step 4 — Demo: green deploy via ECS native (optional, shows ECS native BG in action)
cdk deploy EcsBgStack -c deployment_type=native -c image=nginx:alpine --profile 9975 --require-approval never
```

### Why ServiceName is omitted in native mode

If `service_name` is hardcoded, CFN tries to create the new native service with the same
name while the old CodeDeploy service still exists → `AlreadyExists` error. Omitting it
lets CFN auto-generate a unique name for the replacement service. The ALB connects via
target group ARN, not service name, so routing is unaffected.

### Rollback

Redeploy with `deployment_type=codedeploy` — CFN replaces back to the CodeDeploy service.
Same ~30 second gap applies.

---

## Scenario Comparison

|                                             | **Scenario 1**         | **Scenario 2**        |
| ------------------------------------------- | ---------------------------- | --------------------------- |
| **Services running during migration** | Two (parallel)               | One at a time               |
| **Validate before cutover**           | Yes — native on port 8080   | No — straight replacement  |
| **Downtime**                          | ~1-2 sec (Phase 2b only)     | ~30 sec (Step 3)            |
| **Rollback before cutover**           | Easy — CodeDeploy untouched | N/A — no parallel service  |
| **Rollback after cutover**            | ECS service revision revert  | ECS service revision revert |
| **CDK deploys to migrate**            | 4                            | 2                           |
| **Stack count**                       | 1                            | 1                           |
| **Mirrors AWS blog Option 2**         | Yes                          | No (in-place)               |
| **Recommended for**                   | Production                   | Dev/staging                 |

---

## Repository Layout

```
cdk/
├── scenario1-single-stack-portswap/   ← Production — parallel services, port swap  
│   ├── README.md                      ← full step-by-step instructions
│   ├── app.py
│   ├── requirements.txt
│   └── stacks/
│       ├── config.py
│       └── ecs_migration_stack.py    ← all phases + both cutover approaches (A and B)
│
├── scenario3-parallel/                ← Reference only — two stacks (not tested end-to-end)
│
└── scenario2-inplace/                 ← Dev/staging — in-place replacement  
    ├── app.py
    ├── requirements.txt
    └── stacks/
        ├── config.py
        └── ecs_bg_stack.py

CUSTOMER-QA.md                         ← Customer Q&A with CDK solution
DEMO-APPROACHES.md                     ← Background notes on all approaches explored
```

> **Note on cleanup:** `cdk destroy` may fail with a cluster deletion race condition.
> If it does, run: `aws cloudformation delete-stack --stack-name <name> --region us-west-2 --profile 9975`
