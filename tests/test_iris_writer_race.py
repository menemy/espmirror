# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, PropertyMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import espmirror as mirror
import espmirror_iris as iris


class IrisWriterRaceTests(unittest.TestCase):
    def test_writer_error_after_socket_cleared_uses_cancellation(self):
        link = mirror.IrisLink('iris:' + 'a' * 32)
        client = Mock(closed=False, socket=None)
        client.send.side_effect = ConnectionError('closed while sending')
        link.client = client
        link.send(bytes((6, 1, 1)))
        link.writer(client)
        client.cancel.assert_called_once_with()

    def test_cancel_captures_socket_once_during_concurrent_close(self):
        client = iris.IrisClient('http://localhost', 'a' * 32)
        socket = Mock()
        with patch.object(iris.IrisClient, 'socket', new_callable=PropertyMock, create=True) as current:
            current.side_effect = [socket, None]
            client.cancel()
            self.assertEqual(current.call_count, 1)
        socket.socket.shutdown.assert_called_once_with(iris.socket.SHUT_RDWR)
        self.assertTrue(client.cancelled.is_set())
        client.socket = None
        client.cancel()
