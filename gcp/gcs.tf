# https://github.com/GoogleCloudPlatform/cloud-foundation-fabric/blob/v53.0.0/modules/gcs/README.md

# GCS bucket for storing Terraform state
module "gcs-github-runners-iac" {
  source        = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v53.0.0"
  project_id    = module.project.project_id
  prefix        = module.project.project_id
  name          = "gh-iac-${local.region_shortnames[var.region]}"
  location      = var.region
  versioning    = true
  force_destroy = true
}

# GCS bucket for Cloud Build source staging
module "gcs-github-runners-cloud-build" {
  source        = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v53.0.0"
  project_id    = module.project.project_id
  prefix        = module.project.project_id
  name          = "build-${local.region_shortnames[var.region]}"
  location      = var.region
  versioning    = false
  force_destroy = true
  lifecycle_rules = {
    lr-0 = {
      action = {
        type = "Delete"
      }
      condition = {
        age        = 2
        with_state = "ANY"
      }
    }
  }
  iam = {
    "roles/storage.objectAdmin" = [
      module.service-account-cloud-build-github-runners.iam_email
    ]
  }
  depends_on = [
    time_sleep.wait_for_service_account_cloud_build
  ]
}

# GCS bucket for storing the VM startup script
module "gcs-github-runners-startup-script" {
  source        = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v53.0.0"
  project_id    = module.project.project_id
  prefix        = module.project.project_id
  name          = "gh-start-${local.region_shortnames[var.region]}"
  location      = var.region
  versioning    = false
  force_destroy = true
  iam = {
    "roles/storage.objectViewer" = [
      module.service-account-compute-vm-github-runners.iam_email
    ]
  }
  depends_on = [
    time_sleep.wait_for_service_account_compute_vm
  ]
}

# GCS bucket for sharing the CI workspace tarball (node_modules + build outputs)
# between the develo-emr CI jobs. Runner VMs reach this regional bucket over
# Private Google Access (free, in-region), replacing the GitHub Actions artifact
# download that every downstream job (~40/run) otherwise pulled back through
# Cloud NAT at $0.045/GB. Objects are run/attempt-scoped and short-lived, so a
# 2-day lifecycle delete keeps the bucket from accumulating. The runner VM SA
# gets objectAdmin (read for restore, write for prepare-workspace).
module "gcs-github-runners-ci-workspace" {
  source        = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v53.0.0"
  project_id    = module.project.project_id
  prefix        = module.project.project_id
  name          = "ci-ws-${local.region_shortnames[var.region]}"
  location      = var.region
  versioning    = false
  force_destroy = true
  lifecycle_rules = {
    lr-0 = {
      action = {
        type = "Delete"
      }
      condition = {
        age        = 2
        with_state = "ANY"
      }
    }
  }
  iam = {
    "roles/storage.objectAdmin" = [
      module.service-account-compute-vm-github-runners.iam_email
    ]
  }
  depends_on = [
    time_sleep.wait_for_service_account_compute_vm
  ]
}
