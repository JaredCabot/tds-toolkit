"""FlexRay - the high-speed automotive bus (up to 10 Mbit), one channel,
NRZ. A frame is a transmission start sequence (a low run), a frame start bit,
then bytes - each led by a byte start sequence (BSS: a high bit then a low
bit) that gives the receiver an edge to resynchronise on - and a frame end
sequence. The header holds the frame ID and how many two-byte words the
payload has; a 24-bit CRC covers the header and payload.

Point it at one channel (BP or BM) of the bus. Idle is high; the TSS pulls it
low. The frame CRC is checked; a bad one reads as a fault.

FlexRay is intricate and this reads the common data frame - static or
dynamic, one channel. Bit-level error handling and channel A/B differences
are out of scope; verify against a real capture before trusting it on a live
cluster.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import _shortest_run

_RATES = [2500000, 5000000, 10000000]


def _crc24(bits):
    """FlexRay frame CRC-24: poly 0x5D6DCB, init 0xFEDCBA, over the given
    header+payload bits (most significant first)."""
    crc = 0xFEDCBA
    for b in bits:
        nxt = ((crc >> 23) & 1) ^ (b & 1)
        crc = (crc << 1) & 0xFFFFFF
        if nxt:
            crc ^= 0x5D6DCB
    return crc


class FlexRayDecoder(Decoder):

    NAME = "FlexRay"
    DESCRIPTION = "High-speed automotive bus"
    COLOUR = "#d0605f"

    SOURCES = ["Line"]
    PARAMS = [
        Param("bitrate", "Bit rate", "choice",
              choices=[(0, "Auto")] + [(r, "%d M" % (r // 1000000))
                                       for r in _RATES],
              default=0, note="Auto finds the rate from the record."),
    ]

    def decode(self, signals, params):
        sig = signals["Line"]
        if sig.span < 8:
            return [], "no signal on this line"
        d = sig.digitize()
        n = len(d)
        want = int(params.get("bitrate") or 0)
        bs = 1.0 / (want * sig.dt) if want > 0 else float(_shortest_run(sig))
        if bs < 3:
            return [], "too few samples per bit - slow the timebase"

        frames = []
        seen = 0
        i = 1
        while i < n:
            # TSS: a low run of several bit times after idle high.
            if not (d[i] == 0 and d[i - 1] == 1):
                i += 1
                continue
            j = i
            while j < n and d[j] == 0:
                j += 1
            if (j - i) < 2.5 * bs or j >= n:      # too short to be a TSS
                i = j + 1 if j > i else i + 1
                continue
            end = self._one_frame(sig, d, j, bs, frames)
            if end is None:
                i = j + 1
                continue
            seen += 1
            i = int(end)

        bad = sum(1 for f in frames if f.kind == "crc" and f.status)
        rate = int(round(1.0 / (bs * sig.dt)))
        note = "%d frame(s), %d CRC error(s), ~%d bit/s%s" % (
            seen, bad, rate, " (auto)" if want == 0 else "")
        return frames, note

    def _one_frame(self, sig, d, fss, bs, frames):
        """Parse from `fss` (first high sample after the TSS). Appends the
        frame's fields and returns the sample to resume at, or None."""
        n = len(d)
        fi = sig.first_index

        def read_byte(pos):
            # BSS is a high bit then a low bit; resync on that falling edge,
            # then the eight data bits follow, most significant first.
            e = int(pos)
            lim = min(n, int(pos + 3 * bs))
            fall = None
            while e < lim:
                if d[e] == 0 and d[e - 1] == 1:
                    fall = e
                    break
                e += 1
            base = (fall + bs) if fall is not None else (pos + 2 * bs)
            val = 0
            for k in range(8):
                c = int(base + (k + 0.5) * bs)
                if c >= n:
                    raise IndexError
                val = (val << 1) | (1 if d[c] else 0)
            return val, base, base + 8 * bs

        pos = fss + bs                            # past the FSS bit
        try:
            header = []
            starts = []
            for _ in range(5):
                v, b0, pos = read_byte(pos)
                header.append(v)
                starts.append(b0)
            hbits = []
            for byte in header:
                hbits += [(byte >> k) & 1 for k in range(7, -1, -1)]

            def num(lo, count):
                v = 0
                for k in range(count):
                    v = (v << 1) | hbits[lo + k]
                return v
            frame_id = num(5, 11)
            words = num(16, 7)
            data = []
            dstarts = []
            for _ in range(words * 2):
                v, b0, pos = read_byte(pos)
                data.append(v)
                dstarts.append(b0)
            crcbytes = []
            cstart = pos
            for _ in range(3):
                v, b0, pos = read_byte(pos)
                crcbytes.append(v)
                if len(crcbytes) == 1:
                    cstart = b0
        except IndexError:
            return None

        payload_bits = []
        for byte in data:
            payload_bits += [(byte >> k) & 1 for k in range(7, -1, -1)]
        calc = _crc24(hbits + payload_bits)
        got = (crcbytes[0] << 16) | (crcbytes[1] << 8) | crcbytes[2]

        frames.append(Frame(time=sig.time_at(starts[0]),
                            index=fi + starts[0], end_index=fi + starts[-1],
                            value=frame_id, tag=words, kind="id"))
        for v, b0 in zip(data, dstarts):
            frames.append(Frame(time=sig.time_at(b0), index=fi + b0,
                                end_index=fi + int(b0 + 8 * bs), value=v,
                                kind="data"))
        frames.append(Frame(time=sig.time_at(cstart), index=fi + int(cstart),
                            end_index=fi + int(pos), value=got, tag=calc,
                            status=0 if got == calc else 1, kind="crc"))
        return pos

    def columns(self):
        return [("Time", 92), ("Field", 66), ("Value", 84), ("Info", 120)]

    def row(self, frame, ascii=True):
        v = frame.value
        if frame.kind == "id":
            return ["Frame ID", "%d" % v, "%d word(s) payload" % frame.tag]
        if frame.kind == "crc":
            return ["CRC", "%06X" % v,
                    "OK" if not frame.status else "BAD - calc %06X" % frame.tag]
        ch = chr(v) if 32 <= v < 127 else "."
        return ["Data", "%02X" % (v & 0xFF), ch]

    def label(self, frame, ascii=True):
        if frame.kind == "id":
            return "ID%d" % frame.value
        if frame.kind == "crc":
            return "CRC!" if frame.status else "CRC"
        return "%02X" % (frame.value & 0xFF)
