#!/usr/bin/env bash
# Deploy OpsSherlock: the AgentCore runtime, then the monitoring stack.
#
# `agentcore deploy` only deploys its own CLI-managed stack (AgentCore-<project>-
# <target>); it has no hook to also bring up our separate monitoring stack. This
# wrapper runs both in the right order — agentcore first (so the runtime ARN is
# written to .cli/deployed-state.json), then the monitoring stack (which reads it).
#
# Usage:  ./deploy.sh [agentcore-deploy-args...]
#   e.g.  ./deploy.sh --target default -y
# Passing -y / --yes also skips the CDK approval prompt for the monitoring stack.
set -euo pipefail

# This script lives in agentcore/; the CLI must run from the project root.
agentcore_dir="$(cd "$(dirname "$0")" && pwd)"
project_root="$(dirname "$agentcore_dir")"

# Mirror agentcore's non-interactive flag onto the CDK deploy.
approval=()
for arg in "$@"; do
  case "$arg" in
    -y|--yes) approval=(--require-approval never) ;;
  esac
done

echo "==> agentcore deploy $*"
cd "$project_root"
agentcore deploy "$@"

echo "==> monitoring stack (cdk)"
cd "$agentcore_dir/cdk"
npm run build
./node_modules/.bin/cdk --app "node dist/bin/monitoring.js" deploy "${approval[@]+"${approval[@]}"}"

echo "==> done: runtime + monitoring deployed"
