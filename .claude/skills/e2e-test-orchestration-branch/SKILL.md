<!-- Runs an unmerged orchestration branch end to end on the live in-cluster Prefect worker: build and push a qa-tagged image, swap it onto the worker, register from the branch, copy test grids into the pod, then walk the user through one scenario at a time and clean up. -->
---
name: e2e-test-orchestration-branch
description: Test an unmerged orchestration branch or PR stack end to end on the live Prefect worker. Use when the user wants a branch's sweep, cache, resume, or flow behaviour verified on the cluster before merge.
---

# e2e-test-orchestration-branch

The worker runs whatever orchestration image `:main` names, and `sweep-dispatch` runs from `main`, so neither exercises a branch. This skill puts the branch's own image on the worker and drives the sweep from the workstation. Work from the top of the branch or stack: one image covers every scenario.

The agent builds, pushes, writes grids and reads S3. **Hand every `kubectl` command to the user** to run with `!`: the agent's `kubectl exec` is blocked. The user watches each run in the Prefect UI and confirms it before the next one starts.

## Preconditions

The stack is up (`kubectl -n prefect get deploy prefect-worker` lists it), Docker is running, and the branch's work is committed. If the stack is down, stop and say so; bringing it up is the user's call.

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

## Step 2 — Swap the worker onto it (user)

Record the current image first, so Step 6 can restore it:

```bash
kubectl -n prefect get deploy prefect-worker -o jsonpath='{.spec.template.spec.containers[0].image}'
kubectl -n prefect set image deploy/prefect-worker prefect-worker="<repo>:<tag>"
kubectl -n prefect rollout status deploy/prefect-worker
```

Done when the rollout reports success. Terraform does not see this swap; `just stack-down` discards it.

## Step 3 — Register from the branch (user)

`just prefect-register` from the branch, so the deployment's parameters match the branch's flow. Keep `just prefect-ui` open in another shell. Trigger every run from the workstation; a `sweep-dispatch` run re-registers `main`'s deployment over this one.

## Step 4 — Write the test grids

The image bakes `bench/sweep-grid.yaml` at `/app/bench/sweep-grid.yaml`, and the deployment takes the grid as the `sweep_grid` parameter. So derive each grid a scenario needs from `bench/sweep-grid.yaml` into `/tmp/qa-<label>/`, with the fewest points and cells that show the behaviour, and validate each:

```bash
uv run python -c "from pathlib import Path; from slipstream_bench.contract.grid import load_grid; print(load_grid(Path('/tmp/qa-<label>/grid-2pt.yaml')).tier1)"
```

- Keep `load.num_prompts` a multiple of `load.num_prefixes`. The client sends `num_prefixes × floor(num_prompts / num_prefixes)` requests while the validity gate counts `num_prompts` as attempted, so a remainder reads as failed requests and a healthy cell is rejected.

Hand the user the copy, and re-run it after any worker restart: `/tmp` in the pod does not survive one.

```bash
kubectl -n prefect exec -i deploy/prefect-worker -- sh -c 'cat > /tmp/grid-2pt.yaml' < /tmp/qa-<label>/grid-2pt.yaml
```

Done when every grid validates locally and the user's copy succeeded.

## Step 5 — Run the scenarios one at a time

Derive one scenario per behaviour the branch's issues or PRs claim. Give each its own `run_id`, a grid, and the signals the user will see in the UI: which tasks run or are absent (`deploy`, `scrape`), each cell's state (**Completed** or **Cached**), and the per-point log line (`point <slug> done: N cells run|resumed`). Present them as a table ordered by cost — runs that redeploy nothing first, full re-measures last — and start the first.

```bash
PREFECT_API_URL=http://127.0.0.1:4200/api uv run prefect deployment run knob-sweep/knob-sweep \
  --param run_id=<run-id> \
  --param instance_id="$(terraform -chdir=terraform/bench-endpoint output -raw bench_host_instance_id)" \
  --param sweep_grid=/tmp/<grid>.yaml
```

- Each scenario gets a fresh `run_id`: a rejected cell's JSON stays under its run's S3 prefix and clutters the next look.
- A resume is a new trigger with the same `run_id`, not a UI Retry.
- Read a surprising result from its cell before theorising: `aws s3 cp s3://<bucket>/sweeps/<run-id>/<point-slug>/<cell>.json -` and compare `completed` against `num_prompts`, `failed`, `request_goodput` against `request_throughput`, and the p99 TTFT/TPOT across runs of the same config.

Separate what the scenario tests from what the rig did: a slow engine or a render failure on a fresh measurement is a finding to file, not a failed scenario. Done when every scenario has a confirmed outcome from the user.

## Step 6 — Record and clean up

- Post the scenario table with each `run_id` and its observed outcome as a comment on the top PR.
- Offer an issue for each defect found outside the branch's scope.
- Restore the worker: `just stack-down`, or (user) `kubectl -n prefect set image deploy/prefect-worker prefect-worker=<image recorded in Step 2>`.
- With the user's go-ahead, delete the tag (`aws ecr batch-delete-image --repository-name "${repo#*/}" --image-ids imageTag=<tag>`) and `trash /tmp/qa-<label>`.
