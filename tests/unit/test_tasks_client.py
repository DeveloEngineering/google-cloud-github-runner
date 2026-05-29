"""Unit tests for the Cloud Tasks client."""
import json
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def tasks_env(monkeypatch):
    monkeypatch.setenv('TASKS_QUEUE_PROJECT', 'my-proj')
    monkeypatch.setenv('TASKS_QUEUE_LOCATION', 'us-central1')
    monkeypatch.setenv('TASKS_QUEUE_NAME', 'workflow-job-uc1')
    monkeypatch.setenv('TASKS_INVOKER_SERVICE_ACCOUNT_EMAIL', 'invoker@my-proj.iam.gserviceaccount.com')


class TestCloudTasksClient:
    @patch('app.clients.tasks_client.tasks_v2')
    def test_enqueue_workflow_job_passes_through_payload(self, mock_tasks_v2, tasks_env):
        # Configure mock
        mock_inner = MagicMock()
        mock_inner.queue_path.return_value = 'projects/my-proj/locations/us-central1/queues/workflow-job-uc1'
        mock_inner.create_task.return_value = MagicMock(name='task1')
        mock_tasks_v2.CloudTasksClient.return_value = mock_inner
        # Stub enum + HTTP method
        mock_tasks_v2.HttpMethod.POST = 'POST'

        from app.clients.tasks_client import CloudTasksClient
        c = CloudTasksClient()
        c.enqueue_workflow_job(
            target_url='https://my-service.run.app/internal/process-workflow-job',
            payload={'action': 'queued', 'workflow_job': {'id': 42}},
            delivery_id='delivery-abc',
            source='webhook',
        )

        assert mock_inner.create_task.call_count == 1
        _, kwargs = mock_inner.create_task.call_args
        req = kwargs['request']
        task = req['task']
        body = json.loads(task['http_request']['body'].decode('utf-8'))

        assert body == {
            'source': 'webhook',
            'delivery_id': 'delivery-abc',
            'payload': {'action': 'queued', 'workflow_job': {'id': 42}},
        }
        assert task['http_request']['url'].endswith('/internal/process-workflow-job')
        assert task['http_request']['oidc_token']['service_account_email'] == \
            'invoker@my-proj.iam.gserviceaccount.com'
