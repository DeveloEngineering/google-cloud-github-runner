"""
GitHub Client for authenticating and interacting with the GitHub API.
"""
import os
import time
import threading
import jwt
import requests
import logging

REQUEST_TIMEOUT = 30  # seconds

# Refresh the cached installation access token this many seconds before its
# real expiry. GitHub installation tokens live for ~1 hour; refreshing at
# 50 minutes leaves a 10-minute safety margin.
TOKEN_REFRESH_BUFFER_SECONDS = 600

logger = logging.getLogger(__name__)

# Process-wide cache for installation access tokens. Each Cloud Run instance
# maintains its own cache (no cross-instance sharing needed — at one refresh
# per hour per instance the GitHub API cost is negligible). The lock prevents
# the gunicorn thread pool from racing to mint duplicate tokens during a
# webhook burst.
_token_cache_lock = threading.Lock()
_token_cache = {
    'token': None,
    'expires_at': 0.0,  # epoch seconds
}


class GitHubClient:
    """Client for authenticated interactions with the GitHub API as a GitHub App."""

    def __init__(self):
        """Initialize GitHubClient with environment configuration."""
        self.app_id = os.environ.get('GITHUB_APP_ID')
        self.installation_id = os.environ.get('GITHUB_INSTALLATION_ID')
        self.private_key = os.environ.get('GITHUB_PRIVATE_KEY')
        self.private_key_path = os.environ.get('GITHUB_PRIVATE_KEY_PATH')
        self.project_id = os.environ.get('GOOGLE_CLOUD_PROJECT')

        if not all([self.app_id, self.installation_id]) or not (self.private_key_path or self.private_key):
            logger.warning("GitHub App configuration missing.")

    def _get_private_key(self):
        """
        Retrieve the GitHub App private key.

        Returns:
            str: The private key content.

        Raises:
            ValueError: If no private key source is configured.
        """
        # Retrun environment variable
        if self.private_key:
            return self.private_key
        # Return file content
        elif self.private_key_path:
            with open(self.private_key_path, 'r') as f:
                return f.read()
        else:
            raise ValueError("No private key source configured.")

    def _generate_jwt(self):
        """Generates a JWT for GitHub App authentication."""
        try:
            private_key = self._get_private_key()

            payload = {
                'iat': int(time.time()),
                'exp': int(time.time()) + (10 * 60),
                'iss': self.app_id
            }

            encoded_jwt = jwt.encode(payload, private_key, algorithm='RS256')
            return encoded_jwt
        except Exception as e:
            logger.error(f"Error generating JWT: {e}")
            raise

    def _request_new_installation_token(self):
        """Mints a fresh installation token via the GitHub App API."""
        jwt_token = self._generate_jwt()
        headers = {
            'Authorization': f'Bearer {jwt_token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'
        }
        url = f'https://api.github.com/app/installations/{self.installation_id}/access_tokens'

        response = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        token = payload['token']
        # GitHub returns expires_at as an ISO-8601 string. Parse to epoch
        # seconds; fall back to "now + 55 min" if parsing fails.
        expires_at = payload.get('expires_at')
        try:
            expires_at_epoch = _parse_iso8601_to_epoch(expires_at) if expires_at else None
        except Exception:
            expires_at_epoch = None
        if not expires_at_epoch:
            expires_at_epoch = time.time() + 55 * 60
        return token, expires_at_epoch

    def get_installation_access_token(self):
        """
        Obtains an installation access token, reusing a cached one when valid.

        GitHub installation tokens live for ~1 hour. The previous behaviour
        minted a fresh token (JWT + API round-trip, ~200-500 ms) on every
        webhook, which under burst dominated webhook latency and pushed p99
        past GitHub's 10s delivery timeout. With process-wide caching the
        token is reused across all webhooks handled by a Cloud Run instance,
        reducing the per-webhook GitHub API call count from 2 to 1.
        """
        # https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
        with _token_cache_lock:
            now = time.time()
            cached = _token_cache['token']
            cached_expires = _token_cache['expires_at']
            if cached and now < (cached_expires - TOKEN_REFRESH_BUFFER_SECONDS):
                return cached

            # Cache miss or near-expiry — mint a fresh token. Hold the lock
            # so concurrent webhooks in the same instance share the refresh
            # instead of stampeding the GitHub API.
            token, expires_at_epoch = self._request_new_installation_token()
            _token_cache['token'] = token
            _token_cache['expires_at'] = expires_at_epoch
            logger.info(
                "Refreshed installation access token (expires in %ds)",
                int(expires_at_epoch - now),
            )
            return token

    def get_registration_token(self, org_name=None, repo_name=None, delivery_id=None):
        """Gets a runner registration token."""
        # https://docs.github.com/en/rest/actions/self-hosted-runners
        token = self.get_installation_access_token()
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'
        }
        if org_name:
            # GitHub Docs: https://t.ly/dAyGK
            url = f"https://api.github.com/orgs/{org_name}/actions/runners/registration-token"
            logger.info(
                "Create registration token for organization: %s, delivery_id: %s",
                org_name,
                delivery_id,
            )
        elif repo_name:
            # GitHub Docs: https://t.ly/n0w2a
            url = f"https://api.github.com/repos/{repo_name}/actions/runners/registration-token"
            logger.info(
                "Create registration token for repository: %s, delivery_id: %s",
                repo_name,
                delivery_id,
            )
        else:
            raise ValueError("Either org_name or repo_name must be provided")

        response = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()['token']


def _parse_iso8601_to_epoch(s):
    """Parse a GitHub-style ISO-8601 timestamp (e.g. '2026-05-29T18:15:00Z') to epoch seconds."""
    from datetime import datetime, timezone
    # Python 3.11+ fromisoformat handles trailing 'Z'; pre-3.11 needs munging.
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _reset_token_cache_for_tests():
    """Test helper: clear the module-level token cache between tests."""
    with _token_cache_lock:
        _token_cache['token'] = None
        _token_cache['expires_at'] = 0.0
