# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **A restore onto a freshly formatted disk was refused.** An empty root is
  how a wedged file system shows itself on these instruments, so the
  check before writing refused every empty volume, including one just
  formatted. A volume this program has formatted and then seen empty is
  now believed, until something is written to it. A disk blanked
  elsewhere is still refused, and the message now says to format it from
  the Files tab first.
- **A backup kept only the top 8 bits of each reference.** References were
  read one byte per point, but one saved from an averaged channel holds a
  real low byte: on a 784D, 2,497 of 2,500 points had one. References are
  now read 16 bits wide, and a restore puts back exactly what was saved.
- **Starting a mask measurement on a level mask reported a bug in the
  program.** The handler for the count's result carried a stray copy of the
  limit-template handler, which asked the result for fields it never has.
  Every successful count ended in a "bug in this program" box. The stray
  copy is gone.
- **Backspace in the Files tab's address bar jumped up a folder** and
  overwrote what was being typed; Delete deleted the selected file. Both
  keys now act only when the folder tree or the file list has the focus.
- **A timeout on the instrument was not caught where the program expected
  it.** Eight places caught `IOError` round a VISA query, which the VISA
  library does not raise. The worst was the signal path compensation: a
  `*CAL?` that outlasted its 25 minutes was meant to clear the bus so its
  late answer could not be read as the next reply, and did not. The System
  tab also failed whole on one unanswered field, and a calibration restore
  stopped mid-block. All eight now catch the VISA error too.
- **A failed download left the file's tail in the bus,** so the next
  command in a batch read it as its own reply and every remaining file was
  reported missing. The session is now cleared on a failed read, as it
  already was on a failed write. The same clear follows an `*OPC?` that
  times out after a slow delete or write.
- **Firmware: the on-instrument helper is set up before the flash is
  erased,** not after. On a part with no page buffer, a helper that would
  not go was refused with a message saying nothing had begun - with the
  flash already blank. The refusal now comes while the flash is still whole.
- **The Backup tab's survey raised "The instrument reported something" on
  a scope with an empty floppy.** Probing the volumes is excused those
  events under its own name; the survey ran it under another.
- Changing the language after the Masks tab's **Save setup** dialog had
  been opened and closed raised a fault; the dialog's labels were
  registered for relabelling and were gone.
- A bus scan that failed left the **Scan** button saying Cancel.
- One timeout on the mask counter query switched mask counting off for
  every instrument until the program was restarted.
- A hand-edited mask file naming segment 999999999 made the program
  allocate until it died; segment numbers are now held to 1-8. A setup
  file using `;:` to return to the root (as a `*LRN?` dump does) now reads
  correctly. A zip entry carrying a drive letter is refused on restore. A
  settings value of `1e400` no longer stops the program starting.
- **A calibration restore now warns when the file came from a different
  model.** The constants are laid out per model, and writing a TDS 540's
  set into a TDS 744A would leave the scope miscalibrated. The program
  names both models and asks before going on, and still allows it, since a
  file renamed by hand is the usual cause.
- **An NVRAM backup is the whole window.** The backup used to sample three
  512-byte stretches of the window to decide how much to keep, and a region
  that held data between the samples was left out of the image. The whole
  window is read and written every time.
- **Four jobs that run while the program is otherwise busy** - a limit
  test, a setup send, a live mask count and the clock read - no longer clear
  the busy state of the job they interrupted, so a long transfer's Cancel
  button stays. A failed limit-test picture or build also returns its
  buttons.
- **Reconnecting after an instrument swap** forgets the previous scope's
  state: what it could not do, its System tab snapshot, its limit-test
  references and the firmware monitor are all cleared, so the new scope is
  not shown the old one's answers.
- **A renamed upload that the scope had not yet listed** was reported as
  lost; the listing is now read again after two seconds before the program
  gives up on it. Upload marks are kept per instrument (address and
  identity), so two scopes on the same address do not share them.
- **A firmware image is checked against the catalogue's SHA-256 before it
  is written,** so a wrong or corrupt file under a right name is refused
  while the flash is still whole.
- **Waiting for the instrument's output queue** could not be interrupted by
  a short timeout; the poll now runs with a one-second leash and restores
  the caller's timeout afterwards.
- **A CSV with comma decimals** (`1,5;2,5`, as a European spreadsheet
  writes) is now read when the columns are separated by semicolons or tabs;
  a line that does not yield two numbers says so instead of failing on
  the first comma.
- **A TIFF screen capture without its dimensions, a decoder plug-in calling
  `sys.exit()`, and a mask vertex printed with Python's shortest-repr**
  were each handled: the first now raises a clear error, the second is
  caught like any other plug-in fault, and mask points are written with ten
  significant digits as the scope writes them.
- **A tolerance or grid step typed with a decimal comma was refused.** The
  German, Spanish, French, Italian and Russian error message says to type
  `0,5`, which is how those languages write a half, and the program only
  took `0.5`. The Limits tolerances, the Masks grid step and a point typed
  into the mask editor now take either.
- **Listing a folder of more than about twenty files could leave the
  program busy for good.** Each file in a listing is told apart from a
  folder by trying to enter it, which the instrument answers with an event,
  and its event queue holds about twenty. A longer folder overflowed it,
  the overflow was reported as unexpected, and its warning box opened
  inside the loop that hands results back - so nothing after it finished
  until the box was found and closed. The queue is now emptied as the
  listing goes.
- **NVRAM restore refused every TDS 784C/D, blaming the protection
  switch.** The aliasing probe landed in the empty part of the window above
  the fitted memory, where a write cannot stick, and took that for the
  switch. The switch is now asked about once, at the bottom of the window.
- **Write firmware, Back up... and the System tab's buttons stayed live
  while a job ran.** A second press of Write queued a second erase behind
  the first; a Secure erase pressed during SPC ran after the window had
  already said idle. They are greyed like every other button, and Write
  needs a fresh Connect after each write.
- **A single-file Upload and Send to disk ignored the lock-out** that
  follows a failed transfer, and wrote into the filesystem the program had
  just declared wedged. **A backup restore that was refused** before
  writing anything said "Restored to the instrument".
- **Listing a folder that is no longer there showed the folder before it**,
  and a recursive delete, copy or download of it walked that folder again
  and again. It is now reported as missing.
- **A failed rewrite during a staged overwrite** deleted the only verified
  copy before checking the rewrite. **TEKSecure's own success** raised the
  "instrument reported something" warning. **A download or calibration
  backup that could not be saved** (locked file, read-only folder) was
  thrown away and reported as a bug; the program now asks where else to
  put it.
- **A mask file's SOURCE line could carry a SCPI command** to the
  instrument; it is now held to a source name. **One broken decoder
  plug-in** could empty the protocol list. **An image in a folder inside a
  zip** was listed but could not be written. **A setup line** read later
  commands under the first command's path (`EDGE:SOURCE CH2;SLOPE FALL`
  lost its slope) and took a channel switched off as the mask's source.
- **The Factory options dialog could write one instrument's options to
  another.** Left open while a different scope was connected, Write sent
  the first scope's ticks to the second and switched off whatever the first
  did not have. It now refuses and says so.
- **The setup beside a mask was sent before the last question.** Answering
  No to "Send anyway?", or Cancel in the send dialog, still changed the
  instrument's timebase and channel. Nothing is sent until Send is pressed.
- **A multi-file delete carried on through failures.** It now stops after
  two in a row, as an upload does, and says how many were not attempted.
- **The "instrument reported something" warning held up the job behind
  it** until it was closed; it now opens once the result loop has had its
  turn. **A refused page after the erase on a part with no page buffer**
  was reported as a write that had not begun; it now says the flash has to
  be programmed again.
- Smaller: a malformed block length or a waveform file whose time step is
  NaN, infinite or zero is refused rather than loaded; an ISF save no
  longer fails on a non-ASCII name; a saved error log no longer ends its
  lines with a doubled carriage return on Windows; reconnecting forgets the previous
  instrument's limit test, sent mask and Backup survey; the instrument
  picker cannot reconnect under a running job; a folder listed twice no
  longer breaks the folder tree; the Files toolbar shows one tooltip per
  button; and some dead code is gone.
- **A CSV that would not load said why in English only.** The reason is
  now shown in the program's language, and the new comma-decimal advice
  is translated.

### Changed

- **Option 1G is offered on the TDS 520D, 540D, 724D and 754D as well as the
  TDS 540B.** Tektronix's manual (071-0130-03) lists the option for those four
  models, so the **Factory options** dialog no longer greys it on them. The user
  manuals say the same in all eight languages.
- **Internal tidying after a code review.** The worker's ten mix-in classes
  are one class; eight dialogs share one pop-up scaffold and the Masks and
  Limits tabs share one tool column; the toolbar icons are stored as
  pre-rendered PNGs rather than drawn pixel by pixel at start-up; the
  settings files are read and written through one pair of functions; the
  address argument is parsed by `argparse`; and dead code (an unused scroll
  helper, a redundant word reader, unreachable filters, and two
  calibration-restore messages for results the restore never produces)
  is gone. No behaviour changed; the icons are pixel-identical.
- **The Klingon translation is complete again.** 195 messages added in
  1.2.3 had been left in English.
- **Translations reviewed.** Every entry in the seven other languages was
  checked against the English. 96 messages and tooltips were
  corrected: wrong meanings (the Chinese told the reader a reboot was
  merely unnecessary where it must not happen; "percent of the graticule"
  read as percent of one division), terms used two ways within one
  language, control names quoted differently from the control, Russian
  plurals that broke with the number, and punctuation.
- **Some controls are relabelled in seven languages.** German: **Signalverlauf
  abrufen**, **Bildschirmfoto aufnehmen** (it read the same as its own tab)
  and **Wiederherstellen...**, which now matches the dialogs it opens.
  Italian: the System tab's clock reads **Ora** (it said "Tempo", a
  duration), and PASS reads **SUPERATO**. Russian **Фигура** and **Активные
  каналы**, Chinese **图形** and **吸附到网格**, and Japanese
  **波形を読み込む...**, with smaller fixes in Spanish and French. The user
  manuals quote the new labels.

## [1.2.3] - 2026-09-25

### Changed

- **The user manuals read more plainly.** The wording of both manuals was
  gone over in all eight languages, in the voice of a technical manual for
  the bench, without moving a page break. Every control name and message
  the translated manuals quote now matches what the program shows, and
  their screenshots were retaken from the corrected program.

### Fixed

- **More of the interface is translated.** Progress lines, error messages,
  several dialogs, the zoom buttons' tooltips and the messages from the
  lower-level modules (firmware, calibration, file transfer, screen capture)
  were shown in English whatever the language setting. They are now
  translated in all seven languages. A second copy of the program is still
  recognised whatever language the first one is in.
- **Corrected the seven translations.** Each catalogue was reviewed against
  the English: broken and truncated text (the calibration-restore warning
  in every language), mistranslations, messages that named a button by the
  wrong label, and inconsistent terms. Labels whose translation changed are
  matched in the translated user manuals.
- The PASS/FAIL stamp on the Masks and Limits plots is as wide as its
  word. It was a fixed 70 pixels, which cut longer verdicts such as
  BESTANDEN and CONFORME off at both ends.
- **Simplified Chinese is drawn in a Chinese font.** Windows filled in the
  characters from its first CJK font, a Japanese one on most systems, so
  Chinese came out in Japanese glyph shapes. The program now uses
  Microsoft YaHei UI for Chinese and Meiryo UI for Japanese.
- **Chinese and Japanese text breaks into lines properly.** Tk breaks a
  line only at a space, so a numbered step's "3." was left alone above its
  sentence and lines began with "。" or "，". Explanatory text in both
  languages is now broken between characters by the usual rules: no line
  starts with a closing mark or ends with an opening one, and Latin words
  and numbers are kept whole.
- **Nothing runs off the window at 1280 x 800 any more.** The row of boxes
  under the Masks and Limits editors wraps onto a second line when it runs
  out of room, instead of losing its last box. The Backup tab's left column
  is wider, so its NVRAM buttons are no longer cut off at the bottom. On the
  System tab, the notes wrap wider, so the Options button stays in view, and
  the labels beside the hardcopy settings are no longer cut short.
- A message about restoring NVRAM named a **Back up NVRAM...** button that
  does not exist; it now says **Back up...** in the NVRAM box.

## [1.2.2] - 2026-09-14

### Changed

- **Firmware images are identified by their contents, not by a made-up
  filename.** The app no longer reads a model or version from a
  `Model_vVer_Firmware.bin` filename - that shape was never a Tektronix
  convention. An image is identified by its SHA-256 against the catalogue
  (or is simply a file you point at); the instruments it suits come from
  `FIRMWARE-INDEX.txt`; and its version is read from the library filename,
  or, failing that, from the version string inside the image.

### Added

- **The manuals now ship in eight languages.** Both the user manual and the
  "Writing a Protocol Decoder" technical reference are available in English
  plus German, Spanish, French, Italian, Russian, Japanese and Simplified
  Chinese. Every edition is named with its two-letter language code, English
  (`(en)`) included.

## [1.2.1] - 2026-09-14

### Fixed

- **Completed the interface translations.** Fourteen strings were left in
  English in every non-English catalogue: the firmware backup feature (the
  **Back up firmware** button, its confirmation dialog, and its progress and
  result messages) and the decode and self-test tooltips. They are now
  translated in all seven languages - German, Spanish, French, Italian,
  Russian, Japanese and Simplified Chinese - so those interfaces no longer
  fall back to English.

## [1.2.0] - 2026-09-12

Adds host-side **protocol decode** to the Waveform tab: decoding a captured
waveform (a channel that was read, or a stored reference loaded from a file)
without touching the instrument, so it works on any TDS - including the ones
whose firmware cannot run a bus-decode application of their own.

### Added

- **Protocol decode.** A third list on the Waveform tab, autopopulated from a
  `decoders/` plugin folder the way the masks library is. Pick a protocol, map
  a captured source to each of its wires, and press Decode; the decoded events
  are marked as chevrons on the trace (a draggable band, named for its source)
  and listed in a table beside the controls. Strictly in-app - nothing is sent
  to the scope, which is the point: most base instruments cannot run the
  on-scope decoder.
- **Twenty-six decoders**, each a drop-in plugin: RS-232/UART, RS-422/485,
  I2C, SPI, Dual SPI, I2S, I3C, MODBUS RTU, MIDI, 1-Wire, CAN, LIN, XBee,
  CRSF, SBUS, DMX512, PWM, S/PDIF, IrDA SIR, ISO 7816, FlexRay, ARINC 429,
  MIL-STD-1553, WS2812/SK6812, APA102/DotStar and an IR-remote decoder.
  RS-232/UART is validated against a real capture corpus; the rest against
  synthetic vectors, each with a mutation bite. The bipolar and self-clocking
  ones (ARINC 429, MIL-STD-1553, S/PDIF, FlexRay) read two thresholds or a
  self-clock and should be checked against a real capture before being trusted
  on live gear.
- **Thirteen IR-remote protocols** in the one IR decoder: NEC, Extended NEC,
  Sony SIRC, Philips RC5, RC5X, RC6, RC-MM, Samsung, LG (28-bit), JVC,
  Panasonic/Kaseikyo, Denon/Sharp and Mitsubishi. Each is decoded to a
  documented spec and checked against a synthetic signal, so confirm a given
  remote against a bench capture before trusting it; the maker A/C protocols
  (Daikin, Fujitsu, Carrier/Toshiba) and the exotic ones (Bang & Olufsen,
  BoseWave, MagiQuest, Lego, Lutron) are not yet included.
- **A decoder-plugin contract** (`tds_decode.py`) with a `Decoder` base class, a
  shared async-serial core (`serial_bytes`) that the whole UART family reuses,
  and a transferable skill so a user can have their own Claude build them a
  decoder. Drop a `<name>.py` into the decoders folder
  and it appears in the list; a file that will not import is skipped with a log
  note rather than taking the list down.
- **Decoder-owned chevron colours.** A decoder sets its own colour (`COLOUR`
  for a clean frame, `FAULT_COLOUR` for a faulted one) and can override
  `colour(frame)` to paint a colour per chevron, so one line's address, data
  and framing each read in their own colour. There is no app colour setting for
  decoders; the plugin owns them.
- **TDS694C support.** The TDS694C v6.4e firmware image
  (`TDS_v6.4e_35e7aff8.bin`) is in `FIRMWARE-INDEX.txt` and the capabilities
  table, so the app recognises a connected TDS694C and offers it the right
  image. Its NVRAM checksum table was extracted from the image and confirmed
  5/5 against a real DS1486 dump, so a 694C NVRAM backup is verified like any
  other. No app code changed — the flash is identified by its silicon and
  channel count is read at runtime, so the 694C (a 4-channel C-series like the
  784C) is supported as data alone.
- **Firmware bundled with the app.** The TDS500/600/700 firmware library ships
  as `firmware/TDS-firmware-images.zip` (32 images, ~41 MB); the Firmware tab
  reads firmware straight from the zip.

### Changed

- **The decoders folder is topped up every run** (shared with the masks library
  via `tds_decode.sync_folder`), so a decoder added in an update appears without
  the user reseeding, while a plugin they wrote or edited is left alone.
- **The decode section, reworked.** Controls sit on the left, the results table
  on the right at one fixed width - the same for every decoder and when none is
  chosen, so the table no longer changes size or jumps when a protocol is
  picked. A decoder's source dropdowns sit on one line at equal size and its
  settings two to a row, so a four-wire, four-setting bus like SPI fits in
  about five lines and the waveform window keeps the rest of the height. The
  plot and the decode section are resized by dragging the plot's own bottom
  edge; the table is resized by dragging its left edge - both plain edges, no
  handle and no divider line.
- **The three tabs' left panes match.** The Waveform, Limits and Masks tabs now
  use the same fixed-width left column, which no longer resizes.
- **The zoom icons** were softened to the same grey as the mirror icons on the
  Masks and Limits tabs, so the two toolbars read as one set.

### Fixed

- **Async-serial framing-error recovery.** After a framing error the reader now
  re-synchronises to the next mark before hunting the next start bit, so one
  long dominant (a LIN or DMX break) no longer spawns a train of misaligned
  bytes. A clean stream never took this path, so the capture corpus is
  unaffected.
- **The SPI source boxes and settings** used to clip off the right edge of a
  fixed-width controls column (only the first source was reachable); the
  reworked layout gives them room, so all four wires and every setting are
  shown in full.
- **Every interactive control now has a tooltip.** A pass over the whole app
  filled the last few with none - the Back up firmware button, the decode
  buttons, the self-test picker and the hardcopy/RS-232 dropdowns.

## [1.1.1] - 2026-09-10

Hardens the file-transfer path against the mass-storage wedge (event 250,
"Mass storage error") that a whole-disk upload can provoke. Nothing here
changes what a healthy transfer does; it changes what happens around one that
is not.

### Added

- **Skip-unchanged uploads.** A file already on the instrument byte for byte is
  left alone rather than written again. Rewriting what is already there is the
  churn that takes this family's mass storage down, so a host-side manifest of
  what has been verified onto each instrument lets a repeat upload skip it.
- **Pre- and post-flight health checks on every upload.** The volume must list
  before a byte is written and again after; a root that will not list, or lists
  empty, is the wedged-filesystem signature, so the batch is refused rather than
  written into - and a run that verified its own files but left the root
  unreadable is reported as a failure however well the files themselves landed.

### Changed

- **An existing file is overwritten by staging.** The new copy is written to a
  temporary name, read back and verified, then renamed over the original, so a
  write that dies part way leaves the previous file intact instead of half
  over-written.
- **A transfer that wedges the file system locks the instrument out** of further
  writes until it has been power-cycled and reconnected - a retry alone will not
  get through, because writing again into a wedged file system is what loses a
  card. The reconnect clears the lock only if the volume lists normally again.
- **Only one copy of the program may hold an instrument at a time.** A second
  copy's command would otherwise land inside a file being written; it is now
  turned away, and a lock left by a copy that has since exited is taken over
  rather than blocking forever.

### Fixed

- **A directory listing can no longer answer about the wrong directory.** A
  change-directory that fails silently used to leave a listing pointing wherever
  the working directory still was; the working directory is now read back and a
  mismatch raises rather than handing back a listing of the wrong place.

## [1.1.0] - 2026-09-07

Adds whole-instrument backup and restore, a firmware catalogue, SVG waveform
export, and a large number of fixes across the file, calibration, NVRAM and
limit-test paths.

### Added

- **Backup tab** - captures the whole instrument to a single file and restores
  it: front-panel setups, the acquisition board's calibration constants, the
  NVRAM, and the stored references. Every step is written into the run report,
  and a step that fails says so.
- **Firmware tab** - catalogues the firmware folder by what each image
  contains, so an image is identified even when its filename does not name a
  model or when a folder holds an archive of images rather than loose files.
  An image can be named or loaded directly, a page loop runs on the instrument,
  and the accumulated knowledge lives in a new `FIRMWARE-INDEX.txt`.
- **Save a waveform as SVG** - a true vector export, alongside PNG, CSV, `.tdw`
  and the new `.wfm`.
- **Save a waveform as `.wfm`** as well as `.tdw` - the same bytes under either
  name, for tools that expect the `.wfm` extension.
- **Limits tab template library** - open Tektronix `.ENV` templates, build a
  "learn" template from a loaded trace, save templates as `.LIM`, and read what
  each of the four references holds.
- **Files tab: format a volume**, behind two confirmations; and **delete the
  instrument's own files** (Java runtime, boot chain, shipped applications)
  behind a second question, since every one of them loads back over GPIB.
- **Duplicate a file on the instrument** at around 129 times upload speed, and
  **drag whole folders** onto the file list.

### Changed

- **A capture is shown at the instrument's own horizontal timebase**, zoomed to
  the central scope window rather than stretched across the whole record.
- **The record-strip drag steps in whole microseconds a division**, matching the
  instrument's own timebase ladder.
- **The Limits tab is laid out like the Masks tab** - shared toolbar, a saved
  template library, a PASS/FAIL stamp on the plot, and a Clear button.
- Reads carry 1024 bytes and writes 512, matching each direction's limit; the
  upload-time estimate counts the two directions separately.
- Backup and restore report every step; closing the window mid-transfer, and
  connecting to an instrument that is mid-transfer, are both handled.
- Smaller touches: right-click the error log to copy, the report wraps instead
  of scrolling sideways, the System tab reads the clock each time it opens, and
  buttons grey out when there is nothing for them to act on.

### Fixed

- **Waveform PNG export drew the record strip as disconnected specks** instead
  of a connected trace.
- **An instrument set to 500 us a division was reported as 499.**
- **Calibration restore never stored anything** on any instrument, and reported
  success when it was cancelled, when it failed, and against a protected
  instrument.
- **NVRAM** - an all-zero dump was reported as valid; a restore wrote the
  aliased part of the window twice; the checksum is now read out of the
  firmware; and a backup stops if a byte moves outside the clock.
- **Firmware** - an image larger than the flash is refused rather than written;
  a flash that would not erase is no longer believed; and the version is read
  from the image, not from the compiler that built it.
- **Cancelling one action no longer cancels everything after it**; a message box
  raised from inside the pump no longer stalls it; and a result arriving as the
  window closes no longer crashes it.
- Restoring a reference failed on every reference worth restoring; a waveform
  saved as CSV came back twice as tall; the self test never reported a pass; and
  connecting logged a spurious "Query INTERRUPTED" (410) on every start.
- The manual documents the NVRAM box and which error-log entries are memory
  faults, and can now be built outside the session that last built it.
- Numerous simulator and test-suite correctness fixes underneath all of the
  above.

## [1.0.0] - 2026-08-24

Initial public release.
