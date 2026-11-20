#!/usr/bin/env python3
"""
Puts the "Deployment Type" donut panel back, directly next to the
"Team-wise Deployment Breakdown" panel.

Usage:  python patch_deployment_type.py  templates\\manager.html
Backup: manager.html.type.bak (written only if every step succeeds)
"""
import re, sys, shutil

PANEL = r'''

        <div class="panel">
            <div class="panelhead">
                <div>
                    <h3>Deployment Type</h3>
                    <div class="sub">Automation versus manual deployment</div>
                </div>
            </div>
            <div class="donutwrap">
                <div class="donut" id="typeDonut">
                    <div class="donutcenter">
                        <strong id="typeTotal">0</strong>
                        <span>deployments</span>
                    </div>
                </div>
                <div class="donutlegend" id="typeLegend"></div>
            </div>
        </div>'''


def sub_once(text, pattern, make, label, flags=0):
    ms = list(re.finditer(pattern, text, flags))
    if len(ms) != 1:
        sys.exit(f"ERROR: anchor '{label}' matched {len(ms)} times (expected 1). Nothing was written.")
    m = ms[0]
    return text[:m.start()] + make(m) + text[m.end():]


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    src = open(path, encoding="utf-8").read()
    if 'id="typeDonut"' in src:
        sys.exit("A Deployment Type panel already exists in this file. Nothing changed.")
    # 1) panel right after the Team-wise Deployment Breakdown panel (same grid row)
    src = sub_once(src, r'<div class="team-deployment-empty">[\s\S]*?\{% endif %\}\s*</div>',
                   lambda m: m.group(0) + PANEL, "Team-wise Deployment Breakdown panel")
    # 2) draw the donut again (the earlier Running Status patch stopped calling it)
    if not re.search(r"^[ \t]*buildDonut\(\);", src, re.M):
        src = sub_once(src, r"^([ \t]*)buildStatusBars\(\);",
                       lambda m: m.group(1) + "buildDonut();\n" + m.group(0), "buildStatusBars() call", re.M)
    shutil.copy2(path, path + ".type.bak")
    open(path, "w", encoding="utf-8").write(src)
    print("Patched OK. Backup: manager.html.type.bak")


if __name__ == "__main__":
    main()