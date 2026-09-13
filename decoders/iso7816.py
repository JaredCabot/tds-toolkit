"""ISO 7816 - smart card, the single I/O line, T=0 characters.

The card and reader share one half-duplex I/O line that carries asynchronous
characters: a start bit, eight data bits, an even parity bit, then a couple of
bit times of guard, which is 8E2 to the shared serial core. Two conventions
exist and the card announces which in its first byte (the TS character):

  * direct - a one is a high level and the data bits go least-significant
    first, which reads straight off the serial core;
  * inverse - a one is a low level and the data bits go most-significant
    first, so the line is read inverted and each byte's bits are reversed.

Pick the convention (0x3B in the first byte is direct, 0x3F is inverse). The
bit rate is the card clock divided by its rate factor; leave it on Auto to
read it from the record, or pick it if you know it. Map the I/O line.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import serial_bytes, _status_text

_RATES = [9600, 19200, 38400, 57600, 115200]


def _rev8(b):
    r = 0
    for _ in range(8):
        r = (r << 1) | (b & 1)
        b >>= 1
    return r


class Iso7816Decoder(Decoder):

    NAME = "ISO 7816"
    DESCRIPTION = "Smart card I/O (T=0)"
    COLOUR = "#8060c0"                     # violet

    SOURCES = ["I/O"]
    PARAMS = [
        Param("baud", "Bit rate", "choice",
              choices=[(0, "Auto")] + [(b, str(b)) for b in _RATES],
              default=0, note="Card clock / rate factor; Auto reads it."),
        Param("convention", "Convention", "choice",
              choices=[("direct", "Direct (TS 0x3B)"),
                       ("inverse", "Inverse (TS 0x3F)")],
              default="direct"),
    ]

    def decode(self, signals, params):
        sig = signals["I/O"]
        if sig.span < 8:
            return [], "no signal on the I/O line"
        want = int(params.get("baud") or 0)
        inverse = params.get("convention", "direct") == "inverse"
        raw, err, detected, bit_samples = serial_bytes(sig, want, "8E2",
                                                       inverse)
        if err:
            return [], err
        frames = []
        parity_errs = 0
        for b in raw:
            value = _rev8(b.value & 0xFF) if inverse else (b.value & 0xFF)
            frames.append(Frame(time=b.time, index=b.index,
                                end_index=b.end_index, value=value,
                                status=b.status, kind="byte"))
            if b.status:
                parity_errs += 1
        note = "%d baud%s  8E2 %s  %d byte(s)  %d parity error(s)" % (
            detected, " (auto)" if want == 0 else "",
            "inverse" if inverse else "direct", len(frames), parity_errs)
        return frames, note

    def columns(self):
        return [("Time", 92), ("Hex", 60), ("ASCII", 60), ("Status", 80)]

    def row(self, frame, ascii=True):
        ch = chr(frame.value) if 32 <= frame.value < 127 else "."
        return ["%02X" % (frame.value & 0xFF), ch, _status_text(frame.status)]

    def label(self, frame, ascii=True):
        text = "%02X" % (frame.value & 0xFF)
        return ("!" + text) if frame.status else text
