#!/usr/bin/env python3
import aws_cdk as cdk
from stacks.ecs_migration_stack import EcsMigrationStack
from stacks.config import ACCOUNT, REGION

app = cdk.App()

phase      = app.node.try_get_context("phase")           or "1"
image      = app.node.try_get_context("image")           or "nginxdemos/hello:latest"
prod_port  = int(app.node.try_get_context("prod_port")   or "80")
native_port = int(app.node.try_get_context("native_port") or "8080")

# Phase 1: cdk deploy EcsMigrationStack
# Phase 2: cdk deploy EcsMigrationStack -c phase=2
# Phase 3: cdk deploy EcsMigrationStack -c phase=3   (port swap)
# Phase 4: cdk deploy EcsMigrationStack -c phase=4   (cleanup)

EcsMigrationStack(app, "EcsMigrationStack",
    phase=phase,
    image=image,
    prod_port=prod_port,
    native_port=native_port,
    env=cdk.Environment(account=ACCOUNT, region=REGION),
)

app.synth()
