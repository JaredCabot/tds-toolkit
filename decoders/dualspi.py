"""Dual SPI - a clock and two data lines, two bits a clock.

The dual-I/O mode of SPI flash and similar parts: on each active clock edge
both data lines carry a bit at once, so a byte takes four clocks rather than
eight. IO1 is the higher of the pair and IO0 the lower, most-significant pair
first, which is how a part shifts a byte out over two lines. Map CLK, IO0 and
IO1; map CS as well and only clocks while it is asserted are read and a
release ends the current byte, otherwise a byte is cut every four clocks.

The active edge follows the SPI mode the same way single SPI does: rising when
CPOL equals CPHA, falling otherwise. No rate to detect; the clock is on the
wire.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import OK


class DualSpiDecoder(Decoder):

    NAME = "Dual SPI"
    DESCRIPTION = "Clock + two data lines (IO0, IO1)"
    COLOUR = "#70a060"                     # olive

    SOURCES = ["CLK", "IO0", "IO1", "CS?"]
    PARAMS = [
        Param("mode", "SPI mode", "choice",
              choices=[("0", "0 (CPOL0 CPHA0)"), ("1", "1 (CPOL0 CPHA1)"),
                       ("2", "2 (CPOL1 CPHA0)"), ("3", "3 (CPOL1 CPHA1)")],
              default="0"),
        Param("bits", "Word size", "int", default=8, lo=4, hi=32,
              unit="bits"),
        Param("cs", "Chip select", "choice",
              choices=[("low", "Active low"), ("high", "Active high"),
                       ("none", "Ignore / not mapped")],
              default="low"),
    ]

    def decode(self, signals, params):
        clk_sig = signals["CLK"]
        io0_sig = signals.get("IO0")
        io1_sig = signals.get("IO1")
        cs_sig = signals.get("CS")
        if clk_sig.span < 8:
            return [], "no signal on CLK"
        if io0_sig is None or io0_sig.span < 8:
            return [], "no signal on IO0"
        if io1_sig is None or io1_sig.span < 8:
            return [], "no signal on IO1"

        mode = params.get("mode", "0")
        cpol = mode in ("2", "3")
        cpha = mode in ("1", "3")
        sample_on_rising = (cpol == cpha)
        bits = int(params.get("bits") or 8)
        cs_mode = params.get("cs", "low")

        clk = clk_sig.digitize()
        io0 = io0_sig.digitize()
        io1 = io1_sig.digitize()
        cs = cs_sig.digitize() if (cs_sig is not None
                                   and cs_mode != "none") else None
        n = min(len(clk), len(io0), len(io1))

        def asserted(i):
            if cs is None:
                return True
            return (cs[i] == 0) if cs_mode == "low" else (cs[i] == 1)

        frames = []
        count = 0                                # bits gathered so far
        val = 0
        word_start = 0
        words = 0
        prev_clk = clk[0]
        prev_cs_ok = asserted(0)
        for i in range(1, n):
            ok = asserted(i)
            if cs is not None and ok != prev_cs_ok:
                count = 0
                val = 0
            c = clk[i]
            if ok and c != prev_clk:
                rising = c and not prev_clk
                if rising == sample_on_rising:
                    if count == 0:
                        word_start = i
                    pair = (io1[i] << 1) | io0[i]      # IO1 the higher bit
                    val = (val << 2) | pair            # MSB pair first
                    count += 2
                    if count >= bits:
                        frames.append(Frame(time=clk_sig.time_at(word_start),
                                            index=clk_sig.first_index
                                            + word_start,
                                            end_index=clk_sig.first_index + i,
                                            value=val, status=OK, kind="word"))
                        words += 1
                        count = 0
                        val = 0
            prev_clk = c
            prev_cs_ok = ok

        note = "%d word(s) of %d bits, two lines, mode %s" % (words, bits,
                                                              mode)
        if not words:
            note += " - nothing clocked; check CLK/IO0/IO1 and CS"
        return frames, note

    def columns(self):
        return [("Time", 92), ("Hex", 60), ("ASCII", 60), ("Bits", 90)]

    def row(self, frame, ascii=True):
        v = frame.value
        ch = chr(v) if 32 <= v < 127 else "."
        return ["%02X" % (v & 0xFF), ch, format(v & 0xFF, "08b")]

    def label(self, frame, ascii=True):
        return "%02X" % (frame.value & 0xFF)
