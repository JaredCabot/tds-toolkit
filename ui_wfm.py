"""The Waveforms tab: reading, drawing, decoding and sending waveforms.

Built by run_gui through build(ui). `ui` carries the window's shared
names - state, the worker, busy, say and the rest - and is handed back
everything built here, so run_gui and the tests can reach it.
"""
import copy
import os

import tds_decode
import tds_wfm
from i18n import gettext as _
from tdstoolkit import (APPDIR, LEFT_PANE, Worker, localise, log_context,
                        log_note, resource, stamped)


def build(ui):
    EDGE = ui.EDGE
    filedialog = ui.filedialog
    hints = ui.hints
    messagebox = ui.messagebox
    named = ui.named
    popup = ui.popup
    relabel = ui.relabel
    root = ui.root
    says = ui.says
    state = ui.state
    tabs = ui.tabs
    tk = ui.tk
    tkfont = ui.tkfont
    ttk = ui.ttk
    w = ui.w

    # ------------------------------------------------- the waveform tab
    # A trace is not a file: it comes from CURVE?, not from the disk, and
    # every TDS in the family can do it - including the ones with no
    # filesystem at all. So this tab works even when the other is dead.
    wavetab = ttk.Frame(tabs)
    tabs.add(wavetab, text=_("Waveforms"))
    named(wavetab, "Waveforms")

    # Eight buttons is more than one row holds in a wordy language:
    # measured, the same set is 1029 pixels in English and 1347 in
    # French. Rather than shortening the labels until they fit the
    # narrowest window anybody might use, the row wraps - which also
    # covers a window dragged narrow, and a language nobody has added
    # yet.
    wtop = ttk.Frame(wavetab)
    wtop.pack(fill="x", padx=2, pady=(6, 2))
    wrow1 = ttk.Frame(wtop)
    wrow1.pack(fill="x")
    wrow2 = ttk.Frame(wtop)

    btn_wget = ttk.Button(wtop, text=_("Get waveform"), padding=(10, 2),
                          command=lambda: do_wfm_get())
    btn_wsave = ttk.Button(wtop, text=_("Save waveform"), padding=(10, 2),
                           command=lambda: do_wfm_save())
    btn_wload = ttk.Button(wtop, text=_("Load waveform..."),
                           padding=(10, 2), command=lambda: do_wfm_load())
    btn_wsend = ttk.Button(wtop, text=_("Send to instrument..."),
                           padding=(10, 2), command=lambda: do_wfm_send())
    btn_wdel = ttk.Button(wtop, text=_("Delete waveform"), padding=(10, 2),
                          command=lambda: do_wfm_delete())
    # At the far right of the top bar, the way Masks and Limits carry
    # their Refresh - it asks the instrument which sources it has, which
    # is a top-bar action, not one of the lists' own.
    btn_wscan = ttk.Button(wtop, text=_("Refresh"), padding=(10, 2),
                           command=lambda: do_wfm_sources())
    says(btn_wscan, "Refresh")
    WAVE_LEFT = (btn_wget, btn_wsave, btn_wload, btn_wsend, btn_wdel)
    WAVE_RIGHT = (btn_wscan,)

    def flowing(host, row1, row2, left, right, key, gap=4):
        """A toolbar that wraps to a second row when it runs out of room.

        Measured rather than split by hand: the same ten buttons fit
        across 1280 pixels in English and do not in French, and a split
        chosen once is wrong in some language or at some window size.

        Only repacked when the split actually changes: packing raises
        another <Configure>, and doing it unconditionally is a loop that
        never settles.
        """
        def lay(_evt=None):
            room = host.winfo_width()
            if room <= 1:
                return
            spare = room - sum(b.winfo_reqwidth() + gap for b in right)
            used, wrapped, split = 0, False, []
            for button in left:
                need = button.winfo_reqwidth() + gap
                if used + need > spare and not wrapped and used:
                    wrapped, used = True, 0
                if wrapped:
                    split.append(button)
                used += need
            if split == state.get(key):
                return
            state[key] = list(split)
            for button in right:
                button.pack_forget()
                button.pack(in_=row1, side="right", padx=(gap, 0))
            for button in left:
                button.pack_forget()
                button.pack(in_=row2 if button in split else row1,
                            side="left", padx=(0, gap))
            if split:
                row2.pack(fill="x", pady=(4, 0))
            else:
                row2.pack_forget()
        # All on the first row to begin with, so the bar asks for a
        # row's height before it has been measured. Left empty, a bar
        # that the window was short of room for came out zero pixels
        # tall, was never told its size, and never laid itself out: the
        # Masks tab's row of boxes vanished in Chinese.
        for button in right:
            button.pack(in_=row1, side="right", padx=(gap, 0))
        for button in left:
            button.pack(in_=row1, side="left", padx=(0, gap))
        return lay

    flow_buttons = flowing(wtop, wrow1, wrow2, WAVE_LEFT, WAVE_RIGHT,
                           "wflow")
    wtop.bind("<Configure>", flow_buttons)

    # A plain frame, not a paned window: the left column is a fixed width
    # (LEFT_PANE, the same as the Limits and Masks tabs) and does not resize,
    # so all three tabs match and the waveform window is not squeezed by an
    # accidental drag. The waveform window is resized against the decode
    # section below it instead.
    wpanes = ttk.Frame(wavetab)
    wpanes.pack(fill="both", expand=True, padx=2, pady=4)

    # Two lists, because they are two different things and the
    # difference decides what you get. A channel is acquired: reading it
    # takes a snapshot of what the instrument is looking at this second,
    # and reading it again gives something else. A reference is stored:
    # reading it gives the same samples every time until somebody
    # replaces them. One list of eleven names hid that distinction
    # completely.
    wleftf = ttk.Frame(wpanes, width=LEFT_PANE)
    # The one width for all three left panes - see LEFT_PANE. The frame
    # is told not to shrink to its lists, or its own request would win
    # and the three would go back to disagreeing.
    wleftf.pack_propagate(False)
    # Refresh now lives at the far right of the top button bar (see
    # WAVE_RIGHT), the way Masks and Limits carry theirs, which frees the
    # foot of this column for the three lists.
    wsplit = ttk.PanedWindow(wleftf, orient="vertical")
    wsplit.pack(fill="both", expand=True)

    # A button beside each name, which is the whole of the selection:
    # filled means that source is chosen, and a chosen source is drawn
    # on the graticule as soon as there are samples for it. There used
    # to be a row highlight as well, and two ways of saying "this one"
    # disagreed with each other constantly - picking a button left the
    # highlight on the row before it, and the buttons at the top acted
    # on whichever of the two the code happened to ask. So the highlight
    # is gone and these are all there is.
    SHOWN, HIDDEN = "\u25c9", "\u25cb"

    # The width a vertical scrollbar takes in this theme, measured once from
    # a throwaway. The Live and Stored lists carry no scrollbar of their own
    # but reserve this same gutter on the right, so their white area is the
    # width the Protocol decode list has left after its scrollbar, and the
    # three line up.
    _probe = ttk.Scrollbar(root, orient="vertical")
    _probe.update_idletasks()
    scroll_w = _probe.winfo_reqwidth()
    scroll_w = scroll_w if scroll_w > 1 else 17
    _probe.destroy()

    def source_list(parent, rows):
        # selectmode="none" rather than a highlight nobody reads: with
        # the buttons carrying the selection, a second highlighted row
        # is a second answer to the same question.
        #
        # No scrollbar of its own - six or four rows never overflow - but an
        # empty gutter the width of one on the right, so the white tree area
        # matches the Protocol decode list below (which does carry one) and
        # the three line up rather than this one running a scrollbar's width
        # wider.
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        box = ttk.Treeview(frame, columns=("name",), show="tree",
                           selectmode="none", height=rows)
        # The first column carries the tree's own indent as well as the
        # button, so it needs more room than the glyph alone suggests.
        box.column("#0", width=34, minwidth=34, stretch=False,
                   anchor="center")
        box.column("name", width=150, anchor="w")
        box.tag_configure("off", foreground="#9a9a9a")
        box.bind("<Double-Button-1>",
                 lambda e, b=box: do_wfm_toggle(b.identify_row(e.y)))
        box.bind("<Button-1>", lambda e, b=box: on_click_row(b, e))
        gutter = ttk.Frame(frame, width=scroll_w)
        box.pack(side="left", fill="both", expand=True)
        gutter.pack(side="right", fill="y")
        return box

    def fit_notes(evt):
        """Wrap the list notes to the pane, not to a guess.

        A label clips rather than wraps when it runs out of room, so a
        fixed wraplength is either too wide for a narrow pane or wastes
        half of a wide one. The splitter can be dragged, so it is
        measured whenever it moves.
        """
        room = max(80, evt.width - 16)
        lbl_wlive.config(wraplength=room)
        lbl_wrnote.config(wraplength=room)
        lbl_wdecnote.config(wraplength=room)


    wlivef = ttk.Frame(wsplit)
    lbl_wsrc = ttk.Label(wlivef, text=_("Live channels"))
    says(lbl_wsrc, "Live channels")
    lbl_wsrc.pack(anchor="w")
    lbl_wlive = ttk.Label(wlivef, foreground="#555", wraplength=190,
                          justify="left")
    # Broken by hand, a line to a fact. fit_notes wraps whatever is left
    # over to the pane, so these three stay three however narrow it gets.
    says(lbl_wlive, "A snapshot of what the instrument is currently "
                    "acquiring.\n"
                    "A greyed name is switched off on the instrument.\n"
                    "Double click the name to turn a channel on and off.")
    lbl_wlive.pack(anchor="w", pady=(0, 2))
    # Several at once, so more than one trace can be put on the
    # graticule together, the way they sit on the instrument's screen.
    wsrc = source_list(wlivef, 6)
    wsplit.add(wlivef, weight=1)

    wreff = ttk.Frame(wsplit)
    lbl_wref = ttk.Label(wreff, text=_("Stored waveforms"))
    says(lbl_wref, "Stored waveforms")
    lbl_wref.pack(anchor="w", pady=(6, 0))
    lbl_wrnote = ttk.Label(wreff, foreground="#555", wraplength=190,
                           justify="left")
    says(lbl_wrnote, "Held in the instrument's memory.\n"
                     "Pick one or more, then load a file into them.")
    lbl_wrnote.pack(anchor="w", pady=(0, 2))
    # Exactly the four the instrument has (REF1-REF4), and no taller: the
    # references never grow past four, so a fixed height leaves the room
    # below for the protocol-decode list rather than spending it on empty
    # rows. weight=0 keeps this pane at its natural size when the window
    # grows; the decode pane below takes the slack.
    wref = source_list(wreff, len(tds_wfm.REFS))
    wsplit.add(wreff, weight=0)

    # ---- protocol decode: the freed space underneath
    # A third list, autopopulated from the decoders folder the same way
    # the masks library is - drop a <name>.py in and it appears here. The
    # controls that apply a decode live under the plot on the right, where
    # there is room for a source per wire and the protocol's own settings.
    wdecf = ttk.Frame(wsplit)
    lbl_wdec = ttk.Label(wdecf, text=_("Protocol decode"))
    says(lbl_wdec, "Protocol decode")
    lbl_wdec.pack(anchor="w", pady=(6, 0))
    lbl_wdecnote = ttk.Label(wdecf, foreground="#555", wraplength=190,
                             justify="left")
    says(lbl_wdecnote, "Decode a captured waveform - no instrument needed.\n"
                       "Pick a protocol, then set it up on the right.")
    lbl_wdecnote.pack(anchor="w", pady=(0, 2))
    wdeclist = ttk.Frame(wdecf)
    wdeclist.pack(fill="both", expand=True)
    dec = ttk.Treeview(wdeclist, columns=("wires",), show="tree headings",
                       selectmode="browse", height=4)
    dec.heading("#0", text=_("Protocol"))
    dec.heading("wires", text=_("Wires"))
    # Widths are the user's to drag - a long protocol name or a wide
    # wire list wants more, and the heading border is how ttk lets them
    # say so.
    dec.column("#0", width=120, minwidth=70, anchor="w")
    dec.column("wires", width=64, minwidth=44, anchor="w")
    decbar = ttk.Scrollbar(wdeclist, orient="vertical", command=dec.yview)
    dec.configure(yscrollcommand=decbar.set)
    dec.pack(side="left", fill="both", expand=True)
    decbar.pack(side="right", fill="y")
    wsplit.add(wdecf, weight=1)

    # Bound after both notes exist: a <Configure> can arrive during
    # construction, and a callback that names a widget not yet made
    # raises from inside Tk's own event loop.
    wleftf.bind("<Configure>", fit_notes)
    wleftf.pack(side="left", fill="y", padx=(0, 8))
    # Lambdas so the handlers (defined further down this builder) are
    # resolved when they fire, not now; the scan is deferred to the event
    # loop for the same reason and so the folder read is off the build path.
    dec.bind("<<TreeviewSelect>>", lambda e: dec_build_controls())
    relabel.append(lambda: dec_build_controls())
    relabel.append(lambda: (dec.heading("#0", text=_("Protocol")),
                            dec.heading("wires", text=_("Wires"))))
    root.after(0, lambda: do_dec_scan())

    wrightf = ttk.Frame(wpanes)
    # The plot and the decode panel share the right side through a
    # vertical splitter, so the results table can be dragged taller when
    # there is a lot to read and shrunk out of the way when there is not.
    # No divider line and no handle: the sash is painted the panel's own
    # background so it vanishes, and you resize by dragging the bottom edge
    # of the waveform window itself - the cursor changes over the sash strip
    # there, so the whole edge drags. A raised knob was tried and looked like
    # a stray mark rather than part of the design.
    _panebg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
    # Shared look for the two decode-area splitters: a thin sash, no relief,
    # and no handle knob - painted the panel background (below) so the sash is
    # invisible and is dragged by the edge alone.
    _SASH = dict(sashwidth=7, sashrelief="flat", showhandle=False,
                 borderwidth=0)
    wrightsplit = tk.PanedWindow(wrightf, orient="vertical",
                                 background=_panebg, **_SASH)
    wrightsplit.pack(fill="both", expand=True)
    wplotarea = ttk.Frame(wrightsplit)
    # The zoom tools in a column down the left, spanning the full height -
    # the same shape the Masks and Limits tabs use. A spacer holds them
    # level with the plot rather than with the record strip above it. The
    # strip, the plot and the read-out sit in their own column to the right,
    # so the strip is only as wide as the plot - not stretched across the
    # tool column.
    wzoom = ttk.Frame(wplotarea)
    wzoom.pack(side="left", fill="y", padx=(0, 3))
    ttk.Frame(wzoom, height=48).pack()            # level the tools with the plot

    def _zoombtn(key, cmd, tip):
        btn = ttk.Button(wzoom, style="Toolbutton", padding=3, command=cmd)
        btn.pack(pady=(0, 2))
        hints(btn, tip)
        state.setdefault("wzoombtn", {})[key] = btn
        return btn

    # Horizontal arrows for the time axis, vertical for amplitude; spread to
    # zoom in, pinched to zoom out; a framed record for Whole record. The
    # icons are drawn in main() from winicons, with a text fall-back if the
    # drawing fails - the same path the mask tools take. do_zoom is
    # unchanged: only the controls that call it have moved and become icons.
    _zoombtn("timeout", lambda: do_zoom("time", -1), "Time: zoom out")
    _zoombtn("timein", lambda: do_zoom("time", 1), "Time: zoom in")
    ttk.Separator(wzoom, orient="horizontal").pack(fill="x", pady=5)
    _zoombtn("ampout", lambda: do_zoom("volts", -1), "Amplitude: zoom out")
    _zoombtn("ampin", lambda: do_zoom("volts", 1), "Amplitude: zoom in")
    ttk.Separator(wzoom, orient="horizontal").pack(fill="x", pady=5)
    _zoombtn("whole", lambda: do_zoom("reset", 0), "Whole record")

    wplotcol = ttk.Frame(wplotarea)
    wplotcol.pack(side="left", fill="both", expand=True)
    # A strip above the plot showing the whole record with the part on the
    # graticule marked out, which is what the instrument's own record view
    # does. Without it, a zoomed-in trace gives no clue where in the capture
    # it came from. In the plot's own column, so it is the plot's width.
    over = tk.Canvas(wplotcol, height=44, highlightthickness=1,
                     background=tds_wfm.DEFAULT_COLOURS["background"],
                     highlightbackground=EDGE)
    over.pack(fill="x")
    plot = tk.Canvas(wplotcol,
                     background=tds_wfm.DEFAULT_COLOURS["background"],
                     highlightthickness=1, highlightbackground=EDGE)
    plot.pack(fill="both", expand=True, pady=(2, 0))
    # The readout used to sit here, under the plot but inside the plot pane,
    # which put a strip of grey between the plot's black edge and the resize
    # sash - so the sash, not the plot edge, was what you had to grab. It is
    # now the first thing in the decode pane instead, so the sash falls right
    # on the plot's bottom edge (black meets grey) and that edge is the grab.
    wrightsplit.add(wplotarea, stretch="always", minsize=160)

    # ---- decode controls and results, under the plot
    # Here rather than in the narrow left pane: a two-wire bus needs a
    # source chosen per wire and the protocol its own settings, which is
    # more than the source lists' column has room for. The strip that
    # marks the decoded values on the trace itself is drawn on the plot
    # above; this is where the same values are listed and read.
    wdecpanel = ttk.Frame(wrightsplit, padding=(6, 4))
    # The plot's readout, first thing in the decode pane so it sits just below
    # the plot (the sash above it is the plot's own bottom edge).
    winfo = ttk.Label(wdecpanel, text="", anchor="w")
    winfo.pack(side="top", fill="x", pady=(0, 4))
    # Controls on the left, the results table on the right, split by a
    # horizontal sash. No handle and no line: you resize the table by
    # dragging its left edge, where the cursor changes over the invisible
    # sash strip. The table is half the section wide by default and the same
    # width whatever decoder is chosen (dec_table_fit); the controls take the
    # other half, wide enough to hold the source dropdowns on one line.
    wdecsplit = tk.PanedWindow(wdecpanel, orient="horizontal",
                               background=_panebg, **_SASH)
    wdecsplit.pack(fill="both", expand=True)
    # The left pane: the controls fill it. No fixed column any more - the
    # pane is half the section, wide enough for a four-wire bus on one line.
    wdecleftpane = ttk.Frame(wdecsplit)
    wdecleft = ttk.Frame(wdecleftpane)
    wdecleft.pack(side="left", fill="both", expand=True)
    # Rebuilt whenever the chosen protocol changes: a source box per wire
    # and a control per setting, from the decoder's own declaration, so a
    # new protocol brings its own controls with it and nothing here needs
    # to know what they are.
    # Its contents are dynamic (destroyed and rebuilt per protocol), so
    # they are never registered with says() - that list is for lasting
    # furniture only, and a destroyed widget in it crashes retranslate.
    # dec_build_controls fills this from _() each time, and a relabel hook
    # (added below) re-runs it on a language change so it follows the
    # setting like everything else.
    wdecctl = ttk.Frame(wdecleft)
    wdecctl.pack(fill="x")
    wdecact = ttk.Frame(wdecleft)
    wdecact.pack(fill="x", pady=(4, 0))
    btn_decrun = ttk.Button(wdecact, text=_("Decode"), padding=(10, 2),
                            command=lambda: do_dec_apply())
    btn_decrun.pack(side="left")
    says(btn_decrun, "Decode")
    btn_decclear = ttk.Button(wdecact, text=_("Clear"), padding=(10, 2),
                              command=lambda: do_dec_clear())
    btn_decclear.pack(side="left", padx=(6, 0))
    says(btn_decclear, "Clear")
    state["decascii"] = tk.BooleanVar(value=True)
    chk_decascii = ttk.Checkbutton(wdecact, variable=state["decascii"],
                                   text=_("Show ASCII"),
                                   command=lambda: dec_reformat())
    chk_decascii.pack(side="left", padx=(12, 0))
    says(chk_decascii, "Show ASCII")
    # One line, the full width of the controls pane (to the table edge):
    # wraplength 0 is no wrap, and fill="x" gives it the pane's width. A
    # line shorter than when it wrapped, so the decode pane's minsize drops
    # by a line to match (below).
    lbl_decnote = ttk.Label(wdecleft, foreground="#555", anchor="w",
                            wraplength=0, justify="left")
    lbl_decnote.pack(fill="x", pady=(4, 0))
    # The lister: one row per decoded event, its columns set by the protocol.
    # It is the right pane of the horizontal split, kept at half the section
    # width (dec_table_fit) so it is the same size whatever decoder is picked.
    # The last column stretches to fill that width; grid_propagate off lets
    # the pane, not the tree, set the frame size. Five rows tall, to keep the
    # decode section short and the waveform window large.
    wdectblf = ttk.Frame(wdecsplit, width=300, height=124)
    wdectblf.grid_propagate(False)
    wdectblf.pack_propagate(False)
    dectree = ttk.Treeview(wdectblf, show="headings", height=5,
                           selectmode="browse")
    dectbar = ttk.Scrollbar(wdectblf, orient="vertical",
                            command=dectree.yview)
    dectree.configure(yscrollcommand=dectbar.set)
    dectree.grid(row=0, column=0, sticky="nsew")
    dectbar.grid(row=0, column=1, sticky="ns")
    wdectblf.grid_rowconfigure(0, weight=1)
    wdectblf.grid_columnconfigure(0, weight=1)
    # Both panes hold a floor width: the controls' keeps the four source
    # dropdowns on one line, the table's stops it being dragged away. The
    # controls take spare width (stretch) so the table keeps the width
    # dec_table_fit gave it as the window resizes.
    wdecsplit.add(wdecleftpane, minsize=520, stretch="always")
    wdecsplit.add(wdectblf, minsize=190, stretch="never")
    # minsize covers the readout line plus the tallest controls (a four-wire,
    # four-setting decoder like SPI: sources on one line, settings two to a
    # row, then the buttons, then the one-line note) - so the section opens
    # short and the waveform window takes the rest, but the buttons are never
    # crushed. A line shorter than before, now the note no longer wraps.
    wrightsplit.add(wdecpanel, stretch="never", minsize=160)
    wrightf.pack(side="left", fill="both", expand=True)
    # Place the sash once the window has a real width. dec_table_fit also
    # sets the table frame's requested width, so even before this fires (the
    # tab is behind another at startup) the pane opens at the fixed width.
    root.after(400, lambda: dec_table_fit())

    # ------------------------------------------------ protocol decode
    def dec_folder():
        """Where decoder plugins live on this computer. Topped up from the
        bundled set every run (tds_decode.sync_folder), so a decoder added
        in an update appears without reseeding, while a dropped-in <name>.py
        of the user's own is left alone and picked up the same way."""
        here = os.path.join(APPDIR, "decoders")
        try:
            return tds_decode.sync_folder(resource("decoders"), here)
        except OSError as exc:
            log_note("decode", "%s unusable (%s)" % (here, exc))
            return None

    def do_dec_scan():
        """Re-read the decoders folder and fill the protocol list."""
        folder = dec_folder()
        state["decoders"] = {}
        dec.delete(*dec.get_children())
        for one in tds_decode.discover([folder] if folder else []):
            state["decoders"][one.NAME] = one
            wires = "+".join(r.rstrip("?") for r in one.SOURCES)
            dec.insert("", "end", iid=one.NAME, text=one.NAME,
                       values=(wires,))
        dec_build_controls()

    def dec_sources():
        """Captured waveforms a decode can run on, by name: the channels
        that were fetched and any file staged into a reference."""
        pool = {}
        pool.update(state.get("fetched") or {})
        pool.update(state.get("staged") or {})
        return pool

    def dec_selected():
        sel = dec.selection()
        return state.get("decoders", {}).get(sel[0]) if sel else None

    def _choice_label(param, value):
        for val, lab in param.choices:
            if val == value:
                return lab
        return param.choices[0][1] if param.choices else ""

    def _choice_value(param, label):
        for val, lab in param.choices:
            if lab == label:
                return val
        return param.default

    def dec_refresh_source_lists():
        """Show the captured waveforms that exist right now in every source
        dropdown. Bound to each list's postcommand, so it fires as the list
        opens: enabling or forgetting a channel is picked up without
        rebuilding the panel or disturbing the mappings already made."""
        names = sorted(dec_sources().keys())
        for box, optional in state.get("decsourceboxes", []):
            box.config(values=(([""] if optional else []) + names))

    def dec_build_controls(_evt=None):
        """Rebuild the source boxes and settings for the chosen protocol,
        each straight from the decoder's own declaration."""
        for child in wdecctl.winfo_children():
            child.destroy()
        state["decmap"] = {}
        state["decparams"] = {}
        state["decsourceboxes"] = []
        one = dec_selected()
        if one is None:
            ttk.Label(wdecctl, foreground="#555", text=_(
                "Select a protocol from the list on the left.")).pack(
                anchor="w")
            btn_decrun.config(state="disabled")
            dec_fill_table()
            return
        btn_decrun.config(state="normal")
        names = sorted(dec_sources().keys())
        # The source boxes all on one line: a "Source:" label, then a box per
        # wire (SPI is CLK, MOSI, MISO and CS). The controls pane is half the
        # section - wide enough that even a four-wire bus fits one line, which
        # is where the earlier fixed-width column could not hold them. Each
        # wire defaults to a different captured source, which a real bus wants.
        srow = ttk.Frame(wdecctl)
        srow.pack(fill="x")
        state["decsrow"] = srow            # exposed for the layout self-check
        ttk.Label(srow, text=_("Source:"), foreground="#555").pack(
            side="left", padx=(0, 6))
        si = 0
        for i, role in enumerate(one.SOURCES):
            optional = role.endswith("?")
            key = role.rstrip("?")
            cell = ttk.Frame(srow)
            cell.pack(side="left", padx=(0, 10))
            ttk.Label(cell, text=key + (" *" if optional else "")).pack(
                side="left", padx=(0, 2))
            var = tk.StringVar()
            state["decmap"][key] = var
            values = ([""] if optional else []) + names
            box = ttk.Combobox(cell, textvariable=var, width=8,
                               state="readonly", values=values,
                               postcommand=dec_refresh_source_lists)
            box.pack(side="left")
            state["decsourceboxes"].append((box, optional))
            if not optional and names:
                var.set(names[min(si, len(names) - 1)])
                si += 1
        # The settings two to a row, so a four-setting decoder like SPI takes
        # two lines rather than four and the whole section stays about five
        # lines high. The controls pane is wide enough (half the section) that
        # two auto-sized boxes sit side by side without clipping or spilling.
        prow = ttk.Frame(wdecctl)
        prow.pack(fill="x", pady=(4, 0))
        state["decprow"] = prow            # exposed for the layout self-check
        for i, param in enumerate(one.PARAMS):
            cell = ttk.Frame(prow)
            cell.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 10),
                      pady=1)
            ttk.Label(cell, text=_(param.label) + ":").pack(side="left",
                                                            padx=(0, 2))
            if param.kind == "bool":
                var = tk.BooleanVar(value=bool(param.default))
                ttk.Checkbutton(cell, variable=var).pack(side="left")
            elif param.kind == "int":
                var = tk.StringVar(value=str(param.default))
                ttk.Spinbox(cell, textvariable=var, width=6,
                            from_=param.lo or 0,
                            to=param.hi or 100000).pack(side="left")
            else:
                labels = [lab for _v, lab in param.choices]
                var = tk.StringVar(value=_choice_label(param, param.default))
                # +2 so the dropdown arrow does not clip the last character.
                width = max([8] + [len(lab) for lab in labels]) + 2
                ttk.Combobox(cell, textvariable=var, width=width,
                             state="readonly", values=labels).pack(
                    side="left")
            state["decparams"][param.key] = (param, var)
        # Show this protocol's own columns and width straight away, so the
        # empty table looks the same as it will once a decode has run.
        dec_fill_table()

    def _dec_params():
        out = {}
        for key, (param, var) in (state.get("decparams") or {}).items():
            if param.kind == "bool":
                out[key] = bool(var.get())
            elif param.kind == "int":
                try:
                    out[key] = int(var.get())
                except (TypeError, ValueError):
                    out[key] = param.default
            else:
                out[key] = _choice_value(param, var.get())
        return out

    def do_dec_apply():
        """Run the chosen decoder on the mapped sources and show it."""
        one = dec_selected()
        if one is None:
            ui.say(_("Pick a protocol first"))
            return
        pool = dec_sources()
        signals = {}
        missing = []
        primary_dt = None
        primary_name = ""
        for role in one.SOURCES:
            optional = role.endswith("?")
            key = role.rstrip("?")
            name = (state["decmap"][key].get() or "").strip()
            wave = pool.get(name) if name else None
            if wave is None:
                if not optional:
                    missing.append(key)
                continue
            sig = tds_decode.Signal.from_waveform(wave)
            signals[key] = sig
            if primary_dt is None:
                primary_dt = sig.dt
                primary_name = name
        if missing:
            messagebox.showwarning(_("Decode"), _(
                "Choose a captured source for: %s") % ", ".join(missing))
            return
        try:
            frames, note = one.decode(signals, _dec_params())
        except Exception as exc:                  # noqa: BLE001
            log_note("decode", "%s: %s" % (one.NAME, exc))
            messagebox.showerror(_("Error"),
                                 _("The decode failed: %s") % exc)
            return
        state["decresult"] = {"decoder": one, "frames": frames,
                              "dt": primary_dt or 0.0,
                              "source": primary_name}
        lbl_decnote.config(text=("%s - %s" % (one.NAME, note)) if note
                           else "")
        dec_fill_table()
        draw_plot()
        ui.say(_("Decoded %d event(s)") % len(frames))

    def dec_set_columns(one):
        """Give the table the columns a protocol declares, or clear them.
        Called on selection as well as on decode, so the headers show the
        moment a protocol is picked rather than only after it has run."""
        if one is None:
            dectree["columns"] = ()
            return
        cols = one.columns()
        ids = ["c%d" % i for i in range(len(cols))]
        dectree["columns"] = ids
        last = len(cols) - 1
        for j, (cid, (head, wide)) in enumerate(zip(ids, cols)):
            dectree.heading(cid, text=head)
            # The last column stretches so the columns fill the half-width
            # table rather than leaving a gap on the right.
            dectree.column(cid, width=wide, anchor="w", stretch=(j == last))

    def dec_table_width():
        """The one table width, in pixels: the widest decoder's own columns
        plus the scrollbar and a little air. Constant across every decoder
        (and with none chosen), so the table never changes size and the
        controls beside it keep the same room - enough for four equal source
        dropdowns on one line."""
        ds = state.get("decoders") or {}
        sums = [sum(w for _h, w in d.columns()) for d in ds.values()]
        return (max(sums) if sums else 260) + 18 + 12

    def dec_table_fit():
        """Keep the table at dec_table_width() - the same for every decoder
        and when none is chosen, never sized to the data. The controls take
        the rest (with a floor that keeps the source dropdowns on one line).
        The table frame's own requested width is set to that too, so before
        the sash has ever been placed (the tab was behind another when the
        window was built) the pane still opens at the full width rather than
        the frame's placeholder - otherwise the empty table opened narrow and
        jumped wider the moment a protocol was picked. The user can still drag
        the sash; the next protocol change resets it."""
        want = dec_table_width()
        wdectblf.configure(width=want)          # req width: right before sash
        try:
            total = wdecsplit.winfo_width()
        except tk.TclError:
            total = 0
        if total > 20:
            left = max(520, total - want)      # controls floor first
            wdecsplit.sash_place(0, int(left), 0)

    def dec_fill_table():
        res = state.get("decresult")
        dectree.delete(*dectree.get_children())
        if not res or not res["frames"]:
            # No decode yet: show the picked protocol's headers over an
            # empty table, at that protocol's own width, so the panel looks
            # the same before a decode as after one - just without rows.
            dec_set_columns(dec_selected())
            dec_table_fit()
            return
        one = res["decoder"]
        ascii_on = bool(state["decascii"].get())
        dec_set_columns(one)
        for k, frame in enumerate(res["frames"]):
            cells = [tds_wfm.eng(frame.time, "s")] + list(
                one.row(frame, ascii_on))
            dectree.insert("", "end", iid=str(k), values=cells)
        dec_table_fit()

    def dec_reformat():
        """Hex/ASCII changed: the same events, said the other way."""
        if state.get("decresult"):
            dec_fill_table()
            draw_plot()

    def do_dec_clear():
        """Remove a decode: its strip off the plot, its table, its result,
        and the band back to its resting place."""
        state["decresult"] = None
        state["decoffset"] = 0.0
        dec_fill_table()
        draw_plot()
        ui.say(_("Decode cleared"))

    def draw_decode_strip(left, top, right, bottom):
        """The decoded values marked on the trace itself: one box per event
        at its own place in time, the way an instrument draws a bus line.
        Each box is the protocol's colour (an error box is red); a marker
        at the left slides the whole band up and down, as a channel's
        marker moves its trace. Read alongside the table."""
        state.pop("decmarkerbox", None)
        res = state.get("decresult")
        view = state.get("view")
        if not res or view is None or not res["frames"]:
            return
        one = res["decoder"]
        ascii_on = bool(state["decascii"].get())
        dt = res.get("dt") or 0.0
        band = 16
        # Default just above the scale line; the marker drag moves it, and
        # it is kept inside the graticule either way.
        y1 = bottom - 2 - state.get("decoffset", 0.0)
        y1 = min(bottom - 2, max(top + band, y1))
        y0 = y1 - band
        mid = (y0 + y1) / 2.0
        # Every chevron's colour comes from the decoder itself: the app has
        # no colour setting for decoders, so a plugin owns them completely -
        # one colour for the protocol, or a colour per frame. See
        # Decoder.colour. The marker tab takes the protocol's base colour.
        marker_colour = getattr(one, "COLOUR", None) or "#39a0c0"
        for frame in res["frames"]:
            end_time = frame.time + max(0, frame.end_index - frame.index) * dt
            x0 = view.x_of_time(frame.time, left, right)
            x1 = view.x_of_time(end_time, left, right)
            if x1 < left or x0 > right:
                continue
            x0 = max(left, x0)
            x1 = min(right, x1)
            if x1 - x0 < 2:
                x1 = x0 + 2
            colour = _dec_colour(one, frame)
            notch = min(4.0, (x1 - x0) / 3.0)
            spot = [x0, mid, x0 + notch, y0, x1 - notch, y0, x1, mid,
                    x1 - notch, y1, x0 + notch, y1]
            plot.create_polygon(*spot, outline=colour, fill="",
                                width=1, tags="decode")
            _decode_label(one.label(frame, ascii_on),
                          (x0 + x1) / 2.0, mid, (x1 - x0) - 4, colour)
        # The marker: a pointed flag at the band's level, drawn the same way
        # a channel's marker is - the protocol's colour, the name on it in
        # ink, horizontal and pointed at the graticule - but carrying the
        # protocol in use rather than a channel name, so the band says what
        # it is the way every other trace does. Grabbed to slide the band;
        # its box is remembered so a press on it starts a vertical drag (see
        # on_plot_press).
        mlabel = getattr(one, "NAME", "") or "decode"
        mfont = _decode_font(plot_font().actual("size"))
        mwide = mfont.measure(mlabel) + 10 + 6
        mx0 = tds_wfm.marker_place([], left, mid, mwide, 13, 2,
                                   right=right, room=plot.winfo_width())
        facing = tds_wfm.marker_facing(mx0, right)
        spot = tds_wfm.marker_shape(mx0, mid, mwide, facing=facing)
        plot.create_polygon(*spot, fill=marker_colour, outline=marker_colour,
                            tags=("decode", "decmarker"))
        plot.create_text(mx0 + 5 + (6 if facing < 0 else 0), mid, anchor="w",
                         text=mlabel, fill=_ink(marker_colour), font=mfont,
                         tags=("decode", "decmarker"))
        state["decmarkerbox"] = (mx0 - 2, mid - 8, mx0 + mwide + 2, mid + 8)

    def _dec_colour(one, frame):
        """The chevron colour for a frame, from the decoder and nowhere else.
        Guarded: a plugin whose colour() raises or returns something that is
        not a "#rrggbb" must not take the strip down, so it falls back to a
        readable default rather than to any app setting (there is none)."""
        try:
            c = one.colour(frame)
        except Exception:                       # noqa: BLE001 - a bad plugin
            c = None
        return c if (isinstance(c, str) and c.startswith("#")) else "#39a0c0"

    def _ink(hexcol):
        """Black or white, whichever reads on `hexcol` - used for the marker
        tab's text, so the source name stays legible on a light or a dark
        protocol colour."""
        try:
            r = int(hexcol[1:3], 16)
            g = int(hexcol[3:5], 16)
            b = int(hexcol[5:7], 16)
        except (ValueError, IndexError):
            return "#000000"
        # Rec. 601 luma; the usual mid-point split.
        return "#000000" if (r * 299 + g * 587 + b * 114) / 1000 >= 140 \
            else "#ffffff"

    def _decode_font(size):
        """A canvas font at a given size, cached - the strip asks for a few
        sizes per repaint and making a Font each time is wasteful."""
        cache = state.setdefault("decfonts", {})
        got = cache.get(size)
        if got is None:
            got = tkfont.Font(family=plot_font().actual("family"), size=size)
            cache[size] = got
        return got

    def _decode_label(text, cx, cy, room, colour):
        """The value centred in its box, the font shrunk until it fits; when
        even the smallest will not fit, a dot marks that data is there
        rather than leaving the box blank."""
        if room >= 3:
            base = plot_font().actual("size")   # Tk: <0 is px, >0 is points
            step = -1 if base < 0 else 1
            size = base
            for _ in range(6):
                font = _decode_font(size)
                if font.measure(text) <= room:
                    plot.create_text(cx, cy, text=text, fill=colour,
                                     font=font, tags="decode")
                    return
                nxt = size - step
                if abs(nxt) < 6:                 # do not shrink past ~6
                    break
                size = nxt
        plot.create_oval(cx - 1.5, cy - 1.5, cx + 1.5, cy + 1.5,
                         fill=colour, outline=colour, tags="decode")

    def plot_font():
        """The font the canvas draws text in, for measuring it."""
        if state.get("plotfont") is None:
            state["plotfont"] = tkfont.nametofont("TkDefaultFont")
        return state["plotfont"]

    def preset_name():
        """Which named scheme the current colours are, if any.

        Matched by value rather than remembered by name, which is what
        the dialog's own box does: change one colour by hand and this
        stops saying Instrument, correctly.
        """
        for name, cols in ui.colour_presets().items():
            if cols == tds_wfm.scheme(state.get("colours")):
                return name
        return ""

    def plot_colours(wave=None):
        """The scheme the plot is actually drawn in.

        On the Instrument preset, and only there, the trace takes the
        colour the instrument draws that source in - read from its own
        palette, so a REF really is the colour the REFs are on the
        screen. Any other scheme is the user's choice and is left alone;
        so is an instrument that has no colours to report, which is
        every monochrome one.
        """
        pick = tds_wfm.scheme(state.get("colours"))
        source = getattr(wave, "source", None) if wave else None
        own = (state.get("icolours") or {}).get(str(source or "").upper())
        if own and preset_name() == "Instrument":
            pick["trace"] = own
        return pick

    def trace_colour(wave, pick=None):
        """What to draw this particular trace in.

        With several on the graticule at once they cannot all be the
        scheme's one colour, or there is no telling them apart. On the
        Instrument preset each takes the instrument's own; on any other
        scheme the first keeps the scheme's colour and the rest are
        shifted round the hue circle, which keeps a chosen scheme
        recognisable while still separating the traces.
        """
        pick = pick or tds_wfm.scheme(state.get("colours"))
        own = (state.get("icolours") or {}).get(
            str(getattr(wave, "source", "") or "").upper())
        if own and preset_name() == "Instrument":
            return own
        order = [w.source for w in shown_waves()]
        at = order.index(wave.source) if wave.source in order else 0
        return tds_wfm.shifted_hue(pick["trace"], at)

    def shown_waves():
        """Every trace on the graticule, the primary one first."""
        return [w for w in (state.get("waves") or []) if w is not None]

    def traces_by_name():
        """Colours for the PNG export, which has no colour scheme of
        its own to work out."""
        return dict((w.source, trace_colour(w)) for w in shown_waves())

    def plot_soon():
        """Redraw once the pointer stops arriving, not once per event.

        A drag across the plot or the strip, and a spin of the wheel,
        deliver events faster than a long record can be drawn: each draw
        walks every whole record more than once. Drawn one for one, the
        queue grew and the trace trailed the pointer. Collapsed onto one
        idle callback, a burst costs one draw - the mask editor's
        edit_soon does the same. Letting go draws at once, so a drag
        always ends on an exact picture.
        """
        if state.get("predraw"):
            return
        state["predraw"] = plot.after_idle(plot_now)

    def plot_now():
        state.pop("predraw", None)
        draw_plot()

    def draw_plot(_evt=None):
        """Redraw the held trace to fit the canvas.

        Vector, not a bitmap, so it stays crisp when the window is
        resized - but the geometry comes from the same function the PNG
        export uses, so what is saved is what was on screen.
        """
        pending = state.pop("predraw", None)
        if pending:
            plot.after_cancel(pending)
        plot.delete("all")
        wave = state.get("wave")
        pick = plot_colours(wave)
        plot.config(background=pick["background"])
        w = max(plot.winfo_width(), 60)
        h = max(plot.winfo_height(), 40)
        pad = 34
        left, top, right, bottom = tds_wfm.plot_frame(w, h, pad)
        # Every line comes from tds_wfm.graticule(), which the PNG export
        # draws from as well - so the picture that is saved is the
        # picture that was on screen, rather than two drawings that
        # agree today and drift apart tomorrow.
        for element, x0, y0, x1, y1, thick in tds_wfm.graticule(w, h, pad):
            plot.create_line(x0, y0, x1, y1, fill=pick[element],
                             width=thick)
        if not wave:
            # In a box. Text alone lands on top of the graticule and the
            # centre cross runs straight through the middle of it.
            words = _("No waveform. Pick a source and press Get waveform.")
            # Centred on the graticule rather than on the pane. The two
            # usually agree now that plot_frame centres, but a pane too
            # narrow to keep the markers' room and still centre is the
            # case where they do not.
            label = plot.create_text((left + right) / 2.0,
                                     (top + bottom) / 2.0,
                                     fill=pick["label"], text=words,
                                     tags="empty")
            x0, y0, x1, y1 = plot.bbox(label)
            box = plot.create_rectangle(x0 - 12, y0 - 8, x1 + 12, y1 + 8,
                                        fill=pick["background"],
                                        outline=pick["label"],
                                        tags="empty")
            plot.tag_lower(box, label)
            # The strip above describes the same nothing. Returning
            # before it is redrawn left the last trace drawn up there
            # after every trace had been taken off.
            draw_over()
            return
        taken = []
        for one in shown_waves():
            colour = trace_colour(one, pick)
            xy, _bounds = tds_wfm.plot_geometry(one, w, h, pad,
                                                state.get("view"))
            if len(xy) > 1:
                flat = []
                for x, y in xy:
                    flat += [x, y]
                # Tagged, so a test can ask whether the *trace* was
                # drawn. Asking whether anything was drawn is not the
                # same question: the graticule is always there and
                # answers yes regardless.
                plot.create_line(*flat, fill=colour, width=1, tags="trace")
            # A marker at this trace's own zero volts, filled in its own
            # colour with the name on it in black - which is how an
            # instrument tells its traces apart, and the only thing on
            # the plot that says which of four channels this is. Just
            # inside the left edge of the graticule, where the
            # instrument puts its own.
            name = str(one.label or one.source or "")[:8]
            if not name:
                continue
            # Measured in the theme's own font: a ttk widget has no
            # -font of its own, and a marker sized by counting
            # characters is too small for "MATH1" and too big for "R1".
            wide = plot_font().measure(name) + 10 + 6
            zero = state["view"].y_of_volts(one, 0.0, top, bottom)
            zero = min(bottom - 7, max(top + 7, zero))
            # Outside the graticule, where it covers no signal - the
            # room for it is kept clear by plot_frame. Channels all
            # sitting at position zero would put their markers in the
            # same place and only the last drawn would be readable, so
            # any that collide are stepped aside by the same rule the
            # saved picture uses. The span kept is what the canvas
            # reports for a polygon, which is two pixels wider each
            # side than the shape asked for.
            x0 = tds_wfm.marker_place(taken, left, zero, wide, 13, 2,
                                      right=right, room=w)
            taken.append((x0 - 2, zero, x0 + wide + 2))
            # Pointed towards the graticule whichever margin it ended up
            # in: one that had to go to the right of the frame points
            # left, at the trace, rather than out at the window edge.
            facing = tds_wfm.marker_facing(x0, right)
            spot = tds_wfm.marker_shape(x0, zero, wide, facing=facing)
            # Tagged with the source as well, so a test can ask where
            # *this* trace's marker is rather than where the first one
            # is - which is the same place only when every channel
            # happens to sit at position zero.
            mine = ("marker", "marker:%s" % (wave_name(one) or name))
            plot.create_polygon(*spot, fill=colour, outline=colour,
                                tags=mine)
            plot.create_text(x0 + 5 + (6 if facing < 0 else 0), zero,
                             anchor="w", fill="#000000",
                             text=name, tags=mine)
        # The per-division reading under the graticule, and nothing at
        # the corners: the values that used to sit inside the frame said
        # what this says, in a form no instrument uses, and landed on
        # top of the trace while doing it.
        plot.create_text((left + right) / 2.0, bottom + 4, anchor="n",
                         fill=pick["label"], text=wave_scales(wave),
                         tags="scales")
        draw_decode_strip(left, top, right, bottom)
        draw_over()

    def draw_over():
        """The strip above the plot: the whole record, window marked.

        Drawn from the same samples at a fixed vertical scale - it is a
        map of the capture, not a second plot, so zooming the main view
        must not change what it looks like.
        """
        over.delete("all")
        wave = state.get("wave")
        pick = plot_colours(wave)
        over.config(background=pick["background"])
        w = max(over.winfo_width(), 60)
        h = max(over.winfo_height(), 20)
        view = state.get("view")
        if not wave or view is None:
            return
        # Every trace, each laid out by *time* over the whole span the
        # set covers, so this strip and the window marked on it are
        # measuring the same axis. Drawing only the front trace across
        # the full width said a 500-point channel and a 5000-point
        # reference were the same length, and then marked a window on
        # that which had nothing to do with either.
        whole_span = max(1e-15, view.full_span)
        for one in shown_waves():
            levels = one.levels()
            spots = one.points()
            if len(levels) < 2 or len(spots) < 2:
                continue
            a = (spots[0][0] - view.full_first) / whole_span * w
            b = (spots[-1][0] - view.full_first) / whole_span * w
            wide = max(2.0, b - a)
            # Drawn here rather than through plot_geometry, which keeps
            # a 10:8 graticule square and would fit a 110-pixel picture
            # into the middle of a 900-pixel strip. This is a map of the
            # record, not a plot: it uses the width the record covers
            # and whatever height it has, and the shape of it does not
            # have to mean anything. Each trace is scaled to its own
            # peak, so a small signal beside a large one is still
            # visible rather than a flat line.
            reach = max(1.0, max(abs(min(levels)), abs(max(levels))))
            columns = max(2, int(wide))
            flat = []
            for column in range(columns):
                i = int(len(levels) * column / float(columns))
                j = max(i + 1,
                        int(len(levels) * (column + 1) / float(columns)))
                chunk = levels[i:j]
                x = a + wide * (column + 0.5) / columns
                for level in (max(chunk), min(chunk)):
                    flat += [x, h / 2.0 - (level / reach) * (h / 2.0 - 3)]
            if len(flat) > 3:
                over.create_line(*flat, fill=trace_colour(one, pick),
                                 width=1, tags="overtrace")
        start, end = view.fractions()
        x0, x1 = start * w, end * w
        if x1 - x0 < 3:                 # always grabbable
            middle = (x0 + x1) / 2.0
            x0, x1 = middle - 1.5, middle + 1.5
        over.create_rectangle(x0, 0, x1, h, outline=pick["label"],
                              width=2, tags="window")
        # Shaded either side rather than inside, so the marked part is
        # the part you can see clearly.
        for a, b in ((0, x0), (x1, w)):
            if b > a:
                over.create_rectangle(a, 0, b, h, fill=pick["background"],
                                      outline="", stipple="gray50",
                                      tags="shade")

    plot.bind("<Configure>", draw_plot)
    over.bind("<Configure>", lambda e: draw_over())

    # ------------------------------------------------ moving the view
    # Everything here works in the record's own coordinates - samples
    # across and digitiser counts down - so a drag means the same thing
    # whatever size the window is, and the view survives a resize.

    def plot_frame_now():
        w = max(plot.winfo_width(), 60)
        h = max(plot.winfo_height(), 40)
        return tds_wfm.plot_frame(w, h, 34)

    def at_fractions(x, y):
        """Where a pixel is in the frame, as fractions across and down."""
        left, top, right, bottom = plot_frame_now()
        wide = max(1.0, right - left)
        tall = max(1.0, bottom - top)
        return ((x - left) / wide, (y - top) / tall)

    def time_at(x):
        """Which moment of the record a pixel is over, in seconds."""
        view = state.get("view")
        across, _down = at_fractions(x, 0)
        return view.first + view.span * across

    def division_at(y):
        """How far above the centre line a pixel is, in divisions.

        Screen divisions rather than a trace's own counts: with several
        traces on the graticule at different volts a division, a pixel
        is not one number of counts, but it is always one place on the
        screen.
        """
        _across, down = at_fractions(0, y)
        return (0.5 - down) * tds_wfm.DIVS_Y

    def do_zoom(what, direction, across=0.5, down=0.5, soon=False):
        """Zoom the view. `soon` for the wheel, which arrives in bursts;
        a button press redraws at once."""
        view = state.get("view")
        if view is None:
            return
        if what == "reset":
            view.reset()
        elif what == "time":
            view.zoom_time(direction, across)
        else:
            view.zoom_volts(direction, down)
        if soon:
            plot_soon()
        else:
            draw_plot()

    def on_plot_press(evt):
        """Start a drag: the decode band's marker moves the band, a pan
        moves the view, Shift draws a zoom window."""
        if state.get("view") is None:
            return
        box = state.get("decmarkerbox")
        if (box and box[0] - 2 <= evt.x <= box[2] + 2
                and box[1] - 3 <= evt.y <= box[3] + 3):
            # Grabbed the decode marker: this drag slides the band.
            state["drag"] = {"decode": True, "y": evt.y,
                             "start": state.get("decoffset", 0.0)}
            return
        state["drag"] = {"x": evt.x, "y": evt.y,
                         "from_time": time_at(evt.x),
                         "from_div": division_at(evt.y),
                         # Taking hold of a trace's marker moves that
                         # trace and nothing else, the way the position
                         # knob on the instrument moves the selected
                         # channel. Anywhere else moves the view. The
                         # marker is what is grabbed because it is a
                         # fixed target with the name on it; the trace
                         # itself is a line one pixel wide that moves
                         # whenever the view does.
                         "only": marker_under(evt.x, evt.y),
                         # Where every trace stood when the drag began,
                         # so the vertical move can be worked out from
                         # the start rather than from the last event -
                         # which is what makes the snap onto the centre
                         # line escapable. See PlotView.snap_middle.
                         "held": dict(state["view"].shifts),
                         "box": bool(evt.state & 0x0001)}   # Shift

    def on_plot_move(evt):
        drag = state.get("drag")
        view = state.get("view")
        if not drag or view is None:
            return
        if drag.get("decode"):
            # Up is positive: dragging the marker up raises the band.
            state["decoffset"] = drag["start"] + (drag["y"] - evt.y)
            plot_soon()
            return
        if drag["box"]:
            plot.delete("band")
            pick = plot_colours(state.get("wave"))
            plot.create_rectangle(drag["x"], drag["y"], evt.x, evt.y,
                                  outline=pick["label"], dash=(3, 2),
                                  tags="band")
            return
        # Panning: whatever was under the pointer stays under it. The
        # vertical sign is the one that was wrong - dragging downwards
        # raised the traces, which is the opposite of taking hold of
        # something and moving it.
        #
        # Vertically it moves only the trace whose marker the drag
        # started on, so channels can be moved apart one at a time the
        # way they are on the instrument. A drag that started anywhere
        # else moves the whole view, which is the sensible thing when no
        # trace in particular was taken hold of.
        picked = [x for x in shown_waves()
                  if wave_name(x) == drag.get("only")]
        # Across, always: the window clamps at the ends of the record,
        # so it has to follow the pointer one event at a time.
        view.pan(seconds=drag["from_time"] - time_at(evt.x))
        drag["from_time"] = time_at(evt.x)
        if picked:
            # Down, measured from where the drag began. A trace whose
            # zero comes close to the centre line snaps onto it, and a
            # snap applied to a running total can never be dragged back
            # off: a slow drag moves a pixel or two between events,
            # which is less than the snap's own tolerance, so it would
            # be pulled straight back every time.
            moved = division_at(evt.y) - drag["from_div"]
            for one in picked:
                view.shifts[one.source] = (drag["held"].get(one.source, 0.0)
                                           + moved)
            view.clamp()
            for one in picked:
                view.snap_middle(one)
        else:
            view.pan(divisions=division_at(evt.y) - drag["from_div"])
            drag["from_div"] = division_at(evt.y)
        plot_soon()

    def on_plot_release(evt):
        drag = state.pop("drag", None)
        view = state.get("view")
        if not drag or view is None:
            return
        if drag.get("decode"):
            return                            # the band drag needs no finish
        plot.delete("band")
        if not drag.get("box"):
            return
        # A drag of a pixel or two is a click that shook, not a window.
        if abs(evt.x - drag["x"]) < 6 and abs(evt.y - drag["y"]) < 6:
            return
        view.show_to(drag["from_time"], time_at(evt.x),
                     drag["from_div"], division_at(evt.y))
        draw_plot()

    def on_plot_wheel(evt, direction=None):
        """The wheel zooms about the pointer; Shift zooms amplitude."""
        if state.get("view") is None:
            return
        if direction is None:
            direction = 1 if evt.delta > 0 else -1
        across, down = at_fractions(evt.x, evt.y)
        do_zoom("volts" if (evt.state & 0x0001) else "time",
                direction, across, down, soon=True)

    # How near an edge of the marked window counts as taking hold of it.
    OVER_GRAB = 5

    def over_time(x):
        """The moment in the record a point along the strip stands for."""
        view = state["view"]
        w = max(over.winfo_width(), 60)
        return view.full_first + (x / float(w)) * view.full_span

    def over_edge(x):
        """Which edge of the marked window is under this point, if any."""
        view = state.get("view")
        if view is None:
            return None
        w = max(over.winfo_width(), 60)
        start, end = view.fractions()
        for name, at in (("first", start * w), ("last", end * w)):
            if abs(x - at) <= OVER_GRAB:
                return name
        return None

    def on_over_press(evt):
        """Take hold of an edge, or move the whole window here."""
        if state.get("view") is None:
            return
        state["overdrag"] = over_edge(evt.x)
        on_over_move(evt)

    def on_over_move(evt):
        """Drag: resize from the edge that was taken, or move the window."""
        view = state.get("view")
        if view is None:
            return
        edge = state.get("overdrag")
        if edge is None:
            view.first = over_time(evt.x) - view.span / 2.0
            view.clamp()
        else:
            # The end that was not taken hold of stays where it is -
            # stretch_to keeps it there rather than this reading it
            # back, which is why it can be read back at all.
            other = view.first + view.span if edge == "first" else view.first
            view.stretch_to(over_time(evt.x), other,
                            "last" if edge == "first" else "first")
        plot_soon()

    def on_over_hover(evt):
        """A resize pointer over an edge, so it can be found at all."""
        over.config(cursor="sb_h_double_arrow" if over_edge(evt.x) else "")

    def on_over_wheel(evt, direction=None):
        """The wheel zooms here as well, about the moment under it."""
        view = state.get("view")
        if view is None:
            return
        if direction is None:
            direction = 1 if evt.delta > 0 else -1
        at = ((over_time(evt.x) - view.first) / view.span
              if view.span else 0.5)
        do_zoom("time", direction, at, soon=True)

    plot.bind("<ButtonPress-1>", on_plot_press)
    plot.bind("<B1-Motion>", on_plot_move)
    plot.bind("<ButtonRelease-1>", on_plot_release)
    plot.bind("<MouseWheel>", on_plot_wheel)
    # X11 sends buttons 4 and 5 rather than a wheel event.
    plot.bind("<Button-4>", lambda e: on_plot_wheel(e, 1))
    plot.bind("<Button-5>", lambda e: on_plot_wheel(e, -1))
    over.bind("<ButtonPress-1>", on_over_press)
    over.bind("<B1-Motion>", on_over_move)
    over.bind("<ButtonRelease-1>", lambda e: state.pop("overdrag", None))
    over.bind("<Motion>", on_over_hover)
    over.bind("<MouseWheel>", on_over_wheel)
    over.bind("<Button-4>", lambda e: on_over_wheel(e, 1))
    over.bind("<Button-5>", lambda e: on_over_wheel(e, -1))

    def marker_under(x, y):
        """Which trace's marker is at this point, if any.

        Aimed at the marker rather than at the trace, because a marker
        is a fixed target with a name on it and a trace is a line one
        pixel wide that moves whenever the view does.
        """
        under = plot.find_overlapping(x - 2, y - 2, x + 2, y + 2)
        named = [t.split(":", 1)[1] for i in under
                 for t in plot.gettags(i) if t.startswith("marker:")]
        return named[0] if named else None

    def on_plot_menu(evt):
        """Right-click on a marker: what to do with that trace."""
        name = marker_under(evt.x, evt.y)
        if not name:
            return
        menu = tk.Menu(root, tearoff=0)
        menu.add_command(label=_("Hide %s") % name,
                         command=lambda n=name: do_wfm_shown(n))
        menu.add_command(
            label=_("Show only %s") % name,
            command=lambda n=name: show_waves(
                [x for x in shown_waves() if wave_name(x) == n],
                keep_view=True))
        try:
            menu.tk_popup(evt.x_root, evt.y_root)
        finally:
            menu.grab_release()

    plot.bind("<Button-3>", on_plot_menu)

    def wave_scales(wave):
        """The per-division reading, the way an instrument prints it.

        Under the graticule rather than in the sentence below the plot:
        it is a property of the picture, it belongs with the picture,
        and the sentence was already long enough to be cut off by the
        edge of the window.
        """
        got = wave.measures() if wave else None
        if not got:
            return ""
        view = state.get("view")
        if view is not None:
            # What a division is worth *now*, which is what the reading
            # has to say while the view is being zoomed and panned.
            got = dict(got)
            got["vdiv"] = tds_wfm.eng(view.volts_per_div, wave.yunit)
            got["tdiv"] = tds_wfm.eng(view.seconds_per_div, wave.xunit)
            got["wider"] = not view.whole or got["wider"]
        if got.get("wider"):
            # The record is longer than the ten divisions the instrument
            # draws, so what a division means here is not what it means
            # on the scope. Both are said rather than one of them being
            # quietly wrong.
            return _("%(vdiv)s/div   %(tdiv)s/div   (the instrument is "
                     "set to %(sdiv)s/div)") % got
        return _("%(vdiv)s/div   %(tdiv)s/div") % got

    def wave_summary(wave):
        """The line under the plot, in the user's language.

        Assembled here rather than in tds_wfm, which has no gettext and
        should not have one: a sentence built inside a module that knows
        nothing about languages cannot be translated by anybody.
        """
        got = wave.measures() if wave else None
        if not got:
            return _("%s: nothing") % (wave.source if wave else "")
        return _("%(points)d points over %(span)s, %(low)s to "
                 "%(high)s") % got

    def wave_name(one):
        """Which source in the two lists a trace stands for.

        Normally where it came from. A file loaded from the PC and held
        against a reference is different: its `source` is the file's own
        name - an ISF says "Ch1" whatever the file is called - and the
        thing it stands for in the lists is the reference it is going
        to. Keying off `source` alone left a staged reference drawn on
        the graticule with its button showing it was not.
        """
        return getattr(one, "label", None) or one.source

    def show_wave(wave, keep_view=False):
        """One waveform in the pane, on its own."""
        return show_waves([wave] if wave else [], keep_view)

    def show_waves(waves, keep_view=False):
        """Put traces in the pane, with a fresh view of them.

        The first is the primary one: it is what Save, Send and the
        per-division reading act on, the way a scope's readouts follow
        the selected channel.

        `keep_view` is for redrawing the same traces - a language
        change, say - where throwing the zoom away would be a surprise.
        """
        waves = [w for w in (waves or []) if w is not None]
        # One trace per source. Two copies of the same one is never
        # wanted and is hard to see on screen - the second is drawn
        # exactly on top of the first - but it doubles the work and it
        # puts two markers in the same place.
        seen, unique = set(), []
        for one in waves:
            if wave_name(one) not in seen:
                seen.add(wave_name(one))
                unique.append(one)
        waves = unique
        state["waves"] = waves
        # The buttons in the two lists follow what is drawn, plus
        # anything chosen that has nothing to draw yet - an empty
        # reference picked so a file can be loaded into it, which has no
        # samples and still has to be selectable.
        drawn = [wave_name(w) for w in waves]
        got = state.get("fetched") or {}
        state["wticked"] = wfm_order(
            drawn + [n for n in (state.get("wticked") or ())
                     if n not in drawn and got.get(n) is None])
        # Kept by name, so a trace taken off the graticule can be put
        # back without asking the instrument for it again.
        for one in waves:
            state.setdefault("fetched", {})[wave_name(one)] = one
        state["wave"] = waves[0] if waves else None
        if not keep_view or state.get("view") is None or not waves:
            # A fresh view for a fresh capture: the whole record at the
            # instrument's own scale, which is where every look at a
            # trace ought to start.
            state["view"] = tds_wfm.PlotView(waves) if waves else None
        else:
            # Same view, different traces. What "the whole record" means
            # has changed and has to be measured again, or the window
            # goes on describing a set that is no longer on the
            # graticule - which showed as a full strip above a plot with
            # a tenth of the trace on it.
            state["view"].remeasure(waves)
        state.pop("drag", None)
        draw_plot()
        show_ticks()
        wfm_buttons()
        first = state["wave"]
        if not first:
            winfo.config(text="")
        else:
            # Names and this program's own sentence, in this program's
            # language. It used to lead with the instrument's WFID -
            # "Ch1, DC coupling, 100.0mVolts/div, 500.0us/div, 500
            # points, Sample mode" - which is the instrument's words and
            # is therefore always English, and reads as though half the
            # line had simply not been translated. The WFID is still on
            # the saved picture and in the ISF header, which is where it
            # belongs.
            winfo.config(text=_("%(names)s  -  %(what)s")
                         % {"names": ", ".join(w.label or w.source
                                               for w in waves),
                            "what": wave_summary(first)})

    def ref_line(name):
        """One row of either list: the name, and what the name cannot say.

        Three things can be worth saying beside a source. A reference
        holding a file that has not been sent yet says so, because
        otherwise the pane would show a waveform under a name that means
        something quite different on the instrument. A source that has
        been chosen but never captured says to press Get waveform -
        choosing one no longer reads it, since a source read on its own
        would come from a different moment than everything else on the
        graticule. And a source that is switched off at the instrument
        says that instead, because no amount of pressing Get waveform
        will capture something that is not being acquired.
        """
        held = (state.get("staged") or {}).get(name)
        if held:
            return _("%(ref)s - %(file)s, not sent yet") % {
                "ref": name,
                "file": (held.origin or held.source or "")[:24]}
        if name not in (state.get("wticked") or ()):
            return name
        if (state.get("fetched") or {}).get(name) is not None:
            return name
        if not readable(name):
            return _("%s - switched off") % name
        return _("%s - press Get waveform") % name

    def show_sources(payload):
        """Every source the instrument has, greyed when it is not on.

        Showing only what is displayed hides the fact that a scope has
        four channels, and leaves no way to ask for the one that is
        switched off. Everything is listed; what cannot be read right
        now is greyed, and a double-click turns it on.

        Live channels and maths go in the top list, stored references in
        the bottom one. What is chosen stays chosen across a rebuild:
        the lists are rebuilt every time a source is switched on or off,
        and clearing the buttons each time would take the traces off the
        graticule for no reason the user can see.
        """
        every = payload.get("all") or []
        on = set(payload.get("sources") or [])
        names = [n for n, _o in every] if every and \
            isinstance(every[0], (list, tuple)) else list(every)
        state["wall"] = names
        state["wlive"] = [n for n in names if not n.startswith("REF")]
        refs = [n for n in names if n.startswith("REF")]
        if refs:
            state["wrefs"] = refs
        else:
            refs = list(state.get("wrefs") or tds_wfm.REFS)
        held = [n for n in (state.get("wticked") or ())
                if n in state["wlive"] or n in refs]
        if not held and state["wlive"]:
            # Nothing chosen yet, so the first source that can actually
            # be read - not simply the first one listed. Chosen, not
            # fetched: connecting to an instrument should not start
            # pulling waveforms off it uninvited.
            held = [next((n for n in state["wlive"] if n in on),
                         state["wlive"][0])]
        state["wticked"] = held
        fill_sources(wsrc, state["wlive"], on, ref_line)
        fill_sources(wref, refs, on, ref_line)
        wfm_buttons()

    def wfm_order(names):
        """These sources in the order the two lists show them."""
        live = state.get("wlive") or []
        refs = list(state.get("wrefs") or tds_wfm.REFS)
        want = set(names)
        rest = [n for n in names if n not in live and n not in refs]
        return ([n for n in live if n in want]
                + [n for n in refs if n in want] + rest)

    def show_ticks():
        """Bring the rows up to date with what is chosen, and with what
        they hold.

        Both follow the same state, so both are written here. Writing
        only the button left a captured source still saying "press Get
        waveform" beside a trace that was already on the graticule: the
        words are only rebuilt when the whole list is, and a capture
        does not rebuild the list.
        """
        chose = set(state.get("wticked") or ())
        for box in (wsrc, wref):
            for name in box.get_children():
                box.item(name, text=SHOWN if name in chose else HIDDEN,
                         values=(ref_line(name),))

    def fill_sources(box, names, on, describe=None):
        """Put these sources in a list, with their buttons set.

        The row keeps its name as its own id, so everything else can
        talk about sources rather than about row numbers.
        """
        box.delete(*box.get_children())
        chose = set(state.get("wticked") or ())
        for name in names:
            tags = () if name in on else ("off",)
            box.insert("", "end", iid=name, tags=tags,
                       text=SHOWN if name in chose else HIDDEN,
                       values=((describe or (lambda n: n))(name),))

    def on_click_row(box, evt):
        """A click on the button chooses that source, or lets it go.

        Nothing else in these lists responds to a click. The row
        highlight that used to be here was a second way of saying which
        source was meant, and the two disagreed: pressing a button left
        the highlight on the row above it, and Send, Load and Delete
        each acted on whichever of the two they happened to ask.
        """
        row = box.identify_row(evt.y)
        if row and box.identify_column(evt.x) == "#0":
            do_wfm_shown(row)
        return "break"

    def forget_source(name):
        """Drop a source's samples: it no longer exists on the scope.

        Off the graticule, out of what was kept, and its button let go.
        Leaving the trace drawn under the name of a reference that has
        just been deleted says the instrument still holds something it
        does not, and choosing it again would put it back from a copy of
        something gone.
        """
        state.get("fetched", {}).pop(name, None)
        state["wticked"] = [n for n in (state.get("wticked") or ())
                            if n != name]
        if name in [wave_name(x) for x in shown_waves()]:
            show_waves([x for x in shown_waves() if wave_name(x) != name],
                       keep_view=True)
        else:
            show_ticks()
            wfm_buttons()

    def do_wfm_shown(name):
        """Choose a source, or let it go: the button beside its name.

        Never goes near the instrument. A chosen source is drawn as soon
        as there are samples for it, and the samples come from Get
        waveform, which captures every live source from one acquisition.
        Fetching one source on its own would give a trace from a
        different moment than the ones already on the graticule, which
        is exactly what the capture exists to stop.

        So a source chosen with nothing captured for it is still chosen,
        and its row says to press Get waveform. The same goes for an
        empty reference waiting for a file to be loaded into it: this is
        a selection, not only a trace.
        """
        chose = list(state.get("wticked") or ())
        if name in chose:
            state["wticked"] = [n for n in chose if n != name]
            if name in [wave_name(x) for x in shown_waves()]:
                show_waves([x for x in shown_waves() if wave_name(x) != name],
                           keep_view=True)
            else:
                refresh_sources()
            ui.say(_("%s hidden") % name)
            return
        state["wticked"] = wfm_order(chose + [name])
        held = (state.get("staged") or {}).get(name)
        kept = held if held is not None else \
            (state.get("fetched") or {}).get(name)
        if kept is not None:
            show_waves(shown_waves() + [kept], keep_view=True)
            refresh_sources()
            ui.say(_("%s shown") % name)
            return
        refresh_sources()
        ui.say(_("%s has not been captured - press Get waveform") % name)

    def refresh_sources():
        """Redraw both lists from what is known, and re-decide the buttons.

        The rows carry more than the name - which source is chosen, what
        it holds, whether it is waiting to be captured - so anything
        that changes those has to redraw them.
        """
        show_sources({"all": state.get("wall") or [],
                      "sources": state.get("wsources") or []})

    def wfm_pick(*names):
        """Choose these sources, and let go of everything else.

        Everything that picks from code goes through here, so there is
        one place where the buttons, the graticule and the state behind
        them are set together. The graticule follows: a source that is
        chosen and has samples is drawn, and one that is not chosen is
        not - two answers to "which one" is the thing this whole
        arrangement exists to be rid of.
        """
        live = state.get("wlive") or []
        refs = list(state.get("wrefs") or tds_wfm.REFS)
        state["wticked"] = wfm_order([n for n in names
                                      if n in live or n in refs])
        want = state["wticked"]
        got = state.get("fetched") or {}
        staged = state.get("staged") or {}
        show_waves([w for w in (staged.get(n) or got.get(n) for n in want)
                    if w is not None], keep_view=True)
        # What was asked for, not what show_waves worked back out from
        # the traces: they agree, since the traces come from this list,
        # and saying so here keeps a caller's answer the caller's.
        state["wticked"] = want
        show_ticks()
        wfm_buttons()

    def wfm_picked_refs():
        """Which references are chosen, by name rather than by row."""
        refs = list(state.get("wrefs") or tds_wfm.REFS)
        chose = set(state.get("wticked") or ())
        return [n for n in refs if n in chose]

    def wfm_picked_live():
        """Which live channels are chosen, by name rather than by row."""
        live = state.get("wlive") or []
        chose = set(state.get("wticked") or ())
        return [n for n in live if n in chose]

    def wfm_picked_all():
        """Everything chosen, in either list, in the order listed.

        Several can be on the graticule at once, so Get waveform reads
        the lot. Live channels come first because that is the order the
        lists are in.
        """
        return wfm_picked_live() + wfm_picked_refs()

    def wfm_picked_ref():
        """The one reference the reference buttons act on."""
        refs = wfm_picked_refs()
        return refs[0] if refs else None

    def wfm_picked():
        """The one source the single-source buttons act on.

        Save, Send and Delete each mean one waveform, and with several
        chosen that is the first - the same one the readings under the
        plot describe.
        """
        chosen = wfm_picked_all()
        return chosen[0] if chosen else None

    def wfm_send_wave():
        """The waveform Send to instrument would put back.

        The chosen live channel's, so that pressing a channel's button
        and then Send sends that channel. Only when no live channel is
        chosen does it fall back to whatever is at the front of the
        plot, which is what a chosen reference gives.
        """
        got = state.get("fetched") or {}
        staged = state.get("staged") or {}
        for name in wfm_picked_live() + wfm_picked_refs():
            if staged.get(name) is not None:
                return staged[name]
            if got.get(name) is not None:
                return got[name]
        return None

    def wfm_buttons(_evt=None):
        """Enable what the current selection can actually do.

        Delete and Load only mean something for a stored reference: a
        channel and a maths trace are computed live, there is nothing
        stored to remove, and a file loaded into one would have nowhere
        to go. Offering either and refusing afterwards is a button that
        lies about what it does.
        """
        if state.get("busy"):
            # Everything is disabled while the instrument is being
            # talked to, and this must not quietly hand one back.
            return
        waiting = staged_picked()
        chosen_ref = wfm_picked_ref()
        btn_wdel.config(state="normal" if chosen_ref else "disabled")
        # A file is loaded into a reference, so there has to be one
        # chosen before there is anywhere to put it.
        btn_wload.config(state="normal" if chosen_ref else "disabled")
        # Send means two different things and says which: with staged
        # references chosen it sends those, and otherwise it opens the
        # dialog for the chosen source. With nothing chosen that has
        # samples it can do neither.
        if waiting:
            btn_wsend.config(text=_("Send %d to instrument")
                             % len(waiting), state="normal")
        else:
            btn_wsend.config(
                text=_("Send to instrument..."),
                state="normal" if wfm_send_wave() is not None
                else "disabled")

    def wfm_source_state(name):
        """Is this source displayed on the instrument right now?"""
        return name in (state.get("wsources") or [])

    def do_wfm_toggle(name=None):
        """Double-click: turn a source on or off on the instrument.

        A channel that is not displayed cannot be read - the instrument
        answers 2241, "waveform requested is invalid", and there is no
        way to reach the data without bringing it up. So the way to
        download a switched-off channel is to switch it on, and this is
        that.

        The row that was double-clicked, not the chosen one: with the
        row highlight gone there is nothing to say a double-click on the
        fourth row meant the first.
        """
        if state["busy"]:
            return
        name = name or wfm_picked()
        if not name:
            return
        if name in (state.get("staged") or {}):
            messagebox.showinfo(
                _("Not on the instrument yet"),
                _("%s holds a waveform loaded from this computer that has "
                  "not been sent. Press Send to instrument first.") % name)
            return
        want = not wfm_source_state(name)
        ui.busy(True, "wait")
        ui.say(_("Turning %(name)s %(state)s on the instrument ...")
               % {"name": name, "state": _("on") if want else _("off")})
        w.submit("wfm_select",
                 lambda k, n=name, v=want: k.wfm_select(n, v))

    def do_wfm_sources():
        """Ask which sources are displayed. Only those can be read."""
        if state["busy"]:
            return
        ui.busy(True, "wait")
        ui.say(_("Looking for waveforms ..."))
        w.submit("wfm_sources", lambda k: k.wfm_sources())

    def readable(source):
        """Can this source be read at all right now?

        A channel or a maths trace that is not displayed cannot: the
        instrument answers every one of the eleven preamble fields with
        2241 and its twenty-deep event queue overflows, which then
        surfaces at the *next* connection as a page of errors nobody
        caused. A reference is different - it reads whether it is
        displayed or not, because the data is already there.
        """
        return (wfm_source_state(source)
                or str(source).upper().startswith("REF"))

    def live_sources():
        """Every live source the instrument is displaying, in list order.

        Channels and maths, never references. These are what one press
        of Get waveform captures, whatever is chosen: the point of the
        capture is that it is the instrument's whole state at one
        moment, and leaving a channel out of it would make the traces
        that were included no more comparable than before.
        """
        on = state.get("wsources") or []
        return [n for n in (state.get("wlive") or []) if n in on]

    def do_wfm_get(_evt=None):
        """Capture the instrument: every live source, from one acquisition.

        Acquisition is stopped first, every displayed channel and maths
        trace is read off the same frozen capture, and then it is let go
        again - so what arrives is one moment seen on several sources
        rather than several moments read one after another. See
        TdsWfm.capture for what that is worth and what it refuses to do.

        Chosen references come too, read after the acquisition is
        released: they are not moving, so they gain nothing from the
        freeze and there is no reason to hold the instrument still for
        them.

        What is captured and what is *shown* are two different
        questions. This answers the first; the buttons beside the names
        answer the second, and having captured the lot they answer it
        without going near the instrument again.
        """
        if state["busy"]:
            return
        live = live_sources()
        refs = [n for n in wfm_picked_refs()
                if n not in (state.get("staged") or {})]
        if not live and not refs:
            messagebox.showinfo(
                _("Nothing to capture"),
                _("No channel is displayed on the instrument and no "
                  "stored waveform is chosen, so there is nothing to "
                  "read.\n\nDouble-click a name to switch that source on "
                  "at the instrument."))
            return
        # Only what was chosen and could not be had. Naming every
        # switched-off channel on every capture would put a line of
        # apology under the plot for things nobody asked for.
        held = state.get("staged") or {}
        state["wskipped"] = [n for n in wfm_picked_all()
                             if n not in live and n not in refs
                             and n not in held]
        ui.busy(True, "steps" if len(live) + len(refs) > 1 else "wait")
        ui.say(_("Capturing %s ...") % ", ".join(live + refs))
        w.submit("wfm_get",
                 lambda k, a=list(live), b=list(refs): k.wfm_get(a, b))

    def wfm_bytes(wave, ext):
        """The file this waveform makes, chosen by its extension."""
        if ext == ".isf":
            return wave.to_isf()
        if ext in (".tdw", ".wfm"):
            # The instrument's own layout either way: .tdw is this
            # program's extension for it and .wfm is the instrument's,
            # and a file written under either can be copied to a
            # scope's disk and recalled. Eight-bit captures are widened
            # on the way out, because the format holds two bytes a
            # sample.
            return wave.to_wfm()
        if ext == ".png":
            # What the graticule is showing now, zoom and all, with the
            # record strip above it - the same picture the window has,
            # which is what somebody who has just spent a minute
            # setting up a view means by saving it.
            return wave.to_png(width=state["pngsize"][0],
                               height=state["pngsize"][1],
                               colours=plot_colours(wave),
                               caption=wave_scales(wave),
                               view=state.get("view"), strip=True)
        if ext == ".svg":
            # The same picture as the PNG, drawn as vector so it stays
            # crisp at any size. The size still sets the aspect and the
            # line spacing; SVG then scales it without pixelating.
            return wave.to_svg(width=state["pngsize"][0],
                               height=state["pngsize"][1],
                               colours=plot_colours(wave),
                               caption=wave_scales(wave),
                               view=state.get("view"), strip=True)
        return wave.to_csv()

    def do_wfm_save():
        """Save the held trace, in whichever format is asked for.

        The format follows the extension chosen in the dialog, so one
        capture can be saved as data, as an archive, or as a picture
        without fetching it again - which is the point of keeping the raw
        curve rather than a converted copy.

        A `.wfm` is the instrument's own format: copy one to a disk and
        the scope will recall it into a reference, which none of the
        other three will do.
        """
        wave = state.get("wave")
        if not wave:
            messagebox.showinfo(_("Nothing to save"),
                                _("Fetch a waveform first."))
            return
        kinds = [(_("Spreadsheet, scaled values (*.csv)"), "*.csv"),
                 (_("Instrument format, exact (*.isf)"), "*.isf"),
                 (_("Waveform file (*.tdw)"), "*.tdw"),
                 (_("Instrument waveform (*.wfm)"), "*.wfm"),
                 (_("Picture of the trace (*.png)"), "*.png"),
                 (_("Scalable picture (*.svg)"), "*.svg")]
        dest = filedialog.asksaveasfilename(
            title=_("Save waveform"), defaultextension=".csv",
            initialfile=stamped(wave.source or "waveform", ".csv"),
            filetypes=kinds)
        if not dest:
            return
        ext = os.path.splitext(dest)[1].lower()
        try:
            data = wfm_bytes(wave, ext)
            with open(dest, "wb") as fh:
                fh.write(data)
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                     % (_("Could not save"), exc))
            return
        ui.say(_("Saved %s (%s bytes)")
               % (os.path.basename(dest), format(len(data), ",")))

    def do_wfm_load():
        """Load a waveform from the PC into the pane.

        The same formats this program writes, and the instrument's own
        `.wfm` besides. An `.isf` comes back byte for byte - it carries
        the instrument's own curve and preamble - so what is loaded can
        be sent to an instrument and be exactly what was captured. A
        `.csv` carries scaled values rather than the raw integers, so it
        is re-quantised on the way in; it plots and sends correctly, to
        within one step of the instrument's eight-bit range. A `.wfm`
        carries sixteen-bit samples and their scaling, and comes back
        with the voltages it went out with.

        What is loaded is held against whichever references are chosen
        - several, if several are - and nothing is written to the
        instrument until Send is pressed. That way a set of references
        can be filled in one go, and a file can be looked at without
        committing it to anything. With no reference chosen there is
        nowhere for it to go, which is why the button is greyed until
        there is one.
        """
        if state["busy"]:
            return
        if not wfm_picked_refs():
            messagebox.showinfo(
                _("Select a reference"),
                _("Choose the reference the waveform is to be loaded into, "
                  "using the button beside its name."))
            return
        path = filedialog.askopenfilename(
            parent=root, title=_("Load waveform"),
            filetypes=[(_("Waveform files (*.isf *.csv *.tdw *.wfm)"),
                        "*.isf *.csv *.tdw *.wfm"),
                       (_("Instrument format, exact (*.isf)"), "*.isf"),
                       (_("Spreadsheet, scaled values (*.csv)"), "*.csv"),
                       (_("Waveform file (*.tdw)"), "*.tdw"),
                       (_("The instrument's own file (*.wfm)"), "*.wfm"),
                       (_("All files"), "*.*")])
        if not path:
            return
        try:
            wave = tds_wfm.load(path)
        except Exception as exc:
            messagebox.showerror(
                _("Error"),
                _("%(name)s could not be read as a waveform.\n\n%(why)s")
                % {"name": os.path.basename(path),
                   "why": localise(str(exc))})
            return
        slots = wfm_picked_refs()
        staged = state.setdefault("staged", {})
        for slot in slots:
            # A copy each, because each one is labelled with the
            # reference it is going to and they are going to different
            # ones. The samples are shared; only the label differs.
            held = copy.copy(wave)
            held.label = slot
            held.origin = os.path.basename(path)
            staged[slot] = held
        wave.label = slots[0]
        show_sources({"all": state.get("wall") or [],
                      "sources": state.get("wsources") or []})
        # Beside whatever else is on the graticule, not in place of it:
        # loading a file into a reference is not a reason to take the
        # channel it is going to be compared against off the screen.
        keep = [x for x in shown_waves() if wave_name(x) not in slots]
        show_waves([staged[s] for s in slots] + keep)
        ui.say(_("Loaded %(name)s into %(where)s - not sent yet: %(what)s")
               % {"name": os.path.basename(path), "where": ", ".join(slots),
                  "what": wave_summary(wave)})

    def do_wfm_delete():
        """Delete every chosen reference on the instrument, after asking.

        All of them, not the first: the list lets several be picked and
        clearing four references one confirmation at a time is four
        times the same question.

        A reference holding a file that has not been sent yet is a
        different matter: there is nothing on the instrument to delete,
        so it is simply put down again, and the instrument is not
        touched at all. One selection can hold some of each.
        """
        if state["busy"]:
            return
        picked = wfm_picked_refs()
        staged = state.get("staged") or {}
        down = [n for n in picked if n in staged]
        for name in down:
            del staged[name]
            # Only that trace comes off, not the pane: a file put down
            # is no reason to lose the channel it was being compared
            # against.
            forget_source(name)
        if down:
            show_sources({"all": state.get("wall") or [],
                          "sources": state.get("wsources") or []})
            ui.say(_("%s put down - it was never sent, so nothing on the "
                     "instrument changed") % ", ".join(down))
        gone = [n for n in picked
                if n not in down and n.upper().startswith("REF")]
        if not gone:
            if not down:
                messagebox.showinfo(
                    _("Select a reference"),
                    _("Only a stored reference can be deleted. Channels "
                      "and maths are live and there is nothing stored to "
                      "remove."))
            return
        if not messagebox.askyesno(
                _("Delete waveform"),
                _("Delete %s on the instrument?\n\n"
                  "This operation cannot be undone.\n"
                  "Download and save it first if it is needed.")
                % ", ".join(gone),
                default="no", parent=root):
            return
        ui.busy(True, "wait")
        ui.say(_("Deleting %s on the instrument ...") % ", ".join(gone))
        w.submit("wfm_delete", lambda k, ns=list(gone): k.wfm_delete(ns))

    def wfm_disk_folder():
        """Where a waveform sent to the disk goes, or "" if nowhere.

        The drive the Files tab is on, not the folder: see
        Worker.WFM_DIR.
        """
        drive = (state.get("cwd") or "").split("/")[0]
        return "%s/%s" % (drive, Worker.WFM_DIR) if drive else ""

    def wfm_destinations():
        """Where this instrument will accept a waveform.

        A reference always; the disk only where the firmware has
        WRITEFILE, which the A and B series do not.

        One file format. A .WFM is what the instrument itself recalls
        from a disk; an .ISF written there is a file the instrument can
        do nothing with, and putting one on its disk to fetch back to a
        PC is a round trip through the slowest link in the room when
        Save waveform writes the same file straight to disk.
        """
        out = [(ref, _("Reference memory %s") % ref)
               for ref in (state.get("wrefs") or tds_wfm.REFS)]
        if (state.get("cannot") is not None
                and ui.by_name["Upload"] not in state["cannot"]
                and state.get("cwd")):
            # The folder is named, because nothing else in this dialog
            # says where the file lands. A file written somewhere the
            # user was not thinking of is a file they report as never
            # written.
            out.append(("FILE",
                        _("A .WFM in %s, which the instrument can recall")
                        % wfm_disk_folder()))
        return out

    def staged_picked():
        """Which staged references the Send button would act on.

        The chosen ones, and only those. Sending everything that
        happens to be staged means a reference filled ten minutes ago
        and forgotten about goes to the instrument along with the one
        that was meant - so what is chosen is what is sent.
        """
        staged = state.get("staged") or {}
        return [n for n in wfm_picked_refs() if n in staged]

    def do_wfm_send_staged():
        """Send the selected staged references to the instrument.

        One worker job for the lot, so the instrument is asked for once
        and each write is read back and compared before the next one
        starts - the same proof a single send gets.
        """
        staged = dict(state.get("staged") or {})
        slots = sorted(staged_picked())
        if not slots:
            return
        if not messagebox.askyesno(
                _("Send to instrument"),
                _("Send these waveforms to the instrument?\n\n%s\n\n"
                  "Whatever those references hold now is replaced.")
                % "\n".join(_("%(ref)s  <-  %(file)s")
                            % {"ref": s,
                               "file": (staged[s].origin
                                        or staged[s].source or "")}
                            for s in slots),
                parent=root, default="no"):
            return
        live = [s for s in state.get("wsources") or []
                if not s.startswith("REF")]
        items = [(s, staged[s]) for s in slots]
        ui.busy(True, "steps")
        ui.say(_("Sending %d waveform(s) to the instrument ...") % len(items))
        w.submit("wfm_send_many",
                 lambda k, it=items, a=(live[0] if live else None):
                 k.wfm_send_many(it, a))

    def wfm_send_file(wave):
        """Write the held trace to the instrument's disk as a .WFM.

        Into WAVEFORM at the root of the drive, made if it is not
        there, under a serialised name - the worker picks the name,
        since only it can see what the folder already holds. The
        folder is said here: this reports through the Files tab, and
        whoever pressed Send is looking at the Waveforms tab, so
        without it a write that worked looks like nothing happening.
        """
        folder = wfm_disk_folder()
        if not folder:
            messagebox.showinfo(
                _("Nowhere to put it"),
                _("Open the Files tab and let it list a drive first."))
            return None
        try:
            data = wave.to_wfm()
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                     % (_("Could not send"), exc))
            return None
        ui.busy(True, "steps")
        ui.say(_("Sending a waveform to %s ...") % folder)
        w.submit("upload", lambda k, d=data, v=folder.split("/")[0],
                 s=wave.source: k.wfm_to_disk(d, v, s))
        return folder

    def do_wfm_send():
        """Put the held trace back into the instrument.

        With files staged against references there is nothing to choose:
        each one goes to the reference it was loaded into, and the only
        question is whether to go ahead. Otherwise it is the chosen
        source that is being sent - the channel whose button is pressed
        in the live list - and where it goes is the question.
        """
        if state["busy"]:
            return
        if staged_picked():
            do_wfm_send_staged()
            return
        wave = wfm_send_wave()
        if not wave:
            messagebox.showinfo(
                _("Nothing to send"),
                _("Choose a source with the button beside its name and "
                  "fetch it first."))
            return
        choices = wfm_destinations()
        dlg, pad = popup(_("Send to instrument"))
        ttk.Label(pad, text=_("Send %s to:") % (wave.wfid or wave.source),
                  wraplength=460).pack(anchor="w", pady=(0, 8))
        pick = tk.StringVar(value=choices[0][0])
        for key, label in choices:
            ttk.Radiobutton(pad, text=label, value=key,
                            variable=pick).pack(anchor="w")
        note = ttk.Label(pad, wraplength=460, foreground="#555", text=_(
            "A reference that is empty has to be brought into being from a "
            "live channel first; this is done for you, and overwrites "
            "nothing else."))
        note.pack(fill="x", pady=(8, 0))

        def go():
            if state["busy"]:
                return
            key = pick.get()
            dlg.destroy()
            if key == "FILE":
                wfm_send_file(wave)
                return
            live = [s for s in state.get("wsources") or []
                    if not s.startswith("REF")]
            ui.busy(True, "wait")
            ui.say(_("Sending to %s ...") % key)
            first = live[0] if live else None
            w.submit("wfm_send",
                     lambda k, d=key, a=first: k.wfm_send(wave, d, a))

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

    # The pump's handlers for this tab's jobs - see pump_jobs.
    def pump_wfm_sources(payload):
        ui.busy(False)
        found = payload["sources"]
        state["wsources"] = found
        state["wrefs"] = payload.get("refs") or list(tds_wfm.REFS)
        if payload.get("all"):
            # Held rather than written. The sources are
            # re-read after every capture, every send and
            # every toggle, and a third of a 5000-line log
            # was once this one line repeated with the same
            # answer. What matters is which channels were
            # up when something went wrong, so the latest
            # answer stands and goes out with a fault if
            # there is one.
            log_context("waveform sources",
                        "instrument reports %s; displayed: %s"
                        % (", ".join(payload["all"]),
                           ", ".join(found) or "none"))
        if payload.get("colours"):
            state["icolours"] = payload["colours"]
        show_sources(payload)
        ui.lim_scan()
        if found:
            ui.say(_("%d waveform source(s) on the instrument")
                   % len(found))
        else:
            ui.say(_("Nothing is displayed - double-click a "
                     "source to turn it on"))

    def pump_wfm_select(payload):
        ui.busy(False)
        do_wfm_sources()

    def pump_wfm_delete(payload):
        ui.busy(False)
        gone = ", ".join(payload["names"])
        for one in payload["names"]:
            forget_source(one)
        ui.say(_("%s has been deleted on the instrument") % gone)
        do_wfm_sources()

    def pump_wfm_get(payload):
        ui.busy(False)
        waves = payload["waves"]
        # Everything captured is kept, whether it is on the
        # graticule or not: the capture is the instrument's
        # whole state at one moment, and a source turned on
        # afterwards should come from that moment too rather
        # than send us back to the bus for a different one.
        for one in waves:
            state.setdefault("fetched", {})[wave_name(one)] = one
        # Nothing chosen yet, so show what was captured -
        # otherwise a first press of Get waveform reads the
        # instrument and appears to do nothing.
        if not wfm_picked_all():
            state["wticked"] = wfm_order(
                [wave_name(x) for x in waves])
        wfm_pick(*(state.get("wticked") or ()))
        # A capture is presented at the instrument's own
        # horizontal scale: the middle of the record at the
        # scope's points-a-division, so what the app shows
        # first matches what was on the scope's screen rather
        # than the whole record squeezed onto ten divisions.
        # A record no larger than the screen is shown whole.
        if state.get("view") is not None and state.get("wave"):
            state["view"].to_scope(state["wave"])
            draw_plot()
        missed = [s for s, _why in payload["refused"]]
        missed += list(state.pop("wskipped", None) or [])
        how = payload.get("how") or {}
        state["wacq"] = how
        if how.get("froze"):
            held = _("acquisition held for the capture, then "
                     "released")
        elif how.get("sequence"):
            held = _("left running - it is set to single "
                     "sequence and stopping it would re-arm "
                     "the trigger")
        else:
            held = _("the instrument was already stopped")
        ui.say(_("Captured %(name)s in %(secs).2f s - %(how)s")
               % {"name": ", ".join(wave_name(x) for x in waves),
                  "secs": payload["secs"], "how": held}
               + (_("  -  %s could not be read, being switched "
                    "off on the instrument") % ", ".join(missed)
                  if missed else ""))

    def pump_wfm_send(payload):
        ui.busy(False)
        ui.say(_("Sent %d points to %s, read back and verified")
               % (payload["points"], payload["dest"]))
        do_wfm_sources()

    def pump_wfm_send_many(payload):
        ui.busy(False)
        sent = payload["sent"]
        # Put down what landed. A job that failed part way
        # raises instead of returning, so nothing is put
        # down and pressing Send again sends the lot -
        # writing a reference twice costs a second and
        # leaves it holding the same samples.
        for out in sent:
            state.get("staged", {}).pop(out["dest"], None)
        ui.say(_("Sent %(count)d waveform(s) to %(where)s, read "
                 "back and verified")
               % {"count": len(sent),
                  "where": ", ".join(o["dest"] for o in sent)})
        do_wfm_sources()

    vars(ui).update((k, v) for k, v in locals().items() if k != "ui")
