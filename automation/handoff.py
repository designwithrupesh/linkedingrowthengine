"""Queue the next serialized worker without depending on another cron wake."""
from __future__ import annotations

import os
import re

import requests


class HandoffError(RuntimeError):
    """The scheduler acknowledgement was not established; never replay blindly."""


def dispatch_successor(token, repository, *, session=requests):
    if not isinstance(token, str) or not token.strip():
        raise HandoffError('The worker handoff requires its GitHub Actions token')
    if (not isinstance(repository, str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or any(part in ('.', '..') for part in repository.split('/'))):
        raise HandoffError('The worker handoff requires a valid repository identity')
    url = ('https://api.github.com/repos/' + repository
           + '/actions/workflows/digital-twin.yml/dispatches')
    try:
        response = session.post(url,
            headers={'Authorization': 'Bearer ' + token,
                     'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28'},
            json={'ref': 'main', 'inputs': {'mode': 'autopilot', 'supervisor_minutes': '300'}},
            timeout=30, allow_redirects=False)
    except requests.RequestException:
        raise HandoffError('Worker handoff outcome is uncertain; inspect queued runs before retrying') from None
    if response.status_code != 204:
        raise HandoffError('GitHub did not acknowledge the next worker; inspect the workflow run')
    return {'status': 'queued', 'mode': 'autopilot'}


def main():
    if os.getenv('TWIN_LIVE', 'true') != 'true':
        print('Worker handoff paused by TWIN_LIVE.')
        return
    dispatch_successor(os.getenv('GITHUB_TOKEN'), os.getenv('GITHUB_REPOSITORY'))
    print('Next serialized autopilot worker queued.')


if __name__ == '__main__':
    main()
