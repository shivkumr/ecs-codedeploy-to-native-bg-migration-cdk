# Demo Approaches — ECS Blue/Green Migration

Two CDK Python scenarios were built and tested to demonstrate migrating from
CodeDeploy Blue/Green to ECS Native Blue/Green.

Both scenarios use a single CDK stack and mirror a customer's real setup —
everything in one stack: VPC, ALB, ECS cluster, and ECS service with CodeDeploy BG.

---

## Scenario 1 — Single Stack Port Swap (Production)

**Location:** `cdk/scenario1-single-stack-portswap/`

**Concept:** Deploy the native ECS service alongside the existing CodeDeploy
service in the same stack using a second listener on port 8080. Validate before
cutover. Cutover is a sequential pair of listener port changes — no simultaneous
port conflict, no task replacement gap.

### Stack structure

```
EcsMigrationStack (single stack, all phases)
─────────────────────────────────────────────
VPC (lookup)
Security Group (ports 80, 8080, 9999)
ALB
ECS Cluster
Task Execution Role

--- Phases 1 & 2 (codedeploy resources) ---
  CDListener       port 80   → CodeDeploy TGs
  CfnService       DeploymentController: EXTERNAL
  CfnTaskSet + PrimaryTaskSet
  CodeDeploy TG blue + green

--- Phase 2 (adds alongside codedeploy) ---
  NativeTestListener  port 8080 → Native TGs
  NativeService       DeploymentController: ECS + BLUE_GREEN
  Native TG blue + green
  ECSInfrastructureRole

--- Phase 2b (CDListener moves to temp port) ---
  CDListener       port 9999  → CodeDeploy TGs  (freed port 80)
  NativeTestListener  port 8080 → Native TGs

--- Phase 3b (cutover complete) ---
  NativeTestListener  port 80   → Native TGs    (production)
  CodeDeploy resources deleted
```

### All 4 phases

| Phase | Command | What happens |
|---|---|---|
| 1 | `cdk deploy EcsMigrationStack` | CodeDeploy BG service deployed on port 80 |
| 2 | `cdk deploy EcsMigrationStack -c phase=2` | Native ECS service added on port 8080 — validate here |
| 2b | `cdk deploy EcsMigrationStack -c phase=2b` | CDListener moves to port 9999 — port 80 freed |
| 3b | `cdk deploy EcsMigrationStack -c phase=3b` | Native listener moves to port 80 — cutover complete, CodeDeploy deleted |

### Port swap explained

```
Phase 2:   port 80   → CodeDeploy    port 8080 → Native (test)
Phase 2b:  port 9999 → CodeDeploy    port 8080 → Native (test, port 80 now free)
Phase 3b:  [deleted] → CodeDeploy    port 80   → Native (production)
```

Each phase changes only one listener port — avoids ALB's "port already in use"
error that occurs when both ports are changed simultaneously.

### Rollback

| Point | Rollback |
|---|---|
| After Phase 2 | `cdk deploy -c phase=1` — removes native service |
| After Phase 2b | `cdk deploy -c phase=2` — moves CDListener back to port 80 |
| After Phase 3b | CodeDeploy deleted — revert via ECS service revision |

### Trade-offs

| | Detail |
|---|---|
| Downtime | ~1-2 sec during Phase 2b (port 80 briefly has no listener) |
| Rollback | Easy at any point up to Phase 3b |
| CDK drift | None — CDK owns all listener port values |
| Stack count | 1 — no cross-stack dependencies, clean deletion |
| Complexity | Medium — 4 CDK deploys, intermediate port required |

---

## Scenario 2 — In-Place Update (~30 second downtime)

**Location:** `cdk/scenario2-inplace/`

**Concept:** Single stack throughout. Migration is controlled by a CDK context
flag `deployment_type`. When switched to `native`, the stack removes the
`service_name` from the ECS service construct — this allows CloudFormation to
replace the service (create new auto-named service → delete old service) without
hitting the `AlreadyExists` error caused by the immutable `DeploymentController`
property.

### Stack structure

```
EcsBgStack (single stack, both modes)
──────────────────────────────────────
VPC (lookup)
Security Group
ALB
ECS Cluster
Target Group Blue
Target Group Green
Task Execution Role

--- CodeDeploy mode (deployment_type=codedeploy) ---
  Listener port 80 → Blue TG only
  CfnService (DeploymentController: EXTERNAL)
    service_name = "ecs-bg-demo-service"   ← hardcoded
  CfnTaskSet (blue)
  CfnPrimaryTaskSet

--- Native mode (deployment_type=native) ---
  Listener port 80 → Blue TG (weight=1) + Green TG (weight=0)
  ALBListenerProdRule (required by AdvancedConfiguration)
  ECS Infrastructure Role (for ALB API calls)
  FargateService (DeploymentController: ECS)
    service_name = OMITTED              ← CFN auto-names → allows replacement
    DeploymentConfiguration.Strategy = BLUE_GREEN
    LoadBalancers.AdvancedConfiguration:
      AlternateTargetGroupArn → Green TG
      ProductionListenerRule  → ListenerRule ARN
      RoleArn                 → ECS Infrastructure Role
```

### Two states, four commands

The stack has two structural states. Steps 2 and 4 are optional green deploy demos
within each state — they are not part of the migration itself.

| Step | Command | State | What happens |
|---|---|---|---|
| 1 | `cdk deploy EcsBgStack` | `codedeploy` | Initial deploy — CodeDeploy BG service on port 80 |
| 2 *(demo)* | `cdk deploy EcsBgStack -c image=nginxdemos/hello:plain-text` | `codedeploy` | Green deploy via CFN transform — shows CodeDeploy BG in action |
| 3 | `cdk deploy EcsBgStack -c deployment_type=native` | `native` | **Migration** — CFN replaces ECS service, ~30 sec gap |
| 4 *(demo)* | `cdk deploy EcsBgStack -c deployment_type=native -c image=nginx:alpine` | `native` | Green deploy via ECS native — shows ECS native BG in action |

Steps 1 and 3 are the only mandatory steps. Steps 2 and 4 demonstrate each
deployment mechanism before and after migration respectively.

### Why there is ~30 second downtime in Step 3 (the migration)

`DeploymentController` is an immutable property. CFN cannot update it — it must
replace the ECS service:

```
1. CFN creates new service (auto-named, ECS controller) → registers with ALB
2. ALB health checks new tasks → ~20-30 seconds
3. CFN deletes old service (EXTERNAL controller) → deregisters from ALB
```

During step 2 above, new tasks are starting but not yet healthy. Existing tasks on the
old service are still running. However, there is a brief window during the
transition where the ALB may return 502/503 to some requests.

### Why service_name is omitted in native mode

If `service_name` is hardcoded (e.g. `ecs-bg-demo-service`) in both modes,
CFN tries to create the new service with the same name while the old one still
exists → `AlreadyExists` error.

Omitting `service_name` lets CFN auto-generate a unique name for the replacement
service, avoiding the conflict. The ALB connection is via target group ARN, not
service name — so the auto-generated name has no impact on routing.

### Rollback

```bash
# Revert the service revision via ECS
aws ecs update-service \
  --cluster ecs-bg-demo-cluster \
  --service <service-arn> \
  --task-definition <previous-task-def-arn>

# Or redeploy previous CDK state
cdk deploy EcsBgStack -c deployment_type=codedeploy
```

### Trade-offs

| | Detail |
|---|---|
| Downtime | ~30 seconds during Phase 3 (service replacement) |
| Rollback | Service revision revert or redeploy previous context |
| CDK drift | None — CDK owns all resources |
| Complexity | Low — single stack, single context flag |
| Best for | Dev/staging, or production where brief downtime is acceptable |

---

## Side-by-Side Comparison

| | Scenario 1 (Port Swap) | Scenario 2 (In-Place) |
|---|---|---|
| Stacks | 1 | 1 |
| Services during migration | Two (parallel) | One at a time |
| Validate before cutover | Yes — native on port 8080 | No |
| Downtime during migration | ~1-2 sec (Phase 2b) | ~30 seconds (Phase 3) |
| Cutover mechanism | Listener port change | CFN service replacement |
| Rollback before cutover | Easy — CodeDeploy untouched | N/A |
| Rollback after cutover | Service revision revert | Service revision revert |
| CDK drift risk | None | None |
| Complexity | Medium | Low |
| Mirrors AWS blog Option 2 | Yes | No (in-place) |
| Production recommended | Yes | Dev/staging only |
| Status | ✅ Tested end-to-end | ✅ Tested end-to-end |

---

## Key CDK Patterns Used

### L1 constructs for CodeDeploy BG
ECS does not have L2 support for `DeploymentController: EXTERNAL`. Must use
L1 (`CfnService`, `CfnTaskSet`, `CfnPrimaryTaskSet`) directly.

### L2 + L1 escape hatch for ECS Native BG
`FargateService` (L2) is used for the native service. `AdvancedConfiguration`
on the `LoadBalancers` property is not yet supported in L2 — applied via
L1 escape hatch:

```python
cfn_service = service.node.default_child
cfn_service.add_property_override("DeploymentConfiguration.Strategy", "BLUE_GREEN")
cfn_service.add_property_override("DeploymentConfiguration.BakeTimeInMinutes", 1)
cfn_service.add_property_override("LoadBalancers", [{
    "ContainerName": "nginx",
    "ContainerPort": 80,
    "TargetGroupArn": blue_tg.target_group_arn,
    "AdvancedConfiguration": {
        "AlternateTargetGroupArn": green_tg.target_group_arn,
        "ProductionListenerRule": listener_rule.listener_rule_arn,
        "RoleArn": infra_role.role_arn,
    },
}])
```

### Context flags for phase transitions
Both scenarios use CDK context values (`-c key=value`) to drive phase
transitions without modifying code files:

```bash
# Scenario 1 — port swap phases
cdk deploy EcsMigrationStack -c phase=2
cdk deploy EcsMigrationStack -c phase=2b
cdk deploy EcsMigrationStack -c phase=3b

# Scenario 2 — migration trigger
cdk deploy EcsBgStack -c deployment_type=native -c image=nginx:alpine
```
