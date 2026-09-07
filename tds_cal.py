# -*- coding: utf-8 -*-
"""The acquisition board's calibration constants, over the ordinary bus.

These live in two small EEPROMs on the acquisition board - boards mark
them U1052 and U1055, or U1055 and U1056 - and the firmware reaches them
through a librarian it exposes as a service command. Nothing here wants
the ROM monitor or the NVRAM protection switch: the instrument is
running normally throughout, which is why this is on the System tab and
not the Firmware one.

The command and the file layout are the ones ragges/tektools' getcaldata
uses, so a backup taken here is the same 512 bytes that tool writes and
answers to the same checksum verifier. No code is shared with it - that
tool is GPL v2 and this is MIT - and none needs to be: a command an
instrument answers is a fact about the instrument.

    WORDCONSTANT:ATOFFSET? <base>,<n>       read word base+n
    WORDCONSTANT:ATPUT <base+n>, <v>        write it

The second is what the factory options are written with, and it needs
the NVRAM protection switch set to unprotected. With it protected the
instrument takes the command and changes nothing, which is why nothing
here trusts a write it has not read back.

The file is what the two EEPROMs concatenate to: eight bytes of padding
in front of the part of the first one this can reach, then its 124
words, then the second one's 128. Each word is stored high byte first,
which is the order getcaldata writes.
"""
import re
import struct

#: Where the librarian keeps them. Right for the TDS 500B, 600A and 700A
#: families, which is as far as the reference tool claims. Every other
#: generation is an assumption until an instrument says otherwise, so it
#: is a parameter rather than a constant everywhere it is used.
BASE = 0x40000

#: The service unlock, sent only if reading without it is refused.
#:
#: It is not in any firmware image here as plain text, and the reason is
#: now known rather than a puzzle: `PASSWORD` is implemented in the boot
#: ROM, which is a separate 256 kB part and is not in the 4 MB flash
#: these images are dumps of. So no amount of searching them will ever
#: find it. A live TDS754D answers `PASSWORD` with error 102, not 113 -
#: wrong argument, not unknown header - so the command is real and the
#: protected set behind it is real.
UNLOCK = "PASSWORD PITBULL"

WORDS = 252
#: Where the first EEPROM's words end and the second's begin.
SPLIT = 124
#: Bytes at the front of the file that are not readable through the
#: librarian. getcaldata writes zeros there and so does this.
PAD = 8
SIZE = PAD + WORDS * 2

NUMBER = re.compile(r"-?\d+")


class CalError(Exception):
    """The instrument would not answer for its calibration constants."""


def _value(said, n):
    # The last number, not the first. With HEADER OFF the reply is the
    # value and nothing else; with HEADER ON it is
    # ":WORDC:ATOFFSET <base>,<n>,<value>", where the first number is
    # the base - so taking the first hands back the address masked to
    # sixteen bits, silently, and every word reads as garbage that
    # looks like data. Measured on a TDS 754D. The program sets HEADER
    # OFF at connect, but nothing here should depend on that.
    got = NUMBER.findall(said or "")
    if not got:
        raise CalError("word %d answered %r, which is not a number"
                       % (n, (said or "")[:40]))
    return int(got[-1]) & 0xFFFF


def read(ask, base=BASE, note=None, stop=None):
    """Every calibration word, as the bytes a saved file holds.

    `ask` sends one query and hands back the reply. The first word is
    asked for before anything is unlocked: if the instrument answers,
    the service password is not needed and is not sent.
    """
    out = bytearray(b"\x00" * PAD)
    for n in range(WORDS):
        if stop is not None and stop():
            raise CalError("reading the calibration constants was "
                           "abandoned part way through")
        out += struct.pack(">H", word(ask, n, base))
        if note is not None and not n % 36:
            note("Reading the calibration constants - word %d of %d"
                 % (n + 1, WORDS),
                 n / float(WORDS))
    return bytes(out)


def word(ask, n, base=BASE):
    """One word, by number."""
    return _value(ask("WORDCONSTANT:ATOFFSET? %d,%d" % (base, n)), n)


def words(blob):
    """The words a saved file holds, in the order they were read."""
    if len(blob) != SIZE:
        raise CalError("a calibration backup is %d bytes; this one is %d"
                       % (SIZE, len(blob)))
    return [struct.unpack_from(">H", blob, PAD + n * 2)[0]
            for n in range(WORDS)]


def setting(n, value, base=BASE):
    """The command that writes one word. Nothing here sends it.

    The set form of the query, taking the same base and offset. A
    TDS640A on v3.8.8e also accepts WORDCONSTANT:ATPUT, which is what
    the factory options use and which takes one absolute word number -
    but the words are not at base+n in memory (on that instrument they
    are at NVRAM 0x8CC), so what ATPUT's numbering addresses is not
    established. This form's addressing is not in doubt: it is the
    query's, and a word written with it was read back changed and then
    read back restored.
    """
    return "WORDCONSTANT:ATOFFSET %d,%d,%d" % (base, n, value & 0xFFFF)


def names(model, stamp):
    """What a pair of backups is called, and the combined file."""
    return ("%s_U1052_%s.bin" % (model, stamp),
            "%s_U1055_%s.bin" % (model, stamp),
            "%s_CAL_%s.bin" % (model, stamp))


def checksum(blob):
    """What word 0 should be: the 16-bit sum of the 251 words after it.

    The whole block, not one EEPROM: the two halves split at SPLIT for
    the hardware's sake and the checksum does not know about it.
    Measured on a TDS 680B, a TDS 714L, a TDS 784C and a TDS 784D -
    every instrument whose constants have been read - and on all four
    it is the sum across both parts. Per-half readings were tried on
    the 784D and none of them fits.
    """
    held = words(blob) if isinstance(blob, (bytes, bytearray)) else list(blob)
    return sum(held[1:]) & 0xFFFF


def sound(blob):
    """Does this block agree with the checksum it carries?

    A far better question than looks_empty's, which only ever says the
    address might be wrong. This one says whether the data is the data
    that was written. They answer different things and both are asked.
    """
    held = words(blob) if isinstance(blob, (bytes, bytearray)) else list(blob)
    return held[0] == checksum(held)


def looks_empty(blob):
    """Nothing but one repeated byte, which is not calibration data.

    A base that is wrong for this generation reads somewhere that is not
    the librarian, and the commonest thing to find there is a block of
    one value. It is not proof either way - it is a reason to look.
    """
    body = blob[PAD:]
    return not body or body == body[:1] * len(body)
