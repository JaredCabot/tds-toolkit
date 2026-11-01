"""worker_wfm - the Waveforms, Screen and Errors tabs' jobs.

Sources, captures and sends, the hardcopy, and the instrument's error
log.

One of the mixins Worker in worker.py is made of. Everything here runs
on the worker's thread, never on Tk's.
"""
import time
from i18n import gettext as _
from tdstoolkit import (
    real_names
)


class WaveformJobs(object):
    """Jobs for waveforms, the screen and the error log."""

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
        # 16 bits wide, like the backup: averaging and hi-res put a real
        # low byte in every sample, and a file saved from an 8-bit read
        # had thrown it away. Twice the bytes, about 0.3 s more on a
        # 50,000-point record.
        t = time.time()
        waves, refused, how = self.wfm.capture(
            list(live), list(refs), width=2,
            note=lambda name, done: self._progress(_("Reading %s") % name,
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
            raise IOError(_("%s was written but reading it back gave "
                          "something else - the instrument did not keep "
                          "what was sent.") % dest)
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
            self._progress(_("Sending %s") % dest, i / float(len(items)))
            out = self.wfm.send_to_ref(wave, dest, allocate_from)
            if not self.wfm.verify_ref(wave, dest):
                raise IOError(_("%s was written but reading it back gave "
                              "something else - the instrument did not "
                              "keep what was sent.") % dest)
            out["verified"] = True
            done.append(out)
        return {"sent": done}

    def wfm_to_disk(self, data, drive, stem):
        """Write a .WFM into WAVEFORM on this drive, under a fresh name.

        The folder is made if it is not there. The name is the source
        the trace came from with a four-digit serial after it, and the
        serial is the first the folder does not already hold - so
        sending CH1 twice leaves two files rather than one overwritten
        one. Four characters of source and four of serial is the whole
        of an 8.3 stem.
        """
        drive = drive.rstrip("/")
        folder = "%s/%s" % (drive, self.WFM_DIR)
        # is_dir asks by trying to enter the name, so it answers about
        # wherever the instrument is standing. Moved to the drive first,
        # or a WAVEFORM folder under the folder last browsed would be
        # taken for this one.
        self.fs.set_cwd(drive)
        if not self.is_dir(drive, self.WFM_DIR):
            self.fs.mkdir(folder)
            self.fs.wait_done()
            self.fs.errors()
        # Proved rather than assumed: a MKDIR that failed for any other
        # reason would otherwise leave the write aimed at a path that is
        # not there, and the read-back would report it as a bad bus.
        self.fs.set_cwd(folder)
        if (self.fs.get_cwd() or "").rstrip("/").upper() != folder.upper():
            raise RuntimeError(_("%s could not be made on the instrument")
                               % folder)
        taken = {n.upper() for n in real_names(self.fs.dir())}
        self.fs.errors()
        head = "".join(c for c in (stem or "WAVE").upper()
                       if c.isascii() and c.isalnum())[:4] or "WAVE"
        for serial in range(1, 10000):
            name = "%s%04d.WFM" % (head, serial)
            if name not in taken:
                break
        else:
            raise RuntimeError(_("%s already holds every %s name there is")
                               % (folder, head))
        return self.write_verified("%s/%s" % (folder, name), data)



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
            raise IOError(_("The instrument still reports %d entries after "
                          "being told to clear the log.%s")
                          % (len(left),
                             (_("  It said: ") + "; ".join(events))
                             if events else ""))
        return {"cleared": True}

    def _errlog(self):
        """The instrument's error log, or [] if it has not got one."""
        try:
            return self.err.entries()
        except Exception:
            return []

    def _errlog_since(self, before):
        """The entries added since `before` was taken.

        The log is a ring: once it is full the oldest entries fall off,
        and then what was read first is no longer a prefix of what is
        read now. So the common-prefix answer is checked rather than
        assumed, and anything that does not fit it falls back to
        "whatever is there that was not there before".
        """
        after = self._errlog()
        if after[:len(before)] == before:
            return after[len(before):]
        return [one for one in after if one not in before]



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
