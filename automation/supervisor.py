"""Run bounded autonomous workers between GitHub workflow handoffs.

Outside the engagement window, normal intent-aware runs maintain the post
buffer and check replies. Inside it, the existing continuous session retains
its five-minute polling and public-action pacing. Workflow concurrency and
the successor dispatch belong to the workflow, not this module.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from automation.session import (SessionCheckpointError, SessionSourceUpdated,
                                refresh_checkpoint, run_session)
from automation.twin import (ROOT, GitCheckpointError, Runner,
                             WriteCheckpointError)
from automation.private_actions import PrivateWriteCheckpointError


CHECKPOINT_EXIT_CODE = 78
_FATAL_CHECKPOINT_ERRORS = (SessionCheckpointError, GitCheckpointError,
                            WriteCheckpointError, PrivateWriteCheckpointError)


def _aware(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('A timezone-aware supervisor clock is required')
    return now


def _window(now, policy):
    local = _aware(now).astimezone(ZoneInfo(policy['timezone']))
    start_hour = policy.get('public_engagement_start_hour', 17)
    end_hour = policy.get('public_engagement_end_hour', 22)
    if (type(start_hour) is not int or type(end_hour) is not int
            or not 0 <= start_hour < end_hour <= 24):
        raise ValueError('Invalid supervisor engagement window')
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(hours=start_hour), midnight + timedelta(hours=end_hour)


def run_once(path, *, timeout_seconds):
    """Run one normal cycle, with no subprocess or LinkedIn-write retry."""
    result = subprocess.run([sys.executable, '-m', 'automation.twin', '--state', str(path)],
                            cwd=ROOT, timeout=timeout_seconds, check=False)
    if result.returncode == CHECKPOINT_EXIT_CODE:
        raise SessionCheckpointError('The autonomous worker reported a durable checkpoint failure')
    if result.returncode:
        raise RuntimeError('The autonomous worker deferred this cycle')


def _wait_until(target, clock, sleeper):
    while (remaining := (target - _aware(clock())).total_seconds()) > 0:
        sleeper(min(60, remaining))


def run_supervisor(policy, path, *, max_minutes=300, poll_seconds=900,
                   clock=lambda: datetime.now(timezone.utc), sleeper=time.sleep,
                   one_shot=run_once, session=run_session,
                   refresh=refresh_checkpoint, runner_factory=Runner):
    """Maintain ordinary and engagement workers within one five-hour budget.

    A source update returns normally so the workflow can hand off to a fresh
    checkout. Durable checkpoint failures stop this worker immediately. Other
    phase failures wait for a normal poll; uncertain writes keep their existing
    intents and are never replayed here.
    """
    if type(max_minutes) is not int or not 1 <= max_minutes <= 300:
        raise ValueError('Supervisor duration must be between 1 and 300 minutes')
    if type(poll_seconds) is not int or not 60 <= poll_seconds <= 900:
        raise ValueError('Supervisor polling must be between 60 and 900 seconds')
    started = _aware(clock())
    _window(started, policy)
    deadline = started + timedelta(minutes=max_minutes)
    path = Path(path)
    cycles = 0
    sessions = 0
    recent_errors = []

    def report(status, phase):
        progress = {'status': status, 'phase': phase, 'started_at': started.isoformat(),
                    'deadline': deadline.isoformat(), 'heartbeat_at': _aware(clock()).isoformat(),
                    'cycles': cycles, 'sessions': sessions, 'recent_errors': recent_errors[-6:]}
        # Reload the just-finished child's checkpoint, never a stale parent
        # snapshot. Workflow concurrency keeps the child and this save serial.
        runner = runner_factory(policy, path, live=True, now=_aware(clock()))
        runner.state.setdefault('operational_status', {})['supervisor'] = progress
        try:
            runner.save()
        except Exception:
            raise SessionCheckpointError('Supervisor progress could not be durably checkpointed') from None
        print(json.dumps({'supervisor': progress}), flush=True)

    try:
        while _aware(clock()) < deadline:
            refresh()
            cycle_at = _aware(clock())
            remaining = (deadline - cycle_at).total_seconds()
            if remaining <= 0:
                break
            window_start, window_end = _window(cycle_at, policy)
            active = window_start <= cycle_at < window_end
            phase = 'engagement' if active else 'maintenance'
            report('running', phase)
            remaining = (deadline - _aware(clock())).total_seconds()
            if remaining <= 0:
                break
            cycles += 1
            try:
                if active:
                    # Whole-minute bounds never extend the child session past
                    # this supervisor's deadline. Hand off the final fraction.
                    minutes = min(300, int(remaining // 60))
                    if minutes < 1:
                        _wait_until(deadline, clock, sleeper)
                        break
                    sessions += 1
                    session(policy, path, max_minutes=minutes, poll_seconds=300,
                            clock=clock, sleeper=sleeper, refresh=refresh,
                            runner_factory=runner_factory)
                else:
                    # A slow maintenance draft must yield when engagement
                    # starts. Its persisted intents remain reserved if the
                    # child reaches this boundary while awaiting a response.
                    timeout = remaining
                    if cycle_at < window_start:
                        timeout = min(timeout, (window_start - _aware(clock())).total_seconds())
                    if timeout > 0:
                        one_shot(path, timeout_seconds=timeout)
            except (SessionSourceUpdated, *_FATAL_CHECKPOINT_ERRORS):
                raise
            except Exception as exc:
                recent_errors.append({'phase': phase, 'type': type(exc).__name__,
                                      'at': _aware(clock()).isoformat()})
                recent_errors = recent_errors[-6:]
                print(json.dumps({'supervisor_phase_deferred': recent_errors[-1]}), flush=True)
                # A failed child may have an unpushed intent. Prove checkpoint
                # health before another phase or even a progress-state write.
                refresh()
            now = _aware(clock())
            if now >= deadline:
                break
            # Wake for the start of engagement even when a maintenance poll
            # would land later. Session failures resume at a normal five-minute
            # poll, without a tight retry loop.
            interval = 300 if active else poll_seconds
            target = max(cycle_at + timedelta(seconds=interval), now)
            if cycle_at < window_start:
                target = min(target, max(now, window_start))
            _wait_until(min(target, deadline), clock, sleeper)
        # The last child may have changed state or source. Refresh before the
        # final heartbeat so a source handoff cannot overwrite newer files.
        refresh()
        report('completed', 'handoff')
    except SessionSourceUpdated:
        print(json.dumps({'supervisor': {'status': 'source-updated', 'cycles': cycles,
                                        'sessions': sessions}}), flush=True)
        return {'status': 'source-updated', 'cycles': cycles, 'sessions': sessions}
    return {'status': 'completed', 'cycles': cycles, 'sessions': sessions,
            'recent_errors': recent_errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default='automation/state.json')
    parser.add_argument('--max-minutes', type=int, default=300)
    parser.add_argument('--poll-seconds', type=int, default=900)
    args = parser.parse_args()
    if os.getenv('TWIN_LIVE') != 'true':
        print('Autonomous supervisor paused by TWIN_LIVE.')
        return
    policy = json.loads((ROOT / 'automation/policy.json').read_text(encoding='utf-8'))
    print(json.dumps(run_supervisor(policy, args.state, max_minutes=args.max_minutes,
                                   poll_seconds=args.poll_seconds)), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Autonomous supervisor stopped: ' + type(exc).__name__ +
              '. Check the durable checkpoint and workflow diagnostics.')
        raise SystemExit(1)
