# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import espmirror as mirror
import espmirror_iris as iris

DEVICE = 'a' * 32


class IrisLifecycleTests(unittest.TestCase):
    def test_request_preserves_operation_id_and_http_status(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{}'
        with patch.object(iris, 'urlopen', return_value=response) as opened:
            iris.request_json('http://localhost', '/start', {}, operation_id='example')
        self.assertEqual(opened.call_args.args[0].get_header('X-operation-id'), 'example')
        with patch.object(iris, 'urlopen', side_effect=HTTPError('http://localhost', 409, 'busy', {}, None)):
            with self.assertRaises(iris.GatewayHTTPError) as caught:
                iris.request_json('http://localhost', '/stop', {})
        self.assertEqual(caught.exception.status, 409)

    @unittest.skipUnless(importlib.util.find_spec('websockets'), 'websockets is needed for startup tests')
    def test_delayed_start_is_reconciled_before_stopping_owned_stream(self):
        client = iris.IrisClient('http://localhost', DEVICE)
        calls, operation_ids = [], []
        pending = True

        def request(base, path, body=None, **kwargs):
            calls.append(path)
            if path.endswith('/mirror/start'):
                operation_ids.append(kwargs['operation_id'])
                raise TimeoutError('reply exceeded HTTP timeout')
            if path.startswith('/v2/operations/'):
                self.assertEqual(path, '/v2/operations/' + operation_ids[0])
                return {'status': 'running'} if pending else {'status': 'succeeded', 'result': {'reused': False}}
            self.assertTrue(path.endswith('/mirror/stop'))
            return {}

        connection = Mock(__enter__=Mock(return_value=Mock()), __exit__=Mock(return_value=False))
        with patch('websockets.sync.client.connect', return_value=connection), \
                patch.object(client, 'rpc', return_value=json.dumps(dict(w=3, h=2)).encode()), \
                patch.object(iris, 'request_json', side_effect=request):
            with self.assertRaises(TimeoutError):
                client.open()
            with self.assertRaisesRegex(ConnectionError, 'unresolved'):
                client.cleanup()
            self.assertEqual(str(uuid.UUID(operation_ids[0])), operation_ids[0])
            self.assertFalse(client.closed)
            self.assertIsNone(client.socket)
            self.assertFalse(any(path.endswith('/mirror/stop') for path in calls))
            pending = False
            client.cleanup()
        self.assertTrue(client.closed)
        self.assertEqual(sum(path.endswith('/mirror/start') for path in calls), 1)
        self.assertEqual(sum(path.endswith('/mirror/stop') for path in calls), 1)

    @unittest.skipUnless(importlib.util.find_spec('websockets'), 'websockets is needed for startup tests')
    def test_lost_reused_start_reply_does_not_stop_another_viewer(self):
        client = iris.IrisClient('http://localhost', DEVICE)
        calls = []

        def request(base, path, body=None, **kwargs):
            calls.append(path)
            if path.endswith('/mirror/start'):
                raise TimeoutError('lost reply')
            return {'status': 'succeeded', 'result': {'reused': True}}

        connection = Mock(__enter__=Mock(return_value=Mock()), __exit__=Mock(return_value=False))
        with patch('websockets.sync.client.connect', return_value=connection), \
                patch.object(client, 'rpc', return_value=json.dumps(dict(w=3, h=2)).encode()), \
                patch.object(iris, 'request_json', side_effect=request):
            with self.assertRaises(TimeoutError):
                client.open()
            client.cleanup()
        self.assertTrue(client.closed)
        self.assertFalse(client.owns_stream)
        self.assertFalse(any(path.endswith('/mirror/stop') for path in calls))

    def test_missing_device_finishes_stop(self):
        client = iris.IrisClient('http://localhost', DEVICE)
        client.owns_stream = True
        with patch.object(iris, 'request_json', side_effect=iris.GatewayHTTPError(404, '/stop')):
            client.cleanup()
        self.assertTrue(client.closed)
        self.assertFalse(client.owns_stream)

    def test_permanent_stop_conflict_has_bounded_retries_and_is_reported(self):
        link = mirror.IrisLink('iris:' + DEVICE)
        client = Mock()
        client.open.side_effect = ConnectionError('lost stream')
        client.cleanup.side_effect = iris.GatewayHTTPError(409, '/stop')
        output = io.StringIO()
        with patch.object(iris, 'IrisClient', return_value=client) as factory, \
                patch.object(link.stopped, 'wait', return_value=False), contextlib.redirect_stderr(output):
            link.run()
        self.assertEqual(client.cleanup.call_count, 4)  # Initial cleanup plus three retries.
        self.assertEqual(factory.call_count, 1)
        self.assertIn('cleanup failed after 3 retries', output.getvalue())
        self.assertIn('HTTP 409', output.getvalue())
