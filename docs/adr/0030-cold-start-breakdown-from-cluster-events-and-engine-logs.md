<!-- ADR recording how L2a defines and measures a cold start (issue #37): the window runs from the replica's pod being created until its Ready condition; every run starts from a true zero with no GPU node up, and there is no warm-node variant; the cold-start breakdown has six phases (capacity wait, node launch, image pull, weight download, engine init, readiness lag), each bounded by a named cluster or engine-log marker, plus an unattributed remainder so the phases add up to the total; engine init's torch.compile, graph capture and profiling-run durations are recorded as detail, not phases; a run whose log shows a compile cache hit fails; the timings come from Kubernetes events, pod and NodeClaim conditions and the engine's log, not Prometheus, whose only cold-start series is Karpenter's pod-provisioning histogram; the flow brings up no bench endpoint but holds the rollout freeze. The vLLM log markers were checked against vLLM v0.29.0. Settles spec §10's open cold-start experiment design for L2a. -->

# ADR-0030: Cold-start breakdown from cluster events and engine logs

- Status: Accepted
- Date: 2026-11-13

## Context

slipstream scales its GPU replica to zero to stay within the duty-cycle budget
(ADR-0006), and the first replica back pays a cold start. The only number so far is the
Fleet health dashboard's pod-provisioning cold start (ADR-0026): Karpenter's histogram
from its last scheduling attempt for a pending pod until the pod is Ready. On the one
recorded bring-up it read about 9.7 min, 4.5 min of it the image pull, but it says
nothing about where the rest goes. It also starts too late: time spent waiting out a GPU
capacity shortage before that last attempt, 10–20 min on bad days (#243), is not in it.

L2a's deliverable is a cold-start latency breakdown (spec §4), and the elimination work
after it needs a baseline it can be compared against on the same definitions. Spec §10
left the cold-start experiment design to this phase. This ADR records the definitions and
the measurement; the `cold-start-breakdown` flow, its report and its RBAC implement them.

The engine is vLLM v0.29.0, the version ADR-0006 pins for the GPU replica. Every log
marker below was checked against its source.

## Decision

### The window: pod created → pod Ready

**A cold start runs from the replica's pod being created (the Deployment scaled 0 → 1)
until the pod's Ready condition turns true.** Both ends are timestamps the API server
records, and neither depends on the bench endpoint or the request path.

- *Karpenter's histogram window* was rejected as the definition: it starts at the last
  scheduling attempt, so it hides capacity wait, the phase most likely to dominate on a
  bad day.
- *Ending at the first token* was rejected: that is L2b's wake, which runs until the
  first token reaches a client, needs the bench endpoint, and adds the request path to a
  number meant to measure bring-up.

### Every run starts from a true zero

**A run starts only when no GPU NodeClaim is left.** The flow scales the replica to zero,
waits until Karpenter has reaped the GPU node, and checks that no GPU NodeClaim remains
before it scales up, so a run never starts warm by accident.

There is no warm-node variant (node up, image cached), because the platform never has
one to offer: the GPU node pool's limit is one GPU and a g5.xlarge has one, so a new
replica always needs a new node, and `consolidateAfter: 1m` reaps an empty node about a
minute after scale-down. A warm-node number would measure a state the platform does not
reach. The Rig is measured as it runs, on-demand g5.xlarge; spot is L2b.

### Six phases, each bounded by a named marker

**The cold-start breakdown has six phases, in order, and each starts and ends at a marker
the flow reads from the cluster or the engine's log:**

| Phase | Starts at | Ends at |
| --- | --- | --- |
| Capacity wait | pod created | the NodeClaim that ends up hosting the pod reaches `Launched` |
| Node launch | that NodeClaim's `Launched` | that NodeClaim's `Initialized` |
| Image pull | kubelet's `Pulling` event for the engine container | kubelet's `Pulled` event |
| Weight download | the `Time spent downloading weights` line's timestamp minus the duration it reports | that line's timestamp |
| Engine init | the `Loading weights took` line's timestamp minus the duration it reports | the `init engine (profile, create kv cache, warmup model) took` line |
| Readiness lag | the `Starting vLLM server on` line | the pod's Ready condition |

- **Capacity wait and node launch are split** so a GPU capacity gap shows as capacity
  wait instead of inflating node launch. A single "provisioning" phase was rejected
  because it would mix a regional shortage with EC2 boot and node registration, which
  later work attacks differently. Capacity wait ends at the hosting NodeClaim's
  `Launched`, not the first NodeClaim's, so a launch that fails and is retried counts as
  waiting.
- **Node launch ends at `Initialized`**, which Karpenter sets once the node is Ready, its
  startup taints are gone and the resources the NodeClaim asked for are registered. For
  the GPU node that includes `nvidia.com/gpu`, which only the device plugin advertises
  (ADR-0006), so node launch covers EC2 boot, kubelet registration and GPU readiness. The
  node's own Ready condition was rejected as the end: the pod cannot be placed until the
  GPU is advertised, and the node records no timestamp for that.
- **Image pull is kubelet's `Pulling` → `Pulled` for the engine container.** Kubelet gives
  only the pull's total; whether the time goes to the network path or to unpacking layers
  is not visible from these events.
- **Weight download is derived from one line.** vLLM v0.29.0 logs `Time spent downloading
  weights` with a duration and has no INFO-level line marking the start, so the start is
  the line's timestamp minus that duration.
- **Weight download and engine init are separate phases** because fetching weights and
  loading them onto the GPU are removed by different strategies (a weight cache against
  a faster loader or a persisted compile cache).
- **Engine init starts where `Loading weights took`'s duration starts and ends at `init
  engine (profile, create kv cache, warmup model) took`.** The first line's timer covers
  loading weights onto the GPU and excludes the download; the second covers profiling, KV
  cache sizing, torch.compile and CUDA graph capture. `Model loading took` was rejected as
  a marker: its timer includes the download, so it would count the download twice.
- **Readiness lag runs from `Starting vLLM server on` to the pod's Ready condition.** It
  is the gap between the server listening and the kubelet's probes passing, so the
  replica's probe periods (startup every 15 s, readiness every 10 s) set most of it.

### An unattributed remainder makes the phases add up

**Each run reports `unattributed` as its total minus the sum of the six phases**, so the
stacked bar adds up to the real pod-created → Ready total. It covers the gaps no marker
bounds: scheduling and sandbox creation between node launch and image pull, container
start through the engine's imports and process start, model init and weight
post-processing.

- *Stretching phases to meet each other* was rejected: it would hide a missing or moved
  marker inside a neighbouring phase. A large unattributed share instead points at one.
- *Dropping the gaps* was rejected: the phases would no longer sum to the cold start, and
  a reader could not trust the breakdown is complete.

### Engine init's sub-durations are detail, not phases

**Each run also records engine init's sub-durations from their log lines: `torch.compile
took`, `Graph capturing finished in` and `Initial profiling/warmup run took`.** They show
how much a persisted compile cache could save, given that graph capture is never
persisted. They are detail on the run's record and the chart may split the engine-init bar
by them, but they are not top-level phases.

Making them phases was rejected: whether each appears depends on the engine's
configuration and cache state, so the phase set would change between runs and an
elimination run could not be compared against the baseline phase by phase.

### A compile cache hit fails the run

**A run fails if its engine log shows the compile cache was hit (`Directly load the
compiled graph(s)`).** A true-zero run lands on a fresh node with no cache, so a hit means
the run was not cold. Recording the run with a label was rejected: one warm run in three
moves the median, and the baseline exists to be compared against.

### Timings come from cluster events and engine logs, not Prometheus

**The flow reads Kubernetes events, pod conditions, NodeClaim conditions and the engine
container's log through the existing `Kubectl` transport.** Prometheus was rejected: its
only cold-start series is Karpenter's pod-provisioning histogram, which gives a bucketed
total, starts at the last scheduling attempt and has no phases.

- **Collection happens as soon as the pod is Ready.** Events expire (after about 1 h by
  default), so each run's events, conditions and log are read before the next run starts.
- **A missing marker fails the run with its name.** No download line, no `Launched`
  condition, no `Pulled` event (events expired) and a pod that never becomes Ready each
  raise an error naming what was missing, so a run never quietly reports a wrong phase.
- **The worker's cold-start rights are read-only.** It gets `get`/`list`/`watch` on
  `nodeclaims.karpenter.sh` and `nodes` in a ClusterRole, its first cluster-scoped grants,
  and `list` on `events` in its namespaced Role.

### No bench endpoint, but the rollout freeze is held

**The flow sends no requests, so it brings up no bench endpoint (ADR-0028).** Measuring
through the endpoint was rejected with the first-token end: each run would add the
endpoint's apply, the bench host's boot and its destroy, for a number this ADR does not
define. The flow still holds the rollout freeze under its run id (ADR-0029), since a
rollout landing mid-run would restart the replica and corrupt the cold start.

## Consequences

- A later elimination change is judged by re-running the flow unchanged and comparing its
  run id against the baseline's, phase by phase, on these definitions.
- The markers are log text. An engine upgrade past vLLM v0.29.0 must re-check every line
  named here; a renamed line fails the run with the missing marker's name rather than
  skewing a phase.
- Kubernetes timestamps have one-second resolution, while the engine's durations are
  finer, so each phase carries up to about a second of rounding. The unattributed
  remainder absorbs it and can read slightly negative; a remainder well below zero means
  two markers overlap.
- The breakdown cannot tell a slow image pull's network path from its unpacking, nor
  split node launch into EC2 boot, registration and device-plugin start. Those belong to
  the elimination work.
- Each run holds an on-demand g5.xlarge for about 10 min, plus the reap wait before the
  next run can start.
- The Fleet health dashboard's pod-provisioning cold start panel stays as the live total.
  It reads shorter than this ADR's cold start whenever capacity wait is non-zero.
