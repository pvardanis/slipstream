# Plan-level tests for the bootstrap outputs `just gh-vars` reads to set the
# sweep-dispatch repo variables. They run offline (mocked aws provider) and assert the
# cluster name and Prefect namespace come from bootstrap, so the variables can be set
# before any cluster exists.
mock_provider "aws" {}
mock_provider "random" {}

run "sweep_dispatch_target_outputs" {
  command = plan

  variables {
    cluster_name      = "bench-cluster"
    prefect_namespace = "orchestration"
  }

  assert {
    condition     = output.cluster_name == "bench-cluster"
    error_message = "The cluster_name output must carry the cluster_name variable."
  }
  assert {
    condition     = output.prefect_namespace == "orchestration"
    error_message = "The prefect_namespace output must carry the prefect_namespace variable."
  }
}
