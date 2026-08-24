"""
tds_fs.py - TDS500/600/700 filesystem access over GPIB.

Command forms are taken verbatim from the decompiled on-instrument
FileSystemProxy (TDS.jar, tek.tds.proxies.FileSystemProxy), so they are what
this firmware family actually implements rather than a guess from a manual:

    FILESYSTEM:CWD "hd0:"            set working directory
    FILESYSTEM:CWD?                 query it (reply is quoted)
    FILESYSTEM:DIR?                 list cwd - takes NO argument
    FILESYSTEM:FREESPACE?           bytes free
    :FILESYSTEM:READF "<path>"      then read raw bytes until EOI
    FILESYSTEM:WRITEFILE "<p>", #0  then raw bytes, EOI on the last one
    :FILESYSTEM:MKDIR "<path>"
    :FILESYSTEM:DELETE "<path>"

The proxy sleeps 1000 ms after READF for hd0: and 3000 ms otherwise before
reading; that delay is reproduced here.
"""
import time

import pyvisa

DEFAULT_ADDR = "GPIB0::3::INSTR"


class TdsFs(object):
    # A stalled transfer must FAIL, not sit there holding the VISA session.
    # 600 s was chosen so a 300 kB write at 2.9 kB/s could finish, but it also
    # meant a wedged small write blocked for ten minutes and had to be killed
    # -- which is what corrupts things. Size the timeout to the transfer:
    # use TdsFs() for small operations and TdsFs(timeout_ms=600000) only when
    # actually pushing hundreds of kB.
    def __init__(self, addr=DEFAULT_ADDR, timeout_ms=45000):
        # Writes run at roughly 2-3 kB/s, so a 300 kB payload needs minutes.
        # The default here is deliberately generous; a timeout part-way
        # through an indefinite-length #0 block leaves the instrument waiting
        # for data that will never arrive, and only a device clear recovers it.
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(addr, open_timeout=5000)
        self.inst.timeout = timeout_ms
        # Binary payloads must not have termination characters appended or
        # scanned for; we drive EOI explicitly.
        self.inst.write_termination = ""
        self.inst.read_termination = ""
        # What this particular firmware can do. Filled in by
        # probe_transfers(); assumed to be a full-featured instrument
        # until something says otherwise.
        self.reader = "READFILE"
        self.can_write = True
        self.has_filesystem = True

    def clear(self):
        """Device clear. Use after any aborted transfer before doing anything
        else, or the instrument stays stuck mid-block."""
        try:
            self.inst.clear()
            time.sleep(0.5)
        except Exception:
            pass

    def close(self):
        try:
            self.inst.close()
        except Exception:
            pass

    # -- plain queries -----------------------------------------------------

    def idn(self):
        return self.inst.query("*IDN?").strip()

    def hello(self, seconds=3.0):
        """Identify the instrument quickly, or fail quickly.

        The working timeout is 45 seconds, which is right for a transfer
        and hopeless as a way of finding out whether anything is at this
        address at all: VISA opens a GPIB address whether or not a device
        is listening, so the first query is what discovers an empty
        address - and it used to take three quarters of a minute to do
        it. A scope that is switched on answers *IDN? in milliseconds.
        """
        was = self.inst.timeout
        self.inst.timeout = int(seconds * 1000)
        try:
            return self.idn()
        finally:
            self.inst.timeout = was

    def opts(self):
        return self.inst.query("*OPT?").strip()

    @staticmethod
    def payload(reply):
        """The answer without its SCPI header.

        With HEADER ON the instrument echoes the command before the value:

            :FILESYSTEM:FREESPACE 0
            :FILESYSTEM:DIR "APP","OSSA.BAT"

        A TDS 784D ships with headers off and a TDS 640A with them on, so
        neither can be assumed. The header is stripped here as well as
        being turned off at connect, because an instrument that declines
        the setting should still work.
        """
        reply = (reply or "").strip()
        if reply.startswith(":") and " " in reply:
            return reply.split(" ", 1)[1].strip()
        return reply

    def headers(self, state):
        """HEADER OFF makes every reply just the value."""
        self.inst.write("HEADER %s" % state)

    def get_cwd(self):
        return self.payload(self.inst.query("FILESYSTEM:CWD?")).strip('"')

    def set_cwd(self, path):
        self.inst.write('FILESYSTEM:CWD "%s"' % path)

    def freespace(self):
        """Free bytes, or 0 if the instrument will not say.

        An instrument with no disk fitted, or no disk inserted, answers 0
        or not at all. That is worth reporting as zero rather than raising
        - it must not be the thing that stops a connection.
        """
        try:
            return int(self.payload(self.inst.query("FILESYSTEM:FREESPACE?")))
        except Exception:
            return 0

    def dir(self, path=None):
        """List a directory. Note DIR? takes no argument - we cd first."""
        if path is not None:
            self.set_cwd(path)
        reply = self.payload(self.inst.query("FILESYSTEM:DIR?"))
        out = []
        for tok in reply.split(","):
            tok = tok.strip().strip('"')
            if tok:
                out.append(tok)
        return out

    # -- file transfer -----------------------------------------------------

    UNDEFINED_HEADER = 113
    MAV = 0x10                  # status byte bit 4: message available
    # How long to give the instrument to start talking before deciding it
    # swallowed the command. Both a 206-byte file and a 38 KB one set MAV
    # inside a second - the big one then streams for twelve, but it starts
    # straight away - so four seconds is well outside the normal range.
    PRINT_START = 4.0

    # A name that cannot be on any disk: illegal under 8.3, and unlikely
    # in any case. Used to ask a question, never to read anything.
    NO_SUCH_FILE = "~NONE~.$$$"

    def has_command(self, command):
        """Does this firmware have this command?

        Asked by naming a file that is not there. A firmware that has the
        command answers 256, "file name not found" - the same event this
        program raises dozens of times a minute classifying directory
        entries, so it is invisible. One that does not have the command
        answers 113, "undefined header".

        The earlier version of this sent the bare command header with no
        arguments, which is also conclusive - 100 against 113 - but 100 is
        "command error" and the instrument puts that on its own display.
        Provoking a visible error on someone's oscilloscope to satisfy our
        curiosity is not on, when a question it answers calmly exists.
        """
        self.errors()                       # start from an empty queue
        try:
            self.inst.write(command)
        except Exception:
            return False
        time.sleep(0.3)
        return self.UNDEFINED_HEADER not in self.errors()

    def apply_known(self, entry):
        """Take an instrument's capabilities from the table, not the bus."""
        self.reader = entry.get("reader")
        self.can_write = bool(entry.get("can_write"))
        self.has_filesystem = entry.get("filesystem", True)
        return {"reader": self.reader, "can_write": self.can_write,
                "filesystem": self.has_filesystem, "source": "table"}

    def probe_transfers(self):
        """Work out how, and whether, this instrument can move file bodies.

        A TDS 784D and a TDS 784C both have READFILE and WRITEFILE. A TDS
        640A on firmware v3.8.8e has neither, and its only route to a
        file's contents is FILESYSTEM:PRINT to the GPIB port. Rather than
        keep a table of models, each instrument is asked.

        Only reading is probed. Writing is taken to follow it, because the
        two commands arrived in the firmware together and because there is
        no way to ask about WRITEFILE that does not either create a file or
        provoke a visible error - and an upload that turns out to be
        impossible reports itself clearly enough when it is attempted.
        """
        if self.has_command('FILESYSTEM:READFILE "%s"' % self.NO_SUCH_FILE):
            self.reader = "READFILE"
        elif self.has_command('FILESYSTEM:PRINT "%s",GPIB' % self.NO_SUCH_FILE):
            self.reader = "PRINT"
        else:
            self.reader = None
        self.can_write = self.reader == "READFILE"
        # Whether there is a filesystem at all is a separate question from
        # whether files can be transferred: the earliest firmware has no
        # FILESYSTEM subsystem whatsoever, so there is nothing to browse.
        self.has_filesystem = self.has_command("FILESYSTEM:CWD?")
        return {"reader": self.reader, "can_write": self.can_write,
                "filesystem": self.has_filesystem, "source": "asked"}

    def read(self, path, timeout=None):
        """Read a file off the instrument. Returns bytes.

        `timeout` overrides the session timeout for this transfer only.
        Worth doing: the default is set long enough for a large file at
        33 KB/s, and an instrument that will not answer at all should not
        cost that long to find out about.
        """
        if self.reader is None:
            raise IOError("This instrument's firmware has no command for "
                          "reading a file's contents over GPIB.")
        old = self.inst.timeout
        if timeout:
            self.inst.timeout = int(timeout * 1000)
        try:
            if self.reader == "PRINT":
                return self._read_by_print(path)
            settle = 1.0 if "HD0:" in path.upper() else 3.0
            self.inst.write(':FILESYSTEM:READF "%s"' % path)
            time.sleep(settle)
            return bytes(self.inst.read_raw())
        finally:
            self.inst.timeout = old

    def _wait_mav(self, limit):
        """Has the instrument got something to say yet?

        Bit 4 of the status byte is MAV, message available, and a serial
        poll reads it without going through the output queue - so it can
        be asked repeatedly while the instrument works, and it cannot
        disturb the data waiting behind it. Sleeping a fixed time instead
        and reading blind fails whenever the guess is short: measured on a
        TDS 640A, a 0.6 s sleep got 11 of 20 small files where polling got
        20 of 20.
        """
        end = time.time() + limit
        while time.time() < end:
            try:
                if self.inst.read_stb() & self.MAV:
                    return True
            except Exception:
                pass
            time.sleep(0.05)
        return False

    def _read_by_print(self, path):
        """Read a file on a firmware that has no READFILE.

        FILESYSTEM:PRINT to the GPIB port hands over the file itself, and
        the bytes are exact. What it also does, on a TDS 640A, is swallow
        the command that follows a large transfer: it is accepted, raises
        no event, and produces nothing. The attempt after that works.

        So one retry, and one only. Nothing found clears the state
        deliberately - *OPC?, BUSY?, *CLS, HARDCOPY ABORT, a ten second
        wait and a device clear were all measured and none of them helped
        - but the swallowed attempt clears it itself. The limit of one
        matters: a long run of failed prints is what takes the instrument's
        filesystem down altogether, and that costs a power cycle.
        """
        last = None
        for _attempt in (1, 2):
            self.inst.write('FILESYSTEM:PRINT "%s",GPIB' % path)
            if not self._wait_mav(self.PRINT_START):
                last = ("the instrument accepted the command and sent "
                        "nothing")
                continue
            try:
                return bytes(self.inst.read_raw())
            except Exception as exc:
                last = "%s while reading" % type(exc).__name__
        raise IOError("%s could not be read: %s (tried twice; not trying "
                      "again, because repeated failures put this "
                      "instrument's filesystem out of action until it is "
                      "power cycled)." % (path, last))

    def write(self, path, data):
        """Write bytes to a file on the instrument.

        Refused outright on a firmware with no WRITEFILE, because sending
        it anyway would be accepted in silence and do nothing.

        Header and payload go out as one transfer so that EOI lands on the
        final data byte, which is what terminates the indefinite-length #0
        block. Splitting them would assert EOI after the header.
        """
        if not self.can_write:
            raise IOError("This instrument's firmware has no "
                          "FILESYSTEM:WRITEFILE command, so files cannot "
                          "be uploaded to it over GPIB.")
        header = ('FILESYSTEM:WRITEFILE "%s", #0' % path).encode("ascii")
        self.inst.write_raw(header + data)

    def wait_done(self, timeout=30.0):
        """Block until the instrument has finished the last operation.

        RMDIR and friends return immediately and then work in the
        background, and a command that arrives while dosFs is still walking
        a directory raises event 250, "Mass storage error" - which the front
        panel shows as an error even though the operation itself completes.

        *OPC? does not answer until pending operations are done. Measured
        after an RMDIR of a ten-file folder it returns in 0.15 to 0.67 s,
        tracking the real work, where the fixed 0.8 s sleep it replaces was
        sometimes too short and sometimes three times too long.

        Falls back to a generous sleep if *OPC? does not answer, because a
        missing sync is worth a wasted second and not worth an exception.
        """
        old = self.inst.timeout
        self.inst.timeout = int(timeout * 1000)
        try:
            self.inst.query("*OPC?")
        except Exception:
            time.sleep(3.0)
        finally:
            self.inst.timeout = old

    def mkdir(self, path):
        self.inst.write(':FILESYSTEM:MKDIR "%s"' % path)

    def delete(self, path):
        """Delete a FILE.

        Note this does nothing at all to a directory - not an error, not an
        event, just silence and the directory still there. Measured; see
        rmdir() for the command that does work.
        """
        self.inst.write(':FILESYSTEM:DELETE "%s"' % path)

    def rmdir(self, path):
        """Remove a DIRECTORY - **including everything inside it**.

        FILESYSTEM:RMDIR is not in tds_fs's original command set and was found
        by probing after DELETE turned out to be a silent no-op on folders.

        It is recursive and it is silent. Measured on a TDS 784D: RMDIR on a
        directory containing a file removed the directory and the file, raised
        no event, and gave no warning. There is no dry run and no undo, so the
        caller is responsible for asking first - the instrument will not.
        """
        self.inst.write(':FILESYSTEM:RMDIR "%s"' % path)

    def set_overwrite(self, state):
        """ON allows WRITEFILE to replace an existing file."""
        self.inst.write("FILESYSTEM:OVERWRITE %s" % state)

    def set_delwarn(self, state):
        """OFF suppresses the confirm-delete prompt."""
        self.inst.write("FILESYSTEM:DELWARN %s" % state)

    # -- error queue -------------------------------------------------------

    def errors(self):
        """Drain the event queue. 256 = file not found; 250-259 = IO error.

        The leading *ESR? is NOT optional and is not merely hygiene. On this
        firmware the event queue will not release codes until the Standard
        Event Status Register has been read. Ask EVENT? without that priming
        read and it answers 1 forever, never draining - the instrument spells
        it out if you ask with EVMSG? instead:

            1,"No events to report - new events pending *ESR?"

        An earlier version of this method omitted the *ESR? and therefore
        returned [1, 1, 1, ...] up to its own loop limit whenever anything
        was pending, which looks like twenty errors and is in fact none.
        Verified: *CLS, an undefined header, then EVENT? gives 1 repeatedly;
        the same sequence with *ESR? in front gives 113 and then 0.

        The queue is drained with EVMSG? rather than EVENT?, which costs
        nothing extra and returns the instrument's own wording alongside the
        code. Callers that only want codes get the same list they always
        did; the text is kept in `self.last_messages` for whoever is
        logging, because an event is only diagnosable if the code was
        recorded together with what the instrument called it.
        """
        self.last_messages = []
        try:
            self.inst.query("*ESR?")
        except Exception:
            return []
        out = []
        for _ in range(20):
            reply = self.inst.query("EVMSG?").strip()
            head, _, text = reply.partition(",")
            try:
                code = int(head.strip())
            except ValueError:
                break
            # 0 is an empty queue; 1 means "new events pending *ESR?", which
            # after the priming read above means there is nothing left.
            if code in (0, 1):
                break
            out.append(code)
            self.last_messages.append((code, text.strip().strip('"')))
        return out
