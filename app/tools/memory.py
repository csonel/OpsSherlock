"""AgentCore Memory-backed incident tracking: dedup + recall.

An always-on agent must not re-investigate — or, once remediation graduates out of
DRY_RUN, re-remediate — the same incident on every scan. This module records each
incident's status as an event in an AgentCore Memory and lets the agent check
whether an incident is already being tracked before it acts.

Dedup reads the raw event log (``list_events``), which is strongly consistent and
readable immediately after ``create_event`` — so a concurrent run's incident is
seen at once. The SEMANTIC long-term records are extracted asynchronously and are
unsafe to gate remediation on; they power ``recall_similar_incidents`` instead,
where fuzzy matching is a feature, not a hazard.
"""

import os
import re
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# All incidents share one actor/session so the event log is a single stream across
# invocations (each runtime call would otherwise be an isolated session).
_ACTOR_ID = os.environ.get("INCIDENT_ACTOR_ID", "opssherlock")
_SESSION_ID = os.environ.get("INCIDENT_SESSION_ID", "incidents")
_MEMORY_NAME = os.environ.get("INCIDENT_MEMORY_NAME", "OpsSherlockIncidents")

# An incident whose latest status is still "open" within this window counts as
# already-tracked; recurrences after resolution — or staleness past the window —
# are treated as new.
_COOLDOWN_MINUTES = int(os.environ.get("INCIDENT_COOLDOWN_MINUTES", "30"))
_OPEN_STATUSES = {"open", "investigating", "remediated", "escalated"}

# Bound the log scan; the log holds infrequent status records within the memory's
# retention window, so this is generous.
_MAX_EVENTS_SCAN = 2000

# "Incident <key> → <status>." — natural-language-ish (helps semantic extraction)
# yet trivially parseable for exact dedup.
_EVENT_RE = re.compile(r"^Incident (.+?) → (\w+)\.")

_memory_id_cache = None


def _data_client():
    return boto3.client("bedrock-agentcore", region_name=AWS_REGION)


def _memory_id():
    """Resolve the incident memory id: explicit env override, else look it up by
    name on the control plane (the runtime injects no memory-id env var)."""
    global _memory_id_cache
    if _memory_id_cache:
        return _memory_id_cache
    explicit = os.environ.get("INCIDENT_MEMORY_ID")
    if explicit:
        _memory_id_cache = explicit
        return _memory_id_cache
    ctrl = boto3.client("bedrock-agentcore-control", region_name=AWS_REGION)
    resp = ctrl.list_memories(maxResults=100)
    memories = list(resp.get("memories", []))
    while resp.get("nextToken"):
        resp = ctrl.list_memories(maxResults=100, nextToken=resp["nextToken"])
        memories.extend(resp.get("memories", []))
    for m in memories:
        # CLI-provisioned memory ids are often "<name>-<suffix>".
        if m.get("name") == _MEMORY_NAME or m.get("id", "").startswith(_MEMORY_NAME):
            _memory_id_cache = m["id"]
            return _memory_id_cache
    raise RuntimeError(f"No AgentCore Memory named '{_MEMORY_NAME}' found")


def _record(key: str, status: str, detail: str) -> None:
    text = f"Incident {key} → {status}."
    if detail:
        text += f" {detail}"
    now = datetime.now(timezone.utc)
    _data_client().create_event(
        memoryId=_memory_id(),
        actorId=_ACTOR_ID,
        sessionId=_SESSION_ID,
        eventTimestamp=now,
        payload=[{"conversational": {"content": {"text": text}, "role": "OTHER"}}],
    )


def _parse_event(ev: dict):
    try:
        text = ev["payload"][0]["conversational"]["content"]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    m = _EVENT_RE.match(text or "")
    if not m:
        return None
    return {"incident": m.group(1), "status": m.group(2), "ts": ev.get("eventTimestamp")}


def _latest_status(key: str):
    """Latest (status, eventTimestamp) recorded for ``key``, or (None, None).

    Order-independent: scans the log and keeps the record with the max timestamp,
    so it is correct regardless of how the API pages results.
    """
    client = _data_client()
    mem = _memory_id()
    latest = None
    token = None
    scanned = 0
    while True:
        kwargs = {
            "memoryId": mem,
            "actorId": _ACTOR_ID,
            "sessionId": _SESSION_ID,
            "includePayloads": True,
            "maxResults": 100,
        }
        if token:
            kwargs["nextToken"] = token
        resp = client.list_events(**kwargs)
        events = resp.get("events", [])
        for ev in events:
            rec = _parse_event(ev)
            if rec and rec["incident"] == key and rec["ts"] is not None:
                if latest is None or rec["ts"] > latest[1]:
                    latest = (rec["status"], rec["ts"])
        scanned += len(events)
        token = resp.get("nextToken")
        if not token or scanned >= _MAX_EVENTS_SCAN:
            break
    return latest if latest else (None, None)


def _age_minutes(dt) -> float:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60


@tool
def incident_seen(key: str) -> str:
    """Check whether an incident is already being tracked, for deduplication.

    Call this BEFORE investigating or remediating anything found in a scan or
    alarm. If it reports the incident is already open, skip it — do not
    re-investigate or re-remediate. Reads the strongly-consistent event log, so an
    incident recorded moments ago by a concurrent run is seen immediately.

    Args:
        key: Stable incident identity, e.g. "cluster/namespace/workload/signal"
            (like "prod/payments/api/CrashLoopBackOff"). Use the SAME key across
            the whole lifecycle of one incident.

    Returns:
        "NEW ..." if untracked, previously resolved, or stale (safe to act), or
        "ALREADY TRACKING ..." if an open incident exists within the cooldown
        window (skip it).
    """
    try:
        status, ts = _latest_status(key)
    except (BotoCoreError, ClientError, RuntimeError) as e:
        return f"Error checking incident {key}: {e}"
    if status is None:
        return f"NEW — no prior record for {key}. Safe to investigate."
    if status not in _OPEN_STATUSES:
        return f"NEW — {key} was last '{status}'; treat this recurrence as new."
    age = _age_minutes(ts)
    if age > _COOLDOWN_MINUTES:
        return (
            f"NEW — {key} is still '{status}' but stale ({age:.0f}m old, cooldown "
            f"{_COOLDOWN_MINUTES}m); re-investigate, it may be stuck."
        )
    return (
        f"ALREADY TRACKING — {key} is '{status}' ({age:.0f}m ago). "
        f"Skip it; do not re-investigate or re-remediate."
    )


@tool
def record_incident(key: str, status: str, detail: str = "") -> str:
    """Record an incident's status in memory so later scans/alarms can dedup it.

    Record "open" when you start handling an incident, "remediated" after acting,
    "escalated" when you hand off to a human, and — importantly — "resolved" once
    recovery is confirmed, so a later recurrence is treated as new.

    Args:
        key: Stable incident identity; use the SAME key as incident_seen across
            the incident's lifecycle.
        status: One of "open", "investigating", "remediated", "escalated",
            "resolved".
        detail: Optional short note (root cause or action taken).

    Returns:
        A confirmation string, or an error string.
    """
    try:
        _record(key, status, detail)
        return f"Recorded incident {key} as '{status}'."
    except (BotoCoreError, ClientError, RuntimeError) as e:
        return f"Error recording incident {key}: {e}"


@tool
def recall_similar_incidents(description: str, top_k: int = 5) -> str:
    """Semantically recall past incidents similar to a description, for RCA context.

    Surfaces historically similar incidents and how they were handled. Recall
    only — never use this to decide dedup (use incident_seen for that); these
    records are eventually consistent.

    Args:
        description: Free-text description of the current problem.
        top_k: Maximum number of similar records to return. Defaults to 5.

    Returns:
        The similar incident snippets with relevance scores, a "none found"
        message, or an error string.
    """
    try:
        resp = _data_client().retrieve_memory_records(
            memoryId=_memory_id(),
            namespace=f"/incidents/{_ACTOR_ID}",
            searchCriteria={"searchQuery": description, "topK": top_k},
            maxResults=top_k,
        )
        records = resp.get("memoryRecordSummaries", [])
        if not records:
            return "No similar past incidents found."
        lines = [
            f"- ({r.get('score', 0):.2f}) {r.get('content', {}).get('text', '')}"
            for r in records
        ]
        return "Similar past incidents:\n" + "\n".join(lines)
    except (BotoCoreError, ClientError, RuntimeError) as e:
        return f"Error recalling incidents: {e}"
