"""The instrument in a file.

A backup is a plain zip, so anything can open it and nothing here has to
be installed to get a file back out. Inside:

    manifest.json    what the instrument was, and a checksum per file
    hd0/...          the hard disk, as it was read
    fd0/...          the floppy, if there was one in the drive
    setup.set        *LRN? - every setting the instrument will say
    refs/REF1.wfm    the stored reference waveforms
    cal.bin          the acquisition board's calibration words

The checksums are the point of the manifest. A zip's own CRC catches a
damaged archive; it says nothing about a file that came off the
instrument wrong, and a bad read over GPIB is silent. Every file is
checksummed as it goes in and can be read back and compared later
without an instrument on the bus.

Nothing here talks to an instrument. This builds the file and checks it;
the Worker gathers what goes in.
"""
import hashlib
import json
import os
import time
import zipfile

from i18n import say

FORMAT = 1
MANIFEST = "manifest.json"
SETUP = "setup.set"
CAL = "cal.bin"
REFS = "refs"
DISK = "hd0"
FLOPPY = "fd0"

#: Part name -> what it looks like inside the zip. Used to say what a
#: bundle holds without unpacking it.
PARTS = (("disk", DISK + "/"), ("floppy", FLOPPY + "/"),
         ("setup", SETUP), ("refs", REFS + "/"), ("cal", CAL))


def digest(data):
    return hashlib.sha1(data).hexdigest()


def safe_name(arc):
    """Is this zip entry safe to write to disk?

    A bundle made here never holds anything else, but the file being
    restored is whatever the user picked, and an entry named
    ../../autoexec.bat is the oldest trick there is.
    """
    if not arc or arc.startswith("/") or "\\" in arc:
        return False
    # A drive letter in a segment restarts os.path.join on Windows:
    # hd0/C:/x would land at C:x.
    return not any(seg == ".." or ":" in seg for seg in arc.split("/"))


def pack(folder, path, about):
    """Zip everything under `folder` into `path`, with a manifest.

    Returns the manifest that was written.
    """
    manifest = dict(about)
    manifest["format"] = FORMAT
    manifest.setdefault("made", time.strftime("%Y-%m-%d %H:%M:%S"))
    files = {}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for here, _dirs, names in os.walk(folder):
            for name in sorted(names):
                full = os.path.join(here, name)
                arc = os.path.relpath(full, folder).replace(os.sep, "/")
                with open(full, "rb") as fh:
                    data = fh.read()
                files[arc] = {"bytes": len(data), "sha1": digest(data)}
                z.writestr(arc, data)
        manifest["files"] = files
        z.writestr(MANIFEST, json.dumps(manifest, indent=1, sort_keys=True))
    return manifest


def manifest_of(path):
    """The manifest out of a bundle, or a sentence saying why not."""
    with zipfile.ZipFile(path) as z:
        try:
            said = z.read(MANIFEST)
        except KeyError:
            raise ValueError(say(
                "%s is a zip file, but not a backup made by this program "
                "- there is no %s inside it.",
                os.path.basename(path), MANIFEST))
    return json.loads(said.decode("utf-8"))


def holds(manifest):
    """Which parts a bundle has, as {name: how many files}."""
    found = {}
    for arc in (manifest.get("files") or {}):
        for name, where in PARTS:
            if arc == where or arc.startswith(where):
                found[name] = found.get(name, 0) + 1
                break
    return found


def verify(path):
    """Read every file back and compare it with the checksum recorded."""
    manifest = manifest_of(path)
    want = manifest.get("files") or {}
    bad, missing = [], []
    with zipfile.ZipFile(path) as z:
        held = set(z.namelist())
        for arc in sorted(want):
            if arc not in held:
                missing.append(arc)
            elif digest(z.read(arc)) != want[arc].get("sha1"):
                bad.append(arc)
    extra = sorted(held - set(want) - {MANIFEST})
    return {"manifest": manifest, "checked": len(want), "bad": bad,
            "missing": missing, "extra": extra,
            "ok": not bad and not missing}


def unpack(path, folder, under=""):
    """Write a bundle's files out. `under` takes one part only.

    Returns [(entry name, path written)].
    """
    out = []
    with zipfile.ZipFile(path) as z:
        for arc in sorted(z.namelist()):
            if arc == MANIFEST or arc.endswith("/"):
                continue
            if under and not (arc == under or arc.startswith(under)):
                continue
            if not safe_name(arc):
                raise ValueError(say("%s holds an entry this will not "
                                     "write: %s", os.path.basename(path), arc))
            dest = os.path.join(folder, *arc.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(z.read(arc))
            out.append((arc, dest))
    return out


def split_commands(text):
    """`text` cut at every ';' that is not inside a quoted string.

    *LRN? carries channel labels, hardcopy file names and MESSAGE text
    in quotes, and any of them can hold a ';'. Cut there, the string was
    sent as two commands, the second a fragment under the previous path.
    A quote inside a string is doubled, which this reads as the string
    closing and opening again - the same thing for finding the cuts.
    """
    out, cur, quote = [], [], None
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ";":
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return out


def setup_lines(text):
    """*LRN? split into commands that can be sent one at a time.

    The reply is one long message. A segment beginning with ':' is a
    whole path; a segment without one continues from the segment before
    it, minus that one's last node - ordinary SCPI. Sending the reply
    back whole works too, but then one command this firmware has not got
    takes the rest of the message with it and there is no saying which.
    """
    out, path = [], []
    for chunk in split_commands((text or "").strip()):
        chunk = chunk.strip()
        if not chunk:
            continue
        head = chunk.split(" ", 1)[0]
        if head.startswith(":"):
            path = head.lstrip(":").split(":")
            out.append(chunk)
        else:
            path = path[:-1] + head.split(":")
            out.append(":" + ":".join(path) + chunk[len(head):])
    return out
