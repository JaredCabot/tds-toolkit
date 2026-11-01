"""worker_bak - the Backup tab's jobs.

The calibration constants, making a backup and putting one back.

One of the mixins Worker in worker.py is made of. Everything here runs
on the worker's thread, never on Tk's.
"""
import os
import shutil
import tempfile

import pyvisa
import tds_bak
import tds_cal
import tds_set
import tds_wfm
from i18n import gettext as _
from tdstoolkit import (
    __version__, log_note
)


class BackupJobs(object):
    """Jobs for backups and calibration constants."""

    # ------------------------------------------- calibration constants
    # -- calibration constants -------------------------------------------

    def cal_read(self, base=None, check=True):
        """The acquisition board's calibration words, read twice.

        Read-only. The service unlock is sent only if the first word is
        refused without it: an instrument that answers has nothing to
        unlock, and a command it does not know goes in its event log
        for no reason.
        """
        base = tds_cal.BASE if base is None else base
        # Quietly: this drain throws away whatever happened before the
        # read started, so by definition those events belong to
        # something else and are not this operation's to report. An
        # instrument with no disk in its drive raises 252 whenever
        # anything goes near the filesystem, and the watcher was
        # blaming the next operation to drain the queue - which is how
        # pressing Back up... on a TDS 754D with an empty floppy drive
        # produced "cal_read 252 Missing media" about a read that never
        # touches the filesystem at all.
        self._progress(_("Clearing the bus of anything left over"), 0.0)
        self._quiet_drain()
        # On a short leash, and that is the whole point of it. This one
        # query is asked to find out whether the service unlock is
        # needed, and on an instrument that needs it the answer is
        # silence - so at the session's ordinary timeout the button sat
        # there for the better part of a minute before the first word
        # was read, with nothing on screen to say why. A word that is
        # going to arrive arrives in milliseconds.
        self._progress(_("Asking whether the constants are locked"), 0.0)
        was = self.fs.inst.timeout
        try:
            self.fs.inst.timeout = 3000
            tds_cal.word(self.wfm.q, 0, base)
        except Exception:
            self.fs.inst.timeout = was
            self._progress(_("Sending the service unlock"), 0.0)
            self.fs.inst.write(tds_cal.UNLOCK)
            tds_cal.word(self.wfm.q, 0, base)      # still refused: raise
        finally:
            self.fs.inst.timeout = was
        got = tds_cal.read(self.wfm.q, base, note=self._progress)
        if check:
            self._progress(_("Checking the calibration constants"), 0.0)
            # Reported like the first pass. Both are 252 queries, so a
            # silent second one is half the operation with nothing to
            # watch - which reads as a hang rather than as care.
            if tds_cal.read(self.wfm.q, base,
                            note=lambda t, f: self._progress(
                                _("Checking the calibration constants - "
                                  "word %d of %d") % t.args, f)) != got:
                raise RuntimeError(
                    _("The calibration constants read differently the "
                    "second time. That is the bus, not the instrument. "
                    "Nothing has been written and nothing is lost, but "
                    "the backup cannot be trusted, so this stopped "
                    "here."))
        self._progress(_("Adding the words up to check them against "
                       "word 0"), 1.0)
        held = tds_cal.words(got)
        return {"data": got, "base": base,
                "flat": tds_cal.looks_empty(got),
                # The block's own check word and what the words after it
                # actually add up to, so the report can show the pair
                # the way the NVRAM's sections are shown rather than
                # only saying whether they agreed.
                "check": held[0], "sum": tds_cal.checksum(held),
                # Word 0 is the sum of the rest. A block that disagrees
                # with it is damaged, and saying so at the moment of
                # backup is the difference between finding that out now
                # and finding it out while restoring it.
                "sound": tds_cal.sound(got),
                "events": [str(e) for e in self.fs.errors()]}

    def _cal_store(self):
        """Commit the working copy, and report what the instrument said.

        A query rather than a command, and the answer is the point: 0
        means stored, anything else means it was not. On a TDS 754D
        with the protection switch protected it answers 357 and raises
        no event, so the return value is the only sign there is.

        An instrument that does not know the command at all is a
        different thing from one that refused, and is reported as its
        own case rather than as a failure to store: no such firmware
        has been seen, and guessing which way to call it would be
        guessing about somebody's calibration.
        """
        try:
            said = self.wfm.q("CALIBRATE:STORE?")
        except Exception as exc:
            return {"stored": None, "store_said": str(exc)[:80]}
        digits = tds_cal.NUMBER.findall(said or "")
        if not digits:
            return {"stored": None, "store_said": (said or "")[:80]}
        code = int(digits[-1])
        return {"stored": code == 0, "store_code": code}

    def _cal_put(self, n, value, base):
        """Send one word. *OPC? is the wait, and it may be all the
        answer there is."""
        self.fs.inst.write(tds_cal.setting(n, value, base))
        try:
            self.wfm.q("*OPC?")
        except (IOError, pyvisa.Error):
            pass

    def cal_write(self, plan):
        """Put saved calibration constants back, then store them.

        Two different things, and the difference is the whole of this
        method. `WORDCONSTANT:ATOFFSET` writes into a working copy in
        the instrument's RAM. `CALIBRATE:STORE?` is what commits that
        copy to the EEPROMs on the acquisition board, and it answers 0
        when it did. Tektronix's own field adjustment software ends
        every adjustment that way and treats any other answer as a
        fault; see INSTRUMENT-NOTES, "A calibration word is not stored
        until CALIBRATE:STORE? is sent".

        Measured on a TDS 754D, because it is worth being exact about:
        with the NVRAM protection switch protected, every word was
        written, every word read back as what was sent, the block
        agreed with its own checksum - and a power cycle brought the
        old constants back, because nothing had been stored. Reading
        the words back proves the transfer arrived. It cannot prove
        anything else, because what it reads is the copy just written.

        Every word is written, not only the ones that differ. The point
        is the EEPROM rather than the bus: rewriting a cell refreshes
        the charge holding it, so a wholesale write leaves the whole
        block freshly written instead of leaving the untouched words to
        go on ageing.

        Nothing is read first. What was there is not needed to decide
        what to write, and keeping a copy of it is Back up...'s job.
        """
        base = plan.get("base") or tds_cal.BASE
        want = tds_cal.words(plan["data"])
        self.fs.errors()
        self._progress(_("Sending the service unlock"), 0.0)
        self.fs.inst.write(tds_cal.UNLOCK)
        self.wfm.q("*OPC?")
        report = {"base": base, "wrote": 0, "words": tds_cal.WORDS}
        for n in range(tds_cal.WORDS):
            self._cal_put(n, want[n], base)
            got = tds_cal.word(self.wfm.q, n, base)
            if got != want[n]:
                # The transfer, not the storage: a word that does
                # not read back as what was sent did not even
                # reach the working copy, so there is nothing
                # worth committing and this stops here.
                report["stuck"] = {"word": n, "wanted": want[n],
                                   "got": got}
                break
            report["wrote"] += 1
            if not n % 8:
                self._progress(_("Writing the calibration constants - word "
                               "%d of %d") % (n + 1, tds_cal.WORDS),
                               n / float(tds_cal.WORDS))
        if report["wrote"] == tds_cal.WORDS:
            self._progress(_("Storing them on the acquisition board"), 1.0)
            report["check"] = want[0]
            report["sum"] = tds_cal.checksum(want)
            report.update(self._cal_store())
        report["events"] = [str(e) for e in self.fs.errors()]
        return report



    # --------------------------------------------------- backup and restore
    #
    # tds_bak builds the file and checks it. Everything here is what has
    # to come off the instrument, or go back on to it, first.

    def bak_survey(self):
        """What there is to back up, so the tab offers only that.

        Each reference is asked whether it holds anything. Four bounded
        queries, and the alternative is offering to save four empty
        references and reporting three failures.
        """
        found = self.volumes()
        selection = self.wfm.selection()
        return {"idn": self.fs.idn(), "volumes": found["volumes"],
                "refs": [n for n, _on in selection
                         if n.startswith("REF") and self.wfm.exists(n)]}

    def bak_setup_read(self):
        """*LRN? - every setting the instrument will say, in one string.

        HEADER and VERBOSE have to be on or the reply is values with no
        commands in front of them, which cannot be sent back. Both are
        put back as they were found: this program runs with headers off
        and something else on the bench may not.
        """
        was_head = str(self.wfm.q("HEADER?")).strip()
        was_verb = str(self.wfm.q("VERBOSE?")).strip()
        self.fs.inst.write("HEADER ON;VERBOSE ON")
        try:
            said = self.fs.inst.query("*LRN?")
        finally:
            self.fs.inst.write("VERBOSE %s" % ("ON" if was_verb in ("1", "ON")
                                               else "OFF"))
            self.fs.headers("ON" if was_head in ("1", "ON") else "OFF")
        return {"text": said.strip()}

    def bak_make(self, plan):
        """Gather what was asked for and write it into one zip.

        Collected into a temporary folder and zipped at the end, because
        download_tree already writes a folder tree to disk and a second
        way of walking the instrument would be a second thing to get
        wrong.

        Every part is attempted on its own. A floppy with no disk in it
        should cost that part and not the whole backup.
        """
        want = plan.get("parts") or {}
        work = tempfile.mkdtemp(prefix="tdsbak")
        report = {"path": plan["path"], "failed": []}
        about = {"app": __version__, "idn": self.fs.idn(),
                 "addr": self.addr}
        try:
            # Everything the System tab reads, less the identity, which
            # is already here. Measured on a 784D: *LRN? carries none of
            # the hardcopy port, format or layout, none of the RS-232
            # settings and neither the date nor the time - so a backup
            # that only held *LRN? would not record them anywhere.
            #
            # Under one key rather than spread across the manifest: the
            # hardcopy setting is called "format" and so is the bundle's
            # own version number, and the bundle's won.
            about["system"] = {key: value for key, value
                               in self.sys_read().items() if key != "idn"}

            def part(name, job):
                if not want.get(name):
                    return
                try:
                    job()
                except Exception as exc:
                    log_note("bak_make", "%s: %s" % (name, exc))
                    report["failed"].append((name, str(exc)))

            def keep(name, data):
                dest = os.path.join(work, *name.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(data)

            def volume(drive, as_name):
                got = self.download_tree(drive, work, as_name)
                if got["failed"]:
                    report["failed"].append(
                        (as_name, "%d file(s) would not read"
                         % len(got["failed"])))

            part("disk", lambda: volume("hd0:", tds_bak.DISK))
            part("floppy", lambda: volume("fd0:", tds_bak.FLOPPY))
            part("setup", lambda: keep(
                tds_bak.SETUP,
                self.bak_setup_read()["text"].encode("ascii", "replace")))
            part("refs", lambda: self._bak_refs(plan.get("refs") or [], keep))
            part("cal", lambda: keep(tds_bak.CAL, self.cal_read()["data"]))
            self._progress(_("Writing the backup file"), None)
            manifest = tds_bak.pack(work, plan["path"], about)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        report["manifest"] = manifest
        report["holds"] = tds_bak.holds(manifest)
        report["bytes"] = os.path.getsize(plan["path"])
        return report

    def _bak_refs(self, names, keep):
        """The stored references, each as the .wfm this program writes.

        Read 16 bits wide. A reference saved from an averaged channel
        holds a real low byte: measured on a 784D, REF4 saved from CH2 at
        64 averages had 2,497 of 2,500 low bytes non-zero, all 256 values
        in use. Read 8 bits wide, a restore put back only the top half.
        """
        if not names:
            raise IOError(_("no reference holds a waveform"))
        waves, refused, _how = self.wfm.capture(
            [], list(names), width=2,
            note=lambda name, done: self._progress(_("Reading %s") % name, done))
        for wave in waves:
            keep("%s/%s.wfm" % (tds_bak.REFS, wave.source), wave.to_wfm())
        if refused:
            raise IOError("; ".join("%s: %s" % (n, why) for n, why in refused))

    def bak_put(self, plan):
        """Put parts of a backup back on the instrument.

        Calibration is deliberately not one of them. It needs the NVRAM
        protection switch moved and a confirmation of its own, and
        folding it into a bulk restore is how somebody restores it by
        accident.
        """
        want = plan.get("parts") or {}
        work = tempfile.mkdtemp(prefix="tdsput")
        report = {"path": plan["path"], "done": [], "failed": []}
        try:
            def part(name, job):
                if not want.get(name):
                    return
                try:
                    report["done"].append((name, job()))
                except Exception as exc:
                    log_note("bak_put", "%s: %s" % (name, exc))
                    report["failed"].append((name, str(exc)))

            def volume(where, drive):
                tds_bak.unpack(plan["path"], work, where + "/")
                got = self.upload_tree(os.path.join(work, where), drive)
                # A refusal is reported in its own keys, with nothing in
                # "failed" - and was reported as "0 file(s) restored".
                if got.get("blocked") or got.get("unhealthy"):
                    raise IOError(got.get("blocked") or got["unhealthy"])
                if got["failed"]:
                    raise IOError(_("%d file(s) would not write")
                                  % len(got["failed"]))
                return "%d file(s)" % len(got["done"])

            def setup():
                tds_bak.unpack(plan["path"], work, tds_bak.SETUP)
                # Through the one reader, so a setup out of a backup is
                # decoded exactly as one beside a mask is.
                lines = tds_bak.setup_lines(
                    tds_set.contents(os.path.join(work, tds_bak.SETUP)))
                said = self.set_send(lines)
                if said["refused"]:
                    raise IOError(_("%d of %d command(s) refused: %s")
                                  % (len(said["refused"]), len(lines),
                                     "; ".join(said["refused"][:3])))
                return "%d command(s)" % len(lines)

            def refs():
                got = tds_bak.unpack(plan["path"], work, tds_bak.REFS + "/")
                items = []
                for _arc, path in got:
                    wave = tds_wfm.load(path)
                    items.append((os.path.splitext(os.path.basename(path))[0]
                                  .upper(), wave))
                if not items:
                    raise IOError(_("the backup holds no references"))
                # A reference that holds nothing cannot be written to:
                # every field describing the waveform is refused and the
                # curve is truncated, silently. One live channel brings
                # it into being first - see TdsWfm.send_to_ref.
                #
                # Which matters here more than anywhere else. The
                # Waveforms tab sends into references that usually hold
                # something already; a restore is run precisely when
                # they are empty, so the one path that always needs the
                # allocation was the one path not asking for it. It
                # failed on a 784D with "REF1 is empty, and an empty
                # reference cannot be written to".
                live = [n for n, on in self.wfm.selection()
                        if on and not n.startswith("REF")]
                if not live:
                    raise IOError(
                        _("no channel is displayed on the instrument. An "
                        "empty reference can only be created from a "
                        "live channel, so switch one on and try again."))
                self.wfm_send_many(items, live[0])
                return "%d reference(s)" % len(items)

            part("disk", lambda: volume(tds_bak.DISK, "hd0:"))
            part("floppy", lambda: volume(tds_bak.FLOPPY, "fd0:"))
            part("setup", setup)
            part("refs", refs)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return report
