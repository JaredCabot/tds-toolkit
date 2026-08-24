"""Double-click this to open the file explorer with no console window.

Windows runs a .pyw with pythonw.exe, which has no console attached, so
there is no black window behind the app and none flashing up first. The
program keeps its own log beside this file either way - see tdstoolkit.log
- so nothing is lost by having no console to print to.

tdstoolkit.py still runs normally from a terminal when you want the output.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tdstoolkit

if __name__ == "__main__":
    tdstoolkit.main()
