"""
Route for the reconciler — Cloud Scheduler triggers this every 5 min to find
workflow_jobs that are stuck queued because their webhook never landed.
"""
import logging
from flask import Blueprint, request, jsonify

from app.services import ReconcilerService
from app.utils.auth import verify_scheduler_oidc_token
from app import limiter

logger = logging.getLogger(__name__)

reconcile_bp = Blueprint('reconcile', __name__)


@reconcile_bp.route('/reconcile', methods=['POST'])
@limiter.limit("60 per hour")
def reconcile():
    """Run one reconciliation pass."""
    # /reconcile is invoked by Cloud Scheduler, which signs with the sweeper
    # SA. /internal/process-workflow-job is invoked by Cloud Tasks, which
    # signs with the tasks-invoker SA — different env var.
    if not verify_scheduler_oidc_token(
        request.headers.get('Authorization'),
        env_var='SWEEP_OIDC_SERVICE_ACCOUNT_EMAIL',
    ):
        return jsonify({'status': 'forbidden'}), 403

    # Compute the internal-processing target URL the same way /webhook does,
    # so reconciler-enqueued tasks land at /internal/process-workflow-job on
    # this very service.
    root = request.url_root.rstrip('/')
    if root.startswith('http://'):
        root = 'https://' + root[len('http://'):]
    target_url = f"{root}/internal/process-workflow-job"

    try:
        result = ReconcilerService().reconcile(target_url=target_url)
        return jsonify({'status': 'ok', **result}), 200
    except Exception as e:
        logger.error("Reconcile failed: %s", e)
        return jsonify({'status': 'error', 'message': 'reconcile failed'}), 500
