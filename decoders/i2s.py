"""I2S - digital audio: a bit clock, a word-select, and one data line.

Three wires walked together, like SPI but framed by WS instead of CS:

  * SCK is the bit clock; SD is sampled on its rising edge, MSB first;
  * WS (word select / LR clock) says which channel the word belongs to and,
    by changing, marks where one word ends and the next begins - low is the
    left channel by convention (settable);
  * standard I2S delays the word one clock after the WS edge (the "1-bit
    delay"); left-justified does not. That one setting is the difference
    between the two common formats, so it is a choice, not guesswork.

No rate to detect: the clock is on the wire. The word is shown as a signed
sample, which is what audio data is.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import OK


class I2sDecoder(Decoder):

    NAME = "I2S"
    DESCRIPTION = "Digital audio: SCK + WS + SD"
    COLOUR = "#40b0a0"                     # teal-green

    SOURCES = ["SCK", "WS", "SD"]
    PARAMS = [
        Param("bits", "Word size", "int", default=16, lo=8, hi=32,
              unit="bits"),
        Param("format", "Format", "choice",
              choices=[("i2s", "I2S (1-bit delay)"),
                       ("left", "Left-justified")],
              default="i2s"),
        Param("ws", "WS low is", "choice",
              choices=[("left", "Left channel"), ("right", "Right channel")],
              default="left"),
    ]

    def decode(self, signals, params):
        sck_sig = signals["SCK"]
        ws_sig = signals.get("WS")
        sd_sig = signals.get("SD")
        if sck_sig.span < 8:
            return [], "no signal on SCK"
        if ws_sig is None or ws_sig.span < 8:
            return [], "no signal on WS"
        if sd_sig is None or sd_sig.span < 8:
            return [], "no signal on SD"

        bits = int(params.get("bits") or 16)
        offset = 1 if params.get("format", "i2s") == "i2s" else 0
        low_is_left = params.get("ws", "left") == "left"

        sck = sck_sig.digitize()
        ws = ws_sig.digitize()
        sd = sd_sig.digitize()
        n = len(sck)

        frames = []
        words = 0
        gathered = []                          # (sample_index, bit) this slot
        slot_ws = ws[0]
        slot_start = 0
        prev_sck = sck[0]
        prev_ws = ws[0]

        def flush(level, endi):
            """Emit the word held in `gathered`, if it is long enough."""
            if len(gathered) < offset + bits:
                return 0
            chosen = gathered[offset:offset + bits]
            value = 0
            for _idx, bit in chosen:
                value = (value << 1) | bit     # MSB first
            left = (level == 0) == low_is_left
            frames.append(Frame(time=sck_sig.time_at(chosen[0][0]),
                                index=sck_sig.first_index + chosen[0][0],
                                end_index=sck_sig.first_index + endi,
                                value=value, length=bits, status=OK,
                                kind="left" if left else "right"))
            return 1

        for i in range(1, n):
            if ws[i] != prev_ws:               # word boundary
                words += flush(slot_ws, i)
                gathered = []
                slot_ws = ws[i]
                slot_start = i
            if sck[i] and not prev_sck:        # rising edge samples SD
                gathered.append((i, sd[i]))
            prev_sck = sck[i]
            prev_ws = ws[i]
        words += flush(slot_ws, n - 1)         # the last slot

        note = "%d word(s) of %d bits, %s" % (
            words, bits,
            "I2S" if offset else "left-justified")
        return frames, note

    def _signed(self, frame):
        v = frame.value
        if v & (1 << (frame.length - 1)):
            v -= (1 << frame.length)
        return v

    def columns(self):
        return [("Time", 92), ("Ch", 40), ("Hex", 72), ("Sample", 80)]

    def row(self, frame, ascii=True):
        ch = "L" if frame.kind == "left" else "R"
        width = (frame.length + 3) // 4
        return [ch, "%0*X" % (width, frame.value), str(self._signed(frame))]

    def label(self, frame, ascii=True):
        ch = "L" if frame.kind == "left" else "R"
        width = (frame.length + 3) // 4
        return "%s%0*X" % (ch, width, frame.value)
