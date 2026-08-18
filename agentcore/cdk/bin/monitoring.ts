#!/usr/bin/env node
import { App, type Environment } from 'aws-cdk-lib';
import * as path from 'path';
import * as fs from 'fs';
import { MonitoringStack } from '../lib/monitoring-stack';

interface Target {
  name: string;
  account: string;
  region: string;
}

// Config root is the parent of cdk/ (this bin is run from agentcore/cdk/).
const configRoot = path.resolve(process.cwd(), '..');

const targets: Target[] = JSON.parse(
  fs.readFileSync(path.join(configRoot, 'aws-targets.json'), 'utf8')
);
if (targets.length === 0) {
  throw new Error('No deployment targets in agentcore/aws-targets.json');
}
const target = targets[0];
const env: Environment = { account: target.account, region: target.region };

const deployedState = JSON.parse(
  fs.readFileSync(path.join(configRoot, '.cli', 'deployed-state.json'), 'utf8')
);
const runtimeArn: string | undefined =
  deployedState?.targets?.[target.name]?.resources?.runtimes?.OpsSherlock?.runtimeArn;
if (!runtimeArn) {
  throw new Error(
    `No OpsSherlock runtime ARN in .cli/deployed-state.json for target "${target.name}". ` +
      'Deploy the agent runtime first (agentcore deploy).'
  );
}

const app = new App();
new MonitoringStack(app, 'OpsSherlock-Monitoring', {
  env,
  runtimeArn,
  description: 'OpsSherlock active monitoring (pull sweep) — Phase 2',
});
app.synth();
