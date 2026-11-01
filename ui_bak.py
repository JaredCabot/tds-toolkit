"""The Backup tab.

BackupTab is the tab; build(ui) makes it and hands its names back to
the window, which still reaches the tabs through `ui` (state, the
worker, busy, say and the rest) while they are moved onto classes.
"""
import os
import time
import tkinter as tk
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk

import tds_bak
import tds_cal
import tds_fw
from i18n import gettext as _
from tds_fs import DEFAULT_ADDR
from tdstoolkit import (APPDIR, BAK_PANE, human_bytes, load_settings, model_of,
                        save_settings, stamped, tab_names)


class BackupTab(object):
    """The Backup tab: whole-instrument backups and calibration constants."""

    def __init__(self, ui):
        self.ui = ui
        self.EDGE = ui.EDGE
        self.fw_model = ui.fw_model
        self.hints = ui.hints
        self.named = ui.named
        self.relabel = ui.relabel
        self.root = ui.root
        self.says = ui.says
        self.state = ui.state
        self.tabs = ui.tabs
        self.w = ui.w

        # --------------------------------------------------- the backup tab
        # Everything that can be copied off this instrument and put back, in
        # one file. A zip and not a disk image: no command in this firmware
        # reads a sector, so files are the only thing there is to copy - and
        # a zip is a file anybody can open in ten years without this
        # program.
        #
        # Formatting a volume is deliberately not offered. The instrument
        # has its own way of doing that, on its own front panel, where it
        # cannot be reached by a mis-click three feet away.
        self.baktab = ttk.Frame(self.tabs)
        self.tabs.add(self.baktab, text=_("Backup"))
        self.named(self.baktab, "Backup")

        bakleft = ttk.Frame(self.baktab, width=BAK_PANE)
        bakleft.pack(side="left", fill="y", padx=(4, 3), pady=6)
        bakleft.pack_propagate(False)
        bakright = ttk.Frame(self.baktab)
        bakright.pack(side="left", fill="both", expand=True, padx=(3, 4),
                      pady=6)

        # Part name, and what the checkbox beside it says. The floppy starts
        # off because most instruments have nothing in the drive, and a
        # backup that fails one part every time teaches people to ignore the
        # report.
        #
        # No error log. It has a tab of its own that reads it, saves it and
        # clears it, and a second way to save it here would be a second
        # thing to keep in step.
        #: The jobs whose steps and failures belong in the report on the
        #: right. Membership decides it, so verifying a backup - which
        #: writes to the report but submits no job - cannot leave the
        #: next tab's work being narrated into it.
        self.BAK_JOBS = ("bak_make", "bak_put", "cal_read", "cal_write",
                         "bak_nvram", "bak_nvram_put")

        self.BAK_PARTS = (("disk", "Hard disk (hd0:)"),
                          ("floppy", "Floppy disk (fd0:)"),
                          ("setup", "Instrument settings"),
                          ("refs", "Reference waveforms"),
                          ("cal", "Calibration constants"))

        bakbox1 = ttk.LabelFrame(bakleft, padding=8)
        self.says(bakbox1, "What to include")
        bakbox1.pack(fill="x")
        self.bakchecks = {}
        for _key, _english in self.BAK_PARTS:
            # Volumes start unticked: nothing knows a disk or floppy is
            # even there until Refresh has asked. Settings, references and
            # calibration are always readable, so they stay on.
            self.state["bak" + _key] = tk.BooleanVar(
                value=_key not in ("disk", "floppy"))
            _box = ttk.Checkbutton(bakbox1, variable=self.state["bak" + _key],
                                   command=lambda: self.bak_buttons())
            self.says(_box, _english)
            _box.pack(anchor="w")
            self.bakchecks[_key] = _box
        # Explicit calls, not a dict indexed in the loop: the translation
        # audit reads the literal handed to hints(), and only sees it here.
        self.hints(self.bakchecks["disk"],
                   "Include the hard disk (hd0:) in the backup")
        self.hints(self.bakchecks["floppy"],
                   "Include the floppy disk (fd0:) in the backup")
        self.hints(self.bakchecks["setup"],
                   "Include the instrument's current settings")
        self.hints(self.bakchecks["refs"],
                   "Include the stored reference waveforms")
        self.hints(self.bakchecks["cal"], "Include the calibration constants")
        self.lbl_bakfound = ttk.Label(bakbox1, foreground="#555",
                                      justify="left",
                                      wraplength=BAK_PANE - 40)
        self.says(self.lbl_bakfound,
                  "Refresh to see what this instrument has.")
        self.lbl_bakfound.pack(anchor="w", pady=(6, 0))

        ttk.Separator(bakleft, orient="horizontal").pack(fill="x",
                                                         pady=(10, 8))
        # No Folder button. All three of these open a file dialog anyway,
        # and the folder each one starts in is the folder the last one
        # ended in - which is the whole of what choosing one up front was
        # for.
        bakbox2 = ttk.LabelFrame(bakleft, padding=8)
        self.says(bakbox2, "Backup file")
        bakbox2.pack(fill="x")
        bakrow3 = ttk.Frame(bakbox2)
        bakrow3.pack(fill="x")
        self.btn_bakmake = ttk.Button(bakrow3, text=_("Back up..."),
                                      padding=(10, 2),
                                      command=lambda: self.do_bak_make())
        self.btn_bakmake.pack(side="left")
        self.says(self.btn_bakmake, "Back up...")
        self.btn_bakput = ttk.Button(bakrow3, text=_("Restore..."),
                                     padding=(10, 2),
                                     command=lambda: self.do_bak_put())
        self.btn_bakput.pack(side="left", padx=4)
        self.says(self.btn_bakput, "Restore...")
        self.btn_bakcheck = ttk.Button(bakrow3, text=_("Verify..."),
                                       padding=(10, 2),
                                       command=lambda: self.do_bak_check())
        self.btn_bakcheck.pack(side="left")
        self.says(self.btn_bakcheck, "Verify...")
        self.btn_bakscan = ttk.Button(bakbox2, text=_("Refresh"),
                                      padding=(10, 2),
                                      command=lambda: self.do_bak_refresh())
        self.btn_bakscan.pack(anchor="w", pady=(8, 0))
        self.says(self.btn_bakscan, "Refresh")

        # ---- calibration constants
        # Here rather than on the System tab, where these two used to be:
        # this is the tab about copying things off the instrument and
        # putting them back, and that is what they do. Kept as their own
        # pair of buttons rather than folded into the backup above, because
        # restoring them needs the NVRAM protection switch moved and a
        # confirmation of its own - and something that needs the cabinet
        # opened should not be reachable by a tick in a list.
        ttk.Separator(bakleft, orient="horizontal").pack(fill="x",
                                                         pady=(10, 8))
        bakbox3 = ttk.LabelFrame(bakleft, padding=8)
        self.says(bakbox3, "Calibration constants")
        bakbox3.pack(fill="x")
        lbl_bakcal = ttk.Label(bakbox3, foreground="#555", justify="left",
                               wraplength=BAK_PANE - 40)
        self.says(lbl_bakcal,
                  "The acquisition board's calibration EEPROMs, read over the "
                  "bus while the instrument runs normally. Back these up "
                  "before any service work. Restoring needs the NVRAM "
                  "protection switch set to unprotected, without rebooting "
                  "the scope.")
        lbl_bakcal.pack(anchor="w")
        bakrow4 = ttk.Frame(bakbox3)
        bakrow4.pack(fill="x", pady=(6, 0))
        self.btn_bakcalget = ttk.Button(bakrow4, text=_("Back up..."),
                                        padding=(10, 2),
                                        command=lambda: self.do_bak_cal_get())
        self.btn_bakcalget.pack(side="left")
        self.says(self.btn_bakcalget, "Back up...")
        self.btn_bakcalput = ttk.Button(bakrow4, text=_("Restore..."),
                                        padding=(10, 2),
                                        command=lambda: self.do_bak_cal_put())
        self.btn_bakcalput.pack(side="left", padx=4)
        self.says(self.btn_bakcalput, "Restore...")
        # No status line under these two either, for the reason the NVRAM
        # box below has none: this column is narrow and everything worth
        # saying goes to "What happened" on the right, where it is one
        # running account rather than two that have to be read together.

        # ---- NVRAM
        # Not a tick in the list above, because the instrument cannot be in
        # two states at once. Everything in that list is read from an
        # instrument running its firmware; NVRAM can only be read through
        # the bootloader monitor, which means the protection switch moved
        # and the instrument restarted into something that answers at 29 and
        # has no filesystem, no settings and no librarian. A tick that can
        # never be combined with any other tick is not a tick.
        #
        # Restoring is a write with no way back - static RAM with a battery
        # behind it, so nothing is erased and nothing can be un-written -
        # which is why what is there now is read and saved before a byte
        # goes in, why the file's own checksums are added up first, and why
        # the confirmation says out loud what a dump from a different
        # instrument does to this one's calibration.
        ttk.Separator(bakleft, orient="horizontal").pack(fill="x",
                                                         pady=(10, 8))
        bakbox4 = ttk.LabelFrame(bakleft, padding=8)
        self.says(bakbox4, "NVRAM")
        bakbox4.pack(fill="x")
        lbl_baknv = ttk.Label(bakbox4, foreground="#555", justify="left",
                              wraplength=BAK_PANE - 40)
        self.says(lbl_baknv,
                  "User settings, saved waveforms, limits and masks, and on "
                  "the earlier instruments the calibration constants too. "
                  "Reading it needs the NVRAM protection switch set to "
                  "unprotected and the instrument restarted into its "
                  "bootloader - it is not read over the ordinary bus and "
                  "nothing else here is read at the same time. Takes a few "
                  "minutes.")
        lbl_baknv.pack(anchor="w")
        bakrow5 = ttk.Frame(bakbox4)
        bakrow5.pack(fill="x", pady=(6, 0))
        self.btn_baknv = ttk.Button(bakrow5, text=_("Back up..."),
                                    padding=(10, 2),
                                    command=lambda: self.do_bak_nvram())
        self.btn_baknv.pack(side="left")
        self.says(self.btn_baknv, "Back up...")
        self.btn_baknvput = ttk.Button(bakrow5, text=_("Restore..."),
                                       padding=(10, 2),
                                       command=lambda: self.do_bak_nvram_put())
        self.btn_baknvput.pack(side="left", padx=4)
        self.says(self.btn_baknvput, "Restore...")
        # No status line under these two buttons. Everything it said - the
        # checksum report and the result - is written to "What happened" on
        # the right as well, and the left column is narrow enough that the
        # duplicate was being cut off. The calibration box above keeps its
        # own line, because what that one says goes nowhere else.

        # ---- what happened
        bakbox5 = ttk.LabelFrame(bakright, padding=8)
        self.says(bakbox5, "What happened")
        bakbox5.pack(fill="both", expand=True)
        bakview = ttk.Frame(bakbox5)
        bakview.pack(fill="both", expand=True)
        # Wrapped on words, not scrolled sideways. What goes in here is
        # sentences rather than columns - a filename, a step, a checksum
        # read out in full - and a horizontal scrollbar hides the end of a
        # sentence behind a gesture nobody makes while watching an
        # operation run.
        self.baktxt = tk.Text(bakview, wrap="word", height=10, undo=False,
                              font="TkFixedFont", background="#ffffff",
                              highlightthickness=1,
                              highlightbackground=self.EDGE)
        bakvsb = ttk.Scrollbar(bakview, orient="vertical",
                               command=self.baktxt.yview)
        self.baktxt.configure(yscrollcommand=bakvsb.set)
        bakvsb.pack(side="right", fill="y")
        self.baktxt.pack(side="left", fill="both", expand=True)
        # Read-only the same way the error log is: selectable and copyable,
        # which a disabled Text is not.
        self.baktxt.bind("<Key>",
                         lambda e: (None if (e.state & 4) or e.keysym in
                                    ("Left", "Right", "Up", "Down",
                                     "Home",
                                     "End", "Prior",
                                     "Next") else "break"))

        self.relabel.append(self.bak_relabel)
        self.state["bakfolder"] = load_settings().get("backups") or ""
        self.bak_buttons()

    # ------------------------------------------------ the tab's own logic
    def bak_note(self, *lines):
        """Add to the report, and leave it looking at the newest line."""
        self.baktxt.insert("end", "\n".join(lines) + "\n")
        self.baktxt.see("end")

    def bak_begin(self):
        """Separate this run's account from the one before it.

        One run's steps run into the next otherwise, and the report is
        read after the fact as much as during. The separator goes in at
        the start rather than at the end because a run has one
        beginning and several ways of finishing.
        """
        if self.baktxt.get("1.0", "end").strip():
            self.baktxt.insert("end", "\n")
        self.state["bakstep"] = None

    def bak_available(self):
        """Which parts this instrument was found to have.

        Everything, until a refresh says otherwise: a tab that greys
        every choice before it has asked reads as broken rather than as
        cautious.
        """
        return self.state.get("bakhas") or dict((k, True) for k,
                                                _s in self.BAK_PARTS)

    def bak_wants(self):
        """The ticked parts that this instrument actually has."""
        have = self.bak_available()
        return dict((k,
                     bool(self.state["bak" + k].get()) and bool(have.get(k)))
                    for k, _s in self.BAK_PARTS)

    def bak_buttons(self):
        have = self.bak_available()
        for key, _english in self.BAK_PARTS:
            self.bakchecks[key].config(state="normal" if have.get(key)
                                       else "disabled")
        ready = any(self.bak_wants().values()) and not self.state.get("busy")
        self.btn_bakmake.config(state="normal" if ready else "disabled")

    def bak_remember(self, path):
        """Start the next dialog where this one finished."""
        settings = load_settings()
        settings["backups"] = os.path.dirname(path)
        save_settings(settings)
        self.state["bakfolder"] = os.path.dirname(path)

    def do_bak_refresh(self):
        self.ui.busy(True, "wait")
        self.ui.say(_("Asking the instrument what it has ..."))
        self.w.submit("bak_survey", lambda k: k.bak_survey())

    def bak_show_survey(self, got):
        """What the instrument answered, as ticks that mean something."""
        volumes = [v.lower() for v in got.get("volumes") or []]
        # Not "bakrefs": that is the tick box's own variable, one of
        # those named after a part. The names of the references that
        # hold something are a different thing and need a different key.
        self.state["bakreflist"] = got.get("refs") or []
        self.state["bakhas"] = {
            "disk": "hd0:" in volumes, "floppy": "fd0:" in volumes,
            "setup": True, "refs": bool(self.state["bakreflist"]),
            "cal": True}
        for key, has in self.state["bakhas"].items():
            if key in ("disk", "floppy"):
                # A volume follows what the refresh found: ticked when it
                # is there, cleared when it is not.
                self.state["bak" + key].set(bool(has))
            elif not has:
                self.state["bak" + key].set(False)
        self.lbl_bakfound.config(text=_(
            "%(volumes)s. %(refs)d reference(s) hold a waveform.")
            % {"volumes": ", ".join(got.get("volumes") or [])
               or _("no volumes"),
               "refs": len(self.state["bakreflist"])})
        # The same sentence into the report, with what answered it. The
        # report is the record of the session, and what the instrument
        # was when it was asked is the first line of it.
        self.bak_note(*[t for t in (got.get("idn"),
                                    self.lbl_bakfound.cget("text"))
                        if t])
        self.bak_buttons()

    def do_bak_make(self):
        if self.state["busy"]:
            return
        want = self.bak_wants()
        name = stamped((model_of(self.state.get("idn")) or "TDS").replace(" ",
                                                                          ""),
                       ".zip")
        path = filedialog.asksaveasfilename(
            parent=self.root, title=_("Where to save the backup"),
            initialdir=self.state.get("bakfolder") or APPDIR, initialfile=name,
            defaultextension=".zip",
            filetypes=[(_("Backup file"), "*.zip")])
        if not path:
            return
        self.bak_remember(path)
        self.baktxt.delete("1.0", "end")
        self.bak_begin()
        self.bak_note(_("Backing up to %s") % os.path.basename(path))
        self.ui.busy(True, "steps")
        self.ui.say(_("Backing up the instrument ..."))
        self.w.submit("bak_make", lambda k: k.bak_make(
            {"path": path, "parts": want,
             "refs": self.state.get("bakreflist") or []}))

    def bak_pick_file(self, title):
        """Choose a backup and check it before anything is done with it."""
        path = filedialog.askopenfilename(
            parent=self.root, title=title,
            initialdir=self.state.get("bakfolder") or APPDIR,
            filetypes=[(_("Backup file"), "*.zip"), (_("All files"), "*.*")])
        if not path:
            return None, None
        self.bak_remember(path)
        try:
            checked = tds_bak.verify(path)
        except Exception as exc:
            messagebox.showerror(_("Error"), str(exc))
            return None, None
        return path, checked

    def bak_report(self, path, checked):
        """Say what a backup holds and whether it still reads correctly."""
        manifest = checked["manifest"]
        self.bak_begin()
        self.bak_note(os.path.basename(path),
                      _("Made %(when)s from %(idn)s")
                      % {"when": manifest.get("made") or "?",
                         "idn": manifest.get("idn") or "?"},
                      _("%(files)d file(s) checked, %(bad)d wrong, %(gone)d "
                        "missing") % {"files": checked["checked"],
                                      "bad": len(checked["bad"]),
                                      "gone": len(checked["missing"])})
        for arc in (checked["bad"] + checked["missing"])[:20]:
            self.bak_note("    " + arc)
        holds = tds_bak.holds(manifest)
        self.bak_note("    " + ", ".join("%s (%d)" % (k, n)
                                         for k, n in sorted(holds.items())))
        return holds

    def do_bak_check(self):
        path, checked = self.bak_pick_file(_("Which backup to verify"))
        if not path:
            return
        self.bak_report(path, checked)
        self.ui.say(_("The backup is complete and every file matches")
                    if checked["ok"] else _("The backup does not match "
                                            "itself"))

    def do_bak_put(self):
        """Put parts of a backup back, after saying exactly which."""
        path, checked = self.bak_pick_file(_("Which backup to restore"))
        if not path:
            return
        holds = self.bak_report(path, checked)
        if not checked["ok"] and not messagebox.askyesno(
                _("Restore anyway?"),
                _("This backup does not match its own checksums. Whatever "
                  "is wrong with it would be written to the instrument. "
                  "Proceed?"), icon="warning", default="no"):
            return
        # Calibration is never part of this. It has its own button, its
        # own warning and its own switch to move first.
        want = dict((k, bool(self.state["bak" + k].get()) and k in holds)
                    for k, _s in self.BAK_PARTS if k != "cal")
        picked = [k for k, on in want.items() if on]
        if not picked:
            messagebox.showinfo(
                _("Nothing to restore"),
                _("Tick what to restore on the left. This backup holds "
                  "%s.") % ", ".join(sorted(holds)))
            return
        if not messagebox.askyesno(_("Restore to the instrument?"), _(
                "This writes %(parts)s from\n\n%(file)s\n\nto the "
                "instrument, replacing what is there now. Files of the "
                "same name are overwritten and there is no undo.\n\n"
                "Proceed?") % {"parts": ", ".join(sorted(picked)),
                               "file": os.path.basename(path)},
                icon="warning", default="no"):
            return
        self.ui.busy(True, "steps")
        self.ui.say(_("Restoring to the instrument ..."))
        self.w.submit("bak_put", lambda k: k.bak_put({"path": path,
                                                      "parts": want}))

    def cal_running(self):
        """Is the instrument in the state the constants can be reached in?

        Both calibration buttons need the firmware running: the words go
        through WORDCONSTANT, which the firmware implements and the
        bootloader does not. That is the opposite of the NVRAM beside
        them, and it is the one thing about this box worth being told
        rather than left to infer from "not connected".
        """
        if self.w.fs is not None:
            return True
        messagebox.showinfo(_("The instrument is not answering"), _(
            "The calibration constants are read and written while the "
            "instrument runs normally, so it has to be connected.\n\n"
            "Unlike the NVRAM, this is not done from the bootloader: "
            "set the NVRAM protection switch to unprotected with the "
            "instrument switched on, without rebooting it, and connect "
            "as usual."))
        return False

    def do_bak_cal_get(self):
        """Ask where they go, then read them.

        Asked first, not last. The read is 504 queries and takes the
        better part of a minute; a save dialog that appears at the end
        of it is one nobody is still sitting in front of, and a read
        nobody answers for is a read thrown away.
        """
        if not self.cal_running():
            return
        settings = load_settings()
        name = tds_cal.name((model_of(self.state.get("idn")) or "TDS")
                            .replace(" ", ""),
                            time.strftime("%Y%m%d-%H%M%S"))
        path = filedialog.asksaveasfilename(
            parent=self.root, initialfile=name,
            title=_("Where to save the calibration constants"),
            initialdir=settings.get("cal_backups") or APPDIR,
            defaultextension=".bin",
            filetypes=[(_("Calibration backup"), "*.bin")])
        if not path:
            return
        settings["cal_backups"] = os.path.dirname(path)
        save_settings(settings)
        self.state["calpath"] = path
        self.ui.busy(True, "steps")
        self.ui.say(_("Reading the calibration constants ..."))
        self.bak_begin()
        self.bak_note(_("Reading the calibration constants ..."))
        self.w.submit("cal_read", lambda k: k.cal_read())

    def do_bak_nvram(self):
        """Read the NVRAM through the bootloader monitor and save it."""
        settings = load_settings()
        # A save dialog with the name already in it, the way the
        # calibration backup asks. Choosing a folder and being told
        # nothing about what would land in it left the person to guess
        # both the name and whether anything had happened.
        where = filedialog.asksaveasfilename(
            parent=self.root, title=_("Where to put the NVRAM backup"),
            initialfile="%s_NVRAM_%s.bin"
            % ((self.fw_model() or model_of(self.state.get("idn")) or "TDS")
               .replace(" ", ""),
               time.strftime("%Y%m%d-%H%M%S")),
            initialdir=self.state.get("bakfolder") or settings.get("backups")
            or APPDIR,
            defaultextension=".bin",
            filetypes=[(_("NVRAM backup"), "*.bin")])
        if not where:
            return
        if not messagebox.askyesno(_("Back up the NVRAM?"), _(
                "This reads the NVRAM through the bootloader and writes "
                "it into\n\n%(where)s\n\nThe instrument has to be in its "
                "bootloader for this: NVRAM protection switch set to "
                "unprotected, then switched off and on again. Nothing is "
                "written to the instrument and it takes a few minutes."
                "\n\nProceed?") % {"where": where},
                default="yes"):
            return
        folder = os.path.dirname(where)
        self.state["bakfolder"] = folder
        settings["backups"] = folder
        save_settings(settings)
        self.ui.busy(True, "steps")
        self.ui.say(_("Reading the NVRAM ..."))
        self.bak_begin()
        self.bak_note(_("Reading the NVRAM through the bootloader ..."))
        self.w.submit("bak_nvram", lambda k: k.fw_nvram({
            "resource": tds_fw.bootloader_resource(self.state.get("addr")
                                                   or DEFAULT_ADDR),
            "normal": self.state.get("addr") or DEFAULT_ADDR,
            "backup_dir": folder, "check_backup": True,
            # The name the person just typed, rather than one built from
            # a model the program may not know - it cannot connect to an
            # instrument that is in its bootloader, so fw_model() is
            # often empty exactly when this runs.
            "names": {"NVRAM": os.path.basename(where)},
            "model": self.fw_model() or "TDS",
            "stamp": time.strftime("%Y%m%d-%H%M%S")}), needs_fs=False)

    def do_bak_nvram_put(self):
        """Write a saved NVRAM back, after adding up what is in it."""
        settings = load_settings()
        path = filedialog.askopenfilename(
            parent=self.root, title=_("Which NVRAM backup to restore"),
            initialdir=self.state.get("bakfolder") or settings.get("backups")
            or APPDIR,
            filetypes=[(_("NVRAM backup"), "*.bin"), (_("All files"), "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            messagebox.showerror(_("Error"), str(exc))
            return
        # A file the window cannot hold is not an NVRAM dump, whatever
        # it is called. Shorter is allowed and normal: the tools that
        # came before this one read 0xA0000, which is what is really
        # fitted, and an A-series carries only 0x80000.
        if not data or len(data) > tds_fw.NVRAM_LEN or len(data) % 2:
            messagebox.showerror(_("Error"), _(
                "%(file)s is %(size)s. An NVRAM backup is a whole number "
                "of words and no more than %(most)s, which is all the "
                "bootloader answers for.")
                % {"file": os.path.basename(path),
                   "size": human_bytes(len(data)),
                   "most": human_bytes(tds_fw.NVRAM_LEN)})
            return
        # What the instrument's own firmware checksums, added up here
        # before any of it is written. This is the one check that can
        # tell a good dump from a damaged one, and the tools this came
        # from say to always run it.
        told = tds_fw.nvram_check(data)
        lines = []
        if told["proto"]:
            lines.append(_("%(file)s: %(proto)s, all %(n)d section "
                           "checksums valid")
                         % {"file": os.path.basename(path),
                            "proto": told["proto"], "n": told["of"]})
        elif told.get("nearest"):
            lines.append(_("%(file)s: nearest is %(proto)s, %(ok)d of "
                           "%(n)d section checksums valid")
                         % {"file": os.path.basename(path),
                            "proto": told["nearest"],
                            "ok": told["matched"], "n": told["of"]})
        else:
            lines.append(_("%s: too short to hold any known section")
                         % os.path.basename(path))
        for what, at, want, got in told["bad"]:
            lines.append(_("    %(what)s at 0x%(at)X: holds 0x%(want)04X, "
                           "adds up to 0x%(got)04X")
                         % {"what": what, "at": at, "want": want, "got": got})
        if told["zero"]:
            lines.append(_("    %d section(s) check as zero, which is also "
                           "what an empty one looks like")
                         % len(told["zero"]))
        self.bak_begin()
        self.bak_note(*lines)
        if not told["proto"] and not messagebox.askyesno(
                _("This backup does not add up"), _(
                    "%(file)s does not match any firmware this program "
                    "knows the checksums for.\n\nThat can mean the file is "
                    "damaged. It can also mean the instrument it came from "
                    "is one nobody has recorded the sections for - a "
                    "TDS640A is such a one, and its dumps are perfectly "
                    "good.\n\nWhat the check found is in the report on the "
                    "right.\n\nRestore it anyway?")
                % {"file": os.path.basename(path)},
                icon="warning", default="no"):
            return
        if not messagebox.askyesno(_("Restore the NVRAM?"), _(
                "This writes %(size)s from\n\n%(file)s\n\ninto the "
                "instrument's NVRAM.\n\nNothing is kept first: if you "
                "have not already taken a backup with the button beside "
                "this one, there is no way back.\n\n"
                "If this backup came from a different instrument, that "
                "instrument's settings, saved waveforms, limits and "
                "masks go in with it. The clock is not written.\n\nThe "
                "NVRAM protection switch has to be set "
                "to unprotected and the instrument restarted into its "
                "bootloader.\n\nProceed?")
                % {"size": human_bytes(len(data)),
                   "file": os.path.basename(path)},
                icon="warning", default="no"):
            return
        self.state["bakfolder"] = os.path.dirname(path)
        self.ui.busy(True, "steps")
        self.ui.say(_("Restoring the NVRAM ..."))
        self.w.submit("bak_nvram_put", lambda k, d=data: k.fw_nvram_put({
            "path": path, "data": d,
            "resource": tds_fw.bootloader_resource(self.state.get("addr")
                                                   or DEFAULT_ADDR),
            "normal": self.state.get("addr") or DEFAULT_ADDR,
            "backup_dir": os.path.dirname(path),
            "model": self.fw_model() or "TDS",
            "stamp": time.strftime("%Y%m%d-%H%M%S")}), needs_fs=False)

    def do_bak_cal_put(self):
        """Put a saved set back, after reading what is there now.

        Refused up front unless the instrument is answering normally.
        The constants are read and written with WORDCONSTANT, which the
        firmware implements - so unlike the NVRAM, this one cannot be
        done from the bootloader at all, and an instrument sitting in
        its bootloader answers nothing here. Saying "not connected"
        sends somebody to look at the cable.
        """
        if not self.cal_running():
            return
        settings = load_settings()
        path = filedialog.askopenfilename(
            parent=self.root, title=_("Which calibration backup to restore"),
            initialdir=settings.get("cal_backups") or APPDIR,
            filetypes=[(_("Calibration backup"), "*.bin"),
                       (_("All files"), "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            tds_cal.words(data)             # raises if it is not one
        except Exception as exc:
            messagebox.showerror(_("Error"), str(exc))
            return
        # Word 0 is the sum of the 251 words after it. A file that
        # disagrees with its own checksum is damaged, and writing it
        # onto a working acquisition board is the one mistake here with
        # no way back - so it is asked about on its own, before the
        # ordinary confirmation, rather than as a line inside it.
        if not tds_cal.sound(data) and not messagebox.askyesno(
                _("This file is damaged"),
                _("%(file)s does not agree with its own checksum. Word "
                  "0 should be the sum of the 251 words after it and it "
                  "is not, so some of what is in the file is not what "
                  "was read off the instrument.\n\nWriting it would "
                  "leave the acquisition board holding that damage.\n\n"
                  "Restore it anyway?") % {"file": os.path.basename(path)},
                icon="warning", default="no"):
            return
        # The file names the model it came off (tds_cal.name), and the
        # constants belong to one acquisition board. Another model's go
        # in all the same, if asked twice, because a converted instrument
        # may well be carrying a backup under its old name.
        theirs = os.path.basename(path).split("_")[0].upper()
        mine = model_of(self.state.get("idn")).replace(" ", "").upper()
        if theirs and mine and "_CAL_" in os.path.basename(path).upper() \
                and theirs != mine and not messagebox.askyesno(
                    _("Different model"),
                    _("%(file)s was taken from a %(theirs)s and this "
                      "instrument says it is a %(mine)s. Calibration "
                      "constants belong to one acquisition board, and "
                      "another board's will leave this instrument "
                      "measuring wrongly.\n\nRestore it anyway?")
                    % {"file": os.path.basename(path), "theirs": theirs,
                       "mine": mine},
                    icon="warning", default="no"):
            return
        if not messagebox.askyesno(_("Restore calibration constants?"), _(
                "This writes the calibration constants in\n\n%(file)s"
                "\n\nto the instrument's acquisition board.\n\nNothing "
                "is kept first: if you have not already taken a backup "
                "with the button beside this one, there is no way "
                "back.\n\n"
                "The NVRAM protection switch has to be set to "
                "unprotected, or the words are taken into a "
                "working copy and never stored - which reads back "
                "as a success until the instrument is switched "
                "off.\n\nThe instrument's calibration is "
                "what this changes. Proceed?")
                % {"file": os.path.basename(path)},
                icon="warning", default="no"):
            return
        self.ui.busy(True, "steps")
        self.ui.say(_("Restoring the calibration constants ..."))
        self.bak_begin()
        self.w.submit("cal_write", lambda k: k.cal_write(
            {"data": data, "from": path}))

    def bak_relabel(self):
        """The line this tab writes for itself, said again."""
        if self.state.get("bakhas") is None:
            self.lbl_bakfound.config(
                text=_("Refresh to see what this instrument has."))

    # The pump's handlers for this tab's jobs - see pump_jobs.
    def pump_cal_read(self, payload):
        self.ui.busy(False)
        # Where it goes was settled before the read started,
        # so there is nothing to ask here - only to write.
        path = self.ui.keep_file(self.state.get("calpath"), payload["data"])
        if not path:
            return
        lines = [_("Saved to %s") % os.path.basename(path)]
        # Shown the way the NVRAM's sections are, and named
        # for what it is: the calibration block carries one
        # check word in word 0, not a word per section.
        lines.append(_(
            "    Calibration checksum: word 0 holds "
            "0x%(want)04X, the 251 words after it add up to "
            "0x%(got)04X")
            % {"want": payload.get("check", 0),
               "got": payload.get("sum", 0)})
        if not payload.get("sound", True):
            # Word 0 is the sum of the 251 words after it.
            # A block that disagrees with its own checksum
            # is damaged, and the file just written is a
            # copy of the damage - worth knowing before it
            # is relied on rather than while restoring it.
            lines.append(_(
                "The block does not agree with its own "
                "checksum, so what has been saved is "
                "already damaged. Keep it as evidence, but "
                "do not restore it to a working "
                "instrument."))
        if payload["flat"]:
            # Not proof of anything, but the one thing worth
            # looking at: the librarian's address is right
            # for the families the reference tool names and
            # an assumption everywhere else.
            lines.append(_(
                "Every word read back the same value, which "
                "calibration data is not. The address may be "
                "wrong for this model."))
        self.bak_note(*lines)
        self.ui.say(_("Calibration constants saved"))

    def pump_cal_write(self, payload):
        self.ui.busy(False)
        lines = []
        if payload.get("stuck"):
            lines.append(_(
                "Word %(word)d would not take %(wanted)d - "
                "it reads %(got)d. Stopped there.")
                % payload["stuck"])
        lines.append(_("%(wrote)d of %(words)d word(s) "
                       "written and read back.") % payload)
        # Transferred is not stored. The words go into a
        # working copy in the instrument's RAM and reading
        # them back proves only that they arrived;
        # CALIBRATE:STORE? is what commits the copy to the
        # acquisition board, and its answer is the only
        # thing that says the constants will still be there
        # after the next power cycle.
        if payload.get("stored") is True:
            lines.append(_(
                "Stored on the acquisition board. The "
                "instrument answered 0, which is what it "
                "answers when the constants have been "
                "committed."))
        elif payload.get("stored") is False:
            lines.append(_(
                "NOT stored. The words were sent and read "
                "back, but the instrument answered %d to "
                "the command that commits them, so nothing "
                "was kept: at the next power cycle this "
                "instrument will hold what it held before. "
                "The NVRAM protection switch is what stops "
                "the commit - set it to unprotected, with "
                "the instrument switched on and without "
                "rebooting it, and restore again.")
                % payload.get("store_code", 0))
        elif "stored" in payload:
            lines.append(_(
                "Whether they were stored is not known: the "
                "command that commits them answered "
                "%(said)s. The words were sent and read "
                "back. Power the instrument off and on and "
                "back the constants up again to see what it "
                "kept.") % {"said": payload.get("store_said")
                            or _("nothing")})
        if "sum" in payload:
            lines.append(_(
                "    Calibration checksum: word 0 holds "
                "0x%(want)04X, the 251 words after it add up to "
                "0x%(got)04X")
                % {"want": payload["check"],
                   "got": payload["sum"]})
        self.bak_note(*lines)
        self.ui.say(_("The instrument would not take every "
                      "calibration word")
                    if payload.get("stuck") else
                    _("Calibration constants restored and stored")
                    if payload.get("stored") is True else
                    _("The constants were sent but the instrument "
                      "did not store them")
                    if payload.get("stored") is False
                    else _("Calibration constants sent"))

    def pump_bak_survey(self, payload):
        self.ui.busy(False)
        self.bak_show_survey(payload)
        self.ui.say(_("The instrument answered"))

    def pump_bak_nvram_put(self, payload):
        self.ui.busy(False)
        self.bak_note(_("Restored %(size)s of NVRAM from %(file)s")
                      % {"size": human_bytes(payload["wrote"]),
                         "file": os.path.basename(payload["path"])},
                      _("    read back and identical"))
        # Said whenever the two differ, because otherwise
        # the line above reports 640 kB restored from a
        # 1 MB file and reads like a write that stopped
        # short. It did not: the rest of the window is the
        # same memory answering a second time.
        if payload.get("held", 0) > payload.get("distinct", 0):
            self.bak_note(_(
                "    the file is %(held)s and this instrument "
                "holds %(real)s of distinct memory - the rest "
                "of the window is the same memory answering "
                "again, and is not written twice")
                          % {"held": human_bytes(payload["held"]),
                             "real": human_bytes(payload["distinct"])})
        self.ui.say(_("NVRAM restored"))

    def pump_bak_nvram(self, payload):
        self.ui.busy(False)
        for one in payload["backups"]:
            self.bak_note(_("%(what)s: %(size)s to %(file)s")
                          % {"what": one["what"],
                             "size": human_bytes(one["bytes"]),
                             "file": os.path.basename(one["path"])},
                          "    " + one["sha"])
        for one in payload.get("ticked") or []:
            self.bak_note(_("The clock moved while it was read, in "
                            "%d byte(s). That is the instrument "
                            "keeping time, not a fault.")
                          % one["bytes"])
        self.ui.say(_("NVRAM backed up"))

    def pump_bak_make(self, payload):
        self.ui.busy(False)
        self.bak_note(_("%(files)d file(s), %(size)s")
                      % {"files": len(payload["manifest"]["files"]),
                         "size": human_bytes(payload["bytes"])},
                      "    " + ", ".join(
                     "%s (%d)" % (k, n) for k, n
                     in sorted(payload["holds"].items())))
        for name, why in payload["failed"]:
            self.bak_note(_("%(part)s: %(why)s")
                          % {"part": name, "why": why})
        self.ui.say(_("Backed up to %s")
                    % os.path.basename(payload["path"])
                    if not payload["failed"]
                    else _("Backed up, with %d part(s) missing")
                    % len(payload["failed"]))

    def pump_bak_put(self, payload):
        self.ui.busy(False)
        for name, what in payload["done"]:
            self.bak_note(_("%(part)s: %(what)s restored")
                          % {"part": name, "what": what})
        for name, why in payload["failed"]:
            self.bak_note(_("%(part)s: %(why)s")
                          % {"part": name, "why": why})
        self.ui.say(_("Restored to the instrument")
                    if not payload["failed"]
                    else _("Restored, with %d part(s) that failed")
                    % len(payload["failed"]))


def build(ui):
    """Make the tab, and hand its names to the window as before."""
    tab = BackupTab(ui)
    vars(ui).update(tab_names(tab))
    return tab
