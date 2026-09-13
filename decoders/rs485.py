"""RS-422 / RS-485 - differential async serial.

Electrically these are RS-232's balanced cousins: two wires carrying the
same UART bytes as the voltage between them rather than to ground. A scope
sees the bits either on one line single-ended or, better, on the instrument's
own MATH of the pair (A-B) - and either way the decode is the same async
serial as RS-232. So this is the shared serial core again, named for the bus
the user is probing and defaulting to the idle-high sense a MATH difference
or a receiver's logic output shows.

Point it at the differential MATH for the cleanest read; on a single line,
set the polarity to match which way that line idles.
"""

from tds_decode import Decoder, Param
from tds_decode import FRAMING_ERROR, PARITY_ERROR
from tds_decode import serial_bytes, STANDARD_BAUDS


class Rs485Decoder(Decoder):

    NAME = "RS-422 / RS-485"
    DESCRIPTION = "Differential async serial"
    COLOUR = "#7a86d0"                     # indigo

    SOURCES = ["A-B / line"]
    PARAMS = [
        Param("baud", "Bit rate", "choice",
              choices=[(0, "Auto")] + [(b, str(b)) for b in STANDARD_BAUDS],
              default=0, note="Auto finds the rate from the record."),
        Param("framing", "Framing", "choice",
              choices=[(f, f) for f in ("8N1", "8E1", "8O1", "8N2",
                                        "7N1", "7E1", "7O1", "7E2")],
              default="8N1"),
        Param("polarity", "Polarity", "choice",
              choices=[("ttl", "Idle high (MATH A-B, logic)"),
                       ("rs232", "Idle low (inverted)")],
              default="ttl"),
    ]

    def decode(self, signals, params):
        sig = signals["A-B / line"]
        framing = params.get("framing", "8N1")
        inverted = params.get("polarity") == "rs232"
        want = int(params.get("baud") or 0)
        frames, err, detected, bit_samples = serial_bytes(sig, want, framing,
                                                          inverted)
        if err:
            return [], err
        note = "%d baud%s  %s  %.1f smp/bit  %d frames  (differential)" % (
            detected, " (auto)" if want == 0 else "", framing, bit_samples,
            len(frames))
        return frames, note

    def columns(self):
        return [("Time", 92), ("Hex", 46), ("ASCII", 52), ("Status", 90)]

    def row(self, frame, ascii=True):
        ch = chr(frame.value) if 32 <= frame.value < 127 else "."
        parts = []
        if frame.status & FRAMING_ERROR:
            parts.append("FRAME")
        if frame.status & PARITY_ERROR:
            parts.append("PARITY")
        return ["%02X" % (frame.value & 0xFF), ch,
                " ".join(parts) if parts else "OK"]

    def label(self, frame, ascii=True):
        if ascii and 32 <= frame.value < 127:
            text = chr(frame.value)
        else:
            text = "%02X" % (frame.value & 0xFF)
        return ("!" + text) if frame.status else text
