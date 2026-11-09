# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
import queue
import struct
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import espmirror as mirror


def pointer(down, x):
    return struct.pack('<BHH', down, x, 10)


class IrisDragTests(unittest.TestCase):
    def test_drag_flood_keeps_edges_and_latest_move_without_disconnect(self):
        link = mirror.IrisLink('iris:' + 'a' * 32)
        link.disconnect = Mock()
        link.send(pointer(1, 0))
        for x in range(1, 2000):
            link.send(pointer(1, x))
        self.assertEqual(link.out.qsize(), 2)
        self.assertEqual(link.out.get_nowait(), pointer(1, 0))
        self.assertEqual(link.out.get_nowait(), pointer(1, 1999))
        link.send(pointer(1, 2000))
        link.send(pointer(0, 2001))
        self.assertEqual(link.out.get_nowait(), pointer(0, 2001))
        self.assertTrue(link.out.empty())
        link.disconnect.assert_not_called()

    def test_new_gesture_and_keys_keep_order_while_moves_can_be_dropped(self):
        out = mirror.IrisInputQueue(8)
        key_down, key_up = bytes((6, 2, 1)), bytes((6, 2, 0))
        for item in (pointer(1, 1), pointer(1, 2), key_down, key_up, pointer(0, 3),
                     pointer(1, 4), pointer(1, 5)):
            out.put_nowait(item)
        self.assertEqual([out.get_nowait() for _ in range(out.qsize())],
                         [pointer(1, 1), key_down, key_up, pointer(0, 3), pointer(1, 4), pointer(1, 5)])
        with self.assertRaises(queue.Empty):
            out.get(timeout=.001)

    def test_full_edge_queue_still_accepts_and_coalesces_moves(self):
        out = mirror.IrisInputQueue(1)
        out.put_nowait(pointer(1, 0))
        for x in range(1, 1000):
            out.put_nowait(pointer(1, x))
        self.assertEqual(out.get_nowait(), pointer(1, 0))
        self.assertEqual(out.get_nowait(), pointer(1, 999))
        out.put_nowait(pointer(0, 999))
        self.assertEqual(out.get_nowait(), pointer(0, 999))

    def test_reconnect_discards_old_gesture_and_preserves_new_press(self):
        out = mirror.IrisInputQueue(2)
        out.put_nowait(pointer(1, 1))
        out.put_nowait(pointer(1, 2))
        out.clear()
        out.put_nowait(pointer(1, 3))
        out.put_nowait(pointer(0, 4))
        self.assertEqual(out.get_nowait(), pointer(1, 3))
        self.assertEqual(out.get_nowait(), pointer(0, 4))
