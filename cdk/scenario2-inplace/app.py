#!/usr/bin/env python3
import aws_cdk as cdk
from stacks.ecs_bg_stack import EcsBgStack
from stacks.config import ACCOUNT, REGION

app = cdk.App()

image = app.node.try_get_context("image") or "nginxdemos/hello:latest"
deployment_type = app.node.try_get_context("deployment_type") or "codedeploy"

EcsBgStack(app, "EcsBgStack",
    image=image,
    deployment_type=deployment_type,
    env=cdk.Environment(account=ACCOUNT, region=REGION),
)

app.synth()
