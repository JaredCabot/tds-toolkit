"""The Masks tab, and the polygon editor it shares with the Limits tab.

Built by run_gui through build(ui). `ui` carries the window's shared
names - state, the worker, busy, say and the rest - and is handed back
everything built here, so run_gui and the tests can reach it.
"""
import base64
import os
import re
import time

import tds_decode
import tds_msk
import tds_set
import tds_wfm
from i18n import gettext as _
from tdstoolkit import (APPDIR, LEFT_PANE, Worker, localise, log_context,
                        log_note, resource, stamped, typed)


def build(ui):
    EDGE = ui.EDGE
    filedialog = ui.filedialog
    flow_buttons = ui.flow_buttons
    flowing = ui.flowing
    hints = ui.hints
    messagebox = ui.messagebox
    named = ui.named
    plot_colours = ui.plot_colours
    popup = ui.popup
    root = ui.root
    says = ui.says
    shown_waves = ui.shown_waves
    simpledialog = ui.simpledialog
    state = ui.state
    tabs = ui.tabs
    tk = ui.tk
    tkfont = ui.tkfont
    trace_colour = ui.trace_colour
    ttk = ui.ttk
    w = ui.w
    wave_name = ui.wave_name
    wave_scales = ui.wave_scales

    # ------------------------------------------------------------ masks
    # A mask on these instruments is eight closed polygons in percent of
    # the graticule, and nothing else: no mask file, no SAVE:MASK, and
    # nothing about masks in a saved setup. Measured on all three - see
    # INSTRUMENT-NOTES - and it is why this tab keeps the library on the
    # PC and on the instrument's own disk rather than pretending the
    # instrument has one.
    masktab = ttk.Frame(tabs)
    tabs.add(masktab, text=_("Masks"))
    named(masktab, "Masks")

    # One row that becomes two when it has to, the way the waveform
    # tab's does. A split chosen by hand was wrong as soon as a button
    # was added or the language changed - "Enregistrer la vue..." was
    # clipped by 23px in French - so it is measured instead.
    mtop = ttk.Frame(masktab)
    mtop.pack(fill="x", padx=2, pady=(6, 2))
    mrow1 = ttk.Frame(mtop)
    mrow1.pack(fill="x")
    mtop2 = ttk.Frame(mtop)
    btn_mnew = ttk.Button(mtop, text=_("New mask"), padding=(10, 2),
                          command=lambda: do_msk_new())
    btn_msave = ttk.Button(mtop, text=_("Save mask as..."), padding=(10, 2),
                           command=lambda: do_msk_save())
    btn_mdel = ttk.Button(mtop, text=_("Delete mask"), padding=(10, 2),
                          command=lambda: do_msk_delete())
    btn_mtrace = ttk.Button(mtop, text=_("Load trace..."), padding=(10, 2),
                            command=lambda: do_msk_trace())
    btn_mgrab = ttk.Button(mtop, text=_("Capture screen"), padding=(10, 2),
                           command=lambda: do_msk_behind())
    btn_mdrop = ttk.Button(mtop, text=_("Clear screenshot"), padding=(10, 2),
                           command=lambda: do_msk_drop_shot())
    # A mask is percent of the graticule and says nothing about the
    # signal it was drawn for; the setup beside it is the other half of
    # the test, which is why Tektronix shipped one with every mask.
    btn_msetup = ttk.Button(mtop, text=_("Save setup..."), padding=(10, 2),
                            command=lambda: do_msk_setup())
    btn_mrefresh = ttk.Button(mtop, text=_("Refresh"), padding=(10, 2),
                              command=lambda: do_msk_scan())
    btn_mview = ttk.Button(mtop, text=_("Save image..."), padding=(10, 2),
                           command=lambda: do_msk_view_save())
    # The library and the picture, in that order: what a mask is, then
    # what is behind it, then what comes out. Refresh sits at the right,
    # where the waveform tab keeps it, so the two tabs are not laid out
    # differently for no reason.
    MASK_LEFT = (btn_mnew, btn_msave, btn_msetup, btn_mdel, btn_mtrace,
                 btn_mgrab, btn_mdrop, btn_mview)
    MASK_RIGHT = (btn_mrefresh,)
    mask_flow = flowing(mtop, mrow1, mtop2, MASK_LEFT, MASK_RIGHT, "mflow")
    mtop.bind("<Configure>", mask_flow)

    def reflow():
        """Lay both toolbars out again, from scratch.

        Changing the language changes how wide every label is without
        resizing anything, so no <Configure> arrives and the rows keep
        the widths the old words needed - which is how a French label
        came to be twelve pixels wider than the button holding it. The
        remembered split is dropped first, or an unchanged split makes
        the layout return without packing anything.
        """
        state.pop("wflow", None)
        state.pop("mflow", None)
        state.pop("lflow", None)
        flow_buttons()
        mask_flow()
        ui.lim_flow()
        root.update_idletasks()

    # A plain frame, not a paned window, so the left column is a fixed width
    # (LEFT_PANE) that does not resize - the same as the Limits and Waveforms
    # tabs, for one look across all three.
    mpanes = ttk.Frame(masktab)
    mpanes.pack(fill="both", expand=True, padx=2, pady=4)

    # Two lists down the left, because there are two places a mask can
    # be. On the PC it is a file you own. In the instrument it is live
    # setup state - eight segments, one mask at a time - which is the
    # only thing the hardware actually has. There used to be a third,
    # for masks kept on the instrument's own disk, and it went: this
    # generation has no mask file format, so a mask left there was a
    # file the instrument itself could not read, and the library on
    # this computer had already done the job.
    mleftf = ttk.Frame(mpanes, width=LEFT_PANE)
    mleftf.pack_propagate(False)          # the one width - see LEFT_PANE
    msplit = ttk.PanedWindow(mleftf, orient="vertical")
    msplit.pack(fill="both", expand=True)

    mpcf = ttk.Frame(msplit)
    lbl_mpc = ttk.Label(mpcf, text=_("Masks on this computer"))
    says(lbl_mpc, "Masks on this computer")
    lbl_mpc.pack(anchor="w")
    lbl_mpcnote = ttk.Label(mpcf, foreground="#555", wraplength=190,
                            justify="left")
    says(lbl_mpcnote, "Double click to edit.")
    lbl_mpcnote.pack(anchor="w", pady=(0, 2))
    mpc = ttk.Treeview(mpcf, columns=("name", "signal", "what"),
                       show="headings", selectmode="extended", height=6)
    mpc.heading("name", text=_("Name"),
                command=lambda: do_msk_sort(mpc, "name"))
    mpc.heading("signal", text=_("Signal"),
                command=lambda: do_msk_sort(mpc, "signal"))
    mpc.heading("what", text=_("Shape"),
                command=lambda: do_msk_sort(mpc, "what"))
    mpc.column("name", width=110, anchor="w")
    mpc.column("signal", width=85, anchor="w")
    mpc.column("what", width=90, anchor="w")
    mpc_bar = ttk.Scrollbar(mpcf, orient="vertical",
                           command=mpc.yview)
    mpc.configure(yscrollcommand=mpc_bar.set)
    mpc_bar.pack(side="right", fill="y")
    mpc.pack(side="left", fill="both", expand=True)
    msplit.add(mpcf, weight=2)

    mlivef = ttk.Frame(msplit)
    # The two directions, between the two lists they move a mask
    # between: down to the instrument, up from it. Beside them, the one
    # thing that is not a transfer - emptying the instrument's segments.
    marrows = ttk.Frame(mlivef)
    marrows.pack(fill="x", pady=(6, 2))
    # The arrows on the middle of the line between the two lists, since
    # that is the line they move a mask across. The other two are not
    # directions, so they sit under them rather than beside them.
    mcentre = ttk.Frame(marrows)
    mcentre.pack(side="top")
    # The glyphs are drawn larger and darker than the label font would
    # give them, because these two are the tab's main verbs and at the
    # default weight they read as decoration.
    arrowface = tkfont.nametofont("TkDefaultFont").copy()
    arrowface.configure(size=max(11, abs(arrowface.cget("size")) + 3),
                        weight="bold")
    ttk.Style().configure("Arrow.TButton", font=arrowface,
                          foreground="#101010")
    # Greyed out still has to look greyed out: a style's foreground is
    # a flat colour and would override the theme's disabled one, so the
    # disabled state is given back explicitly.
    ttk.Style().map("Arrow.TButton",
                    foreground=[("disabled", "#a3a3a3")])
    # Each button is the size its own glyph needs. They used to be
    # pinned inside a frame measured off a *plain* button, so that a
    # bigger font would not make a bigger button - but a ttk label that
    # will not fit is not shrunk, it is cut, and it is cut from the
    # bottom. Measured: the styled button asks for 37 by 31 and was
    # being given 28 by 25, so six pixels of the arrow's descent went
    # over the edge and the glyph sat high in the button.
    btn_msend = ttk.Button(mcentre, text="↓", width=3,
                           style="Arrow.TButton",
                           command=lambda: do_msk_send())
    btn_mload = ttk.Button(mcentre, text="↑", width=3,
                           style="Arrow.TButton",
                           command=lambda: do_msk_load())
    # Sending is the eight segments and nothing else. This is the rest
    # of the instrument - sweep, trigger, display, counter - and it is
    # a separate press because it is a separate decision.
    btn_mmeasure = ttk.Button(marrows, text=_("Start measurement"),
                              padding=(8, 0),
                              command=lambda: do_msk_measure())
    btn_mclear = ttk.Button(marrows, text=_("Clear"), padding=(6, 0),
                            command=lambda: do_msk_clear())
    btn_msend.pack(side="left", padx=(0, 3))
    btn_mload.pack(side="left", padx=(3, 0))
    # One under the other with the same gap between each. Clear used to
    # be place()d at the top right, which takes a widget out of the
    # layout altogether - so it sat on top of Start measurement as soon
    # as that button was wide enough to reach it. This pane is about
    # two hundred pixels wide and none of these three share a line.
    btn_mmeasure.pack(side="top", pady=(6, 0))
    btn_mclear.pack(side="top", pady=(6, 0))
    lbl_mlive = ttk.Label(mlivef, text=_("Loaded in the instrument"))
    says(lbl_mlive, "Loaded in the instrument")
    lbl_mlive.pack(anchor="w")
    # Which mask is in there. The instrument keeps no name - it holds
    # eight lists of points and nothing else - so this can only say what
    # this program put there, and says so plainly when it did not.
    lbl_mlivename = ttk.Label(mlivef, foreground="#555", wraplength=190,
                              justify="left")
    lbl_mlivename.pack(anchor="w", pady=(0, 2))
    mlive = ttk.Treeview(mlivef, columns=("seg", "what"), show="headings",
                         selectmode="none", height=8)
    mlive.heading("seg", text=_("Segment"))
    mlive.heading("what", text=_("Shape"))
    mlive.column("seg", width=70, anchor="w")
    mlive.column("what", width=150, anchor="w")
    # No scrollbar: there are eight segments and the list is eight rows
    # tall, so there is never anything to scroll to.
    mlive.pack(side="left", fill="both", expand=True)
    # weight=0, because this pane is eight rows and always eight rows.
    # Given a share of the spare height it drew those eight and left the
    # rest of its allocation blank. The library above scrolls and has as
    # many rows as there are masks, so the spare height belongs to it.
    # The sash still moves if somebody wants it elsewhere.
    msplit.add(mlivef, weight=0)
    mleftf.pack(side="left", fill="y", padx=(0, 8))

    mrightf = ttk.Frame(mpanes)
    # What can be done to a mask, above the drawing: undoing comes
    # first because it is what somebody reaches for when a drag went
    # wrong, and it should not be hunting for it at the far end of a row.
    mtoolbar = ttk.Frame(mrightf)
    mtoolbar.pack(fill="x", pady=(0, 3))
    btn_mundo = ttk.Button(mtoolbar, style="Toolbutton", padding=3,
                           command=lambda: do_msk_undo())
    btn_mredo = ttk.Button(mtoolbar, style="Toolbutton", padding=3,
                           command=lambda: do_msk_redo())
    btn_mundo.pack(side="left")
    btn_mredo.pack(side="left", padx=(2, 0))
    state.setdefault("undobtn", {}).update({"mundo": btn_mundo,
                                            "mredo": btn_mredo})
    hints(btn_mundo, "Undo")
    hints(btn_mredo, "Redo")

    mcanvasrow = ttk.Frame(mrightf)
    mcanvasrow.pack(fill="both", expand=True)
    state["mtool"] = tk.StringVar(value="pen")

    def edit_tools(parent, prefix):
        """The editor's tool column, beside the drawing it acts on.

        One editor, two drawings (see edit_here), so the Masks and
        Limits tabs each get this column and share the tool in hand.
        Radiobuttons for the tools, because exactly one is in your hand
        at a time and the sunken one says which. Below the line: not
        tools but things done to two shapes at once, which is why they
        are ordinary buttons rather than another mode to be in. The
        instrument has no idea any of this happened - it is all
        arithmetic on this side, and what it gets is the pieces.
        """
        tools = ttk.Frame(parent)
        tools.pack(side="left", fill="y", padx=(0, 3))
        for key, english in (
                ("move", "Select and move points and shapes"),
                ("pen", "Draw points"),
                ("eraser", "Delete points and shapes"),
                ("cut", "Split a shape between two of its points")):
            rb = ttk.Radiobutton(tools, value=key, variable=state["mtool"],
                                 style="Toolbutton", padding=3,
                                 command=lambda: msk_tool_changed())
            rb.pack(pady=(0, 2))
            hints(rb, english)
            state.setdefault(prefix + "toolbtn", {})[key] = rb
        ttk.Separator(tools, orient="horizontal").pack(fill="x", pady=5)
        for key, english in (
                ("fliph", "Mirror the selection horizontally"),
                ("flipv", "Mirror the selection vertically")):
            fb = ttk.Button(tools, style="Toolbutton", padding=3,
                            command=lambda k=key: do_msk_flip(k == "fliph"))
            fb.pack(pady=(0, 2))
            hints(fb, english)
            state.setdefault(prefix + "boolbtn", {})[key] = fb
        ttk.Separator(tools, orient="horizontal").pack(fill="x", pady=5)
        for key, english in (
                ("union", "Union: join the two selected shapes into one"),
                ("intersect", "Intersect: keep only where the two selected "
                              "shapes overlap"),
                ("subtract", "Subtract: take the second selected shape "
                             "out of the first")):
            bb = ttk.Button(tools, style="Toolbutton", padding=3,
                            command=lambda k=key: do_msk_boolean(k))
            bb.pack(pady=(0, 2))
            hints(bb, english)
            state.setdefault(prefix + "boolbtn", {})[key] = bb
        return tools

    mtools = edit_tools(mcanvasrow, "m")

    mplot = tk.Canvas(mcanvasrow,
                      background=tds_wfm.DEFAULT_COLOURS["background"],
                      highlightthickness=1, highlightbackground=EDGE)
    mplot.pack(side="left", fill="both", expand=True)
    # The row under the graticule wraps when it runs out of room, the way
    # the Waveforms toolbar does: in German and French the last box ran
    # off the right edge of a 1280-pixel window. The rows are made before
    # anything is put in them, or they would be drawn over it.
    mbar = ttk.Frame(mrightf)
    mbar.pack(fill="x", pady=(4, 0))
    mrow1, mrow2 = ttk.Frame(mbar), ttk.Frame(mbar)
    mrow1.pack(fill="x")
    mgridpair = ttk.Frame(mbar)
    state["mgrid"] = tk.StringVar(value="0.5")
    state["msnap"] = tk.BooleanVar(value=True)
    state["mshowgrid"] = tk.BooleanVar(value=True)
    state["mgratic"] = tk.BooleanVar(value=True)
    state["mcross"] = tk.BooleanVar(value=False)
    lbl_mgrid = ttk.Label(mgridpair, text=_("Grid spacing, divisions"))
    says(lbl_mgrid, "Grid spacing, divisions")
    lbl_mgrid.pack(side="left")
    ent_mgrid = ttk.Combobox(mgridpair, textvariable=state["mgrid"], width=5,
                             values=("0.1", "0.2", "0.25", "0.5", "1"))
    ent_mgrid.pack(side="left", padx=(4, 0))
    ent_mgrid.bind("<<ComboboxSelected>>", lambda e: edit_redraw())
    ent_mgrid.bind("<Return>", lambda e: edit_redraw())
    chk_mshowgrid = ttk.Checkbutton(mbar, text=_("Show grid"),
                                    variable=state["mshowgrid"],
                                    command=lambda: edit_redraw())
    says(chk_mshowgrid, "Show grid")
    chk_msnap = ttk.Checkbutton(mbar, text=_("Snap to grid"),
                                variable=state["msnap"],
                                command=lambda: edit_redraw())
    says(chk_msnap, "Snap to grid")
    chk_mgrat = ttk.Checkbutton(mbar, text=_("Graticule"),
                                variable=state["mgratic"],
                                command=lambda: edit_redraw())
    says(chk_mgrat, "Graticule")
    chk_mcross = ttk.Checkbutton(mbar, text=_("Crosshairs"),
                                 variable=state["mcross"],
                                 command=lambda: edit_redraw())
    says(chk_mcross, "Crosshairs")
    # Filled is how the instrument draws a mask, and outlines are how
    # you edit one - so both, rather than a choice made for the user.
    state["mfill"] = tk.BooleanVar(value=False)
    chk_mfill = ttk.Checkbutton(mbar, text=_("Filled"),
                                variable=state["mfill"],
                                command=lambda: edit_redraw())
    says(chk_mfill, "Filled")
    # And out of the way altogether, for looking at the trace behind
    # it. It comes back the moment there is any reason to see it - a
    # tool picked up, or another mask opened - because a mask editor
    # that has quietly stopped showing the mask is a puzzle.
    state["mhide"] = tk.BooleanVar(value=False)
    chk_mhide = ttk.Checkbutton(mbar, text=_("Hide mask"),
                                variable=state["mhide"],
                                command=lambda: edit_redraw())
    says(chk_mhide, "Hide mask")
    # The handles on their own, for looking at the shape rather than at
    # what it is made of. Back the moment the mask is - the same rule,
    # for the same reason: an editor that has quietly stopped showing
    # what can be dragged is the same puzzle.
    state["mnodots"] = tk.BooleanVar(value=False)
    chk_mnodots = ttk.Checkbutton(mbar, text=_("Hide points"),
                                  variable=state["mnodots"],
                                  command=lambda: edit_redraw())
    says(chk_mnodots, "Hide points")
    mbar.bind("<Configure>", flowing(
        mbar, mrow1, mrow2,
        (mgridpair, chk_mshowgrid, chk_msnap, chk_mgrat, chk_mcross,
         chk_mfill, chk_mhide, chk_mnodots), (), "mflow", gap=10))
    # A mask goes to the instrument as a mask, and nothing else. It
    # used to be able to go as a limit template instead, for an
    # instrument with no Option 2C - but a limit template is what the
    # Limits tab is, and having two tabs that both build one meant two
    # places to look for the same answer. Without 2C this tab still
    # draws, edits, saves and loads; what it cannot do is reach the
    # instrument's mask subsystem, because there is not one.
    minfo = ttk.Label(mrightf, anchor="w", foreground="#555")
    minfo.pack(fill="x", pady=(2, 0))
    mrightf.pack(side="left", fill="both", expand=True)

    # ------------------------------------------- one editor, two drawings
    # The masks tab and the limits tab draw with the same tools, and the
    # tools are written once. What differs between them is only which
    # canvas the gesture landed on and which drawing it acts on, so that
    # is what these four answer and nothing else has to know there are
    # two. A second copy of the editor would be a second set of bugs to
    # find twice.
    #
    # The tools, the selection and the half-drawn shape are shared on
    # purpose - only one of the two tabs can be in front, so there is
    # only ever one of each in play. History and the unsaved mark are
    # not shared: those belong to the drawing. What is in hand is put
    # down when the other tab comes to the front - see edit_switched.
    def edit_here():
        """Which drawing the tools are acting on: 'lim' or 'msk'."""
        try:
            return "lim" if tabs.select() == str(ui.limtab) else "msk"
        except Exception:               # before the notebook exists
            return "msk"

    def edit_canvas():
        return ui.lplot if edit_here() == "lim" else mplot

    def edit_switched(_evt=None):
        """Put down whatever is in hand when the other drawing comes up.

        The selection, a shape half drawn, a split begun and a run of
        nudges are kept once for both drawings, and they are indexes
        into whichever one they were made on. Carried across to the
        other tab they pointed at its shapes instead: Delete took out
        points nobody had chosen there, and the next pen click went on
        drawing a shape that belonged to the mask. So they go, the
        drawing they came from is tidied as it would have been had the
        shape been finished there, and a Coordinates box open on it
        closes.
        """
        now = edit_here()
        was = state.get("medit_on", now)
        state["medit_on"] = now
        if now == was:
            return
        for key in ("mdrawing", "mcutfrom", "mhover", "mband", "mdrag",
                    "mcrossat", "mnarrow"):
            state.pop(key, None)
        state["mpicks"] = []
        state["morder"] = []
        state["mrun"] = None
        box = state.pop("mcoords", None)
        if box is not None:
            try:
                box.destroy()
            except Exception:
                pass
        left = state.get("lmask" if was == "lim" else "mask")
        if left is not None:
            for seg in left.segments:
                if 0 < len(seg) < tds_msk.MIN_POINTS:
                    del seg[:]
        edit_draw("l" if was == "lim" else "m")

    tabs.bind("<<NotebookTabChanged>>", edit_switched, add="+")

    def edit_mask():
        """The drawing being edited: a mask, or the limits tab's band."""
        return state.get("lmask" if edit_here() == "lim" else "mask")

    def edit_segments():
        """How many shapes this drawing may hold.

        Eight for a mask, because the instrument has eight
        segments and that is where a mask goes. A limits drawing
        never goes there - it becomes one envelope, a min and a
        max per column - so the only reason for a ceiling is to
        stop a runaway, and it can be a generous one.
        """
        return 64 if edit_here() == "lim" else tds_msk.SEGMENTS

    def edit_points():
        """How many points one shape may hold.

        Fifty is what a mask segment holds on the instrument -
        asking for 64 stored 50 and left an event in the queue. An
        envelope has no such limit; the only ceiling on a limits
        drawing is how many handles a person can work with.
        """
        return (200 if edit_here() == "lim"
                else tds_msk.POINTS_PER_SEGMENT)

    def edit_tidy():
        """A single point is not a shape, and no action may leave
        one behind.

        Done in one place rather than guarded at every gesture,
        because the rule is about the drawing rather than about
        any one way of changing it - a pen abandoned after one
        click, a delete that takes all but one point, a cut, a
        boolean. Any of them can strand a point and none of them
        should.

        The shape being drawn is exempt while it is being drawn,
        since one point is how every shape starts. Whatever
        finishes it drops that exemption, so abandoning the pen
        after a single click clears it.
        """
        mask = edit_mask()
        if mask is None:
            return
        drawing = state.get("mdrawing")
        for i, seg in enumerate(mask.segments):
            if i != drawing and 0 < len(seg) < tds_msk.MIN_POINTS:
                del seg[:]

    def edit_redraw():
        edit_tidy()
        # Buttons follow the drawing, not just the file library: drawing
        # the first shape on a fresh pane has to enable Save template
        # as... there and then, and every draw action passes through
        # here. The button refreshers only set widget states - neither
        # redraws - so this does not loop.
        if edit_here() == "lim":
            ui.draw_limits()
            ui.lim_buttons()
        else:
            draw_mask()
            msk_buttons()

    def edit_draw(which):
        """Redraw one named canvas, whichever tab is in front.

        `which` is the "m" or "l" that prefixes that drawing's state
        keys. Named rather than inferred, because what puts a trace or
        a screen behind a drawing can finish while the other tab is
        being looked at.
        """
        (ui.draw_limits if which == "l" else draw_mask)()

    def edit_soon():
        """Redraw once the pointer stops arriving, not once per event.

        The pointer delivers motion faster than the canvas can be
        rebuilt - five hundred items with the grid on, and eight
        thousand at a tenth-division grid - and every one of them that
        changes what is under the pointer asks for a rebuild. Drawn one
        for one, the queue grows and the crosshair trails the mouse by a
        visible distance.

        Collapsed onto one idle callback, a burst of motion costs one
        redraw and the crosshair keeps up. The last event in the burst
        is the one that matters anyway: the earlier ones describe
        positions the pointer has already left.
        """
        if state.get("mredraw"):
            return
        state["mredraw"] = edit_canvas().after_idle(edit_now)

    def edit_now():
        state.pop("mredraw", None)
        edit_redraw()

    def edit_key(name):
        """A state key belonging to the drawing rather than the editor."""
        return ("l" if edit_here() == "lim" else "m") + name

    def edit_new():
        """Start a drawing of whichever kind is in front.

        What the pen does on an empty canvas, which is not what the
        New button does: nothing is offered to be saved and nothing
        already on the graticule is taken off. Somebody who has put the
        pen down on the glass has said what they want to happen.
        """
        if edit_here() == "lim":
            state["lmask"] = tds_msk.Mask(
                source=state["lsource"].get())
        else:
            do_msk_new()

    def mask_frame():
        """Where the graticule sits on the drawing in front."""
        canvas = edit_canvas()
        w = max(canvas.winfo_width(), 80)
        h = max(canvas.winfo_height(), 60)
        left, top, right, bottom = tds_wfm.plot_frame(w, h, 20, room=8)
        return (left, top, right, bottom)

    def mask_step():
        """The grid step, as (across, up) in percent of the graticule.

        The box is in *divisions*, not in percent of the whole
        graticule, because a division is what the instrument draws and
        what anybody reading a mask thinks in. A division is 10% of the
        width and 12.5% of the height, so the two steps differ - set as
        one number of percent, the horizontal lines landed between the
        instrument's own, which is a grid that helps with nothing.
        """
        try:
            divs = typed(state["mgrid"].get())
        except (TypeError, ValueError):
            return (0.0, 0.0)
        if divs <= 0:
            return (0.0, 0.0)
        return (divs * 100.0 / tds_wfm.DIVS_X,
                divs * 100.0 / tds_wfm.DIVS_Y)

    def mask_grid():
        """The steps points snap to, or zeroes when they are not to snap.

        Showing the grid and snapping to it are two different questions
        and were one setting: somebody who wanted to see where the
        divisions were had to accept being pulled onto them.
        """
        return mask_step() if state["msnap"].get() else (0.0, 0.0)

    def edit_grid(canvas, pick, frame):
        """A dot at every place a click can put a point.

        Drawn the same way on both editors, because it says the
        same thing on both: where a point will land.
        """
        left, top, right, bottom = frame
        across, up = mask_step()
        if across and up and state["mshowgrid"].get():
            # A dot at every place a point can land, rather than lines
            # through them: what the grid is for is saying where a click
            # will go, and a dot says that exactly where a crossing of
            # two lines says it twice over.
            #
            # Every crossing, however fine. A tenth-division grid is
            # eight thousand of them, which is only affordable because a
            # pointer moving across the canvas no longer rebuilds it -
            # see msk_crosshair.
            xs = [across * i for i in range(1, int(round(100.0 / across)))]
            ys = [up * i for i in range(1, int(round(100.0 / up)))]
            for gx in xs:
                for gy in ys:
                    px, py = tds_msk.to_canvas(gx, gy, (left, top,
                                                        right, bottom))
                    canvas.create_line(px, py, px + 1, py,
                                      fill=pick["grid"], tags="grid")

    def edit_shapes(canvas, mask, pick, frame, joins=True, stipple=""):
        """The drawing, its handles and everything being aimed.

        Shared by the masks tab and the limits tab: a shape, a
        selected point, a shape half drawn, a cut being lined up
        and whatever the pointer is over all look the same and mean
        the same on both. Which drawing it is comes in as an
        argument, so neither tab has to guess.
        """
        left, top, right, bottom = frame
        if mask is not None and not state["mhide"].get():
            # Shapes the instrument would join up its own way are drawn
            # dashed, so the one thing the editor cannot show - what the
            # graticule will actually look like - is at least flagged
            # where it is being drawn. See tds_msk.redraw_reason.
            # `joins` is False for a drawing that is not going
            # into the instrument's mask segments, and so cannot
            # be redrawn by them - see edit_shapes.
            redrawn = dict(mask.redrawn()) if joins else {}
            for number, seg in mask.filled():
                # filled() counts segments from 1, the way the
                # instrument names them; a selection is kept as an index
                # into mask.segments, which counts from 0. Mixing the
                # two drew the hollow "this one is selected" handle on
                # the wrong shape.
                n = number - 1
                flat = []
                for x, y in seg:
                    px, py = tds_msk.to_canvas(x, y, (left, top, right,
                                                      bottom))
                    flat += [px, py]
                chosen = set(state.get("mpicks") or [])
                # A shape every point of which is selected is drawn in
                # the selection colour, so what a flip or a boolean is
                # about to act on is visible without counting handles.
                whole = all((n, i) in chosen for i in range(len(seg)))
                edge = pick["select"] if whole else pick["mask"]
                # `stipple` only touches the fill; the outline stays
                # solid. The limits tab asks for one so the trace it is
                # drawn around still reads through it - see draw_limits.
                canvas.create_polygon(
                    *flat, fill=(edge if state["mfill"].get() else ""),
                    stipple=(stipple if state["mfill"].get() else ""),
                    outline=edge, width=2,
                    dash=(6, 4) if number in redrawn else (),
                    tags=("mask", "seg%d" % number))
                for i, (x, y) in enumerate(seg):
                    if state["mnodots"].get():
                        continue
                    px, py = tds_msk.to_canvas(x, y, (left, top, right,
                                                      bottom))
                    # A selected point is hollow and a little larger, so
                    # which points a drag or a Delete will act on can be
                    # seen at a glance rather than remembered.
                    if (n, i) in chosen:
                        canvas.create_rectangle(px - 4, py - 4,
                                               px + 4, py + 4,
                                               fill=pick["background"],
                                               outline=pick["select"],
                                               width=2,
                                               tags=("handle", "picked",
                                                     "seg%d" % number))
                    else:
                        canvas.create_rectangle(px - 3, py - 3,
                                               px + 3, py + 3,
                                               fill=pick["mask"],
                                               outline=pick["background"],
                                               tags=("handle",
                                                  "seg%d" % number))
                    # The coordinates box numbers its rows and the
                    # canvas did not, so matching row 7 to a handle
                    # meant counting round the shape. Numbered only
                    # while that box is open on this shape: every
                    # point labelled all the time is a drawing nobody
                    # can see.
                    if state.get("mnumbers") == n:
                        canvas.create_text(px + 8, py - 8, anchor="w",
                                           text=str(i + 1),
                                           fill=pick["label"],
                                           tags=("number",
                                                 "seg%d" % number))
        # While a shape is being drawn, show where the next click would
        # put an edge - and the edge that closes it. The closing line is
        # not a preview of something optional: a TDS mask segment is a
        # closed polygon and there is no other kind, so the shape a
        # click is about to make is the shape with the closing edge in
        # it. Dashed, because none of it is placed yet.
        drawing = state.get("mdrawing")
        pointer = state.get("mcrossat")
        if (mask is not None and drawing is not None and pointer is not None
                and drawing < len(mask.segments) and mask.segments[drawing]):
            open_seg = mask.segments[drawing]
            ends = ([open_seg[-1]] if len(open_seg) < 2
                    else [open_seg[-1], open_seg[0]])
            for ex, ey in ends:
                ax, ay = tds_msk.to_canvas(ex, ey, (left, top, right,
                                                    bottom))
                canvas.create_line(ax, ay, pointer[0], pointer[1],
                                  fill=pick["mask"], dash=(4, 3),
                                  tags="elastic")
        # The cut being aimed. Red rather than a scheme colour on
        # purpose: it is the one line on the canvas that is about to
        # divide something, and it should not look like part of the
        # drawing.
        cutting = state.get("mcutfrom")
        if (mask is not None and cutting is not None and pointer is not None
                and cutting[0] < len(mask.segments)
                and cutting[1] < len(mask.segments[cutting[0]])):
            fx, fy = mask.segments[cutting[0]][cutting[1]]
            cx, cy = tds_msk.to_canvas(fx, fy, frame)
            canvas.create_line(cx, cy, pointer[0], pointer[1],
                              fill=pick["cut"], width=2, tags="cut")
        # What the pointer is over, drawn over what it is over: the
        # eraser highlights the whole shape when it is on an edge,
        # because that is what it is about to take away.
        hover = state.get("mhover")
        if mask is not None and hover is not None:
            kind, which = hover
            hot = pick["hover"]
            if kind == "edge" and msk_tool() == "eraser":
                kind, which = "shape", which[0]
            if kind == "point":
                s, i = which
                if s < len(mask.segments) and i < len(mask.segments[s]):
                    hx, hy = tds_msk.to_canvas(*mask.segments[s][i],
                                               frame=(left, top, right,
                                                      bottom))
                    canvas.create_oval(hx - 6, hy - 6, hx + 6, hy + 6,
                                      outline=hot, width=2, tags="hover")
            elif kind == "edge":
                s, after = which[0], which[1]
                seg = mask.segments[s]
                if seg:
                    a = seg[after % len(seg)]
                    b = seg[(after + 1) % len(seg)]
                    ax, ay = tds_msk.to_canvas(a[0], a[1], frame)
                    bx, by = tds_msk.to_canvas(b[0], b[1], frame)
                    canvas.create_line(ax, ay, bx, by, fill=hot, width=4,
                                      tags="hover")
            elif kind == "shape" and which < len(mask.segments):
                flat = []
                for x, y in mask.segments[which]:
                    hx, hy = tds_msk.to_canvas(x, y, (left, top, right,
                                                      bottom))
                    flat += [hx, hy]
                if len(flat) >= 6:
                    canvas.create_polygon(*flat, fill="", outline=hot,
                                         width=3, tags="hover")
                elif len(flat) == 4:
                    # A shape of two points is a line, and a polygon of
                    # two points draws nothing at all - so the eraser
                    # lit up every shape on the canvas except the one
                    # kind a limits drawing is mostly made of, and a
                    # click deleted something that had never been
                    # highlighted.
                    canvas.create_line(*flat, fill=hot, width=4,
                                      tags="hover")
        msk_crosshair()
        band = state.get("mband")
        if band is not None and band.get("moved") and "to" in band:
            canvas.create_rectangle(band["at"][0], band["at"][1],
                                   band["to"][0], band["to"][1],
                                   outline=pick["label"], dash=(2, 2),
                                   tags="band")

    def verdict_stamp(canvas, right, top, words, colour):
        """The PASS/FAIL stamp in the graticule's top right corner.

        As wide as its word, never narrower than the 70 pixels PASS
        gets: a fixed box cut BESTANDEN and CONFORME off at both ends.
        """
        face = ("TkDefaultFont", 12, "bold")
        wide = max(70, tkfont.Font(font=face).measure(words) + 16)
        canvas.create_rectangle(right - 6 - wide, top + 6, right - 6, top + 32,
                                fill=colour, outline="", tags="verdict")
        canvas.create_text(right - 6 - wide / 2.0, top + 19, text=words,
                           fill="#000000", font=face, tags="verdict")

    def draw_mask(_evt=None):
        """The graticule, the grid, the trace under it and the mask on top.

        Drawn in the order somebody would want to see it: the graticule
        is furthest back, then the grid, then any trace being worked
        against, then the mask, then its handles. A mask drawn under the
        trace it is meant to bound would be the wrong way round.
        """
        mplot.delete("all")
        pick = plot_colours(None)
        mplot.configure(background=pick["background"])
        left, top, right, _bottom = mask_frame()
        # The instrument's own screen, where one was captured because it
        # was in DPO and there was no record to read. Furthest back of
        # all: it is cropped to its graticule and scaled to this one, so
        # the editor's graticule lands on the instrument's own and the
        # two agreeing is the thing to be able to see.
        shot = msk_shot_image("m", mask_frame())
        if shot is not None:
            mplot.create_image(left, top, image=shot, anchor="nw",
                               tags="shot")
        if state["mgratic"].get():
            # The same tds_wfm.graticule() the waveform tab and the PNG
            # export draw from, so a mask is drawn over exactly the
            # graticule a trace is drawn over.
            for element, x0, y0, x1, y1, thick in tds_wfm.graticule(
                    max(mplot.winfo_width(), 80),
                    max(mplot.winfo_height(), 60), 20, room=8):
                mplot.create_line(x0, y0, x1, y1, fill=pick[element],
                                  width=thick)
        edit_grid(mplot, pick, mask_frame())
        wave = state.get("mwave")
        if wave is not None:
            xy, _bounds = tds_wfm.plot_geometry(
                wave, max(mplot.winfo_width(), 80),
                max(mplot.winfo_height(), 60), 20, state.get("mview"),
                room=8)
            if len(xy) > 1:
                flat = []
                for x, y in xy:
                    flat += [x, y]
                mplot.create_line(*flat, fill=trace_colour(wave, pick),
                                  width=1, tags="trace")
        mask = state.get("mask")
        edit_shapes(mplot, mask, pick, mask_frame())
        verdict = msk_verdict()
        if verdict is not None:
            words, colour = verdict
            # Filled, with the word in black on it, the same way the
            # saved picture stamps it. See tds_wfm.plot_png.
            verdict_stamp(mplot, right, top, words, colour)
        say_mask()

    def say_mask():
        """The line under the canvas: what is open and how big it is."""
        mask = state.get("mask")
        if mask is None:
            minfo.config(text=_("No mask open - press New mask, or "
                                "double-click one on the left"))
            return
        segs = mask.filled()
        # Complaints stop a save; the redrawn note does not. It is here
        # because it is the one fault the canvas cannot show honestly -
        # the shape is drawn correctly here and the instrument will join
        # the same points up differently.
        # The name is the only place a shipped mask says what signal it
        # is drawn for, so what the name is worth is spelled out here in
        # full - the list on the left has room for the rate alone.
        body, rate = tds_msk.standard(mask.origin or mask.name)
        about = [x for x in (body, rate) if x]
        setup = msk_setup_path(mask)
        if setup:
            try:
                about.append(tds_set.summary(tds_set.parse(
                    tds_set.contents(setup))) or os.path.basename(setup))
            except OSError:
                pass
        notes = mask.complaints() + ["segment %d: %s" % (n, why)
                                     for n, why in mask.redrawn()]
        minfo.config(text=_("%(name)s%(std)s - %(segs)d segment(s), "
                            "%(pts)d point(s)%(bad)s")
                     % {"name": mask.name or _("untitled"),
                        "std": (" (%s)" % ", ".join(about)) if about else "",
                        "segs": len(segs),
                        "pts": len(mask.points),
                        "bad": ("  -  " + "; ".join(notes)) if notes else ""})

    mplot.bind("<Configure>", draw_mask)

    # ---------------------------------------------- drawing with a mouse
    # The gestures are Illustrator's, because that is what somebody who
    # draws shapes already has in their hands: click on empty space adds
    # a point to the segment being drawn, click a point selects it, drag
    # a point moves it, click an edge inserts a point there, and Delete
    # removes what is selected. Escape finishes the segment being drawn.

    def msk_at(evt):
        """Where this click is, in percent, snapped if snapping is on.

        Kept on the graticule last of all. Snapping can only move a
        point by half a grid step, but a click can land anywhere on the
        canvas - the frame has eight pixels of room round it - and a
        point drawn out there is one the instrument will not show.
        """
        x, y = tds_msk.from_canvas(evt.x, evt.y, mask_frame())
        across, up = mask_grid()
        return tds_msk.held(*tds_msk.snap_point(x, y, across, up))

    def msk_reach():
        """How near counts as on a point, in percent rather than pixels.

        A fixed number of pixels is a different distance in a small
        window than a large one, and the shapes are in percent, so the
        reach has to be as well.
        """
        left, _top, right, _bottom = mask_frame()
        return 6.0 / max(1.0, right - left) * 100.0

    def msk_picked():
        """Which points are selected, as (segment, point) pairs.

        A list rather than a single pair, because a rubber band and a
        whole-shape drag both select more than one and everything that
        acts on a selection should act on all of it.
        """
        return list(state.get("mpicks") or [])

    def msk_what_at(evt):
        """What the pointer is over: a point, an edge, or a whole shape.

        One question asked in one place, because three tools all need
        the same answer and the order matters - a point sits on an edge
        which sits inside a shape, and the smallest thing wins.
        """
        mask = edit_mask()
        if mask is None:
            return None
        x, y = tds_msk.from_canvas(evt.x, evt.y, mask_frame())
        reach = msk_reach()
        hit = tds_msk.near_point(mask, x, y, reach)
        if hit is not None:
            return ("point", hit)
        edge = tds_msk.near_edge(mask, x, y, reach)
        if edge is not None:
            return ("edge", edge)
        seg = tds_msk.segment_at(mask, x, y)
        return ("shape", seg) if seg is not None else None

    def msk_add_point(evt, onto=None):
        """Empty space: extend the shape being drawn, or start one.

        `onto` is an existing point the pointer is over, and the new
        one lands exactly on it rather than on the grid. Shapes that
        have to meet then meet: the grid cannot promise that when the
        point being met is not itself on it.
        """
        mask = edit_mask()
        if mask is None:
            return
        seg = state.get("mdrawing")
        if seg is None:
            seg = msk_new_segment()
            if seg is None:
                return
            state["mdrawing"] = seg
        if onto == (seg, 0) and len(mask.segments[seg]) > 2:
            # Back on this shape's own first point: that is the shape
            # closed, not a point stacked on another point.
            do_msk_finish()
            return
        where = mask.segments[onto[0]][onto[1]] if onto else msk_at(evt)
        if mask.segments[seg] and mask.segments[seg][-1] == where:
            return
        if len(mask.segments[seg]) >= edit_points():
            ui.say(_("Segment %(n)d is full at %(max)d points")
                   % {"n": seg + 1, "max": edit_points()})
            return
        msk_remember()
        mask.segments[seg].append(where)
        state["mpicks"] = [(seg, len(mask.segments[seg]) - 1)]
        edit_redraw()

    def msk_drop_point(seg, point):
        """One point, gone."""
        mask = edit_mask()
        if mask is None or seg >= len(mask.segments):
            return
        if point >= len(mask.segments[seg]):
            return
        msk_remember()
        mask.segments[seg].pop(point)
        state["mpicks"] = []
        state.pop("mhover", None)
        edit_redraw()

    def msk_drop_shape(seg):
        """A whole shape, gone. The segment stays, empty: the instrument
        has eight of them whether they hold anything or not."""
        mask = edit_mask()
        if mask is None or seg >= len(mask.segments):
            return
        msk_remember()
        mask.segments[seg] = []
        state["mpicks"] = []
        state.pop("mhover", None)
        edit_redraw()
        ui.say(_("Segment %d deleted") % (seg + 1))

    def msk_start_drag(evt, picks, anchor):
        """Remember where everything was, so a drag can move it as one.

        Positions are kept from the moment of the press rather than
        updated as the pointer moves. Accumulating deltas drifts: each
        step is snapped, and snapping a snapped number repeatedly walks
        a shape off its own grid.

        The whole geometry is kept as well, for undo - but not put on
        the stack until the release, and then only if the drag actually
        moved something. A press that only selects is not an edit.
        """
        mask = edit_mask()
        state["mdrag"] = {
            "from": tds_msk.from_canvas(evt.x, evt.y, mask_frame()),
            "anchor": anchor,
            "before": msk_shapes(),
            "was": {(s, i): mask.segments[s][i] for s, i in picks
                    if s < len(mask.segments)
                    and i < len(mask.segments[s])}}

    # ------------------------------------------------- the three tools
    # Each is one gesture on a press, and the hover highlight says what
    # that gesture is about to act on before it acts. Which tool is in
    # hand decides everything: the same click draws, moves or deletes.

    def msk_pen_press(evt, what):
        """The pen: a click is a point, on an edge or in fresh air."""
        mask = edit_mask()
        if what is not None and what[0] == "edge" \
                and state.get("mdrawing") is None:
            seg, after, ex, ey = what[1]
            msk_remember()
            # Exactly on the edge, not on the grid. Snapping it here
            # moves it off the edge it was put on, which changes the
            # shape - and on a shape whose points are not themselves on
            # the grid it moves it *inwards*, making a hair-thin concave
            # corner that then refuses to save and is far too small to
            # see. Drag it afterwards and the grid applies as usual.
            mask.segments[seg].insert(after + 1, (round(ex, 2),
                                                  round(ey, 2)))
            state["mpicks"] = [(seg, after + 1)]
            state.pop("mhover", None)
            edit_redraw()
            return
        msk_add_point(evt, what[1] if what and what[0] == "point" else None)

    def msk_grabbed(what):
        """Which points a press on this takes hold of.

        Exactly what the hover highlighted: a point is a point, a side
        is its two ends, and the inside of a shape is all of it. The
        highlight is the promise; this keeps it.
        """
        mask = edit_mask()
        kind, which = what
        if kind == "point":
            return [which]
        if kind == "edge":
            seg, after = which[0], which[1]
            count = len(mask.segments[seg])
            return [(seg, after % count), (seg, (after + 1) % count)]
        return [(which, i) for i in range(len(mask.segments[which]))]

    def msk_move_press(evt, what):
        """The four-way arrow: take hold of whatever is lit up."""
        adding = bool(evt.state & 0x0001)          # shift held
        if what is None:
            state["morder"] = []
            state["mband"] = {"at": (evt.x, evt.y), "moved": False}
            return
        grabbed = msk_grabbed(what)
        if not grabbed:
            return
        seg = grabbed[0][0]
        # Which shapes are chosen, and in what order. Subtract takes the
        # second out of the first, so the order is half of what the
        # button means - and it has to survive a shift-click that lands
        # on a corner rather than on an edge, which is most of them.
        order = list(state.get("morder") or []) if adding else []
        if seg not in order:
            order.append(seg)
        state["morder"] = order
        picks = msk_picked()
        if adding:
            picks = picks + [p for p in grabbed if p not in picks]
        elif not set(grabbed) <= set(picks):
            picks = list(grabbed)
        state["mpicks"] = picks
        # Pressing on something already selected keeps the whole
        # selection, so a group can be dragged by any of its parts. If
        # the press turns out to be a click that moved nothing, the
        # release narrows the selection to what was actually pressed.
        state["mnarrow"] = None if adding else list(grabbed)
        msk_start_drag(evt, picks,
                       grabbed[0] if what[0] in ("point", "edge") else None)
        edit_redraw()

    def msk_erase_press(_evt, what):
        """The eraser: a point if the pointer is on one, otherwise the
        whole shape - which is what an edge under the eraser means."""
        if what is None:
            return
        if what[0] == "point":
            msk_drop_point(*what[1])
            return
        msk_drop_shape(what[1][0] if what[0] == "edge" else what[1])

    def msk_room():
        """Which segments are empty and could hold another shape."""
        mask = edit_mask()
        if mask is None:
            return []
        while len(mask.segments) < edit_segments():
            mask.segments.append([])
        return [i for i, seg in enumerate(
            mask.segments[:edit_segments()]) if not seg]

    def msk_cut_press(_evt, what):
        """The scissors: click one point of a shape, then another.

        Both ends have to be points of the same shape, and the line
        between them has to stay inside it - a cut that leaves the shape
        or crosses an edge makes two shapes that are not the one that
        was there.
        """
        mask = edit_mask()
        if what is None or what[0] != "point":
            if state.pop("mcutfrom", None) is not None:
                ui.say(_("Split abandoned"))
                edit_redraw()
            return
        seg, point = what[1]
        start = state.get("mcutfrom")
        if start is None or start[0] != seg or start[1] == point:
            state["mcutfrom"] = (seg, point)
            ui.say(_("Click the point to split to"))
            edit_redraw()
            return
        if not tds_msk.can_cut(mask.segments[seg], start[1], point):
            ui.say(_("That cut would leave the shape or cross one of its "
                     "own edges"))
            return
        room = msk_room()
        if not room:
            messagebox.showinfo(
                _("Nothing left to split into"),
                (_("This drawing already has %d shapes in it. "
                   "Delete one before cutting another in two.")
                 if edit_here() == "lim" else
                 _("This mask already uses all %d segments the "
                   "instrument has. Delete one before cutting "
                   "another in two.")) % edit_segments())
            return
        msk_remember()
        one, two = tds_msk.cut_at(mask.segments[seg], start[1], point)
        mask.segments[seg] = one
        mask.segments[room[0]] = two
        state.pop("mcutfrom", None)
        state["mpicks"] = []
        state["morder"] = []
        edit_redraw()
        ui.say(_("Split into segments %(a)d and %(b)d")
               % {"a": seg + 1, "b": room[0] + 1})

    def msk_chosen_shapes():
        """Which whole shapes the selection covers, in the order chosen."""
        mask = edit_mask()
        if mask is None:
            return []
        whole = {n for n, _i in msk_picked()
                 if n < len(mask.segments) and mask.segments[n]
                 and all((n, i) in set(msk_picked())
                         for i in range(len(mask.segments[n])))}
        order = [n for n in (state.get("morder") or []) if n in whole]
        return order + sorted(whole - set(order))

    def do_msk_flip(across):
        """Mirror what is selected, about its own middle.

        Its own middle, not the graticule's, so a flip is a flip and not
        also a move. With nothing selected there is nothing to mirror -
        flipping the whole mask by accident is a poor surprise.
        """
        mask = edit_mask()
        picks = msk_picked()
        if mask is None or not picks:
            ui.say(_("Nothing selected"))
            return
        here = [mask.segments[n][i] for n, i in picks
                if n < len(mask.segments) and i < len(mask.segments[n])]
        if not here:
            return
        xs = [x for x, _y in here]
        ys = [y for _x, y in here]
        mid = (min(xs) + max(xs)) / 2.0 if across else (min(ys) +
                                                        max(ys)) / 2.0
        msk_remember()
        for n, i in picks:
            if n < len(mask.segments) and i < len(mask.segments[n]):
                x, y = mask.segments[n][i]
                mask.segments[n][i] = ((round(2 * mid - x, 2), y) if across
                                       else (x, round(2 * mid - y, 2)))
        # Mirroring reverses which way round a shape is drawn, and a
        # shape drawn the other way round is the same shape - but the
        # order decides which side an edge insert lands on, so it is put
        # back the way it was.
        for n in {n for n, _i in picks}:
            if n < len(mask.segments) and len(mask.segments[n]) > 2 \
                    and all((n, i) in set(picks)
                            for i in range(len(mask.segments[n]))):
                mask.segments[n].reverse()
        edit_redraw()
        ui.say(_("Mirrored"))

    def do_msk_copy(_evt=None, cut=False):
        """Keep the chosen shapes, to be pasted later."""
        mask = edit_mask()
        chosen = msk_chosen_shapes()
        if mask is None or not chosen:
            ui.say(_("Select a shape first"))
            return "break"
        state["mclip"] = [list(mask.segments[n]) for n in chosen]
        if cut:
            msk_remember()
            for n in chosen:
                mask.segments[n] = []
            state["mpicks"] = []
            state["morder"] = []
            edit_redraw()
        ui.say(_("%d shape(s) copied") % len(state["mclip"]))
        return "break"

    def do_msk_paste(_evt=None):
        """Put back what was copied, one grid step along so it can be seen."""
        mask = edit_mask()
        held = state.get("mclip") or []
        if mask is None or not held:
            return "break"
        room = msk_room()
        if len(held) > len(room):
            messagebox.showerror(
                _("Error"), "%s\n\n%s"
                % (_("There is not room for the result."),
                   _("It comes to %(pieces)d shape(s) and there "
                     "are %(room)d segment(s) free. Delete a shape and "
                     "try again.")
                   % {"pieces": len(held), "room": len(room)}))
            return "break"
        across, up = mask_step()
        across, up = across or 1.0, up or 1.0
        msk_remember()
        picks = []
        for at, shape in zip(room, held):
            mask.segments[at] = [(round(x + across, 2), round(y - up, 2))
                                 for x, y in shape]
            picks += [(at, i) for i in range(len(shape))]
        state["mpicks"] = picks
        state["morder"] = list(room[:len(held)])
        edit_redraw()
        ui.say(_("%d shape(s) pasted") % len(held))
        return "break"

    def do_msk_behind(which="m"):
        """Put whatever the instrument is showing behind the drawing.

        One button and two answers, because the instrument has two
        states: a readable waveform, or DPO, where there is no record to
        read and the screen itself is the only picture of what it is
        acquiring. Three, with a differential pair differenced in MATH1:
        that is a record, but not one of the channels, and not one the
        instrument's own mask counter can see.
        """
        if state["busy"] or state.get("cannot") is None:
            return
        # Which drawing it goes behind, remembered now rather than
        # worked out when the instrument answers: reading a screen
        # takes a second or two and the other tab may be in front by
        # then.
        state["behindfor"] = which
        ui.busy(True, "wait")
        ui.say(_("Reading what the instrument is showing ..."))
        w.submit("msk_behind",
                 lambda k, m=bool(state.get("mmath")): k.msk_behind(m))

    def do_msk_drop_shot():
        """Take whatever is behind the mask back off again.

        A captured screen stays until it is replaced, which is what
        makes it usable - but it also goes stale, and a DPO screen from
        ten minutes ago under a mask being edited now is a picture of
        nothing in particular. The verdict goes with it: it was that
        picture's verdict.
        """
        if not (state.get("mshot") or state.get("mwave")
                or state.get("mhits")):
            return
        state.pop("mshot", None)
        state.pop("mshotimage", None)
        state.pop("mhits", None)
        state["mwave"] = None
        state["mview"] = None
        draw_mask()
        msk_buttons()
        ui.say(_("Cleared what was behind the mask"))

    def msk_view_for(wave):
        """How to draw that record against this mask, or None for as-is.

        None everywhere except MATH1, which the instrument scales
        itself: a 784D shows CH1-CH2 at five times the channel's own
        volts a division, and there is no command to tell it otherwise.
        Drawn as the instrument has it, the difference would sit five
        times too small inside a mask drawn for the volts a division
        the setup names - and pass whatever it did. The zoom is that
        ratio, so the trace is judged and drawn at the scale the mask
        means.
        """
        want = msk_volts_per_div()
        if wave is None or not want or not str(
                wave.source or "").upper().startswith("MATH"):
            return None
        view = tds_wfm.PlotView([wave])
        view.zoom = wave.volts_per_div / want
        return view

    def msk_verdict():
        """(words, colour) for the trace behind the mask, or None.

        A mask says where the signal may not go, so the verdict is
        whether any of it went there. Judged on the samples as drawn -
        which is what the eye is judging too - rather than on the whole
        record: a point between two drawn samples is a point neither the
        mask nor anybody looking at it can see.

        Only offered where there is something to judge. An empty mask or
        no trace is not a pass.
        """
        mask = state.get("mask")
        if mask is None or not mask.filled():
            return None
        # The instrument's own tally first, where there is one. It is
        # the only verdict available in DPO - there is no waveform
        # record to judge, every sample of it reads back mid-scale -
        # and it is the better one everywhere else too, being taken
        # over every acquisition of the capture rather than the single
        # record that came back. See Worker.msk_count_read.
        hits = state.get("mhits")
        if hits and hits.get("waves"):
            return ((_("FAIL"), "#e04a4a") if hits.get("total")
                    else (_("PASS"), "#3fb950"))
        wave = state.get("mwave")
        if wave is None:
            return None
        xy, _bounds = tds_wfm.plot_geometry(
            wave, max(mplot.winfo_width(), 80),
            max(mplot.winfo_height(), 60), 20, state.get("mview"), room=8)
        frame = mask_frame()
        segs = [seg for _n, seg in mask.filled()]
        for px, py in xy:
            x, y = tds_msk.from_canvas(px, py, frame)
            for seg in segs:
                if tds_msk.inside(seg, x, y):
                    return (_("FAIL"), "#e04a4a")
        return (_("PASS"), "#3fb950")

    def msk_shot_behind(which="m"):
        """The captured screen as plot_png wants it, or None.

        The same pixels the canvas draws behind the drawing, so the
        saved picture is the picture on screen. In DPO it is the only
        thing there is to show: a saved mask test with an empty
        graticule behind it is a verdict nobody can check.
        """
        shot = state.get(which + "shot")
        if not shot:
            return None
        return (shot["pixels"], shot["palette"],
                shot["width"], shot["height"])

    def msk_say_hits():
        """What the instrument counted, for the end of the status line.

        Empty where it has no tally to give - a scope without the mask
        option, or nothing loaded in its segments - so the sentence in
        front of this reads properly on its own.
        """
        hits = state.get("mhits")
        if not hits or not hits.get("waves"):
            return ""
        if not hits.get("total"):
            return (_(" - the instrument tested %d acquisition(s) and "
                      "counted no hits") % hits["waves"])
        return (_(" - the instrument counted %(hits)d hit(s) over "
                  "%(waves)d acquisition(s), in segment(s) %(which)s")
                % {"hits": hits["total"], "waves": hits["waves"],
                   "which": ", ".join(str(n) for n in
                                      sorted(hits.get("each") or {}))})

    def msk_shot_image(which, frame):
        """The captured screen, scaled to `frame`, as a PhotoImage.

        Rescaled whenever the window changes size and kept until it
        does, because the scaling is the expensive part and a resize is
        the only thing that invalidates it.

        The frame is passed rather than measured, and the drawing named
        rather than inferred: this is called while one canvas is being
        redrawn, and which canvas that is is not always the one the tab
        strip is showing.
        """
        shot = state.get(which + "shot")
        if not shot:
            return None
        left, top, right, bottom = frame
        wide, tall = int(right - left), int(bottom - top)
        if wide < 8 or tall < 8:
            return None
        held = state.get(which + "shotimage")
        if held and held[0] == (wide, tall):
            return held[1]
        try:
            png = tds_wfm.scaled_indexed(shot["pixels"], shot["palette"],
                                         shot["width"], shot["height"],
                                         wide, tall)
            image = tk.PhotoImage(data=base64.b64encode(png))
        except Exception as exc:
            log_note("mask", "the captured screen would not scale: %s" % exc)
            state.pop(which + "shot", None)
            return None
        state[which + "shotimage"] = ((wide, tall), image)
        return image

    def do_msk_view_save():
        """The mask editor's graticule as a PNG, the way the plot saves.

        The same renderer the waveform tab uses, given the shapes as
        well - so the picture that comes out is the picture on screen,
        in the same scheme and at the size set in the colours dialog.
        """
        mask = state.get("mask")
        # A mask with nothing in it is still worth saving when there is
        # a trace or a captured screen behind it - that picture is the
        # evidence. The button is greyed when there is none of the
        # three, so this is the case where the drawing alone is empty.
        if mask is None or not (mask.filled() or state.get("mwave")
                                is not None or state.get("mshot")):
            messagebox.showinfo(_("Nothing to save"),
                                _("This mask has no segments with points "
                                  "in them yet."))
            return
        path = filedialog.asksaveasfilename(
            parent=root, title=_("Save image"), defaultextension=".png",
            initialfile=stamped(mask.name or _("mask"), ".png"),
            filetypes=[(_("PNG image (*.png)"), "*.png")])
        if not path:
            return
        wave = state.get("mwave")
        try:
            with open(path, "wb") as fh:
                fh.write(tds_wfm.plot_png(
                    [wave] if wave is not None else [],
                    width=state["pngsize"][0], height=state["pngsize"][1],
                    colours=state.get("colours"),
                    caption=wave_scales(wave) if wave is not None else "",
                    shapes=[seg for _n, seg in mask.filled()],
                    grid=(mask_step() if state["mshowgrid"].get()
                          else (0.0, 0.0)),
                    verdict=msk_verdict(),
                    behind=msk_shot_behind()))
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                 % (_("Could not save"), exc))
            return
        ui.say(_("Saved %s") % os.path.basename(path))

    def do_msk_boolean(how):
        """Join, overlap or take away, on the two chosen shapes.

        The answer is the outline the two shapes make, and it is more
        than one shape only where the answer really is - a bar taken
        out of the middle leaves two. It can be concave, and it can be
        a shape the instrument would redraw; that is said under the
        canvas like any other, and it is yours to adjust. See
        tds_msk.combine.
        """
        mask = edit_mask()
        if mask is None:
            return
        order = [n for n in (state.get("morder") or [])
                 if n < len(mask.segments) and len(mask.segments[n]) >= 3]
        if len(order) < 2:
            messagebox.showinfo(
                _("Two shapes are needed"),
                _("Choose a shape with the four-way arrow, then hold "
                  "Shift and click a second one. The order matters: "
                  "subtract takes the second out of the first."))
            return
        first, second = order[0], order[1]
        pieces = tds_msk.combine(how, mask.segments[first],
                                 mask.segments[second])
        if not pieces:
            messagebox.showinfo(
                _("Nothing came of that"),
                _("Those two shapes leave nothing behind - they may not "
                  "overlap at all, or one may cover the other "
                  "completely."))
            return
        room = sorted(set(msk_room() + [first, second]))
        if len(pieces) > len(room):
            messagebox.showerror(
                _("Error"), "%s\n\n%s"
                % (_("There is not room for the result."),
                   _("It comes to %(pieces)d shape(s) and there "
                     "are %(room)d segment(s) free. Delete a shape and "
                     "try again.")
                   % {"pieces": len(pieces), "room": len(room)}))
            return
        msk_remember()
        mask.segments[first] = []
        mask.segments[second] = []
        for at, piece in zip(room, pieces):
            mask.segments[at] = [(round(x, 2), round(y, 2))
                                 for x, y in piece]
        state["mpicks"] = []
        state["morder"] = [at for at, _p in zip(room, pieces)]
        edit_redraw()
        ui.say(_("%d shape(s) from that") % len(pieces))

    def do_msk_press(evt):
        if edit_mask() is None:
            # Nothing open and somebody has started drawing: that is a
            # new drawing, and making them press a button to say so is
            # a button press that carries no information. Which kind
            # of drawing depends on the tab, or the limits tab's pen
            # would quietly start a mask and then draw nothing.
            edit_new()
        if edit_mask() is None:
            return
        edit_canvas().focus_set()
        what = msk_what_at(evt)
        if msk_tool() == "eraser":
            msk_erase_press(evt, what)
        elif msk_tool() == "pen":
            msk_pen_press(evt, what)
        elif msk_tool() == "cut":
            msk_cut_press(evt, what)
        else:
            msk_move_press(evt, what)

    def do_msk_menu(evt):
        """Right button: a point's own menu, or an end to this shape."""
        if edit_mask() is None:
            return
        # Something in hand beats anything under the pointer. A shape
        # being drawn is usually drawn *over* itself - the pointer sits
        # on the edge just laid down - so asking what is under it first
        # popped a Delete/Coordinates menu for that edge instead of
        # ending the line, and the only way out was Escape.
        if (state.get("mdrawing") is not None
                or state.get("mcutfrom") is not None):
            do_msk_finish()
            return
        what = msk_what_at(evt)
        if what is not None and what[0] != "point":
            # A side or the inside both mean the shape: the same two
            # things can be done to it, and where it is means its
            # bounding box's middle - type 50,50 and it lands in the
            # middle of the graticule, which is what somebody
            # positioning a shape means by its coordinates.
            seg = what[1][0] if what[0] == "edge" else what[1]
            menu = tk.Menu(root, tearoff=0)
            menu.add_command(label=_("Delete"),
                             command=lambda: msk_drop_shape(seg))
            menu.add_command(label=_("Coordinates"),
                             command=lambda: msk_typein(seg))
            try:
                menu.tk_popup(evt.x_root, evt.y_root)
            finally:
                menu.grab_release()
            return
        if what is not None and what[0] == "point":
            seg, point = what[1]
            menu = tk.Menu(root, tearoff=0)
            menu.add_command(
                label=_("Delete"),
                command=lambda: msk_drop_point(seg, point))
            menu.add_command(
                label=_("Coordinates"),
                command=lambda: msk_typein(seg, point))
            try:
                menu.tk_popup(evt.x_root, evt.y_root)
            finally:
                menu.grab_release()
            return
        if state.get("mdrawing") is None and state.get("mcutfrom") is None:
            if msk_picked() or state.get("morder"):
                state["mpicks"] = []
                state["morder"] = []
                edit_redraw()
                ui.say(_("Nothing selected"))
            return
        do_msk_finish()

    def do_msk_move(evt):
        mask = edit_mask()
        if mask is None:
            return
        band = state.get("mband")
        if band is not None:
            x0, y0 = band["at"]
            if not band["moved"] and abs(evt.x - x0) + abs(evt.y - y0) > 3:
                band["moved"] = True
            if band["moved"]:
                band["to"] = (evt.x, evt.y)
                state["mpicks"] = msk_in_band(band)
                edit_soon()
            return
        drag = state.get("mdrag")
        if drag is None:
            return
        now = tds_msk.from_canvas(evt.x, evt.y, mask_frame())
        dx = now[0] - drag["from"][0]
        dy = now[1] - drag["from"][1]
        across, up = mask_grid()
        anchor = drag.get("anchor")
        if anchor is not None and anchor in drag["was"]:
            # Snap the point under the pointer and move the rest by the
            # same amount, which is what keeps a shape's own geometry
            # while still landing that point on the grid.
            ax, ay = drag["was"][anchor]
            dx = tds_msk.snapped(ax + dx, across) - ax
            dy = tds_msk.snapped(ay + dy, up) - ay
        elif across and up:
            dx, dy = tds_msk.snap_point(dx, dy, across, up)
        # The move is limited, not each point: clamping the points one
        # by one would flatten a shape against the edge instead of
        # stopping it there. A selection that is already off the
        # graticule - which a mask read from somebody's file may be -
        # is left alone rather than jerked inwards.
        was = list(drag["was"].values())
        if was:
            lo_x, hi_x = min(p[0] for p in was), max(p[0] for p in was)
            lo_y, hi_y = min(p[1] for p in was), max(p[1] for p in was)
            if 0.0 <= lo_x and hi_x <= 100.0:
                dx = min(max(dx, -lo_x), 100.0 - hi_x)
            if 0.0 <= lo_y and hi_y <= 100.0:
                dy = min(max(dy, -lo_y), 100.0 - hi_y)
        for (s, i), (wx, wy) in drag["was"].items():
            if s < len(mask.segments) and i < len(mask.segments[s]):
                mask.segments[s][i] = (wx + dx, wy + dy)
        edit_soon()

    def msk_in_band(band):
        """Every point inside the rubber band, as (segment, point)."""
        mask = edit_mask()
        if mask is None or "to" not in band:
            return []
        ax, ay = tds_msk.from_canvas(band["at"][0], band["at"][1],
                                     mask_frame())
        bx, by = tds_msk.from_canvas(band["to"][0], band["to"][1],
                                     mask_frame())
        lo_x, hi_x = min(ax, bx), max(ax, bx)
        lo_y, hi_y = min(ay, by), max(ay, by)
        out = []
        for s, seg in enumerate(mask.segments):
            for i, (x, y) in enumerate(seg):
                if lo_x <= x <= hi_x and lo_y <= y <= hi_y:
                    out.append((s, i))
        return out

    def do_msk_release(_evt=None):
        drag = state.pop("mdrag", None)
        band = state.pop("mband", None)
        if drag is not None:
            # One undo step for the whole drag, and none at all for a
            # press that only selected something.
            before = drag.get("before")
            moved = before is not None and before != msk_shapes()
            if moved:
                msk_keep(before)
            narrow = state.pop("mnarrow", None)
            if not moved and narrow and msk_picked() != narrow:
                # A click, not a drag, on something that was part of a
                # larger selection: now it is the selection.
                state["mpicks"] = list(narrow)
                state["morder"] = [narrow[0][0]]
                edit_redraw()
            return
        if band is None:
            return
        if band["moved"]:
            ui.say(_("%d point(s) selected") % len(msk_picked()))
            edit_redraw()
        elif msk_tool() == "move":
            state["mpicks"] = []          # a click on nothing lets go
            edit_redraw()

    def do_msk_finish(_evt=None):
        """Right button, Enter or Escape: put down whatever is in hand."""
        if state.pop("mcutfrom", None) is not None:
            edit_redraw()
            ui.say(_("Split abandoned"))
            return
        if state.pop("mdrawing", None) is not None:
            edit_redraw()
            ui.say(_("Shape finished"))

    def msk_one_side(picks):
        """The side these two picks are, if that is what they are.

        Returns (segment, first point index) for two neighbouring points
        of one shape, and None otherwise.
        """
        if len(picks) != 2 or picks[0][0] != picks[1][0]:
            return None
        seg = picks[0][0]
        mask = edit_mask()
        count = len(mask.segments[seg]) if mask else 0
        if count < 3:
            return None
        a, b = sorted((picks[0][1], picks[1][1]))
        if b - a == 1:
            return (seg, a)
        return (seg, b) if a == 0 and b == count - 1 else None

    def do_msk_remove(_evt=None):
        """Delete: take out every selected point.

        A whole side is the exception. Taking out both its ends takes
        the two sides either side of it with them - a rectangle with one
        side deleted came out as a line - so a side is *collapsed*
        instead: its two ends become one point in the middle, the shape
        keeps its other corners, and it has one side fewer, which is
        what deleting a side means.

        Points are taken out from the end of each segment backwards,
        because removing point 2 moves point 5 and a list of positions
        gathered before any of them were removed is otherwise wrong by
        the second one.
        """
        mask = edit_mask()
        picks = msk_picked()
        if mask is None or not picks:
            return
        side = msk_one_side(picks)
        if side is not None:
            seg, first = side
            points = mask.segments[seg]
            second = (first + 1) % len(points)
            ax, ay = points[first]
            bx, by = points[second]
            msk_remember()
            points[first] = (round((ax + bx) / 2.0, 2),
                             round((ay + by) / 2.0, 2))
            points.pop(second)
            state["mpicks"] = [(seg, min(first, len(points) - 1))]
            state.pop("mhover", None)
            edit_redraw()
            return
        msk_remember()
        for s, i in sorted(picks, reverse=True):
            if s < len(mask.segments) and i < len(mask.segments[s]):
                mask.segments[s].pop(i)
        state["mpicks"] = []
        edit_redraw()

    def msk_nudge_step():
        """How far one arrow key moves what is selected.

        A distance set on the Settings tab, the same both ways, where
        one has been chosen. Left alone it follows the grid: a fifth of
        a square, so the keys and the snapping agree with one another
        and five presses cross one. With snapping off there is no grid
        to be a fraction of, so it is half a percent - about a
        twentieth of a division.
        """
        step = state.get("nudge")
        if step:
            return step, step
        across, up = mask_grid()
        return ((across / 5.0, up / 5.0) if across and up
                else (0.5, 0.5))

    def do_msk_nudge(dx, dy):
        """Arrow keys: move the selection, without the mouse.

        The mouse is for getting a shape roughly right; this is for the
        last half-division, where a hand on a mouse cannot be trusted.
        """
        mask = edit_mask()
        picks = msk_picked()
        if mask is None or not picks:
            return "break"
        across, up = msk_nudge_step()
        # A run of presses on the same selection is one step back, not
        # twenty. Somebody walking a point into place would otherwise
        # spend the whole history doing it and have nothing left to undo
        # the thing they were fixing.
        run = ("nudge", tuple(sorted(picks)))
        if state.get("mrun") != run:
            msk_remember()
        state["mrun"] = run
        here = [mask.segments[s][i] for s, i in picks
                if s < len(mask.segments) and i < len(mask.segments[s])]
        if not here:
            return "break"
        # Limited as one, the same way a drag is: nudging a shape into
        # the edge should stop it there rather than fold it flat.
        step_x, step_y = dx * across, dy * up
        lo_x, hi_x = min(p[0] for p in here), max(p[0] for p in here)
        lo_y, hi_y = min(p[1] for p in here), max(p[1] for p in here)
        if 0.0 <= lo_x and hi_x <= 100.0:
            step_x = min(max(step_x, -lo_x), 100.0 - hi_x)
        if 0.0 <= lo_y and hi_y <= 100.0:
            step_y = min(max(step_y, -lo_y), 100.0 - hi_y)
        for s, i in picks:
            if s < len(mask.segments) and i < len(mask.segments[s]):
                x, y = mask.segments[s][i]
                mask.segments[s][i] = (round(x + step_x, 2),
                                       round(y + step_y, 2))
        edit_redraw()
        return "break"

    def msk_bounds(seg):
        """The middle of a shape's extents, and its width and height."""
        xs = [x for x, _y in seg]
        ys = [y for _x, y in seg]
        return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)

    def msk_typein(seg, point=None):
        """The shape's coordinates: its midpoint, and every point in it.

        Clicking gets a shape roughly right and is the wrong tool for
        getting one exactly right - a mask edge at 12.5% is a number,
        not a pixel somebody aimed at. Two decimal places, because that
        is a hundredth of the graticule and finer than the instrument
        stores.

        The midpoint is the middle of the bounding box rather than the
        centre of area: type 50,50 and the shape lands in the middle of
        the graticule, which is what positioning a shape means. A centre
        of area would put an L-shape's heavy end in the middle instead.

        Both the midpoint and the cells edit live: the drawing follows
        every keystroke that parses, and one undo step covers a whole
        edit rather than one per character.
        """
        mask = edit_mask()
        if mask is None or seg >= len(mask.segments):
            return
        if not mask.segments[seg]:
            return
        dlg, pad = popup(_("Segment %d") % (seg + 1))
        ttk.Label(pad, text=_("Coordinates are in percent of the "
                              "graticule: X across, Y up.")).pack(
                                  anchor="w", pady=(0, 8))
        origin = tk.StringVar()
        place = ttk.Frame(pad)
        place.pack(fill="x")
        ttk.Label(place, text=_("Shape centre")).pack(side="left")
        mid = ttk.Entry(place, textvariable=origin, width=16)
        mid.pack(side="left", padx=6)
        hints(mid, "The shape's centre, as X,Y in percent of the "
                   "graticule")
        ttk.Label(pad, text=_("Points")).pack(anchor="w", pady=(10, 2))
        rows = ttk.Treeview(pad, columns=("n", "x", "y"), show="headings",
                            height=8, selectmode="browse")
        for name, title, wide in (("n", "#", 44), ("x", "X", 82),
                                  ("y", "Y", 82)):
            rows.heading(name, text=title)
            rows.column(name, width=wide, anchor="e", stretch=False)
        rows.pack(fill="both", expand=True)
        cell = {}
        move = {}
        size = [len(mask.segments[seg])]

        def gone():
            """True, and the box closed, if its shape changed under it.

            The box is not modal, and it holds the drawing and the
            shape number it was opened on. Undo, the eraser, a point
            added on the canvas, another mask opened or the other tab
            brought up all change what that number means, and a typed
            coordinate then went into the wrong shape or the wrong
            drawing - or raised IndexError past the end of it.
            """
            if (edit_mask() is mask and seg < len(mask.segments)
                    and len(mask.segments[seg]) == size[0]):
                return False
            try:
                alive = dlg.winfo_exists()
            except Exception:
                alive = False
            if alive:
                ui.say(_("The shape changed, so its coordinates box was "
                         "closed"))
                dlg.destroy()
            return True

        def show_row(i):
            x, y = mask.segments[seg][i]
            rows.set(str(i), "x", "%.2f" % x)
            rows.set(str(i), "y", "%.2f" % y)

        def refill(keep=None):
            size[0] = len(mask.segments[seg])
            origin.set("%.2f, %.2f" % msk_bounds(mask.segments[seg]))
            rows.delete(*rows.get_children())
            for i, (x, y) in enumerate(mask.segments[seg]):
                rows.insert("", "end", iid=str(i),
                            values=(i + 1, "%.2f" % x, "%.2f" % y))
            at = min(max(0, keep or 0), len(mask.segments[seg]) - 1)
            rows.selection_set(str(at))
            rows.focus(str(at))
            rows.see(str(at))

        def cell_shut(_evt=None):
            box = cell.pop("box", None)
            if box is not None:
                box.destroy()

        def cell_type(_evt=None):
            """Live: a number that parses moves the point as it is typed."""
            box = cell.get("box")
            if box is None or gone():
                return
            try:
                said = round(typed(box.get()), 2)
            except ValueError:
                return
            i, which = cell["at"]
            x, y = mask.segments[seg][i]
            now = (said, y) if which == "x" else (x, said)
            if now == mask.segments[seg][i]:
                return
            if not cell["kept"]:
                msk_remember()
                cell["kept"] = True
            mask.segments[seg][i] = now
            show_row(i)
            origin.set("%.2f, %.2f" % msk_bounds(mask.segments[seg]))
            edit_redraw()

        def cell_drop(_evt=None):
            """Escape: the point goes back to where the edit found it."""
            if cell.get("box") is None:
                return None
            if gone():
                return "break"
            if cell["kept"]:
                i = cell["at"][0]
                mask.segments[seg][i] = cell["was"]
                show_row(i)
                origin.set("%.2f, %.2f" % msk_bounds(mask.segments[seg]))
                edit_redraw()
            cell_shut()
            return "break"

        def cell_start(item, which):
            """An entry laid over the cell: edit the number in place."""
            cell_shut()
            if gone():
                return
            over = rows.bbox(item, "#2" if which == "x" else "#3")
            if not over:
                return
            box = ttk.Entry(rows, justify="right")
            box.insert(0, rows.set(item, which))
            box.select_range(0, "end")
            box.place(x=over[0], y=over[1], width=over[2], height=over[3])
            box.focus_set()
            cell.update(box=box, at=(int(item), which), kept=False,
                        was=mask.segments[seg][int(item)])
            box.bind("<KeyRelease>", cell_type)
            box.bind("<Return>", lambda e: (cell_type(), cell_shut()))
            box.bind("<Escape>", cell_drop)
            box.bind("<FocusOut>", lambda e: (cell_type(), cell_shut()))

        def cell_open(evt):
            """Double-click: edit whichever cell was clicked."""
            item = rows.identify_row(evt.y)
            column = rows.identify_column(evt.x)
            if item and column in ("#2", "#3"):
                cell_start(item, "x" if column == "#2" else "y")

        def cell_here(_evt=None):
            """Return: edit the X of the row in front."""
            if rows.focus():
                cell_start(rows.focus(), "x")

        rows.bind("<Double-1>", cell_open)
        rows.bind("<Return>", cell_here)

        def mid_hold(_evt=None):
            """Where the shape was, so that a move never compounds."""
            if gone():
                return
            move.update(was=list(mask.segments[seg]),
                        at=msk_bounds(mask.segments[seg]), kept=False)

        def mid_type(_evt=None):
            """Live: a pair that parses slides the whole shape."""
            if "was" not in move or gone():
                return
            try:
                bits = [float(b) for b
                        in origin.get().replace(";", ",").split(",")]
            except ValueError:
                return
            if len(bits) != 2:
                return
            dx = round(bits[0], 2) - move["at"][0]
            dy = round(bits[1], 2) - move["at"][1]
            slid = [(round(x + dx, 2), round(y + dy, 2))
                    for x, y in move["was"]]
            if slid == mask.segments[seg]:
                return
            if not move["kept"]:
                msk_remember()
                move["kept"] = True
            mask.segments[seg] = slid
            for i in range(len(slid)):
                show_row(i)
            edit_redraw()

        def mid_done(_evt=None):
            move.pop("was", None)
            if gone():
                return
            origin.set("%.2f, %.2f" % msk_bounds(mask.segments[seg]))

        mid.bind("<FocusIn>", mid_hold)
        mid.bind("<KeyRelease>", mid_type)
        mid.bind("<FocusOut>", mid_done)
        ttk.Button(pad, text=_("Close"), command=dlg.destroy).pack(
            side="right", pady=(12, 0))
        refill(point)
        # Number this shape's points on the canvas while the box
        # is open, so its rows can be matched to the handles.
        # Dropped however it is closed - the button, Escape, or
        # the window's own corner - which is why it hangs off
        # <Destroy> rather than off any one of them.
        state["mnumbers"] = seg
        edit_redraw()

        def shut(evt):
            if evt.widget is dlg:
                state.pop("mnumbers", None)
                if state.get("mcoords") is dlg:
                    state.pop("mcoords", None)
                edit_redraw()

        dlg.bind("<Destroy>", shut)
        state["mcoords"] = dlg
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (root.winfo_rootx() + 120,
                                 root.winfo_rooty() + 120))
        state["dialog"] = dlg

    def do_msk_typein(evt):
        """Double-click a point: the same dialog, from the drawing.

        Not while a shape is being drawn. The second click lands on the
        point the first one just laid down, so what used to happen was
        the coordinate box opening on top of a half-drawn shape - and
        the double-click is the ordinary way to say "that is the last
        point", which is what it now does.
        """
        if state.get("mdrawing") is not None:
            do_msk_finish()
            return "break"
        what = msk_what_at(evt)
        if what is not None and what[0] == "point":
            msk_typein(*what[1])

    def msk_new_segment():
        """The next empty segment, or None with a word about why not."""
        mask = edit_mask()
        if mask is None:
            return None
        while len(mask.segments) < edit_segments():
            mask.segments.append([])
        for i, seg in enumerate(mask.segments):
            if not seg:
                return i
        messagebox.showinfo(
            _("Nothing left to draw in"),
            (_("This drawing already has %d shapes in it. Delete "
               "one before drawing another.")
             if edit_here() == "lim" else
             _("This mask already uses all %d segments the "
               "instrument has. Delete one before drawing "
               "another.")) % edit_segments())
        return None

    # ------------------------------------------------ undoing an edit
    # A mask is eight short lists of pairs, so a step back is a copy of
    # them and nothing cleverer is worth writing. Twenty deep: enough to
    # get out of a bad drag, and small enough that the copies cost
    # nothing worth measuring.
    UNDO_DEEP = 20

    def msk_shapes():
        """The mask's geometry, copied, or None when none is open."""
        mask = edit_mask()
        return None if mask is None else [list(seg) for seg in mask.segments]

    def msk_keep(shapes):
        """Put a kept copy of the geometry on the undo stack."""
        if shapes is None:
            return
        state[edit_key("dirty")] = True
        state.setdefault(edit_key("undo"), []).append(shapes)
        del state[edit_key("undo")][:-UNDO_DEEP]
        state[edit_key("redo")] = []         # a new edit forks the future
        state["mrun"] = None                 # and ends any run of nudges
        msk_undo_buttons()

    def msk_remember():
        """Keep the mask as it stands, before something changes it.

        Called at the start of every edit rather than at the end: what
        undo has to put back is the state before, and after the change
        that is gone. A drag is the exception - it keeps its own copy at
        the press and hands it over at the release, so a press that only
        selected something is not an edit.
        """
        msk_keep(msk_shapes())

    def msk_forget():
        """A different mask is open; the old one's history is not its own."""
        state[edit_key("undo")], state[edit_key("redo")] = [], []
        state[edit_key("dirty")] = False
        # Nor is the old one's verdict: the instrument counted those
        # hits against whatever was in its segments a moment ago.
        state.pop("mhits", None)
        # And a mask opened is a mask to be looked at. Hiding is for
        # getting a shape out of the way of the trace under it, not a
        # setting to be inherited by whatever is opened next.
        state["mhide"].set(False)
        state["mnodots"].set(False)
        msk_undo_buttons()

    def msk_may_discard():
        """True if the drawing on screen may be thrown away.

        Yes, No, Cancel, the way every other program asks: saving is
        offered rather than assumed, and Cancel means the drawing is
        left exactly as it was.

        Whichever tab is in front, and it saves that tab's kind of
        file: offering to save a limit template and writing a mask
        would put it in the wrong library under the wrong meaning.
        """
        mask = edit_mask()
        if (not state.get(edit_key("dirty")) or mask is None
                or not mask.filled()):
            return True
        if edit_here() == "lim":
            answer = messagebox.askyesnocancel(
                _("Save changes?"),
                _("%s has been changed. Save it before starting a new "
                  "limit template?")
                % (mask.name or _("This limit template")),
                parent=root, default="yes")
        else:
            answer = messagebox.askyesnocancel(
                _("Save changes?"),
                _("%s has been changed. Save it before starting a new "
                  "mask?")
                % (mask.name or _("This mask")), parent=root,
                default="yes")
        if answer is None:
            return False
        if answer:
            (ui.do_lim_file_save if edit_here() == "lim" else do_msk_save)()
            # a cancelled save cancels
            return not state.get(edit_key("dirty"))
        return True

    def msk_restore(shapes):
        mask = edit_mask()
        if mask is None:
            return
        mask.segments = [list(seg) for seg in shapes]
        state["mrun"] = None
        state["mpicks"] = []
        state.pop("mdrawing", None)
        state.pop("mhover", None)
        edit_redraw()

    def msk_undo_buttons():
        """Both tabs' pairs, each from its own drawing's history."""
        for key, btn in (state.get("undobtn") or {}).items():
            btn.state(["!disabled" if state.get(key)
                       else "disabled"])

    def do_msk_undo(_evt=None):
        if not state.get(edit_key("undo")):
            return
        state.setdefault(edit_key("redo"), []).append(msk_shapes())
        del state[edit_key("redo")][:-UNDO_DEEP]
        msk_restore(state[edit_key("undo")].pop())
        msk_undo_buttons()
        ui.say(_("Undone"))

    def do_msk_redo(_evt=None):
        if not state.get(edit_key("redo")):
            return
        state.setdefault(edit_key("undo"), []).append(msk_shapes())
        del state[edit_key("undo")][:-UNDO_DEEP]
        msk_restore(state[edit_key("redo")].pop())
        msk_undo_buttons()
        ui.say(_("Redone"))

    def msk_tool_changed():
        """A different tool is in hand: whatever the last one was in the
        middle of is finished, not carried over.

        And the mask comes back out of hiding, points and all. Picking
        up a tool is asking to edit the mask, and editing a mask nobody
        can see - or dragging handles that are not drawn - is the sort
        of thing that only makes sense to the person who wrote the
        checkbox.
        """
        state.pop("mdrawing", None)
        state.pop("mhover", None)
        state.pop("mcutfrom", None)
        state["mhide"].set(False)
        state["mnodots"].set(False)
        edit_canvas().focus_set()
        edit_redraw()
        msk_say_tool()

    def msk_say_tool():
        """What the tool in hand does, in the line along the bottom."""
        ui.say({"pen": _("Click to place points; right-click, Enter or "
                         "Escape finishes the shape"),
                "move": _("Drag a point to move it, or an edge to move the "
                          "whole shape; arrow keys nudge what is selected"),
                "eraser": _("Click a point to delete it, or an edge to "
                            "delete the whole shape"),
                "cut": _("Click one point of a shape, then another, to cut "
                         "it in two between them")}[msk_tool()])

    def msk_tool():
        return state["mtool"].get()

    def msk_crosshair():
        """Redraw only the crosshair and its reading.

        The pointer moves a lot and the canvas is expensive to rebuild -
        a tenth-division grid alone is eight thousand items - so a move
        that changes nothing but where the crosshair is redraws nothing
        but the crosshair.
        """
        edit_canvas().delete("cross")
        at = state.get("mcrossat")
        if not state["mcross"].get() or at is None:
            return
        left, top, right, bottom = mask_frame()
        pick = plot_colours(None)
        cx, cy = at
        if not (left <= cx <= right and top <= cy <= bottom):
            return
        # The label's colour rather than the grid's: a crosshair the
        # same colour as the grid it is drawn over cannot be found.
        edit_canvas().create_line(left, cy, right, cy, fill=pick["label"],
                          dash=(4, 4), tags="cross")
        edit_canvas().create_line(cx, top, cx, bottom, fill=pick["label"],
                          dash=(4, 4), tags="cross")
        px, py = tds_msk.from_canvas(cx, cy, mask_frame())
        edit_canvas().create_text(right - 4, top + 4, anchor="ne",
                          fill=pick["label"],
                          text="%.2f, %.2f" % (px, py), tags="cross")

    def do_msk_hover(evt):
        """Follow the pointer: what is under it, and the crosshairs.

        What is under it is drawn highlighted, so a click is never a
        guess about which point or which edge it is going to take. The
        pen has no use for a whole shape - it acts on edges and points -
        so it is not offered one.
        """
        state["mcrossat"] = (evt.x, evt.y)
        was = state.get("mhover")
        now = msk_what_at(evt)
        if now is not None and now[0] == "shape" and msk_tool() == "pen":
            now = None
        state["mhover"] = now
        if (now != was or state.get("mdrawing") is not None
                or state.get("mcutfrom") is not None):
            # The crosshair now, the rebuild when the moving stops. The
            # crosshair is three items and the thing the eye is
            # following; the rebuild is five hundred and can wait a
            # frame. Leaving the crosshair to the rebuild made it lag
            # exactly where the pointer had most to say - over the
            # handles, where what is under it keeps changing.
            if state["mcross"].get():
                msk_crosshair()
            edit_soon()
        elif state["mcross"].get():
            msk_crosshair()

    def do_msk_leave(_evt=None):
        gone = state.pop("mcrossat", None) is not None
        gone = state.pop("mhover", None) is not None or gone
        if gone:
            edit_redraw()

    def edit_bindings(canvas):
        """Point the drawing tools at a canvas.

        Called for both of them. Which drawing a gesture acts on is
        decided when it arrives, by which tab is in front, so the same
        handlers serve the masks tab and the limits tab - see edit_here.
        """
        canvas.bind("<Motion>", do_msk_hover)
        canvas.bind("<Leave>", do_msk_leave)
        canvas.bind("<ButtonPress-1>", do_msk_press)
        canvas.bind("<B1-Motion>", do_msk_move)
        canvas.bind("<ButtonRelease-1>", do_msk_release)
        canvas.bind("<Double-Button-1>", do_msk_typein)
        canvas.bind("<Button-3>", do_msk_menu)
        canvas.bind("<Escape>", do_msk_finish)
        canvas.bind("<Return>", do_msk_finish)
        canvas.bind("<KP_Enter>", do_msk_finish)
        # "break", so the key stops here. Tk sends an unhandled key on
        # up to the toplevel, where Delete belongs to the file tab - and
        # a Delete pressed in the mask editor was reaching it and asking
        # the instrument to delete whatever the file list had selected.
        canvas.bind("<Delete>", lambda e: do_msk_remove(e) or "break")
        canvas.bind("<BackSpace>", lambda e: do_msk_remove(e) or "break")
        canvas.bind("<Control-x>", lambda e: do_msk_copy(e, cut=True))
        canvas.bind("<Control-c>", do_msk_copy)
        canvas.bind("<Control-v>", do_msk_paste)
        canvas.bind("<Control-z>", do_msk_undo)
        canvas.bind("<Control-y>", do_msk_redo)
        canvas.bind("<Control-Z>", do_msk_redo)     # Ctrl+Shift+Z as well
        # Up is up: percent counts from the bottom of the graticule
        # here, the way a person means it, and the flip to the
        # instrument's own upside-down numbering happens once, in
        # tds_msk.
        for key, dx, dy in (("Left", -1, 0), ("Right", 1, 0),
                            ("Up", 0, 1), ("Down", 0, -1)):
            canvas.bind("<%s>" % key,
                        lambda e, ax=dx, ay=dy: do_msk_nudge(ax, ay))
        canvas.configure(takefocus=True)
        canvas.bind("<Button-1>", lambda e: canvas.focus_set(), add="+")

    edit_bindings(mplot)
    msk_undo_buttons()

    # ------------------------------------------------------ the lists
    def msk_folder():
        """Where masks are kept on this computer.

        Beside the program rather than in a place chosen by a dialog:
        the point of a library is that it is always the same place.

        Topped up from the bundled set every run (tds_decode.sync_folder,
        shared with the decoder library), because the exe unpacks its
        bundled files to a temporary folder that is deleted on exit - a
        library there would be gone by the next run, and one that cannot be
        added to is not a library. Copied rather than read from both places
        so renaming or deleting one works the same as for a mask somebody
        drew; copy-missing rather than seed-once so a mask shipped in an
        update appears without the user reseeding, while their own is left
        untouched.
        """
        here = os.path.join(APPDIR, "masks")
        try:
            return tds_decode.sync_folder(resource("masks"), here)
        except OSError as exc:
            log_note("masks", "%s unusable (%s)" % (here, exc))
            return None

    def msk_signal(name, path=None):
        """What the list says a mask is for, in a column's worth.

        A mask Tektronix shipped says it in its name; anything else says
        it in the setup saved beside it. Both answer the same question -
        what signal is this drawn for - so they share the column.
        """
        rate = tds_msk.standard(name)[1]
        if rate or not path:
            return rate
        try:
            fields = tds_set.parse(tds_set.contents(tds_set.beside(path)))
        except OSError:
            return ""
        secdiv = tds_set.number(fields, "secdiv")
        return "%s/div" % tds_wfm.eng(secdiv, "s") if secdiv else ""

    def msk_describe(mask):
        """The one-line shape of a mask, for the second column.

        Whether it is a pulse mask or an eye is worth a column of its
        own here: it decides whether the mask can go to an instrument as
        a limit template at all, and finding that out by pressing the
        button and being refused is a poor way to learn it.
        """
        segs = mask.filled()
        try:
            what = _("pulse") if tds_msk.kind(mask) == "pulse" else _("eye")
        except Exception:
            what = "?"
        return _("%(kind)s, %(segs)d seg, %(pts)d pts") % {
            "kind": what, "segs": len(segs), "pts": len(mask.points)}

    def do_msk_scan():
        """Re-read both libraries and the instrument's live segments."""
        here = msk_folder()
        mpc.delete(*mpc.get_children())
        state["mpcfiles"] = {}
        for name in sorted(os.listdir(here) if here else []):
            if not name.upper().endswith(tds_msk.SUFFIX):
                continue
            path = os.path.join(here, name)
            try:
                with open(path, "rb") as fh:
                    mask = tds_msk.load(fh.read(),
                                        name=os.path.splitext(name)[0])
            except Exception as exc:
                mpc.insert("", "end", iid=name,
                           values=(name, "", _("will not read: %s") % exc))
                continue
            state["mpcfiles"][name] = path
            mpc.insert("", "end", iid=name,
                       values=(mask.name or name, msk_signal(name, path),
                               msk_describe(mask)))
        msk_buttons()
        ui.say(_("%d mask(s) on this computer") % len(state["mpcfiles"]))
        msk_scan_scope()

    def msk_scan_scope():
        """Ask the instrument what is in its eight segments, if there is
        an instrument.

        Skipped entirely when nothing is connected, so the Masks tab is
        usable with no instrument at all - a mask can be drawn on a
        train.
        """
        # Not gated on `busy`. This only queues work on the one worker
        # thread, which runs it in turn, and it releases nothing - so a
        # scan asked for while a transfer is running waits its turn
        # rather than being dropped.
        if state.get("cannot") is None:
            return
        w.submit("msk_live", lambda k: k.msk_live())

    def msk_say_live():
        """The line above the segment list: which mask is in there.

        Only this program's own doing can be known. The instrument holds
        eight lists of points and no name, so a mask somebody loaded
        from the front panel is a mask this cannot name - and saying so
        is better than naming the last one that went through here.
        """
        held = sum(state.get("mlivecounts") or [])
        name = state.get("msent")
        if name and held:
            lbl_mlivename.config(text=name)
        elif held:
            lbl_mlivename.config(text=_("not sent from here"))
        else:
            lbl_mlivename.config(text=_("nothing loaded"))

    def msk_show_live(segments):
        """Put the instrument's eight live segments into the bottom list.

        Point counts only. The coordinates are read back with every X
        the same constant, so showing them would be showing something
        that is not what is on the screen - see INSTRUMENT-NOTES.
        """
        mlive.delete(*mlive.get_children())
        state["mlivecounts"] = list(segments or [])
        if segments is None:
            mlive.insert("", "end", iid="none",
                         values=("-", _("this instrument has no mask "
                                        "subsystem")))
            lbl_mlivename.config(text="")
            return
        for i, count in enumerate(segments, 1):
            mlive.insert("", "end", iid="seg%d" % i,
                         values=(_("Segment %d") % i,
                                 _("%d point(s)") % count if count
                                 else _("empty")))
        msk_say_live()

    def do_msk_sort(box, column):
        """Sort a list by a column, and back again if it is already."""
        rows = [(box.set(iid, column), iid) for iid in box.get_children()]
        down = state.get("msort") == (str(box), column)
        rows.sort(reverse=down)
        state["msort"] = None if down else (str(box), column)
        for at, (_value, iid) in enumerate(rows):
            box.move(iid, "", at)

    def msk_buttons():
        """What the buttons can do with what is chosen.

        Note what is offered and what is not. Copying mask files to and
        from the instrument's disk is offered, because it is a file
        transfer and nothing parses the mask on the way - the X problem
        below lives in `MASK:MASK<n>:POINTSPCNT` and this route never
        goes near it. Sending a mask as a limit template is offered for
        the same reason: it goes into a reference as an envelope.

        What is *not* offered is writing the instrument's eight live
        segments, which is the one thing that does go through
        POINTSPCNT. Every X reads back as the same constant there, and
        until somebody has looked at a screen and settled whether that
        is the mask being stored wrongly or only reported wrongly, this
        program will not claim to have put a shape on an instrument.
        See INSTRUMENT-NOTES.
        """
        joined = state.get("cannot") is not None
        # The mask subsystem is Option 2C. Without it MASK:MASK<n> is not
        # a command this firmware has, so everything that reaches for it
        # is greyed - and everything that does not, which is the drawing
        # and the library, carries on working with no instrument in the
        # room at all.
        testable = joined and bool(state.get("masks"))
        btn_msend.config(state="normal" if testable
                         and state.get("mask") is not None else "disabled")
        btn_mload.config(state="normal" if joined else "disabled")
        btn_mclear.config(state="normal" if joined else "disabled")
        btn_mgrab.config(state="normal" if joined else "disabled")
        # There has to be something to save. An empty mask is a name
        # and no points, and a file of one is no use to anybody.
        drawn = state.get("mask")
        btn_msave.config(state="normal" if drawn is not None
                         and drawn.points else "disabled")
        # And something on the graticule to make a picture of - the
        # mask, a trace behind it, or a captured screen. The same rule
        # as the Limits tab's Save image.
        btn_mview.config(state="normal" if (drawn is not None
                                            and drawn.points)
                         or state.get("mwave") is not None
                         or state.get("mshot") else "disabled")
        # The setup comes off the instrument and is written beside the
        # mask's own file, so it needs both to exist.
        btn_msetup.config(state="normal" if joined and state.get("mask")
                          is not None and (state.get("mask").origin or "")
                          else "disabled")
        # Starting a measurement is the instrument's job, so it needs an
        # instrument and something to measure against.
        btn_mmeasure.config(state="normal" if testable
                            and state.get("mask") is not None
                            else "disabled")
        btn_mdrop.config(state="normal" if (state.get("mshot")
                                            or state.get("mwave")
                                            or state.get("mhits"))
                         else "disabled")
        btn_mdel.config(state="normal" if mpc.selection() else "disabled")

    def do_msk_open(_evt=None):
        """Double-click a row: open that mask in the right pane."""
        chosen = list(mpc.selection())
        if not chosen:
            return
        path = (state.get("mpcfiles") or {}).get(chosen[0])
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
            mask = tds_msk.load(raw,
                                name=os.path.splitext(chosen[0])[0])
            state["mipattern"] = tds_msk.looks_like_msk(raw)
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                     % (_("Could not open that mask"),
                                        exc))
            return
        mask.origin = path
        state["mask"] = mask
        msk_forget()
        state["mpicks"] = []
        state.pop("mdrawing", None)
        draw_mask()
        msk_buttons()
        ui.say(_("Opened %s") % os.path.basename(path))

    def msk_click_off(evt):
        """A press below the last row clears the selection.

        What Explorer does, and what anybody expects of a list: there is
        no other way to get back to nothing selected once something is,
        short of ctrl-clicking the row again. Only when the press is on
        no row at all, so it cannot undo an ordinary click.
        """
        if not mpc.identify_row(evt.y):
            mpc.selection_remove(*mpc.selection())
            msk_buttons()

    mpc.bind("<Button-1>", msk_click_off, add="+")
    mpc.bind("<Double-Button-1>", do_msk_open)
    mpc.bind("<<TreeviewSelect>>", lambda e: msk_buttons())

    def do_msk_load():
        """Read the instrument's eight segments back into the editor.

        The other direction of the » button, and the reason the disk
        library went: what a mask is *for* is those eight segments, and
        a file on the instrument's own disk was a file the instrument
        itself could not read.
        """
        if state["busy"] or state.get("cannot") is None:
            return
        if not msk_may_discard():
            return
        ui.busy(True, "wait")
        ui.say(_("Reading the instrument's mask segments ..."))
        w.submit("msk_read", lambda k: k.msk_read())

    def do_msk_clear():
        """Empty the instrument's eight segments, having asked first."""
        if state["busy"] or state.get("cannot") is None:
            return
        if not messagebox.askyesno(
                _("Empty the instrument's segments?"),
                _("All eight of the instrument's mask segments are "
                  "emptied and it stops drawing a mask. What is on this "
                  "computer is untouched."),
                parent=root, default="no"):
            return
        ui.busy(True, "wait")
        ui.say(_("Emptying the instrument's mask segments ..."))
        w.submit("msk_clear", lambda k: k.msk_clear())

    def msk_took_live(replies):
        """Put what the segments answered into the editor as one mask."""
        if replies is None:
            messagebox.showinfo(
                _("No mask segments"),
                _("This instrument has no mask subsystem to read."))
            return
        mask = tds_msk.Mask.from_scpi(replies,
                                      name=_("From the instrument"))
        if not mask.filled():
            ui.say(_("The instrument's mask segments are empty"))
            return
        # It arrives with no name - the instrument holds points and
        # nothing else - so it is given one here and becomes a file on
        # this computer straight away. A mask read off an instrument and
        # left unsaved is a mask that goes when the next one is read.
        here = msk_folder()
        while True:
            said = simpledialog.askstring(
                _("Name this mask"),
                _("The instrument holds points and no name. What should "
                  "this mask be called on this computer?"),
                initialvalue=state.get("msent") or _("From the instrument"),
                parent=root)
            if said is None:
                ui.say(_("Nothing was loaded"))
                return
            said = said.strip()
            if not said:
                continue
            leaf = tds_msk.clean_name(said, [])
            path = os.path.join(here, leaf) if here else None
            if path and os.path.exists(path) and not messagebox.askyesno(
                    _("Replace it?"),
                    _("%s is already in the library. Replace it?") % leaf,
                    parent=root, default="no"):
                continue
            break
        mask.name = said
        state["mipattern"] = False
        try:
            with open(path, "wb") as fh:
                fh.write(mask.to_bytes())
            mask.origin = path
        except (OSError, TypeError) as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                 % (_("Could not save"), exc))
            mask.origin = None
        state["mask"] = mask
        msk_forget()
        state["mpicks"] = []
        state.pop("mdrawing", None)
        draw_mask()
        msk_buttons()
        do_msk_scan()
        ui.say(_("Loaded %(count)d segment(s) from the instrument as "
                 "%(name)s") % {"count": len(mask.filled()),
                                "name": os.path.basename(mask.origin or "")
                                or mask.name})
        msk_say_live()

    def do_msk_new():
        if not msk_may_discard():
            return
        state["mask"] = tds_msk.Mask(name=_("New mask"))
        msk_forget()
        state["mipattern"] = False
        state["mpicks"] = []
        state.pop("mdrawing", None)
        draw_mask()
        msk_buttons()
        msk_say_tool()

    def msk_legal(mask, what):
        """True if this mask may leave the editor, with a word if not.

        Drawing is deliberately not policed - somebody halfway through a
        redesign has eleven shapes and two concave ones for a minute,
        and being stopped at every click would be unusable. What is
        policed is the way out: saving it or sending it says this is a
        mask, and a mask no instrument can hold is not one.
        """
        wrong = mask.complaints()
        if not wrong:
            return True
        messagebox.showerror(
            _("Error"),
            "%s\n\n%s\n\n%s"
            % (what, "\n".join(u"\u2022 " + w for w in wrong),
               _("Fix these and try again. Nothing has been changed.")))
        return False

    def do_msk_save():
        mask = state.get("mask")
        if mask is None:
            return
        if not msk_legal(mask, _("This mask cannot be saved as it is:")):
            return
        here = msk_folder()
        if not here:
            messagebox.showerror(_("Error"),
                                 _("The masks folder could not be made."))
            return
        # The system's own save dialog, so the library folder is a
        # starting point rather than a cage, and so the format is
        # chosen by the extension the way it is everywhere else. It
        # opens on whichever format the mask came from: an i-Pattern
        # mask opened, nudged and saved should still be one that
        # i-Pattern and every other TTiP tool can read.
        as_ip = bool(state.get("mipattern"))
        # An i-Pattern file stores its points as a set and this program
        # puts them back in order by sorting them about their middle,
        # which recovers a convex shape exactly and a concave one only
        # by luck. Measured: a concave L saved as i-Pattern and read
        # back is the same six points joined up differently. So say so
        # while there is still a choice to make.
        concave = [n for n, seg in mask.filled()
                   if not tds_msk.convex_polygon(seg)]
        if as_ip and concave:
            if not messagebox.askyesno(
                    _("Save as an i-Pattern mask?"),
                    _("Segment(s) %(which)s are concave, and an i-Pattern "
                      "file keeps a mask's points without their order - "
                      "so a concave shape does not come back the same "
                      "way round.\n\nThis program's own format keeps "
                      "them exactly. Save as i-Pattern anyway?")
                    % {"which": ", ".join(str(n) for n in concave)},
                    parent=root, default="no"):
                as_ip = False
        # One entry, not two: both formats use .MSK, so a file type
        # box could not tell them apart by extension and would be a
        # choice that does nothing.
        kinds = [(_("Mask (*.MSK)"), "*.MSK"), (_("All files"), "*.*")]
        path = filedialog.asksaveasfilename(
            parent=root, title=_("Save mask as"), initialdir=here,
            initialfile=os.path.basename(mask.origin or "")
            or tds_msk.clean_name(mask.name or _("New mask"), []),
            defaultextension=tds_msk.SUFFIX, filetypes=kinds)
        if not path:
            return
        name = os.path.basename(path)
        mask.name = os.path.splitext(name)[0]
        try:
            with open(path, "wb") as fh:
                fh.write(tds_msk.save_bytes(mask, as_ip))
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                     % (_("Could not save"), exc))
            return
        mask.origin = path
        state["mdirty"] = False
        do_msk_scan()
        ui.say(_("Saved %s") % name)

    def do_msk_delete():
        chosen = [(state.get("mpcfiles") or {}).get(n)
                  for n in mpc.selection()]
        chosen = [p for p in chosen if p]
        if not chosen:
            return
        if not messagebox.askyesno(
                _("Delete mask"),
                _("Delete %d mask(s) from this computer?\n\n%s")
                % (len(chosen),
                   "\n".join(os.path.basename(p) for p in chosen)),
                default="no", parent=root):
            return
        for path in chosen:
            try:
                os.remove(path)
            except OSError as exc:
                messagebox.showerror(_("Error"), "%s\n\n%s"
                                         % (_("Could not delete"), exc))
                break
        do_msk_scan()

    def do_msk_trace(which="m"):
        """Put a trace behind the drawing, to draw against or to check.

        Any waveform this program can read: what was captured on the
        Waveforms tab, or a file from the PC in any of the formats it
        loads.

        `which` is "m" or "l" - the mask or the limits envelope. Both
        tabs put a trace behind what is drawn, and it is the same job;
        only the drawing it lands under differs. The limits tab has no
        view of its own to set, because it draws the record as the
        instrument scaled it.
        """
        held = shown_waves()
        if held and messagebox.askyesno(
                _("Use the captured trace?"),
                _("Put %s from the Waveforms tab behind the mask?\n\n"
                  "No opens a file instead.") % wave_name(held[0]),
                parent=root):
            state[which + "wave"] = held[0]
            # Whose trace this is. Learn template builds from a trace
            # that came from here rather than going to the instrument
            # for another one, and only a trace this button put there
            # counts - the one Refresh reads back is the instrument's
            # own and learning from it would be learning from itself.
            state[which + "wavefrom"] = "pc"
            if which == "m":
                state["mview"] = tds_wfm.PlotView([held[0]])
            edit_draw(which)
            ui.lim_buttons()
            return
        path = filedialog.askopenfilename(
            parent=root, title=_("Load trace"),
            filetypes=[(_("Waveform files (*.isf *.csv *.tdw *.wfm)"),
                        "*.isf *.csv *.tdw *.wfm"),
                       (_("All files"), "*.*")])
        if not path:
            return
        try:
            wave = tds_wfm.load(path)
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                     % (_("Could not load that file"),
                                        localise(str(exc))))
            return
        state[which + "wave"] = wave
        state[which + "wavefrom"] = "pc"
        if which == "m":
            state["mview"] = tds_wfm.PlotView([wave])
        edit_draw(which)
        msk_buttons()
        ui.lim_buttons()
        ui.say(_("Loaded %s behind the mask") % os.path.basename(path))

    def msk_setup_path(mask=None):
        """The setup file that goes with the open mask, if it has one."""
        mask = mask if mask is not None else state.get("mask")
        origin = getattr(mask, "origin", None) if mask is not None else None
        if not origin:
            return None
        beside = tds_set.beside(origin)
        return beside if os.path.exists(beside) else None

    def msk_unit_interval(mask=None):
        """The bit period the open mask was drawn for, in seconds.

        Written into the setup beside it as one machine-readable REM
        line, because every other line there is prose for a person and
        guessing a bit period from an eye's width means trusting the
        shape to be the width this program happens to draw.
        """
        setup = msk_setup_path(mask)
        if not setup:
            return None
        found = re.search(r':REM\s+"UI\s+([0-9.eE+-]+)"',
                          tds_set.contents(setup))
        return float(found.group(1)) if found else None

    def msk_differential(mask=None):
        """Whether this mask is drawn for a signal across a pair.

        The other machine-readable REM line. It matters because a
        differential signal reaches the instrument two different ways -
        one differential probe into one channel, or two probes and the
        difference taken in MATH1 - and the instrument has to be set up
        differently for each. Which is on the bench is not something it
        can be asked, so the user is.
        """
        setup = msk_setup_path(mask)
        return bool(setup and ':REM "DIFFERENTIAL"' in tds_set.contents(setup))

    def msk_seconds_per_div(mask=None):
        """The sweep the mask was drawn against, or None.

        Sending a mask no longer touches the instrument, so Start
        measurement is where the timebase has to come from. Without it
        the eye is built on whatever was dialled up: measured, a USB
        full speed mask against 1 us/div gave 116,921,796 hits over
        345,839 acquisitions, and no clock was found either.
        """
        setup = msk_setup_path(mask)
        if not setup:
            return None
        try:
            return float(tds_set.parse(tds_set.contents(setup)).get("secdiv"))
        except (TypeError, ValueError):
            return None

    def msk_volts_per_div(mask=None):
        """The volts a division this mask was drawn against, or None.

        Read out of the setup's own channel scale, which is the number
        the shapes were computed from. Needed only for MATH1: a math
        waveform's vertical scale cannot be set on this family - there
        is no command for it anywhere, not in the MATH group and not in
        a setup either - and a 784D gives CH1-CH2 five times the
        channel's own scale, measured. So the difference is drawn here
        at the scale the mask means rather than the one the instrument
        happens to be showing it at.
        """
        setup = msk_setup_path(mask)
        if not setup:
            return None
        try:
            return float(tds_set.parse(tds_set.contents(setup)).get("scale"))
        except (TypeError, ValueError):
            return None

    def do_msk_measure():
        """Set the instrument up to judge the mask, and start counting.

        Separate from sending, because sending is eight lists of points
        and this is the sweep, the trigger and the display. A mask can
        sit in an instrument for a while before anybody wants a verdict
        from it, and changing somebody's timebase the moment they press
        an arrow is not what an arrow means.

        For an eye mask it asks the two things it cannot find out - what
        is on the end of the probes, and whether there is a clock - and
        then does the rest by looking. For any other mask there is
        nothing to set up and it only starts the tally.
        """
        mask = state.get("mask")
        if state["busy"] or mask is None or state.get("cannot") is None:
            return
        source = mask.source or (state.get("wsources") or ["CH1"])[0]
        bit = msk_unit_interval()
        if bit is None:
            # A mask with no unit interval draws no eye - a pulse
            # template, a burst envelope - so there is no bit to centre
            # and nothing to delay. The display still has to be set:
            # this used to start the counter and leave the glass as the
            # last mask left it, which meant no DPO at all and, after
            # an eye mask, a delayed window still on.
            ui.busy(True, "wait")
            ui.say(_("Setting the instrument up to count ..."))
            w.submit("msk_count", lambda k: k.msk_watch())
            return
        msk_ask_eye(source, bit, list(mask.filled()))

    def msk_ask_eye(source, bit, segments):
        """The two questions, then the whole setup.

        Neither can be found by asking the instrument: it cannot see
        what is on the end of a probe, and an input with nothing plugged
        in reads the same as one carrying a clock that is switched off.
        Everything else is found by looking, including which input the
        clock is on - a clock is by definition the input you do not want
        on the graticule, so a list of what is displayed can never
        contain it.
        """
        dlg, pad = popup(_("Start measurement"))
        probe = tk.StringVar(value="probe")
        trig = tk.StringVar(value="clock")
        ttk.Label(pad, wraplength=460, text=_(
            "The instrument will be set up for an eye: the sweep slid "
            "so the middle of a bit lands on the mask, DPO on where it "
            "is fitted, and the hit counter started.")).pack(
                anchor="w", pady=(0, 8))
        if msk_differential():
            box = ttk.LabelFrame(pad, text=_("This signal is probed"),
                                 padding=8)
            box.pack(fill="x")
            ttk.Radiobutton(box, variable=probe, value="probe", text=_(
                "with one differential probe, into a single "
                "channel")).pack(anchor="w")
            ttk.Radiobutton(box, variable=probe, value="math", text=_(
                "with two probes on CH1 and CH2, differenced in "
                "MATH1")).pack(anchor="w")
            ttk.Label(box, wraplength=440, foreground="#555", text=_(
                "MATH1 is defined as CH1-CH2 and judged here rather "
                "than by the instrument: its mask counter takes a "
                "channel and refuses a math waveform, and DPO switches "
                "math off, so that route runs on persistence. The "
                "instrument also scales a math waveform itself - there "
                "is no command for it - so on its own graticule the "
                "difference will not line up with the mask. The "
                "picture here does.")).pack(anchor="w", pady=(4, 0))
        box = ttk.LabelFrame(pad, text=_("and triggered from"), padding=8)
        box.pack(fill="x", pady=(8, 0))
        line = ttk.Frame(box)
        line.pack(anchor="w", fill="x")
        ttk.Radiobutton(line, variable=trig, value="clock", text=_(
            "a clock at the bit rate, on")).pack(side="left")
        # Named rather than hunted for. Looking costs a second an input
        # and puts each one on the graticule while it looks, and the
        # person at the bench plugged the clock in and knows where it
        # went. "Find it" is kept for when they did not.
        where = tk.StringVar(value=state.get("mclock") or "CH2")
        inputs = [n for n in ("CH1", "CH2", "CH3", "CH4") if n != source]
        pick_clock = ttk.Combobox(line, textvariable=where, width=10,
                                  state="readonly",
                                  values=inputs + [_("find it")])
        pick_clock.pack(side="left", padx=(6, 0))
        hints(pick_clock, "Which input carries the clock, or find it")
        ttk.Radiobutton(box, variable=trig, value="data", text=_(
            "the data itself")).pack(anchor="w", pady=(4, 0))
        ttk.Label(box, wraplength=440, foreground="#555", text=_(
            "The named input is switched on just long enough to check "
            "there is a clock at the bit rate on it, and the sweep is "
            "then slid so the middle of a bit lands on the mask. "
            "Triggering on the data gives a pseudo-eye: the "
            "instrument's own time base jitter is added to the "
            "signal's, and the sweep is delayed past the trigger so "
            "that both rails reach the middle of the screen."
        )).pack(anchor="w", pady=(4, 0))

        def go():
            if state["busy"]:
                return
            dlg.destroy()
            # Remembered rather than only passed on: Capture screen is
            # a separate press later, and on the math route it has to
            # read MATH1 and leave the instrument's own tally alone,
            # since that can only be counting one leg of the pair.
            state["mmath"] = probe.get() == "math"
            # False for none, an input's name for that input, True to
            # go looking. Remembered so the next mask offers the same
            # channel rather than starting at CH2 every time.
            if trig.get() != "clock":
                asked = False
            elif where.get() in inputs:
                asked = state["mclock"] = where.get()
            else:
                asked = True
            state["eyeing"] = True
            ui.busy(True, "wait")
            ui.say(_("Setting the instrument up for an eye ..."))
            w.submit("msk_eye",
                     lambda k, s=source, b=bit, segs=segments,
                     m=state["mmath"], c=asked,
                     d=msk_seconds_per_div():
                     k.msk_eye(s, b, segs, math=m, clocked=c, secdiv=d))

        row = ttk.Frame(pad)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text=_("Start"), command=go).pack(side="right")
        ttk.Button(row, text=_("Cancel"),
                   command=dlg.destroy).pack(side="right", padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (root.winfo_rootx() + 80,
                                 root.winfo_rooty() + 80))
        state["dialog"] = dlg

    def setup_mask():
        """The drawing whose setup is being written, mask or template."""
        return state.get(state.get("setupfor") or "mask")

    def do_msk_setup(which="mask"):
        """Write the instrument's settings beside the open drawing.

        Read from the instrument rather than made up, because the point
        of the file is to record what the drawing was made against. It
        has to have been saved first: the setup goes beside it, and
        beside nothing is nowhere.

        `which` is the state key of the drawing this is for - "mask" or
        "lmask". Both tabs want the same file beside their own, so this
        is one function and one dialog, and which drawing is being
        asked about is settled when the button is pressed rather than
        by looking at which tab happens to be in front when the
        instrument answers.
        """
        state["setupfor"] = which
        mask = setup_mask()
        if mask is None or state["busy"]:
            return
        if not mask.origin:
            messagebox.showinfo(
                _("Save it first"),
                _("A setup is written beside the drawing's own file, "
                  "under the same name. Save it, then save its setup."))
            return
        ui.busy(True, "wait")
        ui.say(_("Reading the instrument's settings ..."))
        w.submit("set_read", lambda k, s=mask.source or "CH1":
                 k.set_read(s))

    # What the setup dialog offers, in the order it offers it. Only the
    # fields somebody drawing a mask has a reason to change: the rest of
    # what the instrument said is written as it was read.
    SETUP_FIELDS = (
        ("secdiv", "Seconds a division"),
        ("scale", "Volts a division"),
        ("position", "Vertical position, divisions"),
        ("offset", "Vertical offset, volts"),
        ("length", "Record length, points"),
        ("tposition", "Trigger position, percent"),
        ("tlevel", "Trigger level, volts"),
        ("coupling", "Coupling"),
        ("impedance", "Input impedance"),
        ("probe", "Probe attenuation"),
    )

    def msk_setup_edit(fields):
        """Show what the instrument said, and let it be changed.

        The instrument is the starting point rather than the answer. A
        mask is often drawn for a signal nobody has on the bench yet -
        a specification, not a measurement - and the setup beside it
        has to say what the mask means, not what happened to be on the
        screen when it was written.

        The unit interval and the differential flag are here and
        nowhere else: no instrument setting records either, and the eye
        setup cannot be done without the first of them.
        """
        mask = setup_mask()
        if mask is None or not mask.origin:
            return
        # What is already beside the mask wins over what the instrument
        # says, for these two: somebody who has typed a unit interval
        # once should not have to type it again to save a setup. Read
        # through the same two helpers the eye setup reads them with,
        # because they live in REM lines and tds_set.parse steps over
        # those - they are notes to this program, not commands.
        was_ui = msk_unit_interval(mask)
        was_diff = msk_differential(mask)
        dlg, pad = popup(_("Save setup"))
        ttk.Label(pad, wraplength=420, text=_(
            "Read from the instrument. Change anything the mask means "
            "rather than what happened to be set, then save it beside "
            "%s.") % (mask.name or _("this mask"))).pack(anchor="w",
                                                         pady=(0, 8))
        rows = ttk.Frame(pad)
        rows.pack(fill="x")
        boxes = {}
        for at, (key, english) in enumerate(SETUP_FIELDS):
            ttk.Label(rows, text=_(english)).grid(row=at, column=0,
                                                   sticky="w", pady=1)
            box = ttk.Entry(rows, width=16)
            box.insert(0, str(fields.get(key, "")))
            box.grid(row=at, column=1, sticky="w", padx=8, pady=1)
            boxes[key] = box
        at = len(SETUP_FIELDS)
        ttk.Label(rows, text=_("Unit interval, seconds")).grid(
            row=at, column=0, sticky="w", pady=(8, 1))
        ui = ttk.Entry(rows, width=16)
        ui.insert(0, "" if was_ui is None else "%g" % was_ui)
        ui.grid(row=at, column=1, sticky="w", padx=8, pady=(8, 1))
        hints(ui, "The bit period in seconds, for the eye diagram")
        diff = tk.BooleanVar(value=bool(was_diff))
        ttk.Checkbutton(rows, variable=diff,
                        text=_("The signal is differential")).grid(
            row=at + 1, column=0, columnspan=2, sticky="w", pady=1)
        note = ttk.Label(pad, wraplength=420, foreground="#555", text=_(
            "The unit interval is the bit period the mask is drawn for. "
            "An eye mask needs it; any other mask can leave it empty."))
        note.pack(fill="x", pady=(8, 0))

        def go():
            out = dict(fields)
            for key, box in boxes.items():
                said = box.get().strip()
                if said:
                    out[key] = said
                else:
                    out.pop(key, None)
            bit = ui.get().strip()
            if bit:
                try:
                    out["ui"] = float(bit)
                except ValueError:
                    messagebox.showinfo(
                        _("That is not a number"),
                        _("The unit interval is a time in seconds, for "
                          "example 8.0e-9 for 125 Mbit/s."), parent=dlg)
                    return
            out["differential"] = bool(diff.get())
            dlg.destroy()
            msk_setup_write(out)

        row = ttk.Frame(pad)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text=_("Save"), command=go).pack(side="right")
        ttk.Button(row, text=_("Cancel"),
                   command=dlg.destroy).pack(side="right", padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (root.winfo_rootx() + 100,
                                 root.winfo_rooty() + 60))
        state["dialog"] = dlg

    def msk_setup_write(fields):
        """Put what the instrument said into a file beside the drawing."""
        mask = setup_mask()
        if mask is None or not mask.origin:
            return
        path = tds_set.beside(mask.origin)
        if os.path.exists(path) and not messagebox.askyesno(
                _("Replace the setup?"),
                _("%s is already there. Replace it with what the "
                  "instrument is set to now?") % os.path.basename(path),
                parent=root, default="no"):
            ui.say(_("The setup was left as it was"))
            return
        try:
            # Latin-1 with replacement, which is what tds_set.contents
            # reads these back as. Left to the platform default this
            # wrote cp1252 on Windows and was read as latin-1, so the
            # two disagreed over 0x80-0x9F - the range a curly quote or
            # an em dash in a mask's name lands in - and a name outside
            # the code page raised UnicodeEncodeError past the OSError
            # below and took the button down with it.
            with open(path, "w", encoding="latin-1", errors="replace") as fh:
                fh.write(tds_set.text(
                    fields, os.path.splitext(os.path.basename(path))[0],
                    said=(state.get("idn") or "").strip(),
                    when=time.strftime("%Y-%m-%d")))
        except OSError as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                 % (_("Could not save"), exc))
            return
        ui.say(_("Saved %(name)s - %(what)s")
               % {"name": os.path.basename(path),
                  "what": tds_set.summary(fields)})
        # The library the file landed in, and the line under that tab's
        # own canvas. A setup beside a template is listed by the limits
        # library, not by the masks one.
        if state.get("setupfor") == "lmask":
            ui.do_lim_scan()
            ui.say_limits()
        else:
            do_msk_scan()
            say_mask()

    def msk_setup_landed(payload):
        """What the instrument did with a setup, against what was asked.

        Three kinds of answer, needing three different things said. A
        timebase or a gain it would not give can be answered by moving
        the mask, because a mask is in percent of the graticule and
        nothing else. Volts per division it will not reach is usually
        the front end rather than the firmware, and a bigger probe
        lifts it - the ceiling is the input's own times the probe. Any
        other value it replaced is the instrument saying no, and
        nothing on this side makes it yes.
        """
        if payload.get("refused"):
            ui.say(_("Setup sent, but the instrument refused "
                     "%(count)d of it: %(what)s")
                   % {"count": len(payload["refused"]),
                      "what": "; ".join(payload["refused"])})
        else:
            ui.say(_("Setup sent - %d line(s)") % payload["sent"])
        asked = state.pop("msetup", None) or {}
        got = payload.get("got") or {}
        moved = tds_set.differences(asked, got)
        mask = state.get("mask")
        if not moved or mask is None:
            return
        source = asked.get("source", "CH1")

        def worth(key, value):
            """A value in the units it is in, or as it came."""
            number = tds_set.number({key: value}, key)
            if number is None:
                return str(value)
            if key == "secdiv":
                return "%s/div" % tds_wfm.eng(number, "s")
            if key == "scale":
                return "%s/div" % tds_wfm.eng(number, "V")
            return str(value)

        told = "\n".join(
            u"• %s: %s → %s" % (tds_set.command(key, source),
                                worth(key, was), worth(key, now))
            for key, was, now in moved)
        # The two axes are measured from different places: across, the
        # trigger point, which is what stays put when the timebase
        # changes; up, the channel's zero volts, which is what stays
        # put when the gain does.
        across, up, rest = tds_set.stretch(moved)
        at_x = tds_set.number(got, "tposition")
        if at_x is None:
            at_x = tds_set.number(asked, "tposition")
        sits = tds_set.number(got, "position")
        if sits is None:
            sits = tds_set.number(asked, "position") or 0.0
        about = (50.0 if at_x is None else at_x,
                 50.0 + sits * 100.0 / tds_wfm.DIVS_Y)
        probe = tds_set.probe_wanted(asked, got)
        if probe:
            told += "\n\n" + (
                _("A %(probe)gX probe on %(channel)s would reach "
                  "%(volts)s.")
                % {"probe": probe, "channel": source,
                   "volts": worth("scale", asked.get("scale"))})
        if rest and across == 1.0 and up == 1.0:
            messagebox.showinfo(
                _("The instrument changed the setup"), "%s\n\n%s\n\n%s"
                % (_("It took the setup, then quietly replaced these:"),
                   told, _("Nothing here can be answered by moving the "
                           "mask.")))
            return
        fitted = tds_msk.stretched(mask.segments, across, up, about)
        if not tds_msk.on_graticule(fitted):
            messagebox.showinfo(
                _("The instrument changed the setup"), "%s\n\n%s\n\n%s"
                % (_("It took the setup, then quietly replaced these:"),
                   told, _("Stretched to match, this mask would run off "
                           "the graticule, so it is left as it is.")))
            return
        if not messagebox.askyesno(
                _("The instrument changed the setup"), "%s\n\n%s\n\n%s"
                % (_("It took the setup, then quietly replaced these:"),
                   told,
                   _("A mask is held in percent of the graticule, so it "
                     "can be stretched to mean what it meant before. "
                     "Stretch this one to match? Undo puts it back.")),
                parent=root, default="yes"):
            return
        msk_remember()
        mask.segments[:] = [[(round(x, 2), round(y, 2)) for x, y in seg]
                            for seg in fitted]
        state["mpicks"] = []
        draw_mask()
        say_mask()
        ui.say(_("Mask stretched to what the instrument gave - send it again"))

    def do_msk_send():
        """Send the open mask to the instrument, whichever way suits it.

        As a real mask where the instrument has Option 2C, and as a
        limit template where it has not - or whichever the user has
        chosen on the tab. See INSTRUMENT-NOTES for both routes.
        """
        mask = state.get("mask")
        if mask is None or state["busy"]:
            return
        if not mask.filled():
            messagebox.showinfo(_("Nothing to send"),
                                _("This mask has no segments with points "
                                  "in them yet."))
            return
        if not msk_legal(mask, _("This mask cannot be sent as it is:")):
            return
        # The instrument takes these points and keeps them - a readback
        # gives them back unchanged - but draws some shapes its own way.
        # Better said now than found on the graticule afterwards.
        # The setup beside it, if there is one. Offered rather than
        # sent: it changes the timebase and the channel, which is the
        # whole point of it, and doing that unasked to an instrument
        # somebody is watching would be rude.
        setup = msk_setup_path(mask)
        # Asked now, sent with the mask. Sent here, a No to "Send anyway?"
        # or Cancel in the dialog below left the instrument's timebase and
        # channel changed for a mask that never went.
        queued = []
        if setup and messagebox.askyesno(
                _("Send its setup too?"),
                "%s\n\n%s\n\n%s"
                % (_("%s has a setup beside it:")
                   % (mask.name or _("This mask")),
                   tds_set.summary(tds_set.parse(tds_set.contents(setup)))
                   or os.path.basename(setup),
                   _("Send that to the instrument first? It changes the "
                     "channel and the timebase to what the mask was "
                     "drawn against.")),
                parent=root, default="yes"):
            asked = tds_set.parse(tds_set.contents(setup))
            lines = [ln.lstrip(":")
                     for ln in tds_set.contents(setup).splitlines()
                     if ln.strip() and not ln.strip().lstrip(":").upper()
                     .startswith("REM")]
            queued.append((asked, lines))
        odd = mask.redrawn()
        if odd and not messagebox.askyesno(
                _("Send anyway?"),
                "%s\n\n%s\n\n%s"
                % (_("The instrument will not draw this mask the way it "
                     "is drawn here:"),
                   "\n".join(u"• segment %d: %s" % (n, why)
                             for n, why in odd),
                   _("It stores the points exactly, and joins them up "
                     "its own way. Cutting the shape into simpler "
                     "pieces avoids it. Send anyway?")),
                parent=root, default="no"):
            return
        live = [s for s in (state.get("wsources") or [])
                if not s.startswith("REF")]
        dlg, pad = popup(_("Send as a mask"))
        ttk.Label(pad, wraplength=460, text=_(
            "%s goes into the instrument's eight mask segments and is "
            "drawn on its graticule. This instrument has the mask "
            "option fitted.")
            % (mask.name or _("This mask"))).pack(anchor="w", pady=(0, 8))
        against = tk.StringVar(value=live[0] if live else "CH1")
        rows = ttk.Frame(pad)
        rows.pack(fill="x")
        ttk.Label(rows, text=_("Signal")).grid(row=1, column=0, sticky="w",
                                             pady=(6, 0))
        ttk.Combobox(rows, textvariable=against, width=8, state="readonly",
                     values=live or ["CH1"]).grid(row=1, column=1,
                                                  sticky="w", padx=6,
                                                  pady=(6, 0))
        # Sending is only the eight segments. Setting the instrument up
        # to show an eye against them is a separate press, because it
        # changes the sweep, the trigger and the display - and doing
        # that to somebody's instrument on the way past, unasked, is
        # how it used to work and was wrong.
        if msk_unit_interval(mask):
            ttk.Label(pad, wraplength=460, foreground="#555", text=_(
                "This is an eye mask. Sending it puts the shapes in "
                "and leaves the rest of the instrument alone - press "
                "Start measurement to set the sweep, the trigger and "
                "the display up for an eye and begin counting.")).pack(
                    fill="x", pady=(10, 0))
        ttk.Label(pad, wraplength=460, foreground="#555", text=_(
            "Whatever mask the instrument is holding now is replaced, "
            "all eight segments of it.")).pack(fill="x", pady=(10, 0))

        def go():
            if state["busy"]:
                return
            mask.source = against.get()
            dlg.destroy()
            for asked, lines in queued:
                # Kept so the answer can be compared with the question
                # when the instrument has finished with it.
                state["msetup"] = asked
                ui.say(_("Sending the setup ..."))
                w.submit("set_send", lambda k, ln=lines,
                         s=asked.get("source", "CH1"): k.set_send(ln, s))
            state["msent"] = mask.name or _("untitled")
            ui.busy(True, "wait")
            ui.say(_("Sending %(name)s to the instrument's mask "
                     "segments ...") % {"name": mask.name or _("mask")})
            w.submit("msk_send", lambda k, ln=mask.to_scpi():
                     k.msk_send(ln))

        row = ttk.Frame(pad)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text=_("Send"), command=go).pack(side="right")
        ttk.Button(row, text=_("Cancel"),
                   command=dlg.destroy).pack(side="right", padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (root.winfo_rootx() + 80,
                                 root.winfo_rooty() + 80))
        state["dialog"] = dlg

    # after(0) rather than a call here: this runs while the window is
    # still being built, and the status bar it writes to does not exist
    # yet.
    root.after(0, do_msk_scan)


    # The pump's handlers for this tab's jobs - see pump_jobs.
    def pump_msk_behind(payload):
        ui.busy(False)
        # Whichever tab asked for it - see do_msk_behind.
        at = state.pop("behindfor", "m")
        state.pop(at + "shotimage", None)
        # What the instrument counted while the picture was
        # being taken, which is the verdict in DPO and the
        # better one anywhere. See msk_verdict. A limit test
        # has no hit counter, so this is the mask's alone.
        if at == "m":
            state["mhits"] = payload.get("hits")
        if payload.get("shot"):
            state[at + "shot"] = payload["shot"]
            state[at + "wave"] = None
            edit_draw(at)
            ui.say((_("The instrument is in DPO - its screen is "
                      "behind the mask instead of a trace")
                    if at == "m" else
                    _("The instrument is in DPO - its screen is "
                      "behind the envelope instead of a trace"))
                   + msk_say_hits())
        else:
            state.pop(at + "shot", None)
            state[at + "wave"] = payload.get("wave")
            if at == "m":
                state["mview"] = msk_view_for(state["mwave"])
            edit_draw(at)
            held = state.get(at + "wave")
            ui.say((((_("%s is behind the mask") if at == "m"
                      else _("%s is behind the envelope"))
                     % (held.label or held.source)) if held
                    else _("Nothing to capture"))
                   + msk_say_hits())
        # Clear screenshot is offered on whether there is
        # anything behind the mask, so the capture that puts
        # something there has to say the toolbar is stale.
        # Without this it stayed greyed with a screenshot on
        # the canvas until some unrelated click redrew it.
        msk_buttons()
        ui.lim_buttons()

    def pump_msk_live(payload):
        msk_show_live(payload["segments"])

    def pump_set_read(payload):
        ui.busy(False)
        msk_setup_edit(payload["fields"])

    def pump_set_send(payload):
        msk_setup_landed(payload)

    def pump_msk_eye(payload):
        ui.busy(False)
        state.pop("eyeing", None)
        where, late = payload["where"], payload.get("delayed")
        bits = []
        if payload.get("math"):
            bits.append("MATH1 = CH1-CH2")   # not words
        if late:
            # A clock that was asked for and not found is
            # said out loud. The delayed sweep is a good
            # answer to having no clock and a poor one to
            # having a clock nobody could see.
            if payload.get("adrift"):
                # A clock the data is not locked to is the
                # third case, and the one worth naming: it
                # was found, it was used, and the eye it
                # gave walked across the mask.
                bits.append(
                    _("%(clock)s is not locked to the data, "
                      "so it was left alone and the sweep "
                      "delayed %(bits)d bit(s) past the "
                      "trigger")
                    % {"clock": payload["adrift"],
                       "bits": Worker.DELAY_BITS})
            elif payload.get("nameless"):
                # They said which input the clock was on and
                # there was no clock at the bit rate on it.
                # Named, because "no clock found" reads as
                # this program's failure when it is a probe
                # on the wrong channel.
                bits.append(
                    _("nothing at the bit rate on %(clock)s, "
                      "so the data was triggered on and the "
                      "sweep delayed %(bits)d bit(s) past it")
                    % {"clock": payload["nameless"],
                       "bits": Worker.DELAY_BITS})
            else:
                bits.append(
                    _("no clock found, so delayed %(bits)d "
                      "bit(s) past the trigger")
                    if payload.get("wanted") else
                    _("triggered on the data, delayed "
                      "%(bits)d bit(s) past it"))
                bits[-1] = bits[-1] % {"bits": Worker.DELAY_BITS}
            bits.append(_("eye centred") if late["centred"]
                        else _("not centred - no crossings "
                               "to measure"))
        else:
            bits.append(_("triggered on %(clock)s")
                        % {"clock": payload["clock"]})
            bits.append((_("bit centred at %(where).1f%%")
                         % {"where": where})
                        if where is not None else
                        _("not centred - no crossings to "
                          "measure"))
        bits.append(_("DPO") if payload["display"] == "INSTAVU"
                    else _("persistence, no DPO with math")
                    if payload.get("math") else
                    _("persistence, no DPO fitted"))
        if payload["counting"]:
            bits.append(_("counting hits"))
        elif payload.get("math"):
            bits.append(_("judged here, not by the "
                          "instrument's counter"))
        ui.say(_("Set up for an eye: %(what)s")
               % {"what": ", ".join(bits)})
        # And the same thing held in English and in full. A
        # bench report of "it does not work" is unanswerable
        # without knowing which of the three routes was
        # taken and what the instrument was asked for. The
        # status line says it and the next press replaces
        # it; this keeps the last one for a fault to carry.
        log_context("mask eye", "on %s: %s"
                    % (payload.get("source") or "?",
                       ", ".join("%s=%s" % (k, payload[k])
                                 for k in sorted(payload))))
        # A sweep that cannot hold an eye is worth saying
        # out loud rather than leaving somebody to read a
        # verdict off it: at 1 us/div a USB full speed mask
        # counted 116,921,796 hits over 345,839
        # acquisitions, all of them meaningless.
        held = payload.get("bits")
        if held is not None and not 0.4 <= held <= 6.0:
            messagebox.showwarning(
                _("The sweep is wrong for this mask"),
                _("The graticule is holding %(bits).3g bit(s), "
                  "and an eye needs about one. Send the setup "
                  "beside this mask - it names the timebase "
                  "the shapes were drawn against - and start "
                  "the measurement again.")
                % {"bits": held})

    def pump_msk_count(payload):
        ui.busy(False)
        state.pop("mhits", None)
        draw_mask()
        ui.say(_("Counting hits from now, in %(display)s")
               % {"display": _("DPO")
                  if payload["display"] == "INSTAVU"
                  else _("persistence, no DPO fitted")}
               if payload["counting"] else
               _("This instrument has no mask counter to start"))
        log_context("mask count", "no eye: %s" % (payload,))

    def pump_msk_send(payload):
        ui.busy(False)
        want, got = payload["wanted"], payload["got"]
        wrong = [n for n in sorted(want)
                 if got.get(n, 0) != want[n]]
        if not got:
            ui.say(_("Sent, but this instrument reports no mask "
                     "subsystem to read back"))
        elif wrong:
            # Not a silent success: the instrument kept
            # something other than what was sent.
            ui.say(_("Sent, but segment(s) %(which)s came back "
                     "holding %(got)s point(s) rather than "
                     "%(want)s")
                   % {"which": ", ".join(str(n) for n in wrong),
                      "got": ", ".join(str(got.get(n, 0))
                                       for n in wrong),
                      "want": ", ".join(str(want[n])
                                        for n in wrong)})
        else:
            ui.say(_("The instrument is showing the mask - "
                     "%d segment(s), read back and verified")
                   % len([n for n in want if want[n]]))
        msk_scan_scope()

    def pump_msk_clear(payload):
        ui.busy(False)
        state.pop("msent", None)
        msk_show_live(payload["segments"])
        left = sum(payload["segments"] or [])
        ui.say(_("The instrument's mask segments are empty")
               if not left else
               _("Emptied, but %d point(s) are still there")
               % left)

    def pump_msk_read(payload):
        ui.busy(False)
        msk_took_live(payload["replies"])

    vars(ui).update((k, v) for k, v in locals().items() if k != "ui")
