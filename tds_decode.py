"""Host-side serial-protocol decode for captured waveforms.

Nothing here touches an instrument. A decode runs entirely on samples the
Waveform tab already has - a channel that was captured, or a stored
reference loaded from a file - so it works on any TDS whether or not the
scope itself can run a bus-decode application. (Most cannot: the
on-instrument decoder needs a Java runtime the base instruments do not
have. See ../TDS Protocol Decoders.)

The engine is protocol-neutral. A protocol is a `Decoder` subclass in the
`decoders/` folder; `discover()` finds them, so a new protocol is one small
file and nothing here changes. The decode algorithms are ported from the
tested Java suite - same thresholding, auto-baud, bit sampling and framing -
so the corpus that proved them there proves them here.

Writing a decoder: see decoders/README.txt and decoders/_template.py.
"""

import glob
import importlib.util
import os
import shutil

# ---- Frame status bits (a bitwise OR of these) --------------------------
OK = 0
FRAMING_ERROR = 1
PARITY_ERROR = 2
TRUNCATED = 4          # record ended part-way through the frame
ACK_ERROR = 8          # a byte that expected an ACK was not acknowledged


class Frame(object):
    """One decoded event - a UART character, an I2C byte, a SPI word.

    Flat and shared by every protocol, as in the Java suite: a primary
    datum (`value`), an optional secondary (`tag` - an I2C address, a SPI
    MISO word), a `length`, a `status`, and a `kind` string that lets a
    two-wire protocol mark START / ADDR / DATA / ACK / STOP so the table
    and the graph can render each part in its own right. What the fields
    mean is the decoder's business; only the decoder that made a frame
    renders it (see Decoder.row / Decoder.label).

    `index` and `end_index` are record sample positions - the graph view
    draws a box from one to the other, and deriving them back from the
    times would round differently from the decoder that found the edges.
    """

    __slots__ = ("time", "index", "end_index", "value", "tag",
                 "length", "status", "kind")

    def __init__(self, time=0.0, index=0, end_index=0, value=0, tag=0,
                 length=1, status=OK, kind="byte"):
        self.time = time
        self.index = index
        self.end_index = end_index or index
        self.value = value
        self.tag = tag
        self.length = length
        self.status = status
        self.kind = kind


# ---- Signal -------------------------------------------------------------

#: Samples auto_threshold looks at, whatever the record length. A logic
#: signal's two levels show in any fair sample of it, so a full pass buys
#: nothing; this keeps a quarter-million-point record instant.
_THRESHOLD_SAMPLES = 8000


class Signal(object):
    """One captured record, as a decoder needs it: integer samples plus the
    timing to turn a sample index into a time. Raw counts, not volts - a
    decoder compares against a threshold and never needs the scaling.
    """

    def __init__(self, data, seconds_per_sample, time_of_first=0.0,
                 first_index=0):
        self.data = data                      # any indexable of ints
        self.n = len(data)
        self.dt = seconds_per_sample
        self.t0 = time_of_first
        self.first_index = first_index
        self.threshold = 0
        self.hysteresis = 0
        self.span = 0

    def auto_threshold(self):
        """Decision level and dead band from the record itself: mid-point
        of the extremes, dead band an eighth of the span. Enough to reject
        edge ringing without being so wide a slow edge never crosses it."""
        data = self.data
        n = self.n
        if n <= 0:
            self.threshold = self.hysteresis = self.span = 0
            return 0
        stride = n // _THRESHOLD_SAMPLES or 1
        lo = hi = data[0]
        i = stride
        while i < n:
            v = data[i]
            if v < lo:
                lo = v
            elif v > hi:
                hi = v
            i += stride
        self.span = hi - lo
        self.threshold = (hi + lo) // 2
        self.hysteresis = self.span // 8
        return self.span

    def time_at(self, index):
        return self.t0 + index * self.dt

    def digitize(self):
        """A 0/1 level per sample, with the record's own hysteresis so edge
        ringing does not read as several crossings. Built once by a two-wire
        decoder that then walks clock and data together."""
        data = self.data
        upper = self.threshold + self.hysteresis
        lower = self.threshold - self.hysteresis
        out = bytearray(self.n)
        high = 1 if data[0] > self.threshold else 0
        for i in range(self.n):
            v = data[i]
            if high:
                if v < lower:
                    high = 0
            else:
                if v > upper:
                    high = 1
            out[i] = high
        return out

    @classmethod
    def from_waveform(cls, wave):
        """Build from a tds_wfm.Waveform: its integer levels and the XINCR /
        trigger offset out of its preamble."""
        levels = wave.levels()
        dt = wave.number("XINCR") or 1.0
        ptoff = wave.number("PT_OFF") or 0.0
        xzero = wave.number("XZERO") or 0.0
        t0 = xzero - ptoff * dt
        sig = cls(levels, dt, time_of_first=t0)
        sig.auto_threshold()
        return sig


# ---- the plugin contract ------------------------------------------------

class Param(object):
    """One decoder setting the UI renders itself, so a protocol adds a
    control without touching the tab. `kind` is 'choice', 'int' or 'bool'.
    A 'choice' lists (value, label) pairs; an 'int' may give lo/hi and a
    unit; 0 for an int often means 'auto', which `note` should say.
    """

    def __init__(self, key, label, kind="choice", choices=None,
                 default=None, lo=0, hi=0, unit="", note=""):
        self.key = key
        self.label = label
        self.kind = kind
        self.choices = choices or []
        self.default = (default if default is not None
                        else (choices[0][0] if choices else
                              (False if kind == "bool" else 0)))
        self.lo = lo
        self.hi = hi
        self.unit = unit
        self.note = note


class Decoder(object):
    """Base class and whole extension point. A protocol subclasses this,
    sets NAME / SOURCES / PARAMS, and implements decode(); the rest have
    workable defaults. See decoders/_template.py.
    """

    #: Shown in the protocol list and on the graph label.
    NAME = "?"
    #: One line for the list's second column.
    DESCRIPTION = ""
    #: The chevron colour for a clean frame. A decoder owns its colours
    #: completely - the app has no colour setting for decoders - so what a
    #: plugin puts here, in FAULT_COLOUR, and in colour() is the whole of
    #: how its line is drawn. A protocol sets its own so several buses on
    #: one screen tell themselves apart. There is no single industry colour
    #: for these, so the defaults are just distinct and readable.
    COLOUR = "#39a0c0"
    #: The chevron colour for a faulted frame (status set). Red by default,
    #: so an error stands out whatever the protocol's own colour is.
    FAULT_COLOUR = "#d05050"
    #: The waveform roles this protocol needs, in order. A trailing "?"
    #: marks an optional one (SPI MISO, SPI CS). One role is a one-wire
    #: protocol; two or more need a source mapped to each.
    SOURCES = ["Line"]
    #: Settings the UI offers. A list of Param.
    PARAMS = []

    def decode(self, signals, params):
        """Decode. `signals` maps each role name (without any "?") to a
        Signal, an optional one possibly absent; `params` maps each Param
        key to its chosen value. Returns (frames, note) - a list of Frame
        and a one-line string describing what was inferred (detected rate,
        samples per bit), shown above the table.
        """
        raise NotImplementedError

    # -- rendering; a protocol overrides these to speak its own vocabulary

    def columns(self):
        """Table columns as (heading, width_px). The first is always the
        time; the framework fills it."""
        return [("Time", 90), ("Data", 70), ("ASCII", 60), ("Status", 90)]

    def row(self, frame, ascii=True):
        """Cells for one table row, matching columns() after the time."""
        data = "%02X" % (frame.value & 0xFF)
        ch = (chr(frame.value) if 32 <= frame.value < 127 else ".")
        return [data, ch, _status_text(frame.status)]

    def label(self, frame, ascii=True):
        """Shortest useful rendering for the graph box - the value, no
        padding. An error is marked with a leading '!'."""
        if ascii and 32 <= frame.value < 127:
            text = chr(frame.value)
        elif ascii:
            text = "."
        else:
            text = "%02X" % (frame.value & 0xFF)
        return ("!" + text) if frame.status else text

    def colour(self, frame):
        """The chevron colour for one frame, as "#rrggbb". The default
        paints a clean frame in COLOUR and a faulted one in FAULT_COLOUR.
        Override to colour per chevron: switch on frame.kind (or
        frame.status) and return a different colour for each part, so one
        line can draw its address, data and framing in colours of their own.
        The app has no colour setting for decoders; this is the only source,
        so always return a colour."""
        return self.FAULT_COLOUR if frame.status else self.COLOUR


_STATUS = [(FRAMING_ERROR, "FRAME"), (PARITY_ERROR, "PARITY"),
           (ACK_ERROR, "NAK"), (TRUNCATED, "CUT")]


def _status_text(status):
    if not status:
        return "OK"
    return " ".join(name for bit, name in _STATUS if status & bit)


# ---- async-serial core (shared by the UART family) ---------------------
#
# Auto-baud and framed byte recovery, lifted verbatim from the tested
# UartDecoder so RS-232, RS-422/485, MODBUS, MIDI and the rest read one
# line the same proven way. A plugin cannot import a sibling plugin (each
# loads in isolation), but every plugin imports this module - so the shared
# part lives here and uart.py is just its first caller. The corpus that
# proved the algorithm still runs against uart.py, so it still guards this.

STANDARD_BAUDS = [300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800,
                  38400, 57600, 76800, 115200, 230400]
_MIN_SAMPLES_PER_BIT = 3.0
_EDGES_FOR_BAUD = 60          # enough for auto-baud; bounds the first pass
_MIN_OCCURRENCES = 3          # a real bit width recurs; a glitch is a one-off


def parse_framing(text):
    """'8N1' -> (8, parity, 1). parity: 0 none, 1 even, 2 odd."""
    data_bits = int(text[0])
    p = text[1].upper()
    parity = 1 if p == "E" else (2 if p == "O" else 0)
    stop_bits = int(text[2])
    return data_bits, parity, stop_bits


def _shortest_run(sig):
    """Width of one bit in samples: the shortest run of constant level that
    happens more than once. The minimum alone is the least robust statistic
    there is - one runt sets the rate for the whole decode - so runs go into
    a histogram and the answer is the shortest length seen at least
    _MIN_OCCURRENCES times. Single-sample runs are rejected as noise."""
    data = sig.data
    n = sig.n
    threshold = sig.threshold
    upper = threshold + sig.hysteresis
    lower = threshold - sig.hysteresis

    counts = {}
    high = data[0] > threshold
    last_edge = 0
    shortest = n
    edges = 0
    i = 1
    while i < n:
        v = data[i]
        if high:
            if v >= lower:
                i += 1
                continue
            high = False
        else:
            if v <= upper:
                i += 1
                continue
            high = True
        run = i - last_edge
        last_edge = i
        if run > 1:
            if run < shortest:
                shortest = run
            counts[run] = counts.get(run, 0) + 1
        edges += 1
        if edges >= _EDGES_FOR_BAUD:
            break
        i += 1

    for length in sorted(counts):
        if counts[length] >= _MIN_OCCURRENCES:
            return length
    return shortest


def _snap_baud(raw):
    """Report 9600 for 9615: snap to a standard rate within 5%."""
    for standard in STANDARD_BAUDS:
        if abs(raw - standard) <= 0.05 * standard:
            return standard
    return int(raw + 0.5)


def _is_space(sample, threshold, inverted):
    return sample > threshold if inverted else sample <= threshold


def _serial_frames(sig, bit_samples, data_bits, parity, stop_bits, inverted):
    data = sig.data
    n = sig.n
    threshold = sig.threshold
    parity_bits = 0 if parity == 0 else 1
    total_bits = 1 + data_bits + parity_bits + stop_bits
    frame_span = int(bit_samples * (total_bits - 0.5))
    half_bit = int(bit_samples * 0.5)
    step = half_bit if half_bit >= 1 else 1

    frames = []
    i = 0
    # A record that begins mid-character has no knowable alignment: skip to
    # the first mark and start looking from there.
    while i < n and _is_space(data[i], threshold, inverted):
        i += 1

    while i < n:
        if not _is_space(data[i], threshold, inverted):
            i += step
            continue
        # The line left the mark somewhere in the last step; recover the edge.
        edge = i
        limit = i - step if i - step > 0 else 0
        while edge > limit and _is_space(data[edge - 1], threshold, inverted):
            edge -= 1
        # Validate as a start bit, not a glitch: still at the space level
        # half a bit later, as a real UART receiver checks.
        middle = edge + half_bit
        if middle >= n or not _is_space(data[middle], threshold, inverted):
            i = edge + step
            continue
        if edge + frame_span >= n:
            break                             # frame runs off the end

        value = 0
        ones = 0
        status = OK
        k = 1
        while k <= data_bits:
            centre = edge + int(bit_samples * (k + 0.5))
            bit = 0 if _is_space(data[centre], threshold, inverted) else 1
            ones += bit
            value |= bit << (k - 1)
            k += 1
        if parity_bits:
            centre = edge + int(bit_samples * (k + 0.5))
            bit = 0 if _is_space(data[centre], threshold, inverted) else 1
            expected = (ones & 1) if parity == 1 else (1 - (ones & 1))
            if bit != expected:
                status |= PARITY_ERROR
            k += 1
        stop_centre = edge + int(bit_samples * (k + 0.5))
        if _is_space(data[stop_centre], threshold, inverted):
            status |= FRAMING_ERROR

        frames.append(Frame(time=sig.time_at(edge),
                            index=sig.first_index + edge,
                            end_index=sig.first_index + edge + frame_span,
                            value=value, length=1, status=status,
                            kind="byte"))
        i = edge + frame_span                 # resume inside the stop bit
        if status & FRAMING_ERROR:
            # The stop bit was not a mark: the line is still spacing - a
            # break, or a glitch that ran long. Re-synchronise by skipping
            # to the next mark before hunting the next start bit, so one
            # long dominant does not spawn a train of misaligned bytes (LIN
            # and DMX begin every frame with exactly such a break). A clean
            # stream never frames-errors, so this path never runs for one.
            while i < n and _is_space(data[i], threshold, inverted):
                i += 1
    return frames


def serial_bytes(sig, baud=0, framing="8N1", inverted=False):
    """Recover async-serial byte frames from one line - the whole UART
    family's front end. `baud` 0 auto-detects the rate. Returns
    (frames, note, detected_baud, bit_samples): on success note is "" and
    the caller composes its own; on a record that cannot be read, frames is
    empty and note says why (detected_baud 0)."""
    data_bits, parity, stop_bits = parse_framing(framing)
    if sig.n < 16:
        return [], "record too short", 0, 0.0
    if sig.span < 8:
        return [], "no signal on this source", 0, 0.0
    want = int(baud or 0)
    if want > 0:
        detected = want
    else:
        shortest = _shortest_run(sig)
        if shortest <= 0 or shortest >= sig.n:
            return [], "no edges found - is this the right source?", 0, 0.0
        detected = _snap_baud(1.0 / (shortest * sig.dt))
    bit_samples = 1.0 / (detected * sig.dt)
    if bit_samples < _MIN_SAMPLES_PER_BIT:
        return [], "too few samples per bit - slow the timebase", 0, 0.0
    frames = _serial_frames(sig, bit_samples, data_bits, parity, stop_bits,
                            inverted)
    return frames, "", detected, bit_samples


# ---- a bundled library folder -------------------------------------------

def sync_folder(bundled, folder):
    """Make `folder` exist and hold every file the `bundled` copy has,
    copying in only the ones not already there. Shared by the decoder and
    mask libraries and run every time either is read, so a file added in an
    update appears without the user reseeding - while one they wrote, edited
    or added of their own is left untouched. Returns `folder`.

    Copy-missing, not seed-once: the old first-run-only seed meant a file
    added to a later build never reached anyone who already had the folder.
    The trade is that a bundled file the user deletes comes back next run;
    the bundled set is meant to be present, so that is the right default.
    """
    if not os.path.isdir(folder):
        os.makedirs(folder)
    if (os.path.isdir(bundled)
            and os.path.abspath(bundled) != os.path.abspath(folder)):
        for name in os.listdir(bundled):
            src = os.path.join(bundled, name)
            dst = os.path.join(folder, name)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
    return folder


# ---- discovery ----------------------------------------------------------

def discover(folders):
    """Load every decoder found in the given folders, later folders
    overriding earlier ones by NAME (so a user's copy wins over the
    bundled one). Returns Decoder instances sorted by NAME.

    A plugin is a .py file defining one or more Decoder subclasses. Files
    beginning with '_' are helpers, not plugins, and are skipped - which is
    how _template.py stays a template and not an empty protocol in the list.
    """
    found = {}
    order = 0
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for path in sorted(glob.glob(os.path.join(folder, "*.py"))):
            name = os.path.basename(path)
            if name.startswith("_"):
                continue
            for cls in _classes_in(path):
                found[cls.NAME] = cls
    out = [cls() for cls in found.values()]
    out.sort(key=lambda d: d.NAME)
    return out


def _classes_in(path):
    """Import one plugin file in isolation and return its Decoder
    subclasses. A file that will not import is skipped with a note rather
    than taking the whole list down - one bad plugin must not hide the
    good ones."""
    mod_name = "_tdsdecode_" + os.path.splitext(os.path.basename(path))[0]
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:                  # noqa: BLE001 - report, go on
        _note_bad(path, exc)
        return []
    out = []
    for value in vars(module).values():
        if (isinstance(value, type) and issubclass(value, Decoder)
                and value is not Decoder and getattr(value, "NAME", "?")
                not in ("?", None)):
            out.append(value)
    return out


def _note_bad(path, exc):
    try:
        from tdstoolkit import log_note
        log_note("decode", "plugin %s will not load: %s"
                 % (os.path.basename(path), exc))
    except Exception:
        pass
