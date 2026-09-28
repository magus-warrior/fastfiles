"""Local-only rsync transport for integration tests; never opens a network socket."""

import os
import sys

os.chdir(os.environ["FASTFILES_TEST_REMOTE"])
# rsync supplies hostname, executable, then server options to a remote shell.
os.execvp(sys.argv[2], sys.argv[2:])
