"""worker_msk - the Masks and Limits tabs' jobs.

Masks and setups on the instrument, the eye measurement and its clock,
counting, envelopes and limit tests.

One of the mixins Worker in worker.py is made of. Everything here runs
on the worker's thread, never on Tk's.
"""
import time

import pyvisa
import tds_err
import tds_msk
import tds_wfm
from i18n import gettext as _


class MaskJobs(object):
    """Jobs for masks, setups and limit tests."""

    # -- masks ------------------------------------------------------------
    #
    # A mask on the instrument's disk is a file this program put there
    # and the instrument itself cannot read: there is no RECALL:MASK on
    # this generation. It is a library, kept where the masks are used,
    # and it survives the PC being a different PC next time.

    def msk_live(self):
        """What is in the instrument's eight mask segments now."""
        return {"segments": self.wfm.mask_segments()}

    def msk_clear(self):
        """Empty the instrument's eight segments and stop drawing them.

        The empty mask is written the way an empty segment is written
        everywhere else - a single 0,0 - rather than by any command that
        deletes a mask, because the one that sounds right, MASK:STANDARD,
        deletes the points and sets a standard mask in their place.
        """
        self.wfm.send_mask(tds_msk.Mask().to_scpi(), False)
        return {"segments": self.wfm.mask_segments()}

    def msk_read(self):
        """The eight segments as the instrument words them.

        Raw rather than counted, because this is the one that comes back
        into the editor as a mask. None means no mask subsystem at all.
        """
        return {"replies": self.wfm.mask_replies()}

    def set_read(self, source):
        """What the instrument is set to, for the file beside a mask."""
        return {"fields": self.set.read(source)}

    def set_send(self, lines, source="CH1"):
        """Put a setup on the instrument, and say what it would not take.

        Twice over, because there are two ways not to take it. A
        command the firmware does not know is refused and lands in the
        event queue. A value it cannot reach is not refused at all - it
        is quietly replaced with the nearest one it can do - so the
        setup is read back afterwards and compared with what was asked
        for. See tds_set.differences.

        A setup written for one generation can hold a command another
        does not have - TTiP's own files say TRIGGER:MAIN:HOLDOFF:VALUE,
        which a 784D on v7.4e refuses - and the instrument's answer to a
        command it does not know is to log it and carry on. So the log
        is drained afterwards and what it caught is reported, rather
        than the setup being called sent and half of it not applying.
        """
        self.fs.inst.write("*CLS")
        for cmd in lines:
            self.fs.inst.write(cmd)
        self.wfm.q("*OPC?")
        # tds_err knows how to empty the event queue, including that
        # *ESR? has to prime it first. Asking EVMSG? without priming
        # answers "no events to report - new events pending *ESR?" for
        # ever, which a 794D turned into a report that it had refused
        # eighteen lines of a setup it had taken perfectly.
        said = self.err.drain()
        return {"sent": len(lines),
                "refused": [m for m in said if not m.startswith("0,")],
                "got": self.set.read(source)}

    def msk_delayed(self, source, bit):
        """A whole eye on the instrument's own screen, with no clock.

        User Manual 071-0130-03 page 3-24: the delayed time base is for
        when you want "to delay an acquisition so it captures and
        displays events that follow other events". That is exactly the
        case - trigger on a data edge and look far enough past it that
        the data no longer remembers which edge it was, and ones and
        zeroes both land in the middle instead of only the top rail.

        Then centred, because the delay lands where it lands. Adding to
        the delay moves the trace earlier one for one, which was
        measured on a 784D and not assumed: a quarter-bit added moved
        the boundary by a quarter of a bit.
        """
        inst = self.fs.inst
        secdiv = float(self.wfm.q("HORIZONTAL:MAIN:SECDIV?"))
        inst.write("HORIZONTAL:DELAY:MODE RUNSAFTER")
        inst.write("HORIZONTAL:DELAY:SECDIV %g" % secdiv)
        out = self.DELAY_BITS * bit
        inst.write("HORIZONTAL:DELAY:TIME %g" % out)
        inst.write("HORIZONTAL:MODE DELAYED")
        self.wfm.q("*OPC?")
        # Four passes and a dozen crossings each, because the thing
        # being measured is noisy: every acquisition triggers on a
        # different data edge, so the phase read off a handful of
        # records carries the pattern's own jitter divided by the root
        # of how many were counted. Measured on a 784D against a USB
        # low speed signal with 40 ns of jitter on a 667 ns bit: two
        # passes over six crossings landed anywhere between nothing and
        # 7,285,488 hits from one run to the next, and neither run
        # thought anything was wrong.
        adrift = None
        for _pass in range(4):
            time.sleep(self.COUNT_FOR)
            seen, middle = [], None
            for _try in range(10):
                try:
                    wave = self.wfm.capture([source])[0][0]
                except Exception:
                    break
                spots = wave.points()
                volts = [v for _t, v in spots]
                if max(volts) - min(volts) < 0.5:
                    continue
                # The middle of the screen in the record's own time,
                # rather than worked out from the trigger position -
                # which is a main time base setting and says nothing
                # about where a delayed window sits.
                middle = (spots[0][0] + spots[-1][0]) / 2.0
                seen += tds_wfm.crossings_of(wave)
                if len(seen) >= 12:
                    break
            edge = tds_wfm.bit_phase(seen, bit)
            if edge is None or middle is None:
                return {"delay": out, "centred": False}
            adrift = (((middle - bit / 2.0 - edge) + bit / 2.0) % bit
                      - bit / 2.0)
            if abs(adrift) < bit / 50.0:
                break
            out -= adrift
            inst.write("HORIZONTAL:DELAY:TIME %g" % out)
            self.wfm.q("*OPC?")
        return {"delay": out, "adrift": adrift,
                "centred": adrift is not None and abs(adrift) < bit / 20.0}

    def msk_delay_tune(self, out, bit, segments, adrift):
        """The last word on the delay, from the mask rather than a phase.

        The phase above is measured off a handful of records, and every
        one of them triggered on a different data edge - so on a signal
        with real jitter the estimate is noisy, and a noisy estimate
        that lands half a bit out looks exactly like one that did not.
        Measured on a 784D against USB low speed: three runs of the
        same signal at the same delay gave nothing, nothing, and
        2,677,129 hits over 204,190 acquisitions, and all three
        reported the eye as centred.

        So the instrument is asked instead, and asked in DPO - which is
        why this runs after the display is switched rather than inside
        the centring. A fifth of a second of InstaVu is eighty thousand
        acquisitions against a couple of hundred in the ordinary
        display, and the difference is not academic: a CAN eye that
        showed nothing over a tenth of a second of ordinary sweeps came
        back with 1,289,385 hits over 212,836 acquisitions the moment
        DPO was on. It needs no arithmetic here at all - the instrument
        is already deciding what a hit is.
        """
        centred = adrift is not None and abs(adrift) < bit / 20.0
        best = self.msk_delay_hits() if segments else None
        if best is None:
            # Either there is no mask in the instrument to judge
            # against, or it is not counting. Nothing to tune by, so
            # the phase above stands.
            return {"delay": out, "centred": centred}
        # Finer near zero than far from it: the phase is usually right
        # and occasionally out by a fraction, so the small steps are
        # where the answer nearly always is. Stops at the first offset
        # that scores nothing, which is the common case and costs one
        # block.
        where = out
        for step in (0.1, -0.1, 0.2, -0.2, 0.35, -0.35, 0.5):
            if not best:
                break
            self.fs.inst.write("HORIZONTAL:DELAY:TIME %g"
                               % (out + step * bit))
            self.wfm.q("*OPC?")
            got = self.msk_delay_hits()
            if got is not None and got < best:
                best, where = got, out + step * bit
        self.fs.inst.write("HORIZONTAL:DELAY:TIME %g" % where)
        self.wfm.q("*OPC?")
        return {"delay": where, "centred": centred or not best}

    # A restarted tally does not climb, it arrives. Measured on a 784D
    # in DPO: MASK:COUNT:WAVEFORMS? reads 0 at 0.1, 0.2, 0.3 and 0.5
    # seconds and then 26,752 at 0.8, and 2.0 seconds reads exactly two
    # of those - the instrument publishes the count in blocks of about
    # that many acquisitions rather than continuously. A look shorter
    # than one block does not report a small number, it reports nothing
    # at all, which is a different thing entirely.
    def msk_delay_hits(self, most=4.0):
        """A tally to judge a delayed window by, or None if there is none.

        None rather than zero when the instrument has not published a
        block yet. The two are opposites and they look identical: zero
        hits over zero acquisitions is not a clean eye, it is no
        measurement. Reading it as a clean one is what made the tuning
        below do nothing at all - every offset scored zero, so the
        first was kept and the window never moved.

        Waited for rather than timed. A block is a fixed number of
        acquisitions, not a fixed time, so how long it takes depends on
        how fast the instrument is acquiring - which changes with the
        sweep, the display and what else is switched on. A second is
        enough at thirty thousand a second and not at twenty, and the
        difference showed up as a tuner that worked three runs out of
        four.
        """
        try:
            self.fs.inst.write("MASK:COUNT:STATE 0")
            self.fs.inst.write("MASK:COUNT:STATE 1")
            self.wfm.q("*OPC?")
        except (IOError, pyvisa.Error):
            return None
        end = time.time() + most
        while time.time() < end:
            time.sleep(0.25)
            try:
                if float(self.wfm.q("MASK:COUNT:WAVEFORMS?")):
                    return float(self.wfm.q("MASK:COUNT:TOTAL?"))
            except (TypeError, ValueError, IOError):
                return None
        return None

    def mid_level(self, name):
        """Halfway up whatever that input is carrying, in volts.

        The instrument's own set-to-50% is TRIGGER:MAIN SETLEVEL - the
        front panel's Set 50% button - but it acts on the live trigger
        source. The level is measured here instead so it can be set
        exactly, from the captured clock, without the clock having to
        stay the trigger source and while it is still displayed. Zero
        is not a safe default: one leg of a differential pair swings
        between ground and its supply and never goes near zero on the
        way down.
        """
        try:
            volts = [v for _t, v in
                     self.wfm.capture([name])[0][0].points()]
        except Exception:
            return 0.0
        return (max(volts) + min(volts)) / 2.0 if volts else 0.0

    def bit_locked(self, source, bit, tries=12, least=6):
        """Is the data standing still against what is triggering it?

        One phase off each of several records, handed to
        tds_wfm.phase_locked - which is where the measurement and the
        reason for it are written down.

        A phase per record rather than a crossing per record: a screen
        holding a bit and a half often has one crossing on it and
        sometimes none, so taking the first of each gave two or three
        readings out of six tries and phase_locked was answering on
        almost no evidence. The vector mean of a record's own crossings
        is the same measurement made properly.
        """
        seen = []
        for _try in range(tries):
            try:
                marks = tds_wfm.crossings_of(
                    self.wfm.capture([source])[0][0])
            except Exception:
                continue
            if marks:
                seen.append(tds_wfm.bit_phase(marks, bit))
            if len(seen) >= least + 2:
                break
        return tds_wfm.phase_locked(seen, bit)

    def chan_fits(self, name, most=4):
        """Put a channel on a scale that shows all of whatever is on it.

        Returns what it was set to, so the caller can put it back.

        A mask's setup names one input, because that is the input the
        mask is drawn for, so the channel a clock arrives on is on
        whatever was dialled up last. Measured on a 784D with CH2 left
        at 100 mV/div: a GPIB clock swinging +0.26 to +2.55 V read
        back as +0.22 to +0.51, its middle came out +0.36 V instead of
        +1.41, and the instrument triggered on the toe of the rising
        edge where the slew is slowest and the noise widest. 700,391
        hits over 215,409 acquisitions on a signal that passes.
        """
        inst = self.fs.inst
        was = dict((what, str(self.wfm.q("%s:%s?" % (name, what))).strip())
                   for what in ("SCALE", "POSITION", "OFFSET"))
        inst.write("%s:POSITION 0;OFFSET 0" % name)
        for _lap in range(most):
            try:
                volts = [v for _t, v in
                         self.wfm.capture([name])[0][0].points()]
            except Exception:
                break
            scale = float(self.wfm.q("%s:SCALE?" % name))
            if max(abs(min(volts)), abs(max(volts))) >= scale * 3.9:
                want = scale * 5.0
            elif max(volts) - min(volts) < scale * 2.0:
                want = max((max(volts) - min(volts)) / 6.0, 1.0e-3)
            else:
                break
            inst.write("%s:SCALE %.3E" % (name, want))
            self.wfm.q("*OPC?")
            time.sleep(self.COUNT_FOR)
        return was

    def chan_back(self, name, was):
        """Put a channel back the way chan_fits found it."""
        for what, value in was.items():
            self.fs.inst.write("%s:%s %s" % (name, what, value))

    def clock_on(self, source, bit, skip=()):
        """Which input carries a clock at the bit rate, and its level.

        Every candidate looked at in turn, which costs about a second
        each and puts each one on the graticule while it looks. This is
        the fallback: the dialog asks which input the clock is on and
        only comes here when the answer is "find it".

        `skip` is the inputs already spoken for - the second leg of a
        pair being differenced in MATH1, which carries data and is not
        a candidate however it reads.
        """
        for name, _shown in self.wfm.selection():
            if not name.startswith("CH") or name == source or name in skip:
                continue
            looks, level = self.clock_look(name, bit)
            if looks:
                return name, level
        return None, 0.0

    def clock_look(self, name, bit):
        """Is there a clock at the bit rate on this input, and where.

        Returns (looks like one, its middle in volts). The channel is
        switched on just long enough to read and then put back the way
        it was, because a clock is by definition an input nobody wants
        on the graticule.

        A square wave at the bit rate crosses its own middle every half
        a bit, evenly; data crosses at whole multiples of a bit and
        unevenly, so the gaps tell the two apart. The same measurement
        serves both routes: naming the input skips the hunt, it does
        not skip the check, so a probe on the wrong channel is still
        reported rather than triggered on.
        """
        inst = self.fs.inst
        shown = dict(self.wfm.selection()).get(name, False)
        if not shown:
            inst.write("SELECT:%s ON" % name)
            self.wfm.q("*OPC?")
            # A channel just switched on has not acquired anything
            # yet, and the first read after it either raises or
            # gives the last thing that was there. Without this the
            # clock is found or missed depending on timing, which
            # is worse than never finding it.
            time.sleep(self.COUNT_FOR)
        # On a scale that shows all of it before anything is read
        # off it: a clipped record has the wrong middle, and the
        # middle is the trigger level.
        # Put back in a finally (A11): a timeout on SCALE? or an empty
        # record part way through left the clock on screen and rescaled.
        was, gaps, level = None, [], 0.0
        try:
            was = self.chan_fits(name)
            for _try in range(4):
                try:
                    wave = self.wfm.capture([name])[0][0]
                except Exception:
                    time.sleep(0.2)
                    continue
                volts = [v for _t, v in wave.points()]
                if not volts or max(volts) - min(volts) < 0.1:
                    continue
                level = (max(volts) + min(volts)) / 2.0
                edges = tds_wfm.crossings_of(wave)
                gaps += [b - a for a, b in zip(edges, edges[1:])]
                if len(gaps) >= 4:
                    break
        finally:
            if was is not None:
                self.chan_back(name, was)
            if not shown:
                inst.write("SELECT:%s OFF" % name)
        # Every gap half a bit, to within a tenth of one. Data
        # cannot do that: its gaps are whole bits and vary.
        looks = bool(gaps) and all(abs(g - bit / 2.0) < bit / 10.0
                                   for g in gaps)
        return looks, level

    def msk_eye(self, source, bit, segments=(), math=False, clocked=True,
                secdiv=None):
        """Set the instrument up to show an eye, however it can.

        Two things are told to it rather than found: `math`, meaning
        the signal arrives as two single-ended probes to be differenced
        in MATH1 rather than as one differential probe, and `clocked`,
        which is False for no clock, the name of an input - "CH2" - for
        a clock on that input, or True to go looking for one. Neither
        can be discovered: the instrument cannot see what is on the end
        of a probe, and an input with nothing plugged in reads the same
        as one with a clock that is switched off.

        Naming the input is the quick way and the one the tab offers.
        It is still measured before it is trusted, so a probe on the
        wrong channel comes back as `nameless` rather than as a trigger
        on an empty input.

        The order is forced by the instrument rather than chosen. The
        trigger has to be on a bit boundary before the sweep is worth
        measuring; the sweep can only be measured where there is a
        waveform record, which DPO has not got; and the tally means
        nothing until both are settled. So: trigger, ordinary display,
        centre, DPO, count.

        MATH1 changes two of those. DPO and math are mutually exclusive
        - `SELECT:MATH1 ON` under DPO answers 551, "InstaVu active", and
        turning DPO on with math displayed switches the math off
        without saying so - and MASK:SOURCE refuses MATH1 outright, so
        the instrument's own counter would be tallying a single leg.
        The math route therefore runs on persistence and is judged
        here, from MATH1's own record. Measured on a 784D, both.
        """
        out = {"clock": None, "where": None, "crossings": 0,
               "display": None, "counting": False, "delayed": None,
               "math": bool(math), "wanted": bool(clocked), "bits": None,
               "adrift": None, "nameless": None}
        inst = self.fs.inst
        # The sweep the mask was drawn against, where the setup beside
        # it names one. Sending a mask no longer touches the
        # instrument, so without this the eye is built on whatever
        # timebase happened to be dialled up - measured on a 784D, a
        # USB full speed mask against 1 us/div gave 116,921,796 hits
        # over 345,839 acquisitions, and hid the clock as well: at
        # 20 ns a point a 41.7 ns half period is two samples, and the
        # gaps no longer look even enough to be a clock.
        if secdiv:
            inst.write("HORIZONTAL:MAIN:SECDIV %.6E" % float(secdiv))
            self.wfm.q("*OPC?")
        # Back to the plain sweep and the plain display before anything
        # is decided, because whatever was set up last time is still
        # set up now: a delayed window left over from a run with no
        # clock, with a clock plugged in since, triggers on the clock
        # through the wrong part of the record - measured, 551,535 hits
        # over 168,279 acquisitions on a signal that passes.
        inst.write("HORIZONTAL:MODE MAIN")
        inst.write("DISPLAY:MODE NORMAL")
        inst.write("DISPLAY:PERSISTENCE 0")
        # A limit test left running from last time stops the instrument
        # the moment its template is broken, which would look like an
        # eye that had frozen for no reason.
        inst.write("LIMIT:STATE OFF")
        inst.write("ACQUIRE:STOPAFTER RUNSTOP")
        inst.write("ACQUIRE:STATE RUN")
        self.wfm.q("*OPC?")
        # How much of a bit the graticule is holding, whatever the
        # sweep came from. An eye wants about one; a screen holding
        # twelve of them, or a twelfth of one, is not going to make one
        # and the user is better told than left to read a verdict off
        # it. Reported rather than refused: it is their instrument.
        try:
            out["bits"] = (10.0 * float(
                self.wfm.q("HORIZONTAL:MAIN:SECDIV?"))) / bit
        except (TypeError, ValueError):
            pass
        if math:
            # After the display is out of DPO and not before: SELECT
            # MATH1 while InstaVu is on answers 551, "InstaVu active --
            # deactivate to use math", and the math never appears.
            #
            # Programmer Manual 070-9876-00 page 2-205: a dual waveform
            # expression is "<source><operator><source>", the operators
            # being + - * /, and the sources CH<x> or REF<x>. There is
            # no command for a math waveform's vertical scale on this
            # family - only DEFINE, NUMAVG and PROCESSING - so the two
            # legs keep the scale the setup gave them and the
            # difference lands where the instrument puts it.
            inst.write('MATH1:DEFINE "CH1 - CH2"')
            # The second leg put on the first's settings. The setup
            # beside a mask names one channel, because a differential
            # probe needs one; two probes need both, and a leg left on
            # a finer scale clips and takes the difference with it.
            for what in ("COUPLING", "BANDWIDTH", "SCALE", "POSITION",
                         "OFFSET"):
                inst.write("CH2:%s %s"
                           % (what, str(self.wfm.q("CH1:%s?" % what)).strip()))
            inst.write("SELECT:CH2 ON")
            inst.write("SELECT:MATH1 ON")
            self.wfm.q("*OPC?")
            source = "MATH1"
        else:
            # Whatever the other route left behind. This program put
            # MATH1 on the graticule, so this program takes it off
            # again; left there it is a second trace across the mask.
            inst.write("SELECT:MATH1 OFF")
        # What to trigger on if there is no clock, or if the clock
        # turns out to be one the data is not locked to. On the math
        # route it has to be a channel in any case -
        # TRIGGER:MAIN:EDGE:SOURCE does not take MATH1.
        lead = "CH1" if math else source
        # `clocked` is False for none, True for "find it", or the name
        # of the input the user says the clock is on. Naming it skips
        # the hunt - four channels switched on in turn is a second each
        # and a trace across the mask every time - but not the check:
        # an input with nothing on it is reported, not triggered on.
        skip = ("CH1", "CH2") if math else ()
        if not clocked:
            clock, level = None, 0.0
        elif clocked is True:
            clock, level = self.clock_on(source, bit, skip=skip)
        elif clocked == source or clocked in skip:
            # The input they named is carrying the data. Said rather
            # than quietly searched, or the eye is built on a trigger
            # nobody chose.
            clock, level, out["nameless"] = None, 0.0, clocked
        else:
            looks, level = self.clock_look(clocked, bit)
            clock = clocked if looks else None
            if not looks:
                out["nameless"], level = clocked, 0.0
        if clock:
            # The level is measured rather than using the instrument's
            # own set-to-50% (TRIGGER:MAIN SETLEVEL): measuring gives an
            # exact level from the captured clock, needs the clock only
            # to be displayed rather than to be the live trigger source,
            # and copes with a clock that is a 0 to 5 V logic swing as
            # readily as one sitting about zero.
            inst.write("TRIGGER:MAIN:EDGE:SOURCE %s;COUPLING DC;SLOPE RISE"
                       % clock)
            inst.write("TRIGGER:MAIN:LEVEL %g" % level)
            # And off the graticule, however it was found. A clock is
            # triggered on perfectly well switched off, and its trace
            # over an eye mask is a solid band through the keep-out:
            # measured on a 784D with a 2 Vpp clock left displayed,
            # 2,143,642 hits over 58,455 acquisitions on a signal that
            # passes cleanly the moment it is switched off.
            inst.write("SELECT:%s OFF" % clock)
            out["clock"] = clock
        else:
            # Nothing to trigger on but the data. Said explicitly
            # rather than left alone, because whatever was triggered on
            # last time still is: a clock found on a previous run and
            # unplugged since leaves the instrument waiting on a dead
            # input. On the math route it has to be a channel in any
            # case - TRIGGER:MAIN:EDGE:SOURCE does not take MATH1.
            inst.write("TRIGGER:MAIN:EDGE:SOURCE %s;COUPLING DC;SLOPE RISE"
                       % lead)
            inst.write("TRIGGER:MAIN:LEVEL %g" % self.mid_level(lead))
        inst.write("DISPLAY:MODE NORMAL")
        inst.write("DISPLAY:PERSISTENCE 0")
        self.wfm.q("*OPC?")
        time.sleep(self.COUNT_FOR)
        if clock:
            # Triggered on a boundary already, so the main sweep only
            # has to be slid half a bit.
            got = self.msk_centre(bit, source)
            out["where"], out["crossings"] = got["where"], got["crossings"]
            # ... unless the data is not locked to that clock, which
            # looks exactly like a clock until the eye is drawn. Two
            # channels of one generator, set to exactly commensurate
            # frequencies and told to EQPHASE, still walked: see
            # tds_wfm.phase_locked. A clock nothing is locked to is
            # worse than no clock at all, so it is dropped and the
            # no-clock route taken instead.
            if not self.bit_locked(source, bit):
                out["adrift"], out["clock"] = clock, None
                inst.write("TRIGGER:MAIN:EDGE:SOURCE %s;COUPLING DC;"
                           "SLOPE RISE" % lead)
                inst.write("TRIGGER:MAIN:LEVEL %g" % self.mid_level(lead))
                self.wfm.q("*OPC?")
                out["delayed"] = self.msk_delayed(source, bit)
        else:
            # No clock, so triggering on the data can only ever show
            # half an eye - the bit after a rising edge is always high.
            # The delayed time base is the instrument's own answer to
            # that, and it leaves a whole eye on the glass rather than
            # only in this program's picture of it.
            out["delayed"] = self.msk_delayed(source, bit)
        # DPO where it is fitted, asked for rather than assumed: an
        # instrument without it takes the command and stays where it
        # was, so writing the mode and reading it back is the only way
        # to find out which kind you are talking to.
        if not math:
            inst.write("DISPLAY:MODE INSTAVU")
            self.wfm.q("*OPC?")
        out["display"] = str(self.wfm.q("DISPLAY:MODE?")).strip()
        if out["display"] != "INSTAVU":
            # No DPO, or math, which cannot have it. Persistence
            # overlays sweeps into the same picture the slow way -
            # hundreds of acquisitions rather than a hundred thousand,
            # but an eye all the same.
            inst.write("DISPLAY:MODE NORMAL")
            inst.write("DISPLAY:PERSISTENCE 5")
            self.wfm.q("*OPC?")
        # Where the delayed window really wants to be, judged by the
        # mask in whichever display is now on the glass. Last, because
        # the judging is the instrument's tally and DPO fills it a
        # thousand times faster than the ordinary display does.
        #
        # Not on the math route, for the same reason the verdict is not
        # taken there: MASK:SOURCE can only be a channel, so the tally
        # is one leg of a pair against a mask drawn for the difference.
        # Tuning a delay by that number would move the window to
        # wherever suits the wrong signal.
        if out["delayed"] and not math:
            out["delayed"] = self.msk_delay_tune(
                out["delayed"]["delay"], bit, segments,
                out["delayed"].get("adrift"))
        # And the clock route checked against the picture it made,
        # rather than against a measurement taken before there was a
        # picture. A generator's two channels are not one oscillator:
        # the same clock reads as locked over the two seconds it takes
        # to measure and has walked a whole bit a minute later.
        # Measured on a USB low speed signal that read as locked and
        # then scored 6,008,613 hits over 594,594 acquisitions - the
        # same signal, dropped to the delayed sweep, scores none. The
        # mask is the only test that cannot be fooled this way.
        if out["clock"] and segments and not math and self.msk_delay_hits():
            out["adrift"], out["clock"], out["where"] = clock, None, None
            self.msk_eye_no_clock(source, bit, segments, lead, out)
        # The instrument's counter is left alone on the math route. Its
        # source can only be a channel, so what it would be tallying is
        # one leg of the pair against a mask drawn for the difference -
        # a number that looks like a verdict and is not one.
        out["counting"] = False if math else self.msk_count_from()
        return out

    def msk_eye_no_clock(self, source, bit, segments, lead, out):
        """Build the eye on the delayed sweep, from wherever we are.

        The order is forced by the instrument: the centring measures
        waveform records and DPO has none, so the display goes back to
        the ordinary one, the window is placed, and then DPO comes on
        and the delay is tuned against the mask. Used both when no
        clock was found and when one was found and the picture it gave
        turned out to be walking.
        """
        inst = self.fs.inst
        inst.write("DISPLAY:MODE NORMAL")
        inst.write("DISPLAY:PERSISTENCE 0")
        inst.write("TRIGGER:MAIN:EDGE:SOURCE %s;COUPLING DC;SLOPE RISE"
                   % lead)
        inst.write("TRIGGER:MAIN:LEVEL %g" % self.mid_level(lead))
        self.wfm.q("*OPC?")
        got = self.msk_delayed(source, bit)
        inst.write("DISPLAY:MODE INSTAVU")
        self.wfm.q("*OPC?")
        out["display"] = str(self.wfm.q("DISPLAY:MODE?")).strip()
        if out["display"] != "INSTAVU":
            inst.write("DISPLAY:MODE NORMAL")
            inst.write("DISPLAY:PERSISTENCE 5")
            self.wfm.q("*OPC?")
        out["delayed"] = self.msk_delay_tune(got["delay"], bit, segments,
                                             got.get("adrift"))
        return out

    def msk_centre(self, bit, source="CH1"):
        """Slide the sweep until the middle of a bit is on the mask.

        An eye mask is drawn about the middle of a bit and the trigger
        sits on a bit boundary, so the two are half a bit apart and the
        gap has to be measured rather than worked out: triggering on a
        clock, the phase between clock and data is fixed but arbitrary,
        and even on the data the shaping in a driver delays a crossing
        by however long it takes to slew.

        The crossings are bit boundaries, so folding them onto one bit
        and taking the mean angle - rather than an average, which knows
        nothing about wrapping - gives the boundary the sweep is
        holding. Half a bit past it is the middle.
        """
        wide = 10.0 * float(self.wfm.q("HORIZONTAL:MAIN:SECDIV?"))
        found = []
        for _try in range(10):
            try:
                wave = self.wfm.capture([source])[0][0]
            except tds_wfm.NotReadable:
                # DPO. There is no record to measure and no amount of
                # asking again will make one, so say so at once rather
                # than throwing at somebody who pressed a button.
                return {"where": None, "crossings": 0}
            found += tds_wfm.crossings_of(wave)
            if len(found) >= 6:
                break
        where = tds_wfm.bit_centre(found, bit, wide)
        if where is None:
            return {"where": None, "crossings": 0}
        self.fs.inst.write("HORIZONTAL:TRIGGER:POSITION %g" % where)
        self.wfm.q("*OPC?")
        return {"where": where, "crossings": len(found)}

    def msk_behind(self, math=False):
        """Something to draw behind a mask, whichever the scope can give.

        A trace where there is one to read; the screen itself where
        there is not. In DPO there is no waveform record at all - every
        sample reads back mid-scale - so the picture on the glass is the
        only honest answer, and cropped to its graticule it lines up
        with the editor's own ten by eight divisions exactly.

        `math` says the signal is the difference of two probes, taken
        in MATH1. Then MATH1 is what to read, and the instrument's own
        tally is not read at all: its source can only be a channel, so
        it is counting one leg against a mask drawn for the pair.

        The tally is read, never restarted. Start measurement is what
        zeroes it, and a picture taken afterwards is meant to say what
        has landed since - zeroing it here made every verdict a tally
        over the second it took to take the picture.
        """
        counting = False if math else self.msk_counting()
        if math:
            waves, _refused, _how = self.wfm.capture(["MATH1"])
            return {"wave": waves[0] if waves else None, "hits": None}
        if self.wfm.in_dpo():
            shot = self.scr.capture()
            pixels, wide, tall = shot.graticule()
            return {"shot": {"pixels": pixels, "palette": shot.palette,
                             "width": wide, "height": tall},
                    "hits": self.msk_count_read() if counting else None}
        live = [n for n in self.wfm.sources() if n.startswith("CH")]
        if not live:
            live = self.wfm.sources()
        if not live:
            raise tds_wfm.NotReadable(None,
                                      _("No source is displayed on the "
                                      "instrument, so there is nothing "
                                      "to capture."))
        # The tally is read *before* the capture, not after it. A
        # capture stops the acquisition to read the record out, so a
        # count taken afterwards is a count over nothing - measured:
        # zero hits over zero acquisitions, every time. A second of
        # watching first is a few hundred acquisitions to judge on,
        # where the record that comes back is one.
        hits = None
        if counting:
            time.sleep(self.COUNT_FOR)
            hits = self.msk_count_read()
        waves, _refused, _how = self.wfm.capture(live[:1])
        return {"wave": waves[0] if waves else None, "hits": hits}

    def msk_watch(self):
        """Set the instrument up to judge a mask that draws no eye.

        A level mask has no unit interval - GPIB's TTL undefined zone,
        RS-232's, a SATA burst envelope - so there is no bit to centre
        and nothing to delay. There is still a display to set, and this
        route was not setting one: it zeroed the counter and left the
        glass exactly as the last mask left it. Two things followed
        from that on the bench. DPO never came on, so the instrument
        tested a few hundred acquisitions a second where it can test a
        few hundred thousand. And a delayed window left over from the
        eye mask before it stayed on, so a GPIB pulse was judged
        through a window sixteen bits after its own trigger.

        Everything msk_eye does except the eye: the sweep back to
        MAIN, a limit test from last time switched off, DPO where it is
        fitted and persistence where it is not, and then the tally.
        """
        inst = self.fs.inst
        inst.write("HORIZONTAL:MODE MAIN")
        inst.write("LIMIT:STATE OFF")
        inst.write("ACQUIRE:STOPAFTER RUNSTOP")
        inst.write("ACQUIRE:STATE RUN")
        inst.write("DISPLAY:MODE INSTAVU")
        self.wfm.q("*OPC?")
        display = str(self.wfm.q("DISPLAY:MODE?")).strip()
        if display != "INSTAVU":
            inst.write("DISPLAY:MODE NORMAL")
            inst.write("DISPLAY:PERSISTENCE 5")
            self.wfm.q("*OPC?")
        return {"display": display, "counting": self.msk_count_from()}

    def msk_count_from(self):
        """Zero the instrument's own mask tally and start it counting.

        There is no MASK:COUNT:RESET on this firmware - it answers
        "Undefined header" - so the state going off and on again is the
        only way to clear the counters. Started here and read after the
        capture, the tally covers exactly the time the picture was
        being built, which is what makes it the picture's verdict.
        """
        if getattr(self, "_counts", None) is False:
            return False
        try:
            self.fs.inst.write("MASK:COUNT:STATE 0")
            self.fs.inst.write("MASK:COUNT:STATE 1")
            self.wfm.q("*OPC?")
            self._counts = True
            return True
        except Exception:
            self._counts = False
            return False

    def msk_counting(self):
        """Is the instrument's mask tally running?

        Asked rather than remembered, because the counter belongs to
        the instrument and somebody can turn it off at the front panel
        between two presses here. An instrument with no mask subsystem
        answers nothing, which is the same as no - and is asked only
        once, because a query to a subsystem that is not there does not
        answer "Undefined header", it does not answer at all and the
        bus waits out its timeout. Measured on MATH1:SCALE?, which does
        exactly that.
        """
        if getattr(self, "_counts", None) is False:
            return False
        try:
            said = str(self.wfm.q("MASK:COUNT:STATE?")).strip().upper()
        except Exception:
            self._counts = False
            return False
        return said in ("1", "ON")

    def msk_count_read(self):
        """What landed in the mask while the picture was being taken.

        The instrument counts in DPO, where there is no waveform record
        to judge and nothing else can give a verdict at all. Measured on
        a 784D: a hundred and twenty thousand acquisitions against three
        hundred in the ordinary display, which is the whole reason to
        run an eye that way.
        """
        try:
            total = float(self.wfm.q("MASK:COUNT:TOTAL?"))
            waves = float(self.wfm.q("MASK:COUNT:WAVEFORMS?"))
        except Exception:
            self._counts = False
            return None
        each = {}
        for n in range(1, tds_msk.SEGMENTS + 1):
            try:
                landed = float(self.wfm.q("MASK:MASK%d:COUNT?" % n))
            except Exception:
                break
            if landed:
                each[n] = landed
        return {"total": total, "waves": waves, "each": each}

    def msk_send(self, lines, display=True):
        """Put a mask into the instrument's eight segments and show it.

        Read back afterwards rather than trusted: the segments answer
        with their point counts, and a count that does not match what
        was sent means the instrument kept something else. Measured on a
        784D from a clean setup, points round-trip exactly.
        """
        self.wfm.send_mask(lines, display)
        want = {}
        for cmd in lines:
            if ":POINTSPCNT " not in cmd:
                continue
            n = int(cmd[len("MASK:MASK"):].split(":")[0])
            pairs = len(cmd.split(" ", 1)[1].split(",")) // 2
            want[n] = 0 if pairs < 2 else pairs
        got = self.wfm.mask_segments() or {}
        return {"wanted": want,
                "got": dict(enumerate(got, 1)) if got else {}}

    def msk_envelope(self, lines, dest, numbers, allocate_from=None):
        """Send a mask as a limit template, then read it back.

        The read-back is reported rather than raised on. This route has
        been built from Tektronix's own .ENV files and checked as far as
        it can be checked from here, but reading an envelope back off an
        instrument has not been measured on one - so a mismatch is shown
        with both sets of numbers instead of being declared a failure of
        the instrument. See TdsWfm.verify_envelope.
        """
        out = self.wfm.send_envelope(lines, dest, allocate_from)
        out.update(self.wfm.verify_envelope(dest, numbers))
        return out


    # -- limit tests -----------------------------------------------------

    def lim_build(self, source, vertical, horizontal, dest):
        """Have the instrument build a limit template for itself.

        The other way to get one, and much the easier to explain: point
        it at a signal that is known good, say how far a later one may
        wander from it, and it writes the envelope. No mask, no
        polygons, no percent of the graticule.

        Measured on a 784D: the reference that comes back really is an
        envelope - "Ref3, DC coupling, 500.0mVolts/div, 200.0ns/div,
        500 points, Envelope mode", PT_FMT ENV - and a limit test
        against it holds while the signal is the same and stops within
        two seconds of the amplitude being doubled. Tolerances of zero
        are taken without complaint.

        Tolerances are in divisions, which is what the instrument wants
        and, as it happens, what a person can see: half a division is
        half a division on the glass.
        """
        inst = self.fs.inst
        # Not in DPO. A limit test under InstaVu reads back on and
        # tests nothing - 550, "InstaVu active" - and a template built
        # there would be built from a record that does not exist.
        inst.write("ACQUIRE:STOPAFTER RUNSTOP")
        inst.write("LIMIT:STATE OFF")
        inst.write("DISPLAY:MODE NORMAL")
        inst.write("ACQUIRE:STATE RUN")
        self.wfm.q("*OPC?")
        time.sleep(self.COUNT_FOR)
        inst.write("LIMIT:TEMPLATE:SOURCE %s" % source)
        inst.write("LIMIT:TEMPLATE:DESTINATION %s" % dest)
        inst.write("LIMIT:TEMPLATE:TOLERANCE:VERTICAL %g" % vertical)
        inst.write("LIMIT:TEMPLATE:TOLERANCE:HORIZONTAL %g" % horizontal)
        inst.write("LIMIT:TEMPLATE STORE")
        self.wfm.q("*OPC?")
        time.sleep(0.6)
        # What the store itself said, read before exists() is asked -
        # that question clears the queue behind it, and a template that
        # was not made would otherwise be reported with no reason
        # attached to it.
        told = [m for m in tds_err.TdsErr(inst, self.fs.payload).drain()
                if not m.startswith("0,")]
        made = self.wfm.exists(dest)
        return {"dest": dest, "source": source,
                "why": [] if made else told,
                "made": made,
                "vertical": vertical, "horizontal": horizontal}

    def lim_run(self, source, dest):
        """Judge `source` against the template in `dest`, from now on.

        Three rules, each measured on a 784D: out of DPO first, the
        test switched on before the instrument is told to stop on it,
        and no *OPC? after STOPAFTER LIMIT - the instrument does not
        answer that one until the test trips.
        """
        inst = self.fs.inst
        inst.write("ACQUIRE:STOPAFTER RUNSTOP")
        inst.write("DISPLAY:MODE NORMAL")
        inst.write("ACQUIRE:STATE RUN")
        self.wfm.q("*OPC?")
        inst.write("LIMIT:COMPARE:%s %s" % (source, dest))
        inst.write("LIMIT:STATE ON")
        self.wfm.q("*OPC?")
        on = str(self.wfm.q("LIMIT:STATE?")).strip() in ("1", "ON")
        why = [] if on else [
            m for m in tds_err.TdsErr(inst, self.fs.payload).drain()
            if not m.startswith("0,")]
        if on:
            inst.write("ACQUIRE:STOPAFTER LIMIT")
            inst.write("ACQUIRE:STATE RUN")      # and no *OPC? after it
        return {"dest": dest, "source": source, "on": on, "why": why}

    def lim_survey(self, names=None):
        """What each named reference holds. Reads only.

        `names` None means all four. The tab asks for one at a time -
        the one that was double-clicked - because reading a reference
        that holds nothing costs a refusal and an event, and doing that
        to all four every time the tab is opened is four refusals
        nobody asked for.

        An envelope read off a reference that holds nothing comes back
        empty rather than raising, and a reference that has been
        deleted refuses the read - both of which mean the same thing
        here, so both are reported as empty. See TdsWfm.read_envelope
        and delete_ref.

        Cheap where it matters: a reference with nothing in it costs a
        refusal rather than a curve, so a bench with one template in
        use pays for one real read.
        """
        out = []
        for name in (names or tds_wfm.REFS):
            try:
                band = self.wfm.read_envelope(name)
            except Exception:
                band = []
            out.append({"name": name, "columns": len(band)})
        return {"refs": out}

    def lim_picture(self, source, dest):
        """The template and a live trace, for drawing.

        The template is an envelope: two values a column, the lowest
        and the highest the signal may be there. Read as a curve rather
        than as a waveform, because a Waveform is one value a point and
        an envelope is not one of those.
        """
        band = self.wfm.read_envelope(dest)
        wave = None
        try:
            got, _refused, _how = self.wfm.capture([source])
            wave = got[0] if got else None
        except Exception:
            pass
        return {"band": band, "wave": wave, "dest": dest,
                "source": source}

    def limit_state(self):
        """Is the limit test still passing? Running means yes.

        The instrument reports a failure by stopping, so this is the
        whole verdict. Nothing here writes, and nothing asks *OPC?.
        """
        try:
            return {"running": str(self.wfm.q("ACQUIRE:STATE?")).strip()
                    in ("1", "ON", "RUN"),
                    "on": str(self.wfm.q("LIMIT:STATE?")).strip()
                    in ("1", "ON")}
        except Exception:
            return {"running": None, "on": False}

    def limit_stop(self):
        """Switch the test off and give the instrument back."""
        inst = self.fs.inst
        inst.write("LIMIT:STATE OFF")
        inst.write("ACQUIRE:STOPAFTER RUNSTOP")
        inst.write("ACQUIRE:STATE RUN")
        self.wfm.q("*OPC?")
        return {"on": False}

    # How far after the trigger to look when there is no clock. What
    # decorrelates the data is how many different edges can trigger it,
    # not how long the delay is, so this is kept short: the delay's own
    # jitter grows with it and buys nothing.
    DELAY_BITS = 16
