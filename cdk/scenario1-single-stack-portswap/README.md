# Scenario 1 — Single Stack Port Swap Migration

Migrates ECS CodeDeploy Blue/Green to ECS Native Blue/Green in a **single CDK stack**
using the AWS-documented "New service with existing load balancer" approach (Option 2 from
the [AWS blog](https://aws.amazon.com/blogs/containers/migrating-from-aws-codedeploy-to-amazon-ecs-for-blue-green-deployments/)).

## Why this scenario

The customer has everything in one CDK stack. They cannot do a simple in-place update
because `DeploymentController` is an immutable property on `AWS::ECS::Service` — CFN must
replace the service. This scenario shows how to do that with zero production downtime.

## Key insight: the intermediate port trick

The AWS blog says "swap the ports of the original and new listeners" but doesn't explain
the mechanics. A direct simultaneous swap fails — ALB rejects it because both ports are
already in use. The solution is a **three-step sequence**:

```
Phase 2b: Move CodeDeploy listener 80 → 9999  (frees port 80)
Phase 3b: Move Native listener   8080 → 80    (takes over port 80)
```

Each CDK deploy changes only one listener port — no conflict. Port 9999 is a temporary
holding port used only during Phase 2b.

## Architecture

```
Phase 1 (initial state):
  ALB
   └── Listener port 80 → CodeDeploy TG blue/green
       ECS Service: EXTERNAL controller + TaskSet

Phase 2 (parallel state — validate before touching production):
  ALB
   ├── Listener port 80   → CodeDeploy TG  (production, untouched)
   └── Listener port 8080 → Native TG      (test, validate here)
       New ECS Service: ECS controller + BLUE_GREEN

Phase 2b (free port 80 — single listener port update):
  ALB
   ├── Listener port 9999 → CodeDeploy TG  (moved off 80, production gap = 0)
   └── Listener port 8080 → Native TG      (still test)

Phase 3b (port swap cutover — single listener port update):
  ALB
   └── Listener port 80   → Native TG      (production)
       CodeDeploy service + resources deleted

Final state:
  ALB
   └── Listener port 80 → Native ECS service (BLUE_GREEN, production)
```

## Prerequisites

```bash
# AWS CDK bootstrapped in account/region
cdk bootstrap aws://<account>/<region> --profile <profile>

# Python 3 + venv
cd cdk/scenario1-single-stack-portswap
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Migration Steps

### Phase 1 — Deploy CodeDeploy BG (initial state)

```bash
cdk deploy EcsMigrationStack --profile 9975 --require-approval never
```

**What deploys:**
- VPC lookup, Security Group (ports 80, 8080, 9999)
- ALB + Listener port 80 → CodeDeploy TGs (blue/green)
- ECS Cluster + Fargate Task Definition (`nginxdemos/hello:latest`)
- ECS Service (`DeploymentController: EXTERNAL`)
- `AWS::ECS::TaskSet` + `AWS::ECS::PrimaryTaskSet`

**Verify:**
```bash
curl -s -o /dev/null -w "%{http_code}" http://<ALBDNSName>
# Expected: 200
```

---

### Phase 2 — Add native ECS service on port 8080

```bash
cdk deploy EcsMigrationStack -c phase=2 --profile 9975 --require-approval never
```

**What changes:**
- Adds `NativeTestListener` on port 8080 (new listener, CodeDeploy on port 80 untouched)
- Adds native TGs (blue + green)
- Creates new `FargateService` with `DeploymentController: ECS` + `Strategy: BLUE_GREEN`
- Adds `ECSInfrastructureRoleForLoadBalancers` IAM role

**Verify — validate native service before touching production:**
```bash
ALB=<ALBDNSName>
# Production still on CodeDeploy
curl -s -o /dev/null -w "%{http_code}" http://$ALB        # 200
# Native service on test port
curl -s -o /dev/null -w "%{http_code}" http://$ALB:8080   # 200
```

> **Do not proceed to Phase 2b until port 8080 returns 200.**
> Run integration tests against port 8080 here — this is your last safe checkpoint.

---

### Phase 2b — Move CodeDeploy listener to temp port (frees port 80)

```bash
cdk deploy EcsMigrationStack -c phase=2b --profile 9975 --require-approval never
```

**What changes:**
- `CDListener` port updated from **80 → 9999** (single CFN UPDATE on existing listener)
- Port 80 is now free — no listener on it
- Native service still on port 8080

**Note:** There is a brief window (~1-2 seconds) where port 80 has no listener while CFN
applies the update. This is the "minimal disruption" AWS describes. Existing connections
drain gracefully; new connections may get a brief 502 during this window.

**Verify:**
```bash
# Port 80 now free
curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://$ALB        # 000/refused
# CodeDeploy still reachable on temp port
curl -s -o /dev/null -w "%{http_code}" http://$ALB:9999   # 200
# Native still on test port
curl -s -o /dev/null -w "%{http_code}" http://$ALB:8080   # 200
```

**Rollback at this point:**
```bash
# Simply redeploy phase 2 — moves CDListener back to port 80
cdk deploy EcsMigrationStack -c phase=2 --profile 9975 --require-approval never
```

---

### Phase 3b — Port swap (cutover — native takes port 80)

```bash
cdk deploy EcsMigrationStack -c phase=3b --profile 9975 --require-approval never
```

**What changes in one CFN changeset:**
- `NativeTestListener` port updated from **8080 → 80** (single CFN UPDATE)
- `CDListener` (on port 9999) DELETED
- CodeDeploy service, TaskSet, PrimaryTaskSet, TGs DELETED
- Native service `AdvancedConfiguration.ProductionListenerRule` updated

**Verify:**
```bash
# Native ECS serving production on port 80
curl -s -o /dev/null -w "%{http_code}" http://$ALB        # 200
# Port 8080 gone
curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://$ALB:8080  # 000/refused

# Confirm native service
aws ecs describe-services \
  --cluster ecs-bg-demo-cluster \
  --services $(aws ecs list-services \
    --cluster ecs-bg-demo-cluster \
    --region us-west-2 --profile 9975 \
    --query 'serviceArns[0]' --output text) \
  --region us-west-2 --profile 9975 \
  --query 'services[0].{Controller:deploymentController.type,Strategy:deploymentConfiguration.strategy}' \
  --output json
# Expected: {"Controller": "ECS", "Strategy": "BLUE_GREEN"}
```

**Rollback at this point:**
Once Phase 3b is complete, CodeDeploy resources are deleted. Rollback is a service
revision revert:
```bash
aws ecs update-service \
  --cluster ecs-bg-demo-cluster \
  --service <service-name> \
  --task-definition <previous-task-def-arn> \
  --region us-west-2 --profile 9975
```

---

### Test an ECS native blue/green deployment (Phase 4)

Once on native, trigger a deployment by updating the image:

```bash
cdk deploy EcsMigrationStack -c phase=3b -c image=nginx:alpine \
  --profile 9975 --require-approval never
```

ECS handles the blue/green shift internally — creates green tasks, shifts traffic,
runs bake time (1 min), terminates blue tasks.

---

## Cleanup

```bash
cdk destroy EcsMigrationStack --profile 9975 --force
```

---

## Comparison: Two Approaches

This scenario implements both cutover mechanisms. Choose based on your preference:

| | **Approach A** — Listener Rewire | **Approach B** — Port Swap |
|---|---|---|
| Phases | 1 → 2 → 3 | 1 → 2 → 2b → 3b |
| CDK deploys | 3 | 4 |
| Mirrors AWS blog? | No (different mechanism) | **Yes** |
| Port 80 gap | None | ~1-2 sec in Phase 2b |
| Intuitive? | Less obvious | Matches mental model |
| CDK only? | Yes | Yes |
| Drift risk | None | None |

**Use Approach A** if you want fewer deploys and can accept a non-obvious mechanism.
**Use Approach B** if you want to follow the AWS-documented pattern exactly.

### Approach A commands (listener rewire)
```bash
cdk deploy EcsMigrationStack                  # Phase 1
cdk deploy EcsMigrationStack -c phase=2       # Phase 2
cdk deploy EcsMigrationStack -c phase=3       # Phase 3 (rewire)
```

### Approach B commands (port swap — AWS blog Option 2)
```bash
cdk deploy EcsMigrationStack                  # Phase 1
cdk deploy EcsMigrationStack -c phase=2       # Phase 2
cdk deploy EcsMigrationStack -c phase=2b      # Phase 2b (free port 80)
cdk deploy EcsMigrationStack -c phase=3b      # Phase 3b (port swap)
```

---

## How it differs from the AWS blog

The AWS blog describes the port swap approach conceptually but gives no implementation
details. The challenge: you cannot do both port changes simultaneously (ALB rejects it)
and you cannot do them sequentially within one CFN changeset (CFN processes them in
parallel). The solution is splitting them across two separate CDK deploys (Phase 2b and
Phase 3b), each changing only one listener port.

AWS blog reference:
https://aws.amazon.com/blogs/containers/migrating-from-aws-codedeploy-to-amazon-ecs-for-blue-green-deployments/

AWS developer guide:
https://docs.aws.amazon.com/AmazonECS/latest/developerguide/migrate-codedeploy-to-ecs-bluegreen.html
