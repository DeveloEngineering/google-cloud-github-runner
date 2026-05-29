"""
OIDC token verification for internal Cloud Run endpoints.

The Cloud Run service is publicly accessible (invoker_iam_disabled=true) so
that GitHub webhooks can reach it without OIDC. Internal triggers (the orphan
sweeper at /sweep, the reconciler at /reconcile, and the Cloud Tasks dispatch
at /internal/process-workflow-job) are authenticated at the application layer
instead: the upstream signs an OIDC token with a known service account, and
this module verifies the token signature with Google's public keys and checks
that the `email` claim matches the expected service account email.
"""
import logging
import os
from typing import Optional

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = logging.getLogger(__name__)

_GOOGLE_REQUEST = google_requests.Request()


def verify_scheduler_oidc_token(
    authorization_header: Optional[str],
    env_var: str = 'SWEEP_OIDC_SERVICE_ACCOUNT_EMAIL',
) -> bool:
    """Verify an OIDC bearer token whose email claim must match env_var.

    Returns True only if the token is signed by Google and its `email` claim
    matches the configured expected service account email. Audience is not
    checked because the Cloud Run service URL is not stable across redeploys
    and the email claim alone is sufficient: only the SA holder can mint a
    token signed as that SA.

    Args:
        authorization_header: Raw value of the Authorization header
            (e.g. ``"Bearer ey..."``).
        env_var: Name of the env var holding the expected SA email. Defaults
            to ``SWEEP_OIDC_SERVICE_ACCOUNT_EMAIL`` for backward compatibility
            with the /sweep route; ``INTERNAL_OIDC_SERVICE_ACCOUNT_EMAIL`` is
            used by the Cloud Tasks consumer and the reconciler.
    """
    expected_email = os.environ.get(env_var, '').strip()
    if not expected_email:
        logger.error("%s not configured; refusing request", env_var)
        return False

    if not authorization_header or not authorization_header.lower().startswith('bearer '):
        logger.warning("Missing or malformed Authorization header")
        return False

    token = authorization_header.split(' ', 1)[1].strip()
    if not token:
        return False

    try:
        # audience=None skips audience verification; we rely on email claim.
        claims = id_token.verify_token(token, _GOOGLE_REQUEST)
    except Exception as e:
        logger.warning("OIDC token verification failed: %s", e)
        return False

    token_email = claims.get('email', '')
    email_verified = claims.get('email_verified', False)
    if not email_verified or token_email != expected_email:
        logger.warning(
            "OIDC token email mismatch (got %s, expected %s, verified=%s)",
            token_email, expected_email, email_verified,
        )
        return False

    return True
