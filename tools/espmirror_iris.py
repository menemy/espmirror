# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
"""ESP-Iris Gateway HTTP/RPC and RGB565 screen stream adapter."""
import base64
import json
import re
import struct
import threading
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import numpy as np

DEFAULT_GATEWAY = "http://127.0.0.1:8443"
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_MESSAGE_BYTES = MAX_FRAME_BYTES + 65536


def gateway_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Iris Gateway must be an http(s) URL without credentials")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Iris Gateway URL must name the server root")
    return url.rstrip("/")


def request_json(base, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = Request(base + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=3) as response:
            raw = response.read(1024 * 1024 + 1)
    except HTTPError as error:
        raise ConnectionError("Iris Gateway HTTP %d for %s" % (error.code, path)) from error
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
    def __init__(self, url, device_id, fps=25):
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
        self.closed = False
        self.cancelled = threading.Event()
        self.close_lock = threading.RLock()
        self.pointer_down = False
        self.pointer_xy = (0, 0)
        self.keys_down = set()
        self.sequence = 0
        self.assembler = None

    def rpc(self, service, method, payload):
        result = request_json(self.url, self.path + "/rpc/raw", {
            "service_id": service, "method_id": method, "payload_hex": payload.hex(), "deadline_ms": 1000})
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
        ws_url = ("wss" if self.url.startswith("https:") else "ws") + self.url[self.url.index(":"):] + self.path + "/streams/screen"
        self.connection_context = connect(ws_url, open_timeout=3, close_timeout=1, compression=None,
                                          max_size=MAX_MESSAGE_BYTES, max_queue=8, proxy=None)
        self.socket = self.connection_context.__enter__()
        try:
            if self.cancelled.is_set():
                raise ConnectionError("Iris connection closed")
            result = request_json(self.url, self.path + "/mirror/start", {"channel": "screen", "fps": self.fps})["mirror"]
            self.owns_stream = not result.get("reused", False)
            if self.cancelled.is_set():
                raise ConnectionError("Iris connection closed")
            self.assembler = FrameAssembler(result["description"], result["stream_id"])
            if (board.get("w"), board.get("h")) != (self.assembler.width, self.assembler.height):
                raise ValueError("Iris screen geometry differs from board metadata")
        except Exception:
            self.close()
            raise
        return self.assembler

    def receive(self):
        return self.assembler.feed(self.socket.recv(timeout=6))

    def cancel(self):
        self.cancelled.set()
        if self.socket:
            self.socket.close()

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
            if self.rpc(0x1001, 1, wire) != wire:
                raise ConnectionError("Iris pointer response differs from request")
            self.pointer_down = bool(down)
            self.pointer_xy = (x, y)
        elif len(payload) == 3 and payload[0] == 6:
            if self.rpc(0x4553, 1, payload[1:]) != payload[1:]:
                raise ConnectionError("Iris key response differs from request")
            if payload[2]:
                self.keys_down.add(payload[1])
            else:
                self.keys_down.discard(payload[1])
        elif len(payload) == 2 and payload[0] == 3:
            self.send(bytes((6, payload[1], 1)))
            self.send(bytes((6, payload[1], 0)))

    def close(self):
        with self.close_lock:
            if self.closed:
                return
            self.cancelled.set()
            error = None
            try:
                if not self.owns_stream:
                    for key in list(self.keys_down):
                        try:
                            self.rpc(0x4553, 1, bytes((key, 0)))
                            self.keys_down.discard(key)
                        except Exception as failure:
                            error = failure
                    if self.pointer_down:
                        self.sequence = (self.sequence + 1) & 0xffffffff
                        try:
                            self.rpc(0x1001, 1, struct.pack("<BBhhHI", 2, 0, *self.pointer_xy, 0, self.sequence))
                            self.pointer_down = False
                        except Exception as failure:
                            error = failure
                if self.owns_stream:
                    request_json(self.url, self.path + "/mirror/stop", {"channel": "screen"})
                    self.owns_stream = False
                if error:
                    raise error
                self.closed = True
            finally:
                if self.connection_context:
                    context, self.connection_context = self.connection_context, None
                    self.socket = None
                    context.__exit__(None, None, None)
