"""
OIDC token verification for Cloud Scheduler -> Cloud Run requests.

The Cloud Run service is publicly accessible (invoker_iam_disabled=true) so that
GitHub webhooks can reach it without OIDC. Internal triggers (the orphan
sweeper) are authenticated at the application layer instead: Cloud Scheduler
signs an OIDC token with its service account, and this module verifies the
token signature with Google's public keys and checks that the `email` claim
matches an expected service account email.
"""
import logging
import os
from typing import Optional

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = logging.getLogger(__name__)

_GOOGLE_REQUEST = google_requests.Request()


def verify_scheduler_oidc_token(authorization_header: Optional[str]) -> bool:
    """Verify an OIDC bearer token issued to the sweeper service account.

    Returns True only if the token is signed by Google and its `email` claim
    matches the SWEEP_OIDC_SERVICE_ACCOUNT_EMAIL env var. Audience is not
    checked because the Cloud Run service URL is not stable across redeploys
    and the email claim alone is sufficient: only Cloud Scheduler running as
    this service account can mint a token with this email.
    """
    expected_email = os.environ.get('SWEEP_OIDC_SERVICE_ACCOUNT_EMAIL', '').strip()
    if not expected_email:
        logger.error("SWEEP_OIDC_SERVICE_ACCOUNT_EMAIL not configured; refusing /sweep request")
        return False

    if not authorization_header or not authorization_header.lower().startswith('bearer '):
        logger.warning("Missing or malformed Authorization header on /sweep")
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
            token_email,
            expected_email,
            email_verified,
        )
        return False

    return True
