# Linux Uninstaller

**English** | [ภาษาไทย](README.th.md)

A simple uninstaller for Linux that runs as a local web UI in your browser. Browse installed apps and packages, see where they live on disk, and uninstall them from one screen.

## Features

- Lists all `APT/dpkg` packages
- Lists `Snap` apps
- Supports `Flatpak` automatically when the `flatpak` command is available
- Search by package name, description, or source
- Shows version, description, and install locations for the selected app
- Previews exactly which packages will be removed (dry-run) before you confirm, and warns when system or desktop packages would go with it
- "Apps only" filter and a size column, with sortable headers
- Uninstalls via `pkexec` to request admin rights when needed
- Pure Python standard library, no Tkinter or extra frameworks

## Install from a release

Download the `.deb` from [Releases](https://github.com/AUMTERDUM/UninstallApplinux/releases/latest), then run:

```bash
sudo apt install ./linux-uninstaller_1.0.0_all.deb
```

Open **Linux Uninstaller** from your app menu, or run `linux-uninstaller`.

## Run from source

```bash
python3 linux_uninstaller.py
```

Or use the script:

```bash
./run.sh
```

Then open the URL it prints, e.g. `http://127.0.0.1:8765`.

To run without opening a browser automatically:

```bash
python3 linux_uninstaller.py --no-browser
```

## Install as a desktop app

To launch it from your Linux app menu like any other app, run:

```bash
./install_app.sh
```

This installs the following for the current user:

- Launcher at `~/.local/share/applications/linux-uninstaller.desktop`
- Icon at `~/.local/share/icons/hicolor/scalable/apps/linux-uninstaller.svg`
- Desktop shortcut at `~/Desktop/Linux Uninstaller.desktop`

Then search for `Linux Uninstaller` in your app menu.

To remove the launcher:

```bash
./uninstall_app.sh
```

## Build a .deb package

To build a Debian/Ubuntu package:

```bash
./build_deb.sh
```

The package is written to `dist/`.

For a standard Debian-structure build:

```bash
dpkg-buildpackage -b -us -uc -d
```

See `DEB_PACKAGE.md` for details.

## Notes

Uninstalling really removes packages from your system. Always check the package list and command shown in the confirmation dialog before confirming. The app binds to `127.0.0.1` only and rejects requests from other websites, so it is reachable only from your own machine.
