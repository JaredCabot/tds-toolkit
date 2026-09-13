"""CAN bus - the automotive/industrial two-wire bus, decoded off one line.

A scope sees the bus on a transceiver's RX pin (idle high, a dominant bit
pulls it low) or on CAN_H/CAN_L directly (invert the polarity for that). Bits
are NRZ, read at the centre of each bit time; the rate is on the wire, so it
is found the same way as a UART's unless you pin it.

Two things make CAN more than a shift register, and both are here:

  * bit stuffing - after five equal bits the transmitter inserts an opposite
    one, which the reader removes, from the start of frame through the CRC;
  * the 15-bit CRC over everything before it, checked so a frame that did not
    survive the bus reads as bad, not as plausible data.

Standard (11-bit) and extended (29-bit) identifiers are both read; a remote
frame (RTR) carries no data. Fault confinement, error frames and CAN-FD are
out of scope - this reads the data frames a bench capture is full of.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import _shortest_run

_RATES = [125000, 250000, 500000, 1000000]


def _crc15(bits):
    """The CAN 15-bit CRC (poly 0x4599) over the given destuffed bits."""
    crc = 0
    for b in bits:
        nxt = ((crc >> 14) & 1) ^ (b & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= 0x4599
    return crc


class _Reader(object):
    """Reads bits at bit-time centres, removing stuff bits as it goes and
    keeping the destuffed bits (and their sample centres) for the CRC and
    for drawing the fields."""

    def __init__(self, levels, start_edge, bit_samples):
        self.L = levels
        self.n = len(levels)
        self.bs = bit_samples
        self.pos = start_edge + bit_samples * 0.5   # centre of the first bit
        self.seq = []
        self.centres = []
        self.run = 0
        self.last = -1
        self.stuffing = True

    def _raw(self):
        i = int(self.pos)
        if i >= self.n:
            raise IndexError
        self.pos += self.bs
        return self.L[i], i

    def get(self):
        """Next destuffed bit, with its sample centre."""
        if self.stuffing and self.run == 5:
            s, _ = self._raw()                  # stuff bit, dropped from data
            self.last = s
            self.run = 1
        b, c = self._raw()
        if b == self.last:
            self.run += 1
        else:
            self.last = b
            self.run = 1
        self.seq.append(b)
        self.centres.append(c)
        return b

    def field(self, nbits):
        """Read nbits MSB-first; return (value, first_centre, last_centre)."""
        value = 0
        first = last = self.pos
        for k in range(nbits):
            bit = self.get()
            if k == 0:
                first = self.centres[-1]
            last = self.centres[-1]
            value = (value << 1) | bit
        return value, first, last


class CanDecoder(Decoder):

    NAME = "CAN"
    DESCRIPTION = "Automotive / industrial two-wire bus"
    COLOUR = "#d05f9a"                     # pink

    SOURCES = ["Line"]
    PARAMS = [
        Param("bitrate", "Bit rate", "choice",
              choices=[(0, "Auto")] + [(r, "%d k" % (r // 1000))
                                       for r in _RATES],
              default=0, note="Auto finds the rate from the record."),
        Param("polarity", "Polarity", "choice",
              choices=[("rx", "RX pin (idle high, dominant low)"),
                       ("canh", "CAN_H (dominant high)")],
              default="rx"),
    ]

    def decode(self, signals, params):
        sig = signals["Line"]
        if sig.span < 8:
            return [], "no signal on this line"
        d = sig.digitize()
        # Logical bus: dominant 0, recessive 1. On CAN_H the dominant bit is
        # the high one, so that polarity is the inverted read.
        inverted = params.get("polarity") == "canh"
        levels = bytes((1 - x) for x in d) if inverted else bytes(d)

        want = int(params.get("bitrate") or 0)
        if want > 0:
            bit_samples = 1.0 / (want * sig.dt)
        else:
            bit_samples = float(_shortest_run(sig))
        if bit_samples < 3:
            return [], "too few samples per bit - slow the timebase"

        n = len(levels)
        frames = []
        seen = 0
        i = 1
        while i < n:
            if not (levels[i] == 0 and levels[i - 1] == 1):
                i += 1
                continue
            edge = i
            try:
                end = self._one_frame(sig, levels, edge, bit_samples, frames)
            except IndexError:
                break
            if end is None:
                i = edge + 1
                continue
            seen += 1
            i = int(end + 10 * bit_samples)    # skip CRC delim, ACK, EOF

        bad = sum(1 for f in frames if f.kind == "crc" and f.status)
        rate = int(round(1.0 / (bit_samples * sig.dt)))
        note = "%d frame(s), %d CRC error(s), ~%d bit/s%s" % (
            seen, bad, rate, " (auto)" if want == 0 else "")
        return frames, note

    def _one_frame(self, sig, levels, edge, bs, frames):
        """Parse one frame from `edge` (a recessive->dominant SOF edge),
        append its fields to `frames`, and return the last CRC-bit centre -
        or None if it does not begin a frame."""
        r = _Reader(levels, edge, bs)
        fi = sig.first_index

        def emit(kind, value, c0, c1, tag=0, status=0):
            frames.append(Frame(time=sig.time_at(int(c0)),
                                index=fi + int(c0 - bs / 2),
                                end_index=fi + int(c1 + bs / 2),
                                value=value, tag=tag, status=status,
                                kind=kind))

        if r.get() != 0:                       # SOF must be dominant
            return None
        base, idc0, idc1 = r.field(11)
        rtr_srr = r.get()
        ide = r.get()
        extended = ide == 1
        if not extended:
            ident, id_end, rtr = base, idc1, rtr_srr
            r.get()                            # r0 reserved
            dlc, dc0, dc1 = r.field(4)
        else:
            ext, _ec0, ec1 = r.field(18)
            ident, id_end = (base << 18) | ext, ec1
            rtr = r.get()                      # RTR (SRR/IDE were before)
            r.get()
            r.get()                            # r1, r0 reserved
            dlc, dc0, dc1 = r.field(4)
        n_data = 0 if rtr else min(dlc, 8)

        data = []
        for _ in range(n_data):
            v, bc0, bc1 = r.field(8)
            data.append((v, bc0, bc1))

        crc_input = list(r.seq)                # SOF..end of data, destuffed
        calc = _crc15(crc_input)
        crc, cc0, cc1 = r.field(15)

        emit("id", ident, idc0, id_end,
             tag=(1 if extended else 0) | (2 if rtr else 0))
        emit("dlc", dlc, dc0, dc1, tag=n_data)
        for v, bc0, bc1 in data:
            emit("data", v, bc0, bc1)
        emit("crc", crc, cc0, cc1, tag=calc, status=0 if crc == calc else 1)
        return cc1

    def columns(self):
        return [("Time", 92), ("Field", 72), ("Value", 84), ("Info", 120)]

    def row(self, frame, ascii=True):
        v = frame.value
        if frame.kind == "id":
            ext = frame.tag & 1
            rtr = frame.tag & 2
            return ["ID (29-bit)" if ext else "ID",
                    "%0*X" % (8 if ext else 3, v),
                    "remote" if rtr else "data"]
        if frame.kind == "dlc":
            return ["DLC", str(v), "%d data byte(s)" % frame.tag]
        if frame.kind == "crc":
            return ["CRC", "%04X" % v,
                    "OK" if not frame.status else "BAD - calc %04X" % frame.tag]
        ch = chr(v) if 32 <= v < 127 else "."
        return ["Data", "%02X" % (v & 0xFF), ch]

    def label(self, frame, ascii=True):
        if frame.kind == "id":
            return ("R " if frame.tag & 2 else "") + "%X" % frame.value
        if frame.kind == "dlc":
            return "L%d" % frame.value
        if frame.kind == "crc":
            return "CRC!" if frame.status else "CRC"
        return "%02X" % (frame.value & 0xFF)
