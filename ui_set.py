"""The Settings tab.

Built by run_gui through build(ui). `ui` carries the window's shared
names - state, the worker, busy, say and the rest - and is handed back
everything built here, so run_gui and the tests can reach it.
"""
from tkinter import colorchooser

import tds_wfm
from i18n import gettext as _
from tdstoolkit import (APPDIR, NUDGE_GRID, NUDGE_STEPS, PNG_DEFAULT_SIZE,
                        PNG_SIZES, __author__, __email__, __licence__,
                        __version__, load_settings, nudge_named, nudge_value,
                        png_named, png_size, save_settings)


def build(ui):
    draw_limits = ui.draw_limits
    draw_mask = ui.draw_mask
    draw_plot = ui.draw_plot
    hints = ui.hints
    messagebox = ui.messagebox
    named = ui.named
    relabel = ui.relabel
    root = ui.root
    says = ui.says
    simpledialog = ui.simpledialog
    state = ui.state
    sys_rule = ui.sys_rule
    tabs = ui.tabs
    tk = ui.tk
    ttk = ui.ttk

    # ------------------------------------------------- the settings tab
    # This program's own settings, as against the instrument's, which
    # are the tab before this one. The colours used to be a dialog
    # reached from three different tabs; a colour is chosen by looking
    # at the trace it is going to draw, so a window sitting on top of
    # that trace was the wrong shape for the job from the start.
    settab = ttk.Frame(tabs)
    tabs.add(settab, text=_("Settings"))
    named(settab, "Settings")

    setleft = ttk.Frame(settab)
    setleft.pack(side="left", fill="both", expand=True, padx=(4, 3), pady=6)
    setright = ttk.Frame(settab)
    setright.pack(side="left", fill="both", expand=True, padx=(3, 4), pady=6)

    setbox1 = ttk.LabelFrame(setleft, padding=8)
    says(setbox1, "Plot colours")
    setbox1.pack(fill="x")

    set_swatches = {}
    SET_LABELS = (("background", "Background"),
                  ("graticule", "Division lines"),
                  ("major", "Centre lines"),
                  ("pips", "Minor tick marks"),
                  ("border", "Border"),
                  ("trace", "Trace"), ("label", "Text"),
                  ("grid", "Grid"), ("mask", "Mask"),
                  ("select", "Selected"),
                  ("hover", "Mouseover"),
                  ("cut", "Split line"))

    def set_working():
        """The colours in force, as a plain dictionary."""
        return dict(tds_wfm.scheme(state.get("colours")))

    def set_apply(working):
        """Take them, draw with them, and remember them.

        No Apply button and no Cancel: a tab has no moment of opening
        to put things back to, and a colour picked against a written
        description rather than against the trace it draws is a guess.
        """
        state["colours"] = dict(working)
        draw_plot()
        draw_mask()
        draw_limits()
        settings = load_settings()
        settings["colours"] = dict(working)
        save_settings(settings)
        set_repaint()

    def set_repaint():
        working = set_working()
        for key, button in set_swatches.items():
            button.config(background=working[key],
                          activebackground=working[key])

    setgrid = ttk.Frame(setbox1)
    setgrid.pack(fill="x")
    # Two columns, split down the middle however many roles there are, so a
    # long list of colours does not overflow a fixed six-row column.
    set_half = (len(SET_LABELS) + 1) // 2
    for row, (key, label) in enumerate(SET_LABELS):
        one = ttk.Label(setgrid, text=_(label))
        one.grid(row=row % set_half, column=0 if row < set_half else 2,
                 sticky="w", pady=2, padx=(0, 6))
        says(one, label)
        # A plain tk button, not ttk: a themed button will not take a
        # background colour on Windows, which is the one thing this
        # button exists to show.
        swatch = tk.Button(setgrid, width=6, relief="ridge")
        swatch.grid(row=row % set_half, column=1 if row < set_half else 3,
                    sticky="w", pady=2, padx=(0, 16))
        set_swatches[key] = swatch
        hints(swatch, "Click to choose this colour")

        def choose(k=key, name=label):
            working = set_working()
            got = colorchooser.askcolor(color=working[k], parent=root,
                                        title=_(name))
            if got and got[1]:
                working[k] = got[1]
                set_preset.set("")
                set_apply(working)

        swatch.config(command=choose)

    setrow1 = ttk.Frame(setbox1)
    setrow1.pack(fill="x", pady=(10, 0))
    lbl_setpre = ttk.Label(setrow1, text=_("Preset"))
    lbl_setpre.pack(side="left", padx=(0, 6))
    says(lbl_setpre, "Preset")
    set_preset = ttk.Combobox(setrow1, width=20, state="readonly")
    set_preset.pack(side="left")

    def set_refill(select=None):
        names = sorted(ui.colour_presets())
        set_preset.config(values=names)
        if select in names:
            set_preset.set(select)
        else:
            here = set_working()
            match = [n for n, c in ui.colour_presets().items() if c == here]
            set_preset.set(match[0] if match else "")

    def set_load(_evt=None):
        chosen = ui.colour_presets().get(set_preset.get())
        if chosen:
            working = set_working()
            working.update(chosen)
            set_apply(working)
            set_refill(set_preset.get())

    set_preset.bind("<<ComboboxSelected>>", set_load)

    def set_save():
        name = simpledialog.askstring(
            _("Save preset"), _("Name for this colour scheme:"),
            parent=root, initialvalue=set_preset.get() or "")
        name = (name or "").strip()
        if not name:
            return
        settings = load_settings()
        saved = settings.get("colour_presets")
        saved = saved if isinstance(saved, dict) else {}
        saved[name] = set_working()
        settings["colour_presets"] = saved
        save_settings(settings)
        set_refill(name)
        ui.say(_("Saved colour preset '%s'") % name)

    def set_drop():
        name = set_preset.get()
        if not name:
            return
        settings = load_settings()
        saved = settings.get("colour_presets")
        saved = saved if isinstance(saved, dict) else {}
        if name not in saved:
            messagebox.showinfo(
                _("Cannot delete that preset"),
                _("'%s' is one of the supplied schemes, not one of "
                  "yours. Save a scheme of the same name to replace "
                  "it.") % name)
            return
        if not messagebox.askyesno(
                _("Delete preset"),
                _("Delete the colour scheme '%s'?") % name, default="no"):
            return
        del saved[name]
        settings["colour_presets"] = saved
        save_settings(settings)
        set_refill()
        ui.say(_("Deleted colour preset '%s'") % name)

    btn_setsave = ttk.Button(setrow1, text=_("Save..."), padding=(8, 0),
                             command=set_save)
    btn_setsave.pack(side="left", padx=(6, 0))
    says(btn_setsave, "Save...")
    btn_setdrop = ttk.Button(setrow1, text=_("Delete"), padding=(8, 0),
                             command=set_drop)
    btn_setdrop.pack(side="left", padx=4)
    says(btn_setdrop, "Delete")
    lbl_setsays = ttk.Label(setbox1, foreground="#555", wraplength=340,
                            justify="left")
    says(lbl_setsays, "Applies to the waveform plot, the mask editor and "
                      "the limits editor, and to any picture you save. "
                      "Fewer colours make a smaller file.")
    lbl_setsays.pack(anchor="w", pady=(8, 0))

    # ---- how large a saved picture is
    setbox2 = ttk.LabelFrame(setright, padding=8)
    says(setbox2, "Saved pictures")
    setbox2.pack(fill="x")
    setrow2 = ttk.Frame(setbox2)
    setrow2.pack(fill="x")
    lbl_setwide = ttk.Label(setrow2, text=_("Resolution"))
    lbl_setwide.pack(side="left", padx=(0, 6))
    says(lbl_setwide, "Resolution")
    # Read-only, so the only sizes that can be asked for are the ones
    # below. A free-text width was a number nobody had a reason to
    # choose, and the graticule is centred in whatever canvas it is
    # given rather than stretched to it, so an off-list size bought
    # nothing but an odd-shaped file.
    set_wide = ttk.Combobox(setrow2, width=11, state="readonly",
                            values=[png_named(wh) for wh in PNG_SIZES])
    set_wide.set(png_named(state["pngsize"]))
    set_wide.pack(side="left")

    # Not set_size: there is already one of those further down this same
    # function, for the file list's Size column. Two defs of one name in
    # one scope means the later one wins every lookup by name, so Default
    # called the wrong one and raised TypeError.
    def set_png(_evt=None):
        state["pngsize"] = png_size(set_wide.get())
        set_wide.set(png_named(state["pngsize"]))
        settings = load_settings()
        settings["png_size"] = list(state["pngsize"])
        save_settings(settings)

    set_wide.bind("<<ComboboxSelected>>", set_png)

    def set_png_default():
        set_wide.set(png_named(PNG_DEFAULT_SIZE))
        set_png()

    btn_setdef = ttk.Button(setrow2, text=_("Default"), padding=(8, 0),
                            command=set_png_default)
    btn_setdef.pack(side="left", padx=6)
    says(btn_setdef, "Default")
    # Said out loud because the box is in the same panel and would
    # otherwise look as though it governed both. It does not: a
    # screenshot is the instrument's own hardcopy, saved byte for byte.
    lbl_setnative = ttk.Label(setbox2, foreground="#555", wraplength=340,
                              justify="left")
    says(lbl_setnative, "Resolution applies to waveform plot,\nmask "
                        "editor and limits editor.\nScreenshots are not "
                        "affected, they are always saved at the "
                        "oscilloscope's native resolution of 640x480.")
    lbl_setnative.pack(anchor="w", pady=(8, 0))

    # ---- how far an arrow key moves a point
    sys_rule(setright)
    setbox3 = ttk.LabelFrame(setright, padding=8)
    says(setbox3, "Editing")
    setbox3.pack(fill="x")
    setrow3 = ttk.Frame(setbox3)
    setrow3.pack(fill="x")
    lbl_setnudge = ttk.Label(setrow3, text=_("Nudge distance"))
    lbl_setnudge.pack(side="left", padx=(0, 6))
    says(lbl_setnudge, "Nudge distance")
    # Read-only for the same reason the resolution is: these are the
    # distances worth having, and a typed 0.037 is a number nobody
    # chose. The first entry keeps what the program did before there
    # was a setting, which is a fifth of a grid square, so five
    # presses cross one.
    set_nudge = ttk.Combobox(setrow3, width=15, state="readonly")

    def nudge_shown(step):
        """The name for a distance, in whatever language is current."""
        return (_(NUDGE_GRID) if not step else nudge_named(step))

    def nudge_relabel():
        """The list, and what is in the box, after a language change.

        A Combobox's values are strings it was handed once, so unlike a
        label they do not follow the language on their own. One of them
        is a word rather than a number, which is the one that used to
        stay in English.
        """
        set_nudge.config(values=[nudge_shown(n)
                                 for n in (None,) + NUDGE_STEPS])
        set_nudge.set(nudge_shown(state["nudge"]))

    nudge_relabel()
    relabel.append(nudge_relabel)
    set_nudge.pack(side="left")

    def set_nudge_step(_evt=None):
        said = set_nudge.get()
        state["nudge"] = (None if said == nudge_shown(None)
                          else nudge_value(said))
        set_nudge.set(nudge_shown(state["nudge"]))
        settings = load_settings()
        settings["nudge"] = state["nudge"]
        save_settings(settings)

    set_nudge.bind("<<ComboboxSelected>>", set_nudge_step)
    lbl_setnudgesays = ttk.Label(setbox3, foreground="#555", wraplength=340,
                                 justify="left")
    says(lbl_setnudgesays,
         "How far one arrow key moves the selected point or shape in the "
         "mask editor and the limits editor, in percent of the "
         "graticule. One division is ten percent across and twelve and a "
         "half percent up.")
    lbl_setnudgesays.pack(anchor="w", pady=(8, 0))

    # ---- where this program keeps things
    sys_rule(setright)
    setbox4 = ttk.LabelFrame(setright, padding=8)
    says(setbox4, "This program")
    setbox4.pack(fill="x")
    lbl_setwhere = ttk.Label(setbox4, foreground="#555", wraplength=340,
                             justify="left")
    lbl_setwhere.pack(anchor="w")

    def set_fit_where(evt):
        """Wrap these lines to the box, not to a guess.

        The settings path is the longest line in this panel, and a fixed
        wraplength broke it in half in a window with room to spare.
        """
        lbl_setwhere.config(wraplength=max(200, evt.width - 20))

    setbox4.bind("<Configure>", set_fit_where)

    def set_about():
        """Who wrote it, under what, and where it keeps its things.

        Rebuilt on a language change rather than set once: the label was
        written at build time, so everything but the address stayed in
        the language the window started in.
        """
        lbl_setwhere.config(text="\n".join([
            _("Version %s") % __version__,
            "%s  -  %s" % (__author__, __email__),
            _("Free software under the %s licence") % __licence__,
            _("Written with Claude, by Anthropic"),
            _("Settings and log: %s") % APPDIR]))

    set_about()
    relabel.append(set_about)

    def set_first_fill():
        """Fill the boxes once, after the whole window exists.

        Not while the tab is being built: colour_presets is defined
        further down this same function, and a closure looks its names
        up when it runs. Called at build time this raised NameError
        before the window ever appeared.
        """
        set_repaint()
        set_refill()

    root.after_idle(set_first_fill)

    vars(ui).update((k, v) for k, v in locals().items() if k != "ui")
