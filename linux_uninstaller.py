#!/usr/bin/env python3
"""Simple local web UI for uninstalling Linux applications/packages."""

from __future__ import annotations

import argparse
import functools
import glob
import json
import os
import shutil
import socket
import subprocess
import threading
import webbrowser
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterable
from urllib.parse import parse_qs, urlparse


HOST = "127.0.0.1"
DEFAULT_PORT = 8765

ICON_DIRS = (
    os.path.expanduser("~/.local/share/icons"),
    os.path.expanduser("~/.icons"),
    "/usr/share/icons",
    "/var/lib/flatpak/exports/share/icons",
    os.path.expanduser("~/.local/share/flatpak/exports/share/icons"),
)
ICON_SIZES = ("scalable", "128x128", "256x256", "96x96", "64x64", "48x48")
ICON_EXTS = (".svg", ".png")
ICON_TYPES = {".svg": "image/svg+xml", ".png": "image/png"}
# (manager, package_id) -> resolved icon file; /api/icon only serves paths found here.
ICONS: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class AppEntry:
    manager: str
    package_id: str
    name: str
    version: str
    summary: str
    size_kb: int = 0
    is_app: bool = True
    has_icon: bool = False


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def run_text_command(args: list[str]) -> str:
    completed = subprocess.run(
        args,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0 and not completed.stdout:
        return completed.stderr.strip()
    return completed.stdout.strip()


def safe_split_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


@functools.cache
def icon_themes() -> list[str]:
    """The desktop's icon theme plus everything it inherits, ending with hicolor."""
    current = ""
    if command_exists("gsettings"):
        current = run_text_command(["gsettings", "get", "org.gnome.desktop.interface", "icon-theme"]).strip("'")
    themes: list[str] = []
    queue = [current] if current else []
    while queue:
        theme = queue.pop(0)
        if theme in themes:
            continue
        themes.append(theme)
        for base in ICON_DIRS:
            index = os.path.join(base, theme, "index.theme")
            if os.path.isfile(index):
                with open(index, errors="ignore") as handle:
                    for line in handle:
                        if line.startswith("Inherits="):
                            queue += [name.strip() for name in line[9:].split(",") if name.strip()]
                break
    return [theme for theme in themes if theme != "hicolor"] + ["hicolor"]


@functools.cache
def resolve_icon(name: str) -> str | None:
    if os.path.isabs(name):
        return name if name.endswith(ICON_EXTS) and os.path.isfile(name) else None
    for theme in icon_themes():
        for base in ICON_DIRS:
            for size in ICON_SIZES:
                for ext in ICON_EXTS:
                    path = os.path.join(base, theme, size, "apps", name + ext)
                    if os.path.isfile(path):
                        return path
    for ext in ICON_EXTS:
        if os.path.isfile(path := f"/usr/share/pixmaps/{name}{ext}"):
            return path
    return None


def remember_icon(manager: str, package_id: str, desktop_files: list[str]) -> bool:
    """Record the first resolvable Icon= among the app's launchers; True if one was found."""
    for desktop_file in desktop_files:
        try:
            with open(desktop_file, errors="ignore") as handle:
                icon_name = next((line[5:].strip() for line in handle if line.startswith("Icon=")), "")
        except OSError:
            continue
        if icon_name and (icon := resolve_icon(icon_name)):
            ICONS[(manager, package_id)] = icon
            return True
    return False


def load_dpkg_apps() -> list[AppEntry]:
    if not command_exists("dpkg-query"):
        return []

    output = run_text_command(
        [
            "dpkg-query",
            "-W",
            "-f=${Package}\t${Version}\t${db:Status-Abbrev}\t${Installed-Size}\t${binary:Summary}\n",
        ]
    )
    launcher_packages = dpkg_launcher_packages()
    entries: list[AppEntry] = []
    for line in safe_split_lines(output):
        parts = line.split("\t", 4)
        if len(parts) != 5:
            continue
        package_id, version, status, size, summary = parts
        if status.startswith("ii"):
            entries.append(
                AppEntry(
                    manager="APT/dpkg",
                    package_id=package_id,
                    name=package_id,
                    version=version,
                    summary=summary,
                    size_kb=int(size) if size.isdigit() else 0,
                    is_app=package_id in launcher_packages,
                    has_icon=remember_icon("APT/dpkg", package_id, launcher_packages.get(package_id, [])),
                )
            )
    return entries


def dpkg_launcher_packages() -> dict[str, list[str]]:
    """Packages that own a .desktop launcher (what users think of as apps), with those launchers."""
    desktop_files = glob.glob("/usr/share/applications/*.desktop")
    if not desktop_files:
        return {}
    packages: dict[str, list[str]] = {}
    for line in safe_split_lines(run_text_command(["dpkg-query", "-S", *desktop_files])):
        owners, _, path = line.partition(": ")
        for owner in owners.split(","):
            packages.setdefault(owner.split(":")[0].strip(), []).append(path.strip())
    return packages


def snap_size_kb(name: str) -> int:
    return sum(os.path.getsize(path) for path in glob.glob(f"/var/lib/snapd/snaps/{name}_*.snap")) // 1024


def load_snap_apps() -> list[AppEntry]:
    if not command_exists("snap"):
        return []

    output = run_text_command(["snap", "list"])
    entries: list[AppEntry] = []
    for index, line in enumerate(safe_split_lines(output)):
        if index == 0 and line.lower().startswith("name"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name, version = parts[0], parts[1]
        publisher = parts[4].rstrip("*✓") if len(parts) > 4 else ""
        summary = "Snap package" + (f" by {publisher}" if publisher else "")
        launchers = glob.glob(f"/var/lib/snapd/desktop/applications/{name}_*.desktop")
        entries.append(
            AppEntry(
                manager="Snap",
                package_id=name,
                name=name,
                version=version,
                summary=summary,
                size_kb=snap_size_kb(name),
                # bases/runtimes (core22, gnome-46…) ship no launcher, so they aren't "apps"
                is_app=bool(launchers),
                has_icon=remember_icon("Snap", name, launchers),
            )
        )
    return entries


def parse_human_size_kb(text: str) -> int:
    """Parse flatpak's '1.2\xa0GB' / '350.0 MB' / '12 kB' into KiB; 0 if unreadable."""
    number, _, unit = text.replace("\xa0", " ").strip().partition(" ")
    scale = {"bytes": 1 / 1024, "kb": 1, "mb": 1024, "gb": 1024**2, "tb": 1024**3}.get(unit.strip().lower())
    try:
        return int(float(number) * scale) if scale else 0
    except ValueError:
        return 0


def load_flatpak_apps() -> list[AppEntry]:
    if not command_exists("flatpak"):
        return []

    output = run_text_command(
        ["flatpak", "list", "--app", "--columns=application,name,version,size"]
    )
    entries: list[AppEntry] = []
    for line in safe_split_lines(output):
        parts = line.split("\t")
        if not parts:
            continue
        package_id = parts[0]
        display_name = parts[1] if len(parts) > 1 and parts[1] else package_id
        version = parts[2] if len(parts) > 2 else ""
        size = parse_human_size_kb(parts[3]) if len(parts) > 3 else 0
        entries.append(
            AppEntry(
                manager="Flatpak",
                package_id=package_id,
                name=display_name,
                version=version,
                summary=package_id,
                size_kb=size,
                has_icon=remember_icon(
                    "Flatpak",
                    package_id,
                    [
                        f"/var/lib/flatpak/exports/share/applications/{package_id}.desktop",
                        os.path.expanduser(f"~/.local/share/flatpak/exports/share/applications/{package_id}.desktop"),
                    ],
                ),
            )
        )
    return entries


def load_all_apps() -> list[AppEntry]:
    loaders: Iterable[Callable[[], list[AppEntry]]] = (
        load_dpkg_apps,
        load_snap_apps,
        load_flatpak_apps,
    )
    apps: list[AppEntry] = []
    for loader in loaders:
        try:
            apps.extend(loader())
        except Exception as exc:
            apps.append(
                AppEntry(
                    manager="Error",
                    package_id=loader.__name__,
                    name=loader.__name__,
                    version="",
                    summary=str(exc),
                )
            )
    return sorted(apps, key=lambda app: (app.manager.lower(), app.name.lower()))


def find_app(manager: str, package_id: str) -> AppEntry | None:
    for app in load_all_apps():
        if app.manager == manager and app.package_id == package_id:
            return app
    return None


def installed_locations(app: AppEntry) -> list[str]:
    if app.manager == "APT/dpkg":
        files = safe_split_lines(run_text_command(["dpkg", "-L", app.package_id]))
        priority_prefixes = (
            "/usr/share/applications/",
            "/usr/bin/",
            "/usr/sbin/",
            "/bin/",
            "/sbin/",
            "/opt/",
        )
        priority = [path for path in files if path.startswith(priority_prefixes)]
        return priority[:40] or files[:40]

    if app.manager == "Snap":
        candidates = [
            f"/snap/{app.package_id}/current",
            f"/var/snap/{app.package_id}",
            f"/home/{os.environ.get('USER', '')}/snap/{app.package_id}",
        ]
        return [path for path in candidates if os.path.exists(path)] or candidates[:2]

    if app.manager == "Flatpak":
        candidates = [
            f"/var/lib/flatpak/app/{app.package_id}",
            os.path.expanduser(f"~/.local/share/flatpak/app/{app.package_id}"),
        ]
        return [path for path in candidates if os.path.exists(path)] or candidates

    return []


def uninstall_command(app: AppEntry) -> list[str]:
    use_pkexec = command_exists("pkexec")
    if app.manager == "APT/dpkg":
        base = ["apt", "remove", "-y", app.package_id]
        return ["pkexec", *base] if use_pkexec else base
    if app.manager == "Snap":
        base = ["snap", "remove", app.package_id]
        return ["pkexec", *base] if use_pkexec else base
    if app.manager == "Flatpak":
        return ["flatpak", "uninstall", "-y", app.package_id]
    return []


# ponytail: name heuristic for desktop pieces apt marks "optional"; extend when a new DE slips through
DESKTOP_CRITICAL = {"gdm3", "sddm", "lightdm", "xorg", "xserver-xorg-core", "xwayland", "gnome-shell", "plasma-workspace"}


def removal_preview(app: AppEntry) -> dict[str, object]:
    """Packages that would actually be removed, plus which of them are system-critical."""
    if app.manager != "APT/dpkg":
        return {"packages": [app.package_id], "critical": []}

    simulated = run_text_command(["apt-get", "-s", "remove", app.package_id])
    packages = [line.split()[1] for line in simulated.splitlines() if line.startswith("Remv ")]
    if not packages:
        # apt refused (e.g. essential package); the real removal would fail the same way.
        return {"packages": [], "critical": [], "error": simulated[-1500:]}

    info = run_text_command(["dpkg-query", "-W", "-f=${Package}\t${Priority}\t${Essential}\n", *packages])
    critical = []
    for line in safe_split_lines(info):
        name, _, rest = line.partition("\t")
        priority, _, essential = rest.partition("\t")
        if priority in ("required", "important") or essential == "yes":
            critical.append(name)
    critical += [p for p in packages if p not in critical and (p in DESKTOP_CRITICAL or "-desktop" in p)]
    return {"packages": packages, "critical": critical}


def run_uninstall(app: AppEntry) -> dict[str, object]:
    command = uninstall_command(app)
    if not command:
        return {"ok": False, "command": [], "output": "ไม่พบคำสั่งถอนการติดตั้ง"}

    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return {
        "ok": completed.returncode == 0,
        "code": completed.returncode,
        "command": command,
        "output": completed.stdout,
    }

HTML = r'''<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Linux Uninstaller</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f5f8;
      --panel: #ffffff;
      --panel-2: #f8f9fb;
      --ink: #161b26;
      --muted: #687083;
      --faint: #9aa1b1;
      --line: #e5e7ee;
      --hover: #f2f4f9;
      --accent: #4f46e5;
      --accent-ink: #ffffff;
      --accent-soft: #eef0ff;
      --danger: #dc2626;
      --danger-soft: #fef1f1;
      --danger-line: #f7c4c4;
      --ok: #15803d;
      --ok-soft: #ecfbf1;
      --code-bg: #10131b;
      --code-ink: #c9d1e3;
      --shadow: 0 1px 2px rgb(16 19 27 / .05), 0 8px 24px rgb(16 19 27 / .06);
      --radius: 12px;
      --mono: ui-monospace, "JetBrains Mono", "SFMono-Regular", Consolas, monospace;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        color-scheme: dark;
        --bg: #0d0f14;
        --panel: #151821;
        --panel-2: #1a1e29;
        --ink: #e8eaf0;
        --muted: #9097a8;
        --faint: #636a7b;
        --line: #262b38;
        --hover: #1d2230;
        --accent: #818cf8;
        --accent-ink: #0d0f14;
        --accent-soft: #23264a;
        --danger: #f87171;
        --danger-soft: #2a1618;
        --danger-line: #5b2529;
        --ok: #4ade80;
        --ok-soft: #12261a;
        --code-bg: #0a0c11;
        --shadow: 0 1px 2px rgb(0 0 0 / .3), 0 8px 24px rgb(0 0 0 / .25);
      }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { height: 100%; }
    body {
      display: flex;
      flex-direction: column;
      background: var(--bg);
      color: var(--ink);
      font-family: system-ui, -apple-system, "Segoe UI", "Noto Sans Thai", "Noto Sans", sans-serif;
      font-size: 14px;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }
    button, input, select { font: inherit; color: inherit; }
    button { cursor: pointer; }
    :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    [hidden] { display: none !important; }

    .icon { width: 16px; height: 16px; flex-shrink: 0; }
    .icon-lg { width: 22px; height: 22px; }

    /* ─── Top bar ────────────────────────────────────────── */
    .topbar {
      display: flex;
      align-items: center;
      gap: 20px;
      padding: 14px 20px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      flex-shrink: 0;
    }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .brand-mark { flex-shrink: 0; }
    .brand h1 { font-size: 16px; font-weight: 700; line-height: 1.2; }
    .brand p { font-size: 12px; color: var(--muted); }
    .stats { display: flex; gap: 8px; margin-left: auto; }
    .stat {
      padding: 6px 14px;
      border-radius: 10px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      line-height: 1.25;
      min-width: 92px;
    }
    .stat b { display: block; font-size: 15px; font-variant-numeric: tabular-nums; }
    .stat span { font-size: 11px; color: var(--muted); }

    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      height: 38px;
      padding: 0 16px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      font-weight: 600;
      white-space: nowrap;
      transition: background .15s, border-color .15s, filter .15s;
    }
    .btn:hover:not(:disabled) { background: var(--hover); }
    .btn:disabled { opacity: .5; cursor: not-allowed; }
    .btn-danger { background: var(--danger); border-color: var(--danger); color: #fff; }
    .btn-danger:hover:not(:disabled) { background: var(--danger); filter: brightness(1.08); }
    .btn-ghost { border-color: transparent; background: transparent; }
    .btn-square { width: 38px; padding: 0; }
    .spinning .icon { animation: spin .8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* ─── Toolbar ────────────────────────────────────────── */
    .toolbar {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px 20px;
      flex-shrink: 0;
      flex-wrap: wrap;
    }
    .search {
      position: relative;
      flex: 1 1 280px;
      min-width: 0;
      max-width: 520px;
    }
    .search .icon { position: absolute; left: 12px; top: 11px; color: var(--faint); pointer-events: none; }
    .search input {
      width: 100%;
      height: 38px;
      padding: 0 40px 0 38px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      transition: border-color .15s, box-shadow .15s;
    }
    .search input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
    .search kbd {
      position: absolute;
      right: 10px;
      top: 9px;
      padding: 0 6px;
      border: 1px solid var(--line);
      border-radius: 5px;
      font: 11px/18px var(--mono);
      color: var(--faint);
      pointer-events: none;
    }
    .search input:focus + kbd { display: none; }

    .chips { display: flex; gap: 6px; flex-wrap: wrap; }
    .chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      height: 32px;
      padding: 0 12px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel);
      font-size: 13px;
      font-weight: 600;
      color: var(--muted);
      transition: all .15s;
    }
    .chip:hover { color: var(--ink); }
    .chip span { font-size: 11px; font-weight: 600; color: var(--faint); font-variant-numeric: tabular-nums; }
    .chip[aria-pressed="true"] { background: var(--ink); border-color: var(--ink); color: var(--panel); }
    .chip[aria-pressed="true"] span { color: var(--panel); opacity: .7; }

    .switch { display: inline-flex; align-items: center; gap: 8px; margin-left: auto; font-size: 13px; font-weight: 600; color: var(--muted); cursor: pointer; user-select: none; }
    .switch input { position: absolute; opacity: 0; pointer-events: none; }
    .switch i {
      position: relative;
      width: 34px;
      height: 20px;
      border-radius: 999px;
      background: var(--line);
      transition: background .15s;
    }
    .switch i::after {
      content: "";
      position: absolute;
      top: 2px;
      left: 2px;
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: #fff;
      box-shadow: 0 1px 2px rgb(0 0 0 / .25);
      transition: transform .15s;
    }
    .switch input:checked + i { background: var(--accent); }
    .switch input:checked + i::after { transform: translateX(14px); }
    .switch input:focus-visible + i { outline: 2px solid var(--accent); outline-offset: 2px; }

    /* ─── Workspace ──────────────────────────────────────── */
    .workspace {
      flex: 1;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 380px;
      gap: 16px;
      padding: 0 20px 20px;
      min-height: 0;
    }
    .card {
      display: flex;
      flex-direction: column;
      min-height: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    /* ─── App list ───────────────────────────────────────── */
    .list-meta {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 16px;
      border-bottom: 1px solid var(--line);
      font-size: 12px;
      color: var(--muted);
      flex-shrink: 0;
    }
    .list-scroll { flex: 1; overflow: auto; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    thead th {
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 9px 16px;
      background: var(--panel-2);
      border-bottom: 1px solid var(--line);
      text-align: left;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: .05em;
      text-transform: uppercase;
      color: var(--faint);
      white-space: nowrap;
    }
    thead th[data-sort] { cursor: pointer; user-select: none; }
    thead th[data-sort]:hover { color: var(--ink); }
    thead th[aria-sort] { color: var(--accent); }
    thead th[aria-sort="ascending"]::after { content: " ↑"; }
    thead th[aria-sort="descending"]::after { content: " ↓"; }
    tbody tr { cursor: pointer; }
    tbody td {
      padding: 10px 16px;
      border-bottom: 1px solid var(--line);
      vertical-align: middle;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    tbody tr:hover td { background: var(--hover); }
    tbody tr.selected td { background: var(--accent-soft); }
    tbody tr.selected td:first-child { box-shadow: inset 3px 0 0 var(--accent); }
    .col-source { width: 104px; }
    .col-ver    { width: 150px; color: var(--muted); font-size: 12px; font-family: var(--mono); }
    .col-size   { width: 120px; text-align: right; }
    th.col-size { text-align: right; }

    .name-cell { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .name-text { min-width: 0; }
    .name-text .name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; }
    .name-text .sub { font-size: 12px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; }

    .avatar {
      display: grid;
      place-items: center;
      width: 34px;
      height: 34px;
      border-radius: 9px;
      background: hsl(var(--h) 70% 92%);
      color: hsl(var(--h) 55% 32%);
      font-weight: 700;
      font-size: 14px;
      flex-shrink: 0;
    }
    @media (prefers-color-scheme: dark) {
      .avatar { background: hsl(var(--h) 35% 22%); color: hsl(var(--h) 80% 80%); }
    }
    .avatar-lg { width: 56px; height: 56px; border-radius: 14px; font-size: 22px; }
    .avatar { position: relative; }
    .avatar.has-img { background: none; color: transparent; }
    .avatar img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; }

    .size { font-size: 12px; font-variant-numeric: tabular-nums; color: var(--muted); }
    .bar { height: 3px; margin-top: 4px; margin-left: auto; width: 64px; border-radius: 3px; background: var(--line); overflow: hidden; }
    .bar i { display: block; height: 100%; margin-left: auto; background: var(--accent); border-radius: 3px; }

    .tag {
      display: inline-block;
      padding: 2px 9px;
      border-radius: 6px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: .02em;
    }
    .tag-apt   { background: #e0ebff; color: #1d4ed8; }
    .tag-snap  { background: #fde8f1; color: #be185d; }
    .tag-flat  { background: #dcf8ea; color: #047857; }
    .tag-other { background: var(--hover); color: var(--muted); }
    @media (prefers-color-scheme: dark) {
      .tag-apt  { background: #172449; color: #93b4ff; }
      .tag-snap { background: #3a1427; color: #f9a8d4; }
      .tag-flat { background: #0f2e22; color: #6ee7b7; }
    }

    .skel td span {
      display: block;
      height: 14px;
      border-radius: 6px;
      background: linear-gradient(90deg, var(--hover), var(--line), var(--hover));
      background-size: 200% 100%;
      animation: shimmer 1.2s infinite;
    }
    @keyframes shimmer { to { background-position: -200% 0; } }

    .empty {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 6px;
      padding: 64px 24px;
      text-align: center;
      color: var(--muted);
    }
    .empty .icon-lg { width: 40px; height: 40px; color: var(--faint); margin-bottom: 6px; }
    .empty strong { color: var(--ink); font-size: 15px; }
    .empty p { font-size: 13px; }

    /* ─── Detail pane ────────────────────────────────────── */
    .detail-scroll { flex: 1; overflow-y: auto; }
    .detail-hero { display: flex; gap: 14px; padding: 20px; border-bottom: 1px solid var(--line); }
    .detail-hero h2 { font-size: 18px; line-height: 1.3; word-break: break-word; }
    .detail-hero .meta { display: flex; align-items: center; gap: 8px; margin-top: 6px; font-size: 12px; color: var(--muted); flex-wrap: wrap; }
    .detail-summary { padding: 16px 20px 0; color: var(--muted); font-size: 13px; }
    .facts { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; padding: 16px 20px; }
    .fact { padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; background: var(--panel-2); min-width: 0; }
    .fact span { display: block; font-size: 11px; color: var(--faint); font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
    .fact b { display: block; font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .fact.wide { grid-column: 1 / -1; }
    .fact.wide b { font-family: var(--mono); font-weight: 500; font-size: 12px; }
    .section-title {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 4px 20px 8px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: .05em;
      text-transform: uppercase;
      color: var(--faint);
    }
    .locs { list-style: none; padding: 0 20px 20px; }
    .locs li {
      padding: 6px 10px;
      border-radius: 6px;
      font: 12px/1.5 var(--mono);
      color: var(--muted);
      word-break: break-all;
    }
    .locs li:nth-child(odd) { background: var(--panel-2); }
    .locs li.note { font-family: inherit; background: none; color: var(--faint); }
    .detail-footer { padding: 16px 20px; border-top: 1px solid var(--line); background: var(--panel-2); flex-shrink: 0; }
    .detail-footer .btn { width: 100%; height: 42px; font-size: 15px; }
    .detail-footer p { margin-top: 8px; font-size: 12px; color: var(--faint); text-align: center; }
    .detail-empty { flex: 1; justify-content: center; }
    .kbd-hints { display: grid; gap: 6px; margin-top: 18px; font-size: 12px; color: var(--muted); text-align: left; }
    .kbd-hints kbd { display: inline-block; min-width: 22px; margin-right: 6px; padding: 0 5px; border: 1px solid var(--line); border-bottom-width: 2px; border-radius: 5px; font: 11px/18px var(--mono); text-align: center; }

    /* ─── Dialog ─────────────────────────────────────────── */
    dialog {
      width: min(600px, calc(100vw - 32px));
      max-height: calc(100vh - 48px);
      margin: auto;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--panel);
      color: var(--ink);
      box-shadow: 0 24px 64px rgb(0 0 0 / .3);
    }
    dialog[open] { display: flex; flex-direction: column; animation: pop .16s ease-out; }
    @keyframes pop { from { opacity: 0; transform: translateY(8px) scale(.98); } }
    dialog::backdrop { background: rgb(10 12 18 / .5); backdrop-filter: blur(2px); }
    .dlg-head { display: flex; align-items: flex-start; gap: 14px; padding: 20px 20px 0; }
    .dlg-head .danger-mark { display: grid; place-items: center; width: 40px; height: 40px; border-radius: 50%; background: var(--danger-soft); color: var(--danger); flex-shrink: 0; }
    .dlg-head h2 { font-size: 17px; line-height: 1.3; word-break: break-word; }
    .dlg-head p { margin-top: 2px; font-size: 13px; color: var(--muted); }
    .dlg-head .btn-ghost { margin-left: auto; margin-top: -6px; margin-right: -8px; }
    .dlg-body { padding: 16px 20px; overflow-y: auto; }
    .alert {
      display: grid;
      gap: 8px;
      padding: 12px 14px;
      margin-bottom: 14px;
      border-radius: 10px;
      border: 1px solid var(--danger-line);
      background: var(--danger-soft);
      color: var(--danger);
      font-size: 13px;
      font-weight: 600;
    }
    .alert label { display: flex; align-items: center; gap: 8px; cursor: pointer; }
    .kv { display: grid; grid-template-columns: 96px 1fr; gap: 10px 12px; font-size: 13px; }
    .kv > span { color: var(--muted); }
    .kv > div { min-width: 0; word-break: break-word; }
    .pkg-list { max-height: 150px; overflow: auto; margin-top: 6px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; font: 12px/1.7 var(--mono); }
    .pkg-list .crit { color: var(--danger); font-weight: 700; }
    .cmd { padding: 10px 12px; border-radius: 8px; background: var(--code-bg); color: var(--code-ink); font: 12px/1.5 var(--mono); word-break: break-all; }
    .cmd::before { content: "$ "; color: #6b7280; }
    .result { display: flex; align-items: center; gap: 8px; margin-top: 14px; padding: 10px 14px; border-radius: 10px; font-weight: 600; font-size: 13px; }
    .result.ok   { background: var(--ok-soft); color: var(--ok); }
    .result.fail { background: var(--danger-soft); color: var(--danger); }
    .result.run  { background: var(--accent-soft); color: var(--accent); }
    pre.output {
      max-height: 240px;
      overflow: auto;
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 8px;
      background: var(--code-bg);
      color: var(--code-ink);
      font: 12px/1.5 var(--mono);
      white-space: pre-wrap;
      word-break: break-word;
    }
    .dlg-footer { display: flex; justify-content: flex-end; gap: 8px; padding: 14px 20px; border-top: 1px solid var(--line); }

    /* ─── Toasts ─────────────────────────────────────────── */
    .toasts { position: fixed; right: 20px; bottom: 20px; display: grid; gap: 8px; z-index: 10; }
    .toast {
      max-width: 360px;
      padding: 10px 14px;
      border-radius: 10px;
      background: var(--ink);
      color: var(--panel);
      font-size: 13px;
      font-weight: 600;
      box-shadow: var(--shadow);
      animation: pop .16s ease-out;
    }
    .toast.ok { background: var(--ok); color: #fff; }
    .toast.error { background: var(--danger); color: #fff; }

    /* ─── Responsive ─────────────────────────────────────── */
    @media (max-width: 1000px) {
      .col-ver { display: none; }
    }
    @media (max-width: 860px) {
      .workspace { grid-template-columns: 1fr; grid-template-rows: minmax(0, 1fr) auto; }
      .detail-card { max-height: 55vh; }
      .stats .stat:not(:first-child) { display: none; }
      .switch { margin-left: 0; }
    }
    @media (max-width: 560px) {
      .topbar, .toolbar { padding-left: 16px; padding-right: 16px; }
      .workspace { padding: 0 16px 16px; }
      .stats, .brand p, .col-source { display: none; }
    }
  </style>
</head>
<body>

  <header class="topbar">
    <div class="brand">
      <svg xmlns="http://www.w3.org/2000/svg" width="38" height="38" class="brand-mark" viewBox="0 0 256 256" role="img" aria-label="Linux Uninstaller icon">
  <defs>
    <linearGradient id="logo-bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#1d4ed8"/>
      <stop offset="100%" stop-color="#0f172a"/>
    </linearGradient>
  </defs>
  <rect width="256" height="256" rx="56" fill="url(#logo-bg)"/>
  <path d="M58 86c0-8 6-14 14-14h40l12-16c3-4 7-6 12-6h48c8 0 14 6 14 14v22H58V86Z" fill="#bfdbfe"/>
  <path d="M58 92h140v78c0 20-16 36-36 36H94c-20 0-36-16-36-36V92Z" fill="#eff6ff"/>
  <path d="M104 122h12v52h-12zm36 0h12v52h-12z" fill="#1e293b"/>
  <path d="M84 104h88" stroke="#1e293b" stroke-width="10" stroke-linecap="round"/>
  <circle cx="192" cy="188" r="34" fill="#dc2626"/>
  <path d="M176 188h32" stroke="#fff" stroke-width="10" stroke-linecap="round"/>
</svg>
      <div>
        <h1>ถอนการติดตั้งแอป</h1>
        <p>ดูและลบแอปจาก APT, Snap และ Flatpak ในที่เดียว</p>
      </div>
    </div>
    <div class="stats">
      <div class="stat"><b id="statApps">–</b><span>แอป</span></div>
      <div class="stat"><b id="statPkgs">–</b><span>แพ็กเกจทั้งหมด</span></div>
      <div class="stat"><b id="statSize">–</b><span>พื้นที่ที่ใช้</span></div>
    </div>
    <button id="refresh" class="btn btn-square" title="รีเฟรชรายการ" aria-label="รีเฟรชรายการ"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg></button>
  </header>

  <div class="toolbar">
    <div class="search">
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
      <input id="search" type="search" placeholder="ค้นหาแอป เช่น firefox, vlc, discord" aria-label="ค้นหาแอป" autocomplete="off">
      <kbd>/</kbd>
    </div>
    <div id="managerFilter" class="chips" role="group" aria-label="แหล่งติดตั้ง"></div>
    <label class="switch" title="ซ่อนไลบรารีและแพ็กเกจระบบที่ไม่มีไอคอนในเมนู">
      <input id="appsOnly" type="checkbox" checked><i></i>เฉพาะแอปที่มีไอคอน
    </label>
  </div>

  <main class="workspace">
    <section class="card">
      <div class="list-meta">
        <span id="listMeta">กำลังโหลดรายการ...</span>
        <span id="status"></span>
      </div>
      <div class="list-scroll" id="listScroll">
        <table>
          <thead>
            <tr>
              <th data-sort="name">ชื่อแอป</th>
              <th class="col-source" data-sort="manager">แหล่ง</th>
              <th class="col-ver">เวอร์ชัน</th>
              <th class="col-size" data-sort="size_kb">ขนาด</th>
            </tr>
          </thead>
          <tbody id="apps"></tbody>
        </table>
        <div id="emptyState" class="empty" hidden>
          <svg class="icon-lg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/><path d="M8 11h6"/></svg>
          <strong>ไม่พบรายการที่ตรงกัน</strong>
          <p>ลองเปลี่ยนคำค้น หรือปิด “เฉพาะแอปที่มีไอคอน” เพื่อดูแพ็กเกจทั้งหมด</p>
        </div>
      </div>
    </section>

    <aside class="card detail-card">
      <div id="detailEmpty" class="empty detail-empty">
        <svg class="icon-lg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
        <strong>เลือกแอปจากรายการ</strong>
        <p>เพื่อดูรายละเอียด ที่อยู่ไฟล์ และถอนการติดตั้ง</p>
        <div class="kbd-hints">
          <div><kbd>/</kbd>ค้นหา</div>
          <div><kbd>↑</kbd><kbd>↓</kbd>เลื่อนเลือกแอป</div>
          <div><kbd>Del</kbd>ถอนการติดตั้งแอปที่เลือก</div>
        </div>
      </div>
      <div id="detailView" class="detail-scroll" hidden>
        <div class="detail-hero">
          <div id="detailAvatar"></div>
          <div style="min-width:0">
            <h2 id="detailName"></h2>
            <div class="meta" id="detailMeta"></div>
          </div>
        </div>
        <p id="detailSummary" class="detail-summary"></p>
        <div class="facts" id="detailFacts"></div>
        <div class="section-title"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>ที่อยู่ไฟล์ในเครื่อง</div>
        <ul id="locations" class="locs"></ul>
      </div>
      <div id="detailFooter" class="detail-footer" hidden>
        <button id="uninstall" class="btn btn-danger" disabled><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>ถอนการติดตั้ง</button>
        <p>จะแสดงรายการที่จะถูกลบให้ยืนยันก่อนทุกครั้ง</p>
      </div>
    </aside>
  </main>

  <div id="toasts" class="toasts" aria-live="polite"></div>

  <dialog id="dialog">
    <div class="dlg-head">
      <div class="danger-mark"><svg class="icon-lg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg></div>
      <div>
        <h2 id="dialogTitle">ยืนยันการถอนการติดตั้ง</h2>
        <p>ลบออกจากระบบจริงและย้อนกลับไม่ได้ อาจมีหน้าต่างขอรหัสผ่าน</p>
      </div>
      <button id="closeDialog" class="btn btn-ghost btn-square" aria-label="ปิด"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
    </div>
    <div class="dlg-body">
      <div id="dialogInfo"></div>
      <div id="dialogResult" class="result" hidden></div>
      <pre id="dialogOutput" class="output" hidden></pre>
    </div>
    <div class="dlg-footer">
      <button id="cancelDialog" class="btn">ยกเลิก</button>
      <button id="confirmDialog" class="btn btn-danger">ยืนยันถอนการติดตั้ง</button>
    </div>
  </dialog>

  <script>
    const $ = s => document.querySelector(s);
    const appsEl = $('#apps'), searchEl = $('#search'), managerFilterEl = $('#managerFilter'), appsOnlyEl = $('#appsOnly');
    const headEl = $('thead'), refreshEl = $('#refresh'), statusEl = $('#status'), listMetaEl = $('#listMeta');
    const emptyStateEl = $('#emptyState'), uninstallEl = $('#uninstall'), locationsEl = $('#locations');
    const dialog = $('#dialog'), dialogTitle = $('#dialogTitle'), dialogInfoEl = $('#dialogInfo');
    const dialogResult = $('#dialogResult'), dialogOutput = $('#dialogOutput');
    const cancelDialog = $('#cancelDialog'), closeDialog = $('#closeDialog'), confirmDialog = $('#confirmDialog');

    const MANAGERS = { 'APT/dpkg': ['apt', 'APT'], 'Snap': ['snap', 'Snap'], 'Flatpak': ['flat', 'Flatpak'] };
    const nf = n => n.toLocaleString('th-TH');

    let allApps = [];
    let shownApps = [];
    let selected = null;
    let managerFilter = '';
    let sortKey = 'name';
    let sortDir = 1;
    let running = false;

    function fmtSize(kb) {
      if (!kb) return '–';
      if (kb >= 1048576) return `${(kb / 1048576).toFixed(1)} GB`;
      if (kb >= 1024) return `${(kb / 1024).toFixed(1)} MB`;
      return `${kb} KB`;
    }

    function keyOf(app) { return `${app.manager}\x00${app.package_id}`; }

    function esc(value) {
      return String(value ?? '').replace(/[&<>'"]/g, c => (
        {'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]
      ));
    }

    function tag(manager) {
      const [cls, label] = MANAGERS[manager] || ['other', manager];
      return `<span class="tag tag-${cls}">${esc(label)}</span>`;
    }

    function avatar(app, big) {
      let h = 0;
      for (const c of app.name) h = (h * 31 + c.charCodeAt(0)) % 360;
      const letter = (app.name.match(/[\p{L}\p{N}]/u) || ['?'])[0].toUpperCase();
      const icon = app.has_icon
        ? `<img src="/api/icon?manager=${encodeURIComponent(app.manager)}&package_id=${encodeURIComponent(app.package_id)}" alt="" loading="lazy" onerror="this.parentNode.classList.remove('has-img');this.remove()">`
        : '';
      return `<span class="avatar${big ? ' avatar-lg' : ''}${icon ? ' has-img' : ''}" style="--h:${h}">${esc(letter)}${icon}</span>`;
    }

    function toast(message, kind = '') {
      const t = document.createElement('div');
      t.className = `toast ${kind}`;
      t.textContent = message;
      $('#toasts').append(t);
      setTimeout(() => t.remove(), 4500);
    }

    function visibleByType(app) { return !appsOnlyEl.checked || app.is_app; }

    function filteredApps() {
      const q = searchEl.value.trim().toLowerCase();
      return allApps.filter(app => {
        const text = `${app.name} ${app.package_id} ${app.summary}`.toLowerCase();
        return (!managerFilter || app.manager === managerFilter) && visibleByType(app) && (!q || text.includes(q));
      }).sort((a, b) => {
        const x = a[sortKey], y = b[sortKey];
        const c = typeof x === 'number' ? x - y : String(x).localeCompare(String(y));
        return c * sortDir || a.name.localeCompare(b.name);
      });
    }

    function renderStats() {
      $('#statApps').textContent = nf(allApps.filter(a => a.is_app).length);
      $('#statPkgs').textContent = nf(allApps.length);
      $('#statSize').textContent = fmtSize(allApps.reduce((s, a) => s + a.size_kb, 0));
    }

    function renderManagers() {
      const counts = {};
      for (const a of allApps) if (visibleByType(a)) counts[a.manager] = (counts[a.manager] || 0) + 1;
      const managers = Object.keys(counts).sort();
      if (!managers.includes(managerFilter)) managerFilter = '';
      const total = Object.values(counts).reduce((s, n) => s + n, 0);
      managerFilterEl.innerHTML = [['', 'ทั้งหมด', total], ...managers.map(m => [m, (MANAGERS[m] || [, m])[1], counts[m]])]
        .map(([value, label, n]) => `<button type="button" class="chip" data-m="${esc(value)}" aria-pressed="${value === managerFilter}">${esc(label)}<span>${nf(n)}</span></button>`)
        .join('');
    }

    function renderApps() {
      shownApps = filteredApps();
      const maxKb = shownApps.reduce((m, a) => Math.max(m, a.size_kb), 1);
      const selKey = selected && keyOf(selected);
      appsEl.innerHTML = shownApps.map((app, i) => `<tr data-i="${i}"${keyOf(app) === selKey ? ' class="selected"' : ''}>
          <td><div class="name-cell">${avatar(app)}<div class="name-text">
            <div class="name">${esc(app.name)}</div>
            <div class="sub">${esc(app.summary && app.summary !== app.name ? app.summary : app.package_id)}</div>
          </div></div></td>
          <td class="col-source">${tag(app.manager)}</td>
          <td class="col-ver" title="${esc(app.version)}">${esc(app.version || '–')}</td>
          <td class="col-size"><div class="size">${fmtSize(app.size_kb)}</div><div class="bar"><i style="width:${Math.max(2, app.size_kb / maxKb * 100).toFixed(1)}%"></i></div></td>
        </tr>`).join('');

      const shownSize = shownApps.reduce((s, a) => s + a.size_kb, 0);
      listMetaEl.textContent = `แสดง ${nf(shownApps.length)} รายการ · รวม ${fmtSize(shownSize)}`;
      emptyStateEl.hidden = shownApps.length > 0;
      headEl.querySelectorAll('th[data-sort]').forEach(th => {
        if (th.dataset.sort === sortKey) th.setAttribute('aria-sort', sortDir > 0 ? 'ascending' : 'descending');
        else th.removeAttribute('aria-sort');
      });
    }

    function renderSkeleton() {
      appsEl.innerHTML = Array.from({ length: 12 }, () => '<tr class="skel"><td colspan="4"><span></span></td></tr>').join('');
    }

    function clearDetail() {
      selected = null;
      appsEl.querySelector('tr.selected')?.classList.remove('selected');
      $('#detailEmpty').hidden = false;
      $('#detailView').hidden = true;
      $('#detailFooter').hidden = true;
      uninstallEl.disabled = true;
    }

    function selectApp(app, scroll = false) {
      selected = app;
      appsEl.querySelector('tr.selected')?.classList.remove('selected');
      const row = appsEl.querySelector(`tr[data-i="${shownApps.findIndex(a => keyOf(a) === keyOf(app))}"]`);
      row?.classList.add('selected');
      if (scroll) row?.scrollIntoView({ block: 'nearest' });

      $('#detailEmpty').hidden = true;
      $('#detailView').hidden = false;
      $('#detailFooter').hidden = false;
      $('#detailAvatar').innerHTML = avatar(app, true);
      $('#detailName').textContent = app.name;
      $('#detailMeta').innerHTML = `${tag(app.manager)}<span>${app.is_app ? 'แอปพลิเคชัน' : 'แพ็กเกจ/ไลบรารี'}</span>`;
      $('#detailSummary').textContent = app.summary || '';
      $('#detailSummary').hidden = !app.summary || app.summary === app.package_id;
      $('#detailFacts').innerHTML = `
        <div class="fact"><span>ขนาด</span><b>${fmtSize(app.size_kb)}</b></div>
        <div class="fact"><span>เวอร์ชัน</span><b title="${esc(app.version)}">${esc(app.version || '–')}</b></div>
        <div class="fact wide"><span>รหัสแพ็กเกจ</span><b title="${esc(app.package_id)}">${esc(app.package_id)}</b></div>`;
      uninstallEl.disabled = !(app.manager in MANAGERS);
      locationsEl.innerHTML = '<li class="note">กำลังค้นหา...</li>';

      const key = keyOf(app);
      fetch(`/api/locations?manager=${encodeURIComponent(app.manager)}&package_id=${encodeURIComponent(app.package_id)}`)
        .then(r => r.json())
        .then(data => {
          if (!selected || keyOf(selected) !== key) return;
          locationsEl.innerHTML = data.locations.length
            ? data.locations.map(l => `<li>${esc(l)}</li>`).join('')
            : '<li class="note">ไม่พบข้อมูลที่อยู่จัดเก็บ</li>';
        })
        .catch(err => { locationsEl.innerHTML = `<li class="note">ผิดพลาด: ${esc(err)}</li>`; });
    }

    async function loadApps() {
      refreshEl.disabled = true;
      refreshEl.classList.add('spinning');
      statusEl.textContent = 'กำลังโหลด...';
      try {
        const r = await fetch('/api/apps', { cache: 'no-store' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        allApps = await r.json();
      } catch (err) {
        statusEl.textContent = 'โหลดไม่สำเร็จ';
        toast(`โหลดรายการไม่สำเร็จ: ${err.message}`, 'error');
        return;
      } finally {
        refreshEl.disabled = false;
        refreshEl.classList.remove('spinning');
      }
      renderStats();
      renderManagers();
      renderApps();
      const next = selected && allApps.find(a => keyOf(a) === keyOf(selected));
      next ? selectApp(next) : clearDetail();
      statusEl.textContent = `อัปเดต ${new Date().toLocaleTimeString('th-TH', { hour: '2-digit', minute: '2-digit' })}`;
    }

    appsEl.addEventListener('click', e => {
      const row = e.target.closest('tr[data-i]');
      if (row) selectApp(shownApps[row.dataset.i]);
    });
    managerFilterEl.addEventListener('click', e => {
      const chip = e.target.closest('.chip');
      if (!chip) return;
      managerFilter = chip.dataset.m;
      renderManagers();
      renderApps();
    });
    searchEl.addEventListener('input', renderApps);
    appsOnlyEl.addEventListener('change', () => { renderManagers(); renderApps(); });
    headEl.addEventListener('click', e => {
      const th = e.target.closest('th[data-sort]');
      if (!th) return;
      // size defaults to largest-first, text columns to A→Z
      sortDir = th.dataset.sort === sortKey ? -sortDir : (th.dataset.sort === 'size_kb' ? -1 : 1);
      sortKey = th.dataset.sort;
      renderApps();
    });
    refreshEl.addEventListener('click', loadApps);

    document.addEventListener('keydown', e => {
      if (dialog.open) return;
      if (e.key === '/' && document.activeElement !== searchEl) {
        e.preventDefault();
        searchEl.focus();
        searchEl.select();
        return;
      }
      if (e.target !== searchEl && e.target.matches('input, select, textarea')) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (!shownApps.length) return;
        e.preventDefault();
        const i = selected ? shownApps.findIndex(a => keyOf(a) === keyOf(selected)) : -1;
        const next = e.key === 'ArrowDown' ? Math.min(i + 1, shownApps.length - 1) : Math.max(i - 1, 0);
        selectApp(shownApps[next], true);
      } else if (e.key === 'Delete' && e.target !== searchEl && selected && !uninstallEl.disabled) {
        uninstallEl.click();
      }
    });

    function closeIfIdle() { if (!running) dialog.close(); }
    cancelDialog.addEventListener('click', closeIfIdle);
    closeDialog.addEventListener('click', closeIfIdle);
    dialog.addEventListener('cancel', e => { if (running) e.preventDefault(); });

    uninstallEl.addEventListener('click', async () => {
      if (!selected) return;
      uninstallEl.disabled = true;
      let info;
      try {
        const r = await fetch(`/api/command?manager=${encodeURIComponent(selected.manager)}&package_id=${encodeURIComponent(selected.package_id)}`);
        info = await r.json();
      } catch (err) {
        toast(`ตรวจสอบแพ็กเกจไม่สำเร็จ: ${err.message}`, 'error');
        return;
      } finally {
        uninstallEl.disabled = false;
      }
      const { command, packages, critical, error } = info;
      dialogTitle.textContent = `ถอนการติดตั้ง ${selected.name}`;
      const pkgList = packages.map(p => critical.includes(p)
        ? `<div class="crit">${esc(p)} — แพ็กเกจระบบ</div>` : `<div>${esc(p)}</div>`).join('');
      dialogInfoEl.innerHTML = error ? `
        <div class="alert">⛔ apt ไม่ยอมถอนแพ็กเกจนี้ (น่าจะเป็นแพ็กเกจที่ระบบขาดไม่ได้)</div>
        <pre class="output">${esc(error)}</pre>
      ` : `
        ${critical.length ? `<div class="alert">
          <span>⛔ จะลบแพ็กเกจระบบ ${critical.length} ตัว อาจทำให้ระบบหรือเดสก์ท็อปใช้งานไม่ได้</span>
          <label><input id="ackCritical" type="checkbox"> ฉันเข้าใจความเสี่ยงและต้องการลบต่อ</label>
        </div>` : ''}
        <div class="kv">
          <span>แหล่งติดตั้ง</span><div>${tag(selected.manager)}</div>
          <span>จะถูกลบ</span><div><b>${packages.length} แพ็กเกจ</b><div class="pkg-list">${pkgList}</div></div>
          <span>คำสั่ง</span><div class="cmd">${esc(command.join(' '))}</div>
        </div>
      `;
      dialogResult.hidden = true;
      dialogOutput.hidden = true;
      dialogOutput.textContent = '';
      cancelDialog.textContent = 'ยกเลิก';
      cancelDialog.disabled = closeDialog.disabled = false;
      confirmDialog.hidden = false;
      confirmDialog.disabled = critical.length > 0 || !!error;
      confirmDialog.textContent = 'ยืนยันถอนการติดตั้ง';
      $('#ackCritical')?.addEventListener('change', e => { confirmDialog.disabled = !e.target.checked; });
      dialog.showModal();
      cancelDialog.focus();
    });

    confirmDialog.addEventListener('click', async () => {
      if (!selected) return;
      const app = selected;
      running = true;
      confirmDialog.disabled = cancelDialog.disabled = closeDialog.disabled = true;
      confirmDialog.textContent = 'กำลังถอนการติดตั้ง...';
      dialogResult.hidden = false;
      dialogResult.className = 'result run';
      dialogResult.textContent = 'กำลังรัน — อาจมีหน้าต่างขอรหัสผ่านจากระบบ';
      let data;
      try {
        const r = await fetch('/api/uninstall', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ manager: app.manager, package_id: app.package_id })
        });
        data = await r.json();
      } catch (err) {
        data = { ok: false, code: '-', command: [], output: String(err) };
      }
      running = false;
      dialogResult.className = `result ${data.ok ? 'ok' : 'fail'}`;
      dialogResult.textContent = data.ok ? `✓ ถอนการติดตั้ง ${app.name} เรียบร้อย` : `✕ ไม่สำเร็จ (รหัสผลลัพธ์ ${data.code})`;
      dialogOutput.hidden = false;
      dialogOutput.textContent = `${data.command.length ? `$ ${data.command.join(' ')}\n\n` : ''}${data.output || ''}`;
      confirmDialog.hidden = true;
      cancelDialog.disabled = closeDialog.disabled = false;
      cancelDialog.textContent = 'ปิด';
      toast(data.ok ? `ถอนการติดตั้ง ${app.name} แล้ว` : `ถอนการติดตั้ง ${app.name} ไม่สำเร็จ`, data.ok ? 'ok' : 'error');
      await loadApps();
    });

    clearDetail();
    renderSkeleton();
    loadApps();
  </script>
</body>
</html>
'''


class UninstallerHandler(BaseHTTPRequestHandler):
    server_version = "LinuxUninstaller/1.0"

    def host_ok(self) -> bool:
        # Blocks DNS rebinding: only accept requests addressed to our own loopback origin.
        return self.headers.get("Host") == f"{HOST}:{self.server.server_address[1]}"

    def do_GET(self) -> None:
        if not self.host_ok():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(HTML)
            return
        if parsed.path == "/api/apps":
            self.send_json([asdict(app) for app in load_all_apps()])
            return
        if parsed.path == "/api/icon":
            values = parse_qs(parsed.query)
            icon = ICONS.get((values.get("manager", [""])[0], values.get("package_id", [""])[0]))
            if not icon:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                with open(icon, "rb") as handle:
                    body = handle.read()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ICON_TYPES[os.path.splitext(icon)[1]])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600")
            # SVGs opened directly must not run scripts on our origin.
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/locations":
            app = self.app_from_query(parsed.query)
            self.send_json({"locations": installed_locations(app) if app else []})
            return
        if parsed.path == "/api/command":
            app = self.app_from_query(parsed.query)
            self.send_json(
                {
                    "command": uninstall_command(app) if app else [],
                    **(removal_preview(app) if app else {"packages": [], "critical": []}),
                }
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/uninstall":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        # JSON content type forces a CORS preflight, so other websites can't trigger this.
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip()
        if not self.host_ok() or content_type != "application/json":
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return

        app = find_app(str(payload.get("manager", "")), str(payload.get("package_id", "")))
        if app is None:
            self.send_json(
                {"ok": False, "code": 1, "command": [], "output": "ไม่พบแพ็กเกจนี้"},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        self.send_json(run_uninstall(app))

    def log_message(self, format: str, *args: object) -> None:
        return

    def app_from_query(self, query: str) -> AppEntry | None:
        values = parse_qs(query)
        manager = values.get("manager", [""])[0]
        package_id = values.get("package_id", [""])[0]
        return find_app(manager, package_id)

    def send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def open_window(url: str) -> None:
    # ponytail: Chromium-family --app gives a standalone app window; anything else gets a normal tab
    for browser in ("google-chrome", "chromium", "chromium-browser", "brave-browser", "microsoft-edge"):
        if command_exists(browser):
            subprocess.Popen(
                [browser, f"--app={url}", "--window-size=1280,820"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
    webbrowser.open(url)


def choose_port(preferred_port: int) -> int:
    for port in range(preferred_port, preferred_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                candidate.bind((HOST, port))
            except OSError:
                continue
            return port
    raise RuntimeError("ไม่พบพอร์ตว่างสำหรับเปิดโปรแกรม")


def main() -> None:
    parser = argparse.ArgumentParser(description="Simple Linux uninstaller web UI")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    port = choose_port(args.port)
    server = ThreadingHTTPServer((HOST, port), UninstallerHandler)
    url = f"http://{HOST}:{port}"
    print(f"Linux Uninstaller is running at {url}")
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        threading.Timer(0.5, open_window, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Linux Uninstaller.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
