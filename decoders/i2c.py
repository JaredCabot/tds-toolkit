"""I2C - two wires, SDA and SCL.

Both lines are captured and mapped, then walked together. The rules are the
protocol's own:

  * data is read on the rising edge of SCL - it is stable then, and only
    then;
  * a START is SDA falling while SCL is high, a STOP is SDA rising while SCL
    is high - the one time SDA is allowed to move while the clock is high,
    which is what makes those two the frame delimiters;
  * after a START the first byte is the 7-bit address and a read/write bit,
    every byte is followed by a ninth clock for the ACK (SDA low = ACK), and
    a repeated START begins a new address without a STOP.

No clock rate to detect: the clock is on the wire. Any SCL frequency the
capture resolves decodes, which is the whole reason a two-wire bus needs no
auto-baud.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import OK, ACK_ERROR


class I2cDecoder(Decoder):

    NAME = "I2C"
    DESCRIPTION = "Two wire - SDA + SCL"
    COLOUR = "#e0a030"                     # amber

    SOURCES = ["SDA", "SCL"]
    PARAMS = [
        Param("address", "Address", "choice",
              choices=[("7", "7-bit"), ("8", "8-bit (with R/W)")],
              default="7",
              note="How the address byte is shown in the table."),
    ]

    def decode(self, signals, params):
        sda_sig = signals["SDA"]
        scl_sig = signals["SCL"]
        if sda_sig.span < 8 or scl_sig.span < 8:
            return [], "no signal on SDA or SCL"
        sda = sda_sig.digitize()
        scl = scl_sig.digitize()
        n = min(len(sda), len(scl))
        seven = params.get("address", "7") == "7"

        frames = []
        in_frame = False
        first_byte = False
        bit_count = 0
        cur = 0
        byte_start = 0
        starts = stops = bytes_seen = 0

        prev_scl = scl[0]
        prev_sda = sda[0]
        for i in range(1, n):
            s = scl[i]
            d = sda[i]
            # START / STOP: SDA moves while SCL is high.
            if s and prev_scl:
                if prev_sda and not d:                    # SDA fell -> START
                    in_frame = True
                    first_byte = True
                    bit_count = 0
                    cur = 0
                    starts += 1
                    frames.append(Frame(time=scl_sig.time_at(i),
                                        index=scl_sig.first_index + i,
                                        value=0, kind="start"))
                elif not prev_sda and d:                  # SDA rose -> STOP
                    if in_frame:
                        stops += 1
                        frames.append(Frame(time=scl_sig.time_at(i),
                                            index=scl_sig.first_index + i,
                                            value=0, kind="stop"))
                    in_frame = False
                    bit_count = 0
            elif s and not prev_scl and in_frame:         # SCL rising: a bit
                if bit_count == 0:
                    byte_start = i
                if bit_count < 8:
                    cur = (cur << 1) | d
                    bit_count += 1
                    if bit_count == 8:
                        pass                              # ACK is next clock
                else:
                    # Ninth clock: the ACK bit. SDA low = acknowledged.
                    status = OK if d == 0 else ACK_ERROR
                    frames.append(_byte_frame(scl_sig, byte_start, i, cur,
                                              first_byte, seven, status))
                    bytes_seen += 1
                    first_byte = False
                    bit_count = 0
                    cur = 0
            prev_scl = s
            prev_sda = d

        note = "%d byte(s), %d START, %d STOP" % (bytes_seen, starts, stops)
        if not starts:
            note += " - no START seen; check SDA/SCL are not swapped"
        return frames, note

    def columns(self):
        return [("Time", 92), ("Field", 60), ("Hex", 46), ("ASCII", 46),
                ("Ack", 44)]

    def row(self, frame, ascii=True):
        if frame.kind == "start":
            return ["START", "", "", ""]
        if frame.kind == "stop":
            return ["STOP", "", "", ""]
        if frame.kind == "addr":
            field = "Addr %s" % ("R" if frame.tag else "W")
        else:
            field = "Data"
        ch = chr(frame.value) if 32 <= frame.value < 127 else "."
        ack = "NAK" if frame.status & ACK_ERROR else "ACK"
        return [field, "%02X" % (frame.value & 0xFF), ch, ack]

    def label(self, frame, ascii=True):
        if frame.kind == "start":
            return "S"
        if frame.kind == "stop":
            return "P"
        text = "%02X" % (frame.value & 0xFF)
        if frame.kind == "addr":
            text += "r" if frame.tag else "w"
        return ("!" + text) if frame.status & ACK_ERROR else text


def _byte_frame(scl_sig, start, end, byte, is_addr, seven, status):
    if is_addr:
        value = (byte >> 1) if seven else byte
        return Frame(time=scl_sig.time_at(start),
                     index=scl_sig.first_index + start,
                     end_index=scl_sig.first_index + end,
                     value=value, tag=byte & 1, status=status, kind="addr")
    return Frame(time=scl_sig.time_at(start),
                 index=scl_sig.first_index + start,
                 end_index=scl_sig.first_index + end,
                 value=byte, status=status, kind="data")
