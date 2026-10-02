<!-- ADR recording how the knob sweep runs unattended off the laptop: the whole outer loop (Tier-1 GPU redeploys + Tier-2 cells) is promoted into one parent Prefect flow calling per-point subflows; the Prefect server moves from local SQLite to Prefect Cloud (Hobby); the flow runs on a Prefect worker on EKS as a run-to-completion Job; a resume skips a point's GPU redeploy when all its cells already hold a valid measurement; and ArgoCD is ruled out as the sweep runner (kept, if at all, for L2a production serving deploy). Supersedes ADR-0012's "server runs locally" and "flows run from the workstation" sections, firing that ADR's own reopen trigger #3. NOTE: the Cloud-server half is reversed by the 2026-09-29 amendment below — the server is a self-hosted OSS Prefect on EKS that rides the cluster's own duty-cycle; read that amendment for the server location, work pool type, and state backend actually built. -->

# ADR-0015: The knob sweep runs unattended — Cloud server, EKS worker, one parent flow

- Status: Accepted
- Date: 2026-09-29

## Context

[ADR-0012](0012-sweep-resumability-and-orchestrator-choice.md) made the Tier-2
per-point sweep resumable and adopted Prefect as a cache/retry/observability layer,
but pinned two things to the operator's laptop: the Prefect server runs locally
(`prefect server start`, SQLite `~/.prefect/prefect.db`) and the flow is triggered
"from the workstation driving `kubectl` + SSM". Its §Amendment kept the **Tier-1 GPU
redeploy in the justfile** — the flow (`orchestration/flows/point_sweep.py` `run_point_sweep`) drives
**one engine point**; the outer loop over the ~20 points stays an imperative bash loop
in `just knob-sweep`.

That leaves the hours-long half tethered. Cache and results already survive a laptop
death — `result_storage` and `key_storage` are S3 (ADR-0012, `orchestration/storage.py`),
so resume is durable. What dies when the laptop sleeps is the **driver process**: the
bash loop issuing the 20 GPU redeploys, each a ~20-minute rollout, and the local SQLite
server/UI. Closing the lid ends the sweep. This is ADR-0012's own reopen trigger #3
firing verbatim — "unattended multi-hour/multi-day sweeps make 'close the laptop and walk
away' a real pain — the fix there is promoting the flow-runner (a Prefect worker) to
… EKS, at which point a remote server naturally follows."

The knowledge base names no specific orchestrator (`distributed-ml-patterns.md` covers
workflow engines generically); its §4/§5 back the parent-flow shape (fan-out topology,
step memoization) and the against-idle-services stance already cited in ADR-0012. The
Prefect Cloud facts below — the Hobby tier is free forever with no credit card, hosts the
UI+API, caps managed execution at 1 concurrent run / 500 min/month, and retains run
history 7 days — were confirmed against the current Prefect pricing page and
managed-execution / rate-limit docs (2026-09-29). The own-compute worker sidesteps the
managed-execution caps; the 7-day retention governs the Prefect timeline only, never the
results.

## Decision

### The whole knob sweep goes off-laptop, not just Tier-2

The unit promoted is the **entire outer loop**, not the inner per-point sweep alone.
Moving only Tier-2 leaves the 20 GPU redeploys — the hours-long, laptop-tethered part —
on the workstation, so the operator would still babysit. The whole `knob-sweep` loop
(redeploy → rollout wait → ceiling scrape → run the point's cells → next point) becomes
an unattended run.

### The Prefect server moves to Prefect Cloud (Hobby)

ADR-0012 declined self-hosting on EKS because an always-on Postgres+Redis control plane
is recurring cost for an intermittently-used tool and fights the duty-cycle constraint
(ADR-0006). That reasoning stands — so the server does **not** move to a self-hosted EKS
stack. It moves to **Prefect Cloud, Hobby tier**: hosted UI+API (the remote observability
the local SQLite UI could never give), free forever, no always-on infrastructure and no
idle cost. This honours ADR-0012's cost logic while severing the laptop: the UI is
reachable from anywhere, so a running sweep is observable without the workstation.

Two Cloud ceilings are accepted because the design already absorbs them. **7-day run
retention** discards the Prefect timeline, never the numbers — the per-cell JSON in S3 is
the single source of truth (ADR-0012), exactly the tradeoff ADR-0012 accepted for local
SQLite ("losing it loses the timeline, never the cache or results"). The **managed-execution
caps** (1 concurrent run, 500 min/month) do not apply because the sweep brings its own
compute (below), using Cloud for scheduling and observability only.

### The flow runs on a Prefect worker on EKS, as a run-to-completion Job

The flow-runner is a **Prefect worker on the EKS cluster**, executing a sweep as a
**Kubernetes Job that runs to completion and exits** — no idle service between sweeps, so
the duty-cycle constraint is kept. The in-cluster ServiceAccount gives the loop the RBAC
to patch `deploy/vllm-gpu` directly, so the GPU redeploys are native `kubectl` from inside
the cluster. Cluster lifecycle stays out of the runner: `terraform cluster-up`/`cluster-down`
(create/destroy the whole EKS GPU capacity) remain a deliberate bracketing step — an
operator or a CI gate around the sweep — not an unattended process's privilege.

### The outer loop is one parent flow over per-point subflows

The `knob-sweep` loop becomes a **parent `@flow`** calling the existing per-point
`run_point_sweep` as a **subflow** per engine point, with `deploy_gpu` and `scrape_ceiling`
as parent tasks run before each subflow. The per-point flow is kept intact and wrapped,
not rewritten.

This replaces the `run={run_group}` **tag** (`orchestration/flows/point_sweep.py`) with real parent→child
lineage. That tag existed only because the loop lived in bash: 20 separate
`slipstream-orchestrate point-sweep` processes had no process-level parent, so a shared tag
stitched them in the UI. Once the loop is a Prefect flow, the parent run *is* the grouping;
the tag becomes redundant (it may be kept for cross-run filtering). A flat single flow —
20 redeploy tasks + ~240 cell tasks, no subflow layer — was rejected: the point is a real
domain boundary (one GPU deployment, one proxy, one measured ceiling) and the subflow keeps
it legible in the UI and gives the redeploy-skip gate a natural home.

### A resume skips a point's redeploy when its cells are already done

The parent gates `deploy_gpu` and `scrape_ceiling` on **whether the point has any pending
cells** — reusing `is_cell_valid` (`orchestration/validity.py`) against S3, the predicate
resume already trusts. If every cell of a point holds a valid measurement, the point's
~20-minute GPU rollout is **skipped entirely**. Today's bash loop redeploys
unconditionally, so a resume re-pays the most expensive resource to run zero cells. This
gate is why promoting the outer loop pays: resume protected cheap cells but re-paid
expensive redeploys, and this closes that gap.

### The ceiling scrape fails loud

`scrape_ceiling` keeps reading the ceiling from vLLM's startup log line
(`kubectl logs deploy/vllm-gpu | grep 'Maximum concurrency…'`, the source ADR-0009's method
already relies on) but **raises when the pattern is absent** instead of returning empty. A
silent empty scrape would run the point's whole Tier-2 ladder against a garbage ceiling and
surface only as wrong numbers — the one failure unattended operation cannot tolerate. The
task raises, killing the point, in the same shape as the cell validity gate. Replacing the
*source* of the ceiling is a measurement-method change (ADR-0009 territory) and is out of
scope here.

### A sweep is triggered manually now, by CI later

The trigger is a **manual `prefect deployment run`** (CLI or the Cloud UI "Run" button):
kick it, close the laptop, the EKS worker picks it up. That is the minimum that severs the
laptop and delivers both fire-and-forget and remote observability at once. **CI-triggered**
(`workflow_dispatch`) is the natural next step — ADR-0012's "CI/CD later" arm — once sweeps
are gated on a merge or an image bump. A **schedule** is declined: a sweep burns a GPU and
must stay deliberate, not fire on a timer.

### ArgoCD is not the sweep runner

ArgoCD is a continuous reconciler: it drives the cluster toward one git-declared desired
state and reverts drift. A sweep is ~20 deliberate transient states, each deployed,
measured, and discarded — to Argo every knob change is drift to undo. A sweep is an
imperative workflow with ordering under a semaphore(1), which is what ADR-0012 already
chose Prefect for; Argo has no notion of run-to-completion, per-cell caching, or a
flaky-rung retry. **Prefect runs the workflow; Argo, if adopted at all, reconciles the
L2a production serving deployment** — one long-lived desired state where drift-revert is a
feature. Whether L2a needs Argo is a separate, later call and out of scope here.

## Consequences

- This ADR is prose only; no code changes here. Implementation ships as a later stack, one
  kind of change each (per ADR-0012's precedent): the parent `knob-sweep` flow with
  `deploy_gpu`/`scrape_ceiling` tasks and the redeploy-skip gate (code), the EKS worker +
  Job + ServiceAccount RBAC (infra), the Cloud workspace + deployment wiring (orchestration),
  and the CI `workflow_dispatch` trigger (CI) if taken.
- ADR-0012's "The Prefect server runs locally, against S3-backed state" and its §Amendment
  "the driver runs at the control point (a workstation today)" are **superseded** for the
  unattended path: the server is Cloud and the driver is an EKS worker. ADR-0012's S3
  storage decisions (result/key storage, single source of truth, no enclosing transaction)
  are unchanged and are the reason this move is safe.
- The EKS worker's ServiceAccount is a new cluster-mutation privilege (patch
  `deploy/vllm-gpu`); its RBAC is scoped to that, not cluster admin. Cluster create/destroy
  stays outside it.
- Prefect Cloud's 7-day retention means run history and logs must be read within the week
  or exported; results are unaffected (S3).
- The reopen triggers narrow: a **GPU fleet** making points genuinely parallel (real edges
  to schedule, and more than 1 concurrent run — which on Cloud Hobby would meet the managed
  cap only if using managed execution, not the own-compute worker) would reopen the flat-vs-
  subflow shape and the tier choice; **mid-campaign cell-logic edits** still reopen the
  digest's code-version scope (unchanged from ADR-0012).

## Amendment (2026-09-29): the server is self-hosted on EKS, not Prefect Cloud

Implementing the Cloud wiring (#176), `terraform apply` on the work pool returned
`403 {"detail":"Your plan does not support hybrid or push work pools."}` — first for a
**kubernetes** pool, then again after switching to a **process** pool. A CLI probe of the
tier settled the cause: the free Hobby tier permits **only `prefect:managed` work pools**
(Prefect's own serverless compute, capped at 1 concurrent run / 500 min/month). Every pool
an own-compute worker polls — process, kubernetes, and push alike — is gated behind a paid
plan. The Decision above rests on an **own-compute EKS worker** (that is how it sidesteps the
managed-execution caps and brings its own GPU). Hobby cannot host that worker under any pool
type, so the "Prefect Cloud (Hobby)" server choice is unworkable as specified, not merely the
kubernetes-pool detail the previous amendment tried to patch.

**Decision — self-host OSS Prefect on the existing EKS cluster, duty-cycled with it.** The
server does not go to Cloud and does not become a new always-on stack. It rides the cluster's
own duty-cycle: a Prefect server (Deployment) and a worker (Deployment) come up when the
cluster comes up and are torn down with `cluster-down`. The cluster already exists only while
a sweep campaign runs (days, then destroyed — ADR-0006), and the server has **no reason to
outlive the GPU it observes**, so it inherits that cadence at ~$0 marginal cost. This is what
flips ADR-0012/ADR-0015's cost logic: those rejected self-hosting because they pictured an
**always-on** control plane (recurring idle cost fighting the duty-cycle). Pinning the server
to the cluster's lifecycle removes the always-on floor entirely — the objection was the floor,
not self-hosting itself. Self-hosted OSS has no tier gate, so every pool type is free.

Settled shape (grilled 2026-09-29):

| Decision | Choice |
|----------|--------|
| Server location | Self-hosted OSS Prefect 3, in-cluster Deployment, up with the cluster, torn down with `cluster-down`. Not Cloud. |
| Work pool | **process**, concurrency 1 — matches the serial single-GPU sweep. |
| State backend | SQLite on a small EBS PVC (adds the EBS CSI addon + a default gp3 StorageClass, which the cluster lacks). Survives a server-pod bounce so the run stays in the UI; dies with the cluster. Run history is disposable — S3 is the source of truth (ADR-0012). |
| UI access | `kubectl port-forward`. No public endpoint. |
| Worker image / auth | Worker runs our ECR bench-client image (carries the flow code) with S3 + SSM via Pod Identity and in-namespace RBAC to manage `deploy/vllm-gpu`. |
| Trigger | Manual `prefect deployment run` over port-forward; the run executes in-cluster to completion, laptop-independent. |

The process pool stands for the same reason the previous amendment gave — the per-run-Job
wins (isolation, per-run resource requests, per-run images, parallel autoscale) do not apply
to a **serial** sweep on a single static GPU node — but now it is a free choice on a
self-hosted server, not a tier concession. A further correction for the record: the
"always-on Postgres+Redis" premise ADR-0012 cited overstated it twice over — Prefect 3
self-host needs no Redis at all, and a single-instance ephemeral server needs no Postgres
either (SQLite suffices). The real objection was always the always-on floor, and the
duty-cycle pinning removes it.

**Alternatives rejected.** *Cloud Starter* (~$100/mo) unlocks own-compute pools with zero ops,
but a duty-cycled toy sweep does not earn a monthly subscription when the server can ride the
cluster for ~$0. *A dedicated always-on self-hosted stack* (~$95/mo EKS) re-incurs the exact
idle floor ADR-0012/0006 rejected — declined for the same reason, which the ephemeral in-
cluster server avoids by construction.

**What is deferred to implementation, not settled here:** during a sweep the flow reconfigures
`deploy/vllm-gpu` per grid point; today `just gpu-deploy` applies it statically. The two must
not both own that Deployment — reconciling static-vs-per-run vllm ownership belongs to the
implementation ticket.

**Reopen trigger (unchanged in substance):** a **GPU fleet** making points genuinely parallel
revalues per-run Jobs (parallel points want per-run isolation and scheduling a process pool
cannot give) and reopens the pool type — now a switch to a **kubernetes** pool on the same
self-hosted server, no tier change needed.

## Amendment (2026-10-02): the run's configuration is published as one parent-level artifact

A knob-sweep run page showed no record of the configuration the run executed. The served
model, the swept grid, and the engine args a redeploy ran with lived only in the image's
files; an operator verifying what a particular run ran with had to leave the UI and read the
image. The run is already versioned — the bench-client image's content-sha tag, the deep
digest (ADR-0012) — but neither is human-inspectable.

**Decision — publish the configuration as one keyed markdown artifact on the parent run
page.** The flow renders a markdown body and publishes it through
`create_markdown_artifact`, keyed so Prefect keeps a cross-run history of it. The body folds
the version anchors (the bench-client image ref, the orchestration image ref, the deep
config digest) with `model.yaml` and `sweep-grid.yaml` verbatim, the vLLM serving image ref,
and the vLLM container args verbatim — the swept placeholders (`${MAX_NUM_SEQS}`,
`${KV_CACHE_DTYPE}`, `${PREFIX_CACHING_FLAG}`) left intact. The render is a pure function of
its inputs; the Prefect call stays in the flow.

**One artifact at the parent, not one per point or a three-level tree.** The run hierarchy is
one parent flow run, one `point-sweep` subflow run per engine point, and a cell task per
Tier-2 rung. An earlier shape published the configuration lower down — the vLLM args
resolved per engine point into one block each, or a three-level layout with a subflow
artifact per point and a cell artifact per rung. Both were rejected. The swept deltas are
already read off the Prefect graph: the engine point's slug (`mns{N}_kv{dtype}_pc{on|off}`)
and the cell name (`pshare{share}_burst{burstiness}[_mc{cap}]`) name exactly the knobs that
vary. Everything else is fixed for the whole run and belongs recorded once. Resolving the
args per point produced ~20 near-identical blocks that drowned that signal, and re-rendering
the manifest from the same inputs would only prove the substitution is deterministic, not
that the pod actually served those args. The slug on the graph names the point a redeploy
ran; the grid in the artifact decodes what each slug's placeholders resolve to.

**The orchestration image ref is baked at build time.** The flow runs in-process under a
**process** work pool, which gives it no runtime handle to its own worker image, and
neither the Prefect runtime nor the Kubernetes downward API exposes a pod's own image field
for that pool. So the orchestration image's own ref is baked into the image as
`ORCH_IMAGE_REF` at build time (its content-tag ref, deterministic per image) and read from
the environment. A local or test build bakes none, so it reads as `"unknown"`. This is kept
separate from the bench-client image ref because the two images do not always share a tag.

**Consequences.** Ships as a stack, one kind of change each (per this ADR's own precedent):
the build change that bakes `ORCH_IMAGE_REF` (build/CI), and the flow + render code (code).
The deep digest's input set is unchanged — the artifact reads `model.yaml`,
`sweep-grid.yaml`, and the vLLM manifest for display only, and does not fold the
orchestration image ref into the digest. A **three-level artifact layout** reopens only if a
point or a cell gains configuration that is not already encoded in its slug or cell name.
