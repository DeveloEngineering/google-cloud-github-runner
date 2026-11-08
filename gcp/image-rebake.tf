# Periodic runner-image rebake
#
# Why this exists
# ---------------
# startup/install.sh installs "whatever actions/runner release is latest at
# bake time", so a VM image's runner version is frozen the moment it is baked.
# GitHub hard-deprecates old runner versions: a runner that is too old still
# registers successfully and then exits without accepting work, which reads as
# a healthy control plane with every job stuck in `queued`.
#
# On 2026-08-10 that took CI down for ~3 hours. The arm64 image had been baked
# 2026-05-28 with runner 2.334.0; GitHub deprecated that version and every
# runner registered, died, and was recreated by the reconciler in a loop. The
# amd64 image had been rebaked on 2026-06-18 but the arm64 one — the family
# every develo-emr job actually uses — had not, purely because rebaking was a
# manual step nobody owned.
#
# Runner self-update (see app/clients/gcloud_client.py) is the safety net that
# stops a stale image being fatal. This is the other half: keep the baked
# version close to current so that path is rarely exercised, and keep the
# pre-baked Node / yarn / Playwright / Docker layer caches fresh.
#
# How it works
# ------------
# Terraform already generates a build-image-<family>.sh per image family. Those
# scripts are uploaded to GCS here, and a Cloud Run job pulls and runs them in
# sequence on a Cloud Scheduler cron. Reusing the exact same scripts an
# operator runs by hand means there is only one bake path to reason about.
#
# The bake is safe to run unattended because build-image.template.sh verifies
# the `develo/bake-status` guest attribute before promoting a disk to the image
# family. A failed bake leaves the previous good image in place and the job
# exits non-zero — it can never publish a broken image.

# Upload the generated per-family build scripts so the Cloud Run job can fetch
# them. Keyed by image family, matching local_file.github-runners-images.
resource "google_storage_bucket_object" "github-runners-image-build-scripts" {
  for_each = local_file.github-runners-images

  bucket  = module.gcs-github-runners-startup-script.name
  name    = "build-image/${basename(each.value.filename)}"
  content = each.value.content
}

# Service account for the rebake job. It creates/deletes builder VMs and
# publishes images, so it needs compute.admin — the same surface an operator
# running build-image-*.sh by hand already has.
module "service-account-image-rebake" {
  source       = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v53.0.0"
  project_id   = module.project.project_id
  name         = "github-runners-rebake"
  display_name = "Cloud Run Job - Periodic runner image rebake (Terraform managed)"
  iam_project_roles = {
    (module.project.project_id) = [
      "roles/compute.admin",
      "roles/logging.logWriter",
      "roles/monitoring.metricWriter",
    ]
  }
}

# Wait for service account to be fully propagated in Google Cloud IAM
resource "time_sleep" "wait_for_service_account_image_rebake" {
  depends_on = [
    module.service-account-image-rebake
  ]
  create_duration = "30s"
}

# The two IAM grants this job needs are attached to the resources they belong
# to, because the cloud-foundation-fabric modules manage IAM authoritatively
# and a separate *_iam_member for the same role would fight them on every
# apply:
#   - serviceAccountUser on the compute-vm runner SA (the builder VM runs as
#     that SA) — see iam-service-accounts.tf
#   - objectViewer on the startup-script bucket (install.sh + the build
#     scripts) — see gcs.tf

# Cloud Run job that rebakes every runner image family, one after another.
# https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/cloud_run_v2_job
resource "google_cloud_run_v2_job" "github_runners_image_rebake" {
  project             = module.project.project_id
  name                = "github-runners-image-rebake-${local.region_shortnames[var.region]}"
  location            = var.region
  deletion_protection = false

  template {
    task_count = 1
    template {
      service_account = module.service-account-image-rebake.email
      # Bakes run sequentially and each takes ~7-10 minutes. Allow for every
      # family plus headroom; the per-bake backstop is
      # github_runners_image_bake_timeout_seconds inside the scripts.
      timeout = "${var.github_runners_image_bake_timeout_seconds * length(local_file.github-runners-images) + 600}s"
      # A rebake is not idempotent-cheap (each attempt burns a builder VM) and
      # a genuine failure needs a human, not a retry.
      max_retries = 0

      containers {
        image = "gcr.io/google.com/cloudsdktool/cloud-sdk:slim"
        resources {
          limits = {
            cpu    = "1000m"
            memory = "512Mi"
          }
        }
        env {
          name  = "SCRIPT_BUCKET"
          value = module.gcs-github-runners-startup-script.name
        }
        env {
          name = "SCRIPT_NAMES"
          value = join(" ", [
            for o in google_storage_bucket_object.github-runners-image-build-scripts : basename(o.name)
          ])
        }
        command = ["/bin/bash", "-c"]
        args = [
          <<-EOT
          set -euo pipefail
          echo "Rebaking runner images: $SCRIPT_NAMES"
          workdir=$(mktemp -d)
          cd "$workdir"
          failed=""
          for script in $SCRIPT_NAMES; do
            echo "══════════════════════════════════════════════════════════"
            echo "▶ $script"
            echo "══════════════════════════════════════════════════════════"
            gcloud storage cp "gs://$SCRIPT_BUCKET/build-image/$script" "./$script"
            chmod +x "./$script"
            # Keep going if one family fails so a single bad bake does not
            # leave every other family stale; surface it at the end.
            if ! "./$script"; then
              echo "✗ $script FAILED" >&2
              failed="$failed $script"
            fi
          done
          if [ -n "$failed" ]; then
            echo "Rebake finished with failures:$failed" >&2
            exit 1
          fi
          echo "✓ All runner images rebaked successfully"
          EOT
        ]
      }
    }
  }

  depends_on = [
    time_sleep.wait_for_service_account_image_rebake,
    google_storage_bucket_object.github-runners-image-build-scripts,
  ]
}

# Cloud Scheduler triggers the rebake job via the Cloud Run Admin API.
resource "google_cloud_scheduler_job" "github_runners_image_rebake" {
  count = var.github_runners_image_rebake_enabled ? 1 : 0

  project     = module.project.project_id
  region      = var.region
  name        = "github-runners-image-rebake-${local.region_shortnames[var.region]}"
  description = "Rebakes runner VM images so the baked actions/runner version does not go stale"
  schedule    = var.github_runners_image_rebake_schedule
  time_zone   = "Etc/UTC"

  # Only the :run call has to land inside this deadline; the job itself then
  # runs asynchronously for as long as its own timeout allows.
  attempt_deadline = "60s"

  retry_config {
    retry_count          = 1
    min_backoff_duration = "60s"
    max_backoff_duration = "300s"
  }

  http_target {
    http_method = "POST"
    uri = join("", [
      "https://run.googleapis.com/v2/projects/${module.project.project_id}",
      "/locations/${var.region}/jobs/${google_cloud_run_v2_job.github_runners_image_rebake.name}:run",
    ])

    oauth_token {
      service_account_email = module.service-account-cloud-scheduler-sweeper.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    time_sleep.wait_for_service_account_cloud_scheduler
  ]
}

# Let the scheduler service account start executions of the rebake job.
resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_image_rebake" {
  project  = module.project.project_id
  location = var.region
  name     = google_cloud_run_v2_job.github_runners_image_rebake.name
  role     = "roles/run.invoker"
  member   = module.service-account-cloud-scheduler-sweeper.iam_email
}
