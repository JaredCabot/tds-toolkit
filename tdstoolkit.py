"""
tdstoolkit.py - TDS Toolkit: the application window and its tabs, over GPIB.

tds_fs.py already is the filesystem: dir, read, write, mkdir, delete, cwd,
overwrite, and the event-queue drain. This is a front end over it.

Four design points, each of them a lesson from this project rather than a
preference:

  * **One worker thread owns the instrument.** Every VISA call goes through a
    single queue. The bus wedges when two things talk at once or a query is
    interrupted mid-flight, and the symptom - everything times out - looks
    like dead hardware rather than a protocol mistake. The Tk thread never
    touches VISA.

  * **Every upload is verified.** tools/putapp.py exists because a 285-byte
    file once transferred with a clean status and landed on the card as 285
    bytes of zeros. Write, read back, compare, retry; a bad file on the card
    is a script the target shell will happily execute.

  * **Transfers are slow and say so.** Measured: 308,278 bytes in 9.3 s off a
    hard disk, about 33 KB/s; a floppy runs at about 4 KB/s, so the same
    file would take a minute and a half. A screenshot is a ten-second
    operation either way, so progress is reported and nothing polls the bus
    while a transfer is running.

  * **FILESYSTEM:DIR? returns names only** - no sizes, no types, unlike the
    browser on the instrument's own screen. So directory-ness is resolved
    lazily by trying to enter, and sizes are only known once a file has been
    read. The UI says "-" rather than inventing a number.

Usage:
    python tdstoolkit.py              # the GUI
    python tdstoolkit.py --check-translations   # audit lang/*.json
"""
import ast
import atexit
import json
import logging
import logging.handlers
import os
import queue
import re
import struct
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tds_fs import DEFAULT_ADDR
import tds_wfm
import winicons
import i18n
from i18n import gettext as _

# The tabs in ui_*.py import from this module. Run as a script it is
# __main__, and without this their import would load a second copy.
sys.modules.setdefault("tdstoolkit", sys.modules[__name__])

__version__ = "1.3.0"
__author__ = "Jared Cabot"
__email__ = "jetstreamtechnology@protonmail.com"
__licence__ = "MIT"

ATTEMPTS = 4

#: 8.3 sibling name a staged write goes to before it is renamed over the
#: real one. Written, verified, then RENAMEd into place, so a write that
#: fails part way leaves the previous file untouched instead of half over-
#: written. See FILE-TRANSFER-HARDENING.md section 6.
STAGE_NAME = "TDSUPLD.TMP"

# Where the program considers itself to live. Built as a one-file exe,
# __file__ points inside the temporary folder PyInstaller unpacks to and
# then deletes, so a log written next to it would vanish on exit. Next to
# the executable is where a user would look for it.
if getattr(sys, "frozen", False):
    APPDIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APPDIR = os.path.dirname(os.path.abspath(__file__))
LOGFILE = os.path.join(APPDIR, "tdstoolkit.log")

#: Handed run_gui's locals just before the mainloop, if anything has
#: set it. Nothing in this program does.
HOOK = None
SETTINGSFILE = os.path.join(APPDIR, "tdstoolkit.json")

#: A host-side record of files uploaded to each instrument and the sha1
#: verified on the way in, kept as {addr: {dest: sha1}}. An upload can then
#: skip a file already on the instrument byte for byte rather than rewriting
#: it - and rewriting what is already there is the churn that takes this
#: instrument's mass storage down. Beside the settings, never under a folder
#: a build cleans.
UPLOADS_FILE = os.path.join(APPDIR, "tdstoolkit-uploads.json")


def _json_load(path):
    """A dict from `path`, or {} for anything else. Never raises.

    UTF-8, with or without the byte-order mark Notepad adds, the same
    as the settings file (A15). The locale's encoding, cp1252 on most
    Windows machines, read an IDN or a path in the manifest differently
    from how a hand edit had saved it.
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def _json_save(path, data):
    """Best effort: a lost manifest costs a re-upload, not data. True if
    it was written."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return True
    except Exception:
        return False


def load_uploads():
    return _json_load(UPLOADS_FILE)


def save_uploads(marks):
    _json_save(UPLOADS_FILE, marks)


#: Instruments a transfer left wedged, kept as {addr: why}. A run that
#: writes files and then finds the volume root will not list has taken the
#: file system down, and writing into it again is what turns a hung task
#: into a lost card (FILE-TRANSFER-HARDENING.md section 9). Such an
#: instrument is locked out of further writes until a fresh reconnect
#: proves it well again - a retry alone will not clear it, which is the
#: point. Presence of the addr means locked out.
TRANSFERS_FILE = os.path.join(APPDIR, "tdstoolkit-transfers.json")


def load_transfers():
    return _json_load(TRANSFERS_FILE)


def save_transfers(state):
    return _json_save(TRANSFERS_FILE, state)


#: The lock-outs this run has set, held in memory as well as in the file.
#: The file is what carries one past a restart; this is what keeps it in
#: force when the file cannot be written - a program run from a read-only
#: folder used to save nothing and so never lock out at all (A12).
_transfers_here = {}


def transfer_blocked(addr):
    """Why this instrument is locked out after a failed transfer, or None."""
    return (load_transfers().get(addr or "")
            or _transfers_here.get(addr or ""))


def set_transfer_block(addr, why):
    _transfers_here[addr or ""] = why
    state = load_transfers()
    state[addr or ""] = why
    save_transfers(state)


def clear_transfer_block(addr):
    _transfers_here.pop(addr or "", None)
    state = load_transfers()
    if state.pop(addr or "", None) is not None:
        save_transfers(state)


#: Volumes this program formatted and then saw list empty, as (addr,
#: VOLUME). An empty root otherwise reads as wedged (see health_check),
#: which refused a restore onto a freshly formatted disk. Dropped as soon
#: as anything is written there, so a write that still leaves the root
#: empty reads as wedged again. Kept for this run only: a power cycle in
#: between proves nothing either way, so formatting again is the answer.
_formatted = set()


#: One program on the bus at a time. A second program sending to the
#: instrument during a transfer lands its command inside the file being
#: written (FILE-TRANSFER-HARDENING.md section 1), so only one copy may
#: hold an instrument session at once. The lock is the operating
#: system's, on a file beside the settings: it goes when the process
#: goes, however it goes. The pid-file lock it replaced was left behind by
#: a power cut, Task Manager or a hard crash, and Windows hands pids out
#: again quickly, so an unrelated program could hold that number and
#: block every connect until somebody found the file and deleted it (A8).
#: The pid is still written into the file, for the message.
LOCKFILE = os.path.join(APPDIR, "tdstoolkit.lock")
_lock_pid = None
_lock_fh = None
#: Where in the file the lock sits: past the pid, so a second copy can
#: still read who holds it. Windows locks are mandatory, and a lock over
#: the text would make it unreadable.
LOCK_AT = 1024


def _os_lock(fh):
    """Lock one byte of `fh` without waiting. True if this process has it."""
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(LOCK_AT)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def take_process_lock(addr=""):
    """Claim the bus. Returns None on success, or a sentence to show.

    Held for the life of this run once taken; a reconnect by the same
    process is a no-op. Never blocks the user if the lock file itself
    cannot be written - a missing safeguard is better than a program that
    will not start.
    """
    global _lock_pid, _lock_fh
    ours = os.getpid()
    if _lock_pid == ours:
        return None
    try:
        fh = open(LOCKFILE, "a+")
    except Exception:
        return None
    if not _os_lock(fh):
        fh.close()
        # Unbuffered, and only the first 64 bytes: a buffered read asks
        # Windows for a whole block, which overlaps the locked byte and
        # is refused, and the message then named process 0.
        try:
            with open(LOCKFILE, "rb", buffering=0) as raw:
                held = int((raw.read(64).split() or [b"0"])[0])
        except Exception:
            held = 0
        return i18n.say(
            "Another copy of TDS Toolkit (process %d) is using the "
            "instrument. Only one may at a time - a second program's "
            "commands would land inside a file being transferred. Close "
            "the other copy, then connect again.", held)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write("%d %s" % (ours, addr or ""))
        fh.flush()
    except Exception:
        pass
    _lock_fh, _lock_pid = fh, ours
    atexit.register(release_process_lock)
    return None


def release_process_lock():
    global _lock_pid, _lock_fh
    if _lock_pid != os.getpid() or _lock_fh is None:
        return
    try:
        _lock_fh.close()                 # closing lets the lock go
        os.remove(LOCKFILE)
    except Exception:
        pass
    _lock_pid, _lock_fh = None, None


def address_argument(argv=None):
    """--address GPIB0::3::INSTR, or --address=GPIB0::3::INSTR, or None.

    Overrides the remembered address for one run: two instruments on one
    bus and you want the other one.
    """
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--address")
    got = parser.parse_known_args(
        (sys.argv if argv is None else argv)[1:])[0].address
    return got.strip() if got else None


def load_capabilities():
    """What we already know about particular instruments.

    A file beside the program wins over the bundled one, so an instrument
    can be added without rebuilding anything.
    """
    for path in (os.path.join(APPDIR, "capabilities.json"),
                 resource("capabilities.json")):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            rows = data.get("instruments")
            if isinstance(rows, list):
                return rows
        except FileNotFoundError:
            continue
        except Exception as exc:
            log_note("capabilities", "%s unreadable (%s)" % (path, exc))
    return []


def known_instrument(idn):
    """The table's entry for this *IDN?, or None.

    *IDN? is "TEKTRONIX,TDS 784D,0,CF:91.1CT FV:v7.4e": model second,
    firmware last. Matching is exact on the model and either exact or '*'
    on the firmware, so a new firmware version on a known model falls
    through to being asked rather than being assumed.
    """
    parts = [p.strip() for p in (idn or "").split(",")]
    if len(parts) < 4:
        return None
    model, firmware = parts[1], parts[-1]
    for row in load_capabilities():
        if row.get("model", "").upper() != model.upper():
            continue
        want = row.get("firmware", "*")
        if want == "*" or want.upper() in firmware.upper():
            return row
    return None


#: What the System tab's hardcopy and RS-232 boxes offer. One table
#: rather than lists inline in the widget loops, because the same
#: spellings are needed twice: to build the comboboxes, and to expand a
#: reply that came back in Tektronix's short form into the entry it
#: names. Without the second use a 680B answering NON leaves the Parity
#: box reading NON, which is not one of the choices and looks truncated.
#: The self test's areas, and what each one is actually testing.
#:
#: ALL is the only argument any of them takes. Measured on a TDS 754D
#: running v8.0e: DIAg:SELect:<area> with MEMory, FUNCtional or
#: REGister - the three words the programmer manual's own description
#: of ALL implies must exist - is refused with event 102 on every one
#: of the five areas. So there is no memory-only run to be had over the
#: bus, and the area is as targeted as this gets.
#:
#: Which is targeted enough for what people come here for. The
#: acquisition memory tests live in ACQUISITION and the display memory
#: tests in DISPLAY, so the two are separately runnable already; what
#: was missing is anything saying so, because "ACQUISITION" does not.
#: The sub-test names are out of the firmware's own symbol table -
#: digAcqMemAddrDiag, digAcqMemDataDiag, digAcqMemPatDiag and
#: digAtSpeedAcqMemDiag with a lettered variant per channel;
#: dsyRastModeV0Walk and V1Walk, dsyDiagRasRegMem, dsyDiagPPRegMem.
#: Ordered for somebody chasing a memory fault: everything, then the
#: two areas that carry a memory array, then the rest. What each one
#: covers is said in sys_diag_what, where the sentences can be
#: translated.
DIAG_AREAS = ("ALL", "ACQUISITION", "DISPLAY", "CPU", "FPANEL")

SYS_CHOICES = {
    "sysport": ("GPIB", "RS232", "CENTRONICS", "FILE"),
    "sysformat": ("BMP", "BMPCOLOR", "TIFF", "PCX", "PCXCOLOR",
                  "EPSIMAGE", "INTERLEAF", "THINKJET", "DESKJET",
                  "LASERJET", "EPSON"),
    "syslayout": ("LANDSCAPE", "PORTRAIT"),
    "sysbaud": ("300", "600", "1200", "2400", "4800", "9600", "19200"),
    "sysparity": ("NONE", "EVEN", "ODD"),
    "sysstop": ("1", "2"),
}


def firmware_options(idn):
    """The option codes this instrument's firmware reads, or None.

    None means "not known", not "none of them": an instrument missing
    from the table, or a firmware version missing from its model's rows,
    gets no opinion rather than a guess. A code absent from a known list
    has no getter anywhere in that ROM, so the option cannot appear on
    that instrument however its word is written.
    """
    got = (known_instrument(idn) or {}).get("options")
    return set(got) if isinstance(got, list) else None


def load_settings():
    """Remembered settings, or an empty dict. Never raises.

    Kept beside the program rather than in the registry or a profile
    folder, so that copying the executable somewhere else takes its
    settings with it and removing it leaves nothing behind.

    Read as utf-8-sig, because a file edited in Notepad and saved as UTF-8
    acquires a byte-order mark that plain json.load chokes on. A file that
    will not parse is moved aside rather than left to be overwritten by
    the next save - losing a remembered address is a small thing, but
    losing it silently is not.
    """
    try:
        with open(SETTINGSFILE, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        spoiled = SETTINGSFILE + ".bad"
        try:
            if os.path.exists(SETTINGSFILE):
                if os.path.exists(spoiled):
                    os.remove(spoiled)
                os.replace(SETTINGSFILE, spoiled)
                log_note("settings", "unreadable (%s); kept as %s"
                         % (exc, os.path.basename(spoiled)))
        except Exception:
            pass
        return {}


def save_settings(data):
    """Best effort. A read-only folder is a good reason not to save and a
    poor reason to interrupt the user, so failure is silent."""
    try:
        with open(SETTINGSFILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        return True
    except Exception:
        return False


def tab_names(tab):
    """A tab's widgets and methods by name, the way build(ui) used to
    leave its locals: run_gui and the tests still look them up so."""
    out = {n: getattr(tab, n) for n in dir(type(tab))
           if not n.startswith("__")}
    out.update(vars(tab))
    out.pop("ui", None)
    return out


def N_(text):
    """Mark `text` for translation without translating it. See localise()."""
    return text


def exc_words(exc):
    """str(exc), keeping the Said a module raised it with."""
    first = exc.args[0] if len(exc.args) == 1 else None
    return first if isinstance(first, i18n.Said) else str(exc)


def localise(text):
    """`text` in the current language, including the modules' words.

    The modules below the window have no gettext and should not have one
    (see tds_wfm.plot_png); what they say to a user they make with
    i18n.say(), which keeps the template and the values, and that is
    translated here. An exception raised with one counts as it. A string
    the catalogue knows is translated as _() would; anything else comes
    back unchanged.
    """
    if isinstance(text, BaseException):
        text = exc_words(text)
    if isinstance(text, i18n.Said):
        return text.translated()
    if not isinstance(text, str):
        return text
    return _(text)


#: Chinese and Japanese marks that may not begin a line, and those that
#: may not end one - the core of the kinsoku rules both languages set by.
NO_LINE_START = set("。，、．：；！？）」』】〕〉》”’…ー々～ぁぃぅぇぉっゃゅょ"
                    "ゎァィゥェォッャュョヮヵヶ%),.:;!?]}")
NO_LINE_END = set("（「『【〔〈《“‘([{")


def cjk_break(text, measure, room):
    """Break Chinese or Japanese into lines no wider than `room`.

    Tk breaks a line only at a space. In text with no spaces that leaves
    it to cut wherever the width runs out, which put "。" and "，" at the
    start of lines; and where there is one space it breaks there, which
    left a numbered step's "3." on a line of its own above its sentence.
    Here a line may break between any two characters, but not inside a
    run of Latin letters or digits, not before a closing mark and not
    after an opening one: the character before a closing mark goes down
    with it. `measure` gives a string's width in the units of `room`.
    """
    out = []
    for para in text.split("\n"):
        units = re.findall(r"[!-~]+ *| +|.", para)
        lines, cur = [], []
        for unit in units:
            if not cur and unit.isspace():
                continue
            if cur and measure("".join(cur) + unit.rstrip()) > room:
                carry = []
                if unit[0] in NO_LINE_START and len(cur) > 1:
                    carry.append(cur.pop())
                while len(cur) > 1 and cur[-1][-1] in NO_LINE_END:
                    carry.insert(0, cur.pop())
                lines.append("".join(cur).rstrip())
                cur = carry
            cur.append(unit)
        lines.append("".join(cur).rstrip())
        out.append("\n".join(lines))
    return "\n".join(out)


def describe_visa_error(exc):
    """Turn a VISA exception into something a person can act on.

    VISA lists every address it has ever been configured with, so a bus
    with one live instrument can show half a dozen. Saying which of them
    are switched off, which are busy and which were never there saves the
    user working it out by unplugging things.
    """
    text = "%s" % exc
    for needle, plain in (
            ("VI_ERROR_TMO", "no reply - switched off, or not at this "
                             "address"),
            ("VI_ERROR_RSRC_NFOUND", "not present"),
            ("VI_ERROR_RSRC_BUSY", "in use by another program"),
            ("VI_ERROR_NCIC", "the interface is not controller in charge"),
            ("VI_ERROR_NLISTENERS", "nothing listening on the bus"),
            ("VI_ERROR_INV_RSRC_NAME", "address not understood by VISA"),
            ("VI_ERROR_CONN_LOST", "connection lost")):
        if needle in text:
            return plain
    return type(exc).__name__


def looks_like_scope(idn):
    """Is this identification string a TDS oscilloscope?

    Matched loosely: any Tektronix TDS. The program only knows the
    500/600/700 filesystem command set, but refusing to list a TDS3000
    would be less helpful than letting someone try it and find out.
    """
    up = (idn or "").upper()
    return "TEKTRONIX" in up and "TDS" in up


def resource(name):
    """Locate a bundled file, running from source or from the built exe.

    PyInstaller unpacks bundled data to a temporary folder and points
    sys._MEIPASS at it, which is not where APPDIR points - APPDIR is
    deliberately next to the executable, for the log.
    """
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.abspath(__file__))
    return os.path.join(base, name)


def is_phantom(name):
    """True for a directory entry that is not a real file.

    A card imaged from Windows carries VFAT long-filename records for any
    name Windows did not consider 8.3-clean - a lowercase one, typically.
    The instrument's FAT16 driver predates VFAT, so it reads each of those
    records as though it were an ordinary 8.3 entry and reports the result
    as a file. The layout is what gives them away: byte 0 is the sequence
    number, 0x41 for a single-record name, which reads as 'A'; byte 1 is
    the first character of the long name; byte 2 is the high half of that
    UTF-16 character and is zero, which ends the string. So they all come
    back as two characters and a bare trailing dot - "AT." ahead of
    TDS.JAR, "At." ahead of temp.

    They are not files. Reading one returns nothing, and deleting one
    would strip the long name from the real file that follows it, so they
    are filtered out before the UI ever sees them. A trailing dot is a
    safe test: FAT cannot store a name with an empty extension.
    """
    return (not name or name.endswith(".")
            or any(ch < " " or ch == "\x7f" for ch in name))


def real_names(names):
    return [n for n in names if not is_phantom(n)]


# Beyond letters and digits, these are the punctuation marks DOS accepts in
# a short file name. Everything else is rejected by the instrument.
NAME_PUNCT = "$%'-_@~`!(){}^#&"


def _describe_char(ch):
    """Name a character the way a person would say it aloud."""
    return {" ": _("space"), "\t": _("tab"), ".": _("extra dot"), "/": _("slash"),
            "\\": _("backslash"), ":": _("colon"), "*": _("asterisk"),
            "?": _("question mark"), '"': _("double quote"), "<": _("less than"),
            ">": _("greater than"), "|": _("vertical bar"), ",": _("comma"),
            ";": _("semicolon"), "+": _("plus"), "=": _("equals"),
            "[": _("left bracket"), "]": _("right bracket")}.get(
                ch, "'%s'" % ch if ch.isprintable() else
                "character code %d" % ord(ch))


def suggest_copy_name(name, taken):
    """An unused 8.3 name for a copy of `name`.

    "Copy of X" does not fit in eight characters, so the stem is trimmed
    and numbered instead: REPORT.TXT -> REPORT1.TXT, and REPORT1 taken ->
    REPORT2. Falls back to the original if every number is used, which
    leaves the dialog to reject it rather than inventing something worse.
    """
    stem, dot, ext = name.upper().partition(".")
    stem = stem.rstrip("0123456789") or stem
    for n in range(1, 100):
        tail = str(n)
        candidate = stem[:8 - len(tail)] + tail + dot + ext
        if candidate not in taken:
            return candidate
    return name.upper()


def check_83(name):
    """Explain why `name` is not a valid 8.3 name, or return None if it is.

    The instrument's filesystem is FAT16 with no long-name support, and it
    does not complain about a name it cannot store - it just does nothing.
    So the name is checked here, before anything is sent, and the reason is
    spelled out rather than left for the user to deduce from silence.
    """
    if not name or not name.strip():
        return _("A name is required.")
    problems = []
    if name != name.strip():
        problems.append(_("The name starts or ends with a space."))

    stem, dot, ext = name.partition(".")
    if name.count(".") > 1:
        problems.append(_("A name may contain only one dot."))
        ext = ext.replace(".", "")

    bad = []
    for ch in name:
        if ch == ".":
            continue
        if not (ch.isascii() and (ch.isalnum() or ch in NAME_PUNCT)):
            d = _describe_char(ch)
            if d not in bad:
                bad.append(d)
    if bad:
        problems.append(_("These characters are not allowed: %s")
                        % ", ".join(bad))

    if len(stem) > 8:
        problems.append(_("The part before the dot is %d characters long; "
                        "8 is the most that will fit.") % len(stem))
    if not stem:
        problems.append(_("There is nothing before the dot."))
    if dot and len(ext) > 3:
        problems.append(_("The part after the dot is %d characters long; "
                        "3 is the most that will fit.") % len(ext))
    if dot and not ext:
        problems.append(_("There is a dot with nothing after it."))

    if not problems:
        return None
    return (_("'%s' cannot be used as a name on the instrument.\n\n"
            "%s\n\n"
            "Names follow the DOS 8.3 rule: up to 8 characters, then "
            "optionally a dot and up to 3 more. Letters, digits and "
            "%s may be used, and the name is stored in capitals.")
            % (name, "\n".join("  -  " + p for p in problems),
               " ".join(NAME_PUNCT)))


def to_83(name, taken=()):
    """Propose a legal 8.3 name for a file dragged in from Windows.

    Windows names are almost never 8.3-clean, and refusing a drop for that
    reason would make the feature useless. So a name is proposed instead
    and shown for approval before anything is sent - the user sees exactly
    what each file will be called on the instrument.

    `taken` is the set of names already spoken for, in this drop or already
    in the destination folder. A clash gets a ~1, ~2 tail, the same
    convention Windows itself uses for short-name aliases.
    """
    up = os.path.basename(name).upper()
    stem, _, ext = up.rpartition(".")
    if not stem:                       # no dot at all
        stem, ext = up, ""

    def clean(s):
        return "".join(c for c in s
                       if c.isascii() and (c.isalnum() or c in NAME_PUNCT))

    stem, ext = clean(stem)[:8], clean(ext)[:3]
    if not stem:
        stem = "FILE"
    taken = {t.upper() for t in taken}
    candidate = stem + ("." + ext if ext else "")
    n = 0
    while candidate in taken:
        n += 1
        tail = "~%d" % n
        candidate = stem[:8 - len(tail)] + tail + ("." + ext if ext else "")
    return candidate


# Events this program provokes on purpose and understands. Anything else is
# worth recording and worth telling the user about, because the one thing
# this instrument does badly is fail quietly.
EXPECTED_EVENTS = {
    256,   # File name not found - normal when checking whether a name exists
    420,   # Query unterminated - a query the firmware chose not to answer
    113,   # Undefined header - only ever from deliberate probing
    401,   # Power on - the instrument's first words after being switched on
}

# Probing for volumes means naming drives that may not exist, and being told
# so is the answer, not a fault. A TDS 784D says 256, "file name not found";
# a TDS 640A says 257, "file name error", for exactly the same question. Only
# expected while probing - 257 during a real transfer means something.
PROBE_OPS = ("volumes",)
BAD_FILENAME = 257

#: Operations during which "there is no disk in the drive" is an answer
#: rather than a fault, so the event watcher lets it pass.
#:
#: Probing volumes names drives that may be empty. Listing a directory
#: asks the same question of whichever drive is current, and asks it on
#: purpose: listdir_split reads the event queue itself and hands back
#: `no_media`, which the file pane reports as "No disk in the drive".
#: Without this the same empty drive was reported twice - once properly
#: by the pane, and once by the watcher as an unexpected instrument
#: fault, which is what starting the program with an empty floppy drive
#: used to look like.
#:
#: Connecting asks whether this firmware has READFILE by naming a file
#: that is not there. On an instrument whose current volume is an empty
#: floppy drive - a TDS 754D with no hard disk fitted, which is where
#: this turned up - the answer is 252 rather than 256, and it is just as
#: conclusive: the command was understood. Greeting the user with a
#: warning box about a drive they know is empty is not.
#:
#: Only this code, and only in these operations. 257 during a listing
#: still means something, and neither code is routine during a
#: transfer.
NO_MEDIA_OPS = ("volumes", "split", "connect")

# "Missing media": the drive is there and there is no disk in it. Ordinary
# while probing - an empty floppy drive is not a fault - and worth saying
# in as many words when it stops a transfer, because "no disk in the
# drive" is something the user can do something about.
NO_MEDIA = 252

# "Undefined header": the instrument was sent a command its firmware does
# not have. Harmless while probing on purpose; conclusive when it comes back
# from a transfer, because it means that firmware cannot do transfers at all.
UNDEFINED_HEADER = 113

# How long to wait for a file transfer to start producing data. Long enough
# for a floppy to spin up and seek, short enough that an instrument which is
# never going to answer says so in seconds rather than in minutes.
TRANSFER_TIMEOUT = 20.0

# Bytes per second a READFILE is budgeted at, for sizing a read-back to
# the file rather than giving every file the same twenty seconds.
# Measured on a TDS 784D at 19.1, 32.5 and 32.7 kB/s for 4 kB, 64 kB and
# 256 kB; a third of that is used, because this sets a ceiling for giving
# up rather than a wait.
READ_RATE = 10000.0

# How many files in a row may fail before a batch upload gives up. One
# failure is a file; two in a row is the instrument, and every attempt
# deletes before it writes, so ploughing on empties folders it cannot
# refill.
GIVE_UP_AFTER = 2

# Event 250 "Mass storage error - osError" during a recursive delete is the
# instrument's own doing, not ours. Measured on a freshly imaged card: with
# the bus left completely silent for five seconds after RMDIR - no *OPC?,
# no drain, nothing that could collide - it still appeared in 4 of 12
# trials, and the folder was correctly removed in all 12. It is reported
# and recorded, but it is not treated as a failure, because the delete is
# verified by re-listing rather than by trusting the event queue.
DELETE_OPS = ("rmdir", "delete", "deletes")
MASS_STORAGE = 250


# Facts about this session, held rather than written. See log_context.
_LOGGED_CONTEXT = {}

# Half a megabyte, then the log is moved aside and a new one started.
# One generation is kept, which is what a user is asked to send: the
# session that went wrong, and the one before it. Without this the file
# only ever grows, and the first thing a long-running install does with
# a fault report is attach four megabytes of successful startups.
LOGMAX = 512 * 1024

_log = logging.getLogger("tdstoolkit")
_log.setLevel(logging.INFO)
_log.propagate = False


def log_handler(path=None):
    """The log file: opened at the first line written (delay), so a
    session with nothing to say leaves no file, and moved aside to .old
    once it passes LOGMAX.

    The path is an argument so rotation can be tried on a file of its
    own rather than on the log the run is writing to: a check that
    rotates the real log to prove rotation works has thrown away the
    evidence of the run that was checking.
    """
    handler = logging.handlers.RotatingFileHandler(
        path or LOGFILE, maxBytes=LOGMAX, backupCount=1, encoding="utf-8",
        delay=True)
    handler.namer = lambda name: name[:-len(".1")] + ".old"
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(where)-22s %(message)s", "%Y-%m-%d %H:%M:%S"))
    return handler


def log_context(where, text):
    """Remember something about this session without writing it down.

    The log is for faults. What a run that went well did is nobody's
    business, but what a run that went wrong was talking to - which
    instrument, over which transfer route, with which options fitted -
    is the first thing anyone reading the fault needs, and by then it
    is too late to ask. So these are held, and written only if
    something is written at all: beside the banner, which has always
    been lazy for the same reason.

    Last one wins. This is the state a session is in, not its history;
    a key set twenty times with the same answer still costs one line.
    A key set again after the log has been written to is a change, and
    goes out with the next fault rather than being lost behind the
    first one.
    """
    _LOGGED_CONTEXT[where] = text


def log_note(where, text):
    """Append a line to the log beside the script. Never raises.

    For faults and crashes. Anything a working program does routinely
    belongs in the status line, which the next press replaces, and not
    in a file that is kept - see log_context for the exception.

    The first line written in a session identifies the version, because a
    log someone sends you is worthless if you cannot tell what produced
    it. It is written lazily rather than at startup so that an uneventful
    session still writes nothing at all.
    """
    try:
        if not _log.handlers:
            _log.addHandler(log_handler())
            _log.info("TDS Toolkit %s (%s) started", __version__,
                      "exe" if getattr(sys, "frozen", False)
                      else "python %s" % sys.version.split()[0],
                      extra={"where": "==="})
        # Drained, not copied: whatever is still held goes out in
        # front of the fault, and a fact that changes after this
        # goes out in front of the next one.
        for key in list(_LOGGED_CONTEXT):
            _log.info(_LOGGED_CONTEXT.pop(key), extra={"where": key})
        _log.info(text, extra={"where": where})
    except Exception:
        pass


# The Worker - the one thread every instrument job runs on - is in
# worker.py, with each tab's jobs in worker_*.py. It is imported at the
# foot of this module, because those take their constants and helpers
# from here.


# ---------------------------------------------------------------- previews

def describe(name, data):
    """A useful text preview, chosen by extension. Returns (kind, text)."""
    up = name.upper()
    if up.endswith(".SET"):
        return "set", describe_set(data)
    if up.endswith(".BMP"):
        return "bmp", describe_bmp(data)
    if up.endswith((".APP", ".BAT", ".TXT", ".LOG", ".CSV", ".INI")):
        try:
            return "text", data.decode("latin-1")
        except Exception:
            pass
    return "hex", hexdump(data[:1024])


def describe_set(data):
    """Decode a setup file's mask geometry - doc/16 7a."""
    PT_BASE, STRIDE, CNT, CK, CKS = 0x0816, 200, 0x0E56, 0x1208, 0x1C
    if len(data) < CK + 2:
        return "not a recognisable .SET (too short: %d bytes)" % len(data)
    out = ["%d bytes" % len(data)]
    stored = struct.unpack_from(">H", data, CK)[0]
    calc = 0
    for o in range(CKS, len(data) - 1, 2):
        if o != CK:
            calc += struct.unpack_from(">H", data, o)[0]
    calc &= 0xFFFF
    out.append("checksum %04x stored, %04x computed  -> %s"
               % (stored, calc, "OK" if stored == calc else "MISMATCH"))
    out.append("")
    any_pts = False
    for m in range(1, 9):
        n = data[CNT + (m - 1)]
        if not n or n > 50:
            continue
        any_pts = True
        out.append("mask %d: %d points" % (m, n))
        out.append("      #        x        y      x %       y %")
        for k in range(n):
            x, y = struct.unpack_from(">HH", data, PT_BASE + (m - 1) * STRIDE
                                      + 4 * k)
            out.append("    %3d  %7d  %7d  %7.2f  %7.2f"
                       % (k + 1, x, y, (x + 2226) / 50.0, (y - 34) / 4.0))
    if not any_pts:
        out.append("no user mask points stored in any of the eight masks")
    return "\n".join(out)


def describe_bmp(data):
    if len(data) < 54 or data[:2] != b"BM":
        return "not a BMP (%d bytes)" % len(data)
    size, _, _, off = struct.unpack_from("<IHHI", data, 2)
    w, h, _planes, bpp = struct.unpack_from("<iihh", data, 18)
    return ("BMP %d x %d, %d bpp, %d bytes (header says %d), pixels at %d\n"
            "\nUse Save As to write it to the PC; the viewer renders it."
            % (w, h, bpp, len(data), size, off))


def hexdump(data, width=16):
    out = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        txt = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append("%04x  %-*s  %s" % (off, width * 3,
                                       " ".join("%02x" % b for b in chunk),
                                       txt))
    if not out:
        return "(empty)"
    return "\n".join(out)


# --------------------------------------------------------------------- UI

def type_of(name):
    """A Windows-ish type description from the extension."""
    up = name.upper()
    ext = up.rsplit(".", 1)[-1] if "." in up else ""
    known = {"APP": "Application launcher", "BAT": "Batch script",
             "SET": "Instrument setup", "BMP": "Bitmap image",
             "JAR": "Java archive", "ZIP": "Compressed archive",
             "O": "Object file", "BIN": "Binary", "TXT": "Text Document",
             "LOG": "Log file", "CSV": "CSV file", "INI": "Configuration",
             "WFM": "Waveform", "DAT": "Data file"}
    # Translated at the point of use, not when the table is built, so the
    # column follows a language change without the table being rebuilt.
    if ext in known:
        return _(known[ext])
    return (_("%s File") % ext) if ext else _("File")


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox
    from tkinter import simpledialog, ttk
    import types
    import ui_fw
    import ui_scr
    import ui_err
    import ui_sys
    import ui_bak
    import ui_set
    import ui_wfm
    import ui_msk
    import ui_lim
    import ui_files

    # What the tabs built in ui_*.py share with this function: its names,
    # copied in before each tab is built and again once everything is
    # defined, and whatever each tab builds, handed back. See ui_fw.build.
    ui = types.SimpleNamespace()

    # Drag and drop from Explorer needs the tkdnd Tcl extension, which
    # tkinterdnd2 ships. Its absence costs only that one feature, so it is
    # optional rather than required: the program still runs without it and
    # says so once, in the status bar, rather than failing to start.
    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        DND = TkinterDnD
    except Exception:
        DND, DND_FILES = None, None

    # Languages come from lang/*.json beside the program, so one can be
    # added or corrected without rebuilding anything. A remembered choice
    # wins; failing that, the language Windows is set to; failing that,
    # English.
    i18n.discover(APPDIR)
    i18n.use(load_settings().get("language") or i18n.system_default())

    w = Worker()
    root = DND.Tk() if DND else tk.Tk()
    root.title("TDS Toolkit %s" % __version__)
    # Wide enough for the waveform toolbar in the longest language it
    # ships in. Six buttons of German or Spanish need more room than the
    # 1100 this opened at, and the one that was squeezed - Refresh, packed
    # to the right and so given whatever is left - came out 35 pixels wide
    # for a 71 pixel word.
    # Tall enough to show a screenshot turned on its side without
    # cropping it. A screen is 640 x 480, so landscape is 640 tall, and
    # the toolbars, tab strip and status row above and below it come to
    # about 150 more. Cut down to fit where the desktop is smaller than
    # that, because a window taller than the screen cannot be resized
    # from its own bottom edge.
    tall = min(830, max(560, root.winfo_screenheight() - 90))
    root.geometry("1280x%d" % tall)
    root.minsize(980, 560)
    try:
        root.iconbitmap(resource("app.ico"))
    except Exception:
        pass          # an icon is not worth failing to start over

    # A remembered address beats the compiled-in default, so the program is
    # usable by anyone whose scope is not at the address it was written
    # against - which is nearly everyone.
    # Everything that carries words, so a language change can relabel it
    # rather than leaving half the window in the last language. Declared
    # before any widget is made, because widgets register as they are
    # built: anything added afterwards and forgotten is exactly what
    # went untranslated last time.
    labelled = []          # (widget, English source)
    relabel = []           # anything that needs more than a text= setting
    buttons = []           # what busy() greys while a job runs
    by_name = {}           # so a command the instrument lacks can be greyed

    def says(widget, english, **kw):
        """Label a widget now, and again whenever the language changes."""
        labelled.append((widget, english))
        widget.config(text=_(english), **kw)
        return widget

    def named(page, english):
        """The same for a notebook tab, which is labelled by its parent."""
        relabel.append(lambda p=page, s=english: tabs.tab(p, text=_(s)))
        return page

    def dialog_open():
        """Is one of this program's dialogs actually on screen?

        Every dialog puts itself in state["dialog"] and almost none of
        them take themselves out again, so a window closed an hour ago
        went on counting as open. The one place that asks - the Options
        button - then did nothing at all, silently, for the rest of the
        session. Asking the window whether it still exists is the answer
        that cannot go stale.
        """
        dlg = state.get("dialog")
        if dlg is not None and dlg.winfo_exists():
            return True
        state.pop("dialog", None)
        return False

    def popup(title, resizable=None):
        """A dialog of this program's: titled, transient, with its icon
        and a padded frame. Returns (window, frame)."""
        dlg = tk.Toplevel(root)
        dlg.title(title)
        dlg.transient(root)
        if resizable is not None:
            dlg.resizable(*resizable)
        try:
            dlg.iconbitmap(resource("app.ico"))
        except Exception:
            pass
        pad = ttk.Frame(dlg, padding=10)
        pad.pack(fill="both", expand=True)
        return dlg, pad

    def hints(widget, english):
        """A word about what a button does, after a moment's hover.

        Half a second's wait, so running the pointer along a row of
        buttons on the way to something else does not light up every one
        of them. Looked up when it is shown rather than when it is made,
        so it follows a language change with nothing to re-register.
        """
        held = {"tip": None, "due": None}

        def hide(_evt=None):
            if held["due"] is not None:
                widget.after_cancel(held["due"])
                held["due"] = None
            if held["tip"] is not None:
                held["tip"].destroy()
                held["tip"] = None

        def show():
            held["due"] = None
            if not widget.winfo_ismapped():
                return
            tip = tk.Toplevel(widget)
            tip.overrideredirect(True)
            tk.Label(tip, text=_(english), background="#ffffe1",
                     relief="solid", borderwidth=1, padx=4).pack()
            # Under the button, and never off the right of the screen:
            # the buttons at the right-hand end of a row are exactly the
            # ones whose tooltip would go over the edge.
            tip.update_idletasks()
            room = widget.winfo_screenwidth() - tip.winfo_reqwidth() - 4
            tip.geometry("+%d+%d"
                         % (max(0, min(widget.winfo_rootx() + 12, room)),
                            widget.winfo_rooty()
                            + widget.winfo_height() + 3))
            held["tip"] = tip

        def wait(_evt=None):
            hide()
            held["due"] = widget.after(500, show)

        widget.bind("<Enter>", wait, add="+")
        widget.bind("<Leave>", hide, add="+")
        widget.bind("<ButtonPress>", hide, add="+")
        return widget

    # A UI font for the scripts Segoe UI has no glyphs for. Left to
    # Windows, the missing glyphs come from whichever CJK font its font
    # linking lists first - Meiryo UI on an English Windows - so Simplified
    # Chinese came out in Japanese glyph shapes, and Japanese would in
    # Chinese ones on a Chinese Windows. The named fonts are changed, so
    # every widget follows, and put back for any other language.
    #
    # YaHei UI is two pixels taller a line than Segoe UI at 9 points (17
    # against 15), which the System tab's layout has to leave room for.
    SCRIPT_FONTS = {"zh-Hans": ("Microsoft YaHei UI", "Microsoft YaHei"),
                    "ja": ("Meiryo UI", "Yu Gothic UI", "MS UI Gothic")}
    UI_FONTS = ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont",
                "TkIconFont", "TkTooltipFont")
    home_family = {n: tkfont.nametofont(n).cget("family") for n in UI_FONTS}

    def script_font(code):
        """Set the UI font for this language's script."""
        have = set(tkfont.families(root))
        pick = next((f for f in SCRIPT_FONTS.get(code, ()) if f in have),
                    None)
        for n in UI_FONTS:
            tkfont.nametofont(n).configure(family=pick or home_family[n])
        # Fonts copied from the default before the change keep the old
        # family; the few that carry words are refreshed with it.
        for face in state.get("fontcopies", ()):
            face.configure(family=pick or home_family["TkDefaultFont"])
        state.pop("decfonts", None)

    state = {"cwd": None, "busy": False, "cache": {}, "saveas": None,
             "sizes": {}, "bar": False, "seen_events": set(),
             "scanned": [], "scopes": [],
             # Where we have been, and where in that list we are. Back and
             # forward move the pointer; going somewhere new truncates
             # everything ahead of it, as a browser does.
             "history": [], "hist_at": -1, "sort": ("name", False),
             "addr": address_argument() or load_settings().get("address")
             or DEFAULT_ADDR,
             "colours": tds_wfm.scheme(load_settings().get("colours")),
             # Older settings files hold a bare "png_width"; png_size
             # takes either and snaps it to the nearest standard size.
             "pngsize": png_size(load_settings().get("png_size")
                                 or load_settings().get("png_width")),
             # How far an arrow key moves a point. None follows the grid.
             "nudge": nudge_value(load_settings().get("nudge")),
             # What the instrument itself draws each source in, read
             # once per connection, and {} where it has no colours.
             "icolours": {},
             # Waveforms loaded from the PC and held against a reference
             # until they are sent: {"REF2": Waveform}.
             "staged": {},
             "screen": None, "shotpng": None, "shotimg": None,
             "sformats": [], "errlog": None, "idn": ""}
    script_font(i18n.current())

    def fit_text(widget, text):
        """`text` broken into lines for this label, in Chinese or Japanese.

        See cjk_break. Every other language is left to Tk. The lines are
        measured in the label's own font and against its own wraplength,
        so Tk, handed lines that already fit, leaves them as they are.
        """
        if i18n.current() not in SCRIPT_FONTS or not isinstance(text, str):
            return text
        try:
            room = widget.winfo_pixels(widget.cget("wraplength") or 0)
        except (tk.TclError, ValueError):
            return text
        if room <= 0:
            return text
        face = (str(widget.cget("font"))
                or ttk.Style(widget).lookup(widget.winfo_class(), "font")
                or "TkDefaultFont")
        try:
            face = tkfont.nametofont(face)
        except tk.TclError:
            face = tkfont.Font(root=widget, font=face)
        return cjk_break(text, face.measure, room)

    def fit_mapped(evt):
        """Fit a label as it is shown - the dialogs' labels included,
        which are made in one place each and registered nowhere."""
        try:
            said = str(evt.widget.cget("text"))
        except tk.TclError:
            return
        fitted = fit_text(evt.widget, said)
        if fitted != said:
            evt.widget.config(text=fitted)

    for _cls in ("TLabel", "Label"):
        root.bind_class(_cls, "<Map>", fit_mapped, add="+")

    # ------------------------------------------------------ navigation row
    # Explorer's arrangement: back, forward and up together at the left,
    # then the address bar filling the rest of the row.
    # Which instrument, and the language, apply to the whole window, so
    # they sit above the tabs. Everything else belongs to one tab or the
    # other: a Delete button has no meaning while looking at a waveform.
    gbar = ttk.Frame(root)
    gbar.pack(fill="x", padx=6, pady=(6, 2))

    # Colours the platform theme will not take
    TAB_OFF, TAB_ON, EDGE = "#ebebeb", "#fbfbfb", "#7a7a7a"

    def raise_contrast():
        """More contrast on the tabs and the section borders.

        Windows draws a vista notebook tab itself, through the theming
        API, so `TNotebook.Tab` takes a background and ignores it - the
        tabs come out the same flat grey as the page whatever is asked
        for. Switching the whole program to a drawn theme would fix the
        tabs and change every button, entry and scrollbar with them.

        So only the two elements that have to change are borrowed: ttk
        will take an element out of another theme by name, and clam's
        tab and label-frame border are plain filled rectangles that do
        honour colours. Everything else stays exactly as Windows draws
        it.

        Wrapped in try: a theme without those elements, or a Tk that
        will not lend them, is a reason to look ordinary and not a
        reason to fail to start.
        """
        style = ttk.Style()
        try:
            for name, elem in (("Contrast.tab", "tab"),
                               ("Contrast.border", "Labelframe.border")):
                try:
                    style.element_create(name, "from", "clam", elem)
                except tk.TclError:
                    pass                    # already borrowed, or absent
            style.layout("TNotebook.Tab", [
                ("Contrast.tab", {"sticky": "nswe", "children": [
                    ("Notebook.padding", {
                        "side": "top", "sticky": "nswe", "children": [
                            ("Notebook.label", {"side": "top",
                                                "sticky": ""})]})]})])
            style.layout("TLabelframe",
                         [("Contrast.border", {"sticky": "nswe"})])
            style.configure("TNotebook", bordercolor=EDGE,
                            lightcolor=EDGE, darkcolor=EDGE)
            style.configure("TNotebook.Tab", background=TAB_OFF,
                            bordercolor=EDGE, lightcolor=TAB_OFF,
                            darkcolor=TAB_OFF, padding=(10, 4))
            style.map("TNotebook.Tab",
                      background=[("selected", TAB_ON)],
                      lightcolor=[("selected", TAB_ON)],
                      darkcolor=[("selected", TAB_ON)])
            style.configure("TLabelframe", bordercolor=EDGE,
                            lightcolor=EDGE, darkcolor=EDGE)
        except tk.TclError as exc:
            log_note("ui", "the tabs kept the platform's own look: %s" % exc)

    raise_contrast()
    # The instrument row, made here and packed with the toolbar below.
    cmb_inst = ttk.Combobox(gbar, width=28, state="readonly", values=())
    btn_scan = ttk.Button(gbar, text=_("Scan"), padding=(10, 2),
                          command=lambda: do_rescan())
    lbl_inst = ttk.Label(gbar, text=_("Instrument"))
    btn_lang = ttk.Button(gbar, width=3, command=lambda: do_language())

    tabs = ttk.Notebook(root)
    tabs.pack(fill="both", expand=True, padx=6, pady=(4, 0))
    vars(ui).update(locals())
    ui_files.build(ui)
    ent_path = ui.ent_path
    filetab = ui.filetab
    fill_list = ui.fill_list
    lst = ui.lst
    pump_copy = ui.pump_copy
    pump_deletes = ui.pump_deletes
    pump_download = ui.pump_download
    pump_downloads = ui.pump_downloads
    pump_format = ui.pump_format
    pump_rmdir = ui.pump_rmdir
    pump_split = ui.pump_split
    pump_survey = ui.pump_survey
    pump_tree = ui.pump_tree
    pump_uploads = ui.pump_uploads
    pump_volumes = ui.pump_volumes
    pump_written = ui.pump_written
    show_sort_arrow = ui.show_sort_arrow
    tree = ui.tree
    update_nav = ui.update_nav

    vars(ui).update(locals())
    ui_wfm.build(ui)
    btn_decclear = ui.btn_decclear
    btn_decrun = ui.btn_decrun
    btn_wdel = ui.btn_wdel
    btn_wget = ui.btn_wget
    btn_wload = ui.btn_wload
    btn_wsave = ui.btn_wsave
    btn_wscan = ui.btn_wscan
    btn_wsend = ui.btn_wsend
    chk_decascii = ui.chk_decascii
    flow_buttons = ui.flow_buttons
    flowing = ui.flowing
    forget_source = ui.forget_source
    plot_colours = ui.plot_colours
    pump_wfm_delete = ui.pump_wfm_delete
    pump_wfm_get = ui.pump_wfm_get
    pump_wfm_select = ui.pump_wfm_select
    pump_wfm_send = ui.pump_wfm_send
    pump_wfm_send_many = ui.pump_wfm_send_many
    pump_wfm_sources = ui.pump_wfm_sources
    show_sources = ui.show_sources
    show_wave = ui.show_wave
    show_waves = ui.show_waves
    shown_waves = ui.shown_waves
    trace_colour = ui.trace_colour
    wave_name = ui.wave_name
    wave_scales = ui.wave_scales
    wavetab = ui.wavetab
    wfm_buttons = ui.wfm_buttons
    wref = ui.wref
    wsrc = ui.wsrc

    vars(ui).update(locals())
    ui_msk.build(ui)
    btn_mclear = ui.btn_mclear
    btn_mdel = ui.btn_mdel
    btn_mdrop = ui.btn_mdrop
    btn_mgrab = ui.btn_mgrab
    btn_mload = ui.btn_mload
    btn_mmeasure = ui.btn_mmeasure
    btn_mnew = ui.btn_mnew
    btn_mrefresh = ui.btn_mrefresh
    btn_msave = ui.btn_msave
    btn_msend = ui.btn_msend
    btn_msetup = ui.btn_msetup
    btn_mtrace = ui.btn_mtrace
    btn_mview = ui.btn_mview
    chk_mcross = ui.chk_mcross
    chk_mfill = ui.chk_mfill
    chk_mgrat = ui.chk_mgrat
    chk_mhide = ui.chk_mhide
    chk_mnodots = ui.chk_mnodots
    chk_mshowgrid = ui.chk_mshowgrid
    chk_msnap = ui.chk_msnap
    do_msk_behind = ui.do_msk_behind
    do_msk_redo = ui.do_msk_redo
    do_msk_setup = ui.do_msk_setup
    do_msk_trace = ui.do_msk_trace
    do_msk_undo = ui.do_msk_undo
    edit_bindings = ui.edit_bindings
    edit_grid = ui.edit_grid
    edit_redraw = ui.edit_redraw
    edit_shapes = ui.edit_shapes
    edit_tools = ui.edit_tools
    ent_mgrid = ui.ent_mgrid
    masktab = ui.masktab
    mlive = ui.mlive
    mpc = ui.mpc
    msk_describe = ui.msk_describe
    msk_forget = ui.msk_forget
    msk_may_discard = ui.msk_may_discard
    msk_remember = ui.msk_remember
    msk_say_live = ui.msk_say_live
    msk_scan_scope = ui.msk_scan_scope
    msk_shot_behind = ui.msk_shot_behind
    msk_shot_image = ui.msk_shot_image
    pump_msk_behind = ui.pump_msk_behind
    pump_msk_clear = ui.pump_msk_clear
    pump_msk_count = ui.pump_msk_count
    pump_msk_eye = ui.pump_msk_eye
    pump_msk_live = ui.pump_msk_live
    pump_msk_read = ui.pump_msk_read
    pump_msk_send = ui.pump_msk_send
    pump_set_read = ui.pump_set_read
    pump_set_send = ui.pump_set_send
    reflow = ui.reflow
    verdict_stamp = ui.verdict_stamp

    vars(ui).update(locals())
    ui_lim.build(ui)
    btn_ldelfile = ui.btn_ldelfile
    btn_ldestclear = ui.btn_ldestclear
    btn_lgrab = ui.btn_lgrab
    btn_llearn = ui.btn_llearn
    btn_lload = ui.btn_lload
    btn_lnew = ui.btn_lnew
    btn_lrefresh = ui.btn_lrefresh
    btn_lsavefile = ui.btn_lsavefile
    btn_lsend = ui.btn_lsend
    btn_lsetup = ui.btn_lsetup
    btn_lstart = ui.btn_lstart
    btn_lstop = ui.btn_lstop
    btn_ltrace = ui.btn_ltrace
    btn_lview = ui.btn_lview
    cmb_lsource = ui.cmb_lsource
    ent_lgrid = ui.ent_lgrid
    lim_buttons = ui.lim_buttons
    lim_refs_note = ui.lim_refs_note
    lim_verdict = ui.lim_verdict
    limtab = ui.limtab
    pump_lim_build = ui.pump_lim_build
    pump_lim_clear = ui.pump_lim_clear
    pump_lim_picture = ui.pump_lim_picture
    pump_lim_run = ui.pump_lim_run
    pump_lim_send = ui.pump_lim_send
    pump_lim_state = ui.pump_lim_state
    pump_lim_stop = ui.pump_lim_stop
    pump_lim_survey = ui.pump_lim_survey
    say_limits = ui.say_limits

    vars(ui).update(locals())
    ui_scr.build(ui)
    btn_sget = ui.btn_sget
    btn_ssave = ui.btn_ssave
    btn_sscan = ui.btn_sscan
    pump_scr_get = ui.pump_scr_get
    pump_scr_options = ui.pump_scr_options
    scr_fmt = ui.scr_fmt
    scr_format_labels = ui.scr_format_labels
    scr_lay = ui.scr_lay
    scr_layout_labels = ui.scr_layout_labels
    scr_pal = ui.scr_pal
    scr_palette_labels = ui.scr_palette_labels
    scrtab = ui.scrtab
    show_shot = ui.show_shot

    vars(ui).update(locals())
    ui_err.build(ui)
    btn_eclr = ui.btn_eclr
    btn_eget = ui.btn_eget
    btn_esave = ui.btn_esave
    copyable = ui.copyable
    errtab = ui.errtab
    pump_err_available = ui.pump_err_available
    pump_err_clear = ui.pump_err_clear
    pump_err_entries = ui.pump_err_entries
    show_errors = ui.show_errors

    vars(ui).update(locals())
    ui_sys.build(ui)
    btn_sysdiag = ui.btn_sysdiag
    btn_sysfact = ui.btn_sysfact
    btn_sysfree = ui.btn_sysfree
    btn_syslock = ui.btn_syslock
    btn_sysopts = ui.btn_sysopts
    btn_sysports = ui.btn_sysports
    btn_sysread = ui.btn_sysread
    btn_sysspc = ui.btn_sysspc
    btn_syssync = ui.btn_syssync
    btn_systime = ui.btn_systime
    btn_syswipe = ui.btn_syswipe
    chk_sysclock = ui.chk_sysclock
    lbl_sysidn = ui.lbl_sysidn
    pump_sys_clock_read = ui.pump_sys_clock_read
    pump_sys_diag = ui.pump_sys_diag
    pump_sys_factory = ui.pump_sys_factory
    pump_sys_option = ui.pump_sys_option
    pump_sys_option_read = ui.pump_sys_option_read
    pump_sys_read = ui.pump_sys_read
    pump_sys_secure = ui.pump_sys_secure
    pump_sys_send = ui.pump_sys_send
    pump_sys_spc = ui.pump_sys_spc
    sys_rule = ui.sys_rule
    systab = ui.systab

    vars(ui).update(locals())
    ui_fw.build(ui)
    btn_fwback = ui.btn_fwback
    btn_fwbackup = ui.btn_fwbackup
    btn_fwfile = ui.btn_fwfile
    btn_fwfolder = ui.btn_fwfolder
    btn_fwprobe = ui.btn_fwprobe
    btn_fwstart = ui.btn_fwstart
    cbo_fwmodel = ui.cbo_fwmodel
    chk_fwall = ui.chk_fwall
    chk_fwcheck = ui.chk_fwcheck
    chk_fwslow = ui.chk_fwslow
    fw_buttons = ui.fw_buttons
    fw_model = ui.fw_model
    fwtab = ui.fwtab
    lbl_fwstate = ui.lbl_fwstate
    pump_fw_backup = ui.pump_fw_backup
    pump_fw_catalogue = ui.pump_fw_catalogue
    pump_fw_probe = ui.pump_fw_probe
    pump_fw_run = ui.pump_fw_run

    vars(ui).update(locals())
    ui_bak.build(ui)
    BAK_JOBS = ui.BAK_JOBS
    bak_buttons = ui.bak_buttons
    bak_note = ui.bak_note
    bak_relabel = ui.bak_relabel
    baktab = ui.baktab
    btn_bakcalget = ui.btn_bakcalget
    btn_bakcalput = ui.btn_bakcalput
    btn_bakcheck = ui.btn_bakcheck
    btn_bakmake = ui.btn_bakmake
    btn_baknv = ui.btn_baknv
    btn_baknvput = ui.btn_baknvput
    btn_bakput = ui.btn_bakput
    btn_bakscan = ui.btn_bakscan
    pump_bak_make = ui.pump_bak_make
    pump_bak_nvram = ui.pump_bak_nvram
    pump_bak_nvram_put = ui.pump_bak_nvram_put
    pump_bak_put = ui.pump_bak_put
    pump_bak_survey = ui.pump_bak_survey
    pump_cal_read = ui.pump_cal_read
    pump_cal_write = ui.pump_cal_write

    vars(ui).update(locals())
    ui_set.build(ui)
    btn_setdef = ui.btn_setdef
    btn_setdrop = ui.btn_setdrop
    btn_setsave = ui.btn_setsave
    set_nudge = ui.set_nudge
    set_preset = ui.set_preset
    set_wide = ui.set_wide
    settab = ui.settab

    # The order they are read in, which is not the order they are built
    # in. Building follows what depends on what - the limits tab needs
    # the mask editor's tools - and this follows what somebody reaches
    # for first, with the two that are about this program rather than
    # about the instrument at the end.
    for at, page in enumerate((filetab, scrtab, wavetab, limtab, masktab,
                               errtab, systab, baktab, fwtab, settab)):
        tabs.insert(at, page)

    # One status bar along the bottom: the message on the left and the
    # progress bar on the right, which is where the free-space readout
    # used to be. The bar is packed and unpacked rather than left in
    # place empty, so an idle window does not imply something is
    # happening.
    bar = ttk.Frame(root)
    # Inset to match the content panes above. Flush against the frame the
    # sunken edges get clipped by the window's rounded corners.
    #
    # `before=tabs` is what keeps it on screen. The notebook is packed
    # thousands of lines earlier with expand=True, and the packer works
    # in packing order: whatever is packed first claims the cavity, so a
    # bar added afterwards is squeezed to nothing as soon as a tab's
    # content asks for more height than the window has. Putting it ahead
    # of the notebook in the order reserves its height first and lets the
    # notebook expand into what is left.
    bar.pack(fill="x", side="bottom", padx=6, pady=(2, 6), before=tabs)
    status = ttk.Label(bar, text=_("Connecting..."), relief="sunken",
                       anchor="w")
    status.pack(fill="x", side="left", expand=True, padx=(0, 4))
    # The bar is packed and unpacked as work starts and stops, so its
    # height decides whether the whole status row jumps when it appears.
    # The theme's own progressbar is taller than a sunken label, which
    # made everything above it shift by a few pixels every time a
    # transfer began. Measured against the label and told to match.
    style = ttk.Style()
    prog = ttk.Progressbar(bar, length=220, maximum=1000,
                           style="Status.Horizontal.TProgressbar")

    def instrument_name(what):
        """`what`, with the instrument's model in front of it.

        So a folder of logs from three scopes can be told apart at a
        glance rather than by opening them. Anything a file name cannot
        hold is dropped rather than replaced, and an instrument that
        did not identify itself simply contributes nothing.
        """
        model = "".join(c for c in model_of(state.get("idn"))
                        if c.isalnum() or c in "-_").strip()
        return "%s %s" % (model, what) if model else what

    def say(msg):
        status.config(text=msg)

    def fit_progress():
        """Make the bar exactly as tall as the status label beside it.

        The theme picks its own thickness and it is taller than a sunken
        label, so the whole status row - and everything resting on it -
        jumped by a few pixels every time a transfer started. Measured
        rather than guessed, because the answer depends on the theme and
        on the font the user's Windows is set to.
        """
        try:
            want = status.winfo_height() or status.winfo_reqheight()
            if want <= 4 or state.get("bar_h") == want:
                return
            # Thickness is not the whole height - the theme adds its own
            # border on top, and how much varies between themes and
            # platforms. So it is set, measured, and corrected by the
            # difference, which settles it in one pass without this
            # code having to know anything about the theme.
            style.configure("Status.Horizontal.TProgressbar",
                            thickness=want)
            prog.pack(side="right", padx=6)
            root.update_idletasks()
            over = prog.winfo_height() - want
            if over > 0:
                style.configure("Status.Horizontal.TProgressbar",
                                thickness=max(4, want - over))
            prog.pack_forget()
            # And the row itself is pinned, so nothing that appears in
            # it later can move what is above it.
            bar.configure(height=bar.winfo_height())
            bar.pack_propagate(False)
            state["bar_h"] = want
        except Exception:
            pass

    def progress(frac=None):
        """Show the bar. `frac` 0..1 for a known share, None for unknown.

        None puts it in marquee mode, which is the honest thing for a
        single download: the instrument sends the file as one stream with
        no length in front of it, so there is no way to know how far
        through it we are until it ends.
        """
        if not state["bar"]:
            fit_progress()
            # No vertical padding: the height is set to match the label
            # exactly, and padding on top of that would put the row back
            # where it started.
            prog.pack(side="right", padx=6)
            state["bar"] = True
        if frac is None:
            if str(prog.cget("mode")) != "indeterminate":
                prog.config(mode="indeterminate")
                prog.start(15)
        else:
            if str(prog.cget("mode")) != "determinate":
                prog.stop()
                prog.config(mode="determinate")
            prog.config(value=max(0, min(1000, int(frac * 1000))))

    def progress_off():
        if state["bar"]:
            prog.stop()
            prog.pack_forget()
            state["bar"] = False


    def unavailable(*widgets):
        """Grey these for good: the instrument cannot do it.

        Kept in state["cannot"] rather than set on the widget, because
        busy(False) re-enables the whole button set when an operation
        finishes and would otherwise hand back a button that can only
        produce an apology.
        """
        cannot = list(state.get("cannot") or [])
        for widget in widgets:
            if widget not in cannot:
                cannot.append(widget)
        state["cannot"] = cannot
        busy(state.get("busy", False))

    def busy(on, working=None):
        """`working` is None for no bar, 'wait' for marquee, 'steps' for one
        that fills. Anything that clears busy also clears the bar, so it
        cannot be left spinning after the work has stopped."""
        # The jobs a connect queues behind itself hold the window between
        # them. wfm_sources and scr_options each release busy when they
        # are run on their own, and run as part of a connect that handed
        # the buttons back while the volumes and the listing were still
        # queued - see pump(), which lets go once the last of them is in.
        if not on and state.get("connecting"):
            return
        state["busy"] = on
        unavailable = state.get("cannot") or ()
        for b in buttons:
            # A command this firmware does not have stays greyed whatever
            # the busy state is. Offering a button that can only ever
            # produce an apology is worse than not offering it.
            keep = (b is btn_scan and state.get("scanning"))
            b.config(state="disabled" if (on or b in unavailable) and not keep
                     else "normal")
        update_nav()
        # Delete is not merely busy or not: it means nothing unless a
        # stored reference is selected, so it re-decides for itself
        # rather than being switched back on with the rest.
        if not on:
            wfm_buttons()
            lim_buttons()
            # Start measurement is in `buttons` and so was switched on by
            # every busy(False), whatever msk_buttons had decided - no
            # mask, no Option 2C (C5).
            ui.msk_buttons()
        # Write firmware and Back up... are not in `buttons` and decide for
        # themselves - but both read busy, so they have to be asked on the
        # way in too. Asked only on the way out, Write stayed live through
        # the minutes of its own job and a second press queued a second
        # erase behind the first.
        fw_buttons()
        bak_buttons()
        root.config(cursor="watch" if on else "")
        if not on:
            progress_off()
        elif working == "wait":
            progress(None)
        elif working == "steps":
            progress(0.0)

    def keep_file(path, data):
        """Write what came off the bus, asking for another place if that fails.

        Returns where it went, or None if the user gave up. A locked file
        or a read-only folder used to raise inside the pump, throw away
        minutes of reading and call it a bug in this program.
        """
        while path:
            try:
                with open(path, "wb") as fh:
                    fh.write(data)
                return path
            except OSError as exc:
                if not messagebox.askyesno(
                        _("Error"),
                        _("%(file)s could not be written:\n\n%(why)s\n\n"
                          "Save it somewhere else?")
                        % {"file": path, "why": exc.strerror or exc},
                        icon="warning"):
                    break
                path = filedialog.asksaveasfilename(
                    parent=root, initialdir=os.path.dirname(path),
                    initialfile=os.path.basename(path))
        say(_("Nothing was saved"))
        return None

    def fault(where, text):
        """Report a bug in this program without taking the window with it.

        Releases the busy state - a fault mid-operation would otherwise
        leave every button disabled, which is indistinguishable from a
        hang - writes the traceback next to the script so it can be read
        after the fact, and tells the user in as many words that the
        instrument is fine and it is this program that is at fault.
        """
        log_note(where, "FAULT\n" + text)
        # Under pythonw there is no console and sys.stderr is None, so this
        # is a guard and not politeness.
        if sys.stderr is not None:
            sys.stderr.write(text)
        try:
            busy(False)
            say(_("%(job)s failed - see %(file)s")
                % {"job": where, "file": os.path.basename(LOGFILE)})
            messagebox.showerror(
                _("Error"),
                _("%s failed because of a bug in this program.\n\n"
                "Nothing was sent to the instrument, and the window is "
                "still usable. The details were written to:\n%s\n\n%s")
                % (where, LOGFILE, text.strip().splitlines()[-1]))
        except Exception:
            pass

    # A crash in an ordinary callback - a button, a key, a tree click - is
    # printed to a console the user may not even have. Route it here too.
    root.report_callback_exception = lambda *_exc: fault(
        "Command", traceback.format_exc())


    # ----------------------------------------------------------- actions
    # do_download is defined below; these are bound after it exists.
    def inst_label(item):
        """One line for the dropdown: model first, address after.

        The model is what a person recognises; the address is what
        distinguishes two of the same model on one bus.
        """
        idn = (item.get("idn") or "").split(",")
        model = idn[1].strip() if len(idn) > 1 else ""
        return "%s  (%s)" % (model or "unidentified", item["addr"])

    def set_instrument_list(items):
        """Fill the dropdown with the scopes found, and remember the rest.

        Only TDS oscilloscopes go in the list - a signal generator on the
        same bus is not something this program can browse. Everything the
        scan saw is kept for the picker dialog, which does show the lot.
        """
        state["scanned"] = items
        scopes = [it for it in items if it["scope"]]
        state["scopes"] = scopes
        cmb_inst.config(values=[inst_label(it) for it in scopes]
                        + [_("Other address...")])
        here = [i for i, it in enumerate(scopes)
                if it["addr"] == state["addr"]]
        if here:
            cmb_inst.current(here[0])
        elif not scopes:
            cmb_inst.set("")

    def on_instrument_pick(_evt=None):
        """Selecting a different scope connects to it.

        The last entry is not a scope but a way in to the dialog, which is
        where an address can be typed by hand - the Explorer convention of
        putting "more..." at the end of a short list rather than spending
        a toolbar button on it.
        """
        i = cmb_inst.current()
        scopes = state.get("scopes") or []
        if i == len(scopes):
            do_instrument()
            return
        if i < 0 or i > len(scopes) or state["busy"]:
            return
        addr = scopes[i]["addr"]
        if addr == state["addr"]:
            return
        settings = load_settings()
        settings["address"] = addr
        save_settings(settings)
        reconnect(addr)

    def scanning(on):
        """The Scan button becomes Cancel while a scan is running.

        The scan is the one slow job that can be given up safely - it
        opens one address at a time and holds nothing between them - so
        it is the one that gets a way out. The button stays enabled
        while everything else is greyed, which is why it is taken out of
        the busy set for the duration.
        """
        state["scanning"] = on
        btn_scan.config(text=_("Cancel") if on else _("Scan"),
                        state="normal")

    def do_rescan():
        """Re-scan the bus and refresh the dropdown, staying connected.

        Pressed a second time while a scan is running, this cancels it.
        """
        if state.get("scanning"):
            w.cancelled.set()
            # Named for what actually happens. The scan looks at the
            # flag between addresses, so the one being asked now runs to
            # its own timeout first - up to two seconds. "Stopping the
            # scan" promised something instant that a blocking VISA call
            # cannot give.
            say(_("Stopping after this address ..."))
            return
        if state["busy"]:
            return
        busy(True, "steps")
        scanning(True)
        say(_("Scanning the bus ..."))
        state["scan_into"] = lambda payload: (
            set_instrument_list(payload["found"]),
            say(_("%(all)d instrument(s), %(scopes)d of them a TDS "
                  "oscilloscope")
                % {"all": len(payload["found"]),
                   "scopes": len([i for i in payload["found"]
                                  if i["scope"]])}),
            scan_connect())
        w.submit("scan", lambda k: k.scan(), needs_fs=False)

    def scan_target():
        """Which address a finished scan should connect to, or None.

        None when something is already connected - a scan is also how
        the dropdown is refreshed mid-session, and that must not throw
        away the connection it was run from. None when the scan found
        no oscilloscope, and none while the bus is busy.

        Otherwise the one it was looking for, if the scan found it, and
        failing that the first oscilloscope on the bus.
        """
        scopes = state.get("scopes") or []
        if state.get("cannot") is not None or not scopes or state["busy"]:
            return None
        return ([s for s in scopes if s["addr"] == state["addr"]]
                or scopes)[0]["addr"]

    def scan_connect():
        """Connect to what the scan found, if nothing is connected yet.

        Starting the program with the scope switched off, switching it
        on and pressing Scan is the ordinary way round at a bench, and
        it used to end with the scope listed in the dropdown and the
        program still not talking to it.

        The address is remembered, exactly as picking that entry from
        the dropdown by hand would have remembered it.
        """
        addr = scan_target()
        if not addr:
            return
        settings = load_settings()
        settings["address"] = addr
        save_settings(settings)
        reconnect(addr)

    cmb_inst.bind("<<ComboboxSelected>>", on_instrument_pick)

    def do_instrument(_evt=None, reason=None, quiet=False):
        """Choose which instrument to talk to: scan the bus, or type one in.

        Opened from the toolbar, and opened automatically when the program
        cannot reach the address it was expecting - which for anyone whose
        scope is not at the address this was developed against is the first
        thing that will happen to them.
        """
        if state["busy"] and not reason:
            return
        dlg, pad = popup(_("Select instrument"), resizable=(True, False))
        if reason:
            # Red is for something that went wrong. An instrument that is
            # not where it was last time has not gone wrong.
            ttk.Label(pad, text=reason, wraplength=560,
                      foreground=("" if quiet else "#a33")).pack(
                          fill="x", pady=(0, 8))

        ttk.Label(pad, text=_("Instruments found on the bus:")).pack(
            anchor="w")
        cols = ("idn",)
        found = ttk.Treeview(pad, columns=cols, show="tree headings",
                             selectmode="browse", height=7)
        found.heading("#0", text=_("Address"))
        found.heading("idn", text=_("Identification"))
        found.column("#0", width=190, stretch=False)
        found.column("idn", width=420)
        found.pack(fill="both", expand=True, pady=(2, 8))
        found.tag_configure("scope", foreground="#046")

        row = ttk.Frame(pad)
        row.pack(fill="x")
        ttk.Label(row, text=_("Address")).pack(side="left")
        addr_var = tk.StringVar(value=state["addr"] or "")
        ent = ttk.Entry(row, textvariable=addr_var)
        ent.pack(side="left", fill="x", expand=True, padx=6)
        hints(ent, "A VISA address, e.g. GPIB0::1::INSTR")
        remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(pad, text=_("Remember this address"),
                        variable=remember).pack(anchor="w", pady=(6, 0))
        # The status line is two labels side by side: the word that
        # blinks while a scan runs, and the sentence that does not.
        saying = ttk.Frame(pad)
        saying.pack(fill="x", pady=(6, 0))
        scanword = ttk.Label(saying, text="")
        scanword.pack(side="left")
        note = ttk.Label(saying, text="", wraplength=520)
        note.pack(side="left", fill="x", expand=True, padx=(4, 0))

        def on_pick(_e=None):
            sel = found.selection()
            if sel:
                addr_var.set(found.item(sel[0], "text"))

        found.bind("<<TreeviewSelect>>", on_pick)
        found.bind("<Double-1>", lambda e: (on_pick(), do_connect()))

        #: The blinking word and the rest of the sentence, kept apart so
        #: only the word blinks. Two labels rather than one, because
        #: retyping a whole sentence twice a second is how a status line
        #: becomes hard to read.
        blink = {"on": True, "due": None}

        def blink_scanning():
            """Flash the word while a scan runs, and clear it when done.

            Stopped by `alive()` going false as much as by the scan
            finishing: the dialog can be closed mid-scan, and an after()
            that outlives its label raises out of the event loop.
            """
            if not alive() or not state.get("scanning"):
                blink["due"] = None
                scanword.config(text="", foreground="")
                return
            # The word stays put and changes colour. Blanking the text
            # made the label shrink to nothing and the sentence beside
            # it slide left and back twice a second, which is harder to
            # read than no blink at all.
            blink["on"] = not blink["on"]
            scanword.config(text=_("Scanning."),
                            foreground="" if blink["on"] else "#b0b0b0")
            blink["due"] = dlg.after(500, blink_scanning)

        def do_scan():
            if state["busy"]:
                return
            found.delete(*found.get_children())
            note.config(text=_("Searching for connected devices."))
            busy(True, "steps")
            scanning(True)
            if blink["due"] is None:
                blink_scanning()
            state["scan_into"] = fill_found
            state["scan_failed"] = lambda text: (
                note.config(text="%s\n\n%s"
                            % (text, _("Type an address above if you know "
                                       "it."))) if alive() else None)
            w.submit("scan", lambda k: k.scan(), needs_fs=False)

        #: What the Scan button does, reachable from outside the
        #: dialog. The button carries a translated label and sits
        #: several frames down, so finding it by walking the tree
        #: means matching text that changes with the language.
        state["pickerscan"] = do_scan

        def alive():
            """Is the dialog still on screen?

            A scan takes seconds and the dialog can be closed while it
            runs - Cancel, Escape, or the window's own X. The reply then
            arrives for a Treeview that no longer exists, and Tk raises
            `invalid command name ".!toplevel2.!frame.!treeview"` from
            inside the pump. Asked before touching anything that belongs
            to the dialog.
            """
            try:
                return bool(dlg.winfo_exists())
            except Exception:
                return False

        def fill_found(payload):
            items = payload["found"]
            # The dropdown belongs to the main window and is worth
            # filling in whatever became of the dialog.
            set_instrument_list(items)
            if not alive():
                return
            found.delete(*found.get_children())
            for i, it in enumerate(items):
                found.insert("", "end", iid="R%d" % i, text=it["addr"],
                             values=(it["idn"] or it.get("note")
                                     or "no reply",),
                             tags=("scope",) if it["scope"] else ())
            scopes = [i for i, it in enumerate(items) if it["scope"]]
            if scopes:
                found.selection_set("R%d" % scopes[0])
                found.focus("R%d" % scopes[0])
                on_pick()
                note.config(
                    text=_("%d instrument(s) found, %d of them a TDS "
                           "oscilloscope.") % (len(items), len(scopes)))
                say(_("Found %d TDS oscilloscope(s)") % len(scopes))
            elif items:
                note.config(text=_(
                    "No TDS oscilloscope found. %d other instrument(s) "
                    "answered; you can still try one, or type an address.")
                    % len(items))
                say(_("No TDS oscilloscope found on the bus"))
            else:
                note.config(text=_(
                    "No TDS oscilloscope found - nothing on the bus "
                    "answered at all. Check the GPIB driver, the cable, and "
                    "that the instrument is switched on."))
                say(_("No TDS oscilloscope found on the bus"))

        def do_connect():
            # The picker stays usable while a job runs; reconnecting under
            # it wiped the state the job's result was about to land in.
            if state["busy"]:
                return
            addr = addr_var.get().strip()
            if not addr:
                note.config(text=_("Enter an address, or scan and pick "
                                   "one."))
                return
            settings = load_settings()
            if remember.get():
                settings["address"] = addr
                save_settings(settings)
            elif settings.pop("address", None) is not None:
                save_settings(settings)
            dlg.destroy()
            reconnect(addr)

        btns = ttk.Frame(pad)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text=_("Scan"), command=do_scan).pack(side="left")
        ttk.Button(btns, text=_("Cancel"),
                   command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text=_("Connect"),
                   command=do_connect).pack(side="right", padx=6)

        dlg.bind("<Return>", lambda e: do_connect())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        ent.focus_set()
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (root.winfo_rootx() + 60,
                                 root.winfo_rooty() + 60))
        state["dialog"] = dlg

        def gone(event):
            """Drop the callbacks when the dialog goes.

            Belt as well as braces: alive() stops a stale callback from
            touching a destroyed widget, and this stops it being called
            at all. <Destroy> fires for every child too, hence the test.
            """
            if event.widget is not dlg:
                return
            state.pop("pickerscan", None)
            if state.get("scanning"):
                # Dismissing the picker is a decision to stop looking.
                # The scan belongs to this dialog, so letting it run
                # on afterwards is what made Cancel look as though it
                # had set another one going.
                w.cancelled.set()
            if state.get("scan_into") is fill_found:
                # Replaced with something harmless rather than removed.
                # The pump treats a missing scan_failed as "nobody is
                # showing this, so put up an error box" - and a modal
                # box with nobody to click it, for a dialog the user has
                # already dismissed, is worse than the crash this is
                # here to prevent.
                state["scan_into"] = lambda payload: set_instrument_list(
                    payload.get("found") or [])
                state["scan_failed"] = lambda text: (
                    say(_("The scan was stopped: %s") % text),
                    log_note("scan", "failed after the picker closed: %s"
                             % text))

        dlg.bind("<Destroy>", gone)
        note.config(text=_("Press Scan to ask every address on the "
                           "bus to identify itself, or type an "
                           "address above."))

    def reconnect(addr):
        """Point everything at a different instrument and start again."""
        state.pop("connecting", None)
        state["addr"] = addr
        state["cwd"] = None
        state["cache"].clear()
        state["sizes"].clear()
        tree.delete(*tree.get_children())
        lst.delete(*lst.get_children())
        ent_path.delete(0, "end")
        state["history"], state["hist_at"] = [], -1
        # Everything the last instrument told us goes too. A trace from
        # one scope left on screen under another scope's name is worse
        # than an empty pane, and the screenshot and error log are no
        # different.
        show_wave(None)
        wsrc.delete(*wsrc.get_children())
        wref.delete(*wref.get_children())
        state["wall"] = []
        state["wlive"] = []
        state["wsources"] = []
        state["wticked"] = []
        # Including anything staged: REF2 on one instrument is not REF2
        # on another, and colours belong to the instrument that was
        # asked, not to whichever one is connected next.
        state["staged"] = {}
        state["icolours"] = {}
        state["fetched"] = {}
        show_shot(None)
        show_errors([])
        state["errlog"] = None
        # And what the last one could not do, what its System tab read,
        # which references held a template and whether its bootloader
        # answered: each decides something on its own tab, and each was
        # kept, so the next instrument was judged by the last one.
        state["cannot"] = None
        state["sysnow"] = None
        state.pop("lrefs", None)
        state["fwmonitor"] = None
        # And what the Limits, Masks and Backup tabs remember of the last
        # instrument: its running test, the mask sent to it, its live
        # counts and what its Backup survey found.
        for key in ("lrun", "msent", "mlivecounts", "bakhas", "bakreflist"):
            state.pop(key, None)
        lim_verdict()
        bak_relabel()
        # The System tab's identity line is only rewritten by a read, and
        # it went on naming the last instrument while the next was in use.
        lbl_sysidn.config(text="-")
        root.title("TDS Toolkit %s - %s"
                   % (__version__, _("connecting to %s") % addr))
        busy(True, "wait")
        say(_("Connecting to %s ...") % addr)
        w.submit("connect", lambda k, a=addr: k.connect(a), needs_fs=False)


    # --------------------------------------------------- shared helpers

    def pick_from(box, pairs, want=None):
        """Fill a combobox with labels, remembering the values."""
        box.config(values=[label for label, _v in pairs])
        keys = [v for _l, v in pairs]
        if want in keys:
            box.current(keys.index(want))
        elif pairs and not box.get():
            box.current(0)
        return keys

    def chosen(box, keys):
        at = box.current()
        return keys[at] if 0 <= at < len(keys) else None

    def colour_presets():
        """Every preset: the built-in ones, plus whatever has been saved.

        A saved preset of the same name as a built-in wins, so anyone who
        dislikes the supplied Instrument scheme can replace it outright
        rather than working around it.
        """
        out = dict(tds_wfm.BUILT_IN_PRESETS)
        saved = load_settings().get("colour_presets")
        if isinstance(saved, dict):
            for name, cols in saved.items():
                if isinstance(cols, dict):
                    out[name] = tds_wfm.scheme(cols)
        return out

    # ---------------------------------------------------------- language

    def retranslate():
        """Re-label everything already on screen in the current language.

        Only the lasting furniture is relabelled here - buttons, headings,
        menus. Status text and dialogs are translated as they are produced,
        so they follow the setting from the next message onwards without
        needing to be tracked.
        """
        for widget, source in labelled:
            widget.config(text=fit_text(widget, _(source)))
        for again in relabel:
            again()
        show_sort_arrow()
        btn_lang.config(text="")
        if state.get("globe"):
            btn_lang.config(image=state["globe"])
        payload = state["cache"].get(state["cwd"])
        if payload:
            fill_list(state["cwd"], payload)     # "File folder" column
        set_instrument_list(state.get("scanned") or state.get("scopes") or [])
        # A language change during a scan would otherwise re-label the
        # Cancel button back to Scan while the scan is still running.
        scanning(state.get("scanning", False))
        # A longer word needs a wider button, and a wider button may no
        # longer fit the row. The frame has not changed size, so nothing
        # raises <Configure> - the layout is asked for again by hand,
        # with the remembered split cleared so it is not skipped as
        # unchanged.
        state["wflow"] = None
        root.update_idletasks()
        flow_buttons()
        # Send counts what is waiting to be sent, so it is not a fixed
        # string and cannot be relabelled from the list alone.
        ui.refresh_sources()
        # The three boxes on the Screen tab are filled from lists of
        # translated labels, so they have to be built again - keeping
        # whatever was chosen, which is what the keywords are for.
        for box, keys, pairs in (
                (scr_fmt, "sfkeys", scr_format_labels()),
                (scr_lay, "slkeys", scr_layout_labels()),
                (scr_pal, "spkeys",
                 scr_palette_labels(state.get("spalette", True)))):
            if state.get(keys):
                state[keys] = pick_from(box, pairs,
                                        chosen(box, state[keys]))
        # And the two lines under the panes, which are sentences.
        show_waves(shown_waves(), keep_view=True)
        if state.get("screen") is not None:
            show_shot(state["screen"], state.get("ssecs", 0.0))
        show_errors(state.get("errlines") or [],
                    state.get("errcount"))

    def do_language():
        """Pick a language. The list is whatever is in the lang folder."""
        menu = tk.Menu(root, tearoff=0)
        here = i18n.current()
        for lang in i18n.available():
            menu.add_command(
                label=("•  " if lang["code"] == here else "     ")
                + "%s  (%s)" % (lang["native"], lang["name"]),
                command=lambda c=lang["code"]: set_language(c))
        if not i18n.available():
            menu.add_command(label="No language files found",
                             state="disabled")
        x = btn_lang.winfo_rootx()
        y = btn_lang.winfo_rooty() + btn_lang.winfo_height()
        menu.tk_popup(x, y)

    def set_language(code):
        i18n.use(code)
        script_font(code)
        settings = load_settings()
        settings["language"] = code
        save_settings(settings)
        retranslate()
        say("%s: %s" % (_("Language"),
                        i18n._languages.get(code, {}).get("native", code)))

    # The globe goes hard right, at the far end of the row - packed before
    # the left-hand widgets claim the space, which is how pack works.
    btn_lang.pack(side="right", padx=(8, 0))
    lbl_inst.pack(side="left", padx=(0, 4))
    cmb_inst.pack(side="left")
    btn_scan.pack(side="left", padx=4)
    buttons.append(btn_scan)
    labelled += [(btn_scan, "Scan"), (lbl_inst, "Instrument")]
    # The waveform tab's buttons belong to the same busy set, so a
    # transfer on one tab cannot be started on top of one on the other.
    buttons += [btn_wget, btn_wsave, btn_wload, btn_wsend, btn_wdel,
                btn_wscan]
    labelled += [(btn_wget, "Get waveform"), (btn_wsave, "Save waveform"),
                 (btn_wload, "Load waveform..."),
                 (btn_wsend, "Send to instrument..."),
                 (btn_wdel, "Delete waveform"), (btn_wscan, "Refresh")]
    # What each button is for, in a tooltip. Written where the whole set
    # can be seen at once rather than beside each button: they have to
    # read as one voice, and half of them are an arrow or a symbol with
    # no words on them at all.
    for widget, english in (
            (btn_scan, "Ask every address on the bus to identify itself"),
            (btn_lang, "Change language"),
            (btn_wget, "Capture every displayed source, from one "
                       "acquisition"),
            (btn_wsave, "Save the captured trace as data, a picture or an "
                        "instrument file"),
            (btn_wload, "Open a waveform file from this computer"),
            (btn_wsend, "Write the trace into one of the instrument's "
                        "reference memories"),
            (btn_wdel, "Empty the selected reference on the instrument"),
            (btn_wscan, "Ask the instrument which sources it has"),
            (btn_mnew, "Start an empty mask"),
            (btn_msave, "Save this mask on this computer, under a name "
                        "you choose"),
            (btn_msetup, "Read the instrument's settings and save them "
                         "beside this mask, the way Tektronix shipped a "
                         "setup with every mask"),
            (btn_mdel, "Delete the selected mask file"),
            # Not just a captured one: the button offers the Waveforms
            # tab's capture and then a file, which is most of what it
            # is for - a mask is often drawn against a record somebody
            # saved rather than one on the bench now.
            (btn_mtrace, "Draw a captured trace or a saved waveform "
                         "under the mask"),
            (btn_mgrab, "Take what the instrument is showing and draw it "
                        "behind the mask"),
            (btn_mdrop, "Take the captured screen or trace back off the "
                        "mask, and its verdict with it"),
            (btn_mmeasure, "Set the instrument up to judge this mask and "
                           "start counting hits - for an eye, the sweep, "
                           "the trigger and the display as well"),
            (btn_mview, "Save the mask editor's graticule as a picture"),
            (btn_mrefresh, "Read the library and the instrument's "
                           "segments again"),
            (btn_msend, "Send the open mask to the instrument's eight "
                        "segments"),
            (btn_mload, "Read the instrument's eight segments back into "
                        "the editor"),
            (btn_mclear, "Empty the instrument's eight mask segments"),
            # In the Masks tab's words wherever it has the same button,
            # with the mask read as the template or the envelope. One
            # line each: a tooltip that takes reading is one nobody
            # reads.
            (btn_lnew, "Start an empty template"),
            (btn_lsavefile, "Save this template on this computer, under "
                            "a name you choose"),
            (btn_lsetup, "Read the instrument's settings and save them "
                         "beside this template"),
            (btn_ldelfile, "Delete the selected template file"),
            (btn_llearn, "Have the instrument build a template from the "
                         "signal it is showing"),
            (btn_ltrace, "Draw a captured trace under the envelope"),
            (btn_lgrab, "Take what the instrument is showing and draw it "
                        "behind the envelope"),
            (btn_lsend, "Write the envelope drawn here into the "
                        "reference"),
            (btn_lload, "Read the template the selected reference holds "
                        "back into the editor"),
            (btn_ldestclear, "Empty the selected reference on the "
                             "instrument"),
            (btn_lstart, "Judge the signal against the template from now "
                         "on"),
            (btn_lstop, "Switch the test off and let the instrument run "
                        "freely again"),
            (btn_lview, "Save this graticule as a picture"),
            (btn_lrefresh, "Read the template and the signal again"),
            (btn_sget, "Capture the instrument's display"),
            (btn_ssave, "Save the screenshot as a file"),
            (btn_sscan, "Read the instrument's hardcopy settings again"),
            (btn_eget, "Read the instrument's error log"),
            (btn_esave, "Save the error log as a file"),
            (btn_eclr, "Erase the error log on the instrument")):
        hints(widget, english)
    # The mask tab's buttons. New mask, Save and Delete work on files on
    # this computer, so they are deliberately *not* in the busy set: a
    # transfer to an instrument is no reason not to be able to draw.
    # Refresh reads the instrument's library, so that one is.
    buttons += [btn_mrefresh, btn_mmeasure]
    labelled += [(btn_mnew, "New mask"), (btn_msave, "Save mask as..."),
                 (btn_msetup, "Save setup..."),
                 (btn_mdel, "Delete mask"), (btn_mtrace, "Load trace..."),
                 (btn_mgrab, "Capture screen"),
                 (btn_mdrop, "Clear screenshot"), (btn_mclear, "Clear"),
                 (btn_mmeasure, "Start measurement"),
                 (btn_mview, "Save image..."),
                 (btn_mrefresh, "Refresh")]
    # Written text rather than a label's own, so it has to be written
    # again in the new language like the headings below.
    relabel.append(msk_say_live)
    # The toolbars are laid out from the width of the words in them, so
    # new words mean a new layout.
    relabel.append(reflow)
    # A Treeview heading is not a widget and cannot be handed to says(),
    # so its text is put back by hand when the language changes.
    relabel.append(lambda: [box.heading(col, text=_(word))
                            for box, col, word in
                            ((mpc, "name", "Name"),
                             (mpc, "signal", "Signal"),
                             (mpc, "what", "Shape"),
                             (mlive, "seg", "Segment"),
                             (mlive, "what", "Shape"))])
    # The limits tab. Learn, Start and Refresh all talk to the instrument
    # so they belong in the busy set; Stop deliberately does not - it is
    # what somebody reaches for when they want the instrument back.
    # Clear is not in the busy set either, and for the same reason as
    # Stop: busy(False) re-enables everything in it, which would hand
    # back a Clear that lim_buttons had greyed because a test is
    # running. Its own guard and lim_buttons decide when it is live.
    # Send and Load are arrows now, and an arrow is the same glyph in
    # every language: they carry no label to put back, only a tooltip,
    # which is looked up when it is shown.
    buttons += [btn_llearn, btn_lsend, btn_lstart, btn_lrefresh]
    labelled += [(btn_lnew, "New template"),
                 (btn_lsavefile, "Save template as..."),
                 (btn_lsetup, "Save setup..."),
                 (btn_ldelfile, "Delete template"),
                 (btn_llearn, "Learn template"),
                 (btn_ltrace, "Load trace..."),
                 (btn_lgrab, "Capture screen"),
                 (btn_lstart, "Start test"),
                 (btn_lstop, "Stop test"),
                 (btn_lview, "Save image..."),
                 (btn_lrefresh, "Refresh")]
    # Written text again rather than a label's own.
    relabel.append(lim_verdict)
    relabel.append(say_limits)
    buttons += [btn_sget, btn_ssave, btn_sscan]
    labelled += [(btn_sget, "Take screenshot"),
                 (btn_ssave, "Save image..."), (btn_sscan, "Refresh")]
    buttons += [btn_eget, btn_esave, btn_eclr]
    labelled += [(btn_eget, "Download"), (btn_esave, "Save as..."),
                 (btn_eclr, "Clear errors")]
    # Write is not in `buttons`: it depends on an image being chosen as
    # well as on nothing being busy, so it decides for itself.
    buttons += [btn_fwprobe, btn_fwfolder, btn_fwfile, btn_fwback]
    labelled += [(btn_fwprobe, "Connect"), (btn_fwfolder, "Folder..."),
                 (btn_fwfile, "File..."),
                 (btn_fwback, "Save backups to..."),
                 (btn_fwstart, "Write firmware")]
    # Back up is not in `buttons`: it needs something ticked as well as
    # an idle instrument, so bak_buttons decides. The rest are ordinary
    # bus work. Verify reads a file on this computer and needs no
    # instrument at all.
    # Options is here rather than with the rest of the System tab
    # because it is one of the three greyed on firmware with no
    # constant librarian, and state["cannot"] is only read for buttons
    # in this set.
    buttons += [btn_sysopts]
    # The rest of the System tab is bus work like any other, and was the
    # only tab whose buttons stayed live while a job ran: a Secure erase
    # pressed during a 25-minute SPC queued behind it, and SPC's finish
    # then called the window idle while TEKSecure was still running.
    buttons += [btn_sysread, btn_syslock, btn_sysfree, btn_systime,
                btn_syssync, btn_sysports, btn_sysspc, btn_sysdiag,
                btn_syswipe, btn_sysfact]
    # btn_baknv is busy-gated but never in state["cannot"]: the monitor
    # it talks to is in ROM on every instrument in the range, including
    # the v2.16e generation that has no librarian and greys the two
    # calibration buttons beside it.
    buttons += [btn_bakscan, btn_bakput, btn_bakcalget, btn_bakcalput,
                btn_baknv, btn_baknvput]
    labelled += [(btn_bakscan, "Refresh"),
                 (btn_bakmake, "Back up..."), (btn_bakput, "Restore..."),
                 (btn_bakcheck, "Verify..."),
                 (btn_bakcalget, "Back up..."),
                 (btn_bakcalput, "Restore..."),
                 (btn_baknv, "Back up..."),
                 (btn_baknvput, "Restore...")]
    for widget, english in (
            # The two firmware buttons that sit side by side and look
            # alike. The rest of that tab explains itself in full
            # sentences and wants no tooltips.
            (btn_fwfolder, "Read a whole folder of firmware images"),
            (btn_fwfile, "Choose one firmware image from anywhere"),
            (btn_bakscan, "Ask the instrument what it has to back up"),
            (btn_bakmake, "Copy what is ticked into one backup file"),
            (btn_bakput, "Write what is ticked from a backup file back "
                         "to the instrument"),
            (btn_bakcheck, "Read a backup file and check every file in "
                           "it against its checksum"),
            (btn_bakcalget, "Read the calibration constants and save "
                            "them on this computer"),
            (btn_bakcalput, "Write saved calibration constants back to "
                            "the acquisition board"),
            (btn_baknv, "Read the whole NVRAM through the bootloader and "
                        "save it on this computer"),
            (btn_baknvput, "Write a saved NVRAM back through the "
                           "bootloader")):
        hints(widget, english)

    # Every remaining input and button gets a one-line tooltip too, so
    # nothing on the window is unexplained on hover. Looked up when
    # shown, so a language change needs nothing re-registered.
    for widget, english in (
            (cmb_inst, "Pick the instrument to work with, or scan for "
                       "one"),
            # Screenshot tab
            (scr_fmt, "The file format the screenshot is saved in"),
            (scr_lay, "Page orientation for the saved screenshot"),
            (scr_pal, "The palette the instrument renders the "
                      "screenshot in"),
            # Masks tab options
            (chk_mshowgrid, "Show or hide the drawing grid"),
            (chk_msnap, "Snap points to the grid as they are drawn or "
                        "moved"),
            (chk_mgrat, "Show or hide the graticule behind the mask"),
            (chk_mcross, "Show crosshairs at the pointer"),
            (chk_mfill, "Fill the shapes rather than outline them"),
            (chk_mhide, "Hide the mask to see the trace behind it"),
            (chk_mnodots, "Hide the draggable points on the mask"),
            (ent_mgrid, "Grid spacing, in divisions of the graticule"),
            # Limits tab
            (cmb_lsource, "The channel the template is built for and "
                          "judged against"),
            (ent_lgrid, "Grid spacing, in divisions of the graticule"),
            # Waveforms tab - protocol decode
            (btn_decrun, "Decode the mapped sources with the chosen "
                         "protocol"),
            (btn_decclear, "Clear the decode from the table and the plot"),
            (chk_decascii, "Show the decoded bytes as ASCII text where a "
                           "column allows it"),
            # System tab
            (btn_sysread, "Read the front-panel lock state from the "
                          "instrument"),
            (btn_syslock, "Lock the instrument's front panel"),
            (btn_sysfree, "Unlock the instrument's front panel"),
            (btn_systime, "Set the instrument's clock to the date and "
                          "time shown"),
            (btn_syssync, "Set the instrument's clock from this "
                          "computer's clock"),
            (chk_sysclock, "Show or hide the clock on the instrument's "
                           "screen"),
            (btn_sysports, "Apply the RS-232 and Centronics port "
                           "settings"),
            (btn_sysspc, "Run Signal Path Compensation - takes several "
                         "minutes and needs a warmed-up instrument"),
            (btn_sysdiag, "Run the selected self test and show the "
                          "result"),
            (btn_syswipe, "TEKSecure: erase every stored setup, waveform "
                          "and reading on the instrument"),
            (btn_sysfact, "Recall the instrument's factory default "
                          "setup"),
            (btn_sysopts, "View and change the instrument's installed "
                          "options"),
            # Firmware tab
            (btn_fwprobe, "Connect and read the instrument's firmware "
                          "version and flash"),
            (btn_fwback, "Choose the folder the backups are written to"),
            (btn_fwstart, "Back up, then erase the flash and write the "
                          "chosen firmware image"),
            (btn_fwbackup, "Read the instrument's firmware and NVRAM out to "
                           "backup files, without writing anything"),
            (cbo_fwmodel, "The instrument model this firmware image is "
                          "for"),
            (chk_fwall, "List every firmware image, not just those for "
                        "this model"),
            (chk_fwcheck, "Read each backup a second time and compare, "
                          "to catch a bad read"),
            (chk_fwslow, "Bypass the flash page buffer - slower, for a "
                         "first write on a model not tried before"),
            # Settings tab
            (set_preset, "Load a saved preset of colours and sizes"),
            (btn_setsave, "Save the current colours and sizes as a "
                          "named preset"),
            (btn_setdrop, "Delete the selected preset"),
            (btn_setdef, "Put the colours and sizes back to the "
                         "program's defaults"),
            (set_wide, "The pixel size of saved graticule images"),
            (set_nudge, "How far an arrow key moves a selected point")):
        hints(widget, english)


    # ------------------------------------ results, on the Tk thread only
    #: Jobs submitted without busy(True): read in passing and never
    #: owning the bus, so neither their success nor their failure may
    #: release it.
    UNBUSIED = ("lim_state", "set_send", "msk_live", "sys_clock_read")

    # What each finished job does to the window, by the worker's label
    # for it. pump() takes results off the queue and hands each to its
    # handler here; a failure goes to pump_failed whatever the job was.


    def pump_failed(label, payload):
        # Four jobs run without taking busy - they are asked
        # in passing, beside whatever else is on the bus -
        # so their failing must not hand back buttons that
        # another job still holds.
        if label not in UNBUSIED:
            busy(False)
        if label == "scan":
            scanning(False)
        if label == "split" and "hist_was" in state:
            # Back or Forward to a folder that would not list: the
            # history goes back to where it was, and the next folder
            # reached is recorded as usual (D8). Left, it pointed at a
            # folder never reached and the next navigation went
            # unrecorded.
            state["hist_at"] = state.pop("hist_was")
            state.pop("no_record", None)
        if label in ("lim_build", "lim_picture"):
            # The flags a Learn or a Load set for the
            # picture that follows, or the next unrelated
            # Refresh would take the band unasked.
            state.pop("llearn", None)
            state.pop("lload", None)
        raw, payload = payload, localise(payload)
        # A failure is the last line of the run it ends,
        # and the report is the only place it stays.
        if label in BAK_JOBS:
            bak_note(_("Failed - %s") % payload)
        if label == "connect":
            if "Another copy of TDS Toolkit" in (raw or ""):
                # The bus is held by another copy of this
                # program, not an absent instrument. Say so and
                # do not open the picker - there is nothing to
                # pick, and scanning the bus is the very thing
                # one program at a time exists to prevent.
                say(payload)
                root.title("TDS Toolkit %s - %s"
                           % (__version__, _("not connected")))
                return
            # Not an error. The scope being somewhere else, or
            # switched off, is the ordinary first experience of
            # anyone whose bus is not the author's, and raising
            # VISA's own wording at them - "VI_ERROR_RSRC_NFOUND
            # (-1073807343): Insufficient location information"
            # - explains nothing they can act on. Say plainly
            # that nothing answered, then go and look.
            say(_("Nothing answered at %s")
                % (state["addr"] or DEFAULT_ADDR))
            root.title("TDS Toolkit %s - %s"
                       % (__version__, _("not connected")))
            do_instrument(reason=(
                _("Nothing answered at %s.")
                % (state["addr"] or DEFAULT_ADDR)),
                quiet=True)
            return
        say(_("%(job)s failed - %(why)s")
            % {"job": label, "why": payload})
        if label == "fw_probe":
            # The tab has a line for exactly this, and it is
            # the line the user was reading when they pressed
            # Connect. A modal on top of it says the same
            # thing twice and has to be dismissed first.
            state["fwmonitor"] = None
            lbl_fwstate.config(text=str(payload),
                               foreground="#a00")
            fw_buttons()
            say(_("The bootloader did not answer"))
            return
        if label == "lim_picture":
            # A reference that holds nothing refuses to be
            # read, and that refusal is the answer to the
            # question. The list is corrected to say empty
            # and the sentence goes on the status line; a
            # modal box for the ordinary state of an unused
            # reference is a box for nothing.
            state["lhas"] = False
            lim_refs_note(state["ldest"].get(), 0)
            lim_buttons()
            say(str(payload))
            return
        if label == "scan" and state.get("scan_failed"):
            # The picker opens its own scan as it appears. If
            # that fails there is already a dialog on screen
            # saying so, and stacking a modal error box on top
            # of it tells the user the same thing twice.
            state.pop("scan_into", None)
            state.pop("scan_failed")(str(payload))
        else:
            messagebox.showerror(_("Error"), str(payload))

    def pump_connect(payload):
        state["addr"] = payload.get("addr") or state["addr"]
        state["idn"] = payload.get("idn") or ""
        # If this address is not in the dropdown yet - first
        # run, before any scan - put it there so the box is
        # never blank while plainly connected to something.
        if not any(s["addr"] == state["addr"]
                   for s in state.get("scopes") or []):
            set_instrument_list(
                (state.get("scanned") or [])
                + [{"addr": state["addr"], "idn": payload["idn"],
                    "note": "", "scope": True}])
        root.title("TDS Toolkit %s - %s%s"
                   % (__version__, payload["idn"],
                      "" if DND else
                      "   (drag and drop needs tkinterdnd2)"))
        # What this firmware can and cannot do, decided once
        # and reflected in the toolbar rather than discovered
        # halfway through a transfer.
        cannot = []
        if not payload.get("can_write", True):
            cannot.append(by_name["Upload"])
        if not payload.get("reader"):
            cannot.append(by_name["Download"])
        if payload.get("filesystem") is False:
            # No filesystem means no folders to make and
            # nothing to delete either, not just no transfers.
            cannot += [by_name["New folder"], by_name["Delete"]]
            # And it is the marker for the generation that
            # has no constant librarian and no service
            # password: v2.16e answers neither, so the
            # calibration constants and the factory options
            # can only time out there. The two are decided
            # by the same fact and are greyed together.
            cannot += [btn_bakcalget, btn_bakcalput,
                       btn_sysopts]
        state["cannot"] = cannot
        state["reader"] = payload.get("reader")
        # Mask testing is Option 2C. Without it the MASK
        # subsystem answers and draws nothing, so the Masks
        # tab sends a limit template instead.
        state["masks"] = bool(payload.get("masks"))
        log_context("connect options",
                    "%s; mask testing %s"
                    % (payload.get("options") or "?",
                       "yes" if state["masks"] else "no"))
        # Asked for before anything filesystem-shaped can cut
        # this short. An instrument with no filesystem is
        # precisely the one where the waveform tab is the only
        # thing this program can offer.
        w.submit("wfm_sources", lambda k: k.wfm_sources())
        # The screen comes from the hardcopy subsystem, which
        # every instrument in the range has, so this is asked
        # for on the same terms as the waveform sources.
        w.submit("scr_options", lambda k: k.scr_options())
        # And whether it keeps a service log. Every
        # instrument on the bench does, but the earliest
        # firmware in the collection has no ERRLOG at all,
        # and a Download button that can only apologise is
        # worse than one that is plainly greyed.
        w.submit("err_available", lambda k: k.err_available())
        # And what is in its mask segments. Asked here rather
        # than with the volumes, because the mask subsystem
        # has nothing to do with the disk: firmware with no
        # filesystem at all still has masks, and a scope with
        # no disk in the drive still has them too.
        msk_scan_scope()
        # What this connect still has queued, so busy is held until the
        # last of it is in. See busy() and pump().
        state["connecting"] = {"wfm_sources", "scr_options", "err_available"}
        if state.get("cannot") is not None:
            state["connecting"].add("msk_live")
        if payload.get("filesystem") is False:
            # The earliest firmware has no FILESYSTEM subsystem
            # at all. Saying so beats an empty window and a
            # string of errors from commands it never had.
            busy(False)
            # A seam, not dead state: the smoke checks
            # read it to know which of the file checks
            # this instrument can be held to.
            state["no_filesystem"] = True
            say(_("This instrument has no filesystem over GPIB"))
            # Through `after`, like the Limited instrument
            # box below it, and for a reason worth stating.
            # A modal box raised from inside the pump runs
            # Tk's event loop within itself, and the pump is
            # suspended for as long as it is up - so every
            # answer the worker sends back queues behind it,
            # including the waveform sources, the hardcopy
            # formats, the error log question and the mask
            # segments asked for a few lines above. On the
            # one instrument that raises this box, that is
            # every remaining thing the program can do.
            root.after(400, lambda: messagebox.showinfo(
                _("Nothing to browse"),
                _("This instrument's firmware has no "
                  "filesystem commands at all - no working "
                  "directory, no directory listing, no free "
                  "space.\n\nThere is nothing for this "
                  "program to show. Firmware v5.0e and later "
                  "added the file transfer commands; the A "
                  "and B series can browse and download but "
                  "not upload.")))
            return
        if cannot and not state.get("told_about"):
            state["told_about"] = True
            root.after(400, lambda: messagebox.showinfo(
                _("Limited instrument"),
                _("This instrument's firmware has no command for "
                  "transferring file contents over GPIB.\n\n"
                  "You can browse it, create folders and delete "
                  "files. Downloading and uploading are greyed "
                  "out because they cannot work here.")
                if len(cannot) > 1 else
                _("This instrument's firmware cannot receive "
                  "files over GPIB.\n\nYou can browse it, "
                  "download from it, create folders and delete "
                  "files. Only uploading is unavailable.")))
        say(_("Probing volumes ..."))
        w.submit("volumes", lambda k: k.volumes())
        state["connecting"].add("volumes")

    def pump_scan(payload):
        busy(False)
        scanning(False)
        state.pop("scan_failed", None)
        into = state.pop("scan_into", None)
        if into:
            into(payload)
        else:
            say(_("Found %d instrument(s)")
                % len(payload["found"]))
        if payload.get("cancelled"):
            say(_("Scan stopped after %(done)d of %(all)d "
                  "addresses")
                % {"done": payload.get("reached", 0),
                   "all": payload.get("total", 0)})

        # Now that the volumes are known, the Masks tab can
        # say where its library lives on this instrument. The
        # segments were already asked about at connect.


    def pump_progress(payload):
        say(payload["text"])
        progress(payload["frac"])
        # Every step of a backup or a restore is written to
        # the report as well, so the tab keeps an account of
        # what happened rather than only of what is
        # happening - the status line is gone the moment the
        # next line replaces it.
        #
        # The step, though, not every count inside it. The
        # readers format their running totals as
        # "<step> - <n of m>", so the part before the dash
        # is the step: one line per step rather than one per
        # chunk, which on a megabyte would be a thousand.
        if payload.get("job") in BAK_JOBS:
            step = payload["text"].split(" - ")[0]
            if step != state.get("bakstep"):
                state["bakstep"] = step
                bak_note("    " + step)

    def pump_event(payload):
        # An event this program did not ask for. Always in the
        # log and always in the status bar; the dialog only the
        # first time a given code appears, so a repeating fault
        # informs rather than buries.
        say(_("Instrument reported: %s") % payload["detail"])
        fresh = [c for c in payload["codes"]
                 if c not in state["seen_events"]]
        state["seen_events"].update(payload["codes"])
        if fresh:
            # Shown from its own turn of the event loop, not
            # from in here. The job that raised it is usually
            # still running, and a box opened inside the pump
            # held up its progress, its result and the busy
            # release until somebody found the box and closed
            # it - which is how a long listing stalled the
            # screenshot run.
            root.after(0, lambda said=_(
                "While doing '%s' the instrument reported:\n\n%s"
                "\n\nThis was not expected. It has been written "
                "to:\n%s\n\nIf anything looks wrong on the "
                "instrument, stop and say so before deleting "
                "anything else.")
                % (payload["where"], payload["detail"], LOGFILE):
                messagebox.showwarning(
                    _("The instrument reported something"),
                    said))


    pump_jobs = {
        "msk_behind": pump_msk_behind,
        "fw_probe": pump_fw_probe,
        "fw_catalogue": pump_fw_catalogue,
        "fw_run": pump_fw_run,
        "fw_backup": pump_fw_backup,
        "connect": pump_connect,
        "scan": pump_scan,
        "wfm_sources": pump_wfm_sources,
        "wfm_select": pump_wfm_select,
        "wfm_delete": pump_wfm_delete,
        "err_available": pump_err_available,
        "err_entries": pump_err_entries,
        "err_clear": pump_err_clear,
        "scr_options": pump_scr_options,
        "scr_get": pump_scr_get,
        "wfm_get": pump_wfm_get,
        "wfm_send": pump_wfm_send,
        "wfm_send_many": pump_wfm_send_many,
        "sys_read": pump_sys_read,
        "sys_option_read": pump_sys_option_read,
        "sys_clock_read": pump_sys_clock_read,
        "sys_send": pump_sys_send,
        "sys_spc": pump_sys_spc,
        "sys_diag": pump_sys_diag,
        "sys_secure": pump_sys_secure,
        "cal_read": pump_cal_read,
        "cal_write": pump_cal_write,
        "bak_survey": pump_bak_survey,
        "bak_nvram_put": pump_bak_nvram_put,
        "bak_nvram": pump_bak_nvram,
        "bak_make": pump_bak_make,
        "bak_put": pump_bak_put,
        "sys_factory": pump_sys_factory,
        "sys_option": pump_sys_option,
        "msk_live": pump_msk_live,
        "set_read": pump_set_read,
        "set_send": pump_set_send,
        "msk_eye": pump_msk_eye,
        "msk_count": pump_msk_count,
        "lim_run": pump_lim_run,
        "lim_send": pump_lim_send,
        "lim_clear": pump_lim_clear,
        "lim_stop": pump_lim_stop,
        "lim_state": pump_lim_state,
        "lim_survey": pump_lim_survey,
        "lim_picture": pump_lim_picture,
        "msk_send": pump_msk_send,
        "msk_clear": pump_msk_clear,
        "msk_read": pump_msk_read,
        "lim_build": pump_lim_build,
        "volumes": pump_volumes,
        "split": pump_split,
        "progress": pump_progress,
        "event": pump_event,
        "download": pump_download,
        "tree": pump_tree,
        "downloads": pump_downloads,
        "deletes": pump_deletes,
        "survey": pump_survey,
        "rmdir": pump_rmdir,
        "copy": pump_copy,
        "uploads": pump_uploads,
        "format": pump_format,
        "upload": lambda p, x="upload": pump_written(x, p),
        "delete": lambda p, x="delete": pump_written(x, p),
        "mkdir": lambda p, x="mkdir": pump_written(x, p),
    }

    def pump():
        if state.get("closing"):
            return                     # window gone; nothing to update
        try:
            while True:
                # Asked again every time round, not only on the way in.
                # One result can close the window - a check that
                # finishes, a handler that gives up - and the next
                # result in the same batch would then be handed to
                # widgets that no longer exist, which arrives as
                # "invalid command name .!button" out of busy().
                if state.get("closing"):
                    return
                label, ok, payload = w.out.get_nowait()
                # A result ends the running account. Only progress lines
                # belong to a job that is still going, so anything else
                # closes the one the report was following.
                if label not in ("progress", "event"):
                    state["bakstep"] = None
                # One of the jobs a connect queued behind itself. Taken off
                # the list before its handler runs, so the last of them is
                # the one whose busy(False) gets through. The volumes hand
                # on to the first listing, which releases busy itself; any
                # other last job, or a failure, releases it here.
                pending = state.get("connecting")
                last = False
                if pending is not None and label in pending:
                    pending.discard(label)
                    last = not pending
                    if last:
                        state.pop("connecting", None)
                if not ok:
                    pump_failed(label, payload)
                else:
                    handle = pump_jobs.get(label)
                    if handle:
                        handle(payload)
                if last and (not ok or label != "volumes"):
                    busy(False)
        except queue.Empty:
            pass
        except Exception:
            # Anything other than an empty queue used to escape this try and
            # take `root.after` with it, so the pump was never rescheduled.
            # The window kept repainting and the buttons stayed disabled, so
            # it looked frozen when it was really dead. Now a fault is
            # reported and the pump carries on.
            fault("Background update", traceback.format_exc())
        finally:
            # Unconditional: the pump must survive every single fault.
            root.after(120, pump)

    def shut_down():
        """Let go of every image before the interpreter goes.

        Tk images are owned by the interpreter; one collected after it has
        been torn down raises from __del__, where nothing can catch it and
        the traceback is merely printed.

        Asks first if the instrument is mid-operation. The worker is a
        daemon thread, so closing the window kills it wherever it happens
        to be - part way through a block transfer, between a delete and
        the write that was going to replace it. Nothing here can finish
        the operation on the way out, and waiting for it would hang the
        close on a transfer that runs for minutes, so the honest thing is
        to say what is happening and let the answer decide.
        """
        if state.get("busy") and not state.get("closing"):
            if not messagebox.askyesno(
                    _("Still working"),
                    _("The instrument is in the middle of something.\n\n"
                      "Closing now stops it wherever it has got to, which "
                      "can leave a part-written file on the instrument. "
                      "Close anyway?"),
                    icon="warning", default="no"):
                return
        # Nothing more may touch a widget after this. A result arriving
        # from the worker a moment later would otherwise be handed to a
        # destroyed combobox, and "invalid command name .!combobox" is a
        # traceback at exit for no reason at all.
        state["closing"] = True
        state["icons"] = []
        state.pop("globe", None)
        try:
            winicons.reset()
        except Exception:
            pass
        root.destroy()

    # Everything is defined now, so the tabs can reach names that came
    # after them - busy and say among them.
    vars(ui).update(locals())
    root.protocol("WM_DELETE_WINDOW", shut_down)

    try:
        state["globe"] = winicons.globe(tk)
        btn_lang.config(image=state["globe"])
    except Exception:
        btn_lang.config(text="A/文")     # readable anywhere, if drawing fails
    # The mask editor's five. Kept in state because Tk throws away an
    # image nothing holds a reference to, and the button goes blank.
    DRAWN = {"pen": "pen", "move": "move", "eraser": "eraser",
             "cut": "scissors", "union": "union",
             "intersect": "intersect", "subtract": "subtract",
             "fliph": "fliph", "flipv": "flipv",
             "undo": "undo", "redo": "redo",
             # the waveform tab's zoom column
             "timein": "timein", "timeout": "timeout",
             "ampin": "ampin", "ampout": "ampout", "whole": "whole"}
    try:
        state["micons"] = {k: winicons.tool(tk, v)
                           for k, v in DRAWN.items()}
        for held in ("mtoolbtn", "mboolbtn", "ltoolbtn", "lboolbtn",
                     "wzoombtn"):
            for key, btn in (state.get(held) or {}).items():
                btn.config(image=state["micons"][key])
        for key, btn in (state.get("undobtn") or {}).items():
            btn.config(image=state["micons"][key[1:]])
    except Exception:                        # words, if the drawing fails
        words = {"pen": "✎", "move": "✥", "eraser": "⌫", "cut": "✂",
                 "union": "∪", "intersect": "∩", "subtract": "∖",
                 "fliph": "↔", "flipv": "↕",
                 "timein": "↔", "timeout": "→←", "ampin": "↕",
                 "ampout": "↓↑", "whole": "▭"}
        for held in ("mtoolbtn", "mboolbtn", "ltoolbtn", "lboolbtn",
                     "wzoombtn"):
            for key, btn in (state.get(held) or {}).items():
                btn.config(text=words[key])
        for key, btn in (state.get("undobtn") or {}).items():
            btn.config(text="↶" if key.endswith("undo")
                       else "↷")
    retranslate()

    say(_("Connecting to %s ...") % state["addr"])
    busy(True, "wait")
    w.submit("connect", lambda k, a=state["addr"]: k.connect(a),
             needs_fs=False)
    root.after(120, pump)

    # Everything this function built is local to it, so anything that
    # wants to drive the window has to be handed the scope rather than
    # import it. HOOK is that hand-off and nothing here ever sets one.
    if HOOK:
        HOOK(dict(vars(ui), **locals()))

    root.mainloop()
    w.stop()
    return state.get("exit_code", 0)


# -------------------------------------------------------------- self-test

SI_PREFIXES = {"": 1.0, "k": 1e3, "M": 1e6, "G": 1e9, "m": 1e-3,
               "u": 1e-6, "n": 1e-9, "p": 1e-12}
WFID_SCALE = re.compile(r"([-+0-9.eE]+)\s*([kMGmunp]?)(Volts|s)/div")


def wfid_scales(wfid):
    """The per-division settings out of the instrument's own words.

    A TDS describes each waveform in WFID - "Ch1, DC coupling,
    100.0mVolts/div, 500.0us/div, 500 points, Sample mode" - which is a
    second, independent statement of the same two numbers the preamble
    gives as YMULT and XINCR. Worth having: it is what lets a test ask
    whether the plot is drawn to the instrument's scales without
    trusting the arithmetic that draws it.

    Returns {"volts": ..., "seconds": ...} with whatever could be read.
    """
    out = {}
    for number, prefix, unit in WFID_SCALE.findall(str(wfid or "")):
        try:
            value = float(number) * SI_PREFIXES.get(prefix, 1.0)
        except ValueError:
            continue
        out["volts" if unit == "Volts" else "seconds"] = value
    return out


def widget_words(widget, skip=()):
    """Every piece of text on screen, walked from a window down.

    Labels, buttons and checkbuttons carry a `text`; a notebook carries
    one for each tab; a combobox carries a list of them. All three are
    furniture that has to change when the language does, and all three
    have been missed at least once.
    """
    found = []
    if widget in skip:
        return found
    try:
        text = widget.cget("text")
    except Exception:
        text = None
    if text:
        found.append(str(text))
    try:
        found += [str(v) for v in widget.cget("values") if v]
    except Exception:
        pass
    try:
        for tab in widget.tabs():
            found.append(str(widget.tab(tab, "text")))
    except Exception:
        pass
    for child in widget.winfo_children():
        found += widget_words(child, skip)
    return found


#: What a saved picture can be, in the sizes monitors have always come
#: in. 4:3 throughout, because the instrument's own screen is, and the
#: first rung is the screen itself.
PNG_SIZES = ((640, 480), (800, 600), (1024, 768),
             (1280, 960), (1600, 1200), (1920, 1440))
PNG_DEFAULT_SIZE = (800, 600)

# How far one arrow key moves what is selected, in percent of the
# graticule. None follows the grid, which is what the editor did before
# there was a setting: a fifth of a square, so five presses cross one.
NUDGE_GRID = "A fifth of the grid"
NUDGE_STEPS = (0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 5.0)

# How many handles a learnt limit is thinned to, and the range the
# slider on the Limits tab offers. The envelope the instrument writes is
# 250 columns - five hundred handles - and no shape with five hundred
# handles can be dragged; but how far to thin it is a judgement about
# the signal in front of you, not a constant. Fifty was one, and on
# anything with more than a cycle or two of structure in it, too few.
LEARN_HANDLES = 120
LEARN_LEAST = 20
LEARN_MOST = 500

#: How wide the left pane starts on the three tabs that have one.
#:
#: Three lists of things to pick from, in three tabs a bench moves
#: between, and they were 184, 300 and 240 pixels wide - each one
#: whatever its own columns happened to add up to. Wide enough for the
#: widest of the three, which is the mask library's name, signal and
#: shape.
#:
#: The panes carrying it are given no weight, so the extra room a wider
#: window brings goes to the graticule rather than to a list of names.
#: The splitter still drags.
#:
#: 310 rather than 300: the mask library's three columns come to 285 and
#: its scrollbar to another 17, and at 300 the last few pixels of the
#: shape column were cut off.
LEFT_PANE = 310

#: The Backup tab's left column, which is not one of the three above:
#: it holds no list to line up with, and three paragraphs of
#: explanation. At LEFT_PANE they ran the NVRAM buttons off the bottom
#: of a 1280 x 800 window in French, Italian and Russian, and cut the
#: Russian Verify button in half.
BAK_PANE = 400


def nudge_named(step):
    """A nudge distance as it is written on the box.

    Two decimals for every one of them. Mixed, the list read 0.25 %
    beside 1 % and 5 %, which is three different kinds of number in one
    dropdown.
    """
    return NUDGE_GRID if not step else "%.2f %%" % step


def nudge_value(value):
    """One of NUDGE_STEPS, or None for the grid.

    Reads a settings file a person may well have edited by hand, so
    nothing raises and nothing off the list gets through.
    """
    if isinstance(value, str):
        value = value.replace("%", "").strip()
        if not value or value == NUDGE_GRID:
            return None
    try:
        want = float(value)
    except (TypeError, ValueError):
        return None
    if want <= 0:
        return None
    return min(NUDGE_STEPS, key=lambda n: abs(n - want))


def png_named(size):
    """A size as it is written on the box: 800 x 600."""
    return "%d x %d" % tuple(size)


def png_size(value):
    """One of PNG_SIZES, from whatever was asked for.

    Takes "800 x 600", (800, 600), [800, 600] or a bare width, and
    answers with the standard size nearest by width. Nothing raises and
    nothing off the list gets through: this reads a settings file a
    person may well have edited by hand, and older files hold a width
    on its own.
    """
    if isinstance(value, str):
        value = value.split("x")[0]
    elif isinstance(value, (tuple, list)) and value:
        value = value[0]
    try:
        wide = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return PNG_DEFAULT_SIZE
    return min(PNG_SIZES, key=lambda wh: abs(wh[0] - wide))


def typed(text):
    """A number as a person types it: 0.5 or, in most of Europe, 0,5.

    Five of the translations tell the reader to type 0,5 because that is
    how their language writes a half, and float() refuses it.
    """
    return float(str(text).strip().replace(",", "."))


def model_of(idn):
    """The instrument's model out of its *IDN? reply.

    "TEKTRONIX,TDS 784D,0,CF:91.1CT FV:v7.4e" is the maker, the model,
    the serial and the firmware. The model is the field worth putting in
    a file name; anything unexpected gives nothing rather than a guess.
    """
    parts = [p.strip() for p in str(idn or "").split(",")]
    return parts[1] if len(parts) > 1 and parts[1] else ""


def human_bytes(count):
    """A size a person can read: 1.4 MB, not 1,457,664 bytes.

    Steps of 1024 and named KB, MB, GB, which is what a file manager on
    the same machine shows and so the number a user can compare against.
    One decimal below ten and none above, so a floppy reads 1.4 MB and a
    hard disk 105 MB rather than either being given false precision.
    """
    try:
        count = max(0, int(count or 0))
    except (TypeError, ValueError, OverflowError):
        return _("%d bytes") % 0
    if count < 1024:
        return _("%d bytes") % count
    units = (_("KB"), _("MB"), _("GB"), _("TB"))
    size = count / 1024.0
    at = 0
    # Rounded before the step is chosen, not after. A gigabyte less one
    # byte is 1023.9999 MB, which rounds to "1024 MB" - a number that is
    # arithmetically true and reads like a mistake.
    while at < len(units) - 1 and round(size, 1) >= 1024.0:
        size /= 1024.0
        at += 1
    shown = "%.1f" % size if size < 10 else "%d" % int(round(size))
    return _("%(size)s %(unit)s") % {"size": shown, "unit": units[at]}


def stamped(base, ext):
    """A filename with the date and time in it: base-YYYYMMDD-HHMMSS.ext.

    ISO order, so a folder of them sorts into the order they were taken
    rather than into alphabetical nonsense, and no characters Windows
    objects to in a filename.
    """
    return "%s-%s%s" % (base, time.strftime("%Y%m%d-%H%M%S"), ext)


def translatable_strings():
    """What this program says, read from its own source.

    Returns (spoken, literals): the strings written out at a call to
    _(), and every string literal in the file. The second is wider
    because a good deal of translated text never appears as an argument
    to _() - a file type is looked up by extension and translated
    afterwards - and an entry for one of those is not an orphan.

    says() and hints() count as speaking, because they are: both hand
    their English to _() when the label is written or the tooltip is
    shown. Reading only _() calls, the check passed with six strings on
    the Limits tab that no catalogue had an entry for - so they were in
    English in all nine languages, which is exactly what this is for.

    Returns None when the source is not there to read, inside the
    packaged executable for instance, so the caller can tell "nothing to
    translate" from "could not look".
    """
    here = os.path.dirname(os.path.abspath(__file__))
    spoken, literals = set(), set()
    read_any = False
    # The modules beside this one as well as this one: the names of the
    # hardcopy formats are defined in tds_scr.py and translated here, so
    # a search of this file alone reports every one of them as an entry
    # for a string the program no longer has.
    for name in ["tdstoolkit.py", "winicons.py", "i18n.py"] + sorted(
            n for n in os.listdir(here)
            if n.startswith(("ui_", "tds_", "worker")) and n.endswith(".py")):
        try:
            with open(os.path.join(here, name), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except Exception:
            continue
        read_any = True
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
            # (widget, "English") - the shape every entry in the hints
            # table and the `labelled` list has. Both are handed to _()
            # a moment later, so they are spoken as surely as a direct
            # call is, and a table is where it is easiest to add a
            # string and forget the catalogues.
            # Not in the modules below the window, where a pair like
            # (bit, "NAK") is data and speaks only through say().
            if (not name.startswith(("tds_", "worker"))
                    and isinstance(node, ast.Tuple) and len(node.elts) == 2
                    and isinstance(node.elts[0], (ast.Name, ast.Attribute))
                    and isinstance(node.elts[1], ast.Constant)
                    and isinstance(node.elts[1].value, str)
                    and node.elts[1].value):
                spoken.add(node.elts[1].value)
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # self.says(...) in a tab's class as well as says(...).
            called = (func.id if isinstance(func, ast.Name) else
                      func.attr if isinstance(func, ast.Attribute)
                      else None)
            # _("..."), N_("...") and a module's say("...") say their
            # first argument; says(w, "...") and hints(w, "...") their
            # second, and the zoom buttons' _zoombtn(key, cmd, "...") its
            # third. Read as an attribute too: i18n.say(...).
            at = {"_": 0, "N_": 0, "say": 0, "says": 1, "hints": 1,
                  "_zoombtn": 2}.get(called)
            if (at is not None and len(node.args) > at
                    and isinstance(node.args[at], ast.Constant)
                    and isinstance(node.args[at].value, str)):
                spoken.add(node.args[at].value)
    if not read_any:
        return None
    return spoken, literals


def check_translations():
    """Report faults in the language files, without opening a window.

    Documented in i18n.py and now implemented. Returns 0 when every file
    is sound, 1 otherwise, so it can be a build step.
    """
    i18n.discover(APPDIR)
    langs = i18n.available()
    if not langs:
        print("no language files found")
        return 1
    read = translatable_strings()
    if read is None:
        print("note: running from a bundle, so only the placeholders in "
              "each file can be checked, not which strings are missing")
        faults = i18n.audit()
    else:
        faults = i18n.audit(read[0], read[1])
    for lang in langs:
        problems = faults.get(lang["code"])
        if not problems:
            print("%-9s %-22s ok" % (lang["code"], lang["native"]))
            continue
        print("%-9s %-22s %d problem(s)"
              % (lang["code"], lang["native"], len(problems)))
        for line in problems:
            print("    %s" % line)
    return 1 if faults else 0


from worker import Worker  # noqa: E402 - see the note where it was


def main():
    """The entry point for both tdstoolkit.py and the .pyw launcher."""
    # A Windows console is code page 1252 unless told otherwise, and
    # cannot encode Русский or 日本語 - so printing a language's own name
    # in its own script raised UnicodeEncodeError and took the program
    # with it. Anything that will not fit the console is replaced rather
    # than raised. Under pythonw there are no streams at all, hence the
    # guard.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    if "--version" in sys.argv or "-V" in sys.argv:
        print("TDS Toolkit %s" % __version__)
        return 0
    if "--check-translations" in sys.argv:
        return check_translations()
    try:
        return run_gui() or 0
    except Exception:
        # A crash before the window exists cannot use the in-window fault
        # handler, and under pythonw there is no console to print to, so
        # this goes to the log and then to a message box - otherwise the
        # program would appear to do nothing at all when launched.
        detail = traceback.format_exc()
        log_note("startup", detail.replace("\n", " | "))
        if sys.stderr is not None:
            sys.stderr.write(detail)
        try:
            import tkinter
            from tkinter import messagebox
            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror(
                _("Error"),
                _("%s\n\nThe details were written to:\n%s")
                % (detail.strip().splitlines()[-1], LOGFILE))
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
