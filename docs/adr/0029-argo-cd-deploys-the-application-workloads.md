<!-- ADR recording the deploy substrate (issue #36): Argo CD deploys the cluster's application workloads. Terraform keeps the cluster, the add-ons, IAM and Pod Identity, and installs Argo CD, Image Updater and one root Application, its only handoff to Argo CD; Argo CD owns the Prefect worker and the OTel Collector now and the serving fleet and Router later; the experiment flows keep the bench Engine. Image Updater is the single writer of the worker's image tag; the autoscaler alone owns replicas; a flow run holds a self-expiring rollout freeze; Argo CD Notifications creates the Sentry release marker; Argo Rollouts is the planned path for progressive delivery; the L3 PostHog split and a future Rollouts traffic router would both write HTTPRoute weights. Records why Flux + Flagger, CI-driven Helm and Terraform were rejected. Settles spec §10's open deploy substrate and names §5.1's marker mechanism; builds on ADR-0015's "Argo, if adopted at all, reconciles the L2a production serving deployment" and reuses ADR-0028's hook discipline. -->

# ADR-0029: Argo CD deploys the cluster's application workloads

- Status: Accepted
- Date: 2026-11-11

## Context

Three writers decide what runs on a live cluster:

- **Terraform** creates the cluster and its add-ons, and installs the Prefect worker as a
  `helm_release` whose image tag it resolves at `just cluster-up`
  (`terraform/eks/worker.tf`).
- **`just` recipes** `kubectl apply` the OTel Collector (`just obs-up`) and the Engine
  manifests (`just cpu-deploy`, `just gpu-deploy`).
- **The experiment flows** redeploy the bench Engine with each point's or arm run's engine
  knobs (ADR-0015).

A merge to main does not reach the live worker until the operator re-applies the eks stack,
so a dispatch can run stale flow code. The guard in `flow-dispatch.yml` catches a flow
config that differs from main's, not a code-only change. Nothing stops a rollout from
landing in the middle of a measurement, the always-up layers (the serving fleet at L2a, the
Router at L3) have no rollout path, and spec §5.1's Sentry release marker has no deploy
event to ride on. Spec §10 left the substrate open: Argo CD, CI-driven Helm or Terraform.

The choice assumes the always-up future the later layers need. The re-bootstrap cost of the
duty-cycled cluster, which installs the substrate at every `just cluster-up`, was not
weighed.

## Decision

**Argo CD deploys the cluster's application workloads.** Argo Rollouts is not installed;
it is the planned path for progressive delivery (below).

### Ownership boundary

- **Terraform:** the cluster, node pools, Karpenter, IAM and Pod Identity associations, the
  add-ons wired to IAM or remote state (External Secrets, monitoring, the Prefect server,
  the DCGM exporter), the bench-endpoint topology ConfigMap, the Argo CD and Image Updater
  installs, and one root app-of-apps Application. Node scaling stays outside Argo CD, so
  GPU capacity is never a sync concern, and no add-on is rewired to new IAM.
- **Argo CD:** the Prefect worker, which stops being a Terraform `helm_release`; the OTel
  Collector, which replaces `just obs-up`'s `kubectl apply`; and the serving fleet and the
  Router/EPP once L2a and L3 build them.
- **Experiment flows:** the bench Engine (`vllm-gpu`). Argo CD never manages it, so it
  never reverts a point's or arm run's redeploy as drift (ADR-0015).

**The root Application is the only handoff.** It carries the cluster-specific values
Terraform knows: the image repository URL, ServiceAccount names and ConfigMap names. The
apps under it read them from there and from nowhere else in Terraform.

### One writer per field

- **The worker's image tag: Argo CD Image Updater.** Strategy `newest-build`, restricted
  to the immutable content-sha tags the orchestration image workflow pushes (the first 12
  hex characters of the last commit touching the image's inputs; never `:main`), with the `argocd` write-back method. Git write-back is ruled out: main
  requires the `ci-ok` check with admins enforced, so a bot cannot push to it.
  Terraform stops resolving the tag. The tag stays immutable, so a cached image under
  `IfNotPresent` is always the right one, and the dispatch guard can name what the worker
  runs.
- **Replicas: the autoscaler.** `replicas` never appears in git, and Argo CD syncs with
  `ServerSideApply=true`, so dropping the field from a Deployment never resets it to one.
  An autoscale benchmark measures the autoscaler, not a deployer fighting it.

### Rollout freeze

A flow run never has a rollout land under it:

- At start, the flow run adds a deny sync window to the AppProject with `manualSync: true`,
  lasting the flow run's timeout, so it expires on its own if the flow run dies.
- The flow's `finally` and its crash and cancel hooks remove the window early, with the same
  hook discipline as ADR-0028's bench-endpoint destroy.
- A merge during the freeze syncs once the window ends. The window blocks syncs, not
  autoscaling.
- The flows reach Argo CD through an injected port with acquire and release operations,
  as they reach the cluster through the injected `Kubectl`.
- The worker's RBAC widens only to patching sync windows on that one AppProject.
- The rollback rehearsal (spec §5.1) holds the freeze and drives its own rollout by manual
  sync, which `manualSync: true` permits, so it is that rollout's only writer. Rollback is
  a git revert that rolls out like any other merge.

**The dispatch guard stays.** Image Updater polls, and a held freeze delays a sync, so a
dispatch can still race a pending rollout. The guard compares the running worker's image to
the image behind main's `:main` tag and fails with "rollout pending — retry once it
lands".
It also fails when there is not exactly one running worker. It moves out of the inline
workflow step into a tested script.

### Sentry release marker

Argo CD Notifications, on the `on-sync-succeeded` trigger, calls Sentry's releases and
deploys API with the synced commit SHA as the release version. The marker is a property of
the substrate, so every app Argo CD owns gets one with no per-app wiring. It is built once
Sentry is integrated. If the marker later needs canary semantics, Argo Rollouts'
notifications can replace it.

### Progressive delivery: Argo Rollouts, later

Spec §5.2 rules canary and shadow proper out for a single-version demonstrator with no
live users, so no deployment strategy is chosen now. When a progressive-delivery gate is
wanted, an Argo Rollouts `Rollout` resource replaces a `Deployment` in the same Argo CD
app, and an HPA or KEDA `ScaledObject` can target it, so the substrate decision does not
reopen. Blue/green is declined: it doubles GPU capacity and nothing calls for it.

**HTTPRoute weights would have two writers.** The L3 PostHog flag-driven split and a
Rollouts traffic router would both set weights on the same HTTPRoute. Whichever is built
second must settle which one owns them. Whether the Rollouts Gateway API plugin accepts an
InferencePool as a weighted `backendRef` is unverified (its docs show Services); spike it
before relying on it at L3.

## Alternatives considered

- **Flux + Flagger.** Equally GitOps, and Flagger covers progressive delivery. Argo CD
  has sync windows, which the rollout freeze is built on, an app-of-apps root that gives
  Terraform a single handoff, Notifications for the release marker, and a UI that shows
  what is out of sync. Argo Rollouts covers what Flagger would.
- **CI-driven Helm.** A `helm upgrade` from a workflow on merge. It gives the worker's tag
  a second writer beside Terraform, does not correct drift, and needs cluster credentials
  in CI for every rollout. A freeze would have to be a CI-side lock that a crashed flow run
  cannot release.
- **Terraform.** Keep the worker as a `helm_release` and roll by re-applying the eks
  stack. A rollout then needs an operator or a CI job holding the eks stack's rights, every
  rollout plans the whole cluster, and nothing reconciles between applies.

## Consequences

- A merge to main reaches the idle worker without re-applying the eks stack, once its
  image is built and Image Updater's next poll sees it.
- A merge during a flow run waits for it to end. A flow run that crashes without its hooks
  running holds rollouts until its timeout.
- `just cluster-up` installs Argo CD and Image Updater on every bring-up, and the worker
  and the Collector come up only once Argo CD has synced them.
- The Image Updater's override lives on the Application, not in git, so git does not show
  the worker's running tag; the Application and the dispatch guard do.
- Argo CD syncs automatically only when an app is out of sync, so a merge that changes
  none of an app's manifests marks no release. A merge that changes both the worker's
  manifests and its image's inputs syncs twice, once for the manifests and again when
  Image Updater rolls the image, so that release gets two deploys.
- The worker's tag names the last commit that touched the image's inputs, not main's
  head, so it does not match the Sentry release version; the Application's synced
  revision does.
- The CPU Engine (`just cpu-deploy`) is not touched by this decision.
