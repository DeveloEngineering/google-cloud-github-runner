# Cloud Tasks queue for asynchronous webhook processing.
#
# The webhook handler at /webhook (called by GitHub) verifies the signature
# and enqueues the payload here, then returns 200 in <100 ms. Cloud Tasks
# then dispatches the task to /internal/process-workflow-job at its own pace,
# with retries on failure. This decouples GitHub's 10-second webhook timeout
# from how long the actual GitHub-API + GCE-insert work takes.
#
# Rate limits sized to absorb the largest plausible CI fan-out from
# develo-emr (~30 jobs/run × ~20 concurrent runs = ~600 simultaneous tasks).
resource "google_cloud_tasks_queue" "workflow_job_queue" {
  project  = module.project.project_id
  name     = "workflow-job-${local.region_shortnames[var.region]}"
  location = var.region

  rate_limits {
    # We have plenty of Cloud Run capacity (max 30 instances × 8 concurrency
    # = 240 slots) but cap dispatch rate so we don't overwhelm the GCE
    # instances.insert API quota.
    max_dispatches_per_second = 200
    max_concurrent_dispatches = 200
  }

  retry_config {
    # Tasks that fail to dispatch (e.g. transient 5xx from Cloud Run) get
    # retried with exponential backoff. Five attempts over ~5 minutes covers
    # any reasonable transient failure window.
    max_attempts       = 5
    min_backoff        = "1s"
    max_backoff        = "60s"
    max_doublings      = 4
    max_retry_duration = "300s"
  }
}
