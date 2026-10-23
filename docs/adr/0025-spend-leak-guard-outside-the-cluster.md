<!-- ADR recording why the spend-leak guard lives in the bootstrap stack, outside the cluster it watches, as two AWS-native layers feeding one SNS email topic: a CloudWatch alarm on the account's running on-demand G-class vCPUs held above zero for 20 hours (the fast GPU layer), and the existing slipstream-monthly AWS Budget imported into Terraform (the slow catch-all layer). Records why an in-cluster alert, Cost Anomaly Detection, and a $/hr threshold were rejected, why the window is 20 hours, and the spot gap the GPU layer leaves. Supersedes spec §5's "$/hr over the duty-cycle budget" cost-drift wording (issue #35). -->

# ADR-0025: The spend-leak guard lives outside the cluster, as a GPU alarm plus a monthly budget

- Status: Accepted
- Date: 2026-10-23

## Context

The platform runs on a duty cycle: `just cluster-down` destroys the whole `terraform/eks`
stack, and only `terraform/bootstrap` (state bucket, ECR, OIDC) survives it. The one
real-money failure mode is a **spend leak**: spend accruing while no session is running,
most expensively a GPU left up after a failed teardown.

`just stack-down` already runs a zero-leak sweep for `Project=slipstream` instances and
volumes still billing. That sweep only runs when `stack-down` runs, and only reports to the
terminal that ran it. It cannot catch a teardown that never ran, or a failure nobody read.

Spend does not stop at the GPU. The EKS control plane, the NAT gateway, the bench
endpoint's EC2 host and ALB (a separate `terraform/bench-endpoint` stack) all bill too, and
nothing inside the cluster can see the bench endpoint.

A monthly AWS Budget, `slipstream-monthly`, already existed: $200, emailing at 50%, 75%
and 90% of actual spend and at 100% of forecast. It had been made in the console, outside
Terraform.

## Decision

Both layers live in `terraform/bootstrap` and feed one SNS topic that emails the operator.

### Fast layer: a CloudWatch alarm on running GPU vCPUs

The alarm watches `AWS/Usage` `ResourceCount` with `Service=EC2`, `Type=Resource`,
`Resource=vCPU`, `Class=G/OnDemand`, the account's running on-demand G-family vCPUs,
published at one-minute resolution. It fires when the `Maximum` stays above zero for
**20 hours**.

The window sits above the longest legitimate GPU session, an 18-hour benchmark run. A
forgotten GPU therefore burns at most about 20 GPU-hours, roughly $22 at the rig's
`g5.xlarge` on-demand price in `eu-west-1` ($1.123/hr), before the page.

### Slow layer: the monthly budget, imported

`slipstream-monthly` moves into `bootstrap` through a Terraform `import` block, with its
limit and thresholds unchanged, so it keeps its history. It covers all account spend,
including the NAT, the ALBs and the bench host, which the GPU alarm cannot see. AWS Budgets
refreshes up to three times a day, 8–12 hours apart, so this layer is late by design.

### Proof

A `terraform test` asserts the alarm's namespace, dimensions, 20-hour window and SNS
target. One `aws cloudwatch set-alarm-state --state-value ALARM` rehearsal proves alarm →
SNS → inbox end to end, and is recorded as the first runbook entry. Leaving a GPU up for 20
hours to watch the real alarm fire would cost about $22 and prove nothing the rehearsal
does not.

## Alternatives considered

- **An alert in in-cluster Prometheus and Alertmanager.** It dies with the cluster, which
  is the failure it exists to catch, and it cannot see the bench endpoint.
- **AWS Cost Anomaly Detection.** It reads Cost Explorer data, which can take up to 24
  hours to detect an anomaly, and needs 10 days of history per service. A duty-cycle
  account spikes on purpose, so its anomalies would be noise.
- **A threshold on $/hr.** A forgotten GPU burns at the same rate as one in use. Only how
  long the GPU stays up tells them apart.
- **A 12-hour window.** It falls below the 18-hour benchmark run and would page on
  legitimate work.
- **Suppressing the alarm during declared sweeps.** A tighter window, but extra moving
  parts that can themselves fail open, to save about $9 of worst-case leak.
- **Phone paging (ntfy, PagerDuty).** A GPU leak costs about $1.12 an hour, not an outage, so a
  few hours to notice by email is acceptable.

## Consequences

- **The spot gap.** `Class=G/OnDemand` counts on-demand capacity only, since the documented
  usage classes are all `*/OnDemand`. The GPU node pool is on-demand only
  (`k8s/gpu-node-pool.yaml`), so the alarm covers it completely. If L2b allows spot, a spot
  GPU leak reaches only the slow budget layer until the fast layer is extended.
- Non-GPU leaks (NAT, ALBs, the bench host, the control plane) page only through the
  budget, hours after they start.
- A GPU session longer than 20 hours pages falsely. The window rises with the longest
  legitimate run.
- Spec §5's "cost-drift alert: $/hr over the duty-cycle budget" is superseded by the spend
  leak and its two layers.
