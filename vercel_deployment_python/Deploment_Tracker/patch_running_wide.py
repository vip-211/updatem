#!/usr/bin/env python3
"""
Makes the Running Status panel cover the full width of the dashboard (its own wide row at the
top), shows up to 8 live cards, and makes the "Could not refresh" message tell you the real reason.

New layout (desktop):
    [ Running Status ............................ full width ]
    [ Deployment Activity (wide) ........ ] [ Status Breakdown ]
    [ Team-wise Deployment ] [ Deployment Type ] [ (continues) ]

Usage:  python patch_running_wide.py  templates\\manager.html
Needs the Running Status panel patch. Backup: manager.html.wide.bak
"""
import re, sys, shutil

CSS = r'''
/* ===== Running Status wide layout ===== */
.rsp-stats { max-width:640px; }
.rsp-list { grid-template-columns:repeat(auto-fill,minmax(270px,1fr)); max-height:none; overflow:visible; gap:12px; }
.rsp-time { font-size:24px; }
.rsp-empty, .rsp-more { grid-column:1/-1; }
@media (min-width:1251px) {
    .gridcharts .rsp-panel { grid-column:1 / -1; grid-row:1; }
    .gridcharts > .panel:first-child:not(.rsp-panel) { grid-column:1 / 3; grid-row:2; }
    .gridcharts > .panel:has(#statusBars) { grid-column:3; grid-row:2 / 4; }
    .gridcharts > .team-deployment-panel { grid-column:1; grid-row:3; }
    .gridcharts > .panel:has(#typeDonut) { grid-column:2; grid-row:3; }
}
@media (max-width:1250px) {
    .gridcharts .rsp-panel { grid-column:1 / -1; order:-1; }
}
'''


def replace_once(text, old, new, label):
    if text.count(old) != 1:
        sys.exit(f"ERROR: '{label}' found {text.count(old)} times (expected 1). Nothing was written.")
    return text.replace(old, new, 1)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    src = open(path, encoding="utf-8").read()
    if "Running Status wide layout" in src:
        sys.exit("Already patched.")
    if "rspList" not in src:
        sys.exit("The Running Status panel is not in this file. Run patch_running_panel.py first.")
    src = replace_once(src, "const MAX = 5;", "const MAX = 8;", "card limit")
    src = replace_once(
        src,
        "const data = await fetch('/api/running-status', { cache: 'no-store' }).then(r => r.json());",
        "const resp = await fetch('/api/running-status', { cache: 'no-store' });\n"
        "            let data;\n"
        "            try { data = await resp.json(); } catch (_) { throw new Error('HTTP ' + resp.status + ', not JSON (route missing? restart Flask)'); }",
        "fetch call")
    src = replace_once(
        src,
        "sub.textContent = 'Could not refresh. Open via Flask: http://127.0.0.1:5050/manager';",
        "sub.textContent = 'Could not refresh: ' + (e && e.message ? e.message : e) + ' - test http://127.0.0.1:5050/api/running-status';",
        "error message")
    src = replace_once(src, "</style>", CSS + "</style>", "</style>")
    shutil.copy2(path, path + ".wide.bak")
    open(path, "w", encoding="utf-8").write(src)
    print("Patched OK. Backup: manager.html.wide.bak")


if __name__ == "__main__":
    main()