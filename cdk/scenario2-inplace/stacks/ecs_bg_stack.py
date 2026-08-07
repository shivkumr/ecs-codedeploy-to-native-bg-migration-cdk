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


class EcsBgStack(Stack):
    def __init__(self, scope: Construct, construct_id: str,
                 image: str, deployment_type: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        is_native = deployment_type == "native"

        # --- Networking (same in both modes) ---
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

        # --- ALB (same in both modes) ---
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

        blue_tg = elbv2.ApplicationTargetGroup(self, "BlueTG",
            vpc=vpc,
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/",
                healthy_http_codes="200",
                interval=Duration.seconds(10),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
        )

        green_tg = elbv2.ApplicationTargetGroup(self, "GreenTG",
            vpc=vpc,
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/",
                healthy_http_codes="200",
                interval=Duration.seconds(10),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
        )

        # Listener: CodeDeploy needs only blue TG; native needs both pre-wired
        listener_rule = None
        if is_native:
            listener = alb.add_listener("ProdListener",
                port=80,
                protocol=elbv2.ApplicationProtocol.HTTP,
                default_action=elbv2.ListenerAction.weighted_forward([
                    elbv2.WeightedTargetGroup(target_group=blue_tg, weight=1),
                    elbv2.WeightedTargetGroup(target_group=green_tg, weight=0),
                ]),
            )
            listener_rule = elbv2.ApplicationListenerRule(self, "ProdRule",
                listener=listener,
                priority=1,
                conditions=[elbv2.ListenerCondition.path_patterns(["/*"])],
                action=elbv2.ListenerAction.weighted_forward([
                    elbv2.WeightedTargetGroup(target_group=blue_tg, weight=1),
                    elbv2.WeightedTargetGroup(target_group=green_tg, weight=0),
                ]),
            )
        else:
            listener = alb.add_listener("ProdListener",
                port=80,
                protocol=elbv2.ApplicationProtocol.HTTP,
                default_action=elbv2.ListenerAction.weighted_forward([
                    elbv2.WeightedTargetGroup(target_group=blue_tg, weight=1),
                ]),
            )

        # --- IAM ---
        execution_role = iam.Role(self, "TaskExecRole",
            role_name="ecs-bg-demo-task-execution-role",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )

        # --- ECS Cluster (same in both modes) ---
        cluster = ecs.CfnCluster(self, "Cluster",
            cluster_name=CLUSTER_NAME,
        )

        # --- CodeDeploy BG mode ---
        if not is_native:
            task_def = ecs.CfnTaskDefinition(self, "TaskDef",
                family=FAMILY,
                execution_role_arn=execution_role.role_arn,
                requires_compatibilities=["FARGATE"],
                network_mode="awsvpc",
                cpu="256",
                memory="512",
                container_definitions=[{
                    "name": "nginx",
                    "image": image,
                    "essential": True,
                    "portMappings": [{"containerPort": 80, "protocol": "tcp"}],
                }],
            )

            # ServiceName hardcoded here — intentional for CodeDeploy phase
            service = ecs.CfnService(self, "Service",
                service_name="ecs-bg-demo-service",
                cluster=cluster.ref,
                desired_count=1,
                deployment_controller=ecs.CfnService.DeploymentControllerProperty(
                    type="EXTERNAL"
                ),
            )
            service.add_depends_on(listener.node.default_child)

            task_set = ecs.CfnTaskSet(self, "BlueTaskSet",
                cluster=cluster.ref,
                service=service.ref,
                task_definition=task_def.ref,
                launch_type="FARGATE",
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
                    container_name="nginx",
                    container_port=80,
                    target_group_arn=blue_tg.target_group_arn,
                )],
            )

            ecs.CfnPrimaryTaskSet(self, "PrimaryTaskSet",
                cluster=cluster.ref,
                service=service.ref,
                task_set_id=task_set.attr_id,
            )

        # --- ECS Native BG mode ---
        else:
            infra_role = iam.Role(self, "ECSInfraRole",
                role_name="ecs-bg-demo-infra-role-for-alb",
                assumed_by=iam.ServicePrincipal("ecs.amazonaws.com"),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name(
                        "AmazonECSInfrastructureRolePolicyForLoadBalancers"
                    )
                ],
            )

            task_def = ecs.FargateTaskDefinition(self, "TaskDef",
                family=FAMILY,
                execution_role=execution_role,
                cpu=256,
                memory_limit_mib=512,
            )
            task_def.add_container("nginx",
                image=ecs.ContainerImage.from_registry(image),
                essential=True,
                port_mappings=[ecs.PortMapping(container_port=80)],
            )

            # Import cluster as L2 for use with FargateService
            l2_cluster = ecs.Cluster.from_cluster_attributes(
                self, "ClusterRef",
                cluster_name=CLUSTER_NAME,
                vpc=vpc,
                security_groups=[sg],
            )

            # ServiceName intentionally OMITTED — allows CFN to replace the
            # service cleanly when DeploymentController changes (immutable property)
            service = ecs.FargateService(self, "Service",
                cluster=l2_cluster,
                task_definition=task_def,
                desired_count=1,
                assign_public_ip=True,
                security_groups=[sg],
                vpc_subnets=ec2.SubnetSelection(subnets=[
                    ec2.Subnet.from_subnet_id(self, "Sub1Native", SUBNET1),
                    ec2.Subnet.from_subnet_id(self, "Sub2Native", SUBNET2),
                ]),
                deployment_controller=ecs.DeploymentController(
                    type=ecs.DeploymentControllerType.ECS
                ),
            )

            assert listener_rule is not None
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

        CfnOutput(self, "ALBDNSName",
            value=f"http://{alb.load_balancer_dns_name}",
            description="ALB DNS - open in browser",
        )
        CfnOutput(self, "DeploymentType",
            value=deployment_type,
            description="Current deployment type (codedeploy or native)",
        )
