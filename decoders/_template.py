"""Template for a new protocol decoder. Copy this to <name>.py, rename the
class, and fill in decode(). Files beginning with '_' are skipped by the
loader, so this template never appears in the protocol list.

The whole contract is the Decoder base class. You set four class attributes
and write one method; the rest have workable defaults you override only to
show your protocol's own vocabulary in the table and on the graph.

Read a working example alongside this one:
  * uart.py - one wire, an auto-detected bit rate, parity and framing.
  * i2c.py  - two wires walked together, START/STOP framing, ACK.
  * spi.py  - a clock and one or two data lines, an optional chip select.
"""

from tds_decode import Decoder, Frame, Param
from tds_decode import OK   # and FRAMING_ERROR, PARITY_ERROR, ACK_ERROR, ...


class TemplateDecoder(Decoder):

    # Shown in the protocol list and used as the graph label's title.
    NAME = ""                       # e.g. "CAN" - leave blank so the loader
                                    # never lists the template itself
    DESCRIPTION = ""                # one line for the list's second column

    # The waveform roles this protocol needs, in order. One role is a
    # one-wire protocol; two or more each get a captured source mapped to
    # them in the UI. A trailing "?" marks an optional role.
    SOURCES = ["Line"]

    # Settings the UI renders for you. Each Param is one control.
    PARAMS = [
        # Param("baud", "Bit rate", "choice",
        #       choices=[(0, "Auto"), (9600, "9600")], default=0),
        # Param("bits", "Word size", "int", default=8, lo=4, hi=32,
        #       unit="bits"),
        # Param("invert", "Invert", "bool", default=False),
    ]

    # Chevron colours are the plugin's own: the app has no colour setting
    # for decoders. Set COLOUR for one colour for the whole protocol, and
    # FAULT_COLOUR for a frame whose status is set; leave them for the
    # readable defaults. For a colour *per chevron*, override colour() below.
    # COLOUR = "#39a0c0"
    # FAULT_COLOUR = "#d05050"

    def decode(self, signals, params):
        """`signals` maps each role (without any "?") to a Signal; an
        optional role may be absent. `params` maps each Param key to its
        chosen value. Return (frames, note): a list of Frame, and one line
        summarising what was inferred, shown above the table.

        A Signal gives you:
          sig.data          integer samples (indexable)
          sig.n             how many
          sig.dt            seconds per sample
          sig.threshold, sig.hysteresis, sig.span   set for you
          sig.time_at(i)    seconds at sample i, relative to trigger
          sig.digitize()    a 0/1 level per sample, with hysteresis
          sig.first_index   add to a sample index for an absolute one

        Make a Frame per event: Frame(time=..., index=..., end_index=...,
        value=..., tag=..., status=..., kind=...). `index`/`end_index` are
        sample positions - the graph draws a box between them. `kind` is a
        free string ("byte", "addr", "data", ...) your row()/label() switch
        on. Do not compute a threshold; it is already set.
        """
        raise NotImplementedError

    # -- override these to speak your protocol's vocabulary; the base class
    #    defaults render a plain byte with hex, ASCII and a status word.

    def columns(self):
        """Table columns after the time, as (heading, width_px)."""
        return [("Time", 92), ("Data", 60), ("ASCII", 52), ("Status", 90)]

    def row(self, frame, ascii=True):
        """Cells for one row, matching columns() after the time column."""
        ch = chr(frame.value) if 32 <= frame.value < 127 else "."
        return ["%02X" % (frame.value & 0xFF), ch,
                "OK" if frame.status == OK else "ERR"]

    def label(self, frame, ascii=True):
        """Shortest rendering for the graph box - the value, no padding."""
        return "%02X" % (frame.value & 0xFF)

    # A colour per chevron: override colour() and switch on frame.kind, so
    # the address, the data and the framing of one line each read in their
    # own colour. Return a "#rrggbb"; fall back to the base for the kinds
    # you do not colour (it gives COLOUR for a clean frame, FAULT_COLOUR for
    # a faulted one).
    #
    # def colour(self, frame):
    #     return {"addr": "#e0a030", "data": "#39a0c0"}.get(
    #         frame.kind, Decoder.colour(self, frame))
