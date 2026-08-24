"""The instrument setup that goes with a mask - TTiP's other half.

A mask is held in percent of the graticule, which is what makes one
worth keeping: the same mask is the same mask at any timebase. The cost
of that is that a mask on its own says nothing about the signal it was
drawn for. Tektronix answered it by shipping a setup beside every mask -
OC3.MSK and OC3.SET, same name, different extension - and this writes
the same kind of file from what an instrument is set to now.

**Two different files are called .SET and they are not interchangeable.**
This is the plain-SCPI kind that TTiP's LOAD.EXE sends down the bus:
text, editable, and taken by any TDS whatever its firmware. The other
kind is what `SAVE:SETUP` writes on the instrument's own disk - 4618
bytes of binary on a 784D, tied to the firmware that wrote it, and
recallable from the front panel. This module never writes that one; only
the instrument can.

A setup carries no mask, incidentally: a 784D's saved setup with a mask
in segment 1 holds neither the word MASK nor any of the coordinates. The
two files are complementary, not overlapping. See INSTRUMENT-NOTES.
"""
import io
import re

import tds_wfm

SUFFIX = ".SET"

# What is worth recording, and what to ask for it. The order is the
# order the answers are written out in, which is Tektronix's own.
#
# %s is the channel the mask is tested against. A field the instrument
# refuses is left out of the file rather than guessed at - a 640A has no
# CH1:IMPEDANCE - so every one of these is asked for separately.
#
# PROBE is read and never written: CH<x>:PROBE? is a query and nothing
# else, because the instrument reads the attenuation off the probe's
# own coding ring rather than being told it (Programmer Manual 2-75).
# It is worth knowing all the same - the volts per division an input
# will reach is the input's own ceiling times the probe in front of it,
# so it is the difference between "this instrument cannot" and "this
# instrument cannot with that probe".
CHANNEL = (
    ("coupling", "CH%s:COUPLING?"),
    ("impedance", "CH%s:IMPEDANCE?"),
    ("bandwidth", "CH%s:BANDWIDTH?"),
    ("scale", "CH%s:SCALE?"),
    ("position", "CH%s:POSITION?"),
    ("offset", "CH%s:OFFSET?"),
    ("probe", "CH%s:PROBE?"),
)
# Spellings measured on a 784D (v7.4e), not taken from the manual:
# ACQUIRE:REPETITIVE? is refused and ACQUIRE:REPET? answers, and holdoff
# is BY and TIME here where TTiP's own files say HOLDOFF:VALUE - which
# this firmware refuses. Both spellings are read; only the ones this
# generation answers to are written.
INSTRUMENT = (
    ("repet", "ACQUIRE:REPET?"),
    ("style", "DISPLAY:STYLE?"),
    ("secdiv", "HORIZONTAL:MAIN:SECDIV?"),
    ("length", "HORIZONTAL:RECORDLENGTH?"),
    ("fit", "HORIZONTAL:FITTOSCREEN?"),
    ("hposition", "HORIZONTAL:POSITION?"),
    ("tposition", "HORIZONTAL:TRIGGER:POSITION?"),
    ("tmode", "TRIGGER:MAIN:MODE?"),
    ("ttype", "TRIGGER:MAIN:TYPE?"),
    ("tlevel", "TRIGGER:MAIN:LEVEL?"),
    ("tholdby", "TRIGGER:MAIN:HOLDOFF:BY?"),
    ("tholdtime", "TRIGGER:MAIN:HOLDOFF:TIME?"),
    ("tsource", "TRIGGER:MAIN:EDGE:SOURCE?"),
    ("tcoupling", "TRIGGER:MAIN:EDGE:COUPLING?"),
    ("tslope", "TRIGGER:MAIN:EDGE:SLOPE?"),
)


class TdsSet(object):
    """Reads the setup a mask should be used with off an instrument."""

    def __init__(self, inst):
        self.inst = inst

    def ask(self, question):
        """One answer, or None if the instrument will not give it.

        Headers are stripped: an instrument with HEADER ON answers
        ":CH1:COUPLING AC" and what is wanted is AC.
        """
        try:
            said = str(self.inst.query(question)).strip()
        except Exception:
            return None
        if not said:
            return None
        if said.startswith(":"):
            said = said.split(" ", 1)[-1].strip()
        return said or None

    def read(self, source="CH1"):
        """Everything worth writing down, as far as this one will say."""
        channel = re.sub(r"[^0-9]", "", source) or "1"
        out = {"source": "CH%s" % channel}
        for key, question in CHANNEL:
            said = self.ask(question % channel)
            if said is not None:
                out[key] = said
        for key, question in INSTRUMENT:
            said = self.ask(question)
            if said is not None:
                out[key] = said
        return out


def number(fields, key):
    """One field as a float, or None if it is missing or not a number."""
    try:
        return float(fields[key])
    except (KeyError, TypeError, ValueError):
        return None


def text(fields, name, said="", when=""):
    """The file itself, in the shape of Tektronix's own.

    `said` and `when` are the instrument and the moment, passed in
    rather than looked up so that the same fields always write the same
    file - a test can compare one against a fixture.

    BELL is not written, though TTiP's files end with it. A beep is a
    fine way to say "arrived" when a person is running LOAD.EXE by hand
    and a poor one when a program sends a setup as a step in something
    larger. CLEARMENU is kept: it leaves the screen looking like the
    instrument has just been set up, which it has.
    """
    source = fields.get("source", "CH1")
    out = ['REM "This is file - %s%s -"' % (name, SUFFIX)]
    if said:
        out.append('REM "Read from %s"' % said)
    if when:
        out.append('REM "Written by TDS Toolkit on %s"' % when)
    # The two machine-readable REMs. Everything else in a setup is a
    # command the instrument takes; these two describe the signal the
    # mask was drawn for, which no instrument setting records. A REM is
    # where they go because the instrument ignores REMs, so a file
    # carrying them is still a file it will load.
    ui = number(fields, "ui")
    if ui:
        out.append('REM "UI %g"' % ui)
    if fields.get("differential"):
        out.append('REM "DIFFERENTIAL"')
    out.append("ACQUIRE:STATE STOP")
    if "repet" in fields:
        out.append("ACQUIRE:REPET %s" % fields["repet"])

    first = [("COUPLING", "coupling"), ("IMPEDANCE", "impedance"),
             ("BANDWIDTH", "bandwidth")]
    second = [("SCALE", "scale"), ("POSITION", "position"),
              ("OFFSET", "offset")]
    for group in (first, second):
        got = [(word, fields[key]) for word, key in group if key in fields]
        if got:
            out.append("%s:%s" % (source,
                                  ";".join("%s %s" % pair for pair in got)))
    if "style" in fields:
        out.append("DISPLAY:STYLE %s" % fields["style"])
    for word, key in (("MAIN:SECDIV", "secdiv"),
                      ("RECORDLENGTH", "length"),
                      ("FITTOSCREEN", "fit"),
                      ("POSITION", "hposition"),
                      ("TRIGGER:POSITION", "tposition")):
        if key in fields:
            out.append("HORIZONTAL:%s %s" % (word, fields[key]))
    main = [("MODE", "tmode"), ("TYPE", "ttype"), ("LEVEL", "tlevel")]
    got = [(word, fields[key]) for word, key in main if key in fields]
    if got:
        out.append("TRIGGER:MAIN:%s"
                   % ";".join("%s %s" % pair for pair in got))
    # Holdoff only means a number when it is not on DEFAULT, and a
    # number written while it is on DEFAULT is a line the instrument
    # has no use for.
    if "tholdby" in fields:
        out.append("TRIGGER:MAIN:HOLDOFF:BY %s" % fields["tholdby"])
        if (fields["tholdby"].upper().startswith("TIM")
                and "tholdtime" in fields):
            out.append("TRIGGER:MAIN:HOLDOFF:TIME %s" % fields["tholdtime"])
    edge = [("SOURCE", "tsource"), ("COUPLING", "tcoupling"),
            ("SLOPE", "tslope")]
    got = [(word, fields[key]) for word, key in edge if key in fields]
    if got:
        out.append("TRIGGER:MAIN:EDGE:%s"
                   % ";".join("%s %s" % pair for pair in got))
    out.append("SELECT:%s ON" % source)
    out.append("CLEARMENU")
    out.append("ACQUIRE:STATE RUN")
    return "".join(":%s\n" % line for line in out)


# Every command this module knows how to read back out of a file, and
# which field it fills. Tektronix's own files are read by the same
# table, which is the point of writing files in their shape.
READS = {
    "COUPLING": "coupling", "IMPEDANCE": "impedance",
    "BANDWIDTH": "bandwidth", "SCALE": "scale", "POSITION": "position",
    "OFFSET": "offset", "DISPLAY:STYLE": "style",
    "ACQUIRE:REPETITIVE": "repet", "ACQUIRE:REPET": "repet",
    "HORIZONTAL:FITTOSCREEN": "fit",
    "HORIZONTAL:MAIN:SECDIV": "secdiv",
    "HORIZONTAL:RECORDLENGTH": "length",
    "HORIZONTAL:POSITION": "hposition",
    "HORIZONTAL:TRIGGER:POSITION": "tposition",
    "TRIGGER:MAIN:MODE": "tmode", "TRIGGER:MAIN:TYPE": "ttype",
    "TRIGGER:MAIN:LEVEL": "tlevel",
    "TRIGGER:MAIN:HOLDOFF:VALUE": "tholdoff",
    "TRIGGER:MAIN:HOLDOFF:BY": "tholdby",
    "TRIGGER:MAIN:HOLDOFF:TIME": "tholdtime",
    "TRIGGER:MAIN:EDGE:SOURCE": "tsource",
    "TRIGGER:MAIN:EDGE:COUPLING": "tcoupling",
    "TRIGGER:MAIN:EDGE:SLOPE": "tslope",
}
# Tektronix abbreviate. Their own OC3.SET says TRIG:MAIN:MODE and
# HOL:VAL, which are the same commands spelled the short way the manual
# allows, so the short spellings are listed too rather than the file
# being half understood.
READS.update({"TRIG:MAIN:MODE": "tmode", "TRIG:MAIN:TYPE": "ttype",
              "TRIG:MAIN:LEV": "tlevel",
              "TRIG:MAIN:HOL:VAL": "tholdoff",
              "TRIG:MAIN:EDGE:SOU": "tsource",
              "TRIG:MAIN:EDGE:COUPLING": "tcoupling",
              "TRIG:MAIN:EDGE:SLOPE": "tslope",
              "HORIZONTAL:MAIN:SCALE": "secdiv"})


def parse(data):
    """What a .SET file asks for, as the fields read() would have given.

    Tolerant on purpose. These files are hand-editable and Tektronix's
    own are a mixture of full and abbreviated keywords, so anything not
    recognised is passed over rather than complained about.

    Semicolons are read the way the instrument reads them: everything
    after the first one inherits the path of the command in front of it,
    so `TRIG:MAIN:MODE AUTO;TYPE EDGE` sets two fields and not one and a
    half. Getting that wrong is how half of OC3.SET went unread.
    """
    if isinstance(data, bytes):
        data = data.decode("ascii", "replace")
    fields = {}
    for line in data.splitlines():
        line = line.strip().lstrip(":")
        if not line or line.upper().startswith("REM"):
            continue
        head = line.split(" ", 1)[0]
        path = head.rsplit(":", 1)[0] + ":" if ":" in head else ""
        for at, part in enumerate(line.split(";")):
            part = part.strip()
            if not part or " " not in part:
                continue
            word, value = part.split(" ", 1)
            word = word.strip().lstrip(":").upper()
            word = word if at == 0 else (path + word).upper()
            if word.startswith("SELECT:CH"):
                fields.setdefault("source", word.split(":", 1)[1])
                continue
            channel = re.match(r"^(CH[1-4]):(.+)$", word)
            if channel:
                fields.setdefault("source", channel.group(1))
                # One channel's settings, not a mixture. STS1.SET names
                # two - CH1 at 304 mV a division and CH2 at 100 - and
                # taking whichever came last had the program compare
                # CH2's scale against CH1's readback and report a
                # substitution the instrument never made.
                if channel.group(1) != fields["source"]:
                    continue
                word = channel.group(2)
            key = READS.get(word)
            if key:
                fields[key] = value.strip()
    return fields


def summary(fields):
    """One line saying what this setup asks for, for a list or a label."""
    bits = []
    secdiv = number(fields, "secdiv")
    if secdiv:
        bits.append("%s/div" % tds_wfm.eng(secdiv, "s"))
    scale = number(fields, "scale")
    if scale:
        bits.append("%s/div" % tds_wfm.eng(scale, "V"))
    ends = [fields[k] for k in ("coupling", "impedance") if k in fields]
    if ends:
        bits.append(" ".join("1 M" + u"Ω" if e.upper().startswith("MEG")
                             else e for e in ends))
    if "length" in fields:
        bits.append("%s points" % fields["length"])
    return ", ".join(bits)


def command(key, source="CH1"):
    """The command a field is set with, for saying which one moved.

    The tables above already know it, and a bench reads
    HORIZONTAL:MAIN:SECDIV better than it reads "secdiv" - it is what
    the manual calls the thing, and it needs no translating.
    """
    channel = re.sub(r"[^0-9]", "", source) or "1"
    for name, question in tuple(CHANNEL):
        if name == key:
            return (question % channel).rstrip("?")
    for name, question in tuple(INSTRUMENT):
        if name == key:
            return question.rstrip("?")
    return key


def differences(asked, got):
    """The fields the instrument did not take literally.

    Sending a setup is not the same as the instrument having it. A
    command it does not know is refused and lands in the event queue,
    which is read separately; a value it cannot reach is not refused at
    all - it is quietly replaced with the nearest one it can do. Ask a
    784D for 10 ns a division and it gives 12.5; ask a 794D for 5 V a
    division and it gives 1. Neither says a word, and a mask held in
    percent of the graticule then measures something other than what it
    says. So what was asked for is read back and compared.

    Only fields the file asked for: a setup names what it cares about
    and is silent about the rest, and a difference in something nobody
    asked for is noise. Returns [(key, asked, got)] in the order the
    file writes them.
    """
    # The trigger level has a grid of its own. Measured on a 784D at
    # 2, 1, 0.5 and 0.2 V a division, the step is a fiftieth of a
    # division and a value between two steps is truncated towards zero
    # rather than rounded: ask for 0.75 at 1 V a division and the
    # instrument holds 0.74. That is its resolution rather than a value
    # it would not take, and reporting a fiftieth of a division as a
    # substitution sends somebody looking for a fault that is not
    # there. Anything further than one step still gets reported.
    div = number(asked, "scale") or number(got, "scale") or 0.0
    grid = {"tlevel": div / 50.0}
    out = []
    for key, _question in tuple(CHANNEL) + tuple(INSTRUMENT):
        if key not in asked or key not in got:
            continue
        one, two = number(asked, key), number(got, key)
        if one is not None and two is not None:
            if abs(one - two) <= max(abs(one) * 1e-6, grid.get(key, 0.0)):
                continue
        else:
            # Words, and the instrument answers in its own spelling:
            # a file may say RIS where the instrument says RISE.
            was, now = str(asked[key]).upper(), str(got[key]).upper()
            if was == now or (len(was) >= 2 and now.startswith(was)) \
                    or (len(now) >= 2 and was.startswith(now)):
                continue
        out.append((key, asked[key], got[key]))
    return out


def stretch(diffs):
    """How far a mask must stretch to mean what it did, and what is left.

    A timebase the instrument rounded up puts fewer divisions under the
    same signal, so the mask narrows by asked over got; volts per
    division it would not give makes the signal taller by the same
    ratio. Both are exact arithmetic on a mask held in percent of the
    graticule, so both can be answered by moving the mask rather than
    the instrument.

    Everything else comes back untouched. A coupling, an impedance, a
    record length or a trigger the instrument would not take is not a
    scaling and no amount of stretching answers it.
    """
    across = up = 1.0
    rest = []
    for key, asked, got in diffs:
        try:
            ratio = float(asked) / float(got)
        except (TypeError, ValueError, ZeroDivisionError):
            ratio = 0.0
        if ratio and key == "secdiv":
            across = ratio
        elif ratio and key == "scale":
            up = ratio
        else:
            rest.append((key, asked, got))
    return across, up, rest


def probe_wanted(asked, got):
    """The probe that would reach the volts per division asked for.

    The ceiling on an input is the input's own times the attenuation of
    the probe in front of it - the manual gives 10 V a division falling
    to 1 V on a 50 ohm input, and a passive 10X probe taking it to 100
    (Programmer Manual 2-72). So a scale the instrument would not give
    is not always the end of it: the same setup on the same instrument
    with a bigger probe applies exactly.

    Returns the attenuation to ask for, or None where the instrument
    did give what was asked or will not say what probe it has. It is a
    thing to tell somebody standing at the bench, and never a thing to
    set: CH<x>:PROBE? is a query, and PROBEFUNC:EXTATTEN - which does
    set - is a claim about hardware that may not be there. Set it with
    no such probe fitted and the mask fits a trace ten times too small.
    """
    one, two = number(asked, "scale"), number(got, "scale")
    now = number(got, "probe") or 1.0
    if not one or not two or one <= two:
        return None
    for step in (1.0, 10.0, 20.0, 50.0, 100.0, 500.0, 1000.0):
        if step >= now * one / two:
            return step
    return None


def beside(path):
    """The name of the setup that would go with this mask file."""
    import os
    stem = os.path.splitext(path)[0]
    return stem + SUFFIX


def contents(path):
    """A setup file's text, read the one way that cannot fail.

    Not text(): that name is taken, by the function above that renders a
    setup rather than reading one.

    Latin-1 rather than whatever the machine's code page happens to be:
    a setup carries the instrument's own label and WFID fields, which are
    not promised to be ASCII, and every byte maps to a character here
    rather than raising. The callers are button handlers, where an
    exception is a button that silently does nothing.
    """
    with io.open(path, encoding="latin-1") as fh:
        return fh.read()
