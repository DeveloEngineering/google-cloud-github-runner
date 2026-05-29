"""Tests for the (now-async) /webhook route.

The route's responsibility was reduced to: signature-check, parse, enqueue,
return 202. The downstream processing is exercised separately in
test_routes_internal.py + test_webhook_service.py.
"""
import json
from unittest.mock import patch


class TestWebhookRoutes:
    @patch('app.routes.webhook.CloudTasksClient')
    @patch('app.routes.webhook.verify_github_signature')
    def test_workflow_job_enqueues_task(
        self, mock_verify, mock_tasks_client_cls, client, sample_workflow_job_payload
    ):
        """A valid workflow_job event enqueues exactly one Cloud Tasks task."""
        mock_verify.return_value = True
        mock_tasks = mock_tasks_client_cls.return_value

        response = client.post(
            '/webhook',
            data=json.dumps(sample_workflow_job_payload),
            content_type='application/json',
            headers={
                'X-GitHub-Event': 'workflow_job',
                'X-GitHub-Delivery': 'abc-123-def',
            },
        )

        assert response.status_code == 202
        assert response.json['status'] == 'accepted'
        assert response.json['delivery_id'] == 'abc-123-def'

        mock_tasks.enqueue_workflow_job.assert_called_once()
        _, kwargs = mock_tasks.enqueue_workflow_job.call_args
        assert kwargs['payload'] == sample_workflow_job_payload
        assert kwargs['delivery_id'] == 'abc-123-def'
        assert kwargs['source'] == 'webhook'
        assert kwargs['target_url'].endswith('/internal/process-workflow-job')

    @patch('app.routes.webhook.CloudTasksClient')
    @patch('app.routes.webhook.verify_github_signature')
    def test_invalid_signature_does_not_enqueue(
        self, mock_verify, mock_tasks_client_cls, client, sample_workflow_job_payload
    ):
        """Bad signature → 403, no task enqueued."""
        mock_verify.return_value = False

        response = client.post(
            '/webhook',
            data=json.dumps(sample_workflow_job_payload),
            content_type='application/json',
            headers={'X-GitHub-Event': 'workflow_job'},
        )

        assert response.status_code == 403
        mock_tasks_client_cls.return_value.enqueue_workflow_job.assert_not_called()

    @patch('app.routes.webhook.CloudTasksClient')
    @patch('app.routes.webhook.verify_github_signature')
    def test_unknown_event_is_ignored(
        self, mock_verify, mock_tasks_client_cls, client
    ):
        """Non-workflow_job events return 200 ignored without enqueueing."""
        mock_verify.return_value = True

        response = client.post(
            '/webhook',
            data=json.dumps({'irrelevant': True}),
            content_type='application/json',
            headers={'X-GitHub-Event': 'push'},
        )

        assert response.status_code == 200
        assert response.json['status'] == 'ignored'
        mock_tasks_client_cls.return_value.enqueue_workflow_job.assert_not_called()

    def test_ping_event_succeeds_without_signature_check(self, client):
        """Ping events always succeed — used by GitHub when registering the hook."""
        response = client.post(
            '/webhook',
            data=json.dumps({}),
            content_type='application/json',
            headers={'X-GitHub-Event': 'ping'},
        )
        assert response.status_code == 200
        assert response.json['status'] == 'success'

    @patch('app.routes.webhook.CloudTasksClient')
    @patch('app.routes.webhook.verify_github_signature')
    def test_enqueue_failure_returns_500(
        self, mock_verify, mock_tasks_client_cls, client, sample_workflow_job_payload
    ):
        """If Cloud Tasks rejects the enqueue we return 5xx so the failure is
        visible in GitHub's delivery history; the reconciler will recover."""
        mock_verify.return_value = True
        mock_tasks_client_cls.return_value.enqueue_workflow_job.side_effect = Exception('boom')

        response = client.post(
            '/webhook',
            data=json.dumps(sample_workflow_job_payload),
            content_type='application/json',
            headers={'X-GitHub-Event': 'workflow_job'},
        )

        assert response.status_code == 500
