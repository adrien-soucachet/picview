#!/usr/bin/env bash
# Install picview for the current user. No sudo, nothing outside $HOME.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
apps_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

mkdir -p "$bin_dir" "$apps_dir"

install -m 755 "$here/picview.py" "$bin_dir/picview"
echo "installed $bin_dir/picview"

# The MIME types picview offers to open. Kept explicit rather than generated
# from Qt's plugin list, so the desktop entry does not change under you when a
# codec package comes or goes.
mimes="image/jpeg;image/png;image/gif;image/bmp;image/webp;image/tiff;image/x-portable-pixmap;image/svg+xml;"

# Generated rather than shipped, so the Exec path is right on any machine.
cat > "$apps_dir/picview.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=picview
Comment=Simple photo viewer
Exec=$bin_dir/picview %f
Icon=image-x-generic
Terminal=false
MimeType=$mimes
Categories=Graphics;Viewer;Photography;
NoDisplay=false
DESKTOP
echo "installed $apps_dir/picview.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$apps_dir"
fi

if ! command -v jpegtran >/dev/null 2>&1; then
    echo
    echo "note: jpegtran was not found. Everything works except rotating a JPEG,"
    echo "      which uses it to turn the picture without re-encoding it:"
    echo "        sudo apt install libjpeg-turbo-progs"
fi

case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "warning: $bin_dir is not on your PATH — the desktop entry will still work" ;;
esac

echo
echo "Done. Open a photo with:  picview PHOTO.jpg"
echo "To make picview the default for JPEG and PNG, run:"
echo "  xdg-mime default picview.desktop image/jpeg image/png"
