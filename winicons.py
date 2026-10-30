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

The program's own small icons - the mask editor's tools, the zoom column,
the language globe - are 16-pixel PNGs held here as base64. They were
drawn once, pixel by pixel, by the code this replaces (see the history
for winicons.py before October 2026); keeping the pictures rather than
the drawing saves three hundred lines and a few thousand _put() calls at
every start, and Tk 8.6 reads base64 PNG directly.
"""
import base64
import os
import struct
import sys
import zlib

_cache = {}

#: 16 x 16 RGBA PNGs, base64, keyed by the name tool() is asked for.
PNG = {
    '_file':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAALklEQVR42mNgoCZo6Jr2nxiM14Bfv/7gxUPEgGnzVmHFoy4YWS6gyACKMhM5AABtUtNU8ZFiRAAAAABJRU5ErkJggg==",
    '_folder':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAKUlEQVR42mNgGAVgcGSK3H9smGIDCGEUA/6fCCEJjxowKA2gKB0MKAAAeW56iEIhJVMAAAAASUVORK5CYII=",
    'ampin':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAPElEQVR42mNgoCUoKCj4D8IUaSbLEHTNJBmCSzNRhhDSjNcQYjWT5R36RCNVvEAV/1M18KiaGumfmYgFAHu4yyleacKWAAAAAElFTkSuQmCC",
    'ampout':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAOklEQVR42mNgwAMKCgr+gzADuYAkA2CKicUUGUK2S8jyP0UBSXEsDDygiv9JNoSiaKRLKiQrf9A0FgDdTLQTrMB5XQAAAABJRU5ErkJggg==",
    'eraser':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAS0lEQVR42mNgGAUEgZWV1X+KNYNokg3CpoFoQ/ApJOgaYp2NVY5YZxOtGZuzcao7VlLyH6YQxEbGZAUiuiFkRSOys5FdQr+USAwAAILNS1wkaS35AAAAAElFTkSuQmCC",
    'fliph':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAUklEQVR42mNgGPTAysrqP30MWLFixX9SDEhISPiPYQA2Q7AZANKM0wB0Q9ANgGnGawCyIcgGIGsmaADMEJgB6Jpp6wKKwoCiWKAoHQx8UiYXAADp3ZTw5uXeBAAAAABJRU5ErkJggg==",
    'flipv':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAQUlEQVR42mNgGHRgxYoV/wlhigyhyCUUeYeiMKFP6FtZWf0nFdPHZQkJCf8p0gzDFGkm2RBsmok2BJ9msr2DDwAAC5SU8It/wjEAAAAASUVORK5CYII=",
    'globe':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAASUlEQVR42mNgoAWwyFjxHxsmWyNRBiFLItP4xDAMQGdjMwCdjdV2UtgM+ExHV4jTFVQxgBxMfReQHYgURyNVEhLFSZkqmYkcAABaCf8RbAIJzwAAAABJRU5ErkJggg==",
    'intersect':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAKElEQVR42mNgGAVwYGVl9Z8UjNUAdH5VVRUYY5MbNYBWBlAUjSMYAABUsG2NwPhqmgAAAABJRU5ErkJggg==",
    'move':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAO0lEQVR42mNgGPTAysrqP0WaYZhsm9Fp2nsBXQMyn6Bh2BTgMxBngBGDqe8CqoQB1aORooRElaRMFwAAVOpBXAlGTs8AAAAASUVORK5CYII=",
    'pen':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAALElEQVR42mNgGDLAysrqP8WaB4chyJgizSQZQFXNA+dn+mpGN4i+zqZ7JgMAiKRJvQPAJ9MAAAAASUVORK5CYII=",
    'redo':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAMElEQVR42mNgGAWDFFhZWf1HxyRrJiSGUzMhF5FkM7rBeA0h5ESywoNUS2hvwOAEALKxKDnF6n27AAAAAElFTkSuQmCC",
    'scissors':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAASUlEQVR42mNgGFTAysrqPwiTK49XEVGacSkmSTNVDEDWRJZmig0YuDDA52SC3kH3MzYX4DUEXRDdAHxqMfyJLwyI9gbJYUAuAABnG26bvX5SiQAAAABJRU5ErkJggg==",
    'subtract':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAKElEQVR42mNgGAVwYGVl9Z8UjNWAqqoqOEZWhK5h1ABaGkBRNI5gAABFn3wbC9h77QAAAABJRU5ErkJggg==",
    'timein':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAARUlEQVR42mNgGLSgoKDgPzY20ZrRDSDaEJhibAYQNARZIS4DcBqCrgifARiGYFNADKaeARR7gSqBSJVopEpCokpSpisAAHu4yymp1l2dAAAAAElFTkSuQmCC",
    'timeout':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAPUlEQVR42mNgGDSgoKDgP0VqQYLEGIJTHUwCnyF41VDVAGwKcMqjS2BThFcNxQZQ7IXBFwsUJSRaJHvaAwD8crixoGzKygAAAABJRU5ErkJggg==",
    'undo':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAL0lEQVR42mNgGAWDGFhZWf1HxyRrJiRG0GZc8kTZjM8AnIYQ61+CrqA49EdYggMAONAoOcPrQsgAAAAASUVORK5CYII=",
    'union':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAKElEQVR42mNgGAVwYGVl9Z8UjNWAqqoqOEbmY5MbNYBWBlAUjSMYAAClocTh/BL3igAAAABJRU5ErkJggg==",
    'whole':
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAQElEQVR42mNgGDSgoKDgPykYqwGkWEZQEJmPTw6nBkJ8nAZg8ye6GE4DKA5EqsQCRV6gSiBSJRrJTkgUBeKAAQB7uMspdaZ/XQAAAABJRU5ErkJggg==",
}




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
        image = tk.PhotoImage(data=PNG["_folder" if is_dir else "_file"])
    _cache[key] = image
    return image


def globe(tk):
    """A small world, for the language button. A globe rather than a
    flag: languages are not countries."""
    key = ("tool", "globe")
    if key not in _cache:
        _cache[key] = tk.PhotoImage(data=PNG["globe"])
    return _cache[key]


#: The editor's tools, the boolean operations, the flips, undo and redo,
#: and the waveform tab's zoom column - one family, drawn in dark grey
#: ink (the zoom heads lighter) so they sit on a Toolbutton without
#: shouting.
TOOLS = tuple(k for k in PNG if not k.startswith("_") and k != "globe")


def tool(tk, name):
    """One of the program's small icons, as a PhotoImage."""
    key = ("tool", name)
    if key not in _cache:
        _cache[key] = tk.PhotoImage(data=PNG[name])
    return _cache[key]
