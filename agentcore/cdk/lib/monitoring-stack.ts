import { Duration, RemovalPolicy, Stack, type StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as path from 'path';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as logs from 'aws-cdk-lib/aws-logs';

export interface MonitoringStackProps extends StackProps {
  /** ARN of the deployed OpsSherlock AgentCore runtime to invoke. */
  readonly runtimeArn: string;
  /** How often the pull sweep runs. */
  readonly scanRate?: Duration;
}

/**
 * Active-monitoring stack for OpsSherlock (Phase 2 — pull only).
 *
 * A scheduled EventBridge rule fires the invoker Lambda every few minutes; the
 * Lambda calls InvokeAgentRuntime with a "scan all clusters" prompt so the agent
 * reports (in DRY_RUN) anything unhealthy. Kept separate from the CLI-vended
 * AgentCore stack so it is never regenerated.
 */
export class MonitoringStack extends Stack {
  constructor(scope: Construct, id: string, props: MonitoringStackProps) {
    super(scope, id, props);

    // Explicit log group instead of the deprecated `logRetention` prop, which
    // spawns a legacy LogRetention custom-resource provider Lambda (the one that
    // logs the url.parse() DeprecationWarning).
    const invokerLogs = new logs.LogGroup(this, 'OpsSherlockInvokerLogs', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const invoker = new lambda.Function(this, 'OpsSherlockInvoker', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'invoker')),
      // A full RCA -> remediate -> verify pass can take a while; give it room.
      timeout: Duration.minutes(5),
      memorySize: 256,
      // Overlap protection comes from the 5-minute spacing + Phase 3 memory
      // dedup — not a reserved-concurrency slot, which the account's low
      // concurrency limit rejects (would drop unreserved below the floor of 10).
      logGroup: invokerLogs,
      environment: {
        RUNTIME_ARN: props.runtimeArn,
      },
    });

    invoker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeAgentRuntime'],
        // Runtime ARN plus its endpoints/sessions (…/runtime-endpoint/*).
        resources: [props.runtimeArn, `${props.runtimeArn}/*`],
      })
    );

    new events.Rule(this, 'OpsSherlockScanSchedule', {
      description: 'Periodic OpsSherlock cluster scan (pull monitoring)',
      schedule: events.Schedule.rate(props.scanRate ?? Duration.minutes(5)),
      targets: [
        new targets.LambdaFunction(invoker, {
          event: events.RuleTargetInput.fromObject({ source: 'schedule' }),
        }),
      ],
    });
  }
}
