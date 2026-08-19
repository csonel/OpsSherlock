import { Duration, Stack, type StackProps, CfnOutput } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';

/**
 * Throwaway test fixture for the OpsSherlock push path (Phase 4).
 *
 * Stands up one small EC2 instance and a CloudWatch alarm on its CPU. When the
 * alarm enters ALARM, the monitoring stack's EventBridge rule forwards it to the
 * invoker Lambda, which triggers OpsSherlock to investigate the instance (still
 * DRY_RUN — it reports what it would do). Deploy/destroy this on its own; it is
 * not part of the always-on monitoring infrastructure.
 *
 * Trigger it either instantly:
 *   aws cloudwatch set-alarm-state --alarm-name OpsSherlock-Test-EC2-HighCPU \
 *     --state-value ALARM --state-reason "test"
 * or for real: connect via SSM Session Manager and run `stress-ng --cpu 0`.
 */
export class Ec2TestStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    // Minimal, self-contained VPC: one public subnet, no NAT gateway (no cost).
    const vpc = new ec2.Vpc(this, 'TestVpc', {
      maxAzs: 1,
      natGateways: 0,
      subnetConfiguration: [{ name: 'public', subnetType: ec2.SubnetType.PUBLIC }],
    });

    // Outbound-only; SSM Session Manager needs no inbound rules.
    const instance = new ec2.Instance(this, 'TestInstance', {
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
      machineImage: ec2.MachineImage.latestAmazonLinux2023(),
      // SSM access so you can connect and spike CPU to fire the alarm for real.
      ssmSessionPermissions: true,
    });
    instance.role.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore')
    );
    // stress-ng is handy for driving CPU up on demand.
    instance.userData.addCommands('dnf install -y stress-ng || yum install -y stress-ng || true');

    const cpu = new cloudwatch.Metric({
      namespace: 'AWS/EC2',
      metricName: 'CPUUtilization',
      dimensionsMap: { InstanceId: instance.instanceId },
      period: Duration.minutes(1),
      statistic: 'Average',
    });

    // Stable name so the agent's verify_recovery(alarm_name=...) can check it.
    new cloudwatch.Alarm(this, 'HighCpuAlarm', {
      alarmName: 'OpsSherlock-Test-EC2-HighCPU',
      alarmDescription: 'Test: high CPU on the OpsSherlock demo EC2 instance',
      metric: cpu,
      threshold: 50,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    new CfnOutput(this, 'InstanceId', { value: instance.instanceId });
    new CfnOutput(this, 'AlarmName', { value: 'OpsSherlock-Test-EC2-HighCPU' });
  }
}
