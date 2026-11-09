# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import base64
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import struct
import sys
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
import espmirror_iris as iris
import espmirror as mirror

DEVICE = '0123456789abcdef0123456789abcdef'
DESC = dict(x=0, y=0, width=3, height=2, stride=8, format=1)
BOARD = dict(name='Fake ESP-Mosaico', version=2, w=3, h=2, buttons=[], touch=True, audio=True, keyboard=True)


def tile(y, pixels, frame=1, stream=7, **overrides):
    description = {**DESC, 'y': y, 'height': 1, **overrides}
    metadata = json.dumps(dict(stream_id=stream, frame_id=frame, description=description)).encode()
    return struct.pack('<I', len(metadata)) + metadata + struct.pack('<4H', *pixels, 0)


class IrisFrames(unittest.TestCase):
    def test_complete_frame_padding_and_loss(self):
        assembler = iris.FrameAssembler(DESC, 7)
        self.assertIsNone(assembler.feed(tile(0, [1, 2, 3])))
        frame = assembler.feed(tile(1, [4, 5, 6]))
        np.testing.assert_array_equal(frame, [[1, 2, 3], [4, 5, 6]])
        self.assertEqual(assembler.bytes_received, 16)
        self.assertIsNone(assembler.feed(tile(0, [7, 8, 9], frame=2)))
        self.assertIsNone(assembler.feed(tile(1, [1, 1, 1], frame=3)))
        self.assertIsNone(assembler.feed(tile(0, [7, 8, 9], frame=4)))
        assembler.feed('{"kind":"history_gap"}')
        self.assertIsNone(assembler.feed(tile(1, [1, 1, 1], frame=4)))
        self.assertIsNone(assembler.feed(tile(0, [1, 2, 3], stream=8)))
        self.assertIsNone(assembler.feed(tile(1, [1, 2, 3], stream=8)))
        np.testing.assert_array_equal(frame, [[1, 2, 3], [4, 5, 6]])

    def test_bad_geometry_and_tiles(self):
        for bad in ({'width': 0}, {'height': 4097}, {'stride': 4}, {'stride': 9}, {'format': 3}, {'x': 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                iris.FrameAssembler({**DESC, **bad}, 7)
        for message in (b'', b'\xff\xff\xff\xff', tile(2, [1, 2, 3]), tile(0, [1, 2, 3], width=4), tile(0, [1, 2, 3])[:-1]):
            with self.subTest(message=message), self.assertRaises(ValueError):
                iris.FrameAssembler(DESC, 7).feed(message)

    def test_target_and_url_validation(self):
        self.assertEqual(mirror.parse_host('iris:' + DEVICE), ('iris:' + DEVICE, mirror.PORT))
        for target in ('iris:abc', 'iris:../bad'):
            with self.assertRaises(ValueError):
                mirror.parse_host(target)
        for url in ('file:///etc/passwd', 'http://host/path', 'http://user:password@host', 'http://host/?x=1'):
            with self.assertRaises(ValueError):
                iris.gateway_url(url)


@unittest.skipUnless(importlib.util.find_spec('websockets'), 'websockets is needed for fake Gateway tests')
class IrisGateway(unittest.TestCase):
    def setUp(self):
        from websockets.sync.client import connect
        from websockets.sync.server import serve
        self.requests = []
        self.started = threading.Event()
        self.finished = threading.Event()
        self.reused = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, data):
                payload = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self.answer({'devices': [{'device_id': DEVICE, 'connected': True, 'data_available': True},
                                         {'device_id': 'a' * 32, 'connected': False}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append((self.path, body))
                if self.path.endswith('/mirror/start'):
                    outer.started.set()
                    self.answer({'mirror': dict(description=DESC, stream_id=7, reused=outer.reused)})
                elif self.path.endswith('/mirror/stop'):
                    outer.finished.set()
                    self.answer({'mirror': {'state': 'stopped'}})
                else:
                    payload = json.dumps(BOARD).encode() if body['method_id'] == 2 else bytes.fromhex(body['payload_hex'])
                    self.answer({'payload_base64': base64.b64encode(payload).decode()})

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.url = 'http://127.0.0.1:%d' % self.http.server_port

        def stream(ws):
            try:
                if not self.started.wait(3):
                    return
                frame = 0
                while not self.finished.wait(.03):
                    frame += 1
                    ws.send(tile(0, [1, 2, 3], frame=frame))
                    ws.send(tile(1, [4, 5, 6], frame=frame))
            except Exception:
                pass

        self.ws = serve(stream, '127.0.0.1', 0)
        self.ws_thread = threading.Thread(target=self.ws.serve_forever, daemon=True)
        self.ws_thread.start()
        ws_port = self.ws.socket.getsockname()[1]

        def open_ws(url, **kwargs):
            self.assertEqual(url, self.url.replace('http:', 'ws:') + '/v2/devices/' + DEVICE + '/streams/screen')
            return connect('ws://127.0.0.1:%d/streams/screen' % ws_port, **kwargs)

        self.patch = patch('websockets.sync.client.connect', side_effect=open_ws)
        self.patch.start()

    def tearDown(self):
        self.finished.set()
        self.patch.stop()
        self.ws.shutdown()
        self.http.shutdown()
        self.http.server_close()
        self.ws_thread.join(2)
        self.http_thread.join(2)

    def test_http_websocket_rpc_and_close(self):
        self.assertEqual([item['device_id'] for item in iris.discover(self.url)], [DEVICE])
        client = iris.IrisClient(self.url, DEVICE)
        try:
            client.open()
            self.assertFalse(client.board['audio'])
            self.assertFalse(client.board['keyboard'])
            frame = None
            while frame is None:
                frame = client.receive()
            np.testing.assert_array_equal(frame, [[1, 2, 3], [4, 5, 6]])
            for down in (1, 1, 0):
                client.send(struct.pack('<BHH', down, 2, 1))
            client.send(bytes((6, 2, 1)))
            client.send(bytes((6, 2, 0)))
            payloads = [bytes.fromhex(body['payload_hex']) for path, body in self.requests
                        if path.endswith('/rpc/raw') and body['method_id'] == 1]
            self.assertEqual([struct.unpack('<BBhhHI', p)[0] for p in payloads[:3]], [0, 1, 2])
            self.assertEqual(payloads[-2:], [b'\x02\x01', b'\x02\x00'])
        finally:
            client.close()
        self.assertTrue(self.finished.is_set())
        client.close()
        self.assertEqual(sum(path.endswith('/mirror/stop') for path, _ in self.requests), 1)

    def test_reused_stream_releases_own_input_without_stopping(self):
        self.reused = True
        client = iris.IrisClient(self.url, DEVICE)
        client.open()
        client.send(bytes((6, 3, 1)))
        client.close()
        self.assertFalse(self.finished.is_set())
        self.assertEqual(self.requests[-1][1]['payload_hex'], '0300')

    def test_viewer_link_receives_and_stops(self):
        link = mirror.make_link('iris:' + DEVICE, gateway=self.url)
        board_event = threading.Event()
        link.on_board = lambda info: board_event.set()
        try:
            link.start()
            self.assertTrue(board_event.wait(3))
            end = time.monotonic() + 3
            while not link.changes and time.monotonic() < end:
                time.sleep(.01)
            self.assertGreater(link.changes, 0)
            self.assertGreater(link.bytes_received, 0)
            self.assertFalse(link.audio_capable)
        finally:
            link.close()
        self.assertFalse(link.thread.is_alive())
        self.assertTrue(self.finished.is_set())

    def test_failed_stop_retains_ownership_for_retry(self):
        client = iris.IrisClient(self.url, DEVICE)
        client.open()
        with patch.object(iris, 'request_json', side_effect=ConnectionError('temporary failure')):
            with self.assertRaises(ConnectionError):
                client.close()
        self.assertTrue(client.owns_stream)
        self.assertFalse(client.closed)
        self.assertIsNone(client.socket)
        client.close()
        self.assertTrue(client.closed)
        self.assertTrue(self.finished.is_set())

    def test_failed_release_does_not_skip_other_input(self):
        self.reused = True
        client = iris.IrisClient(self.url, DEVICE)
        client.open()
        client.send(bytes((6, 1, 1)))
        client.send(bytes((6, 2, 1)))
        client.send(struct.pack('<BHH', 1, 2, 1))
        rpc = client.rpc

        def fail_one(service, method, payload):
            if service == 0x4553 and payload == b'\x01\x00':
                raise ConnectionError('temporary key failure')
            return rpc(service, method, payload)

        with patch.object(client, 'rpc', side_effect=fail_one):
            with self.assertRaises(ConnectionError):
                client.close()
        self.assertEqual(client.keys_down, {1})
        self.assertFalse(client.pointer_down)
        self.assertFalse(client.closed)
        client.close()
        self.assertTrue(client.closed)
        self.assertFalse(self.finished.is_set())

    def test_viewer_shutdown_retries_failed_stop(self):
        request = iris.request_json
        stops = []

        def fail_once(base, path, body=None):
            if path.endswith('/mirror/stop'):
                stops.append(path)
                if len(stops) == 1:
                    raise ConnectionError('temporary stop failure')
            return request(base, path, body)

        with patch.object(iris, 'request_json', side_effect=fail_once):
            link = mirror.make_link('iris:' + DEVICE, gateway=self.url).start()
            self.assertTrue(self.started.wait(3))
            link.close()
        self.assertFalse(link.thread.is_alive())
        self.assertEqual(len(stops), 2)
        self.assertTrue(self.finished.is_set())

    def test_close_while_loading_metadata_does_not_start_stream(self):
        entered, release = threading.Event(), threading.Event()
        request = iris.request_json

        def delayed(*args, **kwargs):
            if args[1].endswith('/rpc/raw'):
                entered.set()
                release.wait(3)
            return request(*args, **kwargs)

        link = mirror.make_link('iris:' + DEVICE, gateway=self.url)
        with patch.object(iris, 'request_json', side_effect=delayed):
            link.start()
            self.assertTrue(entered.wait(2))
            closer = threading.Thread(target=link.close)
            closer.start()
            self.assertTrue(link.stopped.wait(2))
            release.set()
            closer.join(4)
        self.assertFalse(link.thread.is_alive())
        self.assertFalse(any(path.endswith('/mirror/start') for path, _ in self.requests))
