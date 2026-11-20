#!/usr/bin/env python3
"""
Adds "Running Status" (live timers + Start/Stop history, with CR number)
to the Deployment Tracker manager page.

Usage:   python apply_running_status.py  path/to/app.py  path/to/templates/manager.html
Creates: app.py.bak and manager.html.bak, then patches both files in place.
Every edit is anchored and asserted; if an anchor is missing or ambiguous the
script stops WITHOUT writing anything.
"""
import re, sys, shutil, textwrap

APP_PY_FUNC = r'''def ensure_timer_history_table(cur):
    """One row per Start->Stop session. No FK, so history survives deleted requests."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS public.deployment_timer_history (
            id BIGSERIAL PRIMARY KEY,
            deployment_id BIGINT NOT NULL,
            cr_number TEXT,
            started_at TIMESTAMPTZ NOT NULL,
            stopped_at TIMESTAMPTZ,
            duration_seconds BIGINT
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_timer_history_cr
        ON public.deployment_timer_history (cr_number, started_at DESC)
    """)


'''

APP_PY_LOG = r'''if action == "start":
    cur.execute(
        "INSERT INTO public.deployment_timer_history (deployment_id, cr_number, started_at) VALUES (%s, %s, %s)",
        (deployment_id, cr_number, now),
    )
else:
    cur.execute("""
        UPDATE public.deployment_timer_history
        SET stopped_at = %s, duration_seconds = %s
        WHERE id = (
            SELECT id FROM public.deployment_timer_history
            WHERE deployment_id = %s AND stopped_at IS NULL
            ORDER BY id DESC LIMIT 1
        )
    """, (now, elapsed, deployment_id))
'''

APP_PY_ROUTES = r'''@app.route("/api/running-status", methods=["GET"])
def running_status():
    """Timers that have been started, with CR number, for the Manager page."""
    only_running = request.args.get("only_running", "false").lower() in ("1", "true", "yes")
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                ensure_running_timer_table(cur)
                cur.execute("""
                    SELECT t.deployment_id, d.cr_number, d.rfid, d.application_name,
                           d.status, d.deployment_date, d.slots,
                           t.started_at, t.stopped_at,
                           t.accumulated_seconds, t.is_running
                    FROM public.deployment_running_timers t
                    JOIN public.deployment_tracker d ON d.id = t.deployment_id
                    WHERE t.started_at IS NOT NULL
                      AND (%s = FALSE OR t.is_running = TRUE)
                    ORDER BY t.is_running DESC, t.started_at DESC
                """, (only_running,))
                rows = cur.fetchall()

        now = datetime.now().astimezone()
        timers = []
        for (dep_id, cr, rfid, app_name, status, dep_date, slots,
             started_at, stopped_at, acc, is_running) in rows:
            elapsed = int(acc or 0)
            if is_running and started_at:
                elapsed += max(0, int((now - started_at).total_seconds()))
            timers.append({
                "deployment_id": dep_id,
                "cr_number": cr,
                "rfid": rfid,
                "application_name": app_name,
                "deployment_status": status,
                "deployment_date": dep_date.isoformat() if dep_date else None,
                "slots": slots,
                "started_at": started_at.isoformat() if started_at else None,
                "stopped_at": stopped_at.isoformat() if stopped_at else None,
                "is_running": bool(is_running),
                "running_status": "Running" if is_running else "Stopped",
                "elapsed_seconds": elapsed,
            })
        return jsonify({
            "success": True,
            "count": len(timers),
            "running_count": sum(1 for t in timers if t["is_running"]),
            "server_time": now.isoformat(),
            "timers": timers,
        })
    except Exception:
        app.logger.exception("Could not load running status")
        return jsonify({"success": False, "message": "Unable to load running status."}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route("/api/running-history", methods=["GET"])
def running_history():
    """Every Start/Stop session with its CR number, newest first."""
    cr = (request.args.get("cr") or "").strip()
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                ensure_timer_history_table(cur)
                cur.execute("""
                    SELECT h.id, h.deployment_id, h.cr_number, d.rfid, d.application_name,
                           h.started_at, h.stopped_at, h.duration_seconds
                    FROM public.deployment_timer_history h
                    LEFT JOIN public.deployment_tracker d ON d.id = h.deployment_id
                    WHERE (%s = '' OR h.cr_number ILIKE %s)
                    ORDER BY h.started_at DESC
                    LIMIT 500
                """, (cr, f"%{cr}%"))
                rows = cur.fetchall()
        now = datetime.now().astimezone()
        history = []
        for hid, dep_id, cr_no, rfid, app_name, started, stopped, dur in rows:
            running = stopped is None
            history.append({
                "id": hid, "deployment_id": dep_id, "cr_number": cr_no,
                "rfid": rfid, "application_name": app_name,
                "started_at": started.isoformat() if started else None,
                "stopped_at": stopped.isoformat() if stopped else None,
                "is_running": running,
                "duration_seconds": int(dur) if dur is not None
                    else max(0, int((now - started).total_seconds())),
            })
        return jsonify({"success": True, "count": len(history), "history": history})
    except Exception:
        app.logger.exception("Could not load running history")
        return jsonify({"success": False, "message": "Unable to load running history."}), 500
    finally:
        if conn is not None:
            conn.close()


'''

HTML_MENU = r'''
        <button class="fab-item" type="button" role="menuitem" data-open-modal="runningStatusModal" aria-haspopup="dialog" aria-expanded="false">
            <span class="fab-ico">⏱<span class="mgr-live-badge" id="rsBadge" hidden>0</span></span><span class="fab-txt"><b>Running Status</b><small>Live timers and Start/Stop history</small></span></button>'''

HTML_CSS = r'''.fab-item:nth-of-type(6) .fab-ico { background:#e0e7ff; color:#4338ca; }
'''

HTML_MODAL = r'''    <!-- ===== Running Status (live timers + Start/Stop history) ===== -->
    <div class="mgr-modal" id="runningStatusModal" data-modal hidden>
    <div class="mgr-modal-box" role="dialog" aria-modal="true" aria-label="Running Status">
    <section class="panel records">
        <div class="panelhead">
            <div>
                <h3>Running Status</h3>
                <div class="sub">Deployments started from the DevOps workspace, by CR number. Refreshes every 10 seconds while open.</div>
            </div>
            <div class="mgr-head-actions">
                <button class="btn" type="button" id="rsTabLive">Live <span id="rsLiveCount">0</span></button>
                <button class="btn light" type="button" id="rsTabHistory">History</button>
                <button class="mgr-close" type="button" data-close-modal aria-label="Close Running Status">✕</button>
            </div>
        </div>
        <div class="history-tools">
            <div class="history-search-wrap">
                <input id="rsSearch" type="search" placeholder="Search CR number..." autocomplete="off" aria-label="Search running status by CR number">
                <button class="btn light" type="button" id="rsClear">Clear</button>
            </div>
            <span class="history-count" id="rsCount" aria-live="polite"></span>
        </div>
        <div class="tablewrap">
            <table style="min-width:900px">
                <thead id="rsHead"></thead>
                <tbody id="rsBody"><tr><td class="empty">Loading…</td></tr></tbody>
            </table>
        </div>
    </section>
    </div>
    </div>

'''

HTML_SCRIPT = r'''<script>
(function runningStatusModal() {
    const modal = document.getElementById('runningStatusModal');
    if (!modal) return;
    const body = document.getElementById('rsBody'), head = document.getElementById('rsHead');
    const search = document.getElementById('rsSearch'), countEl = document.getElementById('rsCount');
    const tabLive = document.getElementById('rsTabLive'), tabHist = document.getElementById('rsTabHistory');
    const liveCount = document.getElementById('rsLiveCount'), badge = document.getElementById('rsBadge');
    let tab = 'live', live = [], history = [], pollId = null, tickId = null;

    const esc = s => String(s ?? '').replace(/[&<>"']/g,
        c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const fmtT = iso => iso ? new Date(iso).toLocaleString() : '—';
    const fmtD = s => { s = Math.max(0, s|0); const p = n => String(n).padStart(2,'0');
        return p(Math.floor(s/3600)) + ':' + p(Math.floor(s%3600/60)) + ':' + p(s%60); };
    const state = r => r.is_running
        ? '<span class="jenkins-state running">● Running</span>'
        : '<span class="jenkins-state">■ Stopped</span>';

    function render() {
        const q = search.value.trim().toLowerCase();
        const match = r => !q || String(r.cr_number || '').toLowerCase().includes(q);
        if (tab === 'live') {
            head.innerHTML = '<tr><th>CR Number</th><th>RFID</th><th>Application</th><th>Deployment Status</th><th>Started At</th><th>Stopped At</th><th>Elapsed (total)</th><th>Running Status</th></tr>';
            const rows = live.filter(match);
            countEl.textContent = rows.length + ' timer' + (rows.length === 1 ? '' : 's');
            body.innerHTML = rows.length ? rows.map(r =>
                '<tr><td><strong>' + esc(r.cr_number) + '</strong></td><td>' + esc(r.rfid) + '</td><td>' + esc(r.application_name) +
                '</td><td>' + esc(r.deployment_status) + '</td><td>' + fmtT(r.started_at) + '</td><td>' + (r.is_running ? '—' : fmtT(r.stopped_at)) +
                '</td><td data-live-id="' + r.deployment_id + '">' + fmtD(r.elapsed_seconds) + '</td><td>' + state(r) + '</td></tr>'
            ).join('') : '<tr><td colspan="8" class="empty">No deployment has been started yet.</td></tr>';
        } else {
            head.innerHTML = '<tr><th>SR No.</th><th>CR Number</th><th>RFID</th><th>Application</th><th>Started At</th><th>Stopped At</th><th>Duration</th><th>Status</th></tr>';
            const rows = history.filter(match);
            countEl.textContent = rows.length + ' session' + (rows.length === 1 ? '' : 's');
            body.innerHTML = rows.length ? rows.map((r, i) =>
                '<tr><td>' + (i + 1) + '</td><td><strong>' + esc(r.cr_number) + '</strong></td><td>' + esc(r.rfid) + '</td><td>' + esc(r.application_name) +
                '</td><td>' + fmtT(r.started_at) + '</td><td>' + (r.is_running ? '—' : fmtT(r.stopped_at)) +
                '</td><td data-hist-id="' + r.id + '">' + fmtD(r.duration_seconds) + '</td><td>' + state(r) + '</td></tr>'
            ).join('') : '<tr><td colspan="8" class="empty">No Start/Stop history found.</td></tr>';
        }
    }

    async function load() {
        try {
            const [a, b] = await Promise.all([
                fetch('/api/running-status', {cache:'no-store'}).then(r => r.json()),
                fetch('/api/running-history', {cache:'no-store'}).then(r => r.json())
            ]);
            if (a.success) { live = a.timers; liveCount.textContent = a.running_count;
                badge.textContent = a.running_count; badge.hidden = !a.running_count; }
            if (b.success) history = b.history;
            render();
        } catch (e) {
            body.innerHTML = '<tr><td colspan="8" class="empty">Could not load running status.</td></tr>';
        }
    }

    function setTab(t) {
        tab = t;
        tabLive.className = 'btn' + (t === 'live' ? '' : ' light');
        tabHist.className = 'btn' + (t === 'history' ? '' : ' light');
        render();
    }

    tabLive.addEventListener('click', () => setTab('live'));
    tabHist.addEventListener('click', () => setTab('history'));
    search.addEventListener('input', render);
    document.getElementById('rsClear').addEventListener('click', () => { search.value = ''; render(); });

    modal.addEventListener('modal:open', () => {
        load(); setTab('live');
        clearInterval(pollId); clearInterval(tickId);
        pollId = setInterval(() => { if (modal.hidden) { clearInterval(pollId); clearInterval(tickId); } else load(); }, 10000);
        tickId = setInterval(() => {   // tick running timers locally every second
            if (modal.hidden) return;
            live.forEach(r => { if (!r.is_running) return; r.elapsed_seconds++;
                const c = body.querySelector('[data-live-id="' + r.deployment_id + '"]'); if (c) c.textContent = fmtD(r.elapsed_seconds); });
            history.forEach(r => { if (!r.is_running) return; r.duration_seconds++;
                const c = body.querySelector('[data-hist-id="' + r.id + '"]'); if (c) c.textContent = fmtD(r.duration_seconds); });
        }, 1000);
    });

    load();   // preload so the Views badge shows the running count on page load
})();
</script>

'''


def sub_once(text, pattern, make, label, flags=0):
    ms = list(re.finditer(pattern, text, flags))
    if len(ms) != 1:
        sys.exit(f"ERROR: anchor '{label}' matched {len(ms)} times (expected 1). Nothing was written.")
    m = ms[0]
    return text[:m.start()] + make(m) + text[m.end():]


def patch_app(src):
    if "ensure_timer_history_table" in src:
        sys.exit("app.py already patched.")
    # 1) history table helper
    src = sub_once(src, r"def serialize_running_timer\(row\):",
                   lambda m: APP_PY_FUNC + m.group(0), "serialize_running_timer")
    # 2) create history table + fetch CR number in the start/stop route
    def p2(m):
        i = m.group(1)
        return (f'{i}ensure_timer_history_table(cur)\n'
                f'{i}cur.execute("SELECT cr_number FROM public.deployment_tracker WHERE id = %s", (deployment_id,))\n'
                f'{i}dep_row = cur.fetchone()\n'
                f'{i}if not dep_row:\n'
                f'{i}    return jsonify({{"success": False, "message": "Deployment record not found."}}), 404\n'
                f'{i}cr_number = dep_row[0]')
    src = sub_once(
        src,
        r'( *)cur\.execute\("SELECT 1 FROM public\.deployment_tracker WHERE id = %s", \(deployment_id,\)\)\s*\n\s*'
        r'if not cur\.fetchone\(\):\s*\n\s*return jsonify\(\{"success": False, "message": "Deployment record not found\."\}\), 404',
        p2, "timer route record check")
    # 3) log the session AFTER the RETURNING row has been fetched
    src = sub_once(src, r"( *)updated = cur\.fetchone\(\)",
                   lambda m: m.group(0) + "\n" + textwrap.indent(APP_PY_LOG, m.group(1)).rstrip("\n"),
                   "updated = cur.fetchone()")
    # 4) read endpoints
    src = sub_once(src, r'@app\.route\("/api/deployment-by-id/<int:deployment_id>", methods=\["PUT"\]\)',
                   lambda m: APP_PY_ROUTES + m.group(0), "deployment-by-id route")
    return src


def patch_html(src):
    if "runningStatusModal" in src:
        sys.exit("manager.html already patched.")
    src = sub_once(src, r"<small>Compare two builds</small></span></button>",
                   lambda m: m.group(0) + HTML_MENU, "Build Diff menu item")
    src = sub_once(src, r"/\* popups sit above content; leave room for the floating button \*/",
                   lambda m: HTML_CSS + m.group(0), "popup css comment")
    src = sub_once(src, r"</main>", lambda m: HTML_MODAL + m.group(0), "</main>")
    src = sub_once(src, r"<!-- ===== Page Assistant \(answers only from data on this page; no AI model\) ===== -->",
                   lambda m: HTML_SCRIPT + m.group(0), "Page Assistant marker")
    return src


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    app_path, html_path = sys.argv[1], sys.argv[2]
    app_src = open(app_path, encoding="utf-8").read()
    html_src = open(html_path, encoding="utf-8").read()
    new_app, new_html = patch_app(app_src), patch_html(html_src)   # both succeed before any write
    compile(new_app, app_path, "exec")                              # syntax check
    for p in (app_path, html_path):
        shutil.copy2(p, p + ".bak")
    open(app_path, "w", encoding="utf-8").write(new_app)
    open(html_path, "w", encoding="utf-8").write(new_html)
    print("Patched OK. Backups: app.py.bak, manager.html.bak")


if __name__ == "__main__":
    main()