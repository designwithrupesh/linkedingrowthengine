"""Schedule an existing prepared batch without requiring a model API.
Preview: python -m automation.bootstrap --queue testing/automation/queue.json
Execute only after owner authorization: add --execute and durable checkpoint env.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from automation.twin import Runner, ROOT


def slots(now, count, policy):
    local = now.astimezone(ZoneInfo(policy['timezone']))
    result = []
    day = local.date()
    for offset in range(60):
        date = day + timedelta(days=offset)
        when = datetime.combine(date, datetime.min.time(), tzinfo=local.tzinfo).replace(hour=policy['posting_hour'])
        if when <= local + timedelta(minutes=10):
            continue
        # Start at the next weekday, then follow the standard posting cadence.
        allowed = date.weekday() < 5 if not result else date.weekday() in policy['posting_days']
        if allowed:
            result.append(when.astimezone(timezone.utc))
        if len(result) == count:
            return result
    raise ValueError('Cannot allocate requested slots')


def account_check(policy):
    key = os.getenv('PUBLORA_API_KEY')
    if not key:
        raise RuntimeError('PUBLORA_API_KEY binding is missing')
    r = requests.get('https://api.publora.com/api/v1/platform-connections',
                     headers={'x-publora-key': key}, timeout=30)
    r.raise_for_status()
    def found(value):
        if isinstance(value, dict):
            return any(found(v) for v in value.values())
        if isinstance(value, list):
            return any(found(v) for v in value)
        return value == policy['platform_id']
    if not found(r.json()):
        raise RuntimeError('Configured LinkedIn account is not connected')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--queue', required=True)
    ap.add_argument('--execute', action='store_true')
    args = ap.parse_args()
    path = Path(args.queue).resolve()
    queue = json.loads(path.read_text(encoding='utf-8'))
    policy = json.loads((ROOT / 'automation/policy.json').read_text(encoding='utf-8'))
    now = datetime.now(timezone.utc)
    candidates = queue['posts'][:3]
    plan = slots(now, len(candidates), policy)
    texts = []
    for entry in candidates:
        text_path = (path.parent / entry['text_file']).resolve()
        if not text_path.is_relative_to(path.parent):
            raise ValueError('Queue path escapes its directory')
        text = text_path.read_text(encoding='utf-8').strip()
        if not text or len(text) > 3000:
            raise ValueError('Invalid prepared post')
        texts.append(text)
    if args.execute:
        account_check(policy)
    runner = Runner(policy, ROOT / 'automation/state.json', live=args.execute, now=now)
    for entry, text, when in zip(candidates, texts, plan):
        allocation = when.astimezone(ZoneInfo(policy['timezone'])).date().isoformat()
        key = 'post:' + allocation
        if not args.execute:
            print(json.dumps({'id': entry['id'], 'scheduled_local': when.astimezone(ZoneInfo(policy['timezone'])).isoformat(), 'characters':len(text)}))
            continue
        runner.write(key, 'post', text, scheduled_time=when, allocation_day=allocation)
        action = runner.state['actions'].get(key) or {}
        print(json.dumps({'id':entry['id'], 'scheduled_local':when.astimezone(ZoneInfo(policy['timezone'])).isoformat(),
                          'status':action.get('status'), 'remote_id':action.get('remote_id')}))


if __name__ == '__main__':
    main()
