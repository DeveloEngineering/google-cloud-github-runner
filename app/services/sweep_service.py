"""
Service for deleting orphan GitHub Actions runner VMs.

A runner VM is considered "orphan" when it is older than the configured age
threshold. Under normal operation, `workflow_job.completed` webhooks trigger
deletion within minutes of a job finishing. When that webhook is missed,
dropped, or fails to delete the VM, the sweeper is the safety net.

The Compute Engine instance template also sets `max_run_duration` as a
hard backstop, so even if the sweeper itself fails, GCE will eventually
delete the VM.
"""
import logging
import os

from app.clients import GCloudClient

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_SECONDS = 7200  # 2 hours


class SweepService:
    """Deletes orphan gcp-runner-* GCE instances."""

    def __init__(self):
        self.gcloud_client = GCloudClient()
        try:
            self.max_age_seconds = int(
                os.environ.get('GITHUB_RUNNERS_ORPHAN_MAX_AGE_SECONDS', DEFAULT_MAX_AGE_SECONDS)
            )
        except ValueError:
            logger.warning(
                "Invalid GITHUB_RUNNERS_ORPHAN_MAX_AGE_SECONDS; using default %d",
                DEFAULT_MAX_AGE_SECONDS,
            )
            self.max_age_seconds = DEFAULT_MAX_AGE_SECONDS

    def sweep(self):
        """Delete every runner instance older than max_age_seconds.

        Returns:
            dict: summary with keys `inspected`, `deleted`, `skipped`, `errors`,
                  and `deleted_names` (list of instance names).
        """
        inspected = 0
        deleted_names = []
        skipped = 0
        errors = 0

        for instance in self.gcloud_client.list_runner_instances():
            inspected += 1
            age = GCloudClient.instance_age_seconds(instance)
            if age is None:
                skipped += 1
                continue
            if age < self.max_age_seconds:
                skipped += 1
                continue

            try:
                self.gcloud_client.delete_runner_instance(instance.name)
                deleted_names.append(instance.name)
                logger.info(
                    "Swept orphan runner %s (age=%ds, threshold=%ds)",
                    instance.name,
                    int(age),
                    self.max_age_seconds,
                )
            except Exception as e:
                errors += 1
                logger.error("Failed to delete orphan runner %s: %s", instance.name, e)

        result = {
            'inspected': inspected,
            'deleted': len(deleted_names),
            'skipped': skipped,
            'errors': errors,
            'deleted_names': deleted_names,
            'max_age_seconds': self.max_age_seconds,
        }
        logger.info("Sweep complete: %s", result)
        return result
