"""IR remote control - the demodulated receiver output, one line.

This decodes the output of an IR receiver module (the carrier already stripped
off), not the 38 kHz carrier itself. That output idles high and pulls low
while a burst is present, so a "mark" is a low here; set the polarity the
other way for a raw active-high demodulation. Four common protocols, chosen
rather than guessed because their timings overlap:

  * NEC - a 9 ms leader, then 32 bits by gap length (a long gap is a one),
    carrying address, ~address, command, ~command; the complements are
    checked. A 9 ms leader with a short gap is a repeat.
  * Sony SIRC - a 2.4 ms leader, then bits by burst length (a long burst is
    a one), 12, 15 or 20 of them, command then address.
  * Philips RC5 - 14 Manchester bits at 1.78 ms: two starts, a toggle, five
    address, six command.
  * RC6 - a leader then Manchester bits with a double-width toggle; mode 0
    (8-bit address and command) is read.

Map the receiver's output line and pick the protocol.
"""

from tds_decode import Decoder, Frame, Param

_US = 1e-6


class IrDecoder(Decoder):

    NAME = "IR remote"
    DESCRIPTION = "NEC / Sony / RC5-6 / Samsung / JVC / Panasonic + more"
    COLOUR = "#d0a020"                     # amber-gold

    SOURCES = ["IR"]
    PARAMS = [
        Param("protocol", "Protocol", "choice",
              choices=[("nec", "NEC"), ("necext", "Extended NEC"),
                       ("sirc", "Sony SIRC"),
                       ("rc5", "Philips RC5"), ("rc5x", "Philips RC5X"),
                       ("rc6", "RC6"), ("rcmm", "RC-MM (Nokia)"),
                       ("samsung", "Samsung"), ("lg", "LG (28-bit)"),
                       ("jvc", "JVC"),
                       ("kaseikyo", "Panasonic / Kaseikyo"),
                       ("denon", "Denon / Sharp"),
                       ("mitsubishi", "Mitsubishi")],
              default="nec"),
        Param("polarity", "Idle level", "choice",
              choices=[("high", "Idle high (receiver output)"),
                       ("low", "Idle low (raw / inverted)")],
              default="high"),
    ]

    def decode(self, signals, params):
        sig = signals["IR"]
        if sig.span < 8:
            return [], "no signal on this line"
        d = sig.digitize()
        n = len(d)
        idle_high = params.get("polarity", "high") == "high"
        # A mark is carrier present: low when the line idles high.
        mark = [1 if ((v == 0) if idle_high else (v == 1)) else 0 for v in d]
        # Runs of equal level: (is_mark, start, length_seconds).
        runs = []
        i = 0
        while i < n:
            j = i
            while j < n and mark[j] == mark[i]:
                j += 1
            runs.append((mark[i], i, (j - i) * sig.dt))
            i = j

        proto = params.get("protocol", "nec")
        fn = {"nec": self._nec, "necext": self._necext, "sirc": self._sirc,
              "rc5": self._rc5, "rc5x": self._rc5x, "rc6": self._rc6,
              "rcmm": self._rcmm, "samsung": self._samsung, "lg": self._lg,
              "jvc": self._jvc, "kaseikyo": self._kaseikyo,
              "denon": self._denon,
              "mitsubishi": self._mitsubishi}.get(proto, self._nec)
        return fn(sig, runs)

    # -- NEC -------------------------------------------------------------
    def _nec(self, sig, runs):
        frames = []
        msgs = reps = 0
        k = 0
        while k + 1 < len(runs):
            m, start, dur = runs[k]
            nxt = runs[k + 1]
            if m and _near(dur, 9000 * _US, 0.3):
                if nxt[0] == 0 and _near(nxt[2], 2250 * _US, 0.3):
                    frames.append(Frame(time=sig.time_at(start),
                                        index=sig.first_index + start,
                                        end_index=sig.first_index + nxt[1],
                                        value=0, kind="repeat"))
                    reps += 1
                    k += 2
                    continue
                if nxt[0] == 0 and _near(nxt[2], 4500 * _US, 0.3):
                    bits, end, kk = _nec_bits(runs, k + 2)
                    if len(bits) >= 32:
                        addr = _lsb(bits[0:8])
                        naddr = _lsb(bits[8:16])
                        cmd = _lsb(bits[16:24])
                        ncmd = _lsb(bits[24:32])
                        good = (addr ^ naddr) == 0xFF and (cmd ^ ncmd) == 0xFF
                        st = 0 if good else 1
                        frames.append(Frame(time=sig.time_at(start),
                                            index=sig.first_index + start,
                                            end_index=sig.first_index + end,
                                            value=cmd, tag=addr, status=st,
                                            kind="nec"))
                        msgs += 1
                        k = kk
                        continue
            k += 1
        note = "NEC: %d message(s), %d repeat(s)" % (msgs, reps)
        if not msgs and not reps:
            note += " - no NEC leader found; check protocol and polarity"
        return frames, note

    # -- Sony SIRC -------------------------------------------------------
    def _sirc(self, sig, runs):
        frames = []
        msgs = 0
        k = 0
        while k < len(runs):
            m, start, dur = runs[k]
            if m and _near(dur, 2400 * _US, 0.3):
                bits = []
                end = runs[k][1]
                j = k + 1
                while j + 1 < len(runs) and runs[j][0] == 0:
                    burst = runs[j + 1]
                    if burst[0] != 1:
                        break
                    bit = 1 if burst[2] > 900 * _US else 0
                    bits.append(bit)
                    end = burst[1] + int(burst[2] / sig.dt)
                    j += 2
                    if len(bits) >= 20:
                        break
                if len(bits) >= 12:
                    cmd = _lsb(bits[0:7])
                    addr = _lsb(bits[7:len(bits)])
                    frames.append(Frame(time=sig.time_at(start),
                                        index=sig.first_index + start,
                                        end_index=sig.first_index + end,
                                        value=cmd, tag=addr, length=len(bits),
                                        kind="sirc"))
                    msgs += 1
                    k = j
                    continue
            k += 1
        note = "Sony SIRC: %d message(s)" % msgs
        if not msgs:
            note += " - no SIRC leader found; check protocol and polarity"
        return frames, note

    # -- RC5 / RC6 (Manchester) -----------------------------------------
    def _rc5(self, sig, runs):
        # The frame idles in space and its start bit opens with a space half,
        # so the two merge: trim to the first mark and prepend that leading
        # space half to line the half-bits up. 14 bits = 28 half-bits.
        levels, start, end = _half_levels(runs, 889 * _US, sig, lead_space=True)
        bits = _pair_bits(levels[:28])
        frames = []
        if len(bits) >= 14:
            toggle = bits[2]                    # bit0,1 start; 2 toggle
            addr = _msb(bits[3:8])              # 3..7 address
            cmd = _msb(bits[8:14])              # 8..13 command
            frames.append(Frame(time=sig.time_at(start),
                                index=sig.first_index + start,
                                end_index=sig.first_index + end,
                                value=cmd, tag=addr, length=toggle,
                                kind="rc5"))
        note = "RC5: %d message(s)" % len(frames)
        if not frames:
            note += " - no RC5 frame found; check protocol and polarity"
        return frames, note

    def _rc6(self, sig, runs):
        # Mode 0. The unit is 444 us (a half-bit), the leader a 2.666 ms mark
        # and a 0.889 ms space. RC6's convention is the opposite of RC5 - a
        # one is mark-then-space - and its toggle bit is double width, so it
        # is read off the half-bit levels by hand rather than paired blindly.
        k = 0
        while k + 1 < len(runs):
            if runs[k][0] and _near(runs[k][2], 2666 * _US, 0.3):
                break
            k += 1
        levels, start, end = _half_levels(runs[k + 2:], 444 * _US, sig)
        frames = []
        # start(2) + mode(6) + toggle(4) + 16 bits(32) = 44 half-bits.
        if len(levels) >= 44:
            mode = _msb(_pair_bits(levels[2:8], ones_first=True))
            toggle = 1 if (levels[8] == 1 and levels[10] == 0) else 0
            data = _pair_bits(levels[12:44], ones_first=True)
            addr = _msb(data[0:8])
            cmd = _msb(data[8:16])
            frames.append(Frame(time=sig.time_at(start),
                                index=sig.first_index + start,
                                end_index=sig.first_index + end,
                                value=cmd, tag=addr, length=(mode << 1) | toggle,
                                kind="rc6"))
        note = "RC6: %d message(s)" % len(frames)
        if not frames:
            note += " - no RC6 frame found; check protocol and polarity"
        return frames, note

    # -- Extended NEC ----------------------------------------------------
    def _necext(self, sig, runs):
        # NEC framing, but a full 16-bit address (no complement) and the
        # 8-bit command checked against its complement.
        frames = []
        msgs = reps = 0
        k = 0
        while k + 1 < len(runs):
            m, start, dur = runs[k]
            nxt = runs[k + 1]
            if m and _near(dur, 9000 * _US, 0.3):
                if nxt[0] == 0 and _near(nxt[2], 2250 * _US, 0.3):
                    frames.append(Frame(time=sig.time_at(start),
                                        index=sig.first_index + start,
                                        end_index=sig.first_index + nxt[1],
                                        value=0, kind="repeat"))
                    reps += 1
                    k += 2
                    continue
                if nxt[0] == 0 and _near(nxt[2], 4500 * _US, 0.3):
                    bits, end, kk = _pd_bits(runs, k + 2, 32, 1000 * _US)
                    if len(bits) >= 32:
                        addr = _lsb(bits[0:16])
                        cmd = _lsb(bits[16:24])
                        ncmd = _lsb(bits[24:32])
                        st = 0 if (cmd ^ ncmd) == 0xFF else 1
                        frames.append(Frame(time=sig.time_at(start),
                                            index=sig.first_index + start,
                                            end_index=sig.first_index + end,
                                            value=cmd, tag=addr, status=st,
                                            kind="necext"))
                        msgs += 1
                        k = kk
                        continue
            k += 1
        note = "Extended NEC: %d message(s), %d repeat(s)" % (msgs, reps)
        if not msgs and not reps:
            note += " - no NEC leader found; check protocol and polarity"
        return frames, note

    # -- Samsung ---------------------------------------------------------
    def _samsung(self, sig, runs):
        # A 4.5 ms leader mark and 4.5 ms space, then 32 pulse-distance bits:
        # address, address again, command, then the command's complement.
        frames = []
        msgs = 0
        k = 0
        while k + 1 < len(runs):
            m, start, dur = runs[k]
            nxt = runs[k + 1]
            if (m and _near(dur, 4500 * _US, 0.25)
                    and nxt[0] == 0 and _near(nxt[2], 4500 * _US, 0.25)):
                bits, end, kk = _pd_bits(runs, k + 2, 32, 1000 * _US)
                if len(bits) >= 32:
                    addr = _lsb(bits[0:8])
                    cmd = _lsb(bits[16:24])
                    ncmd = _lsb(bits[24:32])
                    st = 0 if (cmd ^ ncmd) == 0xFF else 1
                    frames.append(Frame(time=sig.time_at(start),
                                        index=sig.first_index + start,
                                        end_index=sig.first_index + end,
                                        value=cmd, tag=addr, status=st,
                                        kind="samsung"))
                    msgs += 1
                    k = kk
                    continue
            k += 1
        note = "Samsung: %d message(s)" % msgs
        if not msgs:
            note += " - no Samsung leader found; check protocol and polarity"
        return frames, note

    # -- LG (28-bit) -----------------------------------------------------
    def _lg(self, sig, runs):
        # NEC-style leader, then 28 bits MSB first: 8-bit address, 16-bit
        # command, 4-bit checksum (the sum of the command's four nibbles).
        frames = []
        msgs = 0
        k = 0
        while k + 1 < len(runs):
            m, start, dur = runs[k]
            nxt = runs[k + 1]
            if (m and _near(dur, 9000 * _US, 0.3)
                    and nxt[0] == 0 and _near(nxt[2], 4500 * _US, 0.3)):
                bits, end, kk = _pd_bits(runs, k + 2, 28, 1000 * _US)
                if len(bits) >= 28:
                    addr = _msb(bits[0:8])
                    cmd = _msb(bits[8:24])
                    chk = _msb(bits[24:28])
                    want = ((cmd >> 12) + ((cmd >> 8) & 0xF)
                            + ((cmd >> 4) & 0xF) + (cmd & 0xF)) & 0xF
                    st = 0 if chk == want else 1
                    frames.append(Frame(time=sig.time_at(start),
                                        index=sig.first_index + start,
                                        end_index=sig.first_index + end,
                                        value=cmd, tag=addr, status=st,
                                        kind="lg"))
                    msgs += 1
                    k = kk
                    continue
            k += 1
        note = "LG: %d message(s)" % msgs
        if not msgs:
            note += " - no LG leader found; check protocol and polarity"
        return frames, note

    # -- JVC -------------------------------------------------------------
    def _jvc(self, sig, runs):
        # An 8.4 ms leader mark and 4.2 ms space, then 16 pulse-distance
        # bits: address then command, no complement. Repeats resend the
        # frame with no leader; only leadered frames are read here.
        frames = []
        msgs = 0
        k = 0
        while k + 1 < len(runs):
            m, start, dur = runs[k]
            nxt = runs[k + 1]
            if (m and _near(dur, 8400 * _US, 0.3)
                    and nxt[0] == 0 and _near(nxt[2], 4200 * _US, 0.3)):
                bits, end, kk = _pd_bits(runs, k + 2, 16, 1050 * _US)
                if len(bits) >= 16:
                    addr = _lsb(bits[0:8])
                    cmd = _lsb(bits[8:16])
                    frames.append(Frame(time=sig.time_at(start),
                                        index=sig.first_index + start,
                                        end_index=sig.first_index + end,
                                        value=cmd, tag=addr, kind="jvc"))
                    msgs += 1
                    k = kk
                    continue
            k += 1
        note = "JVC: %d message(s)" % msgs
        if not msgs:
            note += " - no JVC leader found; check protocol and polarity"
        return frames, note

    # -- Panasonic / Kaseikyo -------------------------------------------
    def _kaseikyo(self, sig, runs):
        # A 3.5 ms leader mark and 1.75 ms space, then 48 pulse-distance
        # bits: six bytes, LSB first. The first two are the vendor (0x2002 is
        # Panasonic); the last is the XOR of bytes 3, 4 and 5 (the checksum).
        frames = []
        msgs = 0
        k = 0
        while k + 1 < len(runs):
            m, start, dur = runs[k]
            nxt = runs[k + 1]
            if (m and _near(dur, 3500 * _US, 0.3)
                    and nxt[0] == 0 and _near(nxt[2], 1750 * _US, 0.3)):
                bits, end, kk = _pd_bits(runs, k + 2, 48, 850 * _US)
                if len(bits) >= 48:
                    b = [_lsb(bits[8 * i:8 * i + 8]) for i in range(6)]
                    st = 0 if b[5] == (b[2] ^ b[3] ^ b[4]) else 1
                    vendor = b[0] | (b[1] << 8)
                    frames.append(Frame(time=sig.time_at(start),
                                        index=sig.first_index + start,
                                        end_index=sig.first_index + end,
                                        value=b[4], tag=b[3], length=vendor,
                                        status=st, kind="kaseikyo"))
                    msgs += 1
                    k = kk
                    continue
            k += 1
        note = "Panasonic/Kaseikyo: %d message(s)" % msgs
        if not msgs:
            note += " - no Kaseikyo leader found; check protocol and polarity"
        return frames, note

    # -- Denon / Sharp ---------------------------------------------------
    def _denon(self, sig, runs):
        # No leader: 15 pulse-distance bits at a ~264 us mark, MSB first -
        # a 5-bit address, an 8-bit command and two frame bits. The command
        # is sent again inverted a frame later; a single frame is read here.
        frames = []
        msgs = 0
        k = 0
        while k + 1 < len(runs):
            m, start, dur = runs[k]
            if m and _near(dur, 264 * _US, 0.5):
                bits, end, kk = _pd_bits(runs, k, 15, 1300 * _US)
                if len(bits) >= 13:
                    addr = _msb(bits[0:5])
                    cmd = _msb(bits[5:13])
                    frames.append(Frame(time=sig.time_at(start),
                                        index=sig.first_index + start,
                                        end_index=sig.first_index + end,
                                        value=cmd, tag=addr, kind="denon"))
                    msgs += 1
                    k = kk
                    continue
            k += 1
        note = "Denon/Sharp: %d message(s)" % msgs
        if not msgs:
            note += " - no Denon frame found; check protocol and polarity"
        return frames, note

    # -- Mitsubishi ------------------------------------------------------
    def _mitsubishi(self, sig, runs):
        # IRP {32.6k,300}<1,-3|1,-7>(D:8,F:8): no leader, 16 pulse-distance
        # bits LSB first at a 300 us mark, device then function.
        frames = []
        msgs = 0
        k = 0
        while k + 1 < len(runs):
            m, start, dur = runs[k]
            if m and _near(dur, 300 * _US, 0.5):
                bits, end, kk = _pd_bits(runs, k, 16, 1500 * _US)
                if len(bits) >= 16:
                    dev = _lsb(bits[0:8])
                    fn = _lsb(bits[8:16])
                    frames.append(Frame(time=sig.time_at(start),
                                        index=sig.first_index + start,
                                        end_index=sig.first_index + end,
                                        value=fn, tag=dev, kind="mitsubishi"))
                    msgs += 1
                    k = kk
                    continue
            k += 1
        note = "Mitsubishi: %d message(s)" % msgs
        if not msgs:
            note += " - no Mitsubishi frame found; check protocol and polarity"
        return frames, note

    # -- Philips RC5X ----------------------------------------------------
    def _rc5x(self, sig, runs):
        # RC5 with a 7-bit command: the second start bit carries the inverted
        # top command bit, so it selects commands 0-127 rather than 0-63.
        # Everything else is exactly RC5.
        levels, start, end = _half_levels(runs, 889 * _US, sig, lead_space=True)
        bits = _pair_bits(levels[:28])
        frames = []
        if len(bits) >= 14:
            toggle = bits[2]
            addr = _msb(bits[3:8])
            cmd = (0 if bits[1] else 64) | _msb(bits[8:14])
            frames.append(Frame(time=sig.time_at(start),
                                index=sig.first_index + start,
                                end_index=sig.first_index + end,
                                value=cmd, tag=addr, length=toggle,
                                kind="rc5x"))
        note = "RC5X: %d message(s)" % len(frames)
        if not frames:
            note += " - no RC5X frame found; check protocol and polarity"
        return frames, note

    # -- RC-MM (Nokia) ---------------------------------------------------
    def _rcmm(self, sig, runs):
        # Pulse-distance-modulation: a constant ~166 us mark and a space of
        # one of four lengths carrying two bits (277/444/611/778 us = 0..3).
        # A 416 us + 277 us leader, then 12 symbols = 24 bits.
        frames = []
        msgs = 0
        k = 0
        while k + 1 < len(runs):
            m, start, dur = runs[k]
            nxt = runs[k + 1]
            if (m and _near(dur, 416 * _US, 0.35)
                    and nxt[0] == 0 and _near(nxt[2], 277 * _US, 0.4)):
                val, end, kk = _rcmm_bits(runs, k + 2, 12)
                if val is not None:
                    addr = (val >> 16) & 0xFF
                    cmd = val & 0xFFFF
                    frames.append(Frame(time=sig.time_at(start),
                                        index=sig.first_index + start,
                                        end_index=sig.first_index + end,
                                        value=cmd, tag=addr, kind="rcmm"))
                    msgs += 1
                    k = kk
                    continue
            k += 1
        note = "RC-MM: %d message(s)" % msgs
        if not msgs:
            note += " - no RC-MM leader found; check protocol and polarity"
        return frames, note

    # -- rendering -------------------------------------------------------
    def columns(self):
        return [("Time", 92), ("Field", 96), ("Addr", 60), ("Cmd", 60)]

    def row(self, frame, ascii=True):
        if frame.kind == "repeat":
            return ["Repeat", "", ""]
        names = {"nec": "NEC", "necext": "Ext-NEC", "sirc": "SIRC",
                 "rc5": "RC5", "rc5x": "RC5X", "rc6": "RC6", "rcmm": "RC-MM",
                 "samsung": "Samsung", "lg": "LG", "jvc": "JVC",
                 "denon": "Denon", "mitsubishi": "Mitsu"}
        if frame.kind == "kaseikyo":
            name = "Panason." if frame.length == 0x2002 else "Kaseikyo"
        else:
            name = names.get(frame.kind, frame.kind)
        if frame.kind in ("rc5", "rc5x", "rc6"):
            name += "  T%d" % (frame.length & 1)
        addr = frame.tag & 0xFFFF
        cmd = frame.value & 0xFFFF
        afmt = "%04X" % addr if addr > 0xFF else "%02X" % addr
        cfmt = "%04X" % cmd if cmd > 0xFF else "%02X" % cmd
        return [name, afmt, cfmt]

    def label(self, frame, ascii=True):
        if frame.kind == "repeat":
            return "rpt"
        v = frame.value & 0xFFFF
        text = "%04X" % v if v > 0xFF else "%02X" % v
        return ("!" + text) if frame.status else text


# ---- helpers ------------------------------------------------------------

def _near(x, target, tol):
    return abs(x - target) <= target * tol


def _lsb(bits):
    v = 0
    for k, b in enumerate(bits):
        v |= (b & 1) << k
    return v


def _msb(bits):
    v = 0
    for b in bits:
        v = (v << 1) | (b & 1)
    return v


def _nec_bits(runs, k):
    """NEC bits from run index k: each bit is a ~560 us mark then a gap, a
    one if the gap is long. Returns (bits, end_index, next_run_index)."""
    bits = []
    while k + 1 < len(runs) and len(bits) < 32:
        m, start, dur = runs[k]
        gap = runs[k + 1]
        if m != 1 or gap[0] != 0:
            break
        bits.append(1 if gap[2] > 1000 * _US else 0)
        k += 2
    end = runs[k][1] if k < len(runs) else (runs[-1][1] if runs else 0)
    return bits, end, k


def _pd_bits(runs, k, count, thresh):
    """Pulse-distance bits from run index k: each bit is a mark then a gap, a
    one if the gap is longer than `thresh` seconds. The same shape as
    _nec_bits, for the many protocols that share the scheme but differ in
    leader, bit count and threshold. Returns (bits, end_index, next_index)."""
    bits = []
    while k + 1 < len(runs) and len(bits) < count:
        m, start, dur = runs[k]
        gap = runs[k + 1]
        if m != 1 or gap[0] != 0:
            break
        bits.append(1 if gap[2] > thresh else 0)
        k += 2
    end = runs[k][1] if k < len(runs) else (runs[-1][1] if runs else 0)
    return bits, end, k


def _rcmm_bits(runs, k, symbols):
    """RC-MM value from run index k: each symbol is a mark then a space of one
    of four lengths carrying two bits (00/01/10/11 for 277/444/611/778 us).
    Returns (value, end_index, next_index), or (None, ...) if it runs short."""
    val = got = 0
    while k + 1 < len(runs) and got < symbols:
        m, start, dur = runs[k]
        gap = runs[k + 1]
        if m != 1 or gap[0] != 0:
            break
        g = gap[2]
        two = 0 if g < 360 * _US else 1 if g < 525 * _US \
            else 2 if g < 695 * _US else 3
        val = (val << 2) | two
        got += 1
        k += 2
    end = runs[k][1] if k < len(runs) else (runs[-1][1] if runs else 0)
    return (val if got >= symbols else None), end, k


def _half_levels(runs, half_seconds, sig, lead_space=False):
    """The mark level at each half-bit, from the first mark on. A run that
    spans several half-bits repeats its level that many times, so a
    double-width bit reads as two half-bits at one level. `lead_space`
    prepends one space half-bit for a frame whose start bit opens in space
    and so merges with the idle before it. Returns (levels, start, end)."""
    k = 0
    while k < len(runs) and runs[k][0] == 0:    # trim idle / leading space
        k += 1
    trimmed = runs[k:]
    if not trimmed:
        return [], 0, 0
    levels = [0] if lead_space else []
    for m, s, dur in trimmed:
        levels.extend([m] * max(1, int(round(dur / half_seconds))))
    end = trimmed[-1][1] + int(round(trimmed[-1][2] / sig.dt))
    return levels, trimmed[0][1], end


def _pair_bits(levels, ones_first=False):
    """Manchester bits from half-bit levels, paired two at a time. A one is
    space->mark by default (RC5); ones_first swaps that to mark->space (RC6).
    A pair with no transition repeats the previous bit rather than dropping
    alignment."""
    bits = []
    for h in range(0, len(levels) - 1, 2):
        first, second = levels[h], levels[h + 1]
        if first == second:
            bits.append(bits[-1] if bits else 0)
            continue
        one = (first == 1 and second == 0) if ones_first \
            else (first == 0 and second == 1)
        bits.append(1 if one else 0)
    return bits
