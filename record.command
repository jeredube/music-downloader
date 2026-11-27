#!/bin/zsh
set -euo pipefail
cd -- "${0:A:h}"
output_dir="$HOME/Desktop/dj/recorded-songs"
mkdir -p -- "$output_dir"
# Repair completed files from earlier versions; active .part files stay hidden.
/usr/bin/find "$output_dir" -type f \( -iname '*.wav' -o -iname '*.aiff' -o -iname '*.aif' \) -exec /usr/bin/chflags nohidden {} +
if [[ ! -x ./capture || capture.swift -nt ./capture ]]; then
    # Build separately so an already-running recorder can finish uninterrupted.
    build_path="$(mktemp "$PWD/.capture-build.XXXXXX")"
    trap 'rm -f -- "$build_path"' EXIT
    swiftc -O capture.swift -o "$build_path"
    mv -f -- "$build_path" ./capture
    trap - EXIT
fi
exec ./capture --mode pre --duration 0 --out "$output_dir" "$@"
