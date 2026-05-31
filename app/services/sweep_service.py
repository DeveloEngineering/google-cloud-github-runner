"""
Sweeper — runner VM cleanup.

Two cleanup mechanisms, selected by runner mode:

* Age-based orphan sweep (ALWAYS runs): deletes any gcp-runner-* VM older than
  ``GITHUB_RUNNERS_ORPHAN_MAX_AGE_SECONDS``. This is the backstop for VMs that
  never registered, got stuck, or whose completion webhook was missed. The
  GCE instance template's ``max_run_duration`` is the final hard backstop.

* Idle-runner reaping (REUSABLE mode only, i.e. RUNNER_EPHEMERAL=false): since
  reusable runners are NOT deleted on job completion, the sweeper scales the
  fleet down. It keeps up to ``demand`` idle runners warm per label (they will
  pick up queued jobs) and reaps the rest. Reaping deregisters the runner from
  GitHub first (non-force — GitHub rejects removing a busy runner, so an
  in-flight job is never killed) and only then deletes the VM.
"""
import logging
import os
from collections import defaultdict

from app.clients import GCloudClient, GitHubClient

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_SECONDS = 7200  # 2 hours


class SweepService:
    """Deletes orphan and (in reusable mode) excess-idle gcp-runner-* VMs."""

    def __init__(self):
        self.gcloud_client = GCloudClient()
        self.github_client = GitHubClient()
        try:
            self.max_age_seconds = int(
                os.environ.get('GITHUB_RUNNERS_ORPHAN_MAX_AGE_SECONDS', DEFAULT_MAX_AGE_SECONDS)
            )
        except ValueError:
            logger.warning(
                "Invalid GITHUB_RUNNERS_ORPHAN_MAX_AGE_SECONDS; using default %d",
                DEFAULT_MAX_AGE_SECONDS,
            )
            self.max_age_seconds = DEFAULT_MAX_AGE_SECONDS

    def sweep(self):
        """Run one sweep pass: age-based orphan deletion plus, in reusable
        mode, idle-runner reaping.

        Returns:
            dict: orphan-sweep summary, with an ``idle_reap`` sub-summary when
            reusable mode is active.
        """
        busy_names = set()
        idle_summary = None
        if not self.gcloud_client.ephemeral:
            try:
                idle_summary, busy_names = self._reap_idle_runners()
            except Exception as e:
                logger.error("Idle-runner reaping failed: %s", e)
                idle_summary = {'error': str(e)}

        # The age backstop must not force-kill a busy long-lived runner
        # mid-job, so skip currently-busy runner names (reusable mode only).
        result = self._sweep_orphans_by_age(skip_names=busy_names)
        if idle_summary is not None:
            result['idle_reap'] = idle_summary
        logger.info("Sweep complete: %s", result)
        return result

    # ------------------------------------------------------------------
    # Age-based orphan sweep (always runs)
    # ------------------------------------------------------------------

    def _sweep_orphans_by_age(self, skip_names=None):
        skip_names = skip_names or set()
        inspected = 0
        deleted_names = []
        skipped = 0
        errors = 0

        for instance in self.gcloud_client.list_runner_instances():
            inspected += 1
            age = GCloudClient.instance_age_seconds(instance)
            if age is None or age < self.max_age_seconds:
                skipped += 1
                continue
            if instance.name in skip_names:
                # Busy long-lived runner — leave it; max_run_duration is the
                # ultimate GCE-side backstop.
                skipped += 1
                continue
            try:
                self.gcloud_client.delete_runner_instance(instance.name)
                deleted_names.append(instance.name)
                logger.info(
                    "Swept orphan runner %s (age=%ds, threshold=%ds)",
                    instance.name, int(age), self.max_age_seconds,
                )
            except Exception as e:
                errors += 1
                logger.error("Failed to delete orphan runner %s: %s", instance.name, e)

        return {
            'inspected': inspected,
            'deleted': len(deleted_names),
            'skipped': skipped,
            'errors': errors,
            'deleted_names': deleted_names,
            'max_age_seconds': self.max_age_seconds,
        }

    # ------------------------------------------------------------------
    # Idle-runner reaping (reusable mode only)
    # ------------------------------------------------------------------

    def _reap_idle_runners(self):
        """Keep up to `demand` idle runners warm per label; reap the rest.

        Demand = count of currently-queued gcp jobs per label across the
        installation. During a burst demand>0 so idle runners are retained;
        when the burst drains demand→0 and all idle runners are reaped.

        Returns:
            (summary dict, set of currently-busy runner VM names) — the busy
            set is used by the age sweep to avoid force-killing live jobs.
        """
        summary = {
            'orgs': 0, 'idle_found': 0, 'reaped': 0,
            'kept_for_demand': 0, 'errors': 0, 'reaped_names': [],
            'demand_by_label': {},
        }
        busy_names = set()

        repos = self.github_client.list_installation_repos()
        # Org-level runners serve every repo in the org. Collect the distinct
        # org logins; user-owned repos are handled per-repo.
        orgs = set()
        user_repos = []
        for repo in repos:
            owner = repo.get('owner', {})
            if owner.get('type') == 'Organization':
                orgs.add(owner.get('login'))
            else:
                user_repos.append(repo.get('full_name'))

        demand = self._count_queued_demand_by_label(repos)
        summary['demand_by_label'] = dict(demand)

        # Reap per org (and per user-repo) independently.
        for org in orgs:
            summary['orgs'] += 1
            self._reap_for_scope(
                summary, busy_names, demand,
                runners_kwargs={'org_name': org},
                deregister_kwargs={'org_name': org},
            )
        for full_name in user_repos:
            self._reap_for_scope(
                summary, busy_names, demand,
                runners_kwargs={'repo_name': full_name},
                deregister_kwargs={'repo_name': full_name},
            )
        return summary, busy_names

    def _reap_for_scope(self, summary, busy_names, demand, runners_kwargs, deregister_kwargs):
        try:
            runners = self.github_client.list_runners(**runners_kwargs)
        except Exception as e:
            summary['errors'] += 1
            logger.error("Failed to list runners for %s: %s", runners_kwargs, e)
            return

        idle_by_label = defaultdict(list)
        for r in runners:
            name = r.get('name', '')
            if not name.startswith('gcp-runner-'):
                continue
            if r.get('busy'):
                busy_names.add(name)
                continue
            if r.get('status') != 'online':
                continue
            label = self._runner_label(r)
            if label:
                idle_by_label[label].append(r)

        for label, idle in idle_by_label.items():
            summary['idle_found'] += len(idle)
            keep = demand.get(label, 0)
            summary['kept_for_demand'] += min(keep, len(idle))
            to_reap = idle[keep:]  # keep `keep` warm; reap the excess
            for r in to_reap:
                if self._reap_one(r, deregister_kwargs):
                    summary['reaped'] += 1
                    summary['reaped_names'].append(r.get('name'))
                else:
                    summary['errors'] += 1

    def _reap_one(self, runner, deregister_kwargs):
        """Deregister (non-force) then delete the VM. Returns True on success."""
        name = runner.get('name')
        try:
            ok = self.github_client.delete_runner(runner.get('id'), **deregister_kwargs)
            if not ok:
                # Busy race or removal rejected — leave the VM alone.
                return False
        except Exception as e:
            logger.error("Failed to deregister runner %s: %s", name, e)
            return False
        try:
            self.gcloud_client.delete_runner_instance(name)
            logger.info("Reaped idle runner %s", name)
            return True
        except Exception as e:
            logger.error("Deregistered runner %s but failed to delete VM: %s", name, e)
            return False

    @staticmethod
    def _runner_label(runner):
        """Return the gcp-/dependabot label of a GitHub runner, or None."""
        for lbl in (runner.get('labels') or []):
            name = lbl.get('name') if isinstance(lbl, dict) else lbl
            if name and (name.startswith('gcp-') or name.lower() == 'dependabot'):
                return name
        return None

    def _count_queued_demand_by_label(self, repos):
        """Count currently-queued gcp jobs per label across the installation."""
        demand = defaultdict(int)
        for repo in repos:
            owner = repo['owner']['login']
            name = repo['name']
            try:
                runs = list(self.github_client.list_active_runs(owner, name))
            except Exception as e:
                logger.warning("Sweeper could not list runs for %s/%s: %s", owner, name, e)
                continue
            for run in runs:
                try:
                    jobs = self.github_client.list_run_jobs(owner, name, run['id'])
                except Exception:
                    continue
                for job in jobs:
                    if job.get('status') != 'queued':
                        continue
                    label = self._runner_label({'labels': [
                        {'name': l} for l in (job.get('labels') or [])
                    ]})
                    if label:
                        demand[label] += 1
        return demand
