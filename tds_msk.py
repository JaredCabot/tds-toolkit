"""Masks: the shapes, the file they live in, and the limits on both.

A TDS500D/700D mask is up to eight segments, each a closed polygon given
as x,y pairs in percent of the graticule - `MASK:MASK<n>:POINTSPCNT`.
That is the whole of the instrument's idea of a mask. There is no
SAVE:MASK, no RECALL:MASK, and no mask file anywhere on a 784D's disk:
its mask-testing applications are Java and keep their definitions inside
their own .JAR files. Measured, not assumed - see INSTRUMENT-NOTES.

So there is no Tektronix mask file format on this generation to adopt,
and the nearest thing to one is the instrument's own numbers. This file
format is exactly that: the POINTSPCNT lists, in percent, one line each,
with a short header. Sending a mask is then replaying the file, and a
mask can be read by a person, kept in version control, and typed by hand
if it comes to that.

    TDSMASK 1
    NAME    Eye 622Mb
    SOURCE  CH1
    INVERT  0
    MARGIN  0 5.0
    SEG1    21.5,33.25,38,12.5,61.75,12.5,78.5,33.25
    SEG2    10,90,90,90,90,60,10,60

Plain ASCII with CRLF line endings, because it has to sit on the
instrument's own FAT filesystem beside files it wrote itself, and be
readable on a PC. The instrument's filesystem is 8.3, so the name a mask
is stored under is not the name it carries inside it - hence NAME.
"""

# What the instrument will accept, measured on a 784D and a 784C rather
# than read out of a manual:
#
#   * eight segments. MASK1..MASK8 answer; MASK9 and beyond give nothing.
#   * fifty points to a segment. Asking for 64 stored 50 and left an
#     event in the queue.
#   * a single point is refused: the segment comes back 0,0. Two is the
#     fewest that stays.
#
# Percent of the graticule, so 0..100 across and 0..100 up. Nothing in
# the file format clamps to that range: a mask that arrives with a
# point off the graticule keeps it, because a mistake in somebody's
# file is worth showing rather than quietly moving. What is clamped is
# the *drawing* - see held() - so no gesture can put one there.
SEGMENTS = 8
POINTS_PER_SEGMENT = 50
MIN_POINTS = 2
OUTWARD_LIMIT = 1.0               # a tenth of a division, in percent

MAGIC = "TDSMASK"
VERSION = 1
SUFFIX = ".MSK"


class MaskError(ValueError):
    """A mask that cannot be represented, with the reason in words."""


class Mask(object):
    """One mask: a name, up to eight polygons, and how it is tested.

    The polygons are held exactly as the instrument holds them - percent
    of the graticule - rather than in volts and seconds. That is a
    deliberate choice and worth stating: a mask in percent is the same
    mask at any timebase, which is what makes one worth saving and
    sending to a differently-configured instrument. Volts and seconds
    would tie it to the settings it was drawn against.
    """

    def __init__(self, name="", segments=None, source="CH1", invert=False,
                 margin=False, margin_percent=5.0):
        self.name = name or ""
        # A list of lists of (x, y). Empty lists are kept, so segment
        # three staying empty while four holds something is expressible.
        self.segments = [list(seg) for seg in (segments or [])]
        self.source = source or "CH1"
        self.invert = bool(invert)
        self.margin = bool(margin)
        self.margin_percent = float(margin_percent)
        self.origin = None            # which file it came from, if any

    # -- what is in it -------------------------------------------------

    @property
    def points(self):
        """Every point in every segment, for a count or a bounding box."""
        return [p for seg in self.segments for p in seg]

    def filled(self):
        """The segments that actually hold a polygon, by number from 1."""
        return [(i + 1, seg) for i, seg in enumerate(self.segments)
                if len(seg) >= MIN_POINTS]

    def bounds(self):
        """(left, bottom, right, top) in percent, or None if empty."""
        pts = self.points
        if not pts:
            return None
        xs = [x for x, _y in pts]
        ys = [y for _x, y in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def complaints(self):
        """Everything about this mask an instrument would refuse.

        Returned rather than raised, and all of them at once: somebody
        who has drawn too many points in two segments should be told
        about both, not sent round the loop twice.
        """
        out = []
        if len(self.segments) > SEGMENTS:
            out.append("%d segments; the instrument holds %d"
                       % (len(self.segments), SEGMENTS))
        for n, seg in enumerate(self.segments[:SEGMENTS], 1):
            if len(seg) > POINTS_PER_SEGMENT:
                out.append("segment %d has %d points; the instrument "
                           "keeps %d and drops the rest"
                           % (n, len(seg), POINTS_PER_SEGMENT))
            if 0 < len(seg) < MIN_POINTS:
                out.append("segment %d has one point; the instrument "
                           "refuses a segment with fewer than %d"
                           % (n, MIN_POINTS))
            for x, y in seg:
                if not (0.0 <= x <= 100.0 and 0.0 <= y <= 100.0):
                    out.append("segment %d has a point at %g,%g, which is "
                               "off the graticule" % (n, x, y))
                    break
        if not self.filled():
            out.append("no segment holds a shape")
        return out

    def redrawn(self):
        """[(number, why)] for segments the instrument would not draw true.

        Not a complaint: the instrument takes the points and keeps them,
        it simply joins them up its own way. See redraw_reason.
        """
        found = [(n, redraw_reason(seg)) for n, seg in self.filled()]
        return [(n, why) for n, why in found if why]

    # -- the instrument ------------------------------------------------

    def to_scpi(self):
        """The commands that put this mask on an instrument.

        Y is flipped. The instrument's origin is the upper left, 0,0 to
        100,100, while a mask here is percent UP the graticule -
        photographed on a 784D to be sure of the direction.

        MASK:STANDARD is deliberately not written. It "deletes the
        existing mask and sets the standard mask", so sending it after
        the points throws them away; a user mask is points with the
        standard left alone.

        Every segment is written, including the empty ones as a single
        0,0 - which is how the instrument says "nothing here". Writing
        only the filled ones would leave whatever the last mask put in
        the others, and half of somebody else's mask is worse than none.
        """
        out = []
        for n in range(1, SEGMENTS + 1):
            seg = self.segments[n - 1] if n <= len(self.segments) else []
            seg = seg[:POINTS_PER_SEGMENT]
            if len(seg) < MIN_POINTS:
                out.append("MASK:MASK%d:POINTSPCNT 0.0,0.0" % n)
                continue
            out.append("MASK:MASK%d:POINTSPCNT %s"
                       % (n, ",".join("%g,%g" % (x, 100.0 - y)
                                      for x, y in seg)))
        out.append("MASK:SOURCE %s" % self.source)
        out.append("MASK:INVERT %d" % (1 if self.invert else 0))
        out.append("MASK:MARGIN:STATE %d" % (1 if self.margin else 0))
        out.append("MASK:MARGIN:PERCENT %g" % self.margin_percent)
        return out

    @classmethod
    def from_scpi(cls, replies, name="", **rest):
        """A mask from what the instrument answered, one reply a segment.

        Y is flipped back: the instrument's origin is the upper left.

        Measured on a 784D from a clean setup, points round-trip
        exactly. The constant X this used to warn about was stale state,
        not firmware - see INSTRUMENT-NOTES.
        """
        segments = []
        for reply in replies:
            numbers = []
            # An instrument with HEADER ON answers
            # ":MASK:MASK1:POINTSPCNT 20.0,80.0,..." and the caller may
            # not have stripped it. Without this the first field is not
            # a number, the whole segment is dropped, and a mask read
            # back off a 794D comes home empty and blameless.
            reply = str(reply).strip()
            if reply.startswith(":") and " " in reply:
                reply = reply.split(" ", 1)[1]
            for part in reply.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    numbers.append(float(part))
                except ValueError:
                    numbers = []
                    break
            pairs = [(x, 100.0 - y)
                     for x, y in zip(numbers[0::2], numbers[1::2])]
            if len(pairs) == 1 and pairs[0] == (0.0, 100.0):
                pairs = []            # 0,0 sent; the instrument's empty
            segments.append(pairs)
        return cls(name=name, segments=segments, **rest)

    # -- the file ------------------------------------------------------

    def to_text(self):
        """The file, as text with CRLF endings."""
        lines = ["%s %d" % (MAGIC, VERSION),
                 "NAME    %s" % (self.name or ""),
                 "SOURCE  %s" % self.source,
                 "INVERT  %d" % (1 if self.invert else 0),
                 "MARGIN  %d %g" % (1 if self.margin else 0,
                                    self.margin_percent)]
        for n, seg in enumerate(self.segments, 1):
            if len(seg) < MIN_POINTS:
                continue
            lines.append("SEG%-4d %s"
                         % (n, ",".join("%g,%g" % (x, y) for x, y in seg)))
        return "\r\n".join(lines) + "\r\n"

    def to_bytes(self):
        return self.to_text().encode("ascii", "replace")

    @classmethod
    def from_text(cls, text):
        """A mask from a file. Raises MaskError on anything it cannot read.

        Strict about the first word and forgiving about everything else:
        a file that is not a mask should say so at once rather than
        producing an empty mask that looks like a mask somebody lost.
        """
        if isinstance(text, bytes):
            text = text.decode("ascii", "replace")
        lines = [ln.strip() for ln in text.replace("\r\n", "\n")
                 .replace("\r", "\n").split("\n")]
        lines = [ln for ln in lines if ln and not ln.startswith("#")]
        if not lines or not lines[0].upper().startswith(MAGIC):
            raise MaskError("this is not a mask file: it begins %r"
                            % (lines[0][:40] if lines else ""))
        try:
            version = int(lines[0].split()[1])
        except (IndexError, ValueError):
            version = VERSION
        if version > VERSION:
            raise MaskError("this mask was written by a later version of "
                            "the program (%d, and this reads %d)"
                            % (version, VERSION))
        mask = cls()
        segments = {}
        for line in lines[1:]:
            word, _sep, rest = line.partition(" ")
            word, rest = word.upper(), rest.strip()
            if word == "NAME":
                mask.name = rest
            elif word == "SOURCE":
                mask.source = rest or "CH1"
            elif word == "INVERT":
                mask.invert = rest.strip() not in ("", "0", "OFF")
            elif word == "MARGIN":
                bits = rest.split()
                mask.margin = bool(bits) and bits[0] not in ("0", "OFF")
                if len(bits) > 1:
                    try:
                        mask.margin_percent = float(bits[1])
                    except ValueError:
                        pass
            elif word.startswith("SEG"):
                try:
                    n = int(word[3:])
                except ValueError:
                    raise MaskError("%r is not a segment number" % word)
                numbers = []
                for part in rest.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    try:
                        numbers.append(float(part))
                    except ValueError:
                        raise MaskError("segment %d has %r in it, which is "
                                        "not a number" % (n, part[:20]))
                if len(numbers) % 2:
                    raise MaskError("segment %d has %d numbers, which is "
                                    "not a whole number of points"
                                    % (n, len(numbers)))
                segments[n] = list(zip(numbers[0::2], numbers[1::2]))
        if segments:
            mask.segments = [segments.get(n, [])
                             for n in range(1, max(segments) + 1)]
        return mask


def clean_name(name, taken=()):
    """An 8.3 file name for a mask, unique among those already there.

    What the save dialog offers when a mask has a name but no file yet.
    8.3 and upper case because that is the shape every mask file this
    program has ever met is in - Tektronix's ten included - and a
    library where one file is called OC3.MSK and the next "Eye diagram
    622Mb.MSK" reads as two libraries. The mask keeps its real name
    inside itself; this is only what it is stored under.
    """
    keep = []
    for ch in (name or "MASK").upper():
        if ch.isalnum():
            keep.append(ch)
        elif keep and keep[-1] != "_":
            keep.append("_")
    stem = ("".join(keep).strip("_") or "MASK")[:8]
    used = set(str(t).upper() for t in taken)
    if (stem + SUFFIX) not in used:
        return stem + SUFFIX
    for n in range(1, 1000):
        tail = str(n)
        candidate = stem[:8 - len(tail)] + tail + SUFFIX
        if candidate not in used:
            return candidate
    raise MaskError("no free name is left for %r" % name)


# ---------------------------------------------------------------- drawing
# A mask is in percent of the graticule and the graticule is wherever
# plot_frame put it, so everything below converts between the two. Kept
# here rather than in the window because it is arithmetic, and
# arithmetic can be checked without opening one.


def to_canvas(x, y, frame):
    """A percent point to a pixel in this frame.

    `frame` is (left, top, right, bottom) as plot_frame gives it. Y is
    the way a person means it - 0 at the bottom of the graticule, 100 at
    the top - and the instrument agrees, so the flip lives here and
    nowhere else.
    """
    left, top, right, bottom = frame
    return (left + (right - left) * x / 100.0,
            bottom - (bottom - top) * y / 100.0)


def from_canvas(px, py, frame):
    """A pixel back to percent. The inverse of to_canvas, exactly."""
    left, top, right, bottom = frame
    wide = max(1e-9, right - left)
    tall = max(1e-9, bottom - top)
    return ((px - left) / wide * 100.0, (bottom - py) / tall * 100.0)


def held(x, y):
    """A point kept on the graticule: 0 to 100 in both directions.

    Applied to gestures rather than to the data. A mask that arrives
    from a file with a point off the graticule keeps it - moving
    somebody's file for them hides the fault - but nothing drawn here
    can go there, because a point off the graticule is a point the
    instrument cannot draw and nobody can see to drag back.
    """
    return (min(100.0, max(0.0, x)), min(100.0, max(0.0, y)))


def holds(points):
    """Is every one of these on the graticule?"""
    return all(0.0 <= x <= 100.0 and 0.0 <= y <= 100.0 for x, y in points)


def snapped(value, step):
    """To the nearest multiple of `step`, or unchanged if there is none.

    A step of zero or less means no grid, which is how the setting is
    switched off - rather than a separate flag that can disagree with it.
    """
    if not step or step <= 0:
        return value
    # floor(v/step + 0.5), not round(): Python's round is banker's
    # rounding, so a point exactly halfway between two grid lines goes
    # to the even one - 12.5 on a 5 grid snaps to 10 and 17.5 snaps to
    # 20. On a drawing grid that is a point that moves the wrong way for
    # no reason anybody could see.
    import math
    return math.floor(value / float(step) + 0.5) * float(step)


def snap_point(x, y, step, ystep=None):
    """A point on the grid. Two steps, because a division is not square.

    The graticule is ten divisions across and eight down, so one
    division is 10% of the width and 12.5% of the height. A grid set in
    divisions therefore has a different step in each direction, and a
    single step would put the horizontal lines somewhere the instrument
    draws nothing.
    """
    return (snapped(x, step), snapped(y, step if ystep is None else ystep))


def near_point(mask, x, y, within):
    """Which point of which segment is within `within` percent of here.

    Returns (segment index, point index) counting from zero, or None.
    The nearest one wins, so two points on top of each other still pick
    the closer, and a tie picks the earlier - which is the one drawn
    first and so the one underneath.
    """
    best, found = None, None
    for s, seg in enumerate(mask.segments):
        for p, (px, py) in enumerate(seg):
            gap = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
            if gap <= within and (best is None or gap < best):
                best, found = gap, (s, p)
    return found


def near_edge(mask, x, y, within):
    """Which edge is within `within` percent of here, and where on it.

    Returns (segment, point index of the edge's start, x, y of the
    closest place on it) or None. The point returned is on the edge, so
    inserting there does not move the shape - clicking an edge to add a
    point should add it where the pointer is, not near it.

    Every segment is closed, so the edge from the last point back to the
    first counts too. Leaving it out means the one edge that is hardest
    to see is also the one you cannot click.
    """
    best, found = None, None
    for s, seg in enumerate(mask.segments):
        if len(seg) < MIN_POINTS:
            continue
        for p in range(len(seg)):
            ax, ay = seg[p]
            bx, by = seg[(p + 1) % len(seg)]
            dx, dy = bx - ax, by - ay
            span = dx * dx + dy * dy
            if span <= 0:
                continue
            along = ((x - ax) * dx + (y - ay) * dy) / span
            along = min(1.0, max(0.0, along))
            cx, cy = ax + dx * along, ay + dy * along
            gap = ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5
            if gap <= within and (best is None or gap < best):
                best, found = gap, (s, p, cx, cy)
    return found


def inside(seg, x, y):
    """Is this point inside this polygon? The even-odd rule.

    Used to say which segment a click landed in when it did not land on
    a point or an edge - dragging a whole shape has to know which shape.
    """
    if len(seg) < 3:
        return False
    hits = False
    for i in range(len(seg)):
        ax, ay = seg[i]
        bx, by = seg[(i + 1) % len(seg)]
        if (ay > y) != (by > y):
            cross = ax + (y - ay) * (bx - ax) / ((by - ay) or 1e-12)
            if x < cross:
                hits = not hits
    return hits


def segment_at(mask, x, y):
    """Which segment contains this point, topmost first, or None.

    Later segments are drawn over earlier ones, so a click in the
    overlap belongs to the later one - the one you can see.
    """
    for s in range(len(mask.segments) - 1, -1, -1):
        if inside(mask.segments[s], x, y):
            return s
    return None


# --------------------------------------------------- Tektronix i-Pattern
# The mask format from TTiP - "Telecommunications Templates and
# i-Pattern", Tektronix 1993/1996 - which is the only Tektronix mask file
# format for this generation of instrument. Ten reference masks came with
# it: OC1, OC3, STS1, STS3, STM1, DS4 and so on.
#
# Decoded from the files themselves; the user manual documents the
# software but never states the format. What it does confirm is "You can
# use up to 50 points to define a single mask" and "A maximum of ten
# masks can be defined on a single waveform" - and 50 points a mask is
# exactly what the instrument's own MASK subsystem turned out to accept,
# measured separately over GPIB. Two independent routes to one number is
# worth more than either alone.
#
#     offset   0   int16       how many masks
#     offset   2   int16 x N   points in each mask
#     offset  26   N x 50 x (int16 x, int16 y)
#                  x is 0..511, y is -127..+127, little-endian
#
# Every file shipped is 2023 bytes, which is 26 + 8 x 200 with 397 left
# over, or 26 + 10 x 200 three bytes short. The manual says ten and the
# spare tail is three bytes shy of two more masks, so ten is likely and
# eight is what the files ever use. Reading is bounded by what the file
# can actually hold rather than by either guess.
MSK_HEAD = 26
MSK_STRIDE = 200                  # 50 points x two int16
MSK_MAX = 10                      # the manual's maximum
MSK_BYTES = 2023                  # what every shipped .MSK measures
# What the shipped masks are for. Nothing in a .MSK file says: it holds
# a mask count, the point counts and the points, and not one byte of
# text - checked across all ten. So the only thing left to go on is the
# name, and this table is Tektronix's own, from the README on TTiP
# Disk 1. Names as the files are called, which is not quite what the
# README's table calls them - it lists DS4N and DS4XN, the files are
# DS4NA.MSK and DS4XNA.MSK.
STANDARDS = {
    "STS1": ("ANSI T1.102", "51.84 Mb/s"),
    "STS1NEW": ("ANSI T1X1.4/93-014", "51.84 Mb/s"),
    "OC1": ("ANSI SONET/CCITT SDH", "51.84 Mb/s"),
    "DS4NA": ("ANSI T1.102", "139.264 Mb/s"),
    "DS4XNA": ("ANSI T1.102", "139.264 Mb/s"),
    "OC3": ("ANSI SONET/CCITT SDH", "155.52 Mb/s"),
    "STM1": ("ANSI SONET/CCITT SDH", "155.52 Mb/s"),
    "STS3": ("ANSI T1.102", "155.52 Mb/s"),
    "STSX3": ("ANSI T1.102", "155.52 Mb/s"),
    # Not in the README's table of standards: it is the example eye that
    # comes with 50OHMEYE.BIN, so it is named rather than left blank.
    "50OHMMSK": ("", "50 ohm example"),
}


def standard(name):
    """(standard, signal) for a mask by name, or ("", "").

    By name because there is nowhere else for it to come from. A mask
    somebody drew here is not in the table and gets nothing, which is
    right - inventing a standard for it would be worse than silence.
    """
    stem = str(name or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    return STANDARDS.get(stem.upper(), ("", ""))


# What the coordinates mean. **This is inference, not documentation.**
# The manual never gives the range. The evidence for it:
#
#   * every value seen lies in 0..511 across and -127..+127 up;
#   * the reference masks' outer segments deliberately run corner to
#     corner - STS1 uses 0 and 511 and both of +127 and -127 - which
#     only makes sense if those are the edges of the display;
#   * the records they were drawn against are 500 points long, and the
#     masks use 497, 510 and 511, so x is display space and not record
#     position;
#   * i-Pattern is a 1993 DOS program, and 512 x 255 is the natural
#     raster for one.
#
# Named rather than written into the arithmetic so that if somebody ever
# looks at a scope and finds otherwise, there is one place to change.
MSK_X_FULL = 511.0
MSK_Y_FULL = 127.0


def msk_to_percent(x, y):
    """An i-Pattern coordinate as percent of the graticule."""
    return (x / MSK_X_FULL * 100.0,
            (y + MSK_Y_FULL) / (2.0 * MSK_Y_FULL) * 100.0)


def percent_to_msk(x, y):
    """Percent of the graticule back to an i-Pattern coordinate.

    Rounded to whole numbers because the format holds integers, and
    clamped because a point off the graticule cannot be written at all -
    the alternative is a silently wrapped int16.
    """
    import math
    px = int(math.floor(x / 100.0 * MSK_X_FULL + 0.5))
    py = int(math.floor((y / 100.0 * 2.0 - 1.0) * MSK_Y_FULL + 0.5))
    return (min(int(MSK_X_FULL), max(0, px)),
            min(int(MSK_Y_FULL), max(-int(MSK_Y_FULL), py)))


def stretched(segments, across, up, about=(50.0, 50.0)):
    """Every point moved away from `about` by these two factors.

    What a mask is held in is percent of the graticule, and that is
    what makes this possible: a scaling the instrument would not give
    can be answered by moving the mask instead of the instrument. Asked
    for 10 ns a division and given 12.5, the same signal covers eight
    tenths of the width the mask was drawn against, and a mask eight
    tenths as wide about the trigger point says exactly what it said.

    `about` is where each axis is measured from, and the two are not
    the same place: across, the trigger point, because that is what
    stays put when the timebase changes; up, the channel's zero volts,
    because that is what stays put when the gain does.
    """
    x0, y0 = about
    return [[(x0 + (x - x0) * across, y0 + (y - y0) * up) for x, y in seg]
            for seg in segments]


def on_graticule(segments):
    """Do all these points still land on the screen?"""
    return all(0.0 <= x <= 100.0 and 0.0 <= y <= 100.0
               for seg in segments for x, y in seg)


def convex_polygon(seg):
    """Is this segment convex, taken in the order it was drawn?

    Not a rule about what a mask may be - the instrument holds and draws
    a concave segment perfectly well, and four of the ten masks
    Tektronix ship are concave. Kept because a convex shape needs no
    triangulating before a boolean, and because the answer is worth
    knowing. See INSTRUMENT-NOTES.

    Every corner has to turn the same way. A shape that turns both ways
    is concave; one that crosses itself turns both ways too, so this
    catches a bowtie as well - which is what a careless drag makes and
    what neither the instrument nor an i-Pattern file can hold.

    Two points are a line and have no corners to turn, so they pass.
    """
    if len(seg) < 3:
        return True
    sign = 0.0
    for i in range(len(seg)):
        a, b, c = seg[i - 1], seg[i], seg[(i + 1) % len(seg)]
        turn = ((b[0] - a[0]) * (c[1] - b[1])
                - (b[1] - a[1]) * (c[0] - b[0]))
        if abs(turn) < 1e-9:
            continue                        # three in a line, no turn
        if sign and (turn > 0) != (sign > 0):
            return False
        sign = turn
    return True


def _reversals(seg, axis):
    """How many times the outline turns back on itself along one axis."""
    values = [p[axis] for p in seg]
    steps = [values[(i + 1) % len(seg)] - values[i] for i in range(len(seg))]
    ups = [step > 0 for step in steps if step]
    return sum(1 for i in range(len(ups)) if ups[i] != ups[i - 1])


def redraw_reason(seg):
    """Why the instrument would not draw this segment as it is here.

    An empty string means it draws it exactly. Measured on a 784D
    (v7.4e) by sending fifteen shapes with the channel switched off,
    reading the screen back and comparing the mask's own ink with the
    polygon that was sent; the readback of POINTSPCNT always came back
    as it was written, so what changes is the drawing, not the points.

    Two things break it, and concavity is not one of them - an L, a T,
    a cross and a staircase all came back exactly as drawn:

    * The outline may run back on itself only once in each direction:
      no horizontal or vertical line may cross it more than twice. A U
      and a chevron (twice up in y) and a shape notched from the side
      (twice across in x) were all drawn as some other polygon through
      the same points.
    * Three points in a row on the same vertical are one too many: the
      instrument drops one of them. Three on the same horizontal are
      fine, and so are three in a line on a slope, and so are four
      points sharing a column when they are not consecutive - the cross
      has four at x=40 and draws perfectly.

    See INSTRUMENT-NOTES for the measurements themselves.
    """
    if len(seg) < 3:
        return ""
    for i in range(len(seg)):
        if seg[i - 1][0] == seg[i][0] == seg[(i + 1) % len(seg)][0]:
            return ("three points in a line one above the other; the "
                    "instrument drops one of them")
    if _reversals(seg, 1) > 2:
        return ("a vertical line can cross it more than twice; the "
                "instrument draws some other shape through the points")
    if _reversals(seg, 0) > 2:
        return ("a horizontal line can cross it more than twice; the "
                "instrument draws some other shape through the points")
    return ""


def convex_order(points):
    """The same points, ordered so the outline does not cross itself.

    An i-Pattern mask is a *convex region* and its points are a set, not
    a path: the manual says that a concave area has to be made from
    several masks, and the shipped files bear it out - OC1's eye is
    listed as (17,50) (36,32) (83,50) (36,68) (64,68) (64,32), which
    drawn in that order is a bowtie. Sorted about the centre it is the
    hexagon it is meant to be.

    Sorted by angle rather than run through a convex hull, because a
    hull would silently drop any point that came out inside - and a
    point somebody put there deliberately should survive being read and
    written back.
    """
    if len(points) < 3:
        return list(points)
    import math
    cx = sum(x for x, _y in points) / float(len(points))
    cy = sum(y for _x, y in points) / float(len(points))
    return sorted(points, key=lambda p: (math.atan2(p[1] - cy, p[0] - cx),
                                         (p[0] - cx) ** 2 + (p[1] - cy) ** 2))


def looks_like_msk(data):
    """Is this an i-Pattern mask file rather than one of ours?"""
    if len(data) < MSK_HEAD + MSK_STRIDE:
        return False
    if data[:len(MAGIC)] == MAGIC.encode("ascii"):
        return False
    import struct
    count = struct.unpack_from("<h", data, 0)[0]
    if not 1 <= count <= MSK_MAX:
        return False
    room = (len(data) - MSK_HEAD) // MSK_STRIDE
    if count > max(room, 1) + 1:
        return False
    counts = struct.unpack_from("<%dh" % count, data, 2)
    return all(0 <= n <= POINTS_PER_SEGMENT for n in counts)


def from_msk(data, name=""):
    """A Mask from a Tektronix i-Pattern .MSK file."""
    import struct
    if len(data) < MSK_HEAD + MSK_STRIDE:
        raise MaskError("an i-Pattern mask is at least %d bytes and this "
                        "is %d" % (MSK_HEAD + MSK_STRIDE, len(data)))
    count = struct.unpack_from("<h", data, 0)[0]
    if not 1 <= count <= MSK_MAX:
        raise MaskError("this file says it holds %d masks, and an "
                        "i-Pattern file holds 1 to %d" % (count, MSK_MAX))
    counts = struct.unpack_from("<%dh" % count, data, 2)
    segments = []
    for i in range(count):
        n = counts[i]
        if not 0 <= n <= POINTS_PER_SEGMENT:
            raise MaskError("mask %d claims %d points and the most is %d"
                            % (i + 1, n, POINTS_PER_SEGMENT))
        at = MSK_HEAD + i * MSK_STRIDE
        if at + n * 4 > len(data):
            raise MaskError("mask %d runs past the end of the file" % (i + 1))
        words = struct.unpack_from("<%dh" % (n * 2), data, at) if n else ()
        segments.append(convex_order([msk_to_percent(x, y)
                                      for x, y in zip(words[0::2],
                                                      words[1::2])]))
    return Mask(name=name, segments=segments)


def to_msk(mask):
    """A Tektronix i-Pattern .MSK file from a Mask.

    Written the size the shipped files are, so it sits beside them
    without looking odd, and zero-filled beyond what is used - which is
    what the originals do.
    """
    import struct
    segs = [seg[:POINTS_PER_SEGMENT] for seg in mask.segments]
    while segs and not segs[-1]:
        segs.pop()                # trailing empties say nothing
    if not segs:
        raise MaskError("there is no shape to write")
    # What fits, not what the count field can say. MSK_MAX is the range
    # of that field; a file this size has room for one whole stride
    # fewer, which is why from_msk refuses to read a tenth mask out of
    # one. Without this the tenth ran off the end of the buffer and
    # struct raised, escaping the module's own MaskError contract.
    room = (MSK_BYTES - MSK_HEAD) // MSK_STRIDE
    if len(segs) > room:
        raise MaskError("%d masks; a %d-byte i-Pattern file holds %d"
                        % (len(segs), MSK_BYTES, room))
    out = bytearray(MSK_BYTES)
    struct.pack_into("<h", out, 0, len(segs))
    for i, seg in enumerate(segs):
        struct.pack_into("<h", out, 2 + i * 2, len(seg))
        at = MSK_HEAD + i * MSK_STRIDE
        for j, (x, y) in enumerate(seg):
            mx, my = percent_to_msk(x, y)
            struct.pack_into("<hh", out, at + j * 4, mx, my)
    return bytes(out)


def load(data, name=""):
    """A mask from a file of either format, told apart by what is in it.

    An i-Pattern file is binary and a file of ours begins with TDSMASK,
    so nothing has to be inferred from the extension - which matters,
    because both are called .MSK and the shipped Tektronix ones have to
    keep the name they came with.
    """
    if isinstance(data, str):
        data = data.encode("ascii", "replace")
    if looks_like_msk(data):
        return from_msk(data, name=name)
    return Mask.from_text(data)


def save_bytes(mask, as_ipattern):
    """The bytes to write, in whichever format was asked for."""
    return to_msk(mask) if as_ipattern else mask.to_bytes()


# ------------------------------------------------------ limit envelopes
# The way Tektronix actually put a limit on one of these instruments was
# not the MASK subsystem at all. TTiP's `.ENV` files are plain SCPI that
# load an *envelope* - a lower and upper limit per column - into a
# reference memory, and then LIMIT:COMPARE tests a channel against it:
#
#     :DATA:DESTINATION REF1;ENCDG ASCII;WIDTH 2;START 1;STOP 1000
#     :ALLOCATE:WAVEFORM:REF1 1000
#     :WFMPRE:PT_FMT ENV;NR_PT 1000;...
#     :CURVE -32767,32767, ..., -19989,20089, ...
#
# NR_PT counts the *values*, so 500 columns is NR_PT 1000. Read off the
# shipped cc0_155m.env, which holds exactly 1000 numbers.
#
# This matters more than it looks: LIMIT: answers on all three
# instruments here, including the 640A, which has no mask subsystem at
# all - and it goes through the reference upload this program already
# has, so it never touches the POINTSPCNT parser whose X coordinate
# cannot be trusted.
ENV_COLUMNS = 500
ENV_COUNTS_PER_DIV = 6400.0       # sixteen-bit; eight bit is 25
ENV_NO_LIMIT = 32767              # what the shipped files use for "free"
ENV_DIVS_Y = 8


def percent_to_counts(percent, divisions=ENV_DIVS_Y):
    """Percent up the graticule as the instrument's own sixteen-bit counts.

    The centre line is zero, and the top of the graticule is four
    divisions above it. 32767 counts is 5.12 divisions - the digitiser's
    full reach, past the edge of the graticule - which is why the
    shipped envelopes use it to mean "no limit here".
    """
    return int(round((percent - 50.0) / 100.0 * divisions
                     * ENV_COUNTS_PER_DIV))


def _spans_at(seg, x):
    """The y intervals a vertical line at `x` cuts out of this polygon."""
    cuts = []
    n = len(seg)
    for i in range(n):
        ax, ay = seg[i]
        bx, by = seg[(i + 1) % n]
        if ax == bx:
            continue
        lo, hi = (ax, bx) if ax < bx else (bx, ax)
        if not (lo <= x < hi):
            continue
        cuts.append(ay + (by - ay) * (x - ax) / (bx - ax))
    cuts.sort()
    return [(cuts[i], cuts[i + 1]) for i in range(0, len(cuts) - 1, 2)]


def to_envelope(mask, columns=ENV_COLUMNS, centre=50.0):
    """A mask as (lower, upper) limits per column, in percent.

    A mask says where the signal may **not** go; an envelope says where
    it **may**. So for each column the shapes are cut by a vertical
    line, and the allowed band is the gap in what they cover that holds
    the centre line.

    That definition does the right thing on a pulse mask - an upper
    keep-out and a lower one, with the pulse between them - and it
    correctly refuses an eye mask, where a shape sits *on* the centre
    and there is no single band for a signal to live in. An envelope is
    one band; an eye is two. Saying so is better than quietly emitting
    an envelope that permits half the eye.

    Columns with nothing above or below are left open, and the caller
    writes the instrument's own "no limit" for those.
    """
    lower, upper = [], []
    blocked = 0
    shapes = [seg for _n, seg in mask.filled()]
    for i in range(columns):
        x = (i + 0.5) / float(columns) * 100.0
        covered = []
        for seg in shapes:
            covered.extend(_spans_at(seg, x))
        low, high = None, None
        shut = False
        for a, b in covered:
            if a <= centre <= b:
                shut = True
                break
            if b < centre:
                low = b if low is None else max(low, b)
            elif a > centre:
                high = a if high is None else min(high, a)
        if shut:
            blocked += 1
            lower.append(centre)
            upper.append(centre)
            continue
        lower.append(low)
        upper.append(high)
    return lower, upper, blocked


def to_band(mask, columns=ENV_COLUMNS):
    """A drawn *allowed area* as (lower, upper) limits per column.

    The inverse of to_envelope, and what the limits tab draws. There a
    shape says where the signal **may** go, which is what a limit
    template is, so no gap-hunting is needed: the band in a column is
    the extent of what is drawn there.

    A column is a *width*, not a place. Measured at its centre alone, a
    steep edge is sliced in half: a band drawn from a learnt envelope
    came back tighter than the envelope in eighteen columns of five
    hundred - all of them at the transitions of a square wave - and the
    template then failed the signal it was learnt from on a 784D. The
    extremes over a column are at its two edges or at a corner inside
    it, so all three are taken and the widest wins. Erring outward is
    the only safe direction: a limit that is too generous passes a
    signal somebody has looked at, and one that is too tight invents a
    failure.

    A limit test permits one band and a drawing can have two - two
    boxes with clear air between them - so a column covered twice is
    closed up to its outermost extent and counted. Counted at the
    centre, because that is a question about the drawing's shape rather
    than about its extremes. The caller says so rather than this
    quietly sending something nobody drew.

    Columns with nothing drawn are left open, and the caller writes the
    instrument's own "no limit" for those.
    """
    lower, upper = [], []
    gaps = 0
    shapes = [seg for _n, seg in mask.filled()]
    corners = [[] for _ in range(columns)]
    for seg in shapes:
        for x, y in seg:
            at = int(x / 100.0 * columns)
            corners[min(columns - 1, max(0, at))].append(y)
    for i in range(columns):
        a = i / float(columns) * 100.0
        b = (i + 1.0) / float(columns) * 100.0
        middle, edges = [], []
        for seg in shapes:
            middle.extend(_spans_at(seg, (a + b) / 2.0))
            edges.extend(_spans_at(seg, a))
            edges.extend(_spans_at(seg, b - 1e-9))
        edges.extend(middle)
        if not edges:
            lower.append(None)
            upper.append(None)
            continue
        if len(middle) > 1:
            gaps += 1
        low = min(lo for lo, _hi in edges)
        high = max(hi for _lo, hi in edges)
        for y in corners[i]:
            low, high = min(low, y), max(high, y)
        lower.append(low)
        upper.append(high)
    return lower, upper, gaps


def _furthest(points, first, last, way=0):
    """The point between `first` and `last` furthest off the line
    joining them, as (distance, index).

    Measured up and down rather than square to the line. An envelope is
    a height at a time, and the distance that costs a limit anything is
    the vertical one: a point beside a near-vertical chord is a hair
    away from it square on, and a whole transition away in volts. That
    blindness is what put a handle off the graticule on a square wave
    of more than a cycle or two - the transitions got no handles at all
    and _outward then had to raise a segment by the height of the edge.

    `way` +1 or -1 measures only the excess above, or below, the chord:
    what the segment would have to be raised, or lowered, to stand
    clear. See thinned.
    """
    ax, ay = points[first]
    bx, by = points[last]
    dx, dy = bx - ax, by - ay
    worst, at = 0.0, first
    for i in range(first + 1, last):
        px, py = points[i]
        if abs(dx) < 1e-12:
            on = min(max(py, min(ay, by)), max(ay, by))
        else:
            on = ay + dy * (px - ax) / dx
        off = (py - on) * way if way else abs(py - on)
        if off > worst:
            worst, at = off, i
    return worst, at


def _outward(points, keep, way):
    """Push the kept line off the points it stands in for.

    A simplified limit is only safe in one direction. Thinned plainly,
    the upper edge of a learnt envelope cuts every corner *inwards*, and
    the template then fails the signal it was learnt from - measured on
    a 784D at 1 kHz, 500 mV/div: the instrument's own template held for
    eight seconds and the same band thinned to fifty handles stopped at
    once, on an unchanged signal. The loss is at the transitions, where
    a straight line across a corner takes away exactly the room the
    horizontal tolerance was there to give.

    So a kept segment is raised, or lowered, until nothing it stands in
    for lies outside it. `way` is +1 for an edge nothing may be above
    and -1 for one nothing may be below. Only ever moves a point
    outward, so it cannot make a template tighter than it was asked to
    be - and three passes, because moving one end of a segment moves an
    end of its neighbour.
    """
    for _ in range(3):
        moved = False
        for k in range(len(keep) - 1):
            first, last = keep[k], keep[k + 1]
            if last <= first + 1:
                continue
            ax, ay = points[first]
            bx, by = points[last]
            worst = 0.0
            for i in range(first + 1, last):
                px, py = points[i]
                if abs(bx - ax) < 1e-12:
                    at = max(ay, by) if way > 0 else min(ay, by)
                else:
                    at = ay + (by - ay) * (px - ax) / (bx - ax)
                worst = max(worst, (py - at) * way)
            if worst > 1e-9:
                points[first] = (ax, ay + worst * way)
                points[last] = (bx, by + worst * way)
                moved = True
        if not moved:
            break
    return points


def thinned(points, most, outward=0):
    """`points` reduced to about `most`, keeping the shape.

    `outward` +1 or -1 keeps the answer on one side of what it replaces
    - see _outward. Nothing else here is safe to thin without it: a
    limit that has been simplified inwards invents failures.

    Douglas-Peucker driven by a budget rather than by a tolerance. The
    number that has to be satisfied is the instrument's fifty points to
    a segment, so that is what is asked for, and the tolerance falls out
    of it. With `outward` the budget is a target rather than a ceiling -
    up to twice it may be spent on transitions, which is the difference
    between a template that follows a square wave of several cycles and
    one with a handle off the graticule.

    An envelope read back off an instrument is 250 columns, which is 500
    handles and not a shape anybody can drag. Thinned it is one.
    """
    points = list(points)
    most = max(2, most)
    if len(points) <= most:
        return points
    # Split the worst-fitting stretch, over and over, until the budget
    # is spent. The usual way round - pick a tolerance and see how many
    # points come out - cannot spend a budget: doubling the tolerance
    # from 0.05 gave 34 points where 50 were allowed, and the twelve
    # unspent handles are detail the shape does not have.
    #
    # Each stretch's worst point is remembered, so a split only
    # re-measures the two halves it made rather than everything.
    keep = [0, len(points) - 1]
    gaps = [_furthest(points, keep[0], keep[1])]
    while len(keep) < most:
        k = max(range(len(gaps)), key=lambda i: gaps[i][0])
        off, at = gaps[k]
        if off <= 0.0:              # every stretch is already exact
            break
        keep.insert(k + 1, at)
        gaps[k:k + 1] = [_furthest(points, keep[k], at),
                         _furthest(points, at, keep[k + 2])]
    if outward:
        # Anything still needing a real lift gets a handle instead, over
        # budget if it has to. A segment that straddles a transition can
        # only be raised clear of it by the whole height of the edge,
        # and that moves both its ends - which is a handle off the
        # graticule and a template nobody can tidy. A corner costs one
        # point. The budget is a number somebody can drag, not the
        # instrument's fifty to a segment: a limits drawing goes out as
        # an envelope. Twice the budget is the ceiling, which a square
        # wave of sixteen cycles across the screen does not reach.
        while len(keep) < most * 2:
            worst, at, k = OUTWARD_LIMIT, None, None
            for i in range(len(keep) - 1):
                off, j = _furthest(points, keep[i], keep[i + 1], outward)
                if off > worst:
                    worst, at, k = off, j, i
            if k is None:
                break
            keep.insert(k + 1, at)
        _outward(points, keep, outward)
    return [points[i] for i in keep]


ENV_SCALING = ("XINCR", "XZERO", "PT_OFF", "XUNIT",
               "YMULT", "YZERO", "YOFF", "YUNIT")


def envelope_scpi(mask, pre, dest="REF1", source="CH1", width=1,
                  how="keepout"):
    """The SCPI that puts this mask on an instrument as a limit template.

    `pre` is the preamble of the channel being judged and `width` the
    bytes a sample it was read at. Both matter, and both were settled on
    a 784D against a signal generator:

    The scaling fields are copied, so the reference describes the same
    volts and seconds as the channel rather than whatever was left in
    it - a template written against a 1 V/div channel came back as
    500 mV/div without them.

    YOFF biases every count. Screen centre is raw zero only for a
    channel at position zero; a channel sitting at YOFF 12800 needs the
    band built about 12800. Copying YOFF without biasing puts the band
    two divisions out and fails a good signal.

    YMULT and YOFF are normalised to sixteen bits, because the curve
    below is sixteen bits whatever the channel was read at. An eight-bit
    read of the same channel reports 40 mV a count and YOFF 50 where a
    sixteen-bit read reports 156.25 uV and 12800 - a factor of 256 - and
    copying the eight-bit pair onto a sixteen-bit curve makes the band
    256 times too tall.

    The length comes from the record. A template with fewer values than
    the record has points is not refused - the instrument stretches it
    across the record instead, which is silent and wrong everywhere the
    band is not flat. See the note on `values` below.

    The instrument reports the verdict by stopping, not by answering a
    query - see INSTRUMENT-NOTES.
    """
    # As many values as the record has points, which is what the
    # instrument's own LIMIT:TEMPLATE STORE writes: a 500 point record
    # comes back as NR_PT 500, which is 250 columns of a min and a max.
    # ENV_COLUMNS is only the fallback for a preamble that does not say.
    #
    # Getting this wrong does not fail loudly - the instrument takes the
    # curve and stretches it across the record. Measured on a 784D: a
    # 500 column template into a 500 point record read back as 250
    # columns, so every column landed at twice its own time and the
    # band drawn for one rail sat over the other. A mask's band is the
    # same all the way across, which is why the mask route never showed
    # it; a band drawn to follow a signal is wrong everywhere.
    values = int(float(pre.get("NR_PT") or 0)) or ENV_COLUMNS * 2
    columns = max(2, values // 2)
    values = columns * 2
    if how == "allowed":
        # Drawn on the limits tab, where a shape is the area the
        # signal has to stay inside. Nothing to refuse: any shape
        # is a band. Overlapping columns are counted by to_band
        # and reported by the caller, not raised on here.
        lower, upper, _gaps = to_band(mask, columns)
        blocked = 0
    else:
        lower, upper, blocked = to_envelope(mask, columns)
    if blocked:
        raise MaskError(
            "this is an eye mask: a shape sits on the centre line in %d "
            "of %d columns, so there is no single band for a signal to "
            "pass through. A limit test allows one band and an eye needs "
            "two, above and below the opening. Send it as a mask instead "
            "- which needs a C or D series instrument, because the A "
            "series has no mask subsystem." % (blocked, columns))
    step = 2 ** (16 - 8 * max(1, int(width)))       # 256 for an 8-bit read
    yoff = int(float(pre.get("YOFF") or 0) * step)
    pairs = []
    for lo, hi in zip(lower, upper):
        # The rails mean "no limit here" and are not biased: they are
        # the ends of the sixteen-bit range, not a place on the screen.
        pairs.append(-ENV_NO_LIMIT if lo is None
                     else percent_to_counts(lo) + yoff)
        pairs.append(ENV_NO_LIMIT if hi is None
                     else percent_to_counts(hi) + yoff)
    fixed = dict(pre)
    if pre.get("YMULT"):
        fixed["YMULT"] = "%.6E" % (float(pre["YMULT"]) / step)
    if pre.get("YOFF"):
        fixed["YOFF"] = "%.6E" % yoff
    scaling = ["WFMPRE:%s %s" % (f, fixed[f]) for f in ENV_SCALING
               if fixed.get(f)]
    return [
        'DATA:DESTINATION %s;ENCDG ASCII;WIDTH 2;START 1;STOP %d'
        % (dest, values),
        'ALLOCATE:WAVEFORM:%s %d' % (dest, values),
        'WFMPRE:BYT_NR 2;BIT_NR 16;BN_FMT RI;ENCDG ASC;BYT_OR MSB;'
        'NR_PT %d' % values,
        'WFMPRE:PT_FMT ENV;NR_PT %d' % values,
    ] + scaling + [
        'CURVE %s' % ",".join(str(v) for v in pairs),
        'SELECT:%s ON' % dest,
        'LIMIT:COMPARE:%s %s' % (source, dest),
    ]


def kind(mask):
    """Is this a pulse mask or an eye mask? They go to the instrument by
    different routes and only one of them works on every instrument.

    A pulse mask keeps the signal between an upper and a lower boundary
    and can be a limit envelope, which works on all three instruments
    measured - including the 640A, which has no mask subsystem. An eye
    mask has a shape sitting on the centre with the signal passing above
    and below it, which a single-band limit test cannot express; it
    needs MASK:MASK<n>:POINTSPCNT and a C or D series instrument.

    Of the ten masks TTiP shipped, STS1 and STS1NEW are pulse masks and
    the other eight are eyes.
    """
    _lo, _hi, blocked = to_envelope(mask, columns=64)
    return "eye" if blocked else "pulse"


# ----------------------------------------------------- cutting and joining
# Everything below works in convex pieces, because that is what a mask
# is: eight convex regions whose union is the shape being tested. So a
# union, an intersection or a difference never has to trace a concave
# outline - it can hand back the pieces, which is the form the
# instrument wants anyway. Every step is a convex polygon clipped
# against a half-plane, which is short and has no special cases.

_ROUND = 4              # decimal places two pieces must agree to
WELD = 1e-3             # nearer than this and it is the same corner


def _round(p):
    return (round(p[0], _ROUND), round(p[1], _ROUND))


def _turn(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _anticlockwise(poly):
    total = 0.0
    for i, p in enumerate(poly):
        q = poly[(i + 1) % len(poly)]
        total += p[0] * q[1] - q[0] * p[1]
    return total > 0


def _in_triangle(p, a, b, c):
    d1, d2, d3 = _turn(a, b, p), _turn(b, c, p), _turn(c, a, p)
    return not ((d1 < -1e-9 or d2 < -1e-9 or d3 < -1e-9)
                and (d1 > 1e-9 or d2 > 1e-9 or d3 > 1e-9))


def triangles(seg):
    """A polygon as a list of triangles, by clipping ears off it.

    Returns [] for anything that is not a simple polygon, which is the
    honest answer for a shape that crosses itself: there is no area to
    divide up.
    """
    if len(seg) < 3:
        return []
    pts = list(seg) if _anticlockwise(seg) else list(reversed(seg))
    left, out, guard = list(range(len(pts))), [], 0
    while len(left) > 3 and guard < 5000:
        guard += 1
        for i in range(len(left)):
            a, b, c = left[i - 1], left[i], left[(i + 1) % len(left)]
            if _turn(pts[a], pts[b], pts[c]) <= 1e-9:
                continue                      # a reflex corner is no ear
            if any(_in_triangle(pts[k], pts[a], pts[b], pts[c])
                   for k in left if k not in (a, b, c)):
                continue                      # something is inside it
            out.append([pts[a], pts[b], pts[c]])
            left.pop(i)
            break
        else:
            return []                         # not simple; nothing to cut
    if len(left) == 3:
        out.append([pts[k] for k in left])
    return out


def clip_to(subject, a, b, keep_left=True):
    """The part of a convex polygon on one side of the line a-b."""
    out = []
    for i, here in enumerate(subject):
        there = subject[(i + 1) % len(subject)]
        side_here = _turn(a, b, here)
        side_there = _turn(a, b, there)
        if not keep_left:
            side_here, side_there = -side_here, -side_there
        if side_here >= -1e-9:
            out.append(here)
        if (side_here > 1e-9) != (side_there > 1e-9):
            span = side_here - side_there
            if abs(span) > 1e-12:
                t = side_here / span
                out.append((here[0] + (there[0] - here[0]) * t,
                            here[1] + (there[1] - here[1]) * t))
    # A clip can leave the same point twice where an edge lay on the line.
    tidy = []
    for p in (_round(q) for q in out):
        if not tidy or p != tidy[-1]:
            tidy.append(p)
    if len(tidy) > 1 and tidy[0] == tidy[-1]:
        tidy.pop()
    return tidy if len(tidy) >= 3 else []


def intersect_convex(one, two):
    """Two convex polygons, overlapped. Convex, or [] if they miss."""
    out = list(one)
    poly = two if _anticlockwise(two) else list(reversed(two))
    for i in range(len(poly)):
        out = clip_to(out, poly[i], poly[(i + 1) % len(poly)])
        if not out:
            return []
    return out


def subtract_convex(subject, hole):
    """A convex polygon with a convex one taken out of it, as pieces.

    Cut against each of the hole's edges in turn: what falls outside
    that edge can never be in the hole and is finished with, and what
    falls inside goes on to the next edge. Every piece comes out convex
    because a convex polygon cut by a line gives two convex polygons.
    """
    poly = hole if _anticlockwise(hole) else list(reversed(hole))
    pieces, rest = [], list(subject)
    for i in range(len(poly)):
        if not rest:
            break
        a, b = poly[i], poly[(i + 1) % len(poly)]
        outside = clip_to(rest, a, b, keep_left=False)
        if outside:
            pieces.append(outside)
        rest = clip_to(rest, a, b)
    return pieces


def _crosses(a, b, c, d):
    """Do the open segments a-b and c-d meet anywhere but at an end?"""
    if len({a, b, c, d}) < 4:
        return False
    one, two = _turn(a, b, c), _turn(a, b, d)
    three, four = _turn(c, d, a), _turn(c, d, b)
    if (one > 0) != (two > 0) and (three > 0) != (four > 0):
        return True
    return any(_between(p, q, r) for p, q, r in
               ((a, b, c), (a, b, d), (c, d, a), (c, d, b)))


def _between(a, b, p):
    """Is p on the line a-b, strictly between the two of them?

    How far off the line, not how big the cross product: the same
    cross product is a hair on a short side and a mile on a long one.
    """
    if p == a or p == b:
        return False
    span = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
    if span < WELD or abs(_turn(a, b, p)) / span > WELD:
        return False
    return (min(a[0], b[0]) - WELD <= p[0] <= max(a[0], b[0]) + WELD
            and min(a[1], b[1]) - WELD <= p[1] <= max(a[1], b[1]) + WELD)


def _weld(pieces):
    """The same pieces, with corners that all but coincide made equal.

    Two pieces cut against the same line work out the crossing point
    separately and can land a millionth of a percent apart. Nothing on
    screen or in a file can hold that difference, but it is enough that
    a shared side is not recognised as shared, and the answer keeps a
    hairline sliver of it. So near enough is made equal, once, before
    any of the sides are compared.
    """
    known = []

    def settle(p):
        for q in known:
            if abs(p[0] - q[0]) <= WELD and abs(p[1] - q[1]) <= WELD:
                return q
        known.append(p)
        return p
    return [[settle(tuple(p)) for p in piece] for piece in pieces]


def _walls(pieces):
    """Every piece's sides, anticlockwise so the inside is on the left.

    Cut where another piece has a corner on them: two pieces meeting
    along part of a side leave one long side against two short ones,
    and a side has to match a side exactly to be recognised as a join.
    """
    out = []
    for poly in _weld(pieces):
        if len(poly) < 3:
            continue
        if not _anticlockwise(poly):
            poly.reverse()
        out += [(p, poly[(i + 1) % len(poly)])
                for i, p in enumerate(poly) if p != poly[(i + 1) % len(poly)]]
    corners = {p for side in out for p in side}
    cut = []
    for u, v in out:
        on = sorted((p for p in corners if _between(u, v, p)),
                    key=lambda p: (p[0] - u[0]) ** 2 + (p[1] - u[1]) ** 2)
        here = u
        for p in on:
            cut.append((here, p))
            here = p
        cut.append((here, v))
    return cut


def _rings(walls):
    """The sides no piece shares, stitched into closed rings.

    A side walked one way by one piece and the other way by its
    neighbour is inside the answer and cancels; what is left is the
    boundary. At a corner where the boundary touches itself, take the
    side that turns hardest clockwise from the way in - that keeps to
    one wall of one ring rather than cutting across to another.
    """
    import math
    count = {}
    for side in walls:
        count[side] = count.get(side, 0) + 1
    leaving = {}
    for (u, v), n in count.items():
        for _each in range(n - count.get((v, u), 0)):
            leaving.setdefault(u, []).append(v)
    rings = []
    while any(leaving.values()):
        start = next(u for u, going in leaving.items() if going)
        ring, here, came = [start], start, None
        while True:
            going = leaving.get(here) or []
            if not going:
                break
            if came is None or len(going) == 1:
                step = going[0]
            else:
                back = math.atan2(came[1] - here[1], came[0] - here[0])
                step = min(going, key=lambda w: (
                    math.atan2(w[1] - here[1], w[0] - here[0]) - back)
                    % (2.0 * math.pi))
            going.remove(step)
            came, here = here, step
            if here == start:
                break
            ring.append(here)
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def _straighten(ring, corners):
    """Drop the corners this program put in, not the ones it was given.

    Cutting sides at their meeting points leaves a point in the middle
    of what is otherwise a straight run. It was never in either shape
    and it costs one of the fifty a segment holds. A point that *was*
    in one of the shapes stays, three in a line or not: it is somebody
    else's shape and not this code's business to redraw it.
    """
    out = list(ring)
    going = True
    while going and len(out) > 3:
        going = False
        for i, p in enumerate(out):
            if any(abs(p[0] - c[0]) <= WELD and abs(p[1] - c[1]) <= WELD
                   for c in corners):
                continue
            if _between(out[i - 1], out[(i + 1) % len(out)], p):
                out.pop(i)
                going = True
                break
    return out


def _bridge(shell, hole):
    """A hole let into the shape around it, by the shortest cut.

    A mask segment is one closed outline and there is no such thing as
    a hole in it, so the two are joined by a cut that goes in and comes
    straight back out. The shape is then one outline that draws as the
    ring it is. The shortest cut that crosses nothing is used, and the
    two sides of it land on top of one another.
    """
    best = None
    for i, a in enumerate(shell):
        for j, b in enumerate(hole):
            if any(_crosses(a, b, r[k - 1], p)
                   for r in (shell, hole) for k, p in enumerate(r)):
                continue
            far = (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
            if best is None or far < best[0]:
                best = (far, i, j)
    if best is None:
        return None
    _far, i, j = best
    return (shell[:i + 1] + hole[j:] + hole[:j]
            + [hole[j], shell[i]] + shell[i + 1:])


def outline(pieces, corners=()):
    """The edge of what a set of touching pieces covers.

    `corners` are the points the shapes were drawn with, and they are
    kept whatever they do; every other point in a straight run is one
    this code put there and is taken back out.

    The pieces are how the answer is worked out - a convex polygon cut
    by a line is the one clip with no special cases - and they are not
    how anybody wants it back. This walks the outside of them: sides
    that two pieces share cancel, and what is left is the outline the
    shapes actually make. Holes are let in on a cut. What comes back
    can be concave, and can be more than one shape where the answer
    genuinely falls in two.
    """
    kept = [tuple(p) for p in corners]
    rings = [_straighten(r, kept) for r in _rings(_walls(pieces))]
    shells = [r for r in rings if _anticlockwise(r)]
    for hole in [r for r in rings if not _anticlockwise(r)]:
        home = next((i for i, shell in enumerate(shells)
                     if all(inside(shell, x, y) for x, y in hole)), None)
        joined = _bridge(shells[home], hole) if home is not None else None
        if joined is None:
            shells.append(hole)     # nowhere to let it in; better seen
        else:
            shells[home] = joined
    return shells


def as_pieces(seg):
    """A shape as convex pieces - itself, if it is already convex.

    A shortcut and nothing else, now that the pieces are walked back
    into an outline: cutting a convex shape into triangles and joining
    them up again gives the same answer, three times slower. It used to
    change the answer, when the answer was the pieces themselves - the
    triangulation's own diagonals survived into it.
    """
    if len(seg) >= 3 and convex_polygon(seg):
        return [list(seg)]
    return triangles(seg)


def combine(how, first, second):
    """Two shapes joined, overlapped or taken away, as outlines.

    `how` is "union", "intersect" or "subtract"; subtract takes the
    second away from the first. Returns [] where nothing is left, which
    is a real answer - subtracting a shape from inside itself leaves
    nothing.

    What comes back is the outline the two shapes make, not a heap of
    convex pieces: one shape for a union that overlaps, an L for a
    corner taken off a rectangle. More than one only where the answer
    really is more than one - a bar subtracted through the middle
    leaves two. The outline can be concave and can break the rule about
    what the instrument redraws; that is the mask editor's business to
    say, not this function's to prevent.
    """
    ones = as_pieces(first)
    twos = as_pieces(second)
    if not ones or not twos:
        return []
    if how == "intersect":
        out = [got for a in ones for b in twos
               if (got := intersect_convex(a, b))]
    elif how == "subtract":
        out = _without(ones, twos)
    else:
        out = list(ones) + _without(twos, ones)
    return outline(out, list(first) + list(second))


def _without(pieces, holes):
    """Every piece with every hole taken out of it."""
    out = list(pieces)
    for hole in holes:
        nxt = []
        for piece in out:
            nxt += subtract_convex(piece, hole)
        out = nxt
    return out


def can_cut(seg, i, j):
    """May this shape be cut between these two of its points?

    The cut has to be a diagonal that stays inside: not the same point,
    not two already joined by an edge, not crossing any edge, and not
    running outside a concave shape - which is exactly the cut somebody
    aiming at the far side of a notch would make.
    """
    n = len(seg)
    if n < 4 or i == j:
        return False
    if (i + 1) % n == j % n or (j + 1) % n == i % n:
        return False
    a, b = seg[i], seg[j]
    for k in range(n):
        c, d = seg[k], seg[(k + 1) % n]
        if k in (i, j) or (k + 1) % n in (i, j):
            continue                      # shares an end; cannot cross
        if (_turn(a, b, c) > 0) != (_turn(a, b, d) > 0) and \
           (_turn(c, d, a) > 0) != (_turn(c, d, b) > 0):
            return False
    return inside(seg, (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def cut_at(seg, i, j):
    """The shape cut in two along the diagonal from point i to point j."""
    i, j = sorted((i, j))
    return [seg[i:j + 1], seg[j:] + seg[:i + 1]]
