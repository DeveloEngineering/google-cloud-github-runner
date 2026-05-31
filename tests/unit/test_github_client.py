import pytest
import logging
from unittest.mock import patch, MagicMock
from app.clients.github_client import GitHubClient, _reset_token_cache_for_tests


@pytest.fixture(autouse=True)
def reset_token_cache():
    """Clear the module-level installation-token cache between tests."""
    _reset_token_cache_for_tests()
    yield
    _reset_token_cache_for_tests()


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Set up mock environment variables for GitHub client."""
    monkeypatch.setenv('GITHUB_APP_ID', '12345')
    monkeypatch.setenv('GITHUB_INSTALLATION_ID', '67890')
    monkeypatch.setenv('GITHUB_PRIVATE_KEY_PATH', 'test-key.pem')
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
    monkeypatch.setenv('GITHUB_WEBHOOK_SECRET', 'test-secret')


@pytest.fixture
def mock_private_key_file(tmp_path):
    """Create a temporary private key file."""
    key_file = tmp_path / "test-key.pem"
    key_content = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8FqH2XZKbNj8xSNiLnmvlYnxF0s
-----END RSA PRIVATE KEY-----"""
    key_file.write_text(key_content)
    return str(key_file)


class TestGitHubClient:
    def test_init_with_env_vars(self, mock_env_vars):
        """Test GitHubClient initialization with environment variables."""
        client = GitHubClient()

        assert client.app_id == '12345'
        assert client.installation_id == '67890'
        assert client.private_key_path == 'test-key.pem'

    def test_init_missing_config(self):
        """Test GitHubClient initialization with missing configuration."""
        with patch.dict('os.environ', {}, clear=True):
            client = GitHubClient()
            assert client.app_id is None
            assert client.installation_id is None

    @patch('builtins.open', create=True)
    def test_get_private_key_from_file(self, mock_open, mock_env_vars):
        """Test retrieving private key from file."""
        mock_file = MagicMock()
        mock_file.read.return_value = "FAKE_PRIVATE_KEY"
        mock_open.return_value.__enter__.return_value = mock_file

        client = GitHubClient()
        key = client._get_private_key()

        assert key == "FAKE_PRIVATE_KEY"
        mock_open.assert_called_once_with('test-key.pem', 'r')

    def test_get_private_key_from_env_var(self, monkeypatch):
        """Test retrieving private key from environment variable."""
        monkeypatch.setenv('GITHUB_APP_ID', '12345')
        monkeypatch.setenv('GITHUB_INSTALLATION_ID', '67890')
        monkeypatch.setenv('GITHUB_PRIVATE_KEY', 'SECRET_KEY_FROM_ENV')
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
        monkeypatch.setenv('GITHUB_WEBHOOK_SECRET', 'test-secret')

        client = GitHubClient()
        key = client._get_private_key()

        assert key == "SECRET_KEY_FROM_ENV"

    @patch('app.clients.github_client.jwt.encode')
    @patch.object(GitHubClient, '_get_private_key')
    def test_generate_jwt(self, mock_get_key, mock_jwt_encode, mock_env_vars):
        """Test JWT generation."""
        mock_get_key.return_value = "FAKE_KEY"
        mock_jwt_encode.return_value = "FAKE_JWT_TOKEN"

        client = GitHubClient()
        token = client._generate_jwt()

        assert token == "FAKE_JWT_TOKEN"
        mock_jwt_encode.assert_called_once()

    @patch('app.clients.github_client.requests.post')
    @patch.object(GitHubClient, '_generate_jwt')
    def test_get_installation_access_token(self, mock_jwt, mock_post, mock_env_vars):
        """Test getting installation access token."""
        mock_jwt.return_value = "JWT_TOKEN"
        mock_response = MagicMock()
        mock_response.json.return_value = {'token': 'INSTALL_TOKEN'}
        mock_post.return_value = mock_response

        client = GitHubClient()
        token = client.get_installation_access_token()

        assert token == 'INSTALL_TOKEN'
        mock_post.assert_called_once()

    @patch('app.clients.github_client.requests.post')
    @patch.object(GitHubClient, 'get_installation_access_token')
    def test_get_registration_token_for_repo(self, mock_install_token, mock_post, mock_env_vars):
        """Test getting registration token for a repository."""
        mock_install_token.return_value = "INSTALL_TOKEN"
        mock_response = MagicMock()
        mock_response.json.return_value = {'token': 'REG_TOKEN'}
        mock_post.return_value = mock_response

        client = GitHubClient()
        token = client.get_registration_token(repo_name='owner/repo')

        assert token == 'REG_TOKEN'
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert 'repos/owner/repo' in args[0]

    @patch('app.clients.github_client.requests.post')
    @patch.object(GitHubClient, 'get_installation_access_token')
    def test_get_registration_token_for_org(self, mock_install_token, mock_post, mock_env_vars):
        """Test getting registration token for an organization."""
        mock_install_token.return_value = "INSTALL_TOKEN"
        mock_response = MagicMock()
        mock_response.json.return_value = {'token': 'ORG_TOKEN'}
        mock_post.return_value = mock_response

        client = GitHubClient()
        token = client.get_registration_token(org_name='my-org')

        assert token == 'ORG_TOKEN'
        args, kwargs = mock_post.call_args
        assert 'orgs/my-org' in args[0]

    @patch.object(GitHubClient, 'get_installation_access_token')
    def test_get_registration_token_no_params(self, mock_install_token, mock_env_vars):
        """Test that ValueError is raised when neither org nor repo is provided."""
        mock_install_token.return_value = "FAKE_TOKEN"
        client = GitHubClient()

        with pytest.raises(ValueError, match="Either org_name or repo_name must be provided"):
            client.get_registration_token()

    def test_get_private_key_no_source(self):
        """Test that ValueError is raised when no private key source is configured."""
        with patch.dict('os.environ', {'GITHUB_APP_ID': '12345', 'GITHUB_INSTALLATION_ID': '67890'}, clear=True):
            client = GitHubClient()
            with pytest.raises(ValueError, match="No private key source configured"):
                client._get_private_key()

    @patch.object(GitHubClient, '_get_private_key')
    @patch('app.clients.github_client.jwt.encode')
    def test_generate_jwt_error(self, mock_jwt, mock_get_key, mock_env_vars):
        """Test JWT generation error handling."""
        mock_get_key.side_effect = Exception("Key error")

        client = GitHubClient()
        with pytest.raises(Exception, match="Key error"):
            client._generate_jwt()


class TestInstallationTokenCaching:
    """The installation token is cached process-wide and reused until near expiry."""

    @patch('app.clients.github_client.requests.post')
    @patch.object(GitHubClient, '_generate_jwt')
    def test_second_call_reuses_cached_token(self, mock_jwt, mock_post, mock_env_vars):
        """Two GitHubClient instances share the same cached installation token."""
        mock_jwt.return_value = "JWT"
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'token': 'CACHED_TOKEN',
            # 1 hour in the future, well past the 10-minute refresh buffer.
            'expires_at': '2099-01-01T00:00:00Z',
        }
        mock_post.return_value = mock_response

        first = GitHubClient().get_installation_access_token()
        second = GitHubClient().get_installation_access_token()

        assert first == 'CACHED_TOKEN'
        assert second == 'CACHED_TOKEN'
        # Only ONE GitHub API call across both invocations — the second is a cache hit.
        assert mock_post.call_count == 1

    @patch('app.clients.github_client.requests.post')
    @patch.object(GitHubClient, '_generate_jwt')
    def test_expired_cache_refreshes(self, mock_jwt, mock_post, mock_env_vars):
        """A cached token within the refresh buffer triggers a refresh."""
        mock_jwt.return_value = "JWT"

        from app.clients import github_client as gc
        # Prime the cache with a token that "expires in 60s" — inside the
        # 10-minute refresh buffer, so the next call should mint a fresh one.
        import time as time_mod
        with gc._token_cache_lock:
            gc._token_cache['token'] = 'STALE_TOKEN'
            gc._token_cache['expires_at'] = time_mod.time() + 60

        mock_response = MagicMock()
        mock_response.json.return_value = {
            'token': 'FRESH_TOKEN',
            'expires_at': '2099-01-01T00:00:00Z',
        }
        mock_post.return_value = mock_response

        token = GitHubClient().get_installation_access_token()

        assert token == 'FRESH_TOKEN'
        assert mock_post.call_count == 1

    @patch('app.clients.github_client.requests.post')
    @patch.object(GitHubClient, '_generate_jwt')
    def test_missing_expires_at_falls_back_to_55min(self, mock_jwt, mock_post, mock_env_vars):
        """GitHub response without expires_at still produces a valid cache entry."""
        mock_jwt.return_value = "JWT"
        mock_response = MagicMock()
        # No expires_at field — older GitHub Enterprise responses sometimes omit it.
        mock_response.json.return_value = {'token': 'TOKEN_NO_EXPIRY'}
        mock_post.return_value = mock_response

        token = GitHubClient().get_installation_access_token()
        assert token == 'TOKEN_NO_EXPIRY'

        # Second call within a few seconds should hit the cache (55-min fallback).
        GitHubClient().get_installation_access_token()
        assert mock_post.call_count == 1


class TestGitHubClientDeliveryIdLogging:
    """Tests to verify that delivery_id is logged in GitHubClient methods."""

    @patch("app.clients.github_client.requests.post")
    @patch.object(GitHubClient, "get_installation_access_token")
    def test_registration_token_for_repo_logs_delivery_id(
        self, mock_install_token, mock_post, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when getting a registration token for a repo."""
        mock_install_token.return_value = "INSTALL_TOKEN"
        mock_response = MagicMock()
        mock_response.json.return_value = {"token": "REG_TOKEN"}
        mock_post.return_value = mock_response

        client = GitHubClient()

        with caplog.at_level(logging.INFO, logger="app.clients.github_client"):
            client.get_registration_token(
                repo_name="owner/repo", delivery_id="gh-repo-delivery-001"
            )

        assert any(
            "gh-repo-delivery-001" in r.message for r in caplog.records
        ), "delivery_id not found in log for repo registration token"

    @patch("app.clients.github_client.requests.post")
    @patch.object(GitHubClient, "get_installation_access_token")
    def test_registration_token_for_org_logs_delivery_id(
        self, mock_install_token, mock_post, mock_env_vars, caplog
    ):
        """Test that delivery_id is logged when getting a registration token for an org."""
        mock_install_token.return_value = "INSTALL_TOKEN"
        mock_response = MagicMock()
        mock_response.json.return_value = {"token": "ORG_TOKEN"}
        mock_post.return_value = mock_response

        client = GitHubClient()

        with caplog.at_level(logging.INFO, logger="app.clients.github_client"):
            client.get_registration_token(
                org_name="my-org", delivery_id="gh-org-delivery-001"
            )

        assert any(
            "gh-org-delivery-001" in r.message for r in caplog.records
        ), "delivery_id not found in log for org registration token"


class TestRunnerManagement:
    @patch('app.clients.github_client.requests.get')
    @patch.object(GitHubClient, 'get_installation_access_token', return_value='tok')
    def test_list_runners_paginates(self, mock_tok, mock_get, mock_env_vars):
        from unittest.mock import MagicMock
        page1 = MagicMock()
        page1.json.return_value = {'runners': [{'id': i, 'name': f'gcp-runner-{i}'} for i in range(100)]}
        page2 = MagicMock()
        page2.json.return_value = {'runners': [{'id': 100, 'name': 'gcp-runner-100'}]}
        mock_get.side_effect = [page1, page2]

        client = GitHubClient()
        runners = client.list_runners(org_name='DeveloEngineering')
        assert len(runners) == 101
        assert mock_get.call_count == 2

    @patch('app.clients.github_client.requests.delete')
    @patch.object(GitHubClient, 'get_installation_access_token', return_value='tok')
    def test_delete_runner_success(self, mock_tok, mock_del, mock_env_vars):
        from unittest.mock import MagicMock
        resp = MagicMock(); resp.status_code = 204
        mock_del.return_value = resp
        client = GitHubClient()
        assert client.delete_runner(5, org_name='DeveloEngineering') is True

    @patch('app.clients.github_client.requests.delete')
    @patch.object(GitHubClient, 'get_installation_access_token', return_value='tok')
    def test_delete_runner_busy_returns_false(self, mock_tok, mock_del, mock_env_vars):
        from unittest.mock import MagicMock
        resp = MagicMock(); resp.status_code = 422; resp.text = 'busy'
        mock_del.return_value = resp
        client = GitHubClient()
        assert client.delete_runner(5, org_name='DeveloEngineering') is False

    @patch.object(GitHubClient, 'get_installation_access_token', return_value='tok')
    def test_delete_runner_requires_scope(self, mock_tok, mock_env_vars):
        client = GitHubClient()
        with pytest.raises(ValueError):
            client.delete_runner(5)
