import os
import sys

from jarvis_voice_helper.main import main

code = main()
sys.stdout.flush()
sys.stderr.flush()
# Leave without waiting for the stdin reader thread, which may still be blocked in a read.
os._exit(code)
