"""
Internal endpoint that processes workflow_job tasks dispatched by Cloud Tasks.

The webhook handler at /webhook returns 200 to GitHub the moment a task is
enqueued. Cloud Tasks then delivers that task to this endpoint, with an OIDC
token signed by the tasks-invoker service account. Because there is no
GitHub-side timeout on this call, the handler can do as much work as needed —
GitHub API calls, GCE VM creation, retries — without risk of dropped events.

Authentication: the endpoint is public at the Cloud Run ingress layer
(``invoker_iam_disabled = true``) so that GitHub's webhook can also reach the
service. App-layer auth verifies the OIDC token signed by the configured
tasks-invoker service account.
"""
import logging
from flask import Blueprint, request, jsonify

from app.services import WebhookService
from app.utils.auth import verify_scheduler_oidc_token
from app import limiter

logger = logging.getLogger(__name__)

internal_bp = Blueprint('internal', __name__)


@internal_bp.route('/internal/process-workflow-job', methods=['POST'])
@limiter.limit("5000 per hour")
def process_workflow_job():
    """Consume one Cloud Tasks delivery and process the underlying workflow_job."""
    if not verify_scheduler_oidc_token(
        request.headers.get('Authorization'),
        env_var='INTERNAL_OIDC_SERVICE_ACCOUNT_EMAIL',
    ):
        return jsonify({'status': 'forbidden'}), 403

    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}
    payload = body.get('payload') or {}
    delivery_id = body.get('delivery_id')
    source = body.get('source', 'unknown')

    if not isinstance(payload, dict) or not payload:
        logger.warning(
            "process_workflow_job received empty/invalid payload (source=%s, delivery_id=%s)",
            source, delivery_id,
        )
        # 400 makes Cloud Tasks drop the task (no retry) — correct for bad data.
        return jsonify({'status': 'error', 'message': 'invalid payload'}), 400

    try:
        result = WebhookService().handle_workflow_job(payload, delivery_id=delivery_id)
        logger.info(
            "process_workflow_job done source=%s action=%s runner_name=%s delivery_id=%s",
            source, result.get('action'), result.get('runner_name'), delivery_id,
        )
        return jsonify({'status': 'success', **result}), 200
    except ValueError as e:
        logger.warning(
            "process_workflow_job validation error: %s (source=%s, delivery_id=%s)",
            e, source, delivery_id,
        )
        # 400: don't retry — the payload is malformed.
        return jsonify({'status': 'error', 'message': 'invalid payload'}), 400
    except Exception as e:
        logger.error(
            "process_workflow_job failed: %s (source=%s, delivery_id=%s)",
            e, source, delivery_id,
        )
        # 5xx: Cloud Tasks will retry per the queue's retry_config.
        return jsonify({'status': 'error', 'message': 'processing failed'}), 500
