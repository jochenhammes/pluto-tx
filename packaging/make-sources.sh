#!/usr/bin/env bash
# The corresponding sources for a release (GPL): pluto-tx itself at HEAD and
# every bundled component at its pinned commit (packaging/components.lock),
# gr-m17 with its submodules, plus the Opus snapshot rade_c builds with.
#
# Usage: packaging/make-sources.sh <version> [<out-dir>]   -> <out-dir>/sources-<version>.tar.xz
#
# Uses the clones in packaging/.cache/<target>/src (from build-deb.sh) when
# present, otherwise clones. The Opus model tarball (176 MB, BSD-3-Clause,
# no source obligation) is not included; its URL and SHA-256 are listed in
# SOURCES.txt.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
VERSION="${1:?usage: make-sources.sh <version> [<out-dir>]}"
OUT="$(mkdir -p "${2:-$HERE/out}" && cd "${2:-$HERE/out}" && pwd)"
# shellcheck disable=SC1091
. "$HERE/components.lock"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/pluto-tx-sources.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
TOP="$WORK/sources-$VERSION"
mkdir -p "$TOP"

src_clone() {   # <name> <repo> <commit> [--recursive] -> path of a clone at that commit
    local name="$1" repo="$2" commit="$3" d
    for d in "$HERE"/.cache/*/src/"$name"; do
        if [[ -d "$d/.git" ]] && git -C "$d" cat-file -e "$commit^{commit}" 2>/dev/null; then
            echo "$d"; return
        fi
    done
    d="$WORK/clones/$name"
    git clone -q ${4:+"$4"} "$repo" "$d"
    git -C "$d" checkout -q "$commit"
    [[ "${4:-}" == "--recursive" ]] && git -C "$d" submodule update -q --init --recursive
    echo "$d"
}

archive() {     # <name> <clone> <commit> [--recursive]
    local name="$1" dir="$2" commit="$3"
    mkdir -p "$TOP/$name"
    git -C "$dir" archive "$commit" | tar -x -C "$TOP/$name"
    if [[ "${4:-}" == "--recursive" ]]; then
        git -C "$dir" checkout -q -f "$commit"
        git -C "$dir" submodule update -q --init --recursive
        # shellcheck disable=SC2016
        git -C "$dir" submodule foreach -q --recursive \
            'mkdir -p "'"$TOP/$name"'/$displaypath" && git archive HEAD | tar -x -C "'"$TOP/$name"'/$displaypath"'
    fi
}

echo "pluto-tx $(git -C "$REPO" rev-parse HEAD)"
archive pluto-tx "$REPO" HEAD
archive gr-m17 "$(src_clone gr-m17 "$GR_M17_REPO" "$GR_M17_COMMIT" --recursive)" "$GR_M17_COMMIT" --recursive
archive gr-lora_sdr "$(src_clone gr-lora_sdr "$GR_LORA_SDR_REPO" "$GR_LORA_SDR_COMMIT")" "$GR_LORA_SDR_COMMIT"
archive rade_c "$(src_clone rade_c "$RADE_C_REPO" "$RADE_C_COMMIT")" "$RADE_C_COMMIT"
archive ft8_lib "$(src_clone ft8_lib "$FT8_LIB_REPO" "$FT8_LIB_COMMIT")" "$FT8_LIB_COMMIT"
archive js8call "$(src_clone js8call "$JS8_REPO" "$JS8_COMMIT")" "$JS8_COMMIT"

OPUS_URL="$(sed -n 's/^set(OPUS_URL \(.*\))$/\1/p' "$TOP/rade_c/cmake/BuildOpus.cmake" | head -1)"
OPUS_ZIP="$(ls "$HERE"/.cache/*/downloads/"$(basename "$OPUS_URL")" 2>/dev/null | head -1 || true)"
[[ -n "$OPUS_ZIP" ]] || { OPUS_ZIP="$WORK/$(basename "$OPUS_URL")"; wget -q --timeout=30 -O "$OPUS_ZIP" "$OPUS_URL"; }
mkdir -p "$TOP/rade_c-opus"
cp "$OPUS_ZIP" "$TOP/rade_c-opus/"
MODEL="$(ls "$HERE"/.cache/*/downloads/opus_data-*.tar.gz 2>/dev/null | head -1 || true)"

{
    echo "Corresponding sources of pluto-tx $VERSION"
    echo
    echo "pluto-tx     $(git -C "$REPO" rev-parse HEAD)  https://github.com/jochenhammes/pluto-tx"
    echo "gr-m17       $GR_M17_COMMIT  $GR_M17_REPO (with submodules)"
    echo "gr-lora_sdr  $GR_LORA_SDR_COMMIT  $GR_LORA_SDR_REPO"
    echo "rade_c       $RADE_C_COMMIT  $RADE_C_REPO"
    echo "ft8_lib      $FT8_LIB_COMMIT  $FT8_LIB_REPO (built with common/monitor.c LOG_LEVEL LOG_WARN)"
    echo "js8call      $JS8_COMMIT  $JS8_REPO ($JS8_TAG)"
    echo "Opus         $(basename "$OPUS_ZIP")  $OPUS_URL (patched by rade_c, src/opus-nnet.*.diff)"
    if [[ -n "$MODEL" ]]; then
        echo "Opus model   $(basename "$MODEL")  sha256 $(sha256sum "$MODEL" | cut -d' ' -f1)"
        echo "             downloaded by Opus' autogen.sh from https://media.xiph.org/opus/models/, not included"
    fi
    echo
    echo "Build: packaging/build-deb.sh in pluto-tx/ (see packaging/README.md)."
} > "$TOP/SOURCES.txt"

tar -C "$WORK" -cJf "$OUT/sources-$VERSION.tar.xz" "sources-$VERSION"
ls -l "$OUT/sources-$VERSION.tar.xz"
