"""Actions : redémarrage direct (dev) ou Pull Request. La cible est revérifiée ici (2e barrière)."""
import base64
import datetime as dt
import os
import re

from allowlist import MEMORY_MI_RANGE, REPLICAS_RANGE, _MEM_RE, _TAG_RE
from schemas import Diagnosis

DIRECT_ACTIONS = {"restart_deployment"}  # le reste est versionné dans Git : on passe par une PR
DIRECT_TARGETS = {("dev", "demo-app")}
PR_ACTIONS = {"scale_deployment", "set_memory_limit", "rollback_image"}
PR_NAMESPACES = {"dev", "prod"}
PR_DEPLOYMENTS = {"demo-app"}
GITHUB_API = "https://api.github.com"
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
BASE_BRANCH = os.getenv("GITHUB_BASE_BRANCH", "main")
_BLOCK_LINES = 6


class ActionRefused(Exception):
    pass


# ---------- action directe ----------
def check_allowed(d: Diagnosis, namespace: str, deployment: str, direct_namespaces: set[str]) -> None:
    if d.action not in DIRECT_ACTIONS:
        raise ActionRefused(f"action non autorisée en direct : {d.action}")
    if (namespace, deployment) not in DIRECT_TARGETS or namespace not in direct_namespaces:
        raise ActionRefused(f"cible non autorisée en direct : {namespace}/{deployment}")


def restart_body(now: dt.datetime) -> dict:
    return {"spec": {"template": {"metadata": {"annotations": {
        "kubectl.kubernetes.io/restartedAt": now.isoformat()}}}}}


def run_direct(d: Diagnosis, namespace: str, deployment: str, direct_namespaces: set[str]) -> str:
    check_allowed(d, namespace, deployment, direct_namespaces)
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    client.AppsV1Api().patch_namespaced_deployment(
        deployment, namespace, restart_body(dt.datetime.now(dt.timezone.utc)))
    return f"restart de {namespace}/{deployment}"


# ---------- Pull Request ----------
def check_pr_allowed(d: Diagnosis, namespace: str, deployment: str) -> None:
    if d.action not in PR_ACTIONS:
        raise ActionRefused(f"action non autorisée par PR : {d.action}")
    if namespace not in PR_NAMESPACES or deployment not in PR_DEPLOYMENTS:
        raise ActionRefused(f"cible non autorisée par PR : {namespace}/{deployment}")
    if d.action == "scale_deployment" and not REPLICAS_RANGE[0] <= d.replicas <= REPLICAS_RANGE[1]:
        raise ActionRefused(f"replicas hors bornes : {d.replicas}")
    if d.action == "set_memory_limit":
        m = _MEM_RE.match(d.memory_limit)
        if not m or not MEMORY_MI_RANGE[0] <= int(m.group(1)) <= MEMORY_MI_RANGE[1]:
            raise ActionRefused(f"memory_limit invalide : {d.memory_limit!r}")
    if d.action == "rollback_image" and not _TAG_RE.match(d.image_tag):
        raise ActionRefused(f"image_tag invalide : {d.image_tag!r}")


def _block(marker: str, path: str, value) -> str:
    return (f"  # aiops:{marker}\n"
            "  - target: {kind: Deployment, name: demo-app}\n"
            "    patch: |-\n"
            "      - op: replace\n"
            f"        path: {path}\n"
            f"        value: {value}\n")


def patch_kustomization_text(raw: str, d: Diagnosis) -> str:
    """Modification minimale du fichier (diff lisible en revue) : un bloc ajouté ou un tag remplacé."""
    if d.action == "rollback_image":
        new, n = re.subn(r"(newTag:\s*)\S+", rf"\g<1>{d.image_tag}", raw, count=1)
        if n != 1:
            raise ValueError("newTag introuvable dans le fichier")
        return new
    if "patches:" not in raw:
        raise ValueError("section patches introuvable dans le fichier")
    if d.action == "set_memory_limit":
        marker, path, value = "memory", "/spec/template/spec/containers/0/resources/limits/memory", d.memory_limit
    elif d.action == "scale_deployment":
        marker, path, value = "replicas", "/spec/replicas", d.replicas
    else:
        raise ValueError(f"pas de modification de manifest pour {d.action}")
    lines = raw.splitlines(keepends=True)
    tag = f"  # aiops:{marker}\n"
    if tag in lines:  # un bloc précédent du même type est remplacé (idempotent)
        i = lines.index(tag)
        del lines[i:i + _BLOCK_LINES]
    text = "".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    return text + _block(marker, path, value)


def _clean(text: str, n: int = 300) -> str:
    """Texte produit par le LLM : pas de mentions @, de HTML ni de retours à la ligne dans la PR."""
    return re.sub(r"[@`<>\r\n]+", " ", text or "")[:n]


def _describe(d: Diagnosis) -> str:
    return {"scale_deployment": f"passer à {d.replicas} réplicas",
            "set_memory_limit": f"limite mémoire à {d.memory_limit}",
            "rollback_image": f"rollback vers {d.image_tag}"}[d.action]


def _gh_client():
    import httpx

    return httpx.Client(base_url=f"{GITHUB_API}/repos/{GITHUB_REPO}", timeout=30,
                        headers={"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN', '')}",
                                 "Accept": "application/vnd.github+json"})


def open_pull_request(d: Diagnosis, namespace: str, deployment: str, incident_id: str, alertname: str) -> str:
    check_pr_allowed(d, namespace, deployment)
    if not re.fullmatch(r"[0-9a-f]{8}", incident_id):
        raise ActionRefused("identifiant d'incident invalide")
    if not GITHUB_REPO or not os.getenv("GITHUB_TOKEN"):
        raise ActionRefused("GITHUB_REPO / GITHUB_TOKEN non configurés")
    path = f"manifests/overlays/{namespace}/kustomization.yaml"
    branch = f"aiops/{incident_id}"

    with _gh_client() as c:
        ref = c.get(f"/git/ref/heads/{BASE_BRANCH}")
        ref.raise_for_status()
        c.post("/git/refs", json={"ref": f"refs/heads/{branch}", "sha": ref.json()["object"]["sha"]}).raise_for_status()

        f = c.get(f"/contents/{path}", params={"ref": BASE_BRANCH})
        f.raise_for_status()
        meta = f.json()
        new = patch_kustomization_text(base64.b64decode(meta["content"]).decode(), d)

        c.put(f"/contents/{path}", json={
            "message": f"aiops: {_describe(d)} sur {namespace}/{deployment} ({_clean(alertname, 60)})",
            "content": base64.b64encode(new.encode()).decode(),
            "sha": meta["sha"],
            "branch": branch,
        }).raise_for_status()

        pr = c.post("/pulls", json={
            "title": f"[AIOps] {namespace}/{deployment} : {_describe(d)}",
            "head": branch,
            "base": BASE_BRANCH,
            "body": (f"**Alerte** : {_clean(alertname, 60)}\n\n"
                     f"**Cause probable (LLM)** : {_clean(d.cause, 160)}\n\n"
                     f"**Confiance déclarée** : {d.confidence:.2f}\n\n"
                     f"**Modification** : {_describe(d)} dans `{path}`\n\n"
                     "Proposition générée par le moteur AIOps : **validation humaine requise** avant merge. "
                     "Après le merge, ArgoCD applique le changement."),
        })
        pr.raise_for_status()
        return pr.json()["html_url"]
