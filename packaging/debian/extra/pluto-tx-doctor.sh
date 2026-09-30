#!/bin/sh
# pluto-tx-doctor from the pluto-tx package (pluto_tx/doctor.py).
PYTHONPATH="/usr/share/pluto-tx:/usr/lib/pluto-tx/python${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -P -m pluto_tx.doctor "$@"
