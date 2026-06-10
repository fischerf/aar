#!/usr/bin/env bash
# Build the Python wheel (.whl) and source distribution (.tar.gz) for a release.
#
# Usage (run from repo root):
#   bash scripts/release/build_packages.sh [VERSION]
#
# If VERSION is omitted it is read from pyproject.toml.
# Packages are written to dist/python/.
#
# After running this script:
#   1. Go to https://github.com/fischerf/aar/releases/new?tag=v<VERSION>
#   2. Paste the [<VERSION>] section from CHANGELOG.md as release notes.
#   3. Attach all files from dist/python/ (and dist/zed/ if built).
#   4. Publish the release.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# ── Version ───────────────────────────────────────────────────────────────────
if [[ -n "${1:-}" ]]; then
    VERSION="$1"
else
    VERSION=$(grep -m1 '^version' "${REPO_ROOT}/pyproject.toml" | sed 's/.*= *"\(.*\)"/\1/')
fi

# Warn if this looks like a dev version
if [[ "${VERSION}" == *".dev"* ]]; then
    echo "WARNING: version '${VERSION}' contains '.dev' — are you on a release commit?"
    echo "         Run this script from the tagged release commit on main, or pass the"
    echo "         correct version as an argument:  bash $0 0.3.2"
    echo ""
    read -r -p "Continue anyway? [y/N] " confirm
    [[ "${confirm}" =~ ^[Yy]$ ]] || exit 1
fi

DIST="${REPO_ROOT}/dist/python"

# ── Clean previous build artefacts ───────────────────────────────────────────
echo "Cleaning previous build artefacts..."
rm -rf "${REPO_ROOT}/build" "${REPO_ROOT}"/src/*.egg-info "${REPO_ROOT}"/*.egg-info
rm -rf "${DIST}"
mkdir -p "${DIST}"

# ── Build ─────────────────────────────────────────────────────────────────────
echo ""
echo "Building aar-agent v${VERSION} packages..."
echo ""

python -m build \
    --wheel \
    --sdist \
    --outdir "${DIST}" \
    "${REPO_ROOT}"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Built packages for aar-agent v${VERSION}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

for f in "${DIST}"/*; do
    fname=$(basename "${f}")
    size=$(wc -c < "${f}" | tr -d ' ')
    # Use sha256sum if available (Linux), fall back to shasum (macOS/Git Bash)
    if command -v sha256sum &>/dev/null; then
        sha256=$(sha256sum "${f}" | awk '{print $1}')
    else
        sha256=$(shasum -a 256 "${f}" | awk '{print $1}')
    fi
    printf "  %-55s  %6d KB\n" "${fname}" $(( size / 1024 ))
    printf "    sha256: %s\n\n" "${sha256}"
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Next steps:"
echo "  1. Open:  https://github.com/fischerf/aar/releases/new?tag=v${VERSION}"
echo "  2. Set title:  Release v${VERSION}"
echo "  3. Paste the [${VERSION}] section from CHANGELOG.md as release notes."
echo "  4. Attach the files above from dist/python/."
if [[ -d "${REPO_ROOT}/dist/zed" ]] && [[ -n "$(ls -A "${REPO_ROOT}/dist/zed" 2>/dev/null)" ]]; then
    echo "  5. Also attach Zed archives from dist/zed/."
else
    echo "  5. Optionally run:  bash scripts/zed/build_archives.sh ${VERSION}"
    echo "     then attach the Zed archives from dist/zed/ as well."
fi
echo ""
echo "Done."
