# SPDX-License-Identifier: GPL-3.0-or-later
"""The timer's ring: a short bell, made once as a WAV in the cache and played with GStreamer.

A kitchen timer has to be heard across the room, so this plays sound itself rather than relying on a
notification sound the person may have turned off. Without GStreamer or an audio device it stays silent and
the app still shows and notifies.
"""
from __future__ import annotations

import math
import os
import struct
import wave

RATE = 22050


def bell(path: str) -> str:
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames = bytearray()
    total = int(RATE * 1.6)
    samples = [0.0] * total
    for start, freq in ((0.0, 1046.5), (0.28, 1318.5), (0.56, 1568.0)):
        s0 = int(start * RATE)
        for i in range(total - s0):
            t = i / RATE
            env = math.exp(-t * 4.2) * min(1.0, t * 400)
            v = env * (math.sin(2 * math.pi * freq * t) + 0.35 * math.sin(2 * math.pi * freq * 2.01 * t))
            samples[s0 + i] += v * 0.28
    for v in samples:
        frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32000))
    tmp = path + ".part"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    os.replace(tmp, path)
    return path


class Chime:
    def __init__(self, path: str):
        self.pipe = None
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            self.Gst = Gst
            self.uri = "file://" + bell(path)
            self.pipe = Gst.ElementFactory.make("playbin", "simmer-chime")
            sink = os.environ.get("SIMMER_AUDIO_SINK")
            if self.pipe is not None and sink:
                self.pipe.set_property("audio-sink", Gst.ElementFactory.make(sink, None))
            if self.pipe is not None:
                self.pipe.set_property("uri", self.uri)
        except Exception:
            self.pipe = None

    def ring(self):
        if self.pipe is None:
            return False
        self.pipe.set_state(self.Gst.State.NULL)
        self.pipe.set_state(self.Gst.State.PLAYING)
        return True

    def stop(self):
        if self.pipe is not None:
            self.pipe.set_state(self.Gst.State.NULL)
