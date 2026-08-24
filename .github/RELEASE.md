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
