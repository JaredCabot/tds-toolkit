"""worker_fw - the Firmware tab's jobs, run against the bootloader.

The probe, the image catalogue, NVRAM and firmware backups, the NVRAM
restore and the flash write.

One of the mixins Worker in worker.py is made of. Everything here runs
on the worker's thread, never on Tk's.
"""
import hashlib
import os

import pyvisa
import tds_fw
from i18n import gettext as _
from tdstoolkit import describe_visa_error, human_bytes, localise


class FirmwareJobs(object):
    """Jobs for the bootloader, NVRAM and flash."""

    # ------------------------------------------------------------ firmware
    #
    # An instrument started with its NVRAM protection switch unprotected
    # comes up in the ROM monitor rather than in its firmware, at its own
    # GPIB address, answering no *IDN?. So none of the rest of this class
    # applies: there is no TdsFs, no SCPI, and no identification to be had.
    # These three open their own session, do their work and close it.

    def fw_session(self, resource, normal=""):
        self._progress(_("Opening the bootloader monitor at %s")
                       % resource, None)
        try:
            inst = pyvisa.ResourceManager().open_resource(resource,
                                                          open_timeout=5000)
        except Exception as exc:
            raise RuntimeError(self.fw_absent(resource, normal, exc))
        try:
            inst.clear()
        except Exception:
            pass                       # not every driver offers a clear
        return inst

    def fw_absent(self, resource, normal, exc):
        """Why the monitor is not there, said as something to do.

        The commonest reason by a distance is that the switch was not
        moved, and there is a way to tell: an instrument running its
        firmware answers *IDN? at its ordinary address, and one sitting
        in its bootloader answers nothing anywhere but 29. So the
        ordinary address is asked before anything is said, and the
        answer decides which of the two this is. Reporting
        VI_ERROR_RSRC_NFOUND instead leaves the user to work that out.
        """
        # Only two VISA errors mean nothing is there: the address is
        # not in its list, or nothing answered at it. Anything else -
        # the resource held by another program, an interface that is
        # not controller in charge - is a different fault, and telling
        # someone to go and move a switch over it sends them to the
        # bench for nothing.
        vanished = ("VI_ERROR_RSRC_NFOUND" in "%s" % exc
                    or "VI_ERROR_TMO" in "%s" % exc)
        told = ""
        if normal and vanished:
            try:
                if self.fs is not None:
                    # hello, not idn: a quick question gets the quick
                    # timeout, not the 45 s a transfer needs (A7).
                    told = self.fs.hello()
                else:
                    one = pyvisa.ResourceManager().open_resource(
                        normal, open_timeout=1500)
                    one.timeout = 2000
                    try:
                        told = (one.query("*IDN?") or "").strip()
                    finally:
                        one.close()
            except Exception:
                told = ""
        if told:
            return (_("The instrument is running its firmware rather than "
                    "its bootloader: it answered at %s as %s.\n\n"
                    "Switch it off, move the NVRAM protection switch to "
                    "unprotected, and switch it on again. In the "
                    "bootloader the screen stays dark, every front-panel "
                    "light stays on, and it answers at %s instead.")
                    % (normal, told, resource))
        return (_("Nothing answered at %s - %s.%s\n\n"
                "Check the instrument is switched on and its GPIB cable "
                "is connected. It reaches its bootloader by being "
                "switched on with the NVRAM protection switch "
                "unprotected; the screen then stays dark and every "
                "front-panel light stays on.")
                % (resource, describe_visa_error(exc),
                   " Nothing answered at %s either." % normal
                   if normal and vanished else ""))

    def fw_probe(self, resource, normal=""):
        """Is the monitor there, and what flash is behind it."""
        inst = self.fw_session(resource, normal)
        try:
            mon = tds_fw.Monitor(inst)
            try:
                head = mon.awake()
            except Exception as exc:
                # The address existed but nothing spoke the protocol,
                # which is the same story to tell as nothing being there.
                raise RuntimeError(self.fw_absent(resource, normal, exc))
            flash = tds_fw.Flash(mon)
            # Named rather than left to the order a dict literal happens
            # to evaluate in: span reads the array, so the part has to
            # have been identified and put back to reading it first.
            name = flash.identify()
            return {"flash": name, "paged": flash.paged,
                    "span": flash.span(), "fitted": flash.fitted(),
                    "head": head, "resource": resource}
        finally:
            try:
                inst.close()
            except Exception:
                pass

    def fw_catalogue(self, folder):
        """Every distinct image in the folder.

        Reads a quarter of a gigabyte.
        """
        return {"images": tds_fw.catalogue(folder, note=self._progress),
                "folder": folder}

    def fw_keep(self, flash, what, base, length, plan, report):
        """Read one region, check it, and write it out as a .bin.

        Read twice and compared when the plan asks for it, because the
        only thing a backup has to be is what was there, and the bus is
        the part that can be wrong.
        """
        self._progress(_("Reading the existing %s ...") % _(what), 0.0)
        got = flash.read(length, base=base,
                         note=lambda t, f, w=what: self._progress(
                             _("Backing up %s - %s") % (_(w), localise(t)), f))
        if plan.get("check_backup"):
            self._progress(_("Reading the %s back to check it") % _(what), 0.0)
            again = flash.read(length, base=base,
                               note=lambda t, f, w=what: self._progress(
                                   _("Checking the %s backup - %s") % (_(w), localise(t)), f))
            # Where the two reads differ, and which of those differences
            # are the clock rather than the bus.
            #
            # This used to allow any handful of moved bytes anywhere,
            # which is a threshold standing in for a fact. The fact is
            # in the DS1486 datasheet: its timekeeping registers are the
            # FIRST FOURTEEN BYTES of the part, and on a TDS680B that
            # part sits at the base of the window - the sixteen bytes
            # read there decode as hundredths, seconds, minutes, hours,
            # day, date, month, year and the command register, and they
            # gave the right date and time to the second. So those
            # fourteen bytes are expected to move and every other byte
            # is not.
            #
            # No break aimed at the length: nothing the simulator can do
            # makes the monitor hand back a short read, so a break there
            # would only prove that the line exists. It stays because
            # zip() stops at the shorter of the two, so without it a
            # truncated second read would compare its own length and
            # pass.
            if len(again) != len(got):
                raise RuntimeError(
                    _("The %s read a different length the second time. "
                    "That is the bus, not the instrument - nothing has "
                    "been written and nothing is lost, but the backup "
                    "cannot be trusted, so this stopped here.") % _(what))
            moved = [] if got == again else [
                i for i, (a, b) in enumerate(zip(bytearray(got),
                                                 bytearray(again)))
                if a != b]
            ticking = [i for i in moved
                       if base == tds_fw.NVRAM_BASE and i < tds_fw.RTC_LEN]
            rest = [i for i in moved if i not in set(ticking)]
            if rest:
                raise RuntimeError(
                    _("The %s read differently the second time, at %d "
                    "place(s) outside the clock, the first at 0x%X. That "
                    "is the bus, not the instrument - nothing has been "
                    "written and nothing is lost, but the backup cannot "
                    "be trusted, so this stopped here.")
                    % (_(what), len(rest), rest[0]))
            if ticking:
                # Not a fault and not tolerated noise: the instrument
                # keeping time while it is read. A backup of a running
                # clock is a snapshot of something live and cannot be
                # otherwise.
                report.setdefault("ticked", []).append(
                    {"what": what, "bytes": len(ticking)})
        # A name the plan chose for this region, if it chose one. The
        # NVRAM backup is asked for through a save dialog, and handing
        # back a file under a different name than the one just typed is
        # a small betrayal. Anything not named keeps the derived name.
        path = os.path.join(
            plan["backup_dir"],
            (plan.get("names") or {}).get(what) or "%s_%s_%s.bin" % (
                plan.get("model") or "TDS", what, plan["stamp"]))
        with open(path, "wb") as fh:
            fh.write(got)
        report.setdefault("backups", []).append(
            {"what": what, "path": path, "bytes": len(got),
             "sha": hashlib.sha256(got).hexdigest()})
        return report

    def fw_nvram(self, plan):
        """The NVRAM alone, for its own sake rather than before a write.

        The same monitor and the same reader the firmware job uses. It
        is here and not on the Backup tab's tick list because the
        instrument cannot be in two states at once: everything else that
        tab collects is read from an instrument running its firmware,
        and this is read from one sitting in its bootloader with the
        protection switch moved.
        """
        report = {"backups": []}
        inst = self.fw_session(plan["resource"], plan.get("normal") or "")
        try:
            flash = tds_fw.Flash(tds_fw.Monitor(inst))
            self.fw_keep(flash, "NVRAM", tds_fw.NVRAM_BASE,
                         tds_fw.NVRAM_LEN, plan, report)
        finally:
            try:
                inst.close()
            except Exception:
                pass
        return report

    def fw_backup(self, plan):
        """Both the NVRAM and the firmware, for their own sake - the backup
        half of fw_run with nothing written. No image is read, and the flash
        is never erased or programmed, so this is safe to run whenever the
        monitor is up. The same reader and the same second-read check as the
        backup a write takes on its way past, through the one fw_keep - so a
        backup kept for its own sake is the same bytes and the same proof.
        """
        report = {"backups": []}
        inst = self.fw_session(plan["resource"], plan.get("normal") or "")
        try:
            flash = tds_fw.Flash(tds_fw.Monitor(inst))
            report["flash"] = flash.identify()
            for what, base, length in (
                    ("NVRAM", tds_fw.NVRAM_BASE, tds_fw.NVRAM_LEN),
                    ("Firmware", tds_fw.FLASH_BASE, plan["backup_len"])):
                self.fw_keep(flash, what, base, length, plan, report)
        finally:
            try:
                inst.close()
            except Exception:
                pass
        return report

    def fw_nvram_put(self, plan):
        """Write a saved NVRAM back.

        Nothing is kept first. The Back up... button beside Restore...
        does exactly that and does it better - it names the file and
        checks the read - so taking a second copy here by a second route
        was a minute of every restore spent on a duplicate. Whoever
        presses Restore is expected to have one, and the confirmation
        says so before anything is written.

        The clock is not written. `write_span` skips it - see there.

        The bytes come in the plan, the ones the window read, sized and
        checksummed before it asked (C13/F6). Reading the file again here
        wrote whatever was in it by then, unchecked. `path` alone, with
        no `data`, still reads the file - for the report and the tests.
        """
        data = plan.get("data")
        if data is None:
            with open(plan["path"], "rb") as fh:
                data = fh.read()
        report = {"path": plan["path"], "wrote": 0, "backups": []}
        inst = self.fw_session(plan["resource"], plan.get("normal") or "")
        try:
            flash = tds_fw.Flash(tds_fw.Monitor(inst))
            # No undo taken here any more. It used to read the whole
            # megabyte back first, which is a minute of the operation
            # spent duplicating what the Back up... button beside this
            # one does on its own - and doing it twice by two routes is
            # two things to keep right. The undo is whatever was backed
            # up before pressing Restore, and the confirmation says so.
            # How much of the window is distinct memory, measured on
            # this instrument rather than inferred from the file. A part
            # smaller than the window answers at more than one address,
            # so writing a whole dump writes those cells twice and the
            # second copy wins. Content cannot answer it: blank memory
            # reads alike at every address whether it is aliased or not,
            # and a wiped NVRAM is exactly what somebody reaching for
            # Restore is holding. The probe writes four bytes and puts
            # them back; the confirmation before this asks for a backup
            # to be in hand first, and that backup is the undo.
            self._progress(_("Measuring what the window really holds"), None)
            here = flash.measure_distinct(tds_fw.NVRAM_BASE,
                                          tds_fw.NVRAM_LEN)
            # And what the file says about itself, as corroboration
            # rather than as the measurement. A dump that repeats
            # somewhere this instrument does not came off a differently
            # laid out one.
            told = tds_fw.nvram_distinct(data)
            report["distinct"], report["held"] = here, len(data)
            if told < len(data) and told != here:
                raise RuntimeError(
                    _("This file was taken from an instrument whose NVRAM "
                    "is laid out differently: it repeats itself from %s "
                    "and this instrument holds %s of distinct memory. "
                    "Restoring it would write the wrong cells. Nothing "
                    "has been written and the instrument still holds "
                    "what it did.")
                    % (human_bytes(told), human_bytes(here)))
            self._progress(_("Writing the NVRAM ..."), 0.0)
            wrote_to = min(len(data), here)
            report["wrote"] = flash.write_span(
                data[:wrote_to], base=tds_fw.NVRAM_BASE,
                skip=tds_fw.RTC_LEN,
                note=lambda t, f: self._progress(
                    _("Restoring NVRAM - %s") % localise(t), f))
            self._progress(_("Reading it back to check it"), 0.0)
            back = flash.read(len(data), base=tds_fw.NVRAM_BASE,
                              note=lambda t, f: self._progress(
                                  _("Checking the NVRAM - %s") % localise(t), f))
            # From the clock upwards, because the clock was not written
            # and has moved on since anyway.
            wrong = [i for i in range(tds_fw.RTC_LEN, len(data))
                     if back[i] != data[i]]
            report["wrong"] = len(wrong)
            # Two different failures, and one message for both of them
            # sends the reader after the wrong thing. Below `wrote_to`
            # the bytes were written and did not take, which is the bus
            # or the part. Above it they were never written: they are
            # the same cells as somewhere lower down and were supposed
            # to follow, so a difference there says the measurement was
            # wrong and not that anything failed to write.
            if wrong and wrong[0] >= wrote_to:
                raise RuntimeError(
                    _("The %s of this file above %s does not match what "
                    "the instrument reads back, at %d place(s), the "
                    "first at 0x%X. That part was not written: it is the "
                    "same memory as lower down and should have followed "
                    "it. The measurement of what this instrument holds "
                    "was wrong, or the file is from a differently laid "
                    "out one. What was written below that point went in "
                    "correctly.")
                    % (human_bytes(len(data) - wrote_to),
                       human_bytes(wrote_to), len(wrong), wrong[0]))
            if wrong:
                raise RuntimeError(
                    _("The NVRAM did not read back as what was written, at "
                    "%d place(s), the first at 0x%X. What is in the "
                    "instrument now is neither what was there before nor "
                    "what is in the file. Whatever Back up... in the NVRAM "
                    "box wrote before this is the way back.")
                    % (len(wrong), wrong[0]))
        finally:
            try:
                inst.close()
            except Exception:
                pass
        return report

    def fw_run(self, plan):
        """Back up, verify, erase, program, verify.

        One job rather than five, because the instrument is only in this
        state once and every step after the first depends on the one
        before it. It is not cancellable: a flash stopped part way is a
        flash that has to be programmed again, so it runs to the end.

        The backups themselves are fw_keep, which the Backup tab's NVRAM
        button calls on its own. One reader, so a backup taken for its
        own sake is the same bytes and the same check as the one taken
        on the way to a write.
        """
        image = tds_fw.read_image(plan["image"], plan.get("archive") or "")
        if not image.startswith(tds_fw.IMAGE_HEAD):
            raise RuntimeError(
                _("%s does not begin like a firmware image for this family.")
                % os.path.basename(plan["image"]))
        # Read again here, so it is checked against what the catalogue
        # showed: a file replaced or damaged between the scan and Write
        # must not be the one that goes into the flash.
        if plan.get("sha") and hashlib.sha256(image).hexdigest() != plan["sha"]:
            raise RuntimeError(
                _("%s is not the file the catalogue listed - it has changed "
                  "since it was scanned. Nothing has been written. Refresh "
                  "the list and try again.") % os.path.basename(plan["image"]))
        report = {"image": plan["image"], "wrote": len(image), "backups": []}
        inst = self.fw_session(plan["resource"], plan.get("normal") or "")
        try:
            mon = tds_fw.Monitor(inst)
            flash = tds_fw.Flash(mon)
            report["flash"] = flash.identify()

            # Does it fit? Measured first, while the part still holds
            # something to measure - after the erase every boundary
            # reads 0xFF and span() can only fall back to the
            # catalogued size, which is the number this is checking.
            #
            # Nothing has ever been written to a part smaller than the
            # image meant for it, and nothing should be: program()
            # writes from the base for the length of the image, so an
            # image longer than the array wraps and overwrites its own
            # start. The images are not all one size - 25 are 4 MB,
            # three are 3 MB and three are 1.5 MB - and which part a
            # model carries does not follow from the image it runs, so
            # this is asked rather than assumed.
            room = flash.span()
            if len(image) > room:
                raise RuntimeError(
                    _("This image is %s and the flash in this instrument "
                    "measures %s. Writing it would run off the end of "
                    "the array and back over its own beginning.\n\n"
                    "Nothing has been erased and nothing is lost. Check "
                    "that the image is the right one for this model.")
                    % (tds_fw._size(len(image)), tds_fw._size(room)))
            report["span"] = room

            # The whole NVRAM window, always: it holds user settings,
            # saved waveforms, limits, masks and on the earlier
            # instruments the calibration constants, and a backup that
            # judged part of it not worth reading is one that cannot put
            # the instrument back. And all of the flash just measured,
            # not the size measured at Connect: the erase below takes
            # the whole part, so the backup has to hold the whole part.
            for what, base, length in (
                    ("NVRAM", tds_fw.NVRAM_BASE, tds_fw.NVRAM_LEN),
                    ("Firmware", tds_fw.FLASH_BASE, room)):
                self.fw_keep(flash, what, base, length, plan, report)
            # The helper is what makes this take minutes rather than
            # hours, but it is code running on the instrument. Asking
            # for the host's own page loop instead is how a first flash
            # on an instrument nobody has tried this on tests one new
            # thing at a time: that loop is the sequence the parts'
            # datasheets describe, driven from here where it can be
            # watched.
            # Whether it was asked for matters as much as what happened:
            # a run somebody chose to drive from here is not the same
            # story as one where the helper would not go, and telling
            # the second when the first is true reads as a failure.
            #
            # Armed before the erase, not after. arm() only writes the
            # helper to scratch RAM and reads it back, and a part with
            # no page buffer cannot be programmed without it - so if it
            # will not go, the refusal has to come while the flash is
            # still whole, which is what the refusal's wording says.
            report["asked_slow"] = bool(plan.get("slow"))
            report["helper"] = (False if plan.get("slow")
                                else flash.arm() is not None)
            if not flash.paged and flash.helper is None:
                flash._refuse()
            self._progress(_("Erasing the flash ..."), None)
            # The whole part, not the devices the image reaches: an old
            # firmware longer than the new one would otherwise stay in
            # the array above it, and the check after the write could
            # not tell an instrument holding one image from one holding
            # the remains of two.
            flash.erase(room, note=lambda t: self._progress(t, None))
            report["slow_pages"], report["blank_pages"] = flash.program(
                image, note=self._progress)
            self._progress(_("Checking what was written ..."), 0.0)
            report["faults"] = flash.verify(image, note=self._progress,
                                            length=room)
        finally:
            try:
                inst.close()
            except Exception:
                pass
        return report
