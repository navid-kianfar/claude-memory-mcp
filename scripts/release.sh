#!/bin/bash
# Cut a release: bump the version, run tests, commit, tag, and push.
#
# The pushed "vX.Y.Z" tag is what triggers the Docker publish workflow -
# normal commits never publish an image, only tagged releases do.
#
#   ./scripts/release.sh              # patch bump (default): 0.6.0 -> 0.6.1
#   ./scripts/release.sh minor        # minor bump:           0.6.0 -> 0.7.0
#   ./scripts/release.sh major        # major bump:           0.6.0 -> 1.0.0
#   ./scripts/release.sh 1.2.3        # explicit version
#   SKIP_TESTS=1 ./scripts/release.sh # skip the pre-release test run
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

current=$(grep -E '^version = ' pyproject.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')
arg="${1:-patch}"

if [[ "$arg" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  new="$arg"
else
  IFS='.' read -r major minor patch <<< "$current"
  case "$arg" in
    major) new="$((major + 1)).0.0" ;;
    minor) new="${major}.$((minor + 1)).0" ;;
    patch) new="${major}.${minor}.$((patch + 1))" ;;
    *) echo "usage: release.sh [patch|minor|major|X.Y.Z]" >&2; exit 1 ;;
  esac
fi

echo "Release: ${current} -> ${new}"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Error: working tree is not clean. Commit or stash changes first." >&2
  exit 1
fi

branch=$(git rev-parse --abbrev-ref HEAD)
if [[ "$branch" != "main" ]]; then
  echo "Error: releases must be cut from 'main' (currently on '${branch}')." >&2
  exit 1
fi

if git rev-parse "v${new}" >/dev/null 2>&1; then
  echo "Error: tag v${new} already exists." >&2
  exit 1
fi

if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  echo "Running tests..."
  uv run pytest -q
fi

# Bump the version in pyproject.toml and the package __init__.
python3 - "$new" <<'PY'
import pathlib, re, sys
new = sys.argv[1]
pp = pathlib.Path("pyproject.toml")
pp.write_text(re.sub(r'^version = ".*"', f'version = "{new}"',
                     pp.read_text(), count=1, flags=re.M))
init = pathlib.Path("src/memory_mcp/__init__.py")
init.write_text(re.sub(r'__version__ = ".*"', f'__version__ = "{new}"',
                       init.read_text(), count=1))
PY

# Keep the lockfile's project version in sync.
uv lock --quiet

git add pyproject.toml src/memory_mcp/__init__.py uv.lock
git commit -m "Release v${new}"
git tag -a "v${new}" -m "v${new}"
git push origin "$branch"
git push origin "v${new}"

echo ""
echo "Released v${new}."
echo "The Docker publish workflow now builds and pushes the multi-arch image."
