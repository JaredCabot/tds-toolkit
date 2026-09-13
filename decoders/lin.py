"""LIN - the automotive sub-bus that rides under CAN, one wire, 8N1.

The bytes come off the shared serial core; LIN is the framing on top. A
frame is a break (the master holds the line dominant well past a byte, which
the core reads as a framing-error 0x00), a sync byte 0x55, a protected
identifier (six ID bits and two parity bits), one to eight data bytes, and a
checksum. Frames are told apart by the break's long idle, the same way MODBUS
frames are told apart by their silence.

One wire, idle recessive (high). The checksum is the enhanced form (over the
PID and data) falling back to classic (data only) - whichever matches is the
one reported.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import serial_bytes, STANDARD_BAUDS


def _pid_parity_ok(pid):
    """The two parity bits of a protected identifier, LIN's own formula."""
    b = [(pid >> i) & 1 for i in range(6)]
    p0 = b[0] ^ b[1] ^ b[2] ^ b[4]
    p1 = 1 - (b[1] ^ b[3] ^ b[4] ^ b[5])
    return ((pid >> 6) & 1) == p0 and ((pid >> 7) & 1) == p1


def _checksum(data, pid=None):
    """LIN checksum: inverted sum with carry. Enhanced folds the PID in."""
    total = 0 if pid is None else pid
    for d in data:
        total += d
        total = (total & 0xFF) + (total >> 8)
    return (~total) & 0xFF


class LinDecoder(Decoder):

    NAME = "LIN"
    DESCRIPTION = "Automotive sub-bus, one wire"
    COLOUR = "#5f8fd0"

    SOURCES = ["Line"]
    PARAMS = [
        Param("baud", "Bit rate", "choice",
              choices=[(0, "Auto")] + [(b, str(b)) for b in STANDARD_BAUDS],
              default=0, note="Auto finds the rate from the record."),
    ]

    def decode(self, signals, params):
        sig = signals["Line"]
        want = int(params.get("baud") or 0)
        bytes_, err, detected, bit_samples = serial_bytes(sig, want, "8N1",
                                                          False)
        if err:
            return [], err
        if not bytes_:
            return [], "no bytes on this line"
        # Each frame opens with a break, which the serial core reports as a
        # framing-error byte (the long dominant runs off the end of a byte),
        # so a framing error is where one frame ends and the next begins.
        frames = []
        group = []
        for b in bytes_:
            if b.status and group:
                frames.extend(self._frame(group))
                group = [b]
            else:
                group.append(b)
        if group:
            frames.extend(self._frame(group))
        note = "%d baud%s  %d frame(s)" % (
            detected, " (auto)" if want == 0 else "",
            sum(1 for f in frames if f.kind == "pid"))
        return frames, note

    def _frame(self, group):
        out = []
        # The sync is always 0x55 and comes straight after the break, so it
        # anchors the frame. Everything before it - the break's framing-error
        # bytes and the byte that straddles the break's end and the delimiter
        # - is the break region, and is reported as one "break".
        syncpos = next((k for k, b in enumerate(group)
                        if (b.value & 0xFF) == 0x55), None)
        if syncpos is None:
            return out
        if syncpos > 0:
            b = group[0]
            out.append(Frame(time=b.time, index=b.index,
                             end_index=b.end_index, value=0, kind="break"))
        rest = group[syncpos:]
        s = rest[0]
        out.append(Frame(time=s.time, index=s.index, end_index=s.end_index,
                         value=s.value & 0xFF, status=0, kind="sync"))
        if len(rest) < 2:
            return out
        pid = rest[1].value & 0xFF
        out.append(Frame(time=rest[1].time, index=rest[1].index,
                         end_index=rest[1].end_index, value=pid,
                         tag=pid & 0x3F, status=0 if _pid_parity_ok(pid)
                         else 1, kind="pid"))
        body = rest[2:]
        if not body:
            return out
        data = [b.value & 0xFF for b in body[:-1]]
        chk = body[-1]
        for b, v in zip(body[:-1], data):
            out.append(Frame(time=b.time, index=b.index,
                             end_index=b.end_index, value=v, kind="data"))
        got = chk.value & 0xFF
        good = got in (_checksum(data, pid), _checksum(data))
        out.append(Frame(time=chk.time, index=chk.index,
                         end_index=chk.end_index, value=got,
                         status=0 if good else 1, kind="checksum"))
        return out

    def columns(self):
        return [("Time", 92), ("Field", 60), ("Value", 54), ("Info", 120)]

    def row(self, frame, ascii=True):
        v = frame.value
        if frame.kind == "break":
            return ["Break", "", "frame start"]
        if frame.kind == "sync":
            return ["Sync", "%02X" % v, "OK" if not frame.status else "not 55"]
        if frame.kind == "pid":
            return ["PID", "%02X" % v, "ID %d%s" % (frame.tag,
                    "" if not frame.status else "  parity!")]
        if frame.kind == "checksum":
            return ["Checksum", "%02X" % v,
                    "OK" if not frame.status else "BAD"]
        ch = chr(v) if 32 <= v < 127 else "."
        return ["Data", "%02X" % v, ch]

    def label(self, frame, ascii=True):
        if frame.kind == "break":
            return "BRK"
        if frame.kind == "sync":
            return "SYN"
        if frame.kind == "pid":
            return "ID%d" % frame.tag
        if frame.kind == "checksum":
            return "CK!" if frame.status else "CK"
        return "%02X" % (frame.value & 0xFF)
