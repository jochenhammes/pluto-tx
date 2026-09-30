#!/usr/bin/env bash
# Builds the JS8 reference harness (js8ref) from the pinned JS8Call source.
#   tools/js8ref/build.sh [JS8CALL_SRC] [OUT_DIR]
# Defaults: JS8CALL_SRC=<repo>/js8call (install-js8.sh), OUT_DIR=<JS8CALL_SRC>/build-js8ref.
# Needs g++ (C++20), Qt6 Core headers (qt6-base-dev), libfftw3-dev, libboost-dev. Without root the
# Qt6 headers can come from an unpacked qt6-base-dev .deb: QT6_INCLUDE=<dir>/usr/include/<multiarch>/qt6
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
SRC="$(cd "${1:-$REPO/js8call}" && pwd)"
OUT="${2:-$SRC/build-js8ref}"
MA="$(dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || gcc -print-multiarch 2>/dev/null || echo x86_64-linux-gnu)"
QI="${QT6_INCLUDE:-/usr/include/$MA/qt6}"
MOC="${QT6_MOC:-/usr/lib/qt6/libexec/moc}"
QTCORE="${QT6_CORE_LIB:-/usr/lib/$MA/libQt6Core.so.6}"
for f in "$QI/QtCore/QString" "$MOC" "$QTCORE" "$SRC/JS8_Mode/JS8.cpp"; do
  [[ -e "$f" ]] || { echo "missing: $f" >&2; exit 1; }
done
mkdir -p "$OUT"

# moc: JS8.cpp includes "JS8.moc" (Worker lives in the .cpp); JS8.h and varicode.h declare Q_OBJECTs.
"$MOC" -I"$SRC" "$SRC/JS8_Mode/JS8.cpp" -o "$OUT/JS8.moc"
"$MOC" -I"$SRC" "$SRC/JS8_Mode/JS8.h" -o "$OUT/moc_JS8.cpp"
"$MOC" -I"$SRC" "$SRC/JS8_Main/varicode.h" -o "$OUT/moc_varicode.cpp"

CXXFLAGS=(-std=c++20 -O2 -fPIC -w -DQT_NO_DEBUG
  -I"$SRC" -I"$SRC/vendor" -I"$SRC/JS8_Mode" -I"$OUT" -I"$QI" -I"$QI/QtCore")
srcs=(
  "$HERE/js8ref.cpp"
  "$SRC/JS8_Mode/JS8.cpp"
  "$SRC/JS8_Mode/JS8Submode.cpp"
  "$SRC/JS8_Mode/kalman.cpp"
  "$SRC/JS8_Mode/decodedtext.cpp"
  "$SRC/JS8_Main/varicode.cpp"
  "$SRC/JS8_jsc/jsc.cpp"
  "$SRC/JS8_jsc/jsc_map.cpp"
  "$SRC/JS8_jsc/jsc_list.cpp"
  "$OUT/moc_JS8.cpp"
  "$OUT/moc_varicode.cpp"
)
objs=()
# At most JOBS compilers at once (default: all cores) -- each C++20/Qt
# translation unit needs a few hundred MB, too much x11 for a 2 GB Raspberry Pi.
jobs_max="${JOBS:-$(nproc)}"
for s in "${srcs[@]}"; do
  o="$OUT/$(basename "${s%.cpp}").o"
  if [[ ! -f "$o" || "$s" -nt "$o" ]]; then
    rm -f "$o"
    while (( $(jobs -rp | wc -l) >= jobs_max )); do wait -n || true; done
    g++ "${CXXFLAGS[@]}" -c "$s" -o "$o" &
  fi
  objs+=("$o")
done
wait
# g++ leaves no object file behind when it fails
for o in "${objs[@]}"; do
  [[ -f "$o" ]] || { echo "compile failed: $o" >&2; exit 1; }
done
g++ ${LDFLAGS:-} -o "$OUT/js8ref" "${objs[@]}" "$QTCORE" -lfftw3f -lfftw3f_threads -lpthread
echo "built $OUT/js8ref"
