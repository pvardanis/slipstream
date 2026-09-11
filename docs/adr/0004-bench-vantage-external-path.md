<!-- ADR recording the benchmark vantage: a single external measurement path and ephemeral, authenticated public vLLM exposure for the L0 baseline. Decision accepted now, implementation deferred to #30/#31. -->

# ADR-0004: Benchmark vantage — single external path + ephemeral public vLLM exposure

- Status: Accepted; implementation deferred to #30/#31
- Date: 2026-09-11

## Context

#18's definition of done is `$/1M self-hosted vs commercial at SLO`
(TTFT p95 ≤ 1000 ms, TPOT ≤ 50 ms), and #30 states the requirement plainly:
"apples-to-apples or it is not a baseline."

Today the load generator runs in-cluster and reaches vLLM over ClusterIP
(`k8s/vllm.yaml`: ClusterIP-only, deliberately never public, reached via
`kubectl port-forward`). That gives the self-hosted arm an intra-cluster network
path — sub-millisecond, no internet — that a commercial API measured over the
internet can never have. The comparison is therefore unfair to the commercial
arm and flattering to self-hosted. The SLOs are user-experienced, so the network
belongs in the number, not hidden.

`kubectl port-forward` does not fix this: it is a single stream through the API
server, so it throttles throughput and adds latency — fine for one curl, invalid
for a load test.

## Decision

**Measure every L0 benchmark from one external vantage.** The baked bench-client
image (ADR-0003) runs via `docker` on an ephemeral EC2 instance outside the
cluster and hits both arms over the internet from that one host: vLLM over a
public endpoint, and the commercial API directly.

- **Fairness invariants:** same vantage for both arms, a characterized network
  path, repeated runs. The EC2 region is chosen to represent the target user and
  is documented per run (a region adjacent to the cluster reports optimistically
  low RTT).
- **Exposure is ephemeral and authenticated.** A public load balancer fronts
  vLLM only for the duration of a baseline run. Authentication is
  **connection-level** — mTLS terminated at the load balancer plus vLLM
  `--api-key` — never a per-request auth proxy, which would add latency to every
  request and corrupt TTFT. mTLS makes access IP-independent, so a personal VPN
  on the operator side is irrelevant; an EC2 elastic-IP allowlist is an optional
  extra layer.
- **A `just baseline-up` / `baseline-down` Terraform toggle** provisions and
  destroys the load balancer and the EC2 instance together, so teardown leaves
  nothing standing — preserving the duty-cycle property of ADR-0001.

This **supersedes the ClusterIP-never-public choice** recorded in the
`k8s/vllm.yaml` comment, but only as *ephemeral* exposure during a baseline run.
vLLM is not left publicly reachable between runs.

## Consequences

- A single measurement path serves all L0 benchmarks. The engine-tuning A/B
  sweeps (prefix-cache on/off, knob comparisons) now carry internet noise; it is
  largely common-mode and cancels in the deltas, but it widens variance and the
  tail. Mitigation: repeated runs with reported variance and p99 alongside p95.
- Exposing vLLM is a real security surface, bounded by ephemerality, mTLS, the
  api-key, and the optional IP allowlist. It is never a standing endpoint.
- A load balancer and an EC2 instance cost money only while a baseline runs, so
  they stay off the steady-state budget except during baselines.
- Implementation is deferred: its value only cashes out at #30/#31, and standing
  the exposure up earlier is unused surface. The decision is recorded now to
  avoid re-litigating it. Until it lands, the in-cluster ClusterIP launch of
  ADR-0003 remains in place. This work is sub-issue `6c`, which blocks #30.
- A developer laptop is rejected as the recorded-run host: uncontrolled Wi-Fi and
  VPN jitter inflate exactly the p95/p99 tail the report defends, and no reviewer
  can reproduce it. A laptop may serve only as an explicitly-labelled,
  uncontrolled "real client" data point.
