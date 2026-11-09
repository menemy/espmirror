# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import base64
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import espmirror as mirror
import espmirror_iris as iris


class IrisCleanupDeadlineTests(unittest.TestCase):
    def test_close_only_interrupts_io_without_rpc_lock_or_websocket_handshake(self):
        client = iris.IrisClient('http://localhost', 'a' * 32)
        client.owns_stream = True
        client.keys_down.add(1)
        client.socket = Mock()
        locked, release = threading.Event(), threading.Event()

        def writer():
            with client.close_lock:
                locked.set()
                release.wait(2)

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(locked.wait(1))
        try:
            with patch.object(iris, 'request_json') as request:
                started = time.monotonic()
                client.close()
                self.assertLess(time.monotonic() - started, .1)
                client.socket.socket.shutdown.assert_called_once_with(iris.socket.SHUT_RDWR)
                client.socket.close.assert_not_called()
                request.assert_not_called()
                with self.assertRaises(TimeoutError):
                    client.cleanup(deadline=time.monotonic() + .04)
                self.assertLess(time.monotonic() - started, .3)
                request.assert_not_called()
        finally:
            release.set()
            thread.join(1)
        self.assertTrue(client.owns_stream)
        self.assertEqual(client.keys_down, {1})
        self.assertFalse(client.closed)

    def test_all_releases_share_one_deadline_and_retain_unfinished_input(self):
        client = iris.IrisClient('http://localhost', 'a' * 32)
        client.keys_down.update((1, 2, 3))
        client.pointer_down = True
        now, calls = [100.0], []

        def request(base, path, body=None, *, deadline):
            calls.append(deadline)
            now[0] += .6
            iris.remaining(deadline)
            return {'payload_base64': base64.b64encode(bytes.fromhex(body['payload_hex'])).decode()}

        with patch.object(iris.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(iris, 'request_json', side_effect=request):
            with self.assertRaises(TimeoutError):
                client.cleanup(deadline=101.0)
        self.assertEqual(calls, [101.0, 101.0])
        self.assertEqual(len(client.keys_down), 2)
        self.assertTrue(client.pointer_down)
        self.assertFalse(client.closed)

    def test_expired_budget_does_not_send_stop(self):
        client = iris.IrisClient('http://localhost', 'a' * 32)
        client.owns_stream = True
        with patch.object(iris, 'request_json') as request:
            with self.assertRaises(TimeoutError):
                client.cleanup(deadline=time.monotonic() - 1)
            request.assert_not_called()
        self.assertTrue(client.owns_stream)

    def test_cancel_interrupts_active_http_writer_and_keeps_uncertain_press_for_cleanup(self):
        entered, release = threading.Event(), threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Length', '100')
                self.end_headers()
                self.wfile.flush()
                entered.set()
                release.wait(2)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
        serving.start()
        client = iris.IrisClient('http://127.0.0.1:%d' % server.server_port, 'a' * 32)
        errors = []

        def send():
            try:
                client.send(bytes((6, 1, 1)))
            except Exception as error:
                errors.append(error)

        writer = threading.Thread(target=send)
        writer.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            client.cancel()
            self.assertLess(time.monotonic() - started, .1)
            writer.join(.5)
            self.assertFalse(writer.is_alive())
            self.assertTrue(errors)
            self.assertFalse(client.http_sockets)
            self.assertEqual(client.keys_down, {1})
            with patch.object(client, 'rpc', return_value=b'\x01\x00') as rpc:
                client.cleanup()
            self.assertEqual(rpc.call_args.args, (0x4553, 1, b'\x01\x00'))
            self.assertFalse(client.keys_down)
        finally:
            release.set()
            client.cancel()
            writer.join(1)
            server.shutdown()
            server.server_close()
            serving.join(1)

    def test_worker_owns_stop_rpc_and_gui_close_does_not_repeat_it(self):
        client = iris.IrisClient('http://localhost', 'a' * 32)
        link = mirror.IrisLink('iris:' + 'a' * 32)
        client.board = {'w': 3, 'h': 2}
        client.owns_stream = True
        assembler = SimpleNamespace(width=3, height=2, bytes_received=0)
        calls = []

        def receive():
            link.alive = False
            link.stopped.set()
            raise ConnectionError('shutdown')

        def request(base, path, body=None, *, deadline):
            calls.append((threading.get_ident(), path, deadline))
            self.assertGreater(deadline, time.monotonic())
            return {}

        with patch.object(iris, 'IrisClient', return_value=client), \
                patch.object(client, 'open', return_value=assembler), \
                patch.object(client, 'receive', side_effect=receive), \
                patch.object(iris, 'request_json', side_effect=request):
            link.start()
            link.thread.join(2)
            self.assertFalse(link.thread.is_alive())
            link.close()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0][0], threading.get_ident())
        self.assertTrue(calls[0][1].endswith('/mirror/stop'))
        self.assertTrue(client.closed)

    def test_http_deadline_stops_trickled_headers_and_body(self):
        stop = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    self.wfile.write(b'HTTP/1.1 200 OK\r\n')
                    if self.path == '/headers':
                        self.wfile.write(b'X-Slow: ')
                    else:
                        self.wfile.write(b'Content-Length: 10000\r\n\r\n')
                    self.wfile.flush()
                    while not stop.wait(.005):
                        self.wfile.write(b' ')
                        self.wfile.flush()
                except OSError:
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
        thread.start()
        try:
            for path in ('/headers', '/body'):
                with self.subTest(path=path):
                    started = time.monotonic()
                    with self.assertRaises((OSError, ValueError, http.client.HTTPException)):
                        iris.request_json('http://127.0.0.1:%d' % server.server_port, path, {}, deadline=started + .08)
                    self.assertLess(time.monotonic() - started, .5)
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join(1)
