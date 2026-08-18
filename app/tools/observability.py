import json
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from strands import tool

from .k8s_client import list_eks_clusters, load_k8s

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# AWS CloudWatch (read-only)
# ---------------------------------------------------------------------------


@tool
def get_cloudwatch_alarms(state: str = "ALARM") -> str:
    """List CloudWatch alarms filtered by state.

    Args:
        state: Alarm state to filter by — one of "ALARM", "OK", or
            "INSUFFICIENT_DATA". Defaults to "ALARM" (firing alarms).

    Returns:
        A JSON list of alarms with name, metric, state, and reason, or an
        error string.
    """
    try:
        cw = boto3.client("cloudwatch", region_name=AWS_REGION)
        resp = cw.describe_alarms(StateValue=state, MaxRecords=100)
        alarms = [
            {
                "name": a.get("AlarmName"),
                "metric": a.get("MetricName"),
                "namespace": a.get("Namespace"),
                "state": a.get("StateValue"),
                "reason": a.get("StateReason"),
            }
            for a in resp.get("MetricAlarms", [])
        ]
        return json.dumps(alarms, indent=2)
    except (BotoCoreError, ClientError) as e:
        return f"Error fetching CloudWatch alarms: {e}"


@tool
def get_metric_statistics(
    namespace: str,
    metric_name: str,
    dimension_name: str = "",
    dimension_value: str = "",
    stat: str = "Average",
    minutes: int = 60,
    period: int = 300,
) -> str:
    """Fetch CloudWatch metric statistics over a recent time window.

    Args:
        namespace: Metric namespace, e.g. "AWS/EC2" or "AWS/RDS".
        metric_name: Metric name, e.g. "CPUUtilization".
        dimension_name: Optional single dimension name, e.g. "InstanceId".
        dimension_value: Value for dimension_name (required if it is set).
        stat: Statistic — "Average", "Sum", "Minimum", "Maximum", or
            "SampleCount". Defaults to "Average".
        minutes: How far back to look, in minutes. Defaults to 60.
        period: Granularity of each datapoint, in seconds. Defaults to 300.

    Returns:
        A JSON list of timestamped datapoints, or an error string.
    """
    try:
        cw = boto3.client("cloudwatch", region_name=AWS_REGION)
        dimensions = []
        if dimension_name and dimension_value:
            dimensions = [{"Name": dimension_name, "Value": dimension_value}]
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes)
        resp = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=start,
            EndTime=end,
            Period=period,
            Statistics=[stat],
        )
        points = sorted(
            (
                {"timestamp": dp["Timestamp"].isoformat(), stat: dp[stat]}
                for dp in resp.get("Datapoints", [])
            ),
            key=lambda p: p["timestamp"],
        )
        return json.dumps(points, indent=2)
    except (BotoCoreError, ClientError) as e:
        return f"Error fetching metric statistics: {e}"


@tool
def query_logs(log_group: str, filter_pattern: str = "", minutes: int = 30) -> str:
    """Search a CloudWatch Logs group for recent matching events.

    Args:
        log_group: The log group name, e.g. "/aws/lambda/my-fn".
        filter_pattern: CloudWatch Logs filter pattern (empty = all events).
        minutes: How far back to search, in minutes. Defaults to 30.

    Returns:
        A newline-separated list of matching log messages (up to 100), or an
        error string.
    """
    try:
        logs = boto3.client("logs", region_name=AWS_REGION)
        start_ms = int(
            (datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp() * 1000
        )
        resp = logs.filter_log_events(
            logGroupName=log_group,
            startTime=start_ms,
            filterPattern=filter_pattern,
            limit=100,
        )
        messages = [e.get("message", "").rstrip() for e in resp.get("events", [])]
        return "\n".join(messages) if messages else "(no matching log events)"
    except (BotoCoreError, ClientError) as e:
        return f"Error querying logs: {e}"


# ---------------------------------------------------------------------------
# Kubernetes (read-only)
# ---------------------------------------------------------------------------


@tool
def list_clusters() -> str:
    """List all EKS clusters in the account for the configured AWS region.

    Use this to discover which clusters exist before targeting one with the
    other Kubernetes tools (each takes a `cluster` argument).

    Returns:
        A JSON list of cluster names, or an error string.
    """
    try:
        return json.dumps(list_eks_clusters(), indent=2)
    except (BotoCoreError, ClientError) as e:
        return f"Error listing EKS clusters: {e}"


@tool
def kubectl_get(
    resource_type: str, namespace: str = "default", cluster: str = ""
) -> str:
    """List Kubernetes resources of a given type in a namespace.

    Args:
        resource_type: One of "pods", "deployments", "services", "events".
        namespace: The namespace to query. Defaults to "default".
        cluster: EKS cluster to target. Defaults to EKS_CLUSTER_NAME if unset.
            Use list_clusters() to discover names.

    Returns:
        A JSON summary of the resources, or an error string.
    """
    try:
        load_k8s(cluster or None)
        core = k8s_client.CoreV1Api()
        apps = k8s_client.AppsV1Api()
        rt = resource_type.lower()
        if rt == "pods":
            items = [
                {
                    "name": p.metadata.name,
                    "phase": p.status.phase,
                    "restarts": sum(
                        cs.restart_count for cs in (p.status.container_statuses or [])
                    ),
                }
                for p in core.list_namespaced_pod(namespace).items
            ]
        elif rt == "deployments":
            items = [
                {
                    "name": d.metadata.name,
                    "ready": f"{d.status.ready_replicas or 0}/{d.spec.replicas}",
                }
                for d in apps.list_namespaced_deployment(namespace).items
            ]
        elif rt == "services":
            items = [
                {"name": s.metadata.name, "type": s.spec.type}
                for s in core.list_namespaced_service(namespace).items
            ]
        elif rt == "events":
            items = [
                {
                    "reason": e.reason,
                    "message": e.message,
                    "object": f"{e.involved_object.kind}/{e.involved_object.name}",
                }
                for e in core.list_namespaced_event(namespace).items
            ]
        else:
            return f"Unsupported resource_type: {resource_type}"
        return json.dumps(items, indent=2)
    except ApiException as e:
        return f"Kubernetes API error: {e.status} {e.reason}"
    except Exception as e:  # config load / connection failures
        return f"Error listing {resource_type}: {e}"


@tool
def kubectl_describe(
    resource_type: str, name: str, namespace: str = "default", cluster: str = ""
) -> str:
    """Describe a single Kubernetes pod or deployment, including related events.

    Args:
        resource_type: "pod" or "deployment".
        name: The resource name.
        namespace: The namespace. Defaults to "default".
        cluster: EKS cluster to target. Defaults to EKS_CLUSTER_NAME if unset.
            Use list_clusters() to discover names.

    Returns:
        A JSON object with the resource status and its recent events, or an
        error string.
    """
    try:
        load_k8s(cluster or None)
        core = k8s_client.CoreV1Api()
        rt = resource_type.lower()
        if rt == "pod":
            obj = core.read_namespaced_pod(name, namespace)
            status = {
                "phase": obj.status.phase,
                "conditions": [
                    {"type": c.type, "status": c.status, "reason": c.reason}
                    for c in (obj.status.conditions or [])
                ],
            }
        elif rt == "deployment":
            apps = k8s_client.AppsV1Api()
            obj = apps.read_namespaced_deployment(name, namespace)
            status = {
                "replicas": obj.spec.replicas,
                "ready_replicas": obj.status.ready_replicas,
                "unavailable_replicas": obj.status.unavailable_replicas,
            }
        else:
            return f"Unsupported resource_type: {resource_type}"

        field_selector = f"involvedObject.name={name}"
        events = [
            {"reason": e.reason, "message": e.message}
            for e in core.list_namespaced_event(
                namespace, field_selector=field_selector
            ).items
        ]
        return json.dumps({"status": status, "events": events}, indent=2)
    except ApiException as e:
        return f"Kubernetes API error: {e.status} {e.reason}"
    except Exception as e:
        return f"Error describing {resource_type}/{name}: {e}"


@tool
def pod_logs(
    pod_name: str,
    namespace: str = "default",
    container: str = "",
    tail: int = 100,
    cluster: str = "",
) -> str:
    """Fetch recent logs from a Kubernetes pod.

    Args:
        pod_name: The pod name.
        namespace: The namespace. Defaults to "default".
        container: Container name (required only for multi-container pods).
        tail: Number of trailing lines to return. Defaults to 100.
        cluster: EKS cluster to target. Defaults to EKS_CLUSTER_NAME if unset.
            Use list_clusters() to discover names.

    Returns:
        The log text, or an error string.
    """
    try:
        load_k8s(cluster or None)
        core = k8s_client.CoreV1Api()
        kwargs = {"name": pod_name, "namespace": namespace, "tail_lines": tail}
        if container:
            kwargs["container"] = container
        return core.read_namespaced_pod_log(**kwargs) or "(no logs)"
    except ApiException as e:
        return f"Kubernetes API error: {e.status} {e.reason}"
    except Exception as e:
        return f"Error fetching logs for {pod_name}: {e}"
