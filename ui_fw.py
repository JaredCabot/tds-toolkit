"""The Firmware tab.

FirmwareTab is the tab; build(ui) makes it and hands its names back to
the window, which still reaches the tabs through `ui` (state, the
worker, busy, say and the rest) while they are moved onto classes.
"""
import os
import re
import time
import tkinter as tk
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk

import tds_fw
from i18n import gettext as _
from tds_fs import DEFAULT_ADDR
from tdstoolkit import (APPDIR, load_settings, log_note, model_of, resource,
                        save_settings, tab_names)


class FirmwareTab(object):
    """The Firmware tab: images, the bootloader, NVRAM and the flash write."""

    def __init__(self, ui):
        self.ui = ui
        self.named = ui.named
        self.relabel = ui.relabel
        self.root = ui.root
        self.says = ui.says
        self.state = ui.state
        self.tabs = ui.tabs
        self.w = ui.w

        # ------------------------------------------------- the firmware tab
        # Everything on this tab happens with the instrument in the ROM
        # monitor it enters when it is switched on with its NVRAM protection
        # switch unprotected. In that state it answers no *IDN?, shows
        # nothing at all on screen and lights every front-panel LED - so what
        # it is has to be remembered from before the switch was moved, and
        # the only thing on the bus that says anything about it is the flash.
        #
        # The monitor lives in mask ROM, which is why this is a reasonable
        # thing to offer at all: a write that goes wrong is put right by
        # writing the original image back, not by opening the cabinet.
        self.fwtab = ttk.Frame(self.tabs)
        self.tabs.add(self.fwtab, text=_("Firmware"))
        self.named(self.fwtab, "Firmware")

        fwleft = ttk.Frame(self.fwtab)
        fwleft.pack(side="left", fill="y", padx=(4, 3), pady=6)
        fwright = ttk.Frame(self.fwtab)
        fwright.pack(side="left", fill="both", expand=True, padx=(3, 4),
                     pady=6)

        # ---- what the person at the bench has to do first
        fwbox0 = ttk.LabelFrame(fwleft, padding=8)
        self.says(fwbox0, "Preparing the instrument")
        fwbox0.pack(fill="both", expand=True)
        lbl_fwstep1 = ttk.Label(fwbox0, wraplength=310, justify="left")
        self.says(lbl_fwstep1,
                  "1. Switch the instrument off at its front-panel power "
                  "button.\n"
                  "2. Move the NVRAM protection switch to unprotected, as in "
                  "the picture below.\n"
                  "3. Switch it on. The screen stays dark and every "
                  "front-panel light stays on. That is the monitor, not a "
                  "fault.")
        lbl_fwstep1.pack(anchor="w")
        # The same figure as the factory options dialog, and the same reason:
        # "the two small access holes on the right side near the front" is a
        # sentence somebody reads three times with a torch in their hand.
        try:
            fwshot = tk.PhotoImage(file=resource("memprotect.png"))
            self.state["fwfigure"] = fwshot           # or Tk collects it
            ttk.Label(fwbox0, image=fwshot).pack(anchor="w", pady=(6, 2))
            self.says(ttk.Label(fwbox0, foreground="#555",
                                wraplength=fwshot.width(), justify="left"),
                      "TDS 500D/600C/700D service manual "
                      "071-0627-02, figure 5-1").pack(anchor="w")
        except Exception as exc:
            log_note("ui", "no memory protection figure: %s" % exc)
        lbl_fwstep2 = ttk.Label(fwbox0, wraplength=310, justify="left")
        self.says(lbl_fwstep2,
                  "4. Connect, choose an image and press Write.\n"
                  "5. When it finishes, switch off at the front panel, put "
                  "the switch back to protected and switch on again.")
        lbl_fwstep2.pack(anchor="w", pady=(8, 0))

        # ---- 1: which instrument
        fwbox1 = ttk.LabelFrame(fwright, padding=8)
        self.says(fwbox1, "1. Select instrument")
        fwbox1.pack(fill="x")
        fwrow1 = ttk.Frame(fwbox1)
        fwrow1.pack(fill="x")
        lbl_fwmodel = ttk.Label(fwrow1, text=_("Model"), width=8)
        lbl_fwmodel.pack(side="left")
        self.says(lbl_fwmodel, "Model")
        self.state["fwmodel"] = tk.StringVar()
        self.cbo_fwmodel = ttk.Combobox(fwrow1,
                                        textvariable=self.state["fwmodel"],
                                        width=12, state="normal")
        self.cbo_fwmodel.pack(side="left")
        # What the instrument said it was running, before its switch was
        # moved. Beside the model because the two together are the whole of
        # "what is on the bench", and choosing an image without knowing the
        # second one is how a scope gets an image from the wrong generation.
        self.lbl_fwfv = ttk.Label(fwrow1, foreground="#555", text="-")
        self.lbl_fwfv.pack(side="left", padx=(8, 0))
        self.btn_fwprobe = ttk.Button(fwrow1, text=_("Connect"),
                                      padding=(10, 2),
                                      command=lambda: self.do_fw_probe())
        self.btn_fwprobe.pack(side="right")
        self.says(self.btn_fwprobe, "Connect")
        self.lbl_fwstate = ttk.Label(fwbox1, wraplength=430, justify="left",
                                     text="-")
        self.lbl_fwstate.pack(anchor="w", pady=(8, 0))

        # ---- 2: which image
        ttk.Separator(fwright, orient="horizontal").pack(fill="x",
                                                         pady=(10, 8))
        fwbox2 = ttk.LabelFrame(fwright, padding=8)
        self.says(fwbox2, "2. Select firmware")
        fwbox2.pack(fill="both", expand=True)
        fwrow2 = ttk.Frame(fwbox2)
        fwrow2.pack(fill="x")
        self.lbl_fwfolder = ttk.Label(fwrow2, text="-", foreground="#555",
                                      anchor="w")
        self.lbl_fwfolder.pack(side="left", fill="x", expand=True)
        self.btn_fwfolder = ttk.Button(fwrow2, text=_("Folder..."),
                                       padding=(10, 2),
                                       command=lambda: self.do_fw_folder())
        self.btn_fwfolder.pack(side="right")
        self.says(self.btn_fwfolder, "Folder...")
        # One image from anywhere, for the copy that is not in the folder.
        self.btn_fwfile = ttk.Button(fwrow2, text=_("File..."),
                                     padding=(10, 2),
                                     command=lambda: self.do_fw_file())
        self.btn_fwfile.pack(side="right", padx=(0, 6))
        self.says(self.btn_fwfile, "File...")
        self.state["fwall"] = tk.BooleanVar(value=False)
        self.chk_fwall = ttk.Checkbutton(fwbox2, variable=self.state["fwall"],
                                         text=_("Show all available firmware"),
                                         command=lambda: self.fw_fill())
        self.chk_fwall.pack(anchor="w", pady=(6, 4))
        self.says(self.chk_fwall, "Show all available firmware")
        lbl_fwmajor = ttk.Label(fwbox2, foreground="#555", wraplength=430,
                                justify="left")
        self.says(lbl_fwmajor,
                  "Stay within the major version the instrument is already "
                  "running - v5.x to v5.x. The first number changes with the "
                  "hardware, and an image from a different one will not run "
                  "on this scope.")
        lbl_fwmajor.pack(anchor="w", pady=(0, 4))

        fwlist = ttk.Frame(fwbox2)
        fwlist.pack(fill="both", expand=True)
        self.tree_fw = ttk.Treeview(fwlist, columns=("size", "also"), height=8,
                                    selectmode="browse")
        self.tree_fw.column("#0", width=110, stretch=False)
        self.tree_fw.column("size", width=80, stretch=False, anchor="e")
        self.tree_fw.column("also", width=240)
        fwbar = ttk.Scrollbar(fwlist, orient="vertical",
                              command=self.tree_fw.yview)
        self.tree_fw.configure(yscrollcommand=fwbar.set)
        self.tree_fw.pack(side="left", fill="both", expand=True)
        fwbar.pack(side="left", fill="y")
        self.lbl_fwpick = ttk.Label(fwbox2, wraplength=430, justify="left",
                                    text="-")
        self.lbl_fwpick.pack(anchor="w", pady=(6, 0))
        self.fw_headings()
        self.relabel.append(self.fw_headings)

        # ---- 3: the backup, and 4: writing
        ttk.Separator(fwright, orient="horizontal").pack(fill="x",
                                                         pady=(10, 8))
        fwbox3 = ttk.LabelFrame(fwright, padding=8)
        self.says(fwbox3, "3. Back up, then write")
        fwbox3.pack(fill="x")
        fwrow3 = ttk.Frame(fwbox3)
        fwrow3.pack(fill="x")
        self.lbl_fwback = ttk.Label(fwrow3, text="-", foreground="#555",
                                    anchor="w")
        self.lbl_fwback.pack(side="left", fill="x", expand=True)
        self.btn_fwback = ttk.Button(fwrow3, text=_("Save backups to..."),
                                     padding=(10, 2),
                                     command=lambda: self.do_fw_backup_dir())
        self.btn_fwback.pack(side="right")
        self.says(self.btn_fwback, "Save backups to...")

        self.state["fwcheck"] = tk.BooleanVar(value=True)
        self.chk_fwcheck = ttk.Checkbutton(
            fwbox3, variable=self.state["fwcheck"],
            text=_("Read each backup a second time and compare"))
        self.chk_fwcheck.pack(anchor="w", pady=(6, 0))
        self.says(self.chk_fwcheck,
                  "Read each backup a second time and compare")

        self.state["fwslow"] = tk.BooleanVar(value=False)
        self.chk_fwslow = ttk.Checkbutton(
            fwbox3, variable=self.state["fwslow"],
            text=_("Slow write (page buffer bypassed)"))
        self.chk_fwslow.pack(anchor="w", pady=(4, 0))
        self.says(self.chk_fwslow, "Slow write (page buffer bypassed)")

        # How much to back up is not a question worth asking. Erase is
        # chip-wide, so the answer is always all of it, and the size is
        # measured at Connect rather than guessed from the catalogue. An
        # answer somebody can get wrong, on the one operation where getting
        # it wrong costs them the instrument's own firmware, is not a
        # setting - it is a trap with a label on it.
        self.lbl_fwlen = ttk.Label(fwbox3, foreground="#555", text="-")
        self.lbl_fwlen.pack(anchor="w", pady=(6, 0))

        fwbtns = ttk.Frame(fwbox3)
        fwbtns.pack(anchor="w", pady=(10, 0))
        self.btn_fwstart = ttk.Button(fwbtns, text=_("Write firmware"),
                                      padding=(10, 3),
                                      command=lambda: self.do_fw_start())
        self.btn_fwstart.pack(side="left")
        self.says(self.btn_fwstart, "Write firmware")
        # Backing up on its own, without a write to follow: no image need be
        # chosen and nothing is ever erased or programmed. It reads the same
        # two regions a write would keep first, through the same fw_keep, so
        # it is the write's own safety net offered by itself.
        self.btn_fwbackup = ttk.Button(fwbtns, text=_("Back up firmware"),
                                       padding=(10, 3),
                                       command=lambda: self.do_fw_backup())
        self.btn_fwbackup.pack(side="left", padx=(8, 0))
        self.says(self.btn_fwbackup, "Back up firmware")

        self.relabel.append(self.fw_relabel)
        self.tree_fw.bind("<<TreeviewSelect>>", self.fw_picked)
        self.cbo_fwmodel.bind("<<ComboboxSelected>>", lambda e: self.fw_fill())
        self.state["fwmodel"].trace_add("write", lambda *a: self.fw_fill())

        self.tabs.bind("<<NotebookTabChanged>>", self.fw_first_look, add="+")

    def fw_told(self):
        """Model and firmware version, from before the switch was moved."""
        got = re.search(r"FV:\s*v?([0-9][0-9.]*[a-z]?)",
                        self.state.get("idn") or "")
        return model_of(self.state.get("idn")), (got.group(1) if got else "")

    def fw_model(self):
        """The model this is all aimed at - told, or chosen by hand."""
        return (self.state["fwmodel"].get() or "").upper().replace(" ", "")

    def fw_headings(self):
        self.tree_fw.heading("#0", text=_("Version"))
        self.tree_fw.heading("size", text=_("Size"))
        self.tree_fw.heading("also", text=_("Compatible scopes"))

    # ------------------------------------------------ the tab's own logic
    def fw_backup_len(self):
        """All of the flash that is really there, measured at Connect."""
        got = self.state.get("fwmonitor") or {}
        return got.get("span") or 0x400000

    def fw_fill(self):
        """Redraw the image list for the model in the box."""
        self.tree_fw.delete(*self.tree_fw.get_children())
        model = self.fw_model()
        wanted = [im for im in self.state.get("fwimages") or []
                  if self.state["fwall"].get() or im.suits(model)]
        # An image chosen by hand is always shown, at the top, whatever
        # model it was shipped for - it was named rather than found, so
        # filtering it back out again would be answering a question
        # nobody asked. The folder's copy of the same bytes gives way.
        one = self.state.get("fwchosen")
        if one is not None:
            wanted = [one] + [im for im in wanted if im.sha != one.sha]
        for i, im in enumerate(wanted):
            # Every model the image is for, this one included. It used
            # to be every model *except* this one, under "Also shipped
            # as" - which read, on the row you were about to write, as
            # a list of the scopes it was not for. With the list shown
            # whole, the selected model appearing in a row is the
            # answer to "will this image run on the scope in front of
            # me", which is the question the column is there for.
            self.tree_fw.insert("", "end", iid=str(i), text="v" + im.version,
                                values=(tds_fw._size(im.size),
                                        ", ".join(im.models)))
        self.state["fwshown"] = wanted
        self.fw_picked()

    def fw_current(self):
        picked = self.tree_fw.selection()
        shown = self.state.get("fwshown") or []
        if not picked or int(picked[0]) >= len(shown):
            return None
        return shown[int(picked[0])]

    def fw_picked(self, *_a):
        """Say what is known about the highlighted image."""
        one = self.fw_current()
        if one is None:
            self.lbl_fwpick.config(text=_("No image chosen."),
                                   foreground="#555")
        else:
            lines = [_("%(name)s, %(size)s")
                     % {"name": os.path.basename(one.path),
                        "size": tds_fw._size(one.size)}]
            if one.mislabelled:
                # Three of the sixty-seven images to hand carry a version
                # inside them that is not the one in the filename. Which is
                # right is not knowable from here, so it is said rather
                # than quietly resolved one way.
                lines.append(_(
                    "The filename says v%(name)s but the image says "
                    "v%(inside)s.") % {"name": one.version,
                                       "inside": one.mislabelled})
            self.lbl_fwpick.config(text="\n".join(lines), foreground="")
        self.fw_buttons()

    def fw_buttons(self):
        # Backing up is not optional, so neither is saying where the
        # backups go: Write stays greyed until there is somewhere to put
        # them, rather than being offered and then refusing.
        where = self.state.get("fwbackups") or ""
        have_where = bool(where) and os.path.isdir(where)
        mon = bool(self.state.get("fwmonitor"))
        idle = not self.state.get("busy")
        ready = mon and self.fw_current() is not None and have_where
        self.btn_fwstart.config(state="normal" if ready and idle
                                else "disabled")
        # The backup button asks less of the tab than Write does: a
        # monitor and somewhere to put the files, but no image chosen,
        # because it never writes one.
        self.btn_fwbackup.config(state="normal" if mon and have_where and idle
                                 else "disabled")

    def fw_say_told(self):
        """The version the instrument reported, beside the model box."""
        _model, version = self.fw_told()
        self.lbl_fwfv.config(text=(_("Firmware v%s") % version) if version
                             else _("firmware not read"))

    def fw_said_len(self):
        """What the backup will cover, now that nobody chooses it."""
        got = self.state.get("fwmonitor")
        if not got:
            self.lbl_fwlen.config(text=_("The firmware and the NVRAM are each "
                                         "backed up whole. Connect to measure "
                                         "how large they are."))
            return
        said = _("Backs up all %(flash)s of firmware and %(nvram)s of "
                 "NVRAM, to two files.") % {
            "flash": tds_fw._size(self.fw_backup_len()),
            "nvram": tds_fw._size(tds_fw.NVRAM_LEN)}
        # The measurement and the part's datasheet should agree. Where
        # they do not, this instrument is not built like the ones this
        # was written against, and that is worth a line rather than a
        # silent decision about whose answer to take.
        fitted = got.get("fitted")
        if fitted and fitted != got.get("span"):
            said += "\n" + _("A %(flash)s is fitted in %(fitted)s "
                             "elsewhere, but this array measures "
                             "%(span)s.") % {
                "flash": got.get("flash"), "fitted": tds_fw._size(fitted),
                "span": tds_fw._size(got.get("span") or 0)}
        self.lbl_fwlen.config(text=said)

    def fw_relabel(self):
        """The lines this tab writes for itself, said again in the new
        language. `labelled` covers text a widget was built with; these
        are rewritten as the tab is used, so they need re-running
        rather than re-setting."""
        self.lbl_fwfolder.config(text=self.state.get("fwfolder")
                                 or _("no folder chosen"))
        self.lbl_fwback.config(text=self.state.get("fwbackups")
                               or _("not chosen"))
        self.fw_say_state()
        self.fw_say_told()
        self.fw_said_len()
        self.fw_picked()

    def do_fw_folder(self):
        settings = load_settings()
        folder = filedialog.askdirectory(
            parent=self.root, title=_("Where the firmware images are"),
            initialdir=settings.get("firmware_folder") or APPDIR)
        if not folder:
            return
        settings["firmware_folder"] = folder
        save_settings(settings)
        self.fw_load_folder(folder)

    def do_fw_file(self):
        """One image named by hand, from wherever it happens to be.

        The folder is how the images are normally found; this is for the
        copy that is not in it - a download, a memory stick, an image
        somebody was sent.
        """
        settings = load_settings()
        path = filedialog.askopenfilename(
            parent=self.root, title=_("Choose a firmware image"),
            initialdir=self.state.get("fwfolder")
            or settings.get("firmware_folder") or APPDIR,
            filetypes=[(_("Firmware images"), "*.bin"),
                       (_("All files"), "*.*")])
        if not path:
            return
        try:
            blob = tds_fw.read_image(path)
        except OSError as exc:
            messagebox.showerror(_("Error"), "%s" % exc)
            return
        # Every image in the family starts the same four bytes. One that
        # does not is not refused - the folder scan already skips those
        # silently, and a file named on purpose deserves an answer
        # rather than nothing happening - but it is said plainly first.
        if not blob.startswith(tds_fw.IMAGE_HEAD):
            if not messagebox.askyesno(_("Use this file?"), _(
                    "%s does not begin like TDS firmware. Writing it "
                    "would leave the instrument with no working firmware "
                    "until a real image is written again.\n\nUse it "
                    "anyway?") % os.path.basename(path),
                    icon="warning", default="no"):
                return
        one = tds_fw.Image(path, blob)
        # A file that is a copy of a known image keeps that image's model
        # list, which the filename on its own may not carry.
        one.models = tds_fw.index_models(os.path.dirname(path)).get(
            os.path.basename(path).lower(), one.models)
        self.state["fwchosen"] = one
        self.fw_fill()
        self.tree_fw.selection_set("0")
        self.tree_fw.focus("0")

    def fw_load_folder(self, folder):
        self.state["fwfolder"] = folder
        self.lbl_fwfolder.config(text=folder)
        self.ui.busy(True, "steps")
        self.ui.say(_("Reading the firmware folder ..."))
        self.w.submit("fw_catalogue", lambda k: k.fw_catalogue(folder),
                      needs_fs=False)

    def do_fw_backup_dir(self):
        settings = load_settings()
        folder = filedialog.askdirectory(
            parent=self.root, title=_("Where to put the backups"),
            initialdir=settings.get("firmware_backups") or APPDIR)
        if not folder:
            return
        settings["firmware_backups"] = folder
        save_settings(settings)
        self.state["fwbackups"] = folder
        self.lbl_fwback.config(text=folder)
        self.fw_buttons()

    def do_fw_probe(self):
        """Find the monitor and ask the flash what it is."""
        where = tds_fw.bootloader_resource(self.state.get("addr")
                                           or DEFAULT_ADDR)
        normal = self.state.get("addr") or DEFAULT_ADDR
        self.ui.busy(True, "wait")
        self.ui.say(_("Looking for the monitor at %s ...") % where)
        self.w.submit("fw_probe", lambda k: k.fw_probe(where, normal),
                      needs_fs=False)

    def fw_say_state(self):
        got = self.state.get("fwmonitor")
        if not got:
            self.lbl_fwstate.config(text=_(
                "Not connected. Switch the instrument on with its NVRAM "
                "protection switch unprotected, then press Connect."),
                                    foreground="#555")
            return
        head = " ".join("%02X" % b for b in bytearray(got["head"]))
        said = _("The monitor answered at %(where)s. Flash: %(flash)s. The "
                 "first four bytes of the fitted firmware read %(head)s.") % {
            "where": got["resource"], "flash": got["flash"], "head": head}
        # A part with no page buffer cannot be driven from here at all -
        # a longword at a time is days - so the option to do it is not
        # offered on one, rather than left to refuse the write after it
        # has been chosen. Same rule as the greying everywhere else: a
        # control that can only produce an apology is not a control.
        paged = bool(got.get("paged"))
        self.fw_said_len()
        self.chk_fwslow.config(state="normal" if paged else "disabled")
        if not paged:
            self.state["fwslow"].set(False)
            said += "\n" + _("This part has no page buffer, so only the "
                             "instrument itself can write it.")
        self.lbl_fwstate.config(foreground="", text=said)

    def do_fw_start(self):
        one = self.fw_current()
        got = self.state.get("fwmonitor")
        if one is None or not got or self.state["busy"]:
            return
        # The same folder the label names and the button gates on. Read
        # again here only in case it has gone since it was chosen.
        where = self.state.get("fwbackups") or APPDIR
        if not os.path.isdir(where):
            messagebox.showerror(_("Error"), _(
                "Choose a folder for the backups first."))
            return
        model = self.fw_model() or "TDS"
        if not one.suits(model):
            # Nothing stops it - a family image is often right for a model
            # its filename does not carry - but it is not passed over.
            if not messagebox.askyesno(_("Write this image?"), _(
                    "%(file)s was not shipped for a %(model)s. It was "
                    "shipped for %(for)s.\n\nWrite it anyway?")
                    % {"file": os.path.basename(one.path), "model": model,
                       "for": ", ".join(one.models)},
                    icon="warning", default="no"):
                return
        if not messagebox.askyesno(_("Write firmware?"), _(
                "This will:\n\n"
                "  read the instrument's NVRAM and firmware into "
                "%(where)s\n"
                "  erase the firmware flash completely\n"
                "  write %(file)s\n"
                "  read it back and compare\n\n"
                "It takes several minutes and must not be interrupted at "
                "the instrument. If it does go wrong, the monitor is in "
                "ROM: switch on the same way and write an image again.\n\n"
                "Proceed?")
                % {"where": where, "file": os.path.basename(one.path)},
                icon="warning", default="no"):
            return
        plan = {"resource": got["resource"], "image": one.path,
                "archive": one.archive, "sha": one.sha,
                "normal": self.state.get("addr") or DEFAULT_ADDR,
                "slow": bool(self.state["fwslow"].get()),
                "backup_dir": where, "backup_len": self.fw_backup_len(),
                "check_backup": bool(self.state["fwcheck"].get()),
                "model": model,
                "stamp": time.strftime("%Y%m%d-%H%M%S")}
        self.ui.busy(True, "steps")
        self.ui.say(_("Backing up before anything is written ..."))
        self.w.submit("fw_run", lambda k: k.fw_run(plan), needs_fs=False)

    def do_fw_backup(self):
        """Read the NVRAM and firmware out to files and stop there. No image
        is chosen and nothing is written to the instrument - this is the
        backup a write would take, taken on its own."""
        got = self.state.get("fwmonitor")
        if not got or self.state["busy"]:
            return
        where = self.state.get("fwbackups") or APPDIR
        if not os.path.isdir(where):
            messagebox.showerror(_("Error"), _(
                "Choose a folder for the backups first."))
            return
        if not messagebox.askyesno(_("Back up firmware?"), _(
                "This reads the instrument's NVRAM and firmware into "
                "%(where)s, to two files. Nothing is erased and nothing is "
                "written to the instrument.\n\nProceed?") % {"where": where},
                default="yes"):
            return
        plan = {"resource": got["resource"],
                "normal": self.state.get("addr") or DEFAULT_ADDR,
                "backup_dir": where, "backup_len": self.fw_backup_len(),
                "check_backup": bool(self.state["fwcheck"].get()),
                "model": self.fw_model() or "TDS",
                "stamp": time.strftime("%Y%m%d-%H%M%S")}
        self.ui.busy(True, "steps")
        self.ui.say(_("Backing up the firmware and NVRAM ..."))
        self.w.submit("fw_backup", lambda k: k.fw_backup(plan), needs_fs=False)

    def fw_first_look(self, _event=None):
        """Fill the tab in the first time it is looked at."""
        try:
            if self.tabs.tab(self.tabs.select(), "text") != _("Firmware"):
                return
        except tk.TclError:
            return
        # Before the once-only guard: the instrument may have been
        # connected since the last look, and this line is where the
        # answer to that shows.
        self.fw_say_told()
        if self.state.get("fwlooked"):
            return
        self.state["fwlooked"] = True
        settings = load_settings()
        model, _version = self.fw_told()
        if model and not self.state["fwmodel"].get():
            self.state["fwmodel"].set(model)
        self.state["fwbackups"] = settings.get("firmware_backups") or ""
        self.lbl_fwback.config(text=self.state["fwbackups"] or _("not chosen"))
        self.fw_say_state()
        self.fw_said_len()
        folder = settings.get("firmware_folder")
        if folder and os.path.isdir(folder):
            self.fw_load_folder(folder)
        else:
            self.lbl_fwfolder.config(text=_("no folder chosen"))
            self.fw_picked()

    def fw_report(self, got):
        """Say what happened, once. It is a long operation and the status
        line will have moved on by the time anyone reads it."""
        lines = []
        for one in got["backups"]:
            lines.append(_("%(what)s backed up: %(path)s")
                         % {"what": one["what"],
                            "path": os.path.basename(one["path"])})
        lines.append("")
        if got.get("helper"):
            lines.append(_("Written a page at a time by the instrument "
                           "itself."))
        elif got.get("asked_slow"):
            lines.append(_("Written from here, page by page, as asked."))
        else:
            lines.append(_("Written from here, page by page - the "
                           "on-instrument helper did not run."))
        # Pages that fell back are worth counting; pages written from
        # here because that is what was asked for are not news.
        if got.get("slow_pages") and not got.get("asked_slow"):
            lines.append(_("%d pages had to be written the slow way.")
                         % got["slow_pages"])
        if got["faults"]:
            lines.append("")
            if got["faults"][0] >= got["wrote"]:
                # The image went in; it is the part above it, which the
                # erase takes too, that still holds something.
                lines.append(_("The flash matches the image, but above it, "
                               "at 0x%06X, it is not erased: part of the "
                               "array did not take the erase.")
                             % got["faults"][0])
            else:
                lines.append(_("The flash does NOT match the image. The "
                               "first difference is at 0x%06X.")
                             % got["faults"][0])
            lines.append(_("Nothing is lost: switch off, leave the switch "
                           "unprotected, switch on and write it again."))
            self.ui.say(_("Firmware written but it does not verify"))
            messagebox.showerror(_("Error"), "\n".join(lines))
            return
        lines.append(_("The flash matches the image, all %s of it.")
                     % tds_fw._size(got["wrote"]))
        lines.append("")
        lines.append(_("Switch off at the front panel, put the NVRAM "
                       "protection switch back to protected, and switch "
                       "on again."))
        self.ui.say(_("Firmware written and verified"))
        messagebox.showinfo(_("Firmware written"), "\n".join(lines))

    def fw_backup_report(self, got):
        """Say what was saved, once - a backup on its own, with no write to
        report after it."""
        lines = []
        for one in got["backups"]:
            lines.append(_("%(what)s backed up: %(path)s (%(size)s)")
                         % {"what": one["what"],
                            "path": os.path.basename(one["path"]),
                            "size": tds_fw._size(one["bytes"])})
        if got.get("ticked"):
            lines.append("")
            lines.append(_("The clock moved while the NVRAM was read, which "
                           "is the instrument keeping time and not a fault."))
        lines.append("")
        lines.append(_("Nothing was written to the instrument."))
        self.ui.say(_("Firmware and NVRAM backed up"))
        messagebox.showinfo(_("Backed up"), "\n".join(lines))

    # The pump's handlers for this tab's jobs - see pump_jobs.
    def pump_fw_probe(self, payload):
        self.ui.busy(False)
        # Never reached on a failure - that is handled above,
        # in the tab's own line rather than in a dialog.
        self.state["fwmonitor"] = payload
        self.fw_say_state()
        self.fw_buttons()
        self.ui.say(_("The monitor answered - the flash is %s")
                    % payload["flash"])

    def pump_fw_catalogue(self, payload):
        self.ui.busy(False)
        self.state["fwimages"] = payload["images"]
        self.cbo_fwmodel.config(values=sorted(
            {m for im in payload["images"] for m in im.models}))
        self.fw_fill()
        self.ui.say(_("%(count)d firmware images in %(folder)s")
                    % {"count": len(payload["images"]),
                       "folder": payload["folder"]})

    def pump_fw_run(self, payload):
        # The report ends by telling the user to switch off, so
        # what Connect measured is about to be stale: Write
        # waits for a fresh Connect rather than sizing the next
        # backup by a part that may since have been swapped.
        self.state["fwmonitor"] = None
        self.ui.busy(False)
        self.fw_say_state()
        self.fw_report(payload)

    def pump_fw_backup(self, payload):
        self.ui.busy(False)
        self.fw_backup_report(payload)


def build(ui):
    """Make the tab, and hand its names to the window as before."""
    tab = FirmwareTab(ui)
    vars(ui).update(tab_names(tab))
    return tab
