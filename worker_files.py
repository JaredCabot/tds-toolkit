"""worker_files - the Files tab's jobs.

Listing, reading and writing files on the instrument, batches, folders,
copies and formatting, and the checks that guard a write.

One of the mixins Worker in worker.py is made of. Everything here runs
on the worker's thread, never on Tk's.
"""
import os
import time

import tds_bak
from i18n import gettext as _
from tdstoolkit import (
    ATTEMPTS, GIVE_UP_AFTER, NO_MEDIA, READ_RATE, STAGE_NAME,
    TRANSFER_TIMEOUT, _formatted, clear_transfer_block, is_phantom,
    load_uploads, log_note, real_names, save_uploads, set_transfer_block,
    transfer_blocked
)


class FileJobs(object):
    """Jobs on the instrument's file system."""

    # -- the filesystem -----------------------------------------------------

    def volumes(self):
        """Which volumes this instrument has.

        Probing an empty floppy raises 252 and a 640A raises 257 for the
        same question, and _routine() excuses both only under this name -
        so the context is set here, for whichever job asked, and put back
        after, or the Backup tab's survey turned them into a modal box.
        """
        was, self.context = self.context, "volumes"
        try:
            here = self.fs.get_cwd() or "hd0:"
            found = []
            for c in self.CANDIDATES:
                try:
                    self.fs.set_cwd(c)
                    if (self.fs.get_cwd() or "").rstrip("/").lower(
                            ).startswith(c.rstrip("/").lower()):
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
        finally:
            self.context = was

    def listdir(self, path=None):
        if path is not None:
            self.fs.set_cwd(path)
        names = real_names(self.fs.dir())
        return {"cwd": self.fs.get_cwd(), "names": names}

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
        # A CWD to a folder that is not there fails without a word and
        # leaves the cwd where it was, so the listing would be of the
        # folder before - shown as this one's contents, and, inside a
        # recursive delete or download, walked again and again under a
        # path that grows each time. An empty drive is the one refusal
        # that is an answer.
        if base.rstrip("/").upper() != path.rstrip("/").upper():
            if NO_MEDIA in (self.fs.errors() or []):
                return {"cwd": path, "dirs": [], "files": [],
                        "no_media": True}
            raise IOError(_("%s does not exist on the instrument.") % path)
        # Filtered before classification, not after: each phantom would
        # otherwise cost two or three round trips to prove it is not a
        # folder, which is most of the wait on an application folder.
        names = real_names(self.fs.dir())
        dirs, files = [], []
        for i, n in enumerate(names):
            # Every file costs one 256 "file name not found", and the
            # instrument's event queue holds about twenty. A folder of
            # 63 files overflowed it, the 350 "queue overflow" was an
            # event nobody expected, and its warning box opened in the
            # middle of the result pump - which then held the program
            # busy until the box was found and closed. Emptied as it
            # goes, the queue never fills.
            if i and not i % 10:
                self.fs.errors()
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
        # No free-space reading here. Nothing has shown one since the
        # readout left the status bar, and it is a query a listing pays
        # for every time. It would not be worth having anyway: measured
        # on a TDS 754D at v8.0e, FILESYSTEM:FREESPACE? answers about 89
        # million to every ask, climbing 2,100 a second and wrapping, on
        # a 1.44 MB volume, with a disk in the drive or without one, and
        # raises no event to say so.
        return {"cwd": base, "dirs": dirs, "files": files,
                "no_media": empty_drive}

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
            raise IOError(_("%s does not exist on the instrument.") % path)
        self.fs.errors()
        t = time.time()
        try:
            data = self.fs.read(path, timeout=TRANSFER_TIMEOUT)
        except Exception as exc:
            # Whatever the instrument was still sending is drained, or
            # the next command in the batch reads the file's tail as its
            # reply - the same wound write_verified clears after.
            try:
                self.fs.clear()
            except Exception:
                pass
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

    def _finalize_staged(self, tmp, real, data):
        """Swap a verified temp file over the real one (Fix 6).

        `tmp` is already on the instrument and has been read back
        byte-for-byte, so the payload is proven. RENAME does the swap in
        place - no second transfer of the bytes - but it will not replace
        an existing name, so the original is deleted first.

        If this firmware has no working RENAME the verified bytes are
        written straight to the real name instead and read back again, so
        even the fallback path never lands a file it has not checked. The
        temp is cleared away either way.
        """
        parent = real.rstrip("/").rsplit("/", 1)[0]
        leaf = real.rsplit("/", 1)[-1]
        try:
            self.fs.delete(real)
            self.fs.wait_done()
            self.fs.rename(tmp, real)
            self.fs.wait_done()
            self.fs.errors()
            # Listed twice before the rename is disbelieved: the
            # directory lags a write (see write_verified), and a rename
            # judged on the first listing fell back to rewriting the
            # whole file, which is the churn the stage exists to avoid.
            for settle in (0.0, 2.0):
                time.sleep(settle)
                listed = [n.upper()
                          for n in real_names(self.fs.dir(parent))]
                if (leaf.upper() in listed
                        and STAGE_NAME.upper() not in listed):
                    return
        except Exception:
            pass
        # RENAME missing or it did not take. The original may be gone
        # (delete above ran), so put the verified bytes down directly and
        # read them back before trusting them, then remove the temp.
        self.fs.clear()
        self.fs.write(real, data)
        self.fs.wait_done()
        self.fs.errors()
        back = self.fs.read(
            real, timeout=TRANSFER_TIMEOUT + len(data) / READ_RATE)
        # Compared before the temp goes. The original is already deleted,
        # so the verified temp is the only good copy on the card until
        # the rewrite is proven; deleted first, a rewrite that came back
        # wrong left nothing good anywhere.
        if back != data:
            raise RuntimeError(
                _("staged rename is unsupported and the direct rewrite of "
                "%s did not verify") % real)
        try:
            self.fs.delete(tmp)
            self.fs.wait_done()
            self.fs.errors()
        except Exception:
            pass

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
        # The lock-out after a failed transfer is checked here, where the
        # write happens, and not only in upload_plan: Upload of a single
        # file and Send to disk come straight here, and went on writing
        # into a filesystem the program had just declared wedged.
        locked = transfer_blocked(getattr(self, "addr", None))
        if locked:
            raise RuntimeError(_(
                "Nothing was written. The instrument's file system was "
                "checked first and did not answer normally:\n\n%s\n\nThis "
                "is usually a wedged file system. Power cycle the "
                "instrument, confirm it lists its files, then try again.")
                % locked)
        leaf = path.rsplit("/", 1)[-1]
        # Fix 6 (staged overwrite): if a file of this name is already on
        # the instrument, send the new copy to a temp sibling and RENAME
        # it over the original only once it has been read back and
        # verified. A write that dies half way then costs the temp, not
        # the copy already on disk. New files (nothing to lose) write
        # straight to their final name, exactly as before.
        real = None
        if self.can_overwrite:
            over_parent = path.rstrip("/").rsplit("/", 1)[0]
            try:
                already = leaf.upper() in [
                    n.upper() for n in real_names(self.fs.dir(over_parent))]
            except Exception:
                already = False
            if already:
                real = path
                path = over_parent + "/" + STAGE_NAME
        last = None
        for attempt in range(1, ATTEMPTS + 1):
            step = span / ATTEMPTS
            here = base + span * (attempt - 1.0) / ATTEMPTS
            suffix = "" if attempt == 1 else _(" (attempt %d of %d)") % (
                attempt, ATTEMPTS)
            self._progress(_("Preparing %s%s") % (leaf, suffix), here)
            # Only on a firmware that cannot overwrite. This delete used
            # to happen before every write, unconditionally, and it is
            # the single most expensive thing this program did to an
            # instrument: DELETE is what exhausts the filesystem, and at
            # one per file an upload of a dozen files was already at the
            # limit. It was never needed - OVERWRITE ON is set at
            # connect and has been for as long as this has - so it was
            # costing the whole upload and buying nothing.
            #
            # It also made a failed upload destructive: the old file was
            # already gone before the new one was sent.
            if not self.can_overwrite:
                try:
                    self.fs.delete(path)
                except Exception:
                    pass
                self.fs.wait_done()
            self._progress(_("Sending %s, %s bytes%s")
                           % (leaf, format(len(data), ","), suffix),
                           here + step / 3)
            try:
                self.fs.write(path, data)
            except Exception as exc:
                # A write that did not finish left the instrument part
                # way through an indefinite-length block, waiting for
                # bytes that are not coming. Everything sent after that
                # times out too, so without this one device clear the
                # first big file in a folder takes the rest of the
                # upload down with it.
                self.fs.clear()
                last = "the write did not finish: %s" % exc
                continue
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
                raise IOError(_("There is no disk in the drive, so nothing "
                              "can be written to it."))
            # Then look for it. A write that landed shows up in the
            # directory immediately; one the instrument accepted and
            # discarded does not, and reading back a file that was never
            # created only buys a timeout on the way to the same answer.
            parent = path.rstrip("/").rsplit("/", 1)[0]
            wleaf = path.rsplit("/", 1)[-1]   # the name actually written
            # Asked twice before it is believed. Measured on a TDS 784D:
            # the listing taken straight after a 256 kB write came back
            # empty, and the file was there all along - the instrument
            # answers *OPC? before its own directory shows the write.
            # Rewriting a large file because of that costs minutes and
            # fixes nothing.
            for settle in (0.0, 2.0):
                time.sleep(settle)
                try:
                    listed = [n.upper()
                              for n in real_names(self.fs.dir(parent))]
                except Exception:
                    listed = None      # cannot tell - fall through and read
                if listed is None or wleaf.upper() in listed:
                    break
            if listed is not None and wleaf.upper() not in listed:
                # The write itself did not raise - the bytes went out and
                # the instrument took them - so the clear beside the
                # write above was never reached. This is the same wound
                # and wants the same dressing: measured on a TDS 784D,
                # once the listing stops showing what was just written,
                # every command after it times out until the bus is
                # cleared.
                self.fs.clear()
                last = "the file never appeared in %s after writing" % parent
                continue
            self._progress(_("Reading %s back to verify it%s") % (leaf, suffix),
                           here + 2 * step / 3)
            try:
                # Sized to the file, not flat. TRANSFER_TIMEOUT alone is
                # how long the instrument may take to start talking; a
                # 1.4 MB read then needs the better part of a minute to
                # arrive at the measured 32 kB/s, and a flat 20 seconds
                # cut off every read-back over about half a megabyte.
                back = self.fs.read(
                    path, timeout=TRANSFER_TIMEOUT + len(data) / READ_RATE)
            except Exception as exc:
                # Same reasoning as the write above: a read that stopped
                # part way leaves bytes in the instrument's output queue,
                # and the next command is answered with the tail of this
                # one.
                self.fs.clear()
                why = self._transfer_failed("READFILE", path, exc, 0.0)
                if self.no_transfers:
                    raise IOError(why)
                last = "read-back failed: %s" % exc
                continue
            if back == data:
                if real is not None:
                    # Verified in the temp; put it in place of the original.
                    self._finalize_staged(path, real, data)
                    return {"path": real, "bytes": len(data),
                            "attempts": attempt}
                return {"path": path, "bytes": len(data), "attempts": attempt}
            nz = sum(1 for b in back if b)
            last = ("mismatch: %d bytes back, %d non-zero"
                    % (len(back), nz))
        raise RuntimeError(_("upload not verified after %d attempts (%s)")
                           % (ATTEMPTS, last))

    def delete(self, path):
        """Delete a file, from the volume root, after the protection check."""
        why = self.refuse_reason(path)
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
            raise RuntimeError(_("%s was not deleted (events %s)")
                               % (path, events))
        return {"path": path, "events": events, "removed": True}

    def mkdir(self, path):
        self.fs.mkdir(path)
        self.fs.wait_done()
        return {"path": path, "events": self.fs.errors()}

    def _folder_names(self, folder):
        """What is in `folder` now, asking twice if it answers empty.

        A listing taken straight after a write or a delete can come back
        empty on this instrument and be right again a moment later - see
        INSTRUMENT-NOTES, "After a big write, the directory can lag" - so
        an empty answer is asked again before it is believed. Without
        that, the loss check below would cry wolf on a folder that is
        merely slow.
        """
        for wait in (0.0, 2.0):
            if wait:
                time.sleep(wait)
            try:
                self.fs.set_cwd(folder)
                names = real_names(self.fs.dir())
            except Exception:
                names = []
            self.fs.errors()
            if names:
                return names
        return []

    def _lost(self, folder, before, asked):
        """Names that vanished from `folder` without being asked for.

        This instrument has, twice, emptied a directory of things nobody
        deleted. It has not been reproduced in about fifty attempts and
        the mechanism is unknown, so there is nothing to fix - but it is
        silent, and silent data loss is the one thing worth spending a
        listing on. Comparing what was there against what is there costs
        one DIR? and turns the loss into a warning.

        Deliberately one-directional: it reports what went, never what
        arrived, so another program writing to the same folder is not
        mistaken for damage.
        """
        if not before:
            return []
        asked = {n.upper() for n in asked}
        after = {n.upper() for n in self._folder_names(folder)}
        return [n for n in before
                if n.upper() not in after and n.upper() not in asked]

    def delete_many(self, paths):
        done, failed = [], []
        # Every path in one delete comes from one folder, which is what
        # the file list can select. Taking the folder from the first is
        # enough, and a mixed list simply checks the wrong folder rather
        # than breaking.
        folder = paths[0].rsplit("/", 1)[0] if paths else None
        before = self._folder_names(folder) if folder else []
        skipped, running = [], 0
        for i, p in enumerate(paths, 1):
            self._progress(_("Deleting %d of %d: %s")
                           % (i, len(paths), p.rsplit("/", 1)[-1]),
                           (i - 1.0) / len(paths))
            try:
                self.delete(p)
                done.append(p)
                running = 0
            except Exception as exc:
                failed.append((p, str(exc)))
                # The rule uploads follow: one failure is a file, two in
                # a row is the instrument. DELETE is what wears this
                # firmware's mass storage out, so ploughing on through
                # forty of them into a filesystem that has stopped only
                # makes it worse.
                running += 1
                if running >= GIVE_UP_AFTER and i < len(paths):
                    skipped = paths[i:]
                    log_note("delete_many", "stopped after %d consecutive"
                             " failures with %d file(s) not attempted"
                             % (running, len(skipped)))
                    break
        lost = self._lost(folder, before,
                          [p.rsplit("/", 1)[-1] for p in paths
                           if p not in skipped])
        return {"done": done, "failed": failed, "lost": lost,
                "folder": folder, "skipped": skipped}

    def upload_many(self, items):
        """`items` is [(path on the PC, destination path on the instrument)].

        Every file is verified individually by write_verified, so a batch
        that reports success really did land byte for byte.
        """
        done, failed, unchanged, skipped, running = [], [], [], [], 0
        # Keyed by address and identity together: a different scope put
        # on the same address must not inherit the last one's marks and
        # have a file skipped that was never compared against it.
        addr = getattr(self, "ident", "") or ""
        marks = load_uploads().get(addr, {})
        seen = {}            # parent -> leaf names now on the instrument
        dirty = False
        try:
            for i, (src, dest) in enumerate(items, 1):
                share = 1.0 / len(items)
                try:
                    with open(src, "rb") as fh:
                        data = fh.read()
                except Exception as exc:
                    # A file on the PC that will not open says nothing
                    # about the instrument, so it is not counted towards
                    # giving up (A10): two unreadable local files used to
                    # stop a healthy instrument's batch.
                    failed.append((dest, str(exc)))
                    continue
                # Skip a file already on the instrument byte for byte.
                # Rewriting what is already there is the churn that takes
                # mass storage down, so a file is skipped only when its hash
                # matches what was last written to this address AND it still
                # appears in the listing - a copy deleted on the instrument
                # since is written again.
                digest = tds_bak.digest(data)
                if marks.get(dest) == digest:
                    parent = dest.rstrip("/").rsplit("/", 1)[0]
                    leaf = dest.rstrip("/").rsplit("/", 1)[-1].upper()
                    if parent not in seen:
                        try:
                            seen[parent] = set(n.upper() for n
                                               in real_names(self.fs.dir(parent)))
                        except Exception:
                            seen[parent] = None     # cannot tell - do not skip
                    there = seen[parent]
                    if there is not None and leaf in there:
                        self._progress(_("%s is already there, unchanged")
                                       % dest.rsplit("/", 1)[-1], i * share)
                        unchanged.append(dest)
                        running = 0
                        continue
                try:
                    self.write_verified(dest, data, (i - 1.0) * share, share)
                    done.append((dest, len(data)))
                    marks[dest] = digest
                    dirty = True
                    running = 0
                except Exception as exc:
                    failed.append((dest, str(exc)))
                    if marks.pop(dest, None) is not None:
                        dirty = True     # a bad write - the mark is no good
                    running += 1
                    # Two in a row is an instrument that has stopped
                    # answering, not two awkward files, and carrying on is
                    # actively harmful: every attempt deletes the file it is
                    # about to replace, so a batch that ploughs on through a
                    # dead bus empties a folder it cannot refill. Measured on
                    # a TDS 784D - it stopped answering part way through a
                    # 70-file upload and every file after it failed four
                    # times, having deleted the instrument's copy first.
                    if running >= GIVE_UP_AFTER and i < len(items):
                        skipped = [d for _s, d in items[i:]]
                        log_note("upload_many", "stopped after %d consecutive"
                                 " failures with %d file(s) not attempted"
                                 % (running, len(skipped)))
                        break
        finally:
            if dirty:
                all_marks = load_uploads()
                all_marks[addr] = marks
                save_uploads(all_marks)
        return {"done": done, "failed": failed, "skipped": skipped,
                "unchanged": unchanged}

    def download_tree(self, path, destdir, as_name=None):
        """Download a folder and everything beneath it.

        `as_name` renames the folder written on the PC. A whole volume is
        why: "hd0:" is a perfectly good name on the instrument and not a
        legal one on Windows.

        The whole tree is enumerated first so that progress can be
        reported against a known total. That costs one listing per folder
        up front, which is cheaper than it sounds beside the transfers
        themselves, and much better than a bar that cannot say how far
        along it is.
        """
        leaf = as_name or path.rstrip("/").rsplit("/", 1)[-1]
        want, folders = [], []

        def walk(remote, local):
            folders.append(local)
            self._progress(_("Looking in %s ...") % remote, None)
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
                raise RuntimeError(_("cannot create %s: %s") % (d, exc))

        done, failed = [], []
        for i, (remote, local) in enumerate(want, 1):
            self._progress(_("Downloading %d of %d: %s")
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
            self._progress(_("Downloading %d of %d: %s") % (i, len(paths), leaf),
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

    @classmethod
    def refuse_reason(cls, path):
        """Why `path` cannot be deleted at all, or None.

        Only the two that are not files: a drive, and the phantom entry
        that carries half of somebody's long file name.
        """
        p = path.rstrip("/")
        leaf = p.rsplit("/", 1)[-1].upper()
        if "/" not in p:
            return _("'%s' is a drive, not a file or folder.") % p
        if is_phantom(leaf):
            # Should be unreachable - these never reach the UI - but a
            # DELETE aimed at one would strip the long name off the real
            # file that follows it in the directory table.
            return (_("'%s' is not a file. It is part of how a long file "
                    "name is stored on the card, and deleting it would "
                    "damage the file it belongs to.") % leaf)
        return None

    @classmethod
    def system_reason(cls, path):
        """Why `path` is the instrument's own, or None if it is not.

        Not a refusal. The caller asks about it first and deletes it if
        the answer is yes.
        """
        p = path.rstrip("/")
        leaf = p.rsplit("/", 1)[-1].upper()
        upper = p.upper()
        if "/" not in p:
            return None
        # Nothing on a floppy is the instrument's. The system files live
        # on the hard disk; a floppy holds whatever the user put there
        # and is theirs to empty.
        if upper.split("/", 1)[0].startswith("FD"):
            return None
        for tree in cls.SYSTEM_TREES:
            if ("/" + tree) in upper or upper.endswith("/" + tree):
                return cls.RUNTIME_MSG
        for table in (cls.SYSTEM_DIRS, cls.SYSTEM_FILES):
            if leaf in table:
                return table[leaf]
        return None

    def format_volume(self, plan):
        """Format a whole volume, and check afterwards that it is empty.

        The instrument says nothing either way - FILESYSTEM:FORMAT has no
        query form and raises no event on success - so the listing
        afterwards is the only report there is. A volume that still holds
        names did not format, and saying so beats a silent success.
        """
        drive = plan["drive"]
        self.context = "format %s" % drive
        self._progress(_("Formatting %s ...") % drive, None)
        self.fs.set_cwd(drive)
        self.fs.format_drive(drive)
        self.fs.wait_done()
        events = self.fs.errors()
        self._progress(_("Checking what is left on %s") % drive, None)
        self.fs.set_cwd(drive)
        left = real_names(self.fs.dir())
        self.fs.errors()
        if not left:
            _formatted.add((getattr(self, "addr", None), drive.upper()))
        return {"drive": drive, "left": left, "events": events}

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
        """Remove a folder and everything in it, deepest folder first.

        FILESYSTEM:RMDIR is recursive on this firmware - see tds_fs.rmdir -
        and on a large tree it does not finish. Measured on a TDS 784D:
        one RMDIR aimed at a folder of 11 subfolders and about 30 files
        raised event 250 and stopped part way, having emptied seven of
        the subfolders and removed one outright, and the folder itself
        was still there afterwards. A half-deleted folder is the worst
        of the three outcomes, so the walk is done here instead: one
        folder at a time, from the bottom up, each command small enough
        for the instrument to finish before the next one arrives.

        Costs more commands than the single RMDIR. It buys a progress
        bar on a tree that took minutes in silence, and a failure part
        way that can simply be run again - the walk finds whatever is
        left, so a second attempt carries on where the first stopped.

        Refuses a protected name, and verifies afterwards rather than
        trusting the empty event queue, because on this instrument silence
        means nothing either way. Only the folder named is checked against
        the protection table, which is what the recursive RMDIR did too:
        the names inside a copy are the same names as inside the original,
        so checking them all would make any copy of APP undeletable.
        """
        why = self.refuse_reason(path)
        if why:
            raise RuntimeError(why)
        leaf = path.rstrip("/").rsplit("/", 1)[-1].upper()
        parent = path.rstrip("/").rsplit("/", 1)[0]
        volume = path.split("/")[0]
        self.context = "rmdir %s" % path
        # What else is in the folder this one is being taken out of.
        # Removing a tree is the operation the unexplained loss has shown
        # up around, so the neighbours are counted before and after; see
        # _lost.
        beside = self._folder_names(parent)

        # Children before parents, which is the order they have to go in.
        tree = []

        def walk(where):
            self._progress(_("Looking in %s ...") % where, None)
            listing = self.listdir_split(where)
            for one in listing["dirs"]:
                walk("%s/%s" % (where, one))
            tree.append((where, listing["files"]))

        walk(path)
        for i, (where, files) in enumerate(tree, 1):
            # Stand at the VOLUME ROOT, which cannot be inside the target
            # at any depth. RMDIR is refused with event 257 if the cwd is
            # within the folder being removed, and that refusal is what
            # wedged the filesystem subsystem badly enough to need a
            # reimage. listdir_split above left the cwd inside the tree,
            # so this is not optional.
            self._progress(_("Emptying %s (%d of %d)")
                           % (where.rsplit("/", 1)[-1], i, len(tree)),
                           (i - 1.0) / len(tree))
            if files:
                # One command for the whole folder, not one per file.
                # DELETE is the operation that exhausts this firmware -
                # about two dozen and mass storage goes down - so a
                # folder of seventy files was three times over the limit
                # on its own. Measured on a TDS 784D: the wildcard form
                # cleared eight files in 0.2 s with an empty event
                # queue, and 120 in about four seconds, with the
                # instrument writing and reading normally straight
                # afterwards. Event 250 is common here and means
                # nothing; the re-listing below is what decides.
                self.fs.set_cwd(volume)
                self.fs.delete("%s/*.*" % where)
                self.fs.wait_done()
                self.fs.errors()
                # Whatever *.* did not match, by hand. On a TDS 784D it
                # matches everything - a folder of WITHEXT.TXT, NOEXT,
                # DOTTED. and X.B was cleared by one wildcard - so this
                # loop is expected never to run there. It stays because
                # the same code serves a 640A, a 680B and a 784C, none
                # of which have been asked, and because the cost of
                # being wrong is a folder that will not delete. One
                # listing a folder is a cheap way not to guess.
                try:
                    self.fs.set_cwd(where)
                    stayed = real_names(self.fs.dir())
                except Exception:
                    stayed = []
                for name in stayed:
                    self.fs.set_cwd(volume)
                    self.fs.delete("%s/%s" % (where, name))
                    # A query arriving while dosFs is still working is
                    # what raises event 250.
                    self.fs.wait_done()
            self.fs.errors()
            self.fs.set_cwd(volume)
            self.fs.rmdir(where)
            self.fs.wait_done()
            events = self.fs.errors()

        still = self._folder_names(parent)
        gone = leaf not in [n.upper() for n in still]
        if not gone:
            raise RuntimeError(_("%s is still there after removing the %d "
                               "folder(s) inside it (events %s). Running "
                               "it again will carry on from here.")
                               % (path, len(tree), events))
        lost = [n for n in beside
                if n.upper() != leaf
                and n.upper() not in {s.upper() for s in still}]
        return {"path": path, "events": events, "removed": True,
                "folders": len(tree), "lost": lost, "folder": parent}

    def copy(self, source, dest, is_dir):
        """Copy a file or a folder on the instrument. No GPIB transfer.

        387 kB/s against 3.0 kB/s for the same bytes through WRITEFILE, so
        this is 129 times faster than downloading and re-uploading and it
        is the right way to duplicate anything already on the instrument.

        A file is one command. A folder has to be walked, because COPY
        refuses a directory source - the manual's own example raises event
        257 on v7.4e and creates nothing. The walk is top down, unlike
        rmdir's: a destination folder has to exist before anything can be
        copied into it. One wildcard per folder rather than one command
        per file, which is the same economy the wildcard delete gets.

        Verifies by listing rather than by the event queue, because on this
        instrument an empty queue means nothing either way.
        """
        volume = dest.split("/")[0]
        self.context = "copy %s to %s" % (source, dest)
        if not is_dir:
            self._progress(_("Copying %s ...") % source.rsplit("/", 1)[-1], None)
            self.fs.set_cwd(volume)
            self.fs.copy(source, dest)
            self.fs.wait_done()
            self.fs.errors()
            return self._copied(dest, 1)

        # Parents before children: a folder must exist to be copied into.
        tree = []

        def walk(where, under):
            self._progress(_("Looking in %s ...") % where, None)
            listing = self.listdir_split(where)
            tree.append((where, under, listing["files"]))
            for one in listing["dirs"]:
                walk("%s/%s" % (where, one), "%s/%s" % (under, one))

        walk(source, dest)
        for i, (where, under, files) in enumerate(tree, 1):
            self._progress(_("Copying %s (%d of %d)")
                           % (under.rsplit("/", 1)[-1], i, len(tree)),
                           (i - 1.0) / len(tree))
            self.fs.set_cwd(volume)
            self.fs.mkdir(under)
            self.fs.wait_done()
            self.fs.errors()
            if files:
                self.fs.set_cwd(volume)
                self.fs.copy("%s/*.*" % where, under)
                self.fs.wait_done()
                self.fs.errors()
        return self._copied(dest, len(tree))

    def _copied(self, dest, folders):
        """Confirm the destination arrived, by listing its parent."""
        parent = dest.rstrip("/").rsplit("/", 1)[0]
        leaf = dest.rstrip("/").rsplit("/", 1)[-1].upper()
        self.fs.set_cwd(parent)
        there = leaf in [n.upper() for n in self.fs.dir()]
        self.fs.errors()
        if not there:
            raise RuntimeError(_("%s is not there after the copy.") % dest)
        return {"dest": dest, "folders": folders, "copied": True}

    def _upload_volume(self, made, items):
        """The volume a batch writes to, e.g. 'hd0:', or None if unclear."""
        for coll in (made, [d for _s, d in items]):
            for p in coll:
                if ":" in p:
                    return p.split(":", 1)[0] + ":"
        return None

    def health_check(self, volume):
        """Is the instrument's file system answering? Returns None if it
        is, or a sentence saying what is wrong.

        A root that will not list is the wedged mass-storage state, and
        writing into it is what turns a hung task into a lost card; a
        listing that comes back empty is the same signature, not a genuinely
        empty disk (see FILE-TRANSFER-HARDENING.md sections 3 and 8). Either
        way the caller must not write. `dir()` already verifies the cwd took
        and raises if the volume will not answer.

        The one empty root believed is a volume this program has just
        formatted and seen empty, with nothing written since.
        """
        try:
            listed = self.fs.dir(volume)
        except Exception as exc:
            return _("%s did not list (%s)") % (volume, exc)
        if not listed:
            if (getattr(self, "addr", None), volume.upper()) in _formatted:
                return None
            return (_("%s listed nothing. On this instrument an empty root "
                      "is the wedged-filesystem signature, not an empty "
                      "disk. If it really is blank, format it from the Files "
                      "tab first: a disk this program has just formatted is "
                      "trusted to be empty.") % volume)
        return None

    def _clear_lockout_if_healthy(self, cwd):
        """Fix 10: let a recovered instrument back in after a failed run.

        A transfer that wedged the file system locks the instrument out of
        further writes (see upload_plan). Only a fresh reconnect clears
        that, and only if its volume now lists - a scope still wedged stays
        locked, so an upload is still refused. Called from connect.
        """
        if not transfer_blocked(self.addr):
            return
        cwd = cwd or ""
        vol = cwd.split(":", 1)[0] + ":" if ":" in cwd else ""
        if vol and not self.health_check(vol):
            clear_transfer_block(self.addr)
            log_note("connect", "instrument healthy again after a failed "
                     "transfer; write lock-out cleared")

    def upload_plan(self, made, items):
        """Make these folders, then write these files. Both already named.

        MKDIR first and its refusals ignored - a folder that is already
        there refuses, and so does one that cannot be made. Which it was
        shows up as the file writes either landing or not, and those are
        verified byte for byte, so guessing from the MKDIR would only be
        a second, worse answer to the same question.

        Folders are made in the order given, which has to be parents
        before children or the children have nowhere to go.

        Split out from upload_tree because the two callers name things
        differently. A restore is putting back names that came off the
        instrument, so they are already legal; a folder dragged in from
        Explorer is not, and every segment of it has to go through the
        8.3 rule first - which is the UI's job, because only the UI
        knows what is already in the destination and therefore what a
        proposed name would collide with.
        """
        # Fix 10 (failure lock-out): refuse outright if an earlier run left
        # this instrument in a failed, wedged state. Only a fresh reconnect
        # clears that (see connect), so a retry alone will not - which is
        # the point: writing again into a wedged file system is exactly what
        # loses a card. Checked before the pre-flight below, so a disk that
        # is flapping back to a good listing cannot let a write slip through
        # without the instrument first being power-cycled.
        locked = transfer_blocked(self.addr)
        if locked:
            log_note("upload_plan",
                     "REFUSED, instrument locked out: %s" % locked)
            # Surfaced through the same "blocked" path as a pre-flight
            # health-check refusal: the reason kept from the run that
            # wedged it reads naturally in that dialog ("... did not
            # answer normally: <reason> ... power cycle ... try again"),
            # and a power cycle is exactly what clears the lock-out - the
            # reconnect after it passes the health check. No second dialog
            # string to translate, and the remedy shown is the right one.
            return {"done": [], "failed": [],
                    "skipped": [d for _s, d in items], "folders": 0,
                    "blocked": locked}
        # Pre-flight: the file system must be answering before a byte is
        # written. If it is not, nothing is written and the batch is
        # BLOCKED - safe to retry once the instrument is well, no override
        # needed - rather than deleting-and-rewriting into a wedged disk.
        volume = self._upload_volume(made, items)
        if volume:
            wrong = self.health_check(volume)
            if wrong:
                log_note("upload_plan", "BLOCKED before writing: %s" % wrong)
                return {"done": [], "failed": [],
                        "skipped": [d for _s, d in items],
                        "folders": 0, "blocked": wrong}
        for i, one in enumerate(made):
            self._progress(_("Making folder %d of %d") % (i + 1, len(made)),
                           None)
            try:
                self.mkdir(one)
            except Exception as exc:
                log_note("upload_plan", "mkdir %s: %s" % (one, exc))
        out = (self.upload_many(items) if items
               else {"done": [], "failed": [], "skipped": []})
        out["folders"] = len(made)
        if volume:
            # Written to now, so an empty root is no longer to be believed.
            _formatted.discard((getattr(self, "addr", None), volume.upper()))
        # Post-flight: a run that verified its own files but left the root
        # unreadable is the state that preceded the loss of a card. The root
        # must still list, or the whole run is a failure however well the
        # individual files verified.
        if volume:
            wrong = self.health_check(volume)
            if wrong:
                log_note("upload_plan", "FAILED after writing: %s" % wrong)
                out["unhealthy"] = wrong
                # Fix 10: this run wedged the disk. Lock the instrument out
                # of further writes until a fresh reconnect proves it well.
                set_transfer_block(self.addr, wrong)
        return out

    def upload_tree(self, folder, dest):
        """Put a local tree back on the instrument, folders and all.

        Names go up as they are. This is the restore path: what is in
        the bundle came off an instrument, so it already fits 8.3.
        """
        items, made = [], []
        for here, _dirs, names in os.walk(folder):
            rel = os.path.relpath(here, folder).replace(os.sep, "/")
            remote = dest if rel == "." else "%s/%s" % (dest, rel)
            if rel != ".":
                made.append(remote)
            for name in sorted(names):
                items.append((os.path.join(here, name),
                              "%s/%s" % (remote, name)))
        return self.upload_plan(made, items)

    # ---------------------------------------------------------- protection
    #
    # Two different answers, and they used to be one.
    #
    # A drive is not a file and a phantom directory entry is not a file
    # either; deleting one is meaningless or damaging and there is nothing
    # to confirm. Those are refused outright.
    #
    # The Java runtime, the boot chain and the shipped applications are a
    # different matter. They were refused outright too, on the belief that
    # putting one back meant reimaging the card. That turned out to be
    # wrong: every system file loads back over GPIB, so this is somebody
    # else's instrument and somebody else's decision. They are now
    # deletable, behind a warning of their own that has to be answered
    # before the ordinary delete confirmation is even asked.
    #
    # Still worth being careful about: an RMDIR test wedged the filesystem
    # subsystem and cost a reimage. That is why the working directory is
    # moved to the volume root first, and that guard is not negotiable.
    RUNTIME_MSG = ("It is part of the Java runtime. Deleting it will stop "
                   "the instrument from running applications until the "
                   "file is put back.")
    BOOT_MSG = ("It is part of the instrument's boot chain. Deleting it "
                "will stop the runtime starting at power-on.")

    # SYSTEM~1 is deliberately not here. It is the card's own recycle
    # folder and holds nothing the instrument needs; it fills up with
    # whatever was deleted from a PC and is exactly the sort of thing
    # somebody opens this program to clear out.
    SYSTEM_DIRS = {
        "APP": ("It holds the Java runtime and every shipped application."),
        "TDSRTE1": RUNTIME_MSG,
    }
    SYSTEM_FILES = {
        "STARTUP.BAT": BOOT_MSG, "OSSA.BAT": BOOT_MSG,
        "RTE1.BAT": BOOT_MSG, "RTE1ORIG.BAT": BOOT_MSG,
        "RT.JAR": RUNTIME_MSG, "JAVA68K.O": RUNTIME_MSG,
        "LIBJIT.O": RUNTIME_MSG, "NIGPIB.O": RUNTIME_MSG,
        "EXTCP.O": RUNTIME_MSG, "PATCH.O": RUNTIME_MSG,
        "LOGO.BIN": RUNTIME_MSG, "VERSION.DAT": RUNTIME_MSG,
    }
    # Everything at or below this path is the runtime itself.
    SYSTEM_TREES = ("APP/TDSRTE1",)
