"""The Files tab: the instrument's file system, browsed like Explorer.

FilesTab is the tab; build(ui) makes it and hands its names back to
the window, which still reaches the tabs through `ui` (state, the
worker, busy, say and the rest) while they are moved onto classes.
"""
import os
import tkinter as tk
from tkinter import filedialog
from tkinter import messagebox
from tkinter import simpledialog
from tkinter import ttk

import winicons
from i18n import gettext as _
from tdstoolkit import (MASS_STORAGE, N_, check_83, localise,
                        suggest_copy_name, tab_names, to_83, type_of)
from worker import Worker


class FilesTab(object):
    """The Files tab: the instrument's folders and files."""

    def __init__(self, ui):
        self.ui = ui
        self.by_name = ui.by_name
        self.hints = ui.hints
        self.named = ui.named
        self.relabel = ui.relabel
        self.root = ui.root
        self.state = ui.state
        self.tabs = ui.tabs
        self.w = ui.w

        self.filetab = ttk.Frame(self.tabs)
        self.tabs.add(self.filetab, text=_("Files"))
        self.named(self.filetab, "Files")

        self.nav = ttk.Frame(self.filetab)
        self.nav.pack(fill="x", padx=2, pady=(6, 2))
        self.btn_back = ttk.Button(self.nav, text="←", width=3,
                                   command=lambda: self.do_back())
        self.btn_fwd = ttk.Button(self.nav, text="→", width=3,
                                  command=lambda: self.do_forward())
        self.btn_up = ttk.Button(self.nav, text="↑", width=3,
                                 command=lambda: self.do_up())
        for self.b in (self.btn_back, self.btn_fwd, self.btn_up):
            self.b.pack(side="left", padx=(0, 2))
        self.ent_path = ttk.Entry(self.nav)
        self.ent_path.pack(side="left", fill="x", expand=True, padx=(6, 0))

        # --------------------------------------------------------- command row
        # What to do with what is selected, then which instrument to do it on.
        # No address readout here: the title bar already names the connected
        # instrument in full.
        tb = ttk.Frame(self.filetab)
        tb.pack(fill="x", padx=2, pady=(0, 4))

        # ----------------------------------------------------------- content
        self.panes = ttk.PanedWindow(self.filetab, orient="horizontal")
        self.panes.pack(fill="both", expand=True, padx=2, pady=4)

        # The expander is left to the platform theme, which on Windows already
        # draws the chevron Explorer uses and turns it when the folder opens.
        # An earlier version replaced it with drawn glyphs under a custom
        # style; that element never picked up the open state, so it showed a
        # chevron that pointed right for ever. Replacing a working native
        # control with a worse copy of it is not an improvement.
        ttk.Style().configure("Treeview", rowheight=20)

        leftf = ttk.Frame(self.panes)
        self.tree = ttk.Treeview(leftf, show="tree", selectmode="browse")
        tsb = ttk.Scrollbar(leftf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.panes.add(leftf, weight=1)

        listf = ttk.Frame(self.panes)
        self.cols = ("size", "type")
        # "extended" is the Explorer convention: click, ctrl-click to add,
        # shift-click for a run, ctrl-A for all.
        self.lst = ttk.Treeview(listf, columns=self.cols, show="tree headings",
                                selectmode="extended")
        self.lst.heading("#0", text=_("Name"))
        self.lst.heading("size", text=_("Size"))
        self.lst.heading("type", text=_("Type"))
        self.lst.column("#0", width=240)
        self.lst.column("size", width=90, anchor="e")
        self.lst.column("type", width=170)
        self.lsb = ttk.Scrollbar(listf, orient="vertical",
                                 command=self.lst.yview)
        self.lst.configure(yscrollcommand=self.lsb.set)
        self.lst.pack(side="left", fill="both", expand=True)
        self.lsb.pack(side="right", fill="y")
        self.panes.add(listf, weight=3)

        # English here and translated as drawn, so the headings follow a
        # language change (D9): translated once at build time, they were
        # put back in the old language by every sort and every redraw.
        self.HEADINGS = (("#0", "name", N_("Name")),
                         ("size", "size", N_("Size")),
                         ("type", "type", N_("Type")))

        for _ident, _key, _label in self.HEADINGS:
            self.lst.heading(_ident, command=lambda k=_key: self.sort_by(k))

        self.tree.bind("<<TreeviewOpen>>", self.on_expand)

        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        self.lst.bind("<Double-1>", self.do_open)

        self.lst.bind("<Return>", self.do_open)

        for key in ("<Control-a>", "<Control-A>"):
            self.lst.bind(key, lambda e: (self.lst.selection_set(
                self.lst.get_children()), "break")[1])

        if ui.DND:
            for widget, handler in ((self.lst, self.on_drop_list),
                                    (self.tree, self.on_drop_tree)):
                widget.drop_target_register(ui.DND_FILES)
                widget.dnd_bind("<<Drop>>", handler)

        self.ent_path.bind("<Return>", self.do_goto)

        # ------------------------------------------------------- the toolbar
        for text, fn in (("Refresh", self.do_refresh),
                         ("Download", self.do_download),
                         ("Upload", self.do_upload),
                         ("New folder", self.do_mkdir),
                         ("Delete", self.do_delete)):
            # No fixed width. A width in characters is really a multiple of the
            # font's average character, which is wrong for German
            # ("Herunterladen" in an 11-wide button is clipped) and wrong the
            # other way for Japanese, whose glyphs are twice as wide as the
            # average. Letting Tk measure the actual string is the only thing
            # that is right in every language.
            self.b = ttk.Button(tb, text=_(text), padding=(10, 2), command=fn)
            self.b.pack(side="left", padx=(0, 4))
            ui.buttons.append(self.b)
            ui.labelled.append((self.b, text))
            self.by_name[text] = self.b

        # And Format at the right-hand end of the buttons themselves, as far
        # from Delete as the row allows. It is the one thing here that takes
        # a whole volume rather than a file, and it does not want to be next
        # to the button somebody presses without looking.
        btn_format = ttk.Button(tb, text=_("Format..."), padding=(10, 2),
                                command=lambda: self.do_format())

        btn_format.pack(side="right", padx=(8, 0))

        ui.buttons.append(btn_format)

        ui.labelled.append((btn_format, "Format..."))

        self.by_name["Format..."] = btn_format

        # What each of the Files tab's buttons is for, in a tooltip - in the
        # same voice as the rest of the window's, below.
        for widget, english in (
                (self.btn_back, "Back to the folder before this one"),
                (self.btn_fwd, "Forward again"),
                (self.btn_up, "Up one folder"),
                (self.by_name["Refresh"],
                 "Read this folder from the instrument again"),
                (self.by_name["Download"],
                 "Copy what is selected to this computer"),
                (self.by_name["Upload"],
                 "Copy files from this computer to the "
                 "instrument"),
                (self.by_name["New folder"], "Make a folder here"),
                (self.by_name["Delete"],
                 "Delete what is selected on the instrument"),
                (btn_format, "Erase a volume and lay down a fresh, empty "
                 "filesystem"),
                (self.ent_path,
                 "The current folder on the instrument - type a "
                 "path and press Enter to go there")):
            self.hints(widget, english)

        # Windows keys: F5 refresh, Backspace up, Delete deletes the selection,
        # Enter and double-click save it, ctrl-A selects every file. Delete
        # works from either pane, as it does in Explorer.
        self.root.bind("<F5>", lambda e: self.do_refresh())

        self.root.bind("<BackSpace>",
                       lambda e: self.do_up() if self.in_file_panes()
                       else None)

        self.root.bind("<Delete>",
                       lambda e: self.do_delete(e) if self.in_file_panes()
                       else None)

        # Explorer's navigation keys, which cost nothing to support.
        self.root.bind("<Alt-Left>", self.do_back)

        self.root.bind("<Alt-Right>", self.do_forward)

        self.root.bind("<Alt-Up>", self.do_up)

        # Right-click context menus, also standard.
        self.menu_file = tk.Menu(self.root, tearoff=0)

        self.menu_file.add_command(label=_("Save as..."),
                                   command=self.do_download)

        self.menu_file.add_separator()

        self.menu_file.add_command(label=_("Delete"), command=self.do_delete)

        # The background menu, for a right-click that hits no row.
        self.menu_empty = tk.Menu(self.root, tearoff=0)

        self.menu_empty.add_command(label=_("New folder..."),
                                    command=lambda: self.do_mkdir())

        self.menu_empty.add_separator()

        self.menu_empty.add_command(label=_("Refresh"),
                                    command=lambda: self.do_refresh())

        self.menu_empty.add_command(label=_("Upload files..."),
                                    command=lambda: self.do_upload())

        self.menu_dir = tk.Menu(self.root, tearoff=0)

        self.menu_dir.add_command(
            label=_("Open"), command=lambda: self.navigate(self.tree.focus()))

        # The folder right-clicked, not the one on show (D7).
        self.menu_dir.add_command(
            label=_("Refresh"),
            command=lambda: self.navigate(self.tree.focus(), force=True))

        self.menu_dir.add_separator()

        self.menu_dir.add_command(
            label=_("New folder..."),
            command=lambda: self.do_mkdir(self.tree.focus()))

        self.menu_dir.add_command(label=_("Delete"), command=self.do_delete)

        self.relabel.append(self.menus_relabel)

        self.lst.bind("<Button-1>", self.on_list_click, add="+")

        self.lst.bind("<Button-3>", self.popup_list)

        self.tree.bind("<Button-3>", self.popup_tree)

    def update_nav(self):
        """Enable each arrow only where it would actually go somewhere.

        Deliberately not part of the blanket enable in busy(): back and
        forward depend on where you have been, so re-enabling them
        wholesale would offer a Back button at the start of a session.
        """
        hist, at = self.state["history"], self.state["hist_at"]
        can = not self.state["busy"]
        self.btn_back.config(state="normal" if can and at > 0 else "disabled")
        self.btn_fwd.config(
            state="normal" if can and at < len(hist) - 1 else "disabled")
        self.btn_up.config(state="normal" if can and self.state["cwd"]
                           and self.parent_of(self.state["cwd"])
                           else "disabled")

    # --------------------------------------------------------- helpers
    def parent_of(self, path):
        p = path.rstrip("/")
        if "/" not in p:
            return None
        return p.rsplit("/", 1)[0] or (p.split(":")[0] + ":")

    def join(self, path, name):
        return "%s/%s" % (path.rstrip("/"), name)

    def icon_for(self, name, is_dir):
        """A Tk image for this name, or None if icons are unavailable.

        Kept referenced in `state`, because Tk discards an image the
        moment Python stops holding it and the row goes blank.
        """
        try:
            img = winicons.icon_for(tk, name, is_dir)
            self.state.setdefault("icons", []).append(img)
            return img
        except Exception:
            return ""

    def sel_rows(self, kind=None):
        """Selected rows of a given kind - 'dir', 'file', or both."""
        rows = self.state.get("rows") or {}
        out = []
        for iid in self.lst.selection():
            k, name = rows.get(iid, ("file", self.lst.item(iid, "text")))
            if kind is None or k == kind:
                out.append(name)
        return out

    def sel_files(self):
        """Every file selected in the right pane, in the order shown."""
        return self.sel_rows("file")

    def sel_folders(self):
        return self.sel_rows("dir")

    def set_size(self, name, nbytes):
        """Fill in the Size column once a file's real length is known.

        Sizes are not in the directory listing, so a file's size is only
        known after it has been transferred. Matched by name because row
        ids are positional.
        """
        self.state["sizes"][self.join(self.state["cwd"] or "",
                                      name)] = format(nbytes, ",")
        for iid in self.lst.get_children():
            if self.lst.item(iid, "text") == name:
                self.lst.set(iid, "size", format(nbytes, ","))
                return

    def mass_storage_note(self, payload):
        """Explain the message the front panel will be showing.

        The instrument puts 'Mass storage error' on its own display when it
        raises event 250, and there is no way to stop it from here. Saying
        nothing would leave the user staring at an error beside a delete
        that actually worked, so the status bar names it first.
        """
        if MASS_STORAGE not in (payload.get("events") or []):
            return ""
        return (_("  (the instrument logged a mass storage error during the "
                "delete - the folder is verified gone; see the log)"))

    def report_failures(self, verb, failed):
        """Say what did not work, without hiding what did."""
        if not failed:
            return
        title, text = {
            "download": (_("Some files could not be downloaded"),
                         _("%d file(s) could not be downloaded. Everything "
                           "else was done.\n\n%s")),
            "upload": (_("Some files could not be uploaded"),
                       _("%d file(s) could not be uploaded. Everything "
                         "else was done.\n\n%s")),
            "delete": (_("Some files could not be deleted"),
                       _("%d file(s) could not be deleted. Everything "
                         "else was done.\n\n%s")),
        }[verb]
        messagebox.showwarning(
            title,
            text % (len(failed),
                    "\n".join("    %s\n        %s"
                              % (p.rsplit("/", 1)[-1], localise(e))
                              for p, e in failed[:6])))

    def report_losses(self, payload):
        """Say so when the instrument took more than it was asked to.

        Twice on a TDS 784D a directory has come back empty of things
        nobody deleted. It has not been reproduced and the mechanism is
        unknown, so this does not prevent it - it makes it visible.
        Silence is what makes that kind of loss dangerous: without this
        the user finds out weeks later, from the gap.
        """
        lost = payload.get("lost") or []
        if not lost:
            return
        messagebox.showwarning(
            _("Files disappeared"),
            _("%(count)d item(s) vanished from %(folder)s that were not "
              "part of what you deleted:\n\n%(names)s\n\nThis instrument "
              "has been seen to empty a directory on its own. The cause "
              "is not known. Check the folder before writing anything "
              "else to it.")
            % {"count": len(lost), "folder": payload.get("folder") or "",
               "names": self.name_list(lost)})

    def name_list(self, names, limit=10):
        """Names for a dialog: readable, and never a wall of text."""
        shown = "\n".join("    " + n for n in names[:limit])
        if len(names) > limit:
            shown += "\n    ...and %d more" % (len(names) - limit)
        return shown

    # ------------------------------------------------------- navigation
    def navigate(self, path, force=False):
        """Show a folder, from cache if we already classified it."""
        if self.state["busy"]:
            return
        path = path.rstrip("/") or "hd0:"
        if not force and path in self.state["cache"]:
            self.show(self.state["cache"][path])
            return
        self.ui.busy(True)
        self.ui.say(_("Reading %s ...") % path)
        self.w.submit("split", lambda k, p=path: k.listdir_split(p))

    def show(self, payload):
        path = payload["cwd"].rstrip("/")
        self.state["cwd"] = path
        self.state["cache"][path] = payload

        # Recorded here rather than in navigate(), so that history holds
        # only folders actually arrived at - not ones that failed to load.
        # A back or forward step sets this flag so it does not record the
        # move it is itself making.
        hist, at = self.state["history"], self.state["hist_at"]
        self.state.pop("hist_was", None)
        if self.state.pop("no_record", False):
            pass
        elif hist and at > 0 and hist[at - 1] == path:
            # Arriving where we came from - going Up out of a folder we had
            # just entered, typically. Treated as a step back rather than a
            # new destination, so Forward still leads back down. Appending
            # instead would leave Forward greyed out and the way back only
            # reachable by clicking into the folder again.
            self.state["hist_at"] = at - 1
        elif not hist or hist[at] != path:
            del hist[at + 1:]
            hist.append(path)
            self.state["hist_at"] = len(hist) - 1

        self.ent_path.delete(0, "end")
        self.ent_path.insert(0, path)

        self.ensure_node(path)
        self.fill_tree(path, payload["dirs"])
        if self.tree.exists(path):
            self.tree.see(path)
            # Focus as well as selection. selection_set fires
            # <<TreeviewSelect>>, whose handler reads tree.focus() - and if
            # that still points at wherever the tree was last clicked, it
            # navigates straight back there. Opening a folder from the file
            # pane then appeared to do nothing at all.
            self.tree.selection_set(path)
            self.tree.focus(path)

        self.fill_list(path, payload)
        self.ui.busy(False)
        if payload.get("no_media"):
            self.ui.say(_("No disk in the drive"))
        else:
            self.ui.say(_("%d items  (%d folders, %d files)")
                        % (len(payload["dirs"]) + len(payload["files"]),
                           len(payload["dirs"]), len(payload["files"])))

    def size_of(self, path, name):
        """Known size in bytes, or None. Sizes only exist once transferred."""
        text = self.state["sizes"].get(self.join(path, name))
        try:
            return int(text.replace(",", "")) if text else None
        except (AttributeError, ValueError):
            return None

    def fill_list(self, path, payload):
        """Draw the file pane from a listing, in the current sort order.

        Separate from show() so that clicking a column header can reorder
        what is already on screen without asking the instrument again -
        each listing costs seconds, and sorting should cost nothing.
        """
        col, rev = self.state["sort"]
        folders = list(payload["dirs"])
        files = list(payload["files"])

        def key(name, is_dir):
            if col == "size":
                # Unknown sizes sort last whichever way the arrow points,
                # rather than pretending to be zero.
                n = self.size_of(path, name)
                return (n is None, n if n is not None else 0, name.upper())
            if col == "type":
                return (type_of(name) if not is_dir else "", name.upper())
            return (name.upper(),)

        folders.sort(key=lambda n: key(n, True), reverse=rev)
        files.sort(key=lambda n: key(n, False), reverse=rev)

        # Folders stay above files whichever column is sorted, as they do
        # in Explorer: the sort orders each group, it does not merge them.
        self.lst.delete(*self.lst.get_children())
        self.state["rows"] = {}
        # Row ids are positional, not derived from the name. Naming them
        # after the file made two entries with the same name a fatal
        # TclError, and the instrument can report the same name twice.
        n = 0
        for d in folders:
            iid = "R%d" % n
            self.lst.insert("", "end", iid=iid, text=d,
                            image=self.icon_for(d, True),
                            values=("", _("File folder")))
            self.state["rows"][iid] = ("dir", d)
            n += 1
        for f in files:
            iid = "R%d" % n
            self.lst.insert("", "end", iid=iid, text=f,
                            image=self.icon_for(f, False),
                            values=(self.state["sizes"].get(self.join(path, f),
                                                            ""),
                                    type_of(f)))
            self.state["rows"][iid] = ("file", f)
            n += 1
        self.show_sort_arrow()

    def show_sort_arrow(self):
        """Mark the sorted column, the way Explorer marks it."""
        col, rev = self.state["sort"]
        for ident, key, label in self.HEADINGS:
            mark = ("  ▲" if not rev else "  ▼") if key == col else ""
            self.lst.heading(ident, text=_(label) + mark)

    def sort_by(self, key):
        """Click a heading to sort by it; click the same one to reverse."""
        col, rev = self.state["sort"]
        self.state["sort"] = (key, not rev if key == col else False)
        payload = self.state["cache"].get(self.state["cwd"])
        if payload:
            keep = [self.state["rows"][i][1] for i in self.lst.selection()
                    if i in self.state.get("rows", {})]
            self.fill_list(self.state["cwd"], payload)
            # Selection follows the files, not the row positions, so
            # sorting does not silently change what Delete would act on.
            again = [i for i,
                     (_k, n) in self.state["rows"].items() if n in keep]
            if again:
                self.lst.selection_set(again)

    # ------------------------------------------------------ folder tree
    def ensure_node(self, path):
        """Create the node for `path` and every ancestor, in order."""
        parts = path.rstrip("/").split("/")
        cur = parts[0]
        if not self.tree.exists(cur):
            self.tree.insert("", "end", iid=cur, text=" " + cur, open=True,
                             image=self.icon_for("drive", True))
        for p in parts[1:]:
            nxt = cur + "/" + p
            if not self.tree.exists(nxt):
                self.tree.insert(cur, "end", iid=nxt, text=" " + p,
                                 image=self.icon_for(p, True))
            cur = nxt
        return cur

    def fill_tree(self, path, dirs):
        node = self.ensure_node(path)
        for child in self.tree.get_children(node):
            self.tree.delete(child)
        for d in dirs:
            iid = self.join(path, d)
            # The instrument can list a name twice, and a row id is the
            # path; a second insert raised half way through show().
            if self.tree.exists(iid):
                continue
            self.tree.insert(node, "end", iid=iid, text=" " + d,
                             image=self.icon_for(d, True))
            # a placeholder so the expander arrow appears without us having
            # to classify the grandchildren yet - that is the expensive part
            self.tree.insert(iid, "end", iid=iid + "/~", text="")
        self.tree.item(node, open=True)

    def on_expand(self, _evt=None):
        node = self.tree.focus()
        if not node or self.state["busy"]:
            return
        kids = self.tree.get_children(node)
        if len(kids) == 1 and kids[0].endswith("/~"):
            self.navigate(node)

    def on_tree_select(self, _evt=None):
        node = self.tree.focus()
        if node and node != self.state["cwd"] and not self.state["busy"]:
            self.navigate(node)

    def do_back(self, _evt=None):
        if self.state["busy"] or self.state["hist_at"] <= 0:
            return
        # Where it was, so a listing that fails can put it back (D8).
        self.state["hist_was"] = self.state["hist_at"]
        self.state["hist_at"] -= 1
        self.state["no_record"] = True
        self.navigate(self.state["history"][self.state["hist_at"]])

    def do_forward(self, _evt=None):
        if (self.state["busy"]
                or self.state["hist_at"] >= len(self.state["history"]) - 1):
            return
        self.state["hist_was"] = self.state["hist_at"]
        self.state["hist_at"] += 1
        self.state["no_record"] = True
        self.navigate(self.state["history"][self.state["hist_at"]])

    def do_up(self, _evt=None):
        p = self.parent_of(self.state["cwd"] or "")
        if p:
            self.navigate(p)

    def do_refresh(self):
        if self.state["cwd"]:
            self.navigate(self.state["cwd"], force=True)

    def do_download(self, _evt=None):
        """Save the selection to the PC. Also what double-click does.

        One file asks where to put it, as Save As does everywhere. Several
        files ask for a folder instead, because asking for ten filenames in
        a row would be a poor way to treat anybody.
        """
        folders = self.sel_folders()
        if folders and not self.sel_files():
            if self.state["busy"]:
                return
            if len(folders) > 1:
                messagebox.showinfo(
                    _("One folder at a time"),
                    _("Saving a folder brings down everything inside it, so "
                    "they are done one at a time."))
                return
            destdir = filedialog.askdirectory(
                title=_("Save '%s' and its contents into") % folders[0],
                mustexist=True)
            if not destdir:
                return
            src = self.join(self.state["cwd"], folders[0])
            self.ui.busy(True, "steps")
            self.ui.say(_("Reading the contents of %s ...") % src)
            self.w.submit("tree",
                          lambda k, p=src, d=destdir: k.download_tree(p, d))
            return

        names = self.sel_files()
        if not names or self.state["busy"]:
            return
        if len(names) == 1:
            dest = filedialog.asksaveasfilename(initialfile=names[0])
            if not dest:
                return
            self.state["saveas"] = dest
            # Marquee: one file arrives as a single stream with no length
            # in front of it, so there is nothing to measure against.
            self.ui.busy(True, "wait")
            self.ui.say(_("Downloading %s ...") % names[0])
            self.w.submit("download",
                          lambda k: k.read(self.join(self.state["cwd"],
                                                     names[0])))
            return

        destdir = filedialog.askdirectory(
            title=_("Save %d files to folder") % len(names), mustexist=True)
        if not destdir:
            return
        clashes = [n for n in names
                   if os.path.exists(os.path.join(destdir, n))]
        if clashes and not messagebox.askyesno(
                _("Replace files?"),
                _("%d of these already exist in that folder and will be "
                  "replaced:\n\n%s\n\nContinue?")
                % (len(clashes), self.name_list(clashes)),
                icon="warning", default="no"):
            return
        paths = [self.join(self.state["cwd"], n) for n in names]
        self.ui.busy(True, "steps")
        self.ui.say(_("Downloading %d files ...") % len(names))
        self.w.submit("downloads",
                      lambda k: k.download_many(paths, destdir))

    def do_open(self, _evt=None):
        """Double-click: open a folder, save a file.

        Explorer's single gesture means two things depending on what is
        under it, and the instrument has nothing that can open a file, so
        saving it is the only sensible reading for one.
        """
        folders = self.sel_folders()
        if folders and not self.sel_files():
            self.navigate(self.join(self.state["cwd"], folders[0]))
            return
        self.do_download()

    def upload_possible(self):
        """False, with an explanation, on a firmware that cannot receive.

        The Upload button is greyed on such an instrument, but a drop from
        Explorer does not go through the button, so the check lives here
        where both routes pass.
        """
        if self.by_name["Upload"] not in (self.state.get("cannot") or ()):
            return True
        messagebox.showinfo(
            _("Cannot upload to this instrument"),
            _("This instrument's firmware has no FILESYSTEM:WRITEFILE "
              "command, so files cannot be sent to it over GPIB.\n\n"
              "Browsing, downloading, creating folders and deleting all "
              "still work."))
        return False

    def do_upload(self):
        if self.state["busy"] or not self.state["cwd"]:
            return
        if not self.upload_possible():
            return
        src = filedialog.askopenfilename()
        if not src:
            return
        with open(src, "rb") as fh:
            data = fh.read()
        # The PC name is very unlikely to be 8.3-clean, so it is offered as
        # a starting point and corrected here rather than silently chopped
        # to twelve characters on the way out, which is what used to happen.
        name = os.path.basename(src).upper()
        while True:
            why = check_83(name)
            if why is None:
                break
            messagebox.showwarning(_("Cannot use that name"), why)
            name = simpledialog.askstring(
                _("Name on the instrument"), _("Name to save it as:"),
                initialvalue=name)
            if name is None:
                return
            name = name.strip().upper()
        if not messagebox.askyesno(
                _("Upload"),
                _("Write %s (%s bytes) to %s ?\n"
                  "\n"
                  "It is read back and compared afterwards; the upload "
                  "reports failure rather than success on a guess.")
                % (name, format(len(data), ","),
                   self.join(self.state["cwd"], name))):
            return
        self.ui.busy(True, "steps")
        self.ui.say(_("Uploading %s, then verifying ...") % name)
        self.w.submit("upload",
                      lambda k: k.write_verified(self.join(self.state["cwd"],
                                                           name), data))

    def est_time(self, nbytes, nfiles, dest=None):
        """A rough figure for how long an upload will take, in words.

        Throughput depends on the medium, not the instrument: a hard disk
        reads at about 33 KB/s, a floppy at about 4 KB/s - measured at 3.9
        on a TDS 784C reading 789,504 bytes and 3.2 on a TDS 640A. Quoting
        the hard disk figure for a floppy underestimates by eight times,
        which turns "about a minute" into ten and looks like a hang.

        Every file is written and then read back to verify, so the payload
        crosses the bus twice - but not at the same speed in each
        direction, and that is the difference between a useful estimate
        and a useless one. Writing is eleven times slower than reading:
        measured on a TDS 784D at 2842 to 2992 B/s for payloads from 4 kB
        to 256 kB, against 32.5 KB/s reading the same files back. Counting
        both crossings at the read rate said seven minutes for a 7 MB
        folder that really takes three quarters of an hour.

        A floppy write has now been measured, and the assumption it
        replaces - no faster than a floppy read - was wrong in the
        optimistic direction. A TDS 754D with a real 1.44 MB drive
        writes at 1,672 B/s and reads at 4,215, so writing is the slow
        half on a floppy as it is on a hard disk. The old 4,000 made a
        floppy upload look two and a half times quicker than it is.
        """
        # The drive being written to, which a drop onto the tree can make
        # a different one from the folder on show (D13).
        floppy = (dest or self.state["cwd"] or "").lower().startswith("fd")
        read_rate = 4000.0 if floppy else 33000.0
        write_rate = 1600.0 if floppy else 2900.0
        secs = nfiles * 1.6 + nbytes / write_rate + nbytes / read_rate
        if secs < 90:
            return _("about %d seconds") % max(2, int(round(secs)))
        return _("about %d minutes") % int(round(secs / 60.0))

    def drop_plan(self, paths, dest_folder):
        """What a drop would do: folders to make, files to write.

        Returns (made, items, renamed). `made` is remote folder paths,
        parents before children, because a folder cannot be made inside
        one that is not there yet. `items` is (local path, remote path).

        Every segment goes through the 8.3 rule, folder names included -
        the instrument holds a directory name to the same eight and
        three as a file's. Names are kept unique per remote folder
        rather than across the whole drop: two files called the same
        thing in two different folders are not a clash, and treating
        them as one would rename a file for no reason.

        A name already on the instrument is NOT stepped around. Dropping
        a folder onto a folder of the same name merges into it and
        replaces the files that clash, which is what dragging a folder
        onto a folder does everywhere else. The earlier version seeded
        this with the destination's own listing, so a copy of APP landed
        beside the instrument's as APP~1 and nothing was ever updated -
        which is not a rename anyone asked for.
        """
        made, items, renamed = [], [], []
        taken = {}

        def slot(where, leaf):
            """A free 8.3 name for `leaf` inside remote folder `where`."""
            names = taken.setdefault(where, [])
            name = to_83(leaf, names)
            names.append(name)
            if name != leaf.upper():
                renamed.append((leaf, name))
            return name

        for src in paths:
            if os.path.isfile(src):
                items.append((src, self.join(dest_folder,
                                             slot(dest_folder,
                                                  os.path.basename(src)))))
                continue
            if not os.path.isdir(src):
                continue
            # The dropped folder itself, then everything under it. Each
            # directory's remote path is worked out as it is reached and
            # kept, so its children can be hung off it - os.walk visits
            # a parent before its children, so it is always there.
            top = os.path.basename(src.rstrip("\\/")) or src
            where = {src: self.join(dest_folder, slot(dest_folder, top))}
            made.append(where[src])
            for here, dirs, names in os.walk(src):
                dirs.sort()
                remote = where[here]
                for one in dirs:
                    where[os.path.join(here, one)] = self.join(
                        remote, slot(remote, one))
                    made.append(where[os.path.join(here, one)])
                for name in sorted(names):
                    items.append((os.path.join(here, name),
                                  self.join(remote, slot(remote, name))))
        return made, items, renamed

    def do_drop(self, paths, dest_folder):
        """Upload what was dropped from Explorer, after showing the plan.

        Files and whole folders both. Nothing is sent until the user has
        seen the destination, what each file will be called once it is
        subject to 8.3, and roughly how long it will take.
        """
        if self.state["busy"]:
            # Said, not swallowed (D11): a drop made while a job runs used
            # to vanish without a word.
            self.ui.say(
                _("Busy - drop the files again when this job has finished"))
            return
        if not dest_folder:
            return
        if not self.upload_possible():
            return
        made, plan, renamed = self.drop_plan(paths, dest_folder)
        if not plan and not made:
            return
        # What is already there, so the confirmation can say what will be
        # replaced. Only the destination folder itself is known - the
        # listing is what the pane is showing - so a file going into a
        # subfolder of the drop is not counted. Saying "at least" would
        # be honest and is not worth the words; the folders being merged
        # into are named on the line above it.
        cached = self.state["cache"].get(dest_folder)
        here = set(n.upper()
                   for n in ((cached["dirs"] + cached["files"])
                             if cached else []))
        over = sorted(d.rsplit("/", 1)[-1] for _s, d in plan
                      if d.rsplit("/", 1)[0] == dest_folder.rstrip("/")
                      and d.rsplit("/", 1)[-1] in here)
        into = sorted(d.rsplit("/", 1)[-1] for d in made
                      if d.rsplit("/", 1)[0] == dest_folder.rstrip("/")
                      and d.rsplit("/", 1)[-1] in here)

        total = sum(os.path.getsize(s) for s, _ in plan)
        if made:
            lines = [_("Upload %(count)d file(s) in %(folders)d folder(s) "
                       "to %(where)s ?")
                     % {"count": len(plan), "folders": len(made),
                        "where": dest_folder}, ""]
        else:
            lines = [_("Upload %(count)d file(s) to %(where)s ?")
                     % {"count": len(plan), "where": dest_folder}, ""]
        # The remote path relative to where it is going, so a tree reads
        # as a tree rather than as a list of full paths repeating the
        # destination on every line.
        cut = len(dest_folder.rstrip("/")) + 1
        lines.append("\n".join(
            "    %s  ->  %s" % (os.path.basename(s), d[cut:])
            for s, d in plan[:12]))
        if len(plan) > 12:
            lines.append(_("    ...and %d more") % (len(plan) - 12))
        lines += ["", _("%(bytes)s bytes, %(time)s.")
                  % {"bytes": format(total, ","),
                     "time": self.est_time(total, len(plan), dest_folder)}]
        if renamed:
            lines.append(_("%d name(s) were shortened to fit the "
                           "instrument's 8.3 limit.") % len(renamed))
        if into:
            lines.append(_("%(count)d folder(s) already there will be "
                           "merged into: %(names)s")
                         % {"count": len(into), "names": ", ".join(into)})
        if over:
            lines.append(_("%(count)d file(s) already there will be "
                           "replaced: %(names)s")
                         % {"count": len(over), "names": ", ".join(over)})
        lines.append("")
        lines.append(_("Each file is read back and compared after "
                       "writing."))

        if not messagebox.askyesno(_("Upload"), "\n".join(lines)):
            return
        self.ui.busy(True, "steps")
        if made:
            self.ui.say(_("Uploading %(count)d file(s) and %(folders)d "
                          "folder(s) to %(where)s ...")
                        % {"count": len(plan), "folders": len(made),
                           "where": dest_folder})
        else:
            self.ui.say(_("Uploading %(count)d file(s) to %(where)s ...")
                        % {"count": len(plan), "where": dest_folder})
        self.w.submit("uploads",
                      lambda k, m=made, it=plan: k.upload_plan(m, it))

    # The drop is handled after the <<Drop>> callback has returned (D11).
    # On Windows, Explorer waits inside DoDragDrop until the target's Drop
    # returns, so asking the upload question from inside it froze the
    # Explorer window for as long as the question was open.
    def on_drop_list(self, evt):
        """Dropped on the file pane: into the folder currently shown."""
        paths = list(self.root.tk.splitlist(evt.data))
        self.root.after(0, lambda: self.do_drop(paths, self.state["cwd"]))

    def on_drop_tree(self, evt):
        """Dropped on the tree: into the folder actually under the pointer,
        which is what Explorer does. Missing the rows drops nowhere."""
        node = self.tree.identify_row(evt.y_root - self.tree.winfo_rooty())
        if not node or node.endswith("/~"):
            return
        paths = list(self.root.tk.splitlist(evt.data))
        self.root.after(0, lambda: self.do_drop(paths, node))

    def allow_system(self, paths):
        """Ask before deleting anything that is the instrument's own.

        Asked before the ordinary delete confirmation rather than instead
        of it, so a system file takes two answers where an ordinary one
        takes the one it always took. These used to be refused outright,
        on the belief that putting one back meant reimaging the card. It
        does not - they load back over GPIB - so the choice belongs to
        whoever owns the instrument, and this is where it is put to them.
        """
        system = [(p.rstrip("/").rsplit("/", 1)[-1], Worker.system_reason(p))
                  for p in paths]
        system = [(n, why) for n, why in system if why]
        if not system:
            return True
        if len(system) == 1:
            body = _("'%(name)s' is part of the instrument's own "
                     "software.\n\n%(why)s") % {"name": system[0][0],
                                                "why": system[0][1]}
        else:
            body = _("%(count)d of these are part of the instrument's own "
                     "software:\n\n%(items)s") % {
                         "count": len(system),
                         "items": self.name_list([n for n, _w in system])}
        return messagebox.askyesno(
            _("Part of the instrument's software"),
            "%s\n\n%s" % (body, _(
                "Every system file can be loaded back over GPIB, so this "
                "can be undone - but the instrument will not run its "
                "applications until it is. Delete it anyway?")),
            icon="warning", default="no", parent=self.root)

    def do_delete(self, _evt=None):
        """Delete whatever is selected - Windows has one Delete, so do we.

        A file selected in the list deletes directly. A folder selected in
        the tree is surveyed first so the confirmation can say how much is
        in it. Both end at a Yes/No box; nothing here asks you to type.
        """
        if self.state["busy"]:
            return
        # Which pane has focus decides what Delete means, as in Explorer.
        # Without this, a file left selected in the list would be deleted
        # when you meant the folder you just clicked in the tree.
        focused = self.root.focus_get()
        target_is_tree = focused is self.tree
        # A folder selected in the file pane is deleted the same way as one
        # selected in the tree: surveyed first, then a confirmation naming
        # what is inside it. Mixing folders and files in one delete is
        # refused rather than half-done.
        if not target_is_tree and self.sel_folders():
            if self.sel_files():
                messagebox.showwarning(
                    _("Select one kind at a time"),
                    _("The selection contains both folders and files.\n\n"
                    "Delete folders and files separately, so that what is "
                    "about to be removed can be shown properly first."))
                return
            if len(self.sel_folders()) > 1:
                messagebox.showwarning(
                    _("One folder at a time"),
                    _("Folders are removed with everything inside them, so "
                    "they are deleted one at a time and each is shown to "
                    "you first."))
                return
            node = self.join(self.state["cwd"], self.sel_folders()[0])
            why = Worker.refuse_reason(node)
            if why:
                messagebox.showwarning(_("Cannot delete"), why)
                return
            if not self.allow_system([node]):
                return
            self.ui.busy(True)
            self.ui.say(_("Checking the contents of %s ...") % node)
            self.w.submit("survey", lambda k, p=node: k.survey(p))
            return

        names = [] if target_is_tree else self.sel_files()
        if names:
            paths = [self.join(self.state["cwd"], n) for n in names]
            # Every name is checked before any of them is deleted. Removing
            # half a selection and then stopping at a refused file would
            # be the worst of both outcomes.
            refused = [(n, Worker.refuse_reason(p))
                       for n, p in zip(names, paths)
                       if Worker.refuse_reason(p)]
            if refused:
                messagebox.showwarning(
                    _("Cannot delete"),
                    refused[0][1] if len(refused) == 1 else
                    _("%d of the %d selected items cannot be deleted, so "
                      "nothing was deleted:\n\n%s")
                    % (len(refused), len(names),
                       self.name_list([n for n, _ in refused])))
                return
            if not self.allow_system(paths):
                return
            if len(names) == 1:
                msg = (_("Are you sure you want to permanently delete "
                         "'%s'?\n\n"
                         "There is no Recycle Bin on the instrument.")
                       % names[0])
            else:
                msg = (_("Are you sure you want to permanently delete "
                         "these %d files?\n\n%s\n\nThere is no Recycle Bin "
                         "on the instrument.")
                       % (len(names), self.name_list(names)))
            if not messagebox.askyesno(_("Delete File"), msg,
                                       icon="warning", default="no"):
                return
            self.ui.busy(True)
            if len(names) == 1:
                self.ui.say(_("Deleting %s ...") % names[0])
                self.w.submit("delete", lambda k, p=paths[0]: k.delete(p))
            else:
                self.ui.busy(True, "steps")
                self.ui.say(_("Deleting %d files ...") % len(names))
                self.w.submit("deletes", lambda k, p=paths: k.delete_many(p))
            return

        node = self.tree.focus()
        if not node:
            return
        why = Worker.refuse_reason(node)
        if why:
            messagebox.showwarning(_("Cannot delete"), why)
            return
        if not self.allow_system([node]):
            return
        self.ui.busy(True)
        self.ui.say(_("Checking the contents of %s ...") % node)
        self.w.submit("survey", lambda k, p=node: k.survey(p))

    def confirm_rmdir(self, path, names):
        """Standard Windows folder-delete confirmation: one Yes/No click."""
        self.ui.busy(False)
        leaf = path.rstrip("/").rsplit("/", 1)[-1]
        if names:
            msg = (_("Are you sure you want to permanently delete the "
                     "folder '%(name)s' and the %(count)d item(s) it "
                     "contains?\n\n%(items)s\n\nThere is no Recycle Bin "
                     "on the instrument, and this cannot be undone.")
                   % {"name": leaf, "count": len(names),
                      "items": ", ".join(names[:8])
                      + (", ..." if len(names) > 8 else "")})
        else:
            msg = (_("Are you sure you want to permanently delete the empty "
                     "folder '%s'?") % leaf)
        if not messagebox.askyesno(_("Delete Folder"), msg,
                                   icon="warning", default="no"):
            self.ui.say(_("Delete cancelled"))
            return
        # A determinate bar: removing a folder is now a walk of the tree
        # inside it, one folder at a time, and on a big one that is
        # minutes. It used to be a single command and a spinner.
        self.ui.busy(True, "steps")
        self.ui.say(_("Deleting %s ...") % path)
        self.w.submit("rmdir", lambda k, p=path: k.rmdir(p))

    def do_duplicate(self):
        """Copy the selected file or folder, on the instrument.

        The bytes never cross GPIB: 387 kB/s against the 3.0 kB/s an upload
        of the same file would run at. Anything already on the instrument
        should be duplicated this way rather than downloaded and sent back.

        One item at a time, and it only ever copies within the folder on
        screen. Copying somewhere else wants a destination picker, which is
        a bigger thing than this is worth until someone asks for it.
        """
        if self.state["busy"] or not self.state["cwd"]:
            return
        folders, files = self.sel_folders(), self.sel_files()
        if len(folders) + len(files) != 1:
            messagebox.showwarning(
                _("One at a time"),
                _("Select a single file or folder to duplicate."))
            return
        is_dir = bool(folders)
        name = (folders or files)[0]
        taken = set()
        cached = self.state["cache"].get(self.state["cwd"])
        if cached:
            taken = {n.upper() for n in cached["dirs"] + cached["files"]}

        suggestion = suggest_copy_name(name, taken)
        while True:
            new = simpledialog.askstring(
                _("Duplicate"),
                _("Name for the copy of '%s':") % name,
                initialvalue=suggestion)
            if new is None:
                return
            new = new.strip().upper()
            why = check_83(new)
            if why is not None:
                messagebox.showwarning(_("Cannot use that name"), why)
                suggestion = new
                continue
            if new in taken:
                messagebox.showwarning(
                    _("Name already in use"),
                    _("'%s' already exists in %s. Choose another name.")
                    % (new, self.state["cwd"]))
                suggestion = new
                continue
            break

        source = self.join(self.state["cwd"], name)
        dest = self.join(self.state["cwd"], new)
        # A folder copy is a walk, so it gets the determinate bar for the
        # same reason removing one does.
        self.ui.busy(True, "steps" if is_dir else None)
        self.ui.say(_("Copying %s ...") % name)
        self.w.submit("copy",
                      lambda k, s=source, d=dest, f=is_dir: k.copy(s, d, f))

    def do_format(self):
        """Format the volume being browsed, after asking twice.

        Twice, and not because two boxes are twice as safe. The first
        says which volume and what is on it; the second is a plain
        statement of what will not be there afterwards, worded for the
        volume in hand, because formatting the hard disk and formatting
        a floppy are not the same act at all. A floppy holds whatever
        somebody put there. The hard disk holds the Java runtime, and
        an instrument without it runs no applications until it is loaded
        back.

        The instrument has this on its own front panel too. It is here
        because a card that is being reloaded over GPIB wants emptying
        over GPIB, and walking to the front panel between steps is not
        an improvement.
        """
        if self.state["busy"] or not self.state["cwd"]:
            return
        drive = self.state["cwd"].split("/")[0]
        hard = not drive.upper().startswith("FD")
        held = self.state["cache"].get(drive) or {}
        count = len(held.get("dirs") or []) + len(held.get("files") or [])
        if not messagebox.askyesno(
                _("Format %s?") % drive,
                _("This erases everything on %(drive)s. There is no "
                  "Recycle Bin on the instrument and no undo.")
                % {"drive": drive}
                + ("\n\n" + _("%d item(s) are in its root.") % count
                   if count else "")
                + "\n\n" + _("Continue?"),
                icon="warning", default="no", parent=self.root):
            return
        second = (_("The hard disk holds the Java runtime and every "
                    "shipped application. After this the instrument will "
                    "not run any of them until they are loaded back over "
                    "GPIB.")
                  if hard else
                  _("Everything on the floppy will be gone."))
        if not messagebox.askyesno(
                _("Last chance"),
                "%s\n\n%s" % (second,
                              _("Format %s now?") % drive),
                icon="warning", default="no", parent=self.root):
            return
        self.ui.busy(True, "wait")
        self.ui.say(_("Formatting %s ...") % drive)
        self.w.submit("format", lambda k,
                      d=drive: k.format_volume({"drive": d}))

    def do_mkdir(self, where=None):
        """Ask for a folder name, and keep asking until it is a legal one.

        `where` is the folder it goes in: the one on show unless the tree's
        right-click menu names another.

        The instrument accepts an illegal name without complaint and then
        does nothing with it, so the name is checked here and the reason is
        shown with the offending characters named. The typed text is put
        back in the box so it can be corrected rather than retyped.
        """
        where = where or self.state["cwd"]
        if self.state["busy"] or not where:
            return
        name = ""
        while True:
            name = simpledialog.askstring(
                _("New Folder"), _("Name for the new folder:"),
                initialvalue=name)
            if name is None:
                return
            name = name.strip()
            why = check_83(name)
            if why is None:
                break
            messagebox.showwarning(_("Cannot use that name"), why)

        existing = self.state["cache"].get(where)
        if existing:
            taken = [n.upper() for n in existing["dirs"] + existing["files"]]
            if name.upper() in taken:
                messagebox.showwarning(
                    _("Name already in use"),
                    _("'%s' already exists in %s. Choose another name.")
                    % (name.upper(), where))
                return
        self.ui.busy(True)
        self.ui.say(_("Creating %s ...") % name.upper())
        self.w.submit("mkdir",
                      lambda k, n=name,
                      d=where: k.mkdir(self.join(d, n.upper())))

    def do_goto(self, _evt=None):
        p = self.ent_path.get().strip()
        if p:
            self.navigate(p)

    def on_files_tab(self):
        """Is the file tab the one being looked at?

        A key bound on the toplevel fires wherever the pointer is, so
        the tab has to be asked rather than assumed. Both of these do
        something irreversible on the instrument.
        """
        try:
            return self.tabs.select() == str(self.filetab)
        except Exception:
            return False

    # And only with a file pane focused: the same keys edit text in the
    # address bar, where Backspace used to jump a folder and overwrite
    # what was being typed.
    def in_file_panes(self):
        return self.on_files_tab() and self.root.focus_get() in (self.lst,
                                                                 self.tree)

    def menus_relabel(self):
        """The right-click menus, in the language now in use (D9)."""
        for menu, words in (
                (self.menu_empty, ("New folder...", None, "Refresh",
                                   "Upload files...")),
                (self.menu_dir, ("Open", "Refresh", None, "New folder...",
                                 "Delete"))):
            for at, word in enumerate(words):
                if word:
                    menu.entryconfigure(at, label=_(word))

    def on_list_click(self, evt):
        """Clicking the empty area below the rows clears the selection.

        Explorer does this, and without it a file stays selected out of
        sight while Delete and Download go on acting upon it.
        """
        if not self.lst.identify_row(evt.y):
            self.lst.selection_remove(*self.lst.selection())

    def popup_list(self, evt):
        row = self.lst.identify_row(evt.y)
        if not row:
            # Right-click on empty space: clear the selection and offer the
            # things that apply to the folder rather than to a file.
            self.lst.selection_remove(*self.lst.selection())
            self.menu_empty.entryconfigure(
                0, state="normal" if self.state["cwd"] else "disabled")
            self.menu_empty.tk_popup(evt.x_root, evt.y_root)
            return
        # Right-clicking inside an existing selection keeps it, as Explorer
        # does; right-clicking elsewhere selects just that row.
        if row not in self.lst.selection():
            self.lst.selection_set(row)
        self.lst.focus(row)
        # The keyboard as well as the focused row: Delete decides what it
        # acts on from which widget has the keyboard (D7).
        self.lst.focus_set()
        # Rebuilt each time rather than reconfigured by index: the menu has
        # a different shape for a folder than for a selection of files, and
        # index bookkeeping across the two is a bug waiting to happen.
        n = len(self.lst.selection())
        folders = self.sel_folders()
        self.menu_file.delete(0, "end")
        if folders and not self.sel_files():
            self.menu_file.add_command(label=_("Open"), command=self.do_open)
            self.menu_file.add_command(label=_("Save as..."),
                                       command=self.do_download)
        else:
            self.menu_file.add_command(
                label=(_("Save as...") if n < 2
                       else _("Save %d files as...") % n),
                command=self.do_download)
        self.menu_file.add_separator()
        # Only for a single item: a copy needs a name, and asking for
        # several names one box at a time is worse than not offering it.
        if n == 1:
            self.menu_file.add_command(label=_("Duplicate..."),
                                       command=self.do_duplicate)
        self.menu_file.add_command(
            label=_("Delete") if n < 2 else _("Delete %d items") % n,
            command=self.do_delete)
        self.menu_file.tk_popup(evt.x_root, evt.y_root)

    def popup_tree(self, evt):
        row = self.tree.identify_row(evt.y)
        if row:
            self.tree.selection_set(row)
            self.tree.focus(row)
            self.tree.focus_set()
            self.menu_dir.tk_popup(evt.x_root, evt.y_root)

    def forget_listings(self, *paths):
        """Drop the cached listings of these folders and all beneath them.

        navigate() serves a cached folder without asking the instrument,
        so a job that changes more than one folder has to forget every
        one it touched (D5). Forgetting only the folder on show left a
        formatted drive's subfolders, a removed tree's children and an
        upload's new folders listed as they had been.
        """
        for path in paths:
            top = (path or "").rstrip("/")
            if not top:
                continue
            for key in list(self.state["cache"]):
                held = key.rstrip("/")
                if held == top or held.startswith(top + "/"):
                    self.state["cache"].pop(key, None)

    def pump_volumes(self, payload):
        vols = payload["volumes"]
        if not vols:
            # Nothing to browse: no hard disk fitted and no
            # floppy in the drive. Saying so beats pretending
            # hd0: exists and failing to list it.
            self.ui.busy(False)
            self.ui.say(_("No drives found on this instrument"))
            messagebox.showinfo(
                _("No drives found on this instrument"),
                _("This instrument reports no usable drive.\n\n"
                  "A TDS with no hard disk option has only its "
                  "floppy drive, so put a disk in and press "
                  "Refresh."))
            return
        for v in vols:
            if not self.tree.exists(v):
                self.tree.insert("", "end", iid=v, text=" " + v,
                                 image=self.icon_for("drive", True))
                self.tree.insert(v, "end", iid=v + "/~", text="")
        # Always open at the root of the hard disk. The
        # instrument's own current directory is wherever the
        # last program to talk to it happened to leave it -
        # which may be three levels inside an application
        # folder, or fd0: with no floppy in the drive. Neither
        # is where a file manager should start.
        start = "hd0:" if "hd0:" in vols else vols[0]
        self.ui.say(_("Volumes: %s") % ", ".join(vols))
        # The connect-then-probe sequence has finished, so the
        # busy state it was holding has to be released BEFORE
        # navigating. navigate() declines to do anything while
        # busy, so leaving this set left the volumes listed but
        # the panes empty and every button disabled - which
        # looks exactly like a failure to connect.
        self.ui.busy(False)
        self.navigate(start)

    def pump_split(self, payload):
        self.show(payload)

    def pump_download(self, payload):
        self.ui.busy(False)
        self.set_size(os.path.basename(payload["path"]),
                      len(payload["data"]))
        where = self.ui.keep_file(self.state["saveas"], payload["data"])
        if where:
            self.ui.say(_("Saved %(bytes)s bytes to %(where)s")
                        % {"bytes": format(len(payload["data"]), ","),
                           "where": where})

    def pump_tree(self, payload):
        self.ui.busy(False)
        total = sum(n for _, n in payload["done"])
        self.ui.say(_("Saved %(files)d file(s) in %(folders)d "
                      "folder(s), %(bytes)s bytes, to %(where)s")
                    % {"files": len(payload["done"]),
                       "folders": payload["folders"],
                       "bytes": format(total, ","),
                       "where": payload["dir"]})
        self.report_failures("download", payload["failed"])

    def pump_downloads(self, payload):
        self.ui.busy(False)
        for dest, n in payload["done"]:
            self.set_size(os.path.basename(dest), n)
        total = sum(n for _, n in payload["done"])
        self.ui.say(_("Saved %(files)d files (%(bytes)s bytes) to "
                      "%(where)s")
                    % {"files": len(payload["done"]),
                       "bytes": format(total, ","),
                       "where": payload["dir"]})
        self.report_failures("download", payload["failed"])

    def pump_deletes(self, payload):
        self.ui.busy(False)
        self.ui.say(_("Deleted %d files") % len(payload["done"]))
        self.report_failures("delete", payload["failed"])
        if payload.get("skipped"):
            messagebox.showwarning(
                _("Delete"),
                _("The delete stopped after %(failed)d file(s) "
                  "failed one after another, and %(skipped)d "
                  "file(s) were not attempted.\n\nThat pattern "
                  "means the instrument stopped answering "
                  "rather than that those files are bad.\n\n"
                  "Power cycle the instrument, then try again.")
                % {"failed": len(payload["failed"]),
                   "skipped": len(payload["skipped"])})
        self.report_losses(payload)
        self.navigate(self.state["cwd"], force=True)

    def pump_survey(self, payload):
        self.confirm_rmdir(payload["path"], payload["names"])

    def pump_rmdir(self, payload):
        self.ui.busy(False)
        gone = payload["path"]
        if self.tree.exists(gone):
            self.tree.delete(gone)
        self.forget_listings(gone, self.parent_of(gone))
        self.ui.say(_("Removed %s") % gone + self.mass_storage_note(payload))
        self.report_losses(payload)
        self.navigate(self.parent_of(gone) or self.state["cwd"], force=True)

    def pump_copy(self, payload):
        self.ui.busy(False)
        dest = payload["dest"]
        self.ui.say(_("Copied to %s") % dest.rsplit("/", 1)[-1])
        self.forget_listings(self.state["cwd"], dest.rsplit("/", 1)[0], dest)
        self.navigate(self.state["cwd"], force=True)

    def pump_uploads(self, payload):
        self.ui.busy(False)
        if payload.get("blocked"):
            # Nothing was written - the pre-flight health check
            # refused. Safe to retry once the instrument is well;
            # no override needed.
            messagebox.showwarning(
                _("Upload"),
                _("Nothing was written. The instrument's file "
                  "system was checked first and did not answer "
                  "normally:\n\n%s\n\nThis is usually a wedged "
                  "file system. Power cycle the instrument, "
                  "confirm it lists its files, then try again.")
                % payload["blocked"])
            self.navigate(self.state["cwd"], force=True)
            return
        for dest, n in payload["done"]:
            # Only rows in the folder on screen. A dropped
            # folder writes into subfolders as well, and
            # their leaf names would otherwise be matched
            # against whatever happens to share a name here.
            if dest.rsplit("/", 1)[0] == self.state["cwd"]:
                self.set_size(dest.rsplit("/", 1)[-1], n)
        total = sum(n for _, n in payload["done"])
        if payload.get("folders"):
            self.ui.say(_("Uploaded and verified %(files)d file(s) in "
                          "%(folders)d folder(s), %(bytes)s bytes")
                        % {"files": len(payload["done"]),
                           "folders": payload["folders"],
                           "bytes": format(total, ",")})
        else:
            self.ui.say(_("Uploaded and verified %(files)d file(s), "
                          "%(bytes)s bytes")
                        % {"files": len(payload["done"]),
                           "bytes": format(total, ",")})
        self.report_failures("upload", payload["failed"])
        if payload.get("skipped"):
            # Said on its own and after the failures, because
            # it is a different fact: these were not tried,
            # and the instrument is the reason.
            messagebox.showwarning(
                _("Upload"),
                _("The upload stopped after %(failed)d file(s) "
                  "failed one after another, and %(skipped)d "
                  "file(s) were not attempted.\n\nThat pattern "
                  "means the instrument stopped answering "
                  "rather than that those files are bad. Each "
                  "attempt deletes before it writes, so going "
                  "on would have emptied folders it could not "
                  "refill.\n\nPower cycle the instrument, then "
                  "try again.")
                % {"failed": len(payload["failed"]),
                   "skipped": len(payload["skipped"])})
        if payload.get("unchanged"):
            # Files already on the instrument, byte for byte,
            # were left alone rather than rewritten.
            self.ui.say(_("%(done)d uploaded, %(same)d already there "
                          "and left unchanged")
                        % {"done": len(payload["done"]),
                           "same": len(payload["unchanged"])})
        if payload.get("unhealthy"):
            # Post-flight failure: the files verified, but the
            # root no longer lists. This is the state that
            # precedes a lost disk - say so plainly.
            messagebox.showerror(
                _("Error"),
                _("The files were written and verified, but "
                  "afterwards the instrument's file system no "
                  "longer lists normally:\n\n%s\n\nThis is the "
                  "state that precedes a lost disk. Do not write "
                  "to it again - power cycle the instrument and "
                  "check its files before doing anything else.")
                % payload["unhealthy"])
        self.forget_listings(*sorted(set(d.rsplit("/", 1)[0]
                                         for d, _n in payload["done"])))
        self.navigate(self.state["cwd"], force=True)

    def pump_format(self, payload):
        self.ui.busy(False)
        # FORMAT has no query form and raises no event when it
        # works, so the listing afterwards is the whole
        # report. Names still there means it did not happen.
        if payload["left"]:
            messagebox.showwarning(
                _("The volume did not format"),
                _("%(drive)s still holds %(count)d item(s) "
                  "after the format: %(items)s")
                % {"drive": payload["drive"],
                   "count": len(payload["left"]),
                   "items": self.name_list(payload["left"])})
            self.ui.say(_("%s did not format") % payload["drive"])
        else:
            self.ui.say(_("%s formatted - it is empty")
                        % payload["drive"])
        # The cache is of a volume that no longer has
        # anything in it, so it goes rather than being
        # trusted for the redraw.
        self.forget_listings(payload["drive"])
        self.navigate(payload["drive"], force=True)

    def pump_written(self, label, payload):
        self.ui.busy(False)
        # Named in words rather than by the worker's own
        # label for the job, which is not a sentence in any
        # language and was never going to be translatable.
        done = {"upload": _("Uploaded"),
                "delete": _("Deleted"),
                "mkdir": _("Folder created")}[label]
        # The path, not the whole result: every one of these jobs
        # answers with a dict, and the status bar showed its repr (D6).
        wrote = payload.get("path") or ""
        self.ui.say("%s: %s%s" % (done, wrote,
                                  self.mass_storage_note(payload)))
        # What was written may not be in the folder on show
        # - a waveform goes to WAVEFORM on the drive - so
        # that folder's cached listing goes too, rather
        # than being kept and shown stale later.
        if "/" in wrote:
            self.state["cache"].pop(wrote.rsplit("/", 1)[0], None)
        self.navigate(self.state["cwd"], force=True)


def build(ui):
    """Make the tab, and hand its names to the window as before."""
    tab = FilesTab(ui)
    vars(ui).update(tab_names(tab))
    return tab
