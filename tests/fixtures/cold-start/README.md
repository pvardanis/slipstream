<!-- Provenance of the recorded cold-start fixtures: one true-zero cold start of the GPU replica on the Rig, captured for the cold-start breakdown's driver tests (#37, #537). -->

# Cold-start fixtures

One cold start from a true zero on the Rig (on-demand g5.xlarge, vLLM v0.29.0), recorded on
2026-11-13. These are the raw cluster and engine responses for the cold-start breakdown's
`Kubectl` fake to replay, so its parsing is tested against real output.

## How it was run

With the GPU NodePool up and no GPU NodeClaim left, `deploy/vllm-gpu` was applied at zero
replicas, scaled to one (its `ScalingReplicaSet` event is at 15:21:43Z) and waited on with
`kubectl rollout status`. It was Ready at 15:30:48Z. Everything was captured right after Ready, before events expired:

| File | Command |
|---|---|
| `pod.json` | `kubectl -n slipstream get pod vllm-gpu-bcd494f9-xsjvl -o json` |
| `events.json` | `kubectl -n slipstream get events -o json` (the whole namespace, so it also holds an unrelated External Secrets `SecretStore` event) |
| `nodeclaim.json` | `kubectl get nodeclaim gpu-xqwqj -o json` (the NodeClaim whose `status.nodeName` is the pod's node) |
| `node.json` | `kubectl get node ip-10-0-17-228.eu-west-1.compute.internal -o json` |
| `engine.log` | `kubectl -n slipstream logs vllm-gpu-bcd494f9-xsjvl -c vllm --timestamps` |

The engine log was taken with `--timestamps`, so every line starts with kubelet's
nanosecond RFC 3339 time. vLLM's logger lines also carry vLLM's own second-resolution,
year-less time; progress bars and uvicorn access lines do not. The pod's
container restart count was 0, so the log is the whole startup.

The files are as `kubectl` returned them, except that the repo's pre-commit hooks strip
trailing whitespace and fix end-of-file newlines. None of the captured objects
carried a secret or the cluster's AWS account id; the account id in `node.json`
(602401143452) is AWS's public EKS image registry.
