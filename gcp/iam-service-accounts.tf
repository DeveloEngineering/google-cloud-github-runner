# https://github.com/GoogleCloudPlatform/cloud-foundation-fabric/blob/v53.0.0/modules/iam-service-account/README.md

# Service Account for GitHub Actions Runners (Compute Engine VMs)
module "service-account-compute-vm-github-runners" {
  source       = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v53.0.0"
  project_id   = module.project.project_id
  name         = "github-runners"
  display_name = "Compute VM - GitHub Actions Runners (Terraform managed)"
  iam = {
    "roles/iam.serviceAccountUser" = [
      module.service-account-cloud-run-github-runners-manager.iam_email
    ]
  }
  iam_project_roles = {
    (module.project.project_id) = [
      "roles/logging.logWriter",
      "roles/monitoring.metricWriter",
    ]
  }
}

# Wait for service account to be fully propagated in Google Cloud IAM
resource "time_sleep" "wait_for_service_account_compute_vm" {
  depends_on = [
    module.service-account-compute-vm-github-runners
  ]
  create_duration = "30s"
}

# Service Account for the Runners Manager (Cloud Run)
module "service-account-cloud-run-github-runners-manager" {
  source       = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v53.0.0"
  project_id   = module.project.project_id
  name         = "github-runners-manager"
  display_name = "Cloud Run - GitHub Actions Runners manager (Terraform managed)"
  iam_project_roles = {
    (module.project.project_id) = [
      "roles/compute.admin",
      "roles/logging.logWriter",
      "roles/monitoring.metricWriter",
      # Lets the webhook handler enqueue tasks onto the Cloud Tasks queue.
      "roles/cloudtasks.enqueuer",
      # Lets the webhook handler attach an OIDC token (signed by the tasks
      # invoker SA) to enqueued tasks. The actAs binding is granted on the
      # invoker SA below.
    ]
  }
}

# Service Account that Cloud Tasks impersonates when delivering tasks back to
# the Cloud Run service. The /internal/process-workflow-job route verifies
# the OIDC token's email claim against this SA.
module "service-account-cloud-tasks-invoker" {
  source       = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v53.0.0"
  project_id   = module.project.project_id
  name         = "github-runners-tasks-invoker"
  display_name = "Cloud Tasks - Webhook task dispatcher (Terraform managed)"
  # The runners-manager SA must be able to "actAs" this SA in order to attach
  # OIDC tokens signed by it to enqueued tasks.
  iam = {
    "roles/iam.serviceAccountUser" = [
      module.service-account-cloud-run-github-runners-manager.iam_email
    ]
  }
}

# Wait for service account to be fully propagated in Google Cloud IAM
resource "time_sleep" "wait_for_service_account_cloud_tasks_invoker" {
  depends_on = [
    module.service-account-cloud-tasks-invoker
  ]
  create_duration = "30s"
}

# Wait for service account to be fully propagated in Google Cloud IAM
resource "time_sleep" "wait_for_service_account_cloud_run" {
  depends_on = [
    module.service-account-cloud-run-github-runners-manager
  ]
  create_duration = "30s"
}

# Service Account for Cloud Scheduler to invoke periodic background jobs
# (orphan-runner sweeper at /sweep, reconciler at /reconcile). This SA does
# not need any project roles: the Cloud Run service authorizes requests at
# the app layer by verifying the OIDC token's email claim against
# INTERNAL_OIDC_SERVICE_ACCOUNT_EMAIL.
module "service-account-cloud-scheduler-sweeper" {
  source       = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v53.0.0"
  project_id   = module.project.project_id
  name         = "github-runners-sweeper"
  display_name = "Cloud Scheduler - Periodic background jobs (Terraform managed)"
}

# Wait for service account to be fully propagated in Google Cloud IAM
resource "time_sleep" "wait_for_service_account_cloud_scheduler" {
  depends_on = [
    module.service-account-cloud-scheduler-sweeper
  ]
  create_duration = "30s"
}

# Service Account for Cloud Build (Image Creation)
module "service-account-cloud-build-github-runners" {
  source       = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v53.0.0"
  project_id   = module.project.project_id
  name         = "cloud-build-github-runners"
  display_name = "Cloud Build - Create images (Terraform managed)"
  iam_project_roles = {
    (module.project.project_id) = [
      "roles/logging.logWriter",
    ]
  }
}

# Wait for service account to be fully propagated in Google Cloud IAM
resource "time_sleep" "wait_for_service_account_cloud_build" {
  depends_on = [
    module.service-account-cloud-build-github-runners
  ]
  create_duration = "30s"
}
