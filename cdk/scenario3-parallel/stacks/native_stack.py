from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    Fn,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
    aws_ecs as ecs,
    aws_iam as iam,
)
from constructs import Construct
from .config import VPC_ID, SUBNET1, SUBNET2, AZ1, AZ2, FAMILY


class EcsNativeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, image: str, native_port: int, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # --- Import shared resources from EcsBgStack ---
        alb_arn = Fn.import_value("ecs-bg-demo-alb-arn")
        sg_id = Fn.import_value("ecs-bg-demo-sg-id")
        cluster_name = Fn.import_value("ecs-bg-demo-cluster-name")

        vpc = ec2.Vpc.from_vpc_attributes(
            self, "Vpc",
            vpc_id=VPC_ID,
            availability_zones=[AZ1, AZ2],
            public_subnet_ids=[SUBNET1, SUBNET2],
        )

        alb = elbv2.ApplicationLoadBalancer.from_application_load_balancer_attributes(
            self, "ALB",
            load_balancer_arn=alb_arn,
            security_group_id=sg_id,
        )

        sg = ec2.SecurityGroup.from_security_group_id(self, "SG", sg_id)

        cluster = ecs.Cluster.from_cluster_attributes(
            self, "Cluster",
            cluster_name=cluster_name,
            vpc=vpc,
            security_groups=[sg],
        )

        # --- New target groups owned by this stack ---
        native_blue_tg = elbv2.ApplicationTargetGroup(self, "NativeBlueTG",
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

        native_green_tg = elbv2.ApplicationTargetGroup(self, "NativeGreenTG",
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

        # --- New listener on native_port (8080 initially, swapped to 80 at cutover) ---
        listener = alb.add_listener("NativeListener",
            port=native_port,
            protocol=elbv2.ApplicationProtocol.HTTP,
            default_action=elbv2.ListenerAction.weighted_forward([
                elbv2.WeightedTargetGroup(target_group=native_blue_tg, weight=1),
                elbv2.WeightedTargetGroup(target_group=native_green_tg, weight=0),
            ]),
        )

        listener_rule = elbv2.ApplicationListenerRule(self, "NativeListenerRule",
            listener=listener,
            priority=1,
            conditions=[elbv2.ListenerCondition.path_patterns(["/*"])],
            action=elbv2.ListenerAction.weighted_forward([
                elbv2.WeightedTargetGroup(target_group=native_blue_tg, weight=1),
                elbv2.WeightedTargetGroup(target_group=native_green_tg, weight=0),
            ]),
        )

        # --- IAM roles ---
        execution_role = iam.Role(self, "TaskExecRole",
            role_name="ecs-bg-native-task-execution-role",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )

        infra_role = iam.Role(self, "ECSInfraRole",
            role_name="ecs-bg-native-infra-role-for-alb",
            assumed_by=iam.ServicePrincipal("ecs.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonECSInfrastructureRolePolicyForLoadBalancers"
                )
            ],
        )

        # --- Task Definition ---
        task_def = ecs.FargateTaskDefinition(self, "TaskDef",
            family=f"{FAMILY}-native",
            execution_role=execution_role,
            cpu=256,
            memory_limit_mib=512,
        )
        task_def.add_container("nginx",
            image=ecs.ContainerImage.from_registry(image),
            essential=True,
            port_mappings=[ecs.PortMapping(container_port=80)],
        )

        # --- ECS Native Blue/Green Service (L2 + L1 escape hatch for AdvancedConfiguration) ---
        service = ecs.FargateService(self, "NativeService",
            cluster=cluster,
            task_definition=task_def,
            desired_count=1,
            assign_public_ip=True,
            security_groups=[sg],
            vpc_subnets=ec2.SubnetSelection(subnets=[
                ec2.Subnet.from_subnet_id(self, "Sub1", SUBNET1),
                ec2.Subnet.from_subnet_id(self, "Sub2", SUBNET2),
            ]),
            deployment_controller=ecs.DeploymentController(
                type=ecs.DeploymentControllerType.ECS
            ),
        )

        # AdvancedConfiguration not yet in L2 — use L1 escape hatch
        cfn_service = service.node.default_child
        cfn_service.add_property_override("DeploymentConfiguration.Strategy", "BLUE_GREEN")
        cfn_service.add_property_override("DeploymentConfiguration.BakeTimeInMinutes", 1)
        cfn_service.add_property_override("LoadBalancers", [{
            "ContainerName": "nginx",
            "ContainerPort": 80,
            "TargetGroupArn": native_blue_tg.target_group_arn,
            "AdvancedConfiguration": {
                "AlternateTargetGroupArn": native_green_tg.target_group_arn,
                "ProductionListenerRule": listener_rule.listener_rule_arn,
                "RoleArn": infra_role.role_arn,
            },
        }])

        CfnOutput(self, "NativeServiceArn",
            value=service.service_arn,
            description="ECS native service ARN",
        )
        # ALB DNS imported from EcsBgStack — access via Fn.import_value since
        # from_application_load_balancer_attributes does not expose load_balancer_dns_name
        CfnOutput(self, "NativeALBDNSName",
            value=Fn.join("", ["http://", Fn.import_value("ecs-bg-demo-alb-dns")]),
            description="ALB DNS (same ALB, test on native_port)",
        )
