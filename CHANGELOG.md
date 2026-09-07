# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
