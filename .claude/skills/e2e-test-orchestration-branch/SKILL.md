---
name: e2e-test-orchestration-branch
description: Test an unmerged orchestration branch or PR stack end to end on the live Prefect worker. Use when the user wants a branch's sweep, cache, resume, or flow behaviour verified on the cluster before merge.
---
<!-- Runs an unmerged orchestration branch end to end on the live in-cluster Prefect worker: build and push a qa-tagged image, swap it onto the worker, register from the branch, copy test grids into the pod, then walk the user through one scenario at a time and clean up. -->

# e2e-test-orchestration-branch

The worker runs whatever orchestration image `:main` names, and `sweep-dispatch` runs from `main`, so neither exercises a branch. This skill puts the branch's own image on the worker and drives the sweep from the workstation. Work from the top of the branch or stack: one image covers every scenario.

The agent runs every command: build, push, the worker swap, registration, grid copies, triggers, S3 reads. When the harness denies one, hand that command to the user to run with `!` and carry on from its output. The user watches each run in the Prefect UI and confirms it before the next one starts.

## Preconditions

The stack is up (`kubectl -n prefect get deploy prefect-worker` lists it), Docker is running, and the branch's work is committed. If the stack is down, stop and say so; bringing it up is the user's call.

A knob sweep and a KV cache A/B each bring their own bench endpoint and refuse to start while one is up, so before either scenario `terraform -chdir=terraform/bench-endpoint state list` must print nothing; otherwise ask the user to run `just bench-endpoint-down`. Never run `just bench-endpoint-down` or `just stack-down` while a run is in flight: either destroys that run's endpoint.

## Step 1 — Build and push a `qa-` image

```bash
repo="$(terraform -chdir=terraform/bootstrap output -raw orchestration_image_repo_url)"
region="$(terraform -chdir=terraform/bootstrap output -raw region)"
tag="qa-<label>"   # e.g. qa-303; never a git sha
docker build --platform linux/amd64 -f orchestration/Dockerfile \
  --build-arg ORCH_IMAGE_REF="${repo}:${tag}" -t "${repo}:${tag}" .
aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "${repo%%/*}"
docker push "${repo}:${tag}"
```

- `linux/amd64`: the worker nodes are amd64; an Apple-silicon default build will not run.
- The `qa-` prefix: CI publishes `:main` only when the commit's content tag is absent from ECR. A branch image pushed under that same sha tag makes the post-merge CI run skip, and `:main` never moves.

Run the build in the background; it is slow under emulation. Done when `docker push` prints the digest.

## Step 2 — Swap the worker onto it

Argo CD deploys the worker, so a `kubectl set image` on its Deployment is reverted as drift. The tag lives on the `prefect-worker` Application as the Helm parameter Image Updater writes, so stop Image Updater and write the parameter yourself:

```bash
kubectl -n argocd get application prefect-worker -o jsonpath='{.status.summary.images[0]}'
kubectl -n argocd scale deploy/argocd-image-updater --replicas=0
kubectl -n argocd patch application prefect-worker --type json -p '[{"op": "replace", "path": "/spec/source/helm/parameters", "value": [{"name": "worker.image.repository", "value": "<repo>"}, {"name": "worker.image.prefectTag", "value": "<tag>"}]}]'
kubectl -n argocd wait application/prefect-worker --for=jsonpath='{.status.summary.images[0]}=<repo>:<tag>' --timeout=300s
kubectl -n prefect rollout status deploy/prefect-worker
```

- Scaled to zero, Image Updater cannot write the newest content tag back over yours; it would also never pick a `qa-` tag, which its `^[0-9a-f]{12}$` filter skips.
- The root Application ignores its child Applications' Helm parameters, so it leaves the patch in place.
- A rollout freeze held by a running flow blocks the sync; wait for the run to end.

Done when the rollout reports success. `just stack-down` discards the swap.

## Step 3 — Register from the branch

Run `just prefect-register-sweep` from the branch (`just prefect-register-kv-ab` for the KV cache A/B), so the deployment's parameters match the branch's flow, and hold `just prefect-ui` open in the background for the API port-forward. Trigger every run from the workstation; a `sweep-dispatch` run re-registers `main`'s knob-sweep deployment over this one.

## Step 4 — Write the test grids

The image bakes `bench/configs/sweep-grid.yaml` at `/app/bench/configs/sweep-grid.yaml`, and the deployment takes the grid as the `sweep_grid` parameter. So derive each grid a scenario needs from `bench/configs/sweep-grid.yaml` into `/tmp/qa-<label>/`, with the fewest points and cells that show the behaviour, and validate each:

```bash
uv run python -c "from pathlib import Path; from slipstream_bench.contract.grid import load_grid; print(load_grid(Path('/tmp/qa-<label>/grid-2pt.yaml')).tier1)"
```

- Keep `load.num_prompts` a multiple of `load.num_prefixes`. The client sends `num_prefixes × floor(num_prompts / num_prefixes)` requests while the validity gate counts `num_prompts` as attempted, so a remainder reads as failed requests and a healthy cell is rejected.

For the KV cache A/B the same holds for its workload: the image bakes `bench/configs/kv-cache-extension-ab.yaml` at `/app/bench/configs/kv-cache-extension-ab.yaml`, and the deployment takes it as the `kv_ab_config` parameter.

Copy each grid into the pod, and copy again after any worker restart: `/tmp` in the pod does not survive one.

```bash
kubectl -n prefect exec -i deploy/prefect-worker -- sh -c 'cat > /tmp/grid-2pt.yaml' < /tmp/qa-<label>/grid-2pt.yaml
```

Done when every grid validates locally and reads back from the pod.

## Step 5 — Run the scenarios one at a time

Derive one scenario per behaviour the branch's issues or PRs claim. Give each its own `run_id`, a grid, and the signals the user will see in the UI: which tasks run or are absent (`deploy`, `scrape`), each cell's state (**Completed** or **Cached**), and the per-point log line (`point <slug> done: N cells run|resumed`). Present them as a table ordered by cost — runs that redeploy nothing first, full re-measures last — and start the first.

```bash
PREFECT_API_URL=http://127.0.0.1:4200/api uv run prefect deployment run knob-sweep/knob-sweep \
  --params '{"run_id": "<run-id>", "sweep_grid": "/tmp/<grid>.yaml"}'
```

- Each scenario gets a fresh `run_id`: a rejected cell's JSON stays under its run's S3 prefix and clutters the next look.
- A resume is a new trigger with the same `run_id`, not a UI Retry.
- Read a surprising result from its cell before theorising: `aws s3 cp s3://<bucket>/knob-sweep/<run-id>/<point-slug>/<cell>.json -` and compare `completed` against `num_prompts`, `failed`, `request_goodput` against `request_throughput`, and the p99 TTFT/TPOT across runs of the same config.

Separate what the scenario tests from what the rig did: a slow engine or a render failure on a fresh measurement is a finding to file, not a failed scenario. Done when every scenario has a confirmed outcome from the user.

## Step 6 — Record and clean up

- Post the scenario table with each `run_id` and its observed outcome as a comment on the top PR.
- Offer an issue for each defect found outside the branch's scope.
- Restore the worker: `just stack-down`, or `kubectl -n argocd scale deploy/argocd-image-updater --replicas=1`, after which Image Updater's first cycle writes the newest content tag back and Argo CD rolls the worker to it.
- With the user's go-ahead, delete the tag (`aws ecr batch-delete-image --repository-name "${repo#*/}" --image-ids imageTag=<tag>`) and `trash /tmp/qa-<label>`.
