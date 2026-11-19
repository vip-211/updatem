#!/usr/bin/env python3
"""
Patch: show rollback timers in the Running Status cards (red "Failed / Rolled Back" group).

Usage (run from the project folder):
    python apply_rollback_patch.py                      # uses app.py and templates/manager.html
    python apply_rollback_patch.py path/app.py path/manager.html

Creates app.py.bak and manager.html.bak first. Restart Flask afterwards.
"""
import re
import shutil
import sys
from pathlib import Path

app_py = Path(sys.argv[1] if len(sys.argv) > 1 else "app.py")
html = Path(sys.argv[2] if len(sys.argv) > 2 else "templates/manager.html")


def flex(text):
    """Regex that matches `text` regardless of whitespace/indentation differences."""
    return r"\s+".join(re.escape(tok) for tok in text.split())


def sub_once(src, old, new, label, between=None):
    pattern = flex(old) if between is None else flex(old) + r".*?" + flex(between)
    matches = list(re.finditer(pattern, src, flags=re.S))
    if len(matches) != 1:
        sys.exit(f"[FAILED] {label}: expected 1 match, found {len(matches)}. Nothing was written.")
    m = matches[0]
    return src[:m.start()] + new + src[m.end():]


# ----------------------------------------------------------------------------
# app.py
# ----------------------------------------------------------------------------
APP_QUERY_OLD = '''ensure_timer_history_table(cur)
cur.execute("""
SELECT d.id, d.cr_number, d.rfid, d.application_name, d.status,
d.change_type, d.roll_back, d.slots,
t.started_at, t.stopped_at,
COALESCE(t.accumulated_seconds, 0), COALESCE(t.is_running, FALSE),
d.category
FROM public.deployment_tracker d
LEFT JOIN public.deployment_running_timers t ON t.deployment_id = d.id
WHERE d.deployment_date = %s
ORDER BY d.slot_start NULLS LAST, d.id'''

APP_QUERY_NEW = '''ensure_timer_history_table(cur)
                ensure_rollback_timer_tables(cur)
                cur.execute("""
                    SELECT d.id, d.cr_number, d.rfid, d.application_name, d.status,
                           d.change_type, d.roll_back, d.slots,
                           t.started_at, t.stopped_at,
                           COALESCE(t.accumulated_seconds, 0), COALESCE(t.is_running, FALSE),
                           d.category,
                           rt.started_at, rt.stopped_at,
                           COALESCE(rt.accumulated_seconds, 0), COALESCE(rt.is_running, FALSE)
                    FROM public.deployment_tracker d
                    LEFT JOIN public.deployment_running_timers t ON t.deployment_id = d.id
                    LEFT JOIN public.deployment_rollback_timers rt ON rt.deployment_id = d.id
                    WHERE d.deployment_date = %s
                    ORDER BY d.slot_start NULLS LAST, d.id'''

APP_LOOP_START = '''for (dep_id, cr, rfid, app_name, status, ctype, rb, slots,
started_at, stopped_at, acc, is_running, cr_category) in rows:'''
APP_LOOP_END = '''items.sort(key=lambda i: 0 if i["category"] == "running" else 1)'''

APP_LOOP_NEW = '''for (dep_id, cr, rfid, app_name, status, ctype, rb, slots,
             started_at, stopped_at, acc, is_running, cr_category,
             rb_started, rb_stopped, rb_acc, rb_running) in rows:
            is_running = bool(is_running)
            rb_running = bool(rb_running)
            rb_has_timer = rb_started is not None

            if rb_has_timer:
                # Rollback was started: show the rollback timer in the red "rolled back" group.
                elapsed = int(rb_acc or 0)
                if rb_running and rb_started:
                    elapsed += max(0, int((now - rb_started).total_seconds()))
                category = "failed"
                label = "ROLLBACK RUNNING" if rb_running else "ROLLED BACK"
                out_started, out_stopped = rb_started, rb_stopped
                out_running, out_has_timer = rb_running, True
            else:
                elapsed = int(acc or 0)
                if is_running and started_at:
                    elapsed += max(0, int((now - started_at).total_seconds()))
                category, label = running_category(status, ctype, rb, is_running)
                out_started, out_stopped = started_at, stopped_at
                out_running, out_has_timer = is_running, started_at is not None

            items.append({
                "deployment_id": dep_id, "cr_number": cr, "rfid": rfid,
                "application_name": app_name, "status": status, "change_type": ctype,
                "roll_back": rb, "slots": slots,
                "started_at": out_started.isoformat() if out_started else None,
                "stopped_at": out_stopped.isoformat() if out_stopped else None,
                "has_timer": out_has_timer,
                "is_running": out_running, "elapsed_seconds": elapsed,
                "rollback_timer": rb_has_timer,
                "rb_running": rb_has_timer and rb_running,
                "category": category, "category_label": label,
                "cr_category": (cr_category or "Uncategorized").strip() or "Uncategorized",
            })
        # running (deployment or rollback) first; stable otherwise
        items.sort(key=lambda i: 0 if (i["category"] == "running" or i["rb_running"]) else 1)'''

# ----------------------------------------------------------------------------
# manager.html
# ----------------------------------------------------------------------------
HTML_EDITS = [
    (
        "dashboard panel meta line",
        "meta = (t.is_running ? 'Started ' + fmtShort(t.started_at) : 'Stopped ' + fmtShort(t.stopped_at)) + ' · ' + (t.status || '');",
        "meta = (t.is_running ? (t.rollback_timer ? 'Rollback started ' : 'Started ') + fmtShort(t.started_at) : (t.rollback_timer ? 'Rollback stopped ' : 'Stopped ') + fmtShort(t.stopped_at)) + ' · ' + (t.status || '');",
    ),
    (
        "dashboard panel row class",
        """'<div class="rsp-row cs-' + c + (c === 'running' ? ' running' : '') + '" data-dep-id=""",
        """'<div class="rsp-row cs-' + c + ((c === 'running' || t.rb_running) ? ' running' : '') + '" data-dep-id=""",
    ),
    (
        "full view meta line",
        "meta = t.is_running ? 'Started ' + fmtShort(t.started_at) : 'Stopped ' + fmtShort(t.stopped_at);",
        "meta = t.is_running ? (t.rollback_timer ? 'Rollback started ' : 'Started ') + fmtShort(t.started_at) : (t.rollback_timer ? 'Rollback stopped ' : 'Stopped ') + fmtShort(t.stopped_at);",
    ),
    (
        "full view card class",
        """'<div class="rs-card cs-' + c + (c === 'running' ? ' running' : '') + '" data-dep-id=""",
        """'<div class="rs-card cs-' + c + ((c === 'running' || t.rb_running) ? ' running' : '') + '" data-dep-id=""",
    ),
]

CSS_MARK = '/* ===== Rollback status (manager "Rolled Back" view) ===== */'
CSS_NEW = '''/* Running rollback = stronger red/orange than a finished one */
.rsp-row.cs-failed.running, .rs-card.cs-failed.running {
    background: linear-gradient(135deg,#be123c 0%,#ea580c 100%);
    box-shadow: 0 8px 20px #ea580c40;
}

''' + CSS_MARK


def main():
    for p in (app_py, html):
        if not p.exists():
            sys.exit(f"File not found: {p}")

    a = app_py.read_text(encoding="utf-8")
    h = html.read_text(encoding="utf-8")

    if "rb_has_timer" in a and "rb_running" in h:
        print("Already patched. Nothing to do.")
        return

    a = sub_once(a, APP_QUERY_OLD, APP_QUERY_NEW, "app.py: running_daily query")
    a = sub_once(a, APP_LOOP_START, APP_LOOP_NEW, "app.py: running_daily loop", between=APP_LOOP_END)

    for label, old, new in HTML_EDITS:
        h = sub_once(h, old, new.replace("\\", "\\\\"), "manager.html: " + label)

    if CSS_MARK not in h:
        sys.exit("[FAILED] manager.html: CSS anchor not found. Nothing was written.")
    h = h.replace(CSS_MARK, CSS_NEW, 1)

    shutil.copy2(app_py, str(app_py) + ".bak")
    shutil.copy2(html, str(html) + ".bak")
    app_py.write_text(a, encoding="utf-8")
    html.write_text(h, encoding="utf-8")
    print("Patched app.py and manager.html (backups: *.bak). Restart Flask.")


if __name__ == "__main__":
    main()