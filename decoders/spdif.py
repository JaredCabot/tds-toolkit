"""S/PDIF (AES3) - digital audio on one self-clocking line, biphase-mark
coded. There is no separate clock: a transition falls on every bit-cell
boundary, and a '1' adds a second transition in the middle of the cell, so
the time between edges is one half-cell for a '1' and a whole cell for a '0'.

A subframe is 32 cells: a 4-cell preamble, then 4 auxiliary, 20 audio, and
the validity, user, channel-status and parity bits, least-significant first.
The preamble breaks the biphase rule with a three-half-cell run, which is how
a subframe boundary is found; its pattern says which channel and whether a
192-subframe block is starting:

  3,1,1,3  = B - left, block start      3,1,3,1 = M - left
  3,1,2,2  = W - right

Point it at the coax/TOSLINK-receiver logic line. The audio word is shown as
a signed 20-bit sample. Preamble patterns follow AES3; verify against a real
capture before trusting the channel labels on unusual gear.
"""

from tds_decode import Decoder, Frame, Param

_PRE = {(3, 1, 1, 3): ("left", True), (3, 1, 3, 1): ("left", False),
        (3, 1, 2, 2): ("right", False)}


class SpdifDecoder(Decoder):

    NAME = "S/PDIF"
    DESCRIPTION = "AES3 digital audio, biphase-mark"
    COLOUR = "#4aa0d0"

    SOURCES = ["Line"]
    PARAMS = [
        Param("bits", "Audio bits", "int", default=20, lo=16, hi=24,
              unit="bits"),
    ]

    def decode(self, signals, params):
        sig = signals["Line"]
        if sig.span < 8:
            return [], "no signal on this line"
        abits = int(params.get("bits") or 20)
        d = sig.digitize()
        n = len(d)
        edges = [i for i in range(1, n) if d[i] != d[i - 1]]
        if len(edges) < 8:
            return [], "no edges - is this the right source?"
        gaps = [(edges[k], edges[k + 1] - edges[k])
                for k in range(len(edges) - 1)]
        unit = min(g for _p, g in gaps)          # one half-cell
        if unit <= 0:
            return [], "cannot find the bit cell"
        # each gap as a multiple of the half-cell: 1, 2 or 3
        cells = [(pos, max(1, int(round(g / float(unit))))) for pos, g in gaps]

        frames = []
        subs = 0
        i = 0
        while i < len(cells):
            if cells[i][1] != 3:                  # find a preamble's violation
                i += 1
                continue
            if i + 4 > len(cells):
                break
            pre = tuple(c for _p, c in cells[i:i + 4])
            chan, block = _PRE.get(pre, ("left", False))
            start = cells[i][0]
            # data cells until the next preamble (a 3), turned back into bits
            j = i + 4
            bits = []
            pend = False
            last = start
            while j < len(cells) and cells[j][1] != 3:
                pos, m = cells[j]
                last = pos
                if m == 2:
                    bits.append(0)
                    pend = False
                else:                             # m == 1: two make a '1'
                    if pend:
                        bits.append(1)
                        pend = False
                    else:
                        pend = True
                j += 1
            frames.append(self._subframe(sig, start, last, bits, chan,
                                         block, abits))
            subs += 1
            i = j
        note = "%d subframe(s), %d half-cell samples" % (subs, unit)
        return frames, note

    def _subframe(self, sig, start, end, bits, chan, block, abits):
        def take(lo, count):
            v = 0
            for k in range(count):
                if lo + k < len(bits):
                    v |= bits[lo + k] << k
            return v
        audio = take(4, abits)                    # after 4 aux bits, LSB first
        parity = 0
        for b in bits:
            parity ^= b & 1
        tag = (1 if chan == "right" else 0) | (2 if block else 0) \
            | (4 if parity else 0)
        return Frame(time=sig.time_at(start), index=sig.first_index + start,
                     end_index=sig.first_index + end, value=audio, tag=tag,
                     length=abits, kind=chan)

    def _signed(self, value, bits):
        if value & (1 << (bits - 1)):
            value -= (1 << bits)
        return value

    def columns(self):
        return [("Time", 92), ("Ch", 40), ("Audio", 90), ("Flags", 90)]

    def row(self, frame, ascii=True):
        ch = "L" if frame.kind == "left" else "R"
        flags = []
        if frame.tag & 2:
            flags.append("block")
        if frame.tag & 4:
            flags.append("parity")
        return [ch, str(self._signed(frame.value, frame.length)),
                " ".join(flags)]

    def label(self, frame, ascii=True):
        ch = "L" if frame.kind == "left" else "R"
        width = (frame.length + 3) // 4
        return "%s%0*X" % (ch, width, frame.value & ((1 << frame.length) - 1))
