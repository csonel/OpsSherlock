#!/usr/bin/env node
import { App, type Environment } from 'aws-cdk-lib';
import * as path from 'path';
import * as fs from 'fs';
import { Ec2TestStack } from '../lib/ec2-test-stack';

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

const app = new App();
new Ec2TestStack(app, 'OpsSherlock-Ec2Test', {
  env,
  description: 'OpsSherlock push-path test fixture: EC2 instance + CPU alarm',
});
app.synth();
