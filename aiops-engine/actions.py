"""Actions directes : liste fermée, cible revérifiée ici (2e barrière), RBAC en dernier recours (3e)."""
import datetime as dt

from schemas import Diagnosis

DIRECT_ACTIONS = {"restart_deployment", "scale_deployment"}
DIRECT_TARGETS = {("dev", "demo-app")}  # seuls couples (namespace, deployment) modifiables directement
REPLICAS_RANGE = (1, 5)


class ActionRefused(Exception):
    pass


def check_allowed(d: Diagnosis, namespace: str, deployment: str, direct_namespaces: set[str]) -> None:
    if d.action not in DIRECT_ACTIONS:
        raise ActionRefused(f"action non autorisée en direct : {d.action}")
    if (namespace, deployment) not in DIRECT_TARGETS or namespace not in direct_namespaces:
        raise ActionRefused(f"cible non autorisée en direct : {namespace}/{deployment}")
    if d.action == "scale_deployment" and not REPLICAS_RANGE[0] <= d.replicas <= REPLICAS_RANGE[1]:
        raise ActionRefused(f"replicas hors bornes : {d.replicas}")


def restart_body(now: dt.datetime) -> dict:
    return {"spec": {"template": {"metadata": {"annotations": {
        "kubectl.kubernetes.io/restartedAt": now.isoformat()}}}}}


def scale_body(replicas: int) -> dict:
    return {"spec": {"replicas": replicas}}


def run_direct(d: Diagnosis, namespace: str, deployment: str, direct_namespaces: set[str]) -> str:
    check_allowed(d, namespace, deployment, direct_namespaces)
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    apps = client.AppsV1Api()

    if d.action == "restart_deployment":
        apps.patch_namespaced_deployment(deployment, namespace, restart_body(dt.datetime.now(dt.timezone.utc)))
        return f"restart de {namespace}/{deployment}"
    apps.patch_namespaced_deployment_scale(deployment, namespace, scale_body(d.replicas))
    return f"scale de {namespace}/{deployment} à {d.replicas}"
