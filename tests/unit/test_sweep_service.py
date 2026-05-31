from unittest.mock import Mock, patch

from app.services.sweep_service import SweepService


def _instance(name, age_seconds):
    inst = Mock()
    inst.name = name
    inst._age_seconds = age_seconds
    return inst


def _bare_service(gcloud, github=None, max_age=600):
    """Build a SweepService with __init__ bypassed and deps injected."""
    with patch.object(SweepService, '__init__', lambda self: None):
        svc = SweepService()
    svc.gcloud_client = gcloud
    svc.github_client = github or Mock()
    svc.max_age_seconds = max_age
    return svc


class TestAgeBasedOrphanSweep:
    def test_deletes_only_instances_older_than_threshold(self):
        gc = Mock()
        gc.ephemeral = True  # ephemeral mode → only age sweep runs
        gc.list_runner_instances.return_value = iter([
            _instance('gcp-runner-young', 100),
            _instance('gcp-runner-old', 9999),
        ])
        svc = _bare_service(gc)
        with patch(
            'app.services.sweep_service.GCloudClient.instance_age_seconds',
            staticmethod(lambda i: i._age_seconds),
        ):
            result = svc.sweep()

        assert result['deleted'] == 1
        assert result['deleted_names'] == ['gcp-runner-old']
        assert result['skipped'] == 1
        assert 'idle_reap' not in result  # ephemeral mode: no idle reaping
        gc.delete_runner_instance.assert_called_once_with('gcp-runner-old')

    def test_continues_when_one_delete_fails(self):
        gc = Mock()
        gc.ephemeral = True
        gc.list_runner_instances.return_value = iter([
            _instance('gcp-runner-bad', 9999),
            _instance('gcp-runner-good', 9999),
        ])
        gc.delete_runner_instance.side_effect = [Exception('boom'), None]
        svc = _bare_service(gc)
        with patch(
            'app.services.sweep_service.GCloudClient.instance_age_seconds',
            staticmethod(lambda i: i._age_seconds),
        ):
            result = svc.sweep()

        assert result['deleted'] == 1
        assert result['errors'] == 1
        assert result['deleted_names'] == ['gcp-runner-good']


class TestIdleReaping:
    def _runner(self, name, busy=False, status='online', label='gcp-ubuntu-24-04-8core-arm', rid=None):
        return {
            'id': rid if rid is not None else hash(name) % 100000,
            'name': name,
            'status': status,
            'busy': busy,
            'labels': [{'name': label}],
        }

    def _repos(self, owner='DeveloEngineering'):
        return [{
            'name': 'develo-emr',
            'full_name': f'{owner}/develo-emr',
            'owner': {'login': owner, 'type': 'Organization'},
        }]

    def test_reaps_idle_runners_beyond_demand(self):
        gc = Mock()
        gc.ephemeral = False
        gc.list_runner_instances.return_value = iter([])  # nothing old for age sweep
        gh = Mock()
        gh.list_installation_repos.return_value = self._repos()
        # demand: 1 queued 8core job → keep 1 idle, reap the other 2
        gh.list_active_runs.return_value = iter([{'id': 1}])
        gh.list_run_jobs.return_value = [
            {'status': 'queued', 'labels': ['gcp-ubuntu-24-04-8core-arm']},
        ]
        gh.list_runners.return_value = [
            self._runner('gcp-runner-a', rid=1),
            self._runner('gcp-runner-b', rid=2),
            self._runner('gcp-runner-c', rid=3),
        ]
        gh.delete_runner.return_value = True
        svc = _bare_service(gc, gh)

        with patch('app.services.sweep_service.GCloudClient.instance_age_seconds',
                   staticmethod(lambda i: 0)):
            result = svc.sweep()

        reap = result['idle_reap']
        assert reap['idle_found'] == 3
        assert reap['kept_for_demand'] == 1
        assert reap['reaped'] == 2  # 3 idle - 1 kept
        assert gh.delete_runner.call_count == 2
        assert gc.delete_runner_instance.call_count == 2

    def test_busy_runners_never_reaped_and_protected_from_age_sweep(self):
        gc = Mock()
        gc.ephemeral = False
        # An OLD busy runner exists — age sweep would normally delete it.
        old_busy = _instance('gcp-runner-busy', 99999)
        gc.list_runner_instances.return_value = iter([old_busy])
        gh = Mock()
        gh.list_installation_repos.return_value = self._repos()
        gh.list_active_runs.return_value = iter([])
        gh.list_run_jobs.return_value = []
        gh.list_runners.return_value = [
            self._runner('gcp-runner-busy', busy=True, rid=9),
        ]
        svc = _bare_service(gc, gh)

        with patch('app.services.sweep_service.GCloudClient.instance_age_seconds',
                   staticmethod(lambda i: i._age_seconds)):
            result = svc.sweep()

        # Busy runner not reaped...
        assert result['idle_reap']['reaped'] == 0
        gh.delete_runner.assert_not_called()
        # ...and the age sweep skipped it (busy-protected), not deleted.
        gc.delete_runner_instance.assert_not_called()
        assert result['deleted'] == 0

    def test_no_demand_reaps_all_idle(self):
        gc = Mock()
        gc.ephemeral = False
        gc.list_runner_instances.return_value = iter([])
        gh = Mock()
        gh.list_installation_repos.return_value = self._repos()
        gh.list_active_runs.return_value = iter([])  # zero demand
        gh.list_run_jobs.return_value = []
        gh.list_runners.return_value = [
            self._runner('gcp-runner-a', rid=1),
            self._runner('gcp-runner-b', rid=2),
        ]
        gh.delete_runner.return_value = True
        svc = _bare_service(gc, gh)

        with patch('app.services.sweep_service.GCloudClient.instance_age_seconds',
                   staticmethod(lambda i: 0)):
            result = svc.sweep()

        assert result['idle_reap']['reaped'] == 2  # nothing to keep

    def test_busy_deregister_race_skips_vm_delete(self):
        """If GitHub rejects deregister (runner became busy), VM is kept."""
        gc = Mock()
        gc.ephemeral = False
        gc.list_runner_instances.return_value = iter([])
        gh = Mock()
        gh.list_installation_repos.return_value = self._repos()
        gh.list_active_runs.return_value = iter([])
        gh.list_run_jobs.return_value = []
        gh.list_runners.return_value = [self._runner('gcp-runner-a', rid=1)]
        gh.delete_runner.return_value = False  # busy race → rejected
        svc = _bare_service(gc, gh)

        with patch('app.services.sweep_service.GCloudClient.instance_age_seconds',
                   staticmethod(lambda i: 0)):
            result = svc.sweep()

        assert result['idle_reap']['reaped'] == 0
        gc.delete_runner_instance.assert_not_called()
