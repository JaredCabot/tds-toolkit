"""1-Wire - one open-drain line, idle high, master and slave sharing it.

Everything is a low pulse the master starts by pulling the line down:

  * a low held past ~400 us is a reset; the short low that follows within
    a few tens of us is the slaves' presence answer;
  * every other low is one bit slot. The line is read a fixed time after the
    falling edge (about 15 us at standard speed) - high is a 1, still low is
    a 0 - which reads a master write and a slave read alike, because the
    scope sees the one wire both drive. Eight bits, least significant first,
    make a byte.

Overdrive runs the same shape about ten times faster; the one setting scales
the reset threshold and the sample point together.
"""

from tds_decode import Decoder, Frame, Param


class OneWireDecoder(Decoder):

    NAME = "1-Wire"
    DESCRIPTION = "Single open-drain line (DS18B20 etc.)"
    COLOUR = "#b09040"                     # ochre

    SOURCES = ["Line"]
    PARAMS = [
        Param("speed", "Speed", "choice",
              choices=[("standard", "Standard"), ("overdrive", "Overdrive")],
              default="standard"),
    ]

    def decode(self, signals, params):
        sig = signals["Line"]
        if sig.span < 8:
            return [], "no signal on this line"
        d = sig.digitize()
        n = len(d)
        dt = sig.dt
        scale = 0.1 if params.get("speed") == "overdrive" else 1.0
        reset_min = 400e-6 * scale             # a low longer than any bit slot
        presence_gap = 60e-6 * scale           # presence follows this soon
        sample_off = max(1, int(round(15e-6 * scale / dt)))

        def low_run(start):
            """End index (first high) of the low run beginning at `start`."""
            j = start
            while j < n and d[j] == 0:
                j += 1
            return j

        def next_fall(start):
            j = start
            while j < n and not (d[j] == 0 and d[j - 1] == 1):
                j += 1
            return j

        frames = []
        bytes_seen = resets = 0
        bits = []
        byte_start = 0
        i = 1
        while i < n:
            if not (d[i] == 0 and d[i - 1] == 1):
                i += 1
                continue
            fall = i
            end = low_run(fall)
            if (end - fall) * dt >= reset_min:
                presence = 0
                after = next_fall(end + 1)
                if after < n and (after - end) * dt < presence_gap:
                    end = low_run(after)       # swallow the presence pulse
                    presence = 1
                frames.append(Frame(time=sig.time_at(fall),
                                    index=sig.first_index + fall,
                                    end_index=sig.first_index + end,
                                    value=0, tag=presence, kind="reset"))
                resets += 1
                bits = []                      # a reset abandons any partial byte
                i = end
                continue
            # an ordinary bit slot: read the line a fixed time into it
            s = fall + sample_off
            bit = d[s] if s < n else 1
            if not bits:
                byte_start = fall
            bits.append(bit)
            if len(bits) == 8:
                value = 0
                for k, b in enumerate(bits):   # least significant bit first
                    value |= b << k
                frames.append(Frame(time=sig.time_at(byte_start),
                                    index=sig.first_index + byte_start,
                                    end_index=sig.first_index + end,
                                    value=value, kind="byte"))
                bytes_seen += 1
                bits = []
            i = end

        note = "%d reset(s), %d byte(s)%s" % (
            resets, bytes_seen, "  (overdrive)" if scale != 1.0 else "")
        return frames, note

    def columns(self):
        return [("Time", 92), ("Type", 64), ("Hex", 46), ("ASCII", 52)]

    def row(self, frame, ascii=True):
        if frame.kind == "reset":
            return ["Reset", "", "presence" if frame.tag else "no presence"]
        ch = chr(frame.value) if 32 <= frame.value < 127 else "."
        return ["Byte", "%02X" % (frame.value & 0xFF), ch]

    def label(self, frame, ascii=True):
        if frame.kind == "reset":
            return "RST"
        if ascii and 32 <= frame.value < 127:
            return chr(frame.value)
        return "%02X" % (frame.value & 0xFF)
