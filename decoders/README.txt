Protocol decoders
=================

Each file here is one protocol. The Waveform tab lists them automatically,
so adding a protocol is one file and nothing else changes.

A decode runs entirely on a captured waveform - a channel you have read, or
a stored reference loaded from a file. Nothing here touches the instrument,
which is the point: it works on any TDS, including the ones whose firmware
cannot run a bus-decode application of their own.

Writing one
-----------

1. Copy _template.py to <name>.py (lower case, no spaces).
2. Rename the class and set NAME, DESCRIPTION, SOURCES and PARAMS.
3. Write decode(signals, params) -> (frames, note).
4. Override columns(), row() and label() to show your protocol's fields.

The whole contract is the Decoder base class in tds_decode.py. Read a decoder
beside the template - one is close to whatever you are adding: uart.py (async
serial, one wire), i2c.py (two wires walked together), spi.py (a clock and
data), can.py (bit stuffing and a CRC), onewire.py (time-slot bits), pwm.py
(just edges), arinc429.py (bipolar, two thresholds).

For anything in the async-serial (UART) family - RS-232/485, MODBUS, MIDI,
LIN, XBee, DMX512 - do not re-implement bit sampling: call the shared,
corpus-tested tds_decode.serial_bytes(sig, baud, framing, inverted) and build
your framing on the byte stream it returns. See lin.py and modbus.py.

SOURCES is how many waveforms the protocol needs and what they are called.
One role ("Line") is a one-wire protocol. Two or more ("SDA", "SCL") each
get a captured source mapped to them in the tab. A trailing "?" ("MISO?",
"CS?") marks an optional role that may be left unmapped.

PARAMS is a list of settings; the tab renders a control for each, so a new
protocol adds its own options without any UI code. A 'choice' lists
(value, label) pairs; an 'int' takes lo/hi/unit; a 'bool' is a checkbox.

Where they live
---------------

The decoders that ship with the program live in this folder inside the
install. A writable copy is kept beside your settings (the "decoders" folder
next to tdstoolkit.json), topped up from here every run - exactly like the
masks library. A decoder added in an update is copied across without
overwriting one you wrote, and a decoder you delete comes back next run (the
bundled set is meant to be present). Drop a new <name>.py into that folder and
it appears in the list the next time the tab is opened; no rebuild needed.

A file that will not import is skipped with a note in the log rather than
taking the list down with it, so a half-finished decoder never hides the
working ones. Files whose name starts with "_" are helpers and never listed,
which is how _template.py stays a template.

Colour: a decoder owns its chevron colours - the app has no colour setting for
decoders. Set COLOUR for one colour for the whole protocol and FAULT_COLOUR for
a faulted frame (both default to readable values), or override colour(frame) to
paint a colour per chevron: switch on frame.kind so a line's address, data and
framing each read in their own colour.

Testing
-------

The RS-232 decoder is checked against the real capture corpus in
../../TDS Protocol Decoders/corpus; every other decoder is checked against a
synthetic vector in tdstest.py, each with a matching mutation bite in
bitecheck.py. Add a case for a protocol you add - a decode that is not tested
is a decode that will drift.

Having Claude write one
-----------------------

There is a skill, "tds-decoder-plugin", that teaches a Claude client the whole
contract so it can build a decoder from a description of the protocol. Point it
at this folder and _template.py.
