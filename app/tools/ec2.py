"""EC2 investigation and remediation tools.

Complements the EKS/Kubernetes tools: when a CloudWatch alarm on an EC2 instance
fires (via the push path), the agent uses these to inspect the instance and, if
needed, recover it. Mutating actions are gated by the same global DRY_RUN switch
as the Kubernetes remediation tools.
"""

import json
import os

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

from .remediation import DRY_RUN, _dry_run_notice

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


def _ec2():
    return boto3.client("ec2", region_name=AWS_REGION)


# ---------------------------------------------------------------------------
# EC2 investigation (read-only)
# ---------------------------------------------------------------------------


@tool
def describe_instance(instance_id: str) -> str:
    """Describe an EC2 instance: state, type, AZ, launch time, IPs, and tags.

    Args:
        instance_id: The instance ID, e.g. "i-0abc123". Usually taken from the
            InstanceId dimension of the CloudWatch alarm that fired.

    Returns:
        A JSON object with the instance's key attributes, or an error string.
    """
    try:
        resp = _ec2().describe_instances(InstanceIds=[instance_id])
        reservations = resp.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            return f"Instance {instance_id} not found."
        i = reservations[0]["Instances"][0]
        launch = i.get("LaunchTime")
        info = {
            "instance_id": i.get("InstanceId"),
            "state": (i.get("State") or {}).get("Name"),
            "state_transition_reason": i.get("StateTransitionReason"),
            "instance_type": i.get("InstanceType"),
            "availability_zone": (i.get("Placement") or {}).get("AvailabilityZone"),
            "launch_time": launch.isoformat() if launch else None,
            "private_ip": i.get("PrivateIpAddress"),
            "public_ip": i.get("PublicIpAddress"),
            "tags": {t["Key"]: t["Value"] for t in i.get("Tags", [])},
        }
        return json.dumps(info, indent=2)
    except (BotoCoreError, ClientError) as e:
        return f"Error describing instance {instance_id}: {e}"


@tool
def get_instance_status(instance_id: str) -> str:
    """Get an EC2 instance's system and instance status checks.

    A failed *instance* status check usually points at the instance itself
    (reboot is the typical fix); a failed *system* status check points at the
    underlying host (stop + start migrates to new hardware).

    Args:
        instance_id: The instance ID, e.g. "i-0abc123".

    Returns:
        A JSON object with the instance state and both status checks, or an
        error string.
    """
    try:
        resp = _ec2().describe_instance_status(
            InstanceIds=[instance_id], IncludeAllInstances=True
        )
        statuses = resp.get("InstanceStatuses", [])
        if not statuses:
            return f"No status information for instance {instance_id}."
        s = statuses[0]
        instance_status = s.get("InstanceStatus") or {}
        system_status = s.get("SystemStatus") or {}
        info = {
            "instance_id": s.get("InstanceId"),
            "instance_state": (s.get("InstanceState") or {}).get("Name"),
            "instance_status": instance_status.get("Status"),
            "system_status": system_status.get("Status"),
            "checks": [
                {"name": d.get("Name"), "status": d.get("Status")}
                for d in instance_status.get("Details", [])
                + system_status.get("Details", [])
            ],
        }
        return json.dumps(info, indent=2)
    except (BotoCoreError, ClientError) as e:
        return f"Error fetching status for instance {instance_id}: {e}"


# ---------------------------------------------------------------------------
# EC2 remediation (mutating, DRY_RUN-gated)
# ---------------------------------------------------------------------------


@tool
def reboot_instance(instance_id: str) -> str:
    """Reboot an EC2 instance in place (preserves the instance and its storage).

    The safest EC2 recovery — prefer it for a failed *instance* status check or a
    hung/overloaded instance.

    Args:
        instance_id: The instance ID, e.g. "i-0abc123".

    Returns:
        A confirmation string, a DRY_RUN notice, or an error string.
    """
    if DRY_RUN:
        return _dry_run_notice(f"reboot EC2 instance {instance_id}")
    try:
        _ec2().reboot_instances(InstanceIds=[instance_id])
        return f"Rebooted EC2 instance {instance_id}."
    except (BotoCoreError, ClientError) as e:
        return f"Error rebooting instance {instance_id}: {e}"


@tool
def stop_instance(instance_id: str) -> str:
    """Stop an EC2 instance.

    Paired with start_instance, this recovers a failed *system* status check by
    moving the instance to new host hardware. Data on EBS volumes is preserved;
    any instance-store data and the public IP (if not an Elastic IP) are lost.

    Args:
        instance_id: The instance ID, e.g. "i-0abc123".

    Returns:
        A confirmation string, a DRY_RUN notice, or an error string.
    """
    if DRY_RUN:
        return _dry_run_notice(f"stop EC2 instance {instance_id}")
    try:
        _ec2().stop_instances(InstanceIds=[instance_id])
        return f"Stopping EC2 instance {instance_id}."
    except (BotoCoreError, ClientError) as e:
        return f"Error stopping instance {instance_id}: {e}"


@tool
def start_instance(instance_id: str) -> str:
    """Start a stopped EC2 instance.

    Args:
        instance_id: The instance ID, e.g. "i-0abc123".

    Returns:
        A confirmation string, a DRY_RUN notice, or an error string.
    """
    if DRY_RUN:
        return _dry_run_notice(f"start EC2 instance {instance_id}")
    try:
        _ec2().start_instances(InstanceIds=[instance_id])
        return f"Starting EC2 instance {instance_id}."
    except (BotoCoreError, ClientError) as e:
        return f"Error starting instance {instance_id}: {e}"
