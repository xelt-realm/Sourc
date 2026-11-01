#!/bin/sh
set -eu

if [ "$(uname -s)" != "Linux" ]; then
    echo "setup_qbe.sh supports Linux only." >&2
    exit 1
fi

prefix=${PREFIX:-"$HOME/.local"}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
mkdir -p "$prefix/bin"

for tool in git make cc install; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Install $tool before running setup_qbe.sh." >&2
        exit 1
    fi
done

git clone --depth 1 git://c9x.me/qbe.git "$work/qbe"
make -C "$work/qbe"
install -m 755 "$work/qbe/qbe" "$prefix/bin/qbe"

echo "Installed qbe in $prefix/bin"
echo "Add this to PATH if needed: export PATH=\"$prefix/bin:\$PATH\""
