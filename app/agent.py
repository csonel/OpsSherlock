import os
import re

from strands import Agent
from strands import tool
from strands.models import BedrockModel
from bedrock_agentcore import BedrockAgentCoreApp
from dotenv import load_dotenv

from tools import *

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "eu.amazon.nova-micro-v1:0")

app = BedrockAgentCoreApp()

model = BedrockModel(
    model_id=MODEL_ID,
    #model_id="eu.anthropic.claude-opus-4-6-v1",
    region_name=AWS_REGION,
)

SYSTEM_PROMPT = """You are OpsSherlock, an autonomous SRE assistant that investigates
    incidents, finds root causes, safely remediates, verifies recovery, and involves
    engineers only when a decision is needed.

    Your workflow for an incident:
    1. Use rca_agent to investigate and produce a root cause analysis.
    2. Use remediation_agent to take the safest reversible action and verify recovery.
    3. Escalate to a human with the escalate tool when — and only when — a decision is
       needed: recovery could not be confirmed, the root cause is ambiguous, or the
       safest remediation is too risky to take unattended. Do not escalate routine
       incidents that were resolved and verified.

    Report clearly: what happened, what you did, and whether recovery was confirmed.

    Monitoring sweeps: if asked to scan for incidents, use rca_agent to list every
    cluster and scan each one for unhealthy workloads. Investigate only what is
    actually unhealthy. If everything is healthy, report "all clear" and stop —
    do not take any action or escalate.

    Deduplication (critical for scheduled sweeps): for each unhealthy workload,
    form a stable incident key "cluster/namespace/workload/signal" (e.g.
    "prod/payments/api/CrashLoopBackOff") and call incident_seen(key) BEFORE
    investigating. If it says ALREADY TRACKING, skip that workload entirely — do
    not re-investigate or re-remediate. Otherwise call record_incident(key,
    "open") and handle it. After remediating call record_incident(key,
    "remediated"); when recovery is confirmed call record_incident(key,
    "resolved"); if you escalate call record_incident(key, "escalated"). Use the
    SAME key throughout an incident's lifecycle.
    """

# ---------------------------------------------------------------------------
# Sub-Agents wrapped as Tools (Agents-as-Tools pattern)
# ---------------------------------------------------------------------------

_rca_agent = Agent(
    model=model,
    system_prompt="""You are a senior Site Reliability Engineer performing root cause analysis.
    Given alarm data, metrics, and log snippets, your job is to:
    1. Identify the most likely root cause(s).
    2. Assess the blast radius (which services/users are affected).
    3. Rate the severity (P1 critical / P2 high / P3 medium).
    4. Propose 2-3 concrete remediation options ranked by risk.
    
    Be precise. Use technical language. Cite specific metric values and log lines.

    If you don't know which cluster is affected, call list_clusters() first, then
    pass the cluster name to the Kubernetes tools. For a monitoring sweep, call
    scan_cluster() on each cluster to surface unhealthy workloads before drilling
    in with kubectl_describe / pod_logs. Call recall_similar_incidents() with a
    short description of the problem to see how similar past incidents were
    handled — use it for context only, never to decide whether to act.
    """,
    tools=[
        get_cloudwatch_alarms,
        get_metric_statistics,
        query_logs,
        list_clusters,
        scan_cluster,
        kubectl_get,
        kubectl_describe,
        pod_logs,
        recall_similar_incidents,
    ],
)

_remediation_agent = Agent(
    model=model,
    system_prompt="""You are a Kubernetes and Helm operations expert.
    Given a root cause analysis, your job is to:
    1. Inspect the current state of affected workloads with kubectl.
    2. Propose and execute the safest remediation action (rollback, restart, scale).
    3. Always prefer reversible actions (rollback > restart > scale).
    4. After acting, call verify_recovery to confirm the incident is resolved
       (alarm back to OK and/or the deployment fully ready).
    5. Confirm the action taken and the recovery result, or explain why no action
       was taken. If recovery is NOT confirmed, say so clearly.

    Pass the affected cluster (named in the root cause analysis) to every tool
    via its `cluster` argument. If the cluster is unknown, call list_clusters().

    In DRY_RUN mode, commands are simulated and safe to run.
    """,
    tools=[
        list_clusters,
        kubectl_get,
        kubectl_describe,
        pod_logs,
        rollout_restart,
        scale_deployment,
        rollback_deployment,
        helm_rollback,
        verify_recovery,
    ],
)

@tool
def rca_agent(context: str) -> str:
    response = _rca_agent(context)
    return str(response)

@tool
def remediation_agent(instructions: str) -> str:
    response = _remediation_agent(instructions)
    return str(response)

# ---------------------------------------------------------------------------
# Main agent
# ---------------------------------------------------------------------------

agent = Agent(
    model=model,
    system_prompt=SYSTEM_PROMPT,
    tools=[
        rca_agent,
        remediation_agent,
        escalate,
        incident_seen,
        record_incident,
    ],
)

# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

_THINKING_RE = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL)


def format_response(text: str) -> str:
    """Turn inline <thinking>...</thinking> blocks into a styled reasoning
    blockquote, kept visually distinct from the final answer."""

    def _to_blockquote(match: re.Match) -> str:
        reasoning = match.group(1).strip().replace("\n", "\n> ")
        return f"\n\n> *Reasoning:* {reasoning}\n\n---\n\n"

    return _THINKING_RE.sub(_to_blockquote, text).strip()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@app.entrypoint
async def agent_invocation(payload):
    """Handler for agent invocation"""
    user_message = payload.get(
        "prompt", "No prompt found in input, please guide customer to create a json payload with prompt key"
    )
    stream = agent.stream_async(user_message)
    chunks = []
    async for event in stream:
        # Collect only the human-readable text chunks; skip raw metadata events
        # (tool calls, message deltas, lifecycle) that render as noisy JSON.
        if "data" in event:
            chunks.append(event["data"])
    yield format_response("".join(chunks))


if __name__ == "__main__":
    app.run()
