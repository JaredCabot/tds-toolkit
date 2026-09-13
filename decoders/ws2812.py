"""WS2812 / SK6812 - one-wire addressable LEDs (NeoPixel).

One data line, idle low, no clock. Every bit is a high pulse followed by a
low, both about 1.25 us together; what tells one from zero is how long the
high lasts - a long high is a 1, a short high is a 0. The parts vary in the
exact numbers, so the decoder measures each pulse against half its own bit
period rather than trusting a datasheet figure. A low held much longer than a
bit (about 50 us or more) is the reset that latches the colours and starts
the next frame.

Each LED takes its colour as bytes, most-significant bit first: three for an
RGB part (WS2812, order GRB on the wire) or four for an RGBW part (SK6812).
Pick the order to match the part; the row splits the value into named
channels.
"""

from tds_decode import Decoder, Frame, Param

_ORDER = {
    "grb": (["G", "R", "B"], "WS2812 (GRB)"),
    "rgb": (["R", "G", "B"], "RGB"),
    "grbw": (["G", "R", "B", "W"], "SK6812 RGBW"),
    "rgbw": (["R", "G", "B", "W"], "RGBW"),
}


class Ws2812Decoder(Decoder):

    NAME = "WS2812 / SK6812"
    DESCRIPTION = "Addressable LEDs (NeoPixel)"
    COLOUR = "#40c080"                     # spring green

    SOURCES = ["Line"]
    PARAMS = [
        Param("order", "Colour order", "choice",
              choices=[("grb", "WS2812 (GRB)"), ("rgb", "RGB"),
                       ("grbw", "SK6812 RGBW"), ("rgbw", "RGBW")],
              default="grb"),
    ]

    def decode(self, signals, params):
        sig = signals["Line"]
        if sig.span < 8:
            return [], "no signal on this line"
        order = params.get("order", "grb")
        self._names = _ORDER.get(order, _ORDER["grb"])[0]
        per = len(self._names)
        bits_each = per * 8

        d = sig.digitize()
        n = len(d)
        pulses = []                             # (rising, falling) per bit
        i = 0
        while i < n:
            if d[i] and (i == 0 or not d[i - 1]):
                r = i
                while i < n and d[i]:
                    i += 1
                pulses.append((r, i))
            else:
                i += 1
        if len(pulses) < bits_each:
            return [], "too few pulses for one LED - check the line"

        # Bit period is rise-to-rise; the median keeps the one long gap of a
        # reset from moving the threshold that tells a 1 from a 0.
        periods = sorted(pulses[k + 1][0] - pulses[k][0]
                         for k in range(len(pulses) - 1))
        period = periods[len(periods) // 2] or 1
        reset_gap = 4 * period

        frames = []
        pixels = resets = 0
        group = []
        group_start = pulses[0][0]
        for k, (r, f) in enumerate(pulses):
            th = f - r
            nxt = pulses[k + 1][0] if k + 1 < len(pulses) else None
            span = (nxt - r) if nxt is not None else period
            bit = 1 if th * 2 > span else 0
            if not group:
                group_start = r
            group.append(bit)
            if len(group) == bits_each:
                value = 0
                for gb in group:                # most-significant bit first
                    value = (value << 1) | gb
                frames.append(Frame(time=sig.time_at(group_start),
                                    index=sig.first_index + group_start,
                                    end_index=sig.first_index + f,
                                    value=value, tag=pixels, length=per,
                                    kind="pixel"))
                pixels += 1
                group = []
            if nxt is not None and (nxt - f) > reset_gap:
                group = []                      # a reset drops any part LED
                frames.append(Frame(time=sig.time_at(f),
                                    index=sig.first_index + f,
                                    end_index=sig.first_index + nxt,
                                    value=0, kind="reset"))
                resets += 1
                pixels = 0

        total = sum(1 for f in frames if f.kind == "pixel")
        note = "%d LED(s), %d reset(s), %s" % (
            total, resets, _ORDER.get(order, _ORDER["grb"])[1])
        return frames, note

    def columns(self):
        return [("Time", 92), ("LED", 44), ("Hex", 66), ("Channels", 120)]

    def _bytes(self, frame):
        per = frame.length or 3
        return [(frame.value >> (8 * (per - 1 - k))) & 0xFF
                for k in range(per)]

    def row(self, frame, ascii=True):
        if frame.kind == "reset":
            return ["Reset", "", "latch / next frame"]
        vals = self._bytes(frame)
        names = getattr(self, "_names", ["G", "R", "B", "W"])[:len(vals)]
        hexs = "".join("%02X" % v for v in vals)
        chans = " ".join("%s%d" % (nm, v) for nm, v in zip(names, vals))
        return ["#%d" % frame.tag, hexs, chans]

    def label(self, frame, ascii=True):
        if frame.kind == "reset":
            return "RST"
        return "#%d" % frame.tag

    def colour(self, frame):
        """Draw each LED's chevron in the colour that LED would light. The
        framework asks the decoder for every frame's colour, so this needs no
        app support - a strip of decoded pixels shows as the strip itself
        would look. A reset keeps the protocol's own colour."""
        if frame.kind != "pixel":
            return self.COLOUR
        vals = self._bytes(frame)
        names = getattr(self, "_names", ["G", "R", "B"])[:len(vals)]
        m = dict(zip(names, vals))
        w = m.get("W", 0)                       # white adds to every channel
        return _visible(min(255, m.get("R", 0) + w),
                        min(255, m.get("G", 0) + w),
                        min(255, m.get("B", 0) + w))


def _visible(r, g, b):
    """`#rrggbb` for an LED, floored so a dim or off pixel still draws a
    chevron on the dark graticule: an off LED is a neutral grey, and a very
    dim colour is scaled up to a readable brightness with its hue kept."""
    top = max(r, g, b)
    if top == 0:
        return "#404040"
    if top < 80:
        f = 80.0 / top
        r, g, b = int(r * f), int(g * f), int(b * f)
    return "#%02x%02x%02x" % (min(255, r), min(255, g), min(255, b))
