"""ARINC 429 - the avionics data bus, bipolar return-to-zero on a wire pair.

Three levels, not two: a '1' is a positive pulse for the first half of the
bit then back to null, a '0' a negative pulse the same way, and the line
rests at null between them. So this reads the raw samples against two
thresholds (a positive and a negative) rather than the one a logic decoder
uses, and takes each bit from the sign of its pulse.

A word is 32 bits - an 8-bit label, source/destination bits, the data, a
sign/status matrix and an odd-parity bit - and words are separated by at
least four bit times of null. Point it at one line of the differential pair,
or the MATH difference. Parity is checked (429 is odd). Verify the label bit
order against a known transmitter; the octal is shown reversed per the usual
convention.
"""

from tds_decode import Decoder, Frame, Param

_RATES = [12500, 100000]


def _recurring_min(gaps, occur=3):
    """Shortest gap that happens at least `occur` times - the bit period,
    robust against a single long inter-word gap."""
    counts = {}
    for g in gaps:
        counts[g] = counts.get(g, 0) + 1
    for g in sorted(counts):
        if counts[g] >= occur:
            return g
    return min(gaps) if gaps else 0


class Arinc429Decoder(Decoder):

    NAME = "ARINC 429"
    DESCRIPTION = "Avionics data bus (bipolar RZ)"
    COLOUR = "#c89030"

    SOURCES = ["Line"]
    PARAMS = [
        Param("bitrate", "Bit rate", "choice",
              choices=[(0, "Auto")] + [(r, "%d k" % (r // 1000))
                                       for r in _RATES],
              default=0, note="Auto finds the rate from the record."),
    ]

    def decode(self, signals, params):
        sig = signals["Line"]
        data = sig.data
        n = sig.n
        if n < 16:
            return [], "record too short"
        lo = min(data)
        hi = max(data)
        if hi - lo < 8:
            return [], "no signal on this line"
        pos_thr = hi * 0.4
        neg_thr = lo * 0.4
        if pos_thr <= 0 or neg_thr >= 0:
            return [], "not a bipolar signal - try a single-ended decoder"

        # Each bit is one pulse; find where a pulse begins (null -> +/-).
        pulses = []
        prev = 0
        for i in range(n):
            v = data[i]
            cur = 1 if v > pos_thr else (-1 if v < neg_thr else 0)
            if cur and not prev:
                pulses.append((i, cur))
            prev = cur
        if len(pulses) < 4:
            return [], "no pulses - is this the right source?"

        want = int(params.get("bitrate") or 0)
        if want > 0:
            bit_samples = 1.0 / (want * sig.dt)
        else:
            gaps = [pulses[k + 1][0] - pulses[k][0]
                    for k in range(len(pulses) - 1)]
            bit_samples = float(_recurring_min(gaps))
        if bit_samples < 2:
            return [], "too few samples per bit - slow the timebase"

        # Split into words where the null gap runs past ~2 bit times.
        frames = []
        word = [pulses[0]]
        for prev_p, this_p in zip(pulses, pulses[1:]):
            if this_p[0] - prev_p[0] > 2.0 * bit_samples:
                frames.append(self._word(sig, word))
                word = [this_p]
            else:
                word.append(this_p)
        frames.append(self._word(sig, word))
        frames = [f for f in frames if f is not None]
        rate = int(round(1.0 / (bit_samples * sig.dt)))
        note = "%d word(s), ~%d bit/s%s" % (
            len(frames), rate, " (auto)" if want == 0 else "")
        return frames, note

    def _word(self, sig, pulses):
        if len(pulses) < 8:
            return None
        bits = [1 if pol > 0 else 0 for _pos, pol in pulses[:32]]
        value = 0
        for k, b in enumerate(bits):              # bit 1 sent first = LSB
            value |= b << k
        ones = sum(bits)
        return Frame(time=sig.time_at(pulses[0][0]),
                     index=sig.first_index + pulses[0][0],
                     end_index=sig.first_index + pulses[-1][0],
                     value=value, tag=len(bits),
                     status=0 if (ones % 2 == 1) else 1, kind="word")

    @staticmethod
    def _label(value):
        # bits 1-8, shown bit-reversed as octal, which is the field's usual
        # printed form.
        lb = value & 0xFF
        rev = 0
        for k in range(8):
            rev = (rev << 1) | ((lb >> k) & 1)
        return rev

    def columns(self):
        return [("Time", 92), ("Label", 54), ("Data", 70), ("SSM", 44),
                ("Parity", 60)]

    def row(self, frame, ascii=True):
        v = frame.value
        data = (v >> 10) & 0x7FFFF
        ssm = (v >> 29) & 0x3
        return ["%03o" % self._label(v), "%05X" % data, "%d" % ssm,
                "OK" if not frame.status else "ODD?"]

    def label(self, frame, ascii=True):
        return "L%03o" % self._label(frame.value)
