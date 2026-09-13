"""I3C - two wires, SDA and SCL, decoded in its I2C-compatible SDR framing.

I3C's single-data-rate traffic is shaped like I2C and read the same way: data
is sampled on the rising edge of SCL, a START is SDA falling while SCL is
high, a STOP is SDA rising while SCL is high, and the first byte after a START
is a 7-bit address with a read/write bit. Two things differ from I2C and the
decoder marks them:

  * the broadcast address 0x7E begins most I3C transactions (a CCC or dynamic
    addressing), and is named as such;
  * after the address byte the ninth bit is not an ACK but the T-bit that
    ends each data byte, so it is shown as T rather than judged as an
    acknowledge.

The faster HDR modes are a different line coding and are not decoded here;
point this at plain SDR traffic. No rate to detect - the clock is on the wire.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import OK, ACK_ERROR


class I3cDecoder(Decoder):

    NAME = "I3C"
    DESCRIPTION = "Two wire SDR - SDA + SCL"
    COLOUR = "#d07840"                     # terracotta

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
            if s and prev_scl:
                if prev_sda and not d:                    # START
                    in_frame = True
                    first_byte = True
                    bit_count = 0
                    cur = 0
                    starts += 1
                    frames.append(Frame(time=scl_sig.time_at(i),
                                        index=scl_sig.first_index + i,
                                        value=0, kind="start"))
                elif not prev_sda and d:                  # STOP
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
                else:
                    frames.append(_byte(scl_sig, byte_start, i, cur, d,
                                        first_byte, seven))
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
        return [("Time", 92), ("Field", 66), ("Hex", 46), ("ASCII", 40),
                ("9th", 40)]

    def row(self, frame, ascii=True):
        if frame.kind == "start":
            return ["START", "", "", ""]
        if frame.kind == "stop":
            return ["STOP", "", "", ""]
        v = frame.value
        if frame.kind == "addr":
            if v == 0x7E:
                field = "Broadcast 7E"
            else:
                field = "Addr %s" % ("R" if frame.tag else "W")
            ninth = "NAK" if frame.status & ACK_ERROR else "ACK"
        else:
            field = "Data"
            ninth = "T%d" % (frame.tag & 1)
        ch = chr(v) if 32 <= v < 127 else "."
        return [field, "%02X" % (v & 0xFF), ch, ninth]

    def label(self, frame, ascii=True):
        if frame.kind == "start":
            return "S"
        if frame.kind == "stop":
            return "P"
        if frame.kind == "addr" and frame.value == 0x7E:
            return "7E"
        text = "%02X" % (frame.value & 0xFF)
        if frame.kind == "addr":
            text += "r" if frame.tag else "w"
            if frame.status & ACK_ERROR:
                text = "!" + text
        return text


def _byte(scl_sig, start, end, byte, ninth, is_addr, seven):
    if is_addr:
        # 7-bit view drops the R/W bit; the broadcast byte 0xFC/0xFD then
        # shows as 0x7E. The R/W bit is kept in tag.
        value = (byte >> 1) if seven else byte
        status = OK if ninth == 0 else ACK_ERROR
        return Frame(time=scl_sig.time_at(start),
                     index=scl_sig.first_index + start,
                     end_index=scl_sig.first_index + end,
                     value=value, tag=byte & 1, status=status, kind="addr")
    # A data byte: the ninth bit is the T-bit, kept in tag, never an error.
    return Frame(time=scl_sig.time_at(start),
                 index=scl_sig.first_index + start,
                 end_index=scl_sig.first_index + end,
                 value=byte, tag=ninth, status=OK, kind="data")
