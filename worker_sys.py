"""worker_sys - the System tab's jobs.

Options, the clock, ports, signal path compensation, diagnostics, secure
erase and the factory setup.

One of the mixins Worker in worker.py is made of. Everything here runs
on the worker's thread, never on Tk's.
"""
import time

import pyvisa
import tds_cal
from i18n import gettext as _


class SystemJobs(object):
    """Jobs for the System tab."""

    # -- the system tab --------------------------------------------------

    def options_read(self):
        """Which options the instrument's own words say are enabled.

        Better than *OPT? for this, because it reads exactly what the
        dialog writes: no code-name mapping in between, and it works on
        an instrument whose *OPT? says nothing useful. Read-only.

        Returns {code: bool}, or None if any word could not be read -
        a partial answer is not usable here, because a code missing
        from it would leave its box unticked and unticked disables.
        """
        out = {}
        for code, word, on, _what in self.OPTION_WORDS:
            try:
                got = tds_cal.word(self.wfm.q, 0,
                                   word + self.OPTION_ATOFFSET_BIAS)
            except Exception:
                return None
            out[code] = (got == on)
        return out

    @classmethod
    def options_fitted(cls, reply):
        """Which option codes *OPT? reported, spelled as OPTION_WORDS.

        None - not an empty set - when the reply says nothing useful.
        The difference matters: the dialog ticks boxes from this, and a
        box left unticked switches an option *off*. "This instrument
        did not tell me" has to be distinguishable from "it told me it
        has none", or a scope that answers nothing gets everything it
        owns turned off by somebody trusting the ticks.
        """
        reply = (reply or "").strip()
        if not reply:
            return None
        codes, said_anything = set(), False
        for field in reply.split(","):
            field = field.strip()
            if not field or field == "0":
                continue
            code = field.split(":", 1)[0].strip().upper()
            if not code or code == "0":
                continue
            said_anything = True
            codes.add(cls.OPTION_ALIASES.get(code, code))
        return codes if said_anything else None

    def sys_clock_read(self):
        """Just the instrument's date and time.

        The tab reads everything once and then leaves the instrument
        alone, but a clock is the one thing on it that is wrong a
        second after it was read. This is two queries rather than the
        dozen sys_read costs, so it can run every time the tab is
        opened without the tab becoming expensive to look at.
        """
        def ask(what):
            try:
                got = self.wfm.q(what)
            except (IOError, pyvisa.Error):
                return None
            # Quoted strings come back quoted, and the boxes want the
            # date, not the quotation marks round it. sys_read strips
            # them too; this path is the one the tab uses every time it
            # is opened, so it stripped nothing and the marks showed.
            got = str(got).strip().strip('"') if got is not None else ""
            return got or None

        return {"date": ask("DATE?"), "time": ask("TIME?")}

    def sys_read(self):
        """Everything the system tab shows, in one trip round the bus.

        One job rather than a dozen, because each one costs a round
        trip and the tab wants all of it at once. Anything the
        instrument refuses comes back as None rather than raising: an
        RS-232 setting on a scope without Option 13 is a blank field,
        not a failed job.
        """
        def ask(what):
            try:
                got = self.wfm.q(what)
            except (IOError, pyvisa.Error):
                return None
            got = str(got).strip() if got is not None else ""
            return got or None

        out = {"idn": ask("*IDN?"), "options": ask("ID?"),
               "date": ask("DATE?"), "time": ask("TIME?"),
               "clock": ask("DISPLAY:CLOCK?"),
               "port": ask("HARDCOPY:PORT?"),
               "format": ask("HARDCOPY:FORMAT?"),
               "layout": ask("HARDCOPY:LAYOUT?"),
               "rs232": {}}
        for key, what in (("baud", "RS232:BAUD?"),
                          ("parity", "RS232:PARITY?"),
                          ("stopbits", "RS232:STOPBITS?"),
                          ("hardflag", "RS232:HARDFLAGGING?"),
                          ("softflag", "RS232:SOFTFLAGGING?")):
            got = ask(what)
            if got is not None:
                out["rs232"][key] = got
        # Quoted strings come back quoted. The tab wants the date, not
        # the quotation marks round it.
        for key in ("idn", "options", "date", "time"):
            if out[key]:
                out[key] = out[key].strip('"')
        return out

    def sys_send(self, lines):
        """Send these settings, and say what the instrument refused.

        Refusals are collected rather than raised. A tab that sets six
        things and stops at the first one an older instrument does not
        have is worse than one that sets the other five and says so.
        """
        self.fs.errors()
        refused = []
        for line in lines:
            self.fs.inst.write(line)
            try:
                self.wfm.q("*OPC?")
            except (IOError, pyvisa.Error):
                pass
            for said in self.fs.errors():
                refused.append((line, str(said)))
        return {"sent": len(lines), "refused": refused,
                "now": self.sys_read()}

    def sys_spc(self, most=1500.0):
        """Signal path compensation: *CAL?, which answers 0 for pass.

        Minutes, not seconds, and nothing else can use the bus while it
        runs - the manual says so and the instrument means it. So it
        cannot truly be started and walked away from: *CAL? is a query,
        and an answer nobody reads is left in the output queue for the
        next query to collect as its own. That is a nasty class of bug
        to leave lying about, and it is worse than waiting.

        What it can do is wait longer than any of these instruments
        take, and give up without calling it a failure. Twenty-five
        minutes covers the slowest of them with room to spare; past
        that the instrument is still working perfectly well, and the
        session is cleared so that whatever it eventually answers
        cannot be read as the reply to a later question.
        """
        was = getattr(self.fs.inst, "timeout", None)
        try:
            self.fs.inst.timeout = int(most * 1000)
            said = str(self.wfm.q("*CAL?")).strip()
        except (IOError, pyvisa.Error):
            try:
                self.fs.inst.clear()
            except Exception:
                pass
            return {"result": None, "passed": False, "waited": most,
                    "stillgoing": True}
        finally:
            if was is not None:
                self.fs.inst.timeout = was
        return {"result": said, "passed": said.strip().startswith("0"),
                "stillgoing": False}

    def sys_diag(self, area="ALL", most=300.0, settle=150.0, poll=15.0):
        """Extended diagnostics: select, execute, wait, then read the log.

        `DIAg:STATE EXECute` warm-boots the instrument, and the manual
        says so: it clears the Event Queue, the Input Queue and the
        status registers on the way through. The Input Queue is the one
        that matters here - a query sent straight after the command is
        thrown away by the boot and never answered by anybody.
        Measured on a 784D, reading DIAg:RESUlt:FLAg? immediately timed
        out after five minutes with the instrument sitting there
        finished and idle.

        SO NOTHING IS SAID TO IT WHILE IT BOOTS. This is the whole
        reason the wait is shaped the way it is, and it was learned the
        expensive way.

        An earlier version asked *IDN? every two seconds from the
        moment the command went out - the manual's own advice is a
        Service Request on the power-on event, and polling was chosen
        instead because it needed no status plumbing. It hung a TDS
        754D twice, on two different areas, one of which passes. Both
        times the instrument had to be power-cycled. It was not the
        instrument's faults and it was not the command:

          - 784D, EXECute sent, VISA session closed, silence for 150 s,
            then one query: answered instantly, FLAG? PASS, LOG? two
            passing modules. Clean.
          - 784D, the same with 60 s of silence: the one query timed
            out and the instrument was still on its Java splash screen.
            It finished booting by itself a little later and was fine,
            so a single query mid-boot is survivable - it is the
            hammering that is not.
          - 754D, polled every two seconds: hung, twice.

        So: send the command, then say nothing at all for `settle`,
        which is the measured-good 150 s rather than a guess. Only then
        start asking, and slowly - `poll` is fifteen seconds, not two.

        How much of the 150 is margin has since been measured: silent
        for 60 s and then one query every 10 s, a healthy 784D answered
        at 112 s twice, so the boot itself is 102 to 112 s. 150 keeps
        about a third in hand, which stays - a failing instrument and a
        slower model are both unmeasured, and trimming it would save
        twenty seconds on a two-minute operation. Sixty seconds is known
        to be too short, so anything shorter than the settle used here
        needs measuring again before it is trusted.

        The result log names modules and says pass or fail. What
        failed inside one is in the instrument's error log, which is
        why a failing module reads "(see error log)" - and the detail
        there is worth having in as many words:

            ERROR: diagnostic test failure, dsyRastModeV0Walk,
            raster fail @ 1,1 read: 8, expect: f

        Sub-test, coordinates, read and expected. That is a per-bit
        memory result, which is the whole reason for running this from
        here rather than reading PASS or FAIL off the screen. So the
        log is read before and after and the difference comes back
        with the verdict.
        """
        was = getattr(self.fs.inst, "timeout", None)
        flag = log = ""
        # What the log holds before the run, so what it holds after can
        # be told apart from years of older history. Not cleared: that
        # is the instrument's own service record and there is no undo.
        before = self._errlog()
        try:
            self.fs.inst.write("DIAG:SELECT:%s ALL" % area)
            self.fs.inst.write("DIAG:STATE EXECUTE")
            # Silence. Not a poll loop with a long interval - nothing
            # is sent at all until this is over. See the docstring.
            end = time.time() + most
            waited, step = 0.0, 5.0
            while waited < settle:
                time.sleep(min(step, settle - waited))
                waited += step
                self._progress(
                    _("Letting the instrument restart undisturbed "
                    "(%d of %d seconds)") % (min(int(waited), int(settle)),
                                            int(settle)),
                    waited / settle)
            # Short, so each attempt while it is still down fails
            # quickly rather than eating the whole budget in one go.
            self.fs.inst.timeout = 3000
            # `poll` and `settle` are parameters so the checks can drive
            # this whole sequence without waiting minutes for an
            # instrument that is imaginary.
            # The deadline is checked at the bottom, so one attempt
            # always happens however small the budget is.
            back = False
            while True:
                try:
                    if str(self.wfm.q("*IDN?")).strip():
                        back = True
                        break
                except Exception:
                    pass
                if time.time() >= end:
                    break
                self._progress(_("Asking whether it is back yet"), None)
                time.sleep(poll)
            if not back:
                return {"area": area, "flag": "", "log": "", "found": [],
                        "passed": False, "back": False}
            self.fs.inst.timeout = 30000
            # Both come back as quoted strings. The quotation marks are
            # the bus's, not the instrument's opinion of its own health,
            # so they do not belong in the pane somebody reads.
            flag = str(self.wfm.q("DIAG:RESULT:FLAG?")).strip().strip('"')
            log = str(self.wfm.q("DIAG:RESULT:LOG?")).strip().strip('"')
        finally:
            if was is not None:
                self.fs.inst.timeout = was
        return {"area": area, "flag": flag, "log": log, "back": True,
                "found": self._errlog_since(before),
                # PAS, not PASS. With VERBOSE off the instrument
                # abbreviates every keyword to four significant
                # characters, so the flag is PAS or FAI - and matching
                # "PASS" therefore never matched, which made every run
                # on this firmware read as a failure. Measured on a TDS
                # 754D: VERBOSE OFF gives 'FAI' and VERBOSE ON 'FAIL'.
                "passed": flag.upper().startswith("PAS")}

    def sys_secure(self):
        """TEKSecure: zero every reference waveform and every setup.

        The instrument raises 2285 for pass and 2286 for fail when it
        has finished, which is the only thing that says it worked - the
        command itself answers nothing.
        """
        self.fs.errors()
        was = getattr(self.fs.inst, "timeout", None)
        try:
            self.fs.inst.timeout = 300000
            self.fs.inst.write("TEKSECURE")
            self.wfm.q("*OPC?")
        finally:
            if was is not None:
                self.fs.inst.timeout = was
        # The pass and fail events are the answer to this job, not
        # something unexpected, so they are read past the event watcher -
        # which otherwise warned about TEKSecure's own success.
        said = [str(e) for e in self._quiet_drain()]
        return {"events": said,
                "passed": any(e.startswith("2285") for e in said),
                "failed": any(e.startswith("2286") for e in said)}

    def sys_factory(self):
        """FACtory: back to the default setup.

        It leaves the GPIB address, the calibration constants and the
        protected user data alone - the manual is explicit about all
        three - so this is a setup reset, not a wipe.
        """
        self.fs.errors()
        self.fs.inst.write("FACTORY")
        self.wfm.q("*OPC?")
        return {"events": [str(e) for e in self.fs.errors()]}

    def sys_option(self, wants):
        """Switch options on or off, by writing their words directly.

        `wants` is a list of (address, 1 or 0).

        This is not in any Tektronix manual. The procedure - password,
        then one word per option - came from the bench, and the words
        are the ones in Worker.OPTION_WORDS. It only works with the
        NVRAM protection switch on the right side of the cabinet pushed
        to unprotected; with it protected the writes are accepted and
        nothing changes, which is why the reading back is the only
        honest way to report it and there is nothing to read back
        from. So this reports what it sent and what the instrument
        said, and leaves the verdict to the boot screen.
        """
        self.fs.errors()
        self.fs.inst.write("PASSWORD PITBULL")
        self.wfm.q("*OPC?")
        said = [str(e) for e in self.fs.errors()
                if not str(e).startswith("0,")]
        sent = []
        for word, value in wants:
            line = "WORDCONSTANT:ATPUT %d, %d" % (int(word), int(value))
            self.fs.inst.write(line)
            try:
                self.wfm.q("*OPC?")
            except (IOError, pyvisa.Error):
                pass
            said += [str(e) for e in self.fs.errors()]
            sent.append(line)
        return {"sent": sent, "refused": said, "options": self.sys_options()}

    def sys_options(self):
        """What the instrument says it has fitted, as a list."""
        try:
            got = str(self.wfm.q("ID?")).strip().strip('"')
        except (IOError, pyvisa.Error):
            return []
        # ID? is a comma-separated list with the options at the end,
        # after a field that starts "CF:". Anything before that is the
        # instrument saying what it is.
        return [p.strip() for p in got.split(",") if p.strip()]

    # ------------------------------------------------- the system tab
    # Housekeeping the instrument keeps in non-volatile memory: its
    # clock, its calibration, its self tests, where hardcopies go, and
    # which options it believes it has. Every command here is out of
    # the TDS Family Programmer Manual 070-9876-00 except the option
    # words, which are not in any manual - see sys_option.

    #: Each row is (code, word, on value, description). The word is a
    #: constant in non-volatile memory and the value switches the
    #: option on; 0 always switches it off. Read out of the firmware:
    #: words 327680-327696 are one block, six longs then eleven words
    #: at 0x04000806, and the getter for each option query pushes its
    #: own index. 327691 is allocated but no firmware in the family
    #: ever reads it, so it is not offered.
    #:
    #: 1G is the odd one. It is not an option word at all: the firmware
    #: reports it when the acquisition board identity reads 14, and
    #: 131219 is the constant that overrides that identity when it is
    #: non-zero. Writing it therefore tells the instrument it has a
    #: different acquisition board, which is why it is last and why 0
    #: (hand the identity back to the hardware) is the way off.
    OPTION_WORDS = (
        ("1M", 327686, 1, "4M acquisition length"),
        ("05", 327687, 1, "Video trigger"),
        ("13", 327688, 1, "RS-232-C and Centronics interfaces"),
        ("2F", 327689, 1, "Advanced DSP math"),
        ("1F", 327690, 1, "Floppy disk drive"),
        ("2C", 327692, 1, "Communication Signal Analyzer"),
        ("3C", 327693, 1, "P6701B with system calibration"),
        ("4C", 327694, 1, "P6703B with system calibration"),
        ("2M", 327695, 1, "8M acquisition length"),
        ("1G", 131219, 14, "Limit sample rate to 1 GS/s"),
    )

    #: The options that are software alone. The rest need hardware and
    #: simply will not appear without it - and 3C and 4C raise a
    #: processor board fault when they are switched on without their
    #: calibration data.
    OPTION_SOFT = ("1M", "2F", "2C", "1G")

    #: The instruments 1G is offered on, spelled the way the catalogue
    #: spells a model (no space). It is held to a list by hand rather
    #: than to the firmware table: plenty of firmwares read the constant
    #: behind it, but it is an acquisition board identity rather than an
    #: option flag, so it is offered only where it is known to apply -
    #: traced on the 540B, and listed for the 520D, 540D, 724D and 754D
    #: in Tektronix's own documentation (manual 071-0130-03).
    OPTION_1G_MODELS = ("TDS540B", "TDS520D", "TDS540D", "TDS724D",
                        "TDS754D")

    #: What *OPT? calls an option, where that is not what the option
    #: word list calls it. Measured, not guessed: a TDS 680B on v4.4.1e
    #: answers "13:Rs232/cent,1M:extended record length,0,2F:math
    #: pack,0,FD:1.44MB floppy drive,0,0,0,0,0,0" - and its floppy is
    #: FD there and 1F here. Anything not in this table is taken to
    #: name itself.
    OPTION_ALIASES = {"FD": "1F"}

    #: ATOFFSET numbers a word one higher than ATPUT does.
    #:
    #: The option words are written with WORDCONSTANT:ATPUT <word>, and
    #: read back with WORDCONSTANT:ATOFFSET? <base>,0 - and the two do
    #: not agree about which word is which. Reading at the address
    #: ATPUT writes gives the word *before* it.
    #:
    #: Measured, and confirmed against a second source rather than
    #: assumed. On a TDS 680B, reading at the ATPUT addresses says 05,
    #: 2F and 1F are on and 1M and 13 are off; *OPT? on the same
    #: instrument says 1M, 13, 2F and the floppy. Adding one makes all
    #: ten agree with *OPT? exactly, including the seven that are off.
    #: Two independent readings of the same ten facts, so this is the
    #: relationship and not a coincidence of one scope's option set.
    #:
    #: Confirmed again on a TDS 754D running v8.0e - a different family
    #: and a different option set - where the ten biased reads agree
    #: with its *OPT? on all ten, 13, 2F, 1F, 2C and 2M on. Reading at
    #: the unbiased addresses on that instrument gives 0, 0, 0, 1, 1,
    #: 0, 1, 0, 0, 23347, which agrees with nothing.
    OPTION_ATOFFSET_BIAS = 1
