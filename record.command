#!/bin/zsh
set -euo pipefail
cd -- "${0:A:h}"
output_dir="$HOME/Desktop/dj/recorded-songs"
mkdir -p -- "$output_dir"
if [[ ! -x ./capture || capture.swift -nt ./capture ]]; then
    swiftc -O capture.swift -o capture
fi
exec ./capture --mode pre --duration 0 --out "$output_dir" "$@"
