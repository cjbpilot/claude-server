"""Bootstrap script for installing/removing the ClaudeAgent Windows service.

Why this exists:
    pywin32 determines the registered module name from cls.__module__. If we
    ran `python -m agent.service install`, __module__ would be '__main__',
    and pywin32 would store a broken reference. Importing the class here
    gives it a proper 'agent.service' module name, which pythonservice.exe
    can re-import at service runtime.
"""

from __future__ import annotations

import sys

import win32serviceutil

from agent.service import ClaudeAgentService

if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(ClaudeAgentService, argv=sys.argv)
