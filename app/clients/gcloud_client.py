"""
Google Cloud Client for managing GCE instances.
"""
import logging
import os
import re
import uuid
import shlex
from datetime import datetime, timezone
import google.cloud.compute_v1 as compute_v1

logger = logging.getLogger(__name__)

# Labels longer than 63 chars or with characters outside [a-z0-9_-] are
# rejected by the Compute Engine API.
_LABEL_VALUE_RE = re.compile(r'[^a-z0-9_-]')


def _sanitize_label_value(value):
    """Coerce an arbitrary string into a valid GCE label value."""
    if value is None:
        return ''
    lowered = str(value).lower()
    return _LABEL_VALUE_RE.sub('-', lowered)[:63]


class GCloudClient:
    """Client for interacting with Google Cloud Compute Engine API."""

    def __init__(self):
        """Initialize GCloudClient with project and zone configuration."""
        self.project_id = os.environ.get('GOOGLE_CLOUD_PROJECT')
        self.zone = os.environ.get('GOOGLE_CLOUD_ZONE', 'us-central1-a')
        self.github_runner_group = os.environ.get('GITHUB_RUNNER_GROUP', '').strip()
        self.region = '-'.join(self.zone.split('-')[:-1])
        # When true, runners register with --ephemeral and are deleted after one
        # job. When false (default), runners stay registered and serve multiple
        # jobs; the sweeper reaps idle ones. Reusing runners avoids paying
        # cold-boot per job and dramatically cuts VM create churn under bursts.
        self.ephemeral = os.environ.get('RUNNER_EPHEMERAL', 'false').strip().lower() == 'true'

        if not self.project_id:
            logger.warning("GOOGLE_CLOUD_PROJECT not set. GCloudClient will not work correctly.")

        # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.instances.InstancesClient
        self.instance_client = compute_v1.InstancesClient()
        # Create a RegionInstanceTemplatesClient for retrieving templates in a specific region
        # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.region_instance_templates
        self.instance_templates_client = compute_v1.RegionInstanceTemplatesClient()

    def _get_template_name(self, template_name):
        """
        Find a matching instance template by name prefix.

        Args:
            template_name (str): The name prefix to search for.

        Returns:
            google.cloud.compute_v1.InstanceTemplate or None: The matching template resource.
        """
        # Replace dots with dashes for template name, so gcp-ubuntu-24.04 matches gcp-ubuntu-24-04
        prefix = template_name.replace('.', '-')
        # logger.info(f"Prefix: {prefix}")
        # Create regex pattern: prefix followed by dash, at least 12 digits, and optional alphanumeric characters
        pattern = re.compile(f"^{re.escape(prefix)}-\\d{{14,}}[a-z0-9]*$")
        try:
            # List all templates to find one that matches the pattern
            for template in self.instance_templates_client.list(project=self.project_id, region=self.region):
                # logger.info(f"Template: {template.name}")
                if pattern.match(template.name):
                    return template
            return None
        except Exception:
            return None

    def create_runner_instance(
        self,
        registration_token,
        repo_url,
        template_name,
        instance_label=None,
        delivery_id=None,
        job_id=None,
    ):
        """
        Create a new GCE instance for a GitHub Actions runner.

        Args:
            registration_token (str): The GitHub Actions runner registration token.
            repo_url (str): The URL of the repository or organization.
            template_name (str): The name of the instance template to use.
            instance_label (str): Label to add to the Instance for Cost Tracking.
            delivery_id (str): The GitHub webhook delivery ID for log correlation.

        Returns:
            str: The name of the created instance.
        """
        instance_template_resource = self._get_template_name(template_name)
        if instance_template_resource:
            logger.info(
                "Found matching instance template: %s, delivery_id: %s",
                instance_template_resource.name,
                delivery_id,
            )
        else:
            logger.warning(
                "No matching instance template found for label '%s' in region %s. "
                "Skipping instance creation. delivery_id: %s",
                template_name,
                self.region,
                delivery_id,
            )
            return None

        # Name must start with a lowercase letter followed by up to 62 lowercase letters,
        # numbers, or hyphens, and cannot end with a hyphen.
        instance_uuid = uuid.uuid4().hex[:16]
        if instance_template_resource.name.startswith("dependabot"):
            instance_name = f"gcp-runner-dependabot-{instance_uuid}"
        else:
            instance_name = f"gcp-runner-{instance_uuid}"

        logger.info(
            "Creating GCE instance %s with template %s, delivery_id: %s",
            instance_name,
            instance_template_resource.self_link,
            delivery_id,
        )

        # Set instance name
        instance_resource = compute_v1.Instance()  # google.cloud.compute_v1.types.Instance
        instance_resource.name = instance_name

        labels = {}
        if instance_label is not None:
            owner, repo = instance_label.split("/")
            labels.update({
                "gha-owner": _sanitize_label_value(owner),
                "gha-repo": _sanitize_label_value(repo),
                "gha-runner": _sanitize_label_value(template_name),
            })
        if job_id is not None:
            # Used to dedupe re-delivered queued webhooks: the webhook service
            # filters on this label before creating a new VM.
            labels["gha-job-id"] = _sanitize_label_value(job_id)
        if labels:
            instance_resource.labels = labels

        # Set metadata (startup script) - use shlex.quote to prevent command injection
        runner_group_flag = ""
        if self.github_runner_group:
            runner_group_flag = f" --runnergroup {shlex.quote(self.github_runner_group)}"

        ephemeral_flag = "--ephemeral " if self.ephemeral else ""
        startup_script = (
            "cd /actions-runner && "
            f"sudo -u runner ./config.sh --url {shlex.quote(repo_url)} "
            f"--token {shlex.quote(registration_token)} "
            f"--name {shlex.quote(instance_name)} "
            f"--labels {shlex.quote(template_name)} "
            f"{runner_group_flag} "
            f"{ephemeral_flag}"
            "--unattended "
            "--no-default-labels "
            "--disableupdate && "
            "sudo -u runner ./run.sh; "
            # Self-clean: once the runner process exits — ephemeral job done, or
            # config failed, or the runner was deregistered in reusable mode —
            # power the VM off. It lands in TERMINATED and the sweeper reclaims
            # it on its next pass, so a dropped completion webhook can no longer
            # leave an idle zombie. The ';' ensures this runs regardless of how
            # run.sh exits. Removes the deletion dependency on the webhook.
            "sudo shutdown -h now"
        )
        metadata = compute_v1.Metadata()
        metadata.items = [
            compute_v1.Items(key="startup-script", value=startup_script),
            compute_v1.Items(key="vmDnsSetting", value="ZonalOnly"),
            compute_v1.Items(key="block-project-ssh-keys", value="true"),
        ]
        instance_resource.metadata = metadata

        # Create the request
        # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.types.InsertInstanceRequest
        request = compute_v1.InsertInstanceRequest(
            project=self.project_id,
            zone=self.zone,
            instance_resource=instance_resource,
            source_instance_template=instance_template_resource.self_link
        )

        try:
            # https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/insert
            operation = self.instance_client.insert(request=request)
            logger.info(
                "Instance creation operation started: %s, delivery_id: %s",
                operation.name,
                delivery_id,
            )
            return instance_name
        except Exception as e:
            logger.error(
                "Failed to create instance: %s, delivery_id: %s", e, delivery_id
            )
            raise

    def delete_runner_instance(self, instance_name, delivery_id=None):
        """
        Delete a GCE instance.

        Args:
            instance_name (str): The name of the instance to delete.
            delivery_id (str): The GitHub webhook delivery ID for log correlation.
        """
        logger.info(
            "Deleting GCE instance %s, delivery_id: %s", instance_name, delivery_id
        )
        try:
            operation = self.instance_client.delete(
                project=self.project_id,
                zone=self.zone,
                instance=instance_name
            )
            logger.info(
                "Instance deletion operation started: %s, delivery_id: %s",
                operation.name,
                delivery_id,
            )
        except Exception as e:
            logger.error(
                "Failed to delete instance %s: %s, delivery_id: %s",
                instance_name,
                e,
                delivery_id,
            )
            raise

    def list_runner_instances(self, name_prefix='gcp-runner-'):
        """
        List GCE instances managed by this service in the configured zone.

        Args:
            name_prefix (str): Only return instances whose name starts with
                this prefix. Defaults to 'gcp-runner-' which matches every
                instance created by create_runner_instance.

        Yields:
            google.cloud.compute_v1.Instance: instances matching the prefix.
        """
        request = compute_v1.ListInstancesRequest(
            project=self.project_id,
            zone=self.zone,
            # Server-side filter on name prefix. The Compute API filter language
            # uses regex-like equality on string fields.
            filter=f'name eq "{name_prefix}.*"',
        )
        try:
            for instance in self.instance_client.list(request=request):
                # Defensive: the API filter is best-effort; double-check locally.
                if instance.name.startswith(name_prefix):
                    yield instance
        except Exception as e:
            logger.error("Failed to list instances with prefix %s: %s", name_prefix, e)
            raise

    def find_runner_by_job_id(self, job_id):
        """
        Return the first runner instance tagged with the given GitHub job_id, or None.

        Used to dedupe re-delivered workflow_job.queued webhooks so we do not
        create two VMs for the same job.
        """
        if job_id is None:
            return None
        target = _sanitize_label_value(job_id)
        for instance in self.list_runner_instances():
            if instance.labels.get('gha-job-id') == target:
                return instance
        return None

    # GCE instance states that represent a runner that is either already
    # serving a job or on its way to serving one. A VM in any of these states
    # counts as "live supply" for capacity planning — including PROVISIONING
    # and STAGING (still booting), so the reconciler does not double-create
    # VMs for jobs whose runner is still coming up.
    _LIVE_STATES = frozenset({'PROVISIONING', 'STAGING', 'RUNNING', 'REPAIRING'})

    def count_live_runners_by_label(self):
        """
        Count non-terminating runner VMs grouped by their ``gha-runner`` label
        (which equals the GitHub ``runs-on`` label the VM was created for).

        Returns:
            dict[str, int]: e.g. {'gcp-ubuntu-24-04-8core-arm': 12, ...}.
            VMs without a gha-runner label (shouldn't happen) are ignored.
        """
        return self.count_supply_by_label(inflight_window_seconds=0)

    def count_supply_by_label(self, inflight_window_seconds=180):
        """
        Count runner VMs that represent current-or-imminent capacity, grouped
        by ``gha-runner`` label.

        A VM counts as supply if it is in a live state (PROVISIONING/STAGING/
        RUNNING/REPAIRING) OR it was created within ``inflight_window_seconds``
        (it is in the create→boot→register pipeline and will serve a job
        shortly). Counting the in-flight pipeline is what stops the reconciler
        from re-creating, every pass, the VMs it created in the previous pass —
        the root cause of burst over-provisioning.

        Args:
            inflight_window_seconds: treat VMs younger than this as supply even
                if not yet live. 0 disables the in-flight grace (live-only).
        """
        counts = {}
        for instance in self.list_runner_instances():
            label = instance.labels.get('gha-runner')
            if not label:
                continue
            is_live = instance.status in self._LIVE_STATES
            is_inflight = False
            if inflight_window_seconds > 0 and instance.status != 'TERMINATED':
                age = self.instance_age_seconds(instance)
                is_inflight = age is not None and age < inflight_window_seconds
            if is_live or is_inflight:
                counts[label] = counts.get(label, 0) + 1
        return counts

    @staticmethod
    def instance_age_seconds(instance):
        """
        Return how many seconds ago the GCE instance was created, or None if unparseable.
        """
        ts = instance.creation_timestamp
        if not ts:
            return None
        try:
            # Compute API returns RFC3339 timestamps with offset, e.g.
            # "2026-05-28T14:35:01.123-07:00". Python 3.11+ handles fromisoformat.
            created = datetime.fromisoformat(ts)
        except ValueError:
            logger.warning("Could not parse creation_timestamp '%s'", ts)
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - created).total_seconds()
