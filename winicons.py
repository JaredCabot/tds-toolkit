"""Windows shell icons for a Tk application, with no extra dependencies.

Tk cannot be handed a Windows HICON, and the usual answer is to convert it
with Pillow. That is a three-megabyte dependency for sixteen-pixel
pictures, so this does the conversion itself: ask the shell for the icon,
read its bits with GetDIBits, and write a PNG in memory with zlib from the
standard library. Tk 8.6 reads base64 PNG directly.

Icons are asked for by *name*, not by opening a file: SHGFI_USEFILEATTRIBUTES
tells the shell to answer from the extension alone. That matters here
because the files are on an oscilloscope, not on this disk.

Everything degrades rather than fails. On a non-Windows system, or if any
part of the API dance goes wrong, a small drawn folder or page is returned
instead, and the program looks slightly plainer.
"""
import base64
import os
import struct
import sys
import zlib

_SIZE = 16
_cache = {}


# ------------------------------------------------------------- PNG writing

def _png_bytes(rgba, w, h):
    """Encode RGBA bytes as a PNG. Tk 8.6 accepts this, base64 encoded."""
    raw = b"".join(b"\x00" + bytes(rgba[y * w * 4:(y + 1) * w * 4])
                   for y in range(h))

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _photo(tk, rgba, w, h):
    return tk.PhotoImage(data=base64.b64encode(_png_bytes(rgba, w, h)))


# ------------------------------------------------------ drawn fall-backs

def _blank():
    return bytearray(_SIZE * _SIZE * 4)


def _put(buf, x, y, rgba):
    if 0 <= x < _SIZE and 0 <= y < _SIZE:
        i = (y * _SIZE + x) * 4
        buf[i:i + 4] = bytes(rgba)


def _rect(buf, x0, y0, x1, y1, rgba):
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            _put(buf, x, y, rgba)


def _drawn_folder():
    buf = _blank()
    edge = (196, 148, 30, 255)
    face = (255, 200, 84, 255)
    _rect(buf, 1, 4, 6, 5, edge)          # tab
    _rect(buf, 1, 5, 14, 13, edge)        # body outline
    _rect(buf, 2, 6, 13, 12, face)
    return buf


def _drawn_file():
    buf = _blank()
    edge = (128, 138, 150, 255)
    face = (250, 250, 252, 255)
    _rect(buf, 3, 1, 12, 14, edge)
    _rect(buf, 4, 2, 11, 13, face)
    for y in (5, 7, 9, 11):
        _rect(buf, 5, y, 10, y, (150, 158, 170, 255))
    return buf


# ------------------------------------------------------ the Windows shell

def _shell_icon_rgba(name, is_dir):
    """RGBA bytes for the shell icon of `name`, or None.

    The file need not exist: SHGFI_USEFILEATTRIBUTES makes the shell answer
    from the name and the attribute flags alone.
    """
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class SHFILEINFOW(ctypes.Structure):
        _fields_ = [("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int),
                    ("dwAttributes", wintypes.DWORD),
                    ("szDisplayName", ctypes.c_wchar * 260),
                    ("szTypeName", ctypes.c_wchar * 80)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD),
                    ("hbmMask", wintypes.HBITMAP),
                    ("hbmColor", wintypes.HBITMAP)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                    ("bmHeight", wintypes.LONG),
                    ("bmWidthBytes", wintypes.LONG),
                    ("bmPlanes", wintypes.WORD),
                    ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                    ("bmiColors", wintypes.DWORD * 3)]

    SHGFI_ICON, SHGFI_SMALLICON, SHGFI_USEFILEATTRIBUTES = 0x100, 0x1, 0x10
    FILE_ATTRIBUTE_NORMAL, FILE_ATTRIBUTE_DIRECTORY = 0x80, 0x10

    info = SHFILEINFOW()
    attrs = FILE_ATTRIBUTE_DIRECTORY if is_dir else FILE_ATTRIBUTE_NORMAL
    if not shell32.SHGetFileInfoW(
            ctypes.c_wchar_p(name), wintypes.DWORD(attrs), ctypes.byref(info),
            ctypes.sizeof(info),
            SHGFI_ICON | SHGFI_SMALLICON | SHGFI_USEFILEATTRIBUTES):
        return None
    if not info.hIcon:
        return None

    ii = ICONINFO()
    hdc = memdc = None
    try:
        if not user32.GetIconInfo(info.hIcon, ctypes.byref(ii)):
            return None
        bm = BITMAP()
        gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm))
        w, h = int(bm.bmWidth), int(bm.bmHeight)
        if w <= 0 or h <= 0:
            return None

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # negative: top-down rows
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0      # BI_RGB

        hdc = user32.GetDC(None)
        memdc = gdi32.CreateCompatibleDC(hdc)
        bits = (ctypes.c_ubyte * (w * h * 4))()
        if not gdi32.GetDIBits(memdc, ii.hbmColor, 0, h, bits,
                               ctypes.byref(bmi), 0):
            return None

        rgba = bytearray(w * h * 4)
        for i in range(w * h):
            b, g, r, a = bits[i * 4:i * 4 + 4]
            rgba[i * 4:i * 4 + 4] = bytes((r, g, b, a))

        # Icons predating 32-bit alpha come back fully transparent, with
        # the shape carried by a separate 1-bit mask where SET means
        # transparent. Without this they render as nothing at all.
        if not any(rgba[3::4]):
            mask = (ctypes.c_ubyte * (w * h * 4))()
            if gdi32.GetDIBits(memdc, ii.hbmMask, 0, h, mask,
                               ctypes.byref(bmi), 0):
                for i in range(w * h):
                    rgba[i * 4 + 3] = 0 if mask[i * 4] else 255
            else:
                for i in range(w * h):
                    rgba[i * 4 + 3] = 255
        return bytes(rgba), w, h
    finally:
        for h_ in (ii.hbmColor, ii.hbmMask):
            if h_:
                gdi32.DeleteObject(h_)
        if memdc:
            gdi32.DeleteDC(memdc)
        if hdc:
            user32.ReleaseDC(None, hdc)
        user32.DestroyIcon(info.hIcon)


# --------------------------------------------------------------- the API

def available():
    return sys.platform.startswith("win")


def reset():
    """Drop every cached image.

    Must be called before the Tk interpreter is torn down. A PhotoImage
    collected after its interpreter has gone raises out of __del__, where
    the exception cannot be handled and is merely printed - which on a
    windowed build goes nowhere and on a console build looks like a crash
    at exit.
    """
    _cache.clear()


def icon_for(tk, name, is_dir=False):
    """A Tk image for `name`, cached by folder-ness and extension.

    Caching is by extension rather than by name because that is what the
    shell answers on, and a folder of two hundred .JAR files should cost
    one lookup rather than two hundred.
    """
    ext = "" if is_dir else os.path.splitext(name)[1].upper()
    key = ("dir" if is_dir else "file", ext)
    if key in _cache:
        return _cache[key]

    image = None
    if available():
        try:
            got = _shell_icon_rgba("C:\\folder" if is_dir else "file" + ext,
                                   is_dir)
            if got:
                rgba, w, h = got
                image = _photo(tk, rgba, w, h)
        except Exception:
            image = None
    if image is None:
        buf = _drawn_folder() if is_dir else _drawn_file()
        image = _photo(tk, buf, _SIZE, _SIZE)
    _cache[key] = image
    return image

def globe(tk):
    """A small world, for the language button.

    Drawn rather than taken from the shell: there is no standard Windows
    icon that reads as "language", and a flag would be worse - languages
    are not countries.
    """
    import math
    buf = bytearray(_SIZE * _SIZE * 4)
    ink = (56, 104, 168, 255)
    c, r = 7.5, 6.6
    for a in range(0, 360, 2):
        t = math.radians(a)
        sin_t, cos_t = math.sin(t), math.cos(t)
        # the outline, and two meridians drawn as narrower ellipses
        for k in (1.0, 0.5, 0.15):
            _put(buf, int(round(c + r * k * cos_t)),
                 int(round(c + r * sin_t)), ink)
    for x in range(_SIZE):                          # the equator
        if abs(x - c) <= r:
            _put(buf, x, int(round(c)), ink)
    return _photo(tk, buf, _SIZE, _SIZE)


# ----------------------------------------------- the mask editor's tools
# Drawn here rather than taken from the shell: Windows has no stock icon
# for "pen" or "eraser" that reads at 16 pixels, and the five have to
# look like one set. Ink is dark grey rather than black so they sit on a
# Toolbutton without shouting.

_INK = (58, 58, 58, 255)


def _line(buf, x0, y0, x1, y1, rgba=_INK):
    steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    for i in range(steps + 1):
        t = i / float(steps)
        _put(buf, int(round(x0 + (x1 - x0) * t)),
             int(round(y0 + (y1 - y0) * t)), rgba)


def _arc(buf, cx, cy, r, first, last, rgba=_INK):
    import math
    step = 2 if last > first else -2
    for a in range(int(first), int(last), step):
        t = math.radians(a)
        _put(buf, int(round(cx + r * math.cos(t))),
             int(round(cy + r * math.sin(t))), rgba)


def _mirrored(buf):
    out = bytearray(len(buf))
    for y in range(_SIZE):
        for x in range(_SIZE):
            i = (y * _SIZE + x) * 4
            j = (y * _SIZE + (_SIZE - 1 - x)) * 4
            out[j:j + 4] = buf[i:i + 4]
    return out


def _pen():
    buf = _blank()
    for k in range(4):                       # the shaft, as parallel lines
        _line(buf, 5 + k, 11, 11 + k, 5)
    _line(buf, 9, 3, 13, 7)                  # the cap across the top end
    _line(buf, 10, 2, 14, 6)
    _line(buf, 5, 11, 2, 14)                 # the nib, down to its point
    _line(buf, 8, 12, 2, 14)
    _line(buf, 4, 13, 2, 14)
    return buf


def _move():
    buf = _blank()
    _line(buf, 8, 2, 8, 13)
    _line(buf, 2, 8, 13, 8)
    for tx, ty, ax, ay, bx, by in ((8, 2, 6, 4, 10, 4),
                                   (8, 13, 6, 11, 10, 11),
                                   (2, 8, 4, 6, 4, 10),
                                   (13, 8, 11, 6, 11, 10)):
        _line(buf, tx, ty, ax, ay)
        _line(buf, tx, ty, bx, by)
    return buf


def _eraser():
    buf = _blank()
    rub = (198, 116, 116, 255)
    a, b, c, d = (2, 10), (8, 4), (14, 8), (8, 14)
    for k in range(3):                       # the rubbed-out half, filled
        _line(buf, a[0] + k, a[1] + 1, d[0] + k, d[1] - 3 + k, rub)
    for i, (x0, y0) in enumerate((a, b, c, d)):
        x1, y1 = (b, c, d, a)[i]
        _line(buf, x0, y0, x1, y1)
    _line(buf, 5, 7, 11, 11)                 # the crease across it
    return buf


def _undo():
    buf = _blank()
    _arc(buf, 8, 11, 5, 200, 345)            # over the top, right to left
    _line(buf, 3, 9, 6, 8)                   # the head, back on itself
    _line(buf, 3, 9, 4, 12)
    _line(buf, 6, 8, 4, 12)
    return buf


TOOLS = {"pen": _pen, "move": _move, "eraser": _eraser,
         "undo": _undo, "redo": lambda: _mirrored(_undo())}


def tool(tk, name):
    """One of the mask editor's small icons, as a PhotoImage."""
    key = ("tool", name)
    if key not in _cache:
        _cache[key] = _photo(tk, TOOLS[name](), _SIZE, _SIZE)
    return _cache[key]


def _both(fill_a, fill_b, fill_overlap):
    """Two overlapping squares, each region filled or not.

    The three boolean icons differ only in which of the three regions is
    solid, so they are one drawing with three sets of switches - which
    is also why they read as a set at sixteen pixels.
    """
    buf = _blank()
    ink = _INK
    pale = (122, 122, 122, 255)
    # The same height, so the overlap is a clear band up the middle
    # rather than a corner nick that vanishes at this size.
    ax0, ay0, ax1, ay1 = 2, 4, 9, 11
    bx0, by0, bx1, by1 = 6, 4, 13, 11
    for y in range(16):
        for x in range(16):
            in_a = ax0 <= x <= ax1 and ay0 <= y <= ay1
            in_b = bx0 <= x <= bx1 and by0 <= y <= by1
            if in_a and in_b:
                on = fill_overlap
            elif in_a:
                on = fill_a
            elif in_b:
                on = fill_b
            else:
                on = False
            if on:
                _put(buf, x, y, pale)
    for x0, y0, x1, y1 in ((ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1)):
        _line(buf, x0, y0, x1, y0, ink)
        _line(buf, x1, y0, x1, y1, ink)
        _line(buf, x1, y1, x0, y1, ink)
        _line(buf, x0, y1, x0, y0, ink)
    return buf


def _union():
    return _both(True, True, True)


def _intersect():
    return _both(False, False, True)


def _subtract():
    return _both(True, False, False)


def _scissors():
    buf = _blank()
    _line(buf, 3, 2, 11, 11)                 # the two blades, crossing
    _line(buf, 4, 2, 12, 11)
    _line(buf, 12, 2, 4, 11)
    _line(buf, 11, 2, 3, 11)
    _arc(buf, 4, 12, 2, 0, 360)              # and the loops under them
    _arc(buf, 11, 12, 2, 0, 360)
    return buf


TOOLS.update({"union": _union, "intersect": _intersect,
              "subtract": _subtract, "scissors": _scissors})


def _wedge(buf, a, b, c, rgba):
    """A filled triangle, scanned line by line."""
    lo = int(min(a[1], b[1], c[1]))
    hi = int(max(a[1], b[1], c[1]))
    for y in range(lo, hi + 1):
        xs = []
        for (x0, y0), (x1, y1) in ((a, b), (b, c), (c, a)):
            if (y0 <= y <= y1) or (y1 <= y <= y0):
                if y0 == y1:
                    xs += [x0, x1]
                else:
                    xs.append(x0 + (x1 - x0) * (y - y0) / float(y1 - y0))
        if xs:
            for x in range(int(round(min(xs))), int(round(max(xs))) + 1):
                _put(buf, x, y, rgba)


def _flip(across):
    """A wedge and its mirror image, with the axis between them."""
    buf = _blank()
    # One side darker than the other, so the icon says which way round
    # the mirroring goes rather than only that there is a mirror.
    pale, deep = (168, 168, 168, 255), (96, 96, 96, 255)
    if across:
        _wedge(buf, (2, 4), (6, 8), (2, 12), pale)
        _wedge(buf, (14, 4), (10, 8), (14, 12), deep)
        for y in range(2, 15):
            _put(buf, 8, y, _INK)
    else:
        _wedge(buf, (4, 2), (8, 6), (12, 2), pale)
        _wedge(buf, (4, 14), (8, 10), (12, 14), deep)
        for x in range(2, 15):
            _put(buf, x, 8, _INK)
    return buf


TOOLS.update({"fliph": lambda: _flip(True),
              "flipv": lambda: _flip(False)})
