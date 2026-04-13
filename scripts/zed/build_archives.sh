#!/usr/bin/env bash
# Build the platform archives that Zed downloads when installing the extension.
#
# Usage (run from repo root):
#   bash scripts/zed/build_archives.sh [VERSION]
#
# If VERSION is omitted it is read from pyproject.toml.
# Archives are written to dist/zed/.
#
# After running this script, attach the generated files to the GitHub Release
# for the corresponding version tag (v<VERSION>).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Resolve version
if [[ -n "${1:-}" ]]; then
    VERSION="$1"
else
    VERSION=$(grep -m1 '^version' "${REPO_ROOT}/pyproject.toml" | sed 's/.*= *"\(.*\)"/\1/')
fi

DIST="${REPO_ROOT}/dist/zed"
mkdir -p "${DIST}"

echo "Building Zed extension archives for aar-agent v${VERSION}"

# Platforms that need a Unix launcher (tar.gz)
UNIX_TARGETS=(
    "darwin-aarch64"
    "darwin-x86_64"
    "linux-x86_64"
    "linux-aarch64"
)

for target in "${UNIX_TARGETS[@]}"; do
    archive="${DIST}/aar-zed-${target}.tar.gz"
    tmp=$(mktemp -d)
    cp "${SCRIPT_DIR}/launch.sh" "${tmp}/launch.sh"
    chmod +x "${tmp}/launch.sh"
    tar -czf "${archive}" -C "${tmp}" launch.sh
    rm -rf "${tmp}"

    sha256=$(shasum -a 256 "${archive}" | awk '{print $1}')
    echo "  ${archive}"
    echo "    sha256: ${sha256}"
done

# Windows (zip)
WIN_ARCHIVE="${DIST}/aar-zed-windows-x86_64.zip"
tmp=$(mktemp -d)
cp "${SCRIPT_DIR}/launch.cmd" "${tmp}/launch.cmd"
(cd "${tmp}" && zip -q "${WIN_ARCHIVE}" launch.cmd)
rm -rf "${tmp}"

sha256=$(shasum -a 256 "${WIN_ARCHIVE}" | awk '{print $1}')
echo "  ${WIN_ARCHIVE}"
echo "    sha256: ${sha256}"

echo ""
echo "Done.  Attach the files in dist/zed/ to the GitHub Release for v${VERSION}."
echo "Then update the sha256 values in extension.toml."
