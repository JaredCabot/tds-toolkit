"""Asynchronous serial - RS-232, RS-422/485 and plain TTL UART.

The auto-baud and framed byte recovery live in tds_decode.serial_bytes
(the tested Java port: shortest recurring run for the rate, centre-of-bit
sampling, parity and stop-bit checks). This decoder is that engine's first
caller; RS-485, MODBUS and MIDI are the others.

One wire. The electrical sense is one setting: a TTL line idles high and a
character starts with a falling start bit; a true RS-232 line idles at its
negative level and starts rising, so its mark reads as "below threshold" -
that is the "RS-232 (inverted)" polarity. RS-485 is the difference of two
lines, which the instrument's own MATH can form; point this at that math and
it is a TTL decode again.
"""

from tds_decode import Decoder, Param
from tds_decode import OK, FRAMING_ERROR, PARITY_ERROR
from tds_decode import serial_bytes, STANDARD_BAUDS


class UartDecoder(Decoder):

    NAME = "RS-232 / UART"
    DESCRIPTION = "Async serial, one wire"
    COLOUR = "#39a0c0"                     # teal

    SOURCES = ["Line"]
    PARAMS = [
        Param("baud", "Bit rate", "choice",
              choices=[(0, "Auto")] + [(b, str(b)) for b in STANDARD_BAUDS],
              default=0, note="Auto finds the rate from the record."),
        Param("framing", "Framing", "choice",
              choices=[(f, f) for f in ("8N1", "8E1", "8O1", "8N2",
                                        "7N1", "7E1", "7O1", "7E2")],
              default="8N1"),
        Param("polarity", "Polarity", "choice",
              choices=[("ttl", "TTL / logic (idle high)"),
                       ("rs232", "RS-232 (inverted, idle low)")],
              default="ttl"),
    ]

    def decode(self, signals, params):
        sig = signals["Line"]
        framing = params.get("framing", "8N1")
        inverted = params.get("polarity") == "rs232"
        want = int(params.get("baud") or 0)
        frames, err, detected, bit_samples = serial_bytes(sig, want, framing,
                                                          inverted)
        if err:
            return [], err
        note = "%d baud%s  %s  %.1f smp/bit  %d frames" % (
            detected, " (auto)" if want == 0 else "", framing, bit_samples,
            len(frames))
        return frames, note

    def columns(self):
        return [("Time", 92), ("Hex", 46), ("ASCII", 52), ("Status", 90)]

    def row(self, frame, ascii=True):
        ch = chr(frame.value) if 32 <= frame.value < 127 else "."
        return ["%02X" % (frame.value & 0xFF), ch, _status_text(frame.status)]

    def label(self, frame, ascii=True):
        if ascii and 32 <= frame.value < 127:
            text = chr(frame.value)
        else:
            text = "%02X" % (frame.value & 0xFF)
        return ("!" + text) if frame.status else text


def _status_text(status):
    parts = []
    if status & FRAMING_ERROR:
        parts.append("FRAME")
    if status & PARITY_ERROR:
        parts.append("PARITY")
    return " ".join(parts) if parts else "OK"
