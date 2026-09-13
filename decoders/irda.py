"""IrDA SIR - infra-red serial, one line.

SIR is a UART underneath, but the wire does not carry the UART levels: each
zero bit is sent as a short infra-red pulse about 3/16 of a bit long at the
start of its bit time, and a one bit is no pulse at all. The line therefore
idles low and shows a brief high pulse wherever the underlying UART had a
zero (including every start bit).

The decoder finds the pulses, works out the bit time from how they are
spaced (or from the rate you pick), and rebuilds the full-width UART line a
zero for any bit whose window holds a pulse, a one for any that does not. That
rebuilt line then goes through the shared serial core as ordinary 8N1, so the
bytes and their framing come out the same way RS-232 does.

Point it at a photodiode or the SIR encoder's output. If nothing decodes, the
rate guess may be off - pick the SIR rate you expect.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import serial_bytes

_MARK, _SPACE = 80, -80                    # rebuilt UART: idle-high mark
_RATES = [2400, 9600, 19200, 38400, 57600, 115200]


class IrdaDecoder(Decoder):

    NAME = "IrDA SIR"
    DESCRIPTION = "Infra-red serial (3/16 pulse)"
    COLOUR = "#d05090"                     # pink

    SOURCES = ["Line"]
    PARAMS = [
        Param("baud", "Bit rate", "choice",
              choices=[(0, "Auto")] + [(b, str(b)) for b in _RATES],
              default=0, note="Auto reads the rate from the pulse spacing."),
    ]

    def decode(self, signals, params):
        sig = signals["Line"]
        if sig.span < 8:
            return [], "no signal on this line"
        want = int(params.get("baud") or 0)
        d = sig.digitize()
        n = len(d)
        rises = [i for i in range(1, n) if d[i] and not d[i - 1]]
        if len(rises) < 2:
            return [], "too few IrDA pulses to read a rate"

        if want > 0:
            bit_samples = (1.0 / want) / sig.dt
        else:
            # Adjacent zero bits put pulses one bit apart, so the shortest
            # rise-to-rise spacing is one bit time.
            bit_samples = float(min(rises[k + 1] - rises[k]
                                    for k in range(len(rises) - 1)))
        if bit_samples < 3:
            return [], "too few samples per bit - slow the timebase"

        # Rebuild the UART line: mark (high) everywhere, then paint the bit
        # window of every pulse as a space (low).
        recon = [_MARK] * n
        phase = rises[0]
        for r in rises:
            w = int(round((r - phase) / bit_samples))
            lo = phase + int(round(w * bit_samples))
            hi = phase + int(round((w + 1) * bit_samples))
            for j in range(max(0, lo), min(n, hi)):
                recon[j] = _SPACE

        from tds_decode import Signal
        rebuilt = Signal(recon, sig.dt, time_of_first=sig.t0,
                         first_index=sig.first_index)
        rebuilt.auto_threshold()
        frames, err, detected, bs = serial_bytes(rebuilt, want, "8N1", False)
        if err:
            return [], err
        note = "%d baud%s  8N1  %d byte(s)  (SIR)" % (
            detected, " (auto)" if want == 0 else "", len(frames))
        return frames, note

    def columns(self):
        return [("Time", 92), ("Hex", 60), ("ASCII", 60), ("Status", 80)]

    def row(self, frame, ascii=True):
        from tds_decode import _status_text
        ch = chr(frame.value) if 32 <= frame.value < 127 else "."
        return ["%02X" % (frame.value & 0xFF), ch, _status_text(frame.status)]

    def label(self, frame, ascii=True):
        if ascii and 32 <= frame.value < 127:
            text = chr(frame.value)
        else:
            text = "%02X" % (frame.value & 0xFF)
        return ("!" + text) if frame.status else text
