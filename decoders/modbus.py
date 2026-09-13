"""MODBUS RTU - the serial industrial bus, usually over RS-485.

The wire is plain async serial (8N1, sometimes 8E1), so the bytes come off
the shared serial core. RTU then frames them by silence: a gap of at least
3.5 character times ends one message and begins the next. Each message is
[address][function][data...][CRC-lo][CRC-hi], and the two-byte CRC-16 is
checked so a frame that did not survive the bus reads as bad rather than as
plausible data.

Point this at one line (or the differential MATH of an RS-485 pair) the same
way as RS-232. It decodes the byte stream; it does not need the master and
slave told apart.
"""

from tds_decode import Decoder, Frame
from tds_decode import Param
from tds_decode import serial_bytes, STANDARD_BAUDS

# The common function codes; an exception reply sets the high bit (0x80).
_FUNC = {
    1: "Read Coils", 2: "Read Discrete In", 3: "Read Holding Regs",
    4: "Read Input Regs", 5: "Write Coil", 6: "Write Register",
    15: "Write Coils", 16: "Write Registers", 17: "Report Server ID",
    22: "Mask Write Reg", 23: "Read/Write Regs",
}


def _crc16(data):
    """MODBUS CRC-16: init 0xFFFF, reflected poly 0xA001. Returns the value
    as it travels the wire - low byte first."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


class ModbusDecoder(Decoder):

    NAME = "MODBUS RTU"
    DESCRIPTION = "Serial industrial bus (RS-485)"
    COLOUR = "#c07030"                     # rust

    SOURCES = ["Line"]
    PARAMS = [
        Param("baud", "Bit rate", "choice",
              choices=[(0, "Auto")] + [(b, str(b)) for b in STANDARD_BAUDS],
              default=0, note="Auto finds the rate from the record."),
        Param("framing", "Framing", "choice",
              choices=[(f, f) for f in ("8N1", "8E1", "8O1", "8N2")],
              default="8N1"),
        Param("polarity", "Polarity", "choice",
              choices=[("ttl", "Idle high (logic / MATH)"),
                       ("rs232", "Idle low (inverted)")],
              default="ttl"),
    ]

    def decode(self, signals, params):
        sig = signals["Line"]
        framing = params.get("framing", "8N1")
        inverted = params.get("polarity") == "rs232"
        want = int(params.get("baud") or 0)
        bytes_, err, detected, bit_samples = serial_bytes(sig, want, framing,
                                                          inverted)
        if err:
            return [], err
        if not bytes_:
            return [], "no bytes on this line"
        # 3.5 character times of silence delimit a frame; a character is 11
        # bit times in RTU. Back-to-back bytes start ~1 char apart, a framed
        # gap ~4.5 - so a start-to-start jump past 2.5 chars is a new frame.
        char = bit_samples * 11.0
        split = 2.5 * char

        messages = []
        cur = [bytes_[0]]
        for prev, this in zip(bytes_, bytes_[1:]):
            if this.index - prev.index > split:
                messages.append(cur)
                cur = [this]
            else:
                cur.append(this)
        messages.append(cur)

        frames = []
        for msg in messages:
            frames.extend(self._frame_message(msg))
        bad = sum(1 for f in frames if f.kind == "crc" and f.status)
        note = "%d baud%s  %s  %d frame(s)  %d CRC error(s)" % (
            detected, " (auto)" if want == 0 else "", framing,
            len(messages), bad)
        return frames, note

    def _frame_message(self, msg):
        """One RTU message -> address, function, data and a checked CRC."""
        vals = [b.value & 0xFF for b in msg]
        out = []
        if len(msg) < 4:
            # Too short to hold address + function + CRC; show the raw bytes
            # rather than inventing fields that are not there.
            for b in msg:
                out.append(Frame(time=b.time, index=b.index,
                                 end_index=b.end_index, value=b.value & 0xFF,
                                 kind="data"))
            return out
        out.append(Frame(time=msg[0].time, index=msg[0].index,
                         end_index=msg[0].end_index, value=vals[0],
                         kind="addr"))
        out.append(Frame(time=msg[1].time, index=msg[1].index,
                         end_index=msg[1].end_index, value=vals[1],
                         kind="func"))
        for b in msg[2:-2]:
            out.append(Frame(time=b.time, index=b.index,
                             end_index=b.end_index, value=b.value & 0xFF,
                             kind="data"))
        got = vals[-2] | (vals[-1] << 8)
        calc = _crc16(vals[:-2])
        out.append(Frame(time=msg[-2].time, index=msg[-2].index,
                         end_index=msg[-1].end_index, value=got, tag=calc,
                         status=0 if got == calc else 1, kind="crc"))
        return out

    def columns(self):
        return [("Time", 92), ("Field", 54), ("Value", 54), ("Info", 150)]

    def row(self, frame, ascii=True):
        v = frame.value
        if frame.kind == "addr":
            return ["Addr", "%02X" % v,
                    "broadcast" if v == 0 else "slave %d" % v]
        if frame.kind == "func":
            name = _FUNC.get(v & 0x7F, "function %d" % (v & 0x7F))
            return ["Func", "%02X" % v,
                    name + (" (exception)" if v & 0x80 else "")]
        if frame.kind == "crc":
            return ["CRC", "%04X" % v,
                    "OK" if not frame.status else "BAD - calc %04X" % frame.tag]
        return ["Data", "%02X" % v, ""]

    def label(self, frame, ascii=True):
        if frame.kind == "addr":
            return "@%02X" % frame.value
        if frame.kind == "func":
            return "F%02X" % frame.value
        if frame.kind == "crc":
            return "CRC!" if frame.status else "CRC"
        return "%02X" % (frame.value & 0xFF)
