"""XBee - the API-mode frames a Digi radio exchanges with its host over the
serial line, 8N1. The radio link itself is off the air and not on a scope;
what a probe on the UART sees is the API framing, and that is what this reads.

Bytes come off the shared serial core. A frame is a 0x7E start delimiter, a
two-byte length, that many frame-data bytes (the first of which is the API
frame type), and a one-byte checksum - 0xFF minus the sum of the frame data.

One wire, idle high, like any TTL UART.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import serial_bytes, STANDARD_BAUDS

_TYPES = {
    0x08: "AT Command", 0x09: "AT Command (queued)", 0x10: "TX Request",
    0x11: "Explicit TX", 0x17: "Remote AT", 0x88: "AT Response",
    0x8A: "Modem Status", 0x8B: "TX Status", 0x90: "RX Packet",
    0x91: "Explicit RX", 0x97: "Remote AT Response",
}


class XBeeDecoder(Decoder):

    NAME = "XBee"
    DESCRIPTION = "Digi radio API frames (serial)"
    COLOUR = "#9d7cd8"

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
        vals = [b.value & 0xFF for b in bytes_]
        n = len(vals)
        frames = []
        seen = 0
        i = 0
        while i < n:
            if vals[i] != 0x7E:
                i += 1
                continue
            if i + 3 >= n:
                break
            length = (vals[i + 1] << 8) | vals[i + 2]
            end = i + 3 + length
            if end >= n:
                break                          # frame runs off the record

            def mk(k, val, a):
                return Frame(time=bytes_[a].time, index=bytes_[a].index,
                             end_index=bytes_[a].end_index, value=val, kind=k)

            frames.append(mk("start", 0x7E, i))
            frames.append(Frame(time=bytes_[i + 1].time,
                                index=bytes_[i + 1].index,
                                end_index=bytes_[i + 2].end_index,
                                value=length, kind="length"))
            frames.append(mk("type", vals[i + 3], i + 3))
            for j in range(i + 4, i + 3 + length):
                frames.append(mk("data", vals[j], j))
            body = vals[i + 3:i + 3 + length]
            cksum = vals[end]
            good = (sum(body) + cksum) & 0xFF == 0xFF
            frames.append(Frame(time=bytes_[end].time,
                                index=bytes_[end].index,
                                end_index=bytes_[end].end_index, value=cksum,
                                status=0 if good else 1, kind="checksum"))
            seen += 1
            i = end + 1
        note = "%d baud%s  %d frame(s)" % (
            detected, " (auto)" if want == 0 else "", seen)
        return frames, note

    def columns(self):
        return [("Time", 92), ("Field", 64), ("Value", 54), ("Info", 130)]

    def row(self, frame, ascii=True):
        v = frame.value
        if frame.kind == "start":
            return ["Start", "7E", "frame delimiter"]
        if frame.kind == "length":
            return ["Length", "%d" % v, "%d frame-data byte(s)" % v]
        if frame.kind == "type":
            return ["Type", "%02X" % v, _TYPES.get(v, "frame type %02X" % v)]
        if frame.kind == "checksum":
            return ["Checksum", "%02X" % v,
                    "OK" if not frame.status else "BAD"]
        ch = chr(v) if 32 <= v < 127 else "."
        return ["Data", "%02X" % v, ch]

    def label(self, frame, ascii=True):
        if frame.kind == "start":
            return "7E"
        if frame.kind == "length":
            return "L%d" % frame.value
        if frame.kind == "type":
            return "T%02X" % frame.value
        if frame.kind == "checksum":
            return "CK!" if frame.status else "CK"
        return "%02X" % (frame.value & 0xFF)
