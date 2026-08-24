# Notes on the instrument

Behaviour of the TDS500/600/700 filesystem over GPIB, measured on a **TDS
784D running firmware v7.4e**. All of it was found by probing rather than
from documentation, and each item changed the code in
[TDS Toolkit](README.md).

Where a number appears here it was measured, not estimated.

---

## Phantom files ending in a dot

A card imaged from Windows carries VFAT long-filename records. The
instrument's FAT16 driver predates VFAT, so it reads each one as an ordinary
8.3 entry and reports it as a file:

```
"DPODEMO1.APP","AD.","DPODEMO1.JAR","AT.","TDS.JAR","AT.","TEK.JAR","At.","TEMP"
```

`AD.`, `AT.` and `At.` are not files. They are long-name records being read as
though they were 8.3 entries:

* byte 0 is the sequence number, `0x41` for a single-record name, read as `A`
* byte 1 is the first character of the long name — `D` for `DPODEMO1.JAR`,
  `T` for `TDS.JAR`
* byte 2 is the high half of that UTF-16 character and is zero, ending the
  string

Hence two characters and a bare trailing dot, one per long name. `At.` ahead
of `TEMP` tells you that folder is really `temp` in lowercase on the card.

A trailing dot is a safe test for them: FAT cannot store a name with an empty
extension. They are filtered out before the UI sees them, and refused as
delete targets — deleting one would strip the long name from the real file
that follows it in the directory table.

Filtering them before classification rather than after also saves two or
three round trips each, which is most of the wait when opening an application
folder.

## "Mass storage error" after deleting a folder

The instrument sometimes displays **Mass storage error** on its own screen and
raises event 250 after a folder delete. It does not mean the delete failed.

The obvious suspicion is that a follow-up command is colliding with a delete
still in progress. It is not. With the bus left completely silent for five
seconds after each RMDIR — no `*OPC?`, no event drain, nothing at all:

```
event 250 raised    4 of 12 trials
folder removed     12 of 12 trials
card contents      unchanged throughout
```

The firmware raises it unprompted during its own recursive delete. The right
response is to record it and carry on, and to confirm the delete by
re-listing the parent rather than by trusting a quiet event queue.

Worth attention, by contrast: event 250 *outside* a delete, a delete that
fails its verification, or a read-back mismatch on upload.

## There is no way to get a file's size

Short of transferring the whole file. `FILESYSTEM:DIR?` returns names only,
and `READFILE` sends a bare byte stream with no `#8<length>` block header to
peek at. Every plausible size query answers with event 113, undefined header:

```
FILESYSTEM?                 -> "hd0:";0;1        (cwd and flags, no sizes)
FILESYSTEM:DIR? "hd0:"      -> event 420          (DIR? takes no argument)
:SIZE?  :FILESIZE?  :LDIR?  :DIR:DETAIL?  :STAT?  :INFO?   -> event 113
HEADER ON; VERBOSE ON; FILESYSTEM:DIR?  -> the same names, nothing more
```

There is a native route, unused here because it is far too slow for browsing:
the firmware exports the VxWorks symbols `_ll`, `_stat`, `_open` and
`_ioTaskStdSet`, so a target-shell script could redirect its own task's stdout
to a file, run `ll` over a directory, and leave a listing with sizes and dates
to be read back. Per-task redirection does not disturb the console. It would
suit a deliberate "get the sizes for this folder" command, not navigation.

## The event queue will not drain without priming

`EVENT?` answers `1` forever until the Standard Event Status Register has been
read. The instrument explains itself if you ask with `EVMSG?` instead:

```
1,"No events to report - new events pending *ESR?"
```

Without the priming read, a drain loop returns `[1, 1, 1, ...]` until it hits
its own limit — which looks like twenty errors and is in fact none. Verified:
`*CLS`, an undefined header, then `EVENT?` gives `1` repeatedly; the same
sequence with `*ESR?` in front gives `113` and then `0`.

`EVMSG?` costs nothing over `EVENT?` and returns the instrument's own wording,
which is worth having in a log:

```
113  Undefined header; unrecognized command - FILESYSTEM:NOSUCHTHING
256  File name not found;  - osError
250  Mass storage error;  - osError
```

## `*OPC?` works; `BUSY?` does not

`BUSY?` exists but reflects acquisition, not the filesystem — it reads `0`
throughout a delete, and polling it *during* an RMDIR is itself enough to
provoke event 250.

`*OPC?` does not answer until the pending operation has finished. Measured
after an RMDIR of a ten-file folder it returns in 0.15 to 0.67 s, tracking the
real work. The code it replaced slept a flat 0.8 s — sometimes too short, and
usually three times longer than necessary. Waiting properly is both faster and
more correct.

RMDIR duration is not predictable: usually about 0.4 s, occasionally over
2.6 s for the same shape of folder.

## `DELETE` does nothing to a directory

Not an error, not an event, just silence and the directory still there.

`FILESYSTEM:RMDIR` is the command that works. It is **recursive** and it is
**silent**: on a TDS 784D, RMDIR on a directory containing a file removed the
directory and the file, raised no event, and gave no warning. There is no dry
run and no undo.

RMDIR is refused with event 257 if the current working directory is inside the
folder being removed — and that refusal is not harmless. It left the
filesystem subsystem returning a fixed non-ASCII pattern for every path,
unrecoverable by device clear, `*CLS`, changing directory, or waiting. The CF
card had to be reimaged. Always stand at the volume root before removing
anything.

## Volumes have to be probed

There is no "list my volumes" query. Each candidate is entered with
`FILESYSTEM:CWD` and the working directory is read back to see whether it
moved; one that does not exist leaves the cwd alone and raises event 256.

Measured on a TDS 784D: `hd0:` and `fd0:` exist. `hd1:`, `fd1:`, `ram:`,
`nvram:`, `cf0:`, `disk0:`, `tffs0:` and `usb0:` do not.

Probing rather than assuming is what makes an instrument with no hard disk
option work: on one of those, `hd0:` simply is not among the volumes that
answer, and nothing else has to know.

## Replies may arrive with the command in front of them

`HEADER` decides whether a reply is the bare value or the value with the
command that produced it in front:

```
HEADER OFF   FILESYSTEM:FREESPACE? -> 1073741823
HEADER ON    FILESYSTEM:FREESPACE? -> :FILESYSTEM:FREESPACE 1073741823
```

`VERBOSE OFF` abbreviates the header as well, so the same reply can come back
as `:FILES:FREES 1073741823`. This is instrument state, not a per-session
default: a TDS 784D was found with headers off and a TDS 640A with them on,
and either can be left in the other state by any program that touched the bus
earlier.

Turning headers off on connect is therefore not enough on its own, since a
reply already in flight or a state that fails to take would go unnoticed. The
header is stripped from every reply as well, by taking what follows the first
space when the reply begins with a colon.

## Not every TDS can transfer a file's contents at all

Measured on a **TDS 640A, firmware v3.8.8e**: there is no
`FILESYSTEM:READFILE` and no `FILESYSTEM:WRITEFILE`. Every spelling of
both — with and without the leading colon, abbreviated, with `#0` and with
a definite-length `#3nnn` block — is answered the same way:

```
113,"Undefined header; unrecognized command - FILESYSTEM:WRITEFILE"
```

The whole subsystem on that firmware is three settings and a handful of
actions:

```
:FILESYSTEM:CWD "fd0:";DELWARN 0;OVERWRITE 1
```

`DIR?`, `DELETE`, `REName`, `COPY`, `PRINT`, `MKDIR` and `RMDIR` are all
present and were confirmed to do what they say — a directory created with
`MKDIR` appeared in the listing and `RMDIR` removed it again. So the
instrument can be browsed and tidied over GPIB; what it cannot do is hand
a file's bytes to the controller with `READFILE`.

### Asking whether a command exists, without doing anything with it

Send the command header on its own, with no arguments, and read the event
queue. A firmware that has the command objects to the missing argument; a
firmware that does not have it objects to the header:

```
FILESYSTEM:PRINT       ->  100 "Command error; command not allowed"    present
FILESYSTEM:WRITEFILE   ->  113 "Undefined header; unrecognized command"  absent
```

Nothing is created, deleted or overwritten either way, which makes this
the only safe way to ask about a destructive command.

### `FILESYSTEM:PRINT <file>,GPIB` puts a file on the bus

The command that prints a file to a port accepts `GPIB` as that port, and
what arrives is the file itself, not a rendering of it. Verified against a
bitmap's own geometry rather than a reference copy, since this instrument
has no way to report a file's size: a 480 x 640 one-bit BMP must occupy
`62 + 60 x 640 = 38462` bytes, and exactly 38462 bytes arrived.

Note that the size field in the BMP header — bytes 2 to 5 — is **not**
filled in correctly by the instrument. It read 975 on a 38462-byte file.
Anything checking these files must use the geometry, not that field.

About 3.2 KB/s, an order slower than `READFILE` on a 784D.

### Read when it says it is ready, not after a guess

`PRINT` is a command, not a query, so there is no response to wait on in
the usual way. Sleeping a fixed time and then reading blind fails whenever
the guess is short: the instrument is addressed to talk before it has
anything to say, raises 420 "query unterminated", and **throws the print
output away**. Measured on a 206-byte file with a 0.6 s sleep: 11 of 20.

A serial poll costs nothing and answers the question properly. Bit 4 of
the status byte is MAV, "message available", and a serial poll reads it
without going through the output queue, so it can be asked repeatedly
while the instrument works. Same file, same instrument, polling for MAV
instead of sleeping: **20 of 20**.

### It swallows the command after a large transfer

Fire prints back to back with nothing in between and the one following a
successful *large* transfer is accepted, raises no event, and produces
nothing. The attempt after that works. The rule held without exception
over 14 transfers: small files in a row never fail; anything after a big
one does.

Nothing clears it deliberately. `*OPC?`, polling `BUSY?` to idle,
`*CLS`, `HARDCOPY ABORT`, a device clear, a ten-second wait and twenty
serial polls were each measured, and none of them helped. What does work
is ordinary traffic — a `CWD` and a `DIR?` between transfers — which is
why the program never sees this: it lists a directory before every read
anyway. Ten consecutive transfers through the program's own code path
succeeded, byte-identical, with no retry needed.

### A long run of failed prints takes mass storage down

This is the one to be careful of. After nine swallowed prints in a row the
floppy stopped answering altogether:

```
DIR?        (empty)
FREESPACE?  0
events      250,"Mass storage error"
```

Ejecting and re-inserting the disk did not clear it; a power cycle did,
with no damage to the disk. A handful of scattered failures is survivable
— two earlier runs had failures and the drive stayed healthy — but a
sustained run of them is not.

Hence the retry limit of exactly one. Retrying is worth it, because a
single swallowed attempt is common and the next one always works; retrying
harder than that risks costing the user a power cycle to recover from.

(An earlier version of this note blamed the GPIB device clear for the
wedge. That was wrong: a later run with no device clear anywhere wedged it
just the same, and the common factor was the run of failures.)

## Transfer rates

The medium decides, not the instrument:

| | measured |
|---|---|
| hard disk, `READFILE` (TDS 784D) | about 33 KB/s |
| floppy, `READFILE` (TDS 784C, 789,504 bytes in 200 s) | 3.9 KB/s |
| floppy, `PRINT ,GPIB` (TDS 640A, 38,462 bytes in 12 s) | 3.2 KB/s |

So a floppy is roughly eight times slower than a hard disk, and quoting
the hard-disk figure for a floppy turns "about a minute" into ten, which
looks like a hang.

An upload is written and then read back to verify, so the payload crosses
the bus twice; the program's time estimates are based on that, at whichever
rate the destination volume runs at.

Uploads cannot report byte-level progress. The payload goes out as a single
transfer because EOI on the last data byte is what terminates the
indefinite-length `#0` block; splitting it would truncate the file.


## Waveforms are a different subsystem, and a far more capable one

Every one of the 34 firmware images has `CURVe`, `WFMPre`, `WAVFrm`,
`DATa:SOURce`, `DATa:DESTination` and `REF1` - including the v2.x ones
with no filesystem at all. So a TDS 520 that can do nothing with files can
still send and receive waveforms.

It is also an order faster, because nothing touches a disk:

| | |
|---|---|
| TDS 784D, 5000 points | 0.02 s, 227 KB/s |
| TDS 640A, 500 points | 0.04 s, 12 KB/s |

### Reading

```
DATA:SOURCE <src>
DATA:ENCDG RIBINARY ; DATA:WIDTH 1
DATA:START 1 ; DATA:STOP <record length>
WFMPRE:<src>:NR_PT?  PT_FMT?  XINCR?  XZERO?  PT_OFF?  XUNIT?
                     YMULT?  YZERO?  YOFF?  YUNIT?  WFID?
CURVE?                     -> #<digits><length><bytes>

volts   = (raw - YOFF) * YMULT + YZERO
seconds = (n - PT_OFF) * XINCR + XZERO
```

Three things that are not obvious and cost an afternoon each:

**The source prefix is not optional.** A bare `WFMPRE:NR_PT?` is answered
with 100, "command error; query not allowed". It must be
`WFMPRE:CH1:NR_PT?`, which is how Tektronix's own `GETWFM.C` writes it.

**The bulk `WFMPRE:<src>?` reply is not in the same field order on every
instrument.** A TDS 784D and a TDS 640A disagree, and mapping one onto the
other put a unit string where a sample count belonged - which then sailed
through as `WFMPRE:PT_OFF "Volts"`. One query is tempting; eleven
individual ones are correct. Not every field exists everywhere either: a
640A has no per-source `XZERO`.

**Only displayed sources can be read.** Asking for a switched-off channel
raises 2241, "waveform requested is invalid". `SELECT?` reports which are
on — but read it with `HEADER ON`, so the reply names its own fields:

```
HEADER OFF   1;0;0;0;0;0;0;0;0;0;1;REF4
HEADER ON    :SELECT:CH1 1;CH2 0;CH3 0;CH4 0;MATH1 0;MATH2 0;MATH3 0;
             REF1 0;REF2 0;REF3 0;REF4 1;CONTROL REF4
```

Reading the bare form by position means assuming how many channels the
instrument has, and a two-channel scope would then have every name after
the missing one shifted — a MATH reported as a CH4. The firmware cannot
settle it either: one image serves both the two-channel and four-channel
model of a family, so `f05bc6bc49` is the TDS 520B *and* the TDS 540B and
carries all eleven keywords regardless. Let the instrument say.

`CONTROL` is not a source; it names which one the front-panel knobs are
attached to.

A TDS 640A answers without the `:SELECT:` prefix while a 784C and 784D
include it, so parse the names loosely.

### Writing into a reference

The same in reverse, with one addition that is the whole difficulty:

```
SAVE:WAVEFORM <live source>,<REF>      only if the REF does not exist yet
DATA:DESTINATION <REF>
DATA:ENCDG RIBINARY ; DATA:WIDTH 1 ; DATA:START 1 ; DATA:STOP <n>
WFMPRE:BYT_NR 1 ; BIT_NR 8 ; ENCDG BIN ; BN_FMT RI ; BYT_OR MSB
WFMPRE:NR_PT, PT_FMT, XINCR, XZERO, PT_OFF, XUNIT, YMULT, YZERO, YOFF, YUNIT
CURVE #<digits><length><bytes>
```

**A reference that does not exist cannot be written to.** On an empty
`REF4`, the transfer settings are accepted but every field describing the
waveform itself - `PT_FMT`, `XINCR`, `PT_OFF`, `YMULT`, `YZERO`, `YOFF` -
is answered with 2241, "waveform requested is invalid", and the curve that
follows is met with 532, "curve data too long, curve truncated". Nothing
warns you; the reference simply stays empty.

`SAVE:WAVEFORM CH1,REF4` brings the reference into being from a live
channel, and after that everything above is accepted and the data lands
exactly. It needs a moment - reading the reference back immediately after
the save still fails - so allow a second before setting the preamble.

Verified on all three generations, reading a channel and writing it into
`REF4`, then reading that back and comparing byte for byte:

```
TDS 784D v7.4e    500 bytes    IDENTICAL
TDS 784C v5.3e    500 bytes    IDENTICAL
TDS 640A v3.8.8e  500 bytes    IDENTICAL
```

`WFMPRE:ENCDG BINARY` is rejected as invalid character data; the accepted
spelling is `BIN`.

**`SAVE:WAVEFORM` to a disk writes 500 points, whatever the record length
is.** Measured across seven records on a 784D v7.4e — 500, 1000, 2500, 5000
and 15000 points asked for, all five held by
`HORIZONTAL:RECORDLENGTH?`, and every file 1210 bytes holding 500 samples.
It is the displayed record that is saved, not the acquired one. A program
that wants the whole of a long record has to read it over the bus and write
the file itself, which is what this one does.

**What a round trip through `.WFM` does and does not reproduce.** The same
seven records, written by the instrument, read by `from_wfm` and written
back by `to_wfm`:

- Every decoded field matches what the instrument says about the same
  waveform over the bus - point count, `XINCR`, `YMULT`, `YZERO`, `YOFF` -
  and the samples come back byte for byte.
- Five header bytes are not decoded and come from the canned template:
  payload **121, 122, 124, 125 and 127**. An earlier note here said 121-122
  and 126-127, which was wrong: 126 is `aa` in every file and in the
  template, and 127 is the one that differs.
- **Nothing the instrument reads is in any of them.** Two files were built
  identical but for those five bytes - one as `to_wfm` writes it, one with
  `0x5A` in each and the checksum redone - written to `hd0:` and recalled
  with `RECALL:WAVEFORM`. Both came back the same 1000 samples, the same
  `YMULT`, `XINCR`, `YZERO` and `PT_OFF`, and the same `WFID`. Writing
  template values into them is safe, not merely untroublesome so far.
  `scratch/_wfmcare.py` runs it.
- What they are is still open, and two guesses were tested and killed.
  124-125 changes on every save with nothing else altered, so it is not a
  setting; it does not increment, so it is not a counter; and its steps do
  not track the wall clock over ten and sixty seconds, so it is not the
  time. 127 was `04` on all five DC files in the corpus and `07` on both AC
  ones, which looked like a coupling code until coupling was moved on its
  own: DC, AC and GND all leave it at `04`. The corpus correlation was the
  timebase and the volts a division moving with it.
  `scratch/_wfmhead.py` and `scratch/_wfm127.py` run those.
- **The axes the corpus held still land in fields already decoded.** Moving
  the vertical position alone moves payload 74 (`POSITION`) and nothing
  else; the offset alone moves 66-71 (`YZERO`); saving from CH2 instead of
  CH1 moves 103 (`SOURCE`). A channel label and `DATA:ENCDG` do not reach
  the file at all. So sweeping length, mode, coupling, volts and seconds a
  division covered every field the format has.
- The sweep is `scratch/_wfmsweep.py`.

**`DATA:SOURCE` on an empty reference stops every `WFMPRE` query dead.**
It is already known here that `WFMPRE:<source>:<field>?` is answered with
2241 unless `DATA:SOURCE` already points at that source - see `TdsWfm.get`,
which writes it first for exactly that reason. What is worse than 2241:
point `DATA:SOURCE` at a reference with *nothing in it* and the queries are
not answered at all. Measured on a 784D with CH1 displayed and perfectly
readable:

```
DATA:SOURCE CH1   -> WFMPRE:CH1:NR_PT?  500
DATA:SOURCE REF4  -> WFMPRE:CH1:NR_PT?  timeout
DATA:SOURCE REF3  -> WFMPRE:CH1:NR_PT?  timeout
DATA:SOURCE CH1   -> WFMPRE:CH1:NR_PT?  500
```

Naming the source in the query does not save you, and the bulk
`WFMPRE:<source>?` goes the same way. Both routes in `preamble` then time
out and the program reports the channel as unreadable, which is false. Every
caller inside the program sets `DATA:SOURCE` first, so this costs nothing
there; a bench script that calls `preamble` cold gets bitten, which is how
it was found.

**A reference holds only what it was allocated, and a longer curve is not
refused.** The four references share one pool, and each is allocated a
length of its own; a reference first written at 500 points stays at 500.
Send a 2500 point curve to it and the instrument keeps the front of it
and says nothing - the read-back is 500 points and does not match what
was sent, which is indistinguishable from the instrument corrupting the
transfer.

```
ALLOCATE:WAVEFORM?              500;0;1000;500      REF1..REF4
ALLOCATE:WAVEFORM:REF4 2500     accepted
ALLOCATE:WAVEFORM?              500;0;1000;2500
```

So `ALLOCATE:WAVEFORM:<REF> <points>` goes out before the transfer
settings, every time. The limit-template route has always sent it - it is
one of the lines `envelope_scpi` builds - and the plain waveform route
never did, which only showed once a record longer than 500 points was
captured. Measured on a 784D v7.4e.

## The screen comes from the printer port

There is no "give me the display" command. The way to a screenshot is the
hardcopy subsystem: `HARDCOPY:PORT GPIB` aims a print job at the
controller instead of at a printer, and several of the formats it offers
are images rather than pages of printer control language.

### An abandoned hardcopy wedges the session

`HARDCOPY START` must be read to the end. Stop reading part way - a
timeout, a killed process - and the instrument is left with a screen's
worth of bytes to say and nobody listening. The next program to open it
gets `VI_ERROR_TMO` on `*IDN?` and looks for all the world like an
instrument that has crashed.

It has not. A device clear abandons the transfer and it answers
immediately afterwards. This program issues one on any failed capture,
and `hcfix.py` in the diagnostics folder does it by hand.

### A second hardcopy started too soon is ignored

Not refused - ignored. No data, no event, nothing at all until the
timeout expires. Measured over seven consecutive captures on a 784D: with
no pause, two of the seven produced nothing; with two seconds between
them, seven of seven succeeded. The wait is part of getting an answer,
not politeness.

### `LAYOUT` is the orientation of the page, not of the image

`PORTRAIT` gives the screen the right way up, 640 x 480. `LANDSCAPE`
turns it on its side, 480 x 640. A 640A that arrives set to `LANDSCAPE`
- as this one did - produces a screenshot lying on its side until it is
asked otherwise. Landscape also costs about twice as long on a colour
instrument: 10.1 s against 5.5 s on a 784D.

### A monochrome instrument accepts colour formats and ignores them

A TDS 640A takes `BMPCOLOR`, `PCXCOLOR` and `RLE` without complaint, then
answers `HARDCOPY:FORMAT?` with `BMP`, `PCX` and `BMP` respectively. The
only honest way to know what a given instrument will actually give you is
to set the format and read it back.

### `bfSize` in the BMP header is wrong

Every BMP these instruments produce carries a file size that is not the
file's size: 77,070 in a 308,278 byte `BMPCOLOR`, 975 in a 38,462 byte
`BMP`, a flat 65,536 in an `RLE` of any length. Read to EOI and use what
arrived; nothing else in the header is wrong.

### What each format costs

Measured on the bench with an NI GPIB-USB-HS, same screen each time:

```
                   TDS 784D              TDS 784C            TDS 640A
                bytes    secs         bytes    secs      bytes    secs
TIFF           38,830    3.28        38,830    3.25     38,858    3.27
BMP            38,462    4.35        38,462    4.32     38,462    6.14
PCX            17,763    4.40        11,125    4.31     14,587    6.43
PCXCOLOR       81,351    5.30        23,284    5.07        - not offered -
RLE            85,318    5.35        21,704    5.09        - not offered -
BMPCOLOR      308,278    5.62       308,278    5.56        - not offered -
```

The time is almost entirely the instrument drawing the thing: `BMPCOLOR`
carries eight times `RLE`'s bytes for a quarter of a second more. So the
smallest is not the fastest, and on this adapter the choice barely
touches the wire at all. It matters on a slower one, which is why `RLE` -
a Windows BMP with run-length encoding, decoded by the same code as
`BMPCOLOR` - is what this program asks for where it exists, and `TIFF`,
twice as quick as `BMP` on a 640A for the same pixels, where there is no
colour to be had.

## The error log is not the event queue

`ERRLOG` is the instrument's own service history, in non-volatile memory
and surviving power cycles: power-on diagnostic failures, librarian
resets, calibration problems. The SCPI event queue this program reads
constantly is a different thing entirely - what just went wrong in this
conversation, emptied as it is read.

There is no command that returns the whole log. It is walked, and the
walk has one property worth knowing:

```
ERRLOG:FIRST?     the oldest entry; positions the cursor
ERRLOG:NEXT?      the next, until there are no more
```

**An empty log answers `""`, not silence.** All three instruments here
return an empty quoted string from `ERRLOG:FIRST?` when they have nothing
to report. That matters because the obvious implementation - treat a
timeout as "no errors" - has it exactly backwards: a timeout is what
firmware *without* `ERRLOG` gives, alongside 113. A healthy instrument
answers; a program that expects silence will call it broken.

Every entry arrives wrapped in the SCPI string delimiters, which are not
part of the text. The end of the log is an empty reply, the same one an
empty log gives to `FIRST?`.

### What clears it

`ERRLOG:CLEAR` does not exist - the parser answers 113 on all three. The
clue is that `ERR?` returns precisely what `ERRLOG?` returns, so `ERR` is
the short form of `ERRLOG` and the thing being asked for is a value given
to the header rather than a subcommand under it:

```
ERRLOG CLEAR      accepted on all three
ERR CLEAR         the same command, short form
ERRLOG:CLEAR      113, "undefined header"
```

Verified by clearing a TDS 640A that held 31 entries and reading back
none. `ERRLOG:NUMENTRIES?` and `ERRLOG:SUPPRESS?` do not exist either, so
there is no way to know the length of the log without reading it.

### What it looks like

Thirty-one entries off a 640A took 3.24 s, about 10 ms each:

```
Fri 01-04-=0 16:26:10  WARNING: 600 error log initialized, nvRamDiag error log re-initialized
Fri 01-04-=0 16:26:36  ERROR: diagnostic test failure, nvLibrariansDiag, Libs with crcc failures: ,  ExtConst, IntConst, State, Environment
Fri 01-04-=0 16:26:11  ERROR: diagnostic test failure, extended cal librarian reset
```

The date is the instrument's, malformed and all - `01-04-=0` is what it
says. It is shown as it arrives rather than tidied, since a service log
that has been reformatted by the reader is worth less than one that has
not.

## A channel that is not displayed cannot be read

`DATA:SOURCE CH2` is accepted whether or not CH2 is on the screen. The
`CURVE?` that follows is not:

```
2241, "Waveform requested is invalid"
410,  "Query INTERRUPTED"
```

Measured on a TDS 784D and a TDS 640A. There is no way round it and no
copy of the data elsewhere to reach for - the channel is not being
acquired, so there is nothing to send. `SELECT:CH2 ON` takes effect
immediately and the curve reads on the next attempt, which is why this
program offers a double-click on the greyed sources rather than simply
hiding them.

A stored reference behaves differently, and usefully: `REF3` reads back
whether or not it is displayed, because the data is already in memory.

## Deleting a reference, and how to tell that it worked

`DELETE:WAVEFORM <REF>` is the command. The bare header answers 100,
"command not allowed", which is what a header that wants an argument
says - not 113, which is what one that does not exist says.

Proving it removes the data rather than merely taking it off the screen
took a behavioural test, because none of the obvious queries can tell the
two apart:

  * `SELECT?` reports whether a reference is *displayed*, which is not
    the same thing. A reference created by `SAVE:WAVEFORM CH1,REF3`
    reports itself as not displayed and is readable anyway.
  * `WFMPRE:<REF>:NR_PT?` answers 2241 for *every* reference on these
    instruments, including one that demonstrably holds a waveform. It is
    no use for anything.

What does answer the question is reading twenty points:

```
create REF3, hide it, show it again  ->  reads fine        (hidden)
create REF3, DELETE:WAVEFORM REF3    ->  will not display,
                                         will not read     (deleted)
```

So the only reliable way to ask whether a reference holds a waveform is
to try to read one.

## `WFMPRE:<source>:<field>?` needs `DATA:SOURCE` set first

Two facts that refused to sit together:

* `WFMPRE:REF4:NR_PT?` is answered with 2241, "waveform requested is
  invalid" - even though REF4 holds a waveform that reads back byte for
  byte.
* `get("REF4")` works, and reads that same field.

The difference is that `get()` writes `DATA:SOURCE REF4` first. Asked
cold, the preamble query is refused for everything; asked after
`DATA:SOURCE`, it answers for exactly the sources that hold data:

```
source   cold (no DATA:SOURCE)   after DATA:SOURCE
CH1      refused                 answers        (displayed)
CH2      refused                 refused        (switched off)
REF1     refused                 answers        (holds a waveform)
REF3     refused                 refused        (empty)
MATH1    refused                 refused        (switched off)
```

So there *is* a reliable way to ask whether a source can be read - set
`DATA:SOURCE`, then ask for `NR_PT` - and the obvious way of asking is
not it.

## One doomed read overflows the event queue

The queue is twenty deep. Reading a source that cannot be read used to
cost twenty entries all by itself:

```
19 x 2241  "Waveform requested is invalid"
 1 x  350  "Queue overflow"
```

because the preamble is eleven fields asked one at a time, each one
refused, and pyvisa's own retries on top. The instrument then carried
that mess until something drained it - which in practice was the *next*
run of a program, where it appeared at connection as a page of errors
with no relation to anything the user had just done.

Asking `NR_PT` first and stopping when it is refused brings the same
doomed read down to three events and no overflow. Not attempting it at
all - which is what this program now does for a channel that is
switched off - costs none.

## `NR_PT` is the transfer window, not the record

`WFMPRE:<source>:NR_PT?` answers how many points *would be sent* if you
asked for a curve right now - which is `DATA:START`..`DATA:STOP`, not the
length of the record:

```
DATA:STOP 20        ->  NR_PT 20        of a 5000 point record
DATA:STOP 500       ->  NR_PT 500
DATA:STOP 5000      ->  NR_PT 5000
DATA:STOP 1000000   ->  NR_PT 5000      clamped, not refused
```

So a program that reads `NR_PT` and then sets the window to match it
inherits whatever window it finds, and there is nothing in the reply to
say that it did. A `.WFM` on the disk or another program on the bus can
leave `DATA:STOP` anywhere.

Setting `DATA:STOP` past the end is clamped, and each source then reports
its own length - 5000 for a channel on a 5000 point record, 500 for a
reference holding 500 points. Asking for a million and reading back the
answer is therefore a reliable way to say "all of it" without having to
know how much that is.

## How fast a waveform can come off, and what limits it

Measured with `streamrate.py`, `streamrate2.py` and `prefast.py` in the
diagnostics. The answer is not the bus.

Every exchange with the instrument costs about 12-13 ms whatever it
carries, so at short record lengths the samples are free and the number
of transactions is the whole cost. A `CURVE?` of 100 points and one of
500 both take 26 ms on a 784D.

| TDS 784D, v7.4e | |
|---|---|
| a bare query, `*ESR?` | 12.7 ms, so ~79 transactions a second |
| eleven `WFMPRE:<src>:<field>?` queries | 419 ms |
| one `WFMPRE:<src>?`, headers on | 81 ms |
| `CURVE?`, 500 points | 22 ms |
| a whole frame, eleven queries and a curve | 441 ms - **2.3 a second** |
| the same frame, one preamble query | 103 ms - **9.7 a second** |
| `WAVFRM?`, preamble and curve in one go | 116 ms - *slower*, 8.6 a second |

The payload rate only matters once the record is long enough to outweigh
that fixed cost. `CURVE?` alone on a 784D, roughly - few enough rounds
that these are indicative, not precise:

| points | curve | rate |
|---|---|---|
| 500 | 29 ms | 17 KB/s |
| 1000 | 28 ms | 35 KB/s |
| 5000 | 23 ms | 210 KB/s |
| 50000 | 118 ms | 412 KB/s |

Sustained, reading as fast as it will go: 40 frames in 5.04 s, 7.9 a
second, while `ACQUIRE:NUMACQ?` showed the instrument making 68
acquisitions in the same 5.04 s - 13.5 a second. So the PC sees a fresh
capture every time and misses about four of every ten.

The other two instruments are the same shape:

| | eleven queries | one query | `CURVE?` | frame now | frame with one |
|---|---|---|---|---|---|
| TDS 784C v5.3e | 457 ms | 76 ms | 39 ms | 496 ms, 2.0/s | 115 ms, 8.7/s |
| TDS 640A v3.8.8e | - | 184 ms | 85 ms | | |

### Stopping first makes the channels one capture

Reading CH1 and then CH2 while the instrument runs gets two different
acquisitions - 600 ms apart on this bus, with a 784D triggering thirteen
times a second in between. They disagree, and `XZERO` is where it shows:
the sub-sample trigger offset is a new number every capture.

```
784D, running        XZERO  CH1 19.75E-9   CH2 19.17E-9    channels agree: no
784D, ACQUIRE:STATE STOP    CH1  2.24E-9   CH2  2.24E-9    channels agree: yes
```

Stopped, `XINCR`, `PT_OFF` and `NR_PT` agree too, the same source read
twice comes back byte for byte the same, and `ACQUIRE:NUMACQ?` reads
13200 before the reads and 13200 after - so nothing was acquired while
the PC was reading. The stop cost 39 ms on a 784D and 118 ms on a 640A.

`ACQUIRE:NUMACQ` comes back as 0 after the release, so restarting resets
the acquisition count: in AVERAGE or ENVELOPE mode the accumulation
starts again, though what was captured is the completed average.

Three states must be left alone:

* **already stopped.** Somebody has frozen a single-shot capture; starting
  it again throws that away.
* **`STOPAFTER SEQUENCE`.** It may be armed and waiting for a rare
  trigger, and stopping and starting re-arms it.
* **anything that fails part way.** The release belongs in a `finally`,
  or a source that refuses leaves the scope halted.

A 640A has no `XZERO` at all, so its channels always agreed on the time
axis - and freezing still matters there for the other reason, which is
the bigger one: three channels read one after another are three
different captures of the signal, whatever the preamble says about them.

### There is no streaming mode on any of them

Every transfer is request and response: the PC asks, the instrument
answers, and then it is silent. Nothing pushes, there is no continuous
output, and there is no subscription to acquisitions. The three things
that come closest:

* **`WAVFRM?`** puts the preamble and the curve in one transaction, which
  should be the fewest a frame can take. Measured *slower* than asking
  for the two separately - 116 ms against 103 - so there is nothing in it.
* **SRQ on operation complete** lets the PC wait for a new acquisition
  instead of polling for one, but the transfer still has to be asked for
  afterwards.
* **FastFrame**, on the D series, captures many frames into the
  instrument's own memory quickly. They still come out over GPIB
  afterwards at the rates above; it is burst capture, not streaming.

So a live display on the PC is a poll loop, and about ten frames a second
at 500-2500 points is what the hardware will give.

### `WFMPRE:<source>?` and the 640A

The eleven separate field queries exist because the *headers-off* bulk
reply is not in the same field order on every instrument. With
`HEADER ON` that objection goes away - the instrument names each field,
and the reply parses like any other compound header:

```
:WFMPRE:CH1:WFID "Ch1, DC coupling, 500.0mVolts/div, ...";NR_PT 5000;
PT_FMT Y;XUNIT "s";XINCR 20.00E-9;XZERO 5.96E-9;PT_OFF 2500;
YUNIT "Volts";YMULT 20.000E-3;YOFF 0.0E+0;YZERO 0.0E+0
```

The 640A answers with **ten** fields, not eleven: it has no `XZERO` at
all. `WFMPRE:CH1:XZERO?` is not merely refused on that firmware, it is
not answered - the query times out and leaves command-error and
query-error bits in `*ESR?`. Reading it one field at a time therefore
costs a timeout on every capture on a 640A, where the bulk reply simply
does not mention it. Treating a missing `XZERO` as zero is right for
that instrument: its trigger is exactly at `PT_OFF`.

## Masks: eight polygons in percent, and no file anywhere

A C or D series TDS has a mask subsystem. A 640A has none at all -
`MASK:STANDARD?` is not a command on v3.8.8e - so anything about masks
is greyed out there for the same reason `WRITEFILE` is.

A mask is **eight segments**, each a closed polygon given as x,y pairs in
**percent of the graticule**:

```
MASK:MASK<1..8>:POINTSPCNT <x1>,<y1>,<x2>,<y2>,...
MASK:DISPLAY 0|1          MASK:SOURCE CH1
MASK:COUNT:STATE|TOTAL    MASK:INVERT
MASK:MARGIN:STATE|PERCENT MASK:AUTOSET:MODE|STANDARD|OFFSETADJ
MASK:STANDARD             NONE, USERMASK, or a built-in telecom standard
```

`MASK9` and beyond answer with nothing, so eight is the number.

Measured limits, which are not in any manual to hand:

| | |
|---|---|
| points a segment keeps | **50** - asking for 64 stored 50 and left an event in the queue |
| fewest points that stay | **2** - a single point comes back `0,0` |
| a whole mask written | 194 ms for eight points |

### There *is* a Tektronix mask format, and it is TTiP's

Not on the instrument - on the PC. **TTiP, "Telecommunications Templates
and i-Pattern", Tektronix 1993/1996, part 070-8919-00** shipped ten
reference masks as `.MSK` files: OC1, OC3, STS1, STS1NEW, STS3, STSX3,
STM1, DS4NA, DS4XNA and a 50-ohm eye. Every one is 2023 bytes.

Decoded from the files; the manual documents the software and never
states the format:

```
offset   0   int16       how many masks
offset   2   int16 x N   points in each mask
offset  26   N x 50 x (int16 x, int16 y), little-endian
             x is 0..511, y is -127..+127
```

The manual does confirm two numbers: "You can use up to 50 points to
define a single mask" and "A maximum of ten masks can be defined on a
single waveform". Fifty points a mask is exactly what the instrument's
own `MASK:MASK<n>:POINTSPCNT` turned out to accept, measured separately
over GPIB - two independent routes to one number.

**The points are a set, not a path.** OC1's eye is listed as (17,50)
(36,32) (83,50) (36,68) (64,68) (64,32), and drawn in that order it is a
bowtie. The order carries no meaning and the outline has to be recovered
by sorting about the centre. Measured across all ten shipped files: 32
of their 37 segments cross themselves if the stored order is drawn
naively, and every one of the 37 comes out a simple polygon once sorted.

### A mask does not have to be convex

The manual says "if you want to create a mask with a concave area,
create several masks", and it is tempting to read that as a rule about
what the hardware will hold. **It is not.** Measured on a 784D with
Option 2C: a concave L - (20,20) (60,20) (60,45) (35,45) (35,80)
(20,80), one plainly reflex corner - sent to `MASK:MASK1:POINTSPCNT`
reads back point for point identical, and the instrument draws it on the
graticule as the L it is, reflex corner and all.

Tektronix's own files bear that out. Four of the ten shipped masks have
concave segments, and not marginally: STS1, STS1NEW, DS4NA and STS3
between them hold ten such segments, with points sitting up to 41% of
the graticule inside their own convex hull. That is not quantisation -
those points are *strictly inside* the hull, so no ordering of them
could be convex. The sentence in the manual is advice about drawing
masks by hand, not a constraint on the format or the firmware.

What a concave shape does **not** survive is the i-Pattern file. Because
the format keeps points without their order, reading one back sorts them
about the centre again - which recovers a convex shape exactly and a
concave one only by luck. The same L saved as i-Pattern and reloaded
comes back as the same six points joined up a different way. This
program's own format keeps the path, so a concave mask survives it
exactly; saving a concave mask as i-Pattern asks first.

The coordinate range is **inferred, not documented**: every value seen
lies in 0..511 and -127..+127; the reference masks' outer segments run
corner to corner using exactly those extremes; the records they were
drawn against are 500 points long while the masks use 497, 510 and 511,
so x is display space rather than record position; and 512 x 255 is the
natural raster for a 1993 DOS program.

### But there are shapes it will not draw the way they were sent

Concave is fine; two other things are not. Measured on a 784D (v7.4e)
with `SELECT:CH1 OFF`, so the only ink on the graticule is the mask's
own (palette index 12): each shape was sent to segment 1, the screen
read back through the hardcopy port, and every possible line between two
of the sent points scored for how much of it was inked. That gives the
edge set the instrument actually drew, rather than a guess scored
against a picture.

`MASK:MASK1:POINTSPCNT?` came back point for point identical every
time, including for the shapes that were drawn wrongly. **What the
instrument changes is the drawing, not the points**: it stores the list
faithfully and joins it up its own way.

Fifteen shapes, and the split is clean:

| shape | drawn as sent |
|---|---|
| rectangle, L, staircase, T, cross | yes |
| three and four points sharing a row | yes |
| three collinear points on a slope | yes |
| U, chevron | **no** - reordered |
| notch cut into the left or the right side, S-bend | **no** - reordered, points dropped |
| three or four points stacked in a column | **no** - a point dropped |

Two rules cover all fifteen:

* **The outline may double back only once in each direction.** No
  horizontal and no vertical line may cross it more than twice. A U or
  a chevron rises twice in y; a shape notched from the side runs twice
  across in x. Being concave is not the issue - the cross has four
  reflex corners and draws perfectly.
* **Three consecutive points on the same vertical are one too many.**
  The instrument drops one of them. Three on the same horizontal are
  fine; so are three in a line on a slope; and so are four points
  sharing a column when they are not consecutive - the cross has four
  at x=40.

What it draws instead is an x-monotone polygon through the same points:
leftmost point, everything above the line to the rightmost point in
order of x, the rightmost point, then everything below it coming back.
The U, for instance, comes back as (20,20) (70,50) (80,20) (80,80)
(30,80) (30,50) (20,80) - the bottom edge replaced by a peak through a
point that belongs on the other side of the shape. The reconstruction is
not worth reproducing exactly: where several points share a column its
tie-breaking is inconsistent between shapes, and where it drops points
which one goes varies. Predicting *whether* it happens is what matters,
and that is what `tds_msk.redraw_reason` does.

Tektronix's own files obey the rule: of the 37 segments in the ten
shipped `.MSK` files, 31 pass. The six that do not are all four segments
of STS1 and the first two of STS1NEW - the oldest SONET masks, whose
outer segments are a band across the whole graticule that dips twice.
Sent as one segment those are drawn wrongly, and the way round is the
way the manual suggests: cut the shape into simpler pieces, which is
what the editor's scissors are for.

### Reading a mask back off the segments

The eight segments answer `MASK:MASK<n>:POINTSPCNT?` with the points
they hold, and that is the only route a mask has *off* an instrument -
there is no mask file to fetch. What comes back depends on the series,
measured on all three here with the same two-segment mask:

| instrument | what came back |
|---|---|
| TDS 784D, v7.4e | the points sent, exactly |
| TDS 784C, v5.3e | x exactly; **y on a 1/399 grid** |
| TDS 640A, v3.8.8e | no mask subsystem at all |

The C series quantisation is not noise and not rounding in transit: 20
comes back as 20.050125313, which is 80/399, and 45, 60 and 80 come back
as 180/399, 239/399 and 319/399. Every value fits a y stored as a whole
number of 399ths of the graticule, so a mask read off a 784C is within
0.13% of the one that went in and never nearer. Nothing here corrects
for it - what the instrument says it holds is what it holds.

**An empty segment answers with one pair of zeros**, `0.0E+0,0.0E+0`,
not with an empty reply. Measured on a 784D with seven segments empty.
That is the convention `Mask.from_scpi` reads as "nothing here", and it
is also how an empty segment is *written*, which makes emptying the
instrument a matter of sending a mask with nothing in it rather than of
finding a command that deletes one. There is no such command worth
using: `MASK:STANDARD` deletes the points and puts a standard mask in
their place.

**x comes back at 99.8 where 100 was sent.** Not a rounding error in
transit: the instrument holds x as one of 500 columns, so the rightmost
it can express is 499/500 of the graticule. A mask whose keep-out runs
to the right-hand edge reads back 0.2% short of it, which is a fifth of
a pixel on its own screen and worth knowing before calling a round trip
a failure. y is not quantised this way on a D series - 0, 20, 80 and 100
all came back exactly.

`MASK:DISPLAY OFF` stops it drawing what is left.

### A 794D is not a faster 784D in the way that matters here

Both are 1 GHz and both hold masks - `2C:comm` is in the 794D's `*OPT?`
- and both offer exactly the same timebase steps, including the same
silent moves. Three differences matter, all measured:

* **Its inputs are 50 ohm only.** `CH1:IMPEDANCE MEG` comes back
  "Parameter error" and the channel stays at FIFTY. A 10x passive probe
  wants the 1 Mohm input, so on a 794D that means an active or a
  differential probe, and it means a setup file that names an impedance
  fails on one of the two instruments whichever way it names it. The
  protocol setups leave `IMPEDANCE` out and say what the probe needs in
  their own REM lines instead.
* **1 V/div is as far as it goes** on those inputs. Asking for 5 V/div
  gets 1 V/div, silently. So RS-232 at +-15 V is not a measurement a
  794D can make - and +-15 V into 50 ohm is 4.5 W, which is a reason to
  keep it away from the input rather than a limit to work around.
* **It answers with headers on** where the 784D here has them off. That
  is a session setting rather than a model trait, but it is worth
  building for: `Mask.from_scpi` strips a leading header now, because a
  reply of ":MASK:MASK1:POINTSPCNT 20.0,..." parsed as numbers gives an
  empty segment and no error at all.

The mask subsystem itself behaves identically: a six-segment mask sent
and read back comes home point for point, with the same x quantisation
to 1/500 at the right-hand edge.

### What the timebase will actually do, and when it is interleaving

Asked for a spread of settings and read back, on a 784D with the record
at 500 points:

| asked | got | asked | got |
|---|---|---|---|
| 200 ps | 200 ps | 12.5 ns | 12.5 ns |
| 500 ps | 500 ps | 20 ns | **25 ns** |
| 1 ns | 1 ns | 25 ns | 25 ns |
| 2 ns | 2 ns | 50 ns | 50 ns |
| 4 ns | **5 ns** | 100 ns and slower | as asked |
| 5 ns | 5 ns | | |
| 10 ns | **12.5 ns** | | |

So it is 1-2-5 at the slow end and 1-2.5-5 through the tens of
nanoseconds, and **it moves silently**: ask for 10 ns/div and the sweep
is 12.5, which is 25% wider than whatever was drawn for it. The reason
is the sample interval, which has to be a multiple of 250 ps: 12.5 ns a
division is 500 points at 4 GS/s exactly.

That number is also the boundary worth knowing. **12.5 ns/div is as
fast as one trigger can fill a 500-point record** - 4 GS/s over 125 ns.
Below it the instrument interleaves successive triggers, which builds a
picture only if the pattern repeats. For a clock that is honest; for
scrambled data it is not, which is why a link has to be put in a test
mode before an eye at 2 ns/div means anything.

### The volts per division ceiling is the probe's, not the firmware's

The 794D stops at 1 V/div where a 784D goes to 10, which reads like a
model difference and is not one. It is the input: the manual has the
maximum vertical scale falling from 10 V to 1 V per division on a 50 Ω
input, "for example, an active 10X probe would provide 10 V per division
and a passive 10X probe would provide 100 V per division" (Programmer
Manual, `CH<x>:IMPedance`, 2-72). The ceiling is the input's own times
the attenuation of whatever is in front of it.

So RS-232 at 5 V/div is not out of a 794D's reach. It is out of reach
*with a 1X probe*, and a 10X probe on that channel puts it back. That is
a thing to tell somebody at the bench, and the program does.

**`CH<x>:PROBE?` is a query and nothing else** (2-75). The instrument
reads the attenuation off the probe's own coding ring and will not be
told otherwise, which is exactly right. `CH<x>:PROBEFunc:EXTAtten` *does*
set, and is a trap: it is a claim about hardware that may not be there.
Set it to 10 with no such probe fitted and every number on the screen is
ten times what the signal is, the mask fits beautifully, and the
measurement is nonsense. It is never set from here.

### A value it cannot reach is replaced without a word

There are two ways an instrument does not do what it was told, and only
one of them is loud.

* A **command** the firmware does not know is refused, and the refusal
  lands in the event queue where `EVMSG?` can be drained for it.
* A **value** it cannot reach is not refused at all. It is silently
  replaced with the nearest one it can do.

The timebase table above is the first case of it - ask 10 ns/div, get
12.5 - and the volts per division ceiling is the second: ask a 794D for
5 V/div and it answers 1, with an empty event queue and no complaint.

Measured by sending `RS232.SET` to both instruments and reading the
settings back, with a 1X probe on CH1 of each:

| asked for | 784D (v7.4e) | 794D (v8.0e) |
|---|---|---|
| `CH1:SCALE 5.000000E+00` | `5.0E+0` | **`1.00E+0`** |
| `HORIZONTAL:MAIN:SECDIV 2.000000E-06` | as asked | as asked |
| `HORIZONTAL:MAIN:SECDIV 1.0E-8` | **`12.5E-9`** | **`12.5E-9`** |
| `CH1:IMPEDANCE MEG` | as asked | **`FIFTY`** |

The whole of `RS232.SET` applies to a 784D without a single change, and
the same file on a 794D silently loses a factor of five in the vertical.
Neither instrument said anything about the timebase.

The impedance is the one case that is *both*: the 794D refuses it into
the event queue - `220,"Parameter error;  -  1M  @ 50 @"` - **and**
leaves the setting at `FIFTY`, so it shows up in the readback as well.
Everything else here is silent.

Which means **`*ESR?`/`EVMSG?` is not enough to know a setup applied**.
The only reliable test is to read the settings back and compare them
with what was asked for, which is what `tds_set.differences` does. A
mask is held in percent of the graticule, so a silent coercion is not a
cosmetic problem: the same mask against a 25% wider sweep is testing
something 25% different and says nothing about it.

### The setup that goes beside a mask, and how this firmware spells it

TTiP shipped a `.SET` with every mask - `OC3.MSK` and `OC3.SET` - and
the reason is that a mask is percent of the graticule: it says nothing
about the signal it was drawn for. Their file is nineteen lines of plain
SCPI, `:REM` comments and all, sent down the bus by `LOAD.EXE`.

**Two different files are called .SET.** The other kind is what
`SAVE:SETUP "hd0:/x.SET"` writes on the instrument's own disk - 4618
bytes of binary on a 784D, tied to the firmware that wrote it, and
recallable from the front panel. A text setup cannot be recalled there
and a binary one cannot be read here. They are not interchangeable.

Reading all 144 text setups TTiP ships, the only commands in them that
are not settings worth keeping are `ACQUIRE:STATE`, `LIMIT:COMPARE` and
the `ZOOM` group. Their semicolons follow the SCPI rule and have to be
read that way: `:TRIG:MAIN:MODE AUTO;TYPE EDGE;LEV 0;HOL:VAL 0` is four
commands sharing a path, and a parser that splits on semicolons without
carrying `TRIG:MAIN:` forward reads one of the four.

Three spellings were **measured on a 784D (v7.4e)** rather than taken
from the manual, and two of them are not what the manual would suggest:

| asked | answer |
|---|---|
| `ACQUIRE:REPETITIVE?` | refused |
| `ACQUIRE:REPET?` | `1` |
| `TRIGGER:MAIN:HOLDOFF:VALUE?` | refused |
| `TRIGGER:MAIN:HOLDOFF?` | `250.0E-9;DEFAULT` |
| `TRIGGER:MAIN:HOLDOFF:TIME?` | `250.0E-9` |
| `TRIGGER:MAIN:HOLDOFF:BY?` | `DEFAULT` |

So TTiP's own `HOL:VAL 0` is a line this generation will not take, which
is worth knowing before sending one of their files: the instrument logs
what it cannot use and carries on, so a setup can half apply in silence.
Anything sent through this program drains the event queue afterwards and
says what was refused.

Read, written out and sent back, all 22 fields a 784D answers to come
back exactly as they were, with nothing in the event queue - `CLEARMENU`,
`HORIZONTAL:FITTOSCREEN` and `ACQUIRE:REPET` included. A field the
instrument will not answer is left out of the file rather than guessed
at: on this instrument that is `ACQUIRE:REPETITIVE` and
`HOLDOFF:VALUE`, and on a 640A it would be `CH1:IMPEDANCE`.

### The way Tektronix put a limit on the instrument was not masks

Disk 2 of TTiP holds `.ENV` files, which are plain SCPI text to send
down the bus:

```
:DATA:DESTINATION REF1;ENCDG ASCII;WIDTH 2;START 1;STOP 1000
:ALLOCATE:WAVEFORM:REF1 1000
:WFMPRE:PT_FMT ENV;XINCR 1.0e-11;PT_OFF 500
:CURVE -32767,32767, ..., 15079,20089, ...
```

An **envelope** - min/max pairs - loaded into a reference memory. The
matching `.SET` configures the instrument and ends `:LIMIT:COMPARE:CH1
REF1`. So the mechanism is **limit testing**, not the MASK subsystem,
and `LIMIT:` answers on all three instruments here - including the 640A,
which has no mask subsystem at all:

```
LIMIT:STATE 0;HARDCOPY 0;BELL 0;COMPARE:CH1 REF1;CH2 NONE;...
LIMIT:TEMPLATE:SOURCE CH1;DESTINATION REF1;TOLERANCE:VERTICAL 40.0E-3;HORIZONTAL 40.0E-3
```

That route goes through the reference upload this program already has
and never touches `POINTSPCNT`, so the constant-X problem below does not
arise on it.

#### What the numbers in an `.ENV` mean

`cc0_155m.env` holds exactly **1000 numbers**, which is 500 min/max
pairs: `NR_PT` counts values, not columns. They are 16-bit `RI`,
`BYT_OR MSB`, and the format that makes the instrument read them in
pairs is `PT_FMT ENV` — send the same numbers with `PT_FMT Y` and it
draws them as an ordinary 1000-point trace.

The vertical unit is the digitiser's own count, **6400 to a division**
at sixteen bits, with the centre line at zero. So the top of the
graticule is four divisions, 25600, and the ±32767 the shipped files use
for "no limit here" is 5.12 divisions — past the edge of the graticule,
which is the point of it. (Eight-bit records are 25 counts a division;
the same arithmetic, a different constant.)

#### Measured end to end, against a signal generator

A 784D with a signal generator on CH1: a 1 kHz square at 1 V/div, and a
mask with keep-outs above 70% and below 30%. **2 Vpp runs on, 4 Vpp
stops.** The instrument's own status line reads `Stop: Limit Test`, and
the failing trace is repositioned so the first offending sample sits at
centre screen, as the user manual describes.

Three things had to be right, and two of them are not obvious:

**The counts are biased by the channel's `YOFF`.** Screen centre is raw
zero only for a channel at position zero. CH1 sat at `YOFF 12800` - two
divisions - so a band of ±10240 about zero is a band of −3.6 V to
−0.4 V, entirely below a ±1 V signal, and it failed the good signal as
well as the bad one. Built about `YOFF`, the band is 2560…23040 and the
signal's own raw 6400…19200 sits inside it. This was the whole bug.

**The scaling fields are copied from the channel.** Without them the
reference keeps whatever was last in it - a template written against a
1 V/div channel came back as `Ref1 500mV 10.0µs` beside `Ch1 1.00V
500µs`. `XINCR`, `XZERO`, `PT_OFF`, `YMULT`, `YZERO`, `YOFF`.

**The length does not have to match.** A 1000-value template against a
5000-point record discriminated correctly, so the point counts need not
agree - which was worth knowing, because the opposite was assumed for a
while. The template is still sized to the record the app captured,
since that is the one length that is certainly right.

`YMULT 156.25E-6 × 6400 = 1.000 V/div` exactly, which confirms
`ENV_COUNTS_PER_DIV = 6400` on hardware rather than by inference.

#### There is no pass/fail query

The whole LIMIT group is `BELl`, `COMPARE:CH<x>`, `COMPARE:MATH<x>`,
`HARDCopy`, `STATE`, `TEMPLate`, `TEMPLate:DESTination`,
`TEMPLate:SOUrce` and `TEMPLate:TOLerance`. Nothing reports the result.

The manual gives the instrument three responses to a violation: ring the
bell, take a hardcopy, or stop. Only the last is readable over the bus,
so the way to get a verdict is `ACQuire:STOPAfter LIMit` and then poll
`ACQuire:STATE?` - 1 while it passes, 0 the moment it does not.

Two traps in the same pages, both of which make limit testing silently
do nothing:

- `LIMit:STATE` "can still set and return values. However, this feature
  will not actually function" when extended-acquisition-length or
  InstaVu mode is on, and cannot be turned on at all while extended
  acquisition is.
- The user manual: "If DPO, Extended Acquisition, or **Masks** mode is
  on, [limit testing] is not available." Masks and limit testing are
  mutually exclusive, which retires the mask subsystem as a route
  entirely.

Tektronix's own workflow is `LIMit:TEMPLate STORe`, which builds a
template *from a waveform* with ±vertical and ±horizontal tolerances in
fractions of a division. Uploading an arbitrary envelope, as TTiP's
`.ENV` files do and as this program does, is the unofficial path.

#### `LIMit:TEMPLate STORe`, measured

Set `LIMIT:TEMPLATE:SOURCE`, `:DESTINATION`, `:TOLERANCE:VERTICAL` and
`:TOLERANCE:HORIZONTAL` (divisions, 0 to 5), then `LIMIT:TEMPLATE
STORE`. On a 784D at 500 mV/div, 200 µs/div, against a 1 kHz square:

```
"Ref3, DC coupling, 500.0mVolts/div, 200.0us/div, 500 points, Envelope mode"
NR_PT 500   PT_FMT ENV   XINCR 4.000E-6   XZERO 1.055E-6   PT_OFF 85
```

Tolerances of zero are accepted. The test held for eight seconds at
1 Vpp and stopped within two of the amplitude being doubled.

#### Reading that template back: two orderings, both easy to get wrong

**`WFMPRE` answers for the `DATA:WIDTH` currently set.** Read the
preamble before setting the width and `YMULT` comes back as the
eight-bit 20 mV a count rather than the sixteen-bit 78.125 µV — every
volt 256 times too big, which draws as a band covering the whole
graticule. A band that covers everything passes everything, so it looks
like it works. Set `DATA:SOURCE`, `:ENCDG` and `:WIDTH` **first**, then
read the preamble.

**`DATA:START`/`:STOP` count values, not columns.** Asked for `NR_PT*2`
the instrument answers `531,"Data stop > record length, Curve
truncated"` and sends `NR_PT` numbers anyway. So `STOP NR_PT` is what to
send, and what comes back is `NR_PT/2` min/max columns — 250 of them
here, not 500.

Those 250 columns cover the **whole** record, not half of it. Measured
rather than assumed: the edges of a 1 kHz square landed 62.3 columns
apart out of 250, which at `NR_PT × XINCR / 250` = 8 µs a column is
498 µs against the 500 that is right. With the tolerances set to zero
the band's edges sat within 4.7 µs of the trace's — less than one
column. So a column's time is

```
XZERO + (i × NR_PT/columns − PT_OFF) × XINCR
```

the same formula a trace is placed with, which is what keeps the two on
one time axis.

#### A template must be as long as the record, or it is stretched

`LIMit:TEMPLate STORe` on a 500 point record writes `NR_PT 500` — 250
columns of a min and a max. Send a template of a different length and
the instrument does **not** refuse it: it takes the curve and spreads it
across the record.

Measured on a 784D. A 500 column template (1000 values) sent into a 500
point record read back as 250 columns, so every column landed at twice
its own time — the band drawn for the low rail sat over the high one. A
band that had just been learnt off the instrument then failed the very
signal it was learnt from, in 117 of 250 columns, with 206 of 500 live
samples outside it. Sized to the record instead, the same band passed
that signal and stopped within two seconds of the amplitude doubling.

Nothing about this is loud. A mask's band is the same all the way across
— keep-out above, keep-out below — so stretching it horizontally changes
nothing, which is why the mask route ran correctly for months with a
fixed length. Only a band that follows the signal shows it.

#### A column is a width, not a place

Turning a drawn shape into per-column limits by sampling the shape at
each column's **centre** slices every steep edge in half: the value
found there is somewhere mid-transition, while the signal in that column
has already reached the far rail. Measured on the same 784D: a band
drawn from a learnt envelope came back tighter than that envelope in 18
of 500 columns, every one of them at a transition of a square wave.

The extremes over a column are at its two edges or at a corner inside
it, so all three are taken and the widest wins. Erring outward is the
only safe direction — a limit that is too generous passes a signal
somebody has already looked at, and one that is too tight invents a
failure. The same applies to simplifying a limit for editing: thinning
must only ever move a boundary away from the band, never into it.

#### A limit test can leave the display drawing half the record

After a run of `ACQUIRE:STOPAFTER LIMit` starts and stops, a 784D was
left drawing only the post-trigger half of its record — five divisions
of ten, with the trigger at the left edge of the trace instead of the
middle.

The record itself was untouched: 500 points over 1.996 ms, real signal
in both halves, all four edges of a 1 kHz square, and `CURVe` returned
every one of them. Only the screen was wrong, so nothing this program
captures, judges or saves is affected.

It also could not be argued out of it. `HORizontal:TRIGger:POSition`
took 10, 50 and 90 and read each back correctly while the picture did
not move a pixel; switching the channel off and on did nothing; stopping
and running did nothing. What fixed it was rebuilding the pixel
database — `DISPLay:MODe INSTAVU` and straight back to `NORMal`.

Worth knowing before believing a screenshot: the graticule can lie about
the record while the record is perfectly good.

#### The mask subsystem is a stub without its application

`MASK:MASK1:POINTSPCNT` accepts points, `MASK:DISPLAY 1` is accepted,
and `MASK:STANDARD USERMASK` is accepted **with an empty event queue** -
and then `MASK:STANDARD?` still answers `NONE` and nothing is drawn on
the screen. Photographed on a 784D to be sure.

That is consistent with everything else here: the mask-testing
applications are Java, they keep their definitions in their own `.JAR`s,
and the SCPI is a front end to an engine that is not running. It also
explains the constant X below - there is nothing behind the parser to
store it. So writing an instrument's live mask segments is not a route
this program offers, and the limit-template route above is the one that
works, on all three instruments here.

**Superseded.** The instrument this was measured on had no Option 2C.
With the option fitted the segments accept points, draw them, and count
against them - see "Masks are Option 2C" below and "The instrument
counts the hits itself". The limit-template route is still what a scope
without the option gets.

#### One band, so pulse masks only

A limit test permits the signal a single band between a lower and an
upper limit. That is exactly what a **pulse mask** is: keep-out above,
keep-out below, signal between. It is not what an **eye mask** is — an
eye has a shape sitting *on* the centre line with the signal passing
above it and below it, which is two bands, and no envelope can say that.

Converting a mask is therefore: cut each column with a vertical line,
take what the shapes cover, and the allowed band is the gap in that
covering **which contains the centre line**. A shape straddling the
centre leaves no such gap, and that is the test for an eye.

Run over the ten masks TTiP shipped, **STS1 and STS1NEW convert and the
other eight do not**. Checked by inspection: OC1's segment 1 spans y
32.3-67.7 and DS4NA's segments 3 and 4 span 23.6-76.4 — the two sides of
an eye opening — while all four of STS1's segments stay clear of the
centre.

#### Reading the segments back is still worth doing

`MASK:MASK<n>:POINTSPCNT?` cannot be trusted for X, but the *number* of
points in each segment is true whatever the X problem turns out to be,
and it is the only way to see what a scope is holding without walking to
it. An empty segment answers with zeros rather than with nothing, so a
pair of zeros is not a point.

That sentinel is only safe for a segment that is *entirely* zeros. The
top left corner of the graticule is literally `0,0` to the instrument,
so a real four-point band along the top edge reads back four points and
was counted as three. Measured on a 784D: the reply carried all four
pairs, and only the counting threw one away. The rule is therefore that
one pair of zeros and nothing else is empty; a zero pair among others is
a corner.

`MASK:STANDARD?` is the cheap way to ask whether there is a mask
subsystem at all. On a 640A it is not a command, so the query has to be
given a short timeout of its own and the answer remembered - left at the
session timeout, asking costs most of a minute every time.

#### The instrument counts the hits itself

A 784D with Option 2C keeps its own tally against the mask, which is
the verdict this program could not otherwise have. Measured, not taken
from the manual:

| command | what it does |
|---|---|
| `MASK:COUNT:STATE 1` | start counting; `MASK:COUNT?` reads the state |
| `MASK:COUNT:TOTAL?` | samples that landed in any segment |
| `MASK:COUNT:WAVEFORMS?` | acquisitions tested |
| `MASK:MASK<n>:COUNT?` | the tally for one segment |

There is **no `MASK:COUNT:RESET`** - it answers `113,"Undefined header"`
- and no `MASK:STOPONVIOL`, no `MASK:TESTWAVES`, no
`MASK:COUNT:FAILURES`. Taking `STATE` to 0 and back to 1 clears the
counters, which is the only reset there is.

What makes this worth having is that **it counts in DPO**, where the
waveform record is filler and nothing can be read back. Measured on a
784D at 25 ns/div against `RS485_S`, a 64-bit pattern into CH1 and its
clock into CH2:

| what was driven | display | tested | hits |
|---|---|---|---|
| 2 Vpp, clean | NORMAL | 308 waveforms | 0 |
| 1 Vpp, into the eye keep-out | NORMAL | 307 waveforms | 30,679, all segment 1 |
| 2 Vpp, clean | INSTAVU | 121,910 waveforms | 0 |
| 2 Vpp, edges displaced ±20 ns | INSTAVU | 121,911 waveforms | 66,811, all segment 1 |

Note the DPO column: a hundred and twenty thousand acquisitions against
three hundred, which is the whole reason to run an eye in DPO at all.

### An eye mask sits half a bit from the trigger

A mask is percent of the graticule, and an eye mask's opening is drawn
about the middle of the screen. The trigger fires on a *bit boundary* -
a crossing, whether it comes from the data or from a clock. Leave the
trigger at the middle of the sweep and the two coincide, so the edge is
driven straight through the middle of the keep-out. Measured on a 784D
against `USBFS_D` with a signal that passes cleanly once the position
is right: **3,851 hits over 362 acquisitions**.

The arithmetic is `50 - (bit/2) / (10 x secdiv) x 100` percent, which
is 16.7% for a 83.3 ns bit at 12.5 ns/div. Two things about it:

- **The instrument holds whole percent only.** Ask for 16.7 and
  `HORIZONTAL:TRIGGER:POSITION?` answers 17. A setup file that asks for
  a tenth reports a difference every single time it is sent, so the
  files are written to whole percent. One percent of a 125 ns sweep is
  1.25 ns, far inside any eye's timing budget.
- **The arithmetic is a starting point, not the answer.** It is only
  exact if the trigger fires precisely on a data boundary. Trigger on a
  clock and the phase between clock and data is fixed but arbitrary -
  measured values on this bench have been 16.1%, 20.2%, 22.6% and 23.4%
  for the same nominal 16.7%, changing whenever the generator's
  waveform was reloaded. Even on a data trigger the driver's own slew
  delays the crossing. It has to be measured.

### Tektronix's own eye application never mentions the trigger

`071-0606-00` is the TDSCEM1 Communication Eye-Diagram Measurements
manual - Tektronix's own mask application for these instruments. The
word **"trigger" does not appear once in its 71 pages**. What it says
instead is:

> Sample Size - The number of **records** needed in the eye-diagram to
> determine when the results are stable

> NOTE. The TDSCEM1 application properly **aligns the eye-diagram** of
> the communications signal over the selected mask pattern.

So their answer was neither a clock nor a delayed sweep: take a series
of ordinary records off CH1 and recover the timing **in software** -
find the bit period from the data, fold the records onto it, and align
the result on the mask. That is what `tds_wfm.fold_eye` does here.

Two things measured while building it:

- The phase is recovered by pooling the crossings of **all** the
  records, not each record separately. Per record there are only one or
  two crossings at a mask's own timebase, so aligning each record to
  its own edge would collapse the crossing spread to nothing and make
  every signal look perfect.
- The samples have to be **joined up**, and the join broken where the
  fold wraps. Plotted as loose points a fast edge is a scatter of
  specks; joined without the break, the last sample of one bit is drawn
  to the first of the next, straight across the middle of the eye.

A record off a 784D costs about **0.6 s** over GPIB, and at a mask's
own timebase holds a bit and a half, so twenty-five records is about
fifteen seconds for forty bits and twelve thousand samples.

### Two ways to a full eye without a clock, one of which does not work

Measured on a 784D with the generator's clock output switched off:

- **Alternating the trigger slope does not work.** Rising edges put a
  high bit after the trigger and falling edges a low one, so the two
  together ought to show both rails. They do not, because changing the
  trigger **clears what has accumulated** - in DPO, where the waveform
  count restarted at 47,967 rather than climbing past 90,000, and in
  the ordinary display too: with `DISPLAY:STYLE INFPERSIST`, eight
  seconds after the slope was flipped the rising-edge traces were gone.
- **A delayed sweep does work.** Run out far enough that the data no
  longer remembers the edge that triggered it and both rails appear.
  What it needs is data that actually decorrelates: a 64-bit pattern
  repeating every 5.3 us gives discrete bands, and the same signal as a
  1024-bit pattern fills the eye in completely. Its cost is that the
  delay's own jitter is added to the signal's.

### A clock cannot be found by asking what the instrument has

`SELECT?` is the only list of inputs there is, and it reports **which
are displayed**, not which exist:

    :SELECT:CH1 1;CH2 0;CH3 0;CH4 0;MATH1 0;...

A clock is by definition an input nobody wants on the graticule - it is
there to trigger, not to look at - so it is always switched off, and
`sources()`, which keeps only the ones that are on, answers `['CH1']`.
Any list built from that offers every input **except** the one that
could be the clock. Measured on a 784D with a 2 Vpp 12 MHz clock
physically connected to CH2 and its trace off: `sources()` still said
`['CH1']`.

So a channel has to be **switched on to be looked at** - asking for a
switched-off one raises 2241, "waveform requested is invalid" - and put
back afterwards. Which of them is a clock is then a measurement: a
square wave at the bit rate crosses its own middle every half a bit,
evenly, where data crosses at whole multiples of a bit and unevenly.

### There is no set-to-50% command, so a clock's level is measured

The front panel has a "Set to 50%" button for the trigger level and the
bus has no equivalent: `TRIGGER:MAIN:SETLEVEL` answers `113,"Undefined
header"` on a 784D at v7.4e. Nor is `TRIGGER:MAIN:LEVEL 0` a safe
default for a clock, which is as likely to be a 0 to 5 V logic swing as
one sitting about zero.

So the level is measured: display the clock's channel, read the record,
take the midpoint of its extremes, write that as the level, and put the
channel's trace back the way it was found. Measured on a 784D against
a generator's 2 Vpp clock, that gives **-2 mV** - which is what a
symmetrical clock should give, and would have been 2.5 V for a TTL one.

`DISPLAY:MODE INSTAVU` is the same shape of problem the other way up:
an instrument without DPO **takes the command and stays where it was**
rather than refusing it, so the only way to find out whether it has DPO
is to ask for it and read the mode back.

### An eye needs a clock, and this generation will not fake one

Triggering on the data gives **half an eye**, and it is worth being
clear why. Trigger on a rising edge and the bit after the trigger is
always high and the bit before is always low, so the middle of the
screen only ever shows the top rail. There is no lower rail and no
diamond - and no amount of persistence or DPO fixes it, because every
acquisition is the same way up.

TTiP said so all along. Every template in its manual gives the signal
requirements as **"the input *and trigger* signals"**, and it ships
`CLOCK.BIN` beside `50OHMEYE.BIN`. Data on CH1, clock into another
input, trigger on the clock: then every bit boundary is a trigger
whatever the data does, ones and zeroes both land in the middle, and
the rails close into a diamond.

Two ways round it without a second signal were tried on a 784D and
both fail:

- **Turning the trigger slope over half way through the accumulation**
  does not overlay rising and falling triggers. Changing the trigger
  *clears* what DPO has built: the waveform count went 48,175 and then
  restarted at 47,967 rather than reaching ninety-odd thousand.
- **A delayed sweep 2.5 bits out**, where the data is random again,
  gives both rails but not an eye. A 64-bit pattern has only about
  thirty rising edges, so what accumulates is thirty particular bit
  sequences - discrete traces with gaps between them, not a
  distribution.

`TRIGGER:MAIN:EDGE:SLOPE` takes `RISE` and `FALL` and nothing else;
`EITHER`, `BOTH` and `ANY` are all syntax errors. `SOURCE` does take
`CH<x>`, `AUXILIARY` and `LINE`, and a channel triggers perfectly well
with `SELECT:CH<x> OFF`, so the clock need not be on the screen.

#### The clock and the data have to divide exactly

Both come out of the same generator and share its reference, but the
clock frequency and the pattern frequency are two separate settings,
each rounded on its own. Ask for a 666.667 ns bit and the clock lands
on 1,499,999 Hz while the 64-bit pattern lands on 23,437.49 Hz - half a
part per million apart, which walks the data past the clock and smears
the eye clean across the mask. Measured: **2.9 million hits on a signal
that was not moving at all.**

Written as one over the rate, with the rate divisible by the pattern
length, both numbers are exact - 10 Mbit/s over 64 is 156,250 Hz,
1.5 Mbit/s is 23,437.5 Hz - and the eye stands still. Zero hits over
269,500 acquisitions on the same signal.

### There is no mask file, and no mask in a saved setup

There is no `SAVE:MASK` and no `RECALL:MASK`. A 784D's disk has 113
files and not one of them is a mask: the mask-testing applications
(`TDSCEM1`, `TDSEYE1`, `TDSDDM1`) are Java and keep their definitions
inside their own `.JAR`s. So this generation has no on-disk mask format
to adopt.

Nor does a saved setup carry one. With a mask in segment 1 and
`MASK:STANDARD USERMASK`, `SAVE:SETUP "hd0:/x.SET"` writes 4618 bytes
containing no occurrence of the word MASK and none of the coordinates -
searched as text in four formats and as IEEE floats at 32 and 64 bits in
both byte orders. So a setup file is not a way round the next problem
either.

### Every X read back as the same number, and why

For a long time, writing a mask and reading it back gave **every X as
54.98** while every Y survived exactly. It was tried with X rising, with
two points, with both X equal, with a closed box, and with
`MASK:STANDARD` set to `USERMASK` first: the same constant every time.
This program was built to keep its own copy of every mask rather than
trust one read back.

**It was never a firmware fault.** From a factory setup the coordinates
round-trip exactly - a box at 25,25 to 75,75 reads back 25,25 to 75,75
and draws on precisely the middle half of the graticule, photographed to
be sure. Three separate things had been in the way:

1. **`MASK:STANDARD` was being written.** It "deletes the existing mask
   and sets the standard mask", so setting it *after* the points threw
   them away and left an undefined mask, which is what answers with a
   constant. There is no `USERMASK` argument - the list is the telecom
   standards. **A user mask is points with the standard left alone.**
2. **Stale instrument state.** Before the reset, X came back offset by
   -25 and clamped at about +25. After `FACTORY` it is exact. What
   exactly was stale was not isolated - the record length was 5000
   before and 500 after, which is the strongest suspect, but the reset
   cleared several things at once and this is not settled.
3. **Y runs the other way**, which made every mask this program sent
   upside down. See below.

### Masks are Option 2C

`*OPT?` on the 784D includes `2C:comm`; the 784C and 640A do not have
it. Without it the MASK subsystem accepts commands, answers queries and
draws nothing at all - which is exactly what a missing option looks
like from the bus, and worth checking before concluding anything about
firmware. The user manual is explicit: "Mask Testing (Option 2C Only)".

### The origin is the upper left

"The upper left is 0,0 and the lower right is 100,100" - so the
instrument's Y counts *down* from the top while a mask in this program
is percent *up* the graticule. Confirmed by drawing a deliberately
asymmetric shape: sent as 90 and 70 it lands along the bottom, which is
10 to 30 percent up. Anything sent without the flip is drawn upside
down, and a symmetric test box cannot tell you so.

Two more rules from the same page, both matching what the i-Pattern
files turned out to hold: "the order of the pairs has no effect on the
mask created", and the boundary is generated by connecting the top-left
point to the bottom-right - everything below that imaginary line is the
bottom edge, everything else the top.

### An eye diagram, and counting against a mask

The instrument will build an eye itself, given the right trigger:
`TRIGGER:MAIN:TYPE COMMUNICATION` with `CODE NRZ` and
`NRZ:PULSEFORM EYEDIAGRAM` - Option 2C again. It recovers the bit clock,
so each acquisition starts at the same point in a bit while the data
either side varies. An edge trigger on the data cannot do this: it locks
to one bit and persistence simply redraws the same shape. A **custom
bit rate** is accepted - `BITRATE 1e6` sticks, though `STANDARD` then
reads `OC1` - which matters because every NRZ standard starts at
51.84 Mb/s.

DPO is `DISPLAY:MODE INSTAVU` (the menus call it DPO, the SCPI kept the
old name), with `DISPLAY:INSTAVU:STYLE` and `:PERSISTENCE` taking
`NOPERSIST`, `VARPERSIST` and `INFPERSIST`. Infinite persistence keeps
whatever was already on screen, so it has to be cleared by passing
through `NOPERSIST` first - otherwise the previous run's picture is
still there and looks like a result.

`MASK:TBPOSITION` is documented as the way to align a waveform to a
mask, and it is the wrong lever here: it shifts the waveform against
the eye trigger, saturates the DPO array and destroys the eye. Moving
`HORIZONTAL:TRIGGER:POSITION` to the left edge is what works - with one
bit across the graticule the crossings land on both edges and the
opening falls in the middle, which is where a mask's hexagon is.

Mask counting is `MASK:COUNT:STATE ON`, zeroed with `MASK:COUNT RESET`,
read with `MASK:COUNT:TOTAL?` and `MASK:MASK<n>:COUNT?` per segment.
Measured with TTiP's OC1 eye mask over a 1 Mb/s PRBS-7 eye at
200 mV/div, winding the generator down:

| Vpp | waveforms | seg 1 (hexagon) | seg 2 (top bar) | seg 3 (bottom) |
|---:|---:|---:|---:|---:|
| 1.00 | 116 424 | 0 | 0 | 0 |
| 0.80 | 116 424 | 0 | 0 | 0 |
| 0.70 | 116 578 | 0 | 0 | 0 |
| 0.60 | 116 424 | 3 243 318 | 0 | 0 |
| 0.50 | 116 424 | 19 625 495 | 0 | 0 |
| 0.40 | 116 424 | 23 407 827 | 0 | 0 |

OC1's hexagon spans y 32.3-67.7. At 200 mV/div, 0.6 Vpp puts the levels
at 31.25% and 68.75% - just inside it - and that is the first row with
hits, while 0.7 Vpp is clean. The mask arithmetic here and the
instrument's own rendering agree to better than a division. Segments 2
and 3 stay at zero throughout, which is the right answer: shrinking the
amplitude moves the levels away from the bars.

About 116,000 waveforms in each three-second window, or 39,000
acquisitions a second, which is what DPO is for.

### DPO hands back no waveform record at all

The price of those 39,000 acquisitions a second is that there is no
record to read. In DPO the instrument accumulates into a pixel
database - the display *is* the acquisition - and `CURVE?` still
answers, politely and instantly, with a record of nothing. Measured on
the 784D, same channel, same signal, only `DISPLAY:MODE` changed:

```
NORMAL   plain get 500 points, raw   8..245
INSTAVU  plain get 500 points, raw 128..128   <- flat, no data
```

Every sample of every channel reads back as mid-scale. It is not a
truncation or a timing race: the transfer succeeds, the preamble is
correct, the byte count is right, and the numbers are uniformly the
zero level. Freezing the acquisition first makes no difference - the
same 128s come back through our capture path and through a bare
`CURVE?`. `DATA:WIDTH 2` gives the 16-bit equivalent.

So no change to how we ask can fix it, and the app should not pretend
otherwise: `TdsWfm.capture()` checks `DISPLAY:MODE?` and raises
`NotReadable` rather than plotting an empty graticule that looks like a
flat signal. The check is bounded and remembered, because A and B
series firmware has no `DISPLAY:MODE` at all and simply times out - one
failed query and we never ask again.

What it must not do is turn DPO off to get its record. The
accumulation on screen is the run - a long eye, or a rare event
somebody has been waiting on - and `DISPLAY:MODE NORMAL` discards it in
one write, with nothing to put it back. So the refusal says what is
wrong and leaves the choice on the bench: switch DPO off there and
capture again, or take the Screen tab's screenshot, which photographs
the display exactly as it stands and needs no waveform record at all.
That last is the honest answer for a DPO picture anyway - the whole
point of the mode is a density no single record carries.

Two related observations from the same session. DPO quietly drops math
from the displayed waveform list (`NORMAL: CH1, CH2, MATH1, REF1`
becomes `INSTAVU: CH1, CH2, REF1`), which matches the manual's
Incompatible Modes table on page 3-66 - math waveforms, records over
500 samples, Envelope/Average/Hi Res, Single Acquisition Sequence,
FastFrame, Zoom and limit testing are all disabled while DPO is on.
Mask testing and mask counting are *not* in that table and work
normally in DPO, which is the whole point of the eye work above.

### Where the graticule is in a hardcopy

Measured on the 784D rather than assumed, because the mask editor draws
a screenshot behind a mask and the two graticules have to be the same
graticule.

A hardcopy arrives **on its side**, 480 x 640, whatever `HARDCOPY:LAYOUT`
is set to - asking for LANDSCAPE changed nothing. Turned three quarters
anticlockwise it is the 640 x 480 screen somebody looks at, with the
readouts along the bottom and the date at the lower right.

In that upright picture the graticule's own border is at:

```
left 24, top 34, right 524, bottom 434
```

which is 501 x 401 pixels counting both borders - **exactly 50 pixels a
division**, ten across and eight down, in both axes. That last number is
the check that the rectangle is right rather than approximately right.

The crop has to keep both borders. Cropping between them, the obvious
way, drops the far two and shifts everything by a pixel against the mask
drawn over it: the giveaway is that the top and left edges of the crop
come out solid ink and the bottom and right come out at eight per cent.

A DPO screen is worth capturing this way and a normal one is not, which
is the whole point of it: in DPO the picture carries the intensity
grading - blue through green and orange to red, by how often the trace
visited that pixel - and no `CURVE?` can express that even when it
works. A 784D screen in DPO came back with 19 colours in it.

## `.WFM` on the instrument's disk is not ISF

`SAVE:WAVEFORM CH1,"fd0:/NAME.WFM"` is accepted and writes a file, but
what it writes is Tektronix's own binary format, not the ISF this
program reads:

```
"LLWFM " then an IEEE block: #5 10198 ...
```

Measured on a 784D: 5000 points came to 10,211 bytes and 500 points to
1,210, which puts the header and trailer at 198 bytes between them and
the samples at two bytes each - twice the resolution of what `CURVE?`
hands over at `DATA:WIDTH 1`. The section below has the layout.

Two experiments worth recording because of what they rule out. Saving the
same *stopped* record at two vertical scales, and at two vertical
positions, produced files that were identical byte for byte - so the file
holds the acquired record and is not affected by how it is being
displayed afterwards. An earlier comparison of two files taken while the
scope was running showed three thousand bytes differing, all of it the
trace moving between captures rather than anything to do with the setting
under test.

## Reading the instrument's own `.WFM`

Worked out on the bench and checked against Tektronix's CNVRTWFM, a 1995
utility that converts these files. It is a 16-bit DOS program and will
not run on anything current, but it runs perfectly well under DOSBox,
which makes it a reference implementation to check against rather than a
memory of one.

The published Tektronix waveform format document (077-0220) describes the
5000/6000/7000 and DPO/MSO format, which begins with a byte-order mark
and `:WFM#`. That is **not** this format. A TDS500/600/700 writes:

```
"LLWFM " then an IEEE block:  #5 10198 <payload>

payload:  132 bytes header
           32 bytes precharge - 16 samples, acquired, before the record
    2 x count bytes the record, big-endian signed
           32 bytes postcharge - 16 samples, after the record
            2 bytes checksum
```

The precharge and postcharge are real acquired samples, not padding, and
are what the later Tektronix format calls by those names. They were what
made the header look 164 bytes long and the trailer 34: a "header" that
changed for no reason is usually not a header.

The whole header, all big-endian. Every offset below was settled by
feeding CNVRTWFM an ISF with one preamble field changed and diffing the
`.WFM` it wrote, so each is the only byte range that moved:

```
  0   uint32   0
  4   int32    payload length, itself included, less the 10 bytes in
               front of it
  8   uint32   0            the checksum covers from here
 12   uint32   0
 16   double   0.5 on every instrument-written file
 24   double   1.0
 32   double   1.0
 40   uint16   acquisition mode   285 Sample, 4 Average, 187 Envelope,
                                  3 Hi Res, 2 Peak Detect
 42   uint16   point format       98 Y, 97 ENV          (PT_FMT)
 44   double   record duration, seconds  (= XINCR x count)
 52   uint16   input coupling     565 DC, 566 AC, 25 GND
 54   uint16   horizontal unit    610 s, 736 Hz         (XUNIT)
 56   double   seconds per sample                       (XINCR)
 64   uint16   vertical unit      609 Volts, 632 VV, 740 dB   (YUNIT)
 66   double   volts at the offset position             (YZERO)
 74   double   vertical offset, in divisions (x 6400 = YOFF)
 82   double   volts per division            (/ 6400 = YMULT)
 90   int32    number of samples                        (NR_PT)
 94   uint16   trigger position, whole percent of the record
 96   uint16   98
 98   uint16   50
100   uint16   number of samples again
102   uint16   source   107 CH1 .. 110 CH4, 111 MATH1 .. 113 MATH3,
                        114 REF1 .. 117 REF4
104   uint16   2
106   uint16   1
108   uint16   525
110   double   6.3556e-05
118   uint16   936
120   12 bytes not known; see below
```

The enumerations are the instrument's own string-table numbers, which is
why they are not consecutive - 565 and 566 are adjacent because "DC" and
"AC" are adjacent in that table, and 285 for Sample is nowhere near
either. 525 lines and 63.556 us are the NTSC frame and line, so the
constants at 108 and 110 are most likely the video trigger's, left at
their defaults on every file measured.

A sample spans 10.24 divisions at either width, so a division is 6400
counts at 16 bits and 25 at 8. CNVRTWFM states YOFF in 8-bit counts even
when it writes a 16-bit file; the instrument's own `WFMPRE:YOFF?` at
`DATA:WIDTH 2` agrees with the 16-bit figure, so the converter is the
odd one out and this program follows the instrument.

`PT_OFF` used to look like half the record because it is stored as a
whole percent and every file measured was taken with the trigger at 50.
`XZERO` really is absent: CNVRTWFM does not emit one either.

### The twelve bytes at 120

Not known, and not load-bearing. On 784D files they read as a small
number 1 to 5, a zero, a word that changes from capture to capture with
no pattern, `0xAA00`, and 1. The Tektronix TTiP template files have
sample-looking values there instead, identical in two files whose curves
are completely different, and both kinds recall correctly on an
instrument. Files this program writes carry what the template had.

### Writing one

Two fields a reader can ignore have to be right, because the instrument
checks them and says nothing useful when they are not:

```
offset 4    int32    payload length, itself included, less the 10
                     bytes in front of it
last 2      uint16   sum of every 16-bit word from offset 8 to the
                     byte before the checksum, truncated to 16 bits
```

A curve that has no precharge or postcharge of its own - anything that
came from an ISF or a CSV - is given the nearest real sample repeated
sixteen times at each end, which is what the display interpolator would
draw there anyway.

The coupling and the acquisition mode are in no ISF field: they are in
the `WFID` text, which is where the instrument itself puts them and
where CNVRTWFM reads them from. A field nothing names is left as the
template had it rather than zeroed, since a value some instrument chose
beats a zero.

The rest of the header is copied from a file the instrument wrote and
patched over.

An eight-bit capture has to be widened, because the format holds two
bytes a sample. Multiplying the samples by 256 means dividing the volts
a count by 256 to keep the same voltages - so volts per division is the
eight-bit `YMULT` times the **25** counts in a division at that width,
and not that times 256 again. Getting this wrong makes CNVRTWFM report a
`YMULT` 256 times too large, which is how it was caught.

### How it was checked

Both directions, against CNVRTWFM under DOSBox.

**Reading.** Fifteen files: six from a 784D across three record lengths,
three timebases, three vertical scales and two positions; four more at
four vertical positions; one from a 640A two firmware generations
earlier; and two Tektronix TTiP templates, which are Envelope rather
than Y and name a source the converter does not recognise either. Every
field this program reads out of all fifteen - samples, `XINCR`,
`PT_OFF`, `YMULT`, `YZERO`, `YOFF`, both units, the point format and the
whole reconstructed `WFID`, coupling and acquisition mode included -
matches what the converter says, byte for byte on the curve and to
floating-point noise on the numbers. The one exception is `YOFF`, where
the converter's own eight-bit scaling is the disagreement.

**Naming the fields.** Sixty synthetic ISFs, each differing from a base
file in exactly one preamble field or one word of the `WFID`, converted
with `cnvrtwfm -w` and diffed against the base's `.WFM`. A field is only
recorded above where the bytes that moved are the ones that field owns
and nothing else moved.

Where the record starts was settled the same way rather than by counting:
the converter's own curve was searched for inside the payload, and it
begins at byte 164 in all fifteen files.

Two experiments that produced nothing are worth recording, because both
looked promising. Saving the same *stopped* record at two vertical scales
gives byte-identical files - the file holds the acquired record and is
not touched by how it is displayed afterwards. And comparing two files
taken while the scope was running showed three thousand bytes differing,
all of it the trace moving between captures rather than the setting under
test. A field only moves when the thing it describes is re-acquired.

**And on the instrument.** A file built here from nothing - 1000 points
of a shape no acquisition would give, 40 ns a sample, the trigger at
20 percent, 200 mV a division, `YZERO` 50 mV, AC coupling, Average mode
- written to `hd0:` with `WRITEFILE`, recalled with
`RECALL:WAVEFORM "hd0:/TDSTKTST.WFM",REF3`, and read straight back off
the bus. A 784D at v7.4e reports every one of those numbers back, the
curve byte for byte, and a `WFID` reading

```
"Ref3, AC coupling, 200.0mVolts/div, 2.000us/div, 1000 points, Average mode"
```

The coupling, the mode and the 2 us a division are in no ISF field and
no `CURVE?`; the instrument can only have got them out of the header
this program wrote. It names the reference the waveform is now in rather
than the source the file claimed, which is right. `scratch/_wfmbench.py`
runs it.

**Writing.** All fifteen read in and written straight back out. Every
one is the same length as the original, is accepted by the converter,
and reads back with every field intact. The only field lost is the
source on the two TTiP templates, whose code the format's own converter
calls Unknown as well and which come back as CH1. Files this program
wrote in the first place round-trip byte for byte identical; files the
instrument wrote differ in the last digits of a float or two, the twelve
unknown bytes, and the precharge and postcharge, which are acquired
samples and are replaced by a repeat.


## Fifty points to a division, whatever the record length

A TDS draws fifty samples to one horizontal division and no more, so ten
divisions is always 500 points. A longer record does not squeeze more
into the screen: it extends past both edges, and the rest is reached by
panning.

So the timebase the instrument is set to comes from the sample interval
alone:

```
seconds per division = XINCR x 50
```

and *not* from the record divided by ten. Measured on a 784D holding
5000 points at 20 ns a sample: its own `WFID` says `1.00us/div`, which is
`XINCR x 50` exactly, while the record divided by ten is ten times that.

`WFID` is worth knowing about for this. The instrument writes its own
settings out in words - `"Ch1, DC coupling, 100.0mVolts/div, 500.0us/div,
500 points, Sample mode"` - which is a second, independent statement of
what `YMULT` and `XINCR` mean. It is the only thing on the bus that can
check the arithmetic without repeating it, and it is what caught the
fifty-points rule.

Vertically the same record spans 10.24 divisions at either width, so a
division is 25 counts at 8 bits and 6400 at 16, and

```
volts per division = YMULT x counts per division
```

which `WFID` confirms on all three instruments here.


## The colours the instrument draws in

A colour TDS keeps four palettes, says which is in use, and will hand
over all of them at once:

```
DISPLAY:COLOR?          with HEADER ON

:DISPLAY:COLOR:PALETTE:REGULAR NORMAL;PERSISTENCE TEMPERATURE;
 NORMAL:BACKGROUND 0,0,0;CH1 0,65,0;CH2 252,48,48;...;
:DISPLAY:COLOR:PALETTE:BOLD:BACKGROUND 0,0,0;CH1 250,39,100;...
:DISPLAY:COLOR:MAP:REF1:BYCONTENTS 0;TO REF;
```

One query for the lot, which matters: individual sub-queries like
`DISPLAY:COLOR:MAP:CH1?` are not commands and simply time out, at 45
seconds each. A monochrome instrument has no such subsystem and answers
nothing at all, so the query needs a short timeout of its own and the
answer - including the empty one - is worth remembering for the session.

Reading the reply needs SCPI's compound headers to be honoured. After
`:DISPLAY:COLOR:PALETTE:NORMAL:BACKGROUND 0,0,0`, a bare `CH1 0,65,0`
means `DISPLAY:COLOR:PALETTE:NORMAL:CH1`. The rule is that each
element's path minus its last node is the prefix for the next one, and
reading it any other way gets the wrong colour rather than no colour.

`CH1`-`CH4` have entries of their own. A reference or a maths trace does
not: `DISPLAY:COLOR:MAP:REF2:TO` names the entry it uses, usually `REF`
but it can be made to name a channel.

### The triple is hue, lightness, saturation - and the hue is offset

Each entry is three numbers. They are hue in degrees, then **lightness**,
then saturation, both as percentages - the hardcopy palette settles it,
where the background is `0,100,0` and is white.

**Tektronix's hue zero is 120 degrees from where a textbook HSL starts.**
Measured against the instrument's own screen: a 784D reports `REF` as
`44,39,72`, which by the usual reckoning is an orange, `#ab851c` - and a
colour screenshot of that same instrument shows its REF trace as
`#851cac`, a purple. Those are the same three bytes rotated, which is
exactly what a 120 degree shift does. With the offset applied, three
independent entries land within four counts of what the screen shows:

```
                reported        converted    on screen
REF             44,39,72        #851cab      #851cac
GRATICULE       165,50,15       #93896c      #92886e
TEXT            165,50,35       #ac9653      #ac9454
CH1             0,65,0          #a6a6a6      #a6a6a6
BACKGROUND      0,0,0           #000000      #000000
```

Worth dwelling on how nearly this shipped wrong. The entries that
settled the field order - white, black, grey - are all unsaturated, and
an unsaturated colour has no hue to get wrong. Every check passed and
the conversion was still 120 degrees out. It took a coloured entry and a
photograph of the screen to see it.


## A monochrome hardcopy is ink on paper

The Screen tab offers Normal and Inverted for an instrument with no
palette of its own, and which way round they go was worth measuring
rather than assuming. A TIFF hardcopy off a 640A is **84% white**, with
the trace and graticule in black.

So what arrives is the printing one, and inverting it is what gives the
white-on-black the screen actually shows. The labels were the other way
round until this was measured.


## LANDSCAPE turns the screen anticlockwise

`HARDCOPY:LAYOUT` decides whether the screen is drawn upright on the page
or on its side, and the words are the printer's rather than the image's:
PORTRAIT gives the screen the right way up at 640 x 480, LANDSCAPE gives
480 x 640.

Which way it turns was worth measuring, because the program turns a
picture it already has rather than spending another five seconds asking
for one, and turning it the wrong way is a picture upside down. A 784D
captured both ways puts "Tek Run: 50.0MS/s" down the **left**-hand edge
of the landscape image, reading upwards - which is where a quarter turn
anticlockwise puts what was the top-left corner.

The two captures never match exactly, so this cannot be settled by
comparing them for equality: they are seconds apart and the trace is
live. What settles it is which way round agrees *more*, since the menus,
the graticule and the readouts do not move between captures and are most
of the picture:

```
turned anticlockwise here   99.6% of pixels agree with the instrument
turned clockwise            83.2%
```

The remaining 0.4% is the trace moving between the two captures.


## Where a channel marker belongs

The instrument draws a marker for each displayed trace at the left-hand
edge of the graticule, level with that channel's **zero volts** - not with the middle of the graticule, which is the same place
only when the channel happens to sit at position zero.

Zero volts for a trace comes out of the preamble by turning the scaling
round:

```
volts = (raw - YOFF) * YMULT + YZERO
raw at 0 V = YOFF - YZERO / YMULT
```

and `YOFF` moves with the vertical position, which is what makes the
marker follow the trace up and down the screen. See the note on reading
`.WFM` for the measurement that settled what `YOFF` is.

Four channels all at position zero put four markers in the same place.
The instrument's own answer is to draw them on top of one another; this
program puts them outside the graticule, where they cover no signal, and
steps any that collide further out - which keeps every one against its
own zero volts and still lets all four be read.


## `FREESPACE?` answers about wherever you are standing

`FILESYSTEM:FREESPACE?` reports the current working directory's volume,
and there is no form of it that takes a volume as an argument. So there
is no way to ask "how much room is left on the floppy" while standing on
the hard disk; the only route is to change directory and ask again.

That is free if it is done at the right moment. The volume probe already
enters each candidate in turn to find out whether it exists - see
"Volumes have to be probed" - so reading the free space while it is
standing there costs one extra query a volume and no extra directory
changes at all.

Afterwards each listing updates the figure for the volume it listed, and
the others keep the number they last gave. Stale by minutes, but true
when it was read, which beats reporting nothing about a disk the user
can see in the tree.


## What is worth protecting, and where

The delete guard exists for one reason: the instrument's own
applications and the Java runtime they need live on the **hard disk**,
and removing one of them leaves a scope that no longer runs its
software. `hd0:/APP/TDSRTE1` is the runtime; `STARTUP.BAT` and the
`.APP` launchers are the rest of it.

A floppy is a different matter. Nothing the instrument needs lives
there - it holds whatever the user put on it - so nothing on `fd0:` is
protected, and the whole disk can be emptied.

The one exception on any volume is a phantom entry: the two-character
name ending in a dot that a VFAT long-name record reads as. Deleting one
strips the long name off the real file that follows it in the directory
table. They never reach the UI, and a delete aimed at one is refused
wherever it lives.
