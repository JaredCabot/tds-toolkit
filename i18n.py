"""Translations, loaded from human-readable files in lang/.

One file per language, named by its IETF tag - lang/de.json, lang/fr.json,
lang/pt-BR.json - which is the convention most desktop and web software
follows. JSON rather than gettext .po/.mo: it needs no compiler and no
third-party library, a translator can edit it in Notepad, and a broken file
is rejected with a clear reason rather than silently producing an empty
catalogue.

The keys are the English source strings. That has two consequences worth
knowing:

  * A string with no translation falls back to English on its own, so a
    partial file is useful immediately and nothing ever renders as a bare
    identifier like "btn.refresh".
  * Editing the English wording in the program orphans that entry in every
    language file. `--check-translations` reports which.

Files are looked for beside the program first and inside the bundle
second, so a user can add or correct a language without rebuilding
anything.
"""
import json
import os
import re
import sys

_catalogue = {}
_current = "en"
_languages = {}


def _dirs(appdir):
    """Where language files may live, in the order they are searched."""
    out = []
    if appdir:
        out.append(os.path.join(appdir, "lang"))
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        out.append(os.path.join(bundled, "lang"))
    here = os.path.dirname(os.path.abspath(__file__))
    out.append(os.path.join(here, "lang"))
    seen, unique = set(), []
    for d in out:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def discover(appdir=None):
    """Find every language file. Returns {code: {name, native, path}}.

    A file that will not parse is skipped rather than allowed to stop the
    program starting - a bad translation should cost you that language,
    not the application.
    """
    global _languages
    found = {}
    for folder in _dirs(appdir):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith(".json"):
                continue
            code = os.path.splitext(name)[0]
            if code in found:          # nearer folder already provided it
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                meta = data.get("_meta", {})
                found[code] = {
                    "code": code,
                    "name": meta.get("name", code),
                    "native": meta.get("native", meta.get("name", code)),
                    "path": path,
                }
            except Exception:
                continue
    _languages = found
    return found


def available():
    """Languages, English first and the rest by their native name."""
    langs = list(_languages.values())
    langs.sort(key=lambda l: (l["code"] != "en", l["native"].upper()))
    return langs


def current():
    return _current


def use(code):
    """Switch language. Unknown codes fall back to English, silently."""
    global _catalogue, _current
    entry = _languages.get(code)
    if not entry:
        _catalogue, _current = {}, "en"
        return False
    try:
        with open(entry["path"], encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        _catalogue, _current = {}, "en"
        return False
    data.pop("_meta", None)
    # Blank entries mean "not translated yet" and must fall through to the
    # English key, not render as nothing.
    _catalogue = {k: v for k, v in data.items() if isinstance(v, str) and v}
    _current = code
    return True


def gettext(text):
    """The translation of `text`, or `text` itself."""
    return _catalogue.get(text, text)


def strings(code):
    """Every source string a language file has an entry for.

    Read from the file rather than from whatever is loaded, so it can be
    asked about one language while another is in use.
    """
    entry = _languages.get(code)
    if not entry:
        return []
    try:
        with open(entry["path"], encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    data.pop("_meta", None)
    return [k for k, v in data.items() if isinstance(v, str) and v]


#: Both spellings the program uses. The named form has to be here too:
#: it was left out to begin with, and a checker that cannot see
#: "%(date)s" passes a translation that renames the key, which is a
#: KeyError at the moment that one line of text is written.
_PLACEHOLDER = re.compile(
    r"%(?:%|\(\w+\)[-+ 0#]*\d*(?:\.\d+)?[sdifeEgGxXor]"
    r"|[-+ 0#]*\d*(?:\.\d+)?[sdifeEgGxXor])")


def placeholders(text):
    """The %-substitutions in a string, in the order they appear."""
    return _PLACEHOLDER.findall(text)


def substitutions(text):
    """The placeholders, compared the way the % operator will use them.

    Positional ones are matched in order, because that is how they are
    filled. Named ones are matched as a set: a translation is free to put
    "%(time)s" before "%(date)s" if the language reads better that way,
    and only the names have to agree.
    """
    found = placeholders(text)
    named = sorted(p for p in found if p.startswith("%("))
    rest = [p for p in found if not p.startswith("%(")]
    return named, rest


def audit(sources=None, literals=None):
    """Check the language files. Returns {code: [problem, ...]}.

    Three faults are looked for. The first is the dangerous one: a
    translation whose %-substitutions differ from those of the English it
    replaces. Python's % operator takes its arguments by position, so a
    translation that puts them in the other order - which reads better in
    some languages, and is a natural thing for a translator to do - hands
    a string to %d and raises TypeError. It does so only in that
    language, at the moment that one line of status text is written,
    which is how such a fault reaches a user rather than a test.

    The other two need to know what the program says. `sources` is the
    set of strings written out at a call to gettext, which is what a
    missing entry is measured against. `literals` is every string in the
    program's source, which is what an orphaned entry is measured
    against - wider on purpose, because plenty of translated text is
    reached indirectly, a file type looked up by extension being the
    obvious case. A reworded string vanishes from both, so the orphan is
    still found; a string that only ever arrives through a table is not
    reported as one. Where `literals` is not given, `sources` serves for
    both.
    """
    out = {}
    for code, entry in sorted(_languages.items()):
        try:
            with open(entry["path"], encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            out[code] = ["will not parse: %s" % exc]
            continue
        data.pop("_meta", None)
        problems = []
        for src in sorted(data):
            text = data[src]
            if not isinstance(text, str) or not text:
                continue
            if substitutions(src) != substitutions(text):
                want, got = placeholders(src), placeholders(text)
                problems.append(
                    "%r takes %s but its English takes %s"
                    % (src, ", ".join(got) or "nothing",
                       ", ".join(want) or "nothing"))
        if sources is not None:
            known = set(sources if literals is None else literals)
            for src in sorted(set(data) - known):
                problems.append("%r is no longer used by the program" % src)
            for src in sorted(set(sources) - set(data)):
                problems.append("%r has no entry" % src)
        if problems:
            out[code] = problems
    return out


def _os_language_tags():
    """Language tags the operating system suggests, best first.

    Deliberately not locale.getdefaultlocale(): it is deprecated and is
    removed in Python 3.15. The environment variables are the Unix
    convention, and GetUserDefaultUILanguage is the Windows one - and on
    Windows the two disagree, because the environment usually says nothing
    at all.
    """
    tags = []
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        raw = os.environ.get(var, "")
        for part in raw.split(":"):
            part = part.split(".")[0].split("@")[0].strip()
            if part and part not in ("C", "POSIX"):
                tags.append(part.replace("_", "-"))

    if sys.platform.startswith("win"):
        try:
            import ctypes
            import locale as _locale
            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            name = _locale.windows_locale.get(lcid)
            if name:
                tags.append(name.replace("_", "-"))
        except Exception:
            pass
    else:
        try:
            import locale as _locale
            name = _locale.getlocale()[0] or ""
            name = name.split(".")[0].strip()
            if name and name not in ("C", "POSIX"):
                tags.append(name.replace("_", "-"))
        except Exception:
            pass
    return tags


def system_default():
    """The best guess at the user's language, from the OS."""
    for tag in _os_language_tags():
        if tag in _languages:
            return tag
        short = tag.split("-")[0]
        if short in _languages:
            return short
        for code in _languages:
            if code.split("-")[0] == short:
                return code
    return "en"
