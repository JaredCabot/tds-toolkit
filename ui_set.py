"""The Settings tab.

SettingsTab is the tab; build(ui) makes it and hands its names back to
the window, which still reaches the tabs through `ui` (state, the
worker, busy, say and the rest) while they are moved onto classes.
"""
import tkinter as tk
from tkinter import messagebox
from tkinter import simpledialog
from tkinter import ttk
from tkinter import colorchooser

import tds_wfm
from i18n import gettext as _
from tdstoolkit import (APPDIR, NUDGE_GRID, NUDGE_STEPS, PNG_DEFAULT_SIZE,
                        PNG_SIZES, __author__, __email__, __licence__,
                        __version__, load_settings, nudge_named, nudge_value,
                        png_named, png_size, save_settings, tab_names)


class SettingsTab(object):
    """The Settings tab: the program's own preferences."""

    def __init__(self, ui):
        self.ui = ui
        self.draw_limits = ui.draw_limits
        self.draw_mask = ui.draw_mask
        self.draw_plot = ui.draw_plot
        self.hints = ui.hints
        self.named = ui.named
        self.relabel = ui.relabel
        self.root = ui.root
        self.says = ui.says
        self.state = ui.state
        self.sys_rule = ui.sys_rule
        self.tabs = ui.tabs

        # ------------------------------------------------- the settings tab
        # This program's own settings, as against the instrument's, which
        # are the tab before this one. The colours used to be a dialog
        # reached from three different tabs; a colour is chosen by looking
        # at the trace it is going to draw, so a window sitting on top of
        # that trace was the wrong shape for the job from the start.
        self.settab = ttk.Frame(self.tabs)
        self.tabs.add(self.settab, text=_("Settings"))
        self.named(self.settab, "Settings")

        setleft = ttk.Frame(self.settab)
        setleft.pack(side="left", fill="both", expand=True, padx=(4, 3),
                     pady=6)
        setright = ttk.Frame(self.settab)
        setright.pack(side="left", fill="both", expand=True, padx=(3, 4),
                      pady=6)

        setbox1 = ttk.LabelFrame(setleft, padding=8)
        self.says(setbox1, "Plot colours")
        setbox1.pack(fill="x")

        self.set_swatches = {}
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

        setgrid = ttk.Frame(setbox1)
        setgrid.pack(fill="x")
        # Two columns, split down the middle however many roles there are, so a
        # long list of colours does not overflow a fixed six-row column.
        set_half = (len(SET_LABELS) + 1) // 2
        for row, (key, label) in enumerate(SET_LABELS):
            self.one = ttk.Label(setgrid, text=_(label))
            self.one.grid(row=row % set_half,
                          column=0 if row < set_half else 2,
                          sticky="w", pady=2, padx=(0, 6))
            self.says(self.one, label)
            # A plain tk button, not ttk: a themed button will not take a
            # background colour on Windows, which is the one thing this
            # button exists to show.
            self.swatch = tk.Button(setgrid, width=6, relief="ridge")
            self.swatch.grid(row=row % set_half,
                             column=1 if row < set_half else 3,
                             sticky="w", pady=2, padx=(0, 16))
            self.set_swatches[key] = self.swatch
            self.hints(self.swatch, "Click to choose this colour")

            def choose(k=key, name=label):
                working = self.set_working()
                got = colorchooser.askcolor(color=working[k], parent=self.root,
                                            title=_(name))
                if got and got[1]:
                    working[k] = got[1]
                    self.set_preset.set("")
                    self.set_apply(working)

            self.swatch.config(command=choose)

        setrow1 = ttk.Frame(setbox1)
        setrow1.pack(fill="x", pady=(10, 0))
        lbl_setpre = ttk.Label(setrow1, text=_("Preset"))
        lbl_setpre.pack(side="left", padx=(0, 6))
        self.says(lbl_setpre, "Preset")
        self.set_preset = ttk.Combobox(setrow1, width=20, state="readonly")
        self.set_preset.pack(side="left")

        self.set_preset.bind("<<ComboboxSelected>>", self.set_load)

        self.btn_setsave = ttk.Button(setrow1, text=_("Save..."),
                                      padding=(8, 0),
                                      command=self.set_save)
        self.btn_setsave.pack(side="left", padx=(6, 0))
        self.says(self.btn_setsave, "Save...")
        self.btn_setdrop = ttk.Button(setrow1, text=_("Delete"),
                                      padding=(8, 0),
                                      command=self.set_drop)
        self.btn_setdrop.pack(side="left", padx=4)
        self.says(self.btn_setdrop, "Delete")
        lbl_setsays = ttk.Label(setbox1, foreground="#555", wraplength=340,
                                justify="left")
        self.says(lbl_setsays,
                  "Applies to the waveform plot, the mask editor and "
                  "the limits editor, and to any picture you save. "
                  "Fewer colours make a smaller file.")
        lbl_setsays.pack(anchor="w", pady=(8, 0))

        # ---- how large a saved picture is
        setbox2 = ttk.LabelFrame(setright, padding=8)
        self.says(setbox2, "Saved pictures")
        setbox2.pack(fill="x")
        setrow2 = ttk.Frame(setbox2)
        setrow2.pack(fill="x")
        lbl_setwide = ttk.Label(setrow2, text=_("Resolution"))
        lbl_setwide.pack(side="left", padx=(0, 6))
        self.says(lbl_setwide, "Resolution")
        # Read-only, so the only sizes that can be asked for are the ones
        # below. A free-text width was a number nobody had a reason to
        # choose, and the graticule is centred in whatever canvas it is
        # given rather than stretched to it, so an off-list size bought
        # nothing but an odd-shaped file.
        self.set_wide = ttk.Combobox(setrow2, width=11, state="readonly",
                                     values=[png_named(wh)
                                             for wh in PNG_SIZES])
        self.set_wide.set(png_named(self.state["pngsize"]))
        self.set_wide.pack(side="left")

        self.set_wide.bind("<<ComboboxSelected>>", self.set_png)

        self.btn_setdef = ttk.Button(setrow2, text=_("Default"),
                                     padding=(8, 0),
                                     command=self.set_png_default)
        self.btn_setdef.pack(side="left", padx=6)
        self.says(self.btn_setdef, "Default")
        # Said out loud because the box is in the same panel and would
        # otherwise look as though it governed both. It does not: a
        # screenshot is the instrument's own hardcopy, saved byte for byte.
        lbl_setnative = ttk.Label(setbox2, foreground="#555", wraplength=340,
                                  justify="left")
        self.says(lbl_setnative, "Resolution applies to waveform plot,\nmask "
                  "editor and limits editor.\nScreenshots are not "
                  "affected, they are always saved at the "
                  "oscilloscope's native resolution of 640x480.")
        lbl_setnative.pack(anchor="w", pady=(8, 0))

        # ---- how far an arrow key moves a point
        self.sys_rule(setright)
        setbox3 = ttk.LabelFrame(setright, padding=8)
        self.says(setbox3, "Editing")
        setbox3.pack(fill="x")
        setrow3 = ttk.Frame(setbox3)
        setrow3.pack(fill="x")
        lbl_setnudge = ttk.Label(setrow3, text=_("Nudge distance"))
        lbl_setnudge.pack(side="left", padx=(0, 6))
        self.says(lbl_setnudge, "Nudge distance")
        # Read-only for the same reason the resolution is: these are the
        # distances worth having, and a typed 0.037 is a number nobody
        # chose. The first entry keeps what the program did before there
        # was a setting, which is a fifth of a grid square, so five
        # presses cross one.
        self.set_nudge = ttk.Combobox(setrow3, width=15, state="readonly")

        self.nudge_relabel()
        self.relabel.append(self.nudge_relabel)
        self.set_nudge.pack(side="left")

        self.set_nudge.bind("<<ComboboxSelected>>", self.set_nudge_step)
        lbl_setnudgesays = ttk.Label(setbox3, foreground="#555",
                                     wraplength=340,
                                     justify="left")
        self.says(lbl_setnudgesays,
                  "How far one arrow key moves the selected point or shape in "
                  "the mask editor and the limits editor, in percent of the "
                  "graticule. One division is ten percent across and twelve "
                  "and a half percent up.")
        lbl_setnudgesays.pack(anchor="w", pady=(8, 0))

        # ---- where this program keeps things
        self.sys_rule(setright)
        setbox4 = ttk.LabelFrame(setright, padding=8)
        self.says(setbox4, "This program")
        setbox4.pack(fill="x")
        self.lbl_setwhere = ttk.Label(setbox4, foreground="#555",
                                      wraplength=340,
                                      justify="left")
        self.lbl_setwhere.pack(anchor="w")

        setbox4.bind("<Configure>", self.set_fit_where)

        self.set_about()
        self.relabel.append(self.set_about)

        self.root.after_idle(self.set_first_fill)

    def set_working(self):
        """The colours in force, as a plain dictionary."""
        return dict(tds_wfm.scheme(self.state.get("colours")))

    def set_apply(self, working):
        """Take them, draw with them, and remember them.

        No Apply button and no Cancel: a tab has no moment of opening
        to put things back to, and a colour picked against a written
        description rather than against the trace it draws is a guess.
        """
        self.state["colours"] = dict(working)
        self.draw_plot()
        self.draw_mask()
        self.draw_limits()
        settings = load_settings()
        settings["colours"] = dict(working)
        save_settings(settings)
        self.set_repaint()

    def set_repaint(self):
        working = self.set_working()
        for key, button in self.set_swatches.items():
            button.config(background=working[key],
                          activebackground=working[key])

    def set_refill(self, select=None):
        names = sorted(self.ui.colour_presets())
        self.set_preset.config(values=names)
        if select in names:
            self.set_preset.set(select)
        else:
            here = self.set_working()
            match = [n for n,
                     c in self.ui.colour_presets().items() if c == here]
            self.set_preset.set(match[0] if match else "")

    def set_load(self, _evt=None):
        chosen = self.ui.colour_presets().get(self.set_preset.get())
        if chosen:
            working = self.set_working()
            working.update(chosen)
            self.set_apply(working)
            self.set_refill(self.set_preset.get())

    def set_save(self):
        name = simpledialog.askstring(
            _("Save preset"), _("Name for this colour scheme:"),
            parent=self.root, initialvalue=self.set_preset.get() or "")
        name = (name or "").strip()
        if not name:
            return
        settings = load_settings()
        saved = settings.get("colour_presets")
        saved = saved if isinstance(saved, dict) else {}
        saved[name] = self.set_working()
        settings["colour_presets"] = saved
        save_settings(settings)
        self.set_refill(name)
        self.ui.say(_("Saved colour preset '%s'") % name)

    def set_drop(self):
        name = self.set_preset.get()
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
        self.set_refill()
        self.ui.say(_("Deleted colour preset '%s'") % name)

    # Not set_size: there is already one of those further down this same
    # function, for the file list's Size column. Two defs of one name in
    # one scope means the later one wins every lookup by name, so Default
    # called the wrong one and raised TypeError.
    def set_png(self, _evt=None):
        self.state["pngsize"] = png_size(self.set_wide.get())
        self.set_wide.set(png_named(self.state["pngsize"]))
        settings = load_settings()
        settings["png_size"] = list(self.state["pngsize"])
        save_settings(settings)

    def set_png_default(self):
        self.set_wide.set(png_named(PNG_DEFAULT_SIZE))
        self.set_png()

    def nudge_shown(self, step):
        """The name for a distance, in whatever language is current."""
        return (_(NUDGE_GRID) if not step else nudge_named(step))

    def nudge_relabel(self):
        """The list, and what is in the box, after a language change.

        A Combobox's values are strings it was handed once, so unlike a
        label they do not follow the language on their own. One of them
        is a word rather than a number, which is the one that used to
        stay in English.
        """
        self.set_nudge.config(values=[self.nudge_shown(n)
                                      for n in (None,) + NUDGE_STEPS])
        self.set_nudge.set(self.nudge_shown(self.state["nudge"]))

    def set_nudge_step(self, _evt=None):
        said = self.set_nudge.get()
        self.state["nudge"] = (None if said == self.nudge_shown(None)
                               else nudge_value(said))
        self.set_nudge.set(self.nudge_shown(self.state["nudge"]))
        settings = load_settings()
        settings["nudge"] = self.state["nudge"]
        save_settings(settings)

    def set_fit_where(self, evt):
        """Wrap these lines to the box, not to a guess.

        The settings path is the longest line in this panel, and a fixed
        wraplength broke it in half in a window with room to spare.
        """
        self.lbl_setwhere.config(wraplength=max(200, evt.width - 20))

    def set_about(self):
        """Who wrote it, under what, and where it keeps its things.

        Rebuilt on a language change rather than set once: the label was
        written at build time, so everything but the address stayed in
        the language the window started in.
        """
        self.lbl_setwhere.config(text="\n".join([
            _("Version %s") % __version__,
            "%s  -  %s" % (__author__, __email__),
            _("Free software under the %s licence") % __licence__,
            _("Written with Claude, by Anthropic"),
            _("Settings and log: %s") % APPDIR]))

    def set_first_fill(self):
        """Fill the boxes once, after the whole window exists.

        Not while the tab is being built: colour_presets is defined
        further down this same function, and a closure looks its names
        up when it runs. Called at build time this raised NameError
        before the window ever appeared.
        """
        self.set_repaint()
        self.set_refill()


def build(ui):
    """Make the tab, and hand its names to the window as before."""
    tab = SettingsTab(ui)
    vars(ui).update(tab_names(tab))
    return tab
