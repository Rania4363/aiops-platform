# AI-Driven Cloud Native Platform : GitOps, observabilité intelligente et autoréparation

Plateforme cloud native sur un cluster Kubernetes (CloudLab, 4 × m400 ARM64) : déploiement GitOps, observabilité,
et un moteur AIOps qui diagnostique les incidents avec un LLM local quantifié puis propose ou applique une
correction **dans des limites strictes**. Toute modification durable passe par une Pull Request validée par un humain.

## Architecture

```
git push ─► GitHub Actions : tests ─► build arm64 ─► test de démarrage ─► scan Trivy ─► GHCR ─► commit du tag (dev)
                                                                                                   │
                                          ArgoCD (app-of-apps, Git = source de vérité) ◄───────────┘
                                          ├─ dev  : synchro automatique
                                          └─ prod : seulement via une PR de promotion validée

Prometheus ─► Alertmanager ─► POST /webhook/alertmanager ─► moteur AIOps (FastAPI)
      collecte (API K8s + Prometheus) ─► LLM local (llama.cpp, Qwen2.5-1.5B Q4_K_M, JSON contraint par schéma)
      ─► validation Pydantic ─► allowlist ─┬─ redémarrage direct (dev, demo-app uniquement)
                                           ├─ Pull Request (réplicas, mémoire, rollback) ─► validation humaine
                                           └─ escalade (prod, confiance faible, cas inconnu)
      ─► vérification (l'alerte a-t-elle disparu ?)
```

| Élément | Détail |
|---|---|
| Cluster | kubeadm, Calico, containerd ; nœuds `k8s-master`, `k8s-worker1`, `k8s-worker2`, `ai-node` (arm64) |
| Environnements | namespaces `dev`, `prod`, `aiops` avec quotas, LimitRange et NetworkPolicy |
| GitOps | ArgoCD, application racine `aiops-root` (dossier `argocd/apps`), Kustomize (`manifests/base` + overlays) |
| CI | tests (matrice `app`, `aiops-engine`), build arm64, test de démarrage, Trivy (CRITICAL), publication GHCR, mise à jour du tag |
| Observabilité | kube-prometheus-stack (Prometheus, Alertmanager, Grafana), 4 règles d'alerte, tableau de bord versionné |
| LLM | llama.cpp sur `k8s-worker1` (`role=llm`), modèle Qwen2.5-1.5B-Instruct Q4_K_M, environ 3 tokens/s en génération |

## Contenu du dépôt

```
app/                      application de démonstration (FastAPI, métriques, injection de pannes)
aiops-engine/             moteur AIOps : webhook, collecte, LLM, garde, allowlist, actions, tests
manifests/base|overlays/  déploiement de l'application (dev, prod)
argocd/apps/              applications ArgoCD
platform/namespaces|alerts|llm|aiops|routing/   plateforme (quotas, alertes, tableau de bord, LLM, moteur, routage)
scripts/                  évaluation du LLM, générateur du tableau de bord
.github/workflows/        ci.yml, validate-manifests.yml, promote-to-prod.yml
```

## Sécurité de la remédiation

- Le LLM ne peut proposer que 5 actions : `restart_deployment`, `scale_deployment`, `set_memory_limit`, `rollback_image`, `escalate`.
- Sa sortie est contrainte par un schéma JSON, puis validée strictement (Pydantic), puis filtrée par une **allowlist** :
  la cible vient de l'alerte et jamais du LLM ; réplicas entre 1 et 5 ; mémoire entre 64 et 1024 Mi ; tag d'image au format `sha-xxxxxxx` ; confiance minimale 0,5.
- Seul le **redémarrage** de `dev/demo-app` est direct. Réplicas, mémoire et rollback passent toujours par une **Pull Request**.
- Droits Kubernetes minimaux (RBAC) : lecture seule sur dev et prod, écriture limitée au déploiement `demo-app` de dev. **Aucun droit d'écriture sur prod.**
- Garde déterministe avant le LLM : si les métriques sont redevenues normales, le LLM n'est pas appelé.
- Les logs transmis au LLM sont filtrés (seules les lignes de journal d'accès HTTP passent) contre l'injection de prompt.
- Délai de sécurité de 10 min par cible, 3 Pull Requests par heure au maximum, texte des PR assaini.
- Interrupteurs `AUTO_REMEDIATION` et `AUTO_PULL_REQUEST` pour passer en simulation.
- Chaîne d'approvisionnement : l'action Trivy est épinglée par empreinte de commit et le binaire Trivy par version (des versions ont été compromises en mars 2026).

## Évaluation du diagnostic LLM

Script : `scripts/eval/eval_llm.py` (7 scénarios × 5 essais, code réellement déployé). Résultats :

| Version | JSON valide | Bonne action | Actions erronées franchissant l'allowlist |
|---|---|---|---|
| Départ | 35/35 | 20/35 | 10 |
| + garde et filtrage des logs, prompt v1 | 35/35 | 25/35 | 0 |
| + prompt v2 | 35/35 | 35/35 | 0 |

Latence moyenne d'un diagnostic : environ 24 s. **Réserve :** scénarios synthétiques, et les corrections du prompt ont été réglées sur ces
mêmes scénarios ; ces chiffres ne prouvent pas la généralisation. La sécurité ne repose pas sur le modèle mais sur l'allowlist, le RBAC et la validation humaine.

## Démonstration

```bash
# panne en dev : le moteur diagnostique puis redémarre
kubectl -n dev run fault-inject --restart=Never --image=curlimages/curl:8.10.1 --command -- sleep 900
kubectl -n dev exec fault-inject -- curl -s -X POST http://demo-app/fault/errors
kubectl -n dev exec fault-inject -- sh -c 'for i in $(seq 1 300); do curl -s -o /dev/null http://demo-app/; sleep 0.5; done'
# incidents du moteur
kubectl -n aiops port-forward svc/aiops-engine 8081:8080 &
curl -s localhost:8081/incidents | python3 -m json.tool
```

Promotion en prod : Actions, `promote-to-prod`, Run workflow, puis merge de la PR.
Retour arrière : `git revert <commit>` puis push (la synchro automatique d'ArgoCD désactive le bouton Rollback de l'interface).

## Reconstruction (points à ne pas oublier)

1. Secret du moteur, hors Git : `kubectl -n aiops create secret generic aiops-secrets --from-literal=github-token=...` (jeton fine-grained, droits `contents` et `pull requests` sur ce seul dépôt ; saisie masquée).
2. Étiqueter le nœud du LLM : `kubectl label node k8s-worker1 role=llm`.
3. Paquets GHCR publics (ou `imagePullSecret`).
4. Réglages du dépôt : Actions avec droits d'écriture et autorisation de créer des Pull Requests.
5. Créer l'application ArgoCD racine `aiops-root` (source : ce dépôt, chemin `argocd/apps`).

## Limites connues

- Le cluster est kubeadm et non K3s.
- Les incidents sont gardés en mémoire (perdus au redémarrage du moteur).
- Le scénario de fuite mémoire n'est pas fiable en démonstration (OOMKill avant la fin de la durée de l'alerte).
- `rollback_image` ne connaît pas le tag précédent : une PR de rollback n'est valide que si le tag proposé est correct.
- Le modèle GGUF est téléchargé sans vérification de somme de contrôle.
- Seul le redémarrage est une action directe ; les autres sont volontairement lentes (PR).
