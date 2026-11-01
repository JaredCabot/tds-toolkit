"""worker - the one thread every instrument job runs on.

Worker is the thread, the connection, the scan and the job queue, plus the
jobs of each tab, which live beside it in worker_*.py as mixins. The UI
submits (label, callable) and reads (label, ok, payload) back off `out`.
"""
import queue
import re
import threading
import traceback

import pyvisa
import tds_err
import tds_scr
import tds_set
import tds_wfm
from tds_fs import TdsFs, DEFAULT_ADDR
from i18n import gettext as _
from tdstoolkit import (
    BAD_FILENAME, DELETE_OPS, EXPECTED_EVENTS, MASS_STORAGE, NO_MEDIA,
    NO_MEDIA_OPS, PROBE_OPS, UNDEFINED_HEADER, describe_visa_error,
    exc_words, known_instrument, localise, log_context, log_note,
    looks_like_scope, take_process_lock
)
from worker_bak import BackupJobs
from worker_files import FileJobs
from worker_fw import FirmwareJobs
from worker_msk import MaskJobs
from worker_sys import SystemJobs
from worker_wfm import WaveformJobs


class Worker(FileJobs, WaveformJobs, MaskJobs, SystemJobs, BackupJobs,
             FirmwareJobs):
    """Serialises every instrument operation onto one thread.

    COUNT_FOR is how long the instrument is left counting against a
    mask before the tally is read, where reading the trace would
    otherwise stop it. A second is a few hundred acquisitions on a
    784D; in DPO no wait is needed, since the hardcopy itself takes
    several.

    Jobs are (label, callable). Results come back as (label, ok, payload) on
    an output queue the UI polls; nothing is called back on this thread.
    """

    # -- the thread itself --------------------------------------------------

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
        # Whether FILESYSTEM:OVERWRITE ON was understood. Until a
        # connection says otherwise, assume not and delete before
        # writing, which is the behaviour that works everywhere.
        self.can_overwrite = False
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
        because a label is a display name and should not carry meaning.

        Giving up on one job is not giving up on the next, so the
        flag is cleared here: it is the one place every job passes
        through. Cleared as the job is queued rather than as it is
        taken up, so a cancel pressed after this cannot be
        swallowed by the clear."""
        self.cancelled.clear()
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
                    raise RuntimeError(_("not connected"))
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
                              exc_words(exc) or type(exc).__name__))

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
        if code == NO_MEDIA and here in NO_MEDIA_OPS:
            return True
        return code == BAD_FILENAME and here in PROBE_OPS

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
        # One program on the bus at a time. Checked before the session is
        # opened, so a second copy is turned away rather than left to
        # corrupt the first one's transfer. A reconnect by this same
        # process already holds the lock and passes straight through.
        wrong = take_process_lock(addr or DEFAULT_ADDR)
        if wrong:
            raise RuntimeError(wrong)
        # Any previous session is closed first. Leaving it open would hold
        # the old instrument's VISA lock, which is exactly what stops a
        # second attempt from working after a failed one.
        if self.fs is not None:
            self.fs.close()
            self.fs = None
        # Per instrument, not per process: one timeout on the counter
        # query used to switch mask counting off for every scope after.
        self._counts = None
        self.fs = TdsFs(addr or DEFAULT_ADDR)
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
        # Cleared before it is asked anything. An instrument left
        # part way through a transfer - by this program being killed, or
        # by anything else that walked away from a read - still has the
        # rest of that file to hand over, and it hands it over before it
        # answers anything new. Measured on a TDS 754D: with 640 kB
        # still queued, *IDN? never came back and connect reported an
        # empty address, on an instrument sitting there working
        # perfectly. The clear used to come thirty lines below this,
        # which is after the question it protects.
        self.fs.clear()
        try:
            told = self.fs.hello()
            # Everything after the hello inside the same guard (A9). One
            # of the later steps raising - the drain, idn(), the transfer
            # probe, the cwd - used to leave the session half set up:
            # self.fs present, capabilities never settled, and jobs that
            # need the filesystem let through against an instrument the
            # window had been told was not connected.
            return self._settle(told, addr)
        except Exception:
            try:
                self.fs.close()
            except Exception:
                pass
            self.fs = self.wfm = self.scr = self.err = self.set = None
            raise

    def _settle(self, told, addr):
        """The rest of connect, once the instrument has said hello."""
        # One VISA session serves both subsystems; the waveform side
        # borrows the filesystem side's header-stripping rule so there is
        # only one place that knows about HEADER ON.
        self.wfm = tds_wfm.TdsWfm(self.fs.inst, TdsFs.payload)
        self.scr = tds_scr.TdsScr(self.fs.inst, TdsFs.payload)
        self.err = tds_err.TdsErr(self.fs.inst, TdsFs.payload)
        self.set = tds_set.TdsSet(self.fs.inst)
        self.addr = addr or DEFAULT_ADDR
        # What the upload marks are kept under: the address and the
        # instrument's own word on what it is, so a different scope put
        # on the same address does not inherit the last one's marks and
        # have a file skipped that was never compared against it.
        self.ident = "%s|%s" % (self.addr, str(told or "").strip())
        self._watch_events()
        # "Is there a waveform in this source?" is answered, for an
        # empty one, with 2241 and a 420 in the event queue. That is the
        # answer, so it is cleared where it is provoked rather than
        # surfacing later as a fault nobody caused. Quietly - the
        # watcher would report the very events the question exists to
        # provoke. See TdsWfm.exists.
        self.wfm.drain = self._quiet_drain
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
        # Whether that was understood decides how an upload replaces a
        # file, and it matters more than it looks. With OVERWRITE ON a
        # write lands straight on top of the old file; without it, the
        # old file has to be deleted first - and DELETE is what exhausts
        # this family's filesystem. Measured on a TDS 784D: 21
        # delete-then-write cycles took mass storage down and wanted a
        # power cycle, where 120 writes of distinct files with no delete
        # and 60 replacements over one name raised nothing whatsoever.
        self.can_overwrite = UNDEFINED_HEADER not in self.fs.errors()
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
        out = {"idn": idn, "cwd": self.fs.get_cwd(), "addr": self.addr,
               "options": opts, "masks": "2C" in opts.upper()}
        out.update(can)
        # Fix 10: a run that wedged this instrument's file system locks it
        # out of further writes (see upload_plan). This fresh session is
        # where a recovered instrument is let back in - but only if it is
        # really well again.
        self._clear_lockout_if_healthy(out["cwd"])
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
        rm = pyvisa.ResourceManager()
        try:
            addresses = list(rm.list_resources())
        except Exception as exc:
            raise RuntimeError(
                _("VISA could not list the bus: %s\n\nCheck that a VISA "
                "runtime and your GPIB driver are installed.") % exc)
        # Swept in numerical order, not the order VISA happened to list
        # them in: each address is split on its runs of digits and those
        # are compared as numbers, so the sweep climbs GPIB0::1, ::2 ...
        # ::17 up the bus rather than ::1, ::17, ::2. A known address is
        # connected to directly and never reaches a scan at all.
        addresses.sort(key=lambda res: [int(t) if t.isdigit() else t
                                        for t in re.split(r"(\d+)", res)])

        found, current = [], getattr(self, "addr", None)
        for i, res in enumerate(addresses, 1):
            if self.cancelled.is_set():
                return {"found": found, "cancelled": True,
                        "reached": i - 1, "total": len(addresses)}
            self._progress(_("Identifying %s  (%d of %d)")
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
                    idn = self.fs.hello()
                except Exception as exc:
                    note = describe_visa_error(exc)
            else:
                inst = None
                try:
                    # Short, because these two decide how long Cancel
                    # takes to be noticed: the flag is only looked at
                    # between addresses, so a scan cannot be stopped
                    # part way through one. Worst case was 3.5 seconds
                    # of silence per dead address and is now two. A
                    # scope that is switched on answers *IDN? in
                    # milliseconds, so this is still three orders of
                    # magnitude of headroom.
                    inst = rm.open_resource(res, open_timeout=800)
                    inst.timeout = 1200
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
                return _("There is no disk in the drive.")
            if UNDEFINED_HEADER in codes:
                self.no_transfers = command
                return (_("This instrument's firmware has no "
                        "FILESYSTEM:%s command, so file contents cannot "
                        "be transferred over GPIB. Browsing, creating "
                        "folders and deleting still work.") % command)
            said = "; ".join("%d %s" % (c, txt) for c, txt in msgs)
        except Exception:
            pass
        return (_("%s gave no answer after %.0f s (%s).%s")
                % (path, secs, type(exc).__name__,
                   (_("  The instrument said: ") + said) if said else ""))

    #: Where a waveform sent to the instrument's disk is put. At the
    #: root of the drive rather than in whichever folder the Files tab
    #: happens to be showing: the instrument recalls a waveform from
    #: wherever it is pointed, and one known place is easier to find
    #: again than wherever somebody was last browsing.
    WFM_DIR = "WAVEFORM"

    # -- batches ----------------------------------------------------------
    #
    # A multiple selection runs as ONE job rather than as a queue of jobs
    # driven from the UI. The bus is single-threaded and every operation
    # here moves the current directory, so interleaving them would be a way
    # to end up somewhere unexpected. Progress is reported as it goes; a
    # failure on one item is recorded and the rest carry on, as Explorer
    # does when one file of a selection is in use.

    def _progress(self, text, frac=None):
        """Tell the UI where we are. `frac` is 0..1, or None for unknown.

        Carries the job it belongs to. A progress line is labelled
        "progress" and not with the work that raised it, so without
        this the only way to tell whose it was is a flag the UI sets
        when it starts something - and a flag can be left standing.
        """
        text = localise(text)
        self.out.put(("progress", True, {"text": text, "frac": frac,
                                         "job": self.context}))
