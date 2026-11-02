"""The System tab.

SystemTab is the tab. build(ui) makes it with the window (App in
tdstoolkit.py, which the tab calls `ui`), and the window keeps it by
name.
"""
import time
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk

import tds_wfm
from i18n import gettext as _
from tdstoolkit import (DIAG_AREAS, SYS_CHOICES, Worker, firmware_options,
                        log_note, model_of, resource)


class SystemTab(object):
    """The System tab: identity, options, clock, ports and diagnostics."""

    def __init__(self, ui):
        self.ui = ui
        self.EDGE = ui.EDGE
        self.copyable = ui.errors.copyable
        self.dialog_open = ui.dialog_open
        self.hints = ui.hints
        self.named = ui.named
        self.popup = ui.popup
        self.relabel = ui.relabel
        self.root = ui.root
        self.says = ui.says
        self.state = ui.state
        self.tabs = ui.tabs
        self.w = ui.w

        # --------------------------------------------------- the system tab
        # The instrument's own housekeeping: its clock, its calibration, its
        # self tests, where hardcopies go, and which options it believes it
        # has. Two columns of boxes rather than a scrolling page, because
        # every one of these is short and a person looking for "the one that
        # runs the self test" should be able to see all of them at once.
        self.systab = ttk.Frame(self.tabs)
        self.tabs.add(self.systab, text=_("System"))
        self.named(self.systab, "System")

        # Said twice and written once. The panel explains what the button
        # will do and the confirmation says the same thing back, and the two
        # drifting apart is how a warning stops being read.
        self.ERASE_SAYS = ("A Secure Erase writes zeroes over all memory "
                           "locations and returns all saved setups back to "
                           "factory settings.\n"
                           "Calibration data is left untouched.\n"
                           "THIS ACTION CANNOT BE UNDONE!")
        self.FACTORY_SAYS = ("This returns the instrument to its default "
                             "settings.\n"
                             "The GPIB address, calibration constants, and "
                             "any protected user data is left untouched.")

        # The explanations on this tab wrap at SYS_WRAP rather than at the
        # 300 pixels they were given when the columns were narrower: at 300
        # the right column ran the Options button off the bottom of a
        # 1280 x 800 window in German, Russian and Chinese. Twelve characters
        # for the labels beside the boxes, where eight cut "Ausrichtung"
        # and "Ориентация" short.
        SYS_WRAP = 440
        sysleft = ttk.Frame(self.systab)
        sysleft.pack(side="left", fill="both", expand=True, padx=(4, 3),
                     pady=6)
        sysright = ttk.Frame(self.systab)
        sysright.pack(side="left", fill="both", expand=True, padx=(3, 4),
                      pady=6)

        # ---- what it is
        sysbox1 = ttk.LabelFrame(sysleft, padding=8)
        self.says(sysbox1, "Instrument ID:")
        sysbox1.pack(fill="x")
        self.lbl_sysidn = ttk.Label(sysbox1, text="-", wraplength=300,
                                    justify="left")
        self.lbl_sysidn.pack(anchor="w")
        # What ID? says is still read - the options dialog greys what this
        # instrument does not offer, and the log records it - but it is not
        # shown here. On an instrument with no options it repeats *IDN?
        # word for word, which is two lines saying one thing.
        sysrow1 = ttk.Frame(sysbox1)
        sysrow1.pack(fill="x", pady=(8, 0))
        self.btn_sysread = ttk.Button(sysrow1, text=_("Read"), padding=(10, 2),
                                      command=lambda: self.do_sys_read())
        self.btn_sysread.pack(side="left")
        self.says(self.btn_sysread, "Read")
        self.btn_syslock = ttk.Button(sysrow1, text=_("Lock front panel"),
                                      padding=(10, 2),
                                      command=lambda: self.do_sys_lock(True))
        self.btn_syslock.pack(side="left", padx=4)
        self.says(self.btn_syslock, "Lock front panel")
        self.btn_sysfree = ttk.Button(sysrow1, text=_("Unlock front panel"),
                                      padding=(10, 2),
                                      command=lambda: self.do_sys_lock(False))
        self.btn_sysfree.pack(side="left")
        self.says(self.btn_sysfree, "Unlock front panel")

        # ---- its clock
        self.sys_rule(sysleft)
        sysbox2 = ttk.LabelFrame(sysleft, padding=8)
        self.says(sysbox2, "Date and time")
        sysbox2.pack(fill="x")

        self.SYS_DATE = (("sysyear", 4, "-"), ("sysmonth", 2, "-"),
                         ("sysday", 2, ""))
        self.SYS_TIME = (("syshour", 2, ":"), ("sysmin", 2, ":"),
                         ("syssec", 2, ""))
        self.sys_boxes(sysbox2, "Date", self.SYS_DATE)
        self.sys_boxes(sysbox2, "Time", self.SYS_TIME)

        self.state["sysclock"] = tk.BooleanVar(value=False)
        self.chk_sysclock = ttk.Checkbutton(
            sysbox2, variable=self.state["sysclock"],
            text=_("Show the clock on the instrument's screen"))
        self.chk_sysclock.pack(anchor="w", pady=(6, 0))
        self.says(self.chk_sysclock,
                  "Show the clock on the instrument's screen")
        # Apply above Synchronise, because they are two different things and
        # the one that sends what is in the boxes should be next to them.
        # Synchronise does not fill the boxes in and wait to be applied - it
        # sets the instrument from this computer's clock there and then, and
        # leaves whatever is typed above alone.
        self.btn_systime = ttk.Button(sysbox2, text=_("Apply"),
                                      padding=(10, 2),
                                      command=lambda: self.do_sys_clock())
        self.btn_systime.pack(anchor="w", pady=(8, 0))
        self.says(self.btn_systime, "Apply")
        self.btn_syssync = ttk.Button(sysbox2,
                                      text=_("Synchronise to this PC"),
                                      padding=(10, 2),
                                      command=lambda: self.do_sys_sync())
        self.btn_syssync.pack(anchor="w", pady=(4, 0))
        self.says(self.btn_syssync, "Synchronise to this PC")

        # ---- where hardcopies go
        self.sys_rule(sysleft)
        sysbox3 = ttk.LabelFrame(sysleft, padding=8)
        self.says(sysbox3, "Hardcopy and ports")
        sysbox3.pack(fill="x")
        self.state["sysport"] = tk.StringVar()
        self.state["sysformat"] = tk.StringVar()
        self.state["syslayout"] = tk.StringVar()
        # Tooltips for the hardcopy and RS-232 dropdowns, keyed like the loops
        # that build them, so both loops label their boxes from one place.
        _systips = {
            "sysport": "Which port hardcopy output is sent to",
            "sysformat": "The image format the instrument writes for hardcopy",
            "syslayout": "The page orientation of the hardcopy",
            "sysbaud": "RS-232 speed in bits per second (Option 13 only)",
            "sysparity": "RS-232 parity (Option 13 only)",
            "sysstop": "RS-232 stop bits (Option 13 only)"}
        for label, key in (("Port", "sysport"), ("Format", "sysformat"),
                           ("Layout", "syslayout")):
            self.values = SYS_CHOICES[key]
            line = ttk.Frame(sysbox3)
            line.pack(fill="x", pady=1)
            self.one = ttk.Label(line, text=_(label), width=12)
            self.one.pack(side="left")
            self.says(self.one, label)
            _cb = ttk.Combobox(line, textvariable=self.state[key],
                               values=self.values,
                               width=14, state="readonly")
            _cb.pack(side="left")
            self.hints(_cb, _systips[key])
        lbl_sysrs = ttk.Label(sysbox3, foreground="#555",
                              wraplength=SYS_WRAP,
                              justify="left")
        self.says(lbl_sysrs,
                  "The RS-232 settings below are Option 13 only. If not "
                  "fitted, these settings read back blank and the "
                  "instrument refuses them.")
        lbl_sysrs.pack(anchor="w", pady=(6, 2))
        self.state["sysbaud"] = tk.StringVar()
        self.state["sysparity"] = tk.StringVar()
        self.state["sysstop"] = tk.StringVar()
        for label, key in (("Baud", "sysbaud"), ("Parity", "sysparity"),
                           ("Stop bits", "sysstop")):
            self.values = SYS_CHOICES[key]
            line = ttk.Frame(sysbox3)
            line.pack(fill="x", pady=1)
            self.one = ttk.Label(line, text=_(label), width=12)
            self.one.pack(side="left")
            self.says(self.one, label)
            _cb = ttk.Combobox(line, textvariable=self.state[key],
                               values=self.values,
                               width=14, state="readonly")
            _cb.pack(side="left")
            self.hints(_cb, _systips[key])
        self.btn_sysports = ttk.Button(sysbox3, text=_("Apply"),
                                       padding=(10, 2),
                                       command=lambda: self.do_sys_ports())
        self.btn_sysports.pack(anchor="w", pady=(8, 0))
        self.says(self.btn_sysports, "Apply")

        # ---- calibration and self test
        sysbox4 = ttk.LabelFrame(sysright, padding=8)
        self.says(sysbox4, "Calibration and self test")
        sysbox4.pack(fill="x")
        lbl_sysspc = ttk.Label(sysbox4, foreground="#555",
                               wraplength=SYS_WRAP,
                               justify="left")
        self.says(lbl_sysspc, "Signal Path Compensation (SPC) takes several "
                  "minutes to complete. Please ensure instrument is "
                  "warmed up for 20 minutes and no input signals are "
                  "present.")
        lbl_sysspc.pack(anchor="w")
        self.btn_sysspc = ttk.Button(sysbox4,
                                     text=_("Signal Path Compensation"),
                                     padding=(10, 2),
                                     command=lambda: self.do_sys_spc())
        self.btn_sysspc.pack(anchor="w", pady=(6, 0))
        self.says(self.btn_sysspc, "Signal Path Compensation")
        sysrow4 = ttk.Frame(sysbox4)
        sysrow4.pack(fill="x", pady=(20, 0))
        self.state["sysdiag"] = tk.StringVar(value="ALL")
        lbl_sysdiag = ttk.Label(sysrow4, text=_("Self test"))
        lbl_sysdiag.pack(side="left", padx=(0, 6))
        self.says(lbl_sysdiag, "Self test")
        # Tektronix's own words, in the order somebody looking for a memory
        # fault wants them: everything, then the two areas that carry a
        # memory array, then the rest. The keywords are what the manual and
        # the instrument's own Utility menu say, so they are not
        # translated; the line underneath says what each one covers and is.
        cmb_sysdiag = ttk.Combobox(sysrow4, textvariable=self.state["sysdiag"],
                                   width=14, state="readonly",
                                   values=list(DIAG_AREAS))
        cmb_sysdiag.pack(side="left")
        self.hints(cmb_sysdiag,
                   "Which area of the instrument the self test checks")
        self.btn_sysdiag = ttk.Button(sysrow4, text=_("Run"), padding=(10, 2),
                                      command=lambda: self.do_sys_diag())
        self.btn_sysdiag.pack(side="left", padx=4)
        self.says(self.btn_sysdiag, "Run")
        self.lbl_sysdiagwhat = ttk.Label(sysbox4, foreground="#555",
                                         wraplength=SYS_WRAP,
                                         justify="left")
        self.lbl_sysdiagwhat.pack(anchor="w", pady=(4, 0))

        self.sys_diag_what()
        self.relabel.append(self.sys_diag_what)
        self.state["sysdiag"].trace_add("write", self.sys_diag_what)
        lbl_sysdiagsay = ttk.Label(sysbox4, foreground="#555",
                                   wraplength=SYS_WRAP,
                                   justify="left")
        self.says(lbl_sysdiagsay,
                  "Extended diagnostics performs a warm-boot and "
                  "takes a few minutes. All on screen data will be "
                  "lost. Each area runs functional, memory and "
                  "register tests. Anything that fails is listed "
                  "below, with what the error log added about "
                  "it.")
        lbl_sysdiagsay.pack(anchor="w", pady=(6, 0))
        # Sized in characters rather than filling the box, and sized for the
        # longest line the instrument actually produces: a 784D answers
        # "pass -- Cal Initialization (see error log)", which is 42
        # characters, so 46 leaves a little room without stretching the pane
        # across the whole tab. Nine rows for eight results and somewhere
        # for a ninth to appear rather than scroll out of sight.
        #
        # Wrapped rather than clipped, and with a scrollbar, because the
        # module list is no longer all that goes in here: the error log
        # lines that say what failed inside a module run to ninety
        # characters, and one of those cut off at the box edge is the half
        # of the sentence without the address in it.
        sysdiagf = ttk.Frame(sysbox4)
        sysdiagf.pack(anchor="w", pady=(6, 0))
        self.txt_sysdiag = tk.Text(sysdiagf, height=9, width=46, wrap="word",
                                   undo=False, font="TkFixedFont",
                                   background="#ffffff", highlightthickness=1,
                                   highlightbackground=self.EDGE)
        sysdiagbar = ttk.Scrollbar(sysdiagf, orient="vertical",
                                   command=self.txt_sysdiag.yview)
        self.txt_sysdiag.configure(yscrollcommand=sysdiagbar.set)
        self.txt_sysdiag.pack(side="left")
        sysdiagbar.pack(side="left", fill="y")
        self.txt_sysdiag.bind("<Key>", lambda e: (None if (e.state & 4) else
                                                  "break"))
        self.copyable(self.txt_sysdiag)
        # The one red on this tab. A failing module is what somebody is
        # looking for, and the instrument marks it itself: "++" where a
        # pass is "--".
        self.txt_sysdiag.tag_configure("bad", foreground="#a00")

        # ---- memory
        self.sys_rule(sysright)
        sysbox5 = ttk.LabelFrame(sysright, padding=8)
        self.says(sysbox5, "Memory")
        sysbox5.pack(fill="x")
        lbl_syswipe = ttk.Label(sysbox5, foreground="#555",
                                wraplength=SYS_WRAP,
                                justify="left")
        self.says(lbl_syswipe, self.ERASE_SAYS)
        lbl_syswipe.pack(anchor="w")
        sysrow5 = ttk.Frame(sysbox5)
        sysrow5.pack(fill="x", pady=(6, 0))
        self.btn_syswipe = ttk.Button(sysrow5, text=_("Secure erase..."),
                                      padding=(10, 2),
                                      command=lambda: self.do_sys_secure())
        self.btn_syswipe.pack(side="left")
        self.says(self.btn_syswipe, "Secure erase...")
        self.btn_sysfact = ttk.Button(sysrow5, text=_("Recall factory setup"),
                                      padding=(10, 2),
                                      command=lambda: self.do_sys_factory())
        self.btn_sysfact.pack(side="left", padx=4)
        self.says(self.btn_sysfact, "Recall factory setup")

        # ---- options
        self.sys_rule(sysright)
        sysbox6 = ttk.LabelFrame(sysright, padding=8)
        self.says(sysbox6, "Factory options")
        sysbox6.pack(fill="x")
        lbl_sysopts = ttk.Label(sysbox6, foreground="#555",
                                wraplength=SYS_WRAP,
                                justify="left")
        self.says(lbl_sysopts, "Enable and disable factory options.")
        lbl_sysopts.pack(anchor="w")
        self.btn_sysopts = ttk.Button(sysbox6, text=_("Options..."),
                                      padding=(10, 2),
                                      command=lambda: self.do_sys_options())
        self.btn_sysopts.pack(anchor="w", pady=(6, 0))
        self.says(self.btn_sysopts, "Options...")

        self.tabs.bind("<<NotebookTabChanged>>", self.sys_first_look, add="+")

    def sys_rule(self, parent):
        """A line between one section and the next."""
        ttk.Separator(parent, orient="horizontal").pack(fill="x",
                                                        pady=(10, 8))

    def sys_boxes(self, parent, label, parts):
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
        self.says(one, label)
        for key, wide, tail in parts:
            self.state[key] = tk.StringVar()
            ttk.Entry(line, textvariable=self.state[key], width=wide + 1,
                      justify="center").pack(side="left")
            if tail:
                ttk.Label(line, text=tail).pack(side="left", padx=2)

    def sys_clock_said(self, parts, gap):
        """The boxes, back together as the instrument wants them.

        Padded, so somebody who types 9 for September gets 09 rather
        than a refusal.
        """
        return gap.join(self.state[key].get().strip().zfill(wide)
                        for key, wide, _tail in parts)

    def sys_clock_show(self, text, parts, gap):
        """The instrument's answer, spread across the boxes."""
        bits = (text or "").split(gap)
        for i, (key, _wide, _tail) in enumerate(parts):
            self.state[key].set(bits[i].strip() if i < len(bits) else "")

    def sys_diag_what(self, *_a):
        """What the selected area covers.

        Written out with the sentences here rather than beside
        DIAG_AREAS so each one is a plain literal inside `_()`, which
        is what puts it in the nine catalogues. Read at every call, so
        it follows the language.

        The sub-tests named are the instrument's own, out of the
        firmware symbol table: digAcqMemAddrDiag, digAcqMemDataDiag,
        digAcqMemPatDiag and digAtSpeedAcqMemDiag with one lettered
        variant per channel; dsyRastModeV0Walk and V1Walk,
        dsyDiagRasRegMem and dsyDiagPPRegMem.
        """
        said = {
            "ALL": _("Every area below, one after another."),
            "ACQUISITION": _(
                "The acquisition system and its RAM: address, data, "
                "pattern and at-speed memory tests, one set per "
                "channel."),
            "DISPLAY": _(
                "The display system and the video RAM: walking-bit, "
                "register and pattern tests over the raster."),
            "CPU": _("The processor board: registers, interrupts and "
                     "FIFO."),
            "FPANEL": _("The front panel's own self tests."),
        }
        self.lbl_sysdiagwhat.config(text=said.get(self.state["sysdiag"].get(),
                                                  ""))

    def sys_diag_show(self, got):
        """The instrument's module list, and what its error log said.

        Two different things, one after the other. The module list is
        the instrument's summary and stops at "pass" or "fail" per
        module; the lines under it are the detail behind whichever
        module failed, and that is where a memory fault names its own
        sub-test, where in the array it was and which bits were wrong.
        A module that fails reads "(see error log)", so this is that
        sentence answered rather than left to the reader.
        """
        lines = [ln.rstrip() for ln in (got.get("log") or "").splitlines()
                 if ln.strip()]
        if not lines and got.get("flag"):
            lines = [got["flag"]]
        found = got.get("found") or []
        if found:
            lines += ["", _("From the error log:")] + list(found)
        # The diag executive stops an area at its first failure: a test
        # returning 0x00 passes and carries on, 0x44 and 0xA4 carry on
        # too, and anything else - 0xA3 is the usual one - jumps to the
        # next area. So the tests after the first failure never ran and
        # their absence from the list is not a pass. There is no
        # per-test selection over GPIB, so this cannot be worked around
        # from here: a second fault in the same area only shows up once
        # the first is fixed and it is run again.
        if lines and got.get("back") and not got.get("passed"):
            lines += ["", _("An area stops at its first failure, so the "
                            "tests after it in that area did not run. "
                            "What they would have found is not in this "
                            "list. Fix this one and run it again.")]
            # Said here as well as in the dialog three minutes ago,
            # because this is the moment it matters: the result is on
            # screen, the instrument looks dead, and the reason it looks
            # dead is that it is waiting at a prompt. Measured on a 754D
            # - a failing instrument stops at the log and stays there,
            # while answering the bus perfectly well.
            lines += ["", _("The instrument is left at its own diagnostic "
                            "log, waiting for CLEAR MENU on its front "
                            "panel. It answers this program either way, "
                            "but it will not return to a live trace until "
                            "that is pressed.")]
        self.txt_sysdiag.delete("1.0", "end")
        self.txt_sysdiag.insert("1.0", "\n".join(lines))
        self.txt_sysdiag.tag_remove("bad", "1.0", "end")
        for n, text in enumerate(lines, 1):
            if "++" in text or text.startswith("ERROR"):
                self.txt_sysdiag.tag_add("bad", "%d.0" % n, "%d.end" % n)
        self.txt_sysdiag.see("1.0")

    # The calibration constants used to be a box here. They are on the
    # Backup tab now, beside everything else that is copied off the
    # instrument and put back.

    def sys_show(self, now, clock=True):
        """Put what the instrument said into the tab's fields.

        `clock` False leaves the date and time boxes alone. Synchronise
        sends this computer's clock without touching them, and reading
        the instrument back afterwards would overwrite whatever somebody
        had typed there with the time that was just sent.
        """
        self.state["sysnow"] = now
        self.lbl_sysidn.config(text=now.get("idn") or _("not connected"))
        if clock:
            self.sys_clock_show(now.get("date"), self.SYS_DATE, "-")
            self.sys_clock_show(now.get("time"), self.SYS_TIME, ":")
        self.state["sysclock"].set(str(now.get("clock") or "0").strip()
                                   in ("1", "ON", "on"))
        # Expanded, not taken as read: an instrument answering in the
        # short form gives PORTR for PORTRAIT and NON for NONE, and a
        # readonly combobox showing a value that is not one of its
        # choices reads as a truncation.
        for key, field in (("sysport", "port"), ("sysformat", "format"),
                           ("syslayout", "layout")):
            self.state[key].set(tds_wfm.expand_keyword(now.get(field),
                                                       SYS_CHOICES[key]))
        rs = now.get("rs232") or {}
        for key, field in (("sysbaud", "baud"), ("sysparity", "parity"),
                           ("sysstop", "stopbits")):
            self.state[key].set(tds_wfm.expand_keyword(rs.get(field),
                                                       SYS_CHOICES[key]))

    def do_sys_read(self):
        self.ui.busy(True, "wait")
        self.ui.say(_("Reading the instrument's settings ..."))
        self.w.submit("sys_read", lambda k: k.sys_read())

    def sys_apply(self, lines, note):
        """Send a handful of settings and say how it went."""
        if not lines:
            return
        self.ui.busy(True, "wait")
        self.state["syssaid"] = note
        self.ui.say(note)
        self.w.submit("sys_send", lambda k, ls=list(lines): k.sys_send(ls))

    def do_sys_lock(self, shut):
        self.sys_apply(["LOCK ALL" if shut else "UNLOCK ALL"],
                       _("Locking the front panel ...") if shut else
                       _("Unlocking the front panel ..."))

    def do_sys_sync(self):
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
        self.state["syskeepclock"] = True
        self.sys_apply(['DATE "%s"' % date, 'TIME "%s"' % clock,
                        "DISPLAY:CLOCK %s"
                        % ("ON" if self.state["sysclock"].get() else "OFF")],
                       _("Setting the instrument to %(date)s %(time)s ...")
                       % {"date": date, "time": clock})

    def do_sys_clock(self):
        lines = []
        date = self.sys_clock_said(self.SYS_DATE, "-")
        clock = self.sys_clock_said(self.SYS_TIME, ":")
        # A row of empty boxes reads back as "0000-00-00", which the
        # instrument would take and be wrong about. Only sent when
        # somebody has actually put numbers in.
        if date.replace("-", "").strip("0"):
            lines.append('DATE "%s"' % date)
        if clock.replace(":", "").strip("0"):
            lines.append('TIME "%s"' % clock)
        lines.append("DISPLAY:CLOCK %s"
                     % ("ON" if self.state["sysclock"].get() else "OFF"))
        self.sys_apply(lines, _("Setting the instrument's clock ..."))

    def do_sys_ports(self):
        lines = []
        for key, cmd in (("sysport", "HARDCOPY:PORT"),
                         ("sysformat", "HARDCOPY:FORMAT"),
                         ("syslayout", "HARDCOPY:LAYOUT"),
                         ("sysbaud", "RS232:BAUD"),
                         ("sysparity", "RS232:PARITY"),
                         ("sysstop", "RS232:STOPBITS")):
            value = self.state[key].get().strip()
            if value:
                lines.append("%s %s" % (cmd, value))
        self.sys_apply(lines, _("Setting the ports ..."))

    def do_sys_spc(self):
        if not messagebox.askyesno(
                _("Signal Path Compensation"),
                _("SPC can take anything from a few minutes to a "
                  "quarter of an hour depending on the model, and "
                  "nothing else can use the bus until it finishes. It "
                  "carries on by itself if this program stops "
                  "waiting.\n"
                  "Ensure the instrument is warmed up and all input "
                  "signals are disconnected.") + "\n\n" + _("Proceed?")):
            return
        self.ui.busy(True, "wait")
        self.ui.say(_("Compensating the signal path - this takes minutes ..."))
        self.w.submit("sys_spc", lambda k: k.sys_spc())

    def do_sys_diag(self):
        area = self.state["sysdiag"].get() or "ALL"
        if not messagebox.askyesno(
                _("Run the self test?"),
                _("Extended diagnostics warm-boot the instrument and "
                  "whatever is on its screen is lost. It restarts "
                  "undisturbed for two and a half minutes before this "
                  "program says anything to it again, so allow about "
                  "three minutes in all.\n\nAn instrument that fails "
                  "stops at its diagnostic log and needs CLEAR MENU "
                  "pressed on its front panel before it is usable "
                  "again.") + "\n\n" + _("Proceed?")):
            return
        self.ui.busy(True, "wait")
        self.txt_sysdiag.delete("1.0", "end")
        self.ui.say(_("Running the %s self test ...") % area)
        self.w.submit("sys_diag", lambda k, a=area: k.sys_diag(a))

    def do_sys_secure(self):
        if not messagebox.askyesno(
                _("Secure erase"),
                _(self.ERASE_SAYS) + "\n\n" + _("Proceed?"),
                icon="warning", default="no"):
            return
        self.ui.busy(True, "wait")
        self.ui.say(_("Erasing the instrument's waveform and setup memory "
                      "..."))
        self.w.submit("sys_secure", lambda k: k.sys_secure())

    def do_sys_factory(self):
        if not messagebox.askyesno(
                _("Recall the factory setup?"),
                _(self.FACTORY_SAYS) + "\n\n" + _("Proceed?")):
            return
        self.ui.busy(True, "wait")
        self.ui.say(_("Recalling the factory setup ..."))
        self.w.submit("sys_factory", lambda k: k.sys_factory())

    def do_sys_options(self):
        """Read the option words, then show the dialog.

        Read first rather than as the dialog builds: it is ten queries
        over the bus, and the worker is where bus work belongs. The
        dialog opens from the pump when the answer arrives.
        """
        if self.dialog_open():
            return
        self.ui.busy(True, "wait")
        self.ui.say(_("Reading the option words ..."))
        self.w.submit("sys_option_read", lambda k: k.options_read())

    def sys_options_dialog(self, read):
        """The option words, with the switch that has to be moved first.

        A dialog rather than a panel on the tab. It is the one thing
        here that writes to non-volatile memory, it needs the cabinet
        opened first, and it is easier to explain in one place than in
        a box three inches wide.

        `read` is {code: bool} from the instrument's own words, or None
        if they could not be read.
        """
        if self.dialog_open():
            return
        # The ticks below are this instrument's. The dialog is not modal,
        # so another one can be connected while it is open - and Write
        # then sent this one's option set to that one, switching off
        # whatever this one did not have.
        opened_for = (self.state.get("addr"), self.state.get("idn"))
        dlg, pad = self.popup(_("Factory options"), resizable=(False, False))
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
            self.state["sysfigure"] = shot        # or Tk collects it
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

        # Some instruments say what they are carrying and some do not,
        # so the boxes are ticked from the instrument where that is
        # possible and left alone where it is not.
        #
        # *OPT? is the one that answers: a TDS 680B on v4.4.1e lists
        # "13:Rs232/cent,1M:extended record length,0,2F:math pack,0,
        # FD:1.44MB floppy drive,0,0,0,0,0,0". A 784D carrying Option 2C
        # says nothing useful to ID?, which is what this dialog was
        # written against and why it used to tick nothing at all.
        #
        # A box left unticked switches an option OFF, so nothing is
        # ticked on a guess: an instrument that did not answer gets the
        # warning it always got, and the reply is shown verbatim so what
        # was ticked can be checked against what the instrument said
        # rather than taken on trust.
        told = Worker.options_fitted(self.state.get("options"))
        if read is not None and told is not None:
            # Two readings of the same ten facts. They agreed on every
            # one on the instrument this was measured on, so a
            # disagreement means one of them is being read wrongly on
            # this model - and there is no way to tell which from here.
            # Nothing is ticked in that case: a wrong tick is a silent
            # option lost, and an empty dialog is only an inconvenience.
            clash = sorted(code for code, _w, _o, _d in Worker.OPTION_WORDS
                           if read.get(code) != (code in told))
            if clash:
                read = None
                log_note("options", "words and *OPT? disagree about %s"
                         % ", ".join(clash))
                ttk.Label(right, wraplength=330, justify="left", text=_(
                    "This instrument's option words and its *OPT? reply "
                    "disagree about %s, so nothing has been ticked for "
                    "you. Tick every option you want to keep, including "
                    "the ones already enabled.") % ", ".join(clash)).pack(
                        anchor="w")
        if read is not None:
            ttk.Label(right, wraplength=330, justify="left", text=_(
                "The ticks below were read from this instrument's own "
                "option words. Check them against its boot screen "
                "before writing: any option left unticked will be "
                "disabled.")).pack(anchor="w")
        elif told is not None:
            ttk.Label(right, wraplength=330, justify="left", text=_(
                "The ticks below are what this instrument reported when "
                "it was connected. Check them against its own boot "
                "screen before writing: any option left unticked will "
                "be disabled.")).pack(anchor="w")
        else:
            ttk.Label(right, wraplength=330, justify="left", text=_(
                "This instrument does not report which options are "
                "enabled via GPIB. Please ensure to tick all desired "
                "options INCLUDING currently enabled options. Any "
                "unticked options will be disabled.")).pack(anchor="w")
        if told is not None:
            ttk.Label(right, wraplength=330, justify="left",
                      foreground="#555", text=_("It answered *OPT? with:")
                      + "\n" + (self.state.get("options") or "")).pack(
                          anchor="w", pady=(4, 0))
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
        # 1G is held to Worker.OPTION_1G_MODELS by hand rather than by
        # that rule: it is an acquisition board identity rather than an
        # option flag, so the instruments it is not known to apply to
        # are greyed until somebody has tried it on them.
        able = firmware_options(self.state.get("idn"))
        # Spelled the way the catalogue spells it, not the way the
        # instrument says it. *IDN? answers "TDS 540B" with a space and
        # this compares against a bare model name, so without the strip
        # the test below is False on the one instrument it is meant to
        # be True on - which is what it silently was.
        model = model_of(self.state.get("idn")).replace(" ", "").upper()

        def offered(code):
            if code == "1G":
                return model in Worker.OPTION_1G_MODELS
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
            boxes[word] = (tk.BooleanVar(
                value=(read[code] if read is not None
                       else bool(told) and code in told)), on)
            ttk.Checkbutton(
                table, variable=boxes[word][0],
                style=("TCheckbutton" if offered(code)
                       else "Absent.TCheckbutton"),
                text="%s - %s%s" % (code, what,
                                    "" if code in Worker.OPTION_SOFT
                                    else _("  (needs hardware)"))
            ).pack(anchor="w")

        def write():
            if (self.state.get("addr"), self.state.get("idn")) != opened_for:
                messagebox.showerror(_("Error"), _(
                    "These options were read from %s, and a different "
                    "instrument is connected now. Nothing has been "
                    "written. Open Options... again to read this one.")
                    % (model_of(opened_for[1]) or opened_for[0]),
                    parent=dlg)
                dlg.destroy()
                return
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
            self.state.pop("dialog", None)
            self.ui.busy(True, "wait")
            self.ui.say(_("Writing the option words ..."))
            self.w.submit("sys_option", lambda k, ws=wants: k.sys_option(ws))

        row = ttk.Frame(right)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text=_("Write"), command=write).pack(side="right")
        ttk.Button(row, text=_("Close"),
                   command=dlg.destroy).pack(side="right", padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.bind("<Destroy>",
                 lambda e: self.state.pop("dialog", None)
                 if e.widget is dlg else None)
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (self.root.winfo_rootx() + 60,
                                 self.root.winfo_rooty() + 40))
        self.state["dialog"] = dlg

    def sys_first_look(self, _evt=None):
        """Read the instrument when this tab is looked at.

        Everything once, on the first look. Not on connect: a dozen
        queries every time somebody opens the program is a second
        nobody asked for, and most sessions never come to this tab at
        all.

        The clock every time after that. It is the one field here that
        is stale the moment it is read, and showing yesterday's time
        beside a Synchronise button invites somebody to correct a clock
        that was never wrong. Two queries, and only while nothing else
        is using the bus.
        """
        try:
            if self.tabs.select() != str(self.systab):
                return
        except tk.TclError:                  # before the notebook is up
            return
        if not self.state.get("idn"):
            return
        if self.state.get("sysnow") is None and not self.state["busy"]:
            self.do_sys_read()
        elif not self.state.get("busy"):
            self.w.submit("sys_clock_read", lambda k: k.sys_clock_read())

    # The pump's handlers for this tab's jobs - see pump_jobs.
    def pump_sys_read(self, payload):
        self.ui.busy(False)
        self.sys_show(payload)
        self.ui.say(_("Read the instrument's settings"))

    def pump_sys_option_read(self, payload):
        self.ui.busy(False)
        # payload is None when a word would not read, and
        # the dialog opens anyway with nothing ticked - the
        # option words are not the only way to work out
        # what an instrument is carrying, and refusing to
        # show the dialog would be worse than showing it
        # the way it always looked.
        self.ui.say(_("Read the option words") if payload is not None
                    else _("The option words would not read"))
        self.sys_options_dialog(payload)

    def pump_sys_clock_read(self, payload):
        # The two clock fields and nothing else, and nothing
        # said on the status line: this runs every time the
        # tab is opened, and a message each time would be
        # noise about something nobody asked for.
        self.sys_clock_show(payload.get("date"), self.SYS_DATE, "-")
        self.sys_clock_show(payload.get("time"), self.SYS_TIME, ":")
        if self.state.get("sysnow"):
            self.state["sysnow"]["date"] = payload.get("date")
            self.state["sysnow"]["time"] = payload.get("time")

    def pump_sys_send(self, payload):
        self.ui.busy(False)
        self.sys_show(payload["now"],
                      clock=not self.state.pop("syskeepclock", False))
        refused = payload["refused"]
        if refused:
            # Named, with what the instrument said about
            # each. "Some settings were refused" is not
            # something anybody can act on.
            self.ui.say(_("%(what)s - %(n)d refused")
                        % {"what": self.state.get("syssaid") or _("Sent"),
                           "n": len(refused)})
            messagebox.showwarning(
                _("The instrument refused these"),
                "\n".join("%s\n    %s" % (line, why)
                          for line, why in refused))
        else:
            self.ui.say(_("%d setting(s) sent and read back")
                        % payload["sent"])

    def pump_sys_spc(self, payload):
        self.ui.busy(False)
        if payload.get("stillgoing"):
            # Not a failure. The instrument is still
            # compensating; this program has only stopped
            # waiting to hear about it.
            self.ui.say(_("Signal path compensation is still "
                          "running on the instrument"))
            messagebox.showinfo(
                _("Still compensating"),
                _("The instrument has been compensating for "
                  "%d minutes and has not finished.\n\nThat "
                  "is not a failure - it is still working, "
                  "and it will finish on its own. Wait until "
                  "the instrument's own screen says it is "
                  "done before using it, then run the self "
                  "test on this tab if you want the verdict.")
                % int(payload.get("waited", 0) / 60))
        else:
            self.ui.say(_("Signal path compensation passed") if
                        payload["passed"] else
                        _("Signal path compensation returned %s - "
                          "anything but 0 is a failure")
                        % payload["result"])
        if not payload["passed"] and not payload.get(
                "stillgoing"):
            messagebox.showwarning(
                _("Compensation did not pass"),
                _("*CAL? returned %s. Zero means it passed; "
                  "anything else means the instrument could "
                  "not compensate itself, and the self test "
                  "on this tab will say more about why.")
                % payload["result"])

    def pump_sys_diag(self, payload):
        self.ui.busy(False)
        self.sys_diag_show(payload)
        if not payload.get("back"):
            # It was left alone for two and a half minutes
            # and asked slowly after that, so this is no
            # longer the ordinary "still booting" case it
            # used to be. Two things are worth saying and
            # neither can be told apart from the bus.
            self.ui.say(_("The instrument has not answered since the "
                          "self test started. Look at its screen: a "
                          "diagnostic log wants CLEAR MENU, and a "
                          "dark or frozen one wants its power "
                          "cycling."))
            messagebox.showwarning(
                _("The instrument has not come back"),
                _("The self test warm-boots the instrument. "
                  "It was left alone for two and a half "
                  "minutes and has still not answered.\n\n"
                  "Look at the instrument's own screen. If "
                  "it is showing a diagnostic log, press "
                  "CLEAR MENU to let it finish starting up. "
                  "If it is frozen, switch it off and on."
                  "\n\nNothing is lost either way. The "
                  "result and the error log survive, so "
                  "press Run again once it is back to read "
                  "them."))
        elif payload["passed"]:
            self.ui.say(_("Self test %s: everything passed")
                        % payload["area"])
        elif payload.get("found"):
            # Said in words rather than as the flag. With
            # VERBOSE off the flag is the three characters
            # "FAI", which is not a sentence to put in
            # front of somebody.
            self.ui.say(_("Self test %(area)s failed - the error "
                          "log added %(n)d line(s), shown below")
                        % {"area": payload["area"],
                           "n": len(payload["found"])})
        else:
            self.ui.say(_("Self test %s failed") % payload["area"])

    def pump_sys_secure(self, payload):
        self.ui.busy(False)
        if payload["failed"]:
            self.ui.say(_("Secure erase failed - the instrument could "
                          "not verify the memory afterwards"))
        elif payload["passed"]:
            self.ui.say(_("Secure erase done - every waveform and "
                          "setup memory is zeroed"))
        else:
            # Neither event arrived. Said plainly rather
            # than assumed either way: this one is not a
            # thing to be vague about.
            self.ui.say(_("Secure erase sent, but the instrument "
                          "raised neither the pass nor the fail "
                          "event - events were %s")
                        % (", ".join(payload["events"]) or _("none")))
        self.do_sys_read()

    def pump_sys_factory(self, payload):
        self.ui.busy(False)
        self.ui.say(_("The instrument is back on its factory setup"))
        self.do_sys_read()

    def pump_sys_option(self, payload):
        self.ui.busy(False)
        self.ui.say(_("Wrote %(n)d option word(s)%(bad)s - power "
                      "cycle the instrument to see what took")
                    % {"n": len(payload["sent"]),
                       "bad": (_(", %d refused")
                               % len(payload["refused"]))
                       if payload["refused"] else ""})
        log_note("system", "option words: sent %s, refused %s"
                 % (payload["sent"], payload["refused"]))


def build(ui):
    """Make the tab; the window keeps what this returns."""
    return SystemTab(ui)
