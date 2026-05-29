"""
GitHub webhook receiver.

This route does the minimum required to acknowledge the delivery within
GitHub's 10-second timeout: verify the signature, parse the body, and
enqueue a Cloud Tasks task. The real work (GitHub API calls + GCE VM
creation) runs in /internal/process-workflow-job, invoked by Cloud Tasks
from the queue created in terraform (cloud-tasks.tf).

Decoupling like this means GitHub can never time out on us regardless of
how slow the downstream GCE API gets — the only synchronous work here is
the HMAC check and JSON parse, which together take milliseconds.
"""
import logging
from flask import Blueprint, request, jsonify
from app.clients import CloudTasksClient
from app.utils.security import verify_github_signature
from app import limiter

logger = logging.getLogger(__name__)

webhook_bp = Blueprint('webhook', __name__)


@webhook_bp.route('/webhook', methods=['POST'])
@limiter.limit("1000 per hour")  # Higher limit for high-traffic webhook endpoint
def webhook():
    """Receive a GitHub webhook event and enqueue async processing."""
    # https://docs.github.com/en/webhooks/webhook-events-and-payloads
    event_type = request.headers.get('X-GitHub-Event')
    # https://docs.github.com/en/webhooks/webhook-events-and-payloads#delivery-headers
    delivery_id = request.headers.get('X-GitHub-Delivery')

    if not event_type or not isinstance(event_type, str):
        logger.error("Missing or invalid X-GitHub-Event header")
        return jsonify({'status': 'error', 'message': 'Invalid event type'}), 400

    logger.info("Received webhook event: %s, delivery_id: %s", event_type, delivery_id)

    if event_type == 'ping':
        return jsonify({'status': 'success'}), 200

    signature = request.headers.get('X-Hub-Signature-256')
    if not verify_github_signature(request.data, signature):
        logger.error(
            "GitHub webhook signature not successfully verified! "
            "Ignoring webhook event. delivery_id: %s",
            delivery_id,
        )
        return jsonify({'status': 'forbidden', 'message': 'Invalid signature'}), 403

    try:
        payload = request.json
        if not payload:
            logger.error("Empty or invalid JSON payload, delivery_id: %s", delivery_id)
            return jsonify({'status': 'error', 'message': 'Invalid JSON payload'}), 400
    except Exception as e:
        logger.error(
            "Failed to parse JSON payload: %s, delivery_id: %s", str(e), delivery_id
        )
        return jsonify({'status': 'error', 'message': 'Invalid JSON'}), 400

    if event_type != 'workflow_job':
        logger.warning(
            "Received unknown event type: %s, delivery_id: %s",
            event_type, delivery_id,
        )
        return jsonify({'status': 'ignored'}), 200

    # Enqueue async processing. The handler at /internal/process-workflow-job
    # receives this task and does the real work.
    try:
        target_url = _derive_internal_target_url(request)
        CloudTasksClient().enqueue_workflow_job(
            target_url=target_url,
            payload=payload,
            delivery_id=delivery_id,
            source='webhook',
        )
    except Exception as e:
        # If we cannot enqueue, return 500 so GitHub records the failure.
        # GitHub does not auto-retry workflow_job webhooks, but the reconciler
        # at /reconcile will pick up any dropped jobs within ~5 minutes.
        logger.error(
            "[Webhook] Failed to enqueue task: %s, delivery_id: %s",
            str(e), delivery_id,
        )
        return jsonify({'status': 'error', 'message': 'enqueue failed'}), 500

    return jsonify({'status': 'accepted', 'delivery_id': delivery_id}), 202


def _derive_internal_target_url(req) -> str:
    """
    Build the absolute URL of /internal/process-workflow-job from the
    inbound request. We can't pass this as an env var because it would create
    a terraform circular dependency (cloud-run module env referencing its
    own service_uri output).
    """
    # request.url_root is e.g. "https://github-runners-manager-uc1-...run.app/"
    root = req.url_root.rstrip('/')
    # Force https — GitHub always delivers webhooks over https, but defensive.
    if root.startswith('http://'):
        root = 'https://' + root[len('http://'):]
    return f"{root}/internal/process-workflow-job"
