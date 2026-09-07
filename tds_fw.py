"""
tds_fw.py - the bootloader these instruments start in, and the flash behind it.

Started with the NVRAM protection switch at unprotected, a TDS500/600/700
comes up in a ROM monitor instead of its firmware. The screen stays dark and
every front-panel LED stays lit, which reads as a dead instrument, but the
monitor answers on GPIB at address 29. It is in mask ROM, so it is there
whatever state the flash is in and a failed write is reflashed rather than
mourned.

The monitor speaks three commands, each a four-byte header - a letter, a
checksum, and a big-endian length - followed by that many bytes:

    m  addr, len                 read memory, reply '=' then the bytes
    M  addr, len, bytes          write memory, reply '='
    B  0, 0, function, arg, ...  call code at `function`, reply 'P'

Every reply opens with '+' and is acknowledged with '+'. The checksum is the
plain sum of the packet with the checksum byte itself taken as zero.

The flash is two 16-bit Intel parts side by side on a 32-bit bus, so every
command goes out in all four bytes of a longword and every status is read in
both halves. Erase is chip-wide - one command for the whole part - and
programming is a 512-byte page at a time through the part's page buffer.

Doing that from here costs about seventeen bus turnarounds per page, nearly
all of them four-byte pokes, which is several hours for four megabytes. So
the page loop is put on the instrument instead: `HELPER` is 292 bytes of 68k
written into scratch RAM once and called with B, one call per page, and the
same four megabytes take minutes. The host keeps the slow path and drops to
it for any page the helper will not take.

`fwhelper.S` beside this file is the helper's source; `HELPER` is what comes
out of it.
"""
import binascii
import hashlib
import os
import re
import struct
import sys
import zipfile
import time

# ---------------------------------------------------------------- the map
FLASH_BASE = 0x01000000
NVRAM_BASE = 0x04000000
#: The whole window the monitor answers for. Not all of it is distinct -
#: on a TDS680B the top 384 kB is the 512 kB part repeating on its own
#: boundary, and on a TDS784C, verified over the bus, it is empty past
#: the parts. See INSTRUMENT-NOTES.
NVRAM_LEN = 0x100000
#: The most distinct NVRAM any instrument in this family holds: a 128 kB
#: part at the base and a 512 kB part above it. Verified on a TDS784C
#: (the peek reads 0x9FFFE and then zeros) and it is the two parts'
#: datasheet sizes added. A backup always reads at least this much in
#: full - it is where everything that cannot be re-derived lives - and
#: only ever trims ABOVE it, and only what a restore of the low region
#: would reproduce anyway. So trimming can drop empty or duplicate bytes
#: and nothing else.
NVRAM_KEEP = 0xA0000
#: Scratch RAM the monitor is not using. The helper is parked here.
SCRATCH_BASE = 0x05010000
#: How far apart the flash devices sit. Commands reach one device, so
#: anything array-wide is repeated at this stride and status is read at the
#: base of the device the address falls in.
DEVICE = 0x200000
PAGE = 512
#: Bytes per m transaction. Measured on a TDS784D: 1024 is answered
#: and 2048 is refused with an 'E', which is the 1024-byte buffer in the
#: monitor's own write command. The reference tools use 512; this is
#: half as many turnarounds for the same bytes.
CHUNK = 1024
#: Bytes per M transaction, which is not the same number. A read's 1024
#: is what the monitor SENDS; a write's payload is what it has to
#: RECEIVE into that same buffer, and the packet carries twelve bytes of
#: header and address in front of it. 1024 overruns it: measured on a
#: TDS680B, where the first chunk of a restore timed out and left the
#: monitor waiting for the rest of a packet it had been promised - stuck
#: badly enough that a device clear did not recover it and only a power
#: cycle did. tektool uses 512 and this is why.
WCHUNK = 512

BOOT_ADDR = 29

#: Every image in the catalogue opens with these: NOP, then JMP to an
#: absolute long address inside the flash. A blob that does not is not a
#: firmware image for this family, whatever it is called.
IMAGE_HEAD = b"\x4e\x71\x4e\xf9"

#: A page with nothing in it. Erase leaves the whole part like this, so
#: writing one is work that changes nothing.
BLANK = b"\xFF" * 512


class FirmwareError(RuntimeError):
    """The monitor said no, or said nothing."""


# ------------------------------------------------------------- the monitor
class Monitor(object):
    """The ROM monitor, over an already-open VISA session.

    The session is used raw: no termination characters either way, because
    every byte of these packets is data and a 0x0A in the middle of a page
    is not the end of anything.
    """

    def __init__(self, inst, timeout_ms=10000):
        self.inst = inst
        inst.write_termination = ""
        inst.read_termination = ""
        inst.timeout = timeout_ms

    @staticmethod
    def packet(cmd, body):
        """A command, with its checksum filled in.

        The checksum is computed over the packet with the checksum byte
        held at zero, which is what it already is while this runs.
        """
        out = (bytearray(cmd.encode("ascii")) + b"\0"
               + struct.pack(">H", len(body)))
        out += body
        out[1] = sum(out) & 0xFF
        return bytes(out)

    def _reply(self):
        """The '+', the header, and whatever follows it. Ack, then return."""
        ack = self.inst.read_bytes(1)
        if ack != b"+":
            raise FirmwareError(
                "the monitor did not acknowledge: %r" % (ack,))
        head = self.inst.read_bytes(4)
        if len(head) < 4:
            raise FirmwareError("short reply header: %r" % (head,))
        extra = struct.unpack(">H", head[2:4])[0]
        rest = self.inst.read_bytes(extra) if extra else b""
        self.inst.write_raw(b"+")
        return head[0:1], rest

    def read(self, addr, length):
        """`length` bytes from `addr`."""
        self.inst.write_raw(
            self.packet("m", struct.pack(">II", addr, length)))
        cmd, data = self._reply()
        if cmd != b"=":
            raise FirmwareError(
                "read of %d bytes at 0x%08X answered %r" % (length, addr, cmd))
        if len(data) != length:
            raise FirmwareError(
                "read of %d bytes at 0x%08X returned %d"
                % (length, addr, len(data)))
        return data

    def write(self, addr, data):
        """`data` to `addr`."""
        self.inst.write_raw(self.packet(
            "M", struct.pack(">II", addr, len(data)) + data))
        cmd, _rest = self._reply()
        if cmd != b"=":
            raise FirmwareError(
                "write of %d bytes at 0x%08X answered %r"
                % (len(data), addr, cmd))

    def poke(self, addr, value):
        """One longword, which is how a flash command is sent."""
        self.write(addr, struct.pack(">I", value))

    def peek(self, addr):
        """One longword."""
        return struct.unpack(">I", self.read(addr, 4))[0]

    def branch(self, function, arg, payload=b""):
        """Call code at `function`.

        The monitor hands the called routine one argument: a pointer to the
        four-byte header it will send back, with a pointer to this command's
        body at +4 of it. `arg` lands at body+12 and `payload` at body+16 -
        read off the monitor's own calling of a routine, not guessed.

        True if the routine reported success, False if it reported failure;
        anything else raises, because a monitor that has stopped speaking
        the protocol is not a failed page.
        """
        body = struct.pack(">IIII", 0, 0, function, arg) + payload
        self.inst.write_raw(self.packet("B", body))
        cmd, _rest = self._reply()
        if cmd == b"P":
            return True
        if cmd == b"-":
            return False
        raise FirmwareError(
            "call of 0x%08X answered %r" % (function, cmd))

    def awake(self):
        """Whether this is the monitor, and whether flash reads like flash.

        Returns the first four bytes of the flash so the caller can say
        which of the two failed. A monitor that answers but hands back
        something other than a 68k reset vector is worth showing rather
        than swallowing: it is the difference between the wrong address and
        an instrument that needs its firmware back.
        """
        return self.read(FLASH_BASE, 4)


def bootloader_resource(resource, addr=BOOT_ADDR):
    """The same GPIB board, at the bootloader's address.

    The instrument in bootloader mode is not at the address it uses in
    normal operation, and it cannot be asked where it is - it answers no
    *IDN?. So its address is derived from the one that was working before.
    """
    out, count = re.subn(r"(?i)^(GPIB\d*::)\d+(::.*)$", r"\g<1>%d\g<2>" % addr,
                         resource or "")
    return out if count else "GPIB0::%d::INSTR" % addr


# --------------------------------------------------------------- the flash
#: name, the two identifier longwords, the erase command, whether the part
#: has a page buffer, how many byte lanes it puts on the 32-bit bus, how
#: much one erase command covers - 0 for a part that erases a whole device
#: at once - and how much array an instrument fitted with it has. The
#: identifiers are what the parts answer to command 0x90, which is also
#: what says how the array is arranged: a value on each 16-bit half is two
#: parts side by side, the same byte four times is four.
#:
#: The last column is the answer both reference tools use, and they take it
#: from the identifier rather than measuring anything. Their table is wider
#: than this one: 0x180000 for a 28F010 and 0x300000 for a 28F020, which is
#: where the 1.5 MB and 3 MB the old dropdown offered came from. Neither of
#: those parts is one this drives - they are an older command set again -
#: and identify refuses them, so they are recorded here rather than listed.
FLASH_TYPES = (
    ("28F016SA", 0x00890089, 0x66A066A0, 0xA7, True, 2, 0, 0x400000),
    ("28F160S5", 0x00B000B0, 0x00D000D0, 0x30, False, 2, 0, 0x400000),
    # Four 1 MB byte-wide parts, as fitted to the TDS640A. No chip-erase
    # at all: sixteen 64 kB blocks each, so one command per 256 kB of bus.
    ("28F008SA", 0x89898989, 0xA2A2A2A2, 0x20, False, 4, 0x40000, 0x400000),
)

#: The helper, assembled from fwhelper.S. Three ways in: the page buffer at
#: +0, a longword at a time at +4 for the parts without one, and the same
#: again at +8 for an array of four byte-wide parts, where the status to
#: wait on is a byte on every lane rather than one on each half.
HELPER = binascii.unhexlify(
    "700060067001600270024e56000048e738382800206e0008226800042469000c47e9"
    "0010426800024228000110bc002d4a84660000a024bc71717171200a0280ffe00000"
    "50802840243c000400042602610000ca4a806b0000b624bce0e0e0e024bc007f007f"
    "24bc00000000284a747f28db51cafffc24bcffffffff24bc71717171200a0280fffe"
    "000058802840243c000800087600610000844a806b00007024bc0c0c0c0c24bc007f"
    "007f24bc0000000024bc71717171243c0080008026026100005a4a806b4624bc5050"
    "505010bc0050603a284a243c008000800c84000000026606243c808080802602383c"
    "007f28bc40404040289b61224a806b1024bcffffffff588c51ccffe810bc005024bc"
    "ffffffff4cdf1c1c4e5e4e75223c001000002014c082b0836708538166f470ff4e75"
    "70004e75")
HELPER_PAGE = SCRATCH_BASE
HELPER_WORD = SCRATCH_BASE + 4
HELPER_WORD4 = SCRATCH_BASE + 8


def _both(half):
    """A 16-bit value on both halves of the 32-bit bus."""
    return (half << 16) | half


class Flash(object):
    """The firmware flash, through the monitor."""

    def __init__(self, mon, base=FLASH_BASE):
        self.mon = mon
        self.base = base
        self.name = None
        self.paged = False
        #: How many byte-wide parts the array puts on the 32-bit bus. It
        #: decides where the status bits are, and nothing else.
        self.lanes = 2
        self.helper = None

    # -------------------------------------------------------------- naming
    def identify(self):
        """Which part is fitted. Leaves it reading its array again."""
        try:
            self.mon.poke(self.base, _both(0x9090))
            one = self.mon.peek(self.base)
            two = self.mon.peek(self.base + 4)
        finally:
            self.mon.poke(self.base, 0xFFFFFFFF)
        for row in FLASH_TYPES:
            if (one, two) == (row[1], row[2]):
                self.name, self.paged, self.lanes = row[0], row[4], row[5]
                return self.name
        raise FirmwareError(
            "unknown flash: identifier 0x%08X 0x%08X. Erasing a part this "
            "does not recognise is not attempted." % (one, two))

    def fitted(self):
        """How much array this part is fitted in, from its own row.

        The datasheet answer, which is what both reference tools use and
        the only answer available for a part that is blank all through.
        `span` measures the instrument in front of you; the two should
        agree, and a disagreement is worth saying out loud rather than
        quietly resolving one way.
        """
        return self._type()[7]

    def span(self, most=0x800000):
        """How much of the array is real, measured rather than guessed.

        An address past the end reads as one inside it: the part decodes
        only the lines it has, so the array is as long as the first
        device boundary whose contents repeat the base.

        An erased part cannot be measured this way - every boundary reads
        0xFF and the first one would look like a wrap - so a blank base
        gives back the usual size instead. Guessing high there would be
        worse than guessing low: reading past the end of a real array is
        answered with an error on at least one of these instruments, and
        that would stop a backup, while a part that is blank has nothing
        in it worth backing up in the first place.
        """
        head = self.mon.read(self.base, 256)
        if head == BLANK[:256]:
            return self.fitted()
        at = DEVICE
        while at < most:
            try:
                if self.mon.read(self.base + at, 256) == head:
                    return at
            except FirmwareError:
                return at           # nothing answers out there
            at += DEVICE
        return most

    def _flag(self, byte):
        """One status bit, on every lane the array puts on the bus.

        Two 16-bit parts answer 0x0080 on each half; four byte-wide ones
        answer 0x80 on each byte. Testing the first pattern against the
        second watches two of the four chips and takes their word for the
        other two.
        """
        return byte * (0x01010101 if self.lanes == 4 else 0x00010001)

    def _type(self):
        for row in FLASH_TYPES:
            if row[0] == self.name:
                return row
        raise FirmwareError("the flash has not been identified yet")

    # --------------------------------------------------------------- erase
    def erase(self, length, note=None, stop=None, limit=300.0):
        """Erase every device under `length` bytes from the base.

        One command erases all of a part's unlocked blocks at once, so
        there is no block loop - but the array is addressed as devices two
        megabytes apart and a command reaches only the device it is written
        to, so a four megabyte array takes two of them. Re-erasing a device
        that is already blank costs a poll and nothing else, which is why
        this rounds up rather than trying to be exact.
        """
        row = self._type()
        cmd, step = row[3], row[6] or DEVICE
        # One clock for the whole array, not one per device: the counter
        # in the status line is what somebody watches to decide whether
        # this has stopped, and restarting it at 0 halfway through says
        # exactly the wrong thing.
        start = time.time()
        for at in range(0, max(length, 1), step):
            here = self.base + at
            self.mon.poke(here, _both(0x5050))      # start from no status
            self.mon.poke(here, _both(cmd | (cmd << 8)))
            self.mon.poke(here, _both(0xD0D0))
            began = time.time()
            while True:
                if stop is not None and stop():
                    raise FirmwareError(
                        "erase abandoned part way through - the flash is "
                        "part erased and has to be programmed again")
                status = self.mon.peek(here)
                if status & self._flag(0x80) == self._flag(0x80):
                    break
                if time.time() - began > limit:
                    raise FirmwareError(
                        "the flash did not finish erasing within %d seconds; "
                        "status 0x%08X" % (limit, status))
                if note:
                    note("Erasing the flash ... %d s"
                         % int(time.time() - start))
                time.sleep(0.5)
            # Ready is not the same as done. The part reports a failed
            # erase in its own status, and without this a block that
            # would not erase sails through here and turns up later as
            # an unexplained verify failure - or, worse, as a page this
            # skipped because the image said it was blank.
            if status & self._flag(0x20):
                raise FirmwareError(
                    "the flash reported an erase failure at 0x%08X "
                    "(status 0x%08X). The part is not taking the erase; "
                    "nothing has been written." % (here, status))
            if status & self._flag(0x08):
                raise FirmwareError(
                    "the flash reported its programming voltage low at "
                    "0x%08X (status 0x%08X). Nothing has been written."
                    % (here, status))
            self.mon.poke(here, _both(0x5050))      # clear the status bits
            self.mon.poke(here, 0xFFFFFFFF)

    # ------------------------------------------------------------- reading
    def read(self, length, base=None, note=None, stop=None):
        """`length` bytes out of the part."""
        base = self.base if base is None else base
        out = bytearray()
        while len(out) < length:
            if stop is not None and stop():
                raise FirmwareError("read abandoned part way through")
            want = min(CHUNK, length - len(out))
            out += self.mon.read(base + len(out), want)
            if note and len(out) % 0x10000 < CHUNK:
                note("Reading %s of %s"
                     % (_size(len(out)), _size(length)),
                     len(out) / float(length))
        return bytes(out)

    def same_cell(self, a, b):
        """Whether two addresses are one cell, measured on the part.

        A part smaller than the window it sits in answers at every
        multiple of its own size, so two addresses can be one cell.
        Which ones is a property of the board's address decoding, and
        nothing about the data stored there says so - blank memory
        reads the same at every address whether or not it is aliased,
        which is why this writes rather than compares.

        Two probes, not one: where `a` and `b` already hold the same
        bytes, one probe cannot tell a mirror from a coincidence.

        Writes to `a` and puts back what was there, `b` is only read.
        Only safe once the caller holds a backup - a probe that is
        interrupted between the write and the restore leaves four
        bytes of nonsense at `a`.
        """
        was = self.mon.read(a, len(PROBES[0]))
        try:
            for probe in PROBES:
                self.mon.write(a, probe)
                if self.mon.read(a, len(probe)) != probe:
                    raise FirmwareError(
                        "the NVRAM did not take a write at 0x%X - it read "
                        "back as it was. Nothing has been changed. On this "
                        "family that is the memory protection switch: it "
                        "has to be moved to unprotected before anything "
                        "can be written." % a)
                if self.mon.read(b, len(probe)) != probe:
                    return False
            return True
        finally:
            self.mon.write(a, was)

    def measure_distinct(self, base, length, stop=None):
        """How many bytes from `base` are distinct cells, measured.

        A TDS680B answers for a megabyte and holds 640 kB of it: a
        128 kB part at the base and a 512 kB part above, the top 384 kB
        being that larger part a second time. A TDS640A holds 512 kB
        and repeats from the base. Which of those - or neither - is
        found here by writing, because it cannot be read out of the
        data and must not be guessed from it.

        It matters on the way in. Writing a whole dump back writes the
        aliased cells twice and the second copy wins: harmless on the
        instrument the dump came from, and on any other it writes the
        low region correctly and then scribbles over it.

        One stride for the whole window, taken at the top. An
        instrument with two separately aliased parts would need more
        than that; there is no evidence of one, and this says so rather
        than pretending to cover it.
        """
        probe = len(PROBES[0])
        top = base + length - probe
        stride = None
        for candidate in ALIAS_STRIDES:
            if candidate >= length:
                continue
            if stop is not None and stop():
                raise FirmwareError("measuring was abandoned part way")
            if self.same_cell(top - candidate, top):
                stride = candidate
                break
        if stride is None:
            return length
        # The clock is the first fourteen bytes of the part at the base
        # and is not something to write even for a moment, so the
        # lowest pair that can be tested is the byte after it against
        # its mirror. A boundary is a whole number of parts, so if that
        # pair is one cell the window repeats from `stride` exactly.
        if self.same_cell(base + RTC_LEN, base + stride + RTC_LEN):
            return stride
        for at in range(stride + PROBE_GRID, length, PROBE_GRID):
            if stop is not None and stop():
                raise FirmwareError("measuring was abandoned part way")
            if self.same_cell(base + at - stride, base + at):
                return at
        return length

    def nvram_keep_len(self, note=None, stop=None):
        """How much of the NVRAM window a backup should read.

        The low NVRAM_KEEP is always kept whole - it holds the two
        memory parts and everything that cannot be re-derived. The
        384 kB above it is kept only if it holds something the low
        region does not. Past the parts it reads as a uniform fill; on a
        part that aliases it repeats the low bytes; and a restore of the
        low region reproduces either, so neither needs storing. The
        moment a probe finds bytes up there that are neither uniform nor
        a copy of what sits a part-size lower, the whole megabyte is
        read instead, so an unseen larger layout is backed up whole
        rather than trimmed wrongly.

        Read-only, unlike measure_distinct: a backup does not write, so
        the aliasing is judged by content here rather than by probing.
        Content cannot tell an alias from a coincidence in general, but
        it does not have to: both an alias and a coincidence up here are
        bytes the low region already carries, and both are safe to drop.
        """
        base = NVRAM_BASE                       # not self.base - that is flash
        probe = min(0x200, PROBE_GRID)
        for off in range(NVRAM_KEEP, NVRAM_LEN, PROBE_GRID):
            if stop is not None and stop():
                raise FirmwareError("measuring was abandoned part way")
            here = self.mon.read(base + off, probe)
            if len(set(bytearray(here))) < 2:
                continue                        # empty: past the parts
            if any(off - stride >= 0
                   and self.mon.read(base + off - stride, probe) == here
                   for stride in ALIAS_STRIDES):
                continue                        # a copy of the low region
            return NVRAM_LEN                    # something distinct up here
        return NVRAM_KEEP

    def write_span(self, data, base=None, skip=0, note=None, stop=None):
        """`data` into memory from `base`, leaving the first `skip` bytes.

        Plain M writes, not the flash's erase-and-program: NVRAM is
        static RAM with a battery behind it and a byte written is a byte
        changed. Nothing has to be erased and nothing can be half
        erased, so an interrupted write leaves a mixture of old and new
        rather than a hole - which is still bad, and is why the caller
        checks afterwards.

        `skip` is how the clock is left alone. Writing a backup's
        timekeeping registers back would set the instrument's clock to
        whenever the backup was taken, and those same fourteen bytes
        carry the command and watchdog registers, which are not
        somebody's data at all. Leaving them is strictly better than
        restoring them.
        """
        base = self.base if base is None else base
        at = skip
        while at < len(data):
            if stop is not None and stop():
                raise FirmwareError(
                    "the write was abandoned %s in - what is in the "
                    "instrument now is part the backup and part what was "
                    "there before" % _size(at))
            piece = data[at:at + WCHUNK]
            self.mon.write(base + at, piece)
            at += len(piece)
            if note and at % 0x10000 < WCHUNK:
                note("Writing %s of %s" % (_size(at), _size(len(data))),
                     at / float(len(data)))
        return len(data) - skip

    # --------------------------------------------------------- programming
    def arm(self):
        """Put the helper on the instrument. None if it will not go.

        Nothing here is trusted to have worked because it was sent: the
        helper is written and read straight back, because a page loop
        running from RAM that arrived corrupt is a much worse failure than
        one that never started.
        """
        entry = (HELPER_PAGE if self.paged
                 else HELPER_WORD4 if self.lanes == 4
                 else HELPER_WORD)
        try:
            self.mon.write(SCRATCH_BASE, HELPER)
            if self.mon.read(SCRATCH_BASE, len(HELPER)) != HELPER:
                return None
        except FirmwareError:
            return None
        self.helper = entry
        return entry

    def _refuse(self):
        raise FirmwareError(
            "%s has no page buffer, and the on-instrument helper did not "
            "run. Driving it a longword at a time from here is thousands of "
            "bus turnarounds per page - days for one image - so it is not "
            "begun." % (self.name or "this flash"))

    def _page_slow(self, addr, data):
        """One page, driven from here. About seventeen turnarounds.

        Every command goes to the page's own address, which settles two
        things at once. A command reaches the device it is written to and
        no other, so an array of more than one device would otherwise take
        the data in one and the instructions about it in another. And the
        commit is not address-don't-care: the page lands where the 0x0C
        was written, so sending that to the block base - which this did -
        writes every page of a block on top of the block's first page.
        Measured on a TDS784C: a page asked for at 0x013FF000 arrived at
        0x013E0000 and the call reported success.
        """
        if not self.paged:
            self._refuse()
        base = addr
        self.mon.poke(base, _both(0x7171))          # read extended status
        self._wait(_gsr(addr), 0x0004, 0x0004, "extended status")
        self.mon.poke(base, _both(0xE0E0))          # page buffer write
        self.mon.poke(base, 0x007F007F)             # 128 longwords, low count
        self.mon.poke(base, 0x00000000)             # high count
        self.mon.write(addr, data)
        self.mon.poke(base, 0xFFFFFFFF)
        self.mon.poke(base, _both(0x7171))
        self._wait(_bsr(addr), 0x0008, 0x0000, "block status")
        self.mon.poke(base, _both(0x0C0C))          # commit the page buffer
        self.mon.poke(base, 0x007F007F)
        self.mon.poke(base, 0x00000000)
        self.mon.poke(base, _both(0x7171))
        self._wait(_bsr(addr), 0x0080, 0x0080, "block status")
        self.mon.poke(base, _both(0x5050))
        self.mon.poke(base, 0xFFFFFFFF)

    def _wait(self, addr, mask, want, what, tries=200):
        for _ in range(tries):
            if self.mon.peek(addr) & _both(mask) == _both(want):
                return
            time.sleep(0.02)
        raise FirmwareError("the flash's %s never reached 0x%04X"
                            % (what, want))

    def program(self, image, note=None, stop=None):
        """Write `image` from the base of the part.

        The helper does the page loop where it can and this drives it where
        it cannot. A page the helper refuses is retried from here, because
        one awkward page is worth seconds rather than starting the whole
        image again; a helper that refuses the very first page is dropped
        for the rest of the run, because it is not going to start working.

        A page the image leaves erased is not written at all. Erase has
        just left the whole part that way, so writing 0xFF over 0xFF is
        a bus round trip that changes nothing - and on the A series that
        is two pages in five. The verify afterwards still reads those
        pages and still compares them, so a block that did not erase is
        caught exactly as it was before.

        Returns (slow, blank): pages that had to be done the slow way,
        and pages that needed nothing doing.
        """
        if not self.paged and self.helper is None:
            self._refuse()                          # before anything is sent
        pages = (len(image) + PAGE - 1) // PAGE
        fell_back, untouched = 0, 0
        for i in range(pages):
            if stop is not None and stop():
                raise FirmwareError(
                    "programming abandoned after %d of %d pages - the flash "
                    "is part written and has to be programmed again"
                    % (i, pages))
            data = image[i * PAGE:(i + 1) * PAGE]
            data += b"\xFF" * (PAGE - len(data))
            if data == BLANK:
                untouched += 1
                continue
            addr = self.base + i * PAGE
            done = False
            if self.helper is not None:
                done = self.mon.branch(self.helper, addr, data)
                if not done and i == 0:
                    self.helper = None
            if not done:
                self._page_slow(addr, data)
                fell_back += 1
            if note and i % 64 == 0:
                note("Writing %s of %s" % (_size(i * PAGE), _size(len(image))),
                     i / float(pages))
        return fell_back, untouched

    def verify(self, image, note=None, stop=None):
        """Where the part differs from `image`. Empty is a clean write."""
        faults = []
        for at in range(0, len(image), CHUNK):
            if stop is not None and stop():
                raise FirmwareError("verify abandoned part way through")
            want = image[at:at + CHUNK]
            got = self.mon.read(self.base + at, len(want))
            if got != want:
                faults.append(at + _first_difference(got, want))
                if len(faults) >= 16:
                    break
            if note and at % 0x10000 == 0:
                note("Checking %s of %s" % (_size(at), _size(len(image))),
                     at / float(len(image)))
        return faults


def _gsr(addr):
    """The part's global status register, at the base of the device."""
    return (addr & ~0x1FFFFF) + 8


def _bsr(addr):
    """The block status register, at the base of the block."""
    return (addr & ~0x1FFFF) + 4


def _first_difference(got, want):
    for i, (a, b) in enumerate(zip(bytearray(got), bytearray(want))):
        if a != b:
            return i
    return min(len(got), len(want))


def _size(n):
    if n >= 1048576:
        return "%.2f MB" % (n / 1048576.0)
    return "%d kB" % (n // 1024)


#: The DS1486's timekeeping registers, which are the first fourteen
#: bytes of that part - hundredths, seconds, minutes, hours, day, date,
#: month, year, the two alarm registers, the command register and the
#: two watchdog registers. Straight out of its datasheet: "The
#: timekeeping registers are located in the first 14 bytes of memory
#: space... Registers E through 1FFFF are user bytes."
#:
#: On a TDS680B that part is at the base of the NVRAM window, so these
#: are the only bytes in a backup that are expected to change between
#: two reads a moment apart. Confirmed by reading them: they decoded to
#: the date and time on the bench, to the second. What sits above it is
#: a DS1250Y or a DS1650 - 512 kB either way, pin for pin, and neither
#: keeps time - so the clock is at the base wherever it is found.
RTC_LEN = 14

#: Sections of NVRAM the instrument's own firmware checksums, by the
#: firmware they belong to. The check word is the big-endian 16-bit word
#: immediately BEFORE each section, and it is the sum of every
#: big-endian word in the section, masked to sixteen bits - the same
#: shape as the calibration constants, where word 0 is the sum of the
#: 251 after it.
#:
#: **Read out of the firmware images, not copied from a table.** The
#: librarian keeps a descriptor per block, and at +0x10 and +0x14 it
#: holds the block's NVRAM address and its size, with the size repeated
#: at +0x18. That doubled size beside an 0x0400xxxx address is a
#: signature no relocation moves, so scratch/_nvgen.py finds them in an
#: image without needing to know where it loads. Every one of the 31
#: images in the collection gives a table; fifteen are distinct.
#:
#: Checked before it was believed. Where TDSNvrCV_2_1 - the verifier
#: that ships with the tdsNvramFloppyTool scripts - has a prototype for
#: the same firmware, the two agree exactly: v3.8.7e six for six,
#: v4.2e/v4.4.1e, v5.2e and v7.4e all five for five. And against the
#: dumps taken here: a TDS784D and a TDS714L both 5/5 on the v6.3e-v8.0e
#: table (right, since a 714L runs the 784D's firmware), a TDS680B 5/5
#: on v4.2e/v4.4.1e, and a **TDS640A 4/6 on v3.8.5e/v3.8.8e - its own
#: firmware, which no borrowed table had at all.**
#:
#: Two things it does not have. `PFCal` has a descriptor of another
#: shape and is missing here, so a v7.4e dump checks five sections
#: where the borrowed table checked six. And two entries were dropped
#: as not being blocks: 0x3F80/16256 and 0x10040/65536, an order of
#: magnitude larger than any named block, identical across unrelated
#: generations, and failing on every real dump from an instrument
#: running that firmware.
#:
#: The v1.x and v2.x rows are **unverified** - no such instrument has
#: been on this bench - and their addresses sit far higher in the
#: window than every later generation's.
NVRAM_PROTOS = (
    ('v1.0e v1.1e',
     ((0x806, 50), (0x1006, 20), (0x1038, 300), (0x1420, 1806),
      (0x1BF0, 2016))),
    ('v2.03e',
     ((0x18F9E, 278), (0x19192, 784), (0x195DE, 1084), (0x1CC8E, 20),
      (0x1FDD0, 36), (0x1FE02, 144))),
    ('v2.04e',
     ((0x19A8E, 278), (0x19C82, 1302), (0x1A25E, 868), (0x1D13E, 20),
      (0x1FDD0, 36), (0x1FE02, 354))),
    ('v2.16e 77619de1',
     ((0x18F9E, 278), (0x19192, 1376), (0x1976E, 868), (0x1C64E, 20),
      (0x1FDD0, 36), (0x1FE02, 442))),
    ('v2.16e d82e9f9e',
     ((0x18F9E, 278), (0x19192, 888), (0x1976E, 864), (0x1C64E, 20),
      (0x1FDD0, 36), (0x1FE02, 250))),
    ('v3.8.2e 3a92ae13',
     ((0x806, 50), (0x8CE, 354), (0x1006, 20), (0x1038, 300),
      (0x1420, 1310), (0x19FC, 1790))),
    ('v3.8.2e v3.8.7e',
     ((0x806, 50), (0x8CE, 250), (0x1006, 20), (0x1038, 300),
      (0x1420, 896), (0x19FC, 1814))),
    ('v3.8.3e v3.8.4e',
     ((0x806, 50), (0x8CE, 442), (0x1006, 20), (0x1038, 300),
      (0x1420, 1384), (0x19FC, 1818))),
    ('v3.8.5e v3.8.8e',
     ((0x806, 50), (0x8CE, 354), (0x1006, 20), (0x1038, 300),
      (0x1420, 1310), (0x19FC, 1792))),
    ('v4.0e v4.1e v4.2.1e',
     ((0x806, 50), (0x1006, 20), (0x1038, 300), (0x1420, 1806),
      (0x1BF0, 2494))),
    ('v4.2e v4.4.1e',
     ((0x806, 50), (0x1006, 20), (0x1038, 300), (0x1420, 1490),
      (0x19FC, 2494))),
    ('v5.0e v5.1.1e v5.2e v5.3e',
     ((0x806, 50), (0x2806, 20), (0x2838, 300), (0x2C20, 1806),
      (0x33F0, 4580))),
    ('v5.1e',
     ((0x806, 50), (0x2806, 20), (0x2838, 300), (0x2C20, 1588),
      (0x3260, 2494))),
    ('v6.0e',
     ((0x806, 50), (0x2806, 20), (0x2838, 300), (0x2C20, 3818),
      (0x3DB4, 4580))),
    ('v6.3e v6.4e v6.6e v7.1.1e v7.2e v7.4e v8.0e',
     ((0x806, 50), (0x2806, 20), (0x2838, 300), (0x2C20, 8618),
      (0x5330, 4580))),
)

#: What the librarian calls each block, by where it keeps it. Read off
#: the name array beside the descriptors in a v8.0e image; the same six
#: names, at addresses that moved once between generations.
NVRAM_BLOCKS = {
    0x0806: "Hardware accountant", 0x08CE: "Ext constants",
    0x1006: "Diagnostics", 0x2806: "Diagnostics",
    0x1038: "Environment", 0x2838: "Environment",
    0x1420: "Int constants", 0x2C20: "Int constants",
    0x19FC: "State", 0x1BF0: "State", 0x3260: "State",
    0x33F0: "State", 0x3DB4: "State", 0x5330: "State",
}


#: Part sizes worth testing for an alias, largest first. NVRAM parts
#: come in powers of two and the window is a megabyte, so a part smaller
#: than the window answers at every multiple of its own size.
ALIAS_STRIDES = (0x80000, 0x40000, 0x20000)
#: How coarsely the boundary between parts is searched for. The smallest
#: part in these instruments is 128 kB, so a boundary is a multiple of
#: that and looking any finer is looking for something that cannot be
#: there. Eight probes cover the whole window.
PROBE_GRID = 0x20000
#: Two of them, and neither is 0x00 or 0xFF. One proves nothing where
#: the two addresses already hold the same bytes, which blank memory
#: does everywhere; the second is what tells a mirror from a
#: coincidence.
PROBES = (b"\x5A\xA5\x5A\xA5", b"\xA5\x5A\xA5\x5A")
#: Below this a repeat found in a *file* is coincidence rather than a
#: part answering twice. The smallest real one seen is 384 kB.
MIN_ALIAS = 0x10000


def _mirrors_from(blob, stride):
    """The address `blob` repeats from at `stride`, or None."""
    top, low = blob[stride:], blob[:len(blob) - stride]
    # The longest tail the two share, by halving rather than by walking
    # back a byte at a time: a megabyte compared byte by byte in Python
    # is a fifth of a second, and twenty slice compares is nothing.
    # Sharing the last k bytes implies sharing the last k-1, so the
    # answer can be bisected for.
    lo, hi = 0, len(top)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if top[-mid:] == low[-mid:]:
            lo = mid
        else:
            hi = mid - 1
    at = len(blob) - lo
    tail = blob[at:]
    # A uniform stretch repeats under any stride, which is the same
    # false match nvram_check refuses for an all-zero section: a dump
    # whose top is nothing but zeros would otherwise be read as a part
    # answering twice and refused as coming from another instrument.
    if len(tail) < MIN_ALIAS or len(set(bytearray(tail))) < 2:
        return None
    return at


def nvram_distinct(blob):
    """Where a dump repeats itself, or its length if it does not.

    **This reads a file, not an instrument.** It is corroboration, not
    measurement: a dump whose repeat sits somewhere the instrument's
    does not came off a differently laid out instrument, which is worth
    refusing. What a restore actually trims to is
    `Flash.measure_distinct`, because content cannot answer this where
    the memory is blank - and a wiped NVRAM is exactly what somebody
    reaching for Restore is holding.
    """
    for stride in ALIAS_STRIDES:
        if stride * 2 > len(blob):
            continue
        at = _mirrors_from(blob, stride)
        if at is not None:
            return at
    return len(blob)


def _word(blob, at):
    return (blob[at] << 8) | blob[at + 1]


def nvram_sections(blob, sections):
    """[(what, address, wanted, computed)] for one firmware's sections."""
    out = []
    for at, size in sections:
        if at < 2 or at + size > len(blob):
            return []                   # the file is too short to be this
        got = sum(_word(blob, at + 2 * i) for i in range(size // 2)) & 0xFFFF
        out.append((NVRAM_BLOCKS.get(at, "Block"), at,
                    _word(blob, at - 2), got))
    return out


def nvram_check(blob):
    """Which firmware this dump is from, and whether its checksums add up.

    Answers the best match rather than the first, because generations
    share the small sections and only the right one agrees everywhere.

    **A section that is all zero sums to zero and matches a check word
    of zero whatever it holds**, so it is not evidence and is not
    counted when choosing. Learned the hard way: a TDS640A dump was
    picking a TDS784C-era table on five all-zero agreements while its
    own firmware's table sat there matching four real ones.
    """
    blob = bytearray(blob)
    best, rows = None, []
    for proto, sections in NVRAM_PROTOS:
        got = nvram_sections(blob, sections)
        good = [r for r in got if r[2] == r[3]]
        # Ranked on the sections that say something, then on all of
        # them, then on the shorter table - a table that fits by
        # having fewer things to be wrong about has not won anything.
        key = (len([r for r in good if r[2]]), len(good), -len(got))
        if got and (best is None or key > best[0]):
            best, rows = (key, proto, len(good), len(got)), got
    if best is None:
        return {"proto": None, "sections": [], "bad": [], "zero": []}
    _key, proto, good, all_of = best
    # A dump of nothing but zeros agrees with every table on every
    # section, because a zero sum matches a zero check word. That is
    # arithmetic, not a match, and calling it one would wave a wiped
    # NVRAM through as a good backup - which is the exact file somebody
    # is most likely to be holding when they reach for Restore.
    told = [r for r in rows if r[2] == r[3] and r[2]]
    return {"proto": proto if good == all_of and told else None,
            "nearest": proto if told else None,
            "matched": len(told), "of": all_of,
            "bad": [r for r in rows if r[2] != r[3]],
            "zero": [r for r in rows if r[2] == r[3] and not r[2]]}


# ----------------------------------------------------------- the catalogue
#: Two shapes of filename. The first carries a model, which is how
#: these arrive from Tektronix and how a collection that has never been
#: tidied still reads. The second deliberately does not: one image
#: serves a whole family, so naming it after one member of that family
#: says something untrue about the other four, and which instruments it
#: is for belongs in the index instead. The eight hex digits are the
#: start of the image's SHA-256, and they are there because a version
#: alone is not unique - v2.16e is one image for a TDS520 and a
#: different one for a TDS540.
NAMES = (
    re.compile(r"^(TDS\d+[A-Z]?)_v([0-9][0-9.]*[a-z]?)_Firmware\.bin$",
               re.IGNORECASE),
    re.compile(r"^TDS()_v([0-9][0-9.]*[a-z]?)_[0-9a-f]{8}\.bin$",
               re.IGNORECASE),
)


def named(leaf):
    """(model, version) out of a filename. Either may be empty."""
    for one in NAMES:
        got = one.match(leaf)
        if got:
            return got.group(1).upper(), got.group(2).lower()
    return "", ""


#: The version the image says it is, which is not always what it is
#: called. The `v` is required: every image from the C series on also
#: carries "FV:3.8eSparc10.atria1", which is the SPARCstation and
#: ClearCase the firmware was built on rather than anything about the
#: firmware, and that one has no `v`.
FV = re.compile(rb"FV:v([0-9][0-9.]*[a-z]?)")

#: Tektronix shipped one binary for a whole family, so a folder holding
#: every copy records which instruments an image suits in the filenames
#: themselves. A folder tidied down to one file per binary has thrown
#: that away, and an index beside the images is where it goes instead -
#: the rows of the FITS table, which this reads when it is there.
INDEX_NAME = "FIRMWARE-INDEX.txt"
#: The filename, the version, the size, then the models, which are the
#: last thing on the line. What sits between the size and them is
#: skipped rather than refused, so a column can be added - a Tektronix
#: part number, if one is ever found; they are not in the images - and
#: this goes on reading the file.
INDEX_ROW = re.compile(
    r"^(\S+\.bin)\s+v\S+\s+[\d.]+\s*[kM]B\s+.*?(TDS\S+(?:\s+TDS\S+)*)$",
    re.IGNORECASE)


def index_models(folder):
    """{filename: [model, ...]}, from the folder's index or the shipped one.

    A folder with an index of its own is believed over the shipped one,
    because it is the one describing the files that are actually there.
    The shipped copy beside this module is the fallback and the place
    the knowledge accumulates: an image nobody here has seen is added to
    it, not discovered again by every user.
    """
    out = {}
    # Bundled into the executable the index is unpacked to _MEIPASS,
    # which is not where this file appears to live; from source the two
    # are the same place. Both are looked at, then the folder, and the
    # last one read wins.
    here = [d for d in (getattr(sys, "_MEIPASS", None),
                        os.path.dirname(os.path.abspath(__file__))) if d]

    def take(text):
        for line in text.splitlines():
            got = INDEX_ROW.match(line.strip())
            if got:
                out[got.group(1).lower()] = sorted(
                    m.upper() for m in got.group(2).split())

    for path in [os.path.join(d, INDEX_NAME) for d in here]:
        try:
            with open(path, encoding="utf-8") as fh:
                take(fh.read())
        except OSError:
            pass
    # An archive carries its own index, which is the only one a folder
    # holding nothing but the archive has.
    for leaf in archives(folder):
        try:
            with zipfile.ZipFile(leaf) as zf:
                take(zf.read(INDEX_NAME).decode("utf-8", "replace"))
        except (OSError, KeyError, zipfile.BadZipFile):
            pass
    try:
        with open(os.path.join(folder or "", INDEX_NAME),
                  encoding="utf-8") as fh:
            take(fh.read())
    except OSError:
        pass
    return out


def archives(folder):
    """The zip files in `folder`, if it is a folder at all."""
    try:
        return [os.path.join(folder, n) for n in sorted(os.listdir(folder))
                if n.lower().endswith(".zip")]
    except (OSError, TypeError):
        return []


def read_image(path, archive=""):
    """The bytes of an image, loose on disk or inside an archive."""
    if archive:
        with zipfile.ZipFile(archive) as zf:
            return zf.read(os.path.basename(path))
    with open(path, "rb") as fh:
        return fh.read()


def version_key(text):
    """Sortable form of "5.1.1e", so 5.10e sorts above 5.2e."""
    parts = re.findall(r"\d+", text)
    return tuple(int(p) for p in parts)


def fv_strings(blob):
    """Every version the image claims for itself."""
    return sorted({m.group(1).decode("ascii", "replace")
                   for m in FV.finditer(blob)})


class Image(object):
    """One firmware binary, and every name it is shipped under.

    Tektronix shipped one binary for a whole family: of 67 files in the
    folder here, 47 are copies of another under a different model's name.
    So which instruments an image suits is not guessed from strings inside
    it - the strings disagree with the filenames in both directions - it is
    read off which filenames carry these exact bytes.
    """

    def __init__(self, path, blob, archive=""):
        #: The zip this came out of, or "" for a file on disk. A member
        #: has had its erased tail removed, so it is shorter than the
        #: image was when it was dumped - which changes nothing, since
        #: erase puts those bytes back before anything is written.
        self.archive = archive
        self.paths = [path]
        self.size = len(blob)
        self.sha = hashlib.sha256(blob).hexdigest()
        self.head = blob[:4]
        self.fv = fv_strings(blob)
        model, self.version = named(os.path.basename(path))
        self.models = [model] if model else []

    def add(self, path):
        self.paths.append(path)
        model, version = named(os.path.basename(path))
        self.version = self.version or version
        if model and model not in self.models:
            self.models.append(model)
            self.models.sort()

    @property
    def path(self):
        return self.paths[0]

    def read(self):
        """The image itself."""
        return read_image(self.path, self.archive)

    @property
    def mislabelled(self):
        """The version inside disagreeing with the version on the tin.

        Three of the sixty-seven do. The file is not necessarily wrong -
        the string inside can be stale - but a version that has to be
        right is worth saying out loud rather than quietly believing.
        """
        if not self.fv or not self.version:
            return ""
        if self.version.rstrip("e") in [f.rstrip("e") for f in self.fv]:
            return ""
        return ", ".join(self.fv)

    def suits(self, model):
        return bool(model) and model.upper().replace(" ", "") in self.models

    def label(self, model=""):
        """One line for the list, said from `model`'s point of view."""
        text = "v%s   %s" % (self.version, _size(self.size))
        if not self.models:
            # A name that carries no model and no index row to go with
            # it. Saying "for nothing" would be worse than saying
            # nothing.
            return text
        if not self.suits(model):
            return "%s   for %s" % (text, ", ".join(self.models))
        others = [m for m in self.models
                  if m != model.upper().replace(" ", "")]
        return text + ("   also %s" % ", ".join(others) if others else "")


def catalogue(folder, note=None):
    """Every distinct image in `folder`, newest version first.

    Copies collapse into one entry carrying every model name they were
    shipped under.
    """
    if not folder or not os.path.isdir(folder):
        return []
    told = index_models(folder)
    found, seen = {}, set()
    for i, (name, archive, blob, total) in enumerate(_every(folder)):
        if note:
            note("Reading %s (%d of %d)" % (name, i + 1, total),
                 (i + 1) / float(total))
        # A folder holding both the loose images and an archive of them
        # lists each image once. The loose file is taken because it is
        # the image as it was dumped; the archived one has had its
        # erased tail removed and so does not hash the same.
        if name.lower() in seen or not blob.startswith(IMAGE_HEAD):
            continue
        seen.add(name.lower())
        path = name if archive else os.path.join(folder, name)
        one = Image(path, blob, archive)
        if one.sha in found:
            found[one.sha].add(path)
        else:
            found[one.sha] = one
    out = list(found.values())
    for one in out:
        named = told.get(os.path.basename(one.path).lower())
        if named:
            one.models = named
    out.sort(key=lambda im: (version_key(im.version), im.models[:1]),
             reverse=True)
    return out


def _every(folder):
    """(name, archive, bytes, how many there are) for every image there.

    Loose files first, then the members of any archive, so a name that
    is in both is taken from the folder rather than from the zip.
    """
    loose = sorted(n for n in os.listdir(folder)
                   if n.lower().endswith(".bin"))
    zips = []
    for path in archives(folder):
        try:
            with zipfile.ZipFile(path) as zf:
                zips += [(path, n) for n in sorted(zf.namelist())
                         if n.lower().endswith(".bin")]
        except (OSError, zipfile.BadZipFile):
            pass
    total = max(len(loose) + len(zips), 1)
    for name in loose:
        try:
            with open(os.path.join(folder, name), "rb") as fh:
                yield name, "", fh.read(), total
        except OSError:
            continue
    for path, name in zips:
        try:
            with zipfile.ZipFile(path) as zf:
                yield name, path, zf.read(name), total
        except (OSError, KeyError, zipfile.BadZipFile):
            continue


def identify(blob, images):
    """Which catalogued image a blob read off an instrument is, if any."""
    sha = hashlib.sha256(blob).hexdigest()
    for one in images:
        if one.sha == sha:
            return one
    return None
