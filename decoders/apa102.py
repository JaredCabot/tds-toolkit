"""APA102 / DotStar - two-wire addressable LEDs, clock and data.

Unlike the WS2812 there is a clock, so this is read the way SPI is: the data
line is sampled on the clock's rising edge, most-significant bit first, and
grouped into 32-bit words. Each word is one thing:

    start frame   32 zero bits
    LED           111bbbbb  BLUE  GREEN  RED   (bbbbb is 5-bit brightness)
    end frame     32 one bits (at least half as many as there are LEDs)

The three top bits of an LED word are always 1, which is what tells an LED
word from the all-zero start frame; the all-ones word is the end frame. Map
the clock and data lines; there is no rate to detect, the clock is on the
wire.
"""

from tds_decode import Decoder, Frame, Param


class Apa102Decoder(Decoder):

    NAME = "APA102 / DotStar"
    DESCRIPTION = "Two-wire addressable LEDs (clock + data)"
    COLOUR = "#c0a040"                     # gold

    SOURCES = ["CLK", "DATA"]
    PARAMS = [
        Param("edge", "Clock edge", "choice",
              choices=[("rising", "Sample on rising"),
                       ("falling", "Sample on falling")],
              default="rising"),
    ]

    def decode(self, signals, params):
        clk_sig = signals["CLK"]
        data_sig = signals.get("DATA")
        if clk_sig.span < 8:
            return [], "no signal on CLK"
        if data_sig is None or data_sig.span < 8:
            return [], "no signal on DATA"
        rising = params.get("edge", "rising") == "rising"
        clk = clk_sig.digitize()
        data = data_sig.digitize()
        n = min(len(clk), len(data))

        frames = []
        count = 0
        word = 0
        word_start = 0
        leds = 0
        idx = 0                                 # LED index within a run
        prev = clk[0]
        for i in range(1, n):
            c = clk[i]
            edge = (c and not prev) if rising else (prev and not c)
            if edge:
                if count == 0:
                    word_start = i
                word = ((word << 1) | data[i]) & 0xFFFFFFFF
                count += 1
                if count == 32:
                    kind, tag = _classify(word)
                    if kind == "start":
                        idx = 0
                    frames.append(Frame(time=clk_sig.time_at(word_start),
                                        index=clk_sig.first_index + word_start,
                                        end_index=clk_sig.first_index + i,
                                        value=word, tag=(idx if kind == "led"
                                                         else 0), kind=kind))
                    if kind == "led":
                        leds += 1
                        idx += 1
                    count = 0
                    word = 0
            prev = c

        note = "%d LED(s)" % leds
        if not leds:
            note += " - no LED word found; check CLK/DATA and the edge"
        return frames, note

    def columns(self):
        return [("Time", 92), ("Field", 52), ("Bright", 46), ("RGB", 120)]

    def row(self, frame, ascii=True):
        if frame.kind == "start":
            return ["Start", "", "start frame"]
        if frame.kind == "end":
            return ["End", "", "end frame"]
        w = frame.value
        bright = (w >> 24) & 0x1F
        b = (w >> 16) & 0xFF
        g = (w >> 8) & 0xFF
        r = w & 0xFF
        return ["LED %d" % frame.tag, "%d/31" % bright,
                "R%d G%d B%d" % (r, g, b)]

    def label(self, frame, ascii=True):
        if frame.kind == "start":
            return "ST"
        if frame.kind == "end":
            return "EN"
        return "#%d" % frame.tag

    def colour(self, frame):
        """Each LED's chevron in the colour it would light, scaled by its own
        5-bit brightness. Start and end frames keep the protocol's colour."""
        if frame.kind != "led":
            return self.COLOUR
        w = frame.value
        bright = (w >> 24) & 0x1F
        b = (w >> 16) & 0xFF
        g = (w >> 8) & 0xFF
        r = w & 0xFF
        sc = bright / 31.0
        return _visible(int(r * sc), int(g * sc), int(b * sc))


def _classify(word):
    if word == 0:
        return "start", 0
    if word == 0xFFFFFFFF:
        return "end", 0
    return "led", 0


def _visible(r, g, b):
    """`#rrggbb` floored so a dim or off pixel still draws a chevron on the
    dark graticule, its hue kept."""
    top = max(r, g, b)
    if top == 0:
        return "#404040"
    if top < 80:
        f = 80.0 / top
        r, g, b = int(r * f), int(g * f), int(b * f)
    return "#%02x%02x%02x" % (min(255, r), min(255, g), min(255, b))
