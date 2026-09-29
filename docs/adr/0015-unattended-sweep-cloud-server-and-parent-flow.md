<!-- ADR recording how the knob sweep runs unattended off the laptop: the whole outer loop (Tier-1 GPU redeploys + Tier-2 cells) is promoted into one parent Prefect flow calling per-point subflows; the Prefect server moves from local SQLite to Prefect Cloud (Hobby); the flow runs on a Prefect worker on EKS as a run-to-completion Job; a resume skips a point's GPU redeploy when all its cells already hold a valid measurement; and ArgoCD is ruled out as the sweep runner (kept, if at all, for L2a production serving deploy). Supersedes ADR-0012's "server runs locally" and "flows run from the workstation" sections, firing that ADR's own reopen trigger #3. -->

# ADR-0015: The knob sweep runs unattended — Cloud server, EKS worker, one parent flow

- Status: Accepted
- Date: 2026-09-29

## Context

[ADR-0012](0012-sweep-resumability-and-orchestrator-choice.md) made the Tier-2
per-point sweep resumable and adopted Prefect as a cache/retry/observability layer,
but pinned two things to the operator's laptop: the Prefect server runs locally
(`prefect server start`, SQLite `~/.prefect/prefect.db`) and the flow is triggered
"from the workstation driving `kubectl` + SSM". Its §Amendment kept the **Tier-1 GPU
redeploy in the justfile** — the flow (`orchestration/flow.py` `run_point_sweep`) drives
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

This replaces the `run={run_group}` **tag** (`orchestration/flow.py`) with real parent→child
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
