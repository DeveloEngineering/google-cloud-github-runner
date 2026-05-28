# Cloud Scheduler job that periodically POSTs to the runners manager's /sweep
# endpoint. The handler lists `gcp-runner-*` GCE instances older than the
# configured threshold and deletes them. This is the safety net for cases
# where a workflow_job.completed webhook is dropped or fails to delete the VM.
#
# Authentication: Cloud Scheduler attaches an OIDC token signed by its service
# account. The Flask app verifies the token signature with Google's public keys
# and checks that the `email` claim matches SWEEP_OIDC_SERVICE_ACCOUNT_EMAIL.
# Cloud Run public ingress is preserved (invoker_iam_disabled = true) so that
# GitHub's webhook POSTs continue to work without OIDC.
resource "google_cloud_scheduler_job" "github_runners_orphan_sweeper" {
  project     = module.project.project_id
  region      = var.region
  name        = "github-runners-orphan-sweeper-${local.region_shortnames[var.region]}"
  description = "Deletes orphan gcp-runner-* GCE instances older than github_runners_orphan_max_age_seconds"
  schedule    = var.github_runners_orphan_sweep_schedule
  time_zone   = "Etc/UTC"

  attempt_deadline = "60s"

  retry_config {
    retry_count          = 2
    min_backoff_duration = "30s"
    max_backoff_duration = "120s"
  }

  http_target {
    http_method = "POST"
    uri         = "${module.cloud_run_github_runners_manager.service_uri}/sweep"

    oidc_token {
      service_account_email = module.service-account-cloud-scheduler-sweeper.email
      audience              = module.cloud_run_github_runners_manager.service_uri
    }
  }

  depends_on = [
    time_sleep.wait_for_service_account_cloud_scheduler
  ]
}
