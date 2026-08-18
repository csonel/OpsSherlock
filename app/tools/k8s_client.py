"""Kubernetes client configuration shared by the OpsSherlock tools.

load_k8s(cluster) configures the default kubernetes client for a specific EKS
cluster. The cluster is resolved from: the explicit argument -> the
EKS_CLUSTER_NAME env var (optional single-cluster default) -> a local kubeconfig
file (dev fallback when neither is set). Because the kubernetes client has one
active config at a time, tools target one cluster per call; use list_clusters()
to discover the clusters in the account.

The EKS path needs no kubeconfig on disk: it reads the cluster endpoint + CA via
boto3 describe_cluster and mints a short-lived bearer token by SigV4-signing an
STS GetCallerIdentity request (the same scheme as `aws eks get-token`). The
runtime IAM role must have eks:DescribeCluster and be mapped via an EKS access
entry / aws-auth to a Kubernetes RBAC identity.
"""

import base64
import os
import tempfile

import boto3
import yaml
from botocore.signers import RequestSigner
from kubernetes import client as k8s_client, config as k8s_config

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# cluster name -> (endpoint, ca_cert_path). Endpoint/CA are stable, so they are
# cached to avoid a describe_cluster call on every tool invocation. The bearer
# token is short-lived and minted fresh on each call.
_cluster_cache: dict[str, tuple[str, str]] = {}

# Path of the kubeconfig file written for CLI subprocesses (helm/kubectl).
_kubeconfig_path: str | None = None


def _write_ca(ca_data: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".pem", prefix="eks-ca-")
    with os.fdopen(fd, "wb") as f:
        f.write(base64.b64decode(ca_data))
    return path


def _get_eks_token(cluster_name: str) -> str:
    """Mint a short-lived EKS bearer token by presigning STS GetCallerIdentity."""
    session = boto3.session.Session()
    sts = session.client("sts", region_name=AWS_REGION)
    signer = RequestSigner(
        sts.meta.service_model.service_id,
        AWS_REGION,
        "sts",
        "v4",
        session.get_credentials(),
        session.events,
    )
    signed_url = signer.generate_presigned_url(
        {
            "method": "GET",
            "url": (
                f"https://sts.{AWS_REGION}.amazonaws.com/"
                "?Action=GetCallerIdentity&Version=2011-06-15"
            ),
            "body": {},
            "headers": {"x-k8s-aws-id": cluster_name},
            "context": {},
        },
        region_name=AWS_REGION,
        expires_in=60,
        operation_name="",
    )
    return "k8s-aws-v1." + base64.urlsafe_b64encode(signed_url.encode()).decode().rstrip("=")


def list_eks_clusters() -> list[str]:
    """Return all EKS cluster names in the account for the configured region."""
    eks = boto3.client("eks", region_name=AWS_REGION)
    names: list[str] = []
    paginator = eks.get_paginator("list_clusters")
    for page in paginator.paginate():
        names.extend(page.get("clusters", []))
    return names


def _resolve_cluster(cluster: str | None) -> str | None:
    """Resolve the target cluster: explicit arg -> EKS_CLUSTER_NAME -> None (dev)."""
    return cluster or os.environ.get("EKS_CLUSTER_NAME") or None


def load_k8s(cluster: str | None = None) -> None:
    """Configure the default kubernetes client for a cluster.

    Resolves the cluster from the argument, then the EKS_CLUSTER_NAME env var.
    Uses EKS (describe_cluster + minted token) when a cluster is resolved,
    otherwise falls back to a local kubeconfig file for dev.
    """
    cluster = _resolve_cluster(cluster)
    if not cluster:
        k8s_config.load_kube_config()
        return

    if cluster not in _cluster_cache:
        eks = boto3.client("eks", region_name=AWS_REGION)
        desc = eks.describe_cluster(name=cluster)["cluster"]
        _cluster_cache[cluster] = (
            desc["endpoint"],
            _write_ca(desc["certificateAuthority"]["data"]),
        )
    endpoint, ca_path = _cluster_cache[cluster]

    cfg = k8s_client.Configuration()
    cfg.host = endpoint
    cfg.ssl_ca_cert = ca_path
    cfg.api_key = {"authorization": "Bearer " + _get_eks_token(cluster)}
    k8s_client.Configuration.set_default(cfg)


def write_kubeconfig(cluster: str | None = None) -> None:
    """Write a kubeconfig file so CLI subprocesses (helm, kubectl) can reach EKS.

    The in-memory client config from load_k8s() does not reach child processes,
    so the CLI tools need a kubeconfig on disk. This mints a fresh token and
    points KUBECONFIG at the generated file. No-op when no cluster is resolved
    (dev relies on the user's existing ~/.kube/config).
    """
    global _kubeconfig_path
    cluster = _resolve_cluster(cluster)
    if not cluster:
        return

    load_k8s(cluster)  # ensures endpoint/CA are cached
    endpoint, ca_path = _cluster_cache[cluster]
    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {"name": cluster, "cluster": {"server": endpoint, "certificate-authority": ca_path}}
        ],
        "users": [{"name": cluster, "user": {"token": _get_eks_token(cluster)}}],
        "contexts": [{"name": cluster, "context": {"cluster": cluster, "user": cluster}}],
        "current-context": cluster,
    }
    if _kubeconfig_path is None:
        fd, _kubeconfig_path = tempfile.mkstemp(suffix=".yaml", prefix="kubeconfig-")
        os.close(fd)
    with open(_kubeconfig_path, "w") as f:
        yaml.safe_dump(kubeconfig, f)
    os.environ["KUBECONFIG"] = _kubeconfig_path
