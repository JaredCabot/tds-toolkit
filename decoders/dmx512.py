"""DMX512 - stage-lighting control over RS-485: 250 kbaud, 8N2, one packet
of up to 513 slots begun by a break.

The bytes come off the shared serial core at the fixed DMX rate. A packet is
a break (a long dominant the core reads as a framing-error 0x00), a mark, a
start code (slot 0 - 0x00 for ordinary dimmer data), and then the channel
levels, one byte each. Packets are told apart by the break's idle, as LIN and
MODBUS frames are.

Point it at one line, or at the differential MATH of the RS-485 pair; idle
is high (mark).
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import serial_bytes


class Dmx512Decoder(Decoder):

    NAME = "DMX512"
    DESCRIPTION = "Stage lighting over RS-485"
    COLOUR = "#d0a24a"

    SOURCES = ["Line"]
    PARAMS = [
        Param("polarity", "Polarity", "choice",
              choices=[("ttl", "Idle high (logic / MATH)"),
                       ("rs232", "Idle low (inverted)")],
              default="ttl"),
    ]

    def decode(self, signals, params):
        sig = signals["Line"]
        inverted = params.get("polarity") == "rs232"
        bytes_, err, detected, bit_samples = serial_bytes(sig, 250000, "8N2",
                                                          inverted)
        if err:
            return [], err
        if not bytes_:
            return [], "no bytes on this line"
        # Each packet opens with a break, which the serial core reports as a
        # framing-error byte (a long dominant that overruns a byte), so a
        # framing error marks where one packet ends and the next begins.
        frames = []
        group = []
        for b in bytes_:
            if b.status and group:
                frames.extend(self._packet(group))
                group = [b]
            else:
                group.append(b)
        if group:
            frames.extend(self._packet(group))
        note = "250000 baud  %.1f smp/bit  %d packet(s)" % (
            bit_samples, sum(1 for f in frames if f.kind == "start"))
        return frames, note

    def _packet(self, group):
        out = []
        # The group opens with the break's framing-error byte; the start code
        # (slot 0) is the first clean byte after it.
        rest = group
        if group and group[0].status:
            b = group[0]
            out.append(Frame(time=b.time, index=b.index,
                             end_index=b.end_index, value=0, kind="break"))
            rest = group[1:]
        if not rest:
            return out
        # Slot 0 is the start code; the rest are channel levels 1..N.
        sc = rest[0]
        out.append(Frame(time=sc.time, index=sc.index, end_index=sc.end_index,
                         value=sc.value & 0xFF, kind="start"))
        for ch, b in enumerate(rest[1:], start=1):
            out.append(Frame(time=b.time, index=b.index, end_index=b.end_index,
                             value=b.value & 0xFF, tag=ch, kind="channel"))
        return out

    def columns(self):
        return [("Time", 92), ("Slot", 60), ("Value", 54), ("Level", 90)]

    def row(self, frame, ascii=True):
        v = frame.value
        if frame.kind == "break":
            return ["Break", "", "packet start"]
        if frame.kind == "start":
            return ["Start code", "%02X" % v,
                    "dimmer data" if v == 0 else "code %02X" % v]
        return ["Ch %d" % frame.tag, "%02X" % v, "%d%%" % round(v / 2.55)]

    def label(self, frame, ascii=True):
        if frame.kind == "break":
            return "BRK"
        if frame.kind == "start":
            return "SC%02X" % frame.value
        return "%02X" % (frame.value & 0xFF)
