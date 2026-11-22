"""Liste d'actions autorisées. La cible (namespace/deployment) vient de l'alerte, jamais du LLM."""
import re
from typing import Literal

from pydantic import BaseModel

from schemas import Diagnosis

ALLOWED_NAMESPACES = {"dev", "prod"}
ALLOWED_DEPLOYMENTS = {"demo-app"}
MIN_CONFIDENCE = 0.5
REPLICAS_RANGE = (1, 5)
MEMORY_MI_RANGE = (64, 1024)

_MEM_RE = re.compile(r"^(\d{2,4})Mi$")
_TAG_RE = re.compile(r"^sha-[0-9a-f]{7}$")


class Decision(BaseModel):
    route: Literal["direct", "pull_request", "escalate", "rejected"]
    reason: str


def decide(d: Diagnosis, namespace: str, deployment: str, direct_namespaces: set[str]) -> Decision:
    if namespace not in ALLOWED_NAMESPACES or deployment not in ALLOWED_DEPLOYMENTS:
        return Decision(route="rejected", reason="cible hors liste autorisée")
    if d.action == "escalate":
        return Decision(route="escalate", reason="le LLM demande une intervention humaine")
    if d.confidence < MIN_CONFIDENCE:
        return Decision(route="escalate", reason=f"confiance trop faible ({d.confidence:.2f})")

    if d.action == "restart_deployment":
        if namespace in direct_namespaces:
            return Decision(route="direct", reason="restart autorisé en environnement direct")
        return Decision(route="escalate", reason="restart direct interdit hors dev")

    if d.action == "scale_deployment":
        lo, hi = REPLICAS_RANGE
        if not lo <= d.replicas <= hi:
            return Decision(route="rejected", reason=f"replicas hors bornes [{lo},{hi}]")
        route = "direct" if namespace in direct_namespaces else "pull_request"
        return Decision(route=route, reason="scale borné")

    if d.action == "set_memory_limit":
        m = _MEM_RE.match(d.memory_limit)
        if not m or not MEMORY_MI_RANGE[0] <= int(m.group(1)) <= MEMORY_MI_RANGE[1]:
            return Decision(route="rejected", reason="memory_limit invalide ou hors bornes")
        return Decision(route="pull_request", reason="changement de limite via PR")

    if d.action == "rollback_image":
        if not _TAG_RE.match(d.image_tag):
            return Decision(route="rejected", reason="image_tag invalide (attendu sha-xxxxxxx)")
        return Decision(route="pull_request", reason="rollback via PR")

    return Decision(route="rejected", reason="action inconnue")
