"""The Error Log tab.

Built by run_gui through build(ui). `ui` carries the window's shared
names - state, the worker, busy, say and the rest - and is handed back
everything built here, so run_gui and the tests can reach it.
"""
import os
import time

import tds_err
from i18n import gettext as _
from tdstoolkit import model_of


def build(ui):
    EDGE = ui.EDGE
    filedialog = ui.filedialog
    messagebox = ui.messagebox
    named = ui.named
    root = ui.root
    says = ui.says
    state = ui.state
    tabs = ui.tabs
    tk = ui.tk
    ttk = ui.ttk
    w = ui.w

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

    def text_copy(widget):
        """What is highlighted in a read-only pane, onto the clipboard."""
        try:
            picked = widget.get("sel.first", "sel.last")
        except tk.TclError:
            return
        root.clipboard_clear()
        root.clipboard_append(picked)

    def text_menu(widget, evt):
        """Right-click over a read-only pane. Built each time rather
        than once, so it is in whatever language is current - the same
        way the file pane's menus are.

        Ctrl-C copies too - the key filter lets anything with Control
        through - but somebody reading a log with a mouse in their hand
        does not go looking for that.

        Two panes use this: the error log, and the self test results on
        the System tab. Both hold a line somebody wants to paste
        somewhere else, and neither can be typed into.
        """
        menu = tk.Menu(root, tearoff=0)
        menu.add_command(label=_("Copy"),
                         command=lambda: text_copy(widget),
                         state="normal" if widget.tag_ranges("sel")
                         else "disabled")
        try:
            menu.tk_popup(evt.x_root, evt.y_root)
        finally:
            menu.grab_release()

    def copyable(widget):
        """Give a read-only pane its right-click Copy."""
        widget.bind("<Button-3>", lambda e, w=widget: text_menu(w, e))

    copyable(errtxt)

    def show_errors(lines, count=None):
        """Put text in the pane. `lines` is already the finished text."""
        state["errlines"] = list(lines)
        state["errcount"] = count
        errtxt.delete("1.0", "end")
        errtxt.insert("1.0", "\n".join(lines))
        errtxt.see("1.0")
        if count is None:
            lbl_ecount.config(text="")
        elif count:
            lbl_ecount.config(text=_("%d entries") % count)
        else:
            lbl_ecount.config(text=_("No errors"))

    def do_err_get():
        """Read the log out, oldest first."""
        if state["busy"]:
            return
        ui.busy(True, "wait")
        ui.say(_("Reading the instrument's error log ..."))
        w.submit("err_entries", lambda k: k.err_entries())

    def show_err_result(payload):
        found = payload["entries"]
        head = ["%s" % (state.get("idn") or ""),
                _("Error log read %s") % time.strftime("%Y-%m-%d %H:%M")]
        if not found:
            # Jared's wording, and the right wording: an empty log is
            # the instrument saying nothing is wrong, not a failure.
            show_errors(head + ["", _("No errors")], 0)
            ui.say(_("No errors in the instrument's log"))
            return
        # Counted at the top rather than left for the reader to spot.
        # A memory fault is the one thing in here somebody is usually
        # looking for, and it is a line among thirty that reads much
        # like the rest of them.
        #
        # Said even when it is none. The count is made from a list of
        # sub-test names, so a firmware that spells one differently
        # would count zero - and a line that is only there when the
        # answer is non-zero makes "none" and "this did not run" look
        # exactly alike.
        head.append(_("%d entry(s) name a memory test failing")
                    % len(tds_err.memory_faults(found)))
        body = ["%4d  %s" % (i, text) for i, text in enumerate(found, 1)]
        show_errors(head + [""] + body + ["", _("End of errors")],
                    len(found))
        ui.say(_("%d entries read in %.1f s") % (len(found), payload["secs"]))

    def do_err_save():
        if not state.get("errlines"):
            messagebox.showinfo(_("Nothing to save"),
                                _("Download the error log first."))
            return
        path = filedialog.asksaveasfilename(
            parent=root, title=_("Save error log"), defaultextension=".txt",
            initialfile="%s_errorlog_%s.txt" % (
                (model_of(state.get("idn")) or "TDS").replace(" ", ""),
                time.strftime("%Y%m%d-%H%M%S")),
            filetypes=[(_("Text file"), "*.txt"),
                       (_("All files"), "*.*")])
        if not path:
            return
        try:
            # Text mode already turns "\n" into the platform's line end;
            # replacing it first as well wrote "\r\r\n" on Windows.
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(state["errlines"]))
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                     % (_("Could not save"), exc))
            return
        ui.say(_("Saved %s (%s bytes)")
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
        ui.busy(True, "wait")
        ui.say(_("Clearing the instrument's error log ..."))
        w.submit("err_clear", lambda k: k.err_clear())

    # The pump's handlers for this tab's jobs - see pump_jobs.
    def pump_err_available(payload):
        has_log = bool(payload.get("errlog"))
        state["errlog"] = has_log
        if not has_log:
            # Through state["cannot"], not by disabling the
            # buttons here: busy(False) re-enables everything
            # in the button set at the end of the next
            # operation, and would hand back a Download that
            # cannot work.
            ui.unavailable(btn_eget, btn_eclr)
            show_errors([_("This firmware has no error log.")])

    def pump_err_entries(payload):
        ui.busy(False)
        show_err_result(payload)

    def pump_err_clear(payload):
        ui.busy(False)
        show_errors([_("No errors")], 0)
        ui.say(_("The instrument's error log has been cleared"))

    vars(ui).update((k, v) for k, v in locals().items() if k != "ui")
