"""
Reconciler — capacity-aware safety net for the webhook path.

The PRIMARY mechanism for spinning up runners is the webhook path
(/webhook -> Cloud Tasks -> /internal -> instances.insert). The reconciler
exists ONLY to recover the rare job whose ``workflow_job.queued`` webhook was
never delivered or never produced a VM. In steady state it should enqueue
zero jobs.

Capacity-aware design (why it's not per-job-id)
-----------------------------------------------
GitHub dispatches a queued job to ANY idle runner that matches its labels —
not to the specific VM we created "for" that job — and our runners are
ephemeral (deleted on job completion). So a job sitting in ``queued`` during a
fan-out usually just means "waiting its turn for a runner", NOT "my webhook
was dropped". A naive per-job check ("is there a VM tagged with THIS job_id?")
can't tell those apart and re-creates VMs for jobs that are simply waiting,
over-provisioning the fleet every pass.

Instead the reconciler reasons about supply vs demand per runner label:

  demand(label)  = # of queued, old-enough, gcp-labelled jobs needing `label`
  supply(label)  = # of live VMs (PROVISIONING/STAGING/RUNNING) tagged
                   gha-runner=`label` — i.e. runners already booting or busy
  deficit(label) = max(0, demand - supply)

It enqueues exactly ``deficit`` VM-creates for the oldest queued jobs of that
label. Because booting VMs count as supply, VMs created by the webhook path
(or a previous reconciler pass) suppress further creation — so the reconciler
only acts when real demand genuinely exceeds the live fleet, which is the
signature of an actually-missed webhook.

The reconciler runs on a Cloud Scheduler cron (see ``scheduler.tf``). It only
considers jobs older than ``GITHUB_RECONCILER_MIN_JOB_AGE_SECONDS`` so it does
not race in-flight webhook deliveries that may still arrive on their own.

Circuit breaker
---------------
Supply/demand accounting assumes a created VM will eventually do work. When
that assumption breaks — GitHub deprecates the runner version baked into the
image, the image is broken, registration tokens are rejected — every VM boots,
dies, and drops out of the supply count, so the reconciler sees a permanent
deficit and recreates the fleet every pass forever. That is not a recovery, it
is a spend loop: on 2026-08-10 it ran ~3 hours at ~40 VMs per 2 minutes and
executed zero jobs.

The breaker refuses to create when both of these hold:

  * NO gcp-labelled job anywhere is ``in_progress`` — nothing is working, so
    this is systemic rather than a normal queue waiting its turn; and
  * at least ``GITHUB_RECONCILER_BREAKER_MIN_DOA`` runner VMs are
    dead-on-arrival — they stopped within
    ``GITHUB_RECONCILER_BREAKER_MAX_LIFETIME_SECONDS`` of being created, i.e.
    they never lived long enough to have run a job.

Requiring both keeps a cold burst (demand, no supply, nothing in progress yet,
no corpses) from tripping it, and keeps a fleet of fast short jobs (corpses,
but jobs demonstrably progressing) from tripping it either. Set
``GITHUB_RECONCILER_BREAKER_MIN_DOA=0`` to disable.
"""
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable, List, Optional

import requests

from app.clients import CloudTasksClient, GCloudClient, GitHubClient

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # seconds, matches github_client
DEFAULT_MIN_AGE_SECONDS = 120
DEFAULT_INFLIGHT_WINDOW_SECONDS = 180
DEFAULT_MAX_CREATES_PER_PASS = 100
# Runner VMs that stopped within this many seconds of creation never lived long
# enough to have run a job (boot alone is ~20-40s).
DEFAULT_BREAKER_MAX_LIFETIME_SECONDS = 180
# How many dead-on-arrival VMs, with nothing in progress, before we stop creating.
DEFAULT_BREAKER_MIN_DOA = 5


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


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
        self.min_age_seconds = _int_env(
            'GITHUB_RECONCILER_MIN_JOB_AGE_SECONDS', DEFAULT_MIN_AGE_SECONDS
        )
        # VMs created within this window count as in-flight supply, so the
        # reconciler does not re-create the runners it spun up last pass.
        self.inflight_window_seconds = _int_env(
            'GITHUB_RECONCILER_INFLIGHT_WINDOW_SECONDS', DEFAULT_INFLIGHT_WINDOW_SECONDS
        )
        # Hard cap on how many VMs the reconciler will create per label per
        # pass. A backstop against runaway creation if the supply count is
        # ever wrong; the webhook path is the primary creator anyway.
        self.max_creates_per_pass = _int_env(
            'GITHUB_RECONCILER_MAX_CREATES_PER_PASS', DEFAULT_MAX_CREATES_PER_PASS
        )
        # Circuit breaker: stop creating when runners are provably unable to
        # work. See the module docstring. 0 disables the breaker entirely.
        self.breaker_min_doa = _int_env(
            'GITHUB_RECONCILER_BREAKER_MIN_DOA', DEFAULT_BREAKER_MIN_DOA
        )
        self.breaker_max_lifetime_seconds = _int_env(
            'GITHUB_RECONCILER_BREAKER_MAX_LIFETIME_SECONDS',
            DEFAULT_BREAKER_MAX_LIFETIME_SECONDS,
        )

    def reconcile(self, target_url: str) -> dict:
        """Run one capacity-aware reconciliation pass.

        Phase 1: enumerate all eligible (queued + old + gcp-labelled) jobs
                 across every repo in the installation, grouped by runner label.
        Phase 2: count live runner VMs per label (booting VMs count as supply).
        Phase 3: for each label, enqueue VM-creates for only the *deficit*
                 (demand - supply) oldest jobs — never one-per-stuck-job.

        Args:
            target_url: absolute URL of /internal/process-workflow-job; tasks
                are enqueued to dispatch here. Provided by the caller because
                the URL is request-derived (see ``app.routes.webhook``).

        Returns:
            dict summary including per-label demand/supply/deficit.
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
            'jobs_skipped_have_capacity': 0,
            'jobs_skipped_breaker_open': 0,
            'errors': 0,
            'jobs_in_progress': 0,
            'circuit_breaker': 'closed',
            'by_label': {},
            'enqueued_job_ids': [],
        }

        now = time.time()

        # ── Phase 1: collect eligible queued jobs, grouped by runner label ──
        # Each entry is (job, repo, owner_type) so we can synthesise an
        # accurate payload (registration token needs repo/org context).
        demand_by_label = defaultdict(list)

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
                    # Any gcp-labelled job actually running proves the fleet can
                    # work; the circuit breaker below keys off this.
                    if job.get('status') == 'in_progress' and self._gcp_label(job) is not None:
                        result['jobs_in_progress'] += 1
                    eligibility = self._classify(job, now)
                    if eligibility == 'young':
                        result['jobs_skipped_young'] += 1
                    elif eligibility == 'no_label':
                        result['jobs_skipped_no_label'] += 1
                    elif eligibility == 'eligible':
                        label = self._gcp_label(job)
                        demand_by_label[label].append((job, repo, owner_type))

        # ── Phase 2: supply per label (live + in-flight VMs, one GCE call) ──
        try:
            supply_by_label = self.gcloud_client.count_supply_by_label(
                inflight_window_seconds=self.inflight_window_seconds
            )
        except Exception as e:
            logger.error("Reconciler could not count supply: %s", e)
            result['errors'] += 1
            supply_by_label = {}

        # ── Circuit breaker: are the runners we create able to work at all? ──
        breaker_open = self._breaker_open(
            demand_by_label=demand_by_label,
            jobs_in_progress=result['jobs_in_progress'],
            result=result,
        )

        # ── Phase 3: enqueue only the deficit, oldest-first, capped ──
        for label, items in demand_by_label.items():
            demand = len(items)
            supply = supply_by_label.get(label, 0)
            deficit = max(0, demand - supply)
            # Cap how many we create this pass; the rest recover next pass once
            # this batch is counted as in-flight supply.
            to_create = min(deficit, self.max_creates_per_pass)
            if breaker_open:
                # Creating more VMs cannot help — they would die the same way.
                # Report the real deficit but create nothing.
                to_create = 0
            result['by_label'][label] = {
                'demand': demand, 'supply': supply,
                'deficit': deficit, 'creating': to_create,
            }
            if to_create == 0:
                # Distinguish "the fleet already covers this" from "we refused
                # to create" — they need very different operator responses.
                if breaker_open:
                    result['jobs_skipped_breaker_open'] += demand
                else:
                    result['jobs_skipped_have_capacity'] += demand
                continue

            # Oldest jobs first — bounds worst-case wait for any single job.
            items.sort(key=lambda t: t[0].get('created_at') or '')
            result['jobs_skipped_have_capacity'] += (demand - to_create)
            for job, repo, owner_type in items[:to_create]:
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
    # Circuit breaker
    # ------------------------------------------------------------------

    def _breaker_open(self, demand_by_label, jobs_in_progress, result) -> bool:
        """Decide whether to refuse creating runners this pass.

        Opens only when the fleet is provably unable to work: nothing is
        ``in_progress`` AND enough runner VMs died before they could possibly
        have run a job. See the module docstring for why both conditions are
        required. Records the reason in ``result['circuit_breaker']``.
        """
        if self.breaker_min_doa <= 0:
            result['circuit_breaker'] = 'disabled'
            return False
        if not demand_by_label:
            return False
        if jobs_in_progress > 0:
            # Jobs are running, so runners demonstrably work. Any queue here is
            # ordinary backlog, not a systemic failure.
            return False

        try:
            doa = self.gcloud_client.count_dead_on_arrival(
                max_lifetime_seconds=self.breaker_max_lifetime_seconds
            )
        except Exception as e:
            # Fail closed-as-in-keep-working: if we cannot measure, behave as
            # before rather than stalling the recovery path.
            logger.warning("Circuit breaker could not count dead-on-arrival VMs: %s", e)
            result['errors'] += 1
            return False

        result['dead_on_arrival'] = doa
        if doa < self.breaker_min_doa:
            return False

        result['circuit_breaker'] = 'open'
        logger.error(
            "Reconciler circuit breaker OPEN: %d runner VMs stopped within %ds of "
            "creation and no gcp-labelled job is in progress. Runners are registering "
            "and dying without claiming work — refusing to create more. Likely causes: "
            "the actions/runner version baked into the VM image has been deprecated by "
            "GitHub, a broken VM image, or rejected registration tokens. Check a runner "
            "VM's serial console output. Set GITHUB_RECONCILER_BREAKER_MIN_DOA=0 to "
            "override.",
            doa, self.breaker_max_lifetime_seconds,
        )
        return True

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

    @staticmethod
    def _gcp_label(job: dict) -> Optional[str]:
        """Return the gcp-/dependabot runner label of a job, or None."""
        for lbl in (job.get('labels') or []):
            if lbl.startswith('gcp-') or lbl.lower() == 'dependabot':
                return lbl
        return None

    def _classify(self, job: dict, now: float) -> str:
        """Classify a job as 'eligible', 'young', or 'no_label'.

        Eligibility here means only "is this a queued, old-enough, gcp-labelled
        job that COULD need a runner". Whether it ACTUALLY needs a new VM is
        decided later in reconcile() by comparing per-label demand to live
        supply — never per job-id, because GitHub assigns jobs to arbitrary
        matching runners.
        """
        if job.get('status') != 'queued':
            return 'no_label'  # not queued — not our concern
        if self._gcp_label(job) is None:
            return 'no_label'

        started = _parse_iso8601_to_epoch(job.get('started_at') or '')
        # GitHub's `started_at` for a queued job is when it entered the queue.
        # If unparseable, treat as too-young to be safe (avoid racing webhooks).
        if started is None or (now - started) < self.min_age_seconds:
            return 'young'

        return 'eligible'

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
