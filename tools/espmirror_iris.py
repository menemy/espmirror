# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
"""ESP-Iris Gateway HTTP/RPC, encoded IMAGE stream and legacy RGB565 adapter."""
import base64
from contextlib import contextmanager
import http.client
import json
import re
import socket
import secrets
import struct
import threading
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPSHandler, Request, build_opener, urlopen

import numpy as np

DEFAULT_GATEWAY = "http://127.0.0.1:8443"
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_MESSAGE_BYTES = MAX_FRAME_BYTES + 65536
CLEANUP_SECONDS = 1.0


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("Iris cleanup deadline expired")
    return seconds


@contextmanager
def open_request(request, deadline, on_socket=None):
    if deadline is None:
        with urlopen(request, timeout=3) as response:
            yield response
        return
    timers, releases = [], []

    def interrupt(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    class DeadlineConnection:
        def connect(self):
            self.timeout = min(3, remaining(deadline))
            super().connect()
            if on_socket is not None:
                releases.append(on_socket(self.sock))
            # Socket timeouts alone permit a peer to trickle headers/body forever.
            timer = threading.Timer(max(0, deadline - time.monotonic()), interrupt, (self.sock,))
            timer.daemon = True
            timers.append(timer)
            timer.start()

    class HTTPConnection(DeadlineConnection, http.client.HTTPConnection):
        pass

    class HTTPSConnection(DeadlineConnection, http.client.HTTPSConnection):
        pass

    class HTTP(HTTPHandler):
        def http_open(self, request):
            return self.do_open(HTTPConnection, request)

    class HTTPS(HTTPSHandler):
        def https_open(self, request):
            return self.do_open(HTTPSConnection, request, context=self._context)

    try:
        with build_opener(HTTP(), HTTPS()).open(request, timeout=min(3, remaining(deadline))) as response:
            yield response
    finally:
        for timer in timers:
            timer.cancel()
        for release in releases:
            release()


ENCODED_FORMAT = 0x4553
CHUNK_HEADER = struct.Struct("<4sIIII")


class StreamGap(ValueError):
    """The delta reference must be replaced before any more pixels are applied."""


class EncodedAssembler:
    def __init__(self, width, height, stream_id, chunk_size, decode):
        self.width, self.height = int(width), int(height)
        if not (0 < self.width <= 4096 and 0 < self.height <= 4096
                and self.width * self.height * 2 <= MAX_FRAME_BYTES and 256 <= chunk_size <= 3840):
            raise ValueError("invalid Iris encoded stream dimensions or chunk limit")
        self.stream_id, self.chunk_size, self.decode = int(stream_id), chunk_size, decode
        self.pixels = np.zeros((self.height, self.width), dtype="<u2")
        self.seen = np.zeros(self.pixels.shape, bool)
        self.bytes_received = 0
        self.reset(0)

    def reset(self, epoch):
        self.epoch, self.message, self.total = epoch, 1, None
        self.buffer = bytearray()
        self.dropped, self.ack, self.state = None, None, 0
        self.pixels.fill(0)
        self.seen.fill(False)

    def feed(self, message):
        self.ack = None
        if isinstance(message, str):
            if json.loads(message).get("kind") == "history_gap":
                raise StreamGap("Gateway subscriber dropped chunks")
            return None
        if len(message) < 4 or len(message) > MAX_MESSAGE_BYTES:
            raise StreamGap("invalid Iris chunk size")
        length, = struct.unpack_from("<I", message)
        if not 0 < length <= 65536 or 4 + length > len(message):
            raise StreamGap("invalid Iris metadata size")
        metadata = json.loads(message[4:4 + length])
        if int(metadata.get("stream_id", -1)) != self.stream_id:
            return None
        data = message[4 + length:]
        if not CHUNK_HEADER.size < len(data) <= self.chunk_size:
            raise StreamGap("invalid encoded chunk length")
        magic, epoch, number, offset, total = CHUNK_HEADER.unpack_from(data)
        if magic != b"ESM1":
            raise StreamGap("unknown encoded chunk version")
        if epoch != self.epoch:
            return None
        desc = metadata.get("description", {})
        if (int(metadata.get("channel", -1)) != 4 or desc.get("format") != ENCODED_FORMAT
                or desc.get("quality") != 1 or desc.get("width") != self.width
                or desc.get("height") != self.height or desc.get("x", 0) or desc.get("y", 0)
                or desc.get("stride", 0)):
            raise StreamGap("encoded stream description changed")
        dropped = int(metadata.get("dropped", -1))
        if not 0 <= dropped <= 0xffffffff or (self.dropped is not None and dropped != self.dropped):
            raise StreamGap("device dropped chunks")
        self.dropped = dropped
        payload = data[CHUNK_HEADER.size:]
        end = offset + len(payload)
        flags = (1 if offset == 0 else 0) | (2 if end == total else 0)
        if (not 8 <= total <= 65536 or number != self.message or metadata.get("frame_id") != number
                or offset != len(self.buffer) or end > total or metadata.get("flags") != flags
                or (self.total is not None and total != self.total)):
            raise StreamGap("encoded message has missing or inconsistent chunks")
        self.total = total
        self.buffer.extend(payload)
        self.bytes_received += len(payload)
        frame = None
        if end == total:
            try:
                self.state = self.decode(self.pixels, bytes(self.buffer), self.seen)
            except (ValueError, IndexError, struct.error) as error:
                raise StreamGap("invalid espmirror frame") from error
            if self.seen.all():
                frame = self.pixels.copy()
            self.message += 1
            self.total = None
            self.buffer.clear()
        self.ack = (epoch, number, end)
        return frame


def gateway_url(url):
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError("Iris Gateway URL cannot contain whitespace or control characters")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Iris Gateway must be an http(s) URL without credentials")
    if parsed.port == 0 or parsed.netloc.endswith(":"):
        raise ValueError("Iris Gateway port must be between 1 and 65535")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Iris Gateway URL must name the server root")
    return url.rstrip("/")


class GatewayHTTPError(ConnectionError):
    def __init__(self, status, path):
        self.status = status
        super().__init__("Iris Gateway HTTP %d for %s" % (status, path))


def request_json(base, path, body=None, *, operation_id=None, deadline=None, on_socket=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if operation_id is not None:
        headers["X-Operation-ID"] = operation_id
    request = Request(base + path, data=data, headers=headers)
    try:
        with open_request(request, deadline, on_socket) as response:
            raw = response.read(1024 * 1024 + 1)
    except HTTPError as error:
        raise GatewayHTTPError(error.code, path) from error
    if len(raw) > 1024 * 1024:
        raise ValueError("Iris Gateway JSON exceeds 1 MiB")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Iris Gateway response must be an object")
    operation = result.get("operation", {})
    if operation.get("state") in ("failed", "cancelled") or operation.get("status") in ("failed", "cancelled"):
        raise ConnectionError("Iris Gateway operation failed")
    return result


def discover(url):
    result = request_json(gateway_url(url), "/v2/devices")
    return [item for item in result.get("devices", [])
            if isinstance(item, dict) and re.fullmatch(r"[0-9a-fA-F]{32}", str(item.get("device_id", "")))
            and item.get("connected") and item.get("data_available", True)]


class FrameAssembler:
    """Only publish complete, consecutive tiles from the requested stream."""

    def __init__(self, description, stream_id):
        self.width = int(description["width"])
        self.height = int(description["height"])
        self.stride = int(description["stride"])
        if (int(description.get("format", 0)) != 1 or description.get("x", 0) or description.get("y", 0)
                or not 0 < self.width <= 4096 or not 0 < self.height <= 4096
                or self.stride < self.width * 2 or self.stride % 2
                or self.stride * self.height > MAX_FRAME_BYTES):
            raise ValueError("unsupported Iris RGB565 screen description")
        self.stream_id = int(stream_id)
        self.pixels = np.empty((self.height, self.width), dtype="<u2")
        self.frame_id = None
        self.next_y = 0
        self.bytes_received = 0

    def reset(self):
        self.frame_id = None
        self.next_y = 0

    def feed(self, message):
        if isinstance(message, str):
            if json.loads(message).get("kind") == "history_gap":
                self.reset()
            return None
        if len(message) < 4 or len(message) > MAX_MESSAGE_BYTES:
            raise ValueError("invalid Iris stream message size")
        length, = struct.unpack_from("<I", message)
        if not 0 < length <= 65536 or 4 + length > len(message):
            raise ValueError("invalid Iris stream metadata size")
        metadata = json.loads(message[4:4 + length])
        if int(metadata.get("stream_id", -1)) != self.stream_id:
            return None
        tile = metadata.get("description", {})
        x, y = int(tile.get("x", -1)), int(tile.get("y", -1))
        width, height = int(tile.get("width", 0)), int(tile.get("height", 0))
        data = memoryview(message)[4 + length:]
        if (int(tile.get("format", 0)) != 1 or x != 0 or width != self.width
                or int(tile.get("stride", 0)) != self.stride or height <= 0 or y < 0
                or y + height > self.height or len(data) != self.stride * height):
            raise ValueError("Iris tile does not match the screen")
        self.bytes_received += len(data)
        frame_id = int(metadata["frame_id"])
        if y == 0:
            self.frame_id, self.next_y = frame_id, 0
        if self.frame_id != frame_id or self.next_y != y:
            self.reset()
            return None
        rows = np.frombuffer(data, dtype="<u2").reshape(height, self.stride // 2)
        self.pixels[y:y + height] = rows[:, :self.width]
        self.next_y += height
        if self.next_y == self.height:
            frame = self.pixels.copy()
            self.reset()
            return frame
        return None


class IrisClient:
    def __init__(self, url, device_id, fps=25, decode=None):
        if not re.fullmatch(r"[0-9a-fA-F]{32}", device_id):
            raise ValueError("Iris target needs a 32-digit hexadecimal device ID")
        if not 1 <= fps <= 60:
            raise ValueError("Iris fps must be between 1 and 60")
        self.url = gateway_url(url)
        self.path = "/v2/devices/" + device_id.lower()
        self.fps = fps
        self.socket = None
        self.connection_context = None
        self.owns_stream = False
        self.start_operation = None
        self.closed = False
        self.cancelled = threading.Event()
        self.close_lock = threading.RLock()
        self.io_lock = threading.Lock()
        self.http_sockets = set()
        self.pointer_down = False
        self.pointer_xy = (0, 0)
        self.keys_down = set()
        self.sequence = 0
        self.assembler = None
        self.decode = decode
        self.channel = "screen"
        self.encoded = False
        self.resyncs = 0

    def rpc(self, service, method, payload, *, deadline=None):
        options = {"deadline": time.monotonic() + 3 if deadline is None else deadline}
        remaining(options["deadline"])
        if deadline is None:
            options["on_socket"] = self.track_socket
        result = request_json(self.url, self.path + "/rpc/raw", {
            "service_id": service, "method_id": method, "payload_hex": payload.hex(), "deadline_ms": 1000}, **options)
        encoded = result.get("payload_base64")
        if not isinstance(encoded, str):
            raise ConnectionError("Iris RPC did not return an accepted response")
        return base64.b64decode(encoded, validate=True)

    def open(self):
        try:
            from websockets.sync.client import connect
        except ImportError as error:
            raise RuntimeError("Iris viewing needs websockets; install tools/requirements.txt") from error
        board = json.loads(self.rpc(0x4553, 2, b""))
        if self.cancelled.is_set():
            raise ConnectionError("Iris connection closed")
        if not isinstance(board, dict):
            raise ValueError("invalid Iris board metadata")
        self.board = {**board, "audio": False, "keyboard": False}
        if board.get("iris_codec", 0) not in (0, 1):
            raise ValueError("unsupported espmirror Iris codec version")
        self.encoded = board.get("iris_codec") == 1
        self.channel = "image" if self.encoded else "screen"
        ws_url = ("wss" if self.url.startswith("https:") else "ws") + self.url[self.url.index(":"):] + self.path + "/streams/" + self.channel
        self.connection_context = connect(ws_url, open_timeout=3, close_timeout=1, compression=None,
                                          max_size=MAX_MESSAGE_BYTES, max_queue=8, proxy=None)
        self.socket = self.connection_context.__enter__()
        try:
            if self.cancelled.is_set():
                raise ConnectionError("Iris connection closed")
            body = {"channel": self.channel, "fps": self.fps}
            if self.encoded:
                body["description"] = dict(width=board["w"], height=board["h"], format=ENCODED_FORMAT, quality=1)
            self.start_operation = str(uuid.uuid4())
            try:
                result = request_json(self.url, self.path + "/mirror/start", body,
                                      operation_id=self.start_operation, deadline=time.monotonic() + 3,
                                      on_socket=self.track_socket)["mirror"]
            except GatewayHTTPError as error:
                if 400 <= error.status < 500 and error.status != 408:
                    self.start_operation = None
                raise
            self.owns_stream = not result.get("reused", False)
            self.start_operation = None
            if self.cancelled.is_set():
                raise ConnectionError("Iris connection closed")
            if self.encoded:
                if not self.owns_stream:
                    raise ConnectionError("encoded Iris stream already has a viewer")
                if self.decode is None:
                    from espmirror import apply_frame
                    self.decode = apply_frame
                self.assembler = EncodedAssembler(board["w"], board["h"], result["stream_id"],
                                                   int(board["iris_chunk"]), self.decode)
                self.resync()
            else:
                self.assembler = FrameAssembler(result["description"], result["stream_id"])
            if (board.get("w"), board.get("h")) != (self.assembler.width, self.assembler.height):
                raise ValueError("Iris screen geometry differs from board metadata")
        except Exception:
            self.close()
            raise
        return self.assembler

    def receive(self):
        if not self.encoded:
            return self.assembler.feed(self.socket.recv(timeout=6))
        try:
            frame = self.assembler.feed(self.socket.recv(timeout=2))
        except (TimeoutError, ValueError, KeyError, TypeError, struct.error):
            self.resyncs += 1
            if self.resyncs > 3:
                raise ConnectionError("Iris stream could not establish a reference")
            self.resync()
            return None
        if self.assembler.ack is not None:
            self.stream_command(1, *self.assembler.ack)
        if frame is not None:
            self.resyncs = 0
        return frame

    def stream_command(self, op, epoch, message=0, offset=0):
        with self.close_lock:
            if self.cancelled.is_set() or self.closed:
                raise ConnectionError("Iris connection closed")
            wire = struct.pack("<BIII", op, epoch, message, offset)
            if self.rpc(0x4553, 3, wire) != wire:
                raise ConnectionError("Iris stream control response differs from request")

    def resync(self):
        epoch = secrets.randbits(32) or 1
        if epoch == self.assembler.epoch:
            epoch = epoch % 0xffffffff + 1
        self.assembler.reset(epoch)
        self.stream_command(0, epoch)

    def cancel(self):
        self.cancelled.set()
        with self.io_lock:
            sockets = tuple(self.http_sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        transport = self.socket
        if transport:
            try:
                transport.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def track_socket(self, sock):
        with self.io_lock:
            if self.cancelled.is_set():
                sock.close()
                raise ConnectionError("Iris connection closed")
            self.http_sockets.add(sock)

        def release():
            with self.io_lock:
                self.http_sockets.discard(sock)

        return release

    def send(self, payload):
        with self.close_lock:
            self._send(payload)

    def _send(self, payload):
        if self.closed or self.cancelled.is_set():
            raise ConnectionError("Iris connection closed")
        if len(payload) == 5 and payload[0] in (0, 1):
            down, x, y = struct.unpack("<BHH", payload)
            phase = 2 if not down else 1 if self.pointer_down else 0
            self.sequence = (self.sequence + 1) & 0xffffffff
            wire = struct.pack("<BBhhHI", phase, 0, x, y, 0, self.sequence)
            if down:
                self.pointer_down = True  # The device may accept a press even if its response is lost.
            self.pointer_xy = (x, y)
            if self.rpc(0x1001, 1, wire) != wire:
                raise ConnectionError("Iris pointer response differs from request")
            self.pointer_down = bool(down)
            self.pointer_xy = (x, y)
        elif len(payload) == 3 and payload[0] == 6:
            if payload[2]:
                self.keys_down.add(payload[1])
            if self.rpc(0x4553, 1, payload[1:]) != payload[1:]:
                raise ConnectionError("Iris key response differs from request")
            if not payload[2]:
                self.keys_down.discard(payload[1])
        elif len(payload) == 2 and payload[0] == 3:
            self.send(bytes((6, payload[1], 1)))
            self.send(bytes((6, payload[1], 0)))

    def close(self):
        """Interrupt I/O only; the owning worker must call cleanup()."""
        self.cancel()

    def cleanup(self, *, deadline=None):
        """Release remote state within one worker-owned budget, retaining failed work."""
        if self.closed:
            return
        deadline = time.monotonic() + CLEANUP_SECONDS if deadline is None else deadline
        self.cancel()
        acquired = False
        try:
            acquired = self.close_lock.acquire(timeout=remaining(deadline))
            if not acquired:
                raise TimeoutError("Iris cleanup deadline expired waiting for input RPC")
            error = None
            if self.start_operation is not None:
                # A timed-out response may have created or reused a stream.
                operation = request_json(self.url, "/v2/operations/" + self.start_operation, deadline=deadline)
                result = operation.get("result")
                if (operation.get("status") != "succeeded" or not isinstance(result, dict)
                        or not isinstance(result.get("reused"), bool)):
                    raise ConnectionError("Iris mirror/start outcome is unresolved; cleanup will retry")
                self.owns_stream = not result["reused"]
                self.start_operation = None
            if not self.owns_stream:
                for key in list(self.keys_down):
                    try:
                        self.rpc(0x4553, 1, bytes((key, 0)), deadline=deadline)
                        self.keys_down.discard(key)
                    except Exception as failure:
                        error = failure
                if self.pointer_down:
                    self.sequence = (self.sequence + 1) & 0xffffffff
                    try:
                        self.rpc(0x1001, 1, struct.pack("<BBhhHI", 2, 0, *self.pointer_xy, 0, self.sequence), deadline=deadline)
                        self.pointer_down = False
                    except Exception as failure:
                        error = failure
            if self.owns_stream:
                try:
                    request_json(self.url, self.path + "/mirror/stop", {"channel": self.channel}, deadline=deadline)
                except GatewayHTTPError as failure:
                    if failure.status != 404:
                        raise
                self.owns_stream = False
            if error:
                raise error
            self.closed = True
        finally:
            if acquired:
                self.close_lock.release()
            if self.connection_context:
                context, self.connection_context = self.connection_context, None
                transport, self.socket = self.socket, None
                if transport:
                    transport.close_timeout = 0  # cancel() already interrupted the socket; do not handshake.
                context.__exit__(None, None, None)
