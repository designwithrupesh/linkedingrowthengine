"""Keep one verified runner alive through the daily engagement window.

GitHub cron can arrive late. Once started, this worker polls independently of
cron while retaining the existing durable intents, budgets and slot quotas.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import time
from zoneinfo import ZoneInfo

from automation import engagement
from automation.post_queue import prepare_next_day_posts
from automation.discovery import DiscoveryClient, read_budget_available
from automation.twin import ROOT, Runner, WriteCheckpointError, preflight
from automation.private_actions import PrivateWriteCheckpointError


class SessionCheckpointError(RuntimeError):
    """The next cycle cannot safely use its durable checkpoint."""


class SessionSourceUpdated(RuntimeError):
    """A newer source revision needs a fresh workflow checkout."""


def _aware(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('A timezone-aware session clock is required')
    return now


def session_bounds(now, policy, *, wait_for_window=False, max_minutes=330):
    """Return today's active bounds, allowing at most one hour of early wait."""
    _aware(now)
    if (not isinstance(max_minutes, int) or isinstance(max_minutes, bool)
            or not 1 <= max_minutes <= 330):
        raise ValueError('Session duration must be between 1 and 330 minutes')
    local = now.astimezone(ZoneInfo(policy['timezone']))
    start_hour = policy.get('public_engagement_start_hour', 17)
    end_hour = policy.get('public_engagement_end_hour', 22)
    if (not isinstance(start_hour, int) or isinstance(start_hour, bool)
            or not isinstance(end_hour, int) or isinstance(end_hour, bool)
            or not 0 <= start_hour < end_hour <= 24):
        raise ValueError('Invalid session engagement window')
    start = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    end = (local.replace(hour=0, minute=0, second=0, microsecond=0)
           + timedelta(hours=end_hour))
    if local >= end:
        return None
    if local < start and (not wait_for_window or start - local > timedelta(hours=1)):
        return None
    deadline = min(end, local + timedelta(minutes=max_minutes))
    if deadline <= start:
        return None
    return max(start, local), deadline


def refresh_checkpoint():
    """Fast-forward state-only updates; stop when deployed source changes.

    Workflow concurrency keeps automated writers apart. This check also
    protects against an owner changing the source during a long session.
    """
    def git(*args):
        try:
            return subprocess.run(['git', *args], cwd=ROOT, check=True,
                                  capture_output=True, text=True).stdout.strip()
        except subprocess.CalledProcessError:
            raise SessionCheckpointError('The session checkpoint could not be refreshed') from None
    if git('status', '--porcelain', '--untracked-files=no'):
        raise SessionCheckpointError('Tracked changes prevent a safe checkpoint refresh')
    git('fetch', 'origin', 'refs/heads/main:refs/remotes/origin/main')
    ahead, behind = map(int, git('rev-list', '--left-right', '--count',
                                'HEAD...refs/remotes/origin/main').split())
    if ahead:
        raise SessionCheckpointError('A local checkpoint has not reached the remote repository')
    if not behind:
        return
    changed = git('diff', '--name-only', 'HEAD..refs/remotes/origin/main').splitlines()
    if any(path != 'automation/state.json' for path in changed):
        raise SessionSourceUpdated('A source update requires a fresh workflow checkout')
    git('merge', '--ff-only', 'refs/remotes/origin/main')


def _refresh_runner_clock(runner, now):
    runner.now = _aware(now)
    runner.local = now.astimezone(ZoneInfo(runner.policy['timezone']))
    runner.day = runner.local.date().isoformat()
    runner.counts = runner.state['days'].setdefault(
        runner.day, {'post': 0, 'interaction': 0, 'read': 0})


def _guard_writes(runner, clock, deadline):
    """Recheck actual slot time after potentially slow drafting."""
    original = runner.write
    def write(key, kind, text, *args, **kwargs):
        if getattr(runner, 'session_checkpoint_failed', False):
            raise SessionCheckpointError('A durable session checkpoint failed')
        _refresh_runner_clock(runner, clock())
        if runner.now >= deadline:
            print('Session write deferred: the engagement window has ended.')
            return
        if kind in ('reaction', 'comment') and engagement.public_actions_due(
                runner.now, runner.policy, runner.state) <= 0:
            print('Session write deferred: the current public slot has no allowance.')
            return
        return original(key, kind, text, *args, **kwargs)
    runner.write = write


def _guard_checkpoints(runner):
    original = runner.save
    runner.session_checkpoint_failed = False
    def save():
        try:
            return original()
        except Exception:
            # Some write paths translate save failures into a write-outcome
            # error. Keep a sticky flag so translation cannot resume writes.
            runner.session_checkpoint_failed = True
            raise SessionCheckpointError('A durable session checkpoint failed') from None
    runner.save = save


def _wait_until(target, clock, sleeper):
    # Small waits let termination and logs stay responsive.
    while (remaining := (target - _aware(clock())).total_seconds()) > 0:
        sleeper(min(60, remaining))


def run_session(policy, path, *, wait_for_window=False, max_minutes=330,
                poll_seconds=300, clock=lambda: datetime.now(timezone.utc),
                sleeper=time.sleep, runner_factory=Runner, reader_factory=DiscoveryClient,
                verify=preflight, refresh=refresh_checkpoint):
    if (not isinstance(poll_seconds, int) or isinstance(poll_seconds, bool)
            or not 60 <= poll_seconds <= 900):
        raise ValueError('Session poll interval must be between 60 and 900 seconds')
    started = _aware(clock())
    bounds = session_bounds(started, policy, wait_for_window=wait_for_window,
                            max_minutes=max_minutes)
    if bounds is None:
        print('Continuous session skipped: outside today\'s engagement window.')
        return {'status': 'outside-window', 'cycles': 0}
    active_start, deadline = bounds
    verify(policy)
    reading = os.getenv('TWIN_READ_ENABLED') == 'true'
    reader = reader_factory() if reading else None
    print(json.dumps({'session': 'starting', 'active_from': active_start.isoformat(),
                      'until': deadline.isoformat(), 'poll_seconds': poll_seconds}), flush=True)
    _wait_until(active_start, clock, sleeper)
    cycles = 0
    last_errors = []
    last_runner = None
    while _aware(clock()) < deadline:
        refresh()
        cycle_at = _aware(clock())
        if cycle_at >= deadline:
            break
        runner = runner_factory(policy, path, live=True, now=cycle_at)
        last_runner = runner
        _guard_checkpoints(runner)
        _guard_writes(runner, clock, deadline)
        if reader is not None:
            runner.read_budget_guard = lambda: read_budget_available(reader)
        stages = [('post', runner.post), ('published-posts', runner.discover_published)]
        if reader is not None:
            # Replies run before public comments; a failure in another stage
            # cannot prevent this cycle from checking the owner's threads.
            stages.extend([('replies', lambda: runner.replies(reader)),
                           ('private', runner.private_workflow),
                           ('public', lambda: runner.public_engagement(reader))])
            if runner.local.weekday() == 4:
                stages.append(('analytics', lambda: runner.analytics(reader)))
        else:
            stages.append(('private', runner.private_workflow))
        stages.append(('next-day-posts', lambda: prepare_next_day_posts(runner)))
        cycle_errors = []
        for phase, operation in stages:
            if _aware(clock()) >= deadline:
                break
            _refresh_runner_clock(runner, clock())
            try:
                operation()
                if runner.session_checkpoint_failed:
                    raise SessionCheckpointError('A durable session checkpoint failed')
            except (WriteCheckpointError, PrivateWriteCheckpointError,
                    subprocess.CalledProcessError, SessionCheckpointError):
                # A failed durable checkpoint must stop all future writes.
                raise SessionCheckpointError('A durable session checkpoint failed') from None
            except Exception as exc:
                if runner.session_checkpoint_failed:
                    raise SessionCheckpointError('A durable session checkpoint failed') from None
                # Log fixed phase/type fields, never provider bodies or content.
                # The next normal poll may proceed; this call is not retried.
                error = {'phase': phase, 'type': type(exc).__name__, 'at': clock().isoformat()}
                cycle_errors.append(error)
                print(json.dumps({'session_stage_deferred': error}), flush=True)
        cycles += 1
        last_errors = (last_errors + cycle_errors)[-6:]
        runner.state.setdefault('operational_status', {})['session'] = {
            'status': 'running', 'started_at': started.isoformat(),
            'deadline': deadline.isoformat(), 'heartbeat_at': clock().isoformat(),
            'cycles': cycles, 'recent_errors': last_errors,
        }
        try:
            runner.save()
        except Exception:
            raise SessionCheckpointError('A durable session checkpoint failed') from None
        print(json.dumps({'session_cycle': cycles, 'at': clock().isoformat(),
                          'counts': runner.counts, 'deferred_stages': len(cycle_errors)}), flush=True)
        # Base cadence on the cycle start, with no overlapping or catch-up loops.
        next_poll = max(cycle_at + timedelta(seconds=poll_seconds), _aware(clock()))
        _wait_until(min(next_poll, deadline), clock, sleeper)
    if last_runner is not None:
        last_runner.state['operational_status']['session'].update(
            status='completed', ended_at=clock().isoformat())
        try:
            last_runner.save()
        except Exception:
            raise SessionCheckpointError('A durable session checkpoint failed') from None
    return {'status': 'completed', 'cycles': cycles, 'recent_errors': last_errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default='automation/state.json')
    parser.add_argument('--wait-for-window', action='store_true')
    parser.add_argument('--max-minutes', type=int, default=330)
    parser.add_argument('--poll-seconds', type=int, default=300)
    args = parser.parse_args()
    if os.getenv('TWIN_LIVE') != 'true':
        print('Continuous session paused by TWIN_LIVE.')
        return
    policy = json.loads((ROOT / 'automation/policy.json').read_text(encoding='utf-8'))
    print(json.dumps(run_session(policy, Path(args.state),
          wait_for_window=args.wait_for_window, max_minutes=args.max_minutes,
          poll_seconds=args.poll_seconds)), flush=True)


if __name__ == '__main__':
    try:
        main()
    except SessionSourceUpdated:
        print('Continuous session stopped: source changed; start a workflow with the new revision.')
    except Exception as exc:
        print('Continuous session stopped: ' + type(exc).__name__ +
              '. Check the durable checkpoint and workflow diagnostics.')
        raise SystemExit(1)
