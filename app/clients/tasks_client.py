"""
Cloud Tasks client — enqueues workflow_job processing for the async path.

The /webhook handler is supposed to do as little work as possible before
returning 200 to GitHub (which enforces a 10-second delivery timeout). Real
work — GitHub API calls + GCE VM creation — is deferred onto this queue and
handled by /internal/process-workflow-job, which is invoked by Cloud Tasks
with its own OIDC token.
"""
import json
import logging
import os
from typing import Optional

from google.cloud import tasks_v2

logger = logging.getLogger(__name__)


class CloudTasksClient:
    """Wraps google-cloud-tasks for enqueueing workflow_job processing."""

    def __init__(self):
        self.project = os.environ.get('TASKS_QUEUE_PROJECT', '').strip()
        self.location = os.environ.get('TASKS_QUEUE_LOCATION', '').strip()
        self.queue = os.environ.get('TASKS_QUEUE_NAME', '').strip()
        self.invoker_sa_email = os.environ.get(
            'TASKS_INVOKER_SERVICE_ACCOUNT_EMAIL', ''
        ).strip()
        if not all([self.project, self.location, self.queue, self.invoker_sa_email]):
            logger.warning(
                "Cloud Tasks env vars missing — enqueue will fail. "
                "TASKS_QUEUE_PROJECT=%s TASKS_QUEUE_LOCATION=%s "
                "TASKS_QUEUE_NAME=%s TASKS_INVOKER_SERVICE_ACCOUNT_EMAIL=%s",
                self.project, self.location, self.queue, self.invoker_sa_email,
            )
        self._client = tasks_v2.CloudTasksClient()

    @property
    def queue_path(self) -> str:
        return self._client.queue_path(self.project, self.location, self.queue)

    def enqueue_workflow_job(
        self,
        target_url: str,
        payload: dict,
        delivery_id: Optional[str] = None,
        source: str = "webhook",
    ) -> str:
        """
        Enqueue a workflow_job for asynchronous processing.

        Args:
            target_url: Absolute URL of the consumer endpoint (typically
                ``https://<service>/internal/process-workflow-job``). The
                webhook handler derives this from the inbound request's host.
            payload: The original ``workflow_job`` event payload from GitHub
                (or a synthetic equivalent from the reconciler).
            delivery_id: GitHub webhook delivery id when available, used for
                log correlation across the async hop. Optional.
            source: ``"webhook"`` or ``"reconciler"`` — recorded in the task
                body so the consumer can distinguish them in logs/metrics.

        Returns:
            The fully-qualified name of the created Cloud Tasks task.
        """
        body = {
            'source': source,
            'delivery_id': delivery_id,
            'payload': payload,
        }
        task = {
            'http_request': {
                'http_method': tasks_v2.HttpMethod.POST,
                'url': target_url,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps(body).encode('utf-8'),
                # Cloud Tasks mints an OIDC token signed by this SA and
                # attaches it to the outbound request. The consumer at
                # /internal/process-workflow-job verifies the token's email
                # claim against INTERNAL_OIDC_SERVICE_ACCOUNT_EMAIL (which
                # is set to this same SA's email in terraform).
                'oidc_token': {
                    'service_account_email': self.invoker_sa_email,
                },
            },
        }
        response = self._client.create_task(
            request={'parent': self.queue_path, 'task': task}
        )
        logger.info(
            "Enqueued workflow_job task source=%s delivery_id=%s task=%s",
            source, delivery_id, response.name,
        )
        return response.name
