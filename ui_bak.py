"""The Backup tab.

Built by run_gui through build(ui). `ui` carries the window's shared
names - state, the worker, busy, say and the rest - and is handed back
everything built here, so run_gui and the tests can reach it.
"""
import os
import time

import tds_bak
import tds_cal
import tds_fw
from i18n import gettext as _
from tds_fs import DEFAULT_ADDR
from tdstoolkit import (APPDIR, BAK_PANE, human_bytes, load_settings, model_of,
                        save_settings, stamped)


def build(ui):
    EDGE = ui.EDGE
    filedialog = ui.filedialog
    fw_model = ui.fw_model
    hints = ui.hints
    messagebox = ui.messagebox
    named = ui.named
    relabel = ui.relabel
    root = ui.root
    says = ui.says
    state = ui.state
    tabs = ui.tabs
    tk = ui.tk
    ttk = ui.ttk
    w = ui.w

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
    baktab = ttk.Frame(tabs)
    tabs.add(baktab, text=_("Backup"))
    named(baktab, "Backup")

    bakleft = ttk.Frame(baktab, width=BAK_PANE)
    bakleft.pack(side="left", fill="y", padx=(4, 3), pady=6)
    bakleft.pack_propagate(False)
    bakright = ttk.Frame(baktab)
    bakright.pack(side="left", fill="both", expand=True, padx=(3, 4), pady=6)

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
    BAK_JOBS = ("bak_make", "bak_put", "cal_read", "cal_write",
                "bak_nvram", "bak_nvram_put")

    BAK_PARTS = (("disk", "Hard disk (hd0:)"),
                 ("floppy", "Floppy disk (fd0:)"),
                 ("setup", "Instrument settings"),
                 ("refs", "Reference waveforms"),
                 ("cal", "Calibration constants"))

    bakbox1 = ttk.LabelFrame(bakleft, padding=8)
    says(bakbox1, "What to include")
    bakbox1.pack(fill="x")
    bakchecks = {}
    for _key, _english in BAK_PARTS:
        # Volumes start unticked: nothing knows a disk or floppy is
        # even there until Refresh has asked. Settings, references and
        # calibration are always readable, so they stay on.
        state["bak" + _key] = tk.BooleanVar(
            value=_key not in ("disk", "floppy"))
        _box = ttk.Checkbutton(bakbox1, variable=state["bak" + _key],
                               command=lambda: bak_buttons())
        says(_box, _english)
        _box.pack(anchor="w")
        bakchecks[_key] = _box
    # Explicit calls, not a dict indexed in the loop: the translation
    # audit reads the literal handed to hints(), and only sees it here.
    hints(bakchecks["disk"], "Include the hard disk (hd0:) in the backup")
    hints(bakchecks["floppy"],
          "Include the floppy disk (fd0:) in the backup")
    hints(bakchecks["setup"], "Include the instrument's current settings")
    hints(bakchecks["refs"], "Include the stored reference waveforms")
    hints(bakchecks["cal"], "Include the calibration constants")
    lbl_bakfound = ttk.Label(bakbox1, foreground="#555", justify="left",
                             wraplength=BAK_PANE - 40)
    says(lbl_bakfound, "Refresh to see what this instrument has.")
    lbl_bakfound.pack(anchor="w", pady=(6, 0))

    ttk.Separator(bakleft, orient="horizontal").pack(fill="x", pady=(10, 8))
    # No Folder button. All three of these open a file dialog anyway,
    # and the folder each one starts in is the folder the last one
    # ended in - which is the whole of what choosing one up front was
    # for.
    bakbox2 = ttk.LabelFrame(bakleft, padding=8)
    says(bakbox2, "Backup file")
    bakbox2.pack(fill="x")
    bakrow3 = ttk.Frame(bakbox2)
    bakrow3.pack(fill="x")
    btn_bakmake = ttk.Button(bakrow3, text=_("Back up..."), padding=(10, 2),
                             command=lambda: do_bak_make())
    btn_bakmake.pack(side="left")
    says(btn_bakmake, "Back up...")
    btn_bakput = ttk.Button(bakrow3, text=_("Restore..."), padding=(10, 2),
                            command=lambda: do_bak_put())
    btn_bakput.pack(side="left", padx=4)
    says(btn_bakput, "Restore...")
    btn_bakcheck = ttk.Button(bakrow3, text=_("Verify..."), padding=(10, 2),
                              command=lambda: do_bak_check())
    btn_bakcheck.pack(side="left")
    says(btn_bakcheck, "Verify...")
    btn_bakscan = ttk.Button(bakbox2, text=_("Refresh"), padding=(10, 2),
                             command=lambda: do_bak_refresh())
    btn_bakscan.pack(anchor="w", pady=(8, 0))
    says(btn_bakscan, "Refresh")

    # ---- calibration constants
    # Here rather than on the System tab, where these two used to be:
    # this is the tab about copying things off the instrument and
    # putting them back, and that is what they do. Kept as their own
    # pair of buttons rather than folded into the backup above, because
    # restoring them needs the NVRAM protection switch moved and a
    # confirmation of its own - and something that needs the cabinet
    # opened should not be reachable by a tick in a list.
    ttk.Separator(bakleft, orient="horizontal").pack(fill="x", pady=(10, 8))
    bakbox3 = ttk.LabelFrame(bakleft, padding=8)
    says(bakbox3, "Calibration constants")
    bakbox3.pack(fill="x")
    lbl_bakcal = ttk.Label(bakbox3, foreground="#555", justify="left",
                           wraplength=BAK_PANE - 40)
    says(lbl_bakcal,
         "The acquisition board's calibration EEPROMs, read over the bus "
         "while the instrument runs normally. Back these up before any "
         "service work. Restoring needs the NVRAM protection switch set "
         "to unprotected, without rebooting the scope.")
    lbl_bakcal.pack(anchor="w")
    bakrow4 = ttk.Frame(bakbox3)
    bakrow4.pack(fill="x", pady=(6, 0))
    btn_bakcalget = ttk.Button(bakrow4, text=_("Back up..."),
                               padding=(10, 2),
                               command=lambda: do_bak_cal_get())
    btn_bakcalget.pack(side="left")
    says(btn_bakcalget, "Back up...")
    btn_bakcalput = ttk.Button(bakrow4, text=_("Restore..."),
                               padding=(10, 2),
                               command=lambda: do_bak_cal_put())
    btn_bakcalput.pack(side="left", padx=4)
    says(btn_bakcalput, "Restore...")
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
    ttk.Separator(bakleft, orient="horizontal").pack(fill="x", pady=(10, 8))
    bakbox4 = ttk.LabelFrame(bakleft, padding=8)
    says(bakbox4, "NVRAM")
    bakbox4.pack(fill="x")
    lbl_baknv = ttk.Label(bakbox4, foreground="#555", justify="left",
                          wraplength=BAK_PANE - 40)
    says(lbl_baknv,
         "User settings, saved waveforms, limits and masks, and on the "
         "earlier instruments the calibration constants too. Reading it "
         "needs the NVRAM protection switch set to unprotected and the "
         "instrument restarted into its bootloader - it is not read "
         "over the ordinary bus and nothing else here is read at the "
         "same time. Takes a few minutes.")
    lbl_baknv.pack(anchor="w")
    bakrow5 = ttk.Frame(bakbox4)
    bakrow5.pack(fill="x", pady=(6, 0))
    btn_baknv = ttk.Button(bakrow5, text=_("Back up..."), padding=(10, 2),
                           command=lambda: do_bak_nvram())
    btn_baknv.pack(side="left")
    says(btn_baknv, "Back up...")
    btn_baknvput = ttk.Button(bakrow5, text=_("Restore..."), padding=(10, 2),
                              command=lambda: do_bak_nvram_put())
    btn_baknvput.pack(side="left", padx=4)
    says(btn_baknvput, "Restore...")
    # No status line under these two buttons. Everything it said - the
    # checksum report and the result - is written to "What happened" on
    # the right as well, and the left column is narrow enough that the
    # duplicate was being cut off. The calibration box above keeps its
    # own line, because what that one says goes nowhere else.

    # ---- what happened
    bakbox5 = ttk.LabelFrame(bakright, padding=8)
    says(bakbox5, "What happened")
    bakbox5.pack(fill="both", expand=True)
    bakview = ttk.Frame(bakbox5)
    bakview.pack(fill="both", expand=True)
    # Wrapped on words, not scrolled sideways. What goes in here is
    # sentences rather than columns - a filename, a step, a checksum
    # read out in full - and a horizontal scrollbar hides the end of a
    # sentence behind a gesture nobody makes while watching an
    # operation run.
    baktxt = tk.Text(bakview, wrap="word", height=10, undo=False,
                     font="TkFixedFont", background="#ffffff",
                     highlightthickness=1, highlightbackground=EDGE)
    bakvsb = ttk.Scrollbar(bakview, orient="vertical", command=baktxt.yview)
    baktxt.configure(yscrollcommand=bakvsb.set)
    bakvsb.pack(side="right", fill="y")
    baktxt.pack(side="left", fill="both", expand=True)
    # Read-only the same way the error log is: selectable and copyable,
    # which a disabled Text is not.
    baktxt.bind("<Key>", lambda e: (None if (e.state & 4) or e.keysym in
                                    ("Left", "Right", "Up", "Down", "Home",
                                     "End", "Prior", "Next") else "break"))

    # ------------------------------------------------ the tab's own logic
    def bak_note(*lines):
        """Add to the report, and leave it looking at the newest line."""
        baktxt.insert("end", "\n".join(lines) + "\n")
        baktxt.see("end")

    def bak_begin():
        """Separate this run's account from the one before it.

        One run's steps run into the next otherwise, and the report is
        read after the fact as much as during. The separator goes in at
        the start rather than at the end because a run has one
        beginning and several ways of finishing.
        """
        if baktxt.get("1.0", "end").strip():
            baktxt.insert("end", "\n")
        state["bakstep"] = None

    def bak_available():
        """Which parts this instrument was found to have.

        Everything, until a refresh says otherwise: a tab that greys
        every choice before it has asked reads as broken rather than as
        cautious.
        """
        return state.get("bakhas") or dict((k, True) for k, _s in BAK_PARTS)

    def bak_wants():
        """The ticked parts that this instrument actually has."""
        have = bak_available()
        return dict((k, bool(state["bak" + k].get()) and bool(have.get(k)))
                    for k, _s in BAK_PARTS)

    def bak_buttons():
        have = bak_available()
        for key, _english in BAK_PARTS:
            bakchecks[key].config(state="normal" if have.get(key)
                                  else "disabled")
        ready = any(bak_wants().values()) and not state.get("busy")
        btn_bakmake.config(state="normal" if ready else "disabled")

    def bak_remember(path):
        """Start the next dialog where this one finished."""
        settings = load_settings()
        settings["backups"] = os.path.dirname(path)
        save_settings(settings)
        state["bakfolder"] = os.path.dirname(path)

    def do_bak_refresh():
        ui.busy(True, "wait")
        ui.say(_("Asking the instrument what it has ..."))
        w.submit("bak_survey", lambda k: k.bak_survey())

    def bak_show_survey(got):
        """What the instrument answered, as ticks that mean something."""
        volumes = [v.lower() for v in got.get("volumes") or []]
        # Not "bakrefs": that is the tick box's own variable, one of
        # those named after a part. The names of the references that
        # hold something are a different thing and need a different key.
        state["bakreflist"] = got.get("refs") or []
        state["bakhas"] = {
            "disk": "hd0:" in volumes, "floppy": "fd0:" in volumes,
            "setup": True, "refs": bool(state["bakreflist"]),
            "cal": True}
        for key, has in state["bakhas"].items():
            if key in ("disk", "floppy"):
                # A volume follows what the refresh found: ticked when it
                # is there, cleared when it is not.
                state["bak" + key].set(bool(has))
            elif not has:
                state["bak" + key].set(False)
        lbl_bakfound.config(text=_(
            "%(volumes)s. %(refs)d reference(s) hold a waveform.")
            % {"volumes": ", ".join(got.get("volumes") or [])
               or _("no volumes"), "refs": len(state["bakreflist"])})
        # The same sentence into the report, with what answered it. The
        # report is the record of the session, and what the instrument
        # was when it was asked is the first line of it.
        bak_note(*[t for t in (got.get("idn"), lbl_bakfound.cget("text"))
                   if t])
        bak_buttons()

    def do_bak_make():
        if state["busy"]:
            return
        want = bak_wants()
        name = stamped((model_of(state.get("idn")) or "TDS").replace(" ", ""),
                       ".zip")
        path = filedialog.asksaveasfilename(
            parent=root, title=_("Where to save the backup"),
            initialdir=state.get("bakfolder") or APPDIR, initialfile=name,
            defaultextension=".zip",
            filetypes=[(_("Backup file"), "*.zip")])
        if not path:
            return
        bak_remember(path)
        baktxt.delete("1.0", "end")
        bak_begin()
        bak_note(_("Backing up to %s") % os.path.basename(path))
        ui.busy(True, "steps")
        ui.say(_("Backing up the instrument ..."))
        w.submit("bak_make", lambda k: k.bak_make(
            {"path": path, "parts": want,
             "refs": state.get("bakreflist") or []}))

    def bak_pick_file(title):
        """Choose a backup and check it before anything is done with it."""
        path = filedialog.askopenfilename(
            parent=root, title=title,
            initialdir=state.get("bakfolder") or APPDIR,
            filetypes=[(_("Backup file"), "*.zip"), (_("All files"), "*.*")])
        if not path:
            return None, None
        bak_remember(path)
        try:
            checked = tds_bak.verify(path)
        except Exception as exc:
            messagebox.showerror(_("Error"), str(exc))
            return None, None
        return path, checked

    def bak_report(path, checked):
        """Say what a backup holds and whether it still reads correctly."""
        manifest = checked["manifest"]
        bak_begin()
        bak_note(os.path.basename(path),
                 _("Made %(when)s from %(idn)s")
                 % {"when": manifest.get("made") or "?",
                    "idn": manifest.get("idn") or "?"},
                 _("%(files)d file(s) checked, %(bad)d wrong, %(gone)d "
                   "missing") % {"files": checked["checked"],
                                 "bad": len(checked["bad"]),
                                 "gone": len(checked["missing"])})
        for arc in (checked["bad"] + checked["missing"])[:20]:
            bak_note("    " + arc)
        holds = tds_bak.holds(manifest)
        bak_note("    " + ", ".join("%s (%d)" % (k, n)
                                    for k, n in sorted(holds.items())))
        return holds

    def do_bak_check():
        path, checked = bak_pick_file(_("Which backup to verify"))
        if not path:
            return
        bak_report(path, checked)
        ui.say(_("The backup is complete and every file matches")
               if checked["ok"] else _("The backup does not match itself"))

    def do_bak_put():
        """Put parts of a backup back, after saying exactly which."""
        path, checked = bak_pick_file(_("Which backup to restore"))
        if not path:
            return
        holds = bak_report(path, checked)
        if not checked["ok"] and not messagebox.askyesno(
                _("Restore anyway?"),
                _("This backup does not match its own checksums. Whatever "
                  "is wrong with it would be written to the instrument. "
                  "Proceed?"), icon="warning", default="no"):
            return
        # Calibration is never part of this. It has its own button, its
        # own warning and its own switch to move first.
        want = dict((k, bool(state["bak" + k].get()) and k in holds)
                    for k, _s in BAK_PARTS if k != "cal")
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
        ui.busy(True, "steps")
        ui.say(_("Restoring to the instrument ..."))
        w.submit("bak_put", lambda k: k.bak_put({"path": path,
                                                 "parts": want}))

    def cal_running():
        """Is the instrument in the state the constants can be reached in?

        Both calibration buttons need the firmware running: the words go
        through WORDCONSTANT, which the firmware implements and the
        bootloader does not. That is the opposite of the NVRAM beside
        them, and it is the one thing about this box worth being told
        rather than left to infer from "not connected".
        """
        if w.fs is not None:
            return True
        messagebox.showinfo(_("The instrument is not answering"), _(
            "The calibration constants are read and written while the "
            "instrument runs normally, so it has to be connected.\n\n"
            "Unlike the NVRAM, this is not done from the bootloader: "
            "set the NVRAM protection switch to unprotected with the "
            "instrument switched on, without rebooting it, and connect "
            "as usual."))
        return False

    def do_bak_cal_get():
        """Ask where they go, then read them.

        Asked first, not last. The read is 504 queries and takes the
        better part of a minute; a save dialog that appears at the end
        of it is one nobody is still sitting in front of, and a read
        nobody answers for is a read thrown away.
        """
        if not cal_running():
            return
        settings = load_settings()
        name = tds_cal.names((model_of(state.get("idn")) or "TDS")
                             .replace(" ", ""),
                             time.strftime("%Y%m%d-%H%M%S"))[2]
        path = filedialog.asksaveasfilename(
            parent=root, initialfile=name,
            title=_("Where to save the calibration constants"),
            initialdir=settings.get("cal_backups") or APPDIR,
            defaultextension=".bin",
            filetypes=[(_("Calibration backup"), "*.bin")])
        if not path:
            return
        settings["cal_backups"] = os.path.dirname(path)
        save_settings(settings)
        state["calpath"] = path
        ui.busy(True, "steps")
        ui.say(_("Reading the calibration constants ..."))
        bak_begin()
        bak_note(_("Reading the calibration constants ..."))
        w.submit("cal_read", lambda k: k.cal_read())

    def do_bak_nvram():
        """Read the NVRAM through the bootloader monitor and save it."""
        settings = load_settings()
        # A save dialog with the name already in it, the way the
        # calibration backup asks. Choosing a folder and being told
        # nothing about what would land in it left the person to guess
        # both the name and whether anything had happened.
        where = filedialog.asksaveasfilename(
            parent=root, title=_("Where to put the NVRAM backup"),
            initialfile="%s_NVRAM_%s.bin"
            % ((fw_model() or model_of(state.get("idn")) or "TDS")
               .replace(" ", ""),
               time.strftime("%Y%m%d-%H%M%S")),
            initialdir=state.get("bakfolder") or settings.get("backups")
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
        state["bakfolder"] = folder
        settings["backups"] = folder
        save_settings(settings)
        ui.busy(True, "steps")
        ui.say(_("Reading the NVRAM ..."))
        bak_begin()
        bak_note(_("Reading the NVRAM through the bootloader ..."))
        w.submit("bak_nvram", lambda k: k.fw_nvram({
            "resource": tds_fw.bootloader_resource(state.get("addr")
                                                   or DEFAULT_ADDR),
            "normal": state.get("addr") or DEFAULT_ADDR,
            "backup_dir": folder, "check_backup": True,
            # The name the person just typed, rather than one built from
            # a model the program may not know - it cannot connect to an
            # instrument that is in its bootloader, so fw_model() is
            # often empty exactly when this runs.
            "names": {"NVRAM": os.path.basename(where)},
            "model": fw_model() or "TDS",
            "stamp": time.strftime("%Y%m%d-%H%M%S")}), needs_fs=False)

    def do_bak_nvram_put():
        """Write a saved NVRAM back, after adding up what is in it."""
        settings = load_settings()
        path = filedialog.askopenfilename(
            parent=root, title=_("Which NVRAM backup to restore"),
            initialdir=state.get("bakfolder") or settings.get("backups")
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
        bak_begin()
        bak_note(*lines)
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
        state["bakfolder"] = os.path.dirname(path)
        ui.busy(True, "steps")
        ui.say(_("Restoring the NVRAM ..."))
        w.submit("bak_nvram_put", lambda k: k.fw_nvram_put({
            "path": path,
            "resource": tds_fw.bootloader_resource(state.get("addr")
                                                   or DEFAULT_ADDR),
            "normal": state.get("addr") or DEFAULT_ADDR,
            "backup_dir": os.path.dirname(path),
            "model": fw_model() or "TDS",
            "stamp": time.strftime("%Y%m%d-%H%M%S")}), needs_fs=False)

    def do_bak_cal_put():
        """Put a saved set back, after reading what is there now.

        Refused up front unless the instrument is answering normally.
        The constants are read and written with WORDCONSTANT, which the
        firmware implements - so unlike the NVRAM, this one cannot be
        done from the bootloader at all, and an instrument sitting in
        its bootloader answers nothing here. Saying "not connected"
        sends somebody to look at the cable.
        """
        if not cal_running():
            return
        settings = load_settings()
        path = filedialog.askopenfilename(
            parent=root, title=_("Which calibration backup to restore"),
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
        # The file names the model it came off (tds_cal.names), and the
        # constants belong to one acquisition board. Another model's go
        # in all the same, if asked twice, because a converted instrument
        # may well be carrying a backup under its old name.
        theirs = os.path.basename(path).split("_")[0].upper()
        mine = model_of(state.get("idn")).replace(" ", "").upper()
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
        ui.busy(True, "steps")
        ui.say(_("Restoring the calibration constants ..."))
        bak_begin()
        w.submit("cal_write", lambda k: k.cal_write(
            {"data": data, "from": path}))

    def bak_relabel():
        """The line this tab writes for itself, said again."""
        if state.get("bakhas") is None:
            lbl_bakfound.config(
                text=_("Refresh to see what this instrument has."))

    relabel.append(bak_relabel)
    state["bakfolder"] = load_settings().get("backups") or ""
    bak_buttons()

    # The pump's handlers for this tab's jobs - see pump_jobs.
    def pump_cal_read(payload):
        ui.busy(False)
        # Where it goes was settled before the read started,
        # so there is nothing to ask here - only to write.
        path = ui.keep_file(state.get("calpath"), payload["data"])
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
        bak_note(*lines)
        ui.say(_("Calibration constants saved"))

    def pump_cal_write(payload):
        ui.busy(False)
        lines = []
        if payload.get("stuck"):
            lines.append(_(
                "Word %(word)d would not take %(wanted)d - "
                "it reads %(got)d. Stopped there.")
                % payload["stuck"])
        lines.append(_("%(wrote)d of %(words)d word(s) "
                       "written and read back.") % payload)
        if payload.get("cancelled"):
            # Stopped part way, so the block is now
            # neither the file nor what was there.
            lines.append(_(
                "This was stopped part way through. The "
                "words already written are the file's; the "
                "rest are whatever the instrument held "
                "before. Restore it again to finish."))
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
        bak_note(*lines)
        ui.say(_("The instrument would not take every "
                 "calibration word")
               if payload.get("stuck") else
               _("The calibration restore was stopped part "
                 "way through")
               if payload.get("cancelled") else
               _("Calibration constants restored and stored")
               if payload.get("stored") is True else
               _("The constants were sent but the instrument "
                 "did not store them")
               if payload.get("stored") is False
               else _("Calibration constants sent"))

    def pump_bak_survey(payload):
        ui.busy(False)
        bak_show_survey(payload)
        ui.say(_("The instrument answered"))

    def pump_bak_nvram_put(payload):
        ui.busy(False)
        bak_note(_("Restored %(size)s of NVRAM from %(file)s")
                 % {"size": human_bytes(payload["wrote"]),
                    "file": os.path.basename(payload["path"])},
                 _("    read back and identical"))
        # Said whenever the two differ, because otherwise
        # the line above reports 640 kB restored from a
        # 1 MB file and reads like a write that stopped
        # short. It did not: the rest of the window is the
        # same memory answering a second time.
        if payload.get("held", 0) > payload.get("distinct", 0):
            bak_note(_(
                "    the file is %(held)s and this instrument "
                "holds %(real)s of distinct memory - the rest "
                "of the window is the same memory answering "
                "again, and is not written twice")
                % {"held": human_bytes(payload["held"]),
                   "real": human_bytes(payload["distinct"])})
        ui.say(_("NVRAM restored"))

    def pump_bak_nvram(payload):
        ui.busy(False)
        for one in payload["backups"]:
            bak_note(_("%(what)s: %(size)s to %(file)s")
                     % {"what": one["what"],
                        "size": human_bytes(one["bytes"]),
                        "file": os.path.basename(one["path"])},
                     "    " + one["sha"])
        for one in payload.get("ticked") or []:
            bak_note(_("The clock moved while it was read, in "
                       "%d byte(s). That is the instrument "
                       "keeping time, not a fault.")
                     % one["bytes"])
        ui.say(_("NVRAM backed up"))

    def pump_bak_make(payload):
        ui.busy(False)
        bak_note(_("%(files)d file(s), %(size)s")
                 % {"files": len(payload["manifest"]["files"]),
                    "size": human_bytes(payload["bytes"])},
                 "    " + ", ".join(
                     "%s (%d)" % (k, n) for k, n
                     in sorted(payload["holds"].items())))
        for name, why in payload["failed"]:
            bak_note(_("%(part)s: %(why)s")
                     % {"part": name, "why": why})
        ui.say(_("Backed up to %s")
               % os.path.basename(payload["path"])
               if not payload["failed"]
               else _("Backed up, with %d part(s) missing")
               % len(payload["failed"]))

    def pump_bak_put(payload):
        ui.busy(False)
        for name, what in payload["done"]:
            bak_note(_("%(part)s: %(what)s restored")
                     % {"part": name, "what": what})
        for name, why in payload["failed"]:
            bak_note(_("%(part)s: %(why)s")
                     % {"part": name, "why": why})
        ui.say(_("Restored to the instrument")
               if not payload["failed"]
               else _("Restored, with %d part(s) that failed")
               % len(payload["failed"]))

    vars(ui).update((k, v) for k, v in locals().items() if k != "ui")
