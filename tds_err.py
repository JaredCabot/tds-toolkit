"""The instrument's own error log.

Separate from the SCPI event queue that the rest of this program reads.
The event queue is what just went wrong in this conversation - 113,
"undefined header" - and empties as it is read. `ERRLOG` is the
instrument's service history, kept in non-volatile memory across power
cycles: diagnostic failures at power-on, librarian resets, calibration
problems. It is what a service engineer would ask for.

There is no "give me the whole log" command. It is walked:

    ERRLOG:FIRST?     the oldest entry, and positions the cursor
    ERRLOG:NEXT?      the one after that, until there are no more

Measured on all three instruments on the bench:

  * An empty log answers `""` - an empty quoted string - not silence.
    That matters: a program that expects a timeout for "nothing to
    report" will call a healthy instrument broken. A timeout does happen
    on firmware that has no ERRLOG at all, and is handled as that.
  * Every entry arrives wrapped in double quotes, which are the SCPI
    string delimiters and not part of the text.
  * The end of the log is an empty reply, exactly as `FIRST?` gives on
    an empty one.
  * 31 entries off a TDS 640A took 3.24 s, about 10 ms an entry.
  * `ERRLOG:CLEAR` does not exist - the parser answers 113. `ERR?`
    returns precisely what `ERRLOG?` returns, so `ERR` is the short form
    of `ERRLOG` and what clears the log is `ERRLOG CLEAR`, a value given
    to the header rather than a subcommand under it. Verified by
    clearing a 640A that had 31 entries in it and reading back none.
"""

# A log with more entries than this is not being read to the end. The
# number is a guard against a firmware that never says it has finished,
# not a limit anybody should reach: a 640A that had been failing its
# power-on diagnostics for years held 31.
LIMIT = 2000

CLEAR = "ERRLOG CLEAR"


def unquote(text):
    """SCPI string delimiters off, everything else left alone."""
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text.replace('""', '"')


class NoErrorLog(Exception):
    """This firmware has no ERRLOG subsystem."""


class TdsErr(object):
    def __init__(self, inst, payload=None):
        self.inst = inst
        self._payload = payload or (lambda r: r)

    def drain(self):
        """Empty the event queue, which *ESR? must prime first."""
        out = []
        try:
            self.inst.query("*ESR?")
            for _ in range(30):
                msg = self._payload(self.inst.query("EVMSG?")).strip()
                if msg.startswith("0,"):
                    break
                out.append(msg)
        except Exception:
            pass
        return out

    def _ask(self, command):
        """One query. Returns the unquoted text, or None if it answered
        nothing at all - which is how firmware without ERRLOG behaves."""
        try:
            return unquote(self._payload(self.inst.query(command)))
        except Exception:
            self.drain()
            return None

    def available(self):
        """Does this firmware have an error log?

        Asked with the harmless half of it. Reading the first entry
        changes nothing and, on an instrument that has no ERRLOG, is
        answered with 113 and a timeout rather than with an entry.
        """
        return self._ask("ERRLOG:FIRST?") is not None

    def entries(self, limit=LIMIT, progress=None):
        """The whole log, oldest first, as a list of strings.

        An empty list means the instrument has nothing to report, which
        is a result rather than a failure.
        """
        first = self._ask("ERRLOG:FIRST?")
        if first is None:
            raise NoErrorLog("This firmware has no ERRLOG subsystem.")
        out = []
        if not first:
            return out
        out.append(first)
        while len(out) < limit:
            if progress:
                progress(len(out))
            text = self._ask("ERRLOG:NEXT?")
            if not text:              # blank, or nothing at all
                break
            out.append(text)
        self.drain()
        return out

    def clear(self):
        """Empty the log. Returns whatever the instrument complained of.

        The caller confirms with the user first; this does not ask.
        """
        self.drain()
        self.inst.write(CLEAR)
        return self.drain()
