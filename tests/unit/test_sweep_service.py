import pytest
from unittest.mock import Mock, patch

from app.services.sweep_service import SweepService


def _instance(name, age_seconds):
    inst = Mock()
    inst.name = name
    # SweepService consults instance_age_seconds via the GCloudClient static
    # method, which is patched per-test. The attribute set here is for
    # readability only.
    inst._age_seconds = age_seconds
    return inst


class TestSweepService:
    @patch('app.services.sweep_service.GCloudClient')
    def test_deletes_only_instances_older_than_threshold(
        self, mock_gc_client_class, monkeypatch
    ):
        monkeypatch.setenv('GITHUB_RUNNERS_ORPHAN_MAX_AGE_SECONDS', '600')

        young = _instance('gcp-runner-young', 100)
        old = _instance('gcp-runner-old', 9999)
        instances = [young, old]

        mock_client = Mock()
        mock_client.list_runner_instances.return_value = iter(instances)
        mock_gc_client_class.return_value = mock_client

        with patch.object(
            SweepService, '__init__', lambda self: None
        ):
            svc = SweepService()
            svc.gcloud_client = mock_client
            svc.max_age_seconds = 600

            def fake_age(instance):
                return instance._age_seconds

            mock_gc_client_class.instance_age_seconds = staticmethod(fake_age)
            with patch(
                'app.services.sweep_service.GCloudClient.instance_age_seconds',
                staticmethod(fake_age),
            ):
                result = svc.sweep()

        assert result['deleted'] == 1
        assert result['deleted_names'] == ['gcp-runner-old']
        assert result['skipped'] == 1
        assert result['errors'] == 0
        mock_client.delete_runner_instance.assert_called_once_with('gcp-runner-old')

    @patch('app.services.sweep_service.GCloudClient')
    def test_continues_when_one_delete_fails(self, mock_gc_client_class):
        bad = _instance('gcp-runner-bad', 9999)
        good = _instance('gcp-runner-good', 9999)

        mock_client = Mock()
        mock_client.list_runner_instances.return_value = iter([bad, good])
        mock_client.delete_runner_instance.side_effect = [
            Exception('boom'),
            None,
        ]
        mock_gc_client_class.return_value = mock_client

        with patch.object(SweepService, '__init__', lambda self: None):
            svc = SweepService()
            svc.gcloud_client = mock_client
            svc.max_age_seconds = 600

            with patch(
                'app.services.sweep_service.GCloudClient.instance_age_seconds',
                staticmethod(lambda i: i._age_seconds),
            ):
                result = svc.sweep()

        assert result['deleted'] == 1
        assert result['errors'] == 1
        assert result['deleted_names'] == ['gcp-runner-good']
