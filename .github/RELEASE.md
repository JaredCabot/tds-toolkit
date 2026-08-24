Pick the one for your machine. Each is self-contained: no Python, no
installer, nothing written to the registry. It keeps its settings and
its log beside itself and copies the mask library out on first run, so
put it somewhere you can write to.

| File | For |
|---|---|
| `TDS-Toolkit-windows-x86_64.exe` | Windows |
| `tds-toolkit-linux-x86_64` | Linux, glibc 2.35 or newer |
| `tds-toolkit-macos-arm64` | Apple silicon |
| `tds-toolkit-macos-x86_64` | Intel Macs |

On Linux and macOS: `chmod +x` the file first.

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

**Only the Windows build is tested against real instruments.** The
others are the same program and pass the same checks against the
simulator, but nobody has yet driven a 784D from them. If you do,
whether it worked or not is worth an issue.

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
