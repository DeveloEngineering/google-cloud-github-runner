# Get the container image from Artifact Registry
# https://registry.terraform.io/providers/hashicorp/google/latest/docs/data-sources/artifact_registry_docker_image
data "google_artifact_registry_docker_image" "container-image-github-runners-manager" {
  project       = module.project.project_id
  location      = var.region
  repository_id = module.artifact-registry-container.name
  image_name    = "app:latest" # Defined in cloudbuild-container.template.yaml
  depends_on = [
    null_resource.build-github-runners-manager-container
  ]
}

# Deploy the GitHub Actions Runners manager service on Cloud Run
# https://github.com/GoogleCloudPlatform/cloud-foundation-fabric/blob/v53.0.0/modules/cloud-run-v2/README.md
module "cloud_run_github_runners_manager" {
  source     = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/cloud-run-v2?ref=v53.0.0"
  project_id = module.project.project_id
  name       = "github-runners-manager-${local.region_shortnames[var.region]}"
  type       = "SERVICE"
  region     = var.region
  containers = {
    github-runners-manager = {
      image = data.google_artifact_registry_docker_image.container-image-github-runners-manager.self_link
      resources = {
        limits = {
          # 2 vCPU + 1Gi keeps per-request CPU time short under burst —
          # webhook handler does sequential GitHub-API + GCE-API calls, both
          # are CPU-bound on JSON+TLS work. Bumping from 1000m fixed a p99
          # tail that was crossing GitHub's 10s timeout under fan-out bursts.
          cpu    = "2000m"
          memory = "1Gi"
        }
        startup_cpu_boost = false # We do not scale to zero.
      }
      env = {
        GOOGLE_CLOUD_PROJECT                  = var.project_id
        GOOGLE_CLOUD_ZONE                     = "${var.region}-${var.zone}"
        GITHUB_RUNNER_GROUP                   = var.github_runner_group
        GITHUB_RUNNERS_ORPHAN_MAX_AGE_SECONDS = tostring(var.github_runners_orphan_max_age_seconds)
        # Service account email Cloud Scheduler uses to OIDC-sign /sweep and
        # /reconcile invocations. The app verifies the token's email claim
        # against this value.
        SWEEP_OIDC_SERVICE_ACCOUNT_EMAIL = module.service-account-cloud-scheduler-sweeper.email
        # Service account email Cloud Tasks uses to OIDC-sign deliveries to
        # /internal/process-workflow-job. Distinct from the sweeper SA so
        # Cloud Run can grant actAs separately to each.
        INTERNAL_OIDC_SERVICE_ACCOUNT_EMAIL = module.service-account-cloud-tasks-invoker.email
        # Cloud Tasks config — webhook handler enqueues work here and the
        # /internal/process-workflow-job route consumes it. See cloud-tasks.tf.
        TASKS_QUEUE_PROJECT                 = var.project_id
        TASKS_QUEUE_LOCATION                = var.region
        TASKS_QUEUE_NAME                    = google_cloud_tasks_queue.workflow_job_queue.name
        TASKS_INVOKER_SERVICE_ACCOUNT_EMAIL = module.service-account-cloud-tasks-invoker.email
        # NOTE: Task target URL is NOT passed as an env var — that would
        # create a circular dependency (the env block of cloud_run referencing
        # cloud_run's own service_uri output). The handler reads its own
        # service URL from the incoming request's Host header at runtime.
        # Reconciler config — minimum age before reconciler will act on a
        # stuck job. Protects against racing in-flight webhook deliveries.
        GITHUB_RECONCILER_MIN_JOB_AGE_SECONDS = tostring(var.github_runners_reconciler_min_job_age_seconds)
      }
      env_from_key = {
        GITHUB_APP_ID = {
          # Secret may only be {secret} or
          # projects/{project}/secrets/{secret},
          # and secret may only have alphanumeric characters, hyphens, and underscores.
          # Secret must be a global secret not a regional secret!
          secret  = module.secret-manager.ids["github-app-id"]
          version = "latest"
        }
        GITHUB_INSTALLATION_ID = {
          secret  = module.secret-manager.ids["github-installation-id"]
          version = "latest"
        }
        GITHUB_PRIVATE_KEY = {
          secret  = module.secret-manager.ids["github-private-key"]
          version = "latest"
        }
        GITHUB_WEBHOOK_SECRET = {
          secret  = module.secret-manager.ids["github-webhook-secret"]
          version = "latest"
        }
      }
    }
  }
  service_config = {
    # Disable IAM permission check
    # There should be no requirement to pass the roles/run.invoker to the IAM block to enable public access.
    # This allows for the org policy domain restricted sharing org policy remain enabled.
    invoker_iam_disabled = true
    # Second generation Cloud Run for faster CPU.
    # The first generation with faster cold starts is still too slow for our webhook.
    gen2_execution_environment = true
    # Cap concurrent requests per instance so Cloud Run scales out under burst
    # webhook traffic instead of queueing requests behind a single instance's
    # gunicorn thread pool.
    max_concurrency = var.github_runners_manager_max_concurrency
    scaling = {
      min_instance_count = var.github_runners_manager_min_instance_count # Min. 1, we do not scale to zero.
      max_instance_count = var.github_runners_manager_max_instance_count
    }
  }
  service_account_config = {
    create = false
    email  = module.service-account-cloud-run-github-runners-manager.email
  }
  deletion_protection = false
  depends_on = [
    google_secret_manager_secret_version.secret-version-default,
    time_sleep.wait_for_service_account_cloud_run,
    time_sleep.wait_for_service_account_cloud_scheduler,
    time_sleep.wait_for_service_account_cloud_tasks_invoker,
    google_cloud_tasks_queue.workflow_job_queue,
  ]
}
