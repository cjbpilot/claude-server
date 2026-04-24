"""Windows Service entry point. Requires pywin32.

Install/remove with install-agent.ps1. Manually:

    python -m agent.service install
    python -m agent.service start
    python -m agent.service stop
    python -m agent.service remove

The service hosts the asyncio runner and stops it cleanly on service stop.
"""

from __future__ import annotations

import asyncio
import sys
import threading

if sys.platform != "win32":
    # Allow importing on non-Windows for tests / type-checking.
    class _Stub:
        def __getattr__(self, name):
            raise RuntimeError("agent.service requires Windows + pywin32")

    sys.modules[__name__] = _Stub()  # type: ignore[assignment]
else:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    from agent import config, runner

    class ClaudeAgentService(win32serviceutil.ServiceFramework):
        _svc_name_ = "ClaudeAgent"
        _svc_display_name_ = "Claude Agent"
        _svc_description_ = "Remote control agent for Claude Code / Cowork."

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.loop: asyncio.AbstractEventLoop | None = None
            self.runner: runner.Runner | None = None
            self.thread: threading.Thread | None = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self.runner and self.loop:
                # Schedule stop on the runner's loop; don't block SvcStop.
                self.loop.call_soon_threadsafe(self.runner.request_stop, "service stop")
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            runner._setup_logging()
            cfg = config.load()

            def _run_in_thread():
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)
                self.runner = runner.Runner(cfg)
                try:
                    self.loop.run_until_complete(self.runner.run())
                finally:
                    try:
                        self.loop.close()
                    except Exception:
                        pass

            self.thread = threading.Thread(target=_run_in_thread, daemon=False)
            self.thread.start()

            # Block on the stop event; Windows uses this to know the service is alive.
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)

            # Let the runner finish a graceful shutdown.
            if self.thread:
                self.thread.join(timeout=20)

    if __name__ == "__main__":
        if len(sys.argv) == 1:
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(ClaudeAgentService)
            servicemanager.StartServiceCtrlDispatcher()
        else:
            win32serviceutil.HandleCommandLine(ClaudeAgentService)
