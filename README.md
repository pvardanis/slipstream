# slipstream

A self-hosted LLM inference platform on AWS EKS: open models served on spot GPUs,
with the cluster-level machinery around the model as the focus — autoscaling on
inference signals, spot-eviction survival mid-stream, cold-start elimination on
large weight loads, inference-aware routing, and prefill/decode disaggregation.
`make up` / `make down` conjures and destroys GPU capacity. It runs
cost-competitive against commercial APIs and proves it on a live scoreboard.

The name carries the platform's job: a slipstream is the low-drag wake behind a
fast-moving object. Every layer here reduces drag — prefix caching, KV-cache
offload, cold-start removal, prefill/decode split — so more tokens travel per
dollar. Token streaming and speculative decoding (a draft model riding the big
model's slipstream) sit literally inside the word.

## Status

Spec landed: [`docs/spec.md`](docs/spec.md) — build-ready, detailed enough to start
Layer 0. It was charted as a wayfinder map on this repo's issues (label
`wayfinder:map`); the decision log at the end of the spec traces every choice back to
its ticket.

## Non-goals

No fine-tuning or LLM training, no RAG / vector DBs / embeddings, no agent
frameworks, no Ray Train (Ray Serve is in scope), no multi-cloud or portability
abstractions, not a real product — this is a platform demonstrator.
