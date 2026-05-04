#!/usr/bin/env bash
set -euo pipefail

APP_ID="linux-uninstaller"
DEFAULT_VERSION="1.0.0"
ARCH="all"
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
resolve_version() {
  if [[ -n "${VERSION:-}" ]]; then
    printf '%s\n' "$VERSION"
    return
  fi

  if command -v git >/dev/null 2>&1 && git -C "$BASE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    local tag
    tag="$(git -C "$BASE_DIR" describe --tags --exact-match 2>/dev/null || true)"
    if [[ -n "$tag" ]]; then
      printf '%s\n' "${tag#v}"
      return
    fi

    local short_hash
    short_hash="$(git -C "$BASE_DIR" rev-parse --short HEAD 2>/dev/null || true)"
    if [[ -n "$short_hash" ]]; then
      printf '%s\n' "${DEFAULT_VERSION}+git${short_hash}"
      return
    fi
  fi

  printf '%s\n' "${DEFAULT_VERSION}+local$(date +%Y%m%d)"
}

VERSION="$(resolve_version)"

if [[ "${1:-}" == "--print-version" ]]; then
  printf '%s\n' "$VERSION"
  exit 0
fi

BUILD_ROOT="$BASE_DIR/build/$APP_ID-${VERSION}"
PACKAGE_ROOT="$BUILD_ROOT/pkg"
OUTPUT_DIR="${OUTPUT_DIR:-$BASE_DIR/dist}"
OUTPUT_FILE="$OUTPUT_DIR/${APP_ID}_${VERSION}_${ARCH}.deb"

rm -rf "$BUILD_ROOT"
mkdir -p \
  "$PACKAGE_ROOT/DEBIAN" \
  "$PACKAGE_ROOT/opt/$APP_ID" \
  "$PACKAGE_ROOT/usr/bin" \
  "$PACKAGE_ROOT/usr/share/applications" \
  "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/apps" \
  "$OUTPUT_DIR"

install -m 0644 "$BASE_DIR/linux_uninstaller.py" "$PACKAGE_ROOT/opt/$APP_ID/linux_uninstaller.py"
install -m 0755 "$BASE_DIR/linux-uninstaller" "$PACKAGE_ROOT/usr/bin/$APP_ID"
install -m 0644 "$BASE_DIR/linux-uninstaller.desktop" "$PACKAGE_ROOT/usr/share/applications/$APP_ID.desktop"
install -m 0644 "$BASE_DIR/assets/$APP_ID.svg" "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"

cat > "$PACKAGE_ROOT/DEBIAN/control" <<EOF
Package: $APP_ID
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Depends: python3
Maintainer: Local Builder <builder@example.invalid>
Description: Simple local web UI for uninstalling Linux applications
 A lightweight local interface to browse installed packages, inspect
 locations, and remove APT/dpkg, Snap, and Flatpak apps from one screen.
EOF

install -m 0755 "$BASE_DIR/debian/postinst" "$PACKAGE_ROOT/DEBIAN/postinst"
install -m 0755 "$BASE_DIR/debian/postrm" "$PACKAGE_ROOT/DEBIAN/postrm"

dpkg-deb --root-owner-group --build "$PACKAGE_ROOT" "$OUTPUT_FILE" >/dev/null
printf 'Built package: %s\n' "$OUTPUT_FILE"