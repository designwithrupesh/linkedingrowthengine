import unittest
from unittest.mock import Mock

import requests

from automation.handoff import HandoffError, dispatch_successor


class HandoffTests(unittest.TestCase):
    def test_acknowledged_dispatch_uses_only_same_workflow_and_bounded_successor(self):
        session = Mock()
        session.post.return_value.status_code = 204
        self.assertEqual(dispatch_successor('fixture-token', 'owner/project', session=session),
                         {'status': 'queued', 'mode': 'autopilot'})
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], 'https://api.github.com/repos/owner/project/actions/workflows/digital-twin.yml/dispatches')
        self.assertEqual(kwargs['json'], {'ref': 'main', 'inputs': {'mode': 'autopilot', 'supervisor_minutes': '300'}})
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], 30)

    def test_ambiguous_transport_is_one_attempt_and_never_leaks_token_or_provider_body(self):
        session = Mock()
        session.post.side_effect = requests.Timeout('fixture-token private provider body')
        with self.assertRaises(HandoffError) as caught:
            dispatch_successor('fixture-token', 'owner/project', session=session)
        self.assertNotIn('fixture-token', str(caught.exception))
        self.assertNotIn('provider body', str(caught.exception))
        session.post.assert_called_once()

    def test_non_acknowledgement_or_invalid_target_is_not_success(self):
        for status in (200, 301, 401, 403, 429, 500):
            session = Mock()
            session.post.return_value.status_code = status
            with self.subTest(status=status), self.assertRaises(HandoffError):
                dispatch_successor('fixture-token', 'owner/project', session=session)
            session.post.assert_called_once()
        for repository in ('', '../project', 'owner/project?query=1', 'https://github.com/owner/project'):
            session = Mock()
            with self.subTest(repository=repository), self.assertRaises(HandoffError):
                dispatch_successor('fixture-token', repository, session=session)
            session.post.assert_not_called()
