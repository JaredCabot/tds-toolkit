"""The Limits tab: envelope templates drawn round a reference and tested live.

Built by run_gui through build(ui). `ui` carries the window's shared
names - state, the worker, busy, say and the rest - and is handed back
everything built here, so run_gui and the tests can reach it.
"""
import os

import tds_msk
import tds_wfm
from i18n import gettext as _
from tdstoolkit import (APPDIR, LEARN_HANDLES, LEARN_LEAST, LEARN_MOST,
                        LEFT_PANE, log_note, stamped, typed)


def build(ui):
    EDGE = ui.EDGE
    do_msk_behind = ui.do_msk_behind
    do_msk_redo = ui.do_msk_redo
    do_msk_setup = ui.do_msk_setup
    do_msk_trace = ui.do_msk_trace
    do_msk_undo = ui.do_msk_undo
    edit_bindings = ui.edit_bindings
    edit_grid = ui.edit_grid
    edit_redraw = ui.edit_redraw
    edit_shapes = ui.edit_shapes
    edit_tools = ui.edit_tools
    filedialog = ui.filedialog
    flowing = ui.flowing
    forget_source = ui.forget_source
    hints = ui.hints
    messagebox = ui.messagebox
    msk_describe = ui.msk_describe
    msk_forget = ui.msk_forget
    msk_may_discard = ui.msk_may_discard
    msk_remember = ui.msk_remember
    msk_shot_behind = ui.msk_shot_behind
    msk_shot_image = ui.msk_shot_image
    named = ui.named
    plot_colours = ui.plot_colours
    relabel = ui.relabel
    root = ui.root
    says = ui.says
    state = ui.state
    tabs = ui.tabs
    tk = ui.tk
    tkfont = ui.tkfont
    trace_colour = ui.trace_colour
    ttk = ui.ttk
    verdict_stamp = ui.verdict_stamp
    w = ui.w
    wave_scales = ui.wave_scales

    # ----------------------------------------------------------- limits
    # The other way to judge a signal, and the one every instrument in
    # this family has whether the mask option was fitted or not: show it
    # one that is known good, say how far a later one may wander, and it
    # writes the envelope itself. No polygons, no percent of the
    # graticule, nothing to draw before there is something to test.
    #
    # Kept apart from the masks tab because it is a different job with
    # different words. A mask is a shape somebody draws; a template is a
    # signal the instrument learns. Sharing one editor between them was
    # what made the mask tab's limit route hard to explain.
    limtab = ttk.Frame(tabs)
    tabs.add(limtab, text=_("Limits"))
    named(limtab, "Limits")

    ltop = ttk.Frame(limtab)
    ltop.pack(fill="x", padx=2, pady=(6, 2))
    lrow1 = ttk.Frame(ltop)
    lrow1.pack(fill="x")
    lrow2 = ttk.Frame(ltop)
    # The same six verbs as the Masks tab's toolbar, in the same order:
    # what a template is, then what it was drawn against, then the
    # picture of it. Everything that reaches the instrument's
    # references - sending an envelope, reading one back, starting and
    # stopping a test, emptying a reference - sits beside the list of
    # references instead, the way the Masks tab keeps its transfers
    # beside the segments they move.
    btn_lnew = ttk.Button(ltop, text=_("New template"), padding=(10, 2),
                          command=lambda: do_lim_new())
    btn_lsavefile = ttk.Button(ltop, text=_("Save template as..."),
                               padding=(10, 2),
                               command=lambda: do_lim_file_save())
    btn_lsetup = ttk.Button(ltop, text=_("Save setup..."), padding=(10, 2),
                            command=lambda: do_msk_setup("lmask"))
    btn_ldelfile = ttk.Button(ltop, text=_("Delete template"),
                              padding=(10, 2),
                              command=lambda: do_lim_file_delete())
    btn_llearn = ttk.Button(ltop, text=_("Learn template"),
                            padding=(10, 2),
                            command=lambda: do_lim_learn())
    # The Masks tab's two, doing the same two things to this tab's
    # drawing: a trace to draw against, and the instrument's own screen
    # for when it is in DPO and there is no record to read.
    btn_ltrace = ttk.Button(ltop, text=_("Load trace..."), padding=(10, 2),
                            command=lambda: do_msk_trace("l"))
    btn_lgrab = ttk.Button(ltop, text=_("Capture screen"), padding=(10, 2),
                           command=lambda: do_msk_behind("l"))
    btn_lview = ttk.Button(ltop, text=_("Save image..."), padding=(10, 2),
                           command=lambda: do_lim_view_save())
    btn_lrefresh = ttk.Button(ltop, text=_("Refresh"), padding=(10, 2),
                              command=lambda: do_lim_refresh())
    # Load trace before Learn template, which is the order they are
    # used in: a template is learnt from a trace, and the trace has to
    # be there first.
    LIM_LEFT = (btn_lnew, btn_lsavefile, btn_lsetup, btn_ldelfile,
                btn_ltrace, btn_llearn, btn_lgrab, btn_lview)
    LIM_RIGHT = (btn_lrefresh,)
    lim_flow = flowing(ltop, lrow1, lrow2, LIM_LEFT, LIM_RIGHT, "lflow")
    ltop.bind("<Configure>", lim_flow)

    lbody = ttk.Frame(limtab)
    lbody.pack(fill="both", expand=True, padx=2, pady=4)

    # One column down the left, the way the Masks tab has one: the
    # library at the top, what the instrument is holding at the bottom,
    # and between them the controls a template is built from.
    #
    # The three groups are packed before they are filled, because the
    # order is the design and it is worth being able to see it in one
    # place. The two lower ones are packed first and take the height
    # they ask for; the library gets what is left, which is what its
    # scrollbar is for. Packed the other way round, a short window
    # would take its bite out of the references instead - and those are
    # the rows this tab is steered by.
    llistf = ttk.Frame(lbody, width=LEFT_PANE)
    llistf.pack_propagate(False)          # the one width - see LEFT_PANE
    llistf.pack(side="left", fill="y", padx=(0, 8))
    lpcg = ttk.Frame(llistf)
    lopts = ttk.Frame(llistf)
    lrefg = ttk.Frame(llistf)
    lrefg.pack(side="bottom", fill="x")
    lopts.pack(side="bottom", fill="x", pady=(10, 12))
    lpcg.pack(side="top", fill="both", expand=True)

    lbl_lpc = ttk.Label(lpcg)
    says(lbl_lpc, "Saved on this computer")
    lbl_lpc.pack(side="top", anchor="w")
    # Saving, deleting and the setup beside a template are all in the
    # toolbar now, where the Masks tab keeps the same three. What is
    # left under the list is what the Masks tab has under its own: how
    # to open one.
    lbl_lpcnote = ttk.Label(lpcg, foreground="#555", wraplength=190,
                            justify="left")
    says(lbl_lpcnote, "Double click to edit.")
    lbl_lpcnote.pack(side="top", anchor="w", pady=(0, 2))
    lpcf = ttk.Frame(lpcg)
    lpcf.pack(side="top", fill="both", expand=True, pady=(2, 0))
    lpc = ttk.Treeview(lpcf, columns=("what",), show="tree headings",
                       height=4, selectmode="browse")
    lpc.column("#0", width=110, stretch=False, anchor="w")
    lpc.column("what", width=110, stretch=True, anchor="w")
    lpc_bar = ttk.Scrollbar(lpcf, orient="vertical", command=lpc.yview)
    lpc.configure(yscrollcommand=lpc_bar.set)
    lpc_bar.pack(side="right", fill="y")
    lpc.pack(side="left", fill="both", expand=True)

    state["lsource"] = tk.StringVar(value="CH1")
    state["ldest"] = tk.StringVar(value="REF1")
    state["lvert"] = tk.StringVar(value="0.5")
    state["lhorz"] = tk.StringVar(value="0.2")

    # Label beside the box rather than over it. Three stacked pairs
    # cost sixty pixels of height that this column no longer has to
    # spare, and none of the three labels is long enough to need the
    # width - the German ones are the longest and still fit.
    lbl_lsource = ttk.Label(lopts, text=_("Signal"))
    says(lbl_lsource, "Signal")
    lbl_lsource.grid(row=1, column=0, sticky="w", pady=(0, 4))
    cmb_lsource = ttk.Combobox(lopts, textvariable=state["lsource"],
                               width=8, state="readonly")
    cmb_lsource.grid(row=1, column=1, sticky="w", padx=(8, 0),
                     pady=(0, 4))

    # Divisions, because that is what the instrument's own command
    # takes and, as it happens, what a person can see: half a division
    # is half a division on the glass. Editable rather than readonly -
    # 0 to 5 is the instrument's range and anything in it is allowed.
    lbl_lvert = ttk.Label(lopts, text=_("Vertical tolerance"))
    says(lbl_lvert, "Vertical tolerance")
    lbl_lvert.grid(row=2, column=0, sticky="w", pady=(0, 4))
    cmb_lvert = ttk.Combobox(lopts, textvariable=state["lvert"], width=8,
                             values=("0", "0.1", "0.2", "0.5", "1", "2"))
    cmb_lvert.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(0, 4))

    lbl_lhorz = ttk.Label(lopts, text=_("Horizontal tolerance"))
    says(lbl_lhorz, "Horizontal tolerance")
    lbl_lhorz.grid(row=3, column=0, sticky="w", pady=(0, 10))
    cmb_lhorz = ttk.Combobox(lopts, textvariable=state["lhorz"], width=8,
                             values=("0", "0.1", "0.2", "0.5", "1", "2"))
    cmb_lhorz.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(0, 10))
    # A tooltip rather than a line of grey under the boxes. The column
    # is a column of controls now that the list and the references are
    # in it as well, and three paragraphs of explanation in it were
    # what pushed the references off the bottom of a short window.
    # Section 5 of the manual is where this is written down.
    for box in (cmb_lvert, cmb_lhorz):
        hints(box, "Both in divisions of the graticule, 0 to 5.")

    # How closely the drawn envelope follows what the instrument
    # learnt. The band is 250 columns and nobody can drag 500 handles, so
    # it is thinned - but how far is a judgement about this signal, not
    # a constant. Dragging this re-thins what is already on the
    # graticule from the band underneath it, so the cost of the
    # choice can be seen rather than guessed at.
    lbl_ldetail = ttk.Label(lopts)
    says(lbl_ldetail, "Detail")
    lbl_ldetail.grid(row=4, column=0, columnspan=2, sticky="w")
    # The slider keeps the full width of the column: it is dragged, and
    # a hundred pixels of travel for four hundred and eighty handles is
    # not something anybody can place.
    ldetail = ttk.Frame(lopts)
    ldetail.grid(row=5, column=0, columnspan=2, sticky="we", pady=(2, 0))
    state["ldetail"] = tk.IntVar(value=LEARN_HANDLES)
    lbl_ldnum = ttk.Label(ldetail, width=4, anchor="e", foreground="#555")
    lbl_ldnum.pack(side="right")
    scl_ldetail = ttk.Scale(ldetail, from_=LEARN_LEAST, to=LEARN_MOST,
                            orient="horizontal")
    scl_ldetail.pack(side="left", fill="x", expand=True)
    hints(scl_ldetail, "Handles to spend on the envelope. More follows "
                       "the signal more closely and is more to edit.")

    def lim_detail(value=None):
        """The slider moved: re-thin the drawn envelope from the band.

        From the band the instrument learnt, not from the drawing -
        thinning something already thinned only ever loses more. One
        undo entry for the whole drag, taken when it starts.

        The number beside the slider is the handles the envelope got,
        not the budget it was given. The two can only differ by a
        handle or so now that the travel ends where the band runs out -
        see lim_range - but it is the count that is the useful number
        to show, and it is the count the label carries.

        Rounded to the nearest ten rather than down, so that the last
        notch reaches the far end rather than stopping a few short of
        it. A slider whose end cannot be got to is the fault this is
        the second half of.
        """
        top = int(float(scl_ldetail.cget("to")))
        want = int(round(float(value if value is not None
                               else state["ldetail"].get())))
        want = min(top, max(LEARN_LEAST, (want + 5) // 10 * 10))
        state["ldetail"].set(want)
        got = want
        if state.get("lband") and state.get("lmask") is not None:
            lim_take_band(remember=False)
            edit_redraw()
            got = len(state["lmask"].points)
        lbl_ldnum.config(text=str(got))

    scl_ldetail.config(command=lim_detail)
    scl_ldetail.set(LEARN_HANDLES)
    scl_ldetail.bind("<ButtonPress-1>", lambda e: msk_remember())
    lim_detail(LEARN_HANDLES)

    def lim_dest_changed(_evt=None):
        """A different reference is a different question.

        What is known about REF1 says nothing about REF2, so the
        knowledge goes back to "not read". Without this, clearing one
        reference would leave Start greyed for every other one as well.

        Selecting a row does not read it. A single click is how the
        destination is chosen and people click through a list to look
        at it; the read is on the double-click, where it was asked for.
        """
        found = {one["name"]: one for one in (state.get("lrefs") or [])}
        one = found.get(state["ldest"].get())
        state["lhas"] = None if one is None else bool(one["columns"])
        lim_buttons()

    # What each of the four references is holding, and everything that
    # is done to one - the way the Masks tab lists the instrument's
    # eight segments with its transfers above them.
    #
    # There is no Destination dropdown any more. The selected row is
    # the destination: one list that says both what is there and which
    # one is being worked on, rather than a list and a picker that can
    # disagree about it.
    larrows = ttk.Frame(lrefg)
    larrows.pack(fill="x", pady=(0, 2))
    # The two directions on the line they move an envelope across, the
    # same glyphs and the same style as the Masks tab's pair. Down to
    # the instrument, up from it.
    lcentre = ttk.Frame(larrows)
    lcentre.pack(side="top")
    btn_lsend = ttk.Button(lcentre, text="↓", width=3,
                           style="Arrow.TButton",
                           command=lambda: do_lim_send())
    btn_lload = ttk.Button(lcentre, text="↑", width=3,
                           style="Arrow.TButton",
                           command=lambda: do_lim_load())
    btn_lsend.pack(side="left", padx=(0, 3))
    btn_lload.pack(side="left", padx=(3, 0))
    # The test itself, under the transfers: it is not a transfer, and
    # the two halves of it are one decision, so they share a line.
    lrunrow = ttk.Frame(larrows)
    lrunrow.pack(side="top", pady=(6, 0))
    btn_lstart = ttk.Button(lrunrow, text=_("Start test"), padding=(8, 0),
                            command=lambda: do_lim_start())
    btn_lstart.pack(side="left")
    btn_lstop = ttk.Button(lrunrow, text=_("Stop test"), padding=(8, 0),
                           command=lambda: do_lim_stop())
    btn_lstop.pack(side="left", padx=(4, 0))
    # Not New template: that one clears the graticule and leaves the
    # instrument alone. This one deletes what the instrument is
    # holding. Both are "clear" in English and only one can be undone,
    # so this is the one that carries the word and asks first.
    btn_ldestclear = ttk.Button(larrows, text=_("Clear"), padding=(6, 0),
                                command=lambda: do_lim_dest_clear())
    btn_ldestclear.pack(side="top", pady=(6, 0))
    says(btn_ldestclear, "Clear")

    lbl_lrefs = ttk.Label(lrefg)
    says(lbl_lrefs, "On the instrument")
    lbl_lrefs.pack(anchor="w", pady=(6, 0))
    lrefsf = ttk.Frame(lrefg)
    lrefsf.pack(fill="x", pady=(2, 0))
    lrefs = ttk.Treeview(lrefsf, columns=("holds",), show="tree headings",
                         height=4, selectmode="browse")
    lrefs.column("#0", width=70, stretch=False, anchor="w")
    lrefs.column("holds", width=130, stretch=True, anchor="w")
    lrefs.pack(side="left", fill="x", expand=True)

    def lim_refs_headings():
        lrefs.heading("#0", text=_("Reference"))
        lrefs.heading("holds", text=_("Holds"))

    lim_refs_headings()
    relabel.append(lim_refs_headings)

    # Greyed until it has been read. Nothing is read when the tab opens
    # - four references are four bus reads and, on a bench where three
    # of them are empty, three refusals - so a row starts saying it does
    # not know, and says so in grey. Double-clicking one reads that one.
    lrefs.tag_configure("unread", foreground="#888")

    def lim_refs_show(found=None):
        """Fill the list. A name missing from `found` has not been read."""
        known = {one["name"]: one for one in (found or [])}
        for name in tds_wfm.REFS:
            if not lrefs.exists(name):
                lrefs.insert("", "end", iid=name, text=name)
            one = known.get(name)
            if one is None:
                said = _("not read")
            elif one["columns"]:
                said = _("%d column(s)") % one["columns"]
            else:
                said = _("empty")
            lrefs.set(name, "holds", said)
            lrefs.item(name, tags=() if one is not None else ("unread",))
        here = state["ldest"].get()
        if here and lrefs.exists(here):
            lrefs.selection_set(here)

    lim_refs_show()

    def lim_refs_note(name, columns):
        """Record what one reference holds, without reading it again.

        Clearing a reference and writing one both say exactly what that
        reference now holds, so the list is corrected from what just
        happened rather than by surveying all four again.

        The list accumulates one row at a time, because that is how it
        is filled: a reference nobody has double-clicked is not in it
        and shows as not read.
        """
        found = state.get("lrefs")
        if found is None:
            found = state["lrefs"] = []
        for one in found:
            if one["name"] == name:
                one["columns"] = columns
                break
        else:
            found.append({"name": name, "columns": columns})
        lim_refs_show(found)

    def lim_refs_picked(_evt=None):
        picked = (lrefs.selection() or [None])[0]
        if picked and picked != state["ldest"].get():
            state["ldest"].set(picked)
            lim_dest_changed()

    def lim_refs_open(_evt=None):
        """Double-click: read that one reference, and only that one."""
        picked = (lrefs.identify_row(_evt.y) if _evt is not None
                  else (lrefs.selection() or [None])[0])
        if picked and lrefs.exists(picked):
            state["ldest"].set(picked)
            do_lim_survey([picked])

    lrefs.bind("<<TreeviewSelect>>", lim_refs_picked, add="+")
    lrefs.bind("<Double-Button-1>", lim_refs_open, add="+")

    # ------------------------------------------- saved limit templates
    def lim_folder():
        """Where limit templates are kept on this computer.

        Beside the program and beside the masks folder, for the same
        reason that one is there: a library is only a library if it is
        always in the same place.
        """
        here = os.path.join(APPDIR, "limits")
        try:
            if not os.path.isdir(here):
                os.makedirs(here)
        except OSError as exc:
            log_note("limits", "%s unusable (%s)" % (here, exc))
            return None
        return here

    def lim_file_buttons():
        """The four toolbar buttons that are about files rather than
        the instrument. New is not among them: starting again is
        always allowed, and there is nothing to be connected to."""
        drawn = state.get("lmask")
        btn_lsavefile.config(state="normal" if drawn is not None
                             and drawn.points else "disabled")
        # The setup is read off the instrument and written beside the
        # template's own file, so it needs both to exist.
        btn_lsetup.config(state="normal"
                          if state.get("cannot") is not None
                          and drawn is not None and (drawn.origin or "")
                          else "disabled")
        btn_ldelfile.config(state="normal" if lpc.selection()
                            else "disabled")

    def do_lim_scan():
        """Re-read the library of saved templates."""
        here = lim_folder()
        lpc.delete(*lpc.get_children())
        state["lpcfiles"] = {}
        for name in sorted(os.listdir(here) if here else []):
            if not name.upper().endswith(tds_msk.LIMIT_SUFFIXES):
                continue
            path = os.path.join(here, name)
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
                one = tds_msk.load_limit(raw,
                                         name=os.path.splitext(name)[0])
            except Exception as exc:
                lpc.insert("", "end", iid=name, text=name,
                           values=(_("will not read: %s") % exc,))
                continue
            state["lpcfiles"][name] = path
            lpc.insert("", "end", iid=name, text=one.name or name,
                       values=(msk_describe(one),))
        lim_file_buttons()

    def do_lim_file_open(_evt=None):
        """Put a saved template on the canvas."""
        chosen = list(lpc.selection())
        if not chosen:
            return
        path = (state.get("lpcfiles") or {}).get(chosen[0])
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
            one = tds_msk.load_limit(raw,
                                     name=os.path.splitext(chosen[0])[0])
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                 % (_("Could not open that template"),
                                    exc))
            return
        # A mask that has been renamed rather than written here. It will
        # load - it is the same format - but it means the opposite, so
        # it is worth one question rather than a silent inversion.
        # Tektronix's own .ENV is not that case: it IS a limit template,
        # and asking about it would be asking about the right answer.
        if not tds_msk.is_limit_file(raw) and not tds_msk.looks_like_env(raw):
            if not messagebox.askyesno(
                    _("Open this as a limit template?"),
                    _("%s was not written as a limit template. A mask "
                      "says where the signal may NOT go; a limit "
                      "template says where it MAY.\n\nOpened here it "
                      "will mean the opposite of what it meant where it "
                      "was drawn.\n\nOpen it anyway?")
                    % os.path.basename(path),
                    icon="warning", default="no"):
                return
        one.origin = path
        state["lmask"] = one
        # The old drawing's history is not this one's. msk_forget works
        # on whichever drawing is in front - see edit_key - so this is
        # the limits history it clears, not the mask tab's.
        msk_forget()
        # "mpicks", not "lpicks": the selection is shared between the
        # two drawings - see the note on edit_here - so a key of its
        # own here cleared nothing, and the handles picked in the old
        # template stayed picked in the new one.
        state["mpicks"] = []
        draw_limits()
        lim_buttons()
        lim_file_buttons()
        ui.say(_("Opened %s") % os.path.basename(path))

    def do_lim_file_save():
        """Write the drawing to the library."""
        drawn = state.get("lmask")
        if drawn is None or not drawn.points:
            return
        here = lim_folder()
        if not here:
            messagebox.showerror(_("Error"),
                                 _("The limits folder could not be "
                                   "made."))
            return
        path = filedialog.asksaveasfilename(
            parent=root, title=_("Save the limit template"),
            initialdir=here, defaultextension=tds_msk.LIMIT_SUFFIX,
            initialfile=(drawn.name or "limit") + tds_msk.LIMIT_SUFFIX,
            filetypes=[(_("Limit template"),
                        "*" + tds_msk.LIMIT_SUFFIX)])
        if not path:
            return
        try:
            with open(path, "wb") as fh:
                fh.write(tds_msk.save_limit_bytes(drawn))
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                 % (_("Could not save"), exc))
            return
        drawn.origin = path
        drawn.name = os.path.splitext(os.path.basename(path))[0]
        # Saved is not changed any more, and msk_may_discard reads this
        # to tell a save that happened from one that was called off.
        state["ldirty"] = False
        do_lim_scan()
        lim_buttons()
        ui.say(_("Saved %s") % os.path.basename(path))

    def do_lim_file_delete():
        """Remove a saved template from the library."""
        chosen = list(lpc.selection())
        if not chosen:
            return
        path = (state.get("lpcfiles") or {}).get(chosen[0])
        if not path:
            return
        if not messagebox.askyesno(
                _("Delete this template?"),
                _("Delete %s from this computer?") % chosen[0],
                default="no", parent=root):
            return
        try:
            os.remove(path)
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                 % (_("Could not delete"), exc))
            return
        do_lim_scan()
        ui.say(_("Deleted %s") % chosen[0])

    def lim_pc_headings():
        lpc.heading("#0", text=_("Template"))
        lpc.heading("what", text=_("Shape"))

    lim_pc_headings()
    relabel.append(lim_pc_headings)
    lpc.bind("<Double-Button-1>", do_lim_file_open)
    lpc.bind("<<TreeviewSelect>>", lambda e: lim_file_buttons())
    do_lim_scan()

    # The same drawing tools as the masks tab, acting on this tab's own
    # drawing. One editor, two drawings - see edit_here. The tool in
    # hand, the selection and the view boxes are shared, because only
    # one of the two tabs can be in front and two sets of them would be
    # two things to keep in step; the history and the unsaved mark are
    # not, because those belong to the drawing.
    lrightf = ttk.Frame(lbody)
    lrightf.pack(side="left", fill="both", expand=True)

    ltoolbar = ttk.Frame(lrightf)
    ltoolbar.pack(fill="x", pady=(0, 3))
    btn_lundo = ttk.Button(ltoolbar, style="Toolbutton", padding=3,
                           command=lambda: do_msk_undo())
    btn_lredo = ttk.Button(ltoolbar, style="Toolbutton", padding=3,
                           command=lambda: do_msk_redo())
    btn_lundo.pack(side="left")
    btn_lredo.pack(side="left", padx=(2, 0))
    state.setdefault("undobtn", {}).update({"lundo": btn_lundo,
                                            "lredo": btn_lredo})
    hints(btn_lundo, "Undo")
    hints(btn_lredo, "Redo")
    # The verdict at the right of the same row, directly over the corner
    # of the graticule the badge is stamped in - so the word and the
    # stamp are read as one thing rather than two that have to be
    # reconciled. A limit test reports a failure by stopping the
    # instrument, so this word is the whole of the result.
    limface = tkfont.nametofont("TkDefaultFont").copy()
    limface.configure(size=max(11, abs(limface.cget("size")) + 3),
                      weight="bold")
    state.setdefault("fontcopies", []).append(limface)
    lbl_lverdict = ttk.Label(ltoolbar, font=limface)
    lbl_lverdict.pack(side="right")
    # No wraplength: this is a row now, not a column, and a sentence
    # given a wrap width in a row that has the height of one line is a
    # sentence with its second half cut off.
    lbl_lsays = ttk.Label(ltoolbar, foreground="#555", anchor="e")
    lbl_lsays.pack(side="right", padx=(10, 8))

    lcanvasrow = ttk.Frame(lrightf)
    lcanvasrow.pack(fill="both", expand=True)
    ltools = edit_tools(lcanvasrow, "l")

    lplot = tk.Canvas(lcanvasrow,
                      background=tds_wfm.DEFAULT_COLOURS["background"],
                      highlightthickness=1, highlightbackground=EDGE)
    lplot.pack(side="left", fill="both", expand=True)
    lplot.bind("<Configure>", lambda e: draw_limits())
    edit_bindings(lplot)

    # The same boxes, on the same variables: Snap to grid means the same
    # thing on both tabs and is one setting, not two that drift apart.
    # Only the last one is worded differently, because what it hides
    # here is not a mask.
    lbar = ttk.Frame(lrightf)
    lbar.pack(fill="x", pady=(4, 0))
    lrow1, lrow2 = ttk.Frame(lbar), ttk.Frame(lbar)
    lrow1.pack(fill="x")
    lgridpair = ttk.Frame(lbar)
    lbl_lgrid = ttk.Label(lgridpair, text=_("Grid spacing, divisions"))
    says(lbl_lgrid, "Grid spacing, divisions")
    lbl_lgrid.pack(side="left")
    ent_lgrid = ttk.Combobox(lgridpair, textvariable=state["mgrid"], width=5,
                             values=("0.1", "0.2", "0.25", "0.5", "1"))
    ent_lgrid.pack(side="left", padx=(4, 0))
    ent_lgrid.bind("<<ComboboxSelected>>", lambda e: edit_redraw())
    ent_lgrid.bind("<Return>", lambda e: edit_redraw())
    linfo = ttk.Label(lrightf, anchor="w", foreground="#555")
    linfo.pack(fill="x", pady=(2, 0))
    lchecks = {}
    for key, english in (("mshowgrid", "Show grid"),
                         ("msnap", "Snap to grid"),
                         ("mgratic", "Graticule"),
                         ("mcross", "Crosshairs"),
                         ("mfill", "Filled"),
                         ("mhide", "Hide envelope"),
                         ("mnodots", "Hide points")):
        cb = ttk.Checkbutton(lbar, text=_(english), variable=state[key],
                             command=lambda: edit_redraw())
        says(cb, english)
        lchecks[key] = cb
    lbar.bind("<Configure>", flowing(
        lbar, lrow1, lrow2, (lgridpair,) + tuple(lchecks.values()), (),
        "lflow", gap=10))
    # Explicit hints() calls so the translation audit sees each literal.
    hints(lchecks["mshowgrid"], "Show or hide the drawing grid")
    hints(lchecks["msnap"], "Snap points to the grid")
    hints(lchecks["mgratic"], "Show or hide the graticule")
    hints(lchecks["mcross"], "Show crosshairs at the pointer")
    hints(lchecks["mfill"], "Fill the envelope rather than outline it")
    hints(lchecks["mhide"], "Hide the envelope to see the trace behind it")
    hints(lchecks["mnodots"], "Hide the draggable points")

    def lim_live():
        """The instrument's channels, which is all a template can learn
        from - a reference is what one is written into."""
        return [s for s in (state.get("wsources") or [])
                if not s.startswith("REF")] or ["CH1"]

    def lim_scan():
        """Put whatever the instrument reported into the two lists."""
        live, refs = lim_live(), list(state.get("wrefs") or tds_wfm.REFS)
        cmb_lsource.config(values=live)
        if state["lsource"].get() not in live:
            state["lsource"].set(live[0])
        if state["ldest"].get() not in refs:
            state["ldest"].set(refs[0] if refs else "REF1")
        # Which row is selected is what the destination is now, so the
        # list is put straight rather than a dropdown's values.
        lim_refs_show(state.get("lrefs"))
        lim_buttons()

    def lim_band():
        """The template as one closed polygon in percent of graticule.

        Percent because that is what the canvas and the PNG export both
        already take, so the band is worked out once and drawn twice
        rather than in two places that can drift apart. It is drawn
        against the live trace's own scale: the template was learnt
        from that channel, so the two share it.
        """
        wave, band = state.get("lwave"), state.get("lband") or []
        if wave is None or len(band) < 2:
            return []
        view = tds_wfm.PlotView([wave])
        ymult = wave.number("YMULT", 1.0) or 1.0
        yzero, yoff = wave.number("YZERO"), wave.number("YOFF")
        per = 100.0 / tds_wfm.DIVS_Y

        def place(seconds, volts):
            level = (volts - yzero) / ymult + yoff
            return (min(100.0, max(0.0, (seconds - view.first)
                                   / view.span * 100.0)),
                    min(100.0, max(0.0, 50.0 + per * view.divisions_of_level(
                        wave, level))))

        return ([place(t, hi) for t, _lo, hi in band]
                + [place(t, lo) for t, lo, _hi in reversed(band)])

    def lim_badge():
        """(words, colour) for the running test, or None if none is.

        The same pair the Masks tab stamps on its own plot, and the
        same two colours, because they mean the same thing and reading
        one tab should teach you the other. Nothing is drawn when no
        test is running: an empty graticule with PASS on it says the
        signal was judged, and nothing has been.
        """
        held = state.get("lrun")
        if not held:
            return None
        return ((_("PASS"), "#3fb950") if held.get("running")
                else (_("FAIL"), "#e04a4a"))

    def lim_stamp():
        """Draw the PASS/FAIL badge on the plot, or clear it.

        Its own function, and called from lim_verdict rather than from
        draw_limits, because the verdict changes without the canvas
        being redrawn: a test fails when the instrument stops, which
        arrives at the pump, and nothing about that touches the
        drawing. Stamped inside draw_limits it showed the verdict as of
        the last thing that happened to redraw the canvas - which, in
        practice, meant the last mouse click on it.

        The frame is measured here rather than passed in, so the stamp
        lands in the same corner whether it is drawn with the rest of
        the plot or on its own.
        """
        lplot.delete("verdict")
        badge = lim_badge()
        if badge is None:
            return
        words, colour = badge
        # The same stamp, in the same corner, at the same size as the
        # one on the Masks tab. See draw_masks.
        _l, top, right, _b = tds_wfm.plot_frame(
            max(lplot.winfo_width(), 80), max(lplot.winfo_height(), 60),
            20, room=8)
        verdict_stamp(lplot, right, top, words, colour)

    def lim_verdict():
        """What the test is saying, in one word and one colour.

        The one place the verdict is put on screen, in both the words
        below the plot and the stamp on it, so the two cannot disagree.
        """
        held = state.get("lrun")
        if not held:
            lbl_lverdict.config(text=_("Not testing"), foreground="#555")
            lbl_lsays.config(text=_("No test is running."))
        elif held.get("running"):
            lbl_lverdict.config(text=_("PASS"), foreground="#1a7f37")
            lbl_lsays.config(text=_("%(source)s is inside the template "
                                    "in %(dest)s.")
                             % {"source": held["source"],
                                "dest": held["dest"]})
        else:
            lbl_lverdict.config(text=_("FAIL"), foreground="#c62828")
            lbl_lsays.config(text=_("The instrument stopped, so "
                                    "%(source)s left the template.")
                             % {"source": held["source"]})
        lim_stamp()

    def draw_limits(_evt=None):
        """The graticule, the template band, the trace, and the drawing.

        In that order, and the order is the argument: the band is what
        the instrument is judging against now, the trace is what it is
        judging, and the area drawn on top is what will replace the band
        when it is sent. Seeing all three at once is the point of the
        tab - the drawing is only worth adjusting against the signal it
        has to pass.
        """
        lplot.delete("all")
        pick = plot_colours(None)
        lplot.configure(background=pick["background"])
        wide = max(lplot.winfo_width(), 80)
        tall = max(lplot.winfo_height(), 60)
        frame = tds_wfm.plot_frame(wide, tall, 20, room=8)
        # The instrument's own screen, if one was captured, furthest
        # back of all - the same place and the same reason as on the
        # Masks tab. See msk_shot_image.
        shot = msk_shot_image("l", frame)
        if shot is not None:
            lplot.create_image(frame[0], frame[1], image=shot,
                               anchor="nw", tags="shot")
        if state["mgratic"].get():
            for element, x0, y0, x1, y1, thick in tds_wfm.graticule(
                    wide, tall, 20, room=8):
                lplot.create_line(x0, y0, x1, y1, fill=pick[element],
                                  width=thick)
        edit_grid(lplot, pick, frame)
        band = lim_band()
        if len(band) > 2:
            flat = []
            for x, y in band:
                px, py = tds_msk.to_canvas(x, y, frame)
                flat += [px, py]
            # Stippled rather than solid: the trace and the drawing both
            # have to stay readable through it, and a Tk canvas has no
            # transparency.
            lplot.create_polygon(*flat, fill=pick["grid"],
                                 outline=pick["grid"], width=1,
                                 stipple="gray25", tags="band")
        wave = state.get("lwave")
        if wave is not None:
            xy, _bounds = tds_wfm.plot_geometry(wave, wide, tall, 20,
                                                None, room=8)
            if len(xy) > 1:
                flat = []
                for x, y in xy:
                    flat += [x, y]
                lplot.create_line(*flat, fill=trace_colour(wave, pick),
                                  width=1, tags="trace")
        # Stippled, for the same reason the band above is: the trace is
        # a one-pixel line and a solid fill over it leaves nothing to
        # see, whatever the drawing order says. Raising the trace puts
        # it on top; the stipple is what makes being on top mean
        # anything.
        edit_shapes(lplot, state.get("lmask"), pick, frame,
                    joins=False, stipple="gray50")
        # The trace over the envelope rather than under it: a filled
        # envelope hid the very thing it is drawn around. The handles
        # come back up over both, since they are what is dragged.
        lplot.tag_raise("trace")
        lplot.tag_raise("handle")
        lplot.tag_raise("number")
        lim_verdict()
        say_limits()

    def say_limits():
        """The line under the canvas: what is drawn, and what it means."""
        mask = state.get("lmask")
        if mask is None or not mask.filled():
            linfo.config(text=_(
                "Nothing drawn. Load a trace and press Learn template "
                "to draw the envelope round it, or use the pen."))
            return
        lower, _upper, gaps = tds_msk.to_band(mask)
        limited = sum(1 for v in lower if v is not None)
        words = [_("%(shapes)d shape(s), %(points)d point(s)")
                 % {"shapes": len(mask.filled()),
                    "points": len(mask.points)},
                 _("%d%% of the width is limited")
                 % round(limited * 100.0 / tds_msk.ENV_COLUMNS)]
        if gaps:
            # A limit test allows one band. Said here rather than only
            # at the moment of sending, because it is a property of the
            # drawing and the drawing is on screen.
            words.append(_("the gap in %d column(s) will be closed up")
                         % gaps)
        linfo.config(text="  -  ".join(words))

    def lim_range():
        """Point the far end of the slider at what this band can use.

        A band is thinned until it is followed exactly, and then it is
        finished: more budget buys nothing. Where that happens is a
        property of the signal, not of the program - a square wave is
        done in thirty handles and a sine is never done at all - so a
        fixed far end leaves whatever is left over as travel that does
        nothing, which is what this looked like from the outside.

        Each edge is thinned once, at the most it could ever be given.
        Twice the busier of the two is the budget that buys both, since
        the slider hands half to each. Once a band, not once a drag:
        the thinning is the expensive part of the drag as it is.
        """
        band = lim_band()
        half = len(band) // 2
        top = LEARN_MOST
        if half >= 3:
            top = 2 * max(len(tds_msk.thinned(band[:half], half - 1,
                                              outward=1)),
                          len(tds_msk.thinned(band[half:], half - 1,
                                              outward=-1)))
            top = min(LEARN_MOST, max(LEARN_LEAST, top))
        scl_ldetail.config(to=top)
        if state["ldetail"].get() > top:
            scl_ldetail.set(top)

    def lim_take_band(remember=True):
        """Put the template the instrument just learnt on the canvas.

        Thinned to as many handles as the slider asks for: 250 columns
        is five hundred handles and not a shape anybody can drag. The
        instrument's own band stays drawn underneath, so what the
        thinning cost is there to be seen rather than taken on trust.

        `remember` False for re-thinning under the slider, where one
        undo entry a pixel of travel is not history, it is noise.
        """
        band = lim_band()
        if len(band) < 6:
            return
        half = len(band) // 2
        # Half the handles to each edge, and how many in all is the
        # slider's business rather than a constant: an envelope has no
        # limit of its own, and a limits drawing is not kept in mask
        # segments. See edit_points.
        #
        # Short of the edge's own length, always. Ask thinned for as
        # many handles as there are columns and it hands the band
        # straight back, every collinear point of it - five hundred
        # handles, which is the thing the slider exists to avoid.
        most = max(2, min(half - 1, state["ldetail"].get() // 2))
        if remember:
            msk_remember()
        # Away from the band, never into it: the upper edge is
        # first and nothing may be drawn above it, the lower edge
        # second and nothing below. Thinned plainly, the corners
        # come in and the template fails the signal it was learnt
        # from - measured on a 784D. See tds_msk._outward.
        state["lmask"] = tds_msk.Mask(
            segments=[tds_msk.thinned(band[:half], most, outward=1)
                      + tds_msk.thinned(band[half:], most,
                                        outward=-1)],
            source=state["lsource"].get())
        state["mpicks"] = []
        state.pop("mdrawing", None)

    def lim_may_draw(loading):
        """May a template that has just been read go onto the canvas?

        A read this tab asked for on its own account - opening the tab,
        or looking to see whether Start is worth offering - may only
        fill an empty canvas. Overwriting somebody's drawing with one
        of those would be a poor way to repay opening the tab. "asked"
        is the ↑ button, where replacing the drawing is the whole point
        and has already been agreed to.
        """
        drawn = state.get("lmask")
        return bool(loading) and (loading == "asked" or drawn is None
                                  or not drawn.points)

    def do_lim_new():
        """Start again on an empty graticule.

        More than "Clear the envelope" was, which is why that button is
        gone: that one took the drawing off and left the instrument's
        own band and the live trace lying underneath it, which is most
        of what is on the graticule. This clears all three, the way New
        mask starts a blank mask on the other tab.

        The instrument is not touched. What a reference holds is what
        the arrows and Clear are for, and a test that is running goes
        on running.
        """
        if not msk_may_discard():
            return
        state["lmask"] = tds_msk.Mask(name=_("New template"),
                                      source=state["lsource"].get())
        msk_forget()
        state["mpicks"] = []
        state.pop("mdrawing", None)
        state.pop("mhover", None)
        # The band read out of the reference, the trace read off the
        # channel or loaded from a file, and any captured screen behind
        # them. All three are pictures of a moment that has passed, and
        # a new template is not drawn against any of them.
        state["lband"] = []
        state["lwave"] = None
        state.pop("lwavefrom", None)
        state.pop("lshot", None)
        state.pop("lshotimage", None)
        lim_buttons()
        edit_redraw()
        ui.say(_("A new template - the graticule is empty"))

    def lim_lines(mask, dest, source):
        """The SCPI for the drawn envelope, or None with the reason shown."""
        wave = state.get("lwave")
        if wave is None:
            messagebox.showinfo(
                _("Read the signal first"),
                _("A limit template is built to the channel it will "
                  "judge - its record length, its volts a division and "
                  "its position. Press Refresh, then send this again."))
            return None
        lower, _upper, gaps = tds_msk.to_band(mask)
        if all(v is None for v in lower):
            messagebox.showinfo(_("Nothing to send"),
                                _("Nothing is drawn on the graticule "
                                  "yet."))
            return None
        if gaps and not messagebox.askyesno(
                _("Close the gap?"),
                "%s\n\n%s" % (
                    _("A limit test allows the signal one band, and "
                      "this drawing leaves two in %d column(s) - "
                      "clear air with something drawn above and below "
                      "it.") % gaps,
                    _("Sent, that air becomes part of the allowed "
                      "envelope. Send it that way?")),
                parent=root, default="no"):
            return None
        return tds_msk.envelope_scpi(mask, wave.pre, dest=dest,
                                     source=source, width=wave.width,
                                     how="allowed")

    def do_lim_send():
        """Write what is drawn into the reference, as a limit template."""
        mask = state.get("lmask")
        if state["busy"] or state.get("cannot") is None or mask is None:
            return
        dest, source = state["ldest"].get(), state["lsource"].get()
        lines = lim_lines(mask, dest, source)
        if lines is None:
            return
        numbers = []
        for cmd in lines:
            if cmd.startswith("CURVE "):
                numbers = [int(v) for v in cmd[len("CURVE "):].split(",")]
        ui.busy(True, "wait")
        ui.say(_("Sending the envelope to %s ...") % dest)
        w.submit("lim_send", lambda k, ln=lines, d=dest, nu=numbers,
                 a=source: k.msk_envelope(ln, d, nu, a))

    def lim_buttons():
        joined = state.get("cannot") is not None
        running = bool(state.get("lrun"))
        # Learn builds from a loaded trace when there is one, which
        # needs no instrument at all - so it stays live off the bus.
        btn_llearn.config(state="normal" if not running
                          and (joined or lim_loaded_trace() is not None)
                          else "disabled")
        # Greyed when the destination is known to hold no template -
        # there is nothing for the instrument to judge against, so the
        # test cannot run. `lhas` is None until something has actually
        # read or written that reference, and not knowing is not a
        # reason to refuse: greying a Start that would have worked is a
        # worse fault than offering one that reports why it did not.
        # The certain case is the one just after Clear.
        empty = state.get("lhas") is False
        btn_lstart.config(state="normal" if joined and not running
                          and not empty else "disabled")
        btn_lstop.config(state="normal" if joined and running
                         else "disabled")
        # Not while a test is running: the destination is what the
        # instrument is judging against, and deleting it underneath a
        # running test is not something to find out about afterwards.
        btn_ldestclear.config(state="normal" if joined and not running
                              else "disabled")
        # Reading a reference back is harmless at any time, the way the
        # Masks tab's ↑ is: it asks the instrument a question and
        # replaces what is drawn here, and neither of those is
        # something a running test minds.
        btn_lload.config(state="normal" if joined else "disabled")
        btn_lrefresh.config(state="normal" if joined else "disabled")
        btn_lgrab.config(state="normal" if joined else "disabled")
        drawn = state.get("lmask")
        # Something on the graticule to make a picture of: the drawing,
        # the band the reference holds, a trace, or a captured screen.
        # The same rule as the Masks tab's Save image.
        btn_lview.config(state="normal" if (drawn is not None
                                            and drawn.points)
                         or state.get("lband")
                         or state.get("lwave") is not None
                         or state.get("lshot") else "disabled")
        btn_lsend.config(state="normal" if joined and drawn is not None
                         and drawn.filled() else "disabled")
        # Save follows the drawing too, so the two cannot disagree about
        # whether there is anything to save.
        lim_file_buttons()

    def lim_tolerances():
        """The two numbers, or None with the complaint already made."""
        try:
            vertical = typed(state["lvert"].get())
            horizontal = typed(state["lhorz"].get())
        except ValueError:
            vertical = horizontal = -1.0
        if not (0.0 <= vertical <= 5.0 and 0.0 <= horizontal <= 5.0):
            messagebox.showerror(
                _("Error"),
                _("How far the signal may wander is given in divisions "
                  "of the graticule, from 0 to 5 - half a division is "
                  "0.5."))
            return None
        return vertical, horizontal

    def lim_loaded_trace():
        """The trace Load trace put on the graticule, if there is one.

        Not the one Refresh reads off the channel: learning a template
        from the instrument's own record by way of this program would
        be the instrument's own Learn with a round trip added.
        """
        wave = state.get("lwave")
        return wave if (wave is not None
                        and state.get("lwavefrom") == "pc") else None

    def lim_learn_here(wave, vertical, horizontal):
        """The envelope a loaded trace and two tolerances make.

        The same shape the instrument's own LIMIT:TEMPLATE STORE makes,
        worked out here instead: every sample is allowed to arrive
        `horizontal` divisions early or late and to sit `vertical`
        divisions high or low, so the band at each column is the
        highest and lowest the signal reaches anywhere in that window,
        widened by the vertical tolerance.

        Returns [(seconds, low volts, high volts)], which is what
        lim_band and lim_lines already take.

        ponytail: a plain window scan, O(points x window). 500 points
        is nothing; if a 15,000-point record at five divisions ever
        feels slow, a running min-max deque is the upgrade.
        """
        volts = [v for _t, v in wave.points()]
        times = [t for t, _v in wave.points()]
        if len(volts) < 2:
            return []
        xincr = abs(wave.number("XINCR", 1.0)) or 1.0
        # A division of this graticule, which draws the whole record
        # across ten of them, and of the instrument's screen too when
        # the record is the 500 points it shows.
        reach = int(round(horizontal * wave.seconds_per_div / xincr))
        margin = vertical * (wave.volts_per_div or 1.0)
        out = []
        for i, t in enumerate(times):
            lo = max(0, i - reach)
            hi = min(len(volts), i + reach + 1)
            window = volts[lo:hi]
            out.append((t, min(window) - margin, max(window) + margin))
        return out

    def do_lim_learn():
        """Build a template - from the loaded trace, or from the scope.

        A trace that was loaded from this computer is what the person
        chose to look at, so it is what a template is built from. With
        nothing loaded there is still the instrument, which does the
        job itself, and that is offered rather than done quietly: the
        two produce different templates from different signals and
        which one happened should not have to be guessed at afterwards.
        """
        if state["busy"]:
            return
        wander = lim_tolerances()
        if wander is None:
            return
        source, dest = state["lsource"].get(), state["ldest"].get()
        wave = lim_loaded_trace()
        if wave is not None:
            state["lband"] = lim_learn_here(wave, wander[0], wander[1])
            state["lhas"] = None       # nothing has been sent yet
            lim_range()
            lim_take_band()
            lim_buttons()
            draw_limits()
            ui.say(_("Template built from the loaded trace - send it to "
                     "%s to test against it") % dest)
            return
        if state.get("cannot") is None:
            messagebox.showinfo(
                _("Load a trace first"),
                _("Learn template builds an envelope round a trace. "
                  "Load one with Load trace..., or connect to an "
                  "instrument and it will build one from the signal it "
                  "is showing."))
            return
        if not messagebox.askyesno(
                _("Learn from the instrument?"),
                _("No trace has been loaded, so there is nothing here "
                  "to build from.\n\nHave %(source)s build a template "
                  "from the signal it is showing now, into %(dest)s?")
                % {"source": source, "dest": dest}, parent=root):
            return
        ui.busy(True, "wait")
        state["llearn"] = True
        ui.say(_("Making a template of %(source)s in %(dest)s ...")
               % {"source": source, "dest": dest})
        w.submit("lim_build", lambda k, s=source, v=wander[0],
                 h=wander[1], d=dest: k.lim_build(s, v, h, d))

    def do_lim_start():
        """Judge the signal against the template, from now on."""
        if state["busy"] or state.get("cannot") is None:
            return
        ui.busy(True, "wait")
        ui.say(_("Starting the limit test ..."))
        w.submit("lim_run", lambda k, s=state["lsource"].get(),
                 d=state["ldest"].get(): k.lim_run(s, d))

    def do_lim_dest_clear():
        """Delete the destination reference on the instrument.

        Distinct from do_lim_new, which clears the graticule and
        touches nothing on the instrument. This is the same
        job the Waveforms tab's Delete uses, with the same warning,
        because it is the same irreversible thing: the instrument has
        no undo, and a template that has been deleted is gone unless it
        was saved to a file first.
        """
        if state["busy"] or state.get("cannot") is None:
            return
        dest = (state["ldest"].get() or "").strip()
        if not dest.upper().startswith("REF"):
            return
        if not messagebox.askyesno(
                _("Clear %s") % dest,
                _("Delete what %s holds on the instrument?\n\n"
                  "This operation cannot be undone.\n"
                  "Save the template to a file first if it is needed.")
                % dest,
                default="no", parent=root):
            return
        ui.busy(True, "wait")
        ui.say(_("Clearing %s on the instrument ...") % dest)
        w.submit("lim_clear", lambda k, n=dest: k.wfm_delete([n]))

    def do_lim_stop():
        """Switch the test off and give the instrument back."""
        if state["busy"] or state.get("cannot") is None:
            return
        state.pop("lrun", None)
        ui.busy(True, "wait")
        w.submit("lim_stop", lambda k: k.limit_stop())

    def lim_look():
        """Find out what the destination holds, rather than assume.

        Until something has read that reference the program does not
        know whether it holds a template, and Start cannot honestly be
        greyed on a guess - see lim_buttons. This is what turns the
        guess into knowledge. `lload` marks the read as one the user did
        not ask for, so its result can be put on the canvas when there
        is nothing drawn there yet.
        """
        if state.get("cannot") is None or state["busy"]:
            return
        state["lload"] = True
        do_lim_refresh()

    def do_lim_load():
        """Read the selected reference's template onto the canvas.

        The other direction of the arrow beside it, and the thing the
        tab could only do by accident before: a read it had not been
        asked for would draw a template that was already there, but
        only onto an empty canvas - see lim_may_draw. Asked for by
        hand it may replace what is drawn, having said so first,
        because that drawing is somebody's work. One undo puts it back
        either way.
        """
        if state["busy"] or state.get("cannot") is None:
            return
        drawn = state.get("lmask")
        if (drawn is not None and drawn.points
                and not messagebox.askyesno(
                    _("Replace the drawing?"),
                    _("What is drawn here is replaced by the template "
                      "the instrument is holding in %s.")
                    % state["ldest"].get(),
                    parent=root, default="no")):
            return
        state["lload"] = "asked"
        do_lim_refresh()

    # Opening this tab reads nothing. It used to survey all four
    # references and then read the selected one's template and capture
    # a trace, which is five bus operations for a tab somebody may have
    # opened to look at a saved file - and on an instrument with three
    # empty references, three refusals in the event log for it. The
    # rows start greyed and saying they have not been read; a
    # double-click reads the one that was double-clicked.

    def do_lim_survey(names=None):
        """Read what the named references hold. None means all four."""
        if state.get("cannot") is None or state["busy"]:
            return
        ui.busy(True, "wait")
        if names:
            ui.say(_("Reading what %s holds ...") % ", ".join(names))
        else:
            ui.say(_("Reading what the references hold ..."))
        w.submit("lim_survey", lambda k, n=names: k.lim_survey(n))

    def do_lim_refresh():
        """Read the template and the live signal again, and redraw."""
        if state["busy"] or state.get("cannot") is None:
            return
        ui.busy(True, "wait")
        ui.say(_("Reading the template and the signal ..."))
        w.submit("lim_picture", lambda k, s=state["lsource"].get(),
                 d=state["ldest"].get(): k.lim_picture(s, d))

    def do_lim_view_save():
        """This graticule as a PNG, the way the other tabs save theirs.

        Everything that is on it: the band the reference holds, the
        envelope drawn over it, the trace, the captured screen behind
        them and the verdict stamp. The Masks tab saves its graticule
        the same way, and for the same reason - the picture is the
        evidence, and one missing half of it proves nothing.
        """
        wave = state.get("lwave")
        drawn = state.get("lmask")
        band = lim_band()
        shapes = ([band] if len(band) > 2 else []) + (
            [seg for _n, seg in drawn.filled()] if drawn is not None
            else [])
        if wave is None and not shapes and not state.get("lshot"):
            messagebox.showinfo(_("Nothing to save"),
                                _("Press Refresh to read the template "
                                  "and the signal first."))
            return
        path = filedialog.asksaveasfilename(
            parent=root, title=_("Save image"), defaultextension=".png",
            initialfile=stamped(state["ldest"].get() or "limit", ".png"),
            filetypes=[(_("PNG image (*.png)"), "*.png")])
        if not path:
            return
        try:
            with open(path, "wb") as fh:
                fh.write(tds_wfm.plot_png(
                    [wave] if wave is not None else [],
                    width=state["pngsize"][0],
                    height=state["pngsize"][1],
                    colours=state.get("colours"),
                    caption=wave_scales(wave) if wave is not None else "",
                    shapes=shapes,
                    verdict=lim_badge(),
                    behind=msk_shot_behind("l")))
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                 % (_("Could not save"), exc))
            return
        ui.say(_("Saved %s") % os.path.basename(path))

    def lim_watch():
        """Ask every couple of seconds whether it is still running,
        which for a limit test is the whole verdict."""
        if not state.get("lrun") or state.get("cannot") is None:
            return
        if not state["busy"]:
            w.submit("lim_state", lambda k: k.limit_state())
        root.after(2000, lim_watch)

    lim_verdict()
    # And the buttons, once, now they all exist. Without this they keep
    # whatever state they were created with until something calls
    # lim_buttons - which left Clear looking live on a window with no
    # instrument connected, because it is deliberately not in the busy
    # set that gets disabled at startup.
    lim_buttons()


    # The pump's handlers for this tab's jobs - see pump_jobs.
    def pump_lim_run(payload):
        ui.busy(False)
        if not payload["on"]:
            messagebox.showerror(
                _("Error"),
                "%s\n\n%s"
                % (_("The instrument would not start the "
                     "limit test."),
                   "\n".join(payload["why"])
                   or _("It reported nothing about why.")))
            ui.say(_("The limit test did not start"))
            lim_buttons()
            return
        state["lrun"] = {"running": True,
                         "dest": payload["dest"],
                         "source": payload["source"]}
        lim_buttons()
        draw_limits()
        ui.say(_("Testing %(source)s against %(dest)s - the "
                 "instrument stops if the signal leaves it")
               % {"source": payload["source"],
                  "dest": payload["dest"]})
        lim_watch()

    def pump_lim_send(payload):
        ui.busy(False)
        # Whether the read-back matched or not, the
        # instrument has been sent an envelope and there is
        # something in the destination to test against.
        state["lhas"] = True
        lim_buttons()
        # And read it back once more, so the list says how
        # many columns it now holds rather than what it held
        # before. The drawing is on the canvas already, so
        # this read cannot overwrite it - see the lim_picture
        # branch.
        root.after(50, lim_look)
        if payload.get("verified"):
            ui.say(_("%(dest)s now holds the envelope drawn "
                     "here, read back and verified")
                   % {"dest": payload["dest"]})
        else:
            # Not called a failure: reading an
            # envelope back off an instrument is the
            # part of this route measured least, so
            # the numbers are shown and the user is
            # asked to look. See TdsWfm.verify_envelope.
            ui.say(_("Sent to %(dest)s, but reading it back "
                     "gave something else - sent %(sent)s, "
                     "read %(got)s. Check the instrument's "
                     "screen.")
                   % {"dest": payload["dest"],
                      "sent": ", ".join(str(v) for v
                                        in payload.get("sent")
                                        or []),
                      "got": ", ".join(str(v) for v
                                       in payload.get("got")
                                       or []) or _("nothing")})
        draw_limits()

    def pump_lim_clear(payload):
        ui.busy(False)
        gone = ", ".join(payload["names"])
        # The template that was on screen came from that
        # reference, so it is put down too - leaving it drawn
        # would show a band the instrument no longer has.
        for one in payload["names"]:
            forget_source(one)
        state["lband"] = []
        state["lwave"] = None
        # Known empty, not merely unread: this is the one
        # moment the program is certain there is nothing in
        # the destination to test against.
        state["lhas"] = False
        for one in payload["names"]:
            lim_refs_note(one, 0)
        lim_buttons()
        draw_limits()
        ui.say(_("%s has been cleared on the instrument") % gone)

    def pump_lim_stop(payload):
        ui.busy(False)
        state.pop("lrun", None)
        lim_buttons()
        draw_limits()
        ui.say(_("The limit test is switched off"))

    def pump_lim_state(payload):
        held = state.get("lrun")
        if held and payload["running"] is not None:
            was, held["running"] = held["running"], \
                payload["running"]
            if was and not payload["running"]:
                # It stopped, and nothing here stopped it.
                lim_verdict()
                ui.say(_("FAIL - the instrument stopped, so "
                         "%(source)s left the template")
                       % {"source": held["source"]})

    def pump_lim_survey(payload):
        ui.busy(False)
        # Merged, not replaced: this usually answers about
        # one reference, and what is already known about
        # the other three is still true.
        known = {one["name"]: one
                 for one in (state.get("lrefs") or [])}
        known.update({one["name"]: one
                      for one in payload["refs"]})
        state["lrefs"] = [known[n] for n in tds_wfm.REFS
                          if n in known]
        lim_refs_show(state["lrefs"])
        # What the survey says about the selected reference
        # is the same knowledge lim_look would have gone and
        # fetched. Taking it here saves reading it twice.
        here = state["ldest"].get()
        for one in payload["refs"]:
            if one["name"] == here:
                state["lhas"] = bool(one["columns"])
        lim_buttons()
        held = [one["name"] for one in payload["refs"]
                if one["columns"]]
        if len(payload["refs"]) == 1:
            # One reference, read because it was
            # double-clicked. Empty is an answer, and it
            # belongs on the status line rather than in a
            # box somebody has to dismiss.
            one = payload["refs"][0]
            ui.say((_("%(ref)s holds a template of %(n)d "
                      "column(s)")
                    % {"ref": one["name"], "n": one["columns"]})
                   if one["columns"]
                   else _("%s is empty") % one["name"])
        else:
            ui.say((_("%s holds a template") % ", ".join(held))
                   if held
                   else _("No reference holds a limit template"))
        # Draw the one that is selected, now it is known to
        # be worth drawing.
        if state.get("lhas"):
            lim_look()

    def pump_lim_picture(payload):
        ui.busy(False)
        state["lband"] = payload["band"]
        # An envelope is two values a column, so anything
        # shorter than a pair is not one. Read from the
        # instrument, so this is knowledge either way.
        state["lhas"] = len(payload["band"] or []) >= 2
        lim_refs_note(payload["dest"],
                      len(payload["band"] or []))
        if payload["wave"] is not None:
            state["lwave"] = payload["wave"]
            state["lwavefrom"] = "instrument"
        # Before anything is thinned from it: how far the
        # slider can usefully go is this band's business.
        lim_range()
        # Only what Learn read goes onto the canvas.
        # Refresh reads the same two things and must
        # leave the drawing alone, or an adjustment
        # would be lost every time the trace was
        # looked at again.
        loading = state.pop("lload", None)
        if state.pop("llearn", None) and payload["band"]:
            lim_take_band()
        elif payload["band"] and lim_may_draw(loading):
            # A template already on the instrument, put on
            # the canvas so it can be seen and adjusted
            # rather than made again. Whose idea the read
            # was decides whether it may replace a drawing;
            # see lim_may_draw.
            lim_take_band()
        lim_buttons()
        draw_limits()
        if payload["band"]:
            ui.say(_("%(dest)s holds a template %(columns)d "
                     "column(s) wide")
                   % {"dest": payload["dest"],
                      "columns": len(payload["band"])})
        else:
            ui.say(_("%s holds no limit template to draw")
                   % payload["dest"])

    def pump_lim_build(payload):
        ui.busy(False)
        if not payload["made"]:
            # The instrument's own refusal, word for word.
            messagebox.showerror(
                _("Error"),
                "%s\n\n%s"
                % (_("The instrument did not write a "
                     "template."),
                   "\n".join(payload["why"])
                   or _("It reported nothing about why.")))
            ui.say(_("No template was written"))
            return
        ui.say(_("%(dest)s now holds a template made from "
                 "%(source)s, allowing %(up)g division(s) up "
                 "and down and %(across)g across")
               % {"dest": payload["dest"],
                  "source": payload["source"],
                  "up": payload["vertical"],
                  "across": payload["horizontal"]})
        do_lim_refresh()

    vars(ui).update((k, v) for k, v in locals().items() if k != "ui")
