"""Adds the running-animation / completed-green CSS to a template.
Usage:  python patch10.py templates/manager.html
"""
import sys, shutil

MARKER = "/* ===== Job state: RUNNING = animated, COMPLETED = green ===== */"

CSS = r'''
/* ===== Job state: RUNNING = animated, COMPLETED = green ===== */

@keyframes jobStripes   { from { background-position: 0 0; } to { background-position: 40px 0; } }
@keyframes jobGlow      { 0%,100% { box-shadow: 0 8px 20px #f59e0b45, 0 0 0 0 #f59e0b66; }
                          50%     { box-shadow: 0 8px 26px #f59e0b70, 0 0 0 7px #f59e0b00; } }
@keyframes jobSpin      { to { transform: rotate(360deg); } }
@keyframes jobBlink     { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
@keyframes jobTick      { 0%,100% { transform: scale(1); } 50% { transform: scale(1.04); } }

/* ---- Running Status cards / rows / category cells ---- */
.rs-card.cs-running, .rsp-row.cs-running, .cat-cell.cs-running,
.rs-card.running,    .rsp-row.running {
    background-image:
        repeating-linear-gradient(135deg, #ffffff22 0 10px, transparent 10px 20px),
        linear-gradient(135deg, #d97706 0%, #f59e0b 60%, #fbbf24 100%);
    background-size: 40px 40px, 100% 100%;
    animation: jobStripes 1s linear infinite, jobGlow 1.8s ease-in-out infinite;
}
/* rollback running keeps its red/orange identity but still animates */
.rs-card.cs-failed.running, .rsp-row.cs-failed.running, .rs-card.cs-rb-running {
    background-image:
        repeating-linear-gradient(135deg, #ffffff22 0 10px, transparent 10px 20px),
        linear-gradient(135deg, #be123c 0%, #ea580c 100%);
    background-size: 40px 40px, 100% 100%;
}

/* ticking timer */
.rs-card.running .rs-time, .rsp-row.running .rsp-time, .cat-cell.cs-running .cc-time {
    animation: jobTick 1s ease-in-out infinite;
}

/* spinner in the pill */
.rs-card.running .rs-pill::before, .rsp-row.running .rsp-pill::before {
    width: 9px; height: 9px; margin-right: 6px;
    border-radius: 50%; background: transparent;
    border: 2px solid #ffffff55; border-top-color: #fff;
    animation: jobSpin .8s linear infinite;
}
.cat-cell.cs-running .cc-pill::before {
    content: ""; display: inline-block; width: 8px; height: 8px; margin-right: 5px;
    border-radius: 50%; border: 2px solid #ffffff55; border-top-color: #fff;
    animation: jobSpin .8s linear infinite; vertical-align: -1px;
}

/* thicker moving progress bar */
.rs-card.running .rs-bar, .rsp-row.running .rsp-bar { height: 5px; }

/* ---- Completed = solid green, no animation ---- */
.rs-card.cs-completed, .rsp-row.cs-completed, .cat-cell.cs-completed {
    background: linear-gradient(135deg, #047857 0%, #10b981 100%);
    box-shadow: 0 8px 20px #10b98138;
    animation: none;
}
.rs-card.cs-completed .rs-pill::before,
.rsp-row.cs-completed .rsp-pill::before,
.cat-cell.cs-completed .cc-pill::before { content: "✓ "; font-weight: 900; }

/* ---- Jenkins Build Status table pills ---- */
.jenkins-status-panel .jenkins-state.success { background: #16a34a; color: #fff; }
.jenkins-status-panel .jenkins-state.success::before { content: "✓ "; font-weight: 900; }
.jenkins-status-panel .jenkins-state.running {
    background: #f59e0b; color: #fff;
    background-image: repeating-linear-gradient(135deg, #ffffff30 0 6px, transparent 6px 12px);
    background-size: 24px 24px;
    animation: jobStripes .8s linear infinite;
}
.jenkins-status-panel .jenkins-state.running::before {
    content: ""; display: inline-block; width: 8px; height: 8px; margin-right: 6px;
    border-radius: 50%; border: 2px solid #ffffff66; border-top-color: #fff;
    animation: jobSpin .8s linear infinite; vertical-align: -1px;
}

/* ---- Jenkins Live Room tiles / executions ---- */
.lr-tile.running { border-color: #f59e0b; animation: jobGlow 1.8s ease-in-out infinite; }
.lr-tile.running .lr-avatar::after { border-color: #f59e0b; }
.lr-badge.running { background: #f59e0b; animation: jobBlink 1.2s ease-in-out infinite; }
.lr-tile.success { border-color: #22c55e; box-shadow: 0 0 0 3px #22c55e22; }
.lr-badge.success { background: #16a34a; color: #fff; }
.lr-exec .st.running { background: #f59e0b; color: #fff; animation: jobBlink 1.2s ease-in-out infinite; }
.lr-exec .st.success { background: #16a34a; color: #fff; }
'''

def main():
    if len(sys.argv) != 2:
        print("Usage: python patch10.py <path-to-template.html>")
        sys.exit(1)
    path = sys.argv[1]
    with open(path, "r", encoding="utf-8", newline="") as f:
        html = f.read()
    if MARKER in html:
        print("Already patched - nothing to do.")
        return
    idx = html.find("</style>")
    if idx == -1:
        print("ERROR: no </style> tag found in", path)
        sys.exit(1)
    shutil.copyfile(path, path + ".bak")
    css = CSS.replace("\n", "\r\n") if "\r\n" in html else CSS
    html = html[:idx] + css.lstrip("\r\n") + "\n" + html[idx:]
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(html)
    print("Patched", path, "(backup saved as", path + ".bak)")

if __name__ == "__main__":
    main()