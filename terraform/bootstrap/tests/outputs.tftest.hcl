# Plan-level tests for the bootstrap outputs `just gh-vars` reads to set the
# sweep-dispatch repo variables. They run offline (mocked aws provider) and assert the
# default cluster name and Prefect namespace match the eks stack's (var.cluster_name and
# local.prefect_namespace), so the variables can be set before any cluster exists and
# point at the cluster and namespace eks creates.
mock_provider "aws" {}
mock_provider "random" {}

run "sweep_dispatch_target_outputs" {
  command = plan

  assert {
    condition     = output.cluster_name == "slipstream"
    error_message = "The cluster_name output must default to slipstream, the eks stack's cluster_name default."
  }
  assert {
    condition     = output.prefect_namespace == "prefect"
    error_message = "The prefect_namespace output must default to prefect, the namespace the eks stack runs the Prefect server in."
  }
}
