"""
Reconciler — defense in depth against dropped or timed-out webhooks.

GitHub does not auto-retry workflow_job webhook deliveries. Under burst
fan-out a small fraction (~0.1 %) of deliveries are either dropped on
GitHub's side or terminated at the 10-second timeout, leaving a workflow_job
permanently in ``queued`` state with no runner ever attached.

The reconciler runs on a Cloud Scheduler cron (~every 5 min, see
``scheduler.tf``). For each repository in the GitHub App installation it
enumerates currently-queued and in-progress workflow runs, filters down to
``status: queued`` jobs that:

  * are older than ``GITHUB_RECONCILER_MIN_JOB_AGE_SECONDS`` (avoids racing
    in-flight webhook deliveries that may still arrive on their own);
  * carry a ``gcp-*`` (or ``dependabot``) runner label;
  * do not yet have a runner VM tagged with their ``gha-job-id`` label.

For each surviving job the reconciler synthesises the workflow_job payload
that the missing webhook *would* have carried and enqueues a Cloud Tasks
task identical to one a webhook would have produced — same downstream code
path, full idempotency from the existing ``gha-job-id`` VM label.
"""
import logging
import os
import time
from datetime import datetime, timezone
from typing import Iterable, List, Optional

import requests

from app.clients import CloudTasksClient, GCloudClient, GitHubClient

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # seconds, matches github_client
DEFAULT_MIN_AGE_SECONDS = 120


def _parse_iso8601_to_epoch(s: str) -> Optional[float]:
    """Parse an ISO-8601 timestamp ('2026-05-29T18:15:00Z') to epoch seconds."""
    if not s:
        return None
    try:
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


class ReconcilerService:
    """Finds stuck workflow_jobs and re-enqueues them through Cloud Tasks."""

    def __init__(self):
        self.github_client = GitHubClient()
        self.gcloud_client = GCloudClient()
        self.tasks_client = CloudTasksClient()
        try:
            self.min_age_seconds = int(
                os.environ.get(
                    'GITHUB_RECONCILER_MIN_JOB_AGE_SECONDS', DEFAULT_MIN_AGE_SECONDS
                )
            )
        except ValueError:
            self.min_age_seconds = DEFAULT_MIN_AGE_SECONDS

    def reconcile(self, target_url: str) -> dict:
        """Run one reconciliation pass.

        Args:
            target_url: absolute URL of /internal/process-workflow-job; tasks
                are enqueued to dispatch here. Provided by the caller because
                the URL is request-derived (see ``app.routes.webhook``).

        Returns:
            dict summary with keys ``repos_scanned``, ``runs_scanned``,
            ``jobs_inspected``, ``jobs_enqueued``, ``jobs_skipped_young``,
            ``jobs_skipped_no_label``, ``jobs_skipped_vm_exists``, ``errors``.
        """
        token = self.github_client.get_installation_access_token()
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }

        result = {
            'repos_scanned': 0,
            'runs_scanned': 0,
            'jobs_inspected': 0,
            'jobs_enqueued': 0,
            'jobs_skipped_young': 0,
            'jobs_skipped_no_label': 0,
            'jobs_skipped_vm_exists': 0,
            'errors': 0,
            'enqueued_job_ids': [],
        }

        now = time.time()

        for repo in self._list_installation_repos(headers):
            result['repos_scanned'] += 1
            owner = repo['owner']['login']
            name = repo['name']
            owner_type = repo['owner'].get('type', 'User')

            for run in self._list_active_runs(headers, owner, name):
                result['runs_scanned'] += 1
                try:
                    jobs = self._list_run_jobs(headers, owner, name, run['id'])
                except Exception as e:
                    logger.warning("Failed to list jobs for run %s: %s", run['id'], e)
                    result['errors'] += 1
                    continue

                for job in jobs:
                    result['jobs_inspected'] += 1
                    decision = self._decide(job, now)
                    if decision == 'young':
                        result['jobs_skipped_young'] += 1
                        continue
                    if decision == 'no_label':
                        result['jobs_skipped_no_label'] += 1
                        continue
                    if decision == 'vm_exists':
                        result['jobs_skipped_vm_exists'] += 1
                        continue
                    # decision == 'enqueue'
                    try:
                        self._enqueue_synthetic_workflow_job(
                            target_url=target_url,
                            job=job,
                            repo=repo,
                            owner_type=owner_type,
                        )
                        result['jobs_enqueued'] += 1
                        result['enqueued_job_ids'].append(job.get('id'))
                    except Exception as e:
                        logger.error(
                            "Failed to enqueue reconciler task for job %s: %s",
                            job.get('id'), e,
                        )
                        result['errors'] += 1

        logger.info("Reconcile pass: %s", result)
        return result

    # ------------------------------------------------------------------
    # GitHub API helpers
    # ------------------------------------------------------------------

    def _list_installation_repos(self, headers) -> Iterable[dict]:
        """List repos the GitHub App is installed on. One page (max 100)."""
        url = 'https://api.github.com/installation/repositories?per_page=100'
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json().get('repositories', [])

    def _list_active_runs(self, headers, owner, name) -> Iterable[dict]:
        """List queued + in_progress workflow runs for a repo (most recent first)."""
        for status in ('queued', 'in_progress'):
            url = (
                f'https://api.github.com/repos/{owner}/{name}/actions/runs'
                f'?status={status}&per_page=30'
            )
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            for run in r.json().get('workflow_runs', []):
                yield run

    def _list_run_jobs(self, headers, owner, name, run_id) -> List[dict]:
        url = (
            f'https://api.github.com/repos/{owner}/{name}/actions/runs/'
            f'{run_id}/jobs?per_page=100'
        )
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json().get('jobs', [])

    # ------------------------------------------------------------------
    # Decision + synthesis
    # ------------------------------------------------------------------

    def _decide(self, job: dict, now: float) -> str:
        """Decide whether to enqueue, skip, or pass on a single job."""
        if job.get('status') != 'queued':
            return 'no_label'  # not queued — implicitly not our problem
        labels = job.get('labels') or []
        if not any(
            (lbl.startswith('gcp-') or lbl.lower() == 'dependabot') for lbl in labels
        ):
            return 'no_label'

        started = _parse_iso8601_to_epoch(job.get('started_at') or '')
        # GitHub's `started_at` for a queued job is the time the job entered
        # the queue. If we can't parse it, treat as too-young to be safe.
        if started is None or (now - started) < self.min_age_seconds:
            return 'young'

        # A VM tagged with this job_id already exists? Then a webhook (or a
        # prior reconcile pass) already created it; do nothing.
        if self.gcloud_client.find_runner_by_job_id(job.get('id')) is not None:
            return 'vm_exists'

        return 'enqueue'

    def _enqueue_synthetic_workflow_job(self, target_url, job, repo, owner_type):
        """Synthesise the workflow_job payload a missing webhook would have carried."""
        owner = repo['owner']['login']
        payload = {
            'action': 'queued',
            'workflow_job': {
                'id': job.get('id'),
                'labels': job.get('labels') or [],
            },
            'repository': {
                'html_url': repo.get('html_url') or f'https://github.com/{owner}/{repo["name"]}',
                'full_name': repo.get('full_name') or f'{owner}/{repo["name"]}',
                'owner': {
                    'html_url': repo['owner'].get('html_url')
                    or f'https://github.com/{owner}',
                },
            },
        }
        if owner_type == 'Organization':
            payload['organization'] = {'login': owner}

        self.tasks_client.enqueue_workflow_job(
            target_url=target_url,
            payload=payload,
            delivery_id=f'reconciler-{job.get("id")}',
            source='reconciler',
        )
