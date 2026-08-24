"""
tdstoolkit.py - a file explorer for the instrument's disk, over GPIB.

tds_fs.py already is the filesystem: dir, read, write, mkdir, delete, cwd,
freespace, overwrite, and the event-queue drain. This is a front end over it.

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
import base64
import copy
import json
import os
import queue
import re
import shutil
import struct
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tds_fs import TdsFs, DEFAULT_ADDR
import tds_err
import tds_msk
import tds_scr
import tds_set
import tds_wfm
import winicons
import i18n
from i18n import gettext as _

__version__ = "1.0.0"
__author__ = "Jared Cabot"
__email__ = "jetstreamtechnology@protonmail.com"
__licence__ = "MIT"

ATTEMPTS = 4

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


def address_argument(argv=None):
    """--address GPIB0::3::INSTR, or --address=GPIB0::3::INSTR.

    Overrides the remembered address for one run. Worth having on its own
    - two instruments on one bus and you want the other one - and it is
    useful on a bench with two instruments on one bus when you want the
    other one.
    """
    args = list(sys.argv if argv is None else argv)
    for i, a in enumerate(args):
        if a.startswith("--address="):
            return a.split("=", 1)[1].strip()
        if a == "--address" and i + 1 < len(args):
            return args[i + 1].strip()
    return None


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
    return {" ": "space", "\t": "tab", ".": "extra dot", "/": "slash",
            "\\": "backslash", ":": "colon", "*": "asterisk",
            "?": "question mark", '"': "double quote", "<": "less than",
            ">": "greater than", "|": "vertical bar", ",": "comma",
            ";": "semicolon", "+": "plus", "=": "equals",
            "[": "left bracket", "]": "right bracket"}.get(
                ch, "'%s'" % ch if ch.isprintable() else
                "character code %d" % ord(ch))


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
        problems.append("The name starts or ends with a space.")

    stem, dot, ext = name.partition(".")
    if name.count(".") > 1:
        problems.append("A name may contain only one dot.")
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
        problems.append("These characters are not allowed: %s"
                        % ", ".join(bad))

    if len(stem) > 8:
        problems.append("The part before the dot is %d characters long; "
                        "8 is the most that will fit." % len(stem))
    if not stem:
        problems.append("There is nothing before the dot.")
    if dot and len(ext) > 3:
        problems.append("The part after the dot is %d characters long; "
                        "3 is the most that will fit." % len(ext))
    if dot and not ext:
        problems.append("There is a dot with nothing after it.")

    if not problems:
        return None
    return ("'%s' cannot be used as a name on the instrument.\n\n"
            "%s\n\n"
            "Names follow the DOS 8.3 rule: up to 8 characters, then "
            "optionally a dot and up to 3 more. Letters, digits and "
            "%s may be used, and the name is stored in capitals."
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

# Event 250 "Mass storage error - osError" during a recursive delete is the
# instrument's own doing, not ours. Measured on a freshly imaged card: with
# the bus left completely silent for five seconds after RMDIR - no *OPC?,
# no drain, nothing that could collide - it still appeared in 4 of 12
# trials, and the folder was correctly removed in all 12. It is reported
# and recorded, but it is not treated as a failure, because the delete is
# verified by re-listing rather than by trusting the event queue.
DELETE_OPS = ("rmdir", "delete", "deletes")
MASS_STORAGE = 250


_LOGGED_BANNER = []

# Facts about this session, held rather than written. See log_context.
_LOGGED_CONTEXT = {}

# Half a megabyte, then the log is moved aside and a new one started.
# One generation is kept, which is what a user is asked to send: the
# session that went wrong, and the one before it. Without this the file
# only ever grows, and the first thing a long-running install does with
# a fault report is attach four megabytes of successful startups.
LOGMAX = 512 * 1024


def log_rotate(path=None):
    """Move the log aside if it has grown past LOGMAX. Never raises.

    The path is an argument so this can be tried on a file of its own
    rather than on the log the run is writing to: a check that rotates
    the real log to prove rotation works has thrown away the evidence
    of the run that was checking.
    """
    path = path or LOGFILE
    try:
        if os.path.getsize(path) < LOGMAX:
            return False
    except OSError:
        return False
    try:
        old = path + ".old"
        if os.path.exists(old):
            os.remove(old)
        os.rename(path, old)
        return True
    except OSError:
        return False


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
    if not _LOGGED_BANNER:
        log_rotate()
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOGFILE, "a", encoding="utf-8",
                  errors="replace") as fh:
            if not _LOGGED_BANNER:
                _LOGGED_BANNER.append(True)
                fh.write("\n=== TDS Toolkit %s (%s) started %s ===\n"
                         % (__version__,
                            "exe" if getattr(sys, "frozen", False)
                            else "python %s" % sys.version.split()[0],
                            now))
            # Drained, not copied: whatever is still held goes out in
            # front of the fault, and a fact that changes after this
            # goes out in front of the next one.
            for key in list(_LOGGED_CONTEXT):
                fh.write("%s  %-22s %s\n"
                         % (now, key, _LOGGED_CONTEXT.pop(key)))
            fh.write("%s  %-22s %s\n" % (now, where, text))
    except Exception:
        pass


class Worker(object):
    """Serialises every instrument operation onto one thread.

    COUNT_FOR is how long the instrument is left counting against a
    mask before the tally is read, where reading the trace would
    otherwise stop it. A second is a few hundred acquisitions on a
    784D; in DPO no wait is needed, since the hardcopy itself takes
    several.

    Jobs are (label, callable). Results come back as (label, ok, payload) on
    an output queue the UI polls; nothing is called back on this thread.
    """

    COUNT_FOR = 1.0

    def __init__(self):
        self.jobs = queue.Queue()
        self.out = queue.Queue()
        self.fs = None
        self.addr = None
        self.context = "start"
        # Set to the command name once an instrument has been found not to
        # have it, so nothing tries the same transfer three more times.
        self.no_transfers = None
        # One correction per connection: if the capability table is wrong
        # about an instrument, ask it once and carry on. Asking again on
        # every subsequent failure would just be the probe by other means.
        self.re_probed = False
        self._stop = threading.Event()
        # Set by the UI to ask a long job to give up. Only the scan reads
        # it, because the scan is the only job that is both slow and
        # safely interruptible - it opens and closes one address at a
        # time and owns nothing in between. Stopping a transfer part-way
        # is a different matter entirely and is not offered.
        self.cancelled = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, label, fn, needs_fs=True):
        """Queue a job. `needs_fs` False for work that runs with no
        instrument open - connecting and scanning, which are how you get
        one. Stated by the caller rather than inferred from the label,
        because a label is a display name and should not carry meaning."""
        self.jobs.put((label, fn, needs_fs))

    def _run(self):
        while not self._stop.is_set():
            try:
                label, fn, needs_fs = self.jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            self.context = label
            try:
                if needs_fs and self.fs is None:
                    raise RuntimeError("not connected")
                self.out.put((label, True, fn(self)))
            except Exception as exc:
                # The class name goes in the log, where it is a clue, and
                # not in the dialog, where "NotReadable:" in front of a
                # written sentence is noise. Some exceptions say nothing
                # at all, and then the name is all there is.
                log_note(label, "FAILED %s: %s" % (type(exc).__name__, exc))
                # And where it came from, for anything that is not one of
                # the instrument's own refusals. A one-line class name
                # says a job failed; it does not say which call failed,
                # and a fault nobody can reproduce is only diagnosable
                # from the log somebody sends afterwards.
                if not isinstance(exc, (tds_wfm.NotReadable, RuntimeError,
                                        IOError, ValueError)):
                    log_note(label, "  " + traceback.format_exc()
                             .strip().replace("\n", "\n  "))
                self.out.put((label, False,
                              str(exc) or type(exc).__name__))

    def stop(self):
        self._stop.set()

    def _routine(self, code):
        """Is this event one we already understand in this context?

        Context matters. A mass storage error while recursively deleting a
        folder is measured, expected and harmless. The same code while
        reading a file would mean something quite different, and should
        still get the user's attention.
        """
        if code in EXPECTED_EVENTS:
            return True
        here = self.context.split(" ")[0]
        if code == MASS_STORAGE and here in DELETE_OPS:
            return True
        return code in (BAD_FILENAME, NO_MEDIA) and here in PROBE_OPS

    def _watch_events(self):
        """Record every event the instrument raises, whoever drained it.

        Written as a wrapper round the one method that empties the event
        queue, rather than as a call added to each operation, because the
        drains are scattered through both this file and tds_fs and the
        whole point is that none of them can quietly discard a code. An
        error the user reports hours later is only diagnosable if it was
        written down when it happened.
        """
        raw = self.fs.errors

        def watched():
            codes = raw()
            if not codes:
                return codes
            # 256 "file name not found" is generated by design, dozens at a
            # time - probing for volumes, classifying each directory entry.
            # Logging those would bury the one line that matters under a
            # hundred that never do, so only the noteworthy are written
            # down. Anything the context does not account for is noteworthy,
            # including 250, which is tolerated but always recorded.
            notable = [c for c in codes
                       if c == MASS_STORAGE or not self._routine(c)]
            msgs = getattr(self.fs, "last_messages", []) or []
            detail = "; ".join("%d %s" % (c, t) for c, t in msgs) or str(codes)
            if notable:
                log_note(self.context, detail)
            odd = [c for c in codes if not self._routine(c)]
            if odd:
                self.out.put(("event", True, {"codes": odd, "detail": detail,
                                              "where": self.context}))
            return codes

        self.fs.errors = watched
        # The unwrapped one is kept, for the one drain that must
        # not report what it finds: whatever is in the queue when
        # a session opens got there before it. See connect.
        self._quiet_drain = raw

    # -- operations, all called on the worker thread -----------------------

    def connect(self, addr=None):
        # Any previous session is closed first. Leaving it open would hold
        # the old instrument's VISA lock, which is exactly what stops a
        # second attempt from working after a failed one.
        if self.fs is not None:
            self.fs.close()
            self.fs = None
        self.fs = TdsFs(**({"addr": addr} if addr else {}))
        # Ask who is there before anything else, on a short leash. An
        # address with nothing on it is otherwise discovered only when
        # the first real query gives up, three quarters of a minute
        # later, and the program looks hung when all that has happened
        # is that the scope is switched off.
        #
        # If it does not answer, everything goes back to disconnected.
        # Left alone, self.fs stayed set while wfm, scr and err still
        # wrapped the session closed above, so the "not connected" guard
        # let jobs through to a handle VISA had already invalidated.
        try:
            self.fs.hello()
        except Exception:
            self.fs.close()
            self.fs = self.wfm = self.scr = self.err = None
            raise
        # One VISA session serves both subsystems; the waveform side
        # borrows the filesystem side's header-stripping rule so there is
        # only one place that knows about HEADER ON.
        self.wfm = tds_wfm.TdsWfm(self.fs.inst, TdsFs.payload)
        self.scr = tds_scr.TdsScr(self.fs.inst, TdsFs.payload)
        self.err = tds_err.TdsErr(self.fs.inst, TdsFs.payload)
        self.addr = addr or getattr(self.fs, "addr", None) or "default"
        self._watch_events()
        self.fs.clear()
        # Replies with no command header in front of them. A 784D is
        # already set that way; a 640A is not, and every reply arrives as
        # ":FILESYSTEM:FREESPACE 0" instead of "0".
        self.fs.headers("OFF")
        self.no_transfers = None
        self.re_probed = False
        # Whatever was in the event queue happened before this program
        # opened the session - very likely another program's doing, or
        # our own from a previous run - and reporting it as though this
        # session caused it is misleading. Drained once, here, so
        # everything after this really is ours.
        #
        # Drained *quietly*, which is the whole point and was not
        # what happened: the watcher raises anything it does not
        # recognise as a modal box saying it was not expected, and
        # a 2241 left in the queue by a bench session an hour
        # earlier greeted the next connect with a warning about
        # something the program had not done. Written to the log,
        # where it is a clue, and nowhere else.
        try:
            stale = self._quiet_drain()
            if stale:
                log_note("connect", "cleared %d event(s) left over from "
                         "before this session: %s"
                         % (len(stale), getattr(self.fs, "last_messages",
                                                stale)))
        except Exception:
            pass
        self.fs.set_overwrite("ON")
        self.fs.set_delwarn("OFF")
        # Which transfer commands this firmware has. Looked up if this
        # instrument is one we already know, asked if it is not - either
        # way settled here, rather than discovered by a user watching a
        # progress bar that is never going to finish.
        idn = self.fs.idn()
        entry = known_instrument(idn)
        can = (self.fs.apply_known(entry) if entry
               else self.fs.probe_transfers())
        log_context("connect", "%s: reader=%s write=%s (%s)"
                    % (idn, can["reader"], can["can_write"], can["source"]))
        # Mask testing is Option 2C - "Mask Testing (Option 2C Only)" in
        # the user manual. A 784D here reports 2C:comm and a 784C does
        # not, and without it the MASK subsystem answers queries and
        # draws nothing. Asked once, here, so the Masks tab can offer
        # the route or grey it rather than failing silently.
        try:
            opts = self.fs.opts()
        except Exception:
            opts = ""
        out = {"idn": idn, "cwd": self.fs.get_cwd(),
               "free": self.fs.freespace(), "addr": self.addr,
               "options": opts, "masks": "2C" in opts.upper()}
        out.update(can)
        return out

    def scan(self):
        """Ask VISA what is on the bus and identify each instrument.

        Every address gets a `*IDN?`, which is the one query every SCPI
        instrument answers and which changes nothing on the device. Short
        timeouts throughout: an address that does not answer promptly is
        far more likely to be a printer or a dead session than a scope
        worth waiting on, and a scan that takes a minute will not be run.

        Anything already open is left alone - its identification is
        already known, and opening a second session to it could disturb a
        transfer in progress.
        """
        import pyvisa
        rm = pyvisa.ResourceManager()
        try:
            addresses = list(rm.list_resources())
        except Exception as exc:
            raise RuntimeError(
                "VISA could not list the bus: %s\n\nCheck that a VISA "
                "runtime and your GPIB driver are installed." % exc)

        found, current = [], getattr(self, "addr", None)
        for i, res in enumerate(addresses, 1):
            if self.cancelled.is_set():
                return {"found": found, "cancelled": True,
                        "reached": i - 1, "total": len(addresses)}
            self._progress("Identifying %s  (%d of %d)"
                           % (res, i, len(addresses)),
                           (i - 1.0) / max(len(addresses), 1))
            idn, note = "", ""
            if current and res == current and self.fs is not None:
                # The one we are already talking to. Asked inside the
                # same guard as the rest: a scope that has been switched
                # off since connecting raises here, and an unguarded
                # raise would end the scan and report nothing about any
                # of the other addresses.
                try:
                    idn = self.fs.idn()
                except Exception as exc:
                    note = describe_visa_error(exc)
            else:
                inst = None
                try:
                    inst = rm.open_resource(res, open_timeout=1500)
                    inst.timeout = 2000
                    idn = (inst.query("*IDN?") or "").strip()
                except Exception as exc:
                    # Why it did not answer is worth showing. VISA lists
                    # everything it has ever been told about, so a bus with
                    # one live instrument can easily show five addresses,
                    # and "no reply" alone leaves the user guessing which
                    # are switched off and which are misconfigured.
                    note = describe_visa_error(exc)
                finally:
                    # Closed whether it answered or not. Left to the
                    # success path, every silent address on the bus
                    # leaked a session per scan, and VISA starts
                    # refusing to open any of them once enough pile up.
                    if inst is not None:
                        try:
                            inst.close()
                        except Exception:
                            pass
            found.append({"addr": res, "idn": idn, "note": note,
                          "scope": looks_like_scope(idn)})
        return {"found": found, "cancelled": False,
                "reached": len(addresses), "total": len(addresses)}

    # The instrument exposes no "list my volumes" query, so they are probed:
    # cd to a candidate and see whether the cwd moved. A candidate that does
    # not exist leaves the cwd alone and raises event 256, which is drained.
    # Measured on a TDS 784D: fd0: and hd0: exist, and hd1:, fd1:, ram:,
    # nvram:, cf0:, disk0:, tffs0:, usb0: all do not. The list is kept longer
    # than that finding so a different model can answer for itself.
    CANDIDATES = ("hd0:", "fd0:", "hd1:", "fd1:")

    def volumes(self):
        """Which volumes this instrument has."""
        here = self.fs.get_cwd() or "hd0:"
        found = []
        for c in self.CANDIDATES:
            try:
                self.fs.set_cwd(c)
                if (self.fs.get_cwd() or "").rstrip("/").lower().startswith(
                        c.rstrip("/").lower()):
                    found.append(c)
            except Exception:
                pass
            finally:
                self.fs.errors()
        try:
            self.fs.set_cwd(here)
        except Exception:
            pass
        return {"volumes": found, "cwd": self.fs.get_cwd() or here}

    def listdir(self, path=None):
        if path is not None:
            self.fs.set_cwd(path)
        names = real_names(self.fs.dir())
        return {"cwd": self.fs.get_cwd(), "names": names,
                "free": self.fs.freespace()}

    def listdir_split(self, path):
        """List a directory, split into folders and files.

        FILESYSTEM:DIR? gives names with no type, so each entry is classified
        by trying to enter it. That is two or three queries per name - about
        three seconds for the 105 entries in hd0: root - which is why the UI
        caches the result per path and only ever asks for a folder once.

        The cwd is left at `path`, so a run of files costs no restore.
        """
        self.fs.set_cwd(path)
        base = self.fs.get_cwd() or path
        # Filtered before classification, not after: each phantom would
        # otherwise cost two or three round trips to prove it is not a
        # folder, which is most of the wait on an application folder.
        names = real_names(self.fs.dir())
        dirs, files = [], []
        for n in names:
            try:
                self.fs.set_cwd(n)
                now = self.fs.get_cwd() or base
            except Exception:
                now = base
            if now.rstrip("/").upper() != base.rstrip("/").upper():
                dirs.append(n)
                self.fs.set_cwd(base)
            else:
                files.append(n)
        # An empty listing has two quite different causes, and the
        # instrument will say which if asked: a folder with nothing in it,
        # or a drive with no disk in it.
        codes = self.fs.errors()
        empty_drive = not names and NO_MEDIA in (codes or [])
        return {"cwd": base, "dirs": dirs, "files": files,
                "no_media": empty_drive, "free": self.fs.freespace()}

    def is_dir(self, cwd, name):
        """Resolve laziy: try to enter it, and put the cwd back either way."""
        try:
            self.fs.set_cwd(name)
            now = self.fs.get_cwd()
            ok = now.rstrip("/").upper() != cwd.rstrip("/").upper()
        except Exception:
            ok = False
            now = cwd
        finally:
            self.fs.set_cwd(cwd)
        return ok

    def read(self, path):
        """Read a file, after checking it is actually there.

        Asking the instrument for a file that does not exist does not fail:
        it simply never answers, and VISA gives up ten seconds later with a
        timeout that tells the user nothing. One cheap directory listing
        turns that into a plain "not found".
        """
        parent = path.rstrip("/").rsplit("/", 1)[0]
        leaf = path.rstrip("/").rsplit("/", 1)[-1].upper()
        self.fs.set_cwd(parent)
        if leaf not in [n.upper() for n in real_names(self.fs.dir())]:
            self.fs.errors()
            raise IOError("%s does not exist on the instrument." % path)
        self.fs.errors()
        t = time.time()
        try:
            data = self.fs.read(path, timeout=TRANSFER_TIMEOUT)
        except Exception as exc:
            why = self._transfer_failed(self.fs.reader or "READFILE", path,
                                        exc, time.time() - t)
            # The capability table said this instrument could do it and
            # the instrument says otherwise. The instrument is right.
            # Ask it properly, once, and try again before giving up -
            # a wrong table entry should cost a few seconds, not a
            # feature.
            if self.no_transfers and not self.re_probed:
                self.re_probed = True
                was = self.fs.reader
                now = self.fs.probe_transfers()
                log_note("capabilities",
                         "table said reader=%s; the instrument disagrees, "
                         "asked it and got reader=%s write=%s"
                         % (was, now["reader"], now["can_write"]))
                if now["reader"] and now["reader"] != was:
                    self.no_transfers = None
                    data = self.fs.read(path, timeout=TRANSFER_TIMEOUT)
                    return {"path": path, "data": data,
                            "secs": time.time() - t}
            raise IOError(why)
        return {"path": path, "data": data, "secs": time.time() - t}

    def _transfer_failed(self, command, path, exc, secs):
        """Why a transfer produced nothing, in the instrument's own words.

        A firmware without FILESYSTEM:READFILE does not refuse the command
        - it says "undefined header" into the event queue and then simply
        never answers, so the only symptom the user sees is the program
        sitting there. Asking the queue afterwards turns that into a
        sentence, and stops the caller retrying something that cannot work.
        """
        said = ""
        try:
            self.fs.errors()
            msgs = getattr(self.fs, "last_messages", []) or []
            codes = [c for c, _t in msgs]
            if NO_MEDIA in codes:
                return "There is no disk in the drive."
            if UNDEFINED_HEADER in codes:
                self.no_transfers = command
                return ("This instrument's firmware has no "
                        "FILESYSTEM:%s command, so file contents cannot "
                        "be transferred over GPIB. Browsing, creating "
                        "folders and deleting still work." % command)
            said = "; ".join("%d %s" % (c, txt) for c, txt in msgs)
        except Exception:
            pass
        return ("%s gave no answer after %.0f s (%s).%s"
                % (path, secs, type(exc).__name__,
                   ("  The instrument said: " + said) if said else ""))

    # -- waveforms ---------------------------------------------------------

    def wfm_sources(self):
        """What this instrument has, and which of it is displayed.

        The names come from the instrument, not from a list here - a
        two-channel scope has no CH3, and its references may not run to
        four either.
        """
        selection = self.wfm.selection()
        return {"sources": [n for n, on in selection if on],
                "refs": [n for n, _on in selection if n.startswith("REF")],
                "all": [n for n, _on in selection],
                # Asked once per session and remembered inside TdsWfm, so
                # this costs a query on the first refresh and nothing
                # afterwards.
                "colours": self.wfm.display_colours()}

    # -- the instrument's error log ----------------------------------------

    def err_entries(self):
        """The service log, oldest first.

        An empty list is a result - the instrument has nothing to report
        - and is not the same as the firmware having no log at all,
        which raises.
        """
        t = time.time()
        found = self.err.entries()
        return {"entries": found, "secs": time.time() - t}

    def err_available(self):
        return {"errlog": self.err.available()}

    def err_clear(self):
        events = self.err.clear()
        left = self.err.entries()
        if left:
            raise IOError("The instrument still reports %d entries after "
                          "being told to clear the log.%s"
                          % (len(left),
                             ("  It said: " + "; ".join(events))
                             if events else ""))
        return {"cleared": True}

    # -- the screen --------------------------------------------------------

    def scr_options(self):
        """What the hardcopy subsystem here can be asked for.

        Every instrument in the range has one, so there is no question
        of whether this works at all - only of which formats come back
        as an image rather than as a page of printer control language,
        and whether the thing has colour to give.
        """
        offers = self.scr.offers()
        settings = self.scr.settings()
        return {"formats": [dict(f) for f in offers],
                "best": self.scr.best(),
                "palette": "PALETTE" in settings,
                "settings": settings}

    def scr_get(self, keyword=None, layout=None, palette=None):
        screen = self.scr.capture(keyword, layout, palette)
        return {"screen": screen, "secs": screen.seconds}

    def wfm_select(self, name, on):
        """Turn a source on or off on the instrument's display.

        A channel that is not displayed cannot be read at all - CURVE?
        is answered with 2241 - so this is the only route to a switched
        off channel's data. Measured on a 784D and a 640A: SELECT:CH2 ON
        takes effect at once and the curve reads immediately afterwards.
        """
        self.wfm.select(name, on)
        return {"name": name, "on": on}

    def wfm_delete(self, names):
        """Delete stored references. There is no undo on the instrument."""
        for name in names:
            self.wfm.delete_ref(name)
        return {"names": list(names)}

    def wfm_get(self, live, refs=()):
        """One acquisition, read on every live source, then the references.

        One source at a time down the bus, because that is the only way
        there is - DATA:SOURCE takes a single name - but all of them
        from the same frozen acquisition, which is what makes the traces
        comparable rather than merely similar. See TdsWfm.capture.

        A source that refuses is reported rather than taking the whole
        read with it: three good traces and one that would not come are
        worth having.
        """
        t = time.time()
        waves, refused, how = self.wfm.capture(
            list(live), list(refs),
            note=lambda name, done: self._progress("Reading %s" % name,
                                                   done))
        if not waves:
            raise IOError(refused[0][1] if refused else "nothing was read")
        return {"waves": waves, "wave": waves[0], "refused": refused,
                "how": how, "secs": time.time() - t}

    def wfm_send(self, wave, dest, allocate_from=None):
        """Send into a reference and prove it arrived.

        Verified by reading the reference back and comparing byte for
        byte, for the same reason uploads to the disk are: an instrument
        that quietly truncated the curve looks exactly like one that did
        not, and 532 is not always raised.
        """
        out = self.wfm.send_to_ref(wave, dest, allocate_from)
        out["verified"] = self.wfm.verify_ref(wave, dest)
        if not out["verified"]:
            raise IOError("%s was written but reading it back gave "
                          "something else - the instrument did not keep "
                          "what was sent." % dest)
        return out

    def wfm_send_many(self, items, allocate_from=None):
        """Several waveforms, each into its own reference.

        Stops at the first one that will not verify rather than carrying
        on: if the instrument is not keeping what it is sent, the next
        three will not land either, and a half-finished set of
        references reported as a success is worse than a clear failure.
        """
        done = []
        for i, (dest, wave) in enumerate(items):
            self._progress("Sending %s" % dest, i / float(len(items)))
            out = self.wfm.send_to_ref(wave, dest, allocate_from)
            if not self.wfm.verify_ref(wave, dest):
                raise IOError("%s was written but reading it back gave "
                              "something else - the instrument did not "
                              "keep what was sent." % dest)
            out["verified"] = True
            done.append(out)
        return {"sent": done}

    def write_verified(self, path, data, base=0.0, span=1.0):
        """Write, read back, compare. Never reports success on a guess.

        `base` and `span` map this file's progress into a slice of the bar,
        so one upload of many fills its own third rather than resetting the
        whole bar each time.

        An upload cannot report byte-by-byte progress: the payload goes out
        as one transfer because EOI on the last data byte is what ends the
        indefinite-length block, so splitting it would truncate the file.
        What it can report honestly is which phase it is in, and each
        attempt is three phases of roughly equal length.
        """
        leaf = path.rsplit("/", 1)[-1]
        last = None
        for attempt in range(1, ATTEMPTS + 1):
            step = span / ATTEMPTS
            here = base + span * (attempt - 1.0) / ATTEMPTS
            suffix = "" if attempt == 1 else " (attempt %d of %d)" % (
                attempt, ATTEMPTS)
            self._progress("Preparing %s%s" % (leaf, suffix), here)
            try:
                self.fs.delete(path)
            except Exception:
                pass
            self.fs.wait_done()
            self._progress("Sending %s, %s bytes%s"
                           % (leaf, format(len(data), ","), suffix),
                           here + step / 3)
            self.fs.write(path, data)
            self.fs.wait_done()
            # Ask before reading back rather than after: waiting for a
            # read-back of a file that was never created costs a timeout
            # to learn what the event queue will say straight away.
            #
            # Note what is NOT checked for here. An undefined-header event
            # after a write does not mean the firmware lacks WRITEFILE -
            # measured on a TDS 784C with no disk in the drive, the write
            # is refused for the real reason and the payload behind it is
            # then read as if it were commands, which raises 113 for a
            # line of file content. Whether the command exists is settled
            # once, safely, at connect; here the instrument's own words
            # are simply passed on.
            self.fs.errors()
            wrote = getattr(self.fs, "last_messages", []) or []
            if NO_MEDIA in [c for c, _t in wrote]:
                raise IOError("There is no disk in the drive, so nothing "
                              "can be written to it.")
            # Then look for it. A write that landed shows up in the
            # directory immediately; one the instrument accepted and
            # discarded does not, and reading back a file that was never
            # created only buys a timeout on the way to the same answer.
            parent = path.rstrip("/").rsplit("/", 1)[0]
            try:
                listed = [n.upper() for n in real_names(self.fs.dir(parent))]
            except Exception:
                listed = None          # cannot tell - fall through and read
            if listed is not None and leaf.upper() not in listed:
                last = "the file never appeared in %s after writing" % parent
                continue
            self._progress("Reading %s back to verify it%s" % (leaf, suffix),
                           here + 2 * step / 3)
            try:
                back = self.fs.read(path, timeout=TRANSFER_TIMEOUT)
            except Exception as exc:
                why = self._transfer_failed("READFILE", path, exc, 0.0)
                if self.no_transfers:
                    raise IOError(why)
                last = "read-back failed: %s" % exc
                continue
            if back == data:
                return {"path": path, "bytes": len(data), "attempts": attempt}
            nz = sum(1 for b in back if b)
            last = ("mismatch: %d bytes back, %d non-zero"
                    % (len(back), nz))
        raise RuntimeError("upload not verified after %d attempts (%s)"
                           % (ATTEMPTS, last))

    def delete(self, path):
        """Delete a file, from the volume root, after the protection check."""
        why = self.protected_reason(path)
        if why:
            raise RuntimeError(why)
        parent = path.rstrip("/").rsplit("/", 1)[0]
        leaf = path.rstrip("/").rsplit("/", 1)[-1].upper()
        self.fs.set_cwd(path.split("/")[0])
        self.fs.delete(path)
        self.fs.wait_done()
        events = self.fs.errors()
        self.fs.set_cwd(parent)
        gone = leaf not in [n.upper() for n in self.fs.dir()]
        self.fs.errors()
        if not gone:
            raise RuntimeError("%s was not deleted (events %s)"
                               % (path, events))
        return {"path": path, "events": events, "removed": True}

    def mkdir(self, path):
        self.fs.mkdir(path)
        self.fs.wait_done()
        return {"path": path, "events": self.fs.errors()}

    # -- masks ------------------------------------------------------------
    #
    # A mask on the instrument's disk is a file this program put there
    # and the instrument itself cannot read: there is no RECALL:MASK on
    # this generation. It is a library, kept where the masks are used,
    # and it survives the PC being a different PC next time.

    def msk_live(self):
        """What is in the instrument's eight mask segments now."""
        return {"segments": self.wfm.mask_segments()}

    def msk_clear(self):
        """Empty the instrument's eight segments and stop drawing them.

        The empty mask is written the way an empty segment is written
        everywhere else - a single 0,0 - rather than by any command that
        deletes a mask, because the one that sounds right, MASK:STANDARD,
        deletes the points and sets a standard mask in their place.
        """
        self.wfm.send_mask(tds_msk.Mask().to_scpi(), False)
        return {"segments": self.wfm.mask_segments()}

    def msk_read(self):
        """The eight segments as the instrument words them.

        Raw rather than counted, because this is the one that comes back
        into the editor as a mask. None means no mask subsystem at all.
        """
        return {"replies": self.wfm.mask_replies()}

    def set_read(self, source):
        """What the instrument is set to, for the file beside a mask."""
        return {"fields": tds_set.TdsSet(self.fs.inst).read(source)}

    def set_send(self, lines, source="CH1"):
        """Put a setup on the instrument, and say what it would not take.

        Twice over, because there are two ways not to take it. A
        command the firmware does not know is refused and lands in the
        event queue. A value it cannot reach is not refused at all - it
        is quietly replaced with the nearest one it can do - so the
        setup is read back afterwards and compared with what was asked
        for. See tds_set.differences.

        A setup written for one generation can hold a command another
        does not have - TTiP's own files say TRIGGER:MAIN:HOLDOFF:VALUE,
        which a 784D on v7.4e refuses - and the instrument's answer to a
        command it does not know is to log it and carry on. So the log
        is drained afterwards and what it caught is reported, rather
        than the setup being called sent and half of it not applying.
        """
        self.fs.inst.write("*CLS")
        for cmd in lines:
            self.fs.inst.write(cmd)
        self.wfm.q("*OPC?")
        # tds_err knows how to empty the event queue, including that
        # *ESR? has to prime it first. Asking EVMSG? without priming
        # answers "no events to report - new events pending *ESR?" for
        # ever, which a 794D turned into a report that it had refused
        # eighteen lines of a setup it had taken perfectly.
        said = tds_err.TdsErr(self.fs.inst, self.fs.payload).drain()
        return {"sent": len(lines),
                "refused": [m for m in said if not m.startswith("0,")],
                "got": tds_set.TdsSet(self.fs.inst).read(source)}

    # How far after the trigger to look when there is no clock. What
    # decorrelates the data is how many different edges can trigger it,
    # not how long the delay is, so this is kept short: the delay's own
    # jitter grows with it and buys nothing.
    DELAY_BITS = 16

    def msk_delayed(self, source, bit):
        """A whole eye on the instrument's own screen, with no clock.

        User Manual 071-0130-03 page 3-24: the delayed time base is for
        when you want "to delay an acquisition so it captures and
        displays events that follow other events". That is exactly the
        case - trigger on a data edge and look far enough past it that
        the data no longer remembers which edge it was, and ones and
        zeroes both land in the middle instead of only the top rail.

        Then centred, because the delay lands where it lands. Adding to
        the delay moves the trace earlier one for one, which was
        measured on a 784D and not assumed: a quarter-bit added moved
        the boundary by a quarter of a bit.
        """
        inst = self.fs.inst
        secdiv = float(self.wfm.q("HORIZONTAL:MAIN:SECDIV?"))
        inst.write("HORIZONTAL:DELAY:MODE RUNSAFTER")
        inst.write("HORIZONTAL:DELAY:SECDIV %g" % secdiv)
        out = self.DELAY_BITS * bit
        inst.write("HORIZONTAL:DELAY:TIME %g" % out)
        inst.write("HORIZONTAL:MODE DELAYED")
        self.wfm.q("*OPC?")
        # Four passes and a dozen crossings each, because the thing
        # being measured is noisy: every acquisition triggers on a
        # different data edge, so the phase read off a handful of
        # records carries the pattern's own jitter divided by the root
        # of how many were counted. Measured on a 784D against a USB
        # low speed signal with 40 ns of jitter on a 667 ns bit: two
        # passes over six crossings landed anywhere between nothing and
        # 7,285,488 hits from one run to the next, and neither run
        # thought anything was wrong.
        adrift = None
        for _pass in range(4):
            time.sleep(self.COUNT_FOR)
            seen, middle = [], None
            for _try in range(10):
                try:
                    wave = self.wfm.capture([source])[0][0]
                except Exception:
                    break
                spots = wave.points()
                volts = [v for _t, v in spots]
                if max(volts) - min(volts) < 0.5:
                    continue
                # The middle of the screen in the record's own time,
                # rather than worked out from the trigger position -
                # which is a main time base setting and says nothing
                # about where a delayed window sits.
                middle = (spots[0][0] + spots[-1][0]) / 2.0
                seen += tds_wfm.crossings_of(wave)
                if len(seen) >= 12:
                    break
            edge = tds_wfm.bit_phase(seen, bit)
            if edge is None or middle is None:
                return {"delay": out, "centred": False}
            adrift = (((middle - bit / 2.0 - edge) + bit / 2.0) % bit
                      - bit / 2.0)
            if abs(adrift) < bit / 50.0:
                break
            out -= adrift
            inst.write("HORIZONTAL:DELAY:TIME %g" % out)
            self.wfm.q("*OPC?")
        return {"delay": out, "adrift": adrift,
                "centred": adrift is not None and abs(adrift) < bit / 20.0}

    def msk_delay_tune(self, out, bit, segments, adrift):
        """The last word on the delay, from the mask rather than a phase.

        The phase above is measured off a handful of records, and every
        one of them triggered on a different data edge - so on a signal
        with real jitter the estimate is noisy, and a noisy estimate
        that lands half a bit out looks exactly like one that did not.
        Measured on a 784D against USB low speed: three runs of the
        same signal at the same delay gave nothing, nothing, and
        2,677,129 hits over 204,190 acquisitions, and all three
        reported the eye as centred.

        So the instrument is asked instead, and asked in DPO - which is
        why this runs after the display is switched rather than inside
        the centring. A fifth of a second of InstaVu is eighty thousand
        acquisitions against a couple of hundred in the ordinary
        display, and the difference is not academic: a CAN eye that
        showed nothing over a tenth of a second of ordinary sweeps came
        back with 1,289,385 hits over 212,836 acquisitions the moment
        DPO was on. It needs no arithmetic here at all - the instrument
        is already deciding what a hit is.
        """
        centred = adrift is not None and abs(adrift) < bit / 20.0
        best = self.msk_delay_hits() if segments else None
        if best is None:
            # Either there is no mask in the instrument to judge
            # against, or it is not counting. Nothing to tune by, so
            # the phase above stands.
            return {"delay": out, "centred": centred}
        # Finer near zero than far from it: the phase is usually right
        # and occasionally out by a fraction, so the small steps are
        # where the answer nearly always is. Stops at the first offset
        # that scores nothing, which is the common case and costs one
        # block.
        where = out
        for step in (0.1, -0.1, 0.2, -0.2, 0.35, -0.35, 0.5):
            if not best:
                break
            self.fs.inst.write("HORIZONTAL:DELAY:TIME %g"
                               % (out + step * bit))
            self.wfm.q("*OPC?")
            got = self.msk_delay_hits()
            if got is not None and got < best:
                best, where = got, out + step * bit
        self.fs.inst.write("HORIZONTAL:DELAY:TIME %g" % where)
        self.wfm.q("*OPC?")
        return {"delay": where, "centred": centred or not best}

    # A restarted tally does not climb, it arrives. Measured on a 784D
    # in DPO: MASK:COUNT:WAVEFORMS? reads 0 at 0.1, 0.2, 0.3 and 0.5
    # seconds and then 26,752 at 0.8, and 2.0 seconds reads exactly two
    # of those - the instrument publishes the count in blocks of about
    # that many acquisitions rather than continuously. A look shorter
    # than one block does not report a small number, it reports nothing
    # at all, which is a different thing entirely.
    def msk_delay_hits(self, most=4.0):
        """A tally to judge a delayed window by, or None if there is none.

        None rather than zero when the instrument has not published a
        block yet. The two are opposites and they look identical: zero
        hits over zero acquisitions is not a clean eye, it is no
        measurement. Reading it as a clean one is what made the tuning
        below do nothing at all - every offset scored zero, so the
        first was kept and the window never moved.

        Waited for rather than timed. A block is a fixed number of
        acquisitions, not a fixed time, so how long it takes depends on
        how fast the instrument is acquiring - which changes with the
        sweep, the display and what else is switched on. A second is
        enough at thirty thousand a second and not at twenty, and the
        difference showed up as a tuner that worked three runs out of
        four.
        """
        try:
            self.fs.inst.write("MASK:COUNT:STATE 0")
            self.fs.inst.write("MASK:COUNT:STATE 1")
            self.wfm.q("*OPC?")
        except IOError:
            return None
        end = time.time() + most
        while time.time() < end:
            time.sleep(0.25)
            try:
                if float(self.wfm.q("MASK:COUNT:WAVEFORMS?")):
                    return float(self.wfm.q("MASK:COUNT:TOTAL?"))
            except (TypeError, ValueError, IOError):
                return None
        return None

    def mid_level(self, name):
        """Halfway up whatever that input is carrying, in volts.

        There is no set-to-50% on this firmware - TRIGGER:MAIN:SETLEVEL
        answers 113, "Undefined header" - so a trigger level has to be
        measured. Zero is not a safe default for it: one leg of a
        differential pair swings between ground and its supply and
        never goes near zero on the way down.
        """
        try:
            volts = [v for _t, v in
                     self.wfm.capture([name])[0][0].points()]
        except Exception:
            return 0.0
        return (max(volts) + min(volts)) / 2.0 if volts else 0.0

    def bit_locked(self, source, bit, tries=12, least=6):
        """Is the data standing still against what is triggering it?

        One phase off each of several records, handed to
        tds_wfm.phase_locked - which is where the measurement and the
        reason for it are written down.

        A phase per record rather than a crossing per record: a screen
        holding a bit and a half often has one crossing on it and
        sometimes none, so taking the first of each gave two or three
        readings out of six tries and phase_locked was answering on
        almost no evidence. The vector mean of a record's own crossings
        is the same measurement made properly.
        """
        seen = []
        for _try in range(tries):
            try:
                marks = tds_wfm.crossings_of(
                    self.wfm.capture([source])[0][0])
            except Exception:
                continue
            if marks:
                seen.append(tds_wfm.bit_phase(marks, bit))
            if len(seen) >= least + 2:
                break
        return tds_wfm.phase_locked(seen, bit)

    def chan_fits(self, name, most=4):
        """Put a channel on a scale that shows all of whatever is on it.

        Returns what it was set to, so the caller can put it back.

        A mask's setup names one input, because that is the input the
        mask is drawn for, so the channel a clock arrives on is on
        whatever was dialled up last. Measured on a 784D with CH2 left
        at 100 mV/div: a GPIB clock swinging +0.26 to +2.55 V read
        back as +0.22 to +0.51, its middle came out +0.36 V instead of
        +1.41, and the instrument triggered on the toe of the rising
        edge where the slew is slowest and the noise widest. 700,391
        hits over 215,409 acquisitions on a signal that passes.
        """
        inst = self.fs.inst
        was = dict((what, str(self.wfm.q("%s:%s?" % (name, what))).strip())
                   for what in ("SCALE", "POSITION", "OFFSET"))
        inst.write("%s:POSITION 0;OFFSET 0" % name)
        for _lap in range(most):
            try:
                volts = [v for _t, v in
                         self.wfm.capture([name])[0][0].points()]
            except Exception:
                break
            scale = float(self.wfm.q("%s:SCALE?" % name))
            if max(abs(min(volts)), abs(max(volts))) >= scale * 3.9:
                want = scale * 5.0
            elif max(volts) - min(volts) < scale * 2.0:
                want = max((max(volts) - min(volts)) / 6.0, 1.0e-3)
            else:
                break
            inst.write("%s:SCALE %.3E" % (name, want))
            self.wfm.q("*OPC?")
            time.sleep(self.COUNT_FOR)
        return was

    def chan_back(self, name, was):
        """Put a channel back the way chan_fits found it."""
        for what, value in was.items():
            self.fs.inst.write("%s:%s %s" % (name, what, value))

    def clock_on(self, source, bit, skip=()):
        """Which input carries a clock at the bit rate, and its level.

        Every candidate looked at in turn, which costs about a second
        each and puts each one on the graticule while it looks. This is
        the fallback: the dialog asks which input the clock is on and
        only comes here when the answer is "find it".

        `skip` is the inputs already spoken for - the second leg of a
        pair being differenced in MATH1, which carries data and is not
        a candidate however it reads.
        """
        for name, _shown in self.wfm.selection():
            if not name.startswith("CH") or name == source or name in skip:
                continue
            looks, level = self.clock_look(name, bit)
            if looks:
                return name, level
        return None, 0.0

    def clock_look(self, name, bit):
        """Is there a clock at the bit rate on this input, and where.

        Returns (looks like one, its middle in volts). The channel is
        switched on just long enough to read and then put back the way
        it was, because a clock is by definition an input nobody wants
        on the graticule.

        A square wave at the bit rate crosses its own middle every half
        a bit, evenly; data crosses at whole multiples of a bit and
        unevenly, so the gaps tell the two apart. The same measurement
        serves both routes: naming the input skips the hunt, it does
        not skip the check, so a probe on the wrong channel is still
        reported rather than triggered on.
        """
        inst = self.fs.inst
        shown = dict(self.wfm.selection()).get(name, False)
        if not shown:
            inst.write("SELECT:%s ON" % name)
            self.wfm.q("*OPC?")
            # A channel just switched on has not acquired anything
            # yet, and the first read after it either raises or
            # gives the last thing that was there. Without this the
            # clock is found or missed depending on timing, which
            # is worse than never finding it.
            time.sleep(self.COUNT_FOR)
        # On a scale that shows all of it before anything is read
        # off it: a clipped record has the wrong middle, and the
        # middle is the trigger level.
        was = self.chan_fits(name)
        gaps, level = [], 0.0
        for _try in range(4):
            try:
                wave = self.wfm.capture([name])[0][0]
            except Exception:
                time.sleep(0.2)
                continue
            volts = [v for _t, v in wave.points()]
            if max(volts) - min(volts) < 0.1:
                continue
            level = (max(volts) + min(volts)) / 2.0
            edges = tds_wfm.crossings_of(wave)
            gaps += [b - a for a, b in zip(edges, edges[1:])]
            if len(gaps) >= 4:
                break
        self.chan_back(name, was)
        if not shown:
            inst.write("SELECT:%s OFF" % name)
        # Every gap half a bit, to within a tenth of one. Data
        # cannot do that: its gaps are whole bits and vary.
        looks = bool(gaps) and all(abs(g - bit / 2.0) < bit / 10.0
                                   for g in gaps)
        return looks, level

    def msk_eye(self, source, bit, segments=(), math=False, clocked=True,
                secdiv=None):
        """Set the instrument up to show an eye, however it can.

        Two things are told to it rather than found: `math`, meaning
        the signal arrives as two single-ended probes to be differenced
        in MATH1 rather than as one differential probe, and `clocked`,
        which is False for no clock, the name of an input - "CH2" - for
        a clock on that input, or True to go looking for one. Neither
        can be discovered: the instrument cannot see what is on the end
        of a probe, and an input with nothing plugged in reads the same
        as one with a clock that is switched off.

        Naming the input is the quick way and the one the tab offers.
        It is still measured before it is trusted, so a probe on the
        wrong channel comes back as `nameless` rather than as a trigger
        on an empty input.

        The order is forced by the instrument rather than chosen. The
        trigger has to be on a bit boundary before the sweep is worth
        measuring; the sweep can only be measured where there is a
        waveform record, which DPO has not got; and the tally means
        nothing until both are settled. So: trigger, ordinary display,
        centre, DPO, count.

        MATH1 changes two of those. DPO and math are mutually exclusive
        - `SELECT:MATH1 ON` under DPO answers 551, "InstaVu active", and
        turning DPO on with math displayed switches the math off
        without saying so - and MASK:SOURCE refuses MATH1 outright, so
        the instrument's own counter would be tallying a single leg.
        The math route therefore runs on persistence and is judged
        here, from MATH1's own record. Measured on a 784D, both.
        """
        out = {"clock": None, "where": None, "crossings": 0,
               "display": None, "counting": False, "delayed": None,
               "math": bool(math), "wanted": bool(clocked), "bits": None,
               "adrift": None, "nameless": None}
        inst = self.fs.inst
        # The sweep the mask was drawn against, where the setup beside
        # it names one. Sending a mask no longer touches the
        # instrument, so without this the eye is built on whatever
        # timebase happened to be dialled up - measured on a 784D, a
        # USB full speed mask against 1 us/div gave 116,921,796 hits
        # over 345,839 acquisitions, and hid the clock as well: at
        # 20 ns a point a 41.7 ns half period is two samples, and the
        # gaps no longer look even enough to be a clock.
        if secdiv:
            inst.write("HORIZONTAL:MAIN:SECDIV %.6E" % float(secdiv))
            self.wfm.q("*OPC?")
        # Back to the plain sweep and the plain display before anything
        # is decided, because whatever was set up last time is still
        # set up now: a delayed window left over from a run with no
        # clock, with a clock plugged in since, triggers on the clock
        # through the wrong part of the record - measured, 551,535 hits
        # over 168,279 acquisitions on a signal that passes.
        inst.write("HORIZONTAL:MODE MAIN")
        inst.write("DISPLAY:MODE NORMAL")
        inst.write("DISPLAY:PERSISTENCE 0")
        # A limit test left running from last time stops the instrument
        # the moment its template is broken, which would look like an
        # eye that had frozen for no reason.
        inst.write("LIMIT:STATE OFF")
        inst.write("ACQUIRE:STOPAFTER RUNSTOP")
        inst.write("ACQUIRE:STATE RUN")
        self.wfm.q("*OPC?")
        # How much of a bit the graticule is holding, whatever the
        # sweep came from. An eye wants about one; a screen holding
        # twelve of them, or a twelfth of one, is not going to make one
        # and the user is better told than left to read a verdict off
        # it. Reported rather than refused: it is their instrument.
        try:
            out["bits"] = (10.0 * float(
                self.wfm.q("HORIZONTAL:MAIN:SECDIV?"))) / bit
        except (TypeError, ValueError):
            pass
        if math:
            # After the display is out of DPO and not before: SELECT
            # MATH1 while InstaVu is on answers 551, "InstaVu active --
            # deactivate to use math", and the math never appears.
            #
            # Programmer Manual 070-9876-00 page 2-205: a dual waveform
            # expression is "<source><operator><source>", the operators
            # being + - * /, and the sources CH<x> or REF<x>. There is
            # no command for a math waveform's vertical scale on this
            # family - only DEFINE, NUMAVG and PROCESSING - so the two
            # legs keep the scale the setup gave them and the
            # difference lands where the instrument puts it.
            inst.write('MATH1:DEFINE "CH1 - CH2"')
            # The second leg put on the first's settings. The setup
            # beside a mask names one channel, because a differential
            # probe needs one; two probes need both, and a leg left on
            # a finer scale clips and takes the difference with it.
            for what in ("COUPLING", "BANDWIDTH", "SCALE", "POSITION",
                         "OFFSET"):
                inst.write("CH2:%s %s"
                           % (what, str(self.wfm.q("CH1:%s?" % what)).strip()))
            inst.write("SELECT:CH2 ON")
            inst.write("SELECT:MATH1 ON")
            self.wfm.q("*OPC?")
            source = "MATH1"
        else:
            # Whatever the other route left behind. This program put
            # MATH1 on the graticule, so this program takes it off
            # again; left there it is a second trace across the mask.
            inst.write("SELECT:MATH1 OFF")
        # What to trigger on if there is no clock, or if the clock
        # turns out to be one the data is not locked to. On the math
        # route it has to be a channel in any case -
        # TRIGGER:MAIN:EDGE:SOURCE does not take MATH1.
        lead = "CH1" if math else source
        # `clocked` is False for none, True for "find it", or the name
        # of the input the user says the clock is on. Naming it skips
        # the hunt - four channels switched on in turn is a second each
        # and a trace across the mask every time - but not the check:
        # an input with nothing on it is reported, not triggered on.
        skip = ("CH1", "CH2") if math else ()
        if not clocked:
            clock, level = None, 0.0
        elif clocked is True:
            clock, level = self.clock_on(source, bit, skip=skip)
        elif clocked == source or clocked in skip:
            # The input they named is carrying the data. Said rather
            # than quietly searched, or the eye is built on a trigger
            # nobody chose.
            clock, level, out["nameless"] = None, 0.0, clocked
        else:
            looks, level = self.clock_look(clocked, bit)
            clock = clocked if looks else None
            if not looks:
                out["nameless"], level = clocked, 0.0
        if clock:
            # The level is measured because there is no set-to-50% on
            # this firmware - TRIGGER:MAIN:SETLEVEL answers 113,
            # "Undefined header" - and a clock is as likely to be a 0 to
            # 5 V logic swing as one sitting about zero.
            inst.write("TRIGGER:MAIN:EDGE:SOURCE %s;COUPLING DC;SLOPE RISE"
                       % clock)
            inst.write("TRIGGER:MAIN:LEVEL %g" % level)
            # And off the graticule, however it was found. A clock is
            # triggered on perfectly well switched off, and its trace
            # over an eye mask is a solid band through the keep-out:
            # measured on a 784D with a 2 Vpp clock left displayed,
            # 2,143,642 hits over 58,455 acquisitions on a signal that
            # passes cleanly the moment it is switched off.
            inst.write("SELECT:%s OFF" % clock)
            out["clock"] = clock
        else:
            # Nothing to trigger on but the data. Said explicitly
            # rather than left alone, because whatever was triggered on
            # last time still is: a clock found on a previous run and
            # unplugged since leaves the instrument waiting on a dead
            # input. On the math route it has to be a channel in any
            # case - TRIGGER:MAIN:EDGE:SOURCE does not take MATH1.
            inst.write("TRIGGER:MAIN:EDGE:SOURCE %s;COUPLING DC;SLOPE RISE"
                       % lead)
            inst.write("TRIGGER:MAIN:LEVEL %g" % self.mid_level(lead))
        inst.write("DISPLAY:MODE NORMAL")
        inst.write("DISPLAY:PERSISTENCE 0")
        self.wfm.q("*OPC?")
        time.sleep(self.COUNT_FOR)
        if clock:
            # Triggered on a boundary already, so the main sweep only
            # has to be slid half a bit.
            got = self.msk_centre(bit, source)
            out["where"], out["crossings"] = got["where"], got["crossings"]
            # ... unless the data is not locked to that clock, which
            # looks exactly like a clock until the eye is drawn. Two
            # channels of one generator, set to exactly commensurate
            # frequencies and told to EQPHASE, still walked: see
            # tds_wfm.phase_locked. A clock nothing is locked to is
            # worse than no clock at all, so it is dropped and the
            # no-clock route taken instead.
            if not self.bit_locked(source, bit):
                out["adrift"], out["clock"] = clock, None
                inst.write("TRIGGER:MAIN:EDGE:SOURCE %s;COUPLING DC;"
                           "SLOPE RISE" % lead)
                inst.write("TRIGGER:MAIN:LEVEL %g" % self.mid_level(lead))
                self.wfm.q("*OPC?")
                out["delayed"] = self.msk_delayed(source, bit)
        else:
            # No clock, so triggering on the data can only ever show
            # half an eye - the bit after a rising edge is always high.
            # The delayed time base is the instrument's own answer to
            # that, and it leaves a whole eye on the glass rather than
            # only in this program's picture of it.
            out["delayed"] = self.msk_delayed(source, bit)
        # DPO where it is fitted, asked for rather than assumed: an
        # instrument without it takes the command and stays where it
        # was, so writing the mode and reading it back is the only way
        # to find out which kind you are talking to.
        if not math:
            inst.write("DISPLAY:MODE INSTAVU")
            self.wfm.q("*OPC?")
        out["display"] = str(self.wfm.q("DISPLAY:MODE?")).strip()
        if out["display"] != "INSTAVU":
            # No DPO, or math, which cannot have it. Persistence
            # overlays sweeps into the same picture the slow way -
            # hundreds of acquisitions rather than a hundred thousand,
            # but an eye all the same.
            inst.write("DISPLAY:MODE NORMAL")
            inst.write("DISPLAY:PERSISTENCE 5")
            self.wfm.q("*OPC?")
        # Where the delayed window really wants to be, judged by the
        # mask in whichever display is now on the glass. Last, because
        # the judging is the instrument's tally and DPO fills it a
        # thousand times faster than the ordinary display does.
        #
        # Not on the math route, for the same reason the verdict is not
        # taken there: MASK:SOURCE can only be a channel, so the tally
        # is one leg of a pair against a mask drawn for the difference.
        # Tuning a delay by that number would move the window to
        # wherever suits the wrong signal.
        if out["delayed"] and not math:
            out["delayed"] = self.msk_delay_tune(
                out["delayed"]["delay"], bit, segments,
                out["delayed"].get("adrift"))
        # And the clock route checked against the picture it made,
        # rather than against a measurement taken before there was a
        # picture. A generator's two channels are not one oscillator:
        # the same clock reads as locked over the two seconds it takes
        # to measure and has walked a whole bit a minute later.
        # Measured on a USB low speed signal that read as locked and
        # then scored 6,008,613 hits over 594,594 acquisitions - the
        # same signal, dropped to the delayed sweep, scores none. The
        # mask is the only test that cannot be fooled this way.
        if out["clock"] and segments and not math and self.msk_delay_hits():
            out["adrift"], out["clock"], out["where"] = clock, None, None
            self.msk_eye_no_clock(source, bit, segments, lead, out)
        # The instrument's counter is left alone on the math route. Its
        # source can only be a channel, so what it would be tallying is
        # one leg of the pair against a mask drawn for the difference -
        # a number that looks like a verdict and is not one.
        out["counting"] = False if math else self.msk_count_from()
        return out

    def msk_eye_no_clock(self, source, bit, segments, lead, out):
        """Build the eye on the delayed sweep, from wherever we are.

        The order is forced by the instrument: the centring measures
        waveform records and DPO has none, so the display goes back to
        the ordinary one, the window is placed, and then DPO comes on
        and the delay is tuned against the mask. Used both when no
        clock was found and when one was found and the picture it gave
        turned out to be walking.
        """
        inst = self.fs.inst
        inst.write("DISPLAY:MODE NORMAL")
        inst.write("DISPLAY:PERSISTENCE 0")
        inst.write("TRIGGER:MAIN:EDGE:SOURCE %s;COUPLING DC;SLOPE RISE"
                   % lead)
        inst.write("TRIGGER:MAIN:LEVEL %g" % self.mid_level(lead))
        self.wfm.q("*OPC?")
        got = self.msk_delayed(source, bit)
        inst.write("DISPLAY:MODE INSTAVU")
        self.wfm.q("*OPC?")
        out["display"] = str(self.wfm.q("DISPLAY:MODE?")).strip()
        if out["display"] != "INSTAVU":
            inst.write("DISPLAY:MODE NORMAL")
            inst.write("DISPLAY:PERSISTENCE 5")
            self.wfm.q("*OPC?")
        out["delayed"] = self.msk_delay_tune(got["delay"], bit, segments,
                                             got.get("adrift"))
        return out

    def msk_centre(self, bit, source="CH1"):
        """Slide the sweep until the middle of a bit is on the mask.

        An eye mask is drawn about the middle of a bit and the trigger
        sits on a bit boundary, so the two are half a bit apart and the
        gap has to be measured rather than worked out: triggering on a
        clock, the phase between clock and data is fixed but arbitrary,
        and even on the data the shaping in a driver delays a crossing
        by however long it takes to slew.

        The crossings are bit boundaries, so folding them onto one bit
        and taking the mean angle - rather than an average, which knows
        nothing about wrapping - gives the boundary the sweep is
        holding. Half a bit past it is the middle.
        """
        wide = 10.0 * float(self.wfm.q("HORIZONTAL:MAIN:SECDIV?"))
        found = []
        for _try in range(10):
            try:
                wave = self.wfm.capture([source])[0][0]
            except tds_wfm.NotReadable:
                # DPO. There is no record to measure and no amount of
                # asking again will make one, so say so at once rather
                # than throwing at somebody who pressed a button.
                return {"where": None, "crossings": 0}
            found += tds_wfm.crossings_of(wave)
            if len(found) >= 6:
                break
        where = tds_wfm.bit_centre(found, bit, wide)
        if where is None:
            return {"where": None, "crossings": 0}
        self.fs.inst.write("HORIZONTAL:TRIGGER:POSITION %g" % where)
        self.wfm.q("*OPC?")
        return {"where": where, "crossings": len(found)}

    def msk_behind(self, math=False):
        """Something to draw behind a mask, whichever the scope can give.

        A trace where there is one to read; the screen itself where
        there is not. In DPO there is no waveform record at all - every
        sample reads back mid-scale - so the picture on the glass is the
        only honest answer, and cropped to its graticule it lines up
        with the editor's own ten by eight divisions exactly.

        `math` says the signal is the difference of two probes, taken
        in MATH1. Then MATH1 is what to read, and the instrument's own
        tally is not read at all: its source can only be a channel, so
        it is counting one leg against a mask drawn for the pair.

        The tally is read, never restarted. Start measurement is what
        zeroes it, and a picture taken afterwards is meant to say what
        has landed since - zeroing it here made every verdict a tally
        over the second it took to take the picture.
        """
        counting = False if math else self.msk_counting()
        if math:
            waves, _refused, _how = self.wfm.capture(["MATH1"])
            return {"wave": waves[0] if waves else None, "hits": None}
        if self.wfm.in_dpo():
            shot = tds_scr.TdsScr(self.fs.inst).capture()
            pixels, wide, tall = shot.graticule()
            return {"shot": {"pixels": pixels, "palette": shot.palette,
                             "width": wide, "height": tall},
                    "hits": self.msk_count_read() if counting else None}
        live = [n for n in self.wfm.sources() if n.startswith("CH")]
        if not live:
            live = self.wfm.sources()
        if not live:
            raise tds_wfm.NotReadable(None,
                                      "No source is displayed on the "
                                      "instrument, so there is nothing "
                                      "to capture.")
        # The tally is read *before* the capture, not after it. A
        # capture stops the acquisition to read the record out, so a
        # count taken afterwards is a count over nothing - measured:
        # zero hits over zero acquisitions, every time. A second of
        # watching first is a few hundred acquisitions to judge on,
        # where the record that comes back is one.
        hits = None
        if counting:
            time.sleep(self.COUNT_FOR)
            hits = self.msk_count_read()
        waves, _refused, _how = self.wfm.capture(live[:1])
        return {"wave": waves[0] if waves else None, "hits": hits}

    def msk_watch(self):
        """Set the instrument up to judge a mask that draws no eye.

        A level mask has no unit interval - GPIB's TTL undefined zone,
        RS-232's, a SATA burst envelope - so there is no bit to centre
        and nothing to delay. There is still a display to set, and this
        route was not setting one: it zeroed the counter and left the
        glass exactly as the last mask left it. Two things followed
        from that on the bench. DPO never came on, so the instrument
        tested a few hundred acquisitions a second where it can test a
        few hundred thousand. And a delayed window left over from the
        eye mask before it stayed on, so a GPIB pulse was judged
        through a window sixteen bits after its own trigger.

        Everything msk_eye does except the eye: the sweep back to
        MAIN, a limit test from last time switched off, DPO where it is
        fitted and persistence where it is not, and then the tally.
        """
        inst = self.fs.inst
        inst.write("HORIZONTAL:MODE MAIN")
        inst.write("LIMIT:STATE OFF")
        inst.write("ACQUIRE:STOPAFTER RUNSTOP")
        inst.write("ACQUIRE:STATE RUN")
        inst.write("DISPLAY:MODE INSTAVU")
        self.wfm.q("*OPC?")
        display = str(self.wfm.q("DISPLAY:MODE?")).strip()
        if display != "INSTAVU":
            inst.write("DISPLAY:MODE NORMAL")
            inst.write("DISPLAY:PERSISTENCE 5")
            self.wfm.q("*OPC?")
        return {"display": display, "counting": self.msk_count_from()}

    def msk_count_from(self):
        """Zero the instrument's own mask tally and start it counting.

        There is no MASK:COUNT:RESET on this firmware - it answers
        "Undefined header" - so the state going off and on again is the
        only way to clear the counters. Started here and read after the
        capture, the tally covers exactly the time the picture was
        being built, which is what makes it the picture's verdict.
        """
        if getattr(self, "_counts", None) is False:
            return False
        try:
            self.fs.inst.write("MASK:COUNT:STATE 0")
            self.fs.inst.write("MASK:COUNT:STATE 1")
            self.wfm.q("*OPC?")
            self._counts = True
            return True
        except Exception:
            self._counts = False
            return False

    def msk_counting(self):
        """Is the instrument's mask tally running?

        Asked rather than remembered, because the counter belongs to
        the instrument and somebody can turn it off at the front panel
        between two presses here. An instrument with no mask subsystem
        answers nothing, which is the same as no - and is asked only
        once, because a query to a subsystem that is not there does not
        answer "Undefined header", it does not answer at all and the
        bus waits out its timeout. Measured on MATH1:SCALE?, which does
        exactly that.
        """
        if getattr(self, "_counts", None) is False:
            return False
        try:
            said = str(self.wfm.q("MASK:COUNT:STATE?")).strip().upper()
        except Exception:
            self._counts = False
            return False
        return said in ("1", "ON")

    def msk_count_read(self):
        """What landed in the mask while the picture was being taken.

        The instrument counts in DPO, where there is no waveform record
        to judge and nothing else can give a verdict at all. Measured on
        a 784D: a hundred and twenty thousand acquisitions against three
        hundred in the ordinary display, which is the whole reason to
        run an eye that way.
        """
        try:
            total = float(self.wfm.q("MASK:COUNT:TOTAL?"))
            waves = float(self.wfm.q("MASK:COUNT:WAVEFORMS?"))
        except Exception:
            self._counts = False
            return None
        each = {}
        for n in range(1, tds_msk.SEGMENTS + 1):
            try:
                landed = float(self.wfm.q("MASK:MASK%d:COUNT?" % n))
            except Exception:
                break
            if landed:
                each[n] = landed
        return {"total": total, "waves": waves, "each": each}

    def msk_send(self, lines, display=True):
        """Put a mask into the instrument's eight segments and show it.

        Read back afterwards rather than trusted: the segments answer
        with their point counts, and a count that does not match what
        was sent means the instrument kept something else. Measured on a
        784D from a clean setup, points round-trip exactly.
        """
        self.wfm.send_mask(lines, display)
        want = {}
        for cmd in lines:
            if ":POINTSPCNT " not in cmd:
                continue
            n = int(cmd[len("MASK:MASK"):].split(":")[0])
            pairs = len(cmd.split(" ", 1)[1].split(",")) // 2
            want[n] = 0 if pairs < 2 else pairs
        got = self.wfm.mask_segments() or {}
        return {"wanted": want,
                "got": dict(enumerate(got, 1)) if got else {}}

    def lim_build(self, source, vertical, horizontal, dest):
        """Have the instrument build a limit template for itself.

        The other way to get one, and much the easier to explain: point
        it at a signal that is known good, say how far a later one may
        wander from it, and it writes the envelope. No mask, no
        polygons, no percent of the graticule.

        Measured on a 784D: the reference that comes back really is an
        envelope - "Ref3, DC coupling, 500.0mVolts/div, 200.0ns/div,
        500 points, Envelope mode", PT_FMT ENV - and a limit test
        against it holds while the signal is the same and stops within
        two seconds of the amplitude being doubled. Tolerances of zero
        are taken without complaint.

        Tolerances are in divisions, which is what the instrument wants
        and, as it happens, what a person can see: half a division is
        half a division on the glass.
        """
        inst = self.fs.inst
        # Not in DPO. A limit test under InstaVu reads back on and
        # tests nothing - 550, "InstaVu active" - and a template built
        # there would be built from a record that does not exist.
        inst.write("ACQUIRE:STOPAFTER RUNSTOP")
        inst.write("LIMIT:STATE OFF")
        inst.write("DISPLAY:MODE NORMAL")
        inst.write("ACQUIRE:STATE RUN")
        self.wfm.q("*OPC?")
        time.sleep(self.COUNT_FOR)
        inst.write("LIMIT:TEMPLATE:SOURCE %s" % source)
        inst.write("LIMIT:TEMPLATE:DESTINATION %s" % dest)
        inst.write("LIMIT:TEMPLATE:TOLERANCE:VERTICAL %g" % vertical)
        inst.write("LIMIT:TEMPLATE:TOLERANCE:HORIZONTAL %g" % horizontal)
        inst.write("LIMIT:TEMPLATE STORE")
        self.wfm.q("*OPC?")
        time.sleep(0.6)
        made = self.wfm.exists(dest)
        said = [] if made else [
            m for m in tds_err.TdsErr(inst, self.fs.payload).drain()
            if not m.startswith("0,")]
        return {"dest": dest, "source": source, "why": said,
                "made": made,
                "vertical": vertical, "horizontal": horizontal}

    def lim_run(self, source, dest):
        """Judge `source` against the template in `dest`, from now on.

        Three rules, each measured on a 784D: out of DPO first, the
        test switched on before the instrument is told to stop on it,
        and no *OPC? after STOPAFTER LIMIT - the instrument does not
        answer that one until the test trips.
        """
        inst = self.fs.inst
        inst.write("ACQUIRE:STOPAFTER RUNSTOP")
        inst.write("DISPLAY:MODE NORMAL")
        inst.write("ACQUIRE:STATE RUN")
        self.wfm.q("*OPC?")
        inst.write("LIMIT:COMPARE:%s %s" % (source, dest))
        inst.write("LIMIT:STATE ON")
        self.wfm.q("*OPC?")
        on = str(self.wfm.q("LIMIT:STATE?")).strip() in ("1", "ON")
        why = [] if on else [
            m for m in tds_err.TdsErr(inst, self.fs.payload).drain()
            if not m.startswith("0,")]
        if on:
            inst.write("ACQUIRE:STOPAFTER LIMIT")
            inst.write("ACQUIRE:STATE RUN")      # and no *OPC? after it
        return {"dest": dest, "source": source, "on": on, "why": why}

    def lim_picture(self, source, dest):
        """The template and a live trace, for drawing.

        The template is an envelope: two values a column, the lowest
        and the highest the signal may be there. Read as a curve rather
        than as a waveform, because a Waveform is one value a point and
        an envelope is not one of those.
        """
        band = self.wfm.read_envelope(dest)
        wave = None
        try:
            got, _refused, _how = self.wfm.capture([source])
            wave = got[0] if got else None
        except Exception:
            pass
        return {"band": band, "wave": wave, "dest": dest,
                "source": source}

    def limit_state(self):
        """Is the limit test still passing? Running means yes.

        The instrument reports a failure by stopping, so this is the
        whole verdict. Nothing here writes, and nothing asks *OPC?.
        """
        try:
            return {"running": str(self.wfm.q("ACQUIRE:STATE?")).strip()
                    in ("1", "ON", "RUN"),
                    "on": str(self.wfm.q("LIMIT:STATE?")).strip()
                    in ("1", "ON")}
        except Exception:
            return {"running": None, "on": False}

    def limit_stop(self):
        """Switch the test off and give the instrument back."""
        inst = self.fs.inst
        inst.write("LIMIT:STATE OFF")
        inst.write("ACQUIRE:STOPAFTER RUNSTOP")
        inst.write("ACQUIRE:STATE RUN")
        self.wfm.q("*OPC?")
        return {"on": False}

    def msk_envelope(self, lines, dest, numbers, allocate_from=None):
        """Send a mask as a limit template, then read it back.

        The read-back is reported rather than raised on. This route has
        been built from Tektronix's own .ENV files and checked as far as
        it can be checked from here, but reading an envelope back off an
        instrument has not been measured on one - so a mismatch is shown
        with both sets of numbers instead of being declared a failure of
        the instrument. See TdsWfm.verify_envelope.
        """
        out = self.wfm.send_envelope(lines, dest, allocate_from)
        out.update(self.wfm.verify_envelope(dest, numbers))
        return out

    # -- batches ----------------------------------------------------------
    #
    # A multiple selection runs as ONE job rather than as a queue of jobs
    # driven from the UI. The bus is single-threaded and every operation
    # here moves the current directory, so interleaving them would be a way
    # to end up somewhere unexpected. Progress is reported as it goes; a
    # failure on one item is recorded and the rest carry on, as Explorer
    # does when one file of a selection is in use.

    def _progress(self, text, frac=None):
        """Tell the UI where we are. `frac` is 0..1, or None for unknown."""
        self.out.put(("progress", True, {"text": text, "frac": frac}))

    def delete_many(self, paths):
        done, failed = [], []
        for i, p in enumerate(paths, 1):
            self._progress("Deleting %d of %d: %s"
                           % (i, len(paths), p.rsplit("/", 1)[-1]),
                           (i - 1.0) / len(paths))
            try:
                self.delete(p)
                done.append(p)
            except Exception as exc:
                failed.append((p, str(exc)))
        return {"done": done, "failed": failed}

    def upload_many(self, items):
        """`items` is [(path on the PC, destination path on the instrument)].

        Every file is verified individually by write_verified, so a batch
        that reports success really did land byte for byte.
        """
        done, failed = [], []
        for i, (src, dest) in enumerate(items, 1):
            share = 1.0 / len(items)
            try:
                with open(src, "rb") as fh:
                    data = fh.read()
                self.write_verified(dest, data, (i - 1.0) * share, share)
                done.append((dest, len(data)))
            except Exception as exc:
                failed.append((dest, str(exc)))
        return {"done": done, "failed": failed}

    def download_tree(self, path, destdir):
        """Download a folder and everything beneath it.

        The whole tree is enumerated first so that progress can be
        reported against a known total. That costs one listing per folder
        up front, which is cheaper than it sounds beside the transfers
        themselves, and much better than a bar that cannot say how far
        along it is.
        """
        leaf = path.rstrip("/").rsplit("/", 1)[-1]
        want, folders = [], []

        def walk(remote, local):
            folders.append(local)
            self._progress("Looking in %s ..." % remote, None)
            listing = self.listdir_split(remote)
            for f in listing["files"]:
                want.append(("%s/%s" % (remote, f), os.path.join(local, f)))
            for d in listing["dirs"]:
                walk("%s/%s" % (remote, d), os.path.join(local, d))

        walk(path, os.path.join(destdir, leaf))
        for d in folders:
            try:
                os.makedirs(d, exist_ok=True)
            except Exception as exc:
                raise RuntimeError("cannot create %s: %s" % (d, exc))

        done, failed = [], []
        for i, (remote, local) in enumerate(want, 1):
            self._progress("Downloading %d of %d: %s"
                           % (i, len(want), remote.rsplit("/", 1)[-1]),
                           (i - 1.0) / max(len(want), 1))
            try:
                data = self.read(remote)["data"]
                with open(local, "wb") as fh:
                    fh.write(data)
                done.append((local, len(data)))
            except Exception as exc:
                failed.append((remote, str(exc)))
        return {"done": done, "failed": failed,
                "dir": os.path.join(destdir, leaf),
                "folders": len(folders)}

    def download_many(self, paths, destdir):
        """Read each file and write it into `destdir` as it arrives.

        Written one at a time rather than collected and returned, so a long
        selection does not sit in memory and a failure part way through
        still leaves the files that did arrive.
        """
        done, failed = [], []
        for i, p in enumerate(paths, 1):
            leaf = p.rsplit("/", 1)[-1]
            self._progress("Downloading %d of %d: %s" % (i, len(paths), leaf),
                           (i - 1.0) / len(paths))
            try:
                data = self.read(p)["data"]
                dest = os.path.join(destdir, leaf)
                with open(dest, "wb") as fh:
                    fh.write(data)
                done.append((dest, len(data)))
            except Exception as exc:
                failed.append((p, str(exc)))
        return {"done": done, "failed": failed, "dir": destdir}

    # ---------------------------------------------------------- protection
    #
    # The Java runtime is what makes the instrument's application support
    # work at all, and it is spread across a folder and two boot scripts at
    # the root. Losing any of it means reimaging the CF card, which is not a
    # small job. None of it is deletable through this program.
    #
    # Learned the hard way: an RMDIR test wedged the filesystem subsystem and
    # cost a reimage. The guards below are not decoration.
    RUNTIME_MSG = ("It is part of the Java runtime. Deleting it would stop "
                   "the instrument from running applications, and putting it "
                   "back means reimaging the CF card.")
    BOOT_MSG = ("It is part of the instrument's boot chain. Deleting it "
                "would stop the runtime starting at power-on.")

    # SYSTEM~1 is deliberately not here. It is the card's own recycle
    # folder and holds nothing the instrument needs; it fills up with
    # whatever was deleted from a PC and is exactly the sort of thing
    # somebody opens this program to clear out.
    PROTECTED_DIRS = {
        "APP": ("It holds the Java runtime and every shipped application."),
        "TDSRTE1": RUNTIME_MSG,
    }
    PROTECTED_FILES = {
        "STARTUP.BAT": BOOT_MSG, "OSSA.BAT": BOOT_MSG,
        "RTE1.BAT": BOOT_MSG, "RTE1ORIG.BAT": BOOT_MSG,
        "RT.JAR": RUNTIME_MSG, "JAVA68K.O": RUNTIME_MSG,
        "LIBJIT.O": RUNTIME_MSG, "NIGPIB.O": RUNTIME_MSG,
        "EXTCP.O": RUNTIME_MSG, "PATCH.O": RUNTIME_MSG,
        "LOGO.BIN": RUNTIME_MSG, "VERSION.DAT": RUNTIME_MSG,
    }
    # Everything at or below this path is the runtime itself.
    PROTECTED_TREES = ("APP/TDSRTE1",)

    @classmethod
    def protected_reason(cls, path):
        """Why `path` may not be deleted, or None if it may be."""
        p = path.rstrip("/")
        leaf = p.rsplit("/", 1)[-1].upper()
        upper = p.upper()
        if "/" not in p:
            return "'%s' is a drive, not a file or folder." % p
        # Nothing on a floppy is protected. The protection exists for the
        # instrument's own applications and its Java runtime, which live
        # on the hard disk; a floppy holds whatever the user put there
        # and is theirs to empty.
        if upper.split("/", 1)[0].startswith("FD"):
            return None
        if is_phantom(leaf):
            # Should be unreachable - these never reach the UI - but a
            # DELETE aimed at one would strip the long name off the real
            # file that follows it in the directory table.
            return ("'%s' is not a file. It is part of how a long file "
                    "name is stored on the card, and deleting it would "
                    "damage the file it belongs to." % leaf)
        for tree in cls.PROTECTED_TREES:
            if ("/" + tree) in upper or upper.endswith("/" + tree):
                return "'%s' cannot be deleted.\n\n%s" % (leaf,
                                                          cls.RUNTIME_MSG)
        for table in (cls.PROTECTED_DIRS, cls.PROTECTED_FILES):
            if leaf in table:
                return "'%s' cannot be deleted.\n\n%s" % (leaf, table[leaf])
        return None

    def survey(self, path):
        """What is inside a folder, for the confirmation dialog.

        Leaves the cwd back at the VOLUME ROOT, never inside `path`. Leaving
        it inside is what caused the incident this program is now careful
        about: RMDIR issued while standing in the target is refused with
        event 257, and that refusal left the filesystem subsystem returning a
        fixed garbage pattern for every path until the card was reimaged.
        """
        self.fs.set_cwd(path)
        names = real_names(self.fs.dir())
        self.fs.set_cwd(path.split("/")[0])
        self.fs.errors()
        return {"path": path, "names": names}

    def rmdir(self, path):
        """Remove a folder. Recursive and silent - see tds_fs.rmdir.

        Refuses a protected name, and verifies afterwards rather than
        trusting the empty event queue, because on this instrument silence
        means nothing either way.
        """
        why = self.protected_reason(path)
        if why:
            raise RuntimeError(why)
        leaf = path.rstrip("/").rsplit("/", 1)[-1].upper()
        parent = path.rstrip("/").rsplit("/", 1)[0]
        self.context = "rmdir %s" % path
        # Stand at the VOLUME ROOT, which cannot be inside the target at any
        # depth. RMDIR is refused with event 257 if the cwd is within the
        # folder being removed, and that refusal is what wedged the
        # filesystem subsystem badly enough to need a reimage.
        self.fs.set_cwd(path.split("/")[0])
        self.fs.errors()
        self.fs.rmdir(path)
        # Wait for the instrument to say it has finished, rather than
        # sleeping a guessed interval. A query arriving while dosFs is
        # still walking the directory is what raises event 250.
        self.fs.wait_done()
        events = self.fs.errors()
        self.fs.set_cwd(parent)
        gone = leaf not in [n.upper() for n in self.fs.dir()]
        self.fs.errors()
        if not gone:
            raise RuntimeError("RMDIR did not remove %s (events %s)"
                               % (path, events))
        return {"path": path, "events": events, "removed": True}

    # ------------------------------------------------- the system tab
    # Housekeeping the instrument keeps in non-volatile memory: its
    # clock, its calibration, its self tests, where hardcopies go, and
    # which options it believes it has. Every command here is out of
    # the TDS Family Programmer Manual 070-9876-00 except the option
    # words, which are not in any manual - see sys_option.

    #: Each row is (code, word, on value, description). The word is a
    #: constant in non-volatile memory and the value switches the
    #: option on; 0 always switches it off. Read out of the firmware:
    #: words 327680-327696 are one block, six longs then eleven words
    #: at 0x04000806, and the getter for each option query pushes its
    #: own index. 327691 is allocated but no firmware in the family
    #: ever reads it, so it is not offered.
    #:
    #: 1G is the odd one. It is not an option word at all: the firmware
    #: reports it when the acquisition board identity reads 14, and
    #: 131219 is the constant that overrides that identity when it is
    #: non-zero. Writing it therefore tells the instrument it has a
    #: different acquisition board, which is why it is last and why 0
    #: (hand the identity back to the hardware) is the way off.
    OPTION_WORDS = (
        ("1M", 327686, 1, "4M acquisition length"),
        ("05", 327687, 1, "Video trigger"),
        ("13", 327688, 1, "RS-232-C and Centronics interfaces"),
        ("2F", 327689, 1, "Advanced DSP math"),
        ("1F", 327690, 1, "Floppy disk drive"),
        ("2C", 327692, 1, "Communication Signal Analyzer"),
        ("3C", 327693, 1, "P6701B with system calibration"),
        ("4C", 327694, 1, "P6703B with system calibration"),
        ("2M", 327695, 1, "8M acquisition length"),
        ("1G", 131219, 14, "Limit sample rate to 1 GS/s"),
    )

    #: The options that are software alone. The rest need hardware and
    #: simply will not appear without it - and 3C and 4C raise a
    #: processor board fault when they are switched on without their
    #: calibration data.
    OPTION_SOFT = ("1M", "2F", "2C", "1G")

    def sys_read(self):
        """Everything the system tab shows, in one trip round the bus.

        One job rather than a dozen, because each one costs a round
        trip and the tab wants all of it at once. Anything the
        instrument refuses comes back as None rather than raising: an
        RS-232 setting on a scope without Option 13 is a blank field,
        not a failed job.
        """
        def ask(what):
            try:
                got = self.wfm.q(what)
            except IOError:
                return None
            got = str(got).strip() if got is not None else ""
            return got or None

        out = {"idn": ask("*IDN?"), "options": ask("ID?"),
               "date": ask("DATE?"), "time": ask("TIME?"),
               "clock": ask("DISPLAY:CLOCK?"),
               "port": ask("HARDCOPY:PORT?"),
               "format": ask("HARDCOPY:FORMAT?"),
               "layout": ask("HARDCOPY:LAYOUT?"),
               "rs232": {}}
        for key, what in (("baud", "RS232:BAUD?"),
                          ("parity", "RS232:PARITY?"),
                          ("stopbits", "RS232:STOPBITS?"),
                          ("hardflag", "RS232:HARDFLAGGING?"),
                          ("softflag", "RS232:SOFTFLAGGING?")):
            got = ask(what)
            if got is not None:
                out["rs232"][key] = got
        # Quoted strings come back quoted. The tab wants the date, not
        # the quotation marks round it.
        for key in ("idn", "options", "date", "time"):
            if out[key]:
                out[key] = out[key].strip('"')
        return out

    def sys_send(self, lines):
        """Send these settings, and say what the instrument refused.

        Refusals are collected rather than raised. A tab that sets six
        things and stops at the first one an older instrument does not
        have is worse than one that sets the other five and says so.
        """
        self.fs.errors()
        refused = []
        for line in lines:
            self.fs.inst.write(line)
            try:
                self.wfm.q("*OPC?")
            except IOError:
                pass
            for said in self.fs.errors():
                if not str(said).startswith("0,"):
                    refused.append((line, str(said)))
        return {"sent": len(lines), "refused": refused,
                "now": self.sys_read()}

    def sys_spc(self):
        """Signal path compensation: *CAL?, which answers 0 for pass.

        Minutes, not seconds, and nothing else runs on the bus while it
        does - the manual says so and the instrument means it. The
        timeout is pushed out and put back, because the ordinary one is
        set for reads that answer immediately.
        """
        was = getattr(self.fs.inst, "timeout", None)
        try:
            self.fs.inst.timeout = 600000
            said = str(self.wfm.q("*CAL?")).strip()
        finally:
            if was is not None:
                self.fs.inst.timeout = was
        return {"result": said, "passed": said.strip().startswith("0")}

    def sys_diag(self, area="ALL", most=240.0):
        """Extended diagnostics: select, execute, wait, then read the log.

        `DIAg:STATE EXECute` warm-boots the instrument, and the manual
        says so: it clears the Event Queue, the Input Queue and the
        status registers on the way through. The Input Queue is the one
        that matters here - a query sent straight after the command is
        thrown away by the boot and never answered by anybody.
        Measured on a 784D, reading DIAg:RESUlt:FLAg? immediately timed
        out after five minutes with the instrument sitting there
        finished and idle.

        The manual's own answer is a Service Request on the power-on
        event. This asks *IDN? every couple of seconds instead, which
        needs no status plumbing and says the same thing: when the
        instrument answers again, it has finished booting.
        """
        was = getattr(self.fs.inst, "timeout", None)
        flag = log = ""
        try:
            self.fs.inst.write("DIAG:SELECT:%s ALL" % area)
            self.fs.inst.write("DIAG:STATE EXECUTE")
            # Short, so each attempt while it is still down fails
            # quickly rather than eating the whole budget in one go.
            self.fs.inst.timeout = 3000
            back, end = False, time.time() + most
            while time.time() < end:
                time.sleep(2.0)
                try:
                    if str(self.wfm.q("*IDN?")).strip():
                        back = True
                        break
                except Exception:
                    continue
            if not back:
                return {"area": area, "flag": "", "log": "",
                        "passed": False, "back": False}
            self.fs.inst.timeout = 30000
            # Both come back as quoted strings. The quotation marks are
            # the bus's, not the instrument's opinion of its own health,
            # so they do not belong in the pane somebody reads.
            flag = str(self.wfm.q("DIAG:RESULT:FLAG?")).strip().strip('"')
            log = str(self.wfm.q("DIAG:RESULT:LOG?")).strip().strip('"')
        finally:
            if was is not None:
                self.fs.inst.timeout = was
        return {"area": area, "flag": flag, "log": log, "back": True,
                "passed": flag.upper().startswith("PASS")}

    def sys_secure(self):
        """TEKSecure: zero every reference waveform and every setup.

        The instrument raises 2285 for pass and 2286 for fail when it
        has finished, which is the only thing that says it worked - the
        command itself answers nothing.
        """
        self.fs.errors()
        was = getattr(self.fs.inst, "timeout", None)
        try:
            self.fs.inst.timeout = 300000
            self.fs.inst.write("TEKSECURE")
            self.wfm.q("*OPC?")
        finally:
            if was is not None:
                self.fs.inst.timeout = was
        said = [str(e) for e in self.fs.errors()]
        return {"events": said,
                "passed": any(e.startswith("2285") for e in said),
                "failed": any(e.startswith("2286") for e in said)}

    def sys_factory(self):
        """FACtory: back to the default setup.

        It leaves the GPIB address, the calibration constants and the
        protected user data alone - the manual is explicit about all
        three - so this is a setup reset, not a wipe.
        """
        self.fs.errors()
        self.fs.inst.write("FACTORY")
        self.wfm.q("*OPC?")
        return {"events": [str(e) for e in self.fs.errors()]}

    def sys_option(self, wants):
        """Switch options on or off, by writing their words directly.

        `wants` is a list of (address, 1 or 0).

        This is not in any Tektronix manual. The procedure - password,
        then one word per option - came from the bench, and the words
        are the ones in Worker.OPTION_WORDS. It only works with the
        NVRAM protection switch on the right side of the cabinet pushed
        to unprotected; with it protected the writes are accepted and
        nothing changes, which is why the reading back is the only
        honest way to report it and there is nothing to read back
        from. So this reports what it sent and what the instrument
        said, and leaves the verdict to the boot screen.
        """
        self.fs.errors()
        self.fs.inst.write("PASSWORD PITBULL")
        self.wfm.q("*OPC?")
        said = [str(e) for e in self.fs.errors()
                if not str(e).startswith("0,")]
        sent = []
        for word, value in wants:
            line = "WORDCONSTANT:ATPUT %d, %d" % (int(word), int(value))
            self.fs.inst.write(line)
            try:
                self.wfm.q("*OPC?")
            except IOError:
                pass
            said += [str(e) for e in self.fs.errors()
                     if not str(e).startswith("0,")]
            sent.append(line)
        return {"sent": sent, "refused": said, "options": self.sys_options()}

    def sys_options(self):
        """What the instrument says it has fitted, as a list."""
        try:
            got = str(self.wfm.q("ID?")).strip().strip('"')
        except IOError:
            return []
        # ID? is a comma-separated list with the options at the end,
        # after a field that starts "CF:". Anything before that is the
        # instrument saying what it is.
        return [p.strip() for p in got.split(",") if p.strip()]


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
    import struct
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
    import struct
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
    from tkinter import colorchooser, filedialog, font as tkfont, messagebox
    from tkinter import simpledialog, ttk

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
             "sformats": [], "errtext": "", "errlog": None, "idn": ""}

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
    tabs = ttk.Notebook(root)
    tabs.pack(fill="both", expand=True, padx=6, pady=(4, 0))
    filetab = ttk.Frame(tabs)
    tabs.add(filetab, text=_("Files"))
    named(filetab, "Files")

    nav = ttk.Frame(filetab)
    nav.pack(fill="x", padx=2, pady=(6, 2))
    btn_back = ttk.Button(nav, text="←", width=3,
                          command=lambda: do_back())
    btn_fwd = ttk.Button(nav, text="→", width=3,
                         command=lambda: do_forward())
    btn_up = ttk.Button(nav, text="↑", width=3,
                        command=lambda: do_up())
    for b in (btn_back, btn_fwd, btn_up):
        b.pack(side="left", padx=(0, 2))
    ent_path = ttk.Entry(nav)
    ent_path.pack(side="left", fill="x", expand=True, padx=(6, 0))

    # --------------------------------------------------------- command row
    # What to do with what is selected, then which instrument to do it on.
    # No address readout here: the title bar already names the connected
    # instrument in full.
    tb = ttk.Frame(filetab)
    tb.pack(fill="x", padx=2, pady=(0, 4))
    cmb_inst = ttk.Combobox(gbar, width=28, state="readonly", values=())
    btn_scan = ttk.Button(gbar, text=_("Scan"), padding=(10, 2),
                          command=lambda: do_rescan())
    lbl_inst = ttk.Label(gbar, text=_("Instrument"))
    btn_lang = ttk.Button(gbar, width=3, command=lambda: do_language())

    # ----------------------------------------------------------- content
    panes = ttk.PanedWindow(filetab, orient="horizontal")
    panes.pack(fill="both", expand=True, padx=2, pady=4)

    # The expander is left to the platform theme, which on Windows already
    # draws the chevron Explorer uses and turns it when the folder opens.
    # An earlier version replaced it with drawn glyphs under a custom
    # style; that element never picked up the open state, so it showed a
    # chevron that pointed right for ever. Replacing a working native
    # control with a worse copy of it is not an improvement.
    ttk.Style().configure("Treeview", rowheight=20)

    leftf = ttk.Frame(panes)
    tree = ttk.Treeview(leftf, show="tree", selectmode="browse")
    tsb = ttk.Scrollbar(leftf, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=tsb.set)
    tree.pack(side="left", fill="both", expand=True)
    tsb.pack(side="right", fill="y")
    panes.add(leftf, weight=1)

    listf = ttk.Frame(panes)
    cols = ("size", "type")
    # "extended" is the Explorer convention: click, ctrl-click to add,
    # shift-click for a run, ctrl-A for all.
    lst = ttk.Treeview(listf, columns=cols, show="tree headings",
                       selectmode="extended")
    lst.heading("#0", text=_("Name"))
    lst.heading("size", text=_("Size"))
    lst.heading("type", text=_("Type"))
    lst.column("#0", width=240)
    lst.column("size", width=90, anchor="e")
    lst.column("type", width=170)
    lsb = ttk.Scrollbar(listf, orient="vertical", command=lst.yview)
    lst.configure(yscrollcommand=lsb.set)
    lst.pack(side="left", fill="both", expand=True)
    lsb.pack(side="right", fill="y")
    panes.add(listf, weight=3)

    # ------------------------------------------------- the waveform tab
    # A trace is not a file: it comes from CURVE?, not from the disk, and
    # every TDS in the family can do it - including the ones with no
    # filesystem at all. So this tab works even when the other is dead.
    wavetab = ttk.Frame(tabs)
    tabs.add(wavetab, text=_("Waveforms"))
    named(wavetab, "Waveforms")

    # Eight buttons is more than one row holds in a wordy language:
    # measured, the same set is 1029 pixels in English and 1347 in
    # French. Rather than shortening the labels until they fit the
    # narrowest window anybody might use, the row wraps - which also
    # covers a window dragged narrow, and a language nobody has added
    # yet.
    wtop = ttk.Frame(wavetab)
    wtop.pack(fill="x", padx=2, pady=(6, 2))
    wrow1 = ttk.Frame(wtop)
    wrow1.pack(fill="x")
    wrow2 = ttk.Frame(wtop)

    btn_wget = ttk.Button(wtop, text=_("Get waveform"), padding=(10, 2),
                          command=lambda: do_wfm_get())
    btn_wsave = ttk.Button(wtop, text=_("Save waveform"), padding=(10, 2),
                           command=lambda: do_wfm_save())
    btn_wload = ttk.Button(wtop, text=_("Load waveform..."),
                           padding=(10, 2), command=lambda: do_wfm_load())
    btn_wsend = ttk.Button(wtop, text=_("Send to instrument..."),
                           padding=(10, 2), command=lambda: do_wfm_send())
    btn_wdel = ttk.Button(wtop, text=_("Delete waveform"), padding=(10, 2),
                          command=lambda: do_wfm_delete())
    WAVE_LEFT = (btn_wget, btn_wsave, btn_wload, btn_wsend, btn_wdel)
    WAVE_RIGHT = ()

    def flowing(host, row1, row2, left, right, key):
        """A toolbar that wraps to a second row when it runs out of room.

        Measured rather than split by hand: the same ten buttons fit
        across 1280 pixels in English and do not in French, and a split
        chosen once is wrong in some language or at some window size.

        Only repacked when the split actually changes: packing raises
        another <Configure>, and doing it unconditionally is a loop that
        never settles.
        """
        def lay(_evt=None):
            room = host.winfo_width()
            if room <= 1:
                return
            spare = room - sum(b.winfo_reqwidth() + 4 for b in right)
            used, wrapped, split = 0, False, []
            for button in left:
                need = button.winfo_reqwidth() + 4
                if used + need > spare and not wrapped and used:
                    wrapped, used = True, 0
                if wrapped:
                    split.append(button)
                used += need
            if split == state.get(key):
                return
            state[key] = list(split)
            for button in right:
                button.pack_forget()
                button.pack(in_=row1, side="right", padx=(4, 0))
            for button in left:
                button.pack_forget()
                button.pack(in_=row2 if button in split else row1,
                            side="left", padx=(0, 4))
            if split:
                row2.pack(fill="x", pady=(4, 0))
            else:
                row2.pack_forget()
        return lay

    flow_buttons = flowing(wtop, wrow1, wrow2, WAVE_LEFT, WAVE_RIGHT,
                           "wflow")
    wtop.bind("<Configure>", flow_buttons)

    wpanes = ttk.PanedWindow(wavetab, orient="horizontal")
    wpanes.pack(fill="both", expand=True, padx=2, pady=4)

    # Two lists, because they are two different things and the
    # difference decides what you get. A channel is acquired: reading it
    # takes a snapshot of what the instrument is looking at this second,
    # and reading it again gives something else. A reference is stored:
    # reading it gives the same samples every time until somebody
    # replaces them. One list of eleven names hid that distinction
    # completely.
    wleftf = ttk.Frame(wpanes)
    # Under the two lists, because the two lists are what it refreshes.
    # Up among the save buttons it read as another way of saying Get
    # waveform, which it is not: this asks the instrument which sources
    # it has, and Get waveform reads one of them. Packed before the
    # splitter so that the splitter's expand does not leave it nowhere
    # to go.
    btn_wscan = ttk.Button(wleftf, text=_("Refresh"), padding=(10, 2),
                           command=lambda: do_wfm_sources())
    btn_wscan.pack(side="bottom", anchor="w", pady=(4, 0))
    wsplit = ttk.PanedWindow(wleftf, orient="vertical")
    wsplit.pack(fill="both", expand=True)

    # A button beside each name, which is the whole of the selection:
    # filled means that source is chosen, and a chosen source is drawn
    # on the graticule as soon as there are samples for it. There used
    # to be a row highlight as well, and two ways of saying "this one"
    # disagreed with each other constantly - picking a button left the
    # highlight on the row before it, and the buttons at the top acted
    # on whichever of the two the code happened to ask. So the highlight
    # is gone and these are all there is.
    SHOWN, HIDDEN = "\u25c9", "\u25cb"

    def source_list(parent, rows):
        # selectmode="none" rather than a highlight nobody reads: with
        # the buttons carrying the selection, a second highlighted row
        # is a second answer to the same question.
        box = ttk.Treeview(parent, columns=("name",), show="tree",
                           selectmode="none", height=rows)
        # The first column carries the tree's own indent as well as the
        # button, so it needs more room than the glyph alone suggests.
        box.column("#0", width=34, minwidth=34, stretch=False,
                   anchor="center")
        box.column("name", width=150, anchor="w")
        box.tag_configure("off", foreground="#9a9a9a")
        box.bind("<Double-Button-1>",
                 lambda e, b=box: do_wfm_toggle(b.identify_row(e.y)))
        box.bind("<Button-1>", lambda e, b=box: on_click_row(b, e))
        return box

    def fit_notes(evt):
        """Wrap the two notes to the pane, not to a guess.

        A label clips rather than wraps when it runs out of room, so a
        fixed wraplength is either too wide for a narrow pane or wastes
        half of a wide one. The splitter can be dragged, so it is
        measured whenever it moves.
        """
        room = max(80, evt.width - 16)
        lbl_wlive.config(wraplength=room)
        lbl_wrnote.config(wraplength=room)


    wlivef = ttk.Frame(wsplit)
    lbl_wsrc = ttk.Label(wlivef, text=_("Live channels"))
    says(lbl_wsrc, "Live channels")
    lbl_wsrc.pack(anchor="w")
    lbl_wlive = ttk.Label(wlivef, foreground="#555", wraplength=190,
                          justify="left")
    says(lbl_wlive, "A snapshot of what the instrument is acquiring now. "
                    "A greyed name is switched off there; choosing it "
                    "switches it on. Double-click a name to switch it off "
                    "again.")
    lbl_wlive.pack(anchor="w", pady=(0, 2))
    # Several at once, so more than one trace can be put on the
    # graticule together, the way they sit on the instrument's screen.
    wsrc = source_list(wlivef, 6)
    wsrc.pack(fill="both", expand=True)
    wsplit.add(wlivef, weight=1)

    wreff = ttk.Frame(wsplit)
    lbl_wref = ttk.Label(wreff, text=_("Stored waveforms"))
    says(lbl_wref, "Stored waveforms")
    lbl_wref.pack(anchor="w", pady=(6, 0))
    lbl_wrnote = ttk.Label(wreff, foreground="#555", wraplength=190,
                           justify="left")
    says(lbl_wrnote, "Held in the instrument's memory. Pick one or more, "
                     "then load a file into them.")
    lbl_wrnote.pack(anchor="w", pady=(0, 2))
    # Several at once, so one file can be loaded into more than one
    # reference and all of them sent in a single go.
    wref = source_list(wreff, 5)
    wref.pack(fill="both", expand=True)
    wsplit.add(wreff, weight=1)
    # Bound after both notes exist: a <Configure> can arrive during
    # construction, and a callback that names a widget not yet made
    # raises from inside Tk's own event loop.
    wleftf.bind("<Configure>", fit_notes)
    wpanes.add(wleftf, weight=1)

    wrightf = ttk.Frame(wpanes)
    # A strip above the plot showing the whole record with the part on
    # the graticule marked out, which is what the instrument's own
    # record view does. Without it, a zoomed-in trace gives no clue
    # where in the capture it came from.
    over = tk.Canvas(wrightf, height=44, highlightthickness=1,
                     background=tds_wfm.DEFAULT_COLOURS["background"],
                     highlightbackground=EDGE)
    over.pack(fill="x")
    plot = tk.Canvas(wrightf, background=tds_wfm.DEFAULT_COLOURS["background"],
                     highlightthickness=1, highlightbackground=EDGE)
    plot.pack(fill="both", expand=True, pady=(2, 0))
    wscroll = ttk.Scrollbar(wrightf, orient="horizontal")
    wscroll.pack(fill="x")

    wzoom = ttk.Frame(wrightf)
    wzoom.pack(fill="x", pady=(4, 0))
    lbl_wtime = ttk.Label(wzoom, text=_("Time"))
    says(lbl_wtime, "Time")
    lbl_wtime.pack(side="left", padx=(0, 4))
    btn_wtout = ttk.Button(wzoom, text="−", width=3,
                           command=lambda: do_zoom("time", -1))
    btn_wtout.pack(side="left")
    btn_wtin = ttk.Button(wzoom, text="+", width=3,
                          command=lambda: do_zoom("time", 1))
    btn_wtin.pack(side="left", padx=(2, 12))
    lbl_wamp = ttk.Label(wzoom, text=_("Amplitude"))
    says(lbl_wamp, "Amplitude")
    lbl_wamp.pack(side="left", padx=(0, 4))
    btn_wvout = ttk.Button(wzoom, text="−", width=3,
                           command=lambda: do_zoom("volts", -1))
    btn_wvout.pack(side="left")
    btn_wvin = ttk.Button(wzoom, text="+", width=3,
                          command=lambda: do_zoom("volts", 1))
    btn_wvin.pack(side="left", padx=(2, 12))
    btn_wwhole = ttk.Button(wzoom, text=_("Whole record"), padding=(8, 1),
                            command=lambda: do_zoom("reset", 0))
    btn_wwhole.pack(side="left")
    winfo = ttk.Label(wrightf, text="", anchor="w")
    winfo.pack(fill="x", pady=(4, 0))
    wpanes.add(wrightf, weight=3)

    def plot_font():
        """The font the canvas draws text in, for measuring it."""
        if state.get("plotfont") is None:
            state["plotfont"] = tkfont.nametofont("TkDefaultFont")
        return state["plotfont"]

    def preset_name():
        """Which named scheme the current colours are, if any.

        Matched by value rather than remembered by name, which is what
        the dialog's own box does: change one colour by hand and this
        stops saying Instrument, correctly.
        """
        for name, cols in colour_presets().items():
            if cols == tds_wfm.scheme(state.get("colours")):
                return name
        return ""

    def plot_colours(wave=None):
        """The scheme the plot is actually drawn in.

        On the Instrument preset, and only there, the trace takes the
        colour the instrument draws that source in - read from its own
        palette, so a REF really is the colour the REFs are on the
        screen. Any other scheme is the user's choice and is left alone;
        so is an instrument that has no colours to report, which is
        every monochrome one.
        """
        pick = tds_wfm.scheme(state.get("colours"))
        source = getattr(wave, "source", None) if wave else None
        own = (state.get("icolours") or {}).get(str(source or "").upper())
        if own and preset_name() == "Instrument":
            pick["trace"] = own
        return pick

    def trace_colour(wave, pick=None):
        """What to draw this particular trace in.

        With several on the graticule at once they cannot all be the
        scheme's one colour, or there is no telling them apart. On the
        Instrument preset each takes the instrument's own; on any other
        scheme the first keeps the scheme's colour and the rest are
        shifted round the hue circle, which keeps a chosen scheme
        recognisable while still separating the traces.
        """
        pick = pick or tds_wfm.scheme(state.get("colours"))
        own = (state.get("icolours") or {}).get(
            str(getattr(wave, "source", "") or "").upper())
        if own and preset_name() == "Instrument":
            return own
        order = [w.source for w in shown_waves()]
        at = order.index(wave.source) if wave.source in order else 0
        return tds_wfm.shifted_hue(pick["trace"], at)

    def shown_waves():
        """Every trace on the graticule, the primary one first."""
        return [w for w in (state.get("waves") or []) if w is not None]

    def traces_by_name():
        """Colours for the PNG export, which has no colour scheme of
        its own to work out."""
        return dict((w.source, trace_colour(w)) for w in shown_waves())

    def draw_plot(_evt=None):
        """Redraw the held trace to fit the canvas.

        Vector, not a bitmap, so it stays crisp when the window is
        resized - but the geometry comes from the same function the PNG
        export uses, so what is saved is what was on screen.
        """
        plot.delete("all")
        wave = state.get("wave")
        pick = plot_colours(wave)
        plot.config(background=pick["background"])
        w = max(plot.winfo_width(), 60)
        h = max(plot.winfo_height(), 40)
        pad = 34
        left, top, right, bottom = tds_wfm.plot_frame(w, h, pad)
        # Every line comes from tds_wfm.graticule(), which the PNG export
        # draws from as well - so the picture that is saved is the
        # picture that was on screen, rather than two drawings that
        # agree today and drift apart tomorrow.
        for element, x0, y0, x1, y1, thick in tds_wfm.graticule(w, h, pad):
            plot.create_line(x0, y0, x1, y1, fill=pick[element],
                             width=thick)
        if not wave:
            # In a box. Text alone lands on top of the graticule and the
            # centre cross runs straight through the middle of it.
            words = _("No waveform. Pick a source and press Get waveform.")
            # Centred on the graticule rather than on the pane. The two
            # usually agree now that plot_frame centres, but a pane too
            # narrow to keep the markers' room and still centre is the
            # case where they do not.
            label = plot.create_text((left + right) / 2.0,
                                     (top + bottom) / 2.0,
                                     fill=pick["label"], text=words,
                                     tags="empty")
            x0, y0, x1, y1 = plot.bbox(label)
            box = plot.create_rectangle(x0 - 12, y0 - 8, x1 + 12, y1 + 8,
                                        fill=pick["background"],
                                        outline=pick["label"],
                                        tags="empty")
            plot.tag_lower(box, label)
            # The strip above and the scrollbar describe the same
            # nothing. Returning before they are redrawn left the last
            # trace drawn up there after every trace had been taken off.
            draw_over()
            set_scroll()
            return
        taken = []
        for one in shown_waves():
            colour = trace_colour(one, pick)
            xy, _bounds = tds_wfm.plot_geometry(one, w, h, pad,
                                                state.get("view"))
            if len(xy) > 1:
                flat = []
                for x, y in xy:
                    flat += [x, y]
                # Tagged, so a test can ask whether the *trace* was
                # drawn. Asking whether anything was drawn is not the
                # same question: the graticule is always there and
                # answers yes regardless.
                plot.create_line(*flat, fill=colour, width=1, tags="trace")
            # A marker at this trace's own zero volts, filled in its own
            # colour with the name on it in black - which is how an
            # instrument tells its traces apart, and the only thing on
            # the plot that says which of four channels this is. Just
            # inside the left edge of the graticule, where the
            # instrument puts its own.
            name = str(one.label or one.source or "")[:8]
            if not name:
                continue
            # Measured in the theme's own font: a ttk widget has no
            # -font of its own, and a marker sized by counting
            # characters is too small for "MATH1" and too big for "R1".
            wide = plot_font().measure(name) + 10 + 6
            zero = state["view"].y_of_volts(one, 0.0, top, bottom)
            zero = min(bottom - 7, max(top + 7, zero))
            # Outside the graticule, where it covers no signal - the
            # room for it is kept clear by plot_frame. Channels all
            # sitting at position zero would put their markers in the
            # same place and only the last drawn would be readable, so
            # any that collide are stepped aside by the same rule the
            # saved picture uses. The span kept is what the canvas
            # reports for a polygon, which is two pixels wider each
            # side than the shape asked for.
            x0 = tds_wfm.marker_place(taken, left, zero, wide, 13, 2,
                                      right=right, room=w)
            taken.append((x0 - 2, zero, x0 + wide + 2))
            # Pointed towards the graticule whichever margin it ended up
            # in: one that had to go to the right of the frame points
            # left, at the trace, rather than out at the window edge.
            facing = tds_wfm.marker_facing(x0, right)
            spot = tds_wfm.marker_shape(x0, zero, wide, facing=facing)
            # Tagged with the source as well, so a test can ask where
            # *this* trace's marker is rather than where the first one
            # is - which is the same place only when every channel
            # happens to sit at position zero.
            mine = ("marker", "marker:%s" % (wave_name(one) or name))
            plot.create_polygon(*spot, fill=colour, outline=colour,
                                tags=mine)
            plot.create_text(x0 + 5 + (6 if facing < 0 else 0), zero,
                             anchor="w", fill="#000000",
                             text=name, tags=mine)
        # The per-division reading under the graticule, and nothing at
        # the corners: the values that used to sit inside the frame said
        # what this says, in a form no instrument uses, and landed on
        # top of the trace while doing it.
        plot.create_text((left + right) / 2.0, bottom + 4, anchor="n",
                         fill=pick["label"], text=wave_scales(wave),
                         tags="scales")
        draw_over()
        set_scroll()

    def draw_over():
        """The strip above the plot: the whole record, window marked.

        Drawn from the same samples at a fixed vertical scale - it is a
        map of the capture, not a second plot, so zooming the main view
        must not change what it looks like.
        """
        over.delete("all")
        wave = state.get("wave")
        pick = plot_colours(wave)
        over.config(background=pick["background"])
        w = max(over.winfo_width(), 60)
        h = max(over.winfo_height(), 20)
        view = state.get("view")
        if not wave or view is None:
            return
        # Every trace, each laid out by *time* over the whole span the
        # set covers, so this strip and the window marked on it are
        # measuring the same axis. Drawing only the front trace across
        # the full width said a 500-point channel and a 5000-point
        # reference were the same length, and then marked a window on
        # that which had nothing to do with either.
        whole_span = max(1e-15, view.full_span)
        for one in shown_waves():
            levels = one.levels()
            spots = one.points()
            if len(levels) < 2 or len(spots) < 2:
                continue
            a = (spots[0][0] - view.full_first) / whole_span * w
            b = (spots[-1][0] - view.full_first) / whole_span * w
            wide = max(2.0, b - a)
            # Drawn here rather than through plot_geometry, which keeps
            # a 10:8 graticule square and would fit a 110-pixel picture
            # into the middle of a 900-pixel strip. This is a map of the
            # record, not a plot: it uses the width the record covers
            # and whatever height it has, and the shape of it does not
            # have to mean anything. Each trace is scaled to its own
            # peak, so a small signal beside a large one is still
            # visible rather than a flat line.
            reach = max(1.0, max(abs(min(levels)), abs(max(levels))))
            columns = max(2, int(wide))
            flat = []
            for column in range(columns):
                i = int(len(levels) * column / float(columns))
                j = max(i + 1,
                        int(len(levels) * (column + 1) / float(columns)))
                chunk = levels[i:j]
                x = a + wide * (column + 0.5) / columns
                for level in (max(chunk), min(chunk)):
                    flat += [x, h / 2.0 - (level / reach) * (h / 2.0 - 3)]
            if len(flat) > 3:
                over.create_line(*flat, fill=trace_colour(one, pick),
                                 width=1, tags="overtrace")
        start, end = view.fractions()
        x0, x1 = start * w, end * w
        if x1 - x0 < 3:                 # always grabbable
            middle = (x0 + x1) / 2.0
            x0, x1 = middle - 1.5, middle + 1.5
        over.create_rectangle(x0, 0, x1, h, outline=pick["label"],
                              width=2, tags="window")
        # Shaded either side rather than inside, so the marked part is
        # the part you can see clearly.
        for a, b in ((0, x0), (x1, w)):
            if b > a:
                over.create_rectangle(a, 0, b, h, fill=pick["background"],
                                      outline="", stipple="gray50",
                                      tags="shade")

    def set_scroll():
        """Point the scrollbar at the part of the record on show."""
        view = state.get("view")
        if view is None:
            wscroll.set(0.0, 1.0)
            return
        start, end = view.fractions()
        wscroll.set(start, end)

    plot.bind("<Configure>", draw_plot)
    over.bind("<Configure>", lambda e: (draw_over(), set_scroll()))

    # ------------------------------------------------ moving the view
    # Everything here works in the record's own coordinates - samples
    # across and digitiser counts down - so a drag means the same thing
    # whatever size the window is, and the view survives a resize.

    def plot_frame_now():
        w = max(plot.winfo_width(), 60)
        h = max(plot.winfo_height(), 40)
        return tds_wfm.plot_frame(w, h, 34)

    def at_fractions(x, y):
        """Where a pixel is in the frame, as fractions across and down."""
        left, top, right, bottom = plot_frame_now()
        wide = max(1.0, right - left)
        tall = max(1.0, bottom - top)
        return ((x - left) / wide, (y - top) / tall)

    def time_at(x):
        """Which moment of the record a pixel is over, in seconds."""
        view = state.get("view")
        across, _down = at_fractions(x, 0)
        return view.first + view.span * across

    def division_at(y):
        """How far above the centre line a pixel is, in divisions.

        Screen divisions rather than a trace's own counts: with several
        traces on the graticule at different volts a division, a pixel
        is not one number of counts, but it is always one place on the
        screen.
        """
        _across, down = at_fractions(0, y)
        return (0.5 - down) * tds_wfm.DIVS_Y

    def do_zoom(what, direction, across=0.5, down=0.5):
        view = state.get("view")
        if view is None:
            return
        if what == "reset":
            view.reset()
        elif what == "time":
            view.zoom_time(direction, across)
        else:
            view.zoom_volts(direction, down)
        draw_plot()

    def on_plot_press(evt):
        """Start a drag: a pan normally, a zoom window with Shift."""
        if state.get("view") is None:
            return
        state["drag"] = {"x": evt.x, "y": evt.y,
                         "from_time": time_at(evt.x),
                         "from_div": division_at(evt.y),
                         # Taking hold of a trace's marker moves that
                         # trace and nothing else, the way the position
                         # knob on the instrument moves the selected
                         # channel. Anywhere else moves the view. The
                         # marker is what is grabbed because it is a
                         # fixed target with the name on it; the trace
                         # itself is a line one pixel wide that moves
                         # whenever the view does.
                         "only": marker_under(evt.x, evt.y),
                         # Where every trace stood when the drag began,
                         # so the vertical move can be worked out from
                         # the start rather than from the last event -
                         # which is what makes the snap onto the centre
                         # line escapable. See PlotView.snap_middle.
                         "held": dict(state["view"].shifts),
                         "box": bool(evt.state & 0x0001)}   # Shift

    def on_plot_move(evt):
        drag = state.get("drag")
        view = state.get("view")
        if not drag or view is None:
            return
        if drag["box"]:
            plot.delete("band")
            pick = plot_colours(state.get("wave"))
            plot.create_rectangle(drag["x"], drag["y"], evt.x, evt.y,
                                  outline=pick["label"], dash=(3, 2),
                                  tags="band")
            return
        # Panning: whatever was under the pointer stays under it. The
        # vertical sign is the one that was wrong - dragging downwards
        # raised the traces, which is the opposite of taking hold of
        # something and moving it.
        #
        # Vertically it moves only the trace whose marker the drag
        # started on, so channels can be moved apart one at a time the
        # way they are on the instrument. A drag that started anywhere
        # else moves the whole view, which is the sensible thing when no
        # trace in particular was taken hold of.
        picked = [x for x in shown_waves()
                  if wave_name(x) == drag.get("only")]
        # Across, always: the window clamps at the ends of the record,
        # so it has to follow the pointer one event at a time.
        view.pan(seconds=drag["from_time"] - time_at(evt.x))
        drag["from_time"] = time_at(evt.x)
        if picked:
            # Down, measured from where the drag began. A trace whose
            # zero comes close to the centre line snaps onto it, and a
            # snap applied to a running total can never be dragged back
            # off: a slow drag moves a pixel or two between events,
            # which is less than the snap's own tolerance, so it would
            # be pulled straight back every time.
            moved = division_at(evt.y) - drag["from_div"]
            for one in picked:
                view.shifts[one.source] = (drag["held"].get(one.source, 0.0)
                                           + moved)
            view.clamp()
            for one in picked:
                view.snap_middle(one)
        else:
            view.pan(divisions=division_at(evt.y) - drag["from_div"])
            drag["from_div"] = division_at(evt.y)
        draw_plot()

    def on_plot_release(evt):
        drag = state.pop("drag", None)
        view = state.get("view")
        if not drag or view is None:
            return
        plot.delete("band")
        if not drag["box"]:
            return
        # A drag of a pixel or two is a click that shook, not a window.
        if abs(evt.x - drag["x"]) < 6 and abs(evt.y - drag["y"]) < 6:
            return
        view.show_to(drag["from_time"], time_at(evt.x),
                     drag["from_div"], division_at(evt.y))
        draw_plot()

    def on_plot_wheel(evt, direction=None):
        """The wheel zooms about the pointer; Shift zooms amplitude."""
        if state.get("view") is None:
            return
        if direction is None:
            direction = 1 if evt.delta > 0 else -1
        across, down = at_fractions(evt.x, evt.y)
        do_zoom("volts" if (evt.state & 0x0001) else "time",
                direction, across, down)

    # How near an edge of the marked window counts as taking hold of it.
    OVER_GRAB = 5

    def over_time(x):
        """The moment in the record a point along the strip stands for."""
        view = state["view"]
        w = max(over.winfo_width(), 60)
        return view.full_first + (x / float(w)) * view.full_span

    def over_edge(x):
        """Which edge of the marked window is under this point, if any."""
        view = state.get("view")
        if view is None:
            return None
        w = max(over.winfo_width(), 60)
        start, end = view.fractions()
        for name, at in (("first", start * w), ("last", end * w)):
            if abs(x - at) <= OVER_GRAB:
                return name
        return None

    def on_over_press(evt):
        """Take hold of an edge, or move the whole window here."""
        if state.get("view") is None:
            return
        state["overdrag"] = over_edge(evt.x)
        on_over_move(evt)

    def on_over_move(evt):
        """Drag: resize from the edge that was taken, or move the window."""
        view = state.get("view")
        if view is None:
            return
        edge = state.get("overdrag")
        if edge is None:
            view.first = over_time(evt.x) - view.span / 2.0
            view.clamp()
        else:
            # The end that was not taken hold of stays where it is.
            other = view.first + view.span if edge == "first" else view.first
            view.stretch_to(over_time(evt.x), other,
                            "last" if edge == "first" else "first")
        draw_plot()

    def on_over_hover(evt):
        """A resize pointer over an edge, so it can be found at all."""
        over.config(cursor="sb_h_double_arrow" if over_edge(evt.x) else "")

    def on_over_wheel(evt, direction=None):
        """The wheel zooms here as well, about the moment under it."""
        view = state.get("view")
        if view is None:
            return
        if direction is None:
            direction = 1 if evt.delta > 0 else -1
        at = ((over_time(evt.x) - view.first) / view.span
              if view.span else 0.5)
        do_zoom("time", direction, at)

    def on_scroll(*args):
        """The scrollbar's own protocol, in the record's coordinates."""
        view = state.get("view")
        if view is None:
            return
        if args and args[0] == "moveto":
            view.scroll_to(float(args[1]))
        elif args and args[0] == "scroll":
            step = float(args[1])
            view.first += step * view.span * (1.0 if args[2] == "pages"
                                              else 0.1)
            view.clamp()
        draw_plot()

    wscroll.config(command=on_scroll)
    plot.bind("<ButtonPress-1>", on_plot_press)
    plot.bind("<B1-Motion>", on_plot_move)
    plot.bind("<ButtonRelease-1>", on_plot_release)
    plot.bind("<MouseWheel>", on_plot_wheel)
    # X11 sends buttons 4 and 5 rather than a wheel event.
    plot.bind("<Button-4>", lambda e: on_plot_wheel(e, 1))
    plot.bind("<Button-5>", lambda e: on_plot_wheel(e, -1))
    over.bind("<ButtonPress-1>", on_over_press)
    over.bind("<B1-Motion>", on_over_move)
    over.bind("<ButtonRelease-1>", lambda e: state.pop("overdrag", None))
    over.bind("<Motion>", on_over_hover)
    over.bind("<MouseWheel>", on_over_wheel)
    over.bind("<Button-4>", lambda e: on_over_wheel(e, 1))
    over.bind("<Button-5>", lambda e: on_over_wheel(e, -1))

    def marker_under(x, y):
        """Which trace's marker is at this point, if any.

        Aimed at the marker rather than at the trace, because a marker
        is a fixed target with a name on it and a trace is a line one
        pixel wide that moves whenever the view does.
        """
        under = plot.find_overlapping(x - 2, y - 2, x + 2, y + 2)
        named = [t.split(":", 1)[1] for i in under
                 for t in plot.gettags(i) if t.startswith("marker:")]
        return named[0] if named else None

    def on_plot_menu(evt):
        """Right-click on a marker: what to do with that trace."""
        name = marker_under(evt.x, evt.y)
        if not name:
            return
        menu = tk.Menu(root, tearoff=0)
        menu.add_command(label=_("Hide %s") % name,
                         command=lambda n=name: do_wfm_shown(n))
        menu.add_command(
            label=_("Show only %s") % name,
            command=lambda n=name: show_waves(
                [x for x in shown_waves() if wave_name(x) == n],
                keep_view=True))
        try:
            menu.tk_popup(evt.x_root, evt.y_root)
        finally:
            menu.grab_release()

    plot.bind("<Button-3>", on_plot_menu)

    def wave_scales(wave):
        """The per-division reading, the way an instrument prints it.

        Under the graticule rather than in the sentence below the plot:
        it is a property of the picture, it belongs with the picture,
        and the sentence was already long enough to be cut off by the
        edge of the window.
        """
        got = wave.measures() if wave else None
        if not got:
            return ""
        view = state.get("view")
        if view is not None:
            # What a division is worth *now*, which is what the reading
            # has to say while the view is being zoomed and panned.
            got = dict(got)
            got["vdiv"] = tds_wfm.eng(view.volts_per_div, wave.yunit)
            got["tdiv"] = tds_wfm.eng(view.seconds_per_div, wave.xunit)
            got["wider"] = not view.whole or got["wider"]
        if got.get("wider"):
            # The record is longer than the ten divisions the instrument
            # draws, so what a division means here is not what it means
            # on the scope. Both are said rather than one of them being
            # quietly wrong.
            return _("%(vdiv)s/div   %(tdiv)s/div   (the instrument is "
                     "set to %(sdiv)s/div)") % got
        return _("%(vdiv)s/div   %(tdiv)s/div") % got

    def wave_summary(wave):
        """The line under the plot, in the user's language.

        Assembled here rather than in tds_wfm, which has no gettext and
        should not have one: a sentence built inside a module that knows
        nothing about languages cannot be translated by anybody.
        """
        got = wave.measures() if wave else None
        if not got:
            return _("%s: nothing") % (wave.source if wave else "")
        return _("%(points)d points over %(span)s, %(low)s to "
                 "%(high)s") % got

    def wave_name(one):
        """Which source in the two lists a trace stands for.

        Normally where it came from. A file loaded from the PC and held
        against a reference is different: its `source` is the file's own
        name - an ISF says "Ch1" whatever the file is called - and the
        thing it stands for in the lists is the reference it is going
        to. Keying off `source` alone left a staged reference drawn on
        the graticule with its button showing it was not.
        """
        return getattr(one, "label", None) or one.source

    def show_wave(wave, keep_view=False):
        """One waveform in the pane, on its own."""
        return show_waves([wave] if wave else [], keep_view)

    def show_waves(waves, keep_view=False):
        """Put traces in the pane, with a fresh view of them.

        The first is the primary one: it is what Save, Send and the
        per-division reading act on, the way a scope's readouts follow
        the selected channel.

        `keep_view` is for redrawing the same traces - a language
        change, say - where throwing the zoom away would be a surprise.
        """
        waves = [w for w in (waves or []) if w is not None]
        # One trace per source. Two copies of the same one is never
        # wanted and is hard to see on screen - the second is drawn
        # exactly on top of the first - but it doubles the work and it
        # puts two markers in the same place.
        seen, unique = set(), []
        for one in waves:
            if wave_name(one) not in seen:
                seen.add(wave_name(one))
                unique.append(one)
        waves = unique
        state["waves"] = waves
        # The buttons in the two lists follow what is drawn, plus
        # anything chosen that has nothing to draw yet - an empty
        # reference picked so a file can be loaded into it, which has no
        # samples and still has to be selectable.
        drawn = [wave_name(w) for w in waves]
        got = state.get("fetched") or {}
        state["wticked"] = wfm_order(
            drawn + [n for n in (state.get("wticked") or ())
                     if n not in drawn and got.get(n) is None])
        # Kept by name, so a trace taken off the graticule can be put
        # back without asking the instrument for it again.
        for one in waves:
            state.setdefault("fetched", {})[wave_name(one)] = one
        state["wave"] = waves[0] if waves else None
        if not keep_view or state.get("view") is None or not waves:
            # A fresh view for a fresh capture: the whole record at the
            # instrument's own scale, which is where every look at a
            # trace ought to start.
            state["view"] = tds_wfm.PlotView(waves) if waves else None
        else:
            # Same view, different traces. What "the whole record" means
            # has changed and has to be measured again, or the window
            # goes on describing a set that is no longer on the
            # graticule - which showed as a full strip above a plot with
            # a tenth of the trace on it.
            state["view"].remeasure(waves)
        state.pop("drag", None)
        draw_plot()
        show_ticks()
        wfm_buttons()
        first = state["wave"]
        if not first:
            winfo.config(text="")
        else:
            # Names and this program's own sentence, in this program's
            # language. It used to lead with the instrument's WFID -
            # "Ch1, DC coupling, 100.0mVolts/div, 500.0us/div, 500
            # points, Sample mode" - which is the instrument's words and
            # is therefore always English, and reads as though half the
            # line had simply not been translated. The WFID is still on
            # the saved picture and in the ISF header, which is where it
            # belongs.
            winfo.config(text=_("%(names)s  -  %(what)s")
                         % {"names": ", ".join(w.label or w.source
                                               for w in waves),
                            "what": wave_summary(first)})

    # --------------------------------------------------- the screen tab
    # ------------------------------------------------------------ masks
    # A mask on these instruments is eight closed polygons in percent of
    # the graticule, and nothing else: no mask file, no SAVE:MASK, and
    # nothing about masks in a saved setup. Measured on all three - see
    # INSTRUMENT-NOTES - and it is why this tab keeps the library on the
    # PC and on the instrument's own disk rather than pretending the
    # instrument has one.
    masktab = ttk.Frame(tabs)
    tabs.add(masktab, text=_("Masks"))
    named(masktab, "Masks")

    # One row that becomes two when it has to, the way the waveform
    # tab's does. A split chosen by hand was wrong as soon as a button
    # was added or the language changed - "Enregistrer la vue..." was
    # clipped by 23px in French - so it is measured instead.
    mtop = ttk.Frame(masktab)
    mtop.pack(fill="x", padx=2, pady=(6, 2))
    mrow1 = ttk.Frame(mtop)
    mrow1.pack(fill="x")
    mtop2 = ttk.Frame(mtop)
    btn_mnew = ttk.Button(mtop, text=_("New mask"), padding=(10, 2),
                          command=lambda: do_msk_new())
    btn_msave = ttk.Button(mtop, text=_("Save mask as..."), padding=(10, 2),
                           command=lambda: do_msk_save())
    btn_mdel = ttk.Button(mtop, text=_("Delete mask"), padding=(10, 2),
                          command=lambda: do_msk_delete())
    btn_mtrace = ttk.Button(mtop, text=_("Load trace..."), padding=(10, 2),
                            command=lambda: do_msk_trace())
    btn_mgrab = ttk.Button(mtop, text=_("Capture screen"), padding=(10, 2),
                           command=lambda: do_msk_behind())
    btn_mdrop = ttk.Button(mtop, text=_("Clear screenshot"), padding=(10, 2),
                           command=lambda: do_msk_drop_shot())
    # A mask is percent of the graticule and says nothing about the
    # signal it was drawn for; the setup beside it is the other half of
    # the test, which is why Tektronix shipped one with every mask.
    btn_msetup = ttk.Button(mtop, text=_("Save setup..."), padding=(10, 2),
                            command=lambda: do_msk_setup())
    btn_mrefresh = ttk.Button(mtop, text=_("Refresh"), padding=(10, 2),
                              command=lambda: do_msk_scan())
    btn_mview = ttk.Button(mtop, text=_("Save image..."), padding=(10, 2),
                           command=lambda: do_msk_view_save())
    # The library and the picture, in that order: what a mask is, then
    # what is behind it, then what comes out. Refresh sits at the right,
    # where the waveform tab keeps it, so the two tabs are not laid out
    # differently for no reason.
    MASK_LEFT = (btn_mnew, btn_msave, btn_msetup, btn_mdel, btn_mtrace,
                 btn_mgrab, btn_mdrop, btn_mview)
    MASK_RIGHT = (btn_mrefresh,)
    mask_flow = flowing(mtop, mrow1, mtop2, MASK_LEFT, MASK_RIGHT, "mflow")
    mtop.bind("<Configure>", mask_flow)

    def reflow():
        """Lay both toolbars out again, from scratch.

        Changing the language changes how wide every label is without
        resizing anything, so no <Configure> arrives and the rows keep
        the widths the old words needed - which is how a French label
        came to be twelve pixels wider than the button holding it. The
        remembered split is dropped first, or an unchanged split makes
        the layout return without packing anything.
        """
        state.pop("wflow", None)
        state.pop("mflow", None)
        state.pop("lflow", None)
        flow_buttons()
        mask_flow()
        lim_flow()
        root.update_idletasks()

    mpanes = ttk.PanedWindow(masktab, orient="horizontal")
    mpanes.pack(fill="both", expand=True, padx=2, pady=4)

    # Two lists down the left, because there are two places a mask can
    # be. On the PC it is a file you own. In the instrument it is live
    # setup state - eight segments, one mask at a time - which is the
    # only thing the hardware actually has. There used to be a third,
    # for masks kept on the instrument's own disk, and it went: this
    # generation has no mask file format, so a mask left there was a
    # file the instrument itself could not read, and the library on
    # this computer had already done the job.
    mleftf = ttk.Frame(mpanes)
    msplit = ttk.PanedWindow(mleftf, orient="vertical")
    msplit.pack(fill="both", expand=True)

    mpcf = ttk.Frame(msplit)
    lbl_mpc = ttk.Label(mpcf, text=_("Masks on this computer"))
    says(lbl_mpc, "Masks on this computer")
    lbl_mpc.pack(anchor="w")
    lbl_mpcnote = ttk.Label(mpcf, foreground="#555", wraplength=190,
                            justify="left")
    says(lbl_mpcnote, "Double click to edit.")
    lbl_mpcnote.pack(anchor="w", pady=(0, 2))
    mpc = ttk.Treeview(mpcf, columns=("name", "signal", "what"),
                       show="headings", selectmode="extended", height=6)
    mpc.heading("name", text=_("Name"),
                command=lambda: do_msk_sort(mpc, "name"))
    mpc.heading("signal", text=_("Signal"),
                command=lambda: do_msk_sort(mpc, "signal"))
    mpc.heading("what", text=_("Shape"),
                command=lambda: do_msk_sort(mpc, "what"))
    mpc.column("name", width=110, anchor="w")
    mpc.column("signal", width=85, anchor="w")
    mpc.column("what", width=90, anchor="w")
    mpc_bar = ttk.Scrollbar(mpcf, orient="vertical",
                           command=mpc.yview)
    mpc.configure(yscrollcommand=mpc_bar.set)
    mpc_bar.pack(side="right", fill="y")
    mpc.pack(side="left", fill="both", expand=True)
    msplit.add(mpcf, weight=2)

    mlivef = ttk.Frame(msplit)
    # The two directions, between the two lists they move a mask
    # between: down to the instrument, up from it. Beside them, the one
    # thing that is not a transfer - emptying the instrument's segments.
    marrows = ttk.Frame(mlivef)
    marrows.pack(fill="x", pady=(6, 2))
    # The arrows on the middle of the line between the two lists, since
    # that is the line they move a mask across. The other two are not
    # directions, so they sit under them rather than beside them.
    mcentre = ttk.Frame(marrows)
    mcentre.pack(side="top")
    # The glyphs are drawn larger and darker than the label font would
    # give them, because these two are the tab's main verbs and at the
    # default weight they read as decoration.
    arrowface = tkfont.nametofont("TkDefaultFont").copy()
    arrowface.configure(size=max(11, abs(arrowface.cget("size")) + 3),
                        weight="bold")
    ttk.Style().configure("Arrow.TButton", font=arrowface,
                          foreground="#101010")
    # Greyed out still has to look greyed out: a style's foreground is
    # a flat colour and would override the theme's disabled one, so the
    # disabled state is given back explicitly.
    ttk.Style().map("Arrow.TButton",
                    foreground=[("disabled", "#a3a3a3")])
    # Each button is the size its own glyph needs. They used to be
    # pinned inside a frame measured off a *plain* button, so that a
    # bigger font would not make a bigger button - but a ttk label that
    # will not fit is not shrunk, it is cut, and it is cut from the
    # bottom. Measured: the styled button asks for 37 by 31 and was
    # being given 28 by 25, so six pixels of the arrow's descent went
    # over the edge and the glyph sat high in the button.
    btn_msend = ttk.Button(mcentre, text="↓", width=3,
                           style="Arrow.TButton",
                           command=lambda: do_msk_send())
    btn_mload = ttk.Button(mcentre, text="↑", width=3,
                           style="Arrow.TButton",
                           command=lambda: do_msk_load())
    # Sending is the eight segments and nothing else. This is the rest
    # of the instrument - sweep, trigger, display, counter - and it is
    # a separate press because it is a separate decision.
    btn_mmeasure = ttk.Button(marrows, text=_("Start measurement"),
                              padding=(8, 0),
                              command=lambda: do_msk_measure())
    btn_mclear = ttk.Button(marrows, text=_("Clear"), padding=(6, 0),
                            command=lambda: do_msk_clear())
    btn_msend.pack(side="left", padx=(0, 3))
    btn_mload.pack(side="left", padx=(3, 0))
    # One under the other with the same gap between each. Clear used to
    # be place()d at the top right, which takes a widget out of the
    # layout altogether - so it sat on top of Start measurement as soon
    # as that button was wide enough to reach it. This pane is about
    # two hundred pixels wide and none of these three share a line.
    btn_mmeasure.pack(side="top", pady=(6, 0))
    btn_mclear.pack(side="top", pady=(6, 0))
    lbl_mlive = ttk.Label(mlivef, text=_("Loaded in the instrument"))
    says(lbl_mlive, "Loaded in the instrument")
    lbl_mlive.pack(anchor="w")
    # Which mask is in there. The instrument keeps no name - it holds
    # eight lists of points and nothing else - so this can only say what
    # this program put there, and says so plainly when it did not.
    lbl_mlivename = ttk.Label(mlivef, foreground="#555", wraplength=190,
                              justify="left")
    lbl_mlivename.pack(anchor="w", pady=(0, 2))
    mlive = ttk.Treeview(mlivef, columns=("seg", "what"), show="headings",
                         selectmode="none", height=8)
    mlive.heading("seg", text=_("Segment"))
    mlive.heading("what", text=_("Shape"))
    mlive.column("seg", width=70, anchor="w")
    mlive.column("what", width=150, anchor="w")
    # No scrollbar: there are eight segments and the list is eight rows
    # tall, so there is never anything to scroll to.
    mlive.pack(side="left", fill="both", expand=True)
    msplit.add(mlivef, weight=1)
    mpanes.add(mleftf, weight=1)

    mrightf = ttk.Frame(mpanes)
    # What can be done to a mask, above the drawing: undoing comes
    # first because it is what somebody reaches for when a drag went
    # wrong, and it should not be hunting for it at the far end of a row.
    mtoolbar = ttk.Frame(mrightf)
    mtoolbar.pack(fill="x", pady=(0, 3))
    btn_mundo = ttk.Button(mtoolbar, style="Toolbutton", padding=3,
                           command=lambda: do_msk_undo())
    btn_mredo = ttk.Button(mtoolbar, style="Toolbutton", padding=3,
                           command=lambda: do_msk_redo())
    btn_mundo.pack(side="left")
    btn_mredo.pack(side="left", padx=(2, 0))
    state.setdefault("undobtn", {}).update({"mundo": btn_mundo,
                                            "mredo": btn_mredo})
    hints(btn_mundo, "Undo")
    hints(btn_mredo, "Redo")

    mcanvasrow = ttk.Frame(mrightf)
    mcanvasrow.pack(fill="both", expand=True)
    # The three tools, in a column against the drawing they act on.
    # Radiobuttons rather than plain ones: exactly one is in your hand
    # at a time, and the sunken one says which.
    mtools = ttk.Frame(mcanvasrow)
    mtools.pack(side="left", fill="y", padx=(0, 3))
    state["mtool"] = tk.StringVar(value="pen")
    for key, english in (("move", "Select and move points and shapes"),
                         ("pen", "Draw points"),
                         ("eraser", "Delete points and shapes"),
                         ("cut", "Split a shape between two of its points")):
        rb = ttk.Radiobutton(mtools, value=key, variable=state["mtool"],
                             style="Toolbutton", padding=3,
                             command=lambda: msk_tool_changed())
        rb.pack(pady=(0, 2))
        hints(rb, english)
        state.setdefault("mtoolbtn", {})[key] = rb
    # Below the line: not tools but things done to two shapes at once,
    # which is why they are ordinary buttons rather than another mode to
    # be in. The instrument has no idea any of this happened - it is all
    # arithmetic on this side, and what it gets is the pieces.
    ttk.Separator(mtools, orient="horizontal").pack(fill="x", pady=5)
    for key, english in (
            ("fliph", "Mirror the selection horizontally"),
            ("flipv", "Mirror the selection vertically")):
        fb = ttk.Button(mtools, style="Toolbutton", padding=3,
                        command=lambda k=key: do_msk_flip(k == "fliph"))
        fb.pack(pady=(0, 2))
        hints(fb, english)
        state.setdefault("mboolbtn", {})[key] = fb
    ttk.Separator(mtools, orient="horizontal").pack(fill="x", pady=5)
    for key, english in (
            ("union", "Union: join the two selected shapes into one"),
            ("intersect", "Intersect: keep only where the two selected shapes overlap"),
            ("subtract", "Subtract: take the second selected shape out of the first")):
        bb = ttk.Button(mtools, style="Toolbutton", padding=3,
                        command=lambda k=key: do_msk_boolean(k))
        bb.pack(pady=(0, 2))
        hints(bb, english)
        state.setdefault("mboolbtn", {})[key] = bb

    mplot = tk.Canvas(mcanvasrow,
                      background=tds_wfm.DEFAULT_COLOURS["background"],
                      highlightthickness=1, highlightbackground=EDGE)
    mplot.pack(side="left", fill="both", expand=True)
    mbar = ttk.Frame(mrightf)
    mbar.pack(fill="x", pady=(4, 0))
    state["mgrid"] = tk.StringVar(value="0.5")
    state["msnap"] = tk.BooleanVar(value=True)
    state["mshowgrid"] = tk.BooleanVar(value=True)
    state["mgratic"] = tk.BooleanVar(value=True)
    state["mcross"] = tk.BooleanVar(value=False)
    lbl_mgrid = ttk.Label(mbar, text=_("Grid spacing, divisions"))
    says(lbl_mgrid, "Grid spacing, divisions")
    lbl_mgrid.pack(side="left")
    ent_mgrid = ttk.Combobox(mbar, textvariable=state["mgrid"], width=5,
                             values=("0.1", "0.2", "0.25", "0.5", "1"))
    ent_mgrid.pack(side="left", padx=(4, 10))
    ent_mgrid.bind("<<ComboboxSelected>>", lambda e: edit_redraw())
    ent_mgrid.bind("<Return>", lambda e: edit_redraw())
    chk_mshowgrid = ttk.Checkbutton(mbar, text=_("Show grid"),
                                    variable=state["mshowgrid"],
                                    command=lambda: edit_redraw())
    says(chk_mshowgrid, "Show grid")
    chk_mshowgrid.pack(side="left", padx=(0, 10))
    chk_msnap = ttk.Checkbutton(mbar, text=_("Snap to grid"),
                                variable=state["msnap"],
                                command=lambda: edit_redraw())
    says(chk_msnap, "Snap to grid")
    chk_msnap.pack(side="left", padx=(0, 10))
    chk_mgrat = ttk.Checkbutton(mbar, text=_("Graticule"),
                                variable=state["mgratic"],
                                command=lambda: edit_redraw())
    says(chk_mgrat, "Graticule")
    chk_mgrat.pack(side="left", padx=(0, 10))
    chk_mcross = ttk.Checkbutton(mbar, text=_("Crosshairs"),
                                 variable=state["mcross"],
                                 command=lambda: edit_redraw())
    says(chk_mcross, "Crosshairs")
    chk_mcross.pack(side="left", padx=(0, 10))
    # Filled is how the instrument draws a mask, and outlines are how
    # you edit one - so both, rather than a choice made for the user.
    state["mfill"] = tk.BooleanVar(value=False)
    chk_mfill = ttk.Checkbutton(mbar, text=_("Filled"),
                                variable=state["mfill"],
                                command=lambda: edit_redraw())
    says(chk_mfill, "Filled")
    chk_mfill.pack(side="left", padx=(0, 10))
    # And out of the way altogether, for looking at the trace behind
    # it. It comes back the moment there is any reason to see it - a
    # tool picked up, or another mask opened - because a mask editor
    # that has quietly stopped showing the mask is a puzzle.
    state["mhide"] = tk.BooleanVar(value=False)
    chk_mhide = ttk.Checkbutton(mbar, text=_("Hide mask"),
                                variable=state["mhide"],
                                command=lambda: edit_redraw())
    says(chk_mhide, "Hide mask")
    chk_mhide.pack(side="left", padx=(0, 10))
    # The handles on their own, for looking at the shape rather than at
    # what it is made of. Back the moment the mask is - the same rule,
    # for the same reason: an editor that has quietly stopped showing
    # what can be dragged is the same puzzle.
    state["mnodots"] = tk.BooleanVar(value=False)
    chk_mnodots = ttk.Checkbutton(mbar, text=_("Hide points"),
                                  variable=state["mnodots"],
                                  command=lambda: edit_redraw())
    says(chk_mnodots, "Hide points")
    chk_mnodots.pack(side="left")
    # A mask goes to the instrument as a mask, and nothing else. It
    # used to be able to go as a limit template instead, for an
    # instrument with no Option 2C - but a limit template is what the
    # Limits tab is, and having two tabs that both build one meant two
    # places to look for the same answer. Without 2C this tab still
    # draws, edits, saves and loads; what it cannot do is reach the
    # instrument's mask subsystem, because there is not one.
    minfo = ttk.Label(mrightf, anchor="w", foreground="#555")
    minfo.pack(fill="x", pady=(2, 0))
    mpanes.add(mrightf, weight=4)

    # ------------------------------------------- one editor, two drawings
    # The masks tab and the limits tab draw with the same tools, and the
    # tools are written once. What differs between them is only which
    # canvas the gesture landed on and which drawing it acts on, so that
    # is what these four answer and nothing else has to know there are
    # two. A second copy of the editor would be a second set of bugs to
    # find twice.
    #
    # The tools, the selection and the half-drawn shape are shared on
    # purpose - only one of the two tabs can be in front, so there is
    # only ever one of each in play. History and the unsaved mark are
    # not shared: those belong to the drawing.
    def edit_here():
        """Which drawing the tools are acting on: 'lim' or 'msk'."""
        try:
            return "lim" if tabs.select() == str(limtab) else "msk"
        except Exception:               # before the notebook exists
            return "msk"

    def edit_canvas():
        return lplot if edit_here() == "lim" else mplot

    def edit_mask():
        """The drawing being edited: a mask, or the limits tab's band."""
        return state.get("lmask" if edit_here() == "lim" else "mask")

    def edit_segments():
        """How many shapes this drawing may hold.

        Eight for a mask, because the instrument has eight
        segments and that is where a mask goes. A limits drawing
        never goes there - it becomes one envelope, a min and a
        max per column - so the only reason for a ceiling is to
        stop a runaway, and it can be a generous one.
        """
        return 64 if edit_here() == "lim" else tds_msk.SEGMENTS

    def edit_points():
        """How many points one shape may hold.

        Fifty is what a mask segment holds on the instrument -
        asking for 64 stored 50 and left an event in the queue. An
        envelope has no such limit; the only ceiling on a limits
        drawing is how many handles a person can work with.
        """
        return (200 if edit_here() == "lim"
                else tds_msk.POINTS_PER_SEGMENT)

    def edit_tidy():
        """A single point is not a shape, and no action may leave
        one behind.

        Done in one place rather than guarded at every gesture,
        because the rule is about the drawing rather than about
        any one way of changing it - a pen abandoned after one
        click, a delete that takes all but one point, a cut, a
        boolean. Any of them can strand a point and none of them
        should.

        The shape being drawn is exempt while it is being drawn,
        since one point is how every shape starts. Whatever
        finishes it drops that exemption, so abandoning the pen
        after a single click clears it.
        """
        mask = edit_mask()
        if mask is None:
            return
        drawing = state.get("mdrawing")
        for i, seg in enumerate(mask.segments):
            if i != drawing and 0 < len(seg) < tds_msk.MIN_POINTS:
                del seg[:]

    def edit_redraw():
        edit_tidy()
        (draw_limits if edit_here() == "lim" else draw_mask)()

    def edit_soon():
        """Redraw once the pointer stops arriving, not once per event.

        The pointer delivers motion faster than the canvas can be
        rebuilt - five hundred items with the grid on, and eight
        thousand at a tenth-division grid - and every one of them that
        changes what is under the pointer asks for a rebuild. Drawn one
        for one, the queue grows and the crosshair trails the mouse by a
        visible distance.

        Collapsed onto one idle callback, a burst of motion costs one
        redraw and the crosshair keeps up. The last event in the burst
        is the one that matters anyway: the earlier ones describe
        positions the pointer has already left.
        """
        if state.get("mredraw"):
            return
        state["mredraw"] = edit_canvas().after_idle(edit_now)

    def edit_now():
        state.pop("mredraw", None)
        edit_redraw()

    def edit_key(name):
        """A state key belonging to the drawing rather than the editor."""
        return ("l" if edit_here() == "lim" else "m") + name

    def edit_new():
        """Start a drawing of whichever kind is in front.

        The masks tab has a New mask button and a library behind
        it; the limits tab has neither, because a limits drawing
        is one band for one signal and there is nothing to name.
        """
        if edit_here() == "lim":
            state["lmask"] = tds_msk.Mask(
                source=state["lsource"].get())
        else:
            do_msk_new()

    def mask_frame():
        """Where the graticule sits on the drawing in front."""
        canvas = edit_canvas()
        w = max(canvas.winfo_width(), 80)
        h = max(canvas.winfo_height(), 60)
        left, top, right, bottom = tds_wfm.plot_frame(w, h, 20, room=8)
        return (left, top, right, bottom)

    def mask_step():
        """The grid step, as (across, up) in percent of the graticule.

        The box is in *divisions*, not in percent of the whole
        graticule, because a division is what the instrument draws and
        what anybody reading a mask thinks in. A division is 10% of the
        width and 12.5% of the height, so the two steps differ - set as
        one number of percent, the horizontal lines landed between the
        instrument's own, which is a grid that helps with nothing.
        """
        try:
            divs = float(state["mgrid"].get())
        except (TypeError, ValueError):
            return (0.0, 0.0)
        if divs <= 0:
            return (0.0, 0.0)
        return (divs * 100.0 / tds_wfm.DIVS_X,
                divs * 100.0 / tds_wfm.DIVS_Y)

    def mask_grid():
        """The steps points snap to, or zeroes when they are not to snap.

        Showing the grid and snapping to it are two different questions
        and were one setting: somebody who wanted to see where the
        divisions were had to accept being pulled onto them.
        """
        return mask_step() if state["msnap"].get() else (0.0, 0.0)

    def edit_grid(canvas, pick, frame):
        """A dot at every place a click can put a point.

        Drawn the same way on both editors, because it says the
        same thing on both: where a point will land.
        """
        left, top, right, bottom = frame
        across, up = mask_step()
        if across and up and state["mshowgrid"].get():
            # A dot at every place a point can land, rather than lines
            # through them: what the grid is for is saying where a click
            # will go, and a dot says that exactly where a crossing of
            # two lines says it twice over.
            #
            # Every crossing, however fine. A tenth-division grid is
            # eight thousand of them, which is only affordable because a
            # pointer moving across the canvas no longer rebuilds it -
            # see msk_crosshair.
            xs = [across * i for i in range(1, int(round(100.0 / across)))]
            ys = [up * i for i in range(1, int(round(100.0 / up)))]
            for gx in xs:
                for gy in ys:
                    px, py = tds_msk.to_canvas(gx, gy, (left, top,
                                                        right, bottom))
                    canvas.create_line(px, py, px + 1, py,
                                      fill=pick["grid"], tags="grid")

    def edit_shapes(canvas, mask, pick, frame, joins=True, stipple=""):
        """The drawing, its handles and everything being aimed.

        Shared by the masks tab and the limits tab: a shape, a
        selected point, a shape half drawn, a cut being lined up
        and whatever the pointer is over all look the same and mean
        the same on both. Which drawing it is comes in as an
        argument, so neither tab has to guess.
        """
        left, top, right, bottom = frame
        if mask is not None and not state["mhide"].get():
            # Shapes the instrument would join up its own way are drawn
            # dashed, so the one thing the editor cannot show - what the
            # graticule will actually look like - is at least flagged
            # where it is being drawn. See tds_msk.redraw_reason.
            # `joins` is False for a drawing that is not going
            # into the instrument's mask segments, and so cannot
            # be redrawn by them - see edit_shapes.
            redrawn = dict(mask.redrawn()) if joins else {}
            for number, seg in mask.filled():
                # filled() counts segments from 1, the way the
                # instrument names them; a selection is kept as an index
                # into mask.segments, which counts from 0. Mixing the
                # two drew the hollow "this one is selected" handle on
                # the wrong shape.
                n = number - 1
                flat = []
                for x, y in seg:
                    px, py = tds_msk.to_canvas(x, y, (left, top, right,
                                                      bottom))
                    flat += [px, py]
                chosen = set(state.get("mpicks") or [])
                # A shape every point of which is selected is drawn in
                # the selection colour, so what a flip or a boolean is
                # about to act on is visible without counting handles.
                whole = all((n, i) in chosen for i in range(len(seg)))
                edge = pick["select"] if whole else pick["mask"]
                # `stipple` only touches the fill; the outline stays
                # solid. The limits tab asks for one so the trace it is
                # drawn around still reads through it - see draw_limits.
                canvas.create_polygon(
                    *flat, fill=(edge if state["mfill"].get() else ""),
                    stipple=(stipple if state["mfill"].get() else ""),
                    outline=edge, width=2,
                    dash=(6, 4) if number in redrawn else (),
                    tags=("mask", "seg%d" % number))
                for i, (x, y) in enumerate(seg):
                    if state["mnodots"].get():
                        continue
                    px, py = tds_msk.to_canvas(x, y, (left, top, right,
                                                      bottom))
                    # A selected point is hollow and a little larger, so
                    # which points a drag or a Delete will act on can be
                    # seen at a glance rather than remembered.
                    if (n, i) in chosen:
                        canvas.create_rectangle(px - 4, py - 4,
                                               px + 4, py + 4,
                                               fill=pick["background"],
                                               outline=pick["select"],
                                               width=2,
                                               tags=("handle", "picked",
                                                     "seg%d" % number))
                    else:
                        canvas.create_rectangle(px - 3, py - 3,
                                               px + 3, py + 3,
                                               fill=pick["mask"],
                                               outline=pick["background"],
                                               tags=("handle",
                                                  "seg%d" % number))
                    # The coordinates box numbers its rows and the
                    # canvas did not, so matching row 7 to a handle
                    # meant counting round the shape. Numbered only
                    # while that box is open on this shape: every
                    # point labelled all the time is a drawing nobody
                    # can see.
                    if state.get("mnumbers") == n:
                        canvas.create_text(px + 8, py - 8, anchor="w",
                                           text=str(i + 1),
                                           fill=pick["label"],
                                           tags=("number",
                                                 "seg%d" % number))
        # While a shape is being drawn, show where the next click would
        # put an edge - and the edge that closes it. The closing line is
        # not a preview of something optional: a TDS mask segment is a
        # closed polygon and there is no other kind, so the shape a
        # click is about to make is the shape with the closing edge in
        # it. Dashed, because none of it is placed yet.
        drawing = state.get("mdrawing")
        pointer = state.get("mcrossat")
        if (mask is not None and drawing is not None and pointer is not None
                and drawing < len(mask.segments) and mask.segments[drawing]):
            open_seg = mask.segments[drawing]
            ends = ([open_seg[-1]] if len(open_seg) < 2
                    else [open_seg[-1], open_seg[0]])
            for ex, ey in ends:
                ax, ay = tds_msk.to_canvas(ex, ey, (left, top, right,
                                                    bottom))
                canvas.create_line(ax, ay, pointer[0], pointer[1],
                                  fill=pick["mask"], dash=(4, 3),
                                  tags="elastic")
        # The cut being aimed. Red rather than a scheme colour on
        # purpose: it is the one line on the canvas that is about to
        # divide something, and it should not look like part of the
        # drawing.
        cutting = state.get("mcutfrom")
        if (mask is not None and cutting is not None and pointer is not None
                and cutting[0] < len(mask.segments)
                and cutting[1] < len(mask.segments[cutting[0]])):
            fx, fy = mask.segments[cutting[0]][cutting[1]]
            cx, cy = tds_msk.to_canvas(fx, fy, frame)
            canvas.create_line(cx, cy, pointer[0], pointer[1],
                              fill=pick["cut"], width=2, tags="cut")
        # What the pointer is over, drawn over what it is over: the
        # eraser highlights the whole shape when it is on an edge,
        # because that is what it is about to take away.
        hover = state.get("mhover")
        if mask is not None and hover is not None:
            kind, which = hover
            hot = pick["hover"]
            if kind == "edge" and msk_tool() == "eraser":
                kind, which = "shape", which[0]
            if kind == "point":
                s, i = which
                if s < len(mask.segments) and i < len(mask.segments[s]):
                    hx, hy = tds_msk.to_canvas(*mask.segments[s][i],
                                               frame=(left, top, right,
                                                      bottom))
                    canvas.create_oval(hx - 6, hy - 6, hx + 6, hy + 6,
                                      outline=hot, width=2, tags="hover")
            elif kind == "edge":
                s, after = which[0], which[1]
                seg = mask.segments[s]
                if seg:
                    a = seg[after % len(seg)]
                    b = seg[(after + 1) % len(seg)]
                    ax, ay = tds_msk.to_canvas(a[0], a[1], frame)
                    bx, by = tds_msk.to_canvas(b[0], b[1], frame)
                    canvas.create_line(ax, ay, bx, by, fill=hot, width=4,
                                      tags="hover")
            elif kind == "shape" and which < len(mask.segments):
                flat = []
                for x, y in mask.segments[which]:
                    hx, hy = tds_msk.to_canvas(x, y, (left, top, right,
                                                      bottom))
                    flat += [hx, hy]
                if len(flat) >= 6:
                    canvas.create_polygon(*flat, fill="", outline=hot,
                                         width=3, tags="hover")
                elif len(flat) == 4:
                    # A shape of two points is a line, and a polygon of
                    # two points draws nothing at all - so the eraser
                    # lit up every shape on the canvas except the one
                    # kind a limits drawing is mostly made of, and a
                    # click deleted something that had never been
                    # highlighted.
                    canvas.create_line(*flat, fill=hot, width=4,
                                      tags="hover")
        msk_crosshair()
        band = state.get("mband")
        if band is not None and band.get("moved") and "to" in band:
            canvas.create_rectangle(band["at"][0], band["at"][1],
                                   band["to"][0], band["to"][1],
                                   outline=pick["label"], dash=(2, 2),
                                   tags="band")

    def draw_mask(_evt=None):
        """The graticule, the grid, the trace under it and the mask on top.

        Drawn in the order somebody would want to see it: the graticule
        is furthest back, then the grid, then any trace being worked
        against, then the mask, then its handles. A mask drawn under the
        trace it is meant to bound would be the wrong way round.
        """
        mplot.delete("all")
        pick = plot_colours(None)
        mplot.configure(background=pick["background"])
        left, top, right, _bottom = mask_frame()
        # The instrument's own screen, where one was captured because it
        # was in DPO and there was no record to read. Furthest back of
        # all: it is cropped to its graticule and scaled to this one, so
        # the editor's graticule lands on the instrument's own and the
        # two agreeing is the thing to be able to see.
        shot = msk_shot_image()
        if shot is not None:
            mplot.create_image(left, top, image=shot, anchor="nw",
                               tags="shot")
        if state["mgratic"].get():
            # The same tds_wfm.graticule() the waveform tab and the PNG
            # export draw from, so a mask is drawn over exactly the
            # graticule a trace is drawn over.
            for element, x0, y0, x1, y1, thick in tds_wfm.graticule(
                    max(mplot.winfo_width(), 80),
                    max(mplot.winfo_height(), 60), 20, room=8):
                mplot.create_line(x0, y0, x1, y1, fill=pick[element],
                                  width=thick)
        edit_grid(mplot, pick, mask_frame())
        wave = state.get("mwave")
        if wave is not None:
            xy, _bounds = tds_wfm.plot_geometry(
                wave, max(mplot.winfo_width(), 80),
                max(mplot.winfo_height(), 60), 20, state.get("mview"),
                room=8)
            if len(xy) > 1:
                flat = []
                for x, y in xy:
                    flat += [x, y]
                mplot.create_line(*flat, fill=trace_colour(wave, pick),
                                  width=1, tags="trace")
        mask = state.get("mask")
        edit_shapes(mplot, mask, pick, mask_frame())
        verdict = msk_verdict()
        if verdict is not None:
            words, colour = verdict
            # Filled, with the word in black on it, the same way the
            # saved picture stamps it. See tds_wfm.plot_png.
            mplot.create_rectangle(right - 76, top + 6, right - 6, top + 32,
                                   fill=colour, outline="", tags="verdict")
            mplot.create_text(right - 41, top + 19, text=words,
                              fill="#000000",
                              font=("TkDefaultFont", 12, "bold"),
                              tags="verdict")
        say_mask()

    def say_mask():
        """The line under the canvas: what is open and how big it is."""
        mask = state.get("mask")
        if mask is None:
            minfo.config(text=_("No mask open - press New mask, or "
                                "double-click one on the left"))
            return
        segs = mask.filled()
        # Complaints stop a save; the redrawn note does not. It is here
        # because it is the one fault the canvas cannot show honestly -
        # the shape is drawn correctly here and the instrument will join
        # the same points up differently.
        # The name is the only place a shipped mask says what signal it
        # is drawn for, so what the name is worth is spelled out here in
        # full - the list on the left has room for the rate alone.
        body, rate = tds_msk.standard(mask.origin or mask.name)
        about = [x for x in (body, rate) if x]
        setup = msk_setup_path(mask)
        if setup:
            try:
                about.append(tds_set.summary(tds_set.parse(
                    tds_set.contents(setup))) or os.path.basename(setup))
            except OSError:
                pass
        notes = mask.complaints() + ["segment %d: %s" % (n, why)
                                     for n, why in mask.redrawn()]
        minfo.config(text=_("%(name)s%(std)s - %(segs)d segment(s), "
                            "%(pts)d point(s)%(bad)s")
                     % {"name": mask.name or _("untitled"),
                        "std": (" (%s)" % ", ".join(about)) if about else "",
                        "segs": len(segs),
                        "pts": len(mask.points),
                        "bad": ("  -  " + "; ".join(notes)) if notes else ""})

    mplot.bind("<Configure>", draw_mask)

    # ---------------------------------------------- drawing with a mouse
    # The gestures are Illustrator's, because that is what somebody who
    # draws shapes already has in their hands: click on empty space adds
    # a point to the segment being drawn, click a point selects it, drag
    # a point moves it, click an edge inserts a point there, and Delete
    # removes what is selected. Escape finishes the segment being drawn.

    def msk_at(evt):
        """Where this click is, in percent, snapped if snapping is on.

        Kept on the graticule last of all. Snapping can only move a
        point by half a grid step, but a click can land anywhere on the
        canvas - the frame has eight pixels of room round it - and a
        point drawn out there is one the instrument will not show.
        """
        x, y = tds_msk.from_canvas(evt.x, evt.y, mask_frame())
        across, up = mask_grid()
        return tds_msk.held(*tds_msk.snap_point(x, y, across, up))

    def msk_reach():
        """How near counts as on a point, in percent rather than pixels.

        A fixed number of pixels is a different distance in a small
        window than a large one, and the shapes are in percent, so the
        reach has to be as well.
        """
        left, _top, right, _bottom = mask_frame()
        return 6.0 / max(1.0, right - left) * 100.0

    def msk_picked():
        """Which points are selected, as (segment, point) pairs.

        A list rather than a single pair, because a rubber band and a
        whole-shape drag both select more than one and everything that
        acts on a selection should act on all of it.
        """
        return list(state.get("mpicks") or [])

    def msk_what_at(evt):
        """What the pointer is over: a point, an edge, or a whole shape.

        One question asked in one place, because three tools all need
        the same answer and the order matters - a point sits on an edge
        which sits inside a shape, and the smallest thing wins.
        """
        mask = edit_mask()
        if mask is None:
            return None
        x, y = tds_msk.from_canvas(evt.x, evt.y, mask_frame())
        reach = msk_reach()
        hit = tds_msk.near_point(mask, x, y, reach)
        if hit is not None:
            return ("point", hit)
        edge = tds_msk.near_edge(mask, x, y, reach)
        if edge is not None:
            return ("edge", edge)
        seg = tds_msk.segment_at(mask, x, y)
        return ("shape", seg) if seg is not None else None

    def msk_add_point(evt, onto=None):
        """Empty space: extend the shape being drawn, or start one.

        `onto` is an existing point the pointer is over, and the new
        one lands exactly on it rather than on the grid. Shapes that
        have to meet then meet: the grid cannot promise that when the
        point being met is not itself on it.
        """
        mask = edit_mask()
        if mask is None:
            return
        seg = state.get("mdrawing")
        if seg is None:
            seg = msk_new_segment()
            if seg is None:
                return
            state["mdrawing"] = seg
        if onto == (seg, 0) and len(mask.segments[seg]) > 2:
            # Back on this shape's own first point: that is the shape
            # closed, not a point stacked on another point.
            do_msk_finish()
            return
        where = mask.segments[onto[0]][onto[1]] if onto else msk_at(evt)
        if mask.segments[seg] and mask.segments[seg][-1] == where:
            return
        if len(mask.segments[seg]) >= edit_points():
            say(_("Segment %(n)d is full at %(max)d points")
                % {"n": seg + 1, "max": edit_points()})
            return
        msk_remember()
        mask.segments[seg].append(where)
        state["mpicks"] = [(seg, len(mask.segments[seg]) - 1)]
        edit_redraw()

    def msk_drop_point(seg, point):
        """One point, gone."""
        mask = edit_mask()
        if mask is None or seg >= len(mask.segments):
            return
        if point >= len(mask.segments[seg]):
            return
        msk_remember()
        mask.segments[seg].pop(point)
        state["mpicks"] = []
        state.pop("mhover", None)
        edit_redraw()

    def msk_drop_shape(seg):
        """A whole shape, gone. The segment stays, empty: the instrument
        has eight of them whether they hold anything or not."""
        mask = edit_mask()
        if mask is None or seg >= len(mask.segments):
            return
        msk_remember()
        mask.segments[seg] = []
        state["mpicks"] = []
        state.pop("mhover", None)
        edit_redraw()
        say(_("Segment %d deleted") % (seg + 1))

    def msk_start_drag(evt, picks, anchor):
        """Remember where everything was, so a drag can move it as one.

        Positions are kept from the moment of the press rather than
        updated as the pointer moves. Accumulating deltas drifts: each
        step is snapped, and snapping a snapped number repeatedly walks
        a shape off its own grid.

        The whole geometry is kept as well, for undo - but not put on
        the stack until the release, and then only if the drag actually
        moved something. A press that only selects is not an edit.
        """
        mask = edit_mask()
        state["mdrag"] = {
            "from": tds_msk.from_canvas(evt.x, evt.y, mask_frame()),
            "anchor": anchor,
            "before": msk_shapes(),
            "was": {(s, i): mask.segments[s][i] for s, i in picks
                    if s < len(mask.segments)
                    and i < len(mask.segments[s])}}

    # ------------------------------------------------- the three tools
    # Each is one gesture on a press, and the hover highlight says what
    # that gesture is about to act on before it acts. Which tool is in
    # hand decides everything: the same click draws, moves or deletes.

    def msk_pen_press(evt, what):
        """The pen: a click is a point, on an edge or in fresh air."""
        mask = edit_mask()
        if what is not None and what[0] == "edge" \
                and state.get("mdrawing") is None:
            seg, after, ex, ey = what[1]
            msk_remember()
            # Exactly on the edge, not on the grid. Snapping it here
            # moves it off the edge it was put on, which changes the
            # shape - and on a shape whose points are not themselves on
            # the grid it moves it *inwards*, making a hair-thin concave
            # corner that then refuses to save and is far too small to
            # see. Drag it afterwards and the grid applies as usual.
            mask.segments[seg].insert(after + 1, (round(ex, 2),
                                                  round(ey, 2)))
            state["mpicks"] = [(seg, after + 1)]
            state.pop("mhover", None)
            edit_redraw()
            return
        msk_add_point(evt, what[1] if what and what[0] == "point" else None)

    def msk_grabbed(what):
        """Which points a press on this takes hold of.

        Exactly what the hover highlighted: a point is a point, a side
        is its two ends, and the inside of a shape is all of it. The
        highlight is the promise; this keeps it.
        """
        mask = edit_mask()
        kind, which = what
        if kind == "point":
            return [which]
        if kind == "edge":
            seg, after = which[0], which[1]
            count = len(mask.segments[seg])
            return [(seg, after % count), (seg, (after + 1) % count)]
        return [(which, i) for i in range(len(mask.segments[which]))]

    def msk_move_press(evt, what):
        """The four-way arrow: take hold of whatever is lit up."""
        adding = bool(evt.state & 0x0001)          # shift held
        if what is None:
            state["morder"] = []
            state["mband"] = {"at": (evt.x, evt.y), "moved": False}
            return
        grabbed = msk_grabbed(what)
        if not grabbed:
            return
        seg = grabbed[0][0]
        # Which shapes are chosen, and in what order. Subtract takes the
        # second out of the first, so the order is half of what the
        # button means - and it has to survive a shift-click that lands
        # on a corner rather than on an edge, which is most of them.
        order = list(state.get("morder") or []) if adding else []
        if seg not in order:
            order.append(seg)
        state["morder"] = order
        picks = msk_picked()
        if adding:
            picks = picks + [p for p in grabbed if p not in picks]
        elif not set(grabbed) <= set(picks):
            picks = list(grabbed)
        state["mpicks"] = picks
        # Pressing on something already selected keeps the whole
        # selection, so a group can be dragged by any of its parts. If
        # the press turns out to be a click that moved nothing, the
        # release narrows the selection to what was actually pressed.
        state["mnarrow"] = None if adding else list(grabbed)
        msk_start_drag(evt, picks,
                       grabbed[0] if what[0] in ("point", "edge") else None)
        edit_redraw()

    def msk_erase_press(_evt, what):
        """The eraser: a point if the pointer is on one, otherwise the
        whole shape - which is what an edge under the eraser means."""
        if what is None:
            return
        if what[0] == "point":
            msk_drop_point(*what[1])
            return
        msk_drop_shape(what[1][0] if what[0] == "edge" else what[1])

    def msk_room():
        """Which segments are empty and could hold another shape."""
        mask = edit_mask()
        if mask is None:
            return []
        while len(mask.segments) < edit_segments():
            mask.segments.append([])
        return [i for i, seg in enumerate(
            mask.segments[:edit_segments()]) if not seg]

    def msk_cut_press(_evt, what):
        """The scissors: click one point of a shape, then another.

        Both ends have to be points of the same shape, and the line
        between them has to stay inside it - a cut that leaves the shape
        or crosses an edge makes two shapes that are not the one that
        was there.
        """
        mask = edit_mask()
        if what is None or what[0] != "point":
            if state.pop("mcutfrom", None) is not None:
                say(_("Split abandoned"))
                edit_redraw()
            return
        seg, point = what[1]
        start = state.get("mcutfrom")
        if start is None or start[0] != seg or start[1] == point:
            state["mcutfrom"] = (seg, point)
            say(_("Click the point to split to"))
            edit_redraw()
            return
        if not tds_msk.can_cut(mask.segments[seg], start[1], point):
            say(_("That cut would leave the shape or cross one of its "
                  "own edges"))
            return
        room = msk_room()
        if not room:
            messagebox.showinfo(
                _("Nothing left to split into"),
                (_("This drawing already has %d shapes in it. "
                   "Delete one before cutting another in two.")
                 if edit_here() == "lim" else
                 _("This mask already uses all %d segments the "
                   "instrument has. Delete one before cutting "
                   "another in two.")) % edit_segments())
            return
        msk_remember()
        one, two = tds_msk.cut_at(mask.segments[seg], start[1], point)
        mask.segments[seg] = one
        mask.segments[room[0]] = two
        state.pop("mcutfrom", None)
        state["mpicks"] = []
        state["morder"] = []
        edit_redraw()
        say(_("Split into segments %(a)d and %(b)d")
            % {"a": seg + 1, "b": room[0] + 1})

    def msk_chosen_shapes():
        """Which whole shapes the selection covers, in the order chosen."""
        mask = edit_mask()
        if mask is None:
            return []
        whole = {n for n, _i in msk_picked()
                 if n < len(mask.segments) and mask.segments[n]
                 and all((n, i) in set(msk_picked())
                         for i in range(len(mask.segments[n])))}
        order = [n for n in (state.get("morder") or []) if n in whole]
        return order + sorted(whole - set(order))

    def do_msk_flip(across):
        """Mirror what is selected, about its own middle.

        Its own middle, not the graticule's, so a flip is a flip and not
        also a move. With nothing selected there is nothing to mirror -
        flipping the whole mask by accident is a poor surprise.
        """
        mask = edit_mask()
        picks = msk_picked()
        if mask is None or not picks:
            say(_("Nothing selected"))
            return
        here = [mask.segments[n][i] for n, i in picks
                if n < len(mask.segments) and i < len(mask.segments[n])]
        if not here:
            return
        xs = [x for x, _y in here]
        ys = [y for _x, y in here]
        mid = (min(xs) + max(xs)) / 2.0 if across else (min(ys) +
                                                        max(ys)) / 2.0
        msk_remember()
        for n, i in picks:
            if n < len(mask.segments) and i < len(mask.segments[n]):
                x, y = mask.segments[n][i]
                mask.segments[n][i] = ((round(2 * mid - x, 2), y) if across
                                       else (x, round(2 * mid - y, 2)))
        # Mirroring reverses which way round a shape is drawn, and a
        # shape drawn the other way round is the same shape - but the
        # order decides which side an edge insert lands on, so it is put
        # back the way it was.
        for n in {n for n, _i in picks}:
            if n < len(mask.segments) and len(mask.segments[n]) > 2 \
                    and all((n, i) in set(picks)
                            for i in range(len(mask.segments[n]))):
                mask.segments[n].reverse()
        edit_redraw()
        say(_("Mirrored"))

    def do_msk_copy(_evt=None, cut=False):
        """Keep the chosen shapes, to be pasted later."""
        mask = edit_mask()
        chosen = msk_chosen_shapes()
        if mask is None or not chosen:
            say(_("Select a shape first"))
            return "break"
        state["mclip"] = [list(mask.segments[n]) for n in chosen]
        if cut:
            msk_remember()
            for n in chosen:
                mask.segments[n] = []
            state["mpicks"] = []
            state["morder"] = []
            edit_redraw()
        say(_("%d shape(s) copied") % len(state["mclip"]))
        return "break"

    def do_msk_paste(_evt=None):
        """Put back what was copied, one grid step along so it can be seen."""
        mask = edit_mask()
        held = state.get("mclip") or []
        if mask is None or not held:
            return "break"
        room = msk_room()
        if len(held) > len(room):
            messagebox.showerror(
                _("Error"), "%s\n\n%s"
                % (_("There is not room for the result."),
                   _("It comes to %(pieces)d shape(s) and there "
                     "are %(room)d segment(s) free. Delete a shape and "
                     "try again.")
                   % {"pieces": len(held), "room": len(room)}))
            return "break"
        across, up = mask_step()
        across, up = across or 1.0, up or 1.0
        msk_remember()
        picks = []
        for at, shape in zip(room, held):
            mask.segments[at] = [(round(x + across, 2), round(y - up, 2))
                                 for x, y in shape]
            picks += [(at, i) for i in range(len(shape))]
        state["mpicks"] = picks
        state["morder"] = list(room[:len(held)])
        edit_redraw()
        say(_("%d shape(s) pasted") % len(held))
        return "break"

    def do_msk_behind():
        """Put whatever the instrument is showing behind the mask.

        One button and two answers, because the instrument has two
        states: a readable waveform, or DPO, where there is no record to
        read and the screen itself is the only picture of what it is
        acquiring. Three, with a differential pair differenced in MATH1:
        that is a record, but not one of the channels, and not one the
        instrument's own mask counter can see.
        """
        if state["busy"] or state.get("cannot") is None:
            return
        busy(True, "wait")
        say(_("Reading what the instrument is showing ..."))
        w.submit("msk_behind",
                 lambda k, m=bool(state.get("mmath")): k.msk_behind(m))

    def do_msk_drop_shot():
        """Take whatever is behind the mask back off again.

        A captured screen stays until it is replaced, which is what
        makes it usable - but it also goes stale, and a DPO screen from
        ten minutes ago under a mask being edited now is a picture of
        nothing in particular. The verdict goes with it: it was that
        picture's verdict.
        """
        if not (state.get("mshot") or state.get("mwave")
                or state.get("mhits")):
            return
        state.pop("mshot", None)
        state.pop("mshotimage", None)
        state.pop("mhits", None)
        state["mwave"] = None
        state["mview"] = None
        draw_mask()
        msk_buttons()
        say(_("Cleared what was behind the mask"))

    def msk_view_for(wave):
        """How to draw that record against this mask, or None for as-is.

        None everywhere except MATH1, which the instrument scales
        itself: a 784D shows CH1-CH2 at five times the channel's own
        volts a division, and there is no command to tell it otherwise.
        Drawn as the instrument has it, the difference would sit five
        times too small inside a mask drawn for the volts a division
        the setup names - and pass whatever it did. The zoom is that
        ratio, so the trace is judged and drawn at the scale the mask
        means.
        """
        want = msk_volts_per_div()
        if wave is None or not want or not str(
                wave.source or "").upper().startswith("MATH"):
            return None
        view = tds_wfm.PlotView([wave])
        view.zoom = wave.volts_per_div / want
        return view

    def msk_verdict():
        """(words, colour) for the trace behind the mask, or None.

        A mask says where the signal may not go, so the verdict is
        whether any of it went there. Judged on the samples as drawn -
        which is what the eye is judging too - rather than on the whole
        record: a point between two drawn samples is a point neither the
        mask nor anybody looking at it can see.

        Only offered where there is something to judge. An empty mask or
        no trace is not a pass.
        """
        mask = state.get("mask")
        if mask is None or not mask.filled():
            return None
        # The instrument's own tally first, where there is one. It is
        # the only verdict available in DPO - there is no waveform
        # record to judge, every sample of it reads back mid-scale -
        # and it is the better one everywhere else too, being taken
        # over every acquisition of the capture rather than the single
        # record that came back. See Worker.msk_count_read.
        hits = state.get("mhits")
        if hits and hits.get("waves"):
            return ((_("FAIL"), "#e04a4a") if hits.get("total")
                    else (_("PASS"), "#3fb950"))
        wave = state.get("mwave")
        if wave is None:
            return None
        xy, _bounds = tds_wfm.plot_geometry(
            wave, max(mplot.winfo_width(), 80),
            max(mplot.winfo_height(), 60), 20, state.get("mview"), room=8)
        frame = mask_frame()
        segs = [seg for _n, seg in mask.filled()]
        for px, py in xy:
            x, y = tds_msk.from_canvas(px, py, frame)
            for seg in segs:
                if tds_msk.inside(seg, x, y):
                    return (_("FAIL"), "#e04a4a")
        return (_("PASS"), "#3fb950")

    def msk_shot_behind():
        """The captured screen as plot_png wants it, or None.

        The same pixels the canvas draws behind the mask, so the saved
        picture is the picture on screen. In DPO it is the only thing
        there is to show: a saved mask test with an empty graticule
        behind it is a verdict nobody can check.
        """
        shot = state.get("mshot")
        if not shot:
            return None
        return (shot["pixels"], shot["palette"],
                shot["width"], shot["height"])

    def msk_say_hits():
        """What the instrument counted, for the end of the status line.

        Empty where it has no tally to give - a scope without the mask
        option, or nothing loaded in its segments - so the sentence in
        front of this reads properly on its own.
        """
        hits = state.get("mhits")
        if not hits or not hits.get("waves"):
            return ""
        if not hits.get("total"):
            return (_(" - the instrument tested %d acquisition(s) and "
                      "counted no hits") % hits["waves"])
        return (_(" - the instrument counted %(hits)d hit(s) over "
                  "%(waves)d acquisition(s), in segment(s) %(which)s")
                % {"hits": hits["total"], "waves": hits["waves"],
                   "which": ", ".join(str(n) for n in
                                      sorted(hits.get("each") or {}))})

    def msk_shot_image():
        """The captured screen, scaled to the graticule, as a PhotoImage.

        Rescaled whenever the window changes size and kept until it
        does, because the scaling is the expensive part and a resize is
        the only thing that invalidates it.
        """
        shot = state.get("mshot")
        if not shot:
            return None
        left, top, right, bottom = mask_frame()
        wide, tall = int(right - left), int(bottom - top)
        if wide < 8 or tall < 8:
            return None
        held = state.get("mshotimage")
        if held and held[0] == (wide, tall):
            return held[1]
        try:
            png = tds_wfm.scaled_indexed(shot["pixels"], shot["palette"],
                                         shot["width"], shot["height"],
                                         wide, tall)
            image = tk.PhotoImage(data=base64.b64encode(png))
        except Exception as exc:
            log_note("mask", "the captured screen would not scale: %s" % exc)
            state.pop("mshot", None)
            return None
        state["mshotimage"] = ((wide, tall), image)
        return image

    def do_msk_view_save():
        """The mask editor's graticule as a PNG, the way the plot saves.

        The same renderer the waveform tab uses, given the shapes as
        well - so the picture that comes out is the picture on screen,
        in the same scheme and at the size set in the colours dialog.
        """
        mask = state.get("mask")
        if mask is None or not mask.filled():
            messagebox.showinfo(_("Nothing to save"),
                                _("This mask has no segments with points "
                                  "in them yet."))
            return
        path = filedialog.asksaveasfilename(
            parent=root, title=_("Save image"), defaultextension=".png",
            initialfile=stamped(mask.name or _("mask"), ".png"),
            filetypes=[(_("PNG image (*.png)"), "*.png")])
        if not path:
            return
        wave = state.get("mwave")
        try:
            with open(path, "wb") as fh:
                fh.write(tds_wfm.plot_png(
                    [wave] if wave is not None else [],
                    width=state["pngsize"][0], height=state["pngsize"][1],
                    colours=state.get("colours"),
                    caption=wave_scales(wave) if wave is not None else "",
                    shapes=[seg for _n, seg in mask.filled()],
                    grid=(mask_step() if state["mshowgrid"].get()
                          else (0.0, 0.0)),
                    verdict=msk_verdict(),
                    behind=msk_shot_behind()))
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                 % (_("Could not save"), exc))
            return
        say(_("Saved %s") % os.path.basename(path))

    def do_msk_boolean(how):
        """Join, overlap or take away, on the two chosen shapes.

        The answer is the outline the two shapes make, and it is more
        than one shape only where the answer really is - a bar taken
        out of the middle leaves two. It can be concave, and it can be
        a shape the instrument would redraw; that is said under the
        canvas like any other, and it is yours to adjust. See
        tds_msk.combine.
        """
        mask = edit_mask()
        if mask is None:
            return
        order = [n for n in (state.get("morder") or [])
                 if n < len(mask.segments) and len(mask.segments[n]) >= 3]
        if len(order) < 2:
            messagebox.showinfo(
                _("Two shapes are needed"),
                _("Choose a shape with the four-way arrow, then hold "
                  "Shift and click a second one. The order matters: "
                  "subtract takes the second out of the first."))
            return
        first, second = order[0], order[1]
        pieces = tds_msk.combine(how, mask.segments[first],
                                 mask.segments[second])
        if not pieces:
            messagebox.showinfo(
                _("Nothing came of that"),
                _("Those two shapes leave nothing behind - they may not "
                  "overlap at all, or one may cover the other "
                  "completely."))
            return
        room = sorted(set(msk_room() + [first, second]))
        if len(pieces) > len(room):
            messagebox.showerror(
                _("Error"), "%s\n\n%s"
                % (_("There is not room for the result."),
                   _("It comes to %(pieces)d shape(s) and there "
                     "are %(room)d segment(s) free. Delete a shape and "
                     "try again.")
                   % {"pieces": len(pieces), "room": len(room)}))
            return
        msk_remember()
        mask.segments[first] = []
        mask.segments[second] = []
        for at, piece in zip(room, pieces):
            mask.segments[at] = [(round(x, 2), round(y, 2))
                                 for x, y in piece]
        state["mpicks"] = []
        state["morder"] = [at for at, _p in zip(room, pieces)]
        edit_redraw()
        say(_("%d shape(s) from that") % len(pieces))

    def do_msk_press(evt):
        if edit_mask() is None:
            # Nothing open and somebody has started drawing: that is a
            # new drawing, and making them press a button to say so is
            # a button press that carries no information. Which kind
            # of drawing depends on the tab, or the limits tab's pen
            # would quietly start a mask and then draw nothing.
            edit_new()
        if edit_mask() is None:
            return
        edit_canvas().focus_set()
        what = msk_what_at(evt)
        if msk_tool() == "eraser":
            msk_erase_press(evt, what)
        elif msk_tool() == "pen":
            msk_pen_press(evt, what)
        elif msk_tool() == "cut":
            msk_cut_press(evt, what)
        else:
            msk_move_press(evt, what)

    def do_msk_menu(evt):
        """Right button: a point's own menu, or an end to this shape."""
        if edit_mask() is None:
            return
        # Something in hand beats anything under the pointer. A shape
        # being drawn is usually drawn *over* itself - the pointer sits
        # on the edge just laid down - so asking what is under it first
        # popped a Delete/Coordinates menu for that edge instead of
        # ending the line, and the only way out was Escape.
        if (state.get("mdrawing") is not None
                or state.get("mcutfrom") is not None):
            do_msk_finish()
            return
        what = msk_what_at(evt)
        if what is not None and what[0] != "point":
            # A side or the inside both mean the shape: the same two
            # things can be done to it, and where it is means its
            # bounding box's middle - type 50,50 and it lands in the
            # middle of the graticule, which is what somebody
            # positioning a shape means by its coordinates.
            seg = what[1][0] if what[0] == "edge" else what[1]
            menu = tk.Menu(root, tearoff=0)
            menu.add_command(label=_("Delete"),
                             command=lambda: msk_drop_shape(seg))
            menu.add_command(label=_("Coordinates"),
                             command=lambda: msk_typein(seg))
            try:
                menu.tk_popup(evt.x_root, evt.y_root)
            finally:
                menu.grab_release()
            return
        if what is not None and what[0] == "point":
            seg, point = what[1]
            menu = tk.Menu(root, tearoff=0)
            menu.add_command(
                label=_("Delete"),
                command=lambda: msk_drop_point(seg, point))
            menu.add_command(
                label=_("Coordinates"),
                command=lambda: msk_typein(seg, point))
            try:
                menu.tk_popup(evt.x_root, evt.y_root)
            finally:
                menu.grab_release()
            return
        if state.get("mdrawing") is None and state.get("mcutfrom") is None:
            if msk_picked() or state.get("morder"):
                state["mpicks"] = []
                state["morder"] = []
                edit_redraw()
                say(_("Nothing selected"))
            return
        do_msk_finish()

    def do_msk_move(evt):
        mask = edit_mask()
        if mask is None:
            return
        band = state.get("mband")
        if band is not None:
            x0, y0 = band["at"]
            if not band["moved"] and abs(evt.x - x0) + abs(evt.y - y0) > 3:
                band["moved"] = True
            if band["moved"]:
                band["to"] = (evt.x, evt.y)
                state["mpicks"] = msk_in_band(band)
                edit_soon()
            return
        drag = state.get("mdrag")
        if drag is None:
            return
        now = tds_msk.from_canvas(evt.x, evt.y, mask_frame())
        dx = now[0] - drag["from"][0]
        dy = now[1] - drag["from"][1]
        across, up = mask_grid()
        anchor = drag.get("anchor")
        if anchor is not None and anchor in drag["was"]:
            # Snap the point under the pointer and move the rest by the
            # same amount, which is what keeps a shape's own geometry
            # while still landing that point on the grid.
            ax, ay = drag["was"][anchor]
            dx = tds_msk.snapped(ax + dx, across) - ax
            dy = tds_msk.snapped(ay + dy, up) - ay
        elif across and up:
            dx, dy = tds_msk.snap_point(dx, dy, across, up)
        # The move is limited, not each point: clamping the points one
        # by one would flatten a shape against the edge instead of
        # stopping it there. A selection that is already off the
        # graticule - which a mask read from somebody's file may be -
        # is left alone rather than jerked inwards.
        was = list(drag["was"].values())
        if was:
            lo_x, hi_x = min(p[0] for p in was), max(p[0] for p in was)
            lo_y, hi_y = min(p[1] for p in was), max(p[1] for p in was)
            if 0.0 <= lo_x and hi_x <= 100.0:
                dx = min(max(dx, -lo_x), 100.0 - hi_x)
            if 0.0 <= lo_y and hi_y <= 100.0:
                dy = min(max(dy, -lo_y), 100.0 - hi_y)
        for (s, i), (wx, wy) in drag["was"].items():
            if s < len(mask.segments) and i < len(mask.segments[s]):
                mask.segments[s][i] = (wx + dx, wy + dy)
        edit_soon()

    def msk_in_band(band):
        """Every point inside the rubber band, as (segment, point)."""
        mask = edit_mask()
        if mask is None or "to" not in band:
            return []
        ax, ay = tds_msk.from_canvas(band["at"][0], band["at"][1],
                                     mask_frame())
        bx, by = tds_msk.from_canvas(band["to"][0], band["to"][1],
                                     mask_frame())
        lo_x, hi_x = min(ax, bx), max(ax, bx)
        lo_y, hi_y = min(ay, by), max(ay, by)
        out = []
        for s, seg in enumerate(mask.segments):
            for i, (x, y) in enumerate(seg):
                if lo_x <= x <= hi_x and lo_y <= y <= hi_y:
                    out.append((s, i))
        return out

    def do_msk_release(_evt=None):
        drag = state.pop("mdrag", None)
        band = state.pop("mband", None)
        if drag is not None:
            # One undo step for the whole drag, and none at all for a
            # press that only selected something.
            before = drag.get("before")
            moved = before is not None and before != msk_shapes()
            if moved:
                msk_keep(before)
            narrow = state.pop("mnarrow", None)
            if not moved and narrow and msk_picked() != narrow:
                # A click, not a drag, on something that was part of a
                # larger selection: now it is the selection.
                state["mpicks"] = list(narrow)
                state["morder"] = [narrow[0][0]]
                edit_redraw()
            return
        if band is None:
            return
        if band["moved"]:
            say(_("%d point(s) selected") % len(msk_picked()))
            edit_redraw()
        elif msk_tool() == "move":
            state["mpicks"] = []          # a click on nothing lets go
            edit_redraw()

    def do_msk_finish(_evt=None):
        """Right button, Enter or Escape: put down whatever is in hand."""
        if state.pop("mcutfrom", None) is not None:
            edit_redraw()
            say(_("Split abandoned"))
            return
        if state.pop("mdrawing", None) is not None:
            edit_redraw()
            say(_("Shape finished"))

    def msk_one_side(picks):
        """The side these two picks are, if that is what they are.

        Returns (segment, first point index) for two neighbouring points
        of one shape, and None otherwise.
        """
        if len(picks) != 2 or picks[0][0] != picks[1][0]:
            return None
        seg = picks[0][0]
        mask = edit_mask()
        count = len(mask.segments[seg]) if mask else 0
        if count < 3:
            return None
        a, b = sorted((picks[0][1], picks[1][1]))
        if b - a == 1:
            return (seg, a)
        return (seg, b) if a == 0 and b == count - 1 else None

    def do_msk_remove(_evt=None):
        """Delete: take out every selected point.

        A whole side is the exception. Taking out both its ends takes
        the two sides either side of it with them - a rectangle with one
        side deleted came out as a line - so a side is *collapsed*
        instead: its two ends become one point in the middle, the shape
        keeps its other corners, and it has one side fewer, which is
        what deleting a side means.

        Points are taken out from the end of each segment backwards,
        because removing point 2 moves point 5 and a list of positions
        gathered before any of them were removed is otherwise wrong by
        the second one.
        """
        mask = edit_mask()
        picks = msk_picked()
        if mask is None or not picks:
            return
        side = msk_one_side(picks)
        if side is not None:
            seg, first = side
            points = mask.segments[seg]
            second = (first + 1) % len(points)
            ax, ay = points[first]
            bx, by = points[second]
            msk_remember()
            points[first] = (round((ax + bx) / 2.0, 2),
                             round((ay + by) / 2.0, 2))
            points.pop(second)
            state["mpicks"] = [(seg, min(first, len(points) - 1))]
            state.pop("mhover", None)
            edit_redraw()
            return
        msk_remember()
        for s, i in sorted(picks, reverse=True):
            if s < len(mask.segments) and i < len(mask.segments[s]):
                mask.segments[s].pop(i)
        state["mpicks"] = []
        edit_redraw()

    def msk_nudge_step():
        """How far one arrow key moves what is selected.

        A distance set on the Settings tab, the same both ways, where
        one has been chosen. Left alone it follows the grid: a fifth of
        a square, so the keys and the snapping agree with one another
        and five presses cross one. With snapping off there is no grid
        to be a fraction of, so it is half a percent - about a
        twentieth of a division.
        """
        step = state.get("nudge")
        if step:
            return step, step
        across, up = mask_grid()
        return ((across / 5.0, up / 5.0) if across and up
                else (0.5, 0.5))

    def do_msk_nudge(dx, dy):
        """Arrow keys: move the selection, without the mouse.

        The mouse is for getting a shape roughly right; this is for the
        last half-division, where a hand on a mouse cannot be trusted.
        """
        mask = edit_mask()
        picks = msk_picked()
        if mask is None or not picks:
            return "break"
        across, up = msk_nudge_step()
        # A run of presses on the same selection is one step back, not
        # twenty. Somebody walking a point into place would otherwise
        # spend the whole history doing it and have nothing left to undo
        # the thing they were fixing.
        run = ("nudge", tuple(sorted(picks)))
        if state.get("mrun") != run:
            msk_remember()
        state["mrun"] = run
        here = [mask.segments[s][i] for s, i in picks
                if s < len(mask.segments) and i < len(mask.segments[s])]
        if not here:
            return "break"
        # Limited as one, the same way a drag is: nudging a shape into
        # the edge should stop it there rather than fold it flat.
        step_x, step_y = dx * across, dy * up
        lo_x, hi_x = min(p[0] for p in here), max(p[0] for p in here)
        lo_y, hi_y = min(p[1] for p in here), max(p[1] for p in here)
        if 0.0 <= lo_x and hi_x <= 100.0:
            step_x = min(max(step_x, -lo_x), 100.0 - hi_x)
        if 0.0 <= lo_y and hi_y <= 100.0:
            step_y = min(max(step_y, -lo_y), 100.0 - hi_y)
        for s, i in picks:
            if s < len(mask.segments) and i < len(mask.segments[s]):
                x, y = mask.segments[s][i]
                mask.segments[s][i] = (round(x + step_x, 2),
                                       round(y + step_y, 2))
        edit_redraw()
        return "break"

    def msk_bounds(seg):
        """The middle of a shape's extents, and its width and height."""
        xs = [x for x, _y in seg]
        ys = [y for _x, y in seg]
        return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)

    def msk_typein(seg, point=None):
        """The shape's coordinates: its midpoint, and every point in it.

        Clicking gets a shape roughly right and is the wrong tool for
        getting one exactly right - a mask edge at 12.5% is a number,
        not a pixel somebody aimed at. Two decimal places, because that
        is a hundredth of the graticule and finer than the instrument
        stores.

        The midpoint is the middle of the bounding box rather than the
        centre of area: type 50,50 and the shape lands in the middle of
        the graticule, which is what positioning a shape means. A centre
        of area would put an L-shape's heavy end in the middle instead.

        Both the midpoint and the cells edit live: the drawing follows
        every keystroke that parses, and one undo step covers a whole
        edit rather than one per character.
        """
        mask = edit_mask()
        if mask is None or seg >= len(mask.segments):
            return
        if not mask.segments[seg]:
            return
        dlg = tk.Toplevel(root)
        dlg.title(_("Segment %d") % (seg + 1))
        dlg.transient(root)
        try:
            dlg.iconbitmap(resource("app.ico"))
        except Exception:
            pass
        pad = ttk.Frame(dlg, padding=10)
        pad.pack(fill="both", expand=True)
        ttk.Label(pad, text=_("Coordinates are in percent of the "
                              "graticule: X across, Y up.")).pack(
                                  anchor="w", pady=(0, 8))
        origin = tk.StringVar()
        place = ttk.Frame(pad)
        place.pack(fill="x")
        ttk.Label(place, text=_("Shape centre")).pack(side="left")
        mid = ttk.Entry(place, textvariable=origin, width=16)
        mid.pack(side="left", padx=6)
        ttk.Label(pad, text=_("Points")).pack(anchor="w", pady=(10, 2))
        rows = ttk.Treeview(pad, columns=("n", "x", "y"), show="headings",
                            height=8, selectmode="browse")
        for name, title, wide in (("n", "#", 44), ("x", "X", 82),
                                  ("y", "Y", 82)):
            rows.heading(name, text=title)
            rows.column(name, width=wide, anchor="e", stretch=False)
        rows.pack(fill="both", expand=True)
        cell = {}
        move = {}

        def show_row(i):
            x, y = mask.segments[seg][i]
            rows.set(str(i), "x", "%.2f" % x)
            rows.set(str(i), "y", "%.2f" % y)

        def refill(keep=None):
            origin.set("%.2f, %.2f" % msk_bounds(mask.segments[seg]))
            rows.delete(*rows.get_children())
            for i, (x, y) in enumerate(mask.segments[seg]):
                rows.insert("", "end", iid=str(i),
                            values=(i + 1, "%.2f" % x, "%.2f" % y))
            at = min(max(0, keep or 0), len(mask.segments[seg]) - 1)
            rows.selection_set(str(at))
            rows.focus(str(at))
            rows.see(str(at))

        def cell_shut(_evt=None):
            box = cell.pop("box", None)
            if box is not None:
                box.destroy()

        def cell_type(_evt=None):
            """Live: a number that parses moves the point as it is typed."""
            box = cell.get("box")
            if box is None:
                return
            try:
                said = round(float(box.get()), 2)
            except ValueError:
                return
            i, which = cell["at"]
            x, y = mask.segments[seg][i]
            now = (said, y) if which == "x" else (x, said)
            if now == mask.segments[seg][i]:
                return
            if not cell["kept"]:
                msk_remember()
                cell["kept"] = True
            mask.segments[seg][i] = now
            show_row(i)
            origin.set("%.2f, %.2f" % msk_bounds(mask.segments[seg]))
            edit_redraw()

        def cell_drop(_evt=None):
            """Escape: the point goes back to where the edit found it."""
            if cell.get("box") is None:
                return None
            if cell["kept"]:
                i = cell["at"][0]
                mask.segments[seg][i] = cell["was"]
                show_row(i)
                origin.set("%.2f, %.2f" % msk_bounds(mask.segments[seg]))
                edit_redraw()
            cell_shut()
            return "break"

        def cell_start(item, which):
            """An entry laid over the cell: edit the number in place."""
            cell_shut()
            over = rows.bbox(item, "#2" if which == "x" else "#3")
            if not over:
                return
            box = ttk.Entry(rows, justify="right")
            box.insert(0, rows.set(item, which))
            box.select_range(0, "end")
            box.place(x=over[0], y=over[1], width=over[2], height=over[3])
            box.focus_set()
            cell.update(box=box, at=(int(item), which), kept=False,
                        was=mask.segments[seg][int(item)])
            box.bind("<KeyRelease>", cell_type)
            box.bind("<Return>", lambda e: (cell_type(), cell_shut()))
            box.bind("<Escape>", cell_drop)
            box.bind("<FocusOut>", lambda e: (cell_type(), cell_shut()))

        def cell_open(evt):
            """Double-click: edit whichever cell was clicked."""
            item = rows.identify_row(evt.y)
            column = rows.identify_column(evt.x)
            if item and column in ("#2", "#3"):
                cell_start(item, "x" if column == "#2" else "y")

        def cell_here(_evt=None):
            """Return: edit the X of the row in front."""
            if rows.focus():
                cell_start(rows.focus(), "x")

        rows.bind("<Double-1>", cell_open)
        rows.bind("<Return>", cell_here)

        def mid_hold(_evt=None):
            """Where the shape was, so that a move never compounds."""
            move.update(was=list(mask.segments[seg]),
                        at=msk_bounds(mask.segments[seg]), kept=False)

        def mid_type(_evt=None):
            """Live: a pair that parses slides the whole shape."""
            if "was" not in move:
                return
            try:
                bits = [float(b) for b
                        in origin.get().replace(";", ",").split(",")]
            except ValueError:
                return
            if len(bits) != 2:
                return
            dx = round(bits[0], 2) - move["at"][0]
            dy = round(bits[1], 2) - move["at"][1]
            slid = [(round(x + dx, 2), round(y + dy, 2))
                    for x, y in move["was"]]
            if slid == mask.segments[seg]:
                return
            if not move["kept"]:
                msk_remember()
                move["kept"] = True
            mask.segments[seg] = slid
            for i in range(len(slid)):
                show_row(i)
            edit_redraw()

        def mid_done(_evt=None):
            move.pop("was", None)
            origin.set("%.2f, %.2f" % msk_bounds(mask.segments[seg]))

        mid.bind("<FocusIn>", mid_hold)
        mid.bind("<KeyRelease>", mid_type)
        mid.bind("<FocusOut>", mid_done)
        ttk.Button(pad, text=_("Close"), command=dlg.destroy).pack(
            side="right", pady=(12, 0))
        refill(point)
        # Number this shape's points on the canvas while the box
        # is open, so its rows can be matched to the handles.
        # Dropped however it is closed - the button, Escape, or
        # the window's own corner - which is why it hangs off
        # <Destroy> rather than off any one of them.
        state["mnumbers"] = seg
        edit_redraw()

        def shut(evt):
            if evt.widget is dlg:
                state.pop("mnumbers", None)
                edit_redraw()

        dlg.bind("<Destroy>", shut)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (root.winfo_rootx() + 120,
                                 root.winfo_rooty() + 120))
        state["dialog"] = dlg

    def do_msk_typein(evt):
        """Double-click a point: the same dialog, from the drawing.

        Not while a shape is being drawn. The second click lands on the
        point the first one just laid down, so what used to happen was
        the coordinate box opening on top of a half-drawn shape - and
        the double-click is the ordinary way to say "that is the last
        point", which is what it now does.
        """
        if state.get("mdrawing") is not None:
            do_msk_finish()
            return "break"
        what = msk_what_at(evt)
        if what is not None and what[0] == "point":
            msk_typein(*what[1])

    def msk_new_segment():
        """The next empty segment, or None with a word about why not."""
        mask = edit_mask()
        if mask is None:
            return None
        while len(mask.segments) < edit_segments():
            mask.segments.append([])
        for i, seg in enumerate(mask.segments):
            if not seg:
                return i
        messagebox.showinfo(
            _("Nothing left to draw in"),
            (_("This drawing already has %d shapes in it. Delete "
               "one before drawing another.")
             if edit_here() == "lim" else
             _("This mask already uses all %d segments the "
               "instrument has. Delete one before drawing "
               "another.")) % edit_segments())
        return None

    # ------------------------------------------------ undoing an edit
    # A mask is eight short lists of pairs, so a step back is a copy of
    # them and nothing cleverer is worth writing. Twenty deep: enough to
    # get out of a bad drag, and small enough that the copies cost
    # nothing worth measuring.
    UNDO_DEEP = 20

    def msk_shapes():
        """The mask's geometry, copied, or None when none is open."""
        mask = edit_mask()
        return None if mask is None else [list(seg) for seg in mask.segments]

    def msk_keep(shapes):
        """Put a kept copy of the geometry on the undo stack."""
        if shapes is None:
            return
        state[edit_key("dirty")] = True
        state.setdefault(edit_key("undo"), []).append(shapes)
        del state[edit_key("undo")][:-UNDO_DEEP]
        state[edit_key("redo")] = []                  # a new edit forks the future
        state["mrun"] = None                 # and ends any run of nudges
        msk_undo_buttons()

    def msk_remember():
        """Keep the mask as it stands, before something changes it.

        Called at the start of every edit rather than at the end: what
        undo has to put back is the state before, and after the change
        that is gone. A drag is the exception - it keeps its own copy at
        the press and hands it over at the release, so a press that only
        selected something is not an edit.
        """
        msk_keep(msk_shapes())

    def msk_forget():
        """A different mask is open; the old one's history is not its own."""
        state[edit_key("undo")], state[edit_key("redo")] = [], []
        state[edit_key("dirty")] = False
        # Nor is the old one's verdict: the instrument counted those
        # hits against whatever was in its segments a moment ago.
        state.pop("mhits", None)
        # And a mask opened is a mask to be looked at. Hiding is for
        # getting a shape out of the way of the trace under it, not a
        # setting to be inherited by whatever is opened next.
        state["mhide"].set(False)
        state["mnodots"].set(False)
        msk_undo_buttons()

    def msk_may_discard():
        """True if the mask on screen may be thrown away.

        Yes, No, Cancel, the way every other program asks: saving is
        offered rather than assumed, and Cancel means the drawing is
        left exactly as it was.
        """
        mask = edit_mask()
        if not state.get(edit_key("dirty")) or mask is None or not mask.filled():
            return True
        answer = messagebox.askyesnocancel(
            _("Save changes?"),
            _("%s has been changed. Save it before starting a new mask?")
            % (mask.name or _("This mask")), parent=root, default="yes")
        if answer is None:
            return False
        if answer:
            do_msk_save()
            return not state.get(edit_key("dirty"))     # a cancelled save cancels
        return True

    def msk_restore(shapes):
        mask = edit_mask()
        if mask is None:
            return
        mask.segments = [list(seg) for seg in shapes]
        state["mrun"] = None
        state["mpicks"] = []
        state.pop("mdrawing", None)
        state.pop("mhover", None)
        edit_redraw()

    def msk_undo_buttons():
        """Both tabs' pairs, each from its own drawing's history."""
        for key, btn in (state.get("undobtn") or {}).items():
            btn.state(["!disabled" if state.get(key)
                       else "disabled"])

    def do_msk_undo(_evt=None):
        if not state.get(edit_key("undo")):
            return
        state.setdefault(edit_key("redo"), []).append(msk_shapes())
        del state[edit_key("redo")][:-UNDO_DEEP]
        msk_restore(state[edit_key("undo")].pop())
        msk_undo_buttons()
        say(_("Undone"))

    def do_msk_redo(_evt=None):
        if not state.get(edit_key("redo")):
            return
        state.setdefault(edit_key("undo"), []).append(msk_shapes())
        del state[edit_key("undo")][:-UNDO_DEEP]
        msk_restore(state[edit_key("redo")].pop())
        msk_undo_buttons()
        say(_("Redone"))

    def msk_tool_changed():
        """A different tool is in hand: whatever the last one was in the
        middle of is finished, not carried over.

        And the mask comes back out of hiding, points and all. Picking
        up a tool is asking to edit the mask, and editing a mask nobody
        can see - or dragging handles that are not drawn - is the sort
        of thing that only makes sense to the person who wrote the
        checkbox.
        """
        state.pop("mdrawing", None)
        state.pop("mhover", None)
        state.pop("mcutfrom", None)
        state["mhide"].set(False)
        state["mnodots"].set(False)
        edit_canvas().focus_set()
        edit_redraw()
        msk_say_tool()

    def msk_say_tool():
        """What the tool in hand does, in the line along the bottom."""
        say({"pen": _("Click to place points; right-click, Enter or "
                      "Escape finishes the shape"),
             "move": _("Drag a point to move it, or an edge to move the "
                       "whole shape; arrow keys nudge what is selected"),
             "eraser": _("Click a point to delete it, or an edge to "
                         "delete the whole shape"),
             "cut": _("Click one point of a shape, then another, to cut "
                      "it in two between them")}[msk_tool()])

    def msk_tool():
        return state["mtool"].get()

    def msk_crosshair():
        """Redraw only the crosshair and its reading.

        The pointer moves a lot and the canvas is expensive to rebuild -
        a tenth-division grid alone is eight thousand items - so a move
        that changes nothing but where the crosshair is redraws nothing
        but the crosshair.
        """
        edit_canvas().delete("cross")
        at = state.get("mcrossat")
        if not state["mcross"].get() or at is None:
            return
        left, top, right, bottom = mask_frame()
        pick = plot_colours(None)
        cx, cy = at
        if not (left <= cx <= right and top <= cy <= bottom):
            return
        # The label's colour rather than the grid's: a crosshair the
        # same colour as the grid it is drawn over cannot be found.
        edit_canvas().create_line(left, cy, right, cy, fill=pick["label"],
                          dash=(4, 4), tags="cross")
        edit_canvas().create_line(cx, top, cx, bottom, fill=pick["label"],
                          dash=(4, 4), tags="cross")
        px, py = tds_msk.from_canvas(cx, cy, mask_frame())
        edit_canvas().create_text(right - 4, top + 4, anchor="ne",
                          fill=pick["label"],
                          text="%.2f, %.2f" % (px, py), tags="cross")

    def do_msk_hover(evt):
        """Follow the pointer: what is under it, and the crosshairs.

        What is under it is drawn highlighted, so a click is never a
        guess about which point or which edge it is going to take. The
        pen has no use for a whole shape - it acts on edges and points -
        so it is not offered one.
        """
        state["mcrossat"] = (evt.x, evt.y)
        was = state.get("mhover")
        now = msk_what_at(evt)
        if now is not None and now[0] == "shape" and msk_tool() == "pen":
            now = None
        state["mhover"] = now
        if (now != was or state.get("mdrawing") is not None
                or state.get("mcutfrom") is not None):
            # The crosshair now, the rebuild when the moving stops. The
            # crosshair is three items and the thing the eye is
            # following; the rebuild is five hundred and can wait a
            # frame. Leaving the crosshair to the rebuild made it lag
            # exactly where the pointer had most to say - over the
            # handles, where what is under it keeps changing.
            if state["mcross"].get():
                msk_crosshair()
            edit_soon()
        elif state["mcross"].get():
            msk_crosshair()

    def do_msk_leave(_evt=None):
        gone = state.pop("mcrossat", None) is not None
        gone = state.pop("mhover", None) is not None or gone
        if gone:
            edit_redraw()

    def edit_bindings(canvas):
        """Point the drawing tools at a canvas.

        Called for both of them. Which drawing a gesture acts on is
        decided when it arrives, by which tab is in front, so the same
        handlers serve the masks tab and the limits tab - see edit_here.
        """
        canvas.bind("<Motion>", do_msk_hover)
        canvas.bind("<Leave>", do_msk_leave)
        canvas.bind("<ButtonPress-1>", do_msk_press)
        canvas.bind("<B1-Motion>", do_msk_move)
        canvas.bind("<ButtonRelease-1>", do_msk_release)
        canvas.bind("<Double-Button-1>", do_msk_typein)
        canvas.bind("<Button-3>", do_msk_menu)
        canvas.bind("<Escape>", do_msk_finish)
        canvas.bind("<Return>", do_msk_finish)
        canvas.bind("<KP_Enter>", do_msk_finish)
        # "break", so the key stops here. Tk sends an unhandled key on
        # up to the toplevel, where Delete belongs to the file tab - and
        # a Delete pressed in the mask editor was reaching it and asking
        # the instrument to delete whatever the file list had selected.
        canvas.bind("<Delete>", lambda e: do_msk_remove(e) or "break")
        canvas.bind("<BackSpace>", lambda e: do_msk_remove(e) or "break")
        canvas.bind("<Control-x>", lambda e: do_msk_copy(e, cut=True))
        canvas.bind("<Control-c>", do_msk_copy)
        canvas.bind("<Control-v>", do_msk_paste)
        canvas.bind("<Control-z>", do_msk_undo)
        canvas.bind("<Control-y>", do_msk_redo)
        canvas.bind("<Control-Z>", do_msk_redo)     # Ctrl+Shift+Z as well
        # Up is up: percent counts from the bottom of the graticule
        # here, the way a person means it, and the flip to the
        # instrument's own upside-down numbering happens once, in
        # tds_msk.
        for key, dx, dy in (("Left", -1, 0), ("Right", 1, 0),
                            ("Up", 0, 1), ("Down", 0, -1)):
            canvas.bind("<%s>" % key,
                        lambda e, ax=dx, ay=dy: do_msk_nudge(ax, ay))
        canvas.configure(takefocus=True)
        canvas.bind("<Button-1>", lambda e: canvas.focus_set(), add="+")

    edit_bindings(mplot)
    msk_undo_buttons()

    # ------------------------------------------------------ the lists
    def msk_folder():
        """Where masks are kept on this computer.

        Beside the program rather than in a place chosen by a dialog:
        the point of a library is that it is always the same place.

        The shipped masks are copied in the first time, because the exe
        unpacks its bundled files to a temporary folder that is deleted
        on exit - a library there would be gone by the next run, and one
        that cannot be added to is not a library. Copied rather than
        read from both places so that renaming or deleting one works
        the same as for a mask somebody drew.
        """
        here = os.path.join(APPDIR, "masks")
        try:
            if not os.path.isdir(here):
                os.makedirs(here)
                came = resource("masks")
                if os.path.isdir(came) and came != here:
                    for name in os.listdir(came):
                        one = os.path.join(came, name)
                        if os.path.isfile(one):
                            shutil.copy2(one, os.path.join(here, name))
        except OSError as exc:
            log_note("masks", "%s unusable (%s)" % (here, exc))
            return None
        return here

    def msk_signal(name, path=None):
        """What the list says a mask is for, in a column's worth.

        A mask Tektronix shipped says it in its name; anything else says
        it in the setup saved beside it. Both answer the same question -
        what signal is this drawn for - so they share the column.
        """
        rate = tds_msk.standard(name)[1]
        if rate or not path:
            return rate
        try:
            fields = tds_set.parse(tds_set.contents(tds_set.beside(path)))
        except OSError:
            return ""
        secdiv = tds_set.number(fields, "secdiv")
        return "%s/div" % tds_wfm.eng(secdiv, "s") if secdiv else ""

    def msk_describe(mask):
        """The one-line shape of a mask, for the second column.

        Whether it is a pulse mask or an eye is worth a column of its
        own here: it decides whether the mask can go to an instrument as
        a limit template at all, and finding that out by pressing the
        button and being refused is a poor way to learn it.
        """
        segs = mask.filled()
        try:
            what = _("pulse") if tds_msk.kind(mask) == "pulse" else _("eye")
        except Exception:
            what = "?"
        return _("%(kind)s, %(segs)d seg, %(pts)d pts") % {
            "kind": what, "segs": len(segs), "pts": len(mask.points)}

    def do_msk_scan():
        """Re-read both libraries and the instrument's live segments."""
        here = msk_folder()
        mpc.delete(*mpc.get_children())
        state["mpcfiles"] = {}
        for name in sorted(os.listdir(here) if here else []):
            if not name.upper().endswith(tds_msk.SUFFIX):
                continue
            path = os.path.join(here, name)
            try:
                with open(path, "rb") as fh:
                    mask = tds_msk.load(fh.read(),
                                        name=os.path.splitext(name)[0])
            except Exception as exc:
                mpc.insert("", "end", iid=name,
                           values=(name, "", _("will not read: %s") % exc))
                continue
            state["mpcfiles"][name] = path
            mpc.insert("", "end", iid=name,
                       values=(mask.name or name, msk_signal(name, path),
                               msk_describe(mask)))
        msk_buttons()
        say(_("%d mask(s) on this computer") % len(state["mpcfiles"]))
        msk_scan_scope()

    def msk_scan_scope():
        """Ask the instrument what is in its eight segments, if there is
        an instrument.

        Skipped entirely when nothing is connected, so the Masks tab is
        usable with no instrument at all - a mask can be drawn on a
        train.
        """
        # Not gated on `busy`. This only queues work on the one worker
        # thread, which runs it in turn, and it releases nothing - so a
        # scan asked for while a transfer is running waits its turn
        # rather than being dropped.
        if state.get("cannot") is None:
            return
        w.submit("msk_live", lambda k: k.msk_live())

    def msk_say_live():
        """The line above the segment list: which mask is in there.

        Only this program's own doing can be known. The instrument holds
        eight lists of points and no name, so a mask somebody loaded
        from the front panel is a mask this cannot name - and saying so
        is better than naming the last one that went through here.
        """
        held = sum(state.get("mlivecounts") or [])
        name = state.get("msent")
        if name and held:
            lbl_mlivename.config(text=name)
        elif held:
            lbl_mlivename.config(text=_("not sent from here"))
        else:
            lbl_mlivename.config(text=_("nothing loaded"))

    def msk_show_live(segments):
        """Put the instrument's eight live segments into the bottom list.

        Point counts only. The coordinates are read back with every X
        the same constant, so showing them would be showing something
        that is not what is on the screen - see INSTRUMENT-NOTES.
        """
        mlive.delete(*mlive.get_children())
        state["mlivecounts"] = list(segments or [])
        if segments is None:
            mlive.insert("", "end", iid="none",
                         values=("-", _("this instrument has no mask "
                                        "subsystem")))
            lbl_mlivename.config(text="")
            return
        for i, count in enumerate(segments, 1):
            mlive.insert("", "end", iid="seg%d" % i,
                         values=(_("Segment %d") % i,
                                 _("%d point(s)") % count if count
                                 else _("empty")))
        msk_say_live()

    def do_msk_sort(box, column):
        """Sort a list by a column, and back again if it is already."""
        rows = [(box.set(iid, column), iid) for iid in box.get_children()]
        down = state.get("msort") == (str(box), column)
        rows.sort(reverse=down)
        state["msort"] = None if down else (str(box), column)
        for at, (_value, iid) in enumerate(rows):
            box.move(iid, "", at)

    def msk_buttons():
        """What the buttons can do with what is chosen.

        Note what is offered and what is not. Copying mask files to and
        from the instrument's disk is offered, because it is a file
        transfer and nothing parses the mask on the way - the X problem
        below lives in `MASK:MASK<n>:POINTSPCNT` and this route never
        goes near it. Sending a mask as a limit template is offered for
        the same reason: it goes into a reference as an envelope.

        What is *not* offered is writing the instrument's eight live
        segments, which is the one thing that does go through
        POINTSPCNT. Every X reads back as the same constant there, and
        until somebody has looked at a screen and settled whether that
        is the mask being stored wrongly or only reported wrongly, this
        program will not claim to have put a shape on an instrument.
        See INSTRUMENT-NOTES.
        """
        joined = state.get("cannot") is not None
        # The mask subsystem is Option 2C. Without it MASK:MASK<n> is not
        # a command this firmware has, so everything that reaches for it
        # is greyed - and everything that does not, which is the drawing
        # and the library, carries on working with no instrument in the
        # room at all.
        testable = joined and bool(state.get("masks"))
        btn_msend.config(state="normal" if testable
                         and state.get("mask") is not None else "disabled")
        btn_mload.config(state="normal" if joined else "disabled")
        btn_mclear.config(state="normal" if joined else "disabled")
        btn_msave.config(state="normal" if state.get("mask") is not None
                         else "disabled")
        # The setup comes off the instrument and is written beside the
        # mask's own file, so it needs both to exist.
        btn_msetup.config(state="normal" if joined and state.get("mask")
                          is not None and (state.get("mask").origin or "")
                          else "disabled")
        # Starting a measurement is the instrument's job, so it needs an
        # instrument and something to measure against.
        btn_mmeasure.config(state="normal" if testable
                            and state.get("mask") is not None
                            else "disabled")
        btn_mdrop.config(state="normal" if (state.get("mshot")
                                            or state.get("mwave")
                                            or state.get("mhits"))
                         else "disabled")
        btn_mdel.config(state="normal" if mpc.selection() else "disabled")

    def do_msk_open(_evt=None):
        """Double-click a row: open that mask in the right pane."""
        chosen = list(mpc.selection())
        if not chosen:
            return
        path = (state.get("mpcfiles") or {}).get(chosen[0])
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
            mask = tds_msk.load(raw,
                                name=os.path.splitext(chosen[0])[0])
            state["mipattern"] = tds_msk.looks_like_msk(raw)
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                     % (_("Could not open that mask"),
                                        exc))
            return
        mask.origin = path
        state["mask"] = mask
        msk_forget()
        state["mpicks"] = []
        state.pop("mdrawing", None)
        draw_mask()
        msk_buttons()
        say(_("Opened %s") % os.path.basename(path))

    def msk_click_off(evt):
        """A press below the last row clears the selection.

        What Explorer does, and what anybody expects of a list: there is
        no other way to get back to nothing selected once something is,
        short of ctrl-clicking the row again. Only when the press is on
        no row at all, so it cannot undo an ordinary click.
        """
        if not mpc.identify_row(evt.y):
            mpc.selection_remove(*mpc.selection())
            msk_buttons()

    mpc.bind("<Button-1>", msk_click_off, add="+")
    mpc.bind("<Double-Button-1>", do_msk_open)
    mpc.bind("<<TreeviewSelect>>", lambda e: msk_buttons())

    def do_msk_load():
        """Read the instrument's eight segments back into the editor.

        The other direction of the » button, and the reason the disk
        library went: what a mask is *for* is those eight segments, and
        a file on the instrument's own disk was a file the instrument
        itself could not read.
        """
        if state["busy"] or state.get("cannot") is None:
            return
        if not msk_may_discard():
            return
        busy(True, "wait")
        say(_("Reading the instrument's mask segments ..."))
        w.submit("msk_read", lambda k: k.msk_read())

    def do_msk_clear():
        """Empty the instrument's eight segments, having asked first."""
        if state["busy"] or state.get("cannot") is None:
            return
        if not messagebox.askyesno(
                _("Empty the instrument's segments?"),
                _("All eight of the instrument's mask segments are "
                  "emptied and it stops drawing a mask. What is on this "
                  "computer is untouched."),
                parent=root, default="no"):
            return
        busy(True, "wait")
        say(_("Emptying the instrument's mask segments ..."))
        w.submit("msk_clear", lambda k: k.msk_clear())

    def msk_took_live(replies):
        """Put what the segments answered into the editor as one mask."""
        if replies is None:
            messagebox.showinfo(
                _("No mask segments"),
                _("This instrument has no mask subsystem to read."))
            return
        mask = tds_msk.Mask.from_scpi(replies,
                                      name=_("From the instrument"))
        if not mask.filled():
            say(_("The instrument's mask segments are empty"))
            return
        # It arrives with no name - the instrument holds points and
        # nothing else - so it is given one here and becomes a file on
        # this computer straight away. A mask read off an instrument and
        # left unsaved is a mask that goes when the next one is read.
        here = msk_folder()
        while True:
            said = simpledialog.askstring(
                _("Name this mask"),
                _("The instrument holds points and no name. What should "
                  "this mask be called on this computer?"),
                initialvalue=state.get("msent") or _("From the instrument"),
                parent=root)
            if said is None:
                say(_("Nothing was loaded"))
                return
            said = said.strip()
            if not said:
                continue
            leaf = tds_msk.clean_name(said, [])
            path = os.path.join(here, leaf) if here else None
            if path and os.path.exists(path) and not messagebox.askyesno(
                    _("Replace it?"),
                    _("%s is already in the library. Replace it?") % leaf,
                    parent=root, default="no"):
                continue
            break
        mask.name = said
        state["mipattern"] = False
        try:
            with open(path, "wb") as fh:
                fh.write(mask.to_bytes())
            mask.origin = path
        except (OSError, TypeError) as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                 % (_("Could not save"), exc))
            mask.origin = None
        state["mask"] = mask
        msk_forget()
        state["mpicks"] = []
        state.pop("mdrawing", None)
        draw_mask()
        msk_buttons()
        do_msk_scan()
        say(_("Loaded %(count)d segment(s) from the instrument as "
              "%(name)s") % {"count": len(mask.filled()),
                             "name": os.path.basename(mask.origin or "")
                             or mask.name})
        msk_say_live()

    def do_msk_new():
        if not msk_may_discard():
            return
        state["mask"] = tds_msk.Mask(name=_("New mask"))
        msk_forget()
        state["mipattern"] = False
        state["mpicks"] = []
        state.pop("mdrawing", None)
        draw_mask()
        msk_buttons()
        msk_say_tool()

    def msk_legal(mask, what):
        """True if this mask may leave the editor, with a word if not.

        Drawing is deliberately not policed - somebody halfway through a
        redesign has eleven shapes and two concave ones for a minute,
        and being stopped at every click would be unusable. What is
        policed is the way out: saving it or sending it says this is a
        mask, and a mask no instrument can hold is not one.
        """
        wrong = mask.complaints()
        if not wrong:
            return True
        messagebox.showerror(
            _("Error"),
            "%s\n\n%s\n\n%s"
            % (what, "\n".join(u"\u2022 " + w for w in wrong),
               _("Fix these and try again. Nothing has been changed.")))
        return False

    def do_msk_save():
        mask = state.get("mask")
        if mask is None:
            return
        if not msk_legal(mask, _("This mask cannot be saved as it is:")):
            return
        here = msk_folder()
        if not here:
            messagebox.showerror(_("Error"),
                                 _("The masks folder could not be made."))
            return
        # The system's own save dialog, so the library folder is a
        # starting point rather than a cage, and so the format is
        # chosen by the extension the way it is everywhere else. It
        # opens on whichever format the mask came from: an i-Pattern
        # mask opened, nudged and saved should still be one that
        # i-Pattern and every other TTiP tool can read.
        as_ip = bool(state.get("mipattern"))
        # An i-Pattern file stores its points as a set and this program
        # puts them back in order by sorting them about their middle,
        # which recovers a convex shape exactly and a concave one only
        # by luck. Measured: a concave L saved as i-Pattern and read
        # back is the same six points joined up differently. So say so
        # while there is still a choice to make.
        concave = [n for n, seg in mask.filled()
                   if not tds_msk.convex_polygon(seg)]
        if as_ip and concave:
            if not messagebox.askyesno(
                    _("Save as an i-Pattern mask?"),
                    _("Segment(s) %(which)s are concave, and an i-Pattern "
                      "file keeps a mask's points without their order - "
                      "so a concave shape does not come back the same "
                      "way round.\n\nThis program's own format keeps "
                      "them exactly. Save as i-Pattern anyway?")
                    % {"which": ", ".join(str(n) for n in concave)},
                    parent=root, default="no"):
                as_ip = False
        # One entry, not two: both formats use .MSK, so a file type
        # box could not tell them apart by extension and would be a
        # choice that does nothing.
        kinds = [(_("Mask (*.MSK)"), "*.MSK"), (_("All files"), "*.*")]
        path = filedialog.asksaveasfilename(
            parent=root, title=_("Save mask as"), initialdir=here,
            initialfile=os.path.basename(mask.origin or "")
            or tds_msk.clean_name(mask.name or _("New mask"), []),
            defaultextension=tds_msk.SUFFIX, filetypes=kinds)
        if not path:
            return
        name = os.path.basename(path)
        mask.name = os.path.splitext(name)[0]
        try:
            with open(path, "wb") as fh:
                fh.write(tds_msk.save_bytes(mask, as_ip))
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                     % (_("Could not save"), exc))
            return
        mask.origin = path
        state["mdirty"] = False
        do_msk_scan()
        say(_("Saved %s") % name)

    def do_msk_delete():
        chosen = [(state.get("mpcfiles") or {}).get(n)
                  for n in mpc.selection()]
        chosen = [p for p in chosen if p]
        if not chosen:
            return
        if not messagebox.askyesno(
                _("Delete mask"),
                _("Delete %d mask(s) from this computer?\n\n%s")
                % (len(chosen),
                   "\n".join(os.path.basename(p) for p in chosen)),
                default="no", parent=root):
            return
        for path in chosen:
            try:
                os.remove(path)
            except OSError as exc:
                messagebox.showerror(_("Error"), "%s\n\n%s"
                                         % (_("Could not delete"), exc))
                break
        do_msk_scan()

    def do_msk_trace():
        """Put a trace behind the mask, to draw against or to check.

        Any waveform this program can read: what was captured on the
        Waveforms tab, or a file from the PC in any of the formats it
        loads.
        """
        held = shown_waves()
        if held and messagebox.askyesno(
                _("Use the captured trace?"),
                _("Put %s from the Waveforms tab behind the mask?\n\n"
                  "No opens a file instead.") % wave_name(held[0]),
                parent=root):
            state["mwave"] = held[0]
            state["mview"] = tds_wfm.PlotView([held[0]])
            draw_mask()
            return
        path = filedialog.askopenfilename(
            parent=root, title=_("Load trace"),
            filetypes=[(_("Waveform files (*.isf *.csv *.tdw *.wfm)"),
                        "*.isf *.csv *.tdw *.wfm"),
                       (_("All files"), "*.*")])
        if not path:
            return
        try:
            wave = tds_wfm.load(path)
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                     % (_("Could not load that file"),
                                        exc))
            return
        state["mwave"] = wave
        state["mview"] = tds_wfm.PlotView([wave])
        draw_mask()
        say(_("Loaded %s behind the mask") % os.path.basename(path))

    def msk_setup_path(mask=None):
        """The setup file that goes with the open mask, if it has one."""
        mask = mask if mask is not None else state.get("mask")
        origin = getattr(mask, "origin", None) if mask is not None else None
        if not origin:
            return None
        beside = tds_set.beside(origin)
        return beside if os.path.exists(beside) else None

    def msk_unit_interval(mask=None):
        """The bit period the open mask was drawn for, in seconds.

        Written into the setup beside it as one machine-readable REM
        line, because every other line there is prose for a person and
        guessing a bit period from an eye's width means trusting the
        shape to be the width this program happens to draw.
        """
        setup = msk_setup_path(mask)
        if not setup:
            return None
        found = re.search(r':REM\s+"UI\s+([0-9.eE+-]+)"', tds_set.contents(setup))
        return float(found.group(1)) if found else None

    def msk_differential(mask=None):
        """Whether this mask is drawn for a signal across a pair.

        The other machine-readable REM line. It matters because a
        differential signal reaches the instrument two different ways -
        one differential probe into one channel, or two probes and the
        difference taken in MATH1 - and the instrument has to be set up
        differently for each. Which is on the bench is not something it
        can be asked, so the user is.
        """
        setup = msk_setup_path(mask)
        return bool(setup and ':REM "DIFFERENTIAL"' in tds_set.contents(setup))

    def msk_seconds_per_div(mask=None):
        """The sweep the mask was drawn against, or None.

        Sending a mask no longer touches the instrument, so Start
        measurement is where the timebase has to come from. Without it
        the eye is built on whatever was dialled up: measured, a USB
        full speed mask against 1 us/div gave 116,921,796 hits over
        345,839 acquisitions, and no clock was found either.
        """
        setup = msk_setup_path(mask)
        if not setup:
            return None
        try:
            return float(tds_set.parse(tds_set.contents(setup)).get("secdiv"))
        except (TypeError, ValueError):
            return None

    def msk_volts_per_div(mask=None):
        """The volts a division this mask was drawn against, or None.

        Read out of the setup's own channel scale, which is the number
        the shapes were computed from. Needed only for MATH1: a math
        waveform's vertical scale cannot be set on this family - there
        is no command for it anywhere, not in the MATH group and not in
        a setup either - and a 784D gives CH1-CH2 five times the
        channel's own scale, measured. So the difference is drawn here
        at the scale the mask means rather than the one the instrument
        happens to be showing it at.
        """
        setup = msk_setup_path(mask)
        if not setup:
            return None
        try:
            return float(tds_set.parse(tds_set.contents(setup)).get("scale"))
        except (TypeError, ValueError):
            return None

    def do_msk_measure():
        """Set the instrument up to judge the mask, and start counting.

        Separate from sending, because sending is eight lists of points
        and this is the sweep, the trigger and the display. A mask can
        sit in an instrument for a while before anybody wants a verdict
        from it, and changing somebody's timebase the moment they press
        an arrow is not what an arrow means.

        For an eye mask it asks the two things it cannot find out - what
        is on the end of the probes, and whether there is a clock - and
        then does the rest by looking. For any other mask there is
        nothing to set up and it only starts the tally.
        """
        mask = state.get("mask")
        if state["busy"] or mask is None or state.get("cannot") is None:
            return
        source = mask.source or (state.get("wsources") or ["CH1"])[0]
        bit = msk_unit_interval()
        if bit is None:
            # A mask with no unit interval draws no eye - a pulse
            # template, a burst envelope - so there is no bit to centre
            # and nothing to delay. The display still has to be set:
            # this used to start the counter and leave the glass as the
            # last mask left it, which meant no DPO at all and, after
            # an eye mask, a delayed window still on.
            busy(True, "wait")
            say(_("Setting the instrument up to count ..."))
            w.submit("msk_count", lambda k: k.msk_watch())
            return
        msk_ask_eye(source, bit, list(mask.filled()))

    def msk_ask_eye(source, bit, segments):
        """The two questions, then the whole setup.

        Neither can be found by asking the instrument: it cannot see
        what is on the end of a probe, and an input with nothing plugged
        in reads the same as one carrying a clock that is switched off.
        Everything else is found by looking, including which input the
        clock is on - a clock is by definition the input you do not want
        on the graticule, so a list of what is displayed can never
        contain it.
        """
        dlg = tk.Toplevel(root)
        dlg.title(_("Start measurement"))
        dlg.transient(root)
        try:
            dlg.iconbitmap(resource("app.ico"))
        except Exception:
            pass
        pad = ttk.Frame(dlg, padding=10)
        pad.pack(fill="both", expand=True)
        probe = tk.StringVar(value="probe")
        trig = tk.StringVar(value="clock")
        ttk.Label(pad, wraplength=460, text=_(
            "The instrument will be set up for an eye: the sweep slid "
            "so the middle of a bit lands on the mask, DPO on where it "
            "is fitted, and the hit counter started.")).pack(
                anchor="w", pady=(0, 8))
        if msk_differential():
            box = ttk.LabelFrame(pad, text=_("This signal is probed"),
                                 padding=8)
            box.pack(fill="x")
            ttk.Radiobutton(box, variable=probe, value="probe", text=_(
                "with one differential probe, into a single "
                "channel")).pack(anchor="w")
            ttk.Radiobutton(box, variable=probe, value="math", text=_(
                "with two probes on CH1 and CH2, differenced in "
                "MATH1")).pack(anchor="w")
            ttk.Label(box, wraplength=440, foreground="#555", text=_(
                "MATH1 is defined as CH1-CH2 and judged here rather "
                "than by the instrument: its mask counter takes a "
                "channel and refuses a math waveform, and DPO switches "
                "math off, so that route runs on persistence. The "
                "instrument also scales a math waveform itself - there "
                "is no command for it - so on its own graticule the "
                "difference will not line up with the mask. The "
                "picture here does.")).pack(anchor="w", pady=(4, 0))
        box = ttk.LabelFrame(pad, text=_("and triggered from"), padding=8)
        box.pack(fill="x", pady=(8, 0))
        line = ttk.Frame(box)
        line.pack(anchor="w", fill="x")
        ttk.Radiobutton(line, variable=trig, value="clock", text=_(
            "a clock at the bit rate, on")).pack(side="left")
        # Named rather than hunted for. Looking costs a second an input
        # and puts each one on the graticule while it looks, and the
        # person at the bench plugged the clock in and knows where it
        # went. "Find it" is kept for when they did not.
        where = tk.StringVar(value=state.get("mclock") or "CH2")
        inputs = [n for n in ("CH1", "CH2", "CH3", "CH4") if n != source]
        pick_clock = ttk.Combobox(line, textvariable=where, width=10,
                                  state="readonly",
                                  values=inputs + [_("find it")])
        pick_clock.pack(side="left", padx=(6, 0))
        ttk.Radiobutton(box, variable=trig, value="data", text=_(
            "the data itself")).pack(anchor="w", pady=(4, 0))
        ttk.Label(box, wraplength=440, foreground="#555", text=_(
            "The named input is switched on just long enough to check "
            "there is a clock at the bit rate on it, and the sweep is "
            "then slid so the middle of a bit lands on the mask. "
            "Triggering on the data gives a pseudo-eye: the "
            "instrument's own time base jitter is added to the "
            "signal's, and the sweep is delayed past the trigger so "
            "that both rails reach the middle of the screen."
        )).pack(anchor="w", pady=(4, 0))

        def go():
            dlg.destroy()
            # Remembered rather than only passed on: Capture screen is
            # a separate press later, and on the math route it has to
            # read MATH1 and leave the instrument's own tally alone,
            # since that can only be counting one leg of the pair.
            state["mmath"] = probe.get() == "math"
            # False for none, an input's name for that input, True to
            # go looking. Remembered so the next mask offers the same
            # channel rather than starting at CH2 every time.
            if trig.get() != "clock":
                asked = False
            elif where.get() in inputs:
                asked = state["mclock"] = where.get()
            else:
                asked = True
            state["eyeing"] = True
            busy(True, "wait")
            say(_("Setting the instrument up for an eye ..."))
            w.submit("msk_eye",
                     lambda k, s=source, b=bit, segs=segments,
                     m=state["mmath"], c=asked,
                     d=msk_seconds_per_div():
                     k.msk_eye(s, b, segs, math=m, clocked=c, secdiv=d))

        row = ttk.Frame(pad)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text=_("Start"), command=go).pack(side="right")
        ttk.Button(row, text=_("Cancel"),
                   command=dlg.destroy).pack(side="right", padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (root.winfo_rootx() + 80,
                                 root.winfo_rooty() + 80))
        state["dialog"] = dlg

    def do_msk_setup():
        """Write the instrument's settings beside the open mask.

        Read from the instrument rather than made up, because the point
        of the file is to record what the mask was drawn against. The
        mask has to have been saved first: the setup goes beside it, and
        beside nothing is nowhere.
        """
        mask = state.get("mask")
        if mask is None or state["busy"]:
            return
        if not mask.origin:
            messagebox.showinfo(
                _("Save the mask first"),
                _("A setup is written beside the mask's own file, under "
                  "the same name. Save the mask, then save its setup."))
            return
        busy(True, "wait")
        say(_("Reading the instrument's settings ..."))
        w.submit("set_read", lambda k, s=mask.source or "CH1":
                 k.set_read(s))

    # What the setup dialog offers, in the order it offers it. Only the
    # fields somebody drawing a mask has a reason to change: the rest of
    # what the instrument said is written as it was read.
    SETUP_FIELDS = (
        ("secdiv", "Seconds a division"),
        ("scale", "Volts a division"),
        ("position", "Vertical position, divisions"),
        ("offset", "Vertical offset, volts"),
        ("length", "Record length, points"),
        ("tposition", "Trigger position, percent"),
        ("tlevel", "Trigger level, volts"),
        ("coupling", "Coupling"),
        ("impedance", "Input impedance"),
        ("probe", "Probe attenuation"),
    )

    def msk_setup_edit(fields):
        """Show what the instrument said, and let it be changed.

        The instrument is the starting point rather than the answer. A
        mask is often drawn for a signal nobody has on the bench yet -
        a specification, not a measurement - and the setup beside it
        has to say what the mask means, not what happened to be on the
        screen when it was written.

        The unit interval and the differential flag are here and
        nowhere else: no instrument setting records either, and the eye
        setup cannot be done without the first of them.
        """
        mask = state.get("mask")
        if mask is None or not mask.origin:
            return
        # What is already beside the mask wins over what the instrument
        # says, for these two: somebody who has typed a unit interval
        # once should not have to type it again to save a setup. Read
        # through the same two helpers the eye setup reads them with,
        # because they live in REM lines and tds_set.parse steps over
        # those - they are notes to this program, not commands.
        was_ui = msk_unit_interval(mask)
        was_diff = msk_differential(mask)
        dlg = tk.Toplevel(root)
        dlg.title(_("Save setup"))
        dlg.transient(root)
        try:
            dlg.iconbitmap(resource("app.ico"))
        except Exception:
            pass
        pad = ttk.Frame(dlg, padding=10)
        pad.pack(fill="both", expand=True)
        ttk.Label(pad, wraplength=420, text=_(
            "Read from the instrument. Change anything the mask means "
            "rather than what happened to be set, then save it beside "
            "%s.") % (mask.name or _("this mask"))).pack(anchor="w",
                                                         pady=(0, 8))
        rows = ttk.Frame(pad)
        rows.pack(fill="x")
        boxes = {}
        for at, (key, english) in enumerate(SETUP_FIELDS):
            says(ttk.Label(rows, text=english),
                 english).grid(row=at, column=0, sticky="w", pady=1)
            box = ttk.Entry(rows, width=16)
            box.insert(0, str(fields.get(key, "")))
            box.grid(row=at, column=1, sticky="w", padx=8, pady=1)
            boxes[key] = box
        at = len(SETUP_FIELDS)
        says(ttk.Label(rows, text="Unit interval, seconds"),
             "Unit interval, seconds").grid(row=at, column=0, sticky="w",
                                            pady=(8, 1))
        ui = ttk.Entry(rows, width=16)
        ui.insert(0, "" if was_ui is None else "%g" % was_ui)
        ui.grid(row=at, column=1, sticky="w", padx=8, pady=(8, 1))
        diff = tk.BooleanVar(value=bool(was_diff))
        says(ttk.Checkbutton(rows, variable=diff),
             "The signal is differential").grid(row=at + 1, column=0,
                                                columnspan=2, sticky="w",
                                                pady=1)
        note = ttk.Label(pad, wraplength=420, foreground="#555", text=_(
            "The unit interval is the bit period the mask is drawn for. "
            "An eye mask needs it; any other mask can leave it empty."))
        note.pack(fill="x", pady=(8, 0))

        def go():
            out = dict(fields)
            for key, box in boxes.items():
                said = box.get().strip()
                if said:
                    out[key] = said
                else:
                    out.pop(key, None)
            bit = ui.get().strip()
            if bit:
                try:
                    out["ui"] = float(bit)
                except ValueError:
                    messagebox.showinfo(
                        _("That is not a number"),
                        _("The unit interval is a time in seconds, for "
                          "example 8.0e-9 for 125 Mbit/s."), parent=dlg)
                    return
            out["differential"] = bool(diff.get())
            dlg.destroy()
            msk_setup_write(out)

        row = ttk.Frame(pad)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text=_("Save"), command=go).pack(side="right")
        ttk.Button(row, text=_("Cancel"),
                   command=dlg.destroy).pack(side="right", padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (root.winfo_rootx() + 100,
                                 root.winfo_rooty() + 60))
        state["dialog"] = dlg

    def msk_setup_write(fields):
        """Put what the instrument said into a file beside the mask."""
        mask = state.get("mask")
        if mask is None or not mask.origin:
            return
        path = tds_set.beside(mask.origin)
        if os.path.exists(path) and not messagebox.askyesno(
                _("Replace the setup?"),
                _("%s is already there. Replace it with what the "
                  "instrument is set to now?") % os.path.basename(path),
                parent=root, default="no"):
            say(_("The setup was left as it was"))
            return
        try:
            with open(path, "w") as fh:
                fh.write(tds_set.text(
                    fields, os.path.splitext(os.path.basename(path))[0],
                    said=(state.get("idn") or "").strip(),
                    when=time.strftime("%Y-%m-%d")))
        except OSError as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                 % (_("Could not save"), exc))
            return
        say(_("Saved %(name)s - %(what)s")
            % {"name": os.path.basename(path),
               "what": tds_set.summary(fields)})
        do_msk_scan()
        say_mask()

    def msk_setup_landed(payload):
        """What the instrument did with a setup, against what was asked.

        Three kinds of answer, needing three different things said. A
        timebase or a gain it would not give can be answered by moving
        the mask, because a mask is in percent of the graticule and
        nothing else. Volts per division it will not reach is usually
        the front end rather than the firmware, and a bigger probe
        lifts it - the ceiling is the input's own times the probe. Any
        other value it replaced is the instrument saying no, and
        nothing on this side makes it yes.
        """
        if payload.get("refused"):
            say(_("Setup sent, but the instrument refused "
                  "%(count)d of it: %(what)s")
                % {"count": len(payload["refused"]),
                   "what": "; ".join(payload["refused"])})
        else:
            say(_("Setup sent - %d line(s)") % payload["sent"])
        asked = state.pop("msetup", None) or {}
        got = payload.get("got") or {}
        moved = tds_set.differences(asked, got)
        mask = state.get("mask")
        if not moved or mask is None:
            return
        source = asked.get("source", "CH1")

        def worth(key, value):
            """A value in the units it is in, or as it came."""
            number = tds_set.number({key: value}, key)
            if number is None:
                return str(value)
            if key == "secdiv":
                return "%s/div" % tds_wfm.eng(number, "s")
            if key == "scale":
                return "%s/div" % tds_wfm.eng(number, "V")
            return str(value)

        told = "\n".join(
            u"• %s: %s → %s" % (tds_set.command(key, source),
                                worth(key, was), worth(key, now))
            for key, was, now in moved)
        # The two axes are measured from different places: across, the
        # trigger point, which is what stays put when the timebase
        # changes; up, the channel's zero volts, which is what stays
        # put when the gain does.
        across, up, rest = tds_set.stretch(moved)
        at_x = tds_set.number(got, "tposition")
        if at_x is None:
            at_x = tds_set.number(asked, "tposition")
        sits = tds_set.number(got, "position")
        if sits is None:
            sits = tds_set.number(asked, "position") or 0.0
        about = (50.0 if at_x is None else at_x,
                 50.0 + sits * 100.0 / tds_wfm.DIVS_Y)
        probe = tds_set.probe_wanted(asked, got)
        if probe:
            told += "\n\n" + (
                _("A %(probe)gX probe on %(channel)s would reach "
                  "%(volts)s.")
                % {"probe": probe, "channel": source,
                   "volts": worth("scale", asked.get("scale"))})
        if rest and across == 1.0 and up == 1.0:
            messagebox.showinfo(
                _("The instrument changed the setup"), "%s\n\n%s\n\n%s"
                % (_("It took the setup, then quietly replaced these:"),
                   told, _("Nothing here can be answered by moving the "
                           "mask.")))
            return
        fitted = tds_msk.stretched(mask.segments, across, up, about)
        if not tds_msk.on_graticule(fitted):
            messagebox.showinfo(
                _("The instrument changed the setup"), "%s\n\n%s\n\n%s"
                % (_("It took the setup, then quietly replaced these:"),
                   told, _("Stretched to match, this mask would run off "
                           "the graticule, so it is left as it is.")))
            return
        if not messagebox.askyesno(
                _("The instrument changed the setup"), "%s\n\n%s\n\n%s"
                % (_("It took the setup, then quietly replaced these:"),
                   told,
                   _("A mask is held in percent of the graticule, so it "
                     "can be stretched to mean what it meant before. "
                     "Stretch this one to match? Undo puts it back.")),
                parent=root, default="yes"):
            return
        msk_remember()
        mask.segments[:] = [[(round(x, 2), round(y, 2)) for x, y in seg]
                            for seg in fitted]
        state["mpicks"] = []
        draw_mask()
        say_mask()
        say(_("Mask stretched to what the instrument gave - send it again"))

    def do_msk_send():
        """Send the open mask to the instrument, whichever way suits it.

        As a real mask where the instrument has Option 2C, and as a
        limit template where it has not - or whichever the user has
        chosen on the tab. See INSTRUMENT-NOTES for both routes.
        """
        mask = state.get("mask")
        if mask is None or state["busy"]:
            return
        if not mask.filled():
            messagebox.showinfo(_("Nothing to send"),
                                _("This mask has no segments with points "
                                  "in them yet."))
            return
        if not msk_legal(mask, _("This mask cannot be sent as it is:")):
            return
        # The instrument takes these points and keeps them - a readback
        # gives them back unchanged - but draws some shapes its own way.
        # Better said now than found on the graticule afterwards.
        # The setup beside it, if there is one. Offered rather than
        # sent: it changes the timebase and the channel, which is the
        # whole point of it, and doing that unasked to an instrument
        # somebody is watching would be rude.
        setup = msk_setup_path(mask)
        if setup and messagebox.askyesno(
                _("Send its setup too?"),
                "%s\n\n%s\n\n%s"
                % (_("%s has a setup beside it:")
                   % (mask.name or _("This mask")),
                   tds_set.summary(tds_set.parse(tds_set.contents(setup)))
                   or os.path.basename(setup),
                   _("Send that to the instrument first? It changes the "
                     "channel and the timebase to what the mask was "
                     "drawn against.")),
                parent=root, default="yes"):
            asked = tds_set.parse(tds_set.contents(setup))
            lines = [ln.lstrip(":") for ln in tds_set.contents(setup).splitlines()
                     if ln.strip() and not ln.strip().lstrip(":").upper()
                     .startswith("REM")]
            # Kept so the answer can be compared with the question when
            # the instrument has finished with it.
            state["msetup"] = asked
            say(_("Sending the setup ..."))
            w.submit("set_send", lambda k, ln=lines,
                     s=asked.get("source", "CH1"): k.set_send(ln, s))
        odd = mask.redrawn()
        if odd and not messagebox.askyesno(
                _("Send anyway?"),
                "%s\n\n%s\n\n%s"
                % (_("The instrument will not draw this mask the way it "
                     "is drawn here:"),
                   "\n".join(u"• segment %d: %s" % (n, why)
                             for n, why in odd),
                   _("It stores the points exactly, and joins them up "
                     "its own way. Cutting the shape into simpler "
                     "pieces avoids it. Send anyway?")),
                parent=root, default="no"):
            return
        live = [s for s in (state.get("wsources") or [])
                if not s.startswith("REF")]
        refs = list(state.get("wrefs") or tds_wfm.REFS)
        dlg = tk.Toplevel(root)
        dlg.title(_("Send as a mask"))
        dlg.transient(root)
        try:
            dlg.iconbitmap(resource("app.ico"))
        except Exception:
            pass
        pad = ttk.Frame(dlg, padding=10)
        pad.pack(fill="both", expand=True)
        ttk.Label(pad, wraplength=460, text=_(
            "%s goes into the instrument's eight mask segments and is "
            "drawn on its graticule. This instrument has the mask "
            "option fitted.")
            % (mask.name or _("This mask"))).pack(anchor="w", pady=(0, 8))
        against = tk.StringVar(value=live[0] if live else "CH1")
        rows = ttk.Frame(pad)
        rows.pack(fill="x")
        ttk.Label(rows, text=_("Signal")).grid(row=1, column=0, sticky="w",
                                             pady=(6, 0))
        ttk.Combobox(rows, textvariable=against, width=8, state="readonly",
                     values=live or ["CH1"]).grid(row=1, column=1,
                                                  sticky="w", padx=6,
                                                  pady=(6, 0))
        # Sending is only the eight segments. Setting the instrument up
        # to show an eye against them is a separate press, because it
        # changes the sweep, the trigger and the display - and doing
        # that to somebody's instrument on the way past, unasked, is
        # how it used to work and was wrong.
        if msk_unit_interval(mask):
            ttk.Label(pad, wraplength=460, foreground="#555", text=_(
                "This is an eye mask. Sending it puts the shapes in "
                "and leaves the rest of the instrument alone - press "
                "Start measurement to set the sweep, the trigger and "
                "the display up for an eye and begin counting.")).pack(
                    fill="x", pady=(10, 0))
        ttk.Label(pad, wraplength=460, foreground="#555", text=_(
            "Whatever mask the instrument is holding now is replaced, "
            "all eight segments of it.")).pack(fill="x", pady=(10, 0))

        def go():
            mask.source = against.get()
            dlg.destroy()
            state["msent"] = mask.name or _("untitled")
            busy(True, "wait")
            say(_("Sending %(name)s to the instrument's mask "
                  "segments ...") % {"name": mask.name or _("mask")})
            w.submit("msk_send", lambda k, ln=mask.to_scpi():
                     k.msk_send(ln))

        row = ttk.Frame(pad)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text=_("Send"), command=go).pack(side="right")
        ttk.Button(row, text=_("Cancel"),
                   command=dlg.destroy).pack(side="right", padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (root.winfo_rootx() + 80,
                                 root.winfo_rooty() + 80))
        state["dialog"] = dlg

    # after(0) rather than a call here: this runs while the window is
    # still being built, and the status bar it writes to does not exist
    # yet.
    root.after(0, do_msk_scan)


    # ----------------------------------------------------------- limits
    # The other way to judge a signal, and the one every instrument in
    # this family has whether the mask option was fitted or not: show it
    # one that is known good, say how far a later one may wander, and it
    # writes the envelope itself. No polygons, no percent of the
    # graticule, nothing to draw before there is something to test.
    #
    # Kept apart from the masks tab because it is a different job with
    # different words. A mask is a shape somebody draws; a template is a
    # signal the instrument learns. Sharing one editor between them was
    # what made the mask tab's limit route hard to explain.
    limtab = ttk.Frame(tabs)
    tabs.add(limtab, text=_("Limits"))
    named(limtab, "Limits")

    ltop = ttk.Frame(limtab)
    ltop.pack(fill="x", padx=2, pady=(6, 2))
    lrow1 = ttk.Frame(ltop)
    lrow1.pack(fill="x")
    lrow2 = ttk.Frame(ltop)
    btn_llearn = ttk.Button(ltop, text=_("Create template"),
                            padding=(10, 2),
                            command=lambda: do_lim_learn())
    btn_lclear = ttk.Button(ltop, text=_("Clear the envelope"),
                            padding=(10, 2),
                            command=lambda: do_lim_clear())
    btn_lsend = ttk.Button(ltop, text=_("Use this envelope"),
                           padding=(10, 2),
                           command=lambda: do_lim_send())
    btn_lstart = ttk.Button(ltop, text=_("Start test"), padding=(10, 2),
                            command=lambda: do_lim_start())
    btn_lstop = ttk.Button(ltop, text=_("Stop test"), padding=(10, 2),
                           command=lambda: do_lim_stop())
    btn_lview = ttk.Button(ltop, text=_("Save image..."), padding=(10, 2),
                           command=lambda: do_lim_view_save())
    btn_lrefresh = ttk.Button(ltop, text=_("Refresh"), padding=(10, 2),
                              command=lambda: do_lim_refresh())
    LIM_LEFT = (btn_llearn, btn_lsend, btn_lclear, btn_lstart,
                btn_lstop, btn_lview)
    LIM_RIGHT = (btn_lrefresh,)
    lim_flow = flowing(ltop, lrow1, lrow2, LIM_LEFT, LIM_RIGHT, "lflow")
    ltop.bind("<Configure>", lim_flow)

    lbody = ttk.Frame(limtab)
    lbody.pack(fill="both", expand=True, padx=2, pady=4)

    lopts = ttk.Frame(lbody)
    lopts.pack(side="left", fill="y", padx=(0, 8))
    # Three numbered lines rather than a paragraph: this is the tab a
    # person who does not know what an envelope is has to be able to
    # work, and the whole of it really is three presses.
    lbl_lsteps = ttk.Label(lopts, wraplength=210, justify="left")
    says(lbl_lsteps,
         "1. Put a signal you are happy with on the instrument.\n"
         "2. Set the vertical and horizontal tolerances.\n"
         "3. Create template, then Start test.")
    lbl_lsteps.grid(row=0, column=0, sticky="w", pady=(0, 10))

    state["lsource"] = tk.StringVar(value="CH1")
    state["ldest"] = tk.StringVar(value="REF1")
    state["lvert"] = tk.StringVar(value="0.5")
    state["lhorz"] = tk.StringVar(value="0.2")

    lbl_lsource = ttk.Label(lopts, text=_("Signal"))
    says(lbl_lsource, "Signal")
    lbl_lsource.grid(row=1, column=0, sticky="w")
    cmb_lsource = ttk.Combobox(lopts, textvariable=state["lsource"],
                               width=10, state="readonly")
    cmb_lsource.grid(row=2, column=0, sticky="w", pady=(2, 10))

    # Divisions, because that is what the instrument's own command
    # takes and, as it happens, what a person can see: half a division
    # is half a division on the glass. Editable rather than readonly -
    # 0 to 5 is the instrument's range and anything in it is allowed.
    lbl_lvert = ttk.Label(lopts, text=_("Vertical tolerance"))
    says(lbl_lvert, "Vertical tolerance")
    lbl_lvert.grid(row=3, column=0, sticky="w")
    cmb_lvert = ttk.Combobox(lopts, textvariable=state["lvert"], width=10,
                             values=("0", "0.1", "0.2", "0.5", "1", "2"))
    cmb_lvert.grid(row=4, column=0, sticky="w", pady=(2, 10))

    lbl_lhorz = ttk.Label(lopts, text=_("Horizontal tolerance"))
    says(lbl_lhorz, "Horizontal tolerance")
    lbl_lhorz.grid(row=5, column=0, sticky="w")
    cmb_lhorz = ttk.Combobox(lopts, textvariable=state["lhorz"], width=10,
                             values=("0", "0.1", "0.2", "0.5", "1", "2"))
    cmb_lhorz.grid(row=6, column=0, sticky="w", pady=(2, 2))
    lbl_ldivs = ttk.Label(lopts, wraplength=210, foreground="#555",
                          justify="left")
    says(lbl_ldivs, "Both in divisions of the graticule, 0 to 5.")
    lbl_ldivs.grid(row=7, column=0, sticky="w", pady=(0, 10))

    # How closely the drawn envelope follows what the instrument
    # learnt. The band is 250 columns and nobody can drag 500 handles, so
    # it is thinned - but how far is a judgement about this signal, not
    # a constant. Dragging this re-thins what is already on the
    # graticule from the band underneath it, so the cost of the
    # choice can be seen rather than guessed at.
    lbl_ldetail = ttk.Label(lopts)
    says(lbl_ldetail, "Detail")
    lbl_ldetail.grid(row=8, column=0, sticky="w")
    ldetail = ttk.Frame(lopts)
    ldetail.grid(row=9, column=0, sticky="we", pady=(2, 0))
    state["ldetail"] = tk.IntVar(value=LEARN_HANDLES)
    lbl_ldnum = ttk.Label(ldetail, width=4, anchor="e", foreground="#555")
    lbl_ldnum.pack(side="right")
    scl_ldetail = ttk.Scale(ldetail, from_=LEARN_LEAST, to=LEARN_MOST,
                            orient="horizontal")
    scl_ldetail.pack(side="left", fill="x", expand=True)
    lbl_ldsays = ttk.Label(lopts, wraplength=210, foreground="#555",
                           justify="left")
    says(lbl_ldsays, "Handles to spend on the envelope. More follows "
                     "the signal more closely and is more to edit.")
    lbl_ldsays.grid(row=10, column=0, sticky="w", pady=(2, 10))

    def lim_detail(value=None):
        """The slider moved: re-thin the drawn envelope from the band.

        From the band the instrument learnt, not from the drawing -
        thinning something already thinned only ever loses more. One
        undo entry for the whole drag, taken when it starts.

        The number beside the slider is the handles the envelope got,
        not the budget it was given. The two can only differ by a
        handle or so now that the travel ends where the band runs out -
        see lim_range - but it is the count that is the useful number
        to show, and it is the count the label carries.

        Rounded to the nearest ten rather than down, so that the last
        notch reaches the far end rather than stopping a few short of
        it. A slider whose end cannot be got to is the fault this is
        the second half of.
        """
        top = int(float(scl_ldetail.cget("to")))
        want = int(round(float(value if value is not None
                               else state["ldetail"].get())))
        want = min(top, max(LEARN_LEAST, (want + 5) // 10 * 10))
        state["ldetail"].set(want)
        got = want
        if state.get("lband") and state.get("lmask") is not None:
            lim_take_band(remember=False)
            edit_redraw()
            got = len(state["lmask"].points)
        lbl_ldnum.config(text=str(got))

    scl_ldetail.config(command=lim_detail)
    scl_ldetail.set(LEARN_HANDLES)
    scl_ldetail.bind("<ButtonPress-1>", lambda e: msk_remember())
    lim_detail(LEARN_HANDLES)

    lbl_ldest = ttk.Label(lopts, text=_("Destination"))
    says(lbl_ldest, "Destination")
    lbl_ldest.grid(row=11, column=0, sticky="w")
    cmb_ldest = ttk.Combobox(lopts, textvariable=state["ldest"], width=10,
                             state="readonly")
    cmb_ldest.grid(row=12, column=0, sticky="w", pady=(2, 2))
    lbl_lwarn = ttk.Label(lopts, wraplength=210, foreground="#555",
                          justify="left")
    says(lbl_lwarn, "Whatever that reference holds now is replaced.")
    lbl_lwarn.grid(row=13, column=0, sticky="w", pady=(0, 12))

    # The verdict, in the one place somebody will be looking. A limit
    # test reports a failure by stopping the instrument, so this is the
    # whole of it and it deserves the room.
    limface = tkfont.nametofont("TkDefaultFont").copy()
    limface.configure(size=max(11, abs(limface.cget("size")) + 3),
                      weight="bold")
    lbl_lverdict = ttk.Label(lopts, font=limface)
    lbl_lverdict.grid(row=14, column=0, sticky="w")
    lbl_lsays = ttk.Label(lopts, wraplength=210, foreground="#555",
                          justify="left")
    lbl_lsays.grid(row=15, column=0, sticky="w", pady=(2, 0))

    # The same drawing tools as the masks tab, acting on this tab's own
    # drawing. One editor, two drawings - see edit_here. The tool in
    # hand, the selection and the view boxes are shared, because only
    # one of the two tabs can be in front and two sets of them would be
    # two things to keep in step; the history and the unsaved mark are
    # not, because those belong to the drawing.
    lrightf = ttk.Frame(lbody)
    lrightf.pack(side="left", fill="both", expand=True)

    ltoolbar = ttk.Frame(lrightf)
    ltoolbar.pack(fill="x", pady=(0, 3))
    btn_lundo = ttk.Button(ltoolbar, style="Toolbutton", padding=3,
                           command=lambda: do_msk_undo())
    btn_lredo = ttk.Button(ltoolbar, style="Toolbutton", padding=3,
                           command=lambda: do_msk_redo())
    btn_lundo.pack(side="left")
    btn_lredo.pack(side="left", padx=(2, 0))
    state.setdefault("undobtn", {}).update({"lundo": btn_lundo,
                                            "lredo": btn_lredo})
    hints(btn_lundo, "Undo")
    hints(btn_lredo, "Redo")

    lcanvasrow = ttk.Frame(lrightf)
    lcanvasrow.pack(fill="both", expand=True)
    ltools = ttk.Frame(lcanvasrow)
    ltools.pack(side="left", fill="y", padx=(0, 3))
    for key, english in (("move", "Select and move points and shapes"),
                         ("pen", "Draw points"),
                         ("eraser", "Delete points and shapes"),
                         ("cut", "Split a shape between two of its points")):
        rb = ttk.Radiobutton(ltools, value=key, variable=state["mtool"],
                             style="Toolbutton", padding=3,
                             command=lambda: msk_tool_changed())
        rb.pack(pady=(0, 2))
        hints(rb, english)
        state.setdefault("ltoolbtn", {})[key] = rb
    ttk.Separator(ltools, orient="horizontal").pack(fill="x", pady=5)
    for key, english in (
            ("fliph", "Mirror the selection horizontally"),
            ("flipv", "Mirror the selection vertically")):
        fb = ttk.Button(ltools, style="Toolbutton", padding=3,
                        command=lambda k=key: do_msk_flip(k == "fliph"))
        fb.pack(pady=(0, 2))
        hints(fb, english)
        state.setdefault("lboolbtn", {})[key] = fb
    ttk.Separator(ltools, orient="horizontal").pack(fill="x", pady=5)
    for key, english in (
            ("union", "Union: join the two selected shapes into one"),
            ("intersect", "Intersect: keep only where the two selected shapes overlap"),
            ("subtract", "Subtract: take the second selected shape out of the first")):
        bb = ttk.Button(ltools, style="Toolbutton", padding=3,
                        command=lambda k=key: do_msk_boolean(k))
        bb.pack(pady=(0, 2))
        hints(bb, english)
        state.setdefault("lboolbtn", {})[key] = bb

    lplot = tk.Canvas(lcanvasrow,
                      background=tds_wfm.DEFAULT_COLOURS["background"],
                      highlightthickness=1, highlightbackground=EDGE)
    lplot.pack(side="left", fill="both", expand=True)
    lplot.bind("<Configure>", lambda e: draw_limits())
    edit_bindings(lplot)

    # The same boxes, on the same variables: Snap to grid means the same
    # thing on both tabs and is one setting, not two that drift apart.
    # Only the last one is worded differently, because what it hides
    # here is not a mask.
    lbar = ttk.Frame(lrightf)
    lbar.pack(fill="x", pady=(4, 0))
    lbl_lgrid = ttk.Label(lbar, text=_("Grid spacing, divisions"))
    says(lbl_lgrid, "Grid spacing, divisions")
    lbl_lgrid.pack(side="left")
    ent_lgrid = ttk.Combobox(lbar, textvariable=state["mgrid"], width=5,
                             values=("0.1", "0.2", "0.25", "0.5", "1"))
    ent_lgrid.pack(side="left", padx=(4, 10))
    ent_lgrid.bind("<<ComboboxSelected>>", lambda e: edit_redraw())
    ent_lgrid.bind("<Return>", lambda e: edit_redraw())
    linfo = ttk.Label(lrightf, anchor="w", foreground="#555")
    linfo.pack(fill="x", pady=(2, 0))
    for key, english in (("mshowgrid", "Show grid"),
                         ("msnap", "Snap to grid"),
                         ("mgratic", "Graticule"),
                         ("mcross", "Crosshairs"),
                         ("mfill", "Filled"),
                         ("mhide", "Hide envelope"),
                         ("mnodots", "Hide points")):
        cb = ttk.Checkbutton(lbar, text=_(english), variable=state[key],
                             command=lambda: edit_redraw())
        says(cb, english)
        cb.pack(side="left", padx=(0, 10))

    def lim_live():
        """The instrument's channels, which is all a template can learn
        from - a reference is what one is written into."""
        return [s for s in (state.get("wsources") or [])
                if not s.startswith("REF")] or ["CH1"]

    def lim_scan():
        """Put whatever the instrument reported into the two lists."""
        live, refs = lim_live(), list(state.get("wrefs") or tds_wfm.REFS)
        cmb_lsource.config(values=live)
        if state["lsource"].get() not in live:
            state["lsource"].set(live[0])
        cmb_ldest.config(values=refs or ["REF1"])
        if state["ldest"].get() not in refs:
            state["ldest"].set(refs[0] if refs else "REF1")
        lim_buttons()

    def lim_band():
        """The template as one closed polygon in percent of graticule.

        Percent because that is what the canvas and the PNG export both
        already take, so the band is worked out once and drawn twice
        rather than in two places that can drift apart. It is drawn
        against the live trace's own scale: the template was learnt
        from that channel, so the two share it.
        """
        wave, band = state.get("lwave"), state.get("lband") or []
        if wave is None or len(band) < 2:
            return []
        view = tds_wfm.PlotView([wave])
        ymult = wave.number("YMULT", 1.0) or 1.0
        yzero, yoff = wave.number("YZERO"), wave.number("YOFF")
        per = 100.0 / tds_wfm.DIVS_Y

        def place(seconds, volts):
            level = (volts - yzero) / ymult + yoff
            return (min(100.0, max(0.0, (seconds - view.first)
                                   / view.span * 100.0)),
                    min(100.0, max(0.0, 50.0 + per * view.divisions_of_level(
                        wave, level))))

        return ([place(t, hi) for t, _lo, hi in band]
                + [place(t, lo) for t, lo, _hi in reversed(band)])

    def lim_verdict():
        """What the test is saying, in one word and one colour."""
        held = state.get("lrun")
        if not held:
            lbl_lverdict.config(text=_("Not testing"), foreground="#555")
            lbl_lsays.config(text=_("No test is running."))
        elif held.get("running"):
            lbl_lverdict.config(text=_("PASS"), foreground="#1a7f37")
            lbl_lsays.config(text=_("%(source)s is inside the template "
                                    "in %(dest)s.")
                             % {"source": held["source"],
                                "dest": held["dest"]})
        else:
            lbl_lverdict.config(text=_("FAIL"), foreground="#c62828")
            lbl_lsays.config(text=_("The instrument stopped, so "
                                    "%(source)s left the template.")
                             % {"source": held["source"]})

    def draw_limits(_evt=None):
        """The graticule, the template band, the trace, and the drawing.

        In that order, and the order is the argument: the band is what
        the instrument is judging against now, the trace is what it is
        judging, and the area drawn on top is what will replace the band
        when it is sent. Seeing all three at once is the point of the
        tab - the drawing is only worth adjusting against the signal it
        has to pass.
        """
        lplot.delete("all")
        pick = plot_colours(None)
        lplot.configure(background=pick["background"])
        wide = max(lplot.winfo_width(), 80)
        tall = max(lplot.winfo_height(), 60)
        if state["mgratic"].get():
            for element, x0, y0, x1, y1, thick in tds_wfm.graticule(
                    wide, tall, 20, room=8):
                lplot.create_line(x0, y0, x1, y1, fill=pick[element],
                                  width=thick)
        frame = tds_wfm.plot_frame(wide, tall, 20, room=8)
        edit_grid(lplot, pick, frame)
        band = lim_band()
        if len(band) > 2:
            flat = []
            for x, y in band:
                px, py = tds_msk.to_canvas(x, y, frame)
                flat += [px, py]
            # Stippled rather than solid: the trace and the drawing both
            # have to stay readable through it, and a Tk canvas has no
            # transparency.
            lplot.create_polygon(*flat, fill=pick["grid"],
                                 outline=pick["grid"], width=1,
                                 stipple="gray25", tags="band")
        wave = state.get("lwave")
        if wave is not None:
            xy, _bounds = tds_wfm.plot_geometry(wave, wide, tall, 20,
                                                None, room=8)
            if len(xy) > 1:
                flat = []
                for x, y in xy:
                    flat += [x, y]
                lplot.create_line(*flat, fill=trace_colour(wave, pick),
                                  width=1, tags="trace")
        # Stippled, for the same reason the band above is: the trace is
        # a one-pixel line and a solid fill over it leaves nothing to
        # see, whatever the drawing order says. Raising the trace puts
        # it on top; the stipple is what makes being on top mean
        # anything.
        edit_shapes(lplot, state.get("lmask"), pick, frame,
                    joins=False, stipple="gray50")
        # The trace over the envelope rather than under it: a filled
        # envelope hid the very thing it is drawn around. The handles
        # come back up over both, since they are what is dragged.
        lplot.tag_raise("trace")
        lplot.tag_raise("handle")
        lplot.tag_raise("number")
        lim_verdict()
        say_limits()

    def say_limits():
        """The line under the canvas: what is drawn, and what it means."""
        mask = state.get("lmask")
        if mask is None or not mask.filled():
            linfo.config(text=_(
                "Nothing drawn. Press Create template to have the "
                "instrument draw the envelope for you, or use the "
                "pen."))
            return
        lower, _upper, gaps = tds_msk.to_band(mask)
        limited = sum(1 for v in lower if v is not None)
        words = [_("%(shapes)d shape(s), %(points)d point(s)")
                 % {"shapes": len(mask.filled()),
                    "points": len(mask.points)},
                 _("%d%% of the width is limited")
                 % round(limited * 100.0 / tds_msk.ENV_COLUMNS)]
        if gaps:
            # A limit test allows one band. Said here rather than only
            # at the moment of sending, because it is a property of the
            # drawing and the drawing is on screen.
            words.append(_("the gap in %d column(s) will be closed up")
                         % gaps)
        linfo.config(text="  -  ".join(words))

    def lim_range():
        """Point the far end of the slider at what this band can use.

        A band is thinned until it is followed exactly, and then it is
        finished: more budget buys nothing. Where that happens is a
        property of the signal, not of the program - a square wave is
        done in thirty handles and a sine is never done at all - so a
        fixed far end leaves whatever is left over as travel that does
        nothing, which is what this looked like from the outside.

        Each edge is thinned once, at the most it could ever be given.
        Twice the busier of the two is the budget that buys both, since
        the slider hands half to each. Once a band, not once a drag:
        the thinning is the expensive part of the drag as it is.
        """
        band = lim_band()
        half = len(band) // 2
        top = LEARN_MOST
        if half >= 3:
            top = 2 * max(len(tds_msk.thinned(band[:half], half - 1,
                                              outward=1)),
                          len(tds_msk.thinned(band[half:], half - 1,
                                              outward=-1)))
            top = min(LEARN_MOST, max(LEARN_LEAST, top))
        scl_ldetail.config(to=top)
        if state["ldetail"].get() > top:
            scl_ldetail.set(top)

    def lim_take_band(remember=True):
        """Put the template the instrument just learnt on the canvas.

        Thinned to as many handles as the slider asks for: 250 columns
        is five hundred handles and not a shape anybody can drag. The
        instrument's own band stays drawn underneath, so what the
        thinning cost is there to be seen rather than taken on trust.

        `remember` False for re-thinning under the slider, where one
        undo entry a pixel of travel is not history, it is noise.
        """
        band = lim_band()
        if len(band) < 6:
            return
        half = len(band) // 2
        # Half the handles to each edge, and how many in all is the
        # slider's business rather than a constant: an envelope has no
        # limit of its own, and a limits drawing is not kept in mask
        # segments. See edit_points.
        #
        # Short of the edge's own length, always. Ask thinned for as
        # many handles as there are columns and it hands the band
        # straight back, every collinear point of it - five hundred
        # handles, which is the thing the slider exists to avoid.
        most = max(2, min(half - 1, state["ldetail"].get() // 2))
        if remember:
            msk_remember()
        # Away from the band, never into it: the upper edge is
        # first and nothing may be drawn above it, the lower edge
        # second and nothing below. Thinned plainly, the corners
        # come in and the template fails the signal it was learnt
        # from - measured on a 784D. See tds_msk._outward.
        state["lmask"] = tds_msk.Mask(
            segments=[tds_msk.thinned(band[:half], most, outward=1)
                      + tds_msk.thinned(band[half:], most,
                                        outward=-1)],
            source=state["lsource"].get())
        state["mpicks"] = []
        state.pop("mdrawing", None)

    def do_lim_clear():
        """Take the drawn envelope off the canvas.

        The drawing only, and one undo puts it back. What the
        instrument holds is untouched - a reference is replaced by
        sending another envelope to it, not by clearing this.
        """
        drawn = state.get("lmask")
        if drawn is None or not drawn.points:
            return
        msk_remember()
        drawn.segments = []
        state["mpicks"] = []
        state.pop("mdrawing", None)
        state.pop("mhover", None)
        lim_buttons()
        edit_redraw()
        say(_("The envelope is cleared"))

    def lim_lines(mask, dest, source):
        """The SCPI for the drawn envelope, or None with the reason shown."""
        wave = state.get("lwave")
        if wave is None:
            messagebox.showinfo(
                _("Read the signal first"),
                _("A limit template is built to the channel it will "
                  "judge - its record length, its volts a division and "
                  "its position. Press Refresh, then send this again."))
            return None
        lower, _upper, gaps = tds_msk.to_band(mask)
        if all(v is None for v in lower):
            messagebox.showinfo(_("Nothing to send"),
                                _("Nothing is drawn on the graticule "
                                  "yet."))
            return None
        if gaps and not messagebox.askyesno(
                _("Close the gap?"),
                "%s\n\n%s" % (
                    _("A limit test allows the signal one band, and "
                      "this drawing leaves two in %d column(s) - "
                      "clear air with something drawn above and below "
                      "it.") % gaps,
                    _("Sent, that air becomes part of the allowed "
                      "envelope. Send it that way?")),
                parent=root, default="no"):
            return None
        return tds_msk.envelope_scpi(mask, wave.pre, dest=dest,
                                     source=source, width=wave.width,
                                     how="allowed")

    def do_lim_send():
        """Write what is drawn into the reference, as a limit template."""
        mask = state.get("lmask")
        if state["busy"] or state.get("cannot") is None or mask is None:
            return
        dest, source = state["ldest"].get(), state["lsource"].get()
        lines = lim_lines(mask, dest, source)
        if lines is None:
            return
        numbers = []
        for cmd in lines:
            if cmd.startswith("CURVE "):
                numbers = [int(v) for v in cmd[len("CURVE "):].split(",")]
        busy(True, "wait")
        say(_("Sending the envelope to %s ...") % dest)
        w.submit("lim_send", lambda k, ln=lines, d=dest, nu=numbers,
                 a=source: k.msk_envelope(ln, d, nu, a))

    def lim_buttons():
        joined = state.get("cannot") is not None
        running = bool(state.get("lrun"))
        btn_llearn.config(state="normal" if joined and not running
                          else "disabled")
        btn_lstart.config(state="normal" if joined and not running
                          else "disabled")
        btn_lstop.config(state="normal" if joined and running
                         else "disabled")
        btn_lrefresh.config(state="normal" if joined else "disabled")
        btn_lview.config(state="normal" if state.get("lwave") is not None
                         else "disabled")
        drawn = state.get("lmask")
        btn_lsend.config(state="normal" if joined and drawn is not None
                         and drawn.filled() else "disabled")
        btn_lclear.config(state="normal" if drawn is not None
                          and drawn.points else "disabled")

    def lim_tolerances():
        """The two numbers, or None with the complaint already made."""
        try:
            vertical = float(state["lvert"].get())
            horizontal = float(state["lhorz"].get())
        except ValueError:
            vertical = horizontal = -1.0
        if not (0.0 <= vertical <= 5.0 and 0.0 <= horizontal <= 5.0):
            messagebox.showerror(
                _("Error"),
                _("How far the signal may wander is given in divisions "
                  "of the graticule, from 0 to 5 - half a division is "
                  "0.5."))
            return None
        return vertical, horizontal

    def do_lim_learn():
        """Have the instrument write a template from what it sees now."""
        if state["busy"] or state.get("cannot") is None:
            return
        wander = lim_tolerances()
        if wander is None:
            return
        source, dest = state["lsource"].get(), state["ldest"].get()
        busy(True, "wait")
        state["llearn"] = True
        say(_("Making a template of %(source)s in %(dest)s ...")
            % {"source": source, "dest": dest})
        w.submit("lim_build", lambda k, s=source, v=wander[0],
                 h=wander[1], d=dest: k.lim_build(s, v, h, d))

    def do_lim_start():
        """Judge the signal against the template, from now on."""
        if state["busy"] or state.get("cannot") is None:
            return
        busy(True, "wait")
        say(_("Starting the limit test ..."))
        w.submit("lim_run", lambda k, s=state["lsource"].get(),
                 d=state["ldest"].get(): k.lim_run(s, d))

    def do_lim_stop():
        """Switch the test off and give the instrument back."""
        if state["busy"] or state.get("cannot") is None:
            return
        state.pop("lrun", None)
        busy(True, "wait")
        w.submit("lim_stop", lambda k: k.limit_stop())

    def do_lim_refresh():
        """Read the template and the live signal again, and redraw."""
        if state["busy"] or state.get("cannot") is None:
            return
        busy(True, "wait")
        say(_("Reading the template and the signal ..."))
        w.submit("lim_picture", lambda k, s=state["lsource"].get(),
                 d=state["ldest"].get(): k.lim_picture(s, d))

    def do_lim_view_save():
        """This graticule as a PNG, the way the other tabs save theirs."""
        wave = state.get("lwave")
        if wave is None:
            messagebox.showinfo(_("Nothing to save"),
                                _("Press Refresh to read the template "
                                  "and the signal first."))
            return
        path = filedialog.asksaveasfilename(
            parent=root, title=_("Save image"), defaultextension=".png",
            initialfile=stamped(state["ldest"].get() or "limit", ".png"),
            filetypes=[(_("PNG image (*.png)"), "*.png")])
        if not path:
            return
        band = lim_band()
        try:
            with open(path, "wb") as fh:
                fh.write(tds_wfm.plot_png(
                    [wave], width=state["pngsize"][0],
                    height=state["pngsize"][1],
                    colours=state.get("colours"),
                    caption=wave_scales(wave),
                    shapes=[band] if len(band) > 2 else []))
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                 % (_("Could not save"), exc))
            return
        say(_("Saved %s") % os.path.basename(path))

    def lim_watch():
        """Ask every couple of seconds whether it is still running,
        which for a limit test is the whole verdict."""
        if not state.get("lrun") or state.get("cannot") is None:
            return
        if not state["busy"]:
            w.submit("lim_state", lambda k: k.limit_state())
        root.after(2000, lim_watch)

    lim_verdict()


    # There is no "give me the display" command on these instruments.
    # What there is, is a hardcopy subsystem meant for a printer, which
    # will send to the bus if asked. So a screenshot is a print job
    # aimed at this program. Which format to ask for was measured on
    # three instruments rather than guessed - see tds_scr.py.
    scrtab = ttk.Frame(tabs)
    tabs.add(scrtab, text=_("Screenshot"))
    named(scrtab, "Screenshot")

    stop_ = ttk.Frame(scrtab)
    stop_.pack(fill="x", padx=2, pady=(6, 2))
    btn_sget = ttk.Button(stop_, text=_("Take screenshot"), padding=(10, 2),
                          command=lambda: do_scr_get())
    btn_sget.pack(side="left")
    btn_ssave = ttk.Button(stop_, text=_("Save image..."), padding=(10, 2),
                           command=lambda: do_scr_save())
    btn_ssave.pack(side="left", padx=4)
    btn_sscan = ttk.Button(stop_, text=_("Refresh"), padding=(10, 2),
                           command=lambda: do_scr_options())
    btn_sscan.pack(side="right")

    sbody = ttk.Frame(scrtab)
    sbody.pack(fill="both", expand=True, padx=2, pady=4)

    sopts = ttk.Frame(sbody)
    sopts.pack(side="left", fill="y", padx=(0, 8))
    lbl_sfmt = ttk.Label(sopts, text=_("Format"))
    says(lbl_sfmt, "Format")
    lbl_sfmt.grid(row=0, column=0, sticky="w", pady=(0, 2))
    scr_fmt = ttk.Combobox(sopts, width=38, state="readonly")
    scr_fmt.grid(row=1, column=0, sticky="w", pady=(0, 10))
    lbl_slay = ttk.Label(sopts, text=_("Layout"))
    says(lbl_slay, "Layout")
    lbl_slay.grid(row=2, column=0, sticky="w", pady=(0, 2))
    scr_lay = ttk.Combobox(sopts, width=38, state="readonly")
    scr_lay.grid(row=3, column=0, sticky="w", pady=(0, 10))
    lbl_spal = ttk.Label(sopts, text=_("Palette"))
    says(lbl_spal, "Palette")
    lbl_spal.grid(row=4, column=0, sticky="w", pady=(0, 2))
    # Wide enough for the longest line any of them offers - measured,
    # not guessed: "Hardcopy - light background, for printing" is 40
    # characters and was cut off at 30.
    scr_pal = ttk.Combobox(sopts, width=38, state="readonly")
    scr_pal.grid(row=5, column=0, sticky="w", pady=(0, 10))
    # Normal and Inverted are done here rather than on the instrument,
    # so the picture can change the moment it is chosen instead of
    # waiting for another five-second capture.
    scr_pal.bind("<<ComboboxSelected>>", lambda e: repaint_shot())
    # Layout too: it turns the picture already in hand rather than
    # waiting for the next capture, which is the same reasoning.
    scr_lay.bind("<<ComboboxSelected>>", lambda e: repaint_shot())
    lbl_snote = ttk.Label(sopts, foreground="#555", wraplength=210,
                          justify="left")
    says(lbl_snote, "The instrument's own hardcopy settings are read "
                    "before each shot and put back afterwards.")
    lbl_snote.grid(row=6, column=0, sticky="w", pady=(6, 0))

    sview = ttk.Frame(sbody)
    sview.pack(side="left", fill="both", expand=True)
    shot = tk.Canvas(sview, background="#3a3a3a", highlightthickness=1,
                     highlightbackground=EDGE)
    shsb = ttk.Scrollbar(sview, orient="horizontal", command=shot.xview)
    shvsb = ttk.Scrollbar(sview, orient="vertical", command=shot.yview)
    shot.configure(xscrollcommand=shsb.set, yscrollcommand=shvsb.set)
    shvsb.pack(side="right", fill="y")
    shsb.pack(side="bottom", fill="x")
    shot.pack(side="left", fill="both", expand=True)
    sinfo = ttk.Label(scrtab, text="", anchor="w")
    sinfo.pack(fill="x", padx=2, pady=(0, 2))

    def draw_shot():
        """Put the captured screen on the canvas at one pixel to one.

        Tk 8.6 reads PNG itself, so the image goes in as the same bytes
        the Save button would write - no scaling, no resampling, and no
        second code path that could disagree with the file.
        """
        shot.delete("all")
        screen = state.get("screen")
        if not screen:
            state["shotimg"] = None
            shot.config(scrollregion=(0, 0, 0, 0))
            shot.create_text(max(shot.winfo_width(), 60) / 2,
                             max(shot.winfo_height(), 40) / 2,
                             fill="#dddddd",
                             text=_("No screenshot yet. Press Take "
                                    "screenshot."))
            return
        state["shotimg"] = tk.PhotoImage(
            data=base64.b64encode(state["shotpng"]).decode("ascii"))
        # Centred when there is room, top-left when there is not, and the
        # scroll region never smaller than the canvas - otherwise a
        # 640-wide picture in a 770-wide pane sits against the left edge
        # with a scrollbar offering to scroll nowhere.
        pane_w = max(shot.winfo_width(), 1)
        pane_h = max(shot.winfo_height(), 1)
        x = max(0, (pane_w - screen.width) // 2)
        y = max(0, (pane_h - screen.height) // 2)
        shot.create_image(x, y, image=state["shotimg"], anchor="nw")
        shot.config(scrollregion=(0, 0, max(pane_w, screen.width + 2 * x),
                                  max(pane_h, screen.height + 2 * y)))

    def repaint_shot():
        """Re-render the shot already in hand under the current settings.

        The palette choice is only this program's where the instrument
        has no HARDCOPY:PALETTE of its own - there it costs a palette
        flip. Where the instrument does have one, the choice is the
        instrument's and only a new capture can honour it, so the
        picture is left alone and the choice applies to the next shot.

        Layout is always this program's to honour, because turning a
        picture on its side is a transpose and asking the instrument for
        it again is five seconds. The pixels are the same pixels either
        way.
        """
        wanted = chosen(scr_pal, state.get("spkeys") or [])
        state["sinvert"] = (wanted == "INVERT")
        base = state.get("sraw")
        if base is None:
            return
        want_inverted = state["sinvert"]
        turns = wanted_turns(base)
        if (want_inverted == state.get("sshown")
                and turns == state.get("sturned")):
            return
        state["sturned"] = turns
        shown = base.inverted() if want_inverted else base
        show_shot(shown.turned(turns) if turns else shown,
                  state.get("ssecs", 0.0))

    def wanted_turns(screen):
        """How many quarter turns from what arrived to what is asked for.

        Portrait and landscape are one quarter apart, so this is one
        quarter or none - but which quarter is not the same both ways,
        and it is worked out from what the capture actually was rather
        than assumed, because an instrument found set to landscape
        hands over a landscape picture to begin with.

        A portrait capture turns anticlockwise into landscape: measured
        on a 784D, that is where "Tek Run:" ends up down the left-hand
        edge reading upwards. Going the other way is the inverse, three
        quarters. Turning that one quarter as well lands the picture
        upside down, which is what a landscape capture asked for
        portrait used to do.
        """
        asked = chosen(scr_lay, state.get("slkeys") or []) or ""
        was = (getattr(screen, "layout", "") or "").upper()
        if not was or not asked or asked == was:
            return 0
        return 1 if was.startswith("PORT") else 3

    def show_shot(screen, secs=0.0):
        state["screen"] = screen
        state["sshown"] = bool(state.get("sinvert")) if screen else None
        if screen is None:
            state["sraw"] = None
            state["sturned"] = 0
        state["shotpng"] = screen.to_png() if screen else None
        draw_shot()
        if screen:
            sinfo.config(text=_("%(w)d x %(h)d, %(c)d colours, %(f)s, "
                                "%(k)s bytes from the instrument in "
                                "%(s).1f s")
                         % {"w": screen.width, "h": screen.height,
                            "c": screen.colours, "f": screen.keyword,
                            "k": format(len(screen.raw), ","),
                            "s": secs})
        else:
            sinfo.config(text="")

    # Redrawn on resize whether or not there is a picture: the empty
    # message and the centring both depend on the size of the pane.
    shot.bind("<Configure>", lambda e: draw_shot())

    # --------------------------------------------------- the errors tab
    # Not the SCPI event queue - that is what just went wrong in this
    # conversation, and the rest of the program reads it constantly.
    # This is ERRLOG, the instrument's own service history in
    # non-volatile memory: what failed at power-on, going back years.
    errtab = ttk.Frame(tabs)
    tabs.add(errtab, text=_("Error Log"))
    named(errtab, "Error Log")

    etop = ttk.Frame(errtab)
    etop.pack(fill="x", padx=2, pady=(6, 2))
    btn_eget = ttk.Button(etop, text=_("Download"), padding=(10, 2),
                          command=lambda: do_err_get())
    btn_eget.pack(side="left")
    btn_esave = ttk.Button(etop, text=_("Save as..."), padding=(10, 2),
                           command=lambda: do_err_save())
    btn_esave.pack(side="left", padx=4)
    btn_eclr = ttk.Button(etop, text=_("Clear errors"), padding=(10, 2),
                          command=lambda: do_err_clear())
    btn_eclr.pack(side="left")

    ebody = ttk.Frame(errtab)
    ebody.pack(fill="both", expand=True, padx=2, pady=4)

    eside = ttk.Frame(ebody)
    eside.pack(side="left", fill="y", padx=(0, 8))
    lbl_ewhat = ttk.Label(eside)
    says(lbl_ewhat, "The instrument's log")
    lbl_ewhat.pack(anchor="w", pady=(0, 4))
    lbl_ecount = ttk.Label(eside, text="", width=26, anchor="w",
                           justify="left")
    lbl_ecount.pack(anchor="w")
    lbl_enote = ttk.Label(eside, foreground="#555", wraplength=200,
                          justify="left")
    says(lbl_enote, "This is the instrument's own service history, kept "
                    "across power cycles - not the errors this program "
                    "reports. Clearing it cannot be undone.")
    lbl_enote.pack(anchor="w", pady=(8, 0))

    eview = ttk.Frame(ebody)
    eview.pack(side="left", fill="both", expand=True)
    # Fixed pitch, so the timestamp at the head of every entry lines up
    # down the page. TkFixedFont is whatever the platform considers its
    # monospace face, which beats naming Courier and hoping.
    errtxt = tk.Text(eview, wrap="none", height=10, undo=False,
                     font="TkFixedFont", background="#ffffff",
                     highlightthickness=1, highlightbackground=EDGE)
    evsb = ttk.Scrollbar(eview, orient="vertical", command=errtxt.yview)
    ehsb = ttk.Scrollbar(eview, orient="horizontal", command=errtxt.xview)
    errtxt.configure(yscrollcommand=evsb.set, xscrollcommand=ehsb.set)
    evsb.pack(side="right", fill="y")
    ehsb.pack(side="bottom", fill="x")
    errtxt.pack(side="left", fill="both", expand=True)
    # Read-only, but selectable and copyable - which is what a disabled
    # Text is not. The insert and delete happen through show_errors.
    errtxt.bind("<Key>", lambda e: (None if (e.state & 4) or e.keysym in
                                    ("Left", "Right", "Up", "Down", "Home",
                                     "End", "Prior", "Next") else "break"))

    def show_errors(lines, count=None):
        """Put text in the pane. `lines` is already the finished text."""
        state["errlines"] = list(lines)
        state["errcount"] = count
        errtxt.delete("1.0", "end")
        errtxt.insert("1.0", "\n".join(lines))
        errtxt.see("1.0")
        state["errtext"] = "\n".join(lines)
        if count is None:
            lbl_ecount.config(text="")
        elif count:
            lbl_ecount.config(text=_("%d entries") % count)
        else:
            lbl_ecount.config(text=_("No errors"))

    # --------------------------------------------------- the system tab
    # The instrument's own housekeeping: its clock, its calibration, its
    # self tests, where hardcopies go, and which options it believes it
    # has. Two columns of boxes rather than a scrolling page, because
    # every one of these is short and a person looking for "the one that
    # runs the self test" should be able to see all of them at once.
    systab = ttk.Frame(tabs)
    tabs.add(systab, text=_("System"))
    named(systab, "System")

    # Said twice and written once. The panel explains what the button
    # will do and the confirmation says the same thing back, and the two
    # drifting apart is how a warning stops being read.
    ERASE_SAYS = ("A Secure Erase writes zeroes over all memory locations "
                  "and returns all saved setups back to factory settings.\n"
                  "Calibration data is left untouched.\n"
                  "THIS ACTION CANNOT BE UNDONE!")
    FACTORY_SAYS = ("This returns the instrument to its default settings.\n"
                    "The GPIB address, calibration constants, and any "
                    "protected user data is left untouched.")

    sysleft = ttk.Frame(systab)
    sysleft.pack(side="left", fill="both", expand=True, padx=(4, 3),
                 pady=6)
    sysright = ttk.Frame(systab)
    sysright.pack(side="left", fill="both", expand=True, padx=(3, 4),
                  pady=6)

    def sys_rule(parent):
        """A line between one section and the next."""
        ttk.Separator(parent, orient="horizontal").pack(fill="x",
                                                        pady=(10, 8))

    # ---- what it is
    sysbox1 = ttk.LabelFrame(sysleft, padding=8)
    says(sysbox1, "Instrument ID:")
    sysbox1.pack(fill="x")
    lbl_sysidn = ttk.Label(sysbox1, text="-", wraplength=300,
                           justify="left")
    lbl_sysidn.pack(anchor="w")
    # What ID? says is still read - the options dialog greys what this
    # instrument does not offer, and the log records it - but it is not
    # shown here. On an instrument with no options it repeats *IDN?
    # word for word, which is two lines saying one thing.
    sysrow1 = ttk.Frame(sysbox1)
    sysrow1.pack(fill="x", pady=(8, 0))
    btn_sysread = ttk.Button(sysrow1, text=_("Read"), padding=(10, 2),
                             command=lambda: do_sys_read())
    btn_sysread.pack(side="left")
    says(btn_sysread, "Read")
    btn_syslock = ttk.Button(sysrow1, text=_("Lock front panel"),
                             padding=(10, 2),
                             command=lambda: do_sys_lock(True))
    btn_syslock.pack(side="left", padx=4)
    says(btn_syslock, "Lock front panel")
    btn_sysfree = ttk.Button(sysrow1, text=_("Unlock front panel"),
                             padding=(10, 2),
                             command=lambda: do_sys_lock(False))
    btn_sysfree.pack(side="left")
    says(btn_sysfree, "Unlock front panel")

    # ---- its clock
    sys_rule(sysleft)
    sysbox2 = ttk.LabelFrame(sysleft, padding=8)
    says(sysbox2, "Date and time")
    sysbox2.pack(fill="x")

    def sys_boxes(parent, label, parts):
        """A row of small entry boxes, one for each number.

        Rather than one field for the whole date: a single field
        invites it written in whatever order the person at the bench
        writes dates in, and the instrument takes exactly one of them -
        yyyy-mm-dd, and nothing else.
        """
        line = ttk.Frame(parent)
        line.pack(fill="x", pady=1)
        one = ttk.Label(line, text=_(label), width=6)
        one.pack(side="left")
        says(one, label)
        for key, wide, tail in parts:
            state[key] = tk.StringVar()
            ttk.Entry(line, textvariable=state[key], width=wide + 1,
                      justify="center").pack(side="left")
            if tail:
                ttk.Label(line, text=tail).pack(side="left", padx=2)

    SYS_DATE = (("sysyear", 4, "-"), ("sysmonth", 2, "-"), ("sysday", 2, ""))
    SYS_TIME = (("syshour", 2, ":"), ("sysmin", 2, ":"), ("syssec", 2, ""))
    sys_boxes(sysbox2, "Date", SYS_DATE)
    sys_boxes(sysbox2, "Time", SYS_TIME)

    def sys_clock_said(parts, gap):
        """The boxes, back together as the instrument wants them.

        Padded, so somebody who types 9 for September gets 09 rather
        than a refusal.
        """
        return gap.join(state[key].get().strip().zfill(wide)
                        for key, wide, _tail in parts)

    def sys_clock_show(text, parts, gap):
        """The instrument's answer, spread across the boxes."""
        bits = (text or "").split(gap)
        for i, (key, _wide, _tail) in enumerate(parts):
            state[key].set(bits[i].strip() if i < len(bits) else "")

    state["sysclock"] = tk.BooleanVar(value=False)
    chk_sysclock = ttk.Checkbutton(
        sysbox2, variable=state["sysclock"],
        text=_("Show the clock on the instrument's screen"))
    chk_sysclock.pack(anchor="w", pady=(6, 0))
    says(chk_sysclock, "Show the clock on the instrument's screen")
    # Apply above Synchronise, because they are two different things and
    # the one that sends what is in the boxes should be next to them.
    # Synchronise does not fill the boxes in and wait to be applied - it
    # sets the instrument from this computer's clock there and then, and
    # leaves whatever is typed above alone.
    btn_systime = ttk.Button(sysbox2, text=_("Apply"), padding=(10, 2),
                             command=lambda: do_sys_clock())
    btn_systime.pack(anchor="w", pady=(8, 0))
    says(btn_systime, "Apply")
    btn_syssync = ttk.Button(sysbox2, text=_("Synchronise to this PC"),
                             padding=(10, 2),
                             command=lambda: do_sys_sync())
    btn_syssync.pack(anchor="w", pady=(4, 0))
    says(btn_syssync, "Synchronise to this PC")

    # ---- where hardcopies go
    sys_rule(sysleft)
    sysbox3 = ttk.LabelFrame(sysleft, padding=8)
    says(sysbox3, "Hardcopy and ports")
    sysbox3.pack(fill="x")
    state["sysport"] = tk.StringVar()
    state["sysformat"] = tk.StringVar()
    state["syslayout"] = tk.StringVar()
    for label, key, values in (
            ("Port", "sysport", ("GPIB", "RS232", "CENTRONICS", "FILE")),
            ("Format", "sysformat", ("BMP", "BMPCOLOR", "TIFF", "PCX",
                                     "PCXCOLOR", "EPSIMAGE", "INTERLEAF",
                                     "THINKJET", "DESKJET", "LASERJET",
                                     "EPSON")),
            ("Layout", "syslayout", ("LANDSCAPE", "PORTRAIT"))):
        line = ttk.Frame(sysbox3)
        line.pack(fill="x", pady=1)
        one = ttk.Label(line, text=_(label), width=8)
        one.pack(side="left")
        says(one, label)
        ttk.Combobox(line, textvariable=state[key], values=values,
                     width=14, state="readonly").pack(side="left")
    lbl_sysrs = ttk.Label(sysbox3, foreground="#555", wraplength=300,
                          justify="left")
    says(lbl_sysrs, "The RS-232 settings below are Option 13 only. If not "
                    "fitted, these settings read back blank and the "
                    "instrument refuses them.")
    lbl_sysrs.pack(anchor="w", pady=(6, 2))
    state["sysbaud"] = tk.StringVar()
    state["sysparity"] = tk.StringVar()
    state["sysstop"] = tk.StringVar()
    for label, key, values in (
            ("Baud", "sysbaud", ("300", "600", "1200", "2400", "4800",
                                 "9600", "19200")),
            ("Parity", "sysparity", ("NONE", "EVEN", "ODD")),
            ("Stop bits", "sysstop", ("1", "2"))):
        line = ttk.Frame(sysbox3)
        line.pack(fill="x", pady=1)
        one = ttk.Label(line, text=_(label), width=8)
        one.pack(side="left")
        says(one, label)
        ttk.Combobox(line, textvariable=state[key], values=values,
                     width=14, state="readonly").pack(side="left")
    btn_sysports = ttk.Button(sysbox3, text=_("Apply"), padding=(10, 2),
                              command=lambda: do_sys_ports())
    btn_sysports.pack(anchor="w", pady=(8, 0))
    says(btn_sysports, "Apply")

    # ---- calibration and self test
    sysbox4 = ttk.LabelFrame(sysright, padding=8)
    says(sysbox4, "Calibration and self test")
    sysbox4.pack(fill="x")
    lbl_sysspc = ttk.Label(sysbox4, foreground="#555", wraplength=300,
                           justify="left")
    says(lbl_sysspc, "Signal Path Compensation (SPC) takes several "
                     "minutes to complete. Please ensure instrument is "
                     "warmed up for 20 minutes and no input signals are "
                     "present.")
    lbl_sysspc.pack(anchor="w")
    btn_sysspc = ttk.Button(sysbox4, text=_("Signal Path Compensation"),
                            padding=(10, 2),
                            command=lambda: do_sys_spc())
    btn_sysspc.pack(anchor="w", pady=(6, 0))
    says(btn_sysspc, "Signal Path Compensation")
    sysrow4 = ttk.Frame(sysbox4)
    sysrow4.pack(fill="x", pady=(20, 0))
    state["sysdiag"] = tk.StringVar(value="ALL")
    lbl_sysdiag = ttk.Label(sysrow4, text=_("Self test"))
    lbl_sysdiag.pack(side="left", padx=(0, 6))
    says(lbl_sysdiag, "Self test")
    ttk.Combobox(sysrow4, textvariable=state["sysdiag"], width=14,
                 state="readonly",
                 values=("ALL", "ACQUISITION", "CPU", "DISPLAY",
                         "FPANEL")).pack(side="left")
    btn_sysdiag = ttk.Button(sysrow4, text=_("Run"), padding=(10, 2),
                             command=lambda: do_sys_diag())
    btn_sysdiag.pack(side="left", padx=4)
    says(btn_sysdiag, "Run")
    lbl_sysdiagsay = ttk.Label(sysbox4, foreground="#555", wraplength=300,
                               justify="left")
    says(lbl_sysdiagsay, "Extended diagnostics performs a warm-boot and "
                         "takes a few minutes. All on screen data will be "
                         "lost.")
    lbl_sysdiagsay.pack(anchor="w", pady=(6, 0))
    # Sized in characters rather than filling the box, and sized for the
    # longest line the instrument actually produces: a 784D answers
    # "pass -- Cal Initialization (see error log)", which is 42
    # characters, so 46 leaves a little room without stretching the pane
    # across the whole tab. Nine rows for eight results and somewhere
    # for a ninth to appear rather than scroll out of sight.
    txt_sysdiag = tk.Text(sysbox4, height=9, width=46, wrap="none",
                          undo=False, font="TkFixedFont",
                          background="#ffffff", highlightthickness=1,
                          highlightbackground=EDGE)
    txt_sysdiag.pack(anchor="w", pady=(6, 0))
    txt_sysdiag.bind("<Key>", lambda e: (None if (e.state & 4) else
                                         "break"))

    # ---- memory
    sys_rule(sysright)
    sysbox5 = ttk.LabelFrame(sysright, padding=8)
    says(sysbox5, "Memory")
    sysbox5.pack(fill="x")
    lbl_syswipe = ttk.Label(sysbox5, foreground="#555", wraplength=300,
                            justify="left")
    says(lbl_syswipe, ERASE_SAYS)
    lbl_syswipe.pack(anchor="w")
    sysrow5 = ttk.Frame(sysbox5)
    sysrow5.pack(fill="x", pady=(6, 0))
    btn_syswipe = ttk.Button(sysrow5, text=_("Secure erase..."),
                             padding=(10, 2),
                             command=lambda: do_sys_secure())
    btn_syswipe.pack(side="left")
    says(btn_syswipe, "Secure erase...")
    btn_sysfact = ttk.Button(sysrow5, text=_("Recall factory setup"),
                             padding=(10, 2),
                             command=lambda: do_sys_factory())
    btn_sysfact.pack(side="left", padx=4)
    says(btn_sysfact, "Recall factory setup")

    # ---- options
    sys_rule(sysright)
    sysbox6 = ttk.LabelFrame(sysright, padding=8)
    says(sysbox6, "Factory options")
    sysbox6.pack(fill="x")
    lbl_sysopts = ttk.Label(sysbox6, foreground="#555", wraplength=300,
                            justify="left")
    says(lbl_sysopts, "Enable and disable factory options.")
    lbl_sysopts.pack(anchor="w")
    btn_sysopts = ttk.Button(sysbox6, text=_("Options..."),
                             padding=(10, 2),
                             command=lambda: do_sys_options())
    btn_sysopts.pack(anchor="w", pady=(6, 0))
    says(btn_sysopts, "Options...")

    def sys_show(now, clock=True):
        """Put what the instrument said into the tab's fields.

        `clock` False leaves the date and time boxes alone. Synchronise
        sends this computer's clock without touching them, and reading
        the instrument back afterwards would overwrite whatever somebody
        had typed there with the time that was just sent.
        """
        state["sysnow"] = now
        lbl_sysidn.config(text=now.get("idn") or _("not connected"))
        if clock:
            sys_clock_show(now.get("date"), SYS_DATE, "-")
            sys_clock_show(now.get("time"), SYS_TIME, ":")
        state["sysclock"].set(str(now.get("clock") or "0").strip()
                              in ("1", "ON", "on"))
        for key, field in (("sysport", "port"), ("sysformat", "format"),
                           ("syslayout", "layout")):
            state[key].set(now.get(field) or "")
        rs = now.get("rs232") or {}
        for key, field in (("sysbaud", "baud"), ("sysparity", "parity"),
                           ("sysstop", "stopbits")):
            state[key].set(rs.get(field) or "")

    def do_sys_read():
        busy(True, "wait")
        say(_("Reading the instrument's settings ..."))
        w.submit("sys_read", lambda k: k.sys_read())

    def sys_apply(lines, note):
        """Send a handful of settings and say how it went."""
        if not lines:
            return
        busy(True, "wait")
        state["syssaid"] = note
        say(note)
        w.submit("sys_send", lambda k, ls=list(lines): k.sys_send(ls))

    def do_sys_lock(shut):
        sys_apply(["LOCK ALL" if shut else "UNLOCK ALL"],
                  _("Locking the front panel ...") if shut else
                  _("Unlocking the front panel ..."))

    def do_sys_sync():
        """Set the instrument from this computer's clock, now.

        Read at the moment the button is pressed and sent straight out.
        Filling the boxes in and waiting for Apply would send whatever
        second the button was pressed on rather than the second the
        Apply was, and the boxes are left alone so that a time somebody
        typed is not quietly overwritten by this.
        """
        now = time.localtime()
        date = time.strftime("%Y-%m-%d", now)
        clock = time.strftime("%H:%M:%S", now)
        state["syskeepclock"] = True
        sys_apply(['DATE "%s"' % date, 'TIME "%s"' % clock,
                   "DISPLAY:CLOCK %s"
                   % ("ON" if state["sysclock"].get() else "OFF")],
                  _("Setting the instrument to %(date)s %(time)s ...")
                  % {"date": date, "time": clock})

    def do_sys_clock():
        lines = []
        date, clock = sys_clock_said(SYS_DATE, "-"), sys_clock_said(SYS_TIME,
                                                                    ":")
        # A row of empty boxes reads back as "0000-00-00", which the
        # instrument would take and be wrong about. Only sent when
        # somebody has actually put numbers in.
        if date.replace("-", "").strip("0"):
            lines.append('DATE "%s"' % date)
        if clock.replace(":", "").strip("0"):
            lines.append('TIME "%s"' % clock)
        lines.append("DISPLAY:CLOCK %s"
                     % ("ON" if state["sysclock"].get() else "OFF"))
        sys_apply(lines, _("Setting the instrument's clock ..."))

    def do_sys_ports():
        lines = []
        for key, cmd in (("sysport", "HARDCOPY:PORT"),
                         ("sysformat", "HARDCOPY:FORMAT"),
                         ("syslayout", "HARDCOPY:LAYOUT"),
                         ("sysbaud", "RS232:BAUD"),
                         ("sysparity", "RS232:PARITY"),
                         ("sysstop", "RS232:STOPBITS")):
            value = state[key].get().strip()
            if value:
                lines.append("%s %s" % (cmd, value))
        sys_apply(lines, _("Setting the ports ..."))

    def do_sys_spc():
        if not messagebox.askyesno(
                _("Signal Path Compensation"),
                _("SPC takes several minutes to complete, and nothing "
                  "else can use the bus until it finishes.\n"
                  "Ensure the instrument is warmed up and all input "
                  "signals are disconnected.") + "\n\n" + _("Proceed?")):
            return
        busy(True, "wait")
        say(_("Compensating the signal path - this takes minutes ..."))
        w.submit("sys_spc", lambda k: k.sys_spc())

    def do_sys_diag():
        area = state["sysdiag"].get() or "ALL"
        if not messagebox.askyesno(
                _("Run the self test?"),
                _("Extended diagnostics warm-boot the instrument. It "
                  "takes half a minute or more, and whatever is on the "
                  "screen is lost.") + "\n\n" + _("Proceed?")):
            return
        busy(True, "wait")
        txt_sysdiag.delete("1.0", "end")
        say(_("Running the %s self test ...") % area)
        w.submit("sys_diag", lambda k, a=area: k.sys_diag(a))

    def do_sys_secure():
        if not messagebox.askyesno(
                _("Secure erase"),
                _(ERASE_SAYS) + "\n\n" + _("Proceed?"),
                icon="warning", default="no"):
            return
        busy(True, "wait")
        say(_("Erasing the instrument's waveform and setup memory ..."))
        w.submit("sys_secure", lambda k: k.sys_secure())

    def do_sys_factory():
        if not messagebox.askyesno(
                _("Recall the factory setup?"),
                _(FACTORY_SAYS) + "\n\n" + _("Proceed?")):
            return
        busy(True, "wait")
        say(_("Recalling the factory setup ..."))
        w.submit("sys_factory", lambda k: k.sys_factory())

    def do_sys_options():
        """The option words, with the switch that has to be moved first.

        A dialog rather than a panel on the tab. It is the one thing
        here that writes to non-volatile memory, it needs the cabinet
        opened first, and it is easier to explain in one place than in
        a box three inches wide.
        """
        if dialog_open():
            return
        dlg = tk.Toplevel(root)
        dlg.title(_("Factory options"))
        dlg.transient(root)
        dlg.resizable(False, False)
        try:
            dlg.iconbitmap(resource("app.ico"))
        except Exception:
            pass
        pad = ttk.Frame(dlg, padding=10)
        pad.pack(fill="both", expand=True)
        left = ttk.Frame(pad)
        left.pack(side="left", fill="y")
        right = ttk.Frame(pad)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        ttk.Label(left, wraplength=300, justify="left", text=_(
            "1. With the instrument switched on, switch the NVRAM "
            "protection switch to unprotected as per the image "
            "below.")).pack(anchor="w")
        # The picture from the service manual, because "the two small
        # access holes on the right side near the front" is a sentence
        # somebody reads three times with a torch in their hand.
        # 071-0627-02 figure 5-1, rendered from the PDF's own vector art
        # at 400 dpi and resampled to the size it is drawn at.
        #
        # A PNG rather than the SVG beside it because Tk only learned to
        # read SVG in 9.0, and this is 8.6: measured, PhotoImage here
        # answers "couldn't recognize data in image file". The vector is
        # kept as memprotect.svg all the same, so a Tk 9 build has only
        # to say format="svg -scaletoheight 284" here.
        try:
            shot = tk.PhotoImage(file=resource("memprotect.png"))
            state["sysfigure"] = shot        # or Tk collects it
            ttk.Label(left, image=shot).pack(anchor="w", pady=(6, 2))
            # Wrapped to the width of the picture above it, so the two
            # read as one block. Left to itself this is a single line
            # half as wide again as the figure.
            ttk.Label(left, foreground="#555", wraplength=shot.width(),
                      justify="left", text=_(
                          "TDS 500D/600C/700D service manual "
                          "071-0627-02, figure 5-1")).pack(anchor="w")
        except Exception as exc:
            log_note("ui", "no memory protection figure: %s" % exc)
        ttk.Label(left, wraplength=300, justify="left", text=_(
            "2. Tick the desired options to be enabled and click "
            "Write.\n"
            "3. Switch the NVRAM switch back to Protected.\n"
            "4. Power cycle the instrument, the boot screen will "
            "display which options are enabled."
        )).pack(anchor="w", pady=(8, 0))

        # Nothing is ticked to begin with, and the reason is said out
        # loud. The instrument does not report its options over the
        # bus: measured on a 784D carrying Option 2C, ID? answers
        # "ID TEK/TDS 784D,CF:91.1CT,FV:v7.4e" and nothing else. Ticking
        # boxes from that would show every option as absent on an
        # instrument that has them, and a box unticked here switches an
        # option *off*.
        ttk.Label(right, wraplength=330, justify="left", text=_(
            "This instrument does not report which options are enabled "
            "via GPIB. Please ensure to tick all desired options "
            "INCLUDING currently enabled options. Any unticked options "
            "will be disabled.")).pack(anchor="w")
        ttk.Label(right, wraplength=330, justify="left", text=_(
            "Options 1M, 2F, 2C and 1G are software only and can be "
            "enabled on any instrument. The other options require "
            "additional hardware to operate and will not appear if the "
            "hardware is not present.\n"
            "Options 3C and 4C also require calibration data and will "
            "cause a processor board fault if enabled without the "
            "calibration data. Either calibrate the probes or disable "
            "the options to clear the fault.\n"
            "Option 1G works by telling the instrument it has a "
            "different acquisition board. Unticking it hands the board "
            "identity back to the hardware.")).pack(anchor="w",
                                                     pady=(6, 0))
        # An option whose firmware has no getter for it cannot appear on
        # that instrument, whatever its word says - so it is greyed. The
        # box still ticks and still writes: the whole point of writing to
        # a firmware that ignores it is to find out that it does.
        #
        # 1G is held to the TDS 540B by hand rather than by that rule.
        # Plenty of firmwares read the constant behind it, but it is an
        # acquisition board identity rather than an option flag and the
        # 540B is the one it has been traced on, so the rest are greyed
        # until somebody has tried it on them.
        able = firmware_options(state.get("idn"))
        told = [p.strip() for p in (state.get("idn") or "").split(",")]
        model = told[1].upper().replace(" ", "") if len(told) > 1 else ""

        def offered(code):
            if code == "1G":
                return model == "TDS540B"
            return able is None or code in able

        if not all(offered(c) for c, _w, _o, _d in Worker.OPTION_WORDS):
            ttk.Label(right, wraplength=330, justify="left",
                      foreground="#555", text=_(
                          "The greyed options are not offered on this "
                          "instrument. They can still be ticked, for "
                          "testing.")).pack(anchor="w", pady=(6, 0))
        ttk.Style().configure("Absent.TCheckbutton", foreground="#999")
        boxes = {}
        table = ttk.Frame(right)
        table.pack(fill="x", pady=(8, 0))
        for code, word, on, what in Worker.OPTION_WORDS:
            boxes[word] = (tk.BooleanVar(value=False), on)
            ttk.Checkbutton(
                table, variable=boxes[word][0],
                style=("TCheckbutton" if offered(code)
                       else "Absent.TCheckbutton"),
                text="%s - %s%s" % (code, what,
                                    "" if code in Worker.OPTION_SOFT
                                    else _("  (needs hardware)"))
            ).pack(anchor="w")

        def write():
            wants = [(word, on if var.get() else 0)
                     for word, (var, on) in boxes.items()]
            if not messagebox.askyesno(
                    _("Write the option words?"),
                    _("This writes %d word(s) to the instrument's "
                      "non-volatile memory. Nothing happens at all "
                      "unless the protection switch is unprotected, and "
                      "nothing can be read back to confirm it - the "
                      "boot screen is the only report.")
                    % len(wants) + "\n\n" + _("Proceed?"),
                    icon="warning", default="no", parent=dlg):
                return
            dlg.destroy()
            state.pop("dialog", None)
            busy(True, "wait")
            say(_("Writing the option words ..."))
            w.submit("sys_option", lambda k, ws=wants: k.sys_option(ws))

        row = ttk.Frame(right)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text=_("Write"), command=write).pack(side="right")
        ttk.Button(row, text=_("Close"),
                   command=dlg.destroy).pack(side="right", padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.bind("<Destroy>",
                 lambda e: state.pop("dialog", None)
                 if e.widget is dlg else None)
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (root.winfo_rootx() + 60,
                                 root.winfo_rooty() + 40))
        state["dialog"] = dlg

    def sys_first_look(_evt=None):
        """Read the instrument once, when this tab is first looked at.

        Not on connect: a dozen queries every time somebody opens the
        program is a second nobody asked for, and most sessions never
        come to this tab at all.
        """
        try:
            if tabs.select() != str(systab):
                return
        except tk.TclError:                  # before the notebook is up
            return
        if state.get("sysnow") is None and state.get("idn"):
            do_sys_read()

    tabs.bind("<<NotebookTabChanged>>", sys_first_look, add="+")

    # ------------------------------------------------- the settings tab
    # This program's own settings, as against the instrument's, which
    # are the tab before this one. The colours used to be a dialog
    # reached from three different tabs; a colour is chosen by looking
    # at the trace it is going to draw, so a window sitting on top of
    # that trace was the wrong shape for the job from the start.
    settab = ttk.Frame(tabs)
    tabs.add(settab, text=_("Settings"))
    named(settab, "Settings")

    setleft = ttk.Frame(settab)
    setleft.pack(side="left", fill="both", expand=True, padx=(4, 3), pady=6)
    setright = ttk.Frame(settab)
    setright.pack(side="left", fill="both", expand=True, padx=(3, 4), pady=6)

    setbox1 = ttk.LabelFrame(setleft, padding=8)
    says(setbox1, "Plot colours")
    setbox1.pack(fill="x")

    set_swatches = {}
    SET_LABELS = (("background", "Background"),
                  ("graticule", "Division lines"),
                  ("major", "Centre lines"),
                  ("pips", "Minor tick marks"),
                  ("border", "Border"),
                  ("trace", "Trace"), ("label", "Text"),
                  ("grid", "Grid"), ("mask", "Mask"),
                  ("select", "Selected"),
                  ("hover", "Mouseover"),
                  ("cut", "Split line"))

    def set_working():
        """The colours in force, as a plain dictionary."""
        return dict(tds_wfm.scheme(state.get("colours")))

    def set_apply(working):
        """Take them, draw with them, and remember them.

        No Apply button and no Cancel: a tab has no moment of opening
        to put things back to, and a colour picked against a written
        description rather than against the trace it draws is a guess.
        """
        state["colours"] = dict(working)
        draw_plot()
        draw_mask()
        draw_limits()
        settings = load_settings()
        settings["colours"] = dict(working)
        save_settings(settings)
        set_repaint()

    def set_repaint():
        working = set_working()
        for key, button in set_swatches.items():
            button.config(background=working[key],
                          activebackground=working[key])

    setgrid = ttk.Frame(setbox1)
    setgrid.pack(fill="x")
    for row, (key, label) in enumerate(SET_LABELS):
        one = ttk.Label(setgrid, text=_(label))
        one.grid(row=row % 6, column=0 if row < 6 else 2, sticky="w",
                 pady=2, padx=(0, 6))
        says(one, label)
        # A plain tk button, not ttk: a themed button will not take a
        # background colour on Windows, which is the one thing this
        # button exists to show.
        swatch = tk.Button(setgrid, width=6, relief="ridge")
        swatch.grid(row=row % 6, column=1 if row < 6 else 3, sticky="w",
                    pady=2, padx=(0, 16))
        set_swatches[key] = swatch

        def choose(k=key, name=label):
            working = set_working()
            got = colorchooser.askcolor(color=working[k], parent=root,
                                        title=_(name))
            if got and got[1]:
                working[k] = got[1]
                set_preset.set("")
                set_apply(working)

        swatch.config(command=choose)

    setrow1 = ttk.Frame(setbox1)
    setrow1.pack(fill="x", pady=(10, 0))
    lbl_setpre = ttk.Label(setrow1, text=_("Preset"))
    lbl_setpre.pack(side="left", padx=(0, 6))
    says(lbl_setpre, "Preset")
    set_preset = ttk.Combobox(setrow1, width=20, state="readonly")
    set_preset.pack(side="left")

    def set_refill(select=None):
        names = sorted(colour_presets())
        set_preset.config(values=names)
        if select in names:
            set_preset.set(select)
        else:
            here = set_working()
            match = [n for n, c in colour_presets().items() if c == here]
            set_preset.set(match[0] if match else "")

    def set_load(_evt=None):
        chosen = colour_presets().get(set_preset.get())
        if chosen:
            working = set_working()
            working.update(chosen)
            set_apply(working)
            set_refill(set_preset.get())

    set_preset.bind("<<ComboboxSelected>>", set_load)

    def set_save():
        name = simpledialog.askstring(
            _("Save preset"), _("Name for this colour scheme:"),
            parent=root, initialvalue=set_preset.get() or "")
        name = (name or "").strip()
        if not name:
            return
        settings = load_settings()
        saved = settings.get("colour_presets")
        saved = saved if isinstance(saved, dict) else {}
        saved[name] = set_working()
        settings["colour_presets"] = saved
        save_settings(settings)
        set_refill(name)
        say(_("Saved colour preset '%s'") % name)

    def set_drop():
        name = set_preset.get()
        if not name:
            return
        settings = load_settings()
        saved = settings.get("colour_presets")
        saved = saved if isinstance(saved, dict) else {}
        if name not in saved:
            messagebox.showinfo(
                _("Cannot delete that preset"),
                _("'%s' is one of the supplied schemes, not one of "
                  "yours. Save a scheme of the same name to replace "
                  "it.") % name)
            return
        if not messagebox.askyesno(
                _("Delete preset"),
                _("Delete the colour scheme '%s'?") % name, default="no"):
            return
        del saved[name]
        settings["colour_presets"] = saved
        save_settings(settings)
        set_refill()
        say(_("Deleted colour preset '%s'") % name)

    btn_setsave = ttk.Button(setrow1, text=_("Save..."), padding=(8, 0),
                             command=set_save)
    btn_setsave.pack(side="left", padx=(6, 0))
    says(btn_setsave, "Save...")
    btn_setdrop = ttk.Button(setrow1, text=_("Delete"), padding=(8, 0),
                             command=set_drop)
    btn_setdrop.pack(side="left", padx=4)
    says(btn_setdrop, "Delete")
    lbl_setsays = ttk.Label(setbox1, foreground="#555", wraplength=340,
                            justify="left")
    says(lbl_setsays, "Applies to the waveform plot, the mask editor and "
                      "the limits editor, and to any picture you save. "
                      "Fewer colours make a smaller file.")
    lbl_setsays.pack(anchor="w", pady=(8, 0))

    # ---- how large a saved picture is
    setbox2 = ttk.LabelFrame(setright, padding=8)
    says(setbox2, "Saved pictures")
    setbox2.pack(fill="x")
    setrow2 = ttk.Frame(setbox2)
    setrow2.pack(fill="x")
    lbl_setwide = ttk.Label(setrow2, text=_("Resolution"))
    lbl_setwide.pack(side="left", padx=(0, 6))
    says(lbl_setwide, "Resolution")
    # Read-only, so the only sizes that can be asked for are the ones
    # below. A free-text width was a number nobody had a reason to
    # choose, and the graticule is centred in whatever canvas it is
    # given rather than stretched to it, so an off-list size bought
    # nothing but an odd-shaped file.
    set_wide = ttk.Combobox(setrow2, width=11, state="readonly",
                            values=[png_named(wh) for wh in PNG_SIZES])
    set_wide.set(png_named(state["pngsize"]))
    set_wide.pack(side="left")

    # Not set_size: there is already one of those further down this same
    # function, for the file list's Size column. Two defs of one name in
    # one scope means the later one wins every lookup by name, so Default
    # called the wrong one and raised TypeError.
    def set_png(_evt=None):
        state["pngsize"] = png_size(set_wide.get())
        set_wide.set(png_named(state["pngsize"]))
        settings = load_settings()
        settings["png_size"] = list(state["pngsize"])
        save_settings(settings)

    set_wide.bind("<<ComboboxSelected>>", set_png)

    def set_png_default():
        set_wide.set(png_named(PNG_DEFAULT_SIZE))
        set_png()

    btn_setdef = ttk.Button(setrow2, text=_("Default"), padding=(8, 0),
                            command=set_png_default)
    btn_setdef.pack(side="left", padx=6)
    says(btn_setdef, "Default")
    # Said out loud because the box is in the same panel and would
    # otherwise look as though it governed both. It does not: a
    # screenshot is the instrument's own hardcopy, saved byte for byte.
    lbl_setnative = ttk.Label(setbox2, foreground="#555", wraplength=340,
                              justify="left")
    says(lbl_setnative, "Resolution applies to waveform plot,\nmask "
                        "editor and limits editor.\nScreenshots are not "
                        "affected, they are always saved at the "
                        "oscilloscope's native resolution of 640x480.")
    lbl_setnative.pack(anchor="w", pady=(8, 0))

    # ---- how far an arrow key moves a point
    sys_rule(setright)
    setbox3 = ttk.LabelFrame(setright, padding=8)
    says(setbox3, "Editing")
    setbox3.pack(fill="x")
    setrow3 = ttk.Frame(setbox3)
    setrow3.pack(fill="x")
    lbl_setnudge = ttk.Label(setrow3, text=_("Nudge distance"))
    lbl_setnudge.pack(side="left", padx=(0, 6))
    says(lbl_setnudge, "Nudge distance")
    # Read-only for the same reason the resolution is: these are the
    # distances worth having, and a typed 0.037 is a number nobody
    # chose. The first entry keeps what the program did before there
    # was a setting, which is a fifth of a grid square, so five
    # presses cross one.
    set_nudge = ttk.Combobox(setrow3, width=15, state="readonly")

    def nudge_shown(step):
        """The name for a distance, in whatever language is current."""
        return (_(NUDGE_GRID) if not step else nudge_named(step))

    def nudge_relabel():
        """The list, and what is in the box, after a language change.

        A Combobox's values are strings it was handed once, so unlike a
        label they do not follow the language on their own. One of them
        is a word rather than a number, which is the one that used to
        stay in English.
        """
        set_nudge.config(values=[nudge_shown(n)
                                 for n in (None,) + NUDGE_STEPS])
        set_nudge.set(nudge_shown(state["nudge"]))

    nudge_relabel()
    relabel.append(nudge_relabel)
    set_nudge.pack(side="left")

    def set_nudge_step(_evt=None):
        said = set_nudge.get()
        state["nudge"] = (None if said == nudge_shown(None)
                          else nudge_value(said))
        set_nudge.set(nudge_shown(state["nudge"]))
        settings = load_settings()
        settings["nudge"] = state["nudge"]
        save_settings(settings)

    set_nudge.bind("<<ComboboxSelected>>", set_nudge_step)
    lbl_setnudgesays = ttk.Label(setbox3, foreground="#555", wraplength=340,
                                 justify="left")
    says(lbl_setnudgesays,
         "How far one arrow key moves the selected point or shape in the "
         "mask editor and the limits editor, in percent of the "
         "graticule. One division is ten percent across and twelve and a "
         "half percent up.")
    lbl_setnudgesays.pack(anchor="w", pady=(8, 0))

    # ---- where this program keeps things
    sys_rule(setright)
    setbox4 = ttk.LabelFrame(setright, padding=8)
    says(setbox4, "This program")
    setbox4.pack(fill="x")
    lbl_setwhere = ttk.Label(setbox4, foreground="#555", wraplength=340,
                             justify="left")
    lbl_setwhere.pack(anchor="w")

    def set_about():
        """Who wrote it, under what, and where it keeps its things.

        Rebuilt on a language change rather than set once: the label was
        written at build time, so everything but the address stayed in
        the language the window started in.
        """
        lbl_setwhere.config(text="\n".join([
            _("Version %s") % __version__,
            "%s  -  %s" % (__author__, __email__),
            _("Free software under the %s licence") % __licence__,
            _("Written with Claude, by Anthropic"),
            _("Settings and log: %s") % APPDIR]))

    set_about()
    relabel.append(set_about)

    def set_first_fill():
        """Fill the boxes once, after the whole window exists.

        Not while the tab is being built: colour_presets is defined
        further down this same function, and a closure looks its names
        up when it runs. Called at build time this raised NameError
        before the window ever appeared.
        """
        set_repaint()
        set_refill()

    root.after_idle(set_first_fill)

    # The order they are read in, which is not the order they are built
    # in. Building follows what depends on what - the limits tab needs
    # the mask editor's tools - and this follows what somebody reaches
    # for first, with the two that are about this program rather than
    # about the instrument at the end.
    for at, page in enumerate((filetab, scrtab, wavetab, limtab, masktab,
                               errtab, systab, settab)):
        tabs.insert(at, page)

    # One status bar along the bottom: the message on the left and the
    # progress bar on the right, which is where the free-space readout
    # used to be. The bar is packed and unpacked rather than left in
    # place empty, so an idle window does not imply something is
    # happening.
    bar = ttk.Frame(root)
    # Inset to match the content panes above. Flush against the frame the
    # sunken edges get clipped by the window's rounded corners.
    bar.pack(fill="x", side="bottom", padx=6, pady=(2, 6))
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
                        if c.isalnum() or c in " -_").strip()
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

    def update_nav():
        """Enable each arrow only where it would actually go somewhere.

        Deliberately not part of the blanket enable in busy(): back and
        forward depend on where you have been, so re-enabling them
        wholesale would offer a Back button at the start of a session.
        """
        hist, at = state["history"], state["hist_at"]
        can = not state["busy"]
        btn_back.config(state="normal" if can and at > 0 else "disabled")
        btn_fwd.config(
            state="normal" if can and at < len(hist) - 1 else "disabled")
        btn_up.config(state="normal" if can and state["cwd"]
                      and parent_of(state["cwd"]) else "disabled")

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

    # Defined for real further down, once the waveform tab's widgets
    # exist. busy() runs before then during start-up and has to be able
    # to call it, and a closure reads the name when it is called rather
    # than when it is written - so the stub is replaced, not shadowed.
    def wfm_buttons(_evt=None):
        pass

    def busy(on, working=None):
        """`working` is None for no bar, 'wait' for marquee, 'steps' for one
        that fills. Anything that clears busy also clears the bar, so it
        cannot be left spinning after the work has stopped."""
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
        root.config(cursor="watch" if on else "")
        if not on:
            progress_off()
        elif working == "wait":
            progress(None)
        elif working == "steps":
            progress(0.0)

    def fault(where, text):
        """Report a bug in this program without taking the window with it.

        Releases the busy state - a fault mid-operation would otherwise
        leave every button disabled, which is indistinguishable from a
        hang - writes the traceback next to the script so it can be read
        after the fact, and tells the user in as many words that the
        instrument is fine and it is this program that is at fault.
        """
        try:
            with open(LOGFILE, "a", encoding="utf-8",
                  errors="replace") as fh:
                fh.write("\n==== %s: %s ====\n%s"
                         % (time.strftime("%Y-%m-%d %H:%M:%S"), where, text))
        except Exception:
            pass
        # Under pythonw there is no console and sys.stderr is None, so this
        # is a guard and not politeness.
        if sys.stderr is not None:
            sys.stderr.write(text)
        try:
            busy(False)
            say(_("%(job)s failed - see %(file)s")
                % {"job": where, "file": os.path.basename(LOGFILE)})
            messagebox.showerror(
                "Error",
                "%s failed because of a bug in this program.\n\n"
                "Nothing was sent to the instrument, and the window is "
                "still usable. The details were written to:\n%s\n\n%s"
                % (where, LOGFILE, text.strip().splitlines()[-1]))
        except Exception:
            pass

    # A crash in an ordinary callback - a button, a key, a tree click - is
    # printed to a console the user may not even have. Route it here too.
    root.report_callback_exception = lambda *_exc: fault(
        "Command", traceback.format_exc())

    # --------------------------------------------------------- helpers
    def parent_of(path):
        p = path.rstrip("/")
        if "/" not in p:
            return None
        return p.rsplit("/", 1)[0] or (p.split(":")[0] + ":")

    def join(path, name):
        return "%s/%s" % (path.rstrip("/"), name)

    def icon_for(name, is_dir):
        """A Tk image for this name, or None if icons are unavailable.

        Kept referenced in `state`, because Tk discards an image the
        moment Python stops holding it and the row goes blank.
        """
        try:
            img = winicons.icon_for(tk, name, is_dir)
            state.setdefault("icons", []).append(img)
            return img
        except Exception:
            return ""

    def sel_rows(kind=None):
        """Selected rows of a given kind - 'dir', 'file', or both."""
        rows = state.get("rows") or {}
        out = []
        for iid in lst.selection():
            k, name = rows.get(iid, ("file", lst.item(iid, "text")))
            if kind is None or k == kind:
                out.append(name)
        return out

    def sel_files():
        """Every file selected in the right pane, in the order shown."""
        return sel_rows("file")

    def sel_folders():
        return sel_rows("dir")

    def set_size(name, nbytes):
        """Fill in the Size column once a file's real length is known.

        Sizes are not in the directory listing, so a file's size is only
        known after it has been transferred. Matched by name because row
        ids are positional.
        """
        state["sizes"][join(state["cwd"] or "", name)] = format(nbytes, ",")
        for iid in lst.get_children():
            if lst.item(iid, "text") == name:
                lst.set(iid, "size", format(nbytes, ","))
                return

    def mass_storage_note(payload):
        """Explain the message the front panel will be showing.

        The instrument puts 'Mass storage error' on its own display when it
        raises event 250, and there is no way to stop it from here. Saying
        nothing would leave the user staring at an error beside a delete
        that actually worked, so the status bar names it first.
        """
        if MASS_STORAGE not in (payload.get("events") or []):
            return ""
        return ("  (the instrument logged a mass storage error during the "
                "delete - the folder is verified gone; see the log)")

    def report_failures(verb, failed):
        """Say what did not work, without hiding what did."""
        if not failed:
            return
        messagebox.showwarning(
            "Some files could not be %sed" % verb,
            "%d file(s) could not be %sed. Everything else was done.\n\n%s"
            % (len(failed), verb,
               "\n".join("    %s\n        %s" % (p.rsplit("/", 1)[-1], e)
                         for p, e in failed[:6])))

    def name_list(names, limit=10):
        """Names for a dialog: readable, and never a wall of text."""
        shown = "\n".join("    " + n for n in names[:limit])
        if len(names) > limit:
            shown += "\n    ...and %d more" % (len(names) - limit)
        return shown

    # ------------------------------------------------------- navigation
    def navigate(path, force=False):
        """Show a folder, from cache if we already classified it."""
        if state["busy"]:
            return
        path = path.rstrip("/") or "hd0:"
        if not force and path in state["cache"]:
            show(state["cache"][path])
            return
        busy(True)
        say(_("Reading %s ...") % path)
        w.submit("split", lambda k, p=path: k.listdir_split(p))

    def show(payload):
        path = payload["cwd"].rstrip("/")
        state["cwd"] = path
        state["cache"][path] = payload

        # Recorded here rather than in navigate(), so that history holds
        # only folders actually arrived at - not ones that failed to load.
        # A back or forward step sets this flag so it does not record the
        # move it is itself making.
        hist, at = state["history"], state["hist_at"]
        if state.pop("no_record", False):
            pass
        elif hist and at > 0 and hist[at - 1] == path:
            # Arriving where we came from - going Up out of a folder we had
            # just entered, typically. Treated as a step back rather than a
            # new destination, so Forward still leads back down. Appending
            # instead would leave Forward greyed out and the way back only
            # reachable by clicking into the folder again.
            state["hist_at"] = at - 1
        elif not hist or hist[at] != path:
            del hist[at + 1:]
            hist.append(path)
            state["hist_at"] = len(hist) - 1

        ent_path.delete(0, "end")
        ent_path.insert(0, path)

        ensure_node(path)
        fill_tree(path, payload["dirs"])
        if tree.exists(path):
            tree.see(path)
            # Focus as well as selection. selection_set fires
            # <<TreeviewSelect>>, whose handler reads tree.focus() - and if
            # that still points at wherever the tree was last clicked, it
            # navigates straight back there. Opening a folder from the file
            # pane then appeared to do nothing at all.
            tree.selection_set(path)
            tree.focus(path)

        fill_list(path, payload)
        busy(False)
        if payload.get("no_media"):
            say(_("No disk in the drive"))
        else:
            say(_("%d items  (%d folders, %d files)")
                % (len(payload["dirs"]) + len(payload["files"]),
                   len(payload["dirs"]), len(payload["files"])))

    def size_of(path, name):
        """Known size in bytes, or None. Sizes only exist once transferred."""
        text = state["sizes"].get(join(path, name))
        try:
            return int(text.replace(",", "")) if text else None
        except (AttributeError, ValueError):
            return None

    def fill_list(path, payload):
        """Draw the file pane from a listing, in the current sort order.

        Separate from show() so that clicking a column header can reorder
        what is already on screen without asking the instrument again -
        each listing costs seconds, and sorting should cost nothing.
        """
        col, rev = state["sort"]
        folders = list(payload["dirs"])
        files = list(payload["files"])

        def key(name, is_dir):
            if col == "size":
                # Unknown sizes sort last whichever way the arrow points,
                # rather than pretending to be zero.
                n = size_of(path, name)
                return (n is None, n if n is not None else 0, name.upper())
            if col == "type":
                return (type_of(name) if not is_dir else "", name.upper())
            return (name.upper(),)

        folders.sort(key=lambda n: key(n, True), reverse=rev)
        files.sort(key=lambda n: key(n, False), reverse=rev)

        # Folders stay above files whichever column is sorted, as they do
        # in Explorer: the sort orders each group, it does not merge them.
        lst.delete(*lst.get_children())
        state["rows"] = {}
        # Row ids are positional, not derived from the name. Naming them
        # after the file made two entries with the same name a fatal
        # TclError, and the instrument can report the same name twice.
        n = 0
        for d in folders:
            iid = "R%d" % n
            lst.insert("", "end", iid=iid, text=d, image=icon_for(d, True),
                       values=("", _("File folder")))
            state["rows"][iid] = ("dir", d)
            n += 1
        for f in files:
            iid = "R%d" % n
            lst.insert("", "end", iid=iid, text=f, image=icon_for(f, False),
                       values=(state["sizes"].get(join(path, f), ""),
                               type_of(f)))
            state["rows"][iid] = ("file", f)
            n += 1
        show_sort_arrow()

    HEADINGS = (("#0", "name", _("Name")), ("size", "size", _("Size")),
                ("type", "type", _("Type")))

    def show_sort_arrow():
        """Mark the sorted column, the way Explorer marks it."""
        col, rev = state["sort"]
        for ident, key, label in HEADINGS:
            mark = ("  ▲" if not rev else "  ▼") if key == col else ""
            lst.heading(ident, text=label + mark)

    def sort_by(key):
        """Click a heading to sort by it; click the same one to reverse."""
        col, rev = state["sort"]
        state["sort"] = (key, not rev if key == col else False)
        payload = state["cache"].get(state["cwd"])
        if payload:
            keep = [state["rows"][i][1] for i in lst.selection()
                    if i in state.get("rows", {})]
            fill_list(state["cwd"], payload)
            # Selection follows the files, not the row positions, so
            # sorting does not silently change what Delete would act on.
            again = [i for i, (_k, n) in state["rows"].items() if n in keep]
            if again:
                lst.selection_set(again)

    for _ident, _key, _label in HEADINGS:
        lst.heading(_ident, command=lambda k=_key: sort_by(k))

    # ------------------------------------------------------ folder tree
    def ensure_node(path):
        """Create the node for `path` and every ancestor, in order."""
        parts = path.rstrip("/").split("/")
        cur = parts[0]
        if not tree.exists(cur):
            tree.insert("", "end", iid=cur, text=" " + cur, open=True,
                        image=icon_for("drive", True))
        for p in parts[1:]:
            nxt = cur + "/" + p
            if not tree.exists(nxt):
                tree.insert(cur, "end", iid=nxt, text=" " + p,
                            image=icon_for(p, True))
            cur = nxt
        return cur

    def fill_tree(path, dirs):
        node = ensure_node(path)
        for child in tree.get_children(node):
            tree.delete(child)
        for d in dirs:
            iid = join(path, d)
            tree.insert(node, "end", iid=iid, text=" " + d,
                        image=icon_for(d, True))
            # a placeholder so the expander arrow appears without us having
            # to classify the grandchildren yet - that is the expensive part
            tree.insert(iid, "end", iid=iid + "/~", text="")
        tree.item(node, open=True)

    def on_expand(_evt=None):
        node = tree.focus()
        if not node or state["busy"]:
            return
        kids = tree.get_children(node)
        if len(kids) == 1 and kids[0].endswith("/~"):
            navigate(node)

    def on_tree_select(_evt=None):
        node = tree.focus()
        if node and node != state["cwd"] and not state["busy"]:
            navigate(node)

    tree.bind("<<TreeviewOpen>>", on_expand)
    tree.bind("<<TreeviewSelect>>", on_tree_select)

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
        if on:
            w.cancelled.clear()

    def do_rescan():
        """Re-scan the bus and refresh the dropdown, staying connected.

        Pressed a second time while a scan is running, this cancels it.
        """
        if state.get("scanning"):
            w.cancelled.set()
            say(_("Stopping the scan ..."))
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
                                  if i["scope"]])}))
        w.submit("scan", lambda k: k.scan(), needs_fs=False)

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
        dlg = tk.Toplevel(root)
        dlg.title(_("Select instrument"))
        dlg.transient(root)
        dlg.resizable(True, False)
        try:
            dlg.iconbitmap(resource("app.ico"))
        except Exception:
            pass

        pad = ttk.Frame(dlg, padding=10)
        pad.pack(fill="both", expand=True)
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
        remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(pad, text=_("Remember this address"),
                        variable=remember).pack(anchor="w", pady=(6, 0))
        note = ttk.Label(pad, text="", wraplength=560)
        note.pack(fill="x", pady=(6, 0))

        def on_pick(_e=None):
            sel = found.selection()
            if sel:
                addr_var.set(found.item(sel[0], "text"))

        found.bind("<<TreeviewSelect>>", on_pick)
        found.bind("<Double-1>", lambda e: (on_pick(), do_connect()))

        def do_scan():
            if state["busy"]:
                return
            found.delete(*found.get_children())
            note.config(text=_("Scanning. Every address is asked to "
                               "identify itself, which changes nothing "
                               "on it."))
            busy(True, "steps")
            scanning(True)
            state["scan_into"] = fill_found
            state["scan_failed"] = lambda text: (
                note.config(text="%s\n\n%s"
                            % (text, _("Type an address above if you know "
                                       "it."))) if alive() else None)
            w.submit("scan", lambda k: k.scan(), needs_fs=False)

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
                note.config(text=_("%d instrument(s) found, %d of them a TDS "
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
        do_scan()

    def reconnect(addr):
        """Point everything at a different instrument and start again."""
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
        root.title("TDS Toolkit %s - %s"
                   % (__version__, _("connecting to %s") % addr))
        busy(True, "wait")
        say(_("Connecting to %s ...") % addr)
        w.submit("connect", lambda k, a=addr: k.connect(a), needs_fs=False)

    def do_back(_evt=None):
        if state["busy"] or state["hist_at"] <= 0:
            return
        state["hist_at"] -= 1
        state["no_record"] = True
        navigate(state["history"][state["hist_at"]])

    def do_forward(_evt=None):
        if state["busy"] or state["hist_at"] >= len(state["history"]) - 1:
            return
        state["hist_at"] += 1
        state["no_record"] = True
        navigate(state["history"][state["hist_at"]])

    def do_up(_evt=None):
        p = parent_of(state["cwd"] or "")
        if p:
            navigate(p)

    def do_refresh():
        if state["cwd"]:
            navigate(state["cwd"], force=True)

    def do_download(_evt=None):
        """Save the selection to the PC. Also what double-click does.

        One file asks where to put it, as Save As does everywhere. Several
        files ask for a folder instead, because asking for ten filenames in
        a row would be a poor way to treat anybody.
        """
        folders = sel_folders()
        if folders and not sel_files():
            if state["busy"]:
                return
            if len(folders) > 1:
                messagebox.showinfo(
                    _("One folder at a time"),
                    "Saving a folder brings down everything inside it, so "
                    "they are done one at a time.")
                return
            destdir = filedialog.askdirectory(
                title="Save '%s' and its contents into" % folders[0],
                mustexist=True)
            if not destdir:
                return
            src = join(state["cwd"], folders[0])
            busy(True, "steps")
            say(_("Reading the contents of %s ...") % src)
            w.submit("tree",
                     lambda k, p=src, d=destdir: k.download_tree(p, d))
            return

        names = sel_files()
        if not names or state["busy"]:
            return
        if len(names) == 1:
            dest = filedialog.asksaveasfilename(initialfile=names[0])
            if not dest:
                return
            state["saveas"] = dest
            # Marquee: one file arrives as a single stream with no length
            # in front of it, so there is nothing to measure against.
            busy(True, "wait")
            say(_("Downloading %s ...") % names[0])
            w.submit("download",
                     lambda k: k.read(join(state["cwd"], names[0])))
            return

        destdir = filedialog.askdirectory(
            title="Save %d files to folder" % len(names), mustexist=True)
        if not destdir:
            return
        clashes = [n for n in names if os.path.exists(os.path.join(destdir, n))]
        if clashes and not messagebox.askyesno(
                _("Replace files?"),
                "%d of these already exist in that folder and will be "
                "replaced:\n\n%s\n\nContinue?"
                % (len(clashes), name_list(clashes)),
                icon="warning", default="no"):
            return
        paths = [join(state["cwd"], n) for n in names]
        busy(True, "steps")
        say(_("Downloading %d files ...") % len(names))
        w.submit("downloads",
                 lambda k: k.download_many(paths, destdir))

    def do_open(_evt=None):
        """Double-click: open a folder, save a file.

        Explorer's single gesture means two things depending on what is
        under it, and the instrument has nothing that can open a file, so
        saving it is the only sensible reading for one.
        """
        folders = sel_folders()
        if folders and not sel_files():
            navigate(join(state["cwd"], folders[0]))
            return
        do_download()

    lst.bind("<Double-1>", do_open)
    lst.bind("<Return>", do_open)
    lst.bind("<Control-a>", lambda e: (lst.selection_set(lst.get_children()),
                                       "break")[1])
    lst.bind("<Control-A>", lambda e: (lst.selection_set(lst.get_children()),
                                       "break")[1])

    def upload_possible():
        """False, with an explanation, on a firmware that cannot receive.

        The Upload button is greyed on such an instrument, but a drop from
        Explorer does not go through the button, so the check lives here
        where both routes pass.
        """
        if by_name["Upload"] not in (state.get("cannot") or ()):
            return True
        messagebox.showinfo(
            _("Cannot upload to this instrument"),
            _("This instrument's firmware has no FILESYSTEM:WRITEFILE "
              "command, so files cannot be sent to it over GPIB.\n\n"
              "Browsing, downloading, creating folders and deleting all "
              "still work."))
        return False

    def do_upload():
        if state["busy"] or not state["cwd"]:
            return
        if not upload_possible():
            return
        src = filedialog.askopenfilename()
        if not src:
            return
        data = open(src, "rb").read()
        # The PC name is very unlikely to be 8.3-clean, so it is offered as
        # a starting point and corrected here rather than silently chopped
        # to twelve characters on the way out, which is what used to happen.
        name = os.path.basename(src).upper()
        while True:
            why = check_83(name)
            if why is None:
                break
            messagebox.showwarning(_("Cannot use that name"), why)
            name = simpledialog.askstring(
                "Name on the instrument", "Name to save it as:",
                initialvalue=name)
            if name is None:
                return
            name = name.strip().upper()
        if not messagebox.askyesno(
                "Upload",
                "Write %s (%s bytes) to %s ?\n\n"
                "It is read back and compared afterwards; the upload reports "
                "failure rather than success on a guess."
                % (name, format(len(data), ","),
                   join(state["cwd"], name))):
            return
        busy(True, "steps")
        say(_("Uploading %s, then verifying ...") % name)
        w.submit("upload",
                 lambda k: k.write_verified(join(state["cwd"], name), data))

    def est_time(nbytes, nfiles):
        """A rough figure for how long an upload will take, in words.

        Throughput depends on the medium, not the instrument: a hard disk
        runs at about 33 KB/s, a floppy at about 4 KB/s - measured at 3.9
        on a TDS 784C reading 789,504 bytes and 3.2 on a TDS 640A. Quoting
        the hard disk figure for a floppy underestimates by eight times,
        which turns "about a minute" into ten and looks like a hang.

        Every file is written and then read back to verify, so the payload
        crosses the bus twice.
        """
        rate = 4000.0 if (state["cwd"] or "").lower().startswith("fd") \
            else 33000.0
        secs = nfiles * 1.6 + 2.0 * nbytes / rate
        if secs < 90:
            return _("about %d seconds") % max(2, int(round(secs)))
        return _("about %d minutes") % int(round(secs / 60.0))

    def do_drop(paths, dest_folder):
        """Upload files dropped from Explorer, after showing the plan.

        Nothing is sent until the user has seen the destination, what each
        file will be called once it is subject to 8.3, which existing files
        will be replaced, and roughly how long it will take.
        """
        if state["busy"]:
            return
        if not dest_folder:
            return
        if not upload_possible():
            return
        folders = [p for p in paths if os.path.isdir(p)]
        files = [p for p in paths if os.path.isfile(p)]
        if folders and not files:
            messagebox.showinfo(
                "Folders cannot be dropped",
                "Only files can be dropped here. Create the folder with "
                "New Folder, open it, and drop the files into it.")
            return

        cached = state["cache"].get(dest_folder)
        present = ((cached["dirs"] + cached["files"]) if cached else [])
        taken = list(present)
        plan, renamed = [], []
        for src in files:
            leaf = os.path.basename(src)
            name = to_83(leaf, taken)
            taken.append(name)
            plan.append((src, name))
            if name != leaf.upper():
                renamed.append((leaf, name))

        total = sum(os.path.getsize(s) for s, _ in plan)
        clashes = [n for _, n in plan if n.upper() in
                   [x.upper() for x in present]]

        lines = [_("Upload %(count)d file(s) to %(where)s ?")
                 % {"count": len(plan), "where": dest_folder}, ""]
        lines.append("\n".join(
            "    %s  ->  %s" % (os.path.basename(s), n)
            for s, n in plan[:12]))
        if len(plan) > 12:
            lines.append(_("    ...and %d more") % (len(plan) - 12))
        lines += ["", _("%(bytes)s bytes, %(time)s.")
                  % {"bytes": format(total, ","),
                     "time": est_time(total, len(plan))}]
        if renamed:
            lines.append(_("%d name(s) were shortened to fit the "
                           "instrument's 8.3 limit.") % len(renamed))
        if clashes:
            lines.append(_("%(count)d file(s) already there will be "
                           "replaced: %(names)s")
                         % {"count": len(clashes),
                            "names": ", ".join(clashes[:6])})
        if folders:
            lines.append(_("%d dropped folder(s) will be skipped.")
                         % len(folders))
        lines.append("")
        lines.append(_("Each file is read back and compared after "
                       "writing."))

        if not messagebox.askyesno(_("Upload"), "\n".join(lines)):
            return
        items = [(s, join(dest_folder, n)) for s, n in plan]
        busy(True, "steps")
        say(_("Uploading %(count)d file(s) to %(where)s ...")
            % {"count": len(items), "where": dest_folder})
        w.submit("uploads", lambda k, it=items: k.upload_many(it))

    def on_drop_list(evt):
        """Dropped on the file pane: into the folder currently shown."""
        do_drop(list(root.tk.splitlist(evt.data)), state["cwd"])

    def on_drop_tree(evt):
        """Dropped on the tree: into the folder actually under the pointer,
        which is what Explorer does. Missing the rows drops nowhere."""
        node = tree.identify_row(evt.y_root - tree.winfo_rooty())
        if not node or node.endswith("/~"):
            return
        do_drop(list(root.tk.splitlist(evt.data)), node)

    if DND:
        for widget, handler in ((lst, on_drop_list), (tree, on_drop_tree)):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", handler)

    # ------------------------------------------------ waveform commands

    def ref_line(name):
        """One row of either list: the name, and what the name cannot say.

        Three things can be worth saying beside a source. A reference
        holding a file that has not been sent yet says so, because
        otherwise the pane would show a waveform under a name that means
        something quite different on the instrument. A source that has
        been chosen but never captured says to press Get waveform -
        choosing one no longer reads it, since a source read on its own
        would come from a different moment than everything else on the
        graticule. And a source that is switched off at the instrument
        says that instead, because no amount of pressing Get waveform
        will capture something that is not being acquired.
        """
        held = (state.get("staged") or {}).get(name)
        if held:
            return _("%(ref)s - %(file)s, not sent yet") % {
                "ref": name,
                "file": (held.origin or held.source or "")[:24]}
        if name not in (state.get("wticked") or ()):
            return name
        if (state.get("fetched") or {}).get(name) is not None:
            return name
        if not readable(name):
            return _("%s - switched off") % name
        return _("%s - press Get waveform") % name

    def show_sources(payload):
        """Every source the instrument has, greyed when it is not on.

        Showing only what is displayed hides the fact that a scope has
        four channels, and leaves no way to ask for the one that is
        switched off. Everything is listed; what cannot be read right
        now is greyed, and a double-click turns it on.

        Live channels and maths go in the top list, stored references in
        the bottom one. What is chosen stays chosen across a rebuild:
        the lists are rebuilt every time a source is switched on or off,
        and clearing the buttons each time would take the traces off the
        graticule for no reason the user can see.
        """
        every = payload.get("all") or []
        on = set(payload.get("sources") or [])
        names = [n for n, _o in every] if every and \
            isinstance(every[0], (list, tuple)) else list(every)
        state["wall"] = names
        state["wlive"] = [n for n in names if not n.startswith("REF")]
        refs = [n for n in names if n.startswith("REF")]
        if refs:
            state["wrefs"] = refs
        else:
            refs = list(state.get("wrefs") or tds_wfm.REFS)
        held = [n for n in (state.get("wticked") or ())
                if n in state["wlive"] or n in refs]
        if not held and state["wlive"]:
            # Nothing chosen yet, so the first source that can actually
            # be read - not simply the first one listed. Chosen, not
            # fetched: connecting to an instrument should not start
            # pulling waveforms off it uninvited.
            held = [next((n for n in state["wlive"] if n in on),
                         state["wlive"][0])]
        state["wticked"] = held
        fill_sources(wsrc, state["wlive"], on, ref_line)
        fill_sources(wref, refs, on, ref_line)
        wfm_buttons()

    def wfm_order(names):
        """These sources in the order the two lists show them."""
        live = state.get("wlive") or []
        refs = list(state.get("wrefs") or tds_wfm.REFS)
        want = set(names)
        rest = [n for n in names if n not in live and n not in refs]
        return ([n for n in live if n in want]
                + [n for n in refs if n in want] + rest)

    def show_ticks():
        """Bring the rows up to date with what is chosen, and with what
        they hold.

        Both follow the same state, so both are written here. Writing
        only the button left a captured source still saying "press Get
        waveform" beside a trace that was already on the graticule: the
        words are only rebuilt when the whole list is, and a capture
        does not rebuild the list.
        """
        chose = set(state.get("wticked") or ())
        for box in (wsrc, wref):
            for name in box.get_children():
                box.item(name, text=SHOWN if name in chose else HIDDEN,
                         values=(ref_line(name),))

    def fill_sources(box, names, on, describe=None):
        """Put these sources in a list, with their buttons set.

        The row keeps its name as its own id, so everything else can
        talk about sources rather than about row numbers.
        """
        box.delete(*box.get_children())
        chose = set(state.get("wticked") or ())
        for name in names:
            tags = () if name in on else ("off",)
            box.insert("", "end", iid=name, tags=tags,
                       text=SHOWN if name in chose else HIDDEN,
                       values=((describe or (lambda n: n))(name),))

    def on_click_row(box, evt):
        """A click on the button chooses that source, or lets it go.

        Nothing else in these lists responds to a click. The row
        highlight that used to be here was a second way of saying which
        source was meant, and the two disagreed: pressing a button left
        the highlight on the row above it, and Send, Load and Delete
        each acted on whichever of the two they happened to ask.
        """
        row = box.identify_row(evt.y)
        if row and box.identify_column(evt.x) == "#0":
            do_wfm_shown(row)
        return "break"

    def forget_source(name):
        """Drop a source's samples: it no longer exists on the scope.

        Off the graticule, out of what was kept, and its button let go.
        Leaving the trace drawn under the name of a reference that has
        just been deleted says the instrument still holds something it
        does not, and choosing it again would put it back from a copy of
        something gone.
        """
        state.get("fetched", {}).pop(name, None)
        state["wticked"] = [n for n in (state.get("wticked") or ())
                            if n != name]
        if name in [wave_name(x) for x in shown_waves()]:
            show_waves([x for x in shown_waves() if wave_name(x) != name],
                       keep_view=True)
        else:
            show_ticks()
            wfm_buttons()

    def do_wfm_shown(name):
        """Choose a source, or let it go: the button beside its name.

        Never goes near the instrument. A chosen source is drawn as soon
        as there are samples for it, and the samples come from Get
        waveform, which captures every live source from one acquisition.
        Fetching one source on its own would give a trace from a
        different moment than the ones already on the graticule, which
        is exactly what the capture exists to stop.

        So a source chosen with nothing captured for it is still chosen,
        and its row says to press Get waveform. The same goes for an
        empty reference waiting for a file to be loaded into it: this is
        a selection, not only a trace.
        """
        chose = list(state.get("wticked") or ())
        if name in chose:
            state["wticked"] = [n for n in chose if n != name]
            if name in [wave_name(x) for x in shown_waves()]:
                show_waves([x for x in shown_waves() if wave_name(x) != name],
                           keep_view=True)
            else:
                refresh_sources()
            say(_("%s hidden") % name)
            return
        state["wticked"] = wfm_order(chose + [name])
        held = (state.get("staged") or {}).get(name)
        kept = held if held is not None else \
            (state.get("fetched") or {}).get(name)
        if kept is not None:
            show_waves(shown_waves() + [kept], keep_view=True)
            refresh_sources()
            say(_("%s shown") % name)
            return
        refresh_sources()
        say(_("%s has not been captured - press Get waveform") % name)

    def refresh_sources():
        """Redraw both lists from what is known, and re-decide the buttons.

        The rows carry more than the name - which source is chosen, what
        it holds, whether it is waiting to be captured - so anything
        that changes those has to redraw them.
        """
        show_sources({"all": state.get("wall") or [],
                      "sources": state.get("wsources") or []})

    def wfm_pick(*names):
        """Choose these sources, and let go of everything else.

        Everything that picks from code goes through here, so there is
        one place where the buttons, the graticule and the state behind
        them are set together. The graticule follows: a source that is
        chosen and has samples is drawn, and one that is not chosen is
        not - two answers to "which one" is the thing this whole
        arrangement exists to be rid of.
        """
        live = state.get("wlive") or []
        refs = list(state.get("wrefs") or tds_wfm.REFS)
        state["wticked"] = wfm_order([n for n in names
                                      if n in live or n in refs])
        want = state["wticked"]
        got = state.get("fetched") or {}
        staged = state.get("staged") or {}
        show_waves([w for w in (staged.get(n) or got.get(n) for n in want)
                    if w is not None], keep_view=True)
        # What was asked for, not what show_waves worked back out from
        # the traces: they agree, since the traces come from this list,
        # and saying so here keeps a caller's answer the caller's.
        state["wticked"] = want
        show_ticks()
        wfm_buttons()

    def wfm_picked_refs():
        """Which references are chosen, by name rather than by row."""
        refs = list(state.get("wrefs") or tds_wfm.REFS)
        chose = set(state.get("wticked") or ())
        return [n for n in refs if n in chose]

    def wfm_picked_live():
        """Which live channels are chosen, by name rather than by row."""
        live = state.get("wlive") or []
        chose = set(state.get("wticked") or ())
        return [n for n in live if n in chose]

    def wfm_picked_all():
        """Everything chosen, in either list, in the order listed.

        Several can be on the graticule at once, so Get waveform reads
        the lot. Live channels come first because that is the order the
        lists are in.
        """
        return wfm_picked_live() + wfm_picked_refs()

    def wfm_picked_ref():
        """The one reference the reference buttons act on."""
        refs = wfm_picked_refs()
        return refs[0] if refs else None

    def wfm_picked():
        """The one source the single-source buttons act on.

        Save, Send and Delete each mean one waveform, and with several
        chosen that is the first - the same one the readings under the
        plot describe.
        """
        chosen = wfm_picked_all()
        return chosen[0] if chosen else None

    def wfm_send_wave():
        """The waveform Send to instrument would put back.

        The chosen live channel's, so that pressing a channel's button
        and then Send sends that channel. Only when no live channel is
        chosen does it fall back to whatever is at the front of the
        plot, which is what a chosen reference gives.
        """
        got = state.get("fetched") or {}
        staged = state.get("staged") or {}
        for name in wfm_picked_live() + wfm_picked_refs():
            if staged.get(name) is not None:
                return staged[name]
            if got.get(name) is not None:
                return got[name]
        return None

    def wfm_buttons(_evt=None):
        """Enable what the current selection can actually do.

        Delete and Load only mean something for a stored reference: a
        channel and a maths trace are computed live, there is nothing
        stored to remove, and a file loaded into one would have nowhere
        to go. Offering either and refusing afterwards is a button that
        lies about what it does.
        """
        if state.get("busy"):
            # Everything is disabled while the instrument is being
            # talked to, and this must not quietly hand one back.
            return
        waiting = staged_picked()
        chosen_ref = wfm_picked_ref()
        btn_wdel.config(state="normal" if chosen_ref else "disabled")
        # A file is loaded into a reference, so there has to be one
        # chosen before there is anywhere to put it.
        btn_wload.config(state="normal" if chosen_ref else "disabled")
        # Send means two different things and says which: with staged
        # references chosen it sends those, and otherwise it opens the
        # dialog for the chosen source. With nothing chosen that has
        # samples it can do neither.
        if waiting:
            btn_wsend.config(text=_("Send %d to instrument")
                             % len(waiting), state="normal")
        else:
            btn_wsend.config(
                text=_("Send to instrument..."),
                state="normal" if wfm_send_wave() is not None
                else "disabled")

    def wfm_source_state(name):
        """Is this source displayed on the instrument right now?"""
        return name in (state.get("wsources") or [])

    def do_wfm_toggle(name=None):
        """Double-click: turn a source on or off on the instrument.

        A channel that is not displayed cannot be read - the instrument
        answers 2241, "waveform requested is invalid", and there is no
        way to reach the data without bringing it up. So the way to
        download a switched-off channel is to switch it on, and this is
        that.

        The row that was double-clicked, not the chosen one: with the
        row highlight gone there is nothing to say a double-click on the
        fourth row meant the first.
        """
        if state["busy"]:
            return
        name = name or wfm_picked()
        if not name:
            return
        if name in (state.get("staged") or {}):
            messagebox.showinfo(
                _("Not on the instrument yet"),
                _("%s holds a waveform loaded from this computer that has "
                  "not been sent. Press Send to instrument first.") % name)
            return
        want = not wfm_source_state(name)
        busy(True, "wait")
        say(_("Turning %(name)s %(state)s on the instrument ...")
            % {"name": name, "state": _("on") if want else _("off")})
        w.submit("wfm_select",
                 lambda k, n=name, v=want: k.wfm_select(n, v))

    def do_wfm_sources():
        """Ask which sources are displayed. Only those can be read."""
        if state["busy"]:
            return
        busy(True, "wait")
        say(_("Looking for waveforms ..."))
        w.submit("wfm_sources", lambda k: k.wfm_sources())

    def readable(source):
        """Can this source be read at all right now?

        A channel or a maths trace that is not displayed cannot: the
        instrument answers every one of the eleven preamble fields with
        2241 and its twenty-deep event queue overflows, which then
        surfaces at the *next* connection as a page of errors nobody
        caused. A reference is different - it reads whether it is
        displayed or not, because the data is already there.
        """
        return (wfm_source_state(source)
                or str(source).upper().startswith("REF"))

    def live_sources():
        """Every live source the instrument is displaying, in list order.

        Channels and maths, never references. These are what one press
        of Get waveform captures, whatever is chosen: the point of the
        capture is that it is the instrument's whole state at one
        moment, and leaving a channel out of it would make the traces
        that were included no more comparable than before.
        """
        on = state.get("wsources") or []
        return [n for n in (state.get("wlive") or []) if n in on]

    def do_wfm_get(_evt=None):
        """Capture the instrument: every live source, from one acquisition.

        Acquisition is stopped first, every displayed channel and maths
        trace is read off the same frozen capture, and then it is let go
        again - so what arrives is one moment seen on several sources
        rather than several moments read one after another. See
        TdsWfm.capture for what that is worth and what it refuses to do.

        Chosen references come too, read after the acquisition is
        released: they are not moving, so they gain nothing from the
        freeze and there is no reason to hold the instrument still for
        them.

        What is captured and what is *shown* are two different
        questions. This answers the first; the buttons beside the names
        answer the second, and having captured the lot they answer it
        without going near the instrument again.
        """
        if state["busy"]:
            return
        live = live_sources()
        refs = [n for n in wfm_picked_refs()
                if n not in (state.get("staged") or {})]
        if not live and not refs:
            messagebox.showinfo(
                _("Nothing to capture"),
                _("No channel is displayed on the instrument and no "
                  "stored waveform is chosen, so there is nothing to "
                  "read.\n\nDouble-click a name to switch that source on "
                  "at the instrument."))
            return
        # Only what was chosen and could not be had. Naming every
        # switched-off channel on every capture would put a line of
        # apology under the plot for things nobody asked for.
        held = state.get("staged") or {}
        state["wskipped"] = [n for n in wfm_picked_all()
                             if n not in live and n not in refs
                             and n not in held]
        busy(True, "steps" if len(live) + len(refs) > 1 else "wait")
        say(_("Capturing %s ...") % ", ".join(live + refs))
        w.submit("wfm_get",
                 lambda k, a=list(live), b=list(refs): k.wfm_get(a, b))

    def do_err_get():
        """Read the log out, oldest first."""
        if state["busy"]:
            return
        busy(True, "wait")
        say(_("Reading the instrument's error log ..."))
        w.submit("err_entries", lambda k: k.err_entries())

    def show_err_result(payload):
        found = payload["entries"]
        # What arrived, kept so a test can ask whether the pane shows all
        # of it. Not an independent measurement of the instrument - it
        # cannot be - but it does catch the pane quietly showing the
        # first twenty of forty, which is the failure worth catching.
        state["wanted_entries"] = len(found)
        head = ["%s" % (state.get("idn") or ""),
                _("Error log read %s") % time.strftime("%Y-%m-%d %H:%M"),
                ""]
        if not found:
            # Jared's wording, and the right wording: an empty log is
            # the instrument saying nothing is wrong, not a failure.
            show_errors(head + [_("No errors")], 0)
            say(_("No errors in the instrument's log"))
            return
        body = ["%4d  %s" % (i, text) for i, text in enumerate(found, 1)]
        show_errors(head + body + ["", _("End of errors")], len(found))
        say(_("%d entries read in %.1f s") % (len(found), payload["secs"]))

    def do_err_save():
        if not state.get("errtext"):
            messagebox.showinfo(_("Nothing to save"),
                                _("Download the error log first."))
            return
        path = filedialog.asksaveasfilename(
            parent=root, title=_("Save error log"), defaultextension=".txt",
            initialfile=stamped(instrument_name("errorlog"), ".txt"),
            filetypes=[(_("Text file"), "*.txt"),
                       (_("All files"), "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(state["errtext"].replace("\n", os.linesep))
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                     % (_("Could not save"), exc))
            return
        say(_("Saved %s (%s bytes)")
            % (os.path.basename(path),
               format(os.path.getsize(path), ",")))

    def do_err_clear():
        """Empty the instrument's log, after asking.

        Worth asking about properly: this is the only thing in the
        program that destroys something on the instrument which cannot
        be fetched back, and the entries are years of power-on history.
        """
        if state["busy"]:
            return
        if not messagebox.askyesno(
                _("Clear errors"),
                _("Clear the error log on the instrument?\n\n"
                  "This operation cannot be undone.\n"
                  "Download and save it first if it is needed."),
                default="no", parent=root):
            return
        busy(True, "wait")
        say(_("Clearing the instrument's error log ..."))
        w.submit("err_clear", lambda k: k.err_clear())

    def scr_layout_labels():
        """Layout, named by what it does rather than by what Tektronix
        calls it. PORTRAIT is the page's orientation, not the image's -
        it is the one that gives the screen the right way up, at
        640x480. LANDSCAPE turns it on its side. Measured on a 640A,
        which arrived set to LANDSCAPE and produced a 480x640 image
        lying on its side."""
        return [(_("Portrait - screen upright, 640 x 480"), "PORTRAIT"),
                (_("Landscape - screen turned 90 degrees"), "LANDSCAPE")]

    def scr_palette_labels(has_palette=True):
        """What the Palette box offers.

        A colour instrument has HARDCOPY:PALETTE and can be asked for
        either. A monochrome one has no such setting - so rather than
        greying the box, the same choice is offered and the inversion is
        done here, on the palette of the image that comes back. That
        costs one subtraction per palette entry instead of another
        five-second capture.
        """
        if has_palette:
            return [(_("Current - the screen's own colours"), "CURRENT"),
                    (_("Hardcopy - light background, for printing"),
                     "HARDCOPY")]
        # Which way round these go was measured, not assumed. What a
        # monochrome instrument sends is a print job: dark ink on white
        # paper. So the image as it arrives is the printing one, and
        # turning it inside out is what gives the white-on-black the
        # screen actually shows.
        return [(_("Normal - dark on light, for printing"), "NORMAL"),
                (_("Inverted - as the screen shows it"), "INVERT")]

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

    def do_scr_options():
        """Ask the instrument which formats it has and whether it has
        colour. Cheap - it sets each format and reads it back, and
        transfers nothing."""
        if state["busy"]:
            return
        busy(True, "wait")
        say(_("Asking what the instrument can print ..."))
        w.submit("scr_options", lambda k: k.scr_options())

    def scr_format_labels():
        """What the Format box offers, in the current language.

        Built from what the instrument said it has, which is remembered,
        so this can be asked again after a language change without going
        back to the bus.

        Which is quickest in each class was measured on the bench rather
        than reasoned about: the time is almost all the instrument
        drawing the picture, so the smallest file is not the fastest.
        """
        return [(_(f["label"]) + (_("  - quickest")
                                  if f.get("quickest") else ""),
                 f["keyword"]) for f in (state.get("sformats") or [])]

    def show_scr_options(payload):
        formats = payload.get("formats") or []
        state["sformats"] = formats
        state["sfkeys"] = pick_from(scr_fmt, scr_format_labels(),
                                    payload.get("best"))
        settings = payload.get("settings") or {}
        state["slkeys"] = pick_from(scr_lay, scr_layout_labels(), "PORTRAIT")
        has_palette = bool(payload.get("palette"))
        state["spalette"] = has_palette
        state["spkeys"] = pick_from(
            scr_pal, scr_palette_labels(has_palette),
            settings.get("PALETTE", "CURRENT") if has_palette else "NORMAL")
        # Enabled either way now: a monochrome instrument has no
        # HARDCOPY:PALETTE, so the inversion is done here instead.
        scr_pal.config(state="readonly")
        lbl_spal.config(foreground="")
        if not formats:
            say(_("This instrument offers no image format this program "
                  "can read."))
        if not formats:
            unavailable(btn_sget)
        btn_sget.config(state="normal" if formats else "disabled")

    def do_scr_get():
        if state["busy"]:
            return
        if not state.get("sformats"):
            messagebox.showinfo(
                _("No image format"),
                _("This instrument's hardcopy formats are all printer "
                  "languages, none of which is an image this program can "
                  "read."))
            return
        keyword = chosen(scr_fmt, state.get("sfkeys") or [])
        layout = chosen(scr_lay, state.get("slkeys") or [])
        wanted = chosen(scr_pal, state.get("spkeys") or [])
        # CURRENT and HARDCOPY are the instrument's own. NORMAL and
        # INVERT are this program's, for an instrument that has no
        # palette setting at all.
        palette = wanted if wanted in tds_scr.PALETTES else None
        state["sinvert"] = (wanted == "INVERT")
        busy(True, "wait")
        progress(None)
        say(_("Taking a screenshot in %s ...") % (keyword or "?"))
        w.submit("scr_get",
                 lambda k, f=keyword, l=layout, p=palette: k.scr_get(f, l, p))

    def do_scr_save():
        """Save the shot, as the pane is showing it.

        The instrument's own format is offered first, because what
        arrived is a perfectly good .bmp and somebody will want that
        rather than a conversion. Untouched it is written back byte for
        byte; turned or inverted here it is encoded again in the same
        family, since the file has to be the picture that was chosen.
        """
        screen = state.get("screen")
        if not screen:
            messagebox.showinfo(_("Nothing to save"),
                                _("Take a screenshot first."))
            return
        # The format it came back in leads, since that is the file the
        # instrument actually produced and converting it is a choice
        # rather than a default. PNG is offered second. Windows picks
        # the extension from whichever line of the dropdown is chosen,
        # which is the convention here.
        native = screen.suffix
        native_name = {".bmp": _("Windows bitmap (*.bmp)"),
                       ".pcx": _("ZSoft PCX image (*.pcx)"),
                       ".tif": _("TIFF image (*.tif)")}.get(
                           native, _("As received from the instrument"))
        types = [(native_name, "*" + native),
                 (_("PNG image (*.png)"), "*.png")]
        path = filedialog.asksaveasfilename(
            parent=root, title=_("Save screenshot"),
            defaultextension=native,
            initialfile=stamped("screen", native), filetypes=types)
        if not path:
            return
        try:
            # Either way the file is the picture on the screen, turned
            # and inverted as the pane shows it. A shot nothing has been
            # done to still saves as the bytes the instrument sent.
            as_png = path.lower().endswith(".png")
            data = state["shotpng"] if as_png else screen.to_native()
            with open(path, "wb") as fh:
                fh.write(data)
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                     % (_("Could not save"), exc))
            return
        say(_("Saved %s (%s bytes)") % (os.path.basename(path),
                                        format(len(data), ",")))

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

    def do_wfm_save():
        """Save the held trace, in whichever format is asked for.

        The format follows the extension chosen in the dialog, so one
        capture can be saved as data, as an archive, or as a picture
        without fetching it again - which is the point of keeping the raw
        curve rather than a converted copy.

        A `.wfm` is the instrument's own format: copy one to a disk and
        the scope will recall it into a reference, which none of the
        other three will do.
        """
        wave = state.get("wave")
        if not wave:
            messagebox.showinfo(_("Nothing to save"),
                                _("Fetch a waveform first."))
            return
        kinds = [(_("Spreadsheet, scaled values (*.csv)"), "*.csv"),
                 (_("Instrument format, exact (*.isf)"), "*.isf"),
                 (_("Waveform file (*.tdw)"), "*.tdw"),
                 (_("Picture of the trace (*.png)"), "*.png")]
        dest = filedialog.asksaveasfilename(
            title=_("Save waveform"), defaultextension=".csv",
            initialfile=stamped(wave.source or "waveform", ".csv"),
            filetypes=kinds)
        if not dest:
            return
        ext = os.path.splitext(dest)[1].lower()
        try:
            if ext == ".isf":
                data = wave.to_isf()
            elif ext in (".tdw", ".wfm"):
                # The instrument's own layout, under this program's own
                # extension: a .WFM is a file some instrument wrote, and
                # this is not one. Send to instrument... is what puts a
                # real .WFM on the scope's disk. Eight-bit captures are
                # widened on the way out, because the format holds two
                # bytes a sample.
                data = wave.to_wfm()
            elif ext == ".png":
                # What the graticule is showing now, zoom and all, with
                # the record strip above it - the same picture the
                # window has, which is what somebody who has just spent
                # a minute setting up a view means by saving it.
                data = wave.to_png(width=state["pngsize"][0],
                                   height=state["pngsize"][1],
                                   colours=plot_colours(wave),
                                   caption=wave_scales(wave),
                                   view=state.get("view"), strip=True)
            else:
                data = wave.to_csv()
            with open(dest, "wb") as fh:
                fh.write(data)
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                     % (_("Could not save"), exc))
            return
        say(_("Saved %s (%s bytes)")
            % (os.path.basename(dest), format(len(data), ",")))


    def do_wfm_load():
        """Load a waveform from the PC into the pane.

        The same formats this program writes, and the instrument's own
        `.wfm` besides. An `.isf` comes back byte for byte - it carries
        the instrument's own curve and preamble - so what is loaded can
        be sent to an instrument and be exactly what was captured. A
        `.csv` carries scaled values rather than the raw integers, so it
        is re-quantised on the way in; it plots and sends correctly, to
        within one step of the instrument's eight-bit range. A `.wfm`
        carries sixteen-bit samples and their scaling, and comes back
        with the voltages it went out with.

        What is loaded is held against whichever references are chosen
        - several, if several are - and nothing is written to the
        instrument until Send is pressed. That way a set of references
        can be filled in one go, and a file can be looked at without
        committing it to anything. With no reference chosen there is
        nowhere for it to go, which is why the button is greyed until
        there is one.
        """
        if state["busy"]:
            return
        if not wfm_picked_refs():
            messagebox.showinfo(
                _("Select a reference"),
                _("Choose the reference the waveform is to be loaded into, "
                  "using the button beside its name."))
            return
        path = filedialog.askopenfilename(
            parent=root, title=_("Load waveform"),
            filetypes=[(_("Waveform files (*.isf *.csv *.tdw *.wfm)"),
                        "*.isf *.csv *.tdw *.wfm"),
                       (_("Instrument format, exact (*.isf)"), "*.isf"),
                       (_("Spreadsheet, scaled values (*.csv)"), "*.csv"),
                       (_("Waveform file (*.tdw)"), "*.tdw"),
                       (_("The instrument's own file (*.wfm)"), "*.wfm"),
                       (_("All files"), "*.*")])
        if not path:
            return
        try:
            wave = tds_wfm.load(path)
        except Exception as exc:
            messagebox.showerror(
                _("Error"),
                _("%(name)s could not be read as a waveform.\n\n%(why)s")
                % {"name": os.path.basename(path), "why": exc})
            return
        slots = wfm_picked_refs()
        staged = state.setdefault("staged", {})
        for slot in slots:
            # A copy each, because each one is labelled with the
            # reference it is going to and they are going to different
            # ones. The samples are shared; only the label differs.
            held = copy.copy(wave)
            held.label = slot
            held.origin = os.path.basename(path)
            staged[slot] = held
        wave.label = slots[0]
        show_sources({"all": state.get("wall") or [],
                      "sources": state.get("wsources") or []})
        # Beside whatever else is on the graticule, not in place of it:
        # loading a file into a reference is not a reason to take the
        # channel it is going to be compared against off the screen.
        keep = [x for x in shown_waves() if wave_name(x) not in slots]
        show_waves([staged[s] for s in slots] + keep)
        say(_("Loaded %(name)s into %(where)s - not sent yet: %(what)s")
            % {"name": os.path.basename(path), "where": ", ".join(slots),
               "what": wave_summary(wave)})

    def do_wfm_delete():
        """Delete every chosen reference on the instrument, after asking.

        All of them, not the first: the list lets several be picked and
        clearing four references one confirmation at a time is four
        times the same question.

        A reference holding a file that has not been sent yet is a
        different matter: there is nothing on the instrument to delete,
        so it is simply put down again, and the instrument is not
        touched at all. One selection can hold some of each.
        """
        if state["busy"]:
            return
        picked = wfm_picked_refs()
        staged = state.get("staged") or {}
        down = [n for n in picked if n in staged]
        for name in down:
            del staged[name]
            # Only that trace comes off, not the pane: a file put down
            # is no reason to lose the channel it was being compared
            # against.
            forget_source(name)
        if down:
            show_sources({"all": state.get("wall") or [],
                          "sources": state.get("wsources") or []})
            say(_("%s put down - it was never sent, so nothing on the "
                  "instrument changed") % ", ".join(down))
        gone = [n for n in picked
                if n not in down and n.upper().startswith("REF")]
        if not gone:
            if not down:
                messagebox.showinfo(
                    _("Select a reference"),
                    _("Only a stored reference can be deleted. Channels "
                      "and maths are live and there is nothing stored to "
                      "remove."))
            return
        if not messagebox.askyesno(
                _("Delete waveform"),
                _("Delete %s on the instrument?\n\n"
                  "This operation cannot be undone.\n"
                  "Download and save it first if it is needed.")
                % ", ".join(gone),
                default="no", parent=root):
            return
        busy(True, "wait")
        say(_("Deleting %s on the instrument ...") % ", ".join(gone))
        w.submit("wfm_delete", lambda k, ns=list(gone): k.wfm_delete(ns))

    def wfm_destinations():
        """Where this instrument will accept a waveform.

        A reference always; the disk only where the firmware has
        WRITEFILE, which the A and B series do not.

        One file format. A .WFM is what the instrument itself recalls
        from a disk; an .ISF written there is a file the instrument can
        do nothing with, and putting one on its disk to fetch back to a
        PC is a round trip through the slowest link in the room when
        Save waveform writes the same file straight to disk.
        """
        out = [(ref, _("Reference memory %s") % ref)
               for ref in (state.get("wrefs") or tds_wfm.REFS)]
        if (state.get("cannot") is not None
                and by_name["Upload"] not in state["cannot"]
                and state.get("cwd")):
            out.append(("FILE",
                        _("A .WFM on the instrument's disk, which the "
                          "instrument can recall")))
        return out

    def staged_picked():
        """Which staged references the Send button would act on.

        The chosen ones, and only those. Sending everything that
        happens to be staged means a reference filled ten minutes ago
        and forgotten about goes to the instrument along with the one
        that was meant - so what is chosen is what is sent.
        """
        staged = state.get("staged") or {}
        return [n for n in wfm_picked_refs() if n in staged]

    def do_wfm_send_staged():
        """Send the selected staged references to the instrument.

        One worker job for the lot, so the instrument is asked for once
        and each write is read back and compared before the next one
        starts - the same proof a single send gets.
        """
        staged = dict(state.get("staged") or {})
        slots = sorted(staged_picked())
        if not slots:
            return
        if not messagebox.askyesno(
                _("Send to instrument"),
                _("Send these waveforms to the instrument?\n\n%s\n\n"
                  "Whatever those references hold now is replaced.")
                % "\n".join(_("%(ref)s  <-  %(file)s")
                            % {"ref": s,
                               "file": (staged[s].origin
                                        or staged[s].source or "")}
                            for s in slots),
                parent=root, default="no"):
            return
        live = [s for s in state.get("wsources") or []
                if not s.startswith("REF")]
        items = [(s, staged[s]) for s in slots]
        busy(True, "steps")
        say(_("Sending %d waveform(s) to the instrument ...") % len(items))
        w.submit("wfm_send_many",
                 lambda k, it=items, a=(live[0] if live else None):
                 k.wfm_send_many(it, a))

    def do_wfm_send():
        """Put the held trace back into the instrument.

        With files staged against references there is nothing to choose:
        each one goes to the reference it was loaded into, and the only
        question is whether to go ahead. Otherwise it is the chosen
        source that is being sent - the channel whose button is pressed
        in the live list - and where it goes is the question.
        """
        if state["busy"]:
            return
        if staged_picked():
            do_wfm_send_staged()
            return
        wave = wfm_send_wave()
        if not wave:
            messagebox.showinfo(
                _("Nothing to send"),
                _("Choose a source with the button beside its name and "
                  "fetch it first."))
            return
        choices = wfm_destinations()
        dlg = tk.Toplevel(root)
        dlg.title(_("Send to instrument"))
        dlg.transient(root)
        try:
            dlg.iconbitmap(resource("app.ico"))
        except Exception:
            pass
        pad = ttk.Frame(dlg, padding=10)
        pad.pack(fill="both", expand=True)
        ttk.Label(pad, text=_("Send %s to:") % (wave.wfid or wave.source),
                  wraplength=460).pack(anchor="w", pady=(0, 8))
        pick = tk.StringVar(value=choices[0][0])
        for key, label in choices:
            ttk.Radiobutton(pad, text=label, value=key,
                            variable=pick).pack(anchor="w")
        note = ttk.Label(pad, wraplength=460, foreground="#555", text=_(
            "A reference that is empty has to be brought into being from a "
            "live channel first; this is done for you, and overwrites "
            "nothing else."))
        note.pack(fill="x", pady=(8, 0))

        def go():
            key = pick.get()
            dlg.destroy()
            if key == "FILE":
                name = to_83("%s.WFM" % wave.source)
                if not state["cwd"]:
                    return
                try:
                    data = wave.to_wfm()
                except Exception as exc:
                    messagebox.showerror(_("Error"), "%s\n\n%s"
                                             % (_("Could not send"), exc))
                    return
                busy(True, "steps")
                say(_("Sending %s ...") % name)
                w.submit("upload", lambda k, d=data, n=name:
                         k.write_verified(join(state["cwd"], n), d))
                return
            live = [s for s in state.get("wsources") or []
                    if not s.startswith("REF")]
            busy(True, "wait")
            say(_("Sending to %s ...") % key)
            w.submit("wfm_send", lambda k, d=key, a=(live[0] if live else None):
                     k.wfm_send(wave, d, a))

        row = ttk.Frame(pad)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text=_("Send"), command=go).pack(side="right")
        ttk.Button(row, text=_("Cancel"),
                   command=dlg.destroy).pack(side="right", padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (root.winfo_rootx() + 80,
                                 root.winfo_rooty() + 80))
        state["dialog"] = dlg

    def do_delete(_evt=None):
        """Delete whatever is selected - Windows has one Delete, so do we.

        A file selected in the list deletes directly. A folder selected in
        the tree is surveyed first so the confirmation can say how much is
        in it. Both end at a Yes/No box; nothing here asks you to type.
        """
        if state["busy"]:
            return
        # Which pane has focus decides what Delete means, as in Explorer.
        # Without this, a file left selected in the list would be deleted
        # when you meant the folder you just clicked in the tree.
        focused = root.focus_get()
        target_is_tree = focused is tree
        # A folder selected in the file pane is deleted the same way as one
        # selected in the tree: surveyed first, then a confirmation naming
        # what is inside it. Mixing folders and files in one delete is
        # refused rather than half-done.
        if not target_is_tree and sel_folders():
            if sel_files():
                messagebox.showwarning(
                    _("Select one kind at a time"),
                    "The selection contains both folders and files.\n\n"
                    "Delete folders and files separately, so that what is "
                    "about to be removed can be shown properly first.")
                return
            if len(sel_folders()) > 1:
                messagebox.showwarning(
                    _("One folder at a time"),
                    "Folders are removed with everything inside them, so "
                    "they are deleted one at a time and each is shown to "
                    "you first.")
                return
            node = join(state["cwd"], sel_folders()[0])
            why = Worker.protected_reason(node)
            if why:
                messagebox.showwarning(_("Cannot delete"), why)
                return
            busy(True)
            say(_("Checking the contents of %s ...") % node)
            w.submit("survey", lambda k, p=node: k.survey(p))
            return

        names = [] if target_is_tree else sel_files()
        if names:
            paths = [join(state["cwd"], n) for n in names]
            # Every name is checked before any of them is deleted. Removing
            # half a selection and then stopping at a protected file would
            # be the worst of both outcomes.
            refused = [(n, Worker.protected_reason(p))
                       for n, p in zip(names, paths)
                       if Worker.protected_reason(p)]
            if refused:
                messagebox.showwarning(
                    "Cannot delete",
                    refused[0][1] if len(refused) == 1 else
                    "%d of the %d selected items cannot be deleted, so "
                    "nothing was deleted:\n\n%s\n\n%s"
                    % (len(refused), len(names),
                       name_list([n for n, _ in refused]),
                       Worker.RUNTIME_MSG))
                return
            if len(names) == 1:
                msg = (_("Are you sure you want to permanently delete '%s'?\n\n"
                         "There is no Recycle Bin on the instrument.")
                       % names[0])
            else:
                msg = ("Are you sure you want to permanently delete these "
                       "%d files?\n\n%s\n\nThere is no Recycle Bin on the "
                       "instrument." % (len(names), name_list(names)))
            if not messagebox.askyesno(_("Delete File"), msg,
                                       icon="warning", default="no"):
                return
            busy(True)
            if len(names) == 1:
                say(_("Deleting %s ...") % names[0])
                w.submit("delete", lambda k, p=paths[0]: k.delete(p))
            else:
                busy(True, "steps")
                say(_("Deleting %d files ...") % len(names))
                w.submit("deletes", lambda k, p=paths: k.delete_many(p))
            return

        node = tree.focus()
        if not node:
            return
        why = Worker.protected_reason(node)
        if why:
            messagebox.showwarning(_("Cannot delete"), why)
            return
        busy(True)
        say(_("Checking the contents of %s ...") % node)
        w.submit("survey", lambda k, p=node: k.survey(p))

    def confirm_rmdir(path, names):
        """Standard Windows folder-delete confirmation: one Yes/No click."""
        busy(False)
        leaf = path.rstrip("/").rsplit("/", 1)[-1]
        if names:
            msg = (_("Are you sure you want to permanently delete the "
                     "folder '%(name)s' and the %(count)d item(s) it "
                     "contains?\n\n%(items)s\n\nThere is no Recycle Bin "
                     "on the instrument, and this cannot be undone.")
                   % {"name": leaf, "count": len(names),
                      "items": ", ".join(names[:8])
                      + (", ..." if len(names) > 8 else "")})
        else:
            msg = (_("Are you sure you want to permanently delete the empty "
                     "folder '%s'?") % leaf)
        if not messagebox.askyesno(_("Delete Folder"), msg,
                                   icon="warning", default="no"):
            say(_("Delete cancelled"))
            return
        busy(True)
        say(_("Deleting %s ...") % path)
        w.submit("rmdir", lambda k, p=path: k.rmdir(p))

    def do_mkdir():
        """Ask for a folder name, and keep asking until it is a legal one.

        The instrument accepts an illegal name without complaint and then
        does nothing with it, so the name is checked here and the reason is
        shown with the offending characters named. The typed text is put
        back in the box so it can be corrected rather than retyped.
        """
        if state["busy"] or not state["cwd"]:
            return
        name = ""
        while True:
            name = simpledialog.askstring(
                _("New Folder"), _("Name for the new folder:"),
                initialvalue=name)
            if name is None:
                return
            name = name.strip()
            why = check_83(name)
            if why is None:
                break
            messagebox.showwarning(_("Cannot use that name"), why)

        existing = state["cache"].get(state["cwd"])
        if existing:
            taken = [n.upper() for n in existing["dirs"] + existing["files"]]
            if name.upper() in taken:
                messagebox.showwarning(
                    _("Name already in use"),
                    "'%s' already exists in %s. Choose another name."
                    % (name.upper(), state["cwd"]))
                return
        busy(True)
        say(_("Creating %s ...") % name.upper())
        w.submit("mkdir",
                 lambda k, n=name: k.mkdir(join(state["cwd"], n.upper())))

    def retranslate():
        """Re-label everything already on screen in the current language.

        Only the lasting furniture is relabelled here - buttons, headings,
        menus. Status text and dialogs are translated as they are produced,
        so they follow the setting from the next message onwards without
        needing to be tracked.
        """
        for widget, source in labelled:
            widget.config(text=_(source))
        for again in relabel:
            again()
        show_sort_arrow()
        lst.heading("size", text=_("Size"))
        lst.heading("type", text=_("Type"))
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
        show_sources({"all": state.get("wall") or [],
                      "sources": state.get("wsources") or []})
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
        settings = load_settings()
        settings["language"] = code
        save_settings(settings)
        retranslate()
        say("%s: %s" % (_("Language"),
                        i18n._languages.get(code, {}).get("native", code)))

    def do_goto(_evt=None):
        p = ent_path.get().strip()
        if p:
            navigate(p)

    ent_path.bind("<Return>", do_goto)

    buttons = []
    by_name = {}           # so a command the instrument lacks can be greyed
    for text, fn in (("Refresh", do_refresh),
                     ("Download", do_download), ("Upload", do_upload),
                     ("New folder", do_mkdir), ("Delete", do_delete)):
        # No fixed width. A width in characters is really a multiple of the
        # font's average character, which is wrong for German
        # ("Herunterladen" in an 11-wide button is clipped) and wrong the
        # other way for Japanese, whose glyphs are twice as wide as the
        # average. Letting Tk measure the actual string is the only thing
        # that is right in every language.
        b = ttk.Button(tb, text=_(text), padding=(10, 2), command=fn)
        b.pack(side="left", padx=(0, 4))
        buttons.append(b)
        labelled.append((b, text))
        by_name[text] = b
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
                 (btn_wdel, "Delete waveform"), (btn_wscan, "Refresh"),
                 (btn_wwhole, "Whole record")]
    # What each button is for, in a tooltip. Written where the whole set
    # can be seen at once rather than beside each button: they have to
    # read as one voice, and half of them are an arrow or a symbol with
    # no words on them at all.
    for widget, english in (
            (btn_back, "Back to the folder before this one"),
            (btn_fwd, "Forward again"),
            (btn_up, "Up one folder"),
            (btn_scan, "Ask every address on the bus to identify itself"),
            (btn_lang, "Change language"),
            (by_name["Refresh"], "Read this folder from the instrument again"),
            (by_name["Download"], "Copy what is selected to this computer"),
            (by_name["Upload"], "Copy files from this computer to the "
                                "instrument"),
            (by_name["New folder"], "Make a folder here"),
            (by_name["Delete"], "Delete what is selected on the instrument"),
            (btn_wget, "Capture every displayed source, from one "
                       "acquisition"),
            (btn_wsave, "Save the captured trace as data, a picture or an "
                        "instrument file"),
            (btn_wload, "Open a waveform file from this computer"),
            (btn_wsend, "Write the trace into one of the instrument's "
                        "reference memories"),
            (btn_wdel, "Empty the selected reference on the instrument"),
            (btn_wscan, "Ask the instrument which sources it has"),
            (btn_wtout, "Zoom out in time"),
            (btn_wtin, "Zoom in on time"),
            (btn_wvout, "Zoom out in amplitude"),
            (btn_wvin, "Zoom in on amplitude"),
            (btn_wwhole, "Put the whole record back on the graticule"),
            (btn_mnew, "Start an empty mask"),
            (btn_msave, "Save this mask on this computer, under a name you choose"),
            (btn_msetup, "Read the instrument's settings and save them "
                         "beside this mask, the way Tektronix shipped a "
                         "setup with every mask"),
            (btn_mdel, "Delete the selected mask file"),
            (btn_mtrace, "Draw a captured trace under the mask"),
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
            (btn_llearn, "Have the instrument make a template from the "
                         "signal it is showing now, to judge later ones "
                         "against"),
            (btn_lsend, "Write the envelope drawn here into the "
                        "reference, so the test judges the signal "
                        "against it"),
            (btn_lclear, "Take the envelope off the canvas - what the "
                         "instrument is holding is left alone"),
            (btn_lstart, "Judge the signal against the template from now "
                         "on - the instrument stops the moment it leaves"),
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
    buttons += [btn_llearn, btn_lsend, btn_lstart, btn_lrefresh]
    labelled += [(btn_llearn, "Create template"),
                 (btn_lsend, "Use this envelope"),
                 (btn_lclear, "Clear the envelope"),
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

    # Windows keys: F5 refresh, Backspace up, Delete deletes the selection,
    # Enter and double-click save it, ctrl-A selects every file. Delete
    # works from either pane, as it does in Explorer.
    root.bind("<F5>", lambda e: do_refresh())
    def on_files_tab():
        """Is the file tab the one being looked at?

        A key bound on the toplevel fires wherever the pointer is, so
        the tab has to be asked rather than assumed. Both of these do
        something irreversible on the instrument.
        """
        try:
            return tabs.select() == str(filetab)
        except Exception:
            return False

    root.bind("<BackSpace>", lambda e: do_up() if on_files_tab() else None)
    root.bind("<Delete>", lambda e: do_delete(e) if on_files_tab() else None)
    # Explorer's navigation keys, which cost nothing to support.
    root.bind("<Alt-Left>", do_back)
    root.bind("<Alt-Right>", do_forward)
    root.bind("<Alt-Up>", do_up)

    # Right-click context menus, also standard.
    menu_file = tk.Menu(root, tearoff=0)
    menu_file.add_command(label=_("Save as..."), command=do_download)
    menu_file.add_separator()
    menu_file.add_command(label="Delete", command=do_delete)

    # The background menu, for a right-click that hits no row.
    menu_empty = tk.Menu(root, tearoff=0)
    menu_empty.add_command(label=_("New folder..."), command=lambda: do_mkdir())
    menu_empty.add_separator()
    menu_empty.add_command(label=_("Refresh"), command=lambda: do_refresh())
    menu_empty.add_command(label=_("Upload files..."),
                           command=lambda: do_upload())

    menu_dir = tk.Menu(root, tearoff=0)
    menu_dir.add_command(label=_("Open"), command=lambda: navigate(tree.focus()))
    menu_dir.add_command(label=_("Refresh"), command=do_refresh)
    menu_dir.add_separator()
    menu_dir.add_command(label=_("New folder..."), command=do_mkdir)
    menu_dir.add_command(label=_("Delete"), command=do_delete)

    def on_list_click(evt):
        """Clicking the empty area below the rows clears the selection.

        Explorer does this, and without it a file stays selected out of
        sight while Delete and Download go on acting upon it.
        """
        if not lst.identify_row(evt.y):
            lst.selection_remove(*lst.selection())

    lst.bind("<Button-1>", on_list_click, add="+")

    def popup_list(evt):
        row = lst.identify_row(evt.y)
        if not row:
            # Right-click on empty space: clear the selection and offer the
            # things that apply to the folder rather than to a file.
            lst.selection_remove(*lst.selection())
            menu_empty.entryconfigure(
                0, state="normal" if state["cwd"] else "disabled")
            menu_empty.tk_popup(evt.x_root, evt.y_root)
            return
        # Right-clicking inside an existing selection keeps it, as Explorer
        # does; right-clicking elsewhere selects just that row.
        if row not in lst.selection():
            lst.selection_set(row)
        lst.focus(row)
        # Rebuilt each time rather than reconfigured by index: the menu has
        # a different shape for a folder than for a selection of files, and
        # index bookkeeping across the two is a bug waiting to happen.
        n = len(lst.selection())
        folders = sel_folders()
        menu_file.delete(0, "end")
        if folders and not sel_files():
            menu_file.add_command(label=_("Open"), command=do_open)
            menu_file.add_command(label=_("Save as..."), command=do_download)
        else:
            menu_file.add_command(
                label=_("Save as...") if n < 2 else _("Save %d files as...") % n,
                command=do_download)
        menu_file.add_separator()
        menu_file.add_command(
            label=_("Delete") if n < 2 else _("Delete %d items") % n,
            command=do_delete)
        menu_file.tk_popup(evt.x_root, evt.y_root)

    def popup_tree(evt):
        row = tree.identify_row(evt.y)
        if row:
            tree.selection_set(row)
            tree.focus(row)
            menu_dir.tk_popup(evt.x_root, evt.y_root)

    lst.bind("<Button-3>", popup_list)
    tree.bind("<Button-3>", popup_tree)

    # ------------------------------------ results, on the Tk thread only
    def pump():
        if state.get("closing"):
            return                     # window gone; nothing to update
        try:
            while True:
                label, ok, payload = w.out.get_nowait()
                if not ok:
                    busy(False)
                    if label == "connect":
                        # Not an error. The scope being somewhere else, or
                        # switched off, is the ordinary first experience of
                        # anyone whose bus is not the author's, and raising
                        # VISA's own wording at them - "VI_ERROR_RSRC_NFOUND
                        # (-1073807343): Insufficient location information"
                        # - explains nothing they can act on. Say plainly
                        # that nothing answered, then go and look.
                        say(_("Nothing answered at %s - scanning the bus ...")
                            % (state["addr"] or DEFAULT_ADDR))
                        root.title("TDS Toolkit %s - %s"
                                   % (__version__, _("not connected")))
                        do_instrument(reason=(
                            _("Nothing answered at %s.")
                            % (state["addr"] or DEFAULT_ADDR)),
                            quiet=True)
                        continue
                    say(_("%(job)s failed - %(why)s")
                        % {"job": label, "why": payload})
                    if label == "scan" and state.get("scan_failed"):
                        # The picker opens its own scan as it appears. If
                        # that fails there is already a dialog on screen
                        # saying so, and stacking a modal error box on top
                        # of it tells the user the same thing twice.
                        state.pop("scan_into", None)
                        state.pop("scan_failed")(str(payload))
                    else:
                        messagebox.showerror(_("Error"), str(payload))
                    continue
                if label == "msk_behind":
                    busy(False)
                    state.pop("mshotimage", None)
                    # What the instrument counted while the picture was
                    # being taken, which is the verdict in DPO and the
                    # better one anywhere. See msk_verdict.
                    state["mhits"] = payload.get("hits")
                    if payload.get("shot"):
                        state["mshot"] = payload["shot"]
                        state["mwave"] = None
                        draw_mask()
                        say(_("The instrument is in DPO - its screen is "
                              "behind the mask instead of a trace")
                            + msk_say_hits())
                    else:
                        state.pop("mshot", None)
                        state["mwave"] = payload.get("wave")
                        state["mview"] = msk_view_for(state["mwave"])
                        draw_mask()
                        held = state.get("mwave")
                        say(((_("%s is behind the mask")
                              % (held.label or held.source)) if held
                             else _("Nothing to capture"))
                            + msk_say_hits())
                    # Clear screenshot is offered on whether there is
                    # anything behind the mask, so the capture that puts
                    # something there has to say the toolbar is stale.
                    # Without this it stayed greyed with a screenshot on
                    # the canvas until some unrelated click redrew it.
                    msk_buttons()
                    continue
                if label == "connect":
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
                    if payload.get("filesystem") is False:
                        # The earliest firmware has no FILESYSTEM subsystem
                        # at all. Saying so beats an empty window and a
                        # string of errors from commands it never had.
                        busy(False)
                        state["no_filesystem"] = True
                        say(_("This instrument has no filesystem over GPIB"))
                        messagebox.showinfo(
                            _("Nothing to browse"),
                            _("This instrument's firmware has no "
                              "filesystem commands at all - no working "
                              "directory, no directory listing, no free "
                              "space.\n\nThere is nothing for this "
                              "program to show. Firmware v5.0e and later "
                              "added the file transfer commands; the A "
                              "and B series can browse and download but "
                              "not upload."))
                        continue
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
                elif label == "scan":
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
                elif label == "wfm_sources":
                    busy(False)
                    found = payload["sources"]
                    state["wsources"] = found
                    state["wrefs"] = payload.get("refs") or list(tds_wfm.REFS)
                    if payload.get("all"):
                        # Held rather than written. The sources are
                        # re-read after every capture, every send and
                        # every toggle, and a third of a 5000-line log
                        # was once this one line repeated with the same
                        # answer. What matters is which channels were
                        # up when something went wrong, so the latest
                        # answer stands and goes out with a fault if
                        # there is one.
                        log_context("waveform sources",
                                    "instrument reports %s; displayed: %s"
                                    % (", ".join(payload["all"]),
                                       ", ".join(found) or "none"))
                    if payload.get("colours"):
                        state["icolours"] = payload["colours"]
                    then = state.pop("wafter", None)
                    if then and then in (payload.get("sources") or []):
                        # Switched on at the instrument, so choose it -
                        # but do not read it. A source read on its own
                        # comes from a different moment than everything
                        # already on the graticule, which is what the
                        # capture exists to prevent. Its row now says to
                        # press Get waveform.
                        state["wticked"] = wfm_order(
                            list(state.get("wticked") or ()) + [then])
                    show_sources(payload)
                    lim_scan()
                    if found:
                        say(_("%d waveform source(s) on the instrument")
                            % len(found))
                    else:
                        say(_("Nothing is displayed - double-click a "
                              "source to turn it on"))
                elif label == "wfm_select":
                    busy(False)
                    # Asked to turn it on so it could be read, rather
                    # than merely toggled - so read it, now that it can
                    # be. The list refresh comes first because that is
                    # what tells us it really did come on.
                    state["wafter"] = state.pop("wthen", None)
                    do_wfm_sources()
                elif label == "wfm_delete":
                    busy(False)
                    gone = ", ".join(payload["names"])
                    for one in payload["names"]:
                        forget_source(one)
                    say(_("%s has been deleted on the instrument") % gone)
                    do_wfm_sources()
                elif label == "err_available":
                    has_log = bool(payload.get("errlog"))
                    state["errlog"] = has_log
                    if not has_log:
                        # Through state["cannot"], not by disabling the
                        # buttons here: busy(False) re-enables everything
                        # in the button set at the end of the next
                        # operation, and would hand back a Download that
                        # cannot work.
                        unavailable(btn_eget, btn_eclr)
                        show_errors([_("This firmware has no error log.")])
                elif label == "err_entries":
                    busy(False)
                    show_err_result(payload)
                elif label == "err_clear":
                    busy(False)
                    show_errors([_("No errors")], 0)
                    say(_("The instrument's error log has been cleared"))
                elif label == "scr_options":
                    busy(False)
                    show_scr_options(payload)
                    names = [f["keyword"] for f in payload.get("formats") or []]
                    log_context("screen formats",
                                "%s; chosen %s; palette %s"
                                % (", ".join(names) or "none",
                                   payload.get("best"),
                                   payload.get("palette")))
                    if names:
                        say(_("%d image format(s) available; %s will be used")
                            % (len(names), payload.get("best")))
                elif label == "scr_get":
                    busy(False)
                    progress_off()
                    screen = payload["screen"]
                    # The instrument's own picture is kept as it
                    # arrived, so switching to Inverted and back does
                    # not need another capture.
                    state["sraw"] = screen
                    state["ssecs"] = payload["secs"]
                    # What arrived already honours the layout it was
                    # asked for, so no turning is owed on it.
                    state["sturned"] = 0
                    if state.get("sinvert"):
                        screen = screen.inverted()
                    show_shot(screen, payload["secs"])
                    say(_("Screen read in %s: %d x %d in %.1f s")
                        % (screen.keyword, screen.width, screen.height,
                           payload["secs"]))
                elif label == "wfm_get":
                    busy(False)
                    waves = payload["waves"]
                    # Everything captured is kept, whether it is on the
                    # graticule or not: the capture is the instrument's
                    # whole state at one moment, and a source turned on
                    # afterwards should come from that moment too rather
                    # than send us back to the bus for a different one.
                    for one in waves:
                        state.setdefault("fetched", {})[wave_name(one)] = one
                    # Nothing chosen yet, so show what was captured -
                    # otherwise a first press of Get waveform reads the
                    # instrument and appears to do nothing.
                    if not wfm_picked_all():
                        state["wticked"] = wfm_order(
                            [wave_name(x) for x in waves])
                    wfm_pick(*(state.get("wticked") or ()))
                    missed = [s for s, _why in payload["refused"]]
                    missed += list(state.pop("wskipped", None) or [])
                    how = payload.get("how") or {}
                    state["wacq"] = how
                    if how.get("froze"):
                        held = _("acquisition held for the capture, then "
                                 "released")
                    elif how.get("sequence"):
                        held = _("left running - it is set to single "
                                 "sequence and stopping it would re-arm "
                                 "the trigger")
                    else:
                        held = _("the instrument was already stopped")
                    say(_("Captured %(name)s in %(secs).2f s - %(how)s")
                        % {"name": ", ".join(wave_name(x) for x in waves),
                           "secs": payload["secs"], "how": held}
                        + (_("  -  %s could not be read, being switched "
                             "off on the instrument") % ", ".join(missed)
                           if missed else ""))
                elif label == "wfm_send":
                    busy(False)
                    say(_("Sent %d points to %s, read back and verified")
                        % (payload["points"], payload["dest"]))
                    do_wfm_sources()
                elif label == "wfm_send_many":
                    busy(False)
                    sent = payload["sent"]
                    # Put down what landed. A job that failed part way
                    # raises instead of returning, so nothing is put
                    # down and pressing Send again sends the lot -
                    # writing a reference twice costs a second and
                    # leaves it holding the same samples.
                    for out in sent:
                        state.get("staged", {}).pop(out["dest"], None)
                    say(_("Sent %(count)d waveform(s) to %(where)s, read "
                          "back and verified")
                        % {"count": len(sent),
                           "where": ", ".join(o["dest"] for o in sent)})
                    do_wfm_sources()
                elif label == "sys_read":
                    busy(False)
                    sys_show(payload)
                    say(_("Read the instrument's settings"))
                elif label == "sys_send":
                    busy(False)
                    sys_show(payload["now"],
                             clock=not state.pop("syskeepclock", False))
                    refused = payload["refused"]
                    if refused:
                        # Named, with what the instrument said about
                        # each. "Some settings were refused" is not
                        # something anybody can act on.
                        say(_("%(what)s - %(n)d refused")
                            % {"what": state.get("syssaid") or _("Sent"),
                               "n": len(refused)})
                        messagebox.showwarning(
                            _("The instrument refused these"),
                            "\n".join("%s\n    %s" % (line, why)
                                      for line, why in refused))
                    else:
                        say(_("%d setting(s) sent and read back")
                            % payload["sent"])
                elif label == "sys_spc":
                    busy(False)
                    say(_("Signal path compensation passed") if
                        payload["passed"] else
                        _("Signal path compensation returned %s - "
                          "anything but 0 is a failure")
                        % payload["result"])
                    if not payload["passed"]:
                        messagebox.showwarning(
                            _("Compensation did not pass"),
                            _("*CAL? returned %s. Zero means it passed; "
                              "anything else means the instrument could "
                              "not compensate itself, and the self test "
                              "on this tab will say more about why.")
                            % payload["result"])
                elif label == "sys_diag":
                    busy(False)
                    txt_sysdiag.delete("1.0", "end")
                    txt_sysdiag.insert("1.0", payload["log"] or
                                       payload["flag"] or "")
                    if not payload.get("back"):
                        # It warm-boots, so silence means it has not
                        # finished coming back rather than that it
                        # failed. Said as the two different things.
                        say(_("The self test was started, but the "
                              "instrument has not answered since - give "
                              "it a moment and press Read"))
                    else:
                        say(_("Self test %(area)s: %(flag)s")
                            % {"area": payload["area"],
                               "flag": payload["flag"]})
                elif label == "sys_secure":
                    busy(False)
                    if payload["failed"]:
                        say(_("Secure erase failed - the instrument could "
                              "not verify the memory afterwards"))
                    elif payload["passed"]:
                        say(_("Secure erase done - every waveform and "
                              "setup memory is zeroed"))
                    else:
                        # Neither event arrived. Said plainly rather
                        # than assumed either way: this one is not a
                        # thing to be vague about.
                        say(_("Secure erase sent, but the instrument "
                              "raised neither the pass nor the fail "
                              "event - events were %s")
                            % (", ".join(payload["events"]) or _("none")))
                    do_sys_read()
                elif label == "sys_factory":
                    busy(False)
                    say(_("The instrument is back on its factory setup"))
                    do_sys_read()
                elif label == "sys_option":
                    busy(False)
                    say(_("Wrote %(n)d option word(s)%(bad)s - power "
                          "cycle the instrument to see what took")
                        % {"n": len(payload["sent"]),
                           "bad": (_(", %d refused")
                                   % len(payload["refused"]))
                           if payload["refused"] else ""})
                    log_note("system", "option words: sent %s, refused %s"
                             % (payload["sent"], payload["refused"]))
                elif label == "msk_live":
                    msk_show_live(payload["segments"])
                elif label == "set_read":
                    busy(False)
                    msk_setup_edit(payload["fields"])
                elif label == "set_send":
                    msk_setup_landed(payload)
                elif label == "msk_eye":
                    busy(False)
                    state.pop("eyeing", None)
                    where, late = payload["where"], payload.get("delayed")
                    bits = []
                    if payload.get("math"):
                        bits.append("MATH1 = CH1-CH2")   # not words
                    if late:
                        # A clock that was asked for and not found is
                        # said out loud. The delayed sweep is a good
                        # answer to having no clock and a poor one to
                        # having a clock nobody could see.
                        if payload.get("adrift"):
                            # A clock the data is not locked to is the
                            # third case, and the one worth naming: it
                            # was found, it was used, and the eye it
                            # gave walked across the mask.
                            bits.append(
                                _("%(clock)s is not locked to the data, "
                                  "so it was left alone and the sweep "
                                  "delayed %(bits)d bit(s) past the "
                                  "trigger")
                                % {"clock": payload["adrift"],
                                   "bits": Worker.DELAY_BITS})
                        elif payload.get("nameless"):
                            # They said which input the clock was on and
                            # there was no clock at the bit rate on it.
                            # Named, because "no clock found" reads as
                            # this program's failure when it is a probe
                            # on the wrong channel.
                            bits.append(
                                _("nothing at the bit rate on %(clock)s, "
                                  "so the data was triggered on and the "
                                  "sweep delayed %(bits)d bit(s) past it")
                                % {"clock": payload["nameless"],
                                   "bits": Worker.DELAY_BITS})
                        else:
                            bits.append(
                                _("no clock found, so delayed %(bits)d "
                                  "bit(s) past the trigger")
                                if payload.get("wanted") else
                                _("triggered on the data, delayed "
                                  "%(bits)d bit(s) past it"))
                            bits[-1] = bits[-1] % {"bits": Worker.DELAY_BITS}
                        bits.append(_("eye centred") if late["centred"]
                                    else _("not centred - no crossings "
                                           "to measure"))
                    else:
                        bits.append(_("triggered on %(clock)s")
                                    % {"clock": payload["clock"]})
                        bits.append((_("bit centred at %(where).1f%%")
                                     % {"where": where})
                                    if where is not None else
                                    _("not centred - no crossings to "
                                      "measure"))
                    bits.append(_("DPO") if payload["display"] == "INSTAVU"
                                else _("persistence, no DPO with math")
                                if payload.get("math") else
                                _("persistence, no DPO fitted"))
                    if payload["counting"]:
                        bits.append(_("counting hits"))
                    elif payload.get("math"):
                        bits.append(_("judged here, not by the "
                                      "instrument's counter"))
                    say(_("Set up for an eye: %(what)s")
                        % {"what": ", ".join(bits)})
                    # And the same thing held in English and in full. A
                    # bench report of "it does not work" is unanswerable
                    # without knowing which of the three routes was
                    # taken and what the instrument was asked for. The
                    # status line says it and the next press replaces
                    # it; this keeps the last one for a fault to carry.
                    log_context("mask eye", "on %s: %s"
                                % (payload.get("source") or "?",
                                   ", ".join("%s=%s" % (k, payload[k])
                                             for k in sorted(payload))))
                    # A sweep that cannot hold an eye is worth saying
                    # out loud rather than leaving somebody to read a
                    # verdict off it: at 1 us/div a USB full speed mask
                    # counted 116,921,796 hits over 345,839
                    # acquisitions, all of them meaningless.
                    held = payload.get("bits")
                    if held is not None and not 0.4 <= held <= 6.0:
                        messagebox.showwarning(
                            _("The sweep is wrong for this mask"),
                            _("The graticule is holding %(bits).3g bit(s), "
                              "and an eye needs about one. Send the setup "
                              "beside this mask - it names the timebase "
                              "the shapes were drawn against - and start "
                              "the measurement again.")
                            % {"bits": held})
                elif label == "msk_count":
                    busy(False)
                    state.pop("mhits", None)
                    draw_mask()
                    say(_("Counting hits from now, in %(display)s")
                        % {"display": _("DPO")
                           if payload["display"] == "INSTAVU"
                           else _("persistence, no DPO fitted")}
                        if payload["counting"] else
                        _("This instrument has no mask counter to start"))
                    log_context("mask count", "no eye: %s" % (payload,))
                    busy(False)
                    if not payload["made"]:
                        # The instrument's own refusal, word for word.
                        messagebox.showerror(
                            _("Error"),
                            "%s\n\n%s"
                            % (_("The instrument did not write a "
                                 "template."),
                               "\n".join(payload["why"])
                               or _("It reported nothing about why.")))
                        say(_("No template was written"))
                        continue
                    say(_("%(dest)s now holds a template made from "
                          "%(source)s, allowing %(up)g division(s) up "
                          "and down and %(across)g across")
                        % {"dest": payload["dest"],
                           "source": payload["source"],
                           "up": payload["vertical"],
                           "across": payload["horizontal"]})
                    do_lim_refresh()
                elif label == "lim_run":
                    busy(False)
                    if not payload["on"]:
                        messagebox.showerror(
                            _("Error"),
                            "%s\n\n%s"
                            % (_("The instrument would not start the "
                                 "limit test."),
                               "\n".join(payload["why"])
                               or _("It reported nothing about why.")))
                        say(_("The limit test did not start"))
                        lim_buttons()
                        continue
                    state["lrun"] = {"running": True,
                                     "dest": payload["dest"],
                                     "source": payload["source"]}
                    lim_buttons()
                    draw_limits()
                    say(_("Testing %(source)s against %(dest)s - the "
                          "instrument stops if the signal leaves it")
                        % {"source": payload["source"],
                           "dest": payload["dest"]})
                    lim_watch()
                elif label == "lim_send":
                    busy(False)
                    if payload.get("verified"):
                        say(_("%(dest)s now holds the envelope drawn "
                              "here, read back and verified")
                            % {"dest": payload["dest"]})
                    else:
                        # Not called a failure: reading an
                        # envelope back off an instrument is the
                        # part of this route measured least, so
                        # the numbers are shown and the user is
                        # asked to look. See TdsWfm.verify_envelope.
                        say(_("Sent to %(dest)s, but reading it back "
                              "gave something else - sent %(sent)s, "
                              "read %(got)s. Check the instrument's "
                              "screen.")
                            % {"dest": payload["dest"],
                               "sent": ", ".join(str(v) for v
                                                 in payload.get("sent")
                                                 or []),
                               "got": ", ".join(str(v) for v
                                                in payload.get("got")
                                                or []) or _("nothing")})
                    draw_limits()
                elif label == "lim_stop":
                    busy(False)
                    state.pop("lrun", None)
                    lim_buttons()
                    draw_limits()
                    say(_("The limit test is switched off"))
                elif label == "lim_state":
                    held = state.get("lrun")
                    if held and payload["running"] is not None:
                        was, held["running"] = held["running"], \
                            payload["running"]
                        if was and not payload["running"]:
                            # It stopped, and nothing here stopped it.
                            lim_verdict()
                            say(_("FAIL - the instrument stopped, so "
                                  "%(source)s left the template")
                                % {"source": held["source"]})
                elif label == "lim_picture":
                    busy(False)
                    state["lband"] = payload["band"]
                    if payload["wave"] is not None:
                        state["lwave"] = payload["wave"]
                    # Before anything is thinned from it: how far the
                    # slider can usefully go is this band's business.
                    lim_range()
                    # Only what Learn read goes onto the canvas.
                    # Refresh reads the same two things and must
                    # leave the drawing alone, or an adjustment
                    # would be lost every time the trace was
                    # looked at again.
                    if state.pop("llearn", None) and payload["band"]:
                        lim_take_band()
                    lim_buttons()
                    draw_limits()
                    if payload["band"]:
                        say(_("%(dest)s holds a template %(columns)d "
                              "column(s) wide")
                            % {"dest": payload["dest"],
                               "columns": len(payload["band"])})
                    else:
                        say(_("%s holds no limit template to draw")
                            % payload["dest"])
                elif label == "msk_send":
                    busy(False)
                    want, got = payload["wanted"], payload["got"]
                    wrong = [n for n in sorted(want)
                             if got.get(n, 0) != want[n]]
                    if not got:
                        say(_("Sent, but this instrument reports no mask "
                              "subsystem to read back"))
                    elif wrong:
                        # Not a silent success: the instrument kept
                        # something other than what was sent.
                        say(_("Sent, but segment(s) %(which)s came back "
                              "holding %(got)s point(s) rather than "
                              "%(want)s")
                            % {"which": ", ".join(str(n) for n in wrong),
                               "got": ", ".join(str(got.get(n, 0))
                                                for n in wrong),
                               "want": ", ".join(str(want[n])
                                                 for n in wrong)})
                    else:
                        say(_("The instrument is showing the mask - "
                              "%d segment(s), read back and verified")
                            % len([n for n in want if want[n]]))
                    msk_scan_scope()
                elif label == "msk_clear":
                    busy(False)
                    state.pop("msent", None)
                    msk_show_live(payload["segments"])
                    left = sum(payload["segments"] or [])
                    say(_("The instrument's mask segments are empty")
                        if not left else
                        _("Emptied, but %d point(s) are still there")
                        % left)
                elif label == "msk_read":
                    busy(False)
                    msk_took_live(payload["replies"])
                elif label == "lim_build":
                    busy(False)
                    if not payload["made"]:
                        # The instrument's own refusal, word for word.
                        messagebox.showerror(
                            _("Error"),
                            "%s\n\n%s"
                            % (_("The instrument did not write a "
                                 "template."),
                               "\n".join(payload["why"])
                               or _("It reported nothing about why.")))
                        say(_("No template was written"))
                        continue
                    say(_("%(dest)s now holds a template made from "
                          "%(source)s, allowing %(up)g division(s) up "
                          "and down and %(across)g across")
                        % {"dest": payload["dest"],
                           "source": payload["source"],
                           "up": payload["vertical"],
                           "across": payload["horizontal"]})
                    do_lim_refresh()
                elif label == "volumes":
                    vols = payload["volumes"]
                    if not vols:
                        # Nothing to browse: no hard disk fitted and no
                        # floppy in the drive. Saying so beats pretending
                        # hd0: exists and failing to list it.
                        busy(False)
                        say(_("No drives found on this instrument"))
                        messagebox.showinfo(
                            _("No drives found on this instrument"),
                            _("This instrument reports no usable drive.\n\n"
                              "A TDS with no hard disk option has only its "
                              "floppy drive, so put a disk in and press "
                              "Refresh."))
                        continue
                    for v in vols:
                        if not tree.exists(v):
                            tree.insert("", "end", iid=v, text=" " + v,
                                        image=icon_for("drive", True))
                            tree.insert(v, "end", iid=v + "/~", text="")
                    # Always open at the root of the hard disk. The
                    # instrument's own current directory is wherever the
                    # last program to talk to it happened to leave it -
                    # which may be three levels inside an application
                    # folder, or fd0: with no floppy in the drive. Neither
                    # is where a file manager should start.
                    start = "hd0:" if "hd0:" in vols else vols[0]
                    say(_("Volumes: %s") % ", ".join(vols))
                    # The connect-then-probe sequence has finished, so the
                    # busy state it was holding has to be released BEFORE
                    # navigating. navigate() declines to do anything while
                    # busy, so leaving this set left the volumes listed but
                    # the panes empty and every button disabled - which
                    # looks exactly like a failure to connect.
                    busy(False)
                    navigate(start)
                    # Now that the volumes are known, the Masks tab can
                    # say where its library lives on this instrument. The
                    # segments were already asked about at connect.
                elif label == "split":
                    show(payload)
                elif label == "progress":
                    say(payload["text"])
                    progress(payload["frac"])
                elif label == "event":
                    # An event this program did not ask for. Always in the
                    # log and always in the status bar; the dialog only the
                    # first time a given code appears, so a repeating fault
                    # informs rather than buries.
                    say(_("Instrument reported: %s") % payload["detail"])
                    fresh = [c for c in payload["codes"]
                             if c not in state["seen_events"]]
                    state["seen_events"].update(payload["codes"])
                    if fresh:
                        messagebox.showwarning(
                            "The instrument reported something",
                            "While doing '%s' the instrument reported:\n\n%s"
                            "\n\nThis was not expected. It has been written "
                            "to:\n%s\n\nIf anything looks wrong on the "
                            "instrument, stop and say so before deleting "
                            "anything else."
                            % (payload["where"], payload["detail"], LOGFILE))
                elif label == "download":
                    busy(False)
                    with open(state["saveas"], "wb") as fh:
                        fh.write(payload["data"])
                    set_size(os.path.basename(payload["path"]),
                             len(payload["data"]))
                    say(_("Saved %(bytes)s bytes to %(where)s")
                        % {"bytes": format(len(payload["data"]), ","),
                           "where": state["saveas"]})
                elif label == "tree":
                    busy(False)
                    total = sum(n for _, n in payload["done"])
                    say(_("Saved %(files)d file(s) in %(folders)d "
                          "folder(s), %(bytes)s bytes, to %(where)s")
                        % {"files": len(payload["done"]),
                           "folders": payload["folders"],
                           "bytes": format(total, ","),
                           "where": payload["dir"]})
                    report_failures("download", payload["failed"])
                elif label == "downloads":
                    busy(False)
                    for dest, n in payload["done"]:
                        set_size(os.path.basename(dest), n)
                    total = sum(n for _, n in payload["done"])
                    say(_("Saved %(files)d files (%(bytes)s bytes) to "
                          "%(where)s")
                        % {"files": len(payload["done"]),
                           "bytes": format(total, ","),
                           "where": payload["dir"]})
                    report_failures("download", payload["failed"])
                elif label == "deletes":
                    busy(False)
                    say(_("Deleted %d files") % len(payload["done"]))
                    report_failures("delete", payload["failed"])
                    navigate(state["cwd"], force=True)
                elif label == "survey":
                    confirm_rmdir(payload["path"], payload["names"])
                elif label == "rmdir":
                    busy(False)
                    gone = payload["path"]
                    if tree.exists(gone):
                        tree.delete(gone)
                    state["cache"].pop(gone, None)
                    say(_("Removed %s") % gone + mass_storage_note(payload))
                    navigate(parent_of(gone) or state["cwd"], force=True)
                elif label == "uploads":
                    busy(False)
                    for dest, n in payload["done"]:
                        set_size(dest.rsplit("/", 1)[-1], n)
                    total = sum(n for _, n in payload["done"])
                    say(_("Uploaded and verified %(files)d file(s), "
                          "%(bytes)s bytes")
                        % {"files": len(payload["done"]),
                           "bytes": format(total, ",")})
                    report_failures("upload", payload["failed"])
                    navigate(state["cwd"], force=True)
                elif label in ("upload", "delete", "mkdir"):
                    busy(False)
                    # Named in words rather than by the worker's own
                    # label for the job, which is not a sentence in any
                    # language and was never going to be translatable.
                    done = {"upload": _("Uploaded"),
                            "delete": _("Deleted"),
                            "mkdir": _("Folder created")}[label]
                    say("%s: %s%s" % (done, payload,
                                      mass_storage_note(payload)))
                    navigate(state["cwd"], force=True)
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
        """
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
             "undo": "undo", "redo": "redo"}
    try:
        state["micons"] = {k: winicons.tool(tk, v)
                           for k, v in DRAWN.items()}
        for held in ("mtoolbtn", "mboolbtn", "ltoolbtn", "lboolbtn"):
            for key, btn in (state.get(held) or {}).items():
                btn.config(image=state["micons"][key])
        for key, btn in (state.get("undobtn") or {}).items():
            btn.config(image=state["micons"][key[1:]])
    except Exception:                        # words, if the drawing fails
        words = {"pen": "✎", "move": "✥", "eraser": "⌫", "cut": "✂",
                 "union": "∪", "intersect": "∩", "subtract": "∖",
                 "fliph": "↔", "flipv": "↕"}
        for held in ("mtoolbtn", "mboolbtn", "ltoolbtn", "lboolbtn"):
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
        HOOK(locals())

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
    except (TypeError, ValueError):
        return PNG_DEFAULT_SIZE
    return min(PNG_SIZES, key=lambda wh: abs(wh[0] - wide))


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
    except (TypeError, ValueError):
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
    for name in ("tdstoolkit.py", "tds_scr.py", "tds_wfm.py", "tds_fs.py",
                 "winicons.py", "i18n.py"):
        try:
            with open(os.path.join(here, name), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except Exception:
            continue
        read_any = True
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                spoken.add(node.args[0].value)
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
                "Error",
                "%s\n\nThe details were written to:\n%s"
                % (detail.strip().splitlines()[-1], LOGFILE))
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
