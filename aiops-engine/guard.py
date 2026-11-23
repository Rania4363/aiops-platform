"""Gardes déterministes autour du LLM : signal réel avant l'appel, logs filtrés avant l'envoi."""
import re

# Seuils volontairement plus bas que ceux des alertes : l'état peut avoir un peu évolué depuis le déclenchement.
ERROR_RATE_MIN = 0.1
LATENCY_MIN_S = 0.5
MEMORY_RATIO_MIN = 0.7

# Seules les lignes au format d'un journal d'accès HTTP sont transmises au LLM.
# Tout autre texte (donc tout texte forgé par un attaquant) est écarté.
_ACCESS_LOG = re.compile(r'^(\d+x )?"[A-Z]{3,7} \S{1,80} HTTP/\d(\.\d)?" \d{3}( [A-Za-z ]{0,30})?$')
_MEM = re.compile(r"^(\d+)Mi$")


def sanitize_logs(lines: list[str]) -> list[str]:
    return [ln for ln in lines if isinstance(ln, str) and _ACCESS_LOG.match(ln)]


def signal_present(alertname: str, ctx: dict) -> tuple[bool, str]:
    """(False, raison) seulement si les métriques montrent clairement que tout est normal.
    Une donnée absente (Prometheus injoignable) ne bloque jamais : on laisse le LLM et l'allowlist décider."""
    m = ctx.get("metrics") or {}
    pods = ctx.get("pods") or []
    dep = ctx.get("deployment") or {}

    if alertname == "DemoAppHighErrorRate":
        v = m.get("error_rate_5xx")
        if v is not None and v < ERROR_RATE_MIN:
            return False, f"taux d'erreurs 5xx redevenu normal ({v:.3f})"
    elif alertname == "DemoAppHighLatency":
        v = m.get("latency_p95_s")
        if v is not None and v < LATENCY_MIN_S:
            return False, f"latence p95 redevenue normale ({v:.3f}s)"
    elif alertname == "DemoAppMemoryNearLimit":
        mem, lim = m.get("memory_mb"), _MEM.match(str(dep.get("memory_limit", "")))
        if mem is not None and lim and mem / int(lim.group(1)) < MEMORY_RATIO_MIN:
            return False, "mémoire redevenue normale"
    elif alertname == "DemoAppCrashLooping":
        if pods and all((p.get("restarts") or 0) == 0 and not p.get("waiting") for p in pods):
            return False, "aucun redémarrage observé"
    return True, ""
