#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "Error: dpkg-deb not found in PATH" >&2
  exit 1
fi

get_release_version() {
  if ! command -v git >/dev/null 2>&1; then
    echo "Error: git not found in PATH" >&2
    return 1
  fi

  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Error: this build must be run inside the git work tree" >&2
    return 1
  fi

  # Build Debian release packages only from an exactly tagged commit.
  # This prevents accidentally reusing the previous release tag for newer code.
  local tag
  tag=$(git describe --tags --exact-match HEAD 2>/dev/null || true)
  if [ -z "$tag" ]; then
    echo "Error: HEAD is not exactly tagged. Create/check out a release tag, e.g.:" >&2
    echo "  git tag -a v1.0.0 -m 'Version 1.0.0'" >&2
    return 1
  fi

  tag=${tag#v}


  printf '%s\n' "$tag"
}

VERSION=$(get_release_version)

PKG=python3-plugin-manager
REL=1
ARCH=all
STAGE=debian
OUT=${PKG}_${VERSION}-${REL}_${ARCH}.deb
PY_DIR="$STAGE/usr/lib/python3/dist-packages"
DOC_DIR="$STAGE/usr/share/doc/$PKG"

cleanup_generated() {
  rm -rf build build-deb debpkg dist .eggs
  rm -rf src/*.egg-info plugin_manager.egg-info *.egg-info
  find . -type d -name '__pycache__' -prune -exec rm -rf {} +
  find . -type f -name '*.pyc' -delete
  find . -type f -name '*.egg' -delete
}

cleanup_generated
rm -rf "$STAGE"
mkdir -p \
  "$STAGE/DEBIAN" \
  "$PY_DIR" \
  "$DOC_DIR"

# Install as a Python package while keeping source files in src/.
# The package directory in /usr/lib/python3/dist-packages is plugin_manager/.
PKG_DIR="$PY_DIR/plugin_manager"
mkdir -p "$PKG_DIR"
for module in src/*.py; do
  cp "$module" "$PKG_DIR/$(basename "$module")"
done

cp README.md "$DOC_DIR/README.md"
cp LICENSE "$DOC_DIR/copyright"

cat > "$DOC_DIR/copyright" <<EOF2
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: plugin-manager
Source: local

Files: *
Copyright: 2026 Fabrizio Pollastri
License: GPL-3.0-or-later
 This package is distributed under the terms of the GNU General Public License,
 version 3 or later.
EOF2

cat > "$STAGE/DEBIAN/control" <<EOF2
Package: $PKG
Version: ${VERSION}-${REL}
Section: python
Priority: optional
Architecture: $ARCH
Maintainer: Fabrizio Pollastri <mxgbot@gmail.com>
Depends: python3 (>= 3.9), python3-packaging
Homepage: https://github.com/fabriziop/plugin-manager
Description: Plugin manager package for Python applications
 Provides a generic plugin manager runtime for Python applications, including
 plugin loading, lifecycle handling, optional task integration, and
 configuration handling utilities.
EOF2

find "$STAGE/usr" -type d -exec chmod 755 {} +
find "$STAGE/usr" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/DEBIAN"
chmod 644 "$STAGE/DEBIAN/control"

rm -f "${PKG}_*_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT"

cleanup_generated

echo "Built package: $OUT"
