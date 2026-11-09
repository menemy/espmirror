# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import base64
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import queue
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
class GatewayFixture(unittest.TestCase):
    encoded = False

    def setUp(self):
        from websockets.sync.client import connect
        from websockets.sync.server import serve
        self.requests = []
        self.operations = {}
        self.started = threading.Event()
        self.finished = threading.Event()
        self.reused = False
        self.control = queue.Queue()
        self.fault = None
        self.fault_used = False
        self.board = {**BOARD, **(dict(iris_codec=1, iris_chunk=256) if self.encoded else {})}
        self.description = dict(width=3, height=2, format=iris.ENCODED_FORMAT, quality=1) if self.encoded else DESC
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
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Cancellation may close HTTP before the durable result is read.

            def do_GET(self):
                if self.path.startswith('/v2/operations/'):
                    self.answer(outer.operations[self.path.rsplit('/', 1)[1]])
                    return
                self.answer({'devices': [{'device_id': DEVICE, 'connected': True, 'data_available': True},
                                         {'device_id': 'a' * 32, 'connected': False}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append((self.path, body))
                if self.path.endswith('/mirror/start'):
                    result = dict(description=outer.description, stream_id=7, reused=outer.reused)
                    outer.operations[self.headers['X-Operation-ID']] = {'status': 'succeeded', 'result': result}
                    outer.started.set()
                    self.answer({'mirror': result})
                elif self.path.endswith('/mirror/stop'):
                    outer.finished.set()
                    self.answer({'mirror': {'state': 'stopped'}})
                else:
                    payload = json.dumps(outer.board).encode() if body['method_id'] == 2 else bytes.fromhex(body['payload_hex'])
                    if body['method_id'] == 3:
                        outer.control.put(struct.unpack('<BIII', payload))
                    self.answer({'payload_base64': base64.b64encode(payload).decode()})

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.url = 'http://127.0.0.1:%d' % self.http.server_port

        def stream(ws):
            try:
                if not self.started.wait(3):
                    return
                if self.encoded:
                    self.encoded_stream(ws)
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
            self.assertEqual(url, self.url.replace('http:', 'ws:') + '/v2/devices/' + DEVICE + '/streams/' + ('image' if self.encoded else 'screen'))
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

class IrisGateway(GatewayFixture):
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
            client.cleanup()
        self.assertTrue(self.finished.is_set())
        client.cleanup()
        self.assertEqual(sum(path.endswith('/mirror/stop') for path, _ in self.requests), 1)

    def test_reused_stream_releases_own_input_without_stopping(self):
        self.reused = True
        client = iris.IrisClient(self.url, DEVICE)
        client.open()
        client.send(bytes((6, 3, 1)))
        client.cleanup()
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
                client.cleanup()
        self.assertTrue(client.owns_stream)
        self.assertFalse(client.closed)
        self.assertIsNone(client.socket)
        client.cleanup()
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

        def fail_one(service, method, payload, **kwargs):
            if service == 0x4553 and payload == b'\x01\x00':
                raise ConnectionError('temporary key failure')
            return rpc(service, method, payload, **kwargs)

        with patch.object(client, 'rpc', side_effect=fail_one):
            with self.assertRaises(ConnectionError):
                client.cleanup()
        self.assertEqual(client.keys_down, {1})
        self.assertFalse(client.pointer_down)
        self.assertFalse(client.closed)
        client.cleanup()
        self.assertTrue(client.closed)
        self.assertFalse(self.finished.is_set())

    def test_viewer_shutdown_retries_failed_stop(self):
        request = iris.request_json
        stops = []

        def fail_once(base, path, body=None, **kwargs):
            if path.endswith('/mirror/stop'):
                stops.append(path)
                if len(stops) == 1:
                    raise ConnectionError('temporary stop failure')
            return request(base, path, body, **kwargs)

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


def encoded_frame(coding=0, value=0x1234, state=0):
    if coding == 0:
        data = struct.pack('<6H', *([value] * 6))
    elif coding in (1, 2, 3):
        data = struct.pack('<HH', 6, value)
    else:
        # QOI RGB red, followed by five repetitions.
        data = bytes([0xfe, 255, 0, 0, 0xc4])
    return struct.pack('<BBHHH', ord('F'), state, 1, 3, 2) + struct.pack('<HHHHBBI', 0, 0, 3, 2, coding, 0, len(data)) + data


def encoded_chunk(payload, epoch=123, number=1, offset=0, count=None, dropped=0, stream=7, **extra):
    data = payload[offset:offset + count] if count is not None else payload[offset:]
    flags = (1 if offset == 0 else 0) | (2 if offset + len(data) == len(payload) else 0)
    metadata = dict(kind='media', channel=4, stream_id=stream, frame_id=number, flags=flags,
                    dropped=dropped, description=dict(width=3, height=2, stride=0, x=0, y=0,
                                                       format=iris.ENCODED_FORMAT, quality=1), **extra)
    wire = json.dumps(metadata).encode()
    return struct.pack('<I', len(wire)) + wire + iris.CHUNK_HEADER.pack(b'ESM1', epoch, number, offset, len(payload)) + data


class EncodedFrames(unittest.TestCase):
    def assembler(self):
        result = iris.EncodedAssembler(3, 2, 7, 256, mirror.apply_frame)
        result.reset(123)
        return result

    def test_all_codecs_share_reference(self):
        assembler = self.assembler()
        expected = 0
        for number, coding in enumerate((0, 1, 2, 3, 4), 1):
            payload = encoded_frame(coding)
            frame = None
            for offset in range(0, len(payload), 7):
                frame = assembler.feed(encoded_chunk(payload, number=number, offset=offset, count=7))
                self.assertEqual(assembler.ack, (123, number, min(offset + 7, len(payload))))
            expected = expected ^ 0x1234 if coding == 3 else 0xf800 if coding == 4 else 0x1234
            np.testing.assert_array_equal(frame, np.full((2, 3), expected))

    def test_loss_discards_partial_message_and_new_epoch_resets_delta(self):
        payload = encoded_frame()
        for fault in ('counter', 'gap', 'offset', 'message', 'truncated', 'format', 'flags'):
            with self.subTest(fault=fault):
                assembler = self.assembler()
                assembler.feed(encoded_chunk(payload, count=7))
                if fault == 'gap':
                    bad = '{"kind":"history_gap","dropped_events":2}'
                else:
                    bad = encoded_chunk(payload, offset=8 if fault == 'offset' else 7,
                                        number=2 if fault == 'message' else 1,
                                        dropped=1 if fault == 'counter' else 0)
                    if fault == 'truncated':
                        bad = bad[:-1]
                    if fault == 'format':
                        bad = bad.replace(b'17747', b'11111')
                    if fault == 'flags':
                        bad = bad.replace(b'"flags": 2', b'"flags": 3')
                with self.assertRaises(iris.StreamGap):
                    assembler.feed(bad)
                assembler.reset(124)
                self.assertIsNone(assembler.feed(encoded_chunk(payload, offset=7)))
                self.assertIsNone(assembler.ack)
                frame = assembler.feed(encoded_chunk(encoded_frame(3), epoch=124))
                np.testing.assert_array_equal(frame, np.full((2, 3), 0x1234))

    def test_partial_refresh_waits_for_full_coverage(self):
        assembler = self.assembler()
        msg = struct.pack('<BBHHH', ord('F'), 0, 1, 3, 2)
        row = struct.pack('<HHHHBBI', 0, 0, 3, 1, 1, 0, 4) + struct.pack('<HH', 3, 5)
        self.assertIsNone(assembler.feed(encoded_chunk(msg + row)))
        row = struct.pack('<HHHHBBI', 0, 1, 3, 1, 1, 0, 4) + struct.pack('<HH', 3, 6)
        frame = assembler.feed(encoded_chunk(msg + row, number=2))
        np.testing.assert_array_equal(frame, [[5, 5, 5], [6, 6, 6]])

    def test_stale_stream_bad_payload_and_bounds(self):
        assembler = self.assembler()
        self.assertIsNone(assembler.feed(encoded_chunk(encoded_frame(), stream=99)))
        with self.assertRaises(iris.StreamGap):
            assembler.feed(encoded_chunk(b'F' + b'\0' * 7))
        for w, h, cap in ((0, 2, 256), (4096, 4096, 256), (3, 2, 3841), (3, 2, 255)):
            with self.assertRaises(ValueError):
                iris.EncodedAssembler(w, h, 7, cap, mirror.apply_frame)


class EncodedGateway(GatewayFixture):
    encoded = True

    def encoded_stream(self, ws):
        epoch, number, offset = 0, 1, 0
        dropped = 0
        while not self.finished.is_set():
            try:
                op, token, msg, end = self.control.get(timeout=.1)
            except queue.Empty:
                continue
            if op == 0:
                epoch, number, offset = token, 1, 0
            elif token == epoch and msg == number:
                offset = end
            else:
                continue
            payload = encoded_frame(1 if number == 1 else 3, state=2)
            if offset == len(payload):
                number += 1
                offset = 0
                payload = encoded_frame(3, state=2)
            if self.fault and not self.fault_used and offset:
                self.fault_used = True
                if self.fault == 'gateway':
                    ws.send('{"kind":"history_gap","dropped_events":1}')
                elif self.fault == 'device':
                    dropped += 1
                elif self.fault == 'silent':
                    offset += 1
            ws.send(encoded_chunk(payload, epoch=epoch, number=number, offset=offset, count=7, dropped=dropped))

    def test_encoded_stream_ack_and_reference(self):
        client = iris.IrisClient(self.url, DEVICE)
        client.open()
        try:
            frame = None
            while frame is None:
                frame = client.receive()
            np.testing.assert_array_equal(frame, np.full((2, 3), 0x1234))
            self.assertEqual(client.assembler.state, 2)
            frame = None
            while frame is None:
                frame = client.receive()
            np.testing.assert_array_equal(frame, np.zeros((2, 3)))
            self.assertTrue(any(body.get('method_id') == 3 for _, body in self.requests))
        finally:
            client.cleanup()
        self.assertEqual(self.requests[-1][1]['channel'], 'image')

    def check_drop(self, fault):
        self.fault = fault
        client = iris.IrisClient(self.url, DEVICE)
        client.open()
        before = client.assembler.epoch
        try:
            frame = None
            for _ in range(20):
                frame = client.receive()
                if frame is not None:
                    break
            self.assertIsNotNone(frame)
            self.assertNotEqual(before, client.assembler.epoch)
            np.testing.assert_array_equal(frame, np.full((2, 3), 0x1234))
        finally:
            client.cleanup()

    def test_gateway_drop_requests_reset(self):
        self.check_drop('gateway')

    def test_device_drop_requests_reset(self):
        self.check_drop('device')

    def test_silent_gap_requests_reset(self):
        self.check_drop('silent')

    def test_timeout_requests_new_epoch(self):
        client = iris.IrisClient(self.url, DEVICE)
        client.open()
        before = client.assembler.epoch
        try:
            with patch.object(client.socket, 'recv', side_effect=TimeoutError):
                self.assertIsNone(client.receive())
            self.assertNotEqual(before, client.assembler.epoch)
            frame = None
            for _ in range(20):
                frame = client.receive()
                if frame is not None:
                    break
            np.testing.assert_array_equal(frame, np.full((2, 3), 0x1234))
        finally:
            client.cleanup()

    def test_reconnect_starts_with_fresh_reference(self):
        client = iris.IrisClient(self.url, DEVICE)
        client.open()
        old_epoch = client.assembler.epoch
        client.cleanup()
        self.finished.clear()
        client = iris.IrisClient(self.url, DEVICE)
        client.open()
        try:
            self.assertNotEqual(old_epoch, client.assembler.epoch)
            self.assertFalse(client.assembler.seen.any())
        finally:
            client.cleanup()

    def test_reused_encoded_stream_is_not_taken_over(self):
        self.reused = True
        client = iris.IrisClient(self.url, DEVICE)
        with self.assertRaisesRegex(ConnectionError, 'already has a viewer'):
            client.open()
        self.assertFalse(self.finished.is_set())
        self.assertFalse(any(body.get('method_id') == 3 for _, body in self.requests))

    def test_viewer_uses_encoded_state(self):
        link = mirror.make_link('iris:' + DEVICE, gateway=self.url).start()
        try:
            until = time.monotonic() + 3
            while not link.changes and time.monotonic() < until:
                time.sleep(.01)
            self.assertGreater(link.changes, 0)
            self.assertEqual(link.state, 2)
        finally:
            link.close()
