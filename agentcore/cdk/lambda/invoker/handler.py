"""EventBridge -> OpsSherlock invoker.

Triggered on a schedule (pull / monitoring sweep) or by a CloudWatch alarm event
(push, added in a later phase). Builds a prompt from the event and invokes the
OpsSherlock AgentCore runtime, logging the result so the sweep is observable.

Phase 2 is pull-only: the schedule sends a "scan all clusters" instruction and the
agent reports (in DRY_RUN) what it would do. No dependencies beyond the boto3 that
ships in the Lambda runtime.
"""

import json
import logging
import os
import uuid

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

RUNTIME_ARN = os.environ["RUNTIME_ARN"]
# AWS_REGION is always set by the Lambda runtime.
_client = boto3.client("bedrock-agentcore", region_name=os.environ["AWS_REGION"])

SCAN_PROMPT = (
    "Scan all EKS clusters and namespaces for unhealthy workloads "
    "(CrashLoopBackOff, not-ready deployments, pending pods, recent Warning "
    "events). Investigate anything unhealthy and report findings. If everything "
    "is healthy, reply 'all clear'."
)


def _metric_context(detail: dict) -> str:
    """Summarize the alarm's metric(s) and dimensions (e.g. the EC2 InstanceId)
    so the agent knows which resource to investigate."""
    metrics = (detail.get("configuration") or {}).get("metrics") or []
    parts = []
    for m in metrics:
        metric = (m.get("metricStat") or {}).get("metric") or {}
        ns = metric.get("namespace")
        mname = metric.get("name")
        dims = metric.get("dimensions") or {}
        if ns or mname or dims:
            parts.append(f"{ns or '?'}/{mname or '?'} dimensions={json.dumps(dims)}")
    return f" Metric(s): {'; '.join(parts)}." if parts else ""


def _build_prompt(event: dict) -> str:
    if event.get("source") == "aws.cloudwatch":
        detail = event.get("detail") or {}
        state = detail.get("state") or {}
        name = detail.get("alarmName", "unknown")
        return (
            f"CloudWatch alarm '{name}' entered {state.get('value', 'ALARM')}: "
            f"{state.get('reason', '')}{_metric_context(detail)} "
            "Investigate and remediate."
        )
    # Scheduled sweep (default).
    return SCAN_PROMPT


def handler(event, context):
    prompt = _build_prompt(event or {})
    # runtimeSessionId must be >= 33 chars.
    session_id = uuid.uuid4().hex + uuid.uuid4().hex
    log.info("OpsSherlock invoke: source=%s session=%s", (event or {}).get("source", "schedule"), session_id)

    resp = _client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt}).encode("utf-8"),
    )
    body = resp["response"].read().decode("utf-8", errors="replace")
    log.info("OpsSherlock result: %s", body[:4000])
    return {"ok": True, "session": session_id}
