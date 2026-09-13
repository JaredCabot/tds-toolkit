"""MIDI - musical instrument serial, 31250 baud 8N1.

The wire is ordinary async serial at a fixed rate, so the bytes come off the
shared serial core; MIDI is what those bytes mean. A status byte (high bit
set) opens a message and says how many data bytes follow; a data byte with
no status of its own reuses the last one (running status). Real-time bytes
(0xF8 and up) are single bytes that may appear between the others.

One wire. Point it at the MIDI line (the receiver's logic side of the
opto-isolator); it idles high and each byte starts low, like any UART.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import serial_bytes

_NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_RT = {0xF8: "Clock", 0xFA: "Start", 0xFB: "Continue", 0xFC: "Stop",
       0xFE: "Active Sensing", 0xFF: "Reset", 0xF9: "Tick", 0xFD: "?"}
_SYS = {0xF1: "MTC Quarter", 0xF2: "Song Position", 0xF3: "Song Select",
        0xF6: "Tune Request"}


def _note_name(n):
    return "%s%d" % (_NOTES[n % 12], n // 12 - 1)


def _msg_len(status):
    """Total bytes in a channel/system-common message including the status
    byte. Program Change and Channel Pressure carry one data byte; the other
    channel messages carry two. SysEx (0xF0) is variable and handled apart."""
    if status < 0xF0:
        return 2 if (status & 0xF0) in (0xC0, 0xD0) else 3
    return {0xF1: 2, 0xF2: 3, 0xF3: 2}.get(status, 1)


class MidiDecoder(Decoder):

    NAME = "MIDI"
    DESCRIPTION = "Musical instrument serial, 31250"
    COLOUR = "#c060b0"                     # magenta

    SOURCES = ["Line"]
    PARAMS = []                            # rate and framing are fixed

    def decode(self, signals, params):
        sig = signals["Line"]
        bytes_, err, detected, bit_samples = serial_bytes(sig, 31250, "8N1",
                                                          False)
        if err:
            return [], err
        frames = self._parse(bytes_)
        note = "31250 baud  %.1f smp/bit  %d message(s)" % (bit_samples,
                                                            len(frames))
        return frames, note

    def _parse(self, bytes_):
        """Assemble the byte frames into MIDI messages, honouring running
        status and letting real-time bytes fall between them."""
        out = []
        n = len(bytes_)
        running = 0
        i = 0
        while i < n:
            b = bytes_[i].value & 0xFF
            start = bytes_[i]
            if b >= 0xF8:                      # real-time, one byte, anywhere
                out.append(Frame(time=start.time, index=start.index,
                                 end_index=start.end_index, value=b,
                                 kind="rt"))
                i += 1
                continue
            if b >= 0x80:                      # a status byte
                if b == 0xF0:                  # SysEx: run to 0xF7 (or a gap)
                    j = i + 1
                    while j < n and (bytes_[j].value & 0xFF) < 0x80:
                        j += 1
                    closed = j < n and (bytes_[j].value & 0xFF) == 0xF7
                    last = bytes_[j] if closed else bytes_[j - 1]
                    out.append(Frame(time=start.time, index=start.index,
                                     end_index=last.end_index, value=0xF0,
                                     length=(j - i) + (1 if closed else 0),
                                     kind="sysex"))
                    i = j + 1 if closed else j
                    continue
                running = b if b < 0xF0 else 0
                status = b
                i += 1
            else:                              # data byte -> running status
                if running == 0:
                    out.append(Frame(time=start.time, index=start.index,
                                     end_index=start.end_index, value=b,
                                     kind="orphan"))
                    i += 1
                    continue
                status = running
            data = []
            need = _msg_len(status) - 1
            while len(data) < need and i < n and (bytes_[i].value & 0xFF) < 0x80:
                data.append(bytes_[i].value & 0xFF)
                i += 1
            tag = 0
            for k, d in enumerate(data):
                tag |= (d & 0xFF) << (8 * k)
            kind = "sys" if status >= 0xF0 else "chan"
            out.append(Frame(time=start.time, index=start.index,
                             end_index=bytes_[i - 1].end_index,
                             value=status, tag=tag, length=1 + len(data),
                             kind=kind))
        return out

    _TYPE = {0x80: "Note Off", 0x90: "Note On", 0xA0: "Poly Aftertouch",
             0xB0: "Control Change", 0xC0: "Program Change",
             0xD0: "Ch Aftertouch", 0xE0: "Pitch Bend"}

    def columns(self):
        return [("Time", 92), ("Message", 122), ("Ch", 36), ("Detail", 150)]

    def _chan_detail(self, kind_hi, d1, d2):
        if kind_hi == 0x90 and d2 == 0:
            return "%s off (vel 0)" % _note_name(d1)
        if kind_hi in (0x80, 0x90):
            return "%s  vel %d" % (_note_name(d1), d2)
        if kind_hi == 0xA0:
            return "%s  press %d" % (_note_name(d1), d2)
        if kind_hi == 0xB0:
            return "controller %d = %d" % (d1, d2)
        if kind_hi == 0xC0:
            return "program %d" % d1
        if kind_hi == 0xD0:
            return "pressure %d" % d1
        if kind_hi == 0xE0:
            return "bend %d" % (d1 | (d2 << 7))
        return ""

    def row(self, frame, ascii=True):
        v = frame.value
        if frame.kind == "rt":
            return ["Real-time", "", _RT.get(v, "?")]
        if frame.kind == "sysex":
            return ["SysEx", "", "%d byte(s)" % frame.length]
        if frame.kind == "orphan":
            return ["(stray data)", "", "%02X - no status" % v]
        if frame.kind == "sys":
            return ["System", "", _SYS.get(v, "common %02X" % v)]
        hi = v & 0xF0
        d1 = frame.tag & 0xFF
        d2 = (frame.tag >> 8) & 0xFF
        return [self._TYPE.get(hi, "%02X" % v), str((v & 0x0F) + 1),
                self._chan_detail(hi, d1, d2)]

    def label(self, frame, ascii=True):
        v = frame.value
        if frame.kind == "rt":
            return {0xF8: "Clk", 0xFA: "Srt", 0xFB: "Con", 0xFC: "Stp",
                    0xFE: "Sns", 0xFF: "Rst"}.get(v, "RT")
        if frame.kind == "sysex":
            return "SysEx"
        if frame.kind == "orphan":
            return "%02X" % v
        if frame.kind == "sys":
            return "Sys"
        hi = v & 0xF0
        d1 = frame.tag & 0xFF
        if hi == 0x90:
            return ("Off %d" if ((frame.tag >> 8) & 0xFF) == 0
                    else "On %d") % d1
        if hi == 0x80:
            return "Off %d" % d1
        if hi == 0xB0:
            return "CC%d" % d1
        if hi == 0xC0:
            return "PC%d" % d1
        if hi == 0xE0:
            return "Bend"
        if hi == 0xA0:
            return "AT%d" % d1
        if hi == 0xD0:
            return "AT"
        return "%02X" % v
