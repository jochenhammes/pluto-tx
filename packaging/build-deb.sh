#!/usr/bin/env bash
# Builds the pluto-tx .deb for one or more targets in containers (podman or
# docker) -- the local counterpart of the CI release workflow.
#
# Usage: packaging/build-deb.sh [--version VER] [--out DIR] [target ...]
#   targets: ubuntu26.04 deb13 (default: both, native architecture)
#   --version  upstream version (default: 0.9.0~dev<date>.g<commit>)
#   --out      where the .deb files go (default: packaging/out)
#   --frontend DIR  a built Web-TRX frontend (its dist/ directory) instead of
#              building it here -- it is architecture-independent, e.g. built
#              once on a PC and copied to a Raspberry Pi
#   JOBS=n     parallel compile jobs (default: all cores; e.g. JOBS=2 on a 2 GB Raspberry Pi)
#
# Steps: build the Web-TRX frontend once (Node.js on the host, or a node
# container), then per target: a source tree of the working tree (the files
# git knows or would add, no ignored ones -- uncommitted changes are included
# and mark the version with +dirty), packaging/debian as debian/,
# dpkg-buildpackage -b in the
# target's build image (packaging/container/Containerfile.build), lintian.
# Downloads and git clones are cached in packaging/.cache/<target>.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
OUT="$HERE/out"
VERSION=""
FRONTEND=""
TARGETS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="${2:?}"; shift 2 ;;
        --out) OUT="${2:?}"; shift 2 ;;
        --frontend) FRONTEND="$(cd "${2:?}" && pwd)"; shift 2 ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
        ubuntu26.04|deb13) TARGETS+=("$1"); shift ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done
[[ ${#TARGETS[@]} -gt 0 ]] || TARGETS=(ubuntu26.04 deb13)

ENGINE="$(command -v podman || command -v docker || true)"
[[ -n "$ENGINE" ]] || { echo "podman or docker needed" >&2; exit 1; }

DIRTY=""
[[ -n "$(git -C "$REPO" status --porcelain)" ]] && DIRTY="+dirty"
if [[ -z "$VERSION" ]]; then
    VERSION="0.9.0~dev$(git -C "$REPO" log -1 --format=%cd --date=format:%Y%m%d HEAD).g$(git -C "$REPO" rev-parse --short HEAD)$DIRTY"
fi

# the working tree without ignored files (build output, venvs, node_modules, clones)
export_tree() {
    local dest="$1"; shift
    mkdir -p "$dest"
    (cd "$REPO" && git ls-files -z --cached --others --exclude-standard -- "$@" \
        | xargs -0 -I{} sh -c 'test -e "{}" && printf "%s\0" "{}"' \
        | tar --null -T - -c) | tar -x -C "$dest"
}
ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
mkdir -p "$OUT"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/pluto-tx-deb.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# --- frontend, once for all targets ------------------------------------------------
echo "== Web-TRX frontend =="
export_tree "$WORK" web-trx/frontend
if [[ -n "$FRONTEND" ]]; then
    test -f "$FRONTEND/index.html" || { echo "$FRONTEND/index.html missing" >&2; exit 1; }
    cp -a "$FRONTEND" "$WORK/web-trx/frontend/dist"
elif command -v npm >/dev/null 2>&1; then
    (cd "$WORK/web-trx/frontend" && npm ci --silent && npm run build --silent)
else
    "$ENGINE" run --rm -v "$WORK/web-trx/frontend:/f:Z" -w /f docker.io/library/node:22 \
        sh -c 'npm ci --silent && npm run build --silent'
fi
test -f "$WORK/web-trx/frontend/dist/index.html"

for target in "${TARGETS[@]}"; do
    case "$target" in
        ubuntu26.04) base=ubuntu:26.04; dist=resolute ;;
        deb13) base=debian:trixie; dist=trixie ;;
    esac
    debver="$VERSION~$target"
    image="pluto-tx-build:$target"
    echo
    echo "== $target: pluto-tx $debver ($ARCH) =="
    "$ENGINE" build -q --build-arg BASE="$base" -t "$image" -f "$HERE/container/Containerfile.build" "$HERE/container" >/dev/null

    src="$WORK/$target/pluto-tx-$VERSION"
    mkdir -p "$src"
    export_tree "$src"
    cp -a "$WORK/web-trx/frontend/dist" "$src/web-trx/frontend/dist"
    cp -a "$src/packaging/debian" "$src/debian"
    cat >"$src/debian/changelog" <<EOF
pluto-tx ($debver) $dist; urgency=medium

  * Build of $(git -C "$REPO" rev-parse HEAD)$DIRTY for $target.

 -- Jochen Hammes <hammesj@me.com>  $(date -R)
EOF
    cache="$HERE/.cache/$target"
    mkdir -p "$cache"
    # shellcheck disable=SC2016  # expanded inside the container
    if ! "$ENGINE" run --rm -v "$WORK/$target:/build:Z" -v "$cache:/cache:Z" -e COMPONENTS_CACHE=/cache -e JOBS="${JOBS:-$(nproc)}" \
        -w "/build/pluto-tx-$VERSION" "$image" bash -c '
            set -e
            dpkg-buildpackage -b -us -uc -j"$JOBS"
            cd /build
            echo "== lintian =="
            lintian --info --display-info --pedantic --profile "$( . /etc/os-release; echo "$ID")" ./*.changes || true
        ' 2>&1 | tee "$OUT/build-$target.log"; then
        echo "build for $target failed, log: $OUT/build-$target.log" >&2
        exit 1
    fi
    cp "$WORK/$target"/pluto-tx_*.deb "$OUT/"
done

(cd "$OUT" && sha256sum pluto-tx_*.deb > SHA256SUMS)
echo
echo "== Done =="
ls -l "$OUT"/*.deb
