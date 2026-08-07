#!/usr/bin/env python3
import aws_cdk as cdk
from stacks.ecs_bg_stack import EcsBgStack
from stacks.native_stack import EcsNativeStack
from stacks.config import ACCOUNT, REGION

app = cdk.App()

image = app.node.try_get_context("image") or "nginxdemos/hello:latest"
prod_port = int(app.node.try_get_context("prod_port") or "80")
native_port = int(app.node.try_get_context("native_port") or "8080")

env = cdk.Environment(account=ACCOUNT, region=REGION)

# Phase 1 & 2: cdk deploy EcsBgStack
# Phase 3:     cdk deploy EcsNativeStack  (deploys alongside, on native_port)
# Phase 4:     cdk deploy --all -c prod_port=8080 -c native_port=80  (port swap)
EcsBgStack(app, "EcsBgStack",
    image=image,
    prod_port=prod_port,
    env=env,
)

EcsNativeStack(app, "EcsNativeStack",
    image=image,
    native_port=native_port,
    env=env,
)

app.synth()
