#!/bin/sh
# pluto-advanced-rx from the pluto-tx package. Only the Python search path is set: the
# native parts in /usr/lib/pluto-tx carry their own RPATH, pluto_tx/paths.py
# finds the rest. -P: the current directory is never searched (a pluto-tx git
# checkout there must not shadow the installed code).
PYTHONPATH="/usr/share/pluto-tx:/usr/lib/pluto-tx/python${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -P -m pluto_advanced_rx.app "$@"
