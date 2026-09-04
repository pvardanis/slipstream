<!-- Research note: which AWS spot GPUs fit a $100-250/mo budget for self-hosted LLM inference, and which model sizes/quantizations actually fit their VRAM (grounded in KV-cache arithmetic). -->

# GPU / model feasibility within a $100–250/month spot budget

**Date of research:** 2026-09-04. All prices are snapshots and move constantly — re-check before committing.

## TL;DR recommendation

**Start with `g5.xlarge` (1× NVIDIA A10G, 24 GB) on spot, running Llama-3.1-8B quantized to FP8 (or AWQ 4-bit) with an FP8 KV cache.**

- A10G spot has been the best value of the 24 GB cards: ~$0.44–0.64/hr in `us-east-1` vs $1.006 on-demand. L4 (`g6.xlarge`) is nominally cheaper on-demand but its spot discount is thin (~$0.73/hr), so it is usually *more* expensive than A10G on spot.
- **The budget does not buy 24/7.** Even the cheapest card (A10G at ~$0.50/hr spot) costs ~$365/mo run continuously. $100–250/mo implies **part-time or scale-to-zero** (~7–17 hrs/day), or riding the cheapest AZ at the low end of the spot range. Plan for autoscaling to zero.
- **Weights quantization is effectively mandatory** to leave usable KV-cache headroom on 24 GB. BF16 8B weights (16 GB) fit but leave room for only ~10 concurrent sequences at 4k context. FP8/AWQ weights free ~2–3× that.
- **~30B needs a 48 GB card.** That means `g6e.xlarge` (L40S, 48 GB), whose spot (~$1.57/hr) blows the 24/7 budget — viable only part-time.
- **A100 is out of budget entirely on AWS.** AWS sells A100 only as 8-GPU `p4d`/`p4de` instances (spot floor ~$7/hr ≈ $5,000+/mo). There is no single-A100 instance. Ignore A100 for this budget.
- **One bigger card beats 2-GPU tensor parallel here.** Tensor parallelism requires multiple GPUs *inside one instance*; two separate spot instances cannot share a model. The 2-A10G option is `g5.12xlarge` (4× A10G, ~$5.67/hr on-demand) — far pricier than a single 48 GB L40S. For ~30B, one L40S wins on both cost and simplicity.

---

## 1. Candidate cards and instances

Single-GPU `xlarge` sizes are the relevant ones for this budget. Specs from AWS product pages; prices are third-party tracker snapshots (Vantage / DoiT) for `us-east-1` unless noted, captured 2026-09-04.

| Instance | GPU | VRAM | vCPU / RAM | On-demand $/hr | Spot $/hr (snapshot) | Notes |
|---|---|---|---|---|---|---|
| `g5.xlarge` | 1× A10G | 24 GB | 4 / 16 GiB | $1.006 | ~$0.44–0.64 | Best spot value of the 24 GB cards |
| `g6.xlarge` | 1× L4 | 24 GB | 4 / 16 GiB | $0.805 | ~$0.73 | Thin spot discount; often dearer than A10G on spot |
| `g6e.xlarge` | 1× L40S | 48 GB | 4 / 32 GiB | $1.861 | ~$1.57 | Only single-instance path to ~30B; over 24/7 budget |
| `g5.12xlarge` | 4× A10G | 96 GB | 48 / 192 GiB | ~$5.67 | (varies) | 2–4-GPU TP option; expensive |
| `p4d.24xlarge` | 8× A100 40 GB | 320 GB | 96 / 1152 GiB | ~$21.96 | ~$7.07 | Only A100 40 GB path; out of budget |
| `p4de.24xlarge` | 8× A100 80 GB | 640 GB | 96 / 1152 GiB | ~$27.45 | ~$22.28 | Only A100 80 GB path; out of budget |

Reported "usable" per-GPU VRAM in trackers: A10G ~22.35 GiB, L40S ~44.7 GiB (some GB reserved by the driver). vLLM further caps usage at `gpu_memory_utilization` (default 0.9).

### Availability by region / AZ (uncertain — verify with the CLI)

- G5 (A10G) and G6 (L4) are the most broadly available G-series families across US/EU regions.
- G6e (L40S) launched GA Aug 2024 and expanded through 2024–2025 (incl. Seoul, Mar 2025); it is in fewer regions than G5/G6.
- I did **not** verify per-AZ spot availability or interruption rates from a primary source. Get ground truth with:
  `aws ec2 describe-spot-price-history --instance-types g5.xlarge --product-descriptions "Linux/UNIX" --start-time <now>`
  and check the Spot placement score. Treat all spot $/hr above as directional.

---

## 2. Budget translation (730 hrs/month)

| Sustained rate | $/month @ 24/7 | Hours to hit $100 | Hours to hit $250 |
|---|---|---|---|
| $0.44/hr (A10G low) | $321 | 227 (~7.5 hr/day) | 568 (~19 hr/day) |
| $0.50/hr (A10G typ.) | $365 | 200 (~6.7 hr/day) | 500 (~16.7 hr/day) |
| $0.73/hr (L4 spot) | $533 | 137 (~4.5 hr/day) | 342 (~11 hr/day) |
| $1.57/hr (L40S spot) | $1,146 | 64 (~2 hr/day) | 159 (~5 hr/day) |

**Takeaway:** No single-GPU spot instance runs 24/7 inside $250/mo. The budget is really a *duty-cycle* budget. A10G on spot with scale-to-zero (idle → 0 replicas) is the only combination that gives meaningful daily uptime under $250. If you need genuine 24/7, the budget is roughly 1.5–2× too low even for the cheapest card.

---

## 3. KV-cache arithmetic

Per-token KV memory (both K and V, all layers):

```
per_token_KV_bytes = 2 × n_layers × n_kv_heads × head_dim × dtype_bytes
```

`dtype_bytes` = 2 for BF16 KV, 1 for FP8 KV. Grouped-Query Attention (GQA) shrinks this via small `n_kv_heads`; models without GQA (e.g. Llama-2-13B, MHA) pay a brutal KV tax.

### Model configs (from model cards / config.json)

| Model | n_layers | n_kv_heads | head_dim | Attn | BF16 weights |
|---|---|---|---|---|---|
| Llama-3.1-8B | 32 | 8 (GQA) | 128 | GQA | ~16 GB |
| Llama-2-13B | 40 | 40 (MHA) | 128 | MHA | ~26 GB |
| Qwen2.5-32B | 64 | 8 (GQA) | 128 | GQA | ~64 GB |

*(Llama-2-13B config values are from general knowledge / secondary sources — the official `config.json` is gated (HTTP 401) and could not be fetched directly. The 8B and 32B configs were read from primary sources; see Sources.)*

### Per-token and per-sequence KV (BF16 KV cache)

| Model | per-token KV | per-seq @ 4k ctx |
|---|---|---|
| Llama-3.1-8B | 2×32×8×128×2 = **131,072 B ≈ 128 KiB** | 4096 × 128 KiB = **0.5 GiB** |
| Llama-2-13B | 2×40×40×128×2 = **819,200 B ≈ 800 KiB** | 4096 × 800 KiB = **3.125 GiB** |
| Qwen2.5-32B | 2×64×8×128×2 = **262,144 B ≈ 256 KiB** | 4096 × 256 KiB = **1.0 GiB** |

The 8B figure (~128 KB/token) matches the worked example in the brief. Note Llama-2-13B costs **6.25×** more KV per token than 8B despite being ~1.6× the params — that is the MHA penalty. Prefer a GQA-based 13B-class model if 13B is a real requirement.

### Concurrent-sequence capacity

Method: usable pool = `VRAM × 0.9` (vLLM default utilization) − weights − ~1 GiB runtime/activation overhead. Concurrency = pool ÷ per-seq KV @ 4k. FP8 KV halves the per-seq cost. These are **worst-case, every-slot-full-at-4k** numbers; real chat traffic averages far shorter generations, so live concurrency is typically several× higher.

**24 GB card (A10G / L4), pool budget ≈ 21.6 GiB:**

| Model + weights | weights | KV pool | seqs @4k (BF16 KV) | seqs @4k (FP8 KV) |
|---|---|---|---|---|
| 8B BF16 | 16 GB | ~4.6 GiB | ~9 | ~18 |
| 8B FP8 | ~8 GB | ~12.6 GiB | ~25 | ~50 |
| 8B AWQ 4-bit | ~5.5 GB | ~15 GiB | ~30 | ~60 |
| 13B BF16 | 26 GB | — | **does not fit** | does not fit |
| 13B FP8 | ~13 GB | ~7.6 GiB | ~2 | ~4 |
| 13B AWQ 4-bit | ~7.5 GB | ~13 GiB | ~4 | ~8 |
| 32B AWQ 4-bit | ~19 GB | ~1.6 GiB | ~1 | ~3 |

**48 GB card (L40S), pool budget ≈ 43.2 GiB:**

| Model + weights | weights | KV pool | seqs @4k (BF16 KV) | seqs @4k (FP8 KV) |
|---|---|---|---|---|
| 8B BF16 | 16 GB | ~26 GiB | ~52 | ~100+ |
| 13B BF16 | 26 GB | ~16 GiB | ~5 | ~10 |
| 32B FP8 | ~32 GB | ~10 GiB | ~10 | ~20 |
| 32B AWQ 4-bit | ~19 GB | ~23 GiB | ~23 | ~46 |

**Reconciling the brief's "~50 seqs" for 8B on a 24 GB card:** strict full-4k math gives only **~9 sequences** for BF16 weights on 24 GB — not 50. You reach ~50 concurrent at 4k only with **FP8 weights + FP8 KV on 24 GB**, or with **BF16 on a 48 GB card**, or if average context is ~1–1.5k rather than a full 4k per slot (typical for chat). The "~50" is realistic under those conditions, not for BF16-weights-at-full-4k. This is the concrete reason weight quantization is the default recommendation on 24 GB.

### Where quantization becomes necessary

- **8B:** fits BF16 on 24 GB, but quantize weights (FP8/AWQ) to get useful concurrency. FP8 KV on top roughly doubles it again.
- **13B:** BF16 does **not** fit 24 GB (26 GB weights). Even quantized, MHA KV makes 24 GB concurrency poor; use 48 GB or a GQA 13B-class alternative.
- **~30B:** requires a 48 GB card. FP8 weights fit with modest concurrency; AWQ 4-bit gives comfortable concurrency. Does not meaningfully fit 24 GB.

---

## 4. Tensor-parallel-across-2-GPU vs one larger card

- TP requires the GPUs to be in **one instance** (NCCL over NVLink/PCIe). Two separate spot instances cannot serve one model together.
- The 2× A10G path is `g5.12xlarge` (4× A10G, ~$5.67/hr on-demand) — you cannot buy just two A10Gs in one box below that class. That is ~4–9× the cost of a single `g6e.xlarge` L40S (48 GB) and gives the same 48 GB of aggregate VRAM but with TP communication overhead and 4 GPUs' worth of spend.
- **Conclusion for ~30B: one L40S (48 GB) beats 2-GPU TP** on cost, simplicity, and interruption blast-radius. Reserve multi-GPU TP for models that genuinely exceed 48 GB (e.g. 70B), which is outside this budget anyway.

---

## 5. Recommendation summary

1. **Primary:** `g5.xlarge` (A10G, 24 GB) spot + **Llama-3.1-8B FP8 (or AWQ) + FP8 KV cache**, with autoscale-to-zero. Fits budget only as a duty-cycle (~7–17 hrs/day at $100–250/mo). Gives ~25–60 concurrent seqs at 4k depending on quant.
2. **If you need ~30B:** `g6e.xlarge` (L40S, 48 GB) spot + Qwen2.5-32B AWQ 4-bit. Budget-viable only part-time (~5 hrs/day at $250/mo).
3. **Avoid:** L4 (`g6.xlarge`) unless A10G spot is unavailable — poor spot discount. A100 (`p4d`/`p4de`) — 8-GPU only, ~$5k+/mo. 2-GPU TP for ≤30B — a single larger card is cheaper.

### Assumptions / caveats flagged

- Spot $/hr are **third-party tracker snapshots** (Vantage/DoiT), not live AWS spot history; verify per-region/AZ with `aws ec2 describe-spot-price-history`.
- Quantized weight sizes (FP8 ≈ 0.5×, AWQ 4-bit ≈ 0.35× of BF16) and the "×0.9 utilization − 1 GiB overhead" model are engineering estimates, not measured.
- Concurrency numbers are worst-case (every slot full at 4k); live throughput is usually higher.
- Llama-2-13B config values are from secondary sources (official config.json is gated).
- Region/AZ availability and interruption rates were **not** verified against a primary AWS source.

---

## Sources

- Amazon EC2 G6 (L4) — https://aws.amazon.com/ec2/instance-types/g6/
- Amazon EC2 G6e (L40S), sizes & specs — https://aws.amazon.com/ec2/instance-types/g6e/
- Amazon EC2 G6e GA announcement — https://aws.amazon.com/about-aws/whats-new/2024/08/amazon-ec2-g6e-instances/
- Amazon EC2 G6e additional regions — https://aws.amazon.com/about-aws/whats-new/2024/11/amazon-ec2-g6e-instances-additional-regions/
- Amazon EC2 G6e Seoul region — https://aws.amazon.com/about-aws/whats-new/2025/03/amazon-ec2-g6e-instances-seoul-region
- AWS DLAMI recommended GPU instances — https://docs.aws.amazon.com/dlami/latest/devguide/gpu.html
- EC2 On-Demand pricing — https://aws.amazon.com/ec2/pricing/on-demand/
- EC2 Spot pricing — https://aws.amazon.com/ec2/spot/pricing/
- SpotPrice API (describe-spot-price-history) — https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_SpotPrice.html
- g5.xlarge price/spec (Vantage) — https://instances.vantage.sh/aws/ec2/g5.xlarge
- g6.xlarge price/spec (Vantage) — https://instances.vantage.sh/aws/ec2/g6.xlarge
- g6e.xlarge price/spec (Vantage) — https://instances.vantage.sh/aws/ec2/g6e.xlarge
- g5.xlarge spot (DoiT) — https://compute.doit.com/spot/us-east-1/g5.xlarge
- p4d.24xlarge price/spec (Vantage) — https://instances.vantage.sh/aws/ec2/p4d.24xlarge
- p4de.24xlarge price/spec (Vantage) — https://instances.vantage.sh/aws/ec2/p4de.24xlarge
- Llama config (n_layers/heads/kv_heads/head_dim), HF transformers — https://github.com/huggingface/transformers/blob/main/src/transformers/models/llama/configuration_llama.py
- Qwen2.5-32B config.json — https://huggingface.co/Qwen/Qwen2.5-32B/blob/main/config.json
- Qwen2 model doc (GQA) — https://huggingface.co/docs/transformers/en/model_doc/qwen2
- Llama-2 model doc — https://huggingface.co/docs/transformers/main/en/model_doc/llama2
- vLLM Quantized KV Cache (FP8) — https://docs.vllm.ai/en/latest/features/quantization/quantized_kvcache/
- vLLM blog: state of FP8 KV-cache & attention quantization (2026-04-22) — https://vllm-project.github.io/2026/04/22/fp8-kvcache.html
