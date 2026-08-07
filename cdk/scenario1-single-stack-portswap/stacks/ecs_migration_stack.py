from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
    aws_ecs as ecs,
    aws_iam as iam,
)
from constructs import Construct
from .config import VPC_ID, SUBNET1, SUBNET2, AZ1, AZ2, CLUSTER_NAME, FAMILY

# Migration phase transitions — two cutover approaches supported:
#
# ── Approach A: Listener DefaultAction Rewire (our approach, fewer deploys) ──
#   Phase 1  → Phase 2  → Phase 3
#
# ── Approach B: Port Swap (mirrors AWS blog Option 2, extra intermediate step) ──
#   Phase 1  → Phase 2  → Phase 2b → Phase 3b
#
# Commands:
#   Phase 1:  cdk deploy EcsMigrationStack                      CodeDeploy BG on port 80
#   Phase 2:  cdk deploy EcsMigrationStack -c phase=2           Native service on port 8080
#   Phase 2b: cdk deploy EcsMigrationStack -c phase=2b          Move CodeDeploy to port 9999 (frees port 80)
#   Phase 3:  cdk deploy EcsMigrationStack -c phase=3           Rewire port 80 listener to native TGs
#   Phase 3b: cdk deploy EcsMigrationStack -c phase=3b          Port swap: move native from 8080 to 80
#   Phase 4:  cdk deploy EcsMigrationStack -c phase=4           Final cleanup

# Phase ordering for comparisons
PHASE_ORDER = {"1": 1, "2": 2, "2b": 2.5, "3": 3, "3b": 3.5, "4": 4}
TEMP_PORT = 9999  # intermediate port used in phase 2b to free port 80


class EcsMigrationStack(Stack):
    def __init__(self, scope: Construct, construct_id: str,
                 phase: str, image: str, prod_port: int, native_port: int,
                 **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        p = PHASE_ORDER.get(phase, 1)

        # ── Shared infrastructure (all phases) ──────────────────────────────

        vpc = ec2.Vpc.from_vpc_attributes(
            self, "Vpc",
            vpc_id=VPC_ID,
            availability_zones=[AZ1, AZ2],
            public_subnet_ids=[SUBNET1, SUBNET2],
        )

        sg = ec2.SecurityGroup(self, "EcsSg",
            vpc=vpc,
            description="ECS blue/green demo",
            allow_all_outbound=True,
        )
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80))
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(8080))
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(TEMP_PORT))

        alb = elbv2.ApplicationLoadBalancer(self, "ALB",
            vpc=vpc,
            internet_facing=True,
            security_group=sg,
            vpc_subnets=ec2.SubnetSelection(subnets=[
                ec2.Subnet.from_subnet_id(self, "Sub1", SUBNET1),
                ec2.Subnet.from_subnet_id(self, "Sub2", SUBNET2),
            ]),
            load_balancer_name="ecs-bg-demo-alb",
        )

        cluster = ecs.CfnCluster(self, "Cluster",
            cluster_name=CLUSTER_NAME,
        )

        execution_role = iam.Role(self, "TaskExecRole",
            role_name="ecs-bg-demo-task-execution-role",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )

        # ── CodeDeploy BG target groups (phases 1, 2, 2b) ───────────────────

        if p <= 2.5:
            cd_blue_tg = elbv2.ApplicationTargetGroup(self, "CDBlueTG",
                vpc=vpc, port=80, protocol=elbv2.ApplicationProtocol.HTTP,
                target_type=elbv2.TargetType.IP,
                health_check=elbv2.HealthCheck(
                    path="/", healthy_http_codes="200",
                    interval=Duration.seconds(10), timeout=Duration.seconds(5),
                    healthy_threshold_count=2, unhealthy_threshold_count=3,
                ),
            )
            cd_green_tg = elbv2.ApplicationTargetGroup(self, "CDGreenTG",
                vpc=vpc, port=80, protocol=elbv2.ApplicationProtocol.HTTP,
                target_type=elbv2.TargetType.IP,
                health_check=elbv2.HealthCheck(
                    path="/", healthy_http_codes="200",
                    interval=Duration.seconds(10), timeout=Duration.seconds(5),
                    healthy_threshold_count=2, unhealthy_threshold_count=3,
                ),
            )

        # ── Native ECS BG resources (phases 2, 2b, 3, 3b, 4) ────────────────

        if p >= 2:
            infra_role = iam.Role(self, "ECSInfraRole",
                role_name="ecs-bg-demo-infra-role-for-alb",
                assumed_by=iam.ServicePrincipal("ecs.amazonaws.com"),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name(
                        "AmazonECSInfrastructureRolePolicyForLoadBalancers"
                    )
                ],
            )
            native_blue_tg = elbv2.ApplicationTargetGroup(self, "NativeBlueTG",
                vpc=vpc, port=80, protocol=elbv2.ApplicationProtocol.HTTP,
                target_type=elbv2.TargetType.IP,
                health_check=elbv2.HealthCheck(
                    path="/", healthy_http_codes="200",
                    interval=Duration.seconds(10), timeout=Duration.seconds(5),
                    healthy_threshold_count=2, unhealthy_threshold_count=3,
                ),
            )
            native_green_tg = elbv2.ApplicationTargetGroup(self, "NativeGreenTG",
                vpc=vpc, port=80, protocol=elbv2.ApplicationProtocol.HTTP,
                target_type=elbv2.TargetType.IP,
                health_check=elbv2.HealthCheck(
                    path="/", healthy_http_codes="200",
                    interval=Duration.seconds(10), timeout=Duration.seconds(5),
                    healthy_threshold_count=2, unhealthy_threshold_count=3,
                ),
            )
            native_task_def = ecs.FargateTaskDefinition(self, "NativeTaskDef",
                family=f"{FAMILY}-native", execution_role=execution_role,
                cpu=256, memory_limit_mib=512,
            )
            native_task_def.add_container("nginx",
                image=ecs.ContainerImage.from_registry(image), essential=True,
                port_mappings=[ecs.PortMapping(container_port=80)],
            )
            l2_cluster = ecs.Cluster.from_cluster_attributes(
                self, "ClusterRef", cluster_name=CLUSTER_NAME,
                vpc=vpc, security_groups=[sg],
            )

        # ── CDListener: same construct ID across all phases where it exists ──
        # Phase 1 & 2:  port 80  → CodeDeploy TGs   (production)
        # Phase 2b:     port 9999 → CodeDeploy TGs   (moved off 80 to free it)
        # Phase 3:      port 80  → Native TGs        (rewire — in-place update)
        # Phases 3b, 4: CDListener absent            (deleted by CFN)

        if p <= 2.5:
            assert cd_blue_tg and cd_green_tg
            cd_listener_port = prod_port if p <= 2 else TEMP_PORT
            cd_listener = alb.add_listener("CDListener",
                port=cd_listener_port,
                protocol=elbv2.ApplicationProtocol.HTTP,
                default_action=elbv2.ListenerAction.weighted_forward([
                    elbv2.WeightedTargetGroup(target_group=cd_blue_tg, weight=1),
                    elbv2.WeightedTargetGroup(target_group=cd_green_tg, weight=0),
                ]),
            )
        elif phase == "3":
            # Approach A: rewire existing CDListener DefaultAction to native TGs
            assert native_blue_tg and native_green_tg
            cd_listener = alb.add_listener("CDListener",
                port=prod_port,
                protocol=elbv2.ApplicationProtocol.HTTP,
                default_action=elbv2.ListenerAction.weighted_forward([
                    elbv2.WeightedTargetGroup(target_group=native_blue_tg, weight=1),
                    elbv2.WeightedTargetGroup(target_group=native_green_tg, weight=0),
                ]),
            )

        # ── CodeDeploy ECS service (phases 1, 2, 2b) ────────────────────────

        if p <= 2.5:
            assert cd_blue_tg and cd_listener
            cd_task_def = ecs.CfnTaskDefinition(self, "CDTaskDef",
                family=FAMILY, execution_role_arn=execution_role.role_arn,
                requires_compatibilities=["FARGATE"], network_mode="awsvpc",
                cpu="256", memory="512",
                container_definitions=[{
                    "name": "nginx", "image": image, "essential": True,
                    "portMappings": [{"containerPort": 80, "protocol": "tcp"}],
                }],
            )
            cd_service = ecs.CfnService(self, "CDService",
                service_name="ecs-bg-demo-codedeploy",
                cluster=cluster.ref, desired_count=1,
                deployment_controller=ecs.CfnService.DeploymentControllerProperty(
                    type="EXTERNAL"
                ),
            )
            cd_service.add_dependency(cd_listener.node.default_child)
            cd_task_set = ecs.CfnTaskSet(self, "CDTaskSet",
                cluster=cluster.ref, service=cd_service.ref,
                task_definition=cd_task_def.ref, launch_type="FARGATE",
                platform_version="LATEST",
                scale=ecs.CfnTaskSet.ScaleProperty(unit="PERCENT", value=100),
                network_configuration=ecs.CfnTaskSet.NetworkConfigurationProperty(
                    aws_vpc_configuration=ecs.CfnTaskSet.AwsVpcConfigurationProperty(
                        assign_public_ip="ENABLED",
                        security_groups=[sg.security_group_id],
                        subnets=[SUBNET1, SUBNET2],
                    )
                ),
                load_balancers=[ecs.CfnTaskSet.LoadBalancerProperty(
                    container_name="nginx", container_port=80,
                    target_group_arn=cd_blue_tg.target_group_arn,
                )],
            )
            ecs.CfnPrimaryTaskSet(self, "CDPrimaryTaskSet",
                cluster=cluster.ref, service=cd_service.ref,
                task_set_id=cd_task_set.attr_id,
            )

        # ── NativeTestListener: same construct ID across phases 2, 2b, 3b ───
        # Phase 2 & 2b: port 8080 → Native TGs  (test)
        # Phase 3b:     port 80   → Native TGs  (production — port swap cutover)
        # Phase 3 & 4:  absent    (approach A uses CDListener instead)

        if phase in ("2", "2b", "3b"):
            assert native_blue_tg and native_green_tg and l2_cluster and native_task_def and infra_role
            native_listener_port = native_port if phase in ("2", "2b") else prod_port
            native_test_listener = alb.add_listener("NativeTestListener",
                port=native_listener_port,
                protocol=elbv2.ApplicationProtocol.HTTP,
                default_action=elbv2.ListenerAction.weighted_forward([
                    elbv2.WeightedTargetGroup(target_group=native_blue_tg, weight=1),
                    elbv2.WeightedTargetGroup(target_group=native_green_tg, weight=0),
                ]),
            )
            native_listener_rule = elbv2.ApplicationListenerRule(
                self, "NativeListenerRule",
                listener=native_test_listener, priority=1,
                conditions=[elbv2.ListenerCondition.path_patterns(["/*"])],
                action=elbv2.ListenerAction.weighted_forward([
                    elbv2.WeightedTargetGroup(target_group=native_blue_tg, weight=1),
                    elbv2.WeightedTargetGroup(target_group=native_green_tg, weight=0),
                ]),
            )
            native_service = ecs.FargateService(self, "NativeService",
                cluster=l2_cluster, task_definition=native_task_def,
                desired_count=1, assign_public_ip=True, security_groups=[sg],
                vpc_subnets=ec2.SubnetSelection(subnets=[
                    ec2.Subnet.from_subnet_id(self, "NativeSub1", SUBNET1),
                    ec2.Subnet.from_subnet_id(self, "NativeSub2", SUBNET2),
                ]),
                deployment_controller=ecs.DeploymentController(
                    type=ecs.DeploymentControllerType.ECS
                ),
            )
            cfn_native = native_service.node.default_child
            cfn_native.add_property_override("DeploymentConfiguration.Strategy", "BLUE_GREEN")
            cfn_native.add_property_override("DeploymentConfiguration.BakeTimeInMinutes", 1)
            cfn_native.add_property_override("LoadBalancers", [{
                "ContainerName": "nginx", "ContainerPort": 80,
                "TargetGroupArn": native_blue_tg.target_group_arn,
                "AdvancedConfiguration": {
                    "AlternateTargetGroupArn": native_green_tg.target_group_arn,
                    "ProductionListenerRule": native_listener_rule.listener_rule_arn,
                    "RoleArn": infra_role.role_arn,
                },
            }])

        # ── Approach A Phase 3: CDListener rewired, native service on CDListener
        elif phase == "3":
            assert native_blue_tg and native_green_tg and l2_cluster and native_task_def and infra_role and cd_listener
            cd_listener_rule = elbv2.ApplicationListenerRule(
                self, "CDListenerRule",
                listener=cd_listener, priority=1,
                conditions=[elbv2.ListenerCondition.path_patterns(["/*"])],
                action=elbv2.ListenerAction.weighted_forward([
                    elbv2.WeightedTargetGroup(target_group=native_blue_tg, weight=1),
                    elbv2.WeightedTargetGroup(target_group=native_green_tg, weight=0),
                ]),
            )
            native_service = ecs.FargateService(self, "NativeService",
                cluster=l2_cluster, task_definition=native_task_def,
                desired_count=1, assign_public_ip=True, security_groups=[sg],
                vpc_subnets=ec2.SubnetSelection(subnets=[
                    ec2.Subnet.from_subnet_id(self, "NativeSub1", SUBNET1),
                    ec2.Subnet.from_subnet_id(self, "NativeSub2", SUBNET2),
                ]),
                deployment_controller=ecs.DeploymentController(
                    type=ecs.DeploymentControllerType.ECS
                ),
            )
            cfn_native = native_service.node.default_child
            cfn_native.add_property_override("DeploymentConfiguration.Strategy", "BLUE_GREEN")
            cfn_native.add_property_override("DeploymentConfiguration.BakeTimeInMinutes", 1)
            cfn_native.add_property_override("LoadBalancers", [{
                "ContainerName": "nginx", "ContainerPort": 80,
                "TargetGroupArn": native_blue_tg.target_group_arn,
                "AdvancedConfiguration": {
                    "AlternateTargetGroupArn": native_green_tg.target_group_arn,
                    "ProductionListenerRule": cd_listener_rule.listener_rule_arn,
                    "RoleArn": infra_role.role_arn,
                },
            }])

        # ── Outputs ──────────────────────────────────────────────────────────

        CfnOutput(self, "ALBDNSName",
            value=f"http://{alb.load_balancer_dns_name}",
            description="ALB DNS - production traffic (port 80)",
        )
        CfnOutput(self, "Phase", value=phase,
            description="Current migration phase",
        )
        if phase in ("1", "2"):
            CfnOutput(self, "CodeDeployURL",
                value=f"http://{alb.load_balancer_dns_name}:{prod_port}",
                description="CodeDeploy service — production traffic",
            )
        if phase == "2":
            CfnOutput(self, "NativeTestURL",
                value=f"http://{alb.load_balancer_dns_name}:{native_port}",
                description="Native service — test URL, validate before cutover",
            )
        if phase == "2b":
            CfnOutput(self, "CodeDeployTempURL",
                value=f"http://{alb.load_balancer_dns_name}:{TEMP_PORT}",
                description="CodeDeploy moved to temp port — port 80 now free",
            )
            CfnOutput(self, "NativeTestURL",
                value=f"http://{alb.load_balancer_dns_name}:{native_port}",
                description="Native service still on port 8080",
            )
        if phase in ("3", "3b"):
            CfnOutput(self, "NativeProductionURL",
                value=f"http://{alb.load_balancer_dns_name}:{prod_port}",
                description="Native ECS service now serving production traffic on port 80",
            )
