"""SPI - a clock and one or two data lines, optionally a chip select.

CLK and MOSI are required; MISO and CS are optional roles - map them if the
capture has them. Each word is assembled from the data line sampled on the
clock's active edge, which the mode fixes:

  * the active (sampling) edge is rising when CPOL == CPHA, falling
    otherwise - the four SPI modes in one line;
  * bit order is the setting most people get wrong first, so it is explicit
    (MSB first by default);
  * with CS mapped, only clocks while CS is asserted are decoded and a CS
    release ends the current word, so word boundaries are the real ones;
    without CS, words are cut every `bits` clocks, which is all an untethered
    clock allows.

No rate to detect: like I2C, the clock is on the wire.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import OK


class SpiDecoder(Decoder):

    NAME = "SPI"
    DESCRIPTION = "Clock + MOSI (+ MISO, + CS)"
    COLOUR = "#50b050"                     # green

    SOURCES = ["CLK", "MOSI", "MISO?", "CS?"]
    PARAMS = [
        Param("mode", "SPI mode", "choice",
              choices=[("0", "0 (CPOL0 CPHA0)"), ("1", "1 (CPOL0 CPHA1)"),
                       ("2", "2 (CPOL1 CPHA0)"), ("3", "3 (CPOL1 CPHA1)")],
              default="0"),
        Param("order", "Bit order", "choice",
              choices=[("msb", "MSB first"), ("lsb", "LSB first")],
              default="msb"),
        Param("bits", "Word size", "int", default=8, lo=4, hi=32,
              unit="bits"),
        Param("cs", "Chip select", "choice",
              choices=[("low", "Active low"), ("high", "Active high"),
                       ("none", "Ignore / not mapped")],
              default="low"),
    ]

    def decode(self, signals, params):
        clk_sig = signals["CLK"]
        mosi_sig = signals.get("MOSI")
        miso_sig = signals.get("MISO")
        cs_sig = signals.get("CS")
        if clk_sig.span < 8:
            return [], "no signal on CLK"
        if mosi_sig is None or mosi_sig.span < 8:
            return [], "no signal on MOSI"

        mode = params.get("mode", "0")
        cpol = mode in ("2", "3")
        cpha = mode in ("1", "3")
        sample_on_rising = (cpol == cpha)
        msb = params.get("order", "msb") == "msb"
        bits = int(params.get("bits") or 8)
        cs_mode = params.get("cs", "low")

        clk = clk_sig.digitize()
        mosi = mosi_sig.digitize()
        miso = miso_sig.digitize() if miso_sig is not None else None
        cs = cs_sig.digitize() if (cs_sig is not None
                                   and cs_mode != "none") else None
        n = len(clk)

        def asserted(i):
            if cs is None:
                return True
            return (cs[i] == 0) if cs_mode == "low" else (cs[i] == 1)

        frames = []
        count = 0
        mo = mi = 0
        word_start = 0
        words = 0
        prev_clk = clk[0]
        prev_cs_ok = asserted(0)
        for i in range(1, n):
            ok = asserted(i)
            if cs is not None and ok != prev_cs_ok:
                count = 0                      # CS edge ends any part-word
                mo = mi = 0
            c = clk[i]
            if ok and c != prev_clk:
                rising = c and not prev_clk
                if rising == sample_on_rising:
                    if count == 0:
                        word_start = i
                    bit_mo = mosi[i]
                    bit_mi = miso[i] if miso is not None else 0
                    if msb:
                        mo = (mo << 1) | bit_mo
                        mi = (mi << 1) | bit_mi
                    else:
                        mo |= bit_mo << count
                        mi |= bit_mi << count
                    count += 1
                    if count >= bits:
                        frames.append(Frame(time=clk_sig.time_at(word_start),
                                            index=clk_sig.first_index
                                            + word_start,
                                            end_index=clk_sig.first_index + i,
                                            value=mo, tag=mi, status=OK,
                                            kind="word"))
                        words += 1
                        count = 0
                        mo = mi = 0
            prev_clk = c
            prev_cs_ok = ok

        note = "%d word(s) of %d bits, mode %s%s" % (
            words, bits, mode, "" if miso is not None else "  (MOSI only)")
        return frames, note

    def columns(self):
        return [("Time", 92), ("MOSI", 60), ("MISO", 60), ("ASCII", 52)]

    def row(self, frame, ascii=True):
        mo = "%02X" % (frame.value & 0xFF)
        mi = "%02X" % (frame.tag & 0xFF)
        ch = chr(frame.value) if 32 <= frame.value < 127 else "."
        return [mo, mi, ch]

    def label(self, frame, ascii=True):
        return "%02X" % (frame.value & 0xFF)
