"""SBUS - Futaba / FrSky RC serial, one wire.

The wire is inverted async serial: 100000 baud, 8E2, idle low (a true SBUS
line sits low between frames and a receiver's output is the inverted UART).
The bytes come off the shared serial core with the inverted flag set, and
SBUS then frames them as a fixed 25-byte packet:

    [0x0F][22 data bytes][flags][end]

The 22 data bytes pack sixteen 11-bit channels, least-significant bit first
across the byte boundaries. The flags byte carries the two digital channels
and the frame-lost and failsafe bits. The end byte is 0x00 on most gear.

Point it at the receiver's SBUS line. If a decode comes back empty, the most
common cause is the polarity: an already-inverted capture wants Idle high.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import serial_bytes

_START = 0x0F


def _channels(data):
    """The sixteen 11-bit channels out of the 22 data bytes, LSB first."""
    bits = 0
    for k, b in enumerate(data):
        bits |= (b & 0xFF) << (8 * k)
    return [(bits >> (11 * c)) & 0x7FF for c in range(16)]


class SbusDecoder(Decoder):

    NAME = "SBUS"
    DESCRIPTION = "Futaba / FrSky RC serial"
    COLOUR = "#5090d0"                     # blue

    SOURCES = ["Line"]
    PARAMS = [
        Param("polarity", "Polarity", "choice",
              choices=[("rs232", "Idle low (raw SBUS)"),
                       ("ttl", "Idle high (already inverted)")],
              default="rs232",
              note="Raw SBUS idles low; a de-inverted capture idles high."),
    ]

    def decode(self, signals, params):
        sig = signals["Line"]
        inverted = params.get("polarity", "rs232") == "rs232"
        bytes_, err, detected, bit_samples = serial_bytes(sig, 100000, "8E2",
                                                          inverted)
        if err:
            return [], err
        if not bytes_:
            return [], "no bytes on this line"
        vals = [b.value & 0xFF for b in bytes_]

        frames = []
        packets = 0
        n = len(vals)
        i = 0
        while i + 25 <= n:
            if vals[i] != _START:
                i += 1
                continue
            packet = bytes_[i:i + 25]
            data = vals[i + 1:i + 23]
            chans = _channels(data)
            flags = vals[i + 23]
            # A start marker and one channel chevron per channel, spread
            # across the 22 data bytes, then the flags.
            frames.append(Frame(time=packet[0].time, index=packet[0].index,
                                end_index=packet[0].end_index,
                                value=_START, kind="start"))
            first, last = packet[1], packet[22]
            span = last.end_index - first.index
            for c, val in enumerate(chans):
                a = first.index + span * c // 16
                b = first.index + span * (c + 1) // 16
                frames.append(Frame(time=first.time, index=a, end_index=b,
                                    value=val, tag=c + 1, kind="chan"))
            fb = packet[23]
            frames.append(Frame(time=fb.time, index=fb.index,
                                end_index=fb.end_index, value=flags,
                                kind="flags"))
            packets += 1
            i += 25

        note = "%d baud  8E2  inverted  %d frame(s) of 16 channels" % (
            detected, packets)
        if not packets:
            note += " - no 0x0F frame start found; check the polarity"
        return frames, note

    def columns(self):
        return [("Time", 92), ("Field", 60), ("Value", 60), ("Info", 130)]

    def row(self, frame, ascii=True):
        if frame.kind == "start":
            return ["Start", "0F", "frame start"]
        if frame.kind == "flags":
            v = frame.value
            bits = []
            if v & 0x01:
                bits.append("ch17")
            if v & 0x02:
                bits.append("ch18")
            if v & 0x04:
                bits.append("frame lost")
            if v & 0x08:
                bits.append("failsafe")
            return ["Flags", "%02X" % v, ", ".join(bits) or "none"]
        return ["Ch %d" % frame.tag, "%d" % frame.value, ""]

    def label(self, frame, ascii=True):
        if frame.kind == "start":
            return "SB"
        if frame.kind == "flags":
            return "FL" + ("!" if frame.value & 0x0C else "")
        return "%d" % frame.value
