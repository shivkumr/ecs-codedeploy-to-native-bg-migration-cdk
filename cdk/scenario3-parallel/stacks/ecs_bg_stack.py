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
    def __init__(self, scope: Construct, construct_id: str, image: str, prod_port: int, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # --- Networking ---
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

        # --- ALB ---
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

        listener = alb.add_listener("ProdListener",
            port=prod_port,
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

        # --- ECS Cluster ---
        cluster = ecs.CfnCluster(self, "Cluster",
            cluster_name=CLUSTER_NAME,
        )

        # --- Task Definition (L1 — needed for CodeDeploy EXTERNAL controller) ---
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

        # --- ECS Service (EXTERNAL controller — required for CodeDeploy BG via CFN transform) ---
        service = ecs.CfnService(self, "Service",
            service_name="ecs-bg-demo-service",
            cluster=cluster.ref,
            desired_count=1,
            deployment_controller=ecs.CfnService.DeploymentControllerProperty(
                type="EXTERNAL"
            ),
        )
        service.add_depends_on(listener.node.default_child)

        # --- TaskSet (blue) ---
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

        # --- PrimaryTaskSet ---
        ecs.CfnPrimaryTaskSet(self, "PrimaryTaskSet",
            cluster=cluster.ref,
            service=service.ref,
            task_set_id=task_set.attr_id,
        )

        # --- Outputs (used by EcsNativeStack via Fn.import_value) ---
        CfnOutput(self, "ALBArn",
            value=alb.load_balancer_arn,
            export_name="ecs-bg-demo-alb-arn",
        )
        CfnOutput(self, "SGId",
            value=sg.security_group_id,
            export_name="ecs-bg-demo-sg-id",
        )
        CfnOutput(self, "ClusterName",
            value=CLUSTER_NAME,
            export_name="ecs-bg-demo-cluster-name",
        )
        CfnOutput(self, "ALBDNSName",
            value=f"http://{alb.load_balancer_dns_name}",
            description="ALB DNS - open in browser",
        )
        CfnOutput(self, "ALBDNSNameExport",
            value=alb.load_balancer_dns_name,
            export_name="ecs-bg-demo-alb-dns",
        )
        CfnOutput(self, "BlueTGArn",
            value=blue_tg.target_group_arn,
            export_name="ecs-bg-demo-blue-tg-arn",
        )
        CfnOutput(self, "GreenTGArn",
            value=green_tg.target_group_arn,
            export_name="ecs-bg-demo-green-tg-arn",
        )
        CfnOutput(self, "ListenerArn",
            value=listener.listener_arn,
            export_name="ecs-bg-demo-listener-arn",
        )
