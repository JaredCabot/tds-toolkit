Pick the one for your machine. Each is self-contained: no Python, no
installer, nothing written to the registry. It keeps its settings and
its log beside itself and copies the mask library out on first run, so
put it somewhere you can write to.

| File | For | Tested against an instrument |
|---|---|---|
| `TDS-Toolkit-windows-x86_64.exe` | Windows | Yes, a TDS 784D |
| `tds-toolkit-linux-x86_64` | Linux, glibc 2.35 or newer | **No** |
| `tds-toolkit-macos-arm64` | Apple silicon | **No** |
| `tds-toolkit-macos-x86_64` | Intel Macs | **No** |

On Linux and macOS: `chmod +x` the file first.

### The Linux and macOS builds have never met an oscilloscope

They are the same program, built from the same commit, and they pass
the same automated checks against a simulator - the window, every tab,
the mask editor, the file formats. What has not happened is anyone
pointing one at real hardware over GPIB. The parts most likely to
differ are the ones the simulator cannot stand in for: the VISA layer,
GPIB timing, and how large transfers behave.

If you try one, **please open an issue either way**. "Worked, TDS 754C
on Ubuntu 24.04 with linux-gpib 4.3.6" is as useful as a bug report,
because right now the honest answer to "does it work on Linux" is that
nobody knows. Say which instrument, which firmware (the System tab
shows it), which OS, and what the GPIB adapter is. If something fails,
`tdstoolkit.log` beside the program has the fault and what the program
was connected to at the time - that file is the whole of what a report
needs.

**They are not code signed.** Windows shows SmartScreen on the first
run - More info, then Run anyway. macOS shows Gatekeeper - open it once
from the right-click menu, or clear it with
`xattr -d com.apple.quarantine tds-toolkit-macos-arm64`.

**You need a VISA layer with GPIB support**, which is separate from
this program and is what actually talks to the hardware:

- **Windows** - NI-VISA or the Keysight IO Libraries Suite. One of
  them, not both.
- **Linux** - `linux-gpib` built for your kernel. The binary carries
  pyvisa-py, so it will tell you if linux-gpib is missing.
- **macOS** - NI-VISA for macOS.

### Checking what you downloaded

`SHA256SUMS.txt` lists every file. `sha256sum -c SHA256SUMS.txt` on
Linux, `shasum -a 256 -c SHA256SUMS.txt` on macOS, or
`Get-FileHash <file>` on Windows. That catches a download that went
wrong, and it tells a bug report which binary it is about.

It does not prove where a file came from - the list sits on the same
page as the files. What does prove it is the build provenance, which
GitHub signs:

    gh attestation verify <file> --repo JaredCabot/tds-toolkit

That ties the binary to the workflow run and the commit it was built
from, and it cannot be forged by replacing a file on this page.
