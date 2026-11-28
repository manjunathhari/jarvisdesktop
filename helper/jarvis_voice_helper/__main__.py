import faulthandler
import os
import sys

# Debug aid: JARVIS_HELPER_DEBUG_STACKS=<seconds> prints every thread's stack to stderr (never
# stdout) once, after that many seconds, so a hang shows where it is stuck.
_stacks = os.environ.get("JARVIS_HELPER_DEBUG_STACKS", "")
if _stacks.isdigit() and int(_stacks) > 0:
    faulthandler.dump_traceback_later(int(_stacks), exit=False, file=sys.stderr)

from jarvis_voice_helper.main import main  # noqa: E402

code = main()
sys.stdout.flush()
sys.stderr.flush()
# Leave without waiting for the stdin reader thread, which may still be blocked in a read.
os._exit(code)
