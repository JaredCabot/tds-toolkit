"""MIL-STD-1553 - the military avionics bus: bipolar Manchester at 1 Mbit on
a transformer-coupled pair.

Two levels but bipolar (+ and -), and Manchester coded: each bit has a
mid-cell transition, a '1' going high-to-low and a '0' low-to-high. A word
opens with a three-bit-time sync that deliberately breaks the Manchester rule
- high for one and a half bits then low for a command or status word, low
then high for a data word - which is how a word is found. After the sync come
16 data bits and one odd-parity bit.

This reads the raw samples against a positive and a negative threshold, takes
the sync from the long run that breaks Manchester, and each bit from the
polarity of its first half. Point it at one line of the pair or the MATH
difference. Verify against a known transmitter before trusting it on a live
bus; command and status words share a sync and are told apart by protocol
context, so both are reported as "cmd/status".
"""

from tds_decode import Decoder, Frame, Param


class Mil1553Decoder(Decoder):

    NAME = "MIL-STD-1553"
    DESCRIPTION = "Avionics bus, bipolar Manchester"
    COLOUR = "#b06fb0"

    SOURCES = ["Line"]
    PARAMS = [
        Param("bitrate", "Bit rate", "choice",
              choices=[(0, "Auto"), (1000000, "1 M")], default=1000000),
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

        def pol(i):
            v = data[i]
            return 1 if v > pos_thr else (-1 if v < neg_thr else 0)

        want = int(params.get("bitrate") or 0)
        if want > 0:
            bit_samples = 1.0 / (want * sig.dt)
        else:
            bit_samples = float(self._auto_bit(data, n, pos_thr, neg_thr))
        if bit_samples < 4:
            return [], "too few samples per bit - slow the timebase"

        half = bit_samples / 2.0
        sync_run = int(bit_samples * 1.3)         # longer than any data run
        frames = []
        i = 1
        while i < n - int(20 * bit_samples):
            p = pol(i)
            if not p or pol(i - 1) == p:
                i += 1
                continue
            # a run of one polarity at least 1.3 bit times long is a sync
            j = i
            while j < n and pol(j) == p:
                j += 1
            if (j - i) < sync_run:
                i = j
                continue
            data_word = self._word(sig, i, p, bit_samples, pol, half)
            if data_word is None:
                i = j
                continue
            frames.append(data_word)
            i = int(i + 20 * bit_samples)
        note = "%d word(s), %.0f samples/bit" % (len(frames), bit_samples)
        return frames, note

    def _auto_bit(self, data, n, pos_thr, neg_thr):
        # shortest recurring run of constant polarity is a Manchester
        # half-bit; the bit is twice that.
        counts = {}
        prev = 0
        start = 0
        for i in range(n):
            v = data[i]
            cur = 1 if v > pos_thr else (-1 if v < neg_thr else 0)
            if cur != prev:
                run = i - start
                if run > 1:
                    counts[run] = counts.get(run, 0) + 1
                start = i
                prev = cur
        half = 0
        for r in sorted(counts):
            if counts[r] >= 3:
                half = r
                break
        return 2 * half if half else 0

    def _word(self, sig, sync_start, first_pol, bit_samples, pol, half):
        # 16 data bits + 1 parity, Manchester, after the 3-bit sync.
        base = sync_start + 3 * bit_samples
        bits = []
        for k in range(17):
            c = int(base + k * bit_samples + half / 2.0)   # first half of bit
            if c >= sig.n:
                return None
            bits.append(1 if pol(c) > 0 else 0)
        word = 0
        for b in bits[:16]:                       # first bit after sync is MSB
            word = (word << 1) | b
        ones = sum(bits)
        return Frame(time=sig.time_at(sync_start),
                     index=sig.first_index + sync_start,
                     end_index=sig.first_index + int(base + 17 * bit_samples),
                     value=word, tag=(0 if first_pol > 0 else 1),
                     status=0 if (ones % 2 == 1) else 1, kind="word")

    def columns(self):
        return [("Time", 92), ("Type", 90), ("Word", 70), ("Parity", 60)]

    def row(self, frame, ascii=True):
        kind = "data" if frame.tag else "cmd/status"
        return [kind, "%04X" % frame.value,
                "OK" if not frame.status else "ODD?"]

    def label(self, frame, ascii=True):
        pre = "D" if frame.tag else "C"
        return "%s%04X" % (pre, frame.value)
