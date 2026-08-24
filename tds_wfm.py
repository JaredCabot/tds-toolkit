"""
tds_wfm.py - waveforms over GPIB, to and from a TDS500/600/700.

A different subsystem from the filesystem, and a far more capable one:
every firmware image in the family has these commands, including the
earliest, which has no filesystem at all. It is also an order faster,
because nothing touches a disk - 5000 points off a TDS 784D in 0.02 s.

Reading, which is Tektronix's own sequence from the GETWFM examples they
shipped with these instruments:

    DATA:SOURCE <src>
    DATA:ENCDG RIBINARY ; DATA:WIDTH 1
    DATA:START 1 ; DATA:STOP <record length>
    WFMPRE:<src>:NR_PT?  PT_FMT?  XINCR?  XZERO?  PT_OFF?  XUNIT?
                         YMULT?  YZERO?  YOFF?  YUNIT?  WFID?
    CURVE?                    -> #<digits><length><bytes>

Writing into a reference, the same in reverse, plus the one step that is
the whole difficulty - see send_to_ref().

Everything here was measured on a TDS 784D (v7.4e), a TDS 784C (v5.3e)
and a TDS 640A (v3.8.8e); see INSTRUMENT-NOTES.md.
"""
import base64
import math
import struct
import time
import zlib

# The per-source preamble. Asked for one field at a time, by name, because
# the bulk WFMPRE:<src>? reply is NOT in the same field order on every
# instrument - a 784D and a 640A disagree, and mapping one onto the other
# quietly puts a unit string where a sample count belongs.
FIELDS = ("NR_PT", "PT_FMT", "XINCR", "XZERO", "PT_OFF", "XUNIT",
          "YMULT", "YZERO", "YOFF", "YUNIT", "WFID")

# What SELECT? reports, in order, as ones and zeros - used only as a last
# resort. The instrument names its own fields if asked with HEADER ON, and
# that is what is used, because this list is an assumption about how many
# channels the instrument has and the program has no business making it.
# One firmware image serves both the two-channel and four-channel models
# of a family, so not even the firmware settles it.
SELECTABLE = ("CH1", "CH2", "CH3", "CH4", "MATH1", "MATH2", "MATH3",
              "REF1", "REF2", "REF3", "REF4")
REFS = ("REF1", "REF2", "REF3", "REF4")


# Bigger than any record these instruments hold - the longest option on
# the range is 50,000 points. DATA:STOP clamps to what the source
# actually has rather than complaining, so this asks for "all of it"
# without having to know how much that is.
WHOLE_RECORD = 1000000


class NotReadable(IOError):
    """This source has nothing that can be read right now.

    Either it is a channel or a maths trace that is not displayed - the
    instrument is not acquiring it, so there is nothing to send - or it
    is a reference with nothing stored in it.
    """

    def __init__(self, source, message=None):
        self.source = source
        IOError.__init__(
            self, message or
            "%s has no waveform that can be read. A channel has to be "
            "displayed on the instrument before it can be read; a "
            "reference has to have something stored in it." % source)

# How a plot is coloured. Held as hex because that is what Tk speaks and
# what a settings file can carry legibly; the PNG side converts. The
# encoder counts whatever colours actually end up in the image and picks
# its bit depth from that, so a scheme with more colours in it costs a
# little more file and needs no change here.
# The graticule is drawn the way the instrument draws its own, which was
# settled by reading the pixels of a screenshot off a 784C rather than
# from memory: an outer border, the ten-by-eight division lines, a
# heavier cross through the centre, and fine pips along that cross at a
# fifth of a division. Each is a separate element with its own colour, so
# the heavier parts can be brought forward or dropped into the background
# as the plot needs.
ELEMENTS = ("background", "graticule", "major", "pips", "border",
            "trace", "label", "grid", "mask", "select", "hover", "cut")

# The last five are the mask editor's. They are in the same scheme
# because a mask is drawn over the same graticule a trace is, and a
# scheme that covered one and not the other would be half a scheme.
# scheme() fills in whatever a settings file predates, so an old file
# and an old saved preset both still load.
DEFAULT_COLOURS = {"background": "#12161c", "graticule": "#39424e",
                   "major": "#55637a", "pips": "#55637a",
                   "border": "#55637a",
                   "trace": "#ffd640", "label": "#9aa5b1",
                   "grid": "#5b4f8a", "mask": "#9aa5b1",
                   "select": "#e08a2e", "hover": "#ffd640",
                   "cut": "#e04a4a"}

# How many pips to a division on the centre cross, and how long they are
# as a fraction of a division. Both are what the instrument uses.
PIPS_PER_DIV = 5
PIP_LENGTH = 0.08

# How wide a saved picture is unless somebody says otherwise.
# The height follows from it, so the graticule fills the image.
DEFAULT_PNG_WIDTH = 800

# Starting points, and a demonstration that the scheme is free. Not
# deletable, but a preset of the same name saved by the user wins.
BUILT_IN_PRESETS = {
    "Instrument": dict(DEFAULT_COLOURS),
    "Printed page": {"background": "#ffffff", "graticule": "#d8d8d8",
                     "major": "#a8a8a8", "pips": "#a8a8a8",
                     "border": "#606060",
                     "trace": "#0b3d91", "label": "#333333",
                     "grid": "#c9b9e6", "mask": "#333333",
                     "select": "#b05a00", "hover": "#e07800",
                     "cut": "#c00000"},
    "Phosphor": {"background": "#06100a", "graticule": "#28503a",
                 "major": "#3d7355", "pips": "#3d7355",
                 "border": "#3d7355",
                 "trace": "#78ff96", "label": "#6f8f7a",
                 "grid": "#2a4a55", "mask": "#9fe8b4",
                 "select": "#3fc06a", "hover": "#78ff96",
                 "cut": "#ff6b6b"},
    "High contrast": {"background": "#000000", "graticule": "#5a5a5a",
                      "major": "#9a9a9a", "pips": "#9a9a9a",
                      "border": "#ffffff",
                      "trace": "#ffffff", "label": "#ffffff",
                      "grid": "#6a6ad0", "mask": "#ffffff",
                      "select": "#00d0ff", "hover": "#ffff00",
                      "cut": "#ff0000"},
}


def rgb(colour, fallback=(0, 0, 0)):
    """'#rrggbb' to a byte triple, forgivingly."""
    text = str(colour or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return fallback
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return fallback


# How far round the hue circle each extra trace is moved when several
# share a graticule and the instrument has no colours of its own to
# offer. Not evenly spaced: these are the gaps that stay apart from one
# another on a dark background at one pixel wide.
HUE_STEPS = (0.0, 0.47, 0.19, 0.68, 0.32, 0.83)


def shifted_hue(colour, step=0):
    """The same colour moved round the hue circle, for another trace."""
    step = int(step) % len(HUE_STEPS)
    if not step:
        return colour
    red, green, blue = [v / 255.0 for v in rgb(colour, (255, 214, 64))]
    high, low = max(red, green, blue), min(red, green, blue)
    light = (high + low) / 2.0
    if high == low:
        # A grey has no hue to move, so give it one rather than
        # returning the same colour for every trace.
        hue, sat = 0.0, 0.6
    else:
        gap = high - low
        sat = gap / (2.0 - high - low if light > 0.5 else high + low)
        if high == red:
            hue = ((green - blue) / gap + (6.0 if green < blue else 0.0)) / 6.0
        elif high == green:
            hue = ((blue - red) / gap + 2.0) / 6.0
        else:
            hue = ((red - green) / gap + 4.0) / 6.0
    return hls_to_hex(((hue + HUE_STEPS[step]) % 1.0) * 360.0 + HUE_ORIGIN,
                      max(35.0, min(75.0, light * 100.0)), sat * 100.0)


def scheme(colours=None):
    """A full colour scheme, whatever the caller supplied."""
    out = dict(DEFAULT_COLOURS)
    for key, value in (colours or {}).items():
        if key in out and value:
            out[key] = value
    return out


class Waveform(object):
    """One captured trace: the raw curve, and what it means.

    The raw bytes are kept exactly as the instrument sent them and every
    format is derived from them on demand, so nothing is lost to rounding
    on the way in and a saved file can always be re-derived differently
    later.
    """

    def __init__(self, source, raw, pre, idn="", width=1):
        self.source = source
        self.raw = bytes(raw)
        self.pre = dict(pre)
        self.idn = idn
        self.width = width
        # What to call it on the plot. Normally the source it came from,
        # but a waveform loaded from a file and held against REF2 until
        # it is sent should say REF2, which is where it is going.
        self.label = None
        # And which file it came off, where it came off one. The source
        # name is the instrument's - an ISF says "Ch1" whatever the file
        # on the disk is called - and a person looking at a list of
        # staged references wants to know which file they picked.
        self.origin = None

    # -- what the preamble says -------------------------------------------

    def number(self, key, default=0.0):
        try:
            return float(self.pre.get(key))
        except (TypeError, ValueError):
            return default

    def text(self, key, default=""):
        val = self.pre.get(key)
        return default if val is None else str(val).strip().strip('"')

    @property
    def count(self):
        return len(self.raw) // self.width

    @property
    def wfid(self):
        return self.text("WFID")

    @property
    def counts_per_div(self):
        """Sample counts to one vertical division.

        The digitiser spans 10.24 divisions at either width, so eight
        bits give 25 counts a division and sixteen give 6400. Measured -
        it is the same constant the .WFM format is built on, and it is
        what makes a raw sample mean a position on the screen.
        """
        return LLWFM_COUNTS_PER_DIV if self.width == 2 else 25.0

    @property
    def volts_per_div(self):
        """The instrument's own vertical scale, from the preamble."""
        return self.number("YMULT", 1.0) * self.counts_per_div

    @property
    def seconds_per_div(self):
        """What one division of *this plot* is worth.

        The whole record is drawn across the ten divisions, so this is
        the record divided by ten - which is the instrument's own
        timebase only when the record is the 500 points the instrument
        shows. See instrument_seconds_per_div for the other one.
        """
        return self.number("XINCR", 1.0) * self.count / float(DIVS_X)

    @property
    def instrument_seconds_per_div(self):
        """The timebase the instrument itself is set to.

        A TDS draws fifty points to a division and no more, whatever the
        record length is: at 5000 points the screen shows a tenth of
        what was captured and the rest is reached by panning. So the
        instrument's setting comes from the sample interval alone, not
        from how much was acquired.

        Measured: a 784D holding 5000 points at 20 ns a sample reports
        1 us/div in its own WFID, and 20 ns x 50 is exactly that, while
        the record divided by ten is ten times too big.
        """
        return self.number("XINCR", 1.0) * POINTS_PER_DIV

    @property
    def yunit(self):
        return self.text("YUNIT", "V")

    @property
    def xunit(self):
        return self.text("XUNIT", "s")

    def levels(self):
        """The samples as the instrument's own signed integers."""
        if self.width == 2:
            return struct.unpack(">%dh" % (len(self.raw) // 2), self.raw)
        return struct.unpack(">%db" % len(self.raw), self.raw)

    def points(self):
        """(seconds, volts) pairs, with the trigger at zero.

        volts   = (raw - YOFF) * YMULT + YZERO
        seconds = (n - PT_OFF) * XINCR + XZERO
        """
        ymult = self.number("YMULT", 1.0)
        yoff = self.number("YOFF")
        yzero = self.number("YZERO")
        xincr = self.number("XINCR", 1.0)
        xzero = self.number("XZERO")
        ptoff = self.number("PT_OFF")
        return [((n - ptoff) * xincr + xzero, (v - yoff) * ymult + yzero)
                for n, v in enumerate(self.levels())]

    def measures(self):
        """What the summary is made of, as numbers and unit strings.

        Given separately so the window can put them into a translated
        sentence. This module has no business knowing what language
        anybody reads in, and a sentence assembled here cannot be
        translated by anyone who does.
        """
        pts = self.points()
        if not pts:
            return None
        volts = [v for _t, v in pts]
        return {"points": len(pts),
                "span": eng(pts[-1][0] - pts[0][0], self.xunit),
                "low": eng(min(volts), self.yunit),
                "high": eng(max(volts), self.yunit),
                "vdiv": eng(self.volts_per_div, self.yunit),
                "tdiv": eng(self.seconds_per_div, self.xunit),
                "sdiv": eng(self.instrument_seconds_per_div, self.xunit),
                # Whether the record is wider than the instrument's own
                # screen, which is what makes those two differ.
                "wider": self.count > DIVS_X * POINTS_PER_DIV}

    def summary(self):
        """The same in English, for logs and for anything with no _()."""
        got = self.measures()
        if not got:
            return "%s: nothing" % self.source
        return ("%(points)d points over %(span)s, %(low)s to %(high)s"
                % got)

    # -- formats ----------------------------------------------------------

    def to_csv(self):
        """Scaled time and amplitude, one pair per line, trigger at zero."""
        out = ["%s,%s" % (self.xunit, self.yunit)]
        out += ["%.9g,%.9g" % (t, v) for t, v in self.points()]
        return ("\r\n".join(out) + "\r\n").encode("ascii")

    def to_isf(self):
        """The instrument's own internal format: preamble, then the curve.

        Byte for byte what CURVE? returned, with the preamble that gives it
        meaning in front - so nothing is lost, and Tektronix's CNVRTWFM
        will convert it onward to anything else.
        """
        order = (("BYT_NR", str(self.width)),
                 ("BIT_NR", "16" if self.width == 2 else "8"),
                 ("ENCDG", "BIN"), ("BN_FMT", "RI"), ("BYT_OR", "MSB"),
                 ("NR_PT", str(self.count)),
                 ("WFID", '"%s"' % self.wfid),
                 ("PT_FMT", self.text("PT_FMT", "Y")),
                 ("XINCR", self.text("XINCR", "1")),
                 ("PT_OFF", self.text("PT_OFF", "0")),
                 ("XZERO", self.text("XZERO", "0")),
                 ("XUNIT", '"%s"' % self.xunit),
                 ("YMULT", self.text("YMULT", "1")),
                 ("YZERO", self.text("YZERO", "0")),
                 ("YOFF", self.text("YOFF", "0")),
                 ("YUNIT", '"%s"' % self.yunit))
        head = ":WFMPRE:" + ";".join("%s %s" % kv for kv in order) + ";"
        body = self.raw
        digits = str(len(body))
        return (head + ":CURVE #%d%s" % (len(digits), digits)).encode(
            "ascii") + body

    def to_wfm(self):
        """The instrument's own .WFM, as SAVE:WAVEFORM would write it.

        Built on a header the instrument wrote, with the fields that
        describe this waveform patched over it. The samples are widened
        to sixteen bits if they came in at eight, which is what the file
        format holds, and the sixteen guard points either side of the
        curve are filled with the nearest real sample - they are there
        for display interpolation and the instrument acquires them, so
        repeating the end of the trace is the honest approximation.
        """
        levels = list(self.levels())
        if not levels:
            raise ValueError("there is nothing to write")
        if self.width == 1:
            # Widening the samples by 256 means the volts a count must
            # shrink by 256 to keep the same voltages - so volts a
            # division is the eight-bit YMULT times the 25 counts in a
            # division at that width, and not that times 256 again.
            levels = [v * 256 for v in levels]
            per_div = self.number("YMULT", 1.0) * 25.0
            offset = self.number("YOFF") * 256.0
        else:
            per_div = self.number("YMULT", 1.0) * LLWFM_COUNTS_PER_DIV
            offset = self.number("YOFF")
        count = len(levels)
        xincr = self.number("XINCR", 1.0)
        wfid = self.wfid

        head = bytearray(LLWFM_TEMPLATE)
        # The sweep as the file gave it, where it came from one. See
        # from_wfm: the instrument works this out in single precision
        # and widens it, so xincr times the count is close but not the
        # same bytes. A waveform read off the bus has no such field and
        # the product is the best there is.
        span = getattr(self, "llwfm_duration", None)
        struct.pack_into(">d", head, LLWFM_DURATION,
                         xincr * count if span is None else span)
        struct.pack_into(">d", head, LLWFM_XINCR, xincr)
        struct.pack_into(">d", head, LLWFM_POSITION,
                         offset / LLWFM_COUNTS_PER_DIV)
        struct.pack_into(">d", head, LLWFM_YZERO, self.number("YZERO"))
        struct.pack_into(">d", head, LLWFM_PERDIV, per_div)
        struct.pack_into(">i", head, LLWFM_COUNT, count)
        struct.pack_into(">H", head, LLWFM_COUNT16, count & 0xFFFF)
        struct.pack_into(">H", head, LLWFM_TRIGGER,
                         min(100, max(0, int(round(self.number("PT_OFF")
                                                   * 100.0 / count)))))
        # The coupling and the acquisition mode live only in the WFID,
        # which is where the instrument itself puts them and where
        # CNVRTWFM reads them from. A field nothing names is left as the
        # template had it rather than zeroed.
        for where, code in (
                (LLWFM_MODE, llwfm_code(LLWFM_MODES, wfid)),
                (LLWFM_COUPLING, llwfm_code(LLWFM_COUPLINGS, wfid)),
                (LLWFM_FORMAT, llwfm_code(LLWFM_FORMATS,
                                          self.text("PT_FMT", "Y"))),
                (LLWFM_XUNIT, LLWFM_UNIT_CODES.get(self.xunit.lower())),
                (LLWFM_YUNIT, LLWFM_UNIT_CODES.get(self.yunit.lower())),
                (LLWFM_SOURCE,
                 LLWFM_SOURCE_0 + LLWFM_SOURCES.index(self.source.upper())
                 if self.source.upper() in LLWFM_SOURCES else None)):
            if code is not None:
                struct.pack_into(">H", head, where, code)

        guard_before = [levels[0]] * LLWFM_GUARD
        guard_after = [levels[-1]] * LLWFM_GUARD
        all_points = guard_before + levels + guard_after
        body = bytes(head) + struct.pack(">%dh" % len(all_points),
                                         *all_points)
        # The length field counts the payload, itself included, less the
        # ten bytes in front of it. Written before the checksum, which
        # covers it.
        payload_len = len(body) + 2
        struct.pack_into(">i", body := bytearray(body), LLWFM_LENGTH,
                         payload_len - 10)
        body = bytes(body)
        body += struct.pack(">H", llwfm_checksum(body))
        digits = str(len(body))
        return (LLWFM_MAGIC + b" #"
                + str(len(digits)).encode("ascii")
                + digits.encode("ascii") + body)

    def to_png(self, width=DEFAULT_PNG_WIDTH, height=None,
               colours=None, caption="", view=None, traces=None,
               strip=False):
        """The trace as a picture.

        With no height given, one is chosen so the graticule fills the
        image: ten divisions by eight, square, with no wasted margin.

        `strip` adds the record strip along the top, as the window has
        it.
        """
        return plot_png(self, width, height, colours, caption,
                        view, traces, strip=strip)


def from_isf(data, name="file"):
    """Read back an .isf - the format to_isf() writes, and the
    instrument's own.

    The preamble is `:WFMPRE:KEY value;KEY value;...` followed by
    `:CURVE #<digits><length><bytes>`. Parsed tolerantly: the header may
    or may not carry the leading colons, the keys may be long or short
    form, and anything unrecognised is kept in the preamble rather than
    dropped, since the only thing that must be understood to plot it is
    the handful of scaling fields.
    """
    at = data.find(b"CURVE")
    if at < 0:
        raise ValueError("no CURVE block - this is not an ISF file")
    head = data[:at].decode("latin-1")
    rest = data[at:]
    hash_at = rest.find(b"#")
    if hash_at < 0:
        raise ValueError("the CURVE block has no length in front of it")
    digits = int(chr(rest[hash_at + 1]))
    length = int(rest[hash_at + 2:hash_at + 2 + digits].decode("ascii"))
    start = hash_at + 2 + digits
    body = rest[start:start + length]
    if len(body) < length:
        raise ValueError("the curve is %d bytes short of the %d it claims"
                         % (length - len(body), length))
    pre = {}
    head = head.split(":WFMPRE:")[-1].split("WFMPRE:")[-1]
    for field in head.split(";"):
        field = field.strip().lstrip(":")
        if not field or " " not in field:
            continue
        key, _sep, value = field.partition(" ")
        pre[key.split(":")[-1].upper()] = value.strip()
    width = 1
    try:
        width = max(1, min(2, int(float(pre.get("BYT_NR", "1")))))
    except (TypeError, ValueError):
        pass
    if width == 2 and len(body) % 2:
        raise ValueError("an odd number of bytes for a 16-bit waveform")
    source = pre.get("WFID", "").strip('"').split(",")[0].strip() or name
    return Waveform(source, body, pre, width=width)


def from_csv(data, name="file"):
    """Read back a .csv of the kind to_csv() writes: seconds, volts.

    The file carries scaled values rather than the instrument's raw
    integers, so it cannot be turned back into the original bytes. What
    it can be turned into is a waveform that plots identically and can
    be sent to an instrument, by scaling the values into the eight-bit
    range the instrument uses and describing that scaling in the
    preamble. Anything the instrument is later asked to do with it then
    works out to the same numbers.
    """
    text = data.decode("utf-8-sig", "replace") if isinstance(data, bytes) \
        else data
    times, volts = [], []
    xunit, yunit = "s", "V"
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.replace("\t", ",").split(",")]
        if len(parts) < 2:
            continue
        try:
            t, v = float(parts[0]), float(parts[1])
        except ValueError:
            # A header row names the columns and their units.
            low = line.lower()
            for unit, mark in (("s", "second"), ("s", "time")):
                if mark in low:
                    xunit = unit
            if "volt" in low:
                yunit = "V"
            continue
        times.append(t)
        volts.append(v)
    if len(times) < 2:
        raise ValueError("fewer than two numeric rows - is this a CSV of "
                         "time and amplitude?")
    lo, hi = min(volts), max(volts)
    if hi == lo:
        hi = lo + 1.0
    # Centre the trace in the instrument's own range, which is -128..127
    # for eight bits, and record the scaling that makes it mean what the
    # file said.
    #
    # Across eight divisions, not the full range: the plot places a
    # sample by its own count, so a file scaled across the digitiser's
    # 10.24 divisions would be drawn running off the top and bottom of
    # the graticule. Eight divisions is 200 counts, which fills the
    # screen exactly and still keeps 200 levels of the file's detail.
    span = hi - lo
    ymult = span / (DIVS_Y * 25.0)
    yzero = (hi + lo) / 2.0
    raw = bytes(int(round((v - yzero) / ymult)) & 0xFF for v in volts)
    xincr = (times[-1] - times[0]) / float(len(times) - 1)
    pre = {"NR_PT": str(len(volts)), "PT_FMT": "Y",
           "XINCR": repr(xincr), "XZERO": repr(times[0]),
           "PT_OFF": "0", "XUNIT": '"%s"' % xunit,
           "YMULT": repr(ymult), "YZERO": repr(yzero), "YOFF": "0",
           "YUNIT": '"%s"' % yunit,
           "WFID": '"%s, loaded from a CSV file, %d points"'
                   % (name, len(volts))}
    return Waveform(name, raw, pre, width=1)


# The instrument's own waveform file, as written by SAVE:WAVEFORM to a
# disk. It is not ISF: it begins "LLWFM " followed by an IEEE block, and
# inside that a 132-byte header, sixteen precharge samples, the record,
# sixteen postcharge samples and a two-byte checksum, at two bytes a
# sample big-endian throughout.
LLWFM_MAGIC = b"LLWFM"

# Where things live in that header, all big-endian. Settled against
# Tektronix's own CNVRTWFM, which converts these files in both
# directions: feeding it an ISF with one preamble field changed and
# comparing the .WFM it writes names a field beyond argument, and
# converting the other way checks the reading. Fifteen instrument-
# written files across two firmware generations and sixty synthetic
# ones; every field below moved when, and only when, its own value did.
#
# What looked at first like a 164-byte header and a 34-byte trailer is a
# 132-byte header and then real samples: sixteen points before the curve
# and sixteen after it, which is what the later Tektronix format calls
# precharge and postcharge and uses for display interpolation. They move
# with the acquisition, which is why they appeared to be a header that
# changed for no reason.
LLWFM_HEADER = 132          # bytes before the precharge points
LLWFM_GUARD = 16            # samples either side of the curve
LLWFM_CURVE = LLWFM_HEADER + LLWFM_GUARD * 2     # 164
LLWFM_TRAILER = LLWFM_GUARD * 2 + 2              # 34, the last two a checksum
LLWFM_LENGTH = 4            # int32: the payload length, less ten
LLWFM_SUM_FROM = 8          # the checksum covers from here to itself
LLWFM_MODE = 40             # uint16: how the record was acquired
LLWFM_FORMAT = 42           # uint16: Y or ENV
LLWFM_DURATION = 44         # double: the whole record, in seconds
LLWFM_COUPLING = 52         # uint16: the input coupling
LLWFM_XUNIT = 54            # uint16: the horizontal unit
LLWFM_XINCR = 56            # double: seconds a sample
LLWFM_YUNIT = 64            # uint16: the vertical unit
LLWFM_YZERO = 66            # double: volts at the offset position
LLWFM_POSITION = 74         # double: vertical offset, in divisions
LLWFM_PERDIV = 82           # double: volts a division
LLWFM_COUNT = 90            # int32: samples
LLWFM_TRIGGER = 94          # uint16: the trigger, as a percent of the record
LLWFM_COUNT16 = 100         # uint16: the sample count again
LLWFM_SOURCE = 102          # uint16: which input the record came from
# A 16-bit sample spans 10.24 divisions, as an 8-bit one does, so a
# division is 6400 counts and YMULT is volts-per-division over that.
LLWFM_COUNTS_PER_DIV = 6400.0

# The enumerations are the instrument's own string-table numbers, which
# is why they are not consecutive. These are the ones seen; a number
# that is not here is reported as unknown rather than guessed at.
LLWFM_MODES = {285: "Sample", 2: "Peak Detect", 3: "Hi Res",
               4: "Average", 187: "Envelope"}
LLWFM_COUPLINGS = {565: "DC", 566: "AC", 25: "GND"}
LLWFM_UNITS = {609: "Volts", 610: "s", 632: "VV", 736: "Hz", 740: "dB"}
LLWFM_UNIT_CODES = {"v": 609, "volt": 609, "volts": 609, "s": 610,
                    "sec": 610, "seconds": 610, "vv": 632, "hz": 736,
                    "db": 740}
LLWFM_FORMATS = {98: "Y", 97: "ENV"}
LLWFM_SOURCE_0 = 107        # CH1; the rest follow in this order
LLWFM_SOURCES = ("CH1", "CH2", "CH3", "CH4", "MATH1", "MATH2", "MATH3",
                 "REF1", "REF2", "REF3", "REF4")


# A header to build new files on. Taken from a file the instrument
# wrote. Every field above is patched over it; what is left is a dozen
# bytes whose meaning is still not known, and they are carried as they
# were found rather than invented. Two of them differ between the files
# a 784D writes and the ones Tektronix shipped with TTiP, and both kinds
# recall correctly, so nothing there is load-bearing.
LLWFM_TEMPLATE = base64.b64decode(
    "AAAAAAAABKQAAAAAAAAAAD/gAAAAAAAAP/AAAAAAAAA/8AAAAAAAAAEdAGI/dHrh"
    "MAAAAAI1AmI+5Pi1gAAAAAJhAAAAAAAAAAAAAAAAAAAAAD+5mZmuAAAAAAAB9AAy"
    "AGIAMgH0AGsAAgABAg0/EKkrxA9KMQOoAAIAAAAAqgIAAAAB")


def llwfm_checksum(payload):
    """The last two bytes: big-endian words from offset 8, added up.

    Found by trying every simple sum from every start offset against
    fourteen files - it is the only one that matches all of them,
    including the file Tektronix's own CNVRTWFM wrote, whose checksum
    differs from the instrument's because the data it wrote differs.
    """
    body = payload[LLWFM_SUM_FROM:]
    words = len(body) // 2
    return sum(struct.unpack(">%dH" % words, body[:words * 2])) & 0xFFFF


def looks_like_llwfm(data):
    return data[:5] == LLWFM_MAGIC


def llwfm_word(table, code, unknown=""):
    return table.get(code, unknown)


def llwfm_code(table, words):
    """The header's number for a word in a WFID, or None for none.

    None leaves whatever the template holds, which is a number some
    instrument wrote. Writing a zero there would be inventing one.
    """
    low = (words or "").lower()
    for code, name in table.items():
        if name.lower() in low:
            return code
    return None


def llwfm_amount(value, unit):
    """500mVolts, 1us - the shape the instrument writes into a WFID."""
    for factor, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "u"),
                           (1e-9, "n"), (1e-12, "p")):
        if abs(value) >= factor:
            return "%g%s%s" % (round(value / factor, 4), prefix, unit)
    return "%g%s" % (value, unit)


def from_wfm(data, name="file"):
    """The instrument's own .WFM, as SAVE:WAVEFORM writes to a disk.

    "LLWFM " then an IEEE block, and inside it a 132-byte header, the
    precharge points, the curve at two bytes a sample big-endian, the
    postcharge points and a checksum.

    Every field the preamble needs is read out of the header. Checked
    field by field against Tektronix's own CNVRTWFM on fifteen files
    from two firmware generations: the samples come out identical and
    every scaling, unit, coupling, mode and trigger position agrees.
    This is the older format of the TDS500/600/700 - the 5000/6000/7000
    series write something else entirely, which begins with a byte-order
    mark and ":WFM#", and is not handled here.
    """
    if not looks_like_llwfm(data):
        raise ValueError("not a TDS .WFM - it does not start \"LLWFM\"")
    at = data.find(b"#")
    if at < 0:
        raise ValueError("no block header after LLWFM")
    digits = int(chr(data[at + 1]))
    length = int(data[at + 2:at + 2 + digits].decode("ascii"))
    body = data[at + 2 + digits:at + 2 + digits + length]
    if len(body) < LLWFM_CURVE + LLWFM_TRAILER:
        raise ValueError("the file is too short to hold a waveform")
    head = body[:LLWFM_HEADER]
    count = struct.unpack_from(">i", head, LLWFM_COUNT)[0]
    room = (len(body) - LLWFM_CURVE - LLWFM_TRAILER) // 2
    if not 0 < count <= room:
        # The stored count and the file's own size disagree; the size is
        # the one that cannot be wrong about how much data is there.
        count = room
    raw = body[LLWFM_CURVE:LLWFM_CURVE + count * 2]
    xincr = struct.unpack_from(">d", head, LLWFM_XINCR)[0]
    perdiv = struct.unpack_from(">d", head, LLWFM_PERDIV)[0]
    # The vertical offset is held as divisions, not as counts. Found by
    # acquiring the same signal at four vertical positions and looking
    # for what moved: the instrument's own YOFF at width 2 is exactly
    # this number times the counts in a division, on every file
    # measured. CNVRTWFM prints YOFF in eight-bit counts whatever width
    # it writes, so it reads 256 times smaller; the instrument is the
    # one to follow, since these are its samples.
    yoff = (struct.unpack_from(">d", head, LLWFM_POSITION)[0]
            * LLWFM_COUNTS_PER_DIV)
    ymult = perdiv / LLWFM_COUNTS_PER_DIV
    # The trigger is held as a whole percent of the record, which is why
    # it always came out at the middle: every file measured was taken
    # with the trigger at 50 percent.
    percent = struct.unpack_from(">H", head, LLWFM_TRIGGER)[0]
    which = struct.unpack_from(">H", head, LLWFM_SOURCE)[0] - LLWFM_SOURCE_0
    known = 0 <= which < len(LLWFM_SOURCES)
    source = LLWFM_SOURCES[which] if known else name
    mode = llwfm_word(LLWFM_MODES,
                      struct.unpack_from(">H", head, LLWFM_MODE)[0])
    coupling = llwfm_word(LLWFM_COUPLINGS,
                          struct.unpack_from(">H", head, LLWFM_COUPLING)[0])
    xunit = llwfm_word(LLWFM_UNITS,
                       struct.unpack_from(">H", head, LLWFM_XUNIT)[0], "s")
    yunit = llwfm_word(LLWFM_UNITS,
                       struct.unpack_from(">H", head, LLWFM_YUNIT)[0],
                       "Volts")
    # The instrument shows fifty points to a division whatever the
    # record length, so its own timebase is the sample interval times
    # fifty - not the record over ten divisions.
    wfid = ", ".join(
        [source.replace("CH", "Ch") if known else "Unknown"]
        + (["%s coupling" % coupling] if coupling else [])
        + [llwfm_amount(perdiv, yunit) + "/div",
           llwfm_amount(xincr * POINTS_PER_DIV, xunit) + "/div",
           "%d points" % count]
        + (["%s mode" % mode] if mode else []))
    pre = {"NR_PT": str(count),
           "PT_FMT": llwfm_word(LLWFM_FORMATS,
                                struct.unpack_from(">H", head,
                                                   LLWFM_FORMAT)[0], "Y"),
           "XINCR": repr(xincr), "XZERO": "0",
           "PT_OFF": str(int(round(percent * count / 100.0))),
           "XUNIT": '"%s"' % xunit,
           "YMULT": repr(ymult),
           "YZERO": repr(struct.unpack_from(">d", head, LLWFM_YZERO)[0]),
           "YOFF": repr(yoff), "YUNIT": '"%s"' % yunit,
           "WFID": '"%s"' % wfid}
    wave = Waveform(source, raw, pre, width=2)
    # Kept rather than worked out again. The instrument computes the
    # sweep in single precision and widens it, so recomputing it here
    # as xincr times the count lands a few mantissa bits away from
    # what it wrote - eight bytes of difference in a file that is
    # otherwise identical. Measured over seven records on a 784D.
    wave.llwfm_duration = struct.unpack_from(">d", head, LLWFM_DURATION)[0]
    return wave


def load(path):
    """A waveform from a file on the PC, by what is in it.

    Sniffed rather than trusted to the extension: a .wfm from another
    program may well be an ISF, and a file renamed by hand should still
    work. A file that really is the instrument's own .WFM is recognised
    and named as such, rather than being reported as a malformed ISF.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    if not data:
        raise ValueError("the file is empty")
    import os
    name = os.path.splitext(os.path.basename(path))[0]
    if looks_like_llwfm(data):
        return from_wfm(data, name)
    if data[:2] in (b"\xf0\xf0", b"\x0f\x0f") or b":WFM#" in data[:16]:
        raise ValueError(
            "This is a waveform file from a TDS5000/6000/7000 or a "
            "DPO/MSO - a different and much later format from the one "
            "the TDS500/600/700 write. It is not handled here.")
    if b"CURVE" in data[:4096] and b"#" in data:
        return from_isf(data, name)
    return from_csv(data, name)


def eng(value, unit=""):
    """A number the way an instrument would print it: 1.23 ms, not 0.00123."""
    if value == 0:
        return "0 %s" % unit if unit else "0"
    scale = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"),
             (1e-6, "u"), (1e-9, "n"), (1e-12, "p")]
    size = abs(value)
    for factor, prefix in scale:
        if size >= factor:
            return "%.4g %s%s" % (value / factor, prefix, unit)
    return "%.4g p%s" % (value / 1e-12, unit)


# ------------------------------------------------------------------ drawing

# A 5x7 bitmap for the characters an axis label can contain: digits, a
# sign, a decimal point, the SI prefixes and the letters of the units
# these instruments report. Enough to label a plot without dragging in a
# font library for the sake of twenty glyphs. Anything with no glyph is
# skipped rather than drawn as a box.
GLYPHS = {'0': ('01110', '10001', '10011', '10101', '11001', '10001', '01110'), '1': ('00100', '01100', '00100', '00100', '00100', '00100', '01110'), '2': ('01110', '10001', '00001', '00010', '00100', '01000', '11111'), '3': ('11111', '00010', '00100', '00010', '00001', '10001', '01110'), '4': ('00010', '00110', '01010', '10010', '11111', '00010', '00010'), '5': ('11111', '10000', '11110', '00001', '00001', '10001', '01110'), '6': ('00110', '01000', '10000', '11110', '10001', '10001', '01110'), '7': ('11111', '00001', '00010', '00100', '01000', '01000', '01000'), '8': ('01110', '10001', '10001', '01110', '10001', '10001', '01110'), '9': ('01110', '10001', '10001', '01111', '00001', '00010', '01100'), '.': ('00000', '00000', '00000', '00000', '00000', '01100', '01100'), '-': ('00000', '00000', '00000', '11111', '00000', '00000', '00000'), '+': ('00000', '00100', '00100', '11111', '00100', '00100', '00000'), ' ': ('00000', '00000', '00000', '00000', '00000', '00000', '00000'), 'V': ('10001', '10001', '10001', '10001', '10001', '01010', '00100'), 'o': ('00000', '00000', '01110', '10001', '10001', '10001', '01110'), 'l': ('01100', '00100', '00100', '00100', '00100', '00100', '01110'), 't': ('01000', '01000', '11110', '01000', '01000', '01001', '00110'), 's': ('00000', '00000', '01111', '10000', '01110', '00001', '11110'), 'm': ('00000', '00000', '11010', '10101', '10101', '10101', '10101'), 'u': ('00000', '00000', '10001', '10001', '10001', '10011', '01101'), 'n': ('00000', '00000', '10110', '11001', '10001', '10001', '10001'), 'p': ('00000', '00000', '11110', '10001', '11110', '10000', '10000'), 'k': ('10000', '10000', '10010', '10100', '11000', '10100', '10010'), 'M': ('10001', '11011', '10101', '10101', '10001', '10001', '10001'), 'G': ('01110', '10001', '10000', '10111', '10001', '10001', '01111'), 'A': ('01110', '10001', '10001', '11111', '10001', '10001', '10001'), 'W': ('10001', '10001', '10001', '10101', '10101', '11011', '10001'), 'd': ('00001', '00001', '01111', '10001', '10001', '10001', '01111'), 'B': ('11110', '10001', '10001', '11110', '10001', '10001', '11110'), 'e': ('00000', '00000', '01110', '10001', '11111', '10000', '01110'), 'c': ('00000', '00000', '01111', '10000', '10000', '10000', '01111'), 'H': ('10001', '10001', '10001', '11111', '10001', '10001', '10001'), 'z': ('00000', '00000', '11111', '00010', '00100', '01000', '11111'), '%': ('11001', '11010', '00010', '00100', '01000', '01011', '10011'), 'C': ('01110', '10001', '10000', '10000', '10000', '10001', '01110'), 'D': ('11110', '10001', '10001', '10001', '10001', '10001', '11110'), 'E': ('11111', '10000', '10000', '11110', '10000', '10000', '11111'), 'F': ('11111', '10000', '10000', '11110', '10000', '10000', '10000'), 'I': ('01110', '00100', '00100', '00100', '00100', '00100', '01110'), 'J': ('00111', '00010', '00010', '00010', '00010', '10010', '01100'), 'K': ('10001', '10010', '10100', '11000', '10100', '10010', '10001'), 'L': ('10000', '10000', '10000', '10000', '10000', '10000', '11111'), 'N': ('10001', '11001', '10101', '10011', '10001', '10001', '10001'), 'O': ('01110', '10001', '10001', '10001', '10001', '10001', '01110'), 'P': ('11110', '10001', '10001', '11110', '10000', '10000', '10000'), 'Q': ('01110', '10001', '10001', '10001', '10101', '10010', '01101'), 'R': ('11110', '10001', '10001', '11110', '10100', '10010', '10001'), 'S': ('01111', '10000', '10000', '01110', '00001', '00001', '11110'), 'T': ('11111', '00100', '00100', '00100', '00100', '00100', '00100'), 'U': ('10001', '10001', '10001', '10001', '10001', '10001', '01110'), 'X': ('10001', '10001', '01010', '00100', '01010', '10001', '10001'), 'Y': ('10001', '10001', '01010', '00100', '00100', '00100', '00100'), 'Z': ('11111', '00001', '00010', '00100', '01000', '10000', '11111'), 'a': ('00000', '00000', '01110', '00001', '01111', '10001', '01111'), 'b': ('10000', '10000', '11110', '10001', '10001', '10001', '11110'), 'f': ('00110', '01001', '01000', '11100', '01000', '01000', '01000'), 'g': ('00000', '00000', '01111', '10001', '01111', '00001', '01110'), 'h': ('10000', '10000', '10110', '11001', '10001', '10001', '10001'), 'i': ('00100', '00000', '01100', '00100', '00100', '00100', '01110'), 'j': ('00010', '00000', '00110', '00010', '00010', '10010', '01100'), 'q': ('00000', '00000', '01111', '10001', '01111', '00001', '00001'), 'r': ('00000', '00000', '10110', '11001', '10000', '10000', '10000'), 'v': ('00000', '00000', '10001', '10001', '10001', '01010', '00100'), 'w': ('00000', '00000', '10001', '10001', '10101', '10101', '01010'), 'x': ('00000', '00000', '10001', '01010', '00100', '01010', '10001'), 'y': ('00000', '00000', '10001', '10001', '01111', '00001', '01110'), '/': ('00001', '00010', '00010', '00100', '01000', '01000', '10000'), ',': ('00000', '00000', '00000', '00000', '01100', '01100', '01000'), ':': ('00000', '01100', '01100', '00000', '01100', '01100', '00000'), '(': ('00010', '00100', '01000', '01000', '01000', '00100', '00010'), ')': ('01000', '00100', '00010', '00010', '00010', '00100', '01000'), '_': ('00000', '00000', '00000', '00000', '00000', '00000', '11111')}


def draw_text(put, text, x, y, rgb, scale=1):
    """Stamp a string into a raster. Returns the width used."""
    cx = x
    for ch in text:
        rows = GLYPHS.get(ch)
        if rows is None:
            cx += 6 * scale
            continue
        for ry, row in enumerate(rows):
            for rx, bit in enumerate(row):
                if bit == "1":
                    for sy in range(scale):
                        for sx in range(scale):
                            put(cx + rx * scale + sx, y + ry * scale + sy, rgb)
        cx += 6 * scale
    return cx - x


def text_width(text, scale=1):
    return len(text) * 6 * scale


# A TDS graticule is ten divisions across by eight down, and they are
# square on the instrument's own screen. Keeping them square here means
# fitting the largest 10:8 rectangle inside whatever space there is and
# centring it, rather than stretching the divisions to the shape of the
# window.
DIVS_X, DIVS_Y = 10, 8
# How many samples an instrument draws to one horizontal division. Fixed
# on this family: ten divisions is always 500 points, and a longer record
# extends past the edges of the screen rather than being squeezed into
# it. Measured on a 784D against its own WFID.
POINTS_PER_DIV = 50


MANTISSAS = (1.0, 2.0, 5.0)


def step_125(value, direction):
    """The next 1-2-5 value above or below this one.

    What the knobs on the instrument do: seconds and volts a division
    go 1, 2, 5, 10, 20, 50 and never land on 3.7. A zoom that steps the
    same way gives a reading somebody can compare against the scope,
    instead of one that has to be read digit by digit.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return value
    if value <= 0:
        return value
    power = int(math.floor(math.log10(value)))
    ladder = sorted(m * 10.0 ** e for e in (power - 1, power, power + 1)
                    for m in MANTISSAS)
    room = abs(value) * 1e-9
    if direction > 0:
        for rung in ladder:
            if rung > value + room:
                return rung
        return ladder[-1]
    for rung in reversed(ladder):
        if rung < value - room:
            return rung
    return ladder[0]


class PlotView(object):
    """Which part of a set of records is on the graticule, and at what
    scale.

    Held in the instruments' own units rather than in pixels: a
    horizontal window measured in seconds, and a vertical scale given as
    a magnification of whatever each trace's own volts a division is.
    Both survive the window being resized, and both mean the same thing
    to the canvas and to the PNG export, which is what keeps a saved
    picture the picture that was on screen.

    Seconds rather than sample numbers because several traces can be on
    the graticule at once and they need not be the same length or the
    same sample interval - a channel and a reference stored an hour ago
    are two different records. Time is the one thing they share.

    Vertically each trace keeps its own scale, exactly as it has on the
    instrument, and the zoom is a magnification applied to all of them.
    Two channels at 100 mV and 5 V a division look on this graticule the
    way they look on the scope.

    Zooming steps the per-division reading along the 1-2-5 ladder, the
    way the instrument's own knobs do, so the number under the graticule
    is always one a person could dial up on the scope.
    """

    MIN_POINTS = 10.0                   # one to a division, and no fewer

    def __init__(self, waves=()):
        self.reset(waves)

    def reset(self, waves=None):
        """The whole of every record, at the instruments' own scales."""
        if waves is not None:
            self.measure(waves)
        self.first = self.full_first
        self.span = self.full_span
        self.zoom = 1.0                 # 1 is the instrument's own scale
        self.offset = 0.0               # divisions every trace is lowered
        # And how far each one has been moved on its own, by name. A
        # scope's channels are moved apart one at a time; a single
        # offset for all of them cannot do that.
        self.shifts = {}
        return self

    def measure(self, waves):
        """Work out the time the whole set covers, and the finest step."""
        waves = [w for w in ([waves] if hasattr(waves, "levels") else waves)
                 if w is not None]
        spans = []
        steps = []
        for wave in waves:
            points = wave.points()
            if len(points) > 1:
                spans.append((points[0][0], points[-1][0]))
                steps.append(abs(wave.number("XINCR", 1.0)) or 1.0)
        self.full_first = min(a for a, _b in spans) if spans else 0.0
        last = max(b for _a, b in spans) if spans else 1.0
        self.full_span = max(1e-15, last - self.full_first)
        self.step = min(steps) if steps else 1.0
        # The trace the readings describe: with several on the graticule
        # only one number can be shown, and it is the first one's, the
        # way a scope shows the selected channel's.
        self.base_vdiv = waves[0].volts_per_div if waves else 1.0
        self.yunit = waves[0].yunit if waves else "V"
        self.xunit = waves[0].xunit if waves else "s"
        return self

    # -- what it is showing ------------------------------------------

    @property
    def seconds_per_div(self):
        return self.span / float(DIVS_X)

    @property
    def volts_per_div(self):
        return self.base_vdiv / (self.zoom or 1.0)

    @property
    def across_whole(self):
        """Is the window the whole of the time every record covers?"""
        return (abs(self.first - self.full_first) < self.full_span * 1e-9
                and abs(self.span - self.full_span) < self.full_span * 1e-9)

    @property
    def whole(self):
        """Is this the whole of every record, at their own scales?"""
        return (self.across_whole
                and abs(self.zoom - 1.0) < 1e-9 and self.offset == 0
                and not any(self.shifts.values()))

    def remeasure(self, waves):
        """Take in a changed set of traces without throwing the view away.

        Adding a trace changes what "the whole record" means: a
        reference stored at a slower timebase reaches further than the
        channel beside it, and a window measured against the old set
        goes on describing traces that are no longer there. That is what
        left the strip above the plot showing a whole record while the
        graticule showed a tenth of one.

        A view that was showing everything goes on showing everything.
        One that was zoomed in stays where it was, clamped into the new
        bounds - somebody who has spent a minute finding a glitch should
        not lose it because a second trace arrived.
        """
        was_whole = self.across_whole
        self.measure(waves)
        if was_whole:
            self.first = self.full_first
            self.span = self.full_span
        return self.clamp()

    def fractions(self):
        """(start, end) as fractions of the whole, for a scrollbar."""
        reach = self.full_span or 1.0
        return (max(0.0, (self.first - self.full_first) / reach),
                min(1.0, (self.first + self.span - self.full_first) / reach))

    # -- where things land on a canvas -------------------------------

    def x_of_time(self, seconds, left, right):
        return left + (seconds - self.first) / self.span * (right - left)

    def divisions_of_level(self, wave, level):
        """How far above the centre line a sample sits, in divisions."""
        per_div = wave.counts_per_div or 1.0
        return (level / per_div * self.zoom - self.offset
                + self.shifts.get(getattr(wave, "source", None), 0.0))

    def y_of_level(self, wave, level, top, bottom):
        per_y = (bottom - top) / float(DIVS_Y)
        return (top + bottom) / 2.0 - self.divisions_of_level(wave,
                                                              level) * per_y

    def y_of_volts(self, wave, volts, top, bottom):
        """Where a voltage falls for this trace.

        volts = (level - YOFF) * YMULT + YZERO, turned round. This is
        what puts a channel marker beside the trace's own zero rather
        than in the middle of the graticule, which is only the same
        place when the channel is at position zero.
        """
        ymult = wave.number("YMULT", 1.0) or 1.0
        level = (volts - wave.number("YZERO")) / ymult + wave.number("YOFF")
        return self.y_of_level(wave, level, top, bottom)

    # -- moving it ---------------------------------------------------

    def clamp(self):
        """Keep the window over the records and on a drawable scale."""
        self.span = min(self.full_span,
                        max(self.MIN_POINTS * self.step, self.span))
        self.first = min(max(self.full_first, self.first),
                         self.full_first + self.full_span - self.span)
        # Far enough either way to be useful and no further: a hundred
        # divisions of a record that is only 10.24 divisions tall is
        # nothing but background.
        self.zoom = min(1000.0, max(0.01, self.zoom))
        self.offset = min(200.0, max(-200.0, self.offset))
        for name in list(self.shifts):
            self.shifts[name] = min(200.0, max(-200.0, self.shifts[name]))
        return self

    def zoom_time(self, direction, at=0.5):
        """One step of the ladder, keeping `at` of the way across fixed."""
        at = min(1.0, max(0.0, at))
        anchor = self.first + self.span * at
        wanted = step_125(self.seconds_per_div, -direction)
        self.span = min(self.full_span,
                        max(self.MIN_POINTS * self.step, wanted * DIVS_X))
        self.first = anchor - self.span * at
        return self.clamp()

    def zoom_volts(self, direction, at=0.5):
        """The same for the vertical scale, about a point down the frame.

        Whatever is under the pointer stays under it. A trace sits at
        `level / counts a division * zoom - offset` divisions above the
        centre line, so holding a point still is a matter of solving
        that for the offset at the new magnification.
        """
        at = min(1.0, max(0.0, at))
        above = (0.5 - at) * DIVS_Y     # where the pointer is, in divisions
        held = (above + self.offset) / (self.zoom or 1.0)
        wanted = step_125(self.volts_per_div, -direction)
        if wanted:
            self.zoom = self.base_vdiv / wanted
        self.clamp()
        self.offset = held * self.zoom - above
        return self.clamp()

    def pan(self, seconds=0.0, divisions=0.0, only=None):
        """Move the window. Positive `divisions` moves the traces up.

        `only` names the traces to move; without it the whole view
        moves, which is what a horizontal drag does either way.
        """
        self.first += seconds
        if divisions:
            if only:
                for name in only:
                    self.shifts[name] = (self.shifts.get(name, 0.0)
                                         + divisions)
            else:
                self.offset -= divisions
        return self.clamp()

    def divisions_off_centre(self, wave):
        """How far this trace's zero volts is above the centre line."""
        ymult = wave.number("YMULT", 1.0) or 1.0
        level = (0.0 - wave.number("YZERO")) / ymult + wave.number("YOFF")
        return self.divisions_of_level(wave, level)

    # Half of one of the fine pips along the centre cross, which is the
    # smallest thing the graticule draws and so the smallest distance a
    # person can be said to be aiming at. Written as the arithmetic
    # rather than as 0.1 so that it follows the graticule if that ever
    # changes.
    SNAP_DIVS = 0.5 / PIPS_PER_DIV

    def snap_middle(self, wave, tolerance=None):
        """Pull one trace's zero onto the centre line when it is close.

        A position knob has no detent, but somebody dragging a trace
        with a mouse is nearly always aiming at the middle, and landing
        a fraction of a division out looks like a slip rather than a
        choice. Close enough is half a minor division - near enough to
        feel like help and far enough from the rest of the graticule to
        stay out of the way. Says whether it moved.

        Whoever calls this must work the drag out from where it started
        rather than from the last event: a snap applied to a running
        total traps the trace inside the tolerance for ever, because a
        slow drag never moves more than a pixel or two between events.
        """
        if tolerance is None:
            tolerance = self.SNAP_DIVS
        away = self.divisions_off_centre(wave)
        if away == 0.0 or abs(away) > tolerance:
            return False
        name = getattr(wave, "source", None)
        self.shifts[name] = self.shifts.get(name, 0.0) - away
        return True

    def show_to(self, first, last, top=None, bottom=None):
        """Zoom to a dragged rectangle, snapped onto the ladder.

        The rectangle is given in seconds across and in divisions above
        the centre line - what the pointer was over, not pixels.

        Snapped outwards, so everything inside the rectangle is still
        inside the graticule afterwards: a zoom that cut off the edge of
        what was asked for would be worse than one that shows a little
        extra.
        """
        first, last = min(first, last), max(first, last)
        want = max(self.MIN_POINTS * self.step, last - first)
        self.span = min(self.full_span, self.snap(want / float(DIVS_X),
                                                  want) * DIVS_X)
        self.first = (first + last) / 2.0 - self.span / 2.0
        if top is not None and bottom is not None and top != bottom:
            high, low = max(top, bottom), min(top, bottom)
            # The scale that fits those divisions into the eight there
            # are, as volts a division so it can be put on the ladder.
            needed = self.volts_per_div * max(1e-9, high - low) / DIVS_Y
            rung = self.snap_up(needed)
            middle = ((high + low) / 2.0 + self.offset) / (self.zoom or 1.0)
            if rung:
                self.zoom = self.base_vdiv / rung
            self.clamp()
            self.offset = middle * self.zoom
        return self.clamp()

    def snap_up(self, value):
        """The smallest 1-2-5 rung that is not below this."""
        if value <= 0:
            return value
        rung = step_125(value, 1)
        for _try in range(4):
            if step_125(rung, -1) >= value:
                rung = step_125(rung, -1)
            else:
                break
        return rung

    def snap(self, value, want_at_least):
        """The smallest 1-2-5 rung whose ten divisions still cover it."""
        rung = step_125(value, 1) if value > 0 else value
        for _try in range(4):
            if step_125(rung, -1) * DIVS_X >= want_at_least:
                rung = step_125(rung, -1)
            else:
                break
        return rung

    def stretch_to(self, moved, held, hold="first"):
        """Resize the window to span these two moments.

        For dragging an edge of the strip above the plot: `moved` is
        where the edge has been taken to, `held` is the other end, and
        `hold` says which of them the answer has to keep. The span
        still lands on the 1-2-5 ladder the way every other way of
        changing it does - the reading under the graticule has to be a
        number somebody could dial up on the instrument - so the edge
        being dragged settles onto a rung and the other one stays put.
        """
        lo, hi = min(moved, held), max(moved, held)
        want = max(self.MIN_POINTS * self.step, hi - lo)
        self.span = min(self.full_span,
                        self.snap(want / float(DIVS_X), want) * DIVS_X)
        self.first = lo if hold == "first" else hi - self.span
        return self.clamp()

    def scroll_to(self, fraction):
        """Put the left edge here, as a fraction of the whole."""
        self.first = self.full_first + fraction * self.full_span
        return self.clamp()


# Room kept clear to the left of the graticule for the channel markers.
# Wide enough for the longest source name on the range - MATH1 - at both
# the window's font and the raster font the PNG draws with. Without it,
# a tall narrow window leaves only the 34-pixel margin and a marker
# either overlaps the graticule or falls off the edge of the canvas.
MARKER_ROOM = 64


def plot_frame(width, height, pad=34, room=MARKER_ROOM, band=0):
    """The drawing area: the largest 10:8 rectangle that fits.

    Returns (left, top, right, bottom). Whatever is left over becomes a
    margin, so the divisions are square at any window shape - with
    `room` kept clear on the left for the channel markers, which sit
    outside the graticule where they cover no signal.

    Centred in both directions. `room` is taken out of the width before
    the graticule is measured, so the leftover is always at least
    `pad + room / 2` a side and centring never eats into the markers'
    room - the max() is a guard for a caller that pads less than that,
    not something the sizes here reach.
    """
    avail_w = max(1, width - 2 * pad - room)
    avail_h = max(1, height - 2 * pad - band)
    per = min(avail_w / float(DIVS_X), avail_h / float(DIVS_Y))
    inner_w = per * DIVS_X
    inner_h = per * DIVS_Y
    left = max(room, (width - inner_w) / 2.0)
    top = band + (height - band - inner_h) / 2.0
    return left, top, left + inner_w, top + inner_h


def graticule(width, height, pad=34, room=MARKER_ROOM, band=0):
    """Every line of the graticule, as (element, x0, y0, x1, y1, thick).

    One description of the drawing, used by the window and by the PNG
    export, so the two cannot disagree about what a graticule looks
    like. `element` is a key in ELEMENTS, so the caller only has to
    know how to draw a line in a colour.

    The arrangement is the instrument's own: division lines, a heavier
    cross through the centre with pips along it at a fifth of a
    division, and a border round the outside.

    `room` is the space kept clear on the left, and has to be the same
    number the caller gave plot_frame or the lines land somewhere the
    caller is not expecting. The waveform tab keeps room for channel
    markers; the mask editor has none to keep.
    """
    left, top, right, bottom = plot_frame(width, height, pad, room, band)
    per_x = (right - left) / float(DIVS_X)
    per_y = (bottom - top) / float(DIVS_Y)
    mid_x = left + per_x * (DIVS_X / 2.0)
    mid_y = top + per_y * (DIVS_Y / 2.0)
    out = []

    for i in range(1, DIVS_X):          # the division lines
        x = left + per_x * i
        if abs(x - mid_x) > 0.5:        # the centre is drawn as major
            out.append(("graticule", x, top, x, bottom, 1))
    for i in range(1, DIVS_Y):
        y = top + per_y * i
        if abs(y - mid_y) > 0.5:
            out.append(("graticule", left, y, right, y, 1))

    out.append(("major", mid_x, top, mid_x, bottom, 2))
    out.append(("major", left, mid_y, right, mid_y, 2))

    # Pips along the centre cross, at a fifth of a division, skipping the
    # ones that land on a division line - there is already a line there.
    tick_y = per_y * PIP_LENGTH
    tick_x = per_x * PIP_LENGTH
    for i in range(1, DIVS_X * PIPS_PER_DIV):
        if i % PIPS_PER_DIV == 0:
            continue
        x = left + per_x * i / float(PIPS_PER_DIV)
        out.append(("pips", x, mid_y - tick_y, x, mid_y + tick_y, 1))
    for i in range(1, DIVS_Y * PIPS_PER_DIV):
        if i % PIPS_PER_DIV == 0:
            continue
        y = top + per_y * i / float(PIPS_PER_DIV)
        out.append(("pips", mid_x - tick_x, y, mid_x + tick_x, y, 1))

    out.append(("border", left, top, right, top, 1))
    out.append(("border", left, bottom, right, bottom, 1))
    out.append(("border", left, top, left, bottom, 1))
    out.append(("border", right, top, right, bottom, 1))
    return out


def plot_geometry(wave, width, height, pad=34, view=None,
                  room=MARKER_ROOM, band=0):
    """Where each sample of one trace lands on a canvas of this size.

    Shared by the on-screen preview and the PNG export so that what is
    saved is what was seen, rather than two drawings that drift apart.

    The scale is the instrument's, not one chosen to fit: a sample is
    placed at its own count divided by the counts in a division, which
    is exactly where the instrument puts it on its own screen. So a
    small signal on a large volts-per-division setting draws as a small
    signal, the way it looks on the scope, rather than being stretched
    to fill the graticule and looking like something else.

    `view` narrows that to part of the record at some other scale, and
    is what several traces share so they line up in time - see PlotView.
    Without one, the whole of this record at its own scale.

    A sample beyond the eight divisions the graticule shows is held at
    the edge. The digitiser reaches 5.12 divisions either side of centre,
    so this is reachable, and drawing it outside the frame would put
    trace over the labels and the margin.

    More samples than there are pixels are drawn as an envelope - the
    lowest and highest in each column - rather than as a line through
    every one of them. Fifty thousand points through a Tk canvas is
    several seconds an update, and the picture is the same: a column of
    pixels can only show the range that fell in it.

    `room` is the margin the graticule is drawn with and has to be the
    same number the caller drew it with. The mask editor keeps 8 pixels
    where the waveform tab keeps 64 for its channel markers, and a trace
    placed against the wider frame landed two minor divisions to the
    right of the graticule it was supposed to be under.
    """
    left, top, right, bottom = plot_frame(width, height, pad, room, band)
    if view is None:
        view = PlotView([wave])
    levels = wave.levels()
    if not levels:
        return [], (0, 0, 0, 0)
    inner_w = right - left
    xincr = wave.number("XINCR", 1.0) or 1.0
    xzero = wave.number("XZERO")
    ptoff = wave.number("PT_OFF")

    def index_at(seconds):
        return (seconds - xzero) / xincr + ptoff

    def screen_y(level):
        return min(bottom, max(top, view.y_of_level(wave, level, top,
                                                    bottom)))

    # Only the part of this record that falls inside the window, and one
    # sample either side so the line runs to the edges rather than
    # stopping short of them.
    lo = max(0, int(math.floor(index_at(view.first))) - 1)
    hi = min(len(levels), int(math.ceil(index_at(view.first + view.span))) + 2)
    if hi <= lo:
        return [], (view.first, view.first + view.span, 0, 0)

    def x_at(index):
        return view.x_of_time((index - ptoff) * xincr + xzero, left, right)

    xy = []
    if hi - lo > 2 * inner_w and inner_w >= 2:
        columns = int(inner_w)
        for column in range(columns):
            a = lo + int((hi - lo) * column / float(columns))
            b = lo + int((hi - lo) * (column + 1) / float(columns))
            chunk = levels[a:max(b, a + 1)]
            if not chunk:
                continue
            x = x_at((a + b) / 2.0)
            xy.append((x, screen_y(max(chunk))))
            xy.append((x, screen_y(min(chunk))))
    else:
        for i in range(lo, hi):
            xy.append((x_at(i), screen_y(levels[i])))
    # Cut off at the borders. Without this a zoomed-in trace runs a
    # sample's width past both of them, across the margin and over the
    # markers.
    xy = clip_x(xy, left, right)

    # What the edges of the graticule mean, for anything that wants to
    # label them: the same volts = (raw - YOFF) * YMULT + YZERO asked at
    # the top and bottom of the eight divisions on show, and the two
    # ends of the window in seconds.
    ymult = wave.number("YMULT", 1.0)
    yoff = wave.number("YOFF")
    yzero = wave.number("YZERO")
    per_div = wave.counts_per_div or 1.0
    reach = DIVS_Y / 2.0
    vhi = (((reach + view.offset) / (view.zoom or 1.0)) * per_div
           - yoff) * ymult + yzero
    vlo = (((-reach + view.offset) / (view.zoom or 1.0)) * per_div
           - yoff) * ymult + yzero
    if vhi < vlo:                       # an inverted channel
        vlo, vhi = vhi, vlo
    return xy, (view.first, view.first + view.span, vlo, vhi)


def clip_x(points, left, right):
    """A polyline cut off at two vertical lines.

    The drawing runs one sample past each edge of the window so the
    trace reaches the borders rather than stopping short of them, and
    zoomed in that sample can be a long way outside - far enough to draw
    over the margin and the markers. Cutting the line at the border
    keeps it inside without moving anything that was already in view:
    the crossing point is worked out from the two samples either side of
    it, so the slope at the edge is the trace's own.
    """
    out = []
    for i, (x, y) in enumerate(points):
        inside = left <= x <= right
        if i:
            px, py = points[i - 1]
            was = left <= px <= right
            for edge in (left, right):
                if (px < edge < x) or (x < edge < px):
                    span = x - px
                    out.append((edge, py + (y - py) * (edge - px) / span
                                if span else y))
            if not inside and not was:
                continue
        if inside:
            out.append((x, y))
    return out


def marker_place(placed, left, zero, width, height=13, edge=1,
                 right=None, room=None):
    """Where to put a marker so it clears the ones already there.

    Three places, in order of preference. The left-hand margin first,
    stepping further out for each one already at that height, because
    that is off the signal. Then the margin on the other side of the
    graticule, which is also off the signal. Only when both are full
    does one go inside the graticule, stepping right past anything
    already there.

    Worked out in one go rather than by nudging until it fits. Nudging
    is the obvious way to write it and it does not terminate: with two
    markers already stacked, stepping left off one lands on the other,
    stepping right off that lands back on the first, and the loop
    swings between them for ever. It hung the program for exactly as
    long as it took to find.
    """
    same = [(a, b) for a, y, b in placed if abs(zero - y) < height + 1]
    if not same:
        return max(edge, left - width - 3)
    outward = min(a for a, _b in same) - width - 3
    if outward >= edge:
        return outward
    if right is not None:
        beyond = [b for a, b in same if a >= right]
        start = (max(beyond) + 3) if beyond else (right + 3)
        if room is None or start + width <= room - edge:
            return start
    inside = [b for a, b in same if left <= a < (right if right is not None
                                                 else a + 1)]
    return (max(inside) + 3) if inside else left + 1


def marker_facing(x0, right=None):
    """Which way a marker at `x0` should point: towards the graticule.

    A marker in the left-hand margin points right, at the trace it
    belongs to. One that had to go to the margin on the other side has
    the trace on its left, so it points left. A marker pointing away
    from the thing it labels reads as though it belonged to whatever is
    outside the frame, which is nothing.
    """
    return -1 if (right is not None and x0 >= right) else 1


def marker_shape(x, y, width, height=13, point=6, facing=1):
    """The outline of a channel marker: a box with one pointed side.

    Returned as a flat list of coordinates, which is what both a canvas
    polygon and the raster fill below want. `x` is its left edge and `y`
    is the point - the height of zero volts for that trace, which is
    what the marker is there to show. `facing` is 1 for a point on the
    right and -1 for one on the left; see `marker_facing`.
    """
    half = height / 2.0
    if facing < 0:
        return [x + width, y - half,
                x + point, y - half,
                x, y,
                x + point, y + half,
                x + width, y + half]
    return [x, y - half,
            x + width - point, y - half,
            x + width, y,
            x + width - point, y + half,
            x, y + half]


def _chunk(tag, data):
    body = tag + data
    return (struct.pack(">I", len(data)) + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))


def _filtered(rows, stride):
    """Scanlines, each with the cheaper of no filter and the Up filter.

    Up subtracts the row above, so the large stretches of unchanged
    background in a plot become runs of zeros, which deflate to almost
    nothing. Chosen per row by the usual heuristic - the lower sum of
    absolute byte values wins - rather than fixed, because the rows
    carrying the trace are not like the rows that do not.
    """
    out = bytearray()
    previous = bytes(stride)
    for row in rows:
        up = bytes((row[i] - previous[i]) & 0xFF for i in range(stride))
        if sum(min(b, 256 - b) for b in up) < sum(min(b, 256 - b)
                                                  for b in row):
            out += b"\x02" + up
        else:
            out += b"\x00" + bytes(row)
        previous = row
    return bytes(out)


def encode_png(rgba, width, height):
    """RGBA bytes to a PNG, palettised when the image allows it.

    A plot of a trace uses about four colours - background, graticule,
    trace, labels - and writing four colours as 32-bit RGBA spends eight
    times the space the picture needs. Anything with 256 colours or fewer
    is written as an indexed image at the smallest bit depth that holds
    the palette, which for a plot is 2 bits a pixel.

    Falls back to RGBA if the image really does have more than 256
    colours, so this cannot be the thing that breaks an export.
    """
    palette, index = [], {}
    for i in range(0, len(rgba), 4):
        key = bytes(rgba[i:i + 4])
        if key not in index:
            if len(palette) == 256:
                index = None
                break
            index[key] = len(palette)
            palette.append(key)

    if index is None:                      # too many colours to index
        rows = [rgba[y * width * 4:(y + 1) * width * 4]
                for y in range(height)]
        body = _filtered(rows, width * 4)
        head = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", head)
                + _chunk(b"IDAT", zlib.compress(body, 9))
                + _chunk(b"IEND", b""))

    depth = 1 if len(palette) <= 2 else (2 if len(palette) <= 4 else
                                         (4 if len(palette) <= 16 else 8))
    per_byte = 8 // depth
    stride = (width + per_byte - 1) // per_byte
    rows = []
    for y in range(height):
        row = bytearray(stride)
        base = y * width * 4
        for x in range(width):
            value = index[bytes(rgba[base + x * 4:base + x * 4 + 4])]
            slot = x // per_byte
            shift = 8 - depth * (x % per_byte + 1)
            row[slot] |= value << shift
        rows.append(bytes(row))

    plte = b"".join(bytes(c[:3]) for c in palette)
    out = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                         depth, 3, 0, 0, 0))
           + _chunk(b"PLTE", plte))
    # Only carry transparency if some colour actually needs it.
    alphas = [c[3] for c in palette]
    if any(a != 255 for a in alphas):
        out += _chunk(b"tRNS", bytes(alphas))
    return (out + _chunk(b"IDAT", zlib.compress(_filtered(rows, stride), 9))
            + _chunk(b"IEND", b""))


DENSITY_INDEX = (16, 31)


def rescale_screen(pixels, width, height, wide, tall):
    """A captured screen at another size, still as palette indexes.

    The DPO palette codes density as HUE - blue, cyan, green, yellow,
    red - so an average taken over the colours lands between two hues
    and means nothing. Averaging is done along the palette index
    instead, which is the density itself, and only where every pixel
    involved is on that ramp: blending the graticule into a trace mixes
    furniture with signal.

    Enlarging interpolates. At a magnification each destination pixel
    covers one source pixel, so a block average degenerates to nearest
    neighbour and only interpolation adds anything - measured, an
    average and a nearest came out byte for byte identical at 700x560.

    Reducing keeps the densest pixel of each block rather than the
    mean. A mean drags a rare hot spot down towards its cold
    neighbours and thin traces thin out to nothing; losing a hit is the
    one error a mask picture must not make.
    """
    wide, tall = max(1, int(wide)), max(1, int(tall))
    low, high = DENSITY_INDEX
    out = bytearray(wide * tall)
    on_ramp = [low <= i <= high for i in range(256)]
    if wide >= width and tall >= height:
        # The column each output pixel comes from is the same on every
        # row, so it is worked out once: at a full screen that is half a
        # million float divisions saved, and a resize of the editor
        # rebuilds this picture on every drag.
        columns = []
        for x in range(wide):
            fx = (x + 0.5) * width / wide - 0.5
            x0 = max(0, min(width - 1, int(fx)))
            columns.append((x0, min(width - 1, x0 + 1),
                            max(0.0, min(1.0, fx - x0))))
        for y in range(tall):
            fy = (y + 0.5) * height / tall - 0.5
            y0 = max(0, min(height - 1, int(fy)))
            wy = max(0.0, min(1.0, fy - y0))
            up = pixels[y0 * width:(y0 + 1) * width]
            down = pixels[min(height - 1, y0 + 1) * width:
                          (min(height - 1, y0 + 1) + 1) * width]
            row = y * wide
            for x, (x0, x1, wx) in enumerate(columns):
                a, b, c, d = up[x0], up[x1], down[x0], down[x1]
                if on_ramp[a] and on_ramp[b] and on_ramp[c] and on_ramp[d]:
                    top = a + (b - a) * wx
                    out[row + x] = int(round(
                        top + (c + (d - c) * wx - top) * wy))
                else:
                    out[row + x] = a if (wx < 0.5 and wy < 0.5) else (
                        b if wy < 0.5 else (c if wx < 0.5 else d))
        return bytes(out)
    for y in range(tall):
        y0 = y * height // tall
        y1 = max(y0 + 1, (y + 1) * height // tall)
        row = y * wide
        for x in range(wide):
            x0 = x * width // wide
            x1 = max(x0 + 1, (x + 1) * width // wide)
            best, other = -1, None
            for sy in range(y0, y1):
                at = sy * width
                for sx in range(x0, x1):
                    got = pixels[at + sx]
                    if on_ramp[got]:
                        if got > best:
                            best = got
                    elif other is None:
                        other = got
            out[row + x] = best if best >= 0 else (other or 0)
    return bytes(out)


def scaled_indexed(pixels, palette, width, height, wide, tall):
    """The same indexed picture at another size, as a PNG.

    Resized by rescale_screen, so a DPO screen keeps its density coding
    and everything else keeps its edges. Done here rather than with an
    imaging library because this program has none and should not grow
    one to resize one picture.
    """
    return encode_png_indexed(
        rescale_screen(pixels, width, height, wide, tall), palette,
        max(1, int(wide)), max(1, int(tall)))


def encode_png_indexed(pixels, palette, width, height):
    """An indexed PNG from pixels that are already indexes.

    An instrument's screen arrives palettised - a BMP or PCX with 256
    entries - and turning it into RGBA so that encode_png can turn it
    back again costs a megabyte and several seconds for no gain. The
    palette is squeezed to the entries the image actually uses first,
    which on a scope screen takes 256 down to a couple of dozen and the
    depth with it.
    """
    used = sorted(set(pixels))
    remap = dict((old, new) for new, old in enumerate(used))
    colours = [tuple(palette[i])[:3] if i < len(palette) else (0, 0, 0)
               for i in used]
    depth = 1 if len(colours) <= 2 else (2 if len(colours) <= 4 else
                                         (4 if len(colours) <= 16 else 8))
    per_byte = 8 // depth
    stride = (width + per_byte - 1) // per_byte
    table = bytes(remap.get(i, 0) for i in range(256))
    rows = []
    for y in range(height):
        line = pixels[y * width:(y + 1) * width]
        if depth == 8:
            rows.append(line.translate(table))
            continue
        row = bytearray(stride)
        for x in range(width):
            row[x // per_byte] |= (table[line[x]]
                                   << (8 - depth * (x % per_byte + 1)))
        rows.append(bytes(row))
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                          depth, 3, 0, 0, 0))
            + _chunk(b"PLTE", b"".join(bytes(c) for c in colours))
            + _chunk(b"IDAT", zlib.compress(_filtered(rows, stride), 9))
            + _chunk(b"IEND", b""))


def png_pixels(data):
    """Read back a PNG this module wrote. Returns [(r, g, b), ...].

    Here so that a test can ask the only question that matters about an
    exported picture - are these the pixels that were captured? - which
    inspecting the encoder's own variables cannot answer. Handles the
    indexed and RGBA images written above and all five scanline
    filters; returns None for anything else rather than guessing.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    at, palette, idat = 8, [], bytearray()
    width = height = depth = kind = 0
    while at + 8 <= len(data):
        length = struct.unpack_from(">I", data, at)[0]
        tag = data[at + 4:at + 8]
        body = data[at + 8:at + 8 + length]
        at += 12 + length
        if tag == b"IHDR":
            width, height, depth, kind = struct.unpack_from(">IIBB", body, 0)
        elif tag == b"PLTE":
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    if kind not in (3, 6):
        return None
    raw = zlib.decompress(bytes(idat))
    channels = 4 if kind == 6 else 1
    stride = (width * depth * channels + 7) // 8
    step = max(1, depth * channels // 8)
    out, previous, at = [], bytearray(stride), 0
    for _y in range(height):
        if at >= len(raw):
            return None
        filt, line = raw[at], bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            left = line[i - step] if i >= step else 0
            up = previous[i]
            upleft = previous[i - step] if i >= step else 0
            if filt == 1:
                line[i] = (line[i] + left) & 0xFF
            elif filt == 2:
                line[i] = (line[i] + up) & 0xFF
            elif filt == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif filt == 4:
                p = left + up - upleft
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
                near = left if (pa <= pb and pa <= pc) else (
                    up if pb <= pc else upleft)
                line[i] = (line[i] + near) & 0xFF
        previous = line
        if kind == 6:
            out += [tuple(line[x * 4:x * 4 + 3]) for x in range(width)]
            continue
        per_byte = 8 // depth
        mask = (1 << depth) - 1
        for x in range(width):
            byte = line[x // per_byte]
            shift = 8 - depth - (x % per_byte) * depth
            index = (byte >> shift) & mask
            out.append(palette[index] if index < len(palette) else (0, 0, 0))
    return out


def png_height_for(width, pad=34, room=MARKER_ROOM):
    """The height that makes a 10:8 graticule fill an image this wide.

    The room kept for the markers comes off the width first, or the
    graticule would be taller than the picture it has to fit in.
    """
    inner = max(1, width - 2 * pad - room)
    return int(round(inner * DIVS_Y / float(DIVS_X)) + 2 * pad)


def bit_phase(crossings, bit):
    """Where a bit boundary sits, in seconds, from the crossings.

    Folding them onto a single bit and taking the mean *angle* rather
    than a mean time is the point: a plain average of boundaries at 1%
    and 99% of a bit gives 50%, the middle, which is the one place
    they are not.
    """
    if not crossings:
        return None
    turns = [2.0 * math.pi * (t % bit) / bit for t in crossings]
    phase = math.atan2(sum(math.sin(a) for a in turns) / len(turns),
                       sum(math.cos(a) for a in turns) / len(turns))
    return phase / (2.0 * math.pi) * bit


def phase_locked(crossings, bit, tight=0.8):
    """Is the data standing still against whatever is triggering it?

    One crossing from each of several records, folded onto a bit. A
    clock at the bit rate is only worth triggering on if the data is
    locked to it, and two sources set to exactly commensurate
    frequencies are not necessarily locked at all: measured on a
    generator driving CAN data from an arb and a 1 MHz square from the
    other channel, both exact to a microhertz and told to EQPHASE, the
    crossings landed at 22, 43, 94, 53, 98 and 13 percent of a bit on
    six consecutive records. Triggered on a clock like that the eye
    walks across the screen, which on a 784D was 9,555,847 hits over
    548,236 acquisitions - on a signal that gives none triggered on
    itself.

    The measure is the length of the mean unit vector rather than a
    spread, because a phase is an angle: crossings at 1% and 99% of a
    bit are a fiftieth of a bit apart, and their arithmetic mean is
    the one place they never are. One is a phase that never moves,
    zero is one that is anywhere; 0.8 leaves room for a tenth of a bit
    of jitter and still catches a walk.

    Too few records to judge on counts as *not* locked, which is the
    way round the bench insists on. The two answers do not cost the
    same: a clock wrongly trusted smears the eye across the mask -
    5,900,945 hits over 596,288 acquisitions on a USB low speed signal
    that gives none - while a clock wrongly refused costs a delayed
    sweep, which draws a whole eye either way. A screen holding a bit
    and a half often reads back one crossing or none, so this is not a
    rare corner: it is most records.
    """
    if len(crossings) < 4:
        return False
    turns = [2.0 * math.pi * (t % bit) / bit for t in crossings]
    across = sum(math.cos(a) for a in turns) / len(turns)
    up = sum(math.sin(a) for a in turns) / len(turns)
    return math.hypot(across, up) > tight


def crossings_of(wave):
    """Where a trace passes its own middle, in seconds.

    Its own middle rather than zero: a mask is drawn about the signal,
    and a signal riding on an offset still has an eye.

    A record has to swing at least a division to be a signal at all.
    Serial data on a screen holding two or three bits often has no
    transition on it, and then the middle taken here sits in the noise
    and every wiggle crosses it: measured on a 784D, 69 and 266 and
    288 crossings off single records against the one or two a real
    trace gives. Their folded phase is a random number, and one such
    record among eight took a signal whose phase never moved by more
    than three thousandths of a bit and scored it 0.64 - not locked -
    so a good clock was thrown away and the eye put on the delayed
    sweep for nothing.
    """
    spots = wave.points()
    volts = [v for _t, v in spots]
    if not volts or max(volts) - min(volts) <= wave.volts_per_div:
        return []
    mid = (max(volts) + min(volts)) / 2.0
    out = []
    for i in range(1, len(spots)):
        (t0, v0), (t1, v1) = spots[i - 1], spots[i]
        if (v0 < mid) != (v1 < mid) and v1 != v0:
            out.append(t0 + (t1 - t0) * (mid - v0) / (v1 - v0))
    return out


def bit_centre(crossings, bit, wide):
    """Where the trigger goes so the middle of a bit lands mid-screen.

    `crossings` are times relative to the trigger where the trace
    passed its own middle, which on serial data are bit boundaries.
    `bit` is one bit in seconds and `wide` the whole sweep.

    Folding them onto a single bit and taking the mean *angle* rather
    than a mean time is the point: a plain average of boundaries at 1%
    and 99% of a bit gives 50%, the middle, which is the one place
    they are not. Half a bit past the boundary is the middle of a bit,
    and any whole bit from there is the same place - so the answer is
    the one of those nearest the middle of the sweep.
    """
    edge = bit_phase(crossings, bit)
    if edge is None:
        return None
    # Any whole bit from there is the same middle, so the one *nearest
    # the middle of the sweep* is the answer rather than the first one
    # that lands on the sweep at all. Measured on a 784D: a CAN mask,
    # two bits across the graticule, wound to 0.9 us and asked for a
    # trigger position of 5% - the clamp - which put the mask over a
    # crossing rather than an eye and gave 9,544,235 hits over 548,238
    # acquisitions on a signal that passes. The same signal centred
    # this way gives none. USB full speed hid it: one and a half bits
    # across the graticule cannot wind further than a third of the
    # sweep, so it never reached the clamp.
    want = edge + bit / 2.0
    want -= bit * round(want / bit)
    return min(95.0, max(5.0, 50.0 - want / wide * 100.0))


def mask_counts(replies):
    """How many points each POINTSPCNT answer holds.

    A module function rather than a method because the simulator has to
    count the same way the instrument is counted - it swaps the whole
    class out, and two copies of this drifted apart once already.

    An empty segment counts zero rather than one: it answers with a
    pair of zeros, and a pair of zeros is the instrument saying nothing
    is there. But *only* when it is the whole answer. The instrument's
    origin is the upper left, so the top left corner of the graticule
    is 0,0 to it, and a mask with a corner there - which every band
    drawn along the top edge has - was counted one point short and
    reported to the user as an instrument that had kept something else.
    Measured on a 784D: a four-corner band along the top reads back all
    four points and was counted as three.
    """
    out = []
    for said in replies:
        bits = []
        for b in str(said).replace(";", ",").split(","):
            try:
                bits.append(float(b))
            except ValueError:
                pass
        pairs = [(bits[i], bits[i + 1])
                 for i in range(0, len(bits) - 1, 2)]
        if len(pairs) == 1 and pairs[0] == (0.0, 0.0):
            pairs = []
        out.append(len(pairs))
    return out


def plot_png(waves, width=DEFAULT_PNG_WIDTH, height=None,
             colours=None, caption="", view=None, traces=None,
             shapes=None, grid=(0.0, 0.0), verdict=None, behind=None,
             strip=False):
    """The traces as a PNG, drawn the same way the preview draws them.

    `waves` is one waveform or several. `traces` gives a colour for each
    by name, for the instrument's own palette; anything not named there
    is drawn in the scheme's trace colour.

    `caption` is the per-division reading, in the caller's language -
    this module has no gettext and should not have one, so the sentence
    is built where the words are known and handed in.

    `verdict` is (text, colour) for the mask result, stamped in the top
    corner. It goes into the picture rather than being drawn over it in
    the window, because a picture of a mask test that does not say
    whether it passed is a picture somebody has to be told about.

    `behind` is (pixels, palette, width, height) - a screen captured
    off the instrument and cropped to its graticule, for the case where
    there is no waveform to draw at all. See Worker.msk_behind.

    `strip` puts the record strip along the top, the way the window
    does: the whole capture with the part on the graticule outlined. A
    picture of a zoomed-in trace says nothing about where in the record
    it came from without it.
    """
    waves = [waves] if hasattr(waves, "levels") else [w for w in waves if w]
    if height is None:
        height = png_height_for(width)
    if view is None:
        view = PlotView(waves)
    pick = scheme(colours)
    bg = rgb(pick["background"], (18, 22, 28))
    buf = bytearray()
    for _ in range(width * height):
        buf += bytes((bg[0], bg[1], bg[2], 255))

    def put(x, y, rgb):
        x, y = int(x), int(y)
        if 0 <= x < width and 0 <= y < height:
            i = (y * width + x) * 4
            buf[i:i + 3] = bytes(rgb)

    pad = 34
    # The band along the top the strip is drawn in, taken out of the
    # height before the graticule is measured so the graticule stays
    # square and nothing overlaps.
    band = max(24, int(height * 0.09)) if (strip and waves) else 0
    left, top, right, bottom = plot_frame(width, height, pad, band=band)
    # The instrument's own screen, where one was captured because there
    # was no record to read - DPO. Furthest back of everything, scaled
    # from its graticule onto this one, so the eye it accumulated sits
    # under the mask exactly as it does on the glass. Without it a DPO
    # mask test saves as an empty graticule with a verdict on it, which
    # is the one picture nobody can check.
    if behind:
        pixels, palette, shot_w, shot_h = behind
        wide, tall = int(right - left), int(bottom - top)
        if wide > 0 and tall > 0:
            spot = rescale_screen(pixels, shot_w, shot_h, wide, tall)
            paint = [bytes(tuple(c)[:3]) for c in palette]
            for y in range(tall):
                row = y * wide
                for x in range(wide):
                    put(left + x, top + y, paint[spot[row + x]])
    ink = dict((key, rgb(pick[key], (52, 62, 72)))
               for key in ("graticule", "major", "pips", "border"))
    for element, x0, y0, x1, y1, thick in graticule(width, height, pad,
                                                    band=band):
        colour = ink[element]
        for step in range(thick):
            if abs(x1 - x0) >= abs(y1 - y0):        # horizontal-ish
                for x in range(int(round(x0)), int(round(x1)) + 1):
                    put(x, y0 + step, colour)
            else:
                for y in range(int(round(y0)), int(round(y1)) + 1):
                    put(x0 + step, y, colour)

    # The mask editor's picture is this same picture with shapes on it.
    # They arrive as lists of percent points - this module knows nothing
    # about masks and does not need to, since a closed polygon in
    # percent of the graticule is a closed polygon in percent of the
    # graticule.
    def at_percent(x, y):
        return (left + (right - left) * x / 100.0,
                bottom - (bottom - top) * y / 100.0)

    def stroke(x0, y0, x1, y1, colour, thick=1):
        steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for s in range(steps + 1):
            f = s / float(steps)
            for k in range(thick):
                put(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f + k, colour)

    across, up = grid if isinstance(grid, (tuple, list)) else (grid, grid)
    if across > 0 and up > 0:
        fine = rgb(pick["grid"], (91, 79, 138))
        at = across
        while at < 100.0:
            gx, _gy = at_percent(at, 0)
            for y in range(int(top), int(bottom) + 1, 3):
                put(gx, y, fine)
            at += across
        at = up
        while at < 100.0:
            _gx, gy = at_percent(0, at)
            for x in range(int(left), int(right) + 1, 3):
                put(x, gy, fine)
            at += up
    for shape in (shapes or ()):
        if len(shape) < 2:
            continue
        edge = rgb(pick["mask"], (154, 165, 177))
        pixels = [at_percent(x, y) for x, y in shape]
        for i, (x0, y0) in enumerate(pixels):
            x1, y1 = pixels[(i + 1) % len(pixels)]
            stroke(x0, y0, x1, y1, edge, 2)

    label = rgb(pick["label"], (154, 165, 177))
    placed = []
    for wave in waves:
        trace = rgb((traces or {}).get(wave.source) or pick["trace"],
                    (255, 214, 64))
        xy, _bounds = plot_geometry(wave, width, height, pad, view,
                                    band=band)
        for i in range(1, len(xy)):
            x0, y0 = xy[i - 1]
            x1, y1 = xy[i]
            steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
            for s in range(steps + 1):
                f = s / float(steps)
                put(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, trace)
                put(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f + 1, trace)
        # The marker at this trace's own zero volts, filled in its own
        # colour with the name in black on it - which is how an
        # instrument tells four traces apart, and the only thing on a
        # saved picture that says which is which.
        name = str(wave.label or wave.source or "")[:8]
        if name:
            mark_h, point = 13, 6
            mark_w = text_width(name) + 8 + point
            zero = view.y_of_volts(wave, 0.0, top, bottom)
            zero = min(bottom - mark_h / 2.0, max(top + mark_h / 2.0, zero))
            # Outside the graticule, where it covers no signal - see
            # MARKER_ROOM. Any that collide step to the right of what
            # they ran into, which is into the graticule; that only
            # happens when two traces share a zero.
            x0 = marker_place(placed, left, zero, mark_w, mark_h, 1,
                              right=right, room=width)
            placed.append((x0, zero, x0 + mark_w))
            # The point goes on whichever side the graticule is, so a
            # marker always points at the trace it names.
            facing = marker_facing(x0, right)
            for dy in range(-(mark_h // 2), mark_h // 2 + 1):
                taper = point * abs(dy) / float(mark_h / 2.0)
                if facing < 0:
                    span = range(int(x0 + taper), int(x0 + mark_w) + 1)
                else:
                    span = range(int(x0), int(x0 + mark_w - taper) + 1)
                for x in span:
                    put(x, zero + dy, trace)
            draw_text(put, name, x0 + 4 + (point if facing < 0 else 0),
                      zero - 3, (0, 0, 0))

    if band:
        # The record strip, drawn the same way the window draws it: each
        # trace laid out by time over the whole span the set covers, at
        # its own peak so a small signal beside a large one is still
        # visible, with the part on the graticule outlined. A map of the
        # capture, not a second plot - so it does not move when the main
        # view is zoomed, which is the whole point of it.
        whole = max(1e-15, view.full_span)
        for wave in waves:
            levels = wave.levels()
            spots = wave.points()
            if len(levels) < 2 or len(spots) < 2:
                continue
            trace = rgb((traces or {}).get(wave.source) or pick["trace"],
                        (255, 214, 64))
            a = (spots[0][0] - view.full_first) / whole * width
            b = (spots[-1][0] - view.full_first) / whole * width
            wide = max(2.0, b - a)
            reach = max(1.0, max(abs(min(levels)), abs(max(levels))))
            columns = max(2, int(wide))
            middle, span = band / 2.0, band / 2.0 - 3
            for column in range(columns):
                i = int(len(levels) * column / float(columns))
                j = max(i + 1,
                        int(len(levels) * (column + 1) / float(columns)))
                chunk = levels[i:j]
                x = a + wide * (column + 0.5) / columns
                lo = middle - (min(chunk) / reach) * span
                hi = middle - (max(chunk) / reach) * span
                for y in range(int(hi), int(lo) + 1):
                    put(x, y, trace)
        start, end = view.fractions()
        x0, x1 = start * width, end * width
        if x1 - x0 < 3:                      # always visible
            x0, x1 = (x0 + x1) / 2.0 - 1.5, (x0 + x1) / 2.0 + 1.5
        edge = rgb(pick["label"], (154, 165, 177))
        for step in range(2):
            for y in range(0, int(band)):
                put(x0 + step, y, edge)
                put(x1 - step, y, edge)
            for x in range(int(x0), int(x1) + 1):
                put(x, step, edge)
                put(x, band - 1 - step, edge)
        for x in range(width):               # a rule under the strip
            put(x, band + 2, rgb(pick["graticule"], (52, 62, 72)))

    # The two per-division settings, centred under the graticule, which
    # is where an instrument puts them.
    if verdict:
        words, colour = verdict
        ink = rgb(colour, (200, 60, 60))
        # Filled, with the word knocked out of it in black. An outline
        # on a dark graticule reads as one more label somebody left
        # there; a solid stamp reads as the answer, which is what it
        # is, and it has to carry across a room.
        scale = 3
        wide = text_width(words, scale) + 12
        tall = 7 * scale + 10
        edge = int(right - wide)
        for y in range(int(top) + 4, int(top) + 4 + tall):
            for x in range(edge, int(right) + 1):
                put(x, y, ink)
        draw_text(put, words, edge + 6, top + 9, (0, 0, 0), scale)
    if caption:
        draw_text(put, caption,
                  (left + right) / 2.0 - text_width(caption) / 2.0,
                  bottom + 4, label)
    first = waves[0] if waves else None
    wfid = (first.wfid or first.source) if first else ""
    if wfid:
        # Under the record strip where there is one, since both want the
        # top of the picture and the strip is the one that has to be
        # full width.
        draw_text(put, wfid[:int(width / 6) - 2], 6, band + 10, label)

    return encode_png(buf, width, height)


# --------------------------------------------------- the instrument's own
# colours

# What each palette entry is called on the instrument, and what this
# program calls the same thing. Only the ones a plot has a use for.
COLOUR_ITEMS = {"BACKGROUND": "background", "GRATICULE": "graticule",
                "TEXT": "label"}


# Tektronix's hue zero is not the red that a textbook HSL starts at.
# Measured against a 784D's own screen: it reports REF as 44,39,72, an
# orange by the usual reckoning, and photographs it as #851cac, a
# purple - and the two are the same three bytes rotated, which is what a
# 120 degree shift of hue does. Confirmed on the graticule and the menu
# text at the same time, both of which land within two counts a channel
# once the shift is applied and are nowhere near without it.
HUE_ORIGIN = 120.0


def hls_to_hex(hue, light, sat):
    """A Tektronix HLS triple to '#rrggbb'.

    Hue in degrees, lightness and saturation as percentages - which is
    the order the instrument prints them in, whatever the letters
    suggest. Settled by reading a 784D's hardcopy palette, where the
    background is 0,100,0: white, so the 100 is the lightness.

    Every entry that settled the field order was grey, white or black,
    and a grey has no hue to get wrong - which is exactly how the 120
    degree offset above survived the first check. See HUE_ORIGIN.
    """
    h = ((float(hue) - HUE_ORIGIN) % 360.0) / 360.0
    l = min(1.0, max(0.0, float(light) / 100.0))
    s = min(1.0, max(0.0, float(sat) / 100.0))
    if s == 0:
        r = g = b = l
    else:
        q = l * (1 + s) if l < 0.5 else l + s - l * s
        p = 2 * l - q

        def channel(t):
            t = t % 1.0
            if t < 1 / 6.0:
                return p + (q - p) * 6 * t
            if t < 0.5:
                return q
            if t < 2 / 3.0:
                return p + (q - p) * (2 / 3.0 - t) * 6
            return p

        r, g, b = channel(h + 1 / 3.0), channel(h), channel(h - 1 / 3.0)
    return "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in (r, g, b))


def scpi_tree(reply):
    """A headers-on reply to a flat dict of full path to value.

    SCPI's compound headers are relative: after `:DISPLAY:COLOR:PALETTE:
    NORMAL:BACKGROUND 0,0,0` a bare `CH1 0,65,0` means
    `DISPLAY:COLOR:PALETTE:NORMAL:CH1`, and reading it as anything else
    gets the wrong colour rather than no colour. The rule is that each
    element's path, minus its last node, is the prefix for the next one.
    """
    out = {}
    path = []
    for element in str(reply or "").strip().split(";"):
        element = element.strip()
        if not element:
            continue
        head, _sp, value = element.partition(" ")
        if head.startswith(":"):
            nodes = head.lstrip(":").upper().split(":")
        else:
            nodes = path + head.upper().split(":")
        if not nodes:
            continue
        out[":".join(nodes)] = value.strip()
        path = nodes[:-1]
    return out


def parse_display_colours(reply):
    """Names to '#rrggbb', from a `DISPLAY:COLOR?` reply.

    Which palette is in use decides which block of the reply is read -
    switching the instrument to its hardcopy palette really does turn
    the traces black, and reporting the screen colours then would be
    reporting something that is not on the screen.

    A reference or a maths trace takes its colour from a mapping rather
    than from an entry of its own, so REF2 is looked up through
    `DISPLAY:COLOR:MAP:REF2:TO`, which usually says REF but can be made
    to say a channel.
    """
    flat = scpi_tree(reply)
    base = "DISPLAY:COLOR"
    using = (flat.get("%s:PALETTE:REGULAR" % base) or "NORMAL").upper()
    if not flat.get("%s:PALETTE:%s:BACKGROUND" % (base, using)):
        return {}

    def entry(item):
        text = flat.get("%s:PALETTE:%s:%s" % (base, using, item))
        parts = [p.strip() for p in (text or "").split(",")]
        if len(parts) != 3:
            return None
        try:
            return hls_to_hex(*[float(p) for p in parts])
        except (TypeError, ValueError):
            return None

    out = {}
    for item, key in COLOUR_ITEMS.items():
        found = entry(item)
        if found:
            out[key] = found
    for name in SELECTABLE:
        item = name
        if not name.startswith("CH"):
            # MATH1 and REF2 have no palette entry of their own.
            item = (flat.get("%s:MAP:%s:TO" % (base, name))
                    or name.rstrip("0123456789")).upper()
        found = entry(item)
        if found:
            out[name] = found
    return out


# ------------------------------------------------------------- the transfer

class TdsWfm(object):
    """Waveform transfer on an already-open instrument session.

    Takes the same VISA resource the filesystem side uses, so one
    connection serves both and neither has to know about the other.
    """

    def __init__(self, inst, payload=None):
        self.inst = inst
        # The filesystem side already knows how to strip a SCPI header
        # from a reply; reuse its rule rather than having two.
        self._payload = payload or (lambda r: r)
        # Asked for once and remembered, including the answer "this
        # instrument has no colours" - see display_colours().
        self._colours = None

    def q(self, cmd):
        return self._payload(self.inst.query(cmd)).strip()

    # -- what the instrument draws in ------------------------------------

    COLOUR_TIMEOUT_MS = 4000

    def display_colours(self):
        """What colour the instrument draws each source in.

        A colour TDS keeps four palettes - NORMAL, BOLD, HARDCOPY and
        MONO - and says which is in use. Each entry is a Tektronix HLS
        triple: hue in degrees, lightness and saturation as percentages.
        Read from one `DISPLAY:COLOR?`, with headers on so the reply
        names its own fields.

        A monochrome instrument has no such subsystem and answers
        nothing at all, so the query is given a short timeout of its own
        and the result - including the empty one - is remembered. Left
        at the session's timeout, a 640A would spend three quarters of a
        minute saying it has no colours, every time the list refreshed.

        Returns names to '#rrggbb', or {} for an instrument with no
        colour to report. Nothing is set; this only reads.
        """
        if self._colours is not None:
            return self._colours
        self._colours = {}
        was = getattr(self.inst, "timeout", None)
        try:
            self.inst.timeout = self.COLOUR_TIMEOUT_MS
            self.inst.write("HEADER ON")
            reply = self.inst.query("DISPLAY:COLOR?")
        except Exception:
            try:
                self.inst.clear()
            except Exception:
                pass
            return self._colours
        finally:
            if was is not None:
                self.inst.timeout = was
            try:
                self.inst.write("HEADER OFF")
            except Exception:
                pass
        self._colours = parse_display_colours(reply)
        return self._colours

    def selection(self):
        """Every source this instrument has, and whether it is displayed.

        Asked with headers on, so the reply names its own fields:

            :SELECT:CH1 1;CH2 0;CH3 0;CH4 0;MATH1 0;...;REF4 1;CONTROL REF4

        which means the names come from the instrument rather than from a
        list here. It matters: a two-channel scope has no CH3, and a
        positional read of the bare reply would then label everything
        after it wrongly - calling a MATH a CH4. The bare form is kept as
        a fall-back for anything that will not answer this way.

        CONTROL is not a source; it names which one the front panel knobs
        are attached to, and is dropped.
        """
        try:
            self.inst.write("HEADER ON")
            reply = self.inst.query("SELECT?").strip()
        except Exception:
            reply = ""
        finally:
            try:
                self.inst.write("HEADER OFF")
            except Exception:
                pass
        out = []
        for field in reply.lstrip(":").split(";"):
            field = field.strip()
            if not field or " " not in field:
                continue
            name, _, value = field.partition(" ")
            name = name.split(":")[-1].upper()
            if name in ("CONTROL", "SELECT"):
                continue
            out.append((name, value.strip() == "1"))
        if out:
            return out
        # Nothing named came back: fall back to reading it by position.
        try:
            flags = self.q("SELECT?").split(";")
        except Exception:
            return []
        return [(name, flag.strip() == "1")
                for name, flag in zip(SELECTABLE, flags)]

    def sources(self):
        """Which sources are displayed, and so can be read at all.

        Asking for a channel that is switched off raises 2241, "waveform
        requested is invalid".
        """
        return [name for name, on in self.selection() if on]

    # A preamble field either answers at once or does not exist. Waiting
    # the session's full timeout for each one that does not is how a
    # TDS 640A - which has no per-source XZERO - turned a tenth of a
    # second into three quarters of a minute.
    FIELD_TIMEOUT_MS = 3000

    def bulk_preamble(self, source):
        """The whole preamble in one query, or None if that will not do.

        `WFMPRE:<source>?` returns every field at once. Asked with
        HEADER ON it names each one, which is what makes it usable: the
        headers-off reply is not in the same field order on every
        instrument, and mapping one instrument's order onto another
        quietly puts a unit string where a sample count belongs. Named,
        the order does not matter.

        Measured against asking one field at a time: 419 ms becomes 81
        on a 784D, 457 becomes 76 on a 784C, and on a 640A 3510 becomes
        196 - because that instrument has no XZERO and does not answer
        the query for it at all, so every capture used to wait out the
        timeout for a field that firmware has never had.

        `self.q` is deliberately not used. It strips a leading SCPI
        header, and on a headers-on compound reply the leading header is
        the *first field's own name* - which silently swallowed WFID the
        first time this was tried.

        Returns None rather than raising if anything is off, so the
        caller can fall back to the eleven queries: one instrument in
        thirty-four answering in some shape not seen here should cost a
        little speed, not the capture.
        """
        was = self.inst.timeout
        self.inst.timeout = self.FIELD_TIMEOUT_MS
        try:
            self.inst.write("HEADER ON")
            try:
                reply = self.inst.query("WFMPRE:%s?" % source) or ""
            finally:
                # The rest of the program works headers-off and expects
                # to find it that way, whatever happened above.
                self.inst.write("HEADER OFF")
        except Exception:
            try:
                self.inst.clear()
            except Exception:
                pass
            return None
        finally:
            self.inst.timeout = was
        out = {}
        for part in reply.split(";"):
            part = part.strip().lstrip(":")
            if part.upper().startswith("WFMPRE:"):
                # ":WFMPRE:CH1:WFID ..." - the subsystem and the source,
                # which are already known, in front of the first field.
                part = part.split(":", 2)[-1]
            if " " not in part:
                continue
            key, value = part.split(" ", 1)
            out[key.strip().upper()] = value.strip()
        # NR_PT is the gate here as much as it is below: without it
        # there is nothing to read and no reason to trust the rest.
        try:
            if int(float(out.get("NR_PT"))) <= 0:
                return None
        except (TypeError, ValueError):
            return None
        return out

    def preamble(self, source):
        """Every field, in one query where that works and eleven where
        it does not.

        NR_PT is the gate: if the instrument will not say how many
        points there are, there is no waveform to describe and the
        remaining ten fields will each be answered with 2241 in turn.
        That is not a theoretical tidiness - the event queue on these
        instruments is twenty deep, and one read of a switched-off
        channel used to leave nineteen 2241s and a queue overflow in it,
        which then surfaced at the *next* connection as a page of errors
        that had nothing to do with anything the user had just done. The
        bulk query keeps that property: it is one transaction, and a
        source with nothing in it answers it with nothing usable.

        A later field failing is tolerated: a 640A has no per-source
        XZERO, and one missing field out of eleven should not cost a
        capture. In the bulk reply it is simply absent, which is the
        same thing said more cheaply.
        """
        quick = self.bulk_preamble(source)
        if quick is not None:
            return quick
        return self.preamble_by_field(source)

    def preamble_by_field(self, source):
        """The eleven queries, one field at a time. The fallback."""
        was = self.inst.timeout
        self.inst.timeout = self.FIELD_TIMEOUT_MS
        try:
            try:
                first = self.q("WFMPRE:%s:NR_PT?" % source)
            except Exception:
                try:
                    self.inst.clear()
                except Exception:
                    pass
                raise NotReadable(source)
            out = {"NR_PT": first}
            for field in FIELDS:
                if field == "NR_PT":
                    continue
                try:
                    out[field] = self.q("WFMPRE:%s:%s?" % (source, field))
                except Exception:
                    out[field] = None   # a 640A has no per-source XZERO
                    try:
                        self.inst.clear()
                    except Exception:
                        pass
        finally:
            self.inst.timeout = was
        return out

    def read_block(self):
        """A definite-length block: #<digits><length><bytes>."""
        old = self.inst.read_termination
        self.inst.read_termination = ""
        try:
            self.inst.write("CURVE?")
            raw = self.inst.read_raw()
        finally:
            self.inst.read_termination = old
        if not raw.startswith(b"#"):
            raise IOError("the instrument did not send a waveform block")
        digits = int(raw[1:2])
        length = int(raw[2:2 + digits])
        body = raw[2 + digits:2 + digits + length]
        if len(body) != length:
            raise IOError("waveform truncated: %d bytes of %d"
                          % (len(body), length))
        return body

    def get(self, source, width=1):
        """Read one source. Returns a Waveform.

        DATA:SOURCE is written before anything is asked about the
        source, and that ordering matters more than it looks: every
        WFMPRE:<source>:<field>? is answered with 2241 unless
        DATA:SOURCE is already pointing at that source. Measured on a
        784D - REF4 holds a waveform and reads perfectly, and yet
        WFMPRE:REF4:NR_PT? asked cold is refused.
        """
        self.inst.write("DATA:SOURCE %s" % source)
        self.inst.write("DATA:ENCDG RIBINARY")
        self.inst.write("DATA:WIDTH %d" % width)
        # Open the transfer window before asking how long the waveform
        # is. NR_PT does not report the record length - it reports how
        # many points *would be sent*, which is DATA:START..DATA:STOP.
        # Read it without setting those first and the answer is whatever
        # window the instrument was left in, by this program or by
        # anything else that has talked to it. Found on a 784D reporting
        # NR_PT 20 for a 5000 point record, because something earlier
        # had set DATA:STOP 20 - and the program would then have
        # downloaded 20 points and called it the waveform.
        #
        # A number past the end is clamped rather than refused, and each
        # source then reports its own length: 5000 for a channel on a
        # 5000 point record, 500 for a reference holding 500.
        self.inst.write("DATA:START 1")
        self.inst.write("DATA:STOP %d" % WHOLE_RECORD)
        pre = self.preamble(source)
        try:
            npts = int(float(pre.get("NR_PT")))
        except (TypeError, ValueError):
            raise NotReadable(source)
        if npts <= 0:
            raise NotReadable(source)
        self.inst.write("DATA:START 1")
        self.inst.write("DATA:STOP %d" % npts)
        raw = self.read_block()
        return Waveform(source, raw, pre, width=width)

    def acquisition(self):
        """What the acquisition is doing, in enough detail to put back."""
        out = {}
        for key, cmd in (("state", "ACQUIRE:STATE?"),
                         ("after", "ACQUIRE:STOPAFTER?")):
            try:
                out[key] = self.q(cmd)
            except Exception:
                out[key] = ""
        return out

    def capture(self, live, refs=(), width=1, note=None):
        """One acquisition, read on every live source, then the references.

        Reading CH1 and then CH2 gets two different captures: 600 ms
        apart on this bus, and a scope triggering thirteen times a
        second in between. Measured on a 784D, the two disagreed about
        XZERO - the sub-sample trigger offset, which is a new number
        every acquisition - so the traces the program drew on one
        graticule were not from one moment and did not line up exactly.
        Freezing first makes them what the instrument actually
        digitises: one acquisition seen on several channels. Stopped,
        the same two channels reported an identical XZERO and the same
        source read twice came back byte for byte the same.

        Three things are deliberately not done:

        * an instrument that is already stopped is left stopped. Somebody
          has frozen a single-shot capture on it, and starting it again
          would throw that away.
        * an instrument set to STOPAFTER SEQUENCE is not touched either.
          It may be armed and waiting for a rare trigger, and stopping
          and starting re-arms it - which loses the event it was waiting
          for.
        * the references are read *after* the acquisition is let go.
          They are not moving, they gain nothing from the freeze, and
          every millisecond frozen is a millisecond the instrument is
          not acquiring.

        The release is in a finally: a source that refuses part way
        through must not leave the instrument stopped.

        DPO is refused outright rather than read: it fills no waveform
        record, so every point of every channel comes back as 128 and
        the plot is empty. See in_dpo. It is never switched off for the
        user - that discards the accumulation on screen, which on a
        long eye or a rare-event hunt is the run itself.
        """
        if self.in_dpo():
            raise NotReadable(
                None, "Waveform capture is not supported in DPO mode.\n"
                "Disable DPO and try again or perform a screen capture.")
        was = self.acquisition()
        running = was.get("state", "").strip().upper() in ("1", "ON", "RUN")
        # SEQUENCE stops itself when the sequence is done, and STOPAFTER
        # LIMIT stops itself when the limit test trips. Freezing either
        # would be taking away the very thing that is running - and with
        # LIMIT there is nothing for an operation-complete query to
        # complete until the test fails, so *OPC? waits out the bus.
        sequence = was.get("after", "").strip().upper()[:3] in ("SEQ", "LIM")
        froze = bool(live) and running and not sequence
        waves, refused = [], []
        if froze:
            self.inst.write("ACQUIRE:STATE STOP")
            try:
                self.q("*OPC?")
            except Exception:
                pass
        try:
            for i, source in enumerate(live):
                if note:
                    note(source, i / float(len(live) + len(refs)))
                try:
                    waves.append(self.get(source, width))
                except Exception as exc:
                    refused.append((source, str(exc)))
        finally:
            if froze:
                self.inst.write("ACQUIRE:STATE RUN")
        # Asked rather than assumed. Leaving somebody's scope stopped
        # because a download finished would be a poor way to find out
        # that the release had not worked.
        try:
            back = self.q("ACQUIRE:STATE?")
        except Exception:
            back = ""
        for j, source in enumerate(refs):
            if note:
                note(source, (len(live) + j) / float(len(live) + len(refs)))
            try:
                waves.append(self.get(source, width))
            except Exception as exc:
                refused.append((source, str(exc)))
        return waves, refused, {"froze": froze, "running": running,
                                "sequence": sequence, "after": back}

    def exists(self, source):
        """Is there a waveform in this source that could be read?

        DATA:SOURCE first. Without it this question is answered with
        2241 for everything, including references that demonstrably hold
        a waveform, which is what made it useless before and made
        send_to_ref allocate whether it needed to or not.

        Bounded, for the same reason the preamble reads are: a source
        with nothing in it does not answer, and this is asked precisely
        when the answer may well be no. Left at the session timeout, a
        single call outlasts the whole allocation it is meant to be
        polling.
        """
        was = self.inst.timeout
        self.inst.timeout = self.FIELD_TIMEOUT_MS
        try:
            self.inst.write("DATA:SOURCE %s" % source)
            self.inst.write("DATA:START 1")
            self.inst.write("DATA:STOP %d" % WHOLE_RECORD)
            return int(float(self.q("WFMPRE:%s:NR_PT?" % source))) > 0
        except Exception:
            try:
                self.inst.clear()
            except Exception:
                pass
            return False
        finally:
            self.inst.timeout = was

    def select(self, name, on=True):
        """Show or hide a source on the instrument's screen.

        Worth doing from here because a channel that is not displayed
        cannot be read: DATA:SOURCE takes it, and CURVE? then answers
        2241, "waveform requested is invalid", followed by a query
        interrupt. Measured on a TDS 784D and a TDS 640A - both refuse
        while the channel is off and both read immediately once it is
        on. A stored reference is the exception: it reads whether it is
        displayed or not, because the data is already there.
        """
        self.inst.write("SELECT:%s %s" % (name, "ON" if on else "OFF"))
        time.sleep(0.3)

    def delete_ref(self, name):
        """Delete a stored reference. Nothing puts it back.

        `DELETE:WAVEFORM <REF>` is the command; the bare header answers
        100, "command not allowed", which is a header that wants an
        argument rather than one that does not exist. That it really
        removes the data, rather than only taking it off the screen, was
        settled by behaviour: a reference that is merely hidden reads
        back fine and comes up again when displayed, while one that has
        been deleted refuses to display and cannot be read.

        `WFMPRE:<REF>:NR_PT?` is no use for checking any of this - it
        answers 2241 for every reference on these instruments, including
        one that demonstrably holds a waveform.
        """
        if not str(name).upper().startswith("REF"):
            raise ValueError("Only a stored reference can be deleted; "
                             "%s is a live source." % name)
        self.inst.write("DELETE:WAVEFORM %s" % name)
        time.sleep(0.8)

    def send_to_ref(self, wave, dest, allocate_from=None, settle=1.5):
        """Put a waveform into REF1-4.

        The step that matters is the first one. A reference that does not
        exist yet cannot be written to: the transfer settings are accepted
        but every field describing the waveform - PT_FMT, XINCR, PT_OFF,
        YMULT, YZERO, YOFF - is answered with 2241, "waveform requested is
        invalid", and the curve that follows is met with 532, "curve data
        too long, curve truncated". Nothing warns you and the reference
        stays empty.

        SAVE:WAVEFORM brings it into being from a live channel, and after
        that everything is accepted and the data lands exactly. Measured
        byte-identical on a 784D, a 784C and a 640A.
        """
        if not self.exists(dest):
            if not allocate_from:
                raise IOError(
                    "%s is empty, and an empty reference cannot be written "
                    "to. A live channel is needed once to bring it into "
                    "being." % dest)
            self.inst.write("SAVE:WAVEFORM %s,%s" % (allocate_from, dest))
            # Wait, then carry on regardless of what the instrument says
            # about the reference. Measured on a 784C and a 640A: after
            # the save the reference is perfectly usable, but
            # WFMPRE:REF4:NR_PT? still does not answer for it - so polling
            # that as proof of allocation rejects a reference that is
            # actually there. What the data landed as is settled at the
            # end by reading it back and comparing, which is the only
            # check worth trusting anyway.
            time.sleep(settle)

        npts = wave.count
        # A reference holds what it was allocated and a longer curve is
        # not refused: the instrument keeps the front of it and the
        # read-back is short, which the verify above this reports as the
        # instrument not keeping what was sent. Measured on a 784D -
        # 2500 points into a REF4 the instrument had allocated 500,
        # which is what a reference written once at 500 stays at.
        # The envelope route has always sent this; this one never did.
        self.inst.write("ALLOCATE:WAVEFORM:%s %d" % (dest, npts))
        self.inst.write("DATA:DESTINATION %s" % dest)
        self.inst.write("DATA:ENCDG RIBINARY")
        self.inst.write("DATA:WIDTH %d" % wave.width)
        for cmd in ("WFMPRE:BYT_NR %d" % wave.width,
                    "WFMPRE:BIT_NR %d" % (16 if wave.width == 2 else 8),
                    "WFMPRE:ENCDG BIN",      # BINARY is rejected; BIN is not
                    "WFMPRE:BN_FMT RI",
                    "WFMPRE:BYT_OR MSB",
                    "WFMPRE:NR_PT %d" % npts):
            self.inst.write(cmd)
        for field in ("PT_FMT", "XINCR", "XZERO", "PT_OFF", "XUNIT",
                      "YMULT", "YZERO", "YOFF", "YUNIT"):
            value = wave.pre.get(field)
            if value:                    # skip what this firmware never gave
                self.inst.write("WFMPRE:%s %s" % (field, value))
        self.inst.write("DATA:START 1")
        self.inst.write("DATA:STOP %d" % npts)

        digits = str(len(wave.raw))
        header = ("CURVE #%d%s" % (len(digits), digits)).encode("ascii")
        old = self.inst.write_termination
        self.inst.write_termination = ""
        try:
            self.inst.write_raw(header + wave.raw + b"\n")
        finally:
            self.inst.write_termination = old
        return {"dest": dest, "points": npts, "bytes": len(wave.raw)}

    def verify_ref(self, wave, dest):
        """Read the reference back and compare. True only on an exact match."""
        time.sleep(0.5)
        try:
            back = self.get(dest, width=wave.width)
        except Exception:
            return False
        return back.raw == wave.raw

    # -- masks, and the way round them ------------------------------------
    # Two different things live here. `mask_segments` reads the
    # instrument's own mask subsystem, which a C or D series has and an A
    # series does not. `send_envelope` does not touch that subsystem at
    # all: it loads a limit template into a reference, which is how
    # Tektronix's own mask applications put a limit on this generation,
    # and which works on all three instruments measured - the 640A
    # included. See INSTRUMENT-NOTES.md.

    MASK_SEGMENTS = 8
    MASK_TIMEOUT_MS = 4000

    def mask_replies(self):
        """What the eight mask segments answer, word for word.

        None where the instrument has no mask subsystem at all: on a
        640A running v3.8.8e `MASK:STANDARD?` is not a command, so the
        query is given a short timeout of its own and the answer -
        including "there is no such thing here" - is remembered. Left at
        the session timeout, a 640A would spend most of a minute saying
        it has no masks every time the list refreshed.

        Raw, because Mask.from_scpi knows how to read them and this
        does not need to: it is the same text the instrument would give
        anybody, and turning it into points in one place rather than two
        is what keeps a mask read back the same as a mask sent.

        Measured on a 784D: a segment nobody has written to answers
        `0.0E+0,0.0E+0` exactly - one pair, not an empty reply - which
        is the convention from_scpi reads as "nothing here".
        """
        # Only the "there is no such subsystem" answer is remembered.
        # The counts themselves are not: segments change as masks are
        # loaded, and the whole point of reading them is to see what is
        # in the instrument now.
        if getattr(self, "_masks", None) is False:
            return None
        was = getattr(self.inst, "timeout", None)
        try:
            self.inst.timeout = self.MASK_TIMEOUT_MS
            self.q("MASK:STANDARD?")
        except Exception:
            try:
                self.inst.clear()
            except Exception:
                pass
            self._masks = False
            return None
        finally:
            if was is not None:
                self.inst.timeout = was
        out = []
        for n in range(1, self.MASK_SEGMENTS + 1):
            try:
                out.append(self.q("MASK:MASK%d:POINTSPCNT?" % n))
            except Exception:
                out.append("")
        return out

    def mask_segments(self):
        """How many points each of the eight mask segments holds.

        None where there is no mask subsystem at all.
        """
        replies = self.mask_replies()
        return None if replies is None else mask_counts(replies)

    DPO_TIMEOUT_MS = 3000

    def in_dpo(self):
        """Is the instrument in DPO mode, where there is nothing to read?

        DPO builds a pixel database instead of filling a waveform
        record, so every sample comes back as mid-scale filler - 128 on
        every point of every channel, measured on a 784D. A plain read
        gives the same as a frozen one, so this is not something the
        capture can work around.

        Bounded and remembered: the A and B series have no DISPLAY:MODE
        at all and must not pay a session timeout to say so.
        """
        if getattr(self, "_dpo", None) is False:
            return False
        was = getattr(self.inst, "timeout", None)
        try:
            self.inst.timeout = self.DPO_TIMEOUT_MS
            mode = self.q("DISPLAY:MODE?").strip().upper()
        except Exception:
            try:
                self.inst.clear()
            except Exception:
                pass
            self._dpo = False          # no such command here
            return False
        finally:
            if was is not None:
                self.inst.timeout = was
        return mode.startswith("INSTAVU") or mode.startswith("DPO")

    def send_mask(self, lines, display=True):
        """Write a mask into the instrument's eight segments.

        `lines` is what Mask.to_scpi built. MASK:STANDARD is not among
        them on purpose - it deletes the mask it is meant to accompany.
        """
        for cmd in lines:
            self.inst.write(cmd)
        self.inst.write("MASK:DISPLAY %s" % ("ON" if display else "OFF"))
        self.q("*OPC?")
        time.sleep(0.4)

    def send_envelope(self, lines, dest, allocate_from=None, settle=1.5):
        """Load a limit template into a reference and test against it.

        `lines` is what tds_msk.envelope_scpi built - the destination,
        the allocation, the preamble with PT_FMT ENV, the curve and the
        LIMIT:COMPARE that makes the instrument use it. They are sent as
        they come, because their order is the order Tektronix's own
        .ENV files use and the preamble has to be in place before the
        curve arrives.

        A reference that does not exist yet cannot be written to - the
        same trap send_to_ref documents - so it is brought into being
        from a live channel first if there is one to use.
        """
        if not self.exists(dest) and allocate_from:
            self.inst.write("SAVE:WAVEFORM %s,%s" % (allocate_from, dest))
            time.sleep(settle)
        for cmd in lines:
            self.inst.write(cmd)
        time.sleep(0.5)
        return {"dest": dest, "commands": len(lines)}

    def read_envelope(self, dest):
        """An envelope reference as (seconds, lowest, highest) columns.

        An envelope is two values a column - the lowest and the highest
        the signal may be there - so it is read as a curve and paired
        up here. A Waveform is one value a point and this is not one of
        those, which is why it does not go through `get`.

        Volts and seconds come from the reference's own preamble, so
        the band lands where the instrument would draw it. Returns []
        where the reference holds nothing, or holds a plain waveform.

        Two orderings here were measured on a 784D and both were wrong
        the other way round:

        The preamble is read *after* DATA:WIDTH, because WFMPRE answers
        for the width currently set. Read first, YMULT comes back as
        the eight-bit 20 mV a count rather than the sixteen-bit 78.125
        uV, and every volt in the band is 256 times too big - which
        draws as a band covering the whole graticule, and a band that
        covers everything passes everything.

        DATA:STOP counts values, not points. Asked for NR_PT*2 the
        instrument answers 531, "Data stop > record length, Curve
        truncated" and sends NR_PT of them anyway, so what comes back
        is NR_PT/2 columns covering the whole record - measured
        against a 1 kHz square at 200 us/div, whose edges land 62.3
        columns apart out of 250: 498 us where 500 is right. So the
        columns are spread over the record rather than assumed to be
        one sample apart.
        """
        self.inst.write("DATA:SOURCE %s" % dest)
        self.inst.write("DATA:ENCDG ASCII")
        self.inst.write("DATA:WIDTH 2")
        pre = self.preamble(dest)
        if "ENV" not in str(pre.get("PT_FMT", "")).upper():
            return []
        points = int(float(pre.get("NR_PT") or 0))
        if points < 2:
            return []
        self.inst.write("DATA:START 1")
        self.inst.write("DATA:STOP %d" % points)
        counts = []
        for b in str(self.q("CURVE?")).replace(";", ",").split(","):
            try:
                counts.append(float(b))
            except ValueError:
                pass
        ymult = float(pre.get("YMULT") or 1.0)
        yoff = float(pre.get("YOFF") or 0.0)
        yzero = float(pre.get("YZERO") or 0.0)
        xincr = float(pre.get("XINCR") or 1.0)
        xzero = float(pre.get("XZERO") or 0.0)
        ptoff = float(pre.get("PT_OFF") or 0.0)
        columns = len(counts) // 2
        if columns < 2:
            return []
        # Samples a column, so the band covers the record whatever the
        # instrument chose to send - and placed by the same formula
        # plot_geometry places a trace with, or the two are drawn on
        # different time axes.
        per = points / float(columns)
        out = []
        for i in range(columns):
            lo, hi = counts[i * 2], counts[i * 2 + 1]
            out.append((xzero + (i * per - ptoff) * xincr,
                        (min(lo, hi) - yoff) * ymult + yzero,
                        (max(lo, hi) - yoff) * ymult + yzero))
        return out

    def verify_envelope(self, dest, wanted):
        """Read the envelope back and compare it number for number.

        Returned rather than raised. Reading an envelope back is the one
        part of this route that has not been measured on an instrument,
        so a mismatch may be the instrument not keeping what it was sent
        or may be this reading it back wrongly - and the two want
        different words to the user. The caller says which happened and
        shows both, instead of this deciding.
        """
        wanted = [int(v) for v in wanted]
        try:
            self.inst.write("DATA:SOURCE %s" % dest)
            self.inst.write("DATA:ENCDG ASCII")
            self.inst.write("DATA:WIDTH 2")
            self.inst.write("DATA:START 1")
            self.inst.write("DATA:STOP %d" % len(wanted))
            said = self.q("CURVE?")
        except Exception as exc:
            return {"verified": False, "why": "%s: %s"
                    % (type(exc).__name__, exc), "sent": wanted[:8],
                    "got": []}
        got = []
        for b in said.replace(";", ",").split(","):
            try:
                got.append(int(float(b)))
            except ValueError:
                pass
        return {"verified": got == wanted, "sent": wanted[:8],
                "got": got[:8], "count": len(got)}


# The class as this module defines it, kept under a second name.
# TdsWfm is replaceable, so anything that has to have the real one -
# whatever else is bound to the name - reaches for this.
SHIPPED = TdsWfm
