"""The instrument's screen, over GPIB.

The TDS500/600/700 have no "give me the display" command. What they
have is a hardcopy subsystem meant for a printer, which will send its
output to the bus instead of to a port if asked - and several of the
formats it offers are screen images rather than pages of printer
control language. So a screenshot is a print job, aimed at the
controller.

Which format to ask for was measured rather than assumed. On the bench,
with an NI GPIB-USB-HS:

    TDS 784D          bytes   seconds        TDS 640A      bytes  seconds
    TIFF             38,830      3.28        TIFF         38,858     3.27
    BMP              38,462      4.35        BMP          38,462     6.14
    PCX              17,763      4.40        PCX          14,587     6.43
    PCXCOLOR         81,351      5.30        (no colour formats)
    RLE              85,318      5.35
    BMPCOLOR        308,278      5.62

The time is almost all the instrument drawing the thing: BMPCOLOR
carries eight times RLE's bytes for a quarter of a second more. So the
choice is not about the wire, and the smallest is not the fastest. What
matters is:

  * whether the image comes back in colour at all - a monochrome
    instrument accepts BMPCOLOR, RLE and PCXCOLOR and quietly hands
    back the monochrome equivalent, which HARDCOPY:FORMAT? then admits
  * how much there is to decode

RLE is the default where the firmware has it: colour, a quarter of the
bytes of BMPCOLOR, and the same decoder, because it is a Windows BMP
with run-length encoding. Where there is no colour at all, TIFF is the
default: on a 640A it is twice as quick as BMP for the same pixels.

Three things learned the hard way, all of which this module handles:

  * HARDCOPY START must be read to the end. Abandon it and the
    instrument is left with a screen's worth of bytes to say and nobody
    listening, and the next program to open it times out on *IDN?. A
    failed capture is followed by a device clear here.
  * A second hardcopy started too soon after the first is ignored -
    no data, no error, just silence until the timeout. There is a
    settling pause.
  * LAYOUT is the orientation of the printed page, not of the image.
    PORTRAIT gives the screen the right way up at 640x480; LANDSCAPE
    turns it on its side at 480x640.

Nothing here changes what the instrument is set to for longer than the
capture takes. Every HARDCOPY setting is read first and written back
afterwards, including PORT - all three scopes on this bench arrived set
to print to a floppy, and would be entitled to be left that way.
"""
import struct
import time

# What the app offers, in the order it offers them. `colour` is what the
# format is capable of, not what a given instrument will do with it.
# `decoder` names the family, since BMP covers three of these.
FORMATS = [
    {"keyword": "RLE", "decoder": "bmp", "colour": True, "suffix": ".bmp",
     "label": "Colour, compressed (RLE)", "quickest": True},
    {"keyword": "PCXCOLOR", "decoder": "pcx", "colour": True,
     "suffix": ".pcx", "label": "Colour PCX"},
    {"keyword": "BMPCOLOR", "decoder": "bmp", "colour": True,
     "suffix": ".bmp", "label": "Colour, uncompressed (BMPCOLOR)"},
    {"keyword": "TIFF", "decoder": "tiff", "colour": False,
     "suffix": ".tif", "label": "Monochrome (TIFF)", "quickest": True},
    {"keyword": "BMP", "decoder": "bmp", "colour": False, "suffix": ".bmp",
     "label": "Monochrome (BMP)"},
    {"keyword": "PCX", "decoder": "pcx", "colour": False, "suffix": ".pcx",
     "label": "Monochrome PCX"},
]

BY_KEYWORD = dict((f["keyword"], f) for f in FORMATS)

# Asked for in this order; the first the instrument accepts and this
# module can decode is what a screenshot is taken in unless the user
# says otherwise.
PREFERRED = ["RLE", "BMPCOLOR", "PCXCOLOR", "TIFF", "BMP", "PCX"]

PALETTES = ["CURRENT", "HARDCOPY"]


class Screen(object):
    """A decoded screen: indexed pixels and the palette they index."""

    def __init__(self, width, height, pixels, palette, raw=b"",
                 keyword="", seconds=0.0, family="", bits=8):
        self.width = width
        self.height = height
        self.pixels = pixels          # one index a pixel, top row first
        self.palette = palette        # list of (r, g, b)
        self.raw = raw                # exactly what the instrument sent
        self.keyword = keyword        # what was asked for
        self.family = family          # what actually arrived: bmp/pcx/tiff
        self.bits = bits              # and at what depth
        self.seconds = seconds
        # Whether anything here has been changed since it arrived. While
        # it is False, `raw` is this picture and saving it back is exact.
        self.changed = False
        # Which way up the instrument drew it, so that changing the
        # Layout box can turn the picture already in hand instead of
        # asking for another five-second capture.
        self.layout = ""

    @property
    def colours(self):
        """How many of the palette entries the image actually uses."""
        return len(set(self.pixels))

    def to_png(self):
        """A PNG of exactly these pixels - no scaling, no resampling."""
        import tds_wfm
        return tds_wfm.encode_png_indexed(self.pixels, self.palette,
                                          self.width, self.height)

    # Where the graticule sits in an upright 640 x 480 hardcopy.
    # Measured on a TDS 784D (v7.4e) rather than assumed: the rows and
    # columns of the capture that are nearly solid ink are the
    # graticule's own border, and they come out at exactly fifty pixels
    # a division both ways - ten divisions across and eight down, which
    # is the graticule the instrument draws. See INSTRUMENT-NOTES.
    GRATICULE = (24, 34, 524, 434)

    def upright(self):
        """This picture the way somebody looks at the instrument.

        A hardcopy arrives on its side - 480 x 640 - whatever LAYOUT is
        set to, so the turn is done here rather than trusted to the
        instrument. Three quarters anticlockwise, settled by capturing
        one and reading the words on it.
        """
        return self if self.width > self.height else self.turned(3)

    def graticule(self):
        """Just the graticule, as (pixels, width, height).

        The mask editor draws a mask over the same ten by eight
        divisions, so a screen cropped to its graticule lines up with it
        exactly - which is what makes a DPO screen usable as a backdrop
        when there is no readable waveform to draw.
        """
        pic = self.upright()
        left, top, right, bottom = self.GRATICULE
        if not (0 <= left < right < pic.width
                and 0 <= top < bottom < pic.height):
            return pic.pixels, pic.width, pic.height      # not as measured
        # Both borders included: the graticule runs from its own left
        # edge to its own right edge, and cropping between them leaves
        # the far two out and shifts everything by a pixel.
        wide, tall = right - left + 1, bottom - top + 1
        out = bytearray()
        for y in range(top, bottom + 1):
            at = y * pic.width
            out += pic.pixels[at + left:at + right + 1]
        return bytes(out), wide, tall

    def turned(self, quarters=1):
        """The same picture rotated by quarter turns, anticlockwise.

        For the Layout box, which on the instrument decides whether the
        screen is drawn upright on the page or on its side. Changing it
        there means another capture; here it is a transpose, and the
        pixels are the same pixels.

        Anticlockwise is not a guess, and it is not what was assumed
        first either. A 784D captured in PORTRAIT and again in
        LANDSCAPE puts "Tek Run:" down the left-hand edge reading
        upwards, which is where a quarter turn anticlockwise puts the
        top-left corner. Turning it clockwise gave the same picture
        upside down.
        """
        quarters = int(quarters) % 4
        if not quarters:
            return self
        width, height = self.width, self.height
        pixels = self.pixels
        for _turn in range(quarters):
            # Anticlockwise: the new left column is the old top row,
            # read downwards.
            out = bytearray(len(pixels))
            for y in range(height):
                row = y * width
                for x in range(width):
                    out[(width - 1 - x) * height + y] = pixels[row + x]
            pixels = bytes(out)
            width, height = height, width
        turned = Screen(width, height, pixels, list(self.palette),
                        self.raw, self.keyword, self.seconds, self.family,
                        self.bits)
        turned.layout = self.layout
        turned.changed = True
        return turned

    def inverted(self):
        """The same image with the palette turned inside out.

        For a monochrome instrument, which has no HARDCOPY:PALETTE to
        offer: white on black is what the screen looks like and black on
        white is what a page wants, and the difference is one subtraction
        per palette entry rather than another five-second capture. Only
        the palette is touched, so this costs nothing and the pixels
        stay exactly as they arrived.
        """
        flipped = [(255 - r, 255 - g, 255 - b) for r, g, b in self.palette]
        out = Screen(self.width, self.height, self.pixels, flipped,
                     self.raw, self.keyword, self.seconds, self.family,
                     self.bits)
        out.layout = self.layout
        out.changed = True
        return out

    def to_native(self):
        """This picture in the format it arrived in.

        Saving "as received" used to mean writing `raw` back out, which
        is the picture the instrument sent and not the one on the screen:
        a shot turned round or inverted here was saved without either.
        Untouched, `raw` is still handed back byte for byte; touched, the
        picture in hand is encoded into its own family.
        """
        if not self.changed and self.raw:
            return self.raw
        return ENCODERS[self.family or "bmp"](self)

    @property
    def suffix(self):
        """The file extension for what is actually in `raw`.

        Taken from the bytes, not from the format that was asked for.
        The two normally agree, and when they do not it is the bytes
        that are right - writing BMP data into a file called .tif
        because the instrument said TIFF would be a mislabelled file,
        which is worse than an unexpected one.
        """
        family = self.family or (
            BY_KEYWORD.get((self.keyword or "").upper()) or {}
            ).get("decoder")
        return {"bmp": ".bmp", "pcx": ".pcx", "tiff": ".tif"}.get(
            family, ".bin")


# What a screen off one of these instruments can be. A corrupt or
# truncated header says four thousand million by four thousand million,
# and a reader that believes it asks for exabytes before it fails - so
# the shape is checked before anything is allocated from it. The
# biggest of the three families is a 640 x 480 BMPCOLOR, 308,278 bytes.
MAX_SIDE, MAX_PIXELS = 8192, 4 * 1024 * 1024


def _shape(width, height, bits, allowed=(1, 4, 8)):
    """The picture's shape, or a refusal. Checked, not believed."""
    if bits not in allowed:
        raise ValueError("%r bits a pixel is not handled" % (bits,))
    if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE
            and width * height <= MAX_PIXELS):
        raise ValueError("a %r x %r image is not an instrument's screen"
                         % (width, height))
    return width, height


def _least(body, want, family):
    """Refuse a file that stopped before its pixels started."""
    if len(body) < want:
        raise ValueError("the %s holds %d byte(s) of pixels, less than "
                         "the %d one row needs" % (family, len(body), want))


def _entries(data, at, count, step=4, order=(2, 1, 0)):
    """Palette entries from `at`, stopping where the data stops.

    A file cut short mid-palette is a file cut short, not a reason to
    index past the end of it - which is what a truncated BMP did.
    """
    out = []
    for i in range(count):
        j = at + i * step
        if j + max(order) >= len(data):
            break
        out.append(tuple(data[j + k] for k in order))
    return out


def _rows_from_indices(data, width, height, bits, bottom_up, stride=None):
    """Unpack packed pixels into one byte per pixel, top row first."""
    if stride is None:
        stride = ((width * bits + 31) // 32) * 4
    out = bytearray(width * height)
    per_byte = 8 // bits
    mask = (1 << bits) - 1
    for y in range(height):
        src = (height - 1 - y) * stride if bottom_up else y * stride
        line = data[src:src + stride]
        if len(line) < stride:
            line = line + bytes(stride - len(line))
        base = y * width
        if bits == 8:
            out[base:base + width] = line[:width]
            continue
        for x in range(width):
            byte = line[x // per_byte]
            shift = 8 - bits - (x % per_byte) * bits
            out[base + x] = (byte >> shift) & mask
    return bytes(out)


def decode_bmp(data):
    """A Windows BMP at 1, 4 or 8 bits, uncompressed or RLE8.

    The instruments fill in bfSize with a number that is not the file's
    size - 77,070 for a 308,278 byte BMPCOLOR - so nothing here reads
    it. The length of what arrived is the length of the file.
    """
    if data[:2] != b"BM":
        raise ValueError("not a BMP")
    try:
        offset = struct.unpack_from("<I", data, 10)[0]
        (hdr, width, height, _planes, bits, comp) = struct.unpack_from(
            "<IiiHHI", data, 14)
        used = struct.unpack_from("<I", data, 46)[0] if hdr >= 40 else 0
    except struct.error as exc:
        raise ValueError("the BMP header is cut short: %s" % exc)
    bottom_up = height > 0
    width, height = _shape(width, abs(height), bits)
    palette = _entries(data, 14 + hdr, used or (1 << bits))
    body = data[offset:]
    # A file cut off before its pixels is not a short picture, it is no
    # picture: without this a forty-byte BMP claiming 480 x 640 came
    # back as a perfectly good black screen. One row is enough to be
    # worth showing - a transfer that stopped half way is better seen
    # than refused - and the rest is padded.
    _least(body, ((width * bits + 31) // 32) * 4 if comp == 0 else 2, "BMP")
    if comp == 1:                      # BI_RLE8
        pixels = _decode_rle8(body, width, height, bottom_up)
    elif comp == 0:
        pixels = _rows_from_indices(body, width, height, bits, bottom_up)
    else:
        raise ValueError("BMP compression %d is not handled" % comp)
    return Screen(width, height, pixels,
                  palette or [(0, 0, 0), (255,) * 3], bits=bits)


def _decode_rle8(body, width, height, bottom_up):
    """BI_RLE8: pairs of (count, index), with escapes for literals."""
    rows = [bytearray(width) for _ in range(height)]
    x, y, at = 0, 0, 0
    while at + 1 < len(body) and y < height:
        count, value = body[at], body[at + 1]
        at += 2
        if count:
            row = rows[y]
            for _ in range(count):
                if x < width:
                    row[x] = value
                x += 1
            continue
        if value == 0:                 # end of line
            x, y = 0, y + 1
        elif value == 1:               # end of bitmap
            break
        elif value == 2:               # delta
            if at + 1 >= len(body):
                break
            x += body[at]
            y += body[at + 1]
            at += 2
        else:                          # `value` literal bytes follow
            row = rows[y] if y < height else bytearray(width)
            for i in range(value):
                if at + i < len(body) and x + i < width:
                    row[x + i] = body[at + i]
            x += value
            at += value + (value & 1)  # padded to a word
    if bottom_up:
        rows.reverse()
    out = bytearray()
    for row in rows:
        out += row
    return bytes(out)


def decode_pcx(data):
    """A ZSoft PCX at 1 or 8 bits. 8-bit carries its palette at the end."""
    if data[0] != 0x0A:
        raise ValueError("not a PCX")
    if len(data) < 128:
        raise ValueError("the PCX header is cut short")
    bits = data[3]
    xmin, ymin, xmax, ymax = struct.unpack_from("<4H", data, 4)
    planes = data[65]
    stride = struct.unpack_from("<H", data, 66)[0]
    if planes != 1:
        raise ValueError("PCX with %d planes is not handled" % planes)
    width, height = _shape(xmax - xmin + 1, ymax - ymin + 1, bits, (1, 8))
    if not 0 < stride <= MAX_SIDE:
        raise ValueError("a PCX row of %r bytes is not a screen" % (stride,))
    end = len(data)
    palette = []
    if bits == 8 and len(data) > 769 and data[-769] == 0x0C:
        tail = data[-768:]
        palette = [(tail[i], tail[i + 1], tail[i + 2])
                   for i in range(0, 768, 3)]
        end = len(data) - 769
    body = bytearray()
    at = 128
    while at < end and len(body) < stride * height:
        byte = data[at]
        at += 1
        if byte & 0xC0 == 0xC0:
            if at >= end:
                break
            body += bytes([data[at]]) * (byte & 0x3F)
            at += 1
        else:
            body.append(byte)
    _least(body, stride, "PCX")
    pixels = _rows_from_indices(bytes(body), width, height, bits,
                                False, stride)
    if not palette:
        # 1-bit PCX: the 16-entry EGA palette in the header. These
        # instruments write black and white, but read it rather than
        # assume it.
        palette = _entries(data, 16, 2, step=3, order=(0, 1, 2))
    return Screen(width, height, pixels,
                  palette or [(0, 0, 0), (255,) * 3], bits=bits)


def decode_tiff(data):
    """The monochrome TIFF these instruments write: 1 bit, in strips.

    Deliberately narrow. This is not a TIFF reader; it is a reader for
    the one TIFF a TDS produces - baseline, uncompressed, one bit a
    pixel, white as zero, split into strips of 25 rows.
    """
    if data[:2] not in (b"MM", b"II"):
        raise ValueError("not a TIFF")
    end = ">" if data[:2] == b"MM" else "<"
    try:
        ifd = struct.unpack_from(end + "I", data, 4)[0]
        count = struct.unpack_from(end + "H", data, ifd)[0]
        tags = {}
        for i in range(count):
            tag, typ, num, raw = struct.unpack_from(end + "HHII", data,
                                                    ifd + 2 + i * 12)
            if typ == 3 and num == 1:                # SHORT, in place
                raw = raw >> 16 if end == ">" else raw & 0xFFFF
            tags[tag] = (typ, num, raw)

        def values(tag):
            typ, num, raw = tags[tag]
            if num == 1:
                return [raw]
            if typ not in (3, 4):
                raise ValueError("TIFF tag %d is of type %d" % (tag, typ))
            size = 2 if typ == 3 else 4
            code = end + ("H" if typ == 3 else "I")
            return [struct.unpack_from(code, data, raw + i * size)[0]
                    for i in range(num)]

        if 256 not in tags or 257 not in tags:
            raise ValueError("the TIFF says no size")
        width = tags[256][2]
        height = tags[257][2]
        bits = tags.get(258, (3, 1, 1))[2]
        comp = tags.get(259, (3, 1, 1))[2]
        photometric = tags.get(262, (3, 1, 0))[2]
        if comp != 1 or bits != 1:
            raise ValueError("TIFF compression %d at %d bits is not handled"
                             % (comp, bits))
        width, height = _shape(width, height, bits, (1,))
        offsets, counts = values(273), values(279)
    except struct.error as exc:
        raise ValueError("the TIFF is cut short: %s" % exc)
    stride = (width + 7) // 8
    body = bytearray()
    for at, n in zip(offsets, counts):
        body += data[at:at + n]
    _least(body, stride, "TIFF")
    pixels = _rows_from_indices(bytes(body), width, height, 1, False, stride)
    # WhiteIsZero: index 0 is white. Give the palette in that order
    # rather than inverting a quarter of a million pixels.
    pal = [(255, 255, 255), (0, 0, 0)]
    if photometric == 1:
        pal.reverse()
    return Screen(width, height, pixels, pal, bits=1)


# ---------------------------------------------------------- writing them

# The other direction, for saving a picture that has been turned round or
# inverted here. Each encoder writes the family and the depth its own
# decoder above reads, so a file written here goes straight back through
# it - which is how they are checked.


def _rows_to_indices(pixels, width, height, bits, stride):
    """Pack one-byte-a-pixel rows back into `bits` a pixel.

    The inverse of _rows_from_indices, top row first either way.
    """
    out = bytearray(stride * height)
    per_byte = 8 // bits
    mask = (1 << bits) - 1
    for y in range(height):
        line = pixels[y * width:(y + 1) * width]
        at = y * stride
        if bits == 8:
            out[at:at + width] = line
            continue
        for x in range(width):
            shift = 8 - bits - (x % per_byte) * bits
            out[at + x // per_byte] |= (line[x] & mask) << shift
    return bytes(out)


def _flat(palette, entries, order=(2, 1, 0), step=4):
    """A palette table, padded out to the entries the format wants."""
    out = bytearray()
    for i in range(entries):
        rgb = tuple(palette[i])[:3] if i < len(palette) else (0, 0, 0)
        out += bytes(rgb[k] for k in order) + bytes(step - 3)
    return bytes(out)


def encode_bmp(screen):
    """A Windows BMP at the depth the picture came in at.

    Uncompressed, even where the picture arrived as an RLE8: the
    compression is the instrument's choice about how to send it, not
    part of what the picture is.
    """
    bits = screen.bits
    width, height = screen.width, screen.height
    stride = ((width * bits + 31) // 32) * 4
    rows = _rows_to_indices(screen.pixels, width, height, bits, stride)
    # Bottom-up, which is what every BMP these instruments write is.
    body = b"".join(rows[y * stride:(y + 1) * stride]
                    for y in range(height - 1, -1, -1))
    entries = 1 << bits
    pal = _flat(screen.palette, entries)
    offset = 14 + 40 + len(pal)
    return (b"BM" + struct.pack("<IHHI", offset + len(body), 0, 0, offset)
            + struct.pack("<IiiHHIIiiII", 40, width, height, 1, bits, 0,
                          len(body), 2835, 2835, entries, 0)
            + pal + body)


def _pcx_row(line):
    """One PCX row, run-length encoded."""
    out = bytearray()
    at, n = 0, len(line)
    while at < n:
        run = 1
        while run < 63 and at + run < n and line[at + run] == line[at]:
            run += 1
        # A lone byte of C0 or above still has to be escaped, or it
        # reads back as the start of a run.
        if run > 1 or line[at] >= 0xC0:
            out += bytes((0xC0 | run, line[at]))
        else:
            out.append(line[at])
        at += run
    return bytes(out)


def encode_pcx(screen):
    """A ZSoft PCX at 1 or 8 bits, one plane, which is what they write."""
    bits = screen.bits if screen.bits in (1, 8) else 8
    width, height = screen.width, screen.height
    stride = (width * bits + 7) // 8
    stride += stride & 1                     # PCX rows are an even length
    rows = _rows_to_indices(screen.pixels, width, height, bits, stride)
    head = bytearray(128)
    head[0], head[1], head[2], head[3] = 0x0A, 5, 1, bits
    struct.pack_into("<4H", head, 4, 0, 0, width - 1, height - 1)
    struct.pack_into("<2H", head, 12, 72, 72)
    head[65] = 1                             # one plane
    struct.pack_into("<H", head, 66, stride)
    struct.pack_into("<H", head, 68, 1)      # colour, not greyscale
    if bits == 1:
        # A 1-bit PCX carries its two colours in the header's own
        # 16-entry table; there is no palette at the end.
        head[16:64] = _flat(screen.palette, 16, order=(0, 1, 2), step=3)
    body = b"".join(_pcx_row(rows[y * stride:(y + 1) * stride])
                    for y in range(height))
    tail = (b"\x0c" + _flat(screen.palette, 256, order=(0, 1, 2), step=3)
            if bits == 8 else b"")
    return bytes(head) + body + tail


def encode_tiff(screen):
    """The one TIFF these instruments make: 1 bit, uncompressed, one strip.

    Narrow on purpose, and matched to the decoder above: nothing in this
    family arrives at any other depth, so nothing has to be written at
    one.
    """
    bits, width, height = screen.bits, screen.width, screen.height
    if bits != 1:
        raise ValueError("a %d-bit TIFF is not written" % bits)
    stride = (width + 7) // 8
    body = _rows_to_indices(screen.pixels, width, height, 1, stride)
    # WhiteIsZero or BlackIsZero, from whichever the palette puts first.
    white_first = sum(tuple(screen.palette[0])[:3]) > 382
    tags = [(256, 3, width), (257, 3, height), (258, 3, 1), (259, 3, 1),
            (262, 3, 0 if white_first else 1), (273, 4, 0),
            (277, 3, 1), (278, 3, height), (279, 4, len(body))]
    at = 8 + 2 + 12 * len(tags) + 4
    out = bytearray(b"MM\x00\x2a" + struct.pack(">I", 8)
                    + struct.pack(">H", len(tags)))
    for tag, typ, value in tags:
        if tag == 273:
            value = at
        out += struct.pack(">HHI", tag, typ, 1)
        # A SHORT that fits is written into the value field's first half.
        out += (struct.pack(">HH", value, 0) if typ == 3
                else struct.pack(">I", value))
    out += struct.pack(">I", 0)
    return bytes(out) + body


ENCODERS = {"bmp": encode_bmp, "pcx": encode_pcx, "tiff": encode_tiff}


def decode(data, keyword=""):
    """Whatever the instrument sent, as pixels. Sniffed, not trusted."""
    if not data:
        raise ValueError("nothing arrived")
    if data[:2] == b"BM":
        screen = decode_bmp(data)
        screen.family = "bmp"
    elif data[:2] in (b"MM", b"II"):
        screen = decode_tiff(data)
        screen.family = "tiff"
    elif data[0] == 0x0A:
        screen = decode_pcx(data)
        screen.family = "pcx"
    else:
        raise ValueError("unrecognised image, starts %s"
                         % " ".join("%02X" % b for b in data[:4]))
    screen.raw = data
    screen.keyword = keyword
    return screen


class TdsScr(object):
    """The hardcopy subsystem, aimed at the controller."""

    def __init__(self, inst, payload=None):
        self.inst = inst
        self._payload = payload or (lambda r: r)
        self._offers = None

    # -- the instrument's own settings ---------------------------------

    def settings(self):
        """Every HARDCOPY field, named. Headers on, so it names itself."""
        try:
            self.inst.write("HEADER ON")
            reply = self.inst.query("HARDCOPY?").strip()
        finally:
            try:
                self.inst.write("HEADER OFF")
            except Exception:
                pass
        out = {}
        for field in reply.lstrip(":").split(";"):
            field = field.strip()
            if " " not in field:
                continue
            name, _sep, value = field.partition(" ")
            out[name.split(":")[-1].upper()] = value.strip()
        return out

    def restore(self, settings):
        """Put back what settings() returned. Never raises."""
        for name in ("FORMAT", "LAYOUT", "PALETTE", "FILENAME", "PORT"):
            if name not in settings:
                continue
            try:
                self.inst.write("HARDCOPY:%s %s" % (name, settings[name]))
            except Exception:
                pass
        self.drain()

    def drain(self):
        """Empty the event queue, which *ESR? must prime first."""
        out = []
        try:
            self.inst.query("*ESR?")
            for _ in range(20):
                msg = self._payload(self.inst.query("EVMSG?")).strip()
                if msg.startswith("0,"):
                    break
                out.append(msg)
        except Exception:
            pass
        return out

    # -- what this instrument can do -----------------------------------

    def offers(self):
        """Which of FORMATS this firmware takes, and what it calls them.

        Asked by setting each and reading back: a monochrome instrument
        accepts BMPCOLOR without complaint and then reports BMP, which
        is the only honest way to find out that its colour is not
        colour. The setting is put back afterwards.
        """
        if self._offers is not None:
            return self._offers
        was = self.settings()
        out = []
        for entry in FORMATS:
            keyword = entry["keyword"]
            self.drain()
            try:
                self.inst.write("HARDCOPY:FORMAT %s" % keyword)
                if self.drain():
                    continue
                got = self._payload(
                    self.inst.query("HARDCOPY:FORMAT?")).strip().upper()
            except Exception:
                continue
            if got == keyword:
                out.append(entry)
        try:
            if was.get("FORMAT"):
                self.inst.write("HARDCOPY:FORMAT %s" % was["FORMAT"])
        except Exception:
            pass
        self.drain()
        self._offers = out
        return out

    def has_palette(self):
        """PALETTE exists only on the instruments that have colour."""
        return "PALETTE" in self.settings()

    def best(self):
        """The format a screenshot is taken in unless told otherwise."""
        taken = dict((e["keyword"], e) for e in self.offers())
        for keyword in PREFERRED:
            if keyword in taken:
                return keyword
        return None

    # -- the capture ---------------------------------------------------

    def capture(self, keyword=None, layout=None, palette=None,
                timeout=90.0, settle=2.0, progress=None):
        """One screen. Returns a Screen; the instrument is left as found.

        `settle` is not politeness. A hardcopy started too soon after
        the previous one is ignored - no data and no error, just silence
        until the timeout - so the wait is part of getting an answer.
        """
        was = self.settings()
        keyword = keyword or self.best()
        if not keyword:
            raise IOError("This instrument offers no image format that "
                          "can be decoded here.")
        old_timeout = getattr(self.inst, "timeout", None)
        try:
            self.inst.write("HARDCOPY:PORT GPIB")
            self.inst.write("HARDCOPY:FORMAT %s" % keyword)
            if layout:
                self.inst.write("HARDCOPY:LAYOUT %s" % layout)
            if palette and "PALETTE" in was:
                self.inst.write("HARDCOPY:PALETTE %s" % palette)
            self.drain()
            now = self.settings()
            if now.get("PORT") != "GPIB":
                raise IOError("The instrument would not send its screen to "
                              "the bus: HARDCOPY:PORT is %s."
                              % now.get("PORT", "unknown"))
            got = now.get("FORMAT", "").upper()
            if progress:
                progress(got)
            time.sleep(settle)
            if old_timeout is not None:
                self.inst.timeout = int(timeout * 1000)
            start = time.time()
            try:
                self.inst.write("HARDCOPY START")
                data = bytes(self.inst.read_raw())
            except Exception:
                # Leaving a half-sent hardcopy in the instrument is what
                # makes the next program's *IDN? time out. Take it back.
                try:
                    self.inst.clear()
                except Exception:
                    pass
                time.sleep(1.0)
                raise
            seconds = time.time() - start
        finally:
            if old_timeout is not None:
                self.inst.timeout = old_timeout
            self.restore(was)
        screen = decode(data, got or keyword)
        screen.seconds = seconds
        screen.layout = (layout or was.get("LAYOUT") or "").upper()
        return screen
