#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PKG_NAME="plugin-manager"
PYTHON_PKG_ENV="SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PLUGIN_MANAGER"
DIST_DIR="dist"

cleanup_generated() {
  rm -rf build .eggs
  rm -rf src/*.egg-info *.egg-info
  find . -type d -name '__pycache__' -prune -exec rm -rf {} +
  find . -type f -name '*.pyc' -delete
  find . -type f -name '*.egg' -delete
}

get_release_version() {
  if ! command -v git >/dev/null 2>&1; then
    echo "Error: git not found in PATH" >&2
    return 1
  fi

  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Error: this build must be run inside the git work tree" >&2
    return 1
  fi

  # Build release artifacts only from an exactly tagged commit.
  # This prevents accidentally publishing a later untagged commit with the old tag version.
  local tag
  tag=$(git describe --tags --exact-match HEAD 2>/dev/null || true)
  if [ -z "$tag" ]; then
    echo "Error: HEAD is not exactly tagged. Create/check out a release tag, e.g.:" >&2
    echo "  git tag -a v1.0.0 -m 'Version 1.0.0'" >&2
    return 1
  fi

  tag=${tag#v}

  if ! python3 - <<PY
from packaging.version import Version
Version("$tag")
PY
  then
    echo "Error: git tag '$tag' is not a valid Python package version" >&2
    return 1
  fi

  printf '%s\n' "$tag"
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 not found in PATH" >&2
  exit 1
fi

if ! python3 -m build --version >/dev/null 2>&1; then
  echo "Error: python build module not found. Install python3-build." >&2
  exit 1
fi

VERSION=$(get_release_version)

cleanup_generated
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

printf 'Building %s from git tag version %s\n' "$PKG_NAME" "$VERSION"

# Package-specific setuptools-scm variable avoids leaking the version to nested builds.
env "$PYTHON_PKG_ENV=$VERSION" \
  SETUPTOOLS_SCM_PRETEND_VERSION="$VERSION" \
  python3 -m build --sdist --wheel --outdir "$DIST_DIR"

cleanup_generated

printf 'Built Python artifacts in ./%s/ using version %s\n' "$DIST_DIR" "$VERSION"
