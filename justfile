# Task runner for the slipstream platform. `just cluster-up` conjures the cluster;
# `just cluster-down` returns spend to zero. Credentials come from your AWS_PROFILE.
# Run `just` with no args to list recipes.

eks_dir := "terraform/eks"
bootstrap_dir := "terraform/bootstrap"
bench_endpoint_dir := "terraform/bench-endpoint"
manifests := "k8s/vllm.yaml"
gpu_pool_manifests := "k8s/gpu-node-pool.yaml"
gpu_manifests := "k8s/vllm-gpu.yaml"
bench_dockerfile := "bench/Dockerfile"
# Derives the bench image's served model, tokenizer slug, and content tags from
# model.yaml + git; the build, the pull ref, and the completion smokes all read
# the served model through it, so the one definition drives them all.
image_tag_tool := "bench/image-tag.sh"
# Builds the image from model.yaml, tagging it with each ref passed; shared with
# the CI workflows so the one build command never diverges.
build_image_tool := "bench/build-image.sh"
# Empty pulls the floating `<slug>-main` tag that CI publishes on every merge to
# main; set a `<slug>-<sha>` tag to pin a reproducible run.
bench_image_tag := ""
cpu_model := "Qwen/Qwen2.5-0.5B-Instruct"
otel_manifests := "k8s/otel-collector.yaml"
otel_config := "k8s/otel-collector-config.yaml"

# List available recipes.
default:
    @just --list

import "just/cluster.just"
import "just/serve.just"
import "just/bench.just"
import "just/obs.just"
import "just/test.just"
import "just/orchestrate.just"
