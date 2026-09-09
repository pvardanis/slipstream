<!-- Explains the vLLM Kubernetes manifest and the kubectl commands the justfile runs against it. -->

# `k8s/` — the vLLM manifest, explained

This directory holds the Kubernetes **manifest** that runs vLLM on the cluster:
[`vllm.yaml`](vllm.yaml). This README explains what a manifest is, walks every
field in that file, and covers the `kubectl` commands the [`justfile`](../justfile)
runs against it. It's written for someone new to Kubernetes.

## What a manifest is

A manifest is a YAML file describing **desired state** — *what* you want running,
not *how* to make it happen. You hand it to the cluster with `kubectl apply`, and
a control loop inside Kubernetes continuously works to make reality match the
file: if a pod dies, it recreates it; if you change the file and re-apply, it
reconciles the difference. This is **declarative** infrastructure — the same
model as Terraform, one layer up (Terraform builds the cluster; manifests run
workloads on it).

> Reference: [Kubernetes Objects](https://kubernetes.io/docs/concepts/overview/working-with-objects/)
> · [Declarative management](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/declarative-config/)

One file can hold several objects, separated by `---` (YAML document
separators). `vllm.yaml` holds three: a **Namespace**, a **Deployment**, and a
**Service**. Every object shares the same four top-level keys:

| Key | Meaning |
|---|---|
| `apiVersion` | Which API group/version defines this object (e.g. `apps/v1`). |
| `kind` | The object type (`Deployment`, `Service`, …). |
| `metadata` | Name, namespace, labels — how the object is identified. |
| `spec` | The desired state, specific to the kind. |

## The three objects

### 1. Namespace — a folder for our resources

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: slipstream
```

A [Namespace](https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/)
is a scope for names — think of it as a folder. Everything we deploy lives in
`slipstream`, so `kubectl -n slipstream …` targets only our stuff and doesn't mix
with the system pods in `kube-system`. Deleting the namespace deletes everything
inside it — a clean undeploy.

### 2. Deployment — keeps the vLLM pod running

A **Pod** is the smallest deployable unit: one or more containers that share a
network address. You rarely create pods directly. Instead a
[Deployment](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)
manages them for you — it guarantees "N replicas of this pod are always running"
and handles rollouts and restarts.

```yaml
spec:
  replicas: 1                 # how many identical pods to keep running
  selector:
    matchLabels:
      app: vllm               # this Deployment owns pods carrying label app=vllm
  template:                   # the pod blueprint the Deployment stamps out
    metadata:
      labels:
        app: vllm             # must match the selector above, or apply is rejected
    spec:
      ...
```

The **selector ↔ template label** pairing is the one piece of Kubernetes that
trips up newcomers: the Deployment finds "its" pods by label, so the label under
`template.metadata.labels` **must** match `selector.matchLabels`. Labels are also
how the Service (below) finds the pods to send traffic to.

Inside `template.spec` (the pod spec):

```yaml
nodeSelector:
  kubernetes.io/arch: amd64   # only schedule onto x86_64 nodes
```
[nodeSelector](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/)
constrains which node the pod may run on. We have one CPU node group (x86_64);
this documents the intent and keeps the pod off any GPU node a later layer adds.

```yaml
containers:
  - name: vllm
    image: vllm/vllm-openai-cpu:v0.29.0-x86_64   # the container image, version-pinned
    args:                                        # arguments passed to the image's entrypoint
      - --model
      - Qwen/Qwen2.5-0.5B-Instruct
      - --dtype
      - bfloat16
      - --max-model-len
      - "2048"
```
The image's entrypoint is the vLLM OpenAI server; `args` are its flags — which
model to serve, the numeric precision, and the max context length. Numbers are
quoted (`"2048"`) because container args are strings.

```yaml
    env:
      - name: VLLM_CPU_KVCACHE_SPACE
        value: "2"            # GiB of host RAM reserved for the CPU KV cache
```
[Environment variables](https://kubernetes.io/docs/tasks/inject-data-application/define-environment-variable-container/)
configure the process. This one is vLLM-specific.

```yaml
    ports:
      - name: http
        containerPort: 8000   # the port vLLM listens on; `name` lets other fields refer to it
```

```yaml
    securityContext:
      capabilities:
        add: ["SYS_NICE"]     # grant one Linux capability the CPU backend needs
```
By default containers drop most Linux
[capabilities](https://kubernetes.io/docs/tasks/configure-pod-container/security-context/).
vLLM's CPU backend pins worker threads, which needs `SYS_NICE`.

```yaml
    resources:
      requests:               # what the scheduler reserves — used to place the pod
        cpu: "1500m"          # 1.5 CPU cores (1000m = 1 core)
        memory: 4Gi
      limits:                 # the hard ceiling — exceed memory and the pod is OOM-killed
        memory: 5Gi
```
[Requests vs limits](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
is the distinction to internalise. **Requests** are what the scheduler guarantees
and uses to decide which node has room. **Limits** are the ceiling the kernel
enforces at runtime. These numbers are why the node group is `t3.large` (8 GiB):
a `t3.medium` (4 GiB) can't satisfy a 4Gi request plus the system overhead.

```yaml
    volumeMounts:
      - name: dshm
        mountPath: /dev/shm   # mount the volume (declared below) at this path
```
```yaml
volumes:
  - name: dshm
    emptyDir:
      medium: Memory          # a RAM-backed scratch volume (tmpfs)
      sizeLimit: 1Gi
```
An [emptyDir](https://kubernetes.io/docs/concepts/storage/volumes/#emptydir)
is scratch space that lives as long as the pod. `medium: Memory` makes it a
RAM disk — vLLM's CPU backend uses `/dev/shm` for inter-process communication.
(Note: a memory-backed volume counts against the container's memory limit, which
is why the limit and this size are tuned together.)

```yaml
    startupProbe:             # "has it finished starting?" — generous, for the model download
      httpGet: { path: /health, port: http }
      periodSeconds: 10
      failureThreshold: 60    # allow up to 10 min before giving up
    readinessProbe:           # "can it take traffic right now?" — gates the Service
      httpGet: { path: /health, port: http }
    livenessProbe:            # "is it still healthy?" — restarts the pod if not
      httpGet: { path: /health, port: http }
```
The three [probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)
answer different questions. The **startup** probe holds off the other two until
the server is up — critical here because the model is pulled from Hugging Face on
first boot and that's slow; without it, the liveness probe would kill the pod
mid-download. The **readiness** probe controls whether the Service routes traffic
to the pod. The **liveness** probe restarts a wedged pod.

### 3. Service — a stable address for the pods

Pods are disposable; their IPs change every restart. A
[Service](https://kubernetes.io/docs/concepts/services-networking/service/)
gives a stable virtual IP and DNS name in front of them, routing to whichever
pods currently match its selector.

```yaml
spec:
  type: ClusterIP             # in-cluster address only — no public exposure
  selector:
    app: vllm                 # send traffic to pods labelled app=vllm
  ports:
    - name: http
      port: 8000              # the port the Service exposes
      targetPort: http        # the pod port to forward to (by name)
```

`type: ClusterIP` is the deliberate choice this PR is built around. The
alternatives — `LoadBalancer` (provisions a cloud load balancer, public) or
`NodePort` — would create an AWS ELB. That's a **public endpoint** and a
**dangling resource that blocks `terraform destroy`**. ClusterIP creates nothing
in AWS, so teardown stays clean and the endpoint stays private. We reach it with
`port-forward` instead (below).

> Reference: [Service types](https://kubernetes.io/docs/concepts/services-networking/service/#publishing-services-service-types)

## The `kubectl` commands in the justfile

The [`justfile`](../justfile) drives the manifest with four `kubectl` verbs.

```sh
kubectl apply -f k8s/vllm.yaml
```
[`apply`](https://kubernetes.io/docs/reference/generated/kubectl/kubectl-commands#apply)
sends the manifest to the cluster and reconciles state to match it. Idempotent —
run it again after an edit and only the diff is applied. (`just deploy`)

```sh
kubectl -n slipstream rollout status deploy/vllm --timeout=600s
```
[`rollout status`](https://kubernetes.io/docs/reference/generated/kubectl/kubectl-commands#rollout)
blocks until the Deployment's pods are up and ready (or the timeout hits). This is
what makes `just deploy` *wait* for vLLM to actually serve instead of returning
immediately. `deploy/vllm` is shorthand for "the Deployment named vllm".

```sh
kubectl delete -f k8s/vllm.yaml --ignore-not-found
```
[`delete -f`](https://kubernetes.io/docs/reference/generated/kubectl/kubectl-commands#delete)
removes every object the file declares. `--ignore-not-found` means it won't error
if they're already gone, so `just undeploy` is safe to run twice.

```sh
kubectl -n slipstream port-forward svc/vllm 8000:8000
```
[`port-forward`](https://kubernetes.io/docs/reference/generated/kubectl/kubectl-commands#port-forward)
opens a tunnel from a local port to the Service through the Kubernetes API
server. `8000:8000` means "local 8000 → service 8000". This is how a private
`ClusterIP` Service becomes curl-able from your laptop without any public
endpoint — `just completion` starts this tunnel, curls `localhost:8000`, then
closes it.

## How a request flows

```
curl localhost:8000                 (your laptop)
   └─ kubectl port-forward tunnel ─▶ API server
        └─ Service vllm (ClusterIP 8000)
             └─ selector app=vllm ─▶ Pod :8000  (vLLM OpenAI server)
```

## Questions

This doc is a starting point — ask your teacher (the agent) to expand any part,
or say "quiz me on the manifest" to check what stuck.
