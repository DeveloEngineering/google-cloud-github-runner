"""Tests for the /reconcile HTTP route."""
import json
from unittest.mock import patch


class TestReconcileRoute:
    @patch('app.routes.reconcile.verify_scheduler_oidc_token')
    @patch('app.routes.reconcile.ReconcilerService')
    def test_happy_path(self, mock_svc_cls, mock_auth, client):
        mock_auth.return_value = True
        mock_svc_cls.return_value.reconcile.return_value = {
            'repos_scanned': 1,
            'jobs_enqueued': 2,
            'enqueued_job_ids': [1, 2],
        }
        response = client.post(
            '/reconcile',
            data=json.dumps({}),
            content_type='application/json',
            headers={'Authorization': 'Bearer fake'},
        )
        assert response.status_code == 200
        assert response.json['status'] == 'ok'
        assert response.json['jobs_enqueued'] == 2
        _, kwargs = mock_svc_cls.return_value.reconcile.call_args
        assert kwargs['target_url'].endswith('/internal/process-workflow-job')

    @patch('app.routes.reconcile.verify_scheduler_oidc_token')
    def test_oidc_failure(self, mock_auth, client):
        mock_auth.return_value = False
        response = client.post('/reconcile')
        assert response.status_code == 403

    @patch('app.routes.reconcile.verify_scheduler_oidc_token')
    @patch('app.routes.reconcile.ReconcilerService')
    def test_service_failure_returns_500(self, mock_svc_cls, mock_auth, client):
        mock_auth.return_value = True
        mock_svc_cls.return_value.reconcile.side_effect = Exception('boom')
        response = client.post(
            '/reconcile', headers={'Authorization': 'Bearer fake'}
        )
        assert response.status_code == 500
