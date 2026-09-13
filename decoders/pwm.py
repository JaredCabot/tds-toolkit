"""PWM - not a bus but a measurement: the duty cycle and frequency of a
pulse-width-modulated line, one period at a time.

Handy for embedded work - a servo pulse, a motor drive, a dimmer, a DAC done
with a filter. Each period runs from one rising edge to the next (or falling,
if the active level is low); the high time over the period is the duty, and
the period's length is its frequency.

One wire. Nothing to frame and no rate to guess - the edges are the whole of
it.
"""

from tds_decode import Decoder, Frame, Param


def _hz(freq):
    for scale, suffix in ((1e6, "MHz"), (1e3, "kHz"), (1.0, "Hz")):
        if freq >= scale:
            return "%.3g %s" % (freq / scale, suffix)
    return "%.3g Hz" % freq


class PwmDecoder(Decoder):

    NAME = "PWM"
    DESCRIPTION = "Duty cycle and frequency"
    COLOUR = "#8fb03a"

    SOURCES = ["Line"]
    PARAMS = [
        Param("active", "Active level", "choice",
              choices=[("high", "High"), ("low", "Low")], default="high"),
    ]

    def decode(self, signals, params):
        sig = signals["Line"]
        if sig.span < 8:
            return [], "no signal on this line"
        d = sig.digitize()
        n = len(d)
        active_high = params.get("active", "high") == "high"
        lvl = d if active_high else bytes((1 - x) for x in d)

        # Period boundaries are the leading edges of the active level (a
        # rising edge when active-high). The active time within a period is
        # up to the trailing edge; the two give duty and frequency.
        rises = [i for i in range(1, n) if lvl[i] and not lvl[i - 1]]
        if len(rises) < 2:
            return [], "no complete period on this line"

        frames = []
        for a, b in zip(rises, rises[1:]):
            period = b - a
            if period <= 0:
                continue
            fall = next((j for j in range(a + 1, b) if not lvl[j]), b)
            high = fall - a
            duty = int(round(100.0 * high / period))
            freq = 1.0 / (period * sig.dt) if sig.dt else 0.0
            frames.append(Frame(time=sig.time_at(a),
                                index=sig.first_index + a,
                                end_index=sig.first_index + b,
                                value=duty, tag=int(round(freq)),
                                kind="period"))
        note = "%d period(s), active %s" % (
            len(frames), "high" if active_high else "low")
        return frames, note

    def columns(self):
        return [("Time", 92), ("Duty", 60), ("Frequency", 90)]

    def row(self, frame, ascii=True):
        return ["%d%%" % frame.value, _hz(float(frame.tag))]

    def label(self, frame, ascii=True):
        return "%d%%" % frame.value
