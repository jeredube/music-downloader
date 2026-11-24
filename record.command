#!/bin/zsh
set -euo pipefail
cd -- "${0:A:h}"
output_dir="$HOME/Desktop/dj/recorded-songs"
mkdir -p -- "$output_dir"
# Repair completed files from earlier versions; active .part files stay hidden.
/usr/bin/find "$output_dir" -type f -name '*.wav' -exec /usr/bin/chflags nohidden {} +
if [[ ! -x ./capture || capture.swift -nt ./capture ]]; then
    swiftc -O capture.swift -o capture
fi
exec ./capture --mode pre --duration 0 --out "$output_dir" "$@"
