#!/bin/sh
# Install the pinned shfmt into $1 and verify its digest, for CI. shfmt is the
# parser the lint reads shell with, so CI runs the version a release was tested
# against rather than whatever a runner image ships.
set -eu

# renovate: datasource=github-releases depName=mvdan/sh
SHFMT_VERSION=v3.14.1
# mvdan/sh publishes no checksum file, so a version bump leaves these stale;
# tools/refresh_shfmt.py downloads each asset and rewrites them.
SHFMT_SHA256_LINUX_AMD64=76e77641faa025814b77f153b29796b8e6fa2fca03e0c76a691608b86c7ea7bf
SHFMT_SHA256_LINUX_ARM64=5f2db09dae91fca848f7adbdd014632e921a383863a2ad7e0450ad3aba0c6489
SHFMT_SHA256_DARWIN_ARM64=b7c872db63553ccffc7253aba3ed7d4885a27d83f1ba567b1138c6315a5847e5
case "$(uname -s)-$(uname -m)" in
Linux-x86_64)
	asset=linux_amd64
	sha256=$SHFMT_SHA256_LINUX_AMD64
	;;
Linux-aarch64)
	asset=linux_arm64
	sha256=$SHFMT_SHA256_LINUX_ARM64
	;;
Darwin-arm64)
	asset=darwin_arm64
	sha256=$SHFMT_SHA256_DARWIN_ARM64
	;;
*)
	echo "install-shfmt: no pinned shfmt for $(uname -s)-$(uname -m)" >&2
	exit 1
	;;
esac

dest=$1
mkdir -p "$dest"
curl -fsSL --retry 3 -o "$dest/shfmt" \
	"https://github.com/mvdan/sh/releases/download/${SHFMT_VERSION}/shfmt_${SHFMT_VERSION}_${asset}"
echo "$sha256  $dest/shfmt" | shasum -a 256 -c -
chmod +x "$dest/shfmt"
"$dest/shfmt" --version
