#!/usr/bin/env python3
"""Simple local web UI for uninstalling Linux applications/packages."""

from __future__ import annotations

import argparse
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


@dataclass(frozen=True)
class AppEntry:
    manager: str
    package_id: str
    name: str
    version: str
    summary: str


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


def load_dpkg_apps() -> list[AppEntry]:
    if not command_exists("dpkg-query"):
        return []

    output = run_text_command(
        [
            "dpkg-query",
            "-W",
            "-f=${Package}\t${Version}\t${db:Status-Abbrev}\t${binary:Summary}\n",
        ]
    )
    entries: list[AppEntry] = []
    for line in safe_split_lines(output):
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        package_id, version, status, summary = parts
        if status.startswith("ii"):
            entries.append(
                AppEntry(
                    manager="APT/dpkg",
                    package_id=package_id,
                    name=package_id,
                    version=version,
                    summary=summary,
                )
            )
    return entries


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
        publisher = parts[4] if len(parts) > 4 else ""
        summary = "Snap package" + (f" by {publisher}" if publisher else "")
        entries.append(
            AppEntry(
                manager="Snap",
                package_id=name,
                name=name,
                version=version,
                summary=summary,
            )
        )
    return entries


def load_flatpak_apps() -> list[AppEntry]:
    if not command_exists("flatpak"):
        return []

    output = run_text_command(
        ["flatpak", "list", "--app", "--columns=application,name,version"]
    )
    entries: list[AppEntry] = []
    for line in safe_split_lines(output):
        parts = line.split("\t")
        if not parts:
            continue
        package_id = parts[0]
        display_name = parts[1] if len(parts) > 1 and parts[1] else package_id
        version = parts[2] if len(parts) > 2 else ""
        entries.append(
            AppEntry(
                manager="Flatpak",
                package_id=package_id,
                name=display_name,
                version=version,
                summary=package_id,
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
  <title>ถอนการติดตั้งแอป</title>
  <style>
    :root {
      --bg: #f0f4f8;
      --panel: #ffffff;
      --ink: #1a2433;
      --muted: #64748b;
      --line: #e2e8f0;
      --accent: #2563eb;
      --accent-bg: #eff6ff;
      --danger: #dc2626;
      --danger-bg: #fef2f2;
      --green: #16a34a;
      --green-bg: #f0fdf4;
      --radius: 10px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }

    html, body { height: 100%; }

    body {
      display: flex;
      flex-direction: column;
      background: var(--bg);
      color: var(--ink);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      line-height: 1.5;
    }

    /* ─── Header ─────────────────────────────────────────── */
    .app-header {
      display: flex;
      align-items: center;
      gap: 16px;
      padding: 0 16px;
      height: 52px;
      background: #1e293b;
      color: #f8fafc;
      flex-shrink: 0;
    }
    .app-header h1 {
      font-size: 16px;
      font-weight: 700;
      white-space: nowrap;
    }
    .header-count {
      padding: 2px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,.12);
      color: #cbd5e1;
      font-size: 12px;
    }

    /* ─── Search bar ─────────────────────────────────────── */
    .search-bar {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 10px 16px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      flex-shrink: 0;
    }
    .search-bar input[type=search] {
      flex: 1;
      min-width: 0;
      height: 38px;
      padding: 0 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      font: inherit;
      background: var(--bg);
    }
    .search-bar input[type=search]:focus {
      outline: none;
      border-color: var(--accent);
      background: #fff;
    }
    /* icon-wrapper overrides the above when used */
    .search-bar .search-wrap input[type=search] { padding-left: 34px; }
    .search-bar select {
      height: 38px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      font: inherit;
      background: #fff;
      color: var(--ink);
      cursor: pointer;
    }
    .search-bar button {
      height: 38px;
      padding: 0 16px;
      border: none;
      border-radius: var(--radius);
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      background: var(--accent);
      color: #fff;
      white-space: nowrap;
    }
    .search-bar button:hover { filter: brightness(1.1); }
    .search-bar button:disabled { opacity: .55; cursor: not-allowed; }

    /* ─── Icons ─────────────────────────────────────────── */
    .icon {
      width: 16px;
      height: 16px;
      display: inline-block;
      vertical-align: -3px;
      flex-shrink: 0;
    }
    .icon-20 { width: 20px; height: 20px; vertical-align: -5px; }
    .btn-icon { display: inline-flex; align-items: center; gap: 6px; }
    .search-wrap {
      flex: 1;
      min-width: 0;
      position: relative;
      display: flex;
      align-items: center;
    }
    .search-wrap .search-icon {
      position: absolute;
      left: 10px;
      color: var(--muted);
      pointer-events: none;
    }
    .search-wrap input[type=search] {
      width: 100%;
      height: 38px;
      padding: 0 12px 0 34px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      font: inherit;
      background: var(--bg);
    }
    .search-wrap input[type=search]:focus {
      outline: none;
      border-color: var(--accent);
      background: #fff;
    }

    /* ─── Main 2-pane ────────────────────────────────────── */
    .workspace {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      min-height: 0;
      flex: 1;
    }

    /* ─── App list ───────────────────────────────────────── */
    .list-pane {
      display: flex;
      flex-direction: column;
      border-right: 1px solid var(--line);
      min-height: 0;
    }
    .list-meta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 6px 16px;
      border-bottom: 1px solid var(--line);
      background: #f8fafc;
      font-size: 12px;
      color: var(--muted);
      flex-shrink: 0;
    }
    .list-scroll {
      flex: 1;
      overflow-y: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 600px;
    }
    thead th {
      position: sticky;
      top: 0;
      padding: 9px 14px;
      background: #f1f5f9;
      border-bottom: 2px solid var(--line);
      text-align: left;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      color: var(--muted);
      letter-spacing: .04em;
      z-index: 1;
    }
    tbody tr {
      cursor: pointer;
    }
    tbody tr:hover td { background: #f0f7ff; }
    tbody tr.selected td {
      background: var(--accent-bg);
    }
    tbody td {
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
      vertical-align: middle;
    }
    .col-source { width: 110px; }
    .col-name   { width: 220px; font-weight: 600; word-break: break-word; }
    .col-ver    { width: 130px; color: var(--muted); font-size: 12px; word-break: break-word; }
    .col-desc   { color: var(--muted); font-size: 13px; word-break: break-word; }

    .tag {
      display: inline-block;
      padding: 2px 9px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
    }
    .tag-apt  { background: #dbeafe; color: #1d4ed8; }
    .tag-snap { background: #fce7f3; color: #be185d; }
    .tag-flat { background: #d1fae5; color: #065f46; }
    .tag-other{ background: #f1f5f9; color: var(--muted); }

    .empty-state {
      display: none;
      padding: 60px 20px;
      text-align: center;
      color: var(--muted);
    }
    .empty-state p { margin-top: 8px; font-size: 13px; }

    /* ─── Detail pane ────────────────────────────────────── */
    .detail-pane {
      display: flex;
      flex-direction: column;
      background: var(--panel);
      min-height: 0;
    }
    .detail-header {
      padding: 16px;
      border-bottom: 1px solid var(--line);
      background: #f8fafc;
      flex-shrink: 0;
    }
    .detail-header .app-name {
      font-size: 17px;
      font-weight: 700;
      word-break: break-word;
      margin-bottom: 4px;
    }
    .detail-header .app-source {
      font-size: 12px;
      color: var(--muted);
    }
    .detail-body {
      padding: 16px;
      border-bottom: 1px solid var(--line);
      flex-shrink: 0;
    }
    .info-row {
      display: flex;
      gap: 8px;
      margin-bottom: 8px;
      font-size: 13px;
    }
    .info-row:last-child { margin-bottom: 0; }
    .info-label {
      flex-shrink: 0;
      width: 90px;
      color: var(--muted);
      font-weight: 600;
    }
    .info-val { color: var(--ink); word-break: break-word; }
    .loc-section {
      flex: 1;
      display: flex;
      flex-direction: column;
      min-height: 0;
      border-bottom: 1px solid var(--line);
    }
    .loc-title {
      padding: 10px 16px 8px;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .04em;
      color: var(--muted);
      flex-shrink: 0;
    }
    .loc-content {
      flex: 1;
      overflow-y: auto;
      padding: 0 16px 12px;
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 12px;
      color: #334155;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .detail-footer {
      padding: 14px 16px;
      background: #fafafa;
      flex-shrink: 0;
    }
    .uninstall-btn {
      display: block;
      width: 100%;
      height: 42px;
      border: none;
      border-radius: var(--radius);
      font: inherit;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
      background: var(--danger);
      color: #fff;
      letter-spacing: .01em;
    }
    .uninstall-btn:hover { filter: brightness(1.1); }
    .uninstall-btn:disabled {
      background: var(--line);
      color: var(--muted);
      cursor: not-allowed;
      filter: none;
    }
    .footer-note {
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
      text-align: center;
    }

    /* ─── Status bar ─────────────────────────────────────── */
    .status-bar {
      padding: 5px 16px;
      background: var(--panel);
      border-top: 1px solid var(--line);
      font-size: 12px;
      color: var(--muted);
      flex-shrink: 0;
    }

    /* ─── Dialog ─────────────────────────────────────────── */
    dialog {
      width: min(640px, calc(100vw - 32px));
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 0;
      box-shadow: 0 20px 60px rgba(0,0,0,.18);
    }
    dialog::backdrop { background: rgb(0 0 0 / .35); }
    .dlg-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 16px 20px 12px;
      border-bottom: 1px solid var(--line);
    }
    .dlg-head h2 { font-size: 17px; }
    .dlg-body { padding: 16px 20px; }
    .alert-box {
      display: flex;
      gap: 10px;
      padding: 12px 14px;
      border-radius: 8px;
      background: var(--danger-bg);
      border: 1px solid #fca5a5;
      color: var(--danger);
      font-size: 13px;
      margin-bottom: 14px;
    }
    .alert-icon { flex-shrink: 0; font-size: 18px; }
    .info-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 14px; }
    .info-table td { padding: 5px 0; vertical-align: top; }
    .info-table td:first-child { width: 100px; color: var(--muted); font-weight: 600; }
    .cmd-block {
      padding: 10px 12px;
      border-radius: 8px;
      background: #0f172a;
      color: #e2e8f0;
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 12px;
      word-break: break-all;
    }
    pre.output-block {
      max-height: 260px;
      overflow: auto;
      margin-top: 12px;
      padding: 10px 12px;
      border-radius: 8px;
      background: #0f172a;
      color: #94a3b8;
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 12px;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .dlg-footer {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      padding: 12px 20px 16px;
      border-top: 1px solid var(--line);
    }
    .btn {
      height: 38px;
      padding: 0 18px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      background: #fff;
    }
    .btn:hover { border-color: #94a3b8; }
    .btn-danger { background: var(--danger); color: #fff; border-color: var(--danger); }
    .btn-danger:hover { filter: brightness(1.1); }
    .btn:disabled { opacity: .55; cursor: not-allowed; filter: none; }

    /* ─── Responsive ─────────────────────────────────────── */
    @media (max-width: 860px) {
      .workspace { grid-template-columns: 1fr; }
      .detail-pane {
        border-top: 2px solid var(--line);
        max-height: 50vh;
      }
    }
    @media (max-width: 600px) {
      .search-bar { flex-wrap: wrap; }
      .search-bar input[type=search] { flex-basis: 100%; }
    }
  </style>
</head>
<body>

  <!-- Header -->
  <header class="app-header">
    <svg class="icon-20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/></svg>
    <h1>ถอนการติดตั้งแอป</h1>
    <span id="headerCount" class="header-count">กำลังโหลด...</span>
  </header>

  <!-- Search & filter bar -->
  <div class="search-bar">
    <div class="search-wrap">
      <svg class="icon search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
      <input id="search" type="search" placeholder="ค้นหาชื่อแอป เช่น firefox, vlc, python3...">
    </div>
    <select id="managerFilter" aria-label="แหล่งติดตั้ง">
      <option value="">ทุกแหล่งติดตั้ง</option>
    </select>
    <button id="refresh" class="btn-icon"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>รีเฟรช</button>
  </div>

  <!-- Main workspace -->
  <div class="workspace">

    <!-- Left: app list -->
    <div class="list-pane">
      <div class="list-meta">
        <span id="listMeta">กำลังโหลดรายการ...</span>
      </div>
      <div class="list-scroll">
        <table>
          <thead>
            <tr>
              <th class="col-source">แหล่งติดตั้ง</th>
              <th class="col-name">ชื่อแอป / แพ็กเกจ</th>
              <th class="col-ver">เวอร์ชัน</th>
              <th class="col-desc">รายละเอียด</th>
            </tr>
          </thead>
          <tbody id="apps"></tbody>
        </table>
        <div id="emptyState" class="empty-state">
          <strong>ไม่พบรายการที่ตรงกัน</strong>
          <p>ลองล้างคำค้นหรือเปลี่ยนตัวกรองแหล่งติดตั้ง</p>
        </div>
      </div>
    </div>

    <!-- Right: detail panel -->
    <div class="detail-pane">
      <div class="detail-header">
        <div id="detailName" class="app-name">ยังไม่ได้เลือกแอป</div>
        <div id="detailSource" class="app-source">คลิกรายการด้านซ้ายเพื่อดูข้อมูล</div>
      </div>
      <div class="detail-body" id="detailBody">
        <div class="info-row">
          <span class="info-label">สถานะ</span>
          <span class="info-val">ยังไม่มีรายการที่เลือก</span>
        </div>
      </div>
      <div class="loc-section">
        <div class="loc-title"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg> ที่อยู่ไฟล์ในเครื่อง</div>
        <div id="locations" class="loc-content">-</div>
      </div>
      <div class="detail-footer">
        <button id="uninstall" class="uninstall-btn btn-icon" disabled><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>ถอนการติดตั้ง</button>
        <div class="footer-note">จะแสดงคำสั่งให้ยืนยันก่อนลบจริงทุกครั้ง</div>
      </div>
    </div>

  </div>

  <!-- Status bar -->
  <div id="status" class="status-bar">กำลังโหลดรายการแอป...</div>

  <!-- Confirm dialog -->
  <dialog id="dialog">
    <div class="dlg-head">
      <h2 id="dialogTitle">ยืนยันการถอนการติดตั้ง</h2>
    </div>
    <div class="dlg-body">
      <div class="alert-box">
        <span class="alert-icon">⚠️</span>
        <span>การถอนการติดตั้งจะลบแพ็กเกจออกจากระบบจริง ไม่สามารถย้อนกลับได้ และอาจมีหน้าต่างขอรหัสผ่านจากระบบ</span>
      </div>
      <div id="dialogInfo"></div>
      <pre id="dialogOutput" class="output-block" hidden></pre>
    </div>
    <div class="dlg-footer">
      <button id="cancelDialog" class="btn">ยกเลิก</button>
      <button id="confirmDialog" class="btn btn-danger">ยืนยันถอนการติดตั้ง</button>
    </div>
  </dialog>

  <script>
    const appsEl        = document.querySelector('#apps');
    const searchEl      = document.querySelector('#search');
    const managerFilterEl = document.querySelector('#managerFilter');
    const refreshEl     = document.querySelector('#refresh');
    const statusEl      = document.querySelector('#status');
    const listMetaEl    = document.querySelector('#listMeta');
    const headerCountEl = document.querySelector('#headerCount');
    const emptyStateEl  = document.querySelector('#emptyState');
    const detailNameEl  = document.querySelector('#detailName');
    const detailSourceEl= document.querySelector('#detailSource');
    const detailBodyEl  = document.querySelector('#detailBody');
    const locationsEl   = document.querySelector('#locations');
    const uninstallEl   = document.querySelector('#uninstall');
    const dialog        = document.querySelector('#dialog');
    const dialogTitle   = document.querySelector('#dialogTitle');
    const dialogInfoEl  = document.querySelector('#dialogInfo');
    const dialogOutput  = document.querySelector('#dialogOutput');
    const cancelDialog  = document.querySelector('#cancelDialog');
    const confirmDialog = document.querySelector('#confirmDialog');

    let allApps = [];
    let selected = null;

    function keyOf(app) { return `${app.manager}\x00${app.package_id}`; }

    function esc(value) {
      return String(value ?? '').replace(/[&<>'"]/g, c => (
        {'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]
      ));
    }

    function tagClass(manager) {
      if (manager === 'APT/dpkg') return 'tag tag-apt';
      if (manager === 'Snap')     return 'tag tag-snap';
      if (manager === 'Flatpak')  return 'tag tag-flat';
      return 'tag tag-other';
    }

    function filteredApps() {
      const q = searchEl.value.trim().toLowerCase();
      const m = managerFilterEl.value;
      return allApps.filter(app => {
        const matchM = !m || app.manager === m;
        const text = `${app.manager} ${app.name} ${app.package_id} ${app.version} ${app.summary}`.toLowerCase();
        return matchM && (!q || text.includes(q));
      });
    }

    function renderManagers() {
      const managers = [...new Set(allApps.map(a => a.manager))].sort();
      const current = managerFilterEl.value;
      managerFilterEl.innerHTML = '<option value="">ทุกแหล่งติดตั้ง</option>' +
        managers.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
      managerFilterEl.value = managers.includes(current) ? current : '';
    }

    function renderApps() {
      const apps = filteredApps();
      appsEl.innerHTML = apps.map(app => {
        const sel = selected && keyOf(selected) === keyOf(app) ? ' class="selected"' : '';
        return `<tr${sel} data-manager="${esc(app.manager)}" data-package="${esc(app.package_id)}">
          <td class="col-source"><span class="${tagClass(app.manager)}">${esc(app.manager)}</span></td>
          <td class="col-name">${esc(app.name)}</td>
          <td class="col-ver">${esc(app.version || '-')}</td>
          <td class="col-desc">${esc(app.summary || '-')}</td>
        </tr>`;
      }).join('');

      const total = allApps.length.toLocaleString('th-TH');
      const shown = apps.length.toLocaleString('th-TH');
      headerCountEl.textContent = `${total} รายการ`;
      listMetaEl.textContent = apps.length === allApps.length
        ? `แสดงทั้งหมด ${shown} รายการ`
        : `แสดง ${shown} จาก ${total} รายการ`;
      emptyStateEl.style.display = apps.length ? 'none' : 'block';
      statusEl.textContent = `โหลดสำเร็จ — ${total} รายการ`;
    }

    function clearDetail() {
      detailNameEl.textContent = 'ยังไม่ได้เลือกแอป';
      detailSourceEl.textContent = 'คลิกรายการด้านซ้ายเพื่อดูข้อมูล';
      detailBodyEl.innerHTML = `<div class="info-row"><span class="info-label">สถานะ</span><span class="info-val">ยังไม่มีรายการที่เลือก</span></div>`;
      locationsEl.textContent = '-';
      uninstallEl.disabled = true;
    }

    function selectApp(app) {
      selected = app;
      detailNameEl.textContent = app.name;
      detailSourceEl.textContent = `${app.manager}  ·  เวอร์ชัน ${app.version || '-'}`;
      detailBodyEl.innerHTML = `
        <div class="info-row"><span class="info-label">แพ็กเกจ</span><span class="info-val">${esc(app.package_id)}</span></div>
        <div class="info-row"><span class="info-label">รายละเอียด</span><span class="info-val">${esc(app.summary || '-')}</span></div>
      `;
      uninstallEl.disabled = !['APT/dpkg', 'Snap', 'Flatpak'].includes(app.manager);
      locationsEl.textContent = 'กำลังค้นหา...';
      renderApps();
      fetch(`/api/locations?manager=${encodeURIComponent(app.manager)}&package_id=${encodeURIComponent(app.package_id)}`)
        .then(r => r.json())
        .then(data => {
          locationsEl.textContent = data.locations.length ? data.locations.join('\n') : 'ไม่พบข้อมูลที่อยู่จัดเก็บ';
        })
        .catch(err => { locationsEl.textContent = `ผิดพลาด: ${err}`; });
    }

    async function loadApps() {
      statusEl.textContent = 'กำลังโหลดรายการแอป...';
      refreshEl.disabled = true;
      const r = await fetch('/api/apps');
      allApps = await r.json();
      refreshEl.disabled = false;
      renderManagers();
      renderApps();
      if (selected) {
        const next = allApps.find(a => keyOf(a) === keyOf(selected));
        next ? selectApp(next) : (selected = null, clearDetail());
      } else {
        clearDetail();
      }
    }

    appsEl.addEventListener('click', e => {
      const row = e.target.closest('tr[data-package]');
      if (!row) return;
      const app = allApps.find(a => a.manager === row.dataset.manager && a.package_id === row.dataset.package);
      if (app) selectApp(app);
    });

    searchEl.addEventListener('input', renderApps);
    managerFilterEl.addEventListener('change', renderApps);
    refreshEl.addEventListener('click', loadApps);
    cancelDialog.addEventListener('click', () => dialog.close());

    uninstallEl.addEventListener('click', async () => {
      if (!selected) return;
      const r = await fetch(`/api/command?manager=${encodeURIComponent(selected.manager)}&package_id=${encodeURIComponent(selected.package_id)}`);
      const { command } = await r.json();
      dialogTitle.textContent = `ถอนการติดตั้ง: ${selected.name}`;
      dialogInfoEl.innerHTML = `
        <table class="info-table">
          <tr><td>แพ็กเกจ</td><td>${esc(selected.package_id)}</td></tr>
          <tr><td>แหล่งติดตั้ง</td><td>${esc(selected.manager)}</td></tr>
          <tr><td>คำสั่ง</td><td><div class="cmd-block">${esc(command.join(' '))}</div></td></tr>
        </table>
      `;
      dialogOutput.hidden = true;
      dialogOutput.textContent = '';
      confirmDialog.hidden = false;
      confirmDialog.disabled = false;
      confirmDialog.textContent = 'ยืนยันถอนการติดตั้ง';
      dialog.showModal();
    });

    confirmDialog.addEventListener('click', async () => {
      if (!selected) return;
      confirmDialog.disabled = true;
      confirmDialog.textContent = 'กำลังถอนการติดตั้ง...';
      dialogOutput.hidden = false;
      dialogOutput.textContent = 'กำลังรัน — อาจมีหน้าต่างรหัสผ่านจากระบบ...\n';
      const r = await fetch('/api/uninstall', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ manager: selected.manager, package_id: selected.package_id })
      });
      const data = await r.json();
      dialogOutput.textContent += `\n$ ${data.command.join(' ')}\n\n${data.output || ''}\n[รหัสผลลัพธ์: ${data.code}]`;
      confirmDialog.disabled = false;
      confirmDialog.hidden = true;
      await loadApps();
    });

    clearDetail();
    loadApps().catch(err => { statusEl.textContent = `โหลดไม่สำเร็จ: ${err}`; });
  </script>
</body>
</html>
'''


class UninstallerHandler(BaseHTTPRequestHandler):
    server_version = "LinuxUninstaller/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(HTML)
            return
        if parsed.path == "/api/apps":
            self.send_json([asdict(app) for app in load_all_apps()])
            return
        if parsed.path == "/api/locations":
            app = self.app_from_query(parsed.query)
            self.send_json({"locations": installed_locations(app) if app else []})
            return
        if parsed.path == "/api/command":
            app = self.app_from_query(parsed.query)
            self.send_json({"command": uninstall_command(app) if app else []})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/uninstall":
            self.send_error(HTTPStatus.NOT_FOUND)
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
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Linux Uninstaller.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
