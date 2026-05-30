"""Unit tests for the reconciler service."""
import time
from unittest.mock import patch, MagicMock

import pytest


def _job(id, status='queued', labels=None, started_at=None, runner_name=None):
    return {
        'id': id,
        'status': status,
        'labels': labels if labels is not None else ['gcp-ubuntu-24-04-4core-arm'],
        'started_at': started_at,
        'runner_name': runner_name,
    }


def _repo(owner='DeveloEngineering', name='develo-emr', owner_type='Organization'):
    return {
        'name': name,
        'html_url': f'https://github.com/{owner}/{name}',
        'full_name': f'{owner}/{name}',
        'owner': {
            'login': owner,
            'html_url': f'https://github.com/{owner}',
            'type': owner_type,
        },
    }


@pytest.fixture
def reconciler_env(monkeypatch):
    monkeypatch.setenv('GITHUB_APP_ID', '1')
    monkeypatch.setenv('GITHUB_INSTALLATION_ID', '1')
    monkeypatch.setenv('GITHUB_PRIVATE_KEY', '-----BEGIN-----\ndummy\n-----END-----')
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test')
    monkeypatch.setenv('TASKS_QUEUE_PROJECT', 'test')
    monkeypatch.setenv('TASKS_QUEUE_LOCATION', 'us-central1')
    monkeypatch.setenv('TASKS_QUEUE_NAME', 'q')
    monkeypatch.setenv('TASKS_INVOKER_SERVICE_ACCOUNT_EMAIL', 'invoker@test.iam.gserviceaccount.com')
    monkeypatch.setenv('GITHUB_RECONCILER_MIN_JOB_AGE_SECONDS', '60')


class TestReconcilerDecide:
    @patch('app.services.reconciler_service.CloudTasksClient')
    @patch('app.services.reconciler_service.GCloudClient')
    @patch('app.services.reconciler_service.GitHubClient')
    def test_young_job_is_skipped(self, mock_gh, mock_gc, mock_tasks, reconciler_env):
        from app.services.reconciler_service import ReconcilerService
        svc = ReconcilerService()
        # started 10s ago, threshold is 60s
        recent = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 10))
        assert svc._decide(_job(1, started_at=recent), time.time()) == 'young'

    @patch('app.services.reconciler_service.CloudTasksClient')
    @patch('app.services.reconciler_service.GCloudClient')
    @patch('app.services.reconciler_service.GitHubClient')
    def test_non_gcp_label_is_skipped(self, mock_gh, mock_gc, mock_tasks, reconciler_env):
        from app.services.reconciler_service import ReconcilerService
        svc = ReconcilerService()
        old = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 600))
        assert svc._decide(_job(1, labels=['ubuntu-latest'], started_at=old), time.time()) == 'no_label'

    @patch('app.services.reconciler_service.CloudTasksClient')
    @patch('app.services.reconciler_service.GCloudClient')
    @patch('app.services.reconciler_service.GitHubClient')
    def test_existing_vm_is_skipped(self, mock_gh, mock_gc_cls, mock_tasks, reconciler_env):
        gc = mock_gc_cls.return_value
        gc.find_runner_by_job_id.return_value = MagicMock(name='already-there')
        from app.services.reconciler_service import ReconcilerService
        svc = ReconcilerService()
        old = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 600))
        assert svc._decide(_job(1, started_at=old), time.time()) == 'vm_exists'

    @patch('app.services.reconciler_service.CloudTasksClient')
    @patch('app.services.reconciler_service.GCloudClient')
    @patch('app.services.reconciler_service.GitHubClient')
    def test_old_unlabeled_no_vm_is_enqueued(self, mock_gh, mock_gc_cls, mock_tasks, reconciler_env):
        mock_gc_cls.return_value.find_runner_by_job_id.return_value = None
        from app.services.reconciler_service import ReconcilerService
        svc = ReconcilerService()
        old = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 600))
        assert svc._decide(_job(1, started_at=old), time.time()) == 'enqueue'


class TestReconcilerEnqueueShape:
    @patch('app.services.reconciler_service.CloudTasksClient')
    @patch('app.services.reconciler_service.GCloudClient')
    @patch('app.services.reconciler_service.GitHubClient')
    def test_synthesized_payload_for_org_repo(self, mock_gh, mock_gc, mock_tasks_cls, reconciler_env):
        tasks = mock_tasks_cls.return_value
        from app.services.reconciler_service import ReconcilerService
        svc = ReconcilerService()
        svc._enqueue_synthetic_workflow_job(
            target_url='https://x/internal/process-workflow-job',
            job=_job(42, labels=['gcp-ubuntu-24-04-4core-arm']),
            repo=_repo(),
            owner_type='Organization',
        )

        tasks.enqueue_workflow_job.assert_called_once()
        _, kwargs = tasks.enqueue_workflow_job.call_args
        p = kwargs['payload']
        assert p['action'] == 'queued'
        assert p['workflow_job'] == {
            'id': 42,
            'labels': ['gcp-ubuntu-24-04-4core-arm'],
        }
        assert p['repository']['full_name'] == 'DeveloEngineering/develo-emr'
        assert p['repository']['owner']['html_url'] == 'https://github.com/DeveloEngineering'
        assert p['organization'] == {'login': 'DeveloEngineering'}
        assert kwargs['source'] == 'reconciler'
        assert kwargs['delivery_id'] == 'reconciler-42'

    @patch('app.services.reconciler_service.CloudTasksClient')
    @patch('app.services.reconciler_service.GCloudClient')
    @patch('app.services.reconciler_service.GitHubClient')
    def test_synthesized_payload_for_user_repo_has_no_org(
        self, mock_gh, mock_gc, mock_tasks_cls, reconciler_env
    ):
        from app.services.reconciler_service import ReconcilerService
        svc = ReconcilerService()
        svc._enqueue_synthetic_workflow_job(
            target_url='https://x/internal/process-workflow-job',
            job=_job(99),
            repo=_repo(owner='someuser', owner_type='User'),
            owner_type='User',
        )
        _, kwargs = mock_tasks_cls.return_value.enqueue_workflow_job.call_args
        assert 'organization' not in kwargs['payload']
