<!-- ADR recording how L2a autoscales (issue #38): the serving fleet is a Deployment separate from the bench Engine, deployed by Argo CD, whose replicas KEDA alone owns from 0 to 2; its manifest is generated from k8s/vllm-gpu.yaml at the 0%-share knob-sweep winner and committed with a drift test, so the bench path is untouched; KEDA scales 1→N on running requests per replica with waiting requests as a backstop, and kv_cache_usage_perc is left out because vLLM counts evictable prefix blocks as free and the Rig breaches its SLO at about 10% KV usage; a static Envoy proxy in front of the serving fleet publishes the request rate that wakes it from zero, and the KEDA HTTP add-on is deferred to L2b's request survival; every trigger reads the in-cluster Prometheus, not Grafana Cloud; the ceiling of 2 replicas is the on-demand G quota; #38 owns the control loop 0→N→0 and L2b owns no request lost during wake; the autoscale-under-load flow drives an open-loop wake/surge/idle profile through a per-run bench endpoint and passes on the replica path 0→1→2→0, recording the SLO without gating on it; each flow refuses to start while the other role holds GPUs. Builds on ADR-0006, ADR-0009, ADR-0026, ADR-0028, ADR-0029 and ADR-0030. -->

# ADR-0031: The serving fleet autoscales on running requests, woken by Envoy

- Status: Accepted
- Date: 2026-11-17

## Context

L2a's second deliverable is an autoscaling-under-load timeline: the platform scales up
from zero under load (spec §4). Before this ADR the spec named KEDA/HPA on
`num_requests_waiting` and Karpenter provisioning. The GPU replica is scaled by hand
(`just gpu-up` / `gpu-down`, ADR-0006) or by a flow (ADR-0030), and no autoscaler is
installed.

Facts that shaped the decisions below:

- The only GPU Deployment, `vllm-gpu`, is the instrument the knob sweep, the KV cache A/B
  and the cold-start flow own: they set its engine knobs and replica count per run, and
  the worker's RBAC is scoped to it.
- The cluster runs EKS 1.36, where the `HPAScaleToZero` feature gate is alpha
  ([feature gates](https://kubernetes.io/docs/reference/command-line-tools-reference/feature-gates/)),
  and EKS's managed control plane does not expose alpha gates. Plain HPA cannot reach
  zero replicas; KEDA can.
- The account's on-demand G quota in eu-west-1 is 8 vCPU, two g5.xlarge; the spot G quota
  is 4 vCPU, one node (Service Quotas `L-DB2E81BA` and `L-3819A6DF`, read 2026-11-17). The
  GPU NodePool is on-demand only (ADR-0006).
- A cold start from a true zero takes about 760 s
  (`reports/cold-start-20261117T143332Z/BASELINE.md`).
- The 0%-share knob-sweep winner, `32 fp8 off`, has a concurrency ceiling of 8 (ADR-0009):
  goodput falls to 0.755 at the next rung, 16, where p95 TPOT is 55 ms against the 50 ms
  gate and TTFT still passes (`reports/sweep-grid-20261008T130350Z/RECOMMENDATION.md`).
- An in-cluster Prometheus scrapes the Engine and remote-writes an allowlist to
  Grafana Cloud (ADR-0026).

## Decision

### #38 owns the control loop; L2b owns request survival

**#38 delivers the autoscaler owning the replica count end to end: 0→1 when load arrives,
1→N as it grows, back to 0 after a cooldown.** A request that arrives during a cold start
may fail; the timeline records it. Holding requests through a wake, draining before a
spot eviction and the chaos day are L2b. Scale-down to zero stays in #38, because an
autoscaled GPU workload that cannot return to zero leaks spend.

### Two roles: the serving fleet and the bench Engine

**The serving fleet is its own Deployment, deployed by Argo CD, and KEDA alone owns its
replicas. `vllm-gpu` stays the bench Engine, owned by the flows, and is not changed.**
A Deployment's replica count can have one writer (ADR-0029). The flows need to restart
the bench Engine with new knobs and force it to 0 and 1; the autoscaler needs the
serving fleet's count all the time.

- *A ScaledObject on `vllm-gpu`*, applied for the length of an autoscale run, was
  rejected: whether KEDA or a flow owns the replicas would depend on which flow is running.

**The serving fleet's manifest is generated from `k8s/vllm-gpu.yaml` and committed, and
a test fails when the committed file differs from a fresh render.** The render fixes the
engine knobs at the 0%-share sweep winner, renames the Deployment, gives it its own
Service and NodePort, and drops `replicas` so Argo CD never resets KEDA's count. The pod
spec keeps one source, and neither the recipe's nor the flows' renderer changes.

- *A hand copy* was rejected: two 240-line pod specs drift apart.
- *A shared Kustomize or Helm base* was rejected: it rewrites both renderers and the
  RBAC-scoped apply path. It stays open if the two roles diverge.

**The serving fleet runs `32 fp8 off`, the 0%-share winner, and the autoscale load runs
at 0% prefix share.** That share has the lowest ceiling, so its threshold is the most
conservative, and one config gives one ceiling to set the threshold against.

**The two roles share the quota, so each flow refuses to start while the other holds
GPUs:** the autoscale flow while `vllm-gpu` has replicas, the bench flows while the
serving fleet does, each failing with which role holds them. The rollout freeze
serializes flow runs; this covers a manual `just gpu-up` and leftover load.

### Ceiling of two replicas, on-demand

**KEDA's `maxReplicaCount` is 2 and the GPU NodePool's limit rises from 1 to 2 GPUs.**
Two on-demand g5.xlarge are the whole G quota. A second replica is what tests the
queue-driven scale-out; 0→1 alone only tests the wake. Spot stays in L2b: its quota fits
one node and it thrashed CreateFleet (ADR-0006).

### 1→N on running requests; waiting as a backstop; KV cache usage left out

**The primary trigger is running plus waiting requests per replica, with a target of 6.
A second trigger fires when `num_requests_waiting` stays above 0 for a minute.** KEDA
scales to whichever asks for more.

- The Rig breaches its SLO on TPOT, the cost of a bigger decode batch on the A10G, before
  any request has to wait. At the 0%-share breach rung, 16 running is below `max-num-seqs`
  32, so the queue is about empty. The running count is the signal that moves with the
  breach, matching [GKE's guidance](https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/machine-learning/inference/autoscaling)
  to scale latency-sensitive serving on batch size.
- The target sits below the ceiling of 8 because a new replica takes about 760 s to come
  up: scaling out at the ceiling would leave the replica past it for the whole cold start.
- Waiting requests are counted in the primary trigger so a replica mid-generation is never
  read as idle and scaled away.
- The waiting trigger catches open-loop overload and preemptions, which vLLM puts back on
  the waiting queue.
- *`num_requests_waiting` alone*, the spec's signal, was rejected: on this Rig it fires
  only after the SLO is already broken.
- *`kv_cache_usage_perc`* was rejected as a trigger. vLLM counts prefix-cached blocks with
  no live request as free, so the gauge measures only blocks live requests hold, and at
  the breach rung that is about 10% of the pool (16 × 1,128 tokens of 185,696). The gauge
  would become worth a trigger with long contexts, a capped KV pool, or preemption storms.

The gauge values at the breach rung are calculated from the sweep reports, which did not
record the gauges. The vLLM metric semantics were read from vLLM's main branch; recheck
them against the pinned v0.29.0 when building.

### A static Envoy proxy wakes the fleet from zero

**A static Envoy proxy, one CPU pod in front of the serving fleet's Service, publishes the
incoming request rate to Prometheus. KEDA's activation trigger fires when that rate is
above 0.** With zero replicas the Engine publishes no series, KEDA reads the empty query
as 0, and the fleet would never wake; the proxy is the component that stays up. While no
replica is Ready it answers 503, which #38 allows.

- *The KEDA HTTP add-on* was rejected for #38. Its advantage is holding requests through
  a wake, which is L2b's job and would need the ALB idle and client timeouts raised past
  the cold start to help at all. It brings an operator, an interceptor, a scaler and its
  own CRDs, and combining its trigger with Prometheus triggers on one ScaledObject was not
  confirmed. L2b should look at it, or at Envoy retries with longer timeouts, for request
  survival.
- *The ALB's CloudWatch `RequestCount`* was rejected: it needs AWS credentials for KEDA, a
  second metric source, and a ScaledObject tied to an ALB that exists only during a run.

L3's Router replaces the proxy as both the front and the metric source.

### Every trigger reads the in-cluster Prometheus

**KEDA's Prometheus triggers query the in-cluster Prometheus, and the serving fleet's
ServiceMonitor scrapes every 15 s.** It scrapes the Engine, needs no credentials,
and keeps the control loop inside the cluster. Grafana Cloud stays the store results are
read from (ADR-0026), not a control path: it would add remote-write lag, free-tier query
limits and a read token in the cluster.

### First timing values

KEDA polls every 15 s, the last replica scales to 0 after a 5 min `cooldownPeriod`, and
the 2→1 scale-down keeps HPA's default 5 min stabilization. These and the target of 6 are
first values: the first run checks them.

### The autoscale-under-load flow

**An `autoscale-under-load` Prefect flow measures one run by default from outside the
cluster, through a bench endpoint it brings up for the run (ADR-0028) pointed at the
serving fleet's NodePort.** It holds the rollout freeze like the other flows; its
failure hooks tear down the bench endpoint and release the freeze, and with no load left
KEDA returns the fleet to 0.

**The load is an open-loop step profile of consecutive `vllm bench serve` phases at a
Poisson request rate:** wake, sized for about 3 running, triggers 0→1; surge, about 14
running in total, triggers 1→2; idle runs until the fleet is back at 0. Each loaded phase
lasts at least 15 min, to cover a cold start. Open-loop arrivals do not slow when the
server does, so the queue grows as real traffic would make it grow.

**A run passes when the fleet goes 0→1→2→0 with no manual step.** The SLO during the
surge is recorded, not gated: the latency and errors of a cold start are what the
cold-start elimination work and L2b measure themselves against. A run that never reaches
two replicas fails with its reason.

**The timeline puts on one time axis** the desired and ready replicas, running and waiting
per pod, the NodeClaims' launch and ready times, each request's send time, TTFT and
outcome, and the phase boundaries. Metrics come from range queries against the in-cluster
Prometheus after the run; events come from the cluster as in ADR-0030; request outcomes
come from `vllm bench serve`'s detailed results. The results go to the Prefect UI, the
results bucket and a committed report with a timeline chart.

## Consequences

- KEDA is a cluster add-on installed by Terraform (ADR-0029's split); the serving fleet,
  its Envoy proxy and its ScaledObject are one Argo CD app.
- The serving fleet and the bench Engine cannot run at the same time, because two
  replicas fill the quota.
