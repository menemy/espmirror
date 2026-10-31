# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import importlib.util
from pathlib import Path
import queue
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import numpy as np
except ImportError:
    if __name__ == '__main__':
        print('SKIP: numpy is needed for PCM tests')
        sys.exit(0)
    raise unittest.SkipTest('numpy is needed for PCM tests')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import espmirror
from espmirror_audio import PCM, Resampler, AudioSession


class AudioTests(unittest.TestCase):
    def test_stream_resampling_matches_one_block(self):
        data = np.sin(np.arange(4800) * .04)
        expected = Resampler(48000, 16000).feed(data)
        converter = Resampler(48000, 16000)
        got = np.concatenate([converter.feed(data[i:i + 47]) for i in range(0, len(data), 47)])
        np.testing.assert_allclose(got, expected, atol=1e-10)
        converter = PCM(48000, 2, 'Float', True)
        stereo = np.repeat(data[:, None], 2, axis=1).astype('<f4').tobytes()
        pcm = b''.join(converter.convert(stereo[i:i + 23]) for i in range(0, len(stereo), 23))
        self.assertEqual(len(pcm), len(expected) * 2)
        np.testing.assert_allclose(np.frombuffer(pcm, '<i2') / 32768, expected, atol=1 / 32768)

    def test_device_formats(self):
        source = np.array([-32768, -12000, 0, 12000, 32767], '<i2').tobytes()
        for kind in ('Int16', 'Int32', 'Float', 'UInt8'):
            with self.subTest(kind=kind):
                output = PCM(16000, 2, kind, False).convert(source)
                result = PCM(16000, 2, kind, True).convert(output)
                np.testing.assert_allclose(np.frombuffer(result, '<i2'), np.frombuffer(source, '<i2'), atol=256)

    def test_profiles_and_rotated_keys(self):
        self.assertEqual(len(espmirror.DEVICES), 6)
        horizontal = espmirror.device_profile('Waveshare 3.49')
        vertical = espmirror.turned(horizontal, 172, 640)
        self.assertTrue(vertical['turned'])
        for old, new in zip(horizontal['keys'], vertical['keys']):
            self.assertEqual(new[2], 'right')
            self.assertAlmostEqual(new[3], horizontal['body']['w'] - old[3])
        self.assertFalse(espmirror.device_profile('Waveshare 3.5')['hold'])
        for name, profile in espmirror.DEVICES:
            self.assertIs(espmirror.device_profile(name), profile)
            self.assertTrue((ROOT / 'tools/devices' / profile['photo']['file']).is_file())

    def test_extra_payload_validation_and_bounds(self):
        link = espmirror.Link('127.0.0.1')
        for mode in range(4):
            link.extra(bytes([ord('L'), mode]))
            self.assertEqual(link.led, mode)
        for i in range(30):
            link.extra(b'A\x01' + bytes([i, 0]) * 640)
        self.assertEqual(link.audio.qsize(), 8)
        self.assertEqual(link.audio.get_nowait()[1][0], 22)
        for bad in (b'L', b'L\x04', b'A\x00\0\0', b'A\x01\0', b'A\x01' + bytes(1282)):
            with self.assertRaises(ValueError):
                link.extra(bad)
        link.tap(2)
        link.key(3, True)
        self.assertEqual(link.out.get_nowait(), b'\x03\x02')
        self.assertEqual(link.out.get_nowait(), b'\x06\x03\x01')

    def test_microphone_permission(self):
        statuses = SimpleNamespace(Granted=1, Denied=2, Undetermined=3)
        app = Mock()
        core = SimpleNamespace(QCoreApplication=SimpleNamespace(instance=lambda: app),
                               QMicrophonePermission=Mock(), Qt=SimpleNamespace(PermissionStatus=statuses))
        session = AudioSession.__new__(AudioSession)
        session.parent = object()
        session.permission_pending = session.closed = False
        session.report = Mock()
        start = Mock()
        with patch.dict(sys.modules, {'PySide6.QtCore': core}):
            app.checkPermission.return_value = statuses.Denied
            session.microphone_permission(start)
            start.assert_not_called()
            session.report.assert_called_once()
            app.checkPermission.return_value = statuses.Undetermined
            session.microphone_permission(start)
            session.microphone_permission(start)
            app.requestPermission.assert_called_once()
            result = SimpleNamespace(status=lambda: statuses.Granted)
            app.requestPermission.call_args.args[2](result)
            start.assert_called_once()
            self.assertFalse(session.permission_pending)
            session.closed = True
            app.requestPermission.call_args.args[2](result)
            start.assert_called_once()

    def test_qt_audio_lifecycle_with_fake_devices(self):
        class Format:
            SampleFormat = SimpleNamespace(Int16=SimpleNamespace(name='Int16'))
            def setSampleRate(self, value): self.rate = value
            def setChannelCount(self, value): self.channels = value
            def setSampleFormat(self, value): self.kind = value
            def sampleRate(self): return self.rate
            def channelCount(self): return self.channels
            def sampleFormat(self): return self.kind
        class IO:
            def __init__(self):
                self.written = bytearray()
                self.readyRead = Mock()
            def write(self, data):
                self.written += data
                return len(data)
            def readAll(self): return bytes(1280)
        class Device:
            def __init__(self, *args): self.io = IO()
            def setBufferSize(self, size): pass
            def start(self): return self.io
            def stop(self): pass
            def deleteLater(self): pass
            def bytesFree(self): return 4096
        hardware = SimpleNamespace(isNull=lambda: False, isFormatSupported=lambda fmt: True)
        media = SimpleNamespace(QAudioFormat=Format, QAudioSink=Device, QAudioSource=Device,
            QMediaDevices=SimpleNamespace(defaultAudioOutput=lambda: hardware, defaultAudioInput=lambda: hardware))
        with patch.dict(sys.modules, {'PySide6.QtCore': SimpleNamespace(QTimer=Mock()), 'PySide6.QtMultimedia': media}):
            notices = []
            audio = AudioSession(None, notices.append)
            link = espmirror.Link('127.0.0.1')
            link.live = True
            audio.listen(link, 2)
            self.assertEqual(link.out.get_nowait(), b'\x07\x02')
            link.extra(b'A\x02' + bytes(1280))
            audio.pump()
            self.assertEqual(len(audio.output.written), 1280)
            with patch.object(audio, 'microphone_permission', side_effect=lambda start: start()):
                audio.talk(link)
            self.assertEqual(link.out.get_nowait(), b'\x08\x01')
            audio.capture()
            self.assertEqual(link.out.get_nowait(), b'S\x01' + bytes(640))
            self.assertEqual(link.out.get_nowait(), b'S\x01' + bytes(640))
            link.live = False
            audio.pump()
            self.assertIsNone(audio.source)
            self.assertIsNone(audio.sink)
            self.assertEqual([link.out.get_nowait(), link.out.get_nowait()], [b'\x08\x00', b'\x07\x00'])
            self.assertFalse(notices)
            audio.close()


if __name__ == '__main__':
    unittest.main()
