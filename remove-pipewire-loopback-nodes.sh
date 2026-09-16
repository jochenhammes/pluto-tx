#!/usr/bin/env bash
# Kills the persistent PipeWire loopback nodes pluto_tx/pluto_advanced_rx
# create for qpwgraph patching (pluto_tx/audio_devices.py's
# ensure_persistent_input_node()/ensure_persistent_output_node()): a stable,
# always-addressable "input.<name>"/"output.<name>" node pair per app/
# direction, spawned via `pw-loopback -n <name>` and DELIBERATELY detached
# from the launching app (Python's subprocess.Popen(..., start_new_session
# =True)) so it survives that app closing or restarting -- see gui.py in
# both apps, which recreate any missing one unconditionally at every
# startup. That persistence is the whole point (a stable qpwgraph patch
# point that doesn't come and go with the app), but it also means these
# processes are NOT cleaned up by closing the apps, quitting PipeWire
# clients, or anything else short of this script, `kill`/`pkill` by hand,
# or a reboot (no systemd unit/autostart entry exists for them either).
#
# A pw-loopback process IS the node pair it creates -- there's no separate
# "destroy this PipeWire node" step needed; killing the process tears down
# both its stream nodes (input.<name> and output.<name>) and any links to
# them cleanly.
#
# The four names below must match the ones actually used in code today:
#   pluto_tx/audio_devices.py:       _PERSISTENT_INPUT_NODE_NAME  = "pluto-tx-input"
#                                     _PERSISTENT_OUTPUT_NODE_NAME = "pluto-tx-output"
#   pluto_advanced_rx/gui.py:        ensure_persistent_input_node(name="pluto-advanced-rx-input")
#                                     ensure_persistent_output_node(name="pluto-advanced-rx-output")
# If a future change renames or adds to these, update this list to match --
# nothing here discovers them automatically.
#
# Usage:
#   ./remove-pipewire-loopback-nodes.sh
#
# Safe to re-run: a name with no matching process is reported and skipped,
# not an error. Exits 0 as long as every matching process it found was
# successfully killed (or none were running at all).
set -euo pipefail

NODE_NAMES=(
    "pluto-tx-input"
    "pluto-tx-output"
    "pluto-advanced-rx-input"
    "pluto-advanced-rx-output"
)

# How long to wait after a graceful SIGTERM before concluding a process is
# stuck and escalating to SIGKILL. pw-loopback is a small, well-behaved
# wrapper around a PipeWire stream with no state to flush, so this is
# generous, not a real expectation of it ever actually being needed.
GRACE_PERIOD_S=2

any_failed=0

for name in "${NODE_NAMES[@]}"; do
    # -f matches the full command line; anchored with ^...$ so
    # "pluto-tx-input" can never accidentally match some OTHER process that
    # merely happens to contain that string (e.g. as a longer name's
    # prefix) -- exact-match only, exactly how these were spawned
    # (subprocess.Popen(["pw-loopback", "-n", name], ...), no extra args).
    mapfile -t pids < <(pgrep -f "^pw-loopback -n ${name}\$" || true)

    if [ "${#pids[@]}" -eq 0 ]; then
        echo "  (not running) ${name}"
        continue
    fi

    for pid in "${pids[@]}"; do
        echo "  killing ${name} (pid ${pid})..."
        kill "${pid}" 2>/dev/null || true

        waited=0
        while kill -0 "${pid}" 2>/dev/null; do
            if [ "${waited}" -ge "${GRACE_PERIOD_S}" ]; then
                echo "    still alive after ${GRACE_PERIOD_S}s, sending SIGKILL"
                kill -9 "${pid}" 2>/dev/null || true
                break
            fi
            sleep 1
            waited=$((waited + 1))
        done

        if kill -0 "${pid}" 2>/dev/null; then
            echo "    FAILED to kill pid ${pid}" >&2
            any_failed=1
        else
            echo "    done"
        fi
    done
done

if [ "${any_failed}" -ne 0 ]; then
    echo "One or more loopback nodes could not be removed -- see above." >&2
    exit 1
fi

echo "All known pluto_tx/pluto_advanced_rx loopback nodes are gone."
echo "They will be recreated automatically the next time either app starts."
