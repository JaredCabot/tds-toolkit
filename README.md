# TDS Toolkit

A Windows program for Tektronix TDS500, TDS600 and TDS700 series digitizing
oscilloscopes over GPIB. It browses and transfers the instrument's files,
captures waveforms and screens, draws and sends masks, runs limit tests, reads
the service error log, and manages the instrument's system settings.

These instruments expose a filesystem, a hardcopy port, a mask subsystem and a
limit-test subsystem over the bus, but ship with no tool to reach any of it
from a PC. This is that tool.

Full documentation is in
[TDS Toolkit User Manual.pdf](docs/TDS%20Toolkit%20User%20Manual.pdf).

## Capabilities

| Tab | What it does |
|---|---|
| Files | Two-pane browser for the instrument's drives. Download, upload, create folders, delete, and drag files in from Explorer. |
| Screenshot | Captures the instrument's screen through its hardcopy port and saves it as PNG. Format, layout and palette are chosen in the program; the instrument's own hardcopy settings are put back afterwards. |
| Waveforms | Captures live channels and stored references, plots them together, zooms and pans, saves as ISF, CSV, WFM or PNG, and loads a file back into a reference. |
| Limits | Builds a template from the signal on screen, sends it to the instrument as a limit test, and reports the verdict. The limit envelope can be drawn or edited by hand. |
| Masks | A mask editor with a shipped library of standard telecom and serial-bus masks. Sends a mask to the instrument, sets the instrument up for an eye diagram, and counts hits against it. |
| Error Log | Reads the instrument's own service error log, saves it, and clears it. |
| System | Identity and firmware, front-panel lock, the clock, hardcopy and RS-232 ports, signal path compensation, extended diagnostics, secure erase, factory recall, and the factory option words. |
| Settings | Plot colours and presets, saved-picture resolution, and where the program keeps its settings and log. |

Nine language catalogues are supplied: English, Deutsch, Español, Français,
Italiano, Русский, 日本語, 简体中文 and tlhIngan Hol. The program follows the
language Windows is set to unless told otherwise, and falls back to English.
A `lang` folder beside the program overrides the bundled catalogues, so a
language can be added or corrected without rebuilding anything.

## What your instrument can do

Firmware decides what is possible, and the difference across the range is
large. The table below was read out of 66 firmware images.

| Generation | Browse | Download | Upload |
|---|---|---|---|
| v2.x: TDS 520, 540, 620 | no filesystem at all | no | no |
| A and B series, v3.x to v4.x | yes | yes | no |
| C and D series, v5.0e and later | yes | yes | yes |

The A and B series have no `FILESYSTEM:WRITEFILE` command, so no program can
send a file to them over GPIB. They browse and download normally.

Waveform transfer works on every instrument in the range, including the v2.x
models with no filesystem: `CURVe`, `WFMPre`, `DATa:SOURce` and
`DATa:DESTination` are present in all 34 firmware images examined.

What each known instrument can do is listed in `capabilities.json`. An
instrument that is not listed is asked directly when it connects, so it works
without anyone editing anything. A `capabilities.json` beside the program
overrides the bundled copy.

## Requirements

* A GPIB controller and its driver, for example a National Instruments
  GPIB-USB-HS, a Keysight 82357B, or an interface card.
* A VISA runtime. This is a system driver, not a Python package, and nothing
  can reach the instrument without it. Install the GPIB driver first, because
  VISA discovers the hardware through it.
  * Windows: [NI-VISA](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html)
    or the [Keysight IO Libraries Suite](https://www.keysight.com/find/iosuite).
    One, not both.
  * macOS: NI-VISA for macOS.
  * Linux: `linux-gpib`, built for your kernel. The Linux binary carries
    pyvisa-py, so it will say so if linux-gpib is missing.
* Windows, Linux or macOS. Binaries are built for all three. Only the Windows
  one is tested against real instruments; the others are the same program and
  pass the same checks against a simulator, but nobody has yet driven a scope
  from them.

Running from source additionally needs Python 3.8 or newer with Tkinter, which
the Windows installer includes.

## Install

### The executable

Download the one for your machine from [Releases](../../releases) and run it.
Each is self-contained: no Python, nothing written to the registry, and no
installer. Put it wherever suits you.

| File | For | Tested against an instrument |
|---|---|---|
| `TDS-Toolkit-windows-x86_64.exe` | Windows | Yes, a TDS 784D |
| `tds-toolkit-linux-x86_64` | Linux, glibc 2.35 or newer | **No** |
| `tds-toolkit-macos-arm64` | Apple silicon | **No** |
| `tds-toolkit-macos-x86_64` | Intel Macs | **No** |

On Linux and macOS, `chmod +x` it first.

The Linux and macOS builds are the same program from the same commit and
pass the same automated checks against a simulator, but **nobody has yet
pointed one at real hardware**. If you do, please open an issue whether it
worked or not - the VISA layer and GPIB timing are exactly what a simulator
cannot stand in for, so a report either way is worth having.

None of them is code signed. Windows brings up SmartScreen on the first run -
**More info**, then **Run anyway**. macOS brings up Gatekeeper - open it once
from the right-click menu, or clear it with
`xattr -d com.apple.quarantine <file>`.

The program keeps `tdstoolkit.json` and `tdstoolkit.log` beside the
executable, and copies its mask library into a `masks` folder there the first
time it runs. Choose a folder you can write to.

### From source

Clone this repository, then:

```
pip install -r requirements.txt
python "TDS Toolkit.pyw"
```

`requirements.txt` installs `pyvisa`, the Python binding for the VISA runtime,
and `tkinterdnd2`, which is optional and adds dragging files in from Explorer.
Without `tkinterdnd2` everything else works and the title bar says so.

To check that VISA can see the instrument before starting:

```
python -c "import pyvisa; rm = pyvisa.ResourceManager(); print(rm.list_resources())"
```

## First run

Nothing needs configuring. The program tries `GPIB0::3::INSTR`, and if nothing
answers it scans the bus and opens the instrument picker. Every address is
asked to identify itself with `*IDN?`, which changes nothing on any of them.
Pick the instrument, tick **Remember this address**, and the next run connects
straight to it.

The address can also be given on the command line, which overrides the
remembered one for that run only:

```
"TDS Toolkit.exe" --address=GPIB0::1::INSTR
```

Other switches:

| Switch | Effect |
|---|---|
| `--version` | Print the version and exit. |
| `--check-translations` | Audit `lang/*.json` and exit non-zero if anything is wrong. |

## Building the executable

```
pip install pyinstaller
pyinstaller --noconfirm --clean "TDS Toolkit.spec"
```

The spec file is the build recipe and is edited by hand. It bundles the icon,
the language catalogues, `capabilities.json` and the mask library, and stamps
the version resource from `version_info.txt`.

The released binaries are not built by hand. Tagging a commit `v*` runs
[`.github/workflows/release.yml`](.github/workflows/release.yml), which builds
each one on the system it is for - PyInstaller cannot cross-compile - and
attaches all four to the release. The Linux build runs on Ubuntu 22.04 rather
than the newest runner, because a binary built against an older glibc runs on
newer distributions and the reverse is not true.

## Licence

MIT. See [LICENSE](LICENSE).

Tektronix, TDS and TEK are trademarks of Tektronix, Inc. This program is not a
Tektronix product and is not endorsed by Tektronix.
