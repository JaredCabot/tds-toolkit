"""The Screenshot tab.

Built by run_gui through build(ui). `ui` carries the window's shared
names - state, the worker, busy, say and the rest - and is handed back
everything built here, so run_gui and the tests can reach it.
"""
import base64
import os

import tds_scr
from i18n import gettext as _
from tdstoolkit import log_context, stamped


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

    # There is no "give me the display" command on these instruments.
    # What there is, is a hardcopy subsystem meant for a printer, which
    # will send to the bus if asked. So a screenshot is a print job
    # aimed at this program. Which format to ask for was measured on
    # three instruments rather than guessed - see tds_scr.py.
    scrtab = ttk.Frame(tabs)
    tabs.add(scrtab, text=_("Screenshot"))
    named(scrtab, "Screenshot")

    stop_ = ttk.Frame(scrtab)
    stop_.pack(fill="x", padx=2, pady=(6, 2))
    btn_sget = ttk.Button(stop_, text=_("Take screenshot"), padding=(10, 2),
                          command=lambda: do_scr_get())
    btn_sget.pack(side="left")
    btn_ssave = ttk.Button(stop_, text=_("Save image..."), padding=(10, 2),
                           command=lambda: do_scr_save())
    btn_ssave.pack(side="left", padx=4)
    btn_sscan = ttk.Button(stop_, text=_("Refresh"), padding=(10, 2),
                           command=lambda: do_scr_options())
    btn_sscan.pack(side="right")

    sbody = ttk.Frame(scrtab)
    sbody.pack(fill="both", expand=True, padx=2, pady=4)

    sopts = ttk.Frame(sbody)
    sopts.pack(side="left", fill="y", padx=(0, 8))
    lbl_sfmt = ttk.Label(sopts, text=_("Format"))
    says(lbl_sfmt, "Format")
    lbl_sfmt.grid(row=0, column=0, sticky="w", pady=(0, 2))
    scr_fmt = ttk.Combobox(sopts, width=38, state="readonly")
    scr_fmt.grid(row=1, column=0, sticky="w", pady=(0, 10))
    lbl_slay = ttk.Label(sopts, text=_("Layout"))
    says(lbl_slay, "Layout")
    lbl_slay.grid(row=2, column=0, sticky="w", pady=(0, 2))
    scr_lay = ttk.Combobox(sopts, width=38, state="readonly")
    scr_lay.grid(row=3, column=0, sticky="w", pady=(0, 10))
    lbl_spal = ttk.Label(sopts, text=_("Palette"))
    says(lbl_spal, "Palette")
    lbl_spal.grid(row=4, column=0, sticky="w", pady=(0, 2))
    # Wide enough for the longest line any of them offers - measured,
    # not guessed: "Hardcopy - light background, for printing" is 40
    # characters and was cut off at 30.
    scr_pal = ttk.Combobox(sopts, width=38, state="readonly")
    scr_pal.grid(row=5, column=0, sticky="w", pady=(0, 10))
    # Normal and Inverted are done here rather than on the instrument,
    # so the picture can change the moment it is chosen instead of
    # waiting for another five-second capture.
    scr_pal.bind("<<ComboboxSelected>>", lambda e: repaint_shot())
    # Layout too: it turns the picture already in hand rather than
    # waiting for the next capture, which is the same reasoning.
    scr_lay.bind("<<ComboboxSelected>>", lambda e: repaint_shot())
    lbl_snote = ttk.Label(sopts, foreground="#555", wraplength=210,
                          justify="left")
    says(lbl_snote, "The instrument's own hardcopy settings are read "
                    "before each shot and put back afterwards.")
    lbl_snote.grid(row=6, column=0, sticky="w", pady=(6, 0))

    sview = ttk.Frame(sbody)
    sview.pack(side="left", fill="both", expand=True)
    # No scrollbars: a scope screen is 640 x 480 (or turned, 480 x 640) and
    # the pane is larger than that at any normal window size, so the picture
    # is shown whole and centred rather than offered scrollbars that scroll
    # nowhere. draw_shot centres it; if the window is dragged smaller than
    # the picture the edges simply clip, which is nicer than a scrollbar.
    shot = tk.Canvas(sview, background="#3a3a3a", highlightthickness=1,
                     highlightbackground=EDGE)
    shot.pack(side="left", fill="both", expand=True)
    sinfo = ttk.Label(scrtab, text="", anchor="w")
    sinfo.pack(fill="x", padx=2, pady=(0, 2))

    def draw_shot():
        """Put the captured screen on the canvas at one pixel to one.

        Tk 8.6 reads PNG itself, so the image goes in as the same bytes
        the Save button would write - no scaling, no resampling, and no
        second code path that could disagree with the file.
        """
        shot.delete("all")
        screen = state.get("screen")
        if not screen:
            state["shotimg"] = None
            shot.config(scrollregion=(0, 0, 0, 0))
            shot.create_text(max(shot.winfo_width(), 60) / 2,
                             max(shot.winfo_height(), 40) / 2,
                             fill="#dddddd",
                             text=_("No screenshot yet. Press Take "
                                    "screenshot."))
            return
        state["shotimg"] = tk.PhotoImage(
            data=base64.b64encode(state["shotpng"]).decode("ascii"))
        # Centred when there is room, top-left when there is not, and the
        # scroll region never smaller than the canvas - otherwise a
        # 640-wide picture in a 770-wide pane sits against the left edge
        # with a scrollbar offering to scroll nowhere.
        pane_w = max(shot.winfo_width(), 1)
        pane_h = max(shot.winfo_height(), 1)
        x = max(0, (pane_w - screen.width) // 2)
        y = max(0, (pane_h - screen.height) // 2)
        shot.create_image(x, y, image=state["shotimg"], anchor="nw")
        shot.config(scrollregion=(0, 0, max(pane_w, screen.width + 2 * x),
                                  max(pane_h, screen.height + 2 * y)))

    def repaint_shot():
        """Re-render the shot already in hand under the current settings.

        The palette choice is only this program's where the instrument
        has no HARDCOPY:PALETTE of its own - there it costs a palette
        flip. Where the instrument does have one, the choice is the
        instrument's and only a new capture can honour it, so the
        picture is left alone and the choice applies to the next shot.

        Layout is always this program's to honour, because turning a
        picture on its side is a transpose and asking the instrument for
        it again is five seconds. The pixels are the same pixels either
        way.
        """
        wanted = ui.chosen(scr_pal, state.get("spkeys") or [])
        state["sinvert"] = (wanted == "INVERT")
        base = state.get("sraw")
        if base is None:
            return
        want_inverted = state["sinvert"]
        turns = wanted_turns(base)
        if (want_inverted == state.get("sshown")
                and turns == state.get("sturned")):
            return
        state["sturned"] = turns
        shown = base.inverted() if want_inverted else base
        show_shot(shown.turned(turns) if turns else shown,
                  state.get("ssecs", 0.0))

    def wanted_turns(screen):
        """How many quarter turns from what arrived to what is asked for.

        Portrait and landscape are one quarter apart, so this is one
        quarter or none - but which quarter is not the same both ways,
        and it is worked out from what the capture actually was rather
        than assumed, because an instrument found set to landscape
        hands over a landscape picture to begin with.

        A portrait capture turns anticlockwise into landscape: measured
        on a 784D, that is where "Tek Run:" ends up down the left-hand
        edge reading upwards. Going the other way is the inverse, three
        quarters. Turning that one quarter as well lands the picture
        upside down, which is what a landscape capture asked for
        portrait used to do.
        """
        asked = ui.chosen(scr_lay, state.get("slkeys") or []) or ""
        was = (getattr(screen, "layout", "") or "").upper()
        if not was or not asked or asked == was:
            return 0
        return 1 if was.startswith("PORT") else 3

    def show_shot(screen, secs=0.0):
        state["screen"] = screen
        state["sshown"] = bool(state.get("sinvert")) if screen else None
        if screen is None:
            state["sraw"] = None
            state["sturned"] = 0
        state["shotpng"] = screen.to_png() if screen else None
        draw_shot()
        if screen:
            sinfo.config(text=_("%(w)d x %(h)d, %(c)d colours, %(f)s, "
                                "%(k)s bytes from the instrument in "
                                "%(s).1f s")
                         % {"w": screen.width, "h": screen.height,
                            "c": screen.colours, "f": screen.keyword,
                            "k": format(len(screen.raw), ","),
                            "s": secs})
        else:
            sinfo.config(text="")

    # Redrawn on resize whether or not there is a picture: the empty
    # message and the centring both depend on the size of the pane.
    shot.bind("<Configure>", lambda e: draw_shot())

    def scr_layout_labels():
        """Layout, named by what it does rather than by what Tektronix
        calls it. PORTRAIT is the page's orientation, not the image's -
        it is the one that gives the screen the right way up, at
        640x480. LANDSCAPE turns it on its side. Measured on a 640A,
        which arrived set to LANDSCAPE and produced a 480x640 image
        lying on its side."""
        return [(_("Portrait - screen upright, 640 x 480"), "PORTRAIT"),
                (_("Landscape - screen turned 90 degrees"), "LANDSCAPE")]

    def scr_palette_labels(has_palette=True):
        """What the Palette box offers.

        A colour instrument has HARDCOPY:PALETTE and can be asked for
        either. A monochrome one has no such setting - so rather than
        greying the box, the same choice is offered and the inversion is
        done here, on the palette of the image that comes back. That
        costs one subtraction per palette entry instead of another
        five-second capture.
        """
        if has_palette:
            return [(_("Current - the screen's own colours"), "CURRENT"),
                    (_("Hardcopy - light background, for printing"),
                     "HARDCOPY")]
        # Which way round these go was measured, not assumed. What a
        # monochrome instrument sends is a print job: dark ink on white
        # paper. So the image as it arrives is the printing one, and
        # turning it inside out is what gives the white-on-black the
        # screen actually shows.
        return [(_("Normal - dark on light, for printing"), "NORMAL"),
                (_("Inverted - as the screen shows it"), "INVERT")]

    def do_scr_options():
        """Ask the instrument which formats it has and whether it has
        colour. Cheap - it sets each format and reads it back, and
        transfers nothing."""
        if state["busy"]:
            return
        ui.busy(True, "wait")
        ui.say(_("Asking what the instrument can print ..."))
        w.submit("scr_options", lambda k: k.scr_options())

    def scr_format_labels():
        """What the Format box offers, in the current language.

        Built from what the instrument said it has, which is remembered,
        so this can be asked again after a language change without going
        back to the bus.

        Which is quickest in each class was measured on the bench rather
        than reasoned about: the time is almost all the instrument
        drawing the picture, so the smallest file is not the fastest.
        """
        return [(_(f["label"]) + (_("  - quickest")
                                  if f.get("quickest") else ""),
                 f["keyword"]) for f in (state.get("sformats") or [])]

    def show_scr_options(payload):
        formats = payload.get("formats") or []
        state["sformats"] = formats
        state["sfkeys"] = ui.pick_from(scr_fmt, scr_format_labels(),
                                       payload.get("best"))
        settings = payload.get("settings") or {}
        state["slkeys"] = ui.pick_from(scr_lay, scr_layout_labels(),
                                       "PORTRAIT")
        has_palette = bool(payload.get("palette"))
        state["spalette"] = has_palette
        state["spkeys"] = ui.pick_from(
            scr_pal, scr_palette_labels(has_palette),
            settings.get("PALETTE", "CURRENT") if has_palette else "NORMAL")
        # Enabled either way now: a monochrome instrument has no
        # HARDCOPY:PALETTE, so the inversion is done here instead.
        scr_pal.config(state="readonly")
        lbl_spal.config(foreground="")
        if not formats:
            ui.say(_("This instrument offers no image format this program "
                     "can read."))
        if not formats:
            ui.unavailable(btn_sget)
        btn_sget.config(state="normal" if formats else "disabled")

    def do_scr_get():
        if state["busy"]:
            return
        if not state.get("sformats"):
            messagebox.showinfo(
                _("No image format"),
                _("This instrument's hardcopy formats are all printer "
                  "languages, none of which is an image this program can "
                  "read."))
            return
        keyword = ui.chosen(scr_fmt, state.get("sfkeys") or [])
        layout = ui.chosen(scr_lay, state.get("slkeys") or [])
        wanted = ui.chosen(scr_pal, state.get("spkeys") or [])
        # CURRENT and HARDCOPY are the instrument's own. NORMAL and
        # INVERT are this program's, for an instrument that has no
        # palette setting at all.
        palette = wanted if wanted in tds_scr.PALETTES else None
        state["sinvert"] = (wanted == "INVERT")
        ui.busy(True, "wait")
        ui.progress(None)
        ui.say(_("Taking a screenshot in %s ...") % (keyword or "?"))
        w.submit("scr_get",
                 lambda k, f=keyword, y=layout, p=palette: k.scr_get(f, y, p))

    def do_scr_save():
        """Save the shot, as the pane is showing it.

        The instrument's own format is offered first, because what
        arrived is a perfectly good .bmp and somebody will want that
        rather than a conversion. Untouched it is written back byte for
        byte; turned or inverted here it is encoded again in the same
        family, since the file has to be the picture that was chosen.
        """
        screen = state.get("screen")
        if not screen:
            messagebox.showinfo(_("Nothing to save"),
                                _("Take a screenshot first."))
            return
        # The format it came back in leads, since that is the file the
        # instrument actually produced and converting it is a choice
        # rather than a default. PNG is offered second. Windows picks
        # the extension from whichever line of the dropdown is chosen,
        # which is the convention here.
        native = screen.suffix
        native_name = {".bmp": _("Windows bitmap (*.bmp)"),
                       ".pcx": _("ZSoft PCX image (*.pcx)"),
                       ".tif": _("TIFF image (*.tif)")}.get(
                           native, _("As received from the instrument"))
        types = [(native_name, "*" + native),
                 (_("PNG image (*.png)"), "*.png")]
        path = filedialog.asksaveasfilename(
            parent=root, title=_("Save screenshot"),
            defaultextension=native,
            initialfile=stamped("screen", native), filetypes=types)
        if not path:
            return
        try:
            # Either way the file is the picture on the screen, turned
            # and inverted as the pane shows it. A shot nothing has been
            # done to still saves as the bytes the instrument sent.
            as_png = path.lower().endswith(".png")
            data = state["shotpng"] if as_png else screen.to_native()
            with open(path, "wb") as fh:
                fh.write(data)
        except Exception as exc:
            messagebox.showerror(_("Error"), "%s\n\n%s"
                                     % (_("Could not save"), exc))
            return
        ui.say(_("Saved %s (%s bytes)") % (os.path.basename(path),
                                           format(len(data), ",")))

    # The pump's handlers for this tab's jobs - see pump_jobs.
    def pump_scr_options(payload):
        ui.busy(False)
        show_scr_options(payload)
        names = [f["keyword"]
                 for f in payload.get("formats") or []]
        log_context("screen formats",
                    "%s; chosen %s; palette %s"
                    % (", ".join(names) or "none",
                       payload.get("best"),
                       payload.get("palette")))
        if names:
            ui.say(_("%d image format(s) available; %s will be used")
                   % (len(names), payload.get("best")))

    def pump_scr_get(payload):
        ui.busy(False)
        ui.progress_off()
        screen = payload["screen"]
        # The instrument's own picture is kept as it
        # arrived, so switching to Inverted and back does
        # not need another capture.
        state["sraw"] = screen
        state["ssecs"] = payload["secs"]
        # What arrived already honours the layout it was
        # asked for, so no turning is owed on it.
        state["sturned"] = 0
        if state.get("sinvert"):
            screen = screen.inverted()
        show_shot(screen, payload["secs"])
        ui.say(_("Screen read in %s: %d x %d in %.1f s")
               % (screen.keyword, screen.width, screen.height,
                  payload["secs"]))

    vars(ui).update((k, v) for k, v in locals().items() if k != "ui")
