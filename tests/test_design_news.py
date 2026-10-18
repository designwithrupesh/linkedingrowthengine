from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import requests

from automation import design_news
from automation.post_queue import prepare_next_day_posts
from automation.twin import Runner


NOW = datetime(2026, 10, 18, 9, tzinfo=ZoneInfo('Asia/Calcutta'))
POLICY = {'timezone': 'Asia/Calcutta', 'platform_id': 'linkedin-test',
          'posting_hours': [9], 'posting_days': list(range(7)), 'max_posts_per_day': 1,
          'topics': ['product design'], 'source_notes': [], 'background': ['product design'],
          'max_interactions_per_day': 40}
TEXT = ('Linear describes a way to keep related changes connected as they move through review. '
        'The design question is how clearly the interface explains what depends on what. '
        'A shorter path through review can help a team, but only when the next person can '
        'understand which decision is ready and which is waiting.\n\n'
        'I would make that dependency visible before adding another shortcut. For a small '
        'team, clarity about the next decision is often more useful than another control.')


def source(feed=None, *, title='Stacked pull requests', days=2, path='changelog/design-change'):
    feed = feed or design_news.OFFICIAL_FEEDS[0]
    return {'publisher': feed.publisher, 'title': title,
            'url': f'https://{feed.article_hosts[0]}/{path}',
            'published_at': (NOW - timedelta(days=days)).astimezone(timezone.utc).isoformat(),
            'excerpt': 'Related changes stay connected during review.'}


def rss(*, stamp='Fri, 16 Oct 2026 03:30:00 GMT', link='https://linear.app/changelog/design-change',
        title='Stacked pull requests', excerpt='<p>A <strong>connected</strong> review.</p>'):
    return (f'<rss version="2.0"><channel><item><title>{title}</title>'
            f'<pubDate>{stamp}</pubDate><link>{link}</link>'
            f'<description><![CDATA[{excerpt}]]></description></item></channel></rss>').encode()


class DesignNewsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def runner(self, *, now=NOW, policy=None):
        return Runner({**POLICY, **(policy or {})}, self.path / 'state.json', now=now,
                      generate=Mock(return_value={'text': TEXT, 'skip': False}),
                      capacity=Mock(return_value=(True, 'verified')))

    def cache(self, runner, items):
        runner.state[design_news.CACHE_KEY] = {'checked_day': runner.day, 'items': items}

    def plan(self, day='2026-10-18', hour=9, **extra):
        plan = {'slots': [{'date': day, 'hour': hour, 'topic': 'Linear dependency design',
                           'format': 'text', 'source_mode': 'current_design', **extra}]}
        (self.path / 'automation').mkdir(exist_ok=True)
        (self.path / 'automation' / 'content-plan.json').write_text(json.dumps(plan), encoding='utf-8')

    def test_rss_preserves_exact_date_and_strips_html_and_scripts(self):
        items = design_news.parse_feed(rss(excerpt='<p>A connected review.</p><script>steal()</script>'),
                                       design_news.OFFICIAL_FEEDS[0], NOW)
        self.assertEqual(items[0]['published_at'], '2026-10-16T03:30:00+00:00')
        self.assertEqual(items[0]['excerpt'], 'A connected review.')
        self.assertEqual(items[0]['url'], 'https://linear.app/changelog/design-change')

    def test_atom_published_date_and_alternate_link(self):
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <title>Review dependencies</title><published>2026-10-17T12:00:00Z</published>
          <link rel="self" href="https://untrusted.example/item"/>
          <link rel="alternate" href="https://linear.app/changelog/review"/>
          <summary type="html">A visible review dependency.</summary></entry></feed>'''
        items = design_news.parse_feed(atom, design_news.OFFICIAL_FEEDS[0], NOW)
        self.assertEqual(items[0]['url'], 'https://linear.app/changelog/review')
        self.assertEqual(items[0]['published_at'], '2026-10-17T12:00:00+00:00')

    def test_stale_future_missing_and_naive_publication_dates_are_excluded(self):
        for stamp in ('Sat, 03 Oct 2026 03:30:00 GMT', 'Sun, 18 Oct 2026 03:30:01 GMT',
                      '', '2026-10-17T12:00:00'):
            with self.subTest(stamp=stamp):
                self.assertEqual(design_news.parse_feed(rss(stamp=stamp), design_news.OFFICIAL_FEEDS[0], NOW), [])

    def test_exact_fourteen_day_boundary_is_allowed(self):
        self.assertEqual(len(design_news.parse_feed(rss(stamp='Sun, 04 Oct 2026 03:30:00 GMT'),
                                                    design_news.OFFICIAL_FEEDS[0], NOW)), 1)

    def test_official_feed_cannot_supply_external_or_credentialed_article_links(self):
        for link in ('https://linear.app.evil.example/post', 'https://user:secret@linear.app/post',
                     'http://linear.app/post', 'https://127.0.0.1/post', 'https://linear.app:8080/post'):
            with self.subTest(link=link):
                self.assertEqual(design_news.parse_feed(rss(link=link), design_news.OFFICIAL_FEEDS[0], NOW), [])

    def test_tracking_query_is_not_part_of_article_identity(self):
        items = design_news.parse_feed(rss(link='https://linear.app/changelog/change?utm_source=feed#section'),
                                       design_news.OFFICIAL_FEEDS[0], NOW)
        self.assertEqual(items[0]['url'], 'https://linear.app/changelog/change')

    def test_dtd_and_oversized_xml_are_rejected(self):
        for body in (b'<!DOCTYPE rss [<!ENTITY x "expanded">]><rss/>', b'x' * (design_news.MAX_FEED_BYTES + 1)):
            with self.subTest(size=len(body)), self.assertRaises(ValueError):
                design_news.parse_feed(body, design_news.OFFICIAL_FEEDS[0], NOW)

    def test_unknown_feed_never_reaches_the_network(self):
        unknown = design_news.OfficialFeed('Other', 'https://untrusted.example/rss', ('untrusted.example',))
        with patch('automation.design_news.requests.get') as request, self.assertRaises(ValueError):
            design_news._fetch_feed(unknown, NOW)
        request.assert_not_called()

    def test_reputable_design_analysis_sources_are_attributed_as_analysis(self):
        for publisher, host in (('Nielsen Norman Group', 'www.nngroup.com'),
                                ('Smashing Magazine', 'smashingmagazine.com')):
            feed = next(item for item in design_news.OFFICIAL_FEEDS if item.publisher == publisher)
            with self.subTest(publisher=publisher):
                items = design_news.parse_feed(rss(title='A researched design analysis', link=f'https://{host}/articles/design/'), feed, NOW)
                self.assertEqual(items[0]['source_kind'], 'design_analysis')
                self.assertEqual(items[0]['publisher'], publisher)
                self.assertEqual(items[0]['url'], f'https://{feed.article_hosts[0]}/articles/design/')
        self.assertIn('not a product release', design_news.CURRENT_DESIGN_RULES)

    def test_streaming_reader_stops_at_eight_complete_entries_before_historical_archive(self):
        entries = b''.join(rss(title=f'Design topic {i}').split(b'<channel>', 1)[1].split(b'</channel>', 1)[0]
                           for i in range(design_news.MAX_FEED_ENTRIES))
        prefix = b'<rss><channel>' + entries
        response = Mock(status_code=200)
        def chunks():
            yield prefix
            self.fail('Historical feed content should not be downloaded')
        response.iter_content.return_value = chunks()
        with patch('automation.design_news.requests.get', return_value=response):
            self.assertEqual(len(design_news._fetch_feed(design_news.OFFICIAL_FEEDS[0], NOW)),
                             design_news.MAX_FEED_ENTRIES)
        response.close.assert_called_once()

    def test_streaming_reader_rejects_oversized_and_unfinished_entries(self):
        for chunks in ([b'<rss><channel><item>' + b'x' * design_news.MAX_FEED_BYTES],
                       [rss().removesuffix(b'</channel></rss>')]):
            response = Mock(status_code=200)
            response.iter_content.return_value = chunks
            with self.subTest(size=sum(map(len, chunks))), patch('automation.design_news.requests.get', return_value=response):
                with self.assertRaises((ValueError, design_news.ElementTree.ParseError)):
                    design_news._fetch_feed(design_news.OFFICIAL_FEEDS[0], NOW)
            response.close.assert_called_once()

    def test_streaming_reader_rejects_split_doctype_before_feeding_xml_parser(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [b'<!DOC', b'TYPE rss><rss/>']
        with patch('automation.design_news.requests.get', return_value=response), self.assertRaises(ValueError):
            design_news._fetch_feed(design_news.OFFICIAL_FEEDS[0], NOW)

    def test_fetch_is_unauthenticated_direct_bounded_and_closes_response(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [rss()]
        with patch('automation.design_news.requests.get', return_value=response) as request:
            self.assertEqual(len(design_news._fetch_feed(design_news.OFFICIAL_FEEDS[0], NOW)), 1)
        self.assertEqual(request.call_args.args[0], design_news.OFFICIAL_FEEDS[0].url)
        self.assertIsInstance(request.call_args.kwargs['auth'], design_news._Unauthenticated)
        self.assertFalse(request.call_args.kwargs['allow_redirects'])
        self.assertEqual(request.call_args.kwargs['timeout'], (5, 10))
        self.assertTrue(request.call_args.kwargs['stream'])
        self.assertNotIn('Authorization', request.call_args.kwargs['headers'])
        response.close.assert_called_once()

    def test_redirect_never_follows_or_parses(self):
        response = Mock(status_code=302)
        with patch('automation.design_news.requests.get', return_value=response), self.assertRaises(ValueError):
            design_news._fetch_feed(design_news.OFFICIAL_FEEDS[0], NOW)
        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    def test_daily_refresh_is_reserved_before_network_and_survives_restart(self):
        runner = self.runner()
        def fetch(feed, now):
            saved = json.loads(runner.path.read_text(encoding='utf-8'))
            self.assertEqual(saved['design_news']['status'], 'refresh-reserved')
            return [source(feed)]
        with patch('automation.design_news._fetch_feed', side_effect=fetch) as request:
            self.assertEqual(len(design_news.daily_sources(runner)), len(design_news.OFFICIAL_FEEDS))
            self.assertEqual(len(design_news.daily_sources(self.runner())), len(design_news.OFFICIAL_FEEDS))
        self.assertEqual(request.call_count, len(design_news.OFFICIAL_FEEDS))

    def test_failed_feeds_are_not_retried_at_each_poll(self):
        runner = self.runner()
        with patch('automation.design_news._fetch_feed', side_effect=requests.Timeout('private error')) as request:
            self.assertEqual(design_news.daily_sources(runner), [])
            self.assertEqual(design_news.daily_sources(self.runner()), [])
        self.assertEqual(request.call_count, len(design_news.OFFICIAL_FEEDS))
        self.assertEqual(runner.state['design_news']['failed_feeds'], len(design_news.OFFICIAL_FEEDS))
        self.assertNotIn('private error', json.dumps(runner.state))

    def test_new_ist_day_allows_one_new_refresh(self):
        runner = self.runner()
        self.cache(runner, [source()])
        runner.now += timedelta(days=1)
        with patch('automation.design_news._fetch_feed', return_value=[]) as request:
            design_news.daily_sources(runner)
            design_news.daily_sources(runner)
        self.assertEqual(request.call_count, len(design_news.OFFICIAL_FEEDS))

    def test_reserved_crash_cache_fails_closed_without_refetch(self):
        runner = self.runner()
        runner.state['design_news'] = {'checked_day': runner.day, 'status': 'refresh-reserved', 'items': []}
        with patch('automation.design_news._fetch_feed') as request:
            self.assertEqual(design_news.daily_sources(runner), [])
        request.assert_not_called()

    def test_cache_cannot_introduce_a_different_host_or_future_date(self):
        runner = self.runner()
        self.cache(runner, [source(), {**source(), 'url': 'https://untrusted.example/post'}, source(days=-1)])
        self.assertEqual(len(design_news.daily_sources(runner)), 1)

    def test_source_must_still_be_fresh_at_tomorrows_publication(self):
        runner = self.runner()
        self.cache(runner, [source(days=14)])
        self.assertIsNone(design_news.select_source(runner, {}, publication_time=NOW + timedelta(days=1)))

    def test_used_url_or_title_exhausts_inventory_instead_of_repeating(self):
        runner = self.runner()
        self.cache(runner, [source()])
        for text in (source()['url'], 'A point of view on Stacked pull requests.'):
            with self.subTest(text=text):
                runner.state['actions'] = {'old': {'kind': 'post', 'text': text, 'at': NOW.isoformat()}}
                self.assertIsNone(design_news.select_source(runner, {}))

    def test_provider_rotation_prefers_an_unused_publisher_over_its_newest_article(self):
        runner = self.runner()
        linear, penpot = design_news.OFFICIAL_FEEDS[:2]
        self.cache(runner, [source(linear, days=1), source(penpot, title='Penpot layouts', days=3)])
        runner.state['actions']['old'] = {'kind': 'post', 'at': NOW.isoformat(),
                                        'text': 'Source: https://linear.app/changelog/another-change'}
        self.assertEqual(design_news.select_source(runner, {})['publisher'], 'Penpot')

    def test_article_host_alias_and_tracking_do_not_create_a_second_source(self):
        runner = self.runner()
        feed = next(item for item in design_news.OFFICIAL_FEEDS if item.publisher == 'Smashing Magazine')
        self.cache(runner, [source(feed, title='Design analysis')])
        runner.state['actions']['old'] = {'kind': 'post', 'at': NOW.isoformat(),
                                        'text': 'Source: https://smashingmagazine.com/changelog/design-change?utm_source=feed'}
        self.assertIsNone(design_news.select_source(runner, {}))

    def test_policy_keeps_mornings_grounded_after_calendar_ends(self):
        self.assertTrue(design_news.requires_current_design(None, {'current_design_hours': [9]}, 9))
        self.assertFalse(design_news.requires_current_design(None, {'current_design_hours': [9]}, 18))
        self.assertFalse(design_news.requires_current_design({'source_mode': 'owner_work'}, {'current_design_hours': [9]}, 9))

    def test_context_has_exact_attribution_but_never_becomes_owner_source_notes(self):
        context = {'source_notes': []}
        design_news.apply_source_context(context, source())
        self.assertEqual(context['source_notes'], [])
        self.assertTrue(context['current_design_source']['data_only'])
        self.assertEqual(context['current_design_source']['published_at'], source()['published_at'])

    def test_final_text_keeps_source_link_and_refuses_unsupported_firsthand_testing(self):
        cited = design_news.finalize_source_post(TEXT, source(), POLICY)
        self.assertTrue(cited.endswith('Source: ' + source()['url']))
        self.assertEqual(design_news.finalize_source_post(cited, source(), POLICY), cited)
        for claim in ('I tested Linear with my team.', "I've used Linear for this.", 'We tried the update yesterday.'):
            with self.subTest(claim=claim):
                self.assertIsNone(design_news.finalize_source_post(claim, source(), POLICY))
        approved = {'source_notes': ['I tested Linear with my team.']}
        self.assertIsNotNone(design_news.finalize_source_post('I tested Linear with my team.', source(), approved))

    def test_due_slot_passes_source_and_grounding_rules_and_preserves_link(self):
        runner = self.runner()
        self.cache(runner, [source()])
        self.plan()
        with patch('automation.twin.ROOT', self.path), patch('automation.twin.skills', return_value='Skill: linkedin-post-writer'):
            runner.post()
        task, context, _ = runner.generate.call_args.args
        self.assertIn(design_news.CURRENT_DESIGN_RULES, task)
        self.assertEqual(context['current_design_source']['published_at'], source()['published_at'])
        self.assertIn(source()['url'], runner.state['actions']['post:2026-10-18:0900']['text'])

    def test_due_slot_without_source_skips_generation_and_retains_cooldown(self):
        runner = self.runner()
        self.cache(runner, [])
        self.plan()
        with patch('automation.twin.ROOT', self.path), patch('automation.twin.skills', return_value='Skill: linkedin-post-writer'):
            runner.post()
            runner.post()
        runner.generate.assert_not_called()
        self.assertFalse(runner.state['actions'])
        self.assertIn('post:2026-10-18:0900', runner.state['post_queue']['attempts'])

    def test_next_day_source_slot_uses_text_and_cites_without_generic_visual_fallback(self):
        runner = self.runner()
        self.cache(runner, [source()])
        self.plan(day='2026-10-19', format='image')
        with patch('automation.post_queue.ROOT', self.path), patch('automation.post_queue.prepare_visual') as visual:
            self.assertEqual(prepare_next_day_posts(runner), ['post:2026-10-19:0900'])
        visual.assert_not_called()
        task, context, _ = runner.generate.call_args.args
        self.assertIn(design_news.CURRENT_DESIGN_RULES, task)
        self.assertEqual(context['current_design_source']['url'], source()['url'])
        self.assertIn(source()['url'], runner.state['actions']['post:2026-10-19:0900']['text'])

    def test_next_day_unavailable_source_skips_without_consuming_post_quota(self):
        runner = self.runner()
        self.cache(runner, [])
        self.plan(day='2026-10-19')
        with patch('automation.post_queue.ROOT', self.path):
            self.assertEqual(prepare_next_day_posts(runner), [])
            self.assertEqual(prepare_next_day_posts(runner), [])
        runner.generate.assert_not_called()
        self.assertNotIn('2026-10-19', runner.state['days'])
        self.assertIn('post:2026-10-19:0900', runner.state['post_queue']['attempts'])


if __name__ == '__main__':
    unittest.main()
