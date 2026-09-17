<!-- ADR recording the first GPU layer: a Karpenter-provisioned g5.xlarge spot node pool and the Qwen3-8B AWQ-INT4 / FP8-KV vLLM replica that runs on it (spec §3.1, epic #19 / issue #32). -->

# ADR-0006: GPU node pool (Karpenter) and the AWQ-INT4 / FP8-KV replica

- Status: Accepted
- Date: 2026-09-17

## Context

Every layer up to here runs on the small on-demand CPU node group the `eks` stack
creates (ADR-0001), serving a tiny CPU vLLM replica (issue #25). That CPU replica
stays — it is the substrate the platform machinery (autoscaling, routing, the
observability spine) is built and debugged against so GPU hours go to measurement,
not YAML debugging (spec §2). This ADR adds the first **GPU** capacity next to it:
a `g5.xlarge` spot A10G brought up by Karpenter, running Qwen3-8B AWQ-INT4 with an
FP8 KV cache, no tensor parallelism — the rig the platform is measured against
(CONTEXT.md, spec §3.1). It is the bring-up, not the tuning: the concurrency knob
sweep is a separate piece (issue #33).

Several decisions have more than one defensible answer; recording them here keeps
the reasoning visible and gives the four implementation pieces (#89–#92) a fixed
target. The current knowledge base (`terraform-patterns`, `kubernetes-patterns`,
`vllm-internals`) covers the general principles but predates Karpenter, AWQ, and
FP8 KV specifically; the Karpenter CRD shapes and vLLM engine flags below were
confirmed against current upstream docs (Karpenter AWS provider v1.14, vLLM
v0.29.0).

## Decision

### Karpenter control plane — via the eks module's submodule, Pod Identity

Karpenter installs through the `terraform-aws-modules/eks` Karpenter submodule the
cluster already depends on (`~> 21.0`), not a hand-rolled `helm_release` plus
hand-written IAM. The controller IAM policy, node IAM role, and SQS
spot-interruption queue are the kind of boilerplate the module maintains correctly;
re-implementing them by hand is tedious *and* a live hazard — a stale controller
policy is one of the documented GPU bring-up traps (silent quota-0 / no-provision
failures, `docs/research/llm-inference-on-eks-prior-art.md`).

The controller authenticates with **EKS Pod Identity**, not IRSA/OIDC. The cluster
already runs the `eks-pod-identity-agent` addon (added `before_compute`), so Pod
Identity needs no OIDC provider stood up; it is the module default and the newer of
the two paths, with no upside to IRSA here.

The interruption queue is provisioned because it is part of a correct install —
Karpenter cordons and drains a node on the two-minute spot notice through it. This
ADR does **not** add mid-generation token-stream survival (drain-aware graceful
shutdown, request bleed-off): that is the whole deliverable of L2b / chaos day
(spec §4). The GPU replica here relies on Karpenter's default drain only.

### NodePool and EC2NodeClass — raw manifests, not `kubernetes_manifest`

`NodePool` (`karpenter.sh/v1`) and `EC2NodeClass` (`karpenter.k8s.aws/v1`) are CRDs;
they cannot exist until the Karpenter Helm release is applied. Shipping them as
Terraform `kubernetes_manifest` resources would need those CRDs live at *plan* time
— a chicken-and-egg that makes the plan fragile and order-dependent. Instead they
ship as raw manifests applied by a `just` recipe, matching the existing
`k8s/*.yaml` pattern (`vllm.yaml`, `otel-collector.yaml`) and keeping them under the
same conftest policy set. This also draws a clean line: Karpenter *install* is
Terraform (#89), Karpenter *config* is manifests (#90).

The GPU `NodePool` is restricted to `g5.xlarge`, `karpenter.sh/capacity-type In
["spot"]`, and carries a taint `nvidia.com/gpu=true:NoSchedule` so only pods that
tolerate it (the GPU replica) land there — nothing else drifts onto the expensive
card. Its `disruption` block uses `consolidationPolicy: WhenEmptyOrUnderutilized`
with a `consolidateAfter` window, which is what returns spend to zero: when the GPU
replica scales to zero the node goes empty and Karpenter reaps it.

### GPU driver — AL2023 NVIDIA AMI + a standalone device-plugin DaemonSet

The `EC2NodeClass` selects the AL2023 **NVIDIA** accelerated AMI, which ships the
GPU driver baked in — no in-cluster driver compile. The AMI does **not** advertise
the GPU to Kubernetes on its own: without the `nvidia-device-plugin` DaemonSet the
`nvidia.com/gpu` resource is never registered and the vLLM pod stays `Pending`
forever. So the plugin ships as one DaemonSet manifest alongside the node pool.

The NVIDIA GPU Operator (which would manage driver, plugin, and DCGM together) is
rejected as overkill for a single node type; Bottlerocket-NVIDIA (driver *and*
plugin bundled) is rejected to stay on the familiar AL2023 base. One AMI alias plus
one DaemonSet is the smallest thing that exposes the card.

### Node root disk — 100 GB gp3

The `g5.xlarge`'s default EBS root (~20 GB) cannot hold the vLLM GPU image (~10 GB)
plus the ~6 GB of AWQ weights plus CUDA/compile cache. The `EC2NodeClass`
`blockDeviceMappings` sizes a 100 GB gp3 root — the same class of fix already made
for the baseline bench host's root volume. The instance's local NVMe is ephemeral
and unmanaged; the model store lives on the EBS root, not there.

### The replica — `k8s/vllm-gpu.yaml`, separate from the CPU replica

The GPU replica is its own manifest, not a parametrised `vllm.yaml`. It differs from
the CPU replica in image, resources, tolerations, node target, and engine args;
folding both into one templated file trades a small, honest duplication for a
conditional mess. The two coexist and are deployed independently
(`just deploy` = CPU, `just gpu-deploy` = GPU), each conftest-linted.

Engine configuration (confirmed against vLLM v0.29.0):

- Image `vllm/vllm-openai:v0.29.0`, matching the CPU replica's pinned
  `v0.29.0-x86_64` so the engine version is one number across both.
- `--model Qwen/Qwen3-8B-AWQ` — the official Qwen AWQ-INT4 checkpoint (public,
  Apache-2.0, ~6.1 GB measured — spec §3.1 and the feasibility research quote ~5.5 GB as
  a pre-build estimate; the checkpoint's actual safetensors are the ~6.1 GB used for the
  disk sizing above). Its `config.json` declares `quant_method: awq`, so vLLM would
  auto-select the Marlin kernel; `--quantization awq_marlin` is passed **explicitly
  anyway** to pin the recipe (spec §3.1) rather than depend on auto-detection.
- `--kv-cache-dtype fp8` — the FP8 KV cache (A10G/Ampere has no hardware FP8 for
  *weights*, so INT4 AWQ is the weight quantization, but the KV cache still uses FP8,
  roughly doubling concurrent-sequence capacity — spec §3.1).
- `--tensor-parallel-size 1` (the default; stated to make "no TP" explicit — 8B fits
  one A10G, spec §3.1).
- `--max-model-len 4096`, `--gpu-memory-utilization 0.90`, `--max-num-seqs 16` are
  **starting values, not tuned** — the concurrency ceiling is found by the knob
  sweep in #33. The manifest says so in a comment so the numbers are not mistaken for
  measured settings.

Reproducibility (spec §3.1) is pinned three ways: the image tag above, the
`--quantization awq_marlin` recipe, and the model by **HF revision commit SHA**
(`--revision <sha>`) rather than a floating branch — a floating `main` would
silently re-quantize the weights out from under a re-run.

### Scale-to-zero — manual scale plus Karpenter consolidation, no KEDA

Returning GPU spend to zero (issue #32 AC) is, at this layer, node-level and manual:
`just gpu-down` sets the GPU Deployment to `replicas: 0`, the g5 node goes empty, and
Karpenter's `consolidateAfter` reaps it. `just gpu-up` scales back to one.
Autoscaling on inference signals (KEDA/HPA on `num_requests_waiting`) is L2a/L2b (spec
§4 — scale-up-from-zero under load at L2a, scale-to-zero + wake at L2b, later issues);
adding it here would pull that scope into the bring-up and break the depth-first rule.

### Harness path — reuse the baseline, GPU Service takes nodePort 30800

"The L0 harness runs green against it" reuses the existing baseline mutual-TLS ALB →
NodePort path (#25/#27). The GPU `Service` takes over the fixed `nodePort: 30800`
with `selector: app=vllm-gpu`, so the baseline needs no change — but the CPU and GPU
replicas are exposed **one at a time**, never both. The baseline is ephemeral and
single-arm by design (one bench sweep at a time); a second nodePort with ALB
retargeting is multi-replica routing (L3), not this layer.

## Consequences

- The four implementation pieces have a fixed target and a strict order: install
  (#89, Terraform) → node pool + device plugin (#90, manifests) → replica (#91,
  manifest) → cloud verify (#92). Each is one kind of change, reviewable on its own.
- Provisioning a live GPU costs real money, so the acceptance criteria that need one
  — model serves, harness green, scale-to-zero returns spend to zero — are verified
  only in the label-gated cloud tier (ADR-0002, #92), hand-triggered, never on push.
  The no-cloud tier (fmt/validate/tflint, kubeconform, conftest, plan-only tests) is
  all that gates the install and manifest PRs.
- A GPU `Service` on `nodePort 30800` means bringing the GPU replica up takes the
  baseline path away from the CPU replica. That is acceptable for a single-arm bench
  but is a constraint L3 will have to lift when more than one replica is exposed.
- The tuning knobs (`max-num-seqs`, `gpu-memory-utilization`, `max-model-len`) are
  deliberately un-tuned starting values here; treating #32's numbers as measured
  settings would be wrong — #33's sweep produces the real ones.
- Choosing the eks module's Karpenter submodule couples the install to that module's
  version and conventions. Accepted for the same reason the cluster itself uses the
  module: the IAM and interruption wiring it maintains is exactly the surface where a
  hand-rolled version rots into a silent no-provision failure.
