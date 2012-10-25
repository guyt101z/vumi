"""Tests for vumi.sentry."""

import logging
import base64
import json
import sys
import traceback

from twisted.trial.unittest import TestCase
from twisted.internet.defer import inlineCallbacks, Deferred
from twisted.web import http
from twisted.python.failure import Failure

from vumi.tests.utils import MockHttpServer, LogCatcher, import_skip, mocking
from vumi.sentry import (quiet_get_page, SentryLogObserver, vumi_raven_client,
                         SentryLoggerService)


class TestQuietGetPage(TestCase):

    @inlineCallbacks
    def setUp(self):
        self.mock_http = MockHttpServer(self._handle_request)
        yield self.mock_http.start()

    @inlineCallbacks
    def tearDown(self):
        yield self.mock_http.stop()

    def _handle_request(self, request):
        request.setResponseCode(http.OK)
        request.do_not_log = True
        return "Hello"

    @inlineCallbacks
    def test_request(self):
        with LogCatcher() as lc:
            result = yield quiet_get_page(self.mock_http.url)
            self.assertEqual(lc.logs, [])
        self.assertEqual(result, "Hello")


class DummySentryClient(object):
    def __init__(self):
        self.exceptions = []
        self.messages = []
        self.teardowns = 0

    def captureMessage(self, *args, **kwargs):
        self.messages.append((args, kwargs))

    def captureException(self, *args, **kwargs):
        self.exceptions.append((args, kwargs))

    def teardown(self):
        self.teardowns += 1


class TestSentryLogObserver(TestCase):
    def setUp(self):
        self.client = DummySentryClient()
        self.obs = SentryLogObserver(self.client)

    def test_level_for_event(self):
        for expected_level, event in [
            (logging.WARN, {'logLevel': logging.WARN}),
            (logging.ERROR, {'isError': 1}),
            (logging.INFO, {}),
        ]:
            self.assertEqual(self.obs.level_for_event(event), expected_level)

    def test_logger_for_event(self):
        self.assertEqual(self.obs.logger_for_event({'system': 'foo,bar'}),
                         'foo,bar')
        self.assertEqual(self.obs.logger_for_event({}), 'unknown')

    def test_log_failure(self):
        e = ValueError("foo error")
        f = Failure(e)
        self.obs({'failure': f, 'system': 'test.log'})
        self.assertEqual(self.client.exceptions, [
            (((type(e), e, None),),
             {'data': {'level': 20, 'logger': 'test.log'}}),
        ])

    def test_log_traceback(self):
        try:
            raise ValueError("foo")
        except ValueError:
            f = Failure(*sys.exc_info())
        self.obs({'failure': f})
        [call_args] = self.client.exceptions
        exc_info = call_args[0][0]
        tb = ''.join(traceback.format_exception(*exc_info))
        self.assertTrue('raise ValueError("foo")' in tb)

    def test_log_message(self):
        self.obs({'message': ["a"], 'system': 'test.log'})
        self.assertEqual(self.client.messages, [
            (('a',),
             {'data': {'level': 20, 'logger': 'test.log'}})
        ])


class TestSentryLoggerSerivce(TestCase):

    def setUp(self):
        import vumi.sentry
        self.client = DummySentryClient()
        self.patch(vumi.sentry, 'vumi_raven_client', lambda dsn: self.client)
        self.service = SentryLoggerService("http://example.com/")

    @inlineCallbacks
    def test_stop_not_running(self):
        yield self.service.stopService()
        self.assertFalse(self.service.running)

    @inlineCallbacks
    def test_start_stop(self):
        self.assertFalse(self.service.registered())
        self.assertEqual(self.client.teardowns, 0)
        yield self.service.startService()
        self.assertTrue(self.service.registered())
        yield self.service.stopService()
        self.assertFalse(self.service.registered())
        self.assertEqual(self.client.teardowns, 1)


class TestRavenUtilityFunctions(TestCase):

    def setUp(self):
        try:
            import raven
        except ImportError, e:
            import_skip(e, 'raven')

    def mk_sentry_dsn(self):
        proj_user = "4c96ae4ca518483192dd9917c03847c4"
        proj_key = "05d9515b5c504cc7bf180597fd6f67"
        proj_no = 2
        host, port = "example.com", "30000"
        dsn = "http://%s:%s@%s:%s/%s" % (proj_user, proj_key, host, port,
                                         proj_no)
        return dsn

    def parse_call(self, sentry_call):
        postdata = sentry_call.kwargs['postdata']
        return json.loads(base64.b64decode(postdata).decode('zlib'))

    def test_vumi_raven_client_capture_message(self):
        dsn = self.mk_sentry_dsn()
        mock_page = mocking(quiet_get_page)
        mock_page.return_value = Deferred()
        with mock_page:
            client = vumi_raven_client(dsn)
            client.captureMessage("my message")
        [sentry_call] = mock_page.history
        sentry_data = self.parse_call(sentry_call)
        self.assertEqual(sentry_data['message'], "my message")
