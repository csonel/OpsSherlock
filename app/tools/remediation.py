import os
import subprocess
from datetime import datetime, timezone

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from strands import tool

from .k8s_client import load_k8s, write_kubeconfig

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"


def _dry_run_notice(action: str) -> str:
    return f"[DRY_RUN] Would {action}. Set DRY_RUN=false to execute."


def _run(cmd: list[str]) -> str:
    """Run a CLI command and return combined output or an error string."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return f"Command failed ({' '.join(cmd)}): {result.stderr.strip()}"
        return result.stdout.strip() or f"Command succeeded: {' '.join(cmd)}"
    except FileNotFoundError:
        return f"Binary not found: {cmd[0]} (is it installed in the runtime?)"
    except subprocess.TimeoutExpired:
        return f"Command timed out: {' '.join(cmd)}"


# ---------------------------------------------------------------------------
# Kubernetes actions (mutating, DRY_RUN-gated)
# ---------------------------------------------------------------------------


@tool
def rollout_restart(
    deployment: str, namespace: str = "default", cluster: str = ""
) -> str:
    """Trigger a rolling restart of a Kubernetes deployment.

    Args:
        deployment: The deployment name.
        namespace: The namespace. Defaults to "default".
        cluster: EKS cluster to target. Defaults to EKS_CLUSTER_NAME if unset.
            Use list_clusters() to discover names.

    Returns:
        A confirmation string, a DRY_RUN notice, or an error string.
    """
    if DRY_RUN:
        return _dry_run_notice(f"restart deployment {deployment} in {namespace}")
    try:
        load_k8s(cluster or None)
        apps = k8s_client.AppsV1Api()
        now = datetime.now(timezone.utc).isoformat()
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"kubectl.kubernetes.io/restartedAt": now}
                    }
                }
            }
        }
        apps.patch_namespaced_deployment(deployment, namespace, body)
        return f"Restarted deployment {deployment} in {namespace}."
    except ApiException as e:
        return f"Kubernetes API error: {e.status} {e.reason}"
    except Exception as e:
        return f"Error restarting {deployment}: {e}"


@tool
def scale_deployment(
    deployment: str, replicas: int, namespace: str = "default", cluster: str = ""
) -> str:
    """Scale a Kubernetes deployment to a given replica count.

    Args:
        deployment: The deployment name.
        replicas: Desired number of replicas.
        namespace: The namespace. Defaults to "default".
        cluster: EKS cluster to target. Defaults to EKS_CLUSTER_NAME if unset.
            Use list_clusters() to discover names.

    Returns:
        A confirmation string, a DRY_RUN notice, or an error string.
    """
    if DRY_RUN:
        return _dry_run_notice(
            f"scale deployment {deployment} to {replicas} replicas in {namespace}"
        )
    try:
        load_k8s(cluster or None)
        apps = k8s_client.AppsV1Api()
        apps.patch_namespaced_deployment_scale(
            deployment, namespace, {"spec": {"replicas": replicas}}
        )
        return f"Scaled deployment {deployment} to {replicas} replicas in {namespace}."
    except ApiException as e:
        return f"Kubernetes API error: {e.status} {e.reason}"
    except Exception as e:
        return f"Error scaling {deployment}: {e}"


@tool
def rollback_deployment(
    deployment: str, namespace: str = "default", cluster: str = ""
) -> str:
    """Roll a deployment back to its previous revision (kubectl rollout undo).

    This is the safest, reversible remediation and should be preferred over
    restart or scale when a bad rollout is suspected.

    Args:
        deployment: The deployment name.
        namespace: The namespace. Defaults to "default".
        cluster: EKS cluster to target. Defaults to EKS_CLUSTER_NAME if unset.
            Use list_clusters() to discover names.

    Returns:
        The command output, a DRY_RUN notice, or an error string.
    """
    if DRY_RUN:
        return _dry_run_notice(
            f"roll back deployment {deployment} to its previous revision in {namespace}"
        )
    try:
        write_kubeconfig(cluster or None)
    except Exception as e:
        return f"Error preparing cluster access: {e}"
    return _run(["kubectl", "rollout", "undo", f"deployment/{deployment}", "-n", namespace])


# ---------------------------------------------------------------------------
# Helm actions (mutating, DRY_RUN-gated)
# ---------------------------------------------------------------------------


@tool
def helm_rollback(
    release: str, revision: int = 0, namespace: str = "default", cluster: str = ""
) -> str:
    """Roll a Helm release back to a previous revision.

    Args:
        release: The Helm release name.
        revision: Target revision number. 0 (default) means the previous revision.
        namespace: The namespace. Defaults to "default".
        cluster: EKS cluster to target. Defaults to EKS_CLUSTER_NAME if unset.
            Use list_clusters() to discover names.

    Returns:
        The command output, a DRY_RUN notice, or an error string.
    """
    target = str(revision) if revision else "previous revision"
    if DRY_RUN:
        return _dry_run_notice(
            f"roll back Helm release {release} to {target} in {namespace}"
        )
    try:
        write_kubeconfig(cluster or None)
    except Exception as e:
        return f"Error preparing cluster access: {e}"
    cmd = ["helm", "rollback", release]
    if revision:
        cmd.append(str(revision))
    cmd += ["-n", namespace]
    return _run(cmd)
