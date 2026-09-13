"""CRSF - Crossfire / ExpressLRS RC link, one wire.

The wire is plain async serial, 420000 baud 8N1, idle high (TTL), so the
bytes come off the shared serial core. CRSF then frames them:

    [addr][len][type][payload ...][crc8]

`len` counts everything after it - type, payload and the CRC byte. The CRC is
a CRC-8 with polynomial 0xD5 (DVB-S2) taken over the type and payload, so a
frame that did not survive the link reads as bad rather than as plausible
channel data. `addr` is the destination (0xC8 a flight controller, 0xEA a
handset, 0xEE a transmitter module); it doubles as the sync byte a stream is
framed on.

Point it at the single CRSF line (or the MATH of a differential pair).
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import serial_bytes

_ADDR = {0xC8: "flight ctrl", 0xEA: "handset", 0xEE: "TX module",
         0xEC: "receiver", 0x00: "broadcast"}
_TYPE = {0x02: "GPS", 0x08: "Battery", 0x14: "Link stats",
         0x16: "RC channels", 0x1E: "Attitude", 0x21: "Flight mode",
         0x28: "Ping", 0x29: "Device info", 0x2B: "Parameter",
         0x32: "Command"}


def _crc8(data):
    """CRSF CRC-8, polynomial 0xD5, init 0, not reflected."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc & 0xFF


class CrsfDecoder(Decoder):

    NAME = "CRSF"
    DESCRIPTION = "Crossfire / ExpressLRS RC link"
    COLOUR = "#c060c0"                     # magenta

    SOURCES = ["Line"]
    PARAMS = [
        Param("baud", "Bit rate", "choice",
              choices=[(0, "Auto"), (420000, "420000"), (400000, "400000"),
                       (115200, "115200")],
              default=420000,
              note="CRSF runs at 420000; Auto reads it from the record."),
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
        vals = [b.value & 0xFF for b in bytes_]

        frames = []
        msgs = bad = 0
        i = 0
        n = len(vals)
        while i < n - 1:
            length = vals[i + 1]
            # A CRSF frame is addr + len + (len) bytes; len counts type,
            # payload and CRC and is 2..62. Anything else is not a frame
            # header, so step on a byte and try to re-sync.
            if 2 <= length <= 62 and i + 1 + length < n:
                addr = bytes_[i]
                lenb = bytes_[i + 1]
                body = bytes_[i + 2:i + 2 + length]        # type..crc
                calc = _crc8([b.value & 0xFF for b in body[:-1]])
                got = body[-1].value & 0xFF
                status = 0 if got == calc else 1
                frames.append(Frame(time=addr.time, index=addr.index,
                                    end_index=addr.end_index,
                                    value=addr.value & 0xFF, kind="addr"))
                frames.append(Frame(time=lenb.time, index=lenb.index,
                                    end_index=lenb.end_index,
                                    value=length, kind="len"))
                frames.append(Frame(time=body[0].time, index=body[0].index,
                                    end_index=body[0].end_index,
                                    value=body[0].value & 0xFF, kind="type"))
                for b in body[1:-1]:
                    frames.append(Frame(time=b.time, index=b.index,
                                        end_index=b.end_index,
                                        value=b.value & 0xFF, kind="data"))
                crcb = body[-1]
                frames.append(Frame(time=crcb.time, index=crcb.index,
                                    end_index=crcb.end_index, value=got,
                                    tag=calc, status=status, kind="crc"))
                msgs += 1
                if status:
                    bad += 1
                i += 2 + length
            else:
                i += 1

        note = "%d baud%s  8N1  %d frame(s)  %d CRC error(s)" % (
            detected, " (auto)" if want == 0 else "", msgs, bad)
        if not msgs:
            note += " - no CRSF frame found; check the bit rate"
        return frames, note

    def columns(self):
        return [("Time", 92), ("Field", 56), ("Value", 54), ("Info", 140)]

    def row(self, frame, ascii=True):
        v = frame.value
        if frame.kind == "addr":
            return ["Addr", "%02X" % v, _ADDR.get(v, "device %02X" % v)]
        if frame.kind == "len":
            return ["Len", "%d" % v, "%d bytes to follow" % v]
        if frame.kind == "type":
            return ["Type", "%02X" % v, _TYPE.get(v, "type %02X" % v)]
        if frame.kind == "crc":
            return ["CRC", "%02X" % v,
                    "OK" if not frame.status else "BAD - calc %02X" % frame.tag]
        return ["Data", "%02X" % v, ""]

    def label(self, frame, ascii=True):
        v = frame.value
        if frame.kind == "addr":
            return "@%02X" % v
        if frame.kind == "len":
            return "L%d" % v
        if frame.kind == "type":
            return _TYPE.get(v, "%02X" % v).split()[0]
        if frame.kind == "crc":
            return "CRC!" if frame.status else "CRC"
        return "%02X" % v
