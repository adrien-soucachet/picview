# picview

A minimal photo **viewer** for the desktop — it shows a picture and lets you walk
through the folder. No library, no sidebar, no thumbnails, no editing.

## Why

Gwenview does far more than I want and puts most of it on screen. I wanted a window
that is entirely the photo, opens fitted, moves with the arrow keys, and can turn a
picture the right way up without quietly re-compressing it.

So this is ~700 lines of Python on Qt. It starts in about 0.2 s, has no thumbnail
cache, no database and no background indexer.

## Features

- Opens **fitted** to the window; double-click for **1:1** under the cursor, double-click
  again to fit
- **Sharp on a scaled desktop.** Shrinking happens in real screen pixels, so a display at
  150% or 200% shows every pixel it is capable of. Zoom percentages count screen pixels
  too, so 100% means one pixel of the photo on one pixel of the panel
- **Left / Right** step through the folder **in the same order Dolphin shows it**:
  newest photo first, wrapping at the ends. Photos taken in the same second fall back
  to name order, using the same `QCollator` KDE sorts with — so `IMG_9` comes before
  `IMG_10`, case is ignored, and `été` files under E rather than after Z
- **Rotate left or right, replacing the file on disk** — losslessly for JPEG
- **`Delete` moves the photo to the Trash**, after a confirmation that defaults to
  Cancel — so it can be put back from the file manager
- Follows the desktop light/dark theme, and switches **live** when you change it
- Fullscreen on `F` / `F12`, on a near-black background whatever the theme
- Honours the EXIF orientation, so phone photos are the right way up
- Wheel zooms around the cursor; drag to pan when the picture is larger than the window
- Remembers window size and fullscreen state between launches

## Keyboard

| Key | Action |
|---|---|
| `←` / `→` | Previous / next photo (also `Backspace` / `Space`) |
| `Home` / `End` | First / last photo in the folder |
| `F` / `F12` / `F11` | Toggle fullscreen |
| `Esc` | Leave fullscreen, or close |
| `R` / `Ctrl+R` | Rotate right, and save |
| `L` / `Ctrl+Shift+R` | Rotate left, and save |
| `Delete` | Move the photo to the Trash (asks first) |
| `0` / `Ctrl+0` | Fit to window |
| `1` / `Ctrl+1` | Actual size, 1:1 |
| `+` / `-` | Zoom in / out |
| `Ctrl+W` / `Ctrl+Q` | Close |

Double-click toggles between fit and 1:1. The wheel zooms around the pointer; drag the
picture to pan once it is bigger than the window.

## Rotating

`R` and `L` rewrite the file in place — that is the point of them, so there is no
separate save step. A short message confirms it each time.

For JPEG this is done **without decoding the image**, so no generation of quality is
lost and rotating four times gives you back the original file byte for byte. picview
shells out to `jpegtran` for that, and then clears the now-stale EXIF orientation tag
so every other program agrees which way up the photo is. Where the width or height is
not a whole number of JPEG blocks, `jpegtran` cannot turn the edge cleanly; picview
falls back to flipping the EXIF orientation tag alone, which is also exact. Only if the
file has no such tag *and* has awkward dimensions does the edge strip get rebuilt.

Phone photos are rarely a single JPEG: every Pixel shot carries a second image
appended after the first (Google's MPF gain map), and a Motion Photo carries a whole
MP4 video there too — on the sample I checked, 2.4 MB of it. That payload lives past
the end-of-image marker, where no JPEG tool looks, and `jpegtran` drops it. So when
picview sees anything attached, it rotates by rewriting the EXIF orientation tag
instead: two bytes change, the attachment survives byte for byte, and the offsets
inside the file that point at it stay valid. The trade-off is that a program which
ignores EXIF orientation will show such a photo unturned — there is no way to both
move the pixels and keep the attachment intact.

PNG and the other lossless formats are simply rotated and rewritten. The file's
permissions and timestamps are preserved, so a folder sorted by date does not
reshuffle and a rotated photo keeps its place in the sequence instead of jumping to
the front, and the write is atomic — an interrupted rotation cannot leave you with
half a photo.

Nothing else in picview ever writes to your files.

## Deleting

`Delete` asks before it does anything — a modal naming the photo, with **Cancel** as the
default button, so leaning on the key does not walk through a folder deleting it.

Confirming moves the file to the desktop Trash rather than unlinking it, following the
same freedesktop spec Dolphin does: it lands in the same place, and you put it back the
same way. Somewhere without a trash directory of its own — a removable disk, some
network mounts — the move is refused and picview says so. It does not fall back to an
unlink, because that would quietly turn a reversible delete into a permanent one.

The next photo takes the deleted one's place, wrapping at the end of the folder the way
`→` does.

## Install

**Debian / Ubuntu** — use the system Qt package, it is the best-tested path:

```sh
sudo apt install python3-pyqt6 libjpeg-turbo-progs
git clone https://github.com/adrien-soucachet/picview.git
cd picview && ./install.sh
```

This copies the script to `~/.local/bin/picview` and generates a `.desktop` entry, so
picview shows up under *Open With*. Nothing is written outside `$HOME` and no `sudo` is
needed for the install step itself.

**Anywhere else** — via pipx:

```sh
pipx install git+https://github.com/adrien-soucachet/picview.git
```

(pipx pulls PyQt6 from PyPI, which is a much larger download than the distro package.
On Debian/Ubuntu prefer `install.sh`.)

To make picview the default for photos:

```sh
xdg-mime default picview.desktop image/jpeg image/png
```

## Speed

Measured on a folder of 5,051 real Pixel photos (24 GB) sitting on a pCloud network
mount, and on a synthetic folder of 14,950:

| | 5,051 photos (network) | 14,950 photos (local) |
|---|---|---|
| Scan the folder | 160 ms cold, 21 ms warm | 56 ms |
| Cold start to first photo | 76 ms | 87 ms |
| Next photo (12 MP JPEG) | ~300 ms, network-bound | 40 ms |
| Jump to first / last | 40 ms | 37 ms |
| Memory, at rest | 165 MB | 128 MB |

Timestamps come from `scandir`, which carries them along with the directory listing
rather than making a second call per file — on the network mount that is 21 ms instead
of 120 ms. Memory is flat over hundreds of photos: one picture is held at a time, plus one
pre-scaled copy at the size it is being shown. There is no thumbnail cache and no
index, which is why startup does not care how large the folder is. Rotating a 12 MP
JPEG takes about 125 ms.

## Requirements

Python 3.9+ and PyQt6. `jpegtran` (`libjpeg-turbo-progs`) is optional and only used for
rotating JPEGs — without it, viewing still works and `R` / `L` say so rather than
re-encoding your photo behind your back.

Which formats open depends on the Qt image plugins installed; JPEG, PNG, GIF, BMP,
WebP, TIFF and SVG come as standard. `picview` also accepts a folder and opens the
first picture in it.

## Limitations

Deliberate omissions — it is a viewer:

- no thumbnails, no filmstrip, no folder tree, no tabs, no slideshow
- no cropping, resizing, colour adjustment or export; **rotation is the only edit**
- no rename or move; **`Delete` goes to the Trash**, never straight to unlink
- **fit never enlarges.** A picture too small to fill the window is shown at 1:1 rather
  than scaled up into a blur; zoom in by hand if you want it bigger
- **the order is fixed**: newest first, by the file's modification time. There is no way
  to sort by name or oldest-first from inside picview
- the folder is listed once, at startup — pictures added while it is open do not appear
  until you reopen
- **no preloading.** The next photo is decoded when you ask for it, so on a network
  mount holding the arrow key down moves as fast as the files arrive
- **window position is not restored** between launches. Wayland does not let an
  application place its own window — size and fullscreen state do come back

Tested on KDE Plasma / Wayland, Ubuntu 26.04, Python 3.14, PyQt6, on a 3840×2400
panel at 190% scaling.

## Status

A personal tool that does exactly what I need, shared in case it is useful to someone
else. It is not currently under an open-source licence, so it is here to read and use
rather than to build on. Open an issue if that is a problem for you and I will look at
adding one.
