import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from strands import tool

from .k8s_client import load_k8s

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Cap the recheck window so a tool call can't hang the runtime indefinitely.
_MAX_TIMEOUT = 600


def _check_alarm(alarm_name: str) -> tuple[bool, str]:
    """Return (recovered, detail) for a CloudWatch alarm. Recovered means OK."""
    cw = boto3.client("cloudwatch", region_name=AWS_REGION)
    resp = cw.describe_alarms(AlarmNames=[alarm_name], MaxRecords=1)
    alarms = resp.get("MetricAlarms", [])
    if not alarms:
        return False, f"alarm '{alarm_name}' not found"
    state = alarms[0].get("StateValue")
    return state == "OK", f"alarm '{alarm_name}' is {state}"


def _check_deployment(deployment: str, namespace: str, cluster: str) -> tuple[bool, str]:
    """Return (recovered, detail) for a deployment. Recovered means fully ready."""
    load_k8s(cluster or None)
    apps = k8s_client.AppsV1Api()
    d = apps.read_namespaced_deployment(deployment, namespace)
    desired = d.spec.replicas or 0
    ready = d.status.ready_replicas or 0
    recovered = ready >= desired and (d.status.unavailable_replicas or 0) == 0
    return recovered, f"deployment '{deployment}' ready {ready}/{desired}"


@tool
def verify_recovery(
    alarm_name: str = "",
    deployment: str = "",
    namespace: str = "default",
    timeout: int = 120,
    interval: int = 15,
    cluster: str = "",
) -> str:
    """Verify an incident has recovered after a remediation, polling until healthy.

    Provide the CloudWatch alarm that fired and/or the deployment that was
    remediated. The tool rechecks each signal every `interval` seconds until all
    provided signals are healthy or `timeout` is reached. When both are given,
    both must recover for success.

    Args:
        alarm_name: Name of the CloudWatch alarm to confirm has returned to OK.
        deployment: Name of the deployment to confirm has reached full readiness.
        namespace: Namespace of the deployment. Defaults to "default".
        timeout: Maximum seconds to wait for recovery. Capped at 600. Defaults to 120.
        interval: Seconds between rechecks. Defaults to 15.
        cluster: EKS cluster of the deployment. Defaults to EKS_CLUSTER_NAME if unset.

    Returns:
        A string stating whether recovery was confirmed, with the last observed
        state of each signal, or an error string.
    """
    if not alarm_name and not deployment:
        return "Nothing to verify: provide alarm_name and/or deployment."

    timeout = min(max(timeout, 0), _MAX_TIMEOUT)
    interval = max(interval, 1)
    deadline = time.monotonic() + timeout

    last_details: list[str] = []
    while True:
        details: list[str] = []
        recovered = True
        try:
            if alarm_name:
                ok, detail = _check_alarm(alarm_name)
                recovered = recovered and ok
                details.append(detail)
            if deployment:
                ok, detail = _check_deployment(deployment, namespace, cluster)
                recovered = recovered and ok
                details.append(detail)
        except (BotoCoreError, ClientError) as e:
            return f"Error verifying recovery (CloudWatch): {e}"
        except ApiException as e:
            return f"Error verifying recovery (Kubernetes): {e.status} {e.reason}"
        except Exception as e:
            return f"Error verifying recovery: {e}"

        last_details = details
        if recovered:
            return "RECOVERY CONFIRMED — " + "; ".join(details) + "."
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)

    return (
        f"RECOVERY NOT CONFIRMED after {timeout}s — "
        + "; ".join(last_details)
        + ". Consider escalating to a human engineer."
    )


@tool
def escalate(
    summary: str,
    reason: str,
    severity: str = "P2",
    suggested_actions: str = "",
) -> str:
    """Escalate the incident to a human engineer.

    Use this when a decision is needed rather than acting: recovery could not be
    confirmed, the root cause is ambiguous, or a remediation is too risky to take
    unattended. This does not take any infrastructure action — it produces a
    handoff for a human.

    Args:
        summary: One-line summary of the incident.
        reason: Why a human is needed (what is uncertain or risky).
        severity: Incident severity — "P1", "P2", or "P3". Defaults to "P2".
        suggested_actions: Optional recommended next steps for the engineer.

    Returns:
        A structured escalation block to surface in the response.
    """
    lines = [
        "🚨 ESCALATION NEEDED — human decision required",
        f"Severity: {severity}",
        f"Summary: {summary}",
        f"Reason: {reason}",
    ]
    if suggested_actions:
        lines.append(f"Suggested actions: {suggested_actions}")
    return "\n".join(lines)
