"""
Route for the orphan-runner sweeper.

Triggered by Cloud Scheduler on a fixed cadence. Authenticated by verifying
the OIDC token's email claim against SWEEP_OIDC_SERVICE_ACCOUNT_EMAIL.
"""
import logging
from flask import Blueprint, request, jsonify

from app.services import SweepService
from app.utils.auth import verify_scheduler_oidc_token
from app import limiter

logger = logging.getLogger(__name__)

sweep_bp = Blueprint('sweep', __name__)


@sweep_bp.route('/sweep', methods=['POST'])
@limiter.limit("60 per hour")
def sweep():
    """Run one pass of the orphan-runner sweeper."""
    if not verify_scheduler_oidc_token(request.headers.get('Authorization')):
        return jsonify({'status': 'forbidden'}), 403

    try:
        result = SweepService().sweep()
        return jsonify({'status': 'ok', **result}), 200
    except Exception as e:
        logger.error("Sweep failed: %s", e)
        return jsonify({'status': 'error', 'message': 'sweep failed'}), 500
