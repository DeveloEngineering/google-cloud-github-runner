"""Tests for /internal/process-workflow-job — the Cloud Tasks consumer."""
import json
from unittest.mock import patch


def _bearer():
    return {'Authorization': 'Bearer fake-oidc-token'}


class TestInternalProcessWorkflowJob:
    @patch('app.routes.internal.verify_scheduler_oidc_token')
    @patch('app.routes.internal.WebhookService')
    def test_happy_path_invokes_webhook_service(
        self, mock_ws_cls, mock_auth, client
    ):
        mock_auth.return_value = True
        ws = mock_ws_cls.return_value
        ws.handle_workflow_job.return_value = {'action': 'created', 'runner_name': 'gcp-runner-123'}

        body = {
            'source': 'webhook',
            'delivery_id': 'delivery-1',
            'payload': {'action': 'queued', 'workflow_job': {'labels': ['gcp-foo']}},
        }
        response = client.post(
            '/internal/process-workflow-job',
            data=json.dumps(body),
            content_type='application/json',
            headers=_bearer(),
        )

        assert response.status_code == 200
        assert response.json['status'] == 'success'
        assert response.json['action'] == 'created'
        ws.handle_workflow_job.assert_called_once_with(
            body['payload'], delivery_id='delivery-1'
        )

    @patch('app.routes.internal.verify_scheduler_oidc_token')
    def test_oidc_failure_returns_403(self, mock_auth, client):
        mock_auth.return_value = False
        response = client.post(
            '/internal/process-workflow-job',
            data=json.dumps({'payload': {'action': 'queued'}}),
            content_type='application/json',
        )
        assert response.status_code == 403

    @patch('app.routes.internal.verify_scheduler_oidc_token')
    @patch('app.routes.internal.WebhookService')
    def test_empty_payload_returns_400_no_retry(self, mock_ws_cls, mock_auth, client):
        mock_auth.return_value = True
        response = client.post(
            '/internal/process-workflow-job',
            data=json.dumps({'source': 'webhook'}),
            content_type='application/json',
            headers=_bearer(),
        )
        assert response.status_code == 400
        mock_ws_cls.return_value.handle_workflow_job.assert_not_called()

    @patch('app.routes.internal.verify_scheduler_oidc_token')
    @patch('app.routes.internal.WebhookService')
    def test_processing_error_returns_500_for_retry(self, mock_ws_cls, mock_auth, client):
        mock_auth.return_value = True
        mock_ws_cls.return_value.handle_workflow_job.side_effect = Exception('transient')

        response = client.post(
            '/internal/process-workflow-job',
            data=json.dumps({'payload': {'action': 'queued'}}),
            content_type='application/json',
            headers=_bearer(),
        )
        assert response.status_code == 500
