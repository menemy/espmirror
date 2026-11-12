# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
"""Qt audio lives on the GUI thread. Network threads only fill bounded queues."""
import queue

import numpy as np


class Resampler:
    def __init__(self, source_rate, target_rate):
        self.step = source_rate / target_rate
        self.tail = np.empty(0)
        self.position = 0.0

    def feed(self, samples):
        samples = np.asarray(samples, dtype=np.float64)
        if self.step == 1 or not samples.size:
            return samples
        data = np.concatenate((self.tail, samples))
        positions = np.arange(self.position, len(data) - 1, self.step)
        result = np.interp(positions, np.arange(len(data)), data)
        self.position += len(positions) * self.step - (len(data) - 1)
        self.tail = data[-1:]
        return result


class PCM:
    def __init__(self, rate, channels, kind, capture):
        self.channels, self.kind, self.capture = channels, kind, capture
        self.dtype = {'Int16': '<i2', 'Int32': '<i4', 'Float': '<f4', 'UInt8': 'u1'}[kind]
        self.frame_size = np.dtype(self.dtype).itemsize * channels
        self.raw = bytearray()
        self.resampler = Resampler(rate if capture else 16000, 16000 if capture else rate)

    def convert(self, data):
        if self.capture:
            self.raw.extend(data)
            size = len(self.raw) // self.frame_size * self.frame_size
            samples = np.frombuffer(bytes(self.raw[:size]), self.dtype).astype(np.float64)
            del self.raw[:size]
            if not size:
                return b''
            if self.kind == 'UInt8':
                samples = (samples - 128) / 128
            elif self.kind != 'Float':
                samples /= 32768 if self.kind == 'Int16' else 2147483648
            mono = self.resampler.feed(samples.reshape(-1, self.channels).mean(axis=1))
            return np.clip(np.rint(mono * 32768), -32768, 32767).astype('<i2').tobytes()
        samples = np.frombuffer(data, '<i2').astype(np.float64) / 32768
        samples = self.resampler.feed(samples)
        samples = np.repeat(samples[:, None], self.channels, axis=1)
        if self.kind == 'UInt8':
            samples = np.clip(np.rint(samples * 128 + 128), 0, 255)
        elif self.kind == 'Int16':
            samples = np.clip(np.rint(samples * 32768), -32768, 32767)
        elif self.kind == 'Int32':
            samples = np.clip(np.rint(samples * 2147483648), -2147483648, 2147483647)
        return samples.astype(self.dtype).tobytes()


class AudioSession:
    def __init__(self, parent, report):
        from PySide6.QtCore import QTimer
        from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QAudioSource, QMediaDevices
        self.Format, self.Sink, self.Source, self.Devices = QAudioFormat, QAudioSink, QAudioSource, QMediaDevices
        self.parent, self.report = parent, report
        self.listen_link = self.talk_link = None
        self.permission_pending = False
        self.closed = False
        self.listen_source = 0
        self.listen_solo = False        # the board's speaker silent while the viewer hears it
        self.talk_lend = 0              # 0 talk through the speaker; the board's microphone with ours 1, ours alone 2
        self.sink = self.source = self.output = self.input = None
        self.pending = bytearray()
        self.capture_pending = bytearray()
        self.timer = QTimer(parent, interval=10, timeout=self.pump)
        self.timer.start()

    def format(self, device):
        if device.isNull():
            raise ValueError('No audio device is available')
        fmt = self.Format()
        fmt.setSampleRate(16000)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(self.Format.SampleFormat.Int16)
        if not device.isFormatSupported(fmt):
            fmt = device.preferredFormat()
        if fmt.sampleFormat().name not in ('Int16', 'Int32', 'UInt8', 'Float'):
            raise ValueError('Unsupported audio device format')
        return fmt

    def stop_listen(self):
        if self.listen_link:
            if self.listen_solo:
                self.listen_link.send(bytes([9, 0]))
            self.listen_link.send(bytes([7, 0]))
        if self.sink:
            self.sink.stop()
            self.sink.deleteLater()
        self.listen_link = self.sink = self.output = None
        self.listen_source = 0
        self.listen_solo = False
        self.pending.clear()

    def listen(self, link, source):
        same = self.listen_link is link and self.listen_source == source
        self.stop_listen()
        if same:
            return
        try:
            device = self.Devices.defaultAudioOutput()
            fmt = self.format(device)
            self.play_pcm = PCM(fmt.sampleRate(), fmt.channelCount(), fmt.sampleFormat().name, False)
            self.sink = self.Sink(device, fmt, self.parent)
            self.limit = fmt.sampleRate() * self.play_pcm.frame_size // 4
            self.sink.setBufferSize(max(1024, self.limit // 2))
            self.output = self.sink.start()
            if self.output is None:
                raise ValueError('Cannot start audio output')
            while not link.audio.empty():
                try:
                    link.audio.get_nowait()
                except queue.Empty:
                    break
            self.listen_link, self.listen_source = link, source
            link.send(bytes([7, source]))
        except (ValueError, RuntimeError) as error:
            self.stop_listen()
            self.report(str(error))

    def stop_talk(self):
        if self.source:
            self.source.stop()
            self.source.deleteLater()
        if self.talk_link:
            self.talk_link.send(bytes([10, 0]) if self.talk_lend else bytes([8, 0]))
        self.talk_link = self.source = self.input = None
        self.talk_lend = 0
        self.capture_pending.clear()

    def microphone_permission(self, start):
        from PySide6.QtCore import QCoreApplication, QMicrophonePermission, Qt
        from espmirror_macos import declares_microphone
        if not declares_microphone():
            start()                 # Qt would refuse without asking; the system asks for the terminal instead
            return
        app = QCoreApplication.instance()
        permission = QMicrophonePermission()
        status = app.checkPermission(permission)
        if status == Qt.PermissionStatus.Granted:
            start()
        elif status == Qt.PermissionStatus.Denied:
            self.report('Microphone permission denied; allow it in OS privacy settings')
        elif not self.permission_pending:
            self.permission_pending = True

            def granted(result):
                self.permission_pending = False
                if self.closed:
                    return
                if result.status() == Qt.PermissionStatus.Granted:
                    start()
                else:
                    self.report('Microphone permission denied')

            app.requestPermission(permission, self.parent, granted)

    def talk(self, link):
        same = self.talk_link is link and not self.talk_lend
        self.stop_talk()
        if not same:
            self.microphone_permission(lambda: self.start_talk(link))

    def speaker_mode(self, link):
        """0 off, 1 on the board and here, 2 here alone."""
        if self.listen_link is not link or self.listen_source != 2:
            return 0
        return 2 if self.listen_solo else 1

    def mic_mode(self, link):
        """0 off, 1 our microphone with the board's, 2 ours alone; talk through the speaker counts as off."""
        return self.talk_lend if self.talk_link is link else 0

    def cycle_speaker(self, link):
        mode = (self.speaker_mode(link) + 1) % (3 if getattr(link, 'audio_route', False) else 2)
        if mode == 0:
            self.stop_listen()
        elif mode == 1:
            self.listen(link, 2)
        else:
            link.send(bytes([9, 1]))
            self.listen_solo = True
        return mode

    def cycle_mic(self, link):
        mode = (self.mic_mode(link) + 1) % 3
        if mode == 0:
            self.stop_talk()
        elif mode == 1:
            self.stop_talk()
            self.microphone_permission(lambda: self.start_talk(link, 1))
        else:
            link.send(bytes([10, 2]))
            self.talk_lend = 2
        return mode

    def start_talk(self, link, lend=0):
        if self.closed or not link.live:
            return
        try:
            device = self.Devices.defaultAudioInput()
            fmt = self.format(device)
            self.capture_pcm = PCM(fmt.sampleRate(), fmt.channelCount(), fmt.sampleFormat().name, True)
            self.source = self.Source(device, fmt, self.parent)
            self.source.setBufferSize(max(1024, fmt.sampleRate() * self.capture_pcm.frame_size // 10))
            self.input = self.source.start()
            if self.input is None:
                raise ValueError('Cannot start microphone; check OS microphone permission')
            self.talk_link, self.talk_lend = link, lend
            link.send(bytes([10, lend]) if lend else bytes([8, 1]))
            self.input.readyRead.connect(self.capture)
        except (ValueError, RuntimeError) as error:
            self.stop_talk()
            self.report(str(error))

    def capture(self):
        if not self.input or not self.talk_link:
            return
        self.capture_pending += self.capture_pcm.convert(bytes(self.input.readAll()))
        while len(self.capture_pending) >= 640:
            pcm = bytes(self.capture_pending[:640])
            del self.capture_pending[:640]
            # Do not queue seconds of stale microphone data behind a blocked cable.
            if self.talk_link.out.qsize() < 16:
                self.talk_link.send((b'S\x02' if self.talk_lend else b'S\x01') + pcm)

    def pump(self):
        if self.talk_link and not self.talk_link.live:
            self.stop_talk()
        if self.listen_link and not self.listen_link.live:
            self.stop_listen()
        if not self.listen_link:
            return
        while True:
            try:
                source, pcm = self.listen_link.audio.get_nowait()
            except queue.Empty:
                break
            if source == self.listen_source:
                self.pending += self.play_pcm.convert(pcm)
        if len(self.pending) > self.limit:
            excess = len(self.pending) - self.limit
            excess += (-excess) % self.play_pcm.frame_size
            del self.pending[:excess]
        if self.pending and self.output:
            size = min(len(self.pending), self.sink.bytesFree())
            size -= size % self.play_pcm.frame_size
            if size:
                written = self.output.write(bytes(self.pending[:size]))
                if written < 0:
                    self.stop_listen()
                    self.report('Audio output stopped')
                else:
                    del self.pending[:written]

    def close(self):
        self.closed = True
        self.timer.stop()
        self.stop_talk()
        self.stop_listen()
