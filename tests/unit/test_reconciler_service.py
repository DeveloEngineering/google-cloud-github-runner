"""Unit tests for the capacity-aware reconciler service."""
import time
from unittest.mock import patch, MagicMock

import pytest


def _job(id, status='queued', labels=None, started_at=None):
    return {
        'id': id,
        'status': status,
        'labels': labels if labels is not None else ['gcp-ubuntu-24-04-4core-arm'],
        'started_at': started_at,
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


def _old_ts(seconds_ago=600):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - seconds_ago))


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


@pytest.fixture
def svc(reconciler_env):
    with patch('app.services.reconciler_service.CloudTasksClient'), \
         patch('app.services.reconciler_service.GCloudClient'), \
         patch('app.services.reconciler_service.GitHubClient'):
        from app.services.reconciler_service import ReconcilerService
        return ReconcilerService()


class TestClassify:
    def test_young_job_skipped(self, svc):
        assert svc._classify(_job(1, started_at=_old_ts(10)), time.time()) == 'young'

    def test_non_gcp_label_skipped(self, svc):
        assert svc._classify(_job(1, labels=['ubuntu-latest'], started_at=_old_ts()), time.time()) == 'no_label'

    def test_non_queued_skipped(self, svc):
        assert svc._classify(_job(1, status='in_progress', started_at=_old_ts()), time.time()) == 'no_label'

    def test_old_gcp_queued_is_eligible(self, svc):
        assert svc._classify(_job(1, started_at=_old_ts()), time.time()) == 'eligible'

    def test_gcp_label_extraction(self, svc):
        assert svc._gcp_label(_job(1, labels=['self-hosted', 'gcp-ubuntu-24-04-8core-arm'])) == 'gcp-ubuntu-24-04-8core-arm'
        assert svc._gcp_label(_job(1, labels=['dependabot'])) == 'dependabot'
        assert svc._gcp_label(_job(1, labels=['ubuntu-latest'])) is None


class TestCapacityAwareReconcile:
    def _wire(self, svc, jobs, live_by_label):
        """Stub the GitHub API + GCE calls for a single-repo single-run pass."""
        svc._list_installation_repos = MagicMock(return_value=[_repo()])
        svc._list_active_runs = MagicMock(return_value=[{'id': 999}])
        svc._list_run_jobs = MagicMock(return_value=jobs)
        svc.gcloud_client.count_live_runners_by_label = MagicMock(return_value=live_by_label)
        svc.tasks_client.enqueue_workflow_job = MagicMock()

    def test_no_enqueue_when_supply_meets_demand(self, svc):
        # 3 queued 8core jobs, 3 live 8core runners → deficit 0 → enqueue nothing
        jobs = [_job(i, started_at=_old_ts()) for i in range(3)]
        for j in jobs:
            j['labels'] = ['gcp-ubuntu-24-04-8core-arm']
        self._wire(svc, jobs, {'gcp-ubuntu-24-04-8core-arm': 3})

        result = svc.reconcile(target_url='https://x/internal/process-workflow-job')

        assert result['jobs_enqueued'] == 0
        assert result['jobs_skipped_have_capacity'] == 3
        assert result['by_label']['gcp-ubuntu-24-04-8core-arm'] == {
            'demand': 3, 'supply': 3, 'deficit': 0
        }
        svc.tasks_client.enqueue_workflow_job.assert_not_called()

    def test_enqueues_only_the_deficit(self, svc):
        # 5 queued, 2 live → deficit 3 → enqueue exactly 3 (the oldest)
        jobs = []
        for i in range(5):
            j = _job(i, started_at=_old_ts(600 + i))  # i=0 oldest
            j['labels'] = ['gcp-ubuntu-24-04-8core-arm']
            jobs.append(j)
        self._wire(svc, jobs, {'gcp-ubuntu-24-04-8core-arm': 2})

        result = svc.reconcile(target_url='https://x/internal/process-workflow-job')

        assert result['jobs_enqueued'] == 3
        assert result['jobs_skipped_have_capacity'] == 2
        assert result['by_label']['gcp-ubuntu-24-04-8core-arm']['deficit'] == 3
        assert svc.tasks_client.enqueue_workflow_job.call_count == 3

    def test_no_live_runners_enqueues_full_demand(self, svc):
        jobs = [_job(i, started_at=_old_ts()) for i in range(4)]
        for j in jobs:
            j['labels'] = ['gcp-ubuntu-24-04-4core-arm']
        self._wire(svc, jobs, {})  # zero supply

        result = svc.reconcile(target_url='https://x/internal/process-workflow-job')

        assert result['jobs_enqueued'] == 4
        assert svc.tasks_client.enqueue_workflow_job.call_count == 4

    def test_separate_labels_tracked_independently(self, svc):
        jobs = []
        for i in range(3):
            j = _job(100 + i, started_at=_old_ts())
            j['labels'] = ['gcp-ubuntu-24-04-8core-arm']
            jobs.append(j)
        for i in range(2):
            j = _job(200 + i, started_at=_old_ts())
            j['labels'] = ['gcp-ubuntu-24-04-4core-arm']
            jobs.append(j)
        # 8core: demand 3, supply 3 → 0;  4core: demand 2, supply 0 → 2
        self._wire(svc, jobs, {'gcp-ubuntu-24-04-8core-arm': 3})

        result = svc.reconcile(target_url='https://x/internal/process-workflow-job')

        assert result['by_label']['gcp-ubuntu-24-04-8core-arm']['deficit'] == 0
        assert result['by_label']['gcp-ubuntu-24-04-4core-arm']['deficit'] == 2
        assert result['jobs_enqueued'] == 2

    def test_young_jobs_excluded_from_demand(self, svc):
        jobs = [
            _job(1, started_at=_old_ts(600), labels=['gcp-ubuntu-24-04-8core-arm']),
            _job(2, started_at=_old_ts(5), labels=['gcp-ubuntu-24-04-8core-arm']),  # young
        ]
        self._wire(svc, jobs, {})

        result = svc.reconcile(target_url='https://x/internal/process-workflow-job')

        # Only the old one counts toward demand
        assert result['jobs_skipped_young'] == 1
        assert result['by_label']['gcp-ubuntu-24-04-8core-arm']['demand'] == 1
        assert result['jobs_enqueued'] == 1


class TestSyntheticPayload:
    def test_org_payload_shape(self, svc):
        svc.tasks_client.enqueue_workflow_job = MagicMock()
        svc._enqueue_synthetic_workflow_job(
            target_url='https://x/internal/process-workflow-job',
            job=_job(42, labels=['gcp-ubuntu-24-04-4core-arm']),
            repo=_repo(),
            owner_type='Organization',
        )
        _, kwargs = svc.tasks_client.enqueue_workflow_job.call_args
        p = kwargs['payload']
        assert p['action'] == 'queued'
        assert p['workflow_job'] == {'id': 42, 'labels': ['gcp-ubuntu-24-04-4core-arm']}
        assert p['organization'] == {'login': 'DeveloEngineering'}
        assert kwargs['source'] == 'reconciler'
        assert kwargs['delivery_id'] == 'reconciler-42'

    def test_user_repo_has_no_org(self, svc):
        svc.tasks_client.enqueue_workflow_job = MagicMock()
        svc._enqueue_synthetic_workflow_job(
            target_url='https://x/internal/process-workflow-job',
            job=_job(99),
            repo=_repo(owner='someuser', owner_type='User'),
            owner_type='User',
        )
        _, kwargs = svc.tasks_client.enqueue_workflow_job.call_args
        assert 'organization' not in kwargs['payload']
