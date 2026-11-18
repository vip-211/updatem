#!/usr/bin/env python3
"""
patch.py - ONE patch for the Deployment Tracker Running Status feature.

It applies, in this order (each step is skipped if already present):
  1. WIDE LAYOUT     Running Status panel gets its own full-width row (CSS)
  2. DAILY VIEW      colour-coded daily Running Status (panel + full view popup,
                     day arrows, /api/running-daily route)
  3. CATEGORY TABLE  dynamic "By Category" table: one column per deployment
                     category found on the selected day; also fixes the broken
                     INSERT in save_deployment() and saves `category`
  4. ROLLBACK STATUS new "Rollback Status" tab (Start/Stop timer) on the
                     governance tracker page; shows on the Manager dashboard
                     under Running Status -> "Rolled Back" (cards + history).
                     Needs templates/governance_tracker.html (optional: if the
                     file is missing, only the manager/app parts are applied).
  5. TIDY PANEL      "Hide counts / Show counts" button for the colour counters
                     (choice is remembered) + cleaner Running Status toolbar.
  6. CR DETAIL VIEW  click any CR card (panel, full view, By Category, Rolled
                     Back) to open a popup with the Release Calendar details,
                     the latest DevOps Workspace details and a "Change History"
                     button. Adds GET /api/deployment-detail/<id>.

USAGE (run from the folder that contains app.py):
    python patch.py                                  apply everything
    python patch.py app.py templates\\manager.html templates\\governance_tracker.html
                                                     same, with explicit paths
    python patch.py --dry-run                        show what would change
    python patch.py --apply-sql                      also add the `category`
                                                     column in PostgreSQL (.env)
    python patch.py --restore                        restore latest backup
    python patch.py --dir C:\\path\\to\\project        project elsewhere

SAFETY
  * Every step must match your code exactly once, otherwise NOTHING is written.
  * Patched app.py is compile-checked before saving.
  * Originals are copied to patch_backups/<timestamp>/ first.
  * Safe to run repeatedly: applied steps are detected and skipped.
  * Requires the base Running Status feature (timer table / panel / popup).
"""
import argparse
import os
import py_compile
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

CSS_WIDE = r'''
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

APP_ROUTE = r'''def running_category(status, change_type, roll_back, is_running):
    """Colour group for the Running Status view."""
    s = (status or "").strip().lower()
    ct = (change_type or "").strip().lower()
    rolled = "roll" in s or (roll_back or "").strip().lower() == "yes"
    if rolled or "fail" in s:
        return "failed", ("ROLLED BACK" if rolled else "FAILED")
    if "complet" in s:
        return "completed", "COMPLETED"
    if is_running:
        return "running", "RUNNING"
    if "forward" in ct:
        return "forward", "FORWARD FIX"
    if s in ("cancelled", "rejected", "on hold", "postponed"):
        return "planned", s.upper()
    return "planned", "PLANNED"


@app.route("/api/running-daily", methods=["GET"])
def running_daily():
    """All deployments of ONE day with their timer state and colour group, plus that day's sessions."""
    date_text = (request.args.get("date") or "").strip()
    try:
        selected = date.fromisoformat(date_text) if date_text else date.today()
    except ValueError:
        return jsonify({"success": False, "message": "Choose a valid date (YYYY-MM-DD)."}), 400
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                ensure_running_timer_table(cur)
                ensure_timer_history_table(cur)
                cur.execute("""
                    SELECT d.id, d.cr_number, d.rfid, d.application_name, d.status,
                           d.change_type, d.roll_back, d.slots,
                           t.started_at, t.stopped_at,
                           COALESCE(t.accumulated_seconds, 0), COALESCE(t.is_running, FALSE)
                    FROM public.deployment_tracker d
                    LEFT JOIN public.deployment_running_timers t ON t.deployment_id = d.id
                    WHERE d.deployment_date = %s
                    ORDER BY d.slot_start NULLS LAST, d.id
                """, (selected,))
                rows = cur.fetchall()
                cur.execute("""
                    SELECT h.id, h.deployment_id, h.cr_number, d.rfid, d.application_name,
                           h.started_at, h.stopped_at, h.duration_seconds
                    FROM public.deployment_timer_history h
                    JOIN public.deployment_tracker d ON d.id = h.deployment_id
                    WHERE d.deployment_date = %s
                    ORDER BY h.started_at DESC
                    LIMIT 500
                """, (selected,))
                hrows = cur.fetchall()
                cur.execute("SELECT COUNT(*) FROM public.deployment_running_timers WHERE is_running")
                running_total = int(cur.fetchone()[0] or 0)

        now = datetime.now().astimezone()
        items = []
        for (dep_id, cr, rfid, app_name, status, ctype, rb, slots,
             started_at, stopped_at, acc, is_running) in rows:
            is_running = bool(is_running)
            elapsed = int(acc or 0)
            if is_running and started_at:
                elapsed += max(0, int((now - started_at).total_seconds()))
            category, label = running_category(status, ctype, rb, is_running)
            items.append({
                "deployment_id": dep_id, "cr_number": cr, "rfid": rfid,
                "application_name": app_name, "status": status, "change_type": ctype,
                "roll_back": rb, "slots": slots,
                "started_at": started_at.isoformat() if started_at else None,
                "stopped_at": stopped_at.isoformat() if stopped_at else None,
                "has_timer": started_at is not None,
                "is_running": is_running, "elapsed_seconds": elapsed,
                "category": category, "category_label": label,
            })
        items.sort(key=lambda i: 0 if i["category"] == "running" else 1)   

        sessions = []
        for hid, dep_id, cr, rfid, app_name, started, stopped, dur in hrows:
            sessions.append({
                "id": hid, "deployment_id": dep_id, "cr_number": cr, "rfid": rfid,
                "application_name": app_name,
                "started_at": started.isoformat() if started else None,
                "stopped_at": stopped.isoformat() if stopped else None,
                "is_running": stopped is None,
                "duration_seconds": int(dur) if dur is not None
                    else max(0, int((now - started).total_seconds())),
            })
        counts = {k: 0 for k in ("running", "completed", "planned", "forward", "failed")}
        for i in items:
            counts[i["category"]] += 1
        return jsonify({
            "success": True, "date": selected.isoformat(), "count": len(items),
            "counts": counts, "running_total": running_total,
            "items": items, "sessions": sessions,
        })
    except Exception:
        app.logger.exception("Could not load daily running status")
        return jsonify({"success": False, "message": "Unable to load running status."}), 500
    finally:
        if conn is not None:
            conn.close()


'''

PANEL = r'''<div class="panel rsp-panel">
            <div class="panelhead">
                <div>
                    <h3>Running Status</h3>
                    <div class="sub" id="rspSub">Connecting…</div>
                </div>
                <div class="rsp-head-right">
                    <div class="rsp-daynav" aria-label="Choose the day">
                        <button type="button" class="week-nav" id="rspPrev" aria-label="Previous day" title="Previous day">‹</button>
                        <button type="button" class="week-nav rsp-today" id="rspToday" title="Go to today">Today</button>
                        <input type="date" id="rspDate" aria-label="Choose a day">
                        <button type="button" class="week-nav" id="rspNext" aria-label="Next day" title="Next day">›</button>
                    </div>
                    <span class="rsp-live"><i></i>LIVE</span>
                    <button class="btn light" type="button" data-open-modal="runningStatusModal">Full view ↗</button>
                </div>
            </div>
            <div class="rsp-day" id="rspDayLabel"></div>
            <div class="rsp-stats">
                <button type="button" class="rsp-stat t-all active" data-rsp-cat="all"><b>0</b><span>ALL</span></button>
                <button type="button" class="rsp-stat t-running" data-rsp-cat="running"><b>0</b><span>RUNNING</span></button>
                <button type="button" class="rsp-stat t-completed" data-rsp-cat="completed"><b>0</b><span>COMPLETED</span></button>
                <button type="button" class="rsp-stat t-planned" data-rsp-cat="planned"><b>0</b><span>PLANNED</span></button>
                <button type="button" class="rsp-stat t-forward" data-rsp-cat="forward"><b>0</b><span>FORWARD FIX</span></button>
                <button type="button" class="rsp-stat t-failed" data-rsp-cat="failed"><b>0</b><span>FAILED / ROLLED BACK</span></button>
            </div>
            <div class="rsp-list" id="rspList"><div class="rsp-empty">Loading running status…</div></div>
        </div>'''

MODAL = r'''<!-- ===== Running Status view (daily, colour-coded) ===== -->
    <div class="mgr-modal" id="runningStatusModal" data-modal hidden>
    <div class="mgr-modal-box rsv-box" role="dialog" aria-modal="true" aria-label="Running Status">
        <header class="rsv-top">
            <div class="rsv-top-row">
                <div>
                    <h3><span aria-hidden="true">⏱</span> Running Status <span class="rs-live-tag"><i></i>LIVE</span></h3>
                    <p id="rsvSub">Connecting…</p>
                </div>
                <button class="mgr-close" type="button" data-close-modal aria-label="Close Running Status">✕</button>
            </div>
            <div class="rsv-stats">
                <div class="rsv-stat t-running"><span>RUNNING</span><b id="rsvN-running">0</b></div>
                <div class="rsv-stat t-completed"><span>COMPLETED</span><b id="rsvN-completed">0</b></div>
                <div class="rsv-stat t-planned"><span>PLANNED</span><b id="rsvN-planned">0</b></div>
                <div class="rsv-stat t-forward"><span>FORWARD FIX</span><b id="rsvN-forward">0</b></div>
                <div class="rsv-stat t-failed"><span>FAILED / ROLLED BACK</span><b id="rsvN-failed">0</b></div>
            </div>
        </header>
        <div class="rsv-toolbar">
            <div class="rsv-daynav" aria-label="Choose the day">
                <button type="button" class="rsv-arrow" id="rsvPrev" aria-label="Previous day" title="Previous day">‹</button>
                <div class="rsv-daybox"><b id="rsvDayLabel"></b><input type="date" id="rsvDate" aria-label="Choose a day"></div>
                <button type="button" class="rsv-arrow" id="rsvNext" aria-label="Next day" title="Next day">›</button>
                <button type="button" class="rsv-today" id="rsvToday">Today</button>
            </div>
            <div class="rsv-seg" id="rsvViews"><button type="button" class="active" data-rsv-view="live">Live</button><button type="button" data-rsv-view="history">History</button></div>
            <div class="rsv-seg" id="rsvFilters">
                <button type="button" class="active" data-rsv-filter="all">All</button>
                <button type="button" data-rsv-filter="running">Running</button>
                <button type="button" data-rsv-filter="completed">Completed</button>
                <button type="button" data-rsv-filter="planned">Planned</button>
                <button type="button" data-rsv-filter="forward">Forward Fix</button>
                <button type="button" data-rsv-filter="failed">Failed</button>
            </div>
            <div class="rsv-search"><input id="rsvSearch" type="search" placeholder="Search CR number or application..." autocomplete="off" aria-label="Search running status"></div>
        </div>
        <div class="rsv-body" id="rsvBody"><div class="rs-empty">Loading…</div></div>
    </div>
    </div>

'''

COMMON_JS = r'''
    const esc = s => String(s ?? '').replace(/[&<>"']/g,
        c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const p2 = n => String(n).padStart(2, '0');
    const fmtD = s => { s = Math.max(0, Math.floor(s)); return p2(Math.floor(s / 3600)) + ':' + p2(Math.floor(s % 3600 / 60)) + ':' + p2(s % 60); };
    const ymd = d => d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate());
    const parseDay = s => { const a = String(s).split('-').map(Number); return new Date(a[0], a[1] - 1, a[2]); };
    const shiftDay = (s, n) => { const d = parseDay(s); d.setDate(d.getDate() + n); return ymd(d); };
    const dayText = s => parseDay(s).toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'short', year: 'numeric' }) + (s === ymd(new Date()) ? ' · Today' : '');
    function fmtShort(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d.getTime())) return '';
        const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        return new Date().toDateString() === d.toDateString() ? t : d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' ' + t;
    }
    const isFwd = t => /forward/i.test(String(t.change_type || ''));
    const slotStart = t => String(t.slots || '').split('-')[0].trim() || '—';
'''

PANEL_JS = r'''<script>
(function runningStatusPanel() {
    const $ = id => document.getElementById(id);
    const list = $('rspList');
    if (!list) return;
    const sub = $('rspSub'), dayLabel = $('rspDayLabel'), dateInput = $('rspDate');
    const MAX = 8;
''' + COMMON_JS + r'''
    let day = window.__rsDay || ymd(new Date());
    window.__rsDay = day;
    let items = [], other = 0, fetchedAt = Date.now(), cat = 'all', seq = 0;
    const secs = t => t.elapsed_seconds + (t.is_running ? (Date.now() - fetchedAt) / 1000 : 0);

    function rowHtml(t) {
        const c = t.category;
        const fwdChip = (isFwd(t) && c !== 'forward') ? '<span class="rs-fwd">FORWARD FIX</span>' : '';
        let timeHtml, meta;
        if (t.has_timer) {
            timeHtml = '<div class="rsp-time" data-rsp-id="' + esc(t.deployment_id) + '">' + fmtD(secs(t)) + '</div>';
            meta = (t.is_running ? 'Started ' + fmtShort(t.started_at) : 'Stopped ' + fmtShort(t.stopped_at)) + ' · ' + (t.status || '');
        } else {
            timeHtml = '<div class="rsp-time rsp-slot">' + esc(slotStart(t)) + '</div>';
            meta = 'Slot ' + (t.slots || '—') + ' · ' + (t.status || '');
        }
        return '<div class="rsp-row cs-' + c + (c === 'running' ? ' running' : '') + '"><div class="rsp-main">' +
            '<div class="rsp-cr">' + esc(t.cr_number || '—') + '<span class="rsp-pill">' + esc(t.category_label) + '</span>' + fwdChip + '</div>' +
            '<div class="rsp-app" title="' + esc(t.application_name) + '">' + esc(t.application_name || 'Application not set') + '</div>' +
            '<div class="rsp-meta">' + esc(meta) + '</div></div>' + timeHtml + '<div class="rsp-bar"></div></div>';
    }

    function render() {
        const counts = { all: items.length, running: 0, completed: 0, planned: 0, forward: 0, failed: 0 };
        items.forEach(i => { counts[i.category] = (counts[i.category] || 0) + 1; });
        document.querySelectorAll('[data-rsp-cat]').forEach(b => {
            b.querySelector('b').textContent = counts[b.dataset.rspCat] || 0;
            b.classList.toggle('active', b.dataset.rspCat === cat);
        });
        dayLabel.textContent = dayText(day);
        dateInput.value = day;
        const rows = items.filter(i => cat === 'all' || i.category === cat);
        if (!rows.length) {
            list.innerHTML = '<div class="rsp-empty"><b>' + (items.length ? 'Nothing in this colour group' : 'No deployments scheduled for this day') + '</b>' +
                (items.length ? 'Click the highlighted counter again to see all.' : 'Use the arrows to look at another day.') + '</div>';
            return;
        }
        list.innerHTML = rows.slice(0, MAX).map(rowHtml).join('') + (rows.length > MAX
            ? '<button type="button" class="rsp-more" data-open-modal="runningStatusModal">+' + (rows.length - MAX) + ' more · View all</button>' : '');
    }

    async function load() {
        if (document.hidden) return;
        const my = ++seq;
        try {
            const resp = await fetch('/api/running-daily?date=' + encodeURIComponent(day), { cache: 'no-store' });
            let data;
            try { data = await resp.json(); } catch (_) { throw new Error('HTTP ' + resp.status + ', not JSON (route missing? restart Flask)'); }
            if (my !== seq) return;
            if (!data.success) throw new Error(data.message || 'failed');
            items = data.items || [];
            other = Math.max(0, (data.running_total || 0) - (data.counts ? data.counts.running : 0));
            fetchedAt = Date.now();
            sub.textContent = 'Updated ' + new Date().toLocaleTimeString() + ' · every 5s' + (other ? ' · ' + other + ' more running on other days' : '');
            render();
        } catch (e) {
            if (my === seq) sub.textContent = 'Could not refresh: ' + (e && e.message ? e.message : e) + ' - test http://127.0.0.1:5050/api/running-daily';
        }
    }

    function setDay(d, fromEvent) {
        if (!d) return;
        day = d; window.__rsDay = d;
        if (!fromEvent) document.dispatchEvent(new CustomEvent('rs:day', { detail: d }));
        items = []; render(); load();
    }
    $('rspPrev').addEventListener('click', () => setDay(shiftDay(day, -1)));
    $('rspNext').addEventListener('click', () => setDay(shiftDay(day, 1)));
    $('rspToday').addEventListener('click', () => setDay(ymd(new Date())));
    dateInput.addEventListener('change', () => setDay(dateInput.value));
    document.addEventListener('rs:day', e => { if (e.detail !== day) setDay(e.detail, true); });
    document.querySelectorAll('[data-rsp-cat]').forEach(b => b.addEventListener('click', () => {
        cat = (cat === b.dataset.rspCat) ? 'all' : b.dataset.rspCat; render();
    }));

    setInterval(() => {
        if (document.hidden) return;
        items.forEach(t => { if (!t.is_running) return;
            const el = list.querySelector('[data-rsp-id="' + t.deployment_id + '"]'); if (el) el.textContent = fmtD(secs(t)); });
    }, 1000);
    setInterval(load, 5000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
    render(); load();
})();
</script>

'''

MODAL_JS = r'''<script>
(function runningStatusView() {
    const $ = id => document.getElementById(id);
    const modal = $('runningStatusModal');
    if (!modal) return;
    const body = $('rsvBody'), sub = $('rsvSub'), search = $('rsvSearch'), badge = $('rsBadge'), dateInput = $('rsvDate');
''' + COMMON_JS + r'''
    let day = window.__rsDay || ymd(new Date());
    window.__rsDay = day;
    let view = 'live', filter = 'all', items = [], sessions = [], fetchedAt = Date.now(), ticks = 0, seq = 0;
    const sinceFetch = () => (Date.now() - fetchedAt) / 1000;
    const liveSecs = t => t.elapsed_seconds + (t.is_running ? sinceFetch() : 0);
    const histSecs = h => h.duration_seconds + (h.is_running ? sinceFetch() : 0);
    const fmtT = iso => iso ? new Date(iso).toLocaleString() : '—';

    function passes(r) {
        if (filter !== 'all' && r.category !== undefined && r.category !== filter) return false;
        const q = search.value.trim().toLowerCase();
        return !q || String(r.cr_number || '').toLowerCase().includes(q) || String(r.application_name || '').toLowerCase().includes(q);
    }

    function cardHtml(t) {
        const c = t.category;
        const fwdChip = (isFwd(t) && c !== 'forward') ? '<span class="rs-fwd">FORWARD FIX</span>' : '';
        let timeHtml, meta;
        if (t.has_timer) {
            timeHtml = '<div class="rs-time" data-rs-id="' + esc(t.deployment_id) + '">' + fmtD(liveSecs(t)) + '</div>';
            meta = t.is_running ? 'Started ' + fmtShort(t.started_at) : 'Stopped ' + fmtShort(t.stopped_at);
        } else {
            timeHtml = '<div class="rs-time rs-slot">' + esc(slotStart(t)) + '</div>';
            meta = 'Planned slot ' + (t.slots || '—');
        }
        return '<div class="rs-card cs-' + c + (c === 'running' ? ' running' : '') + '">' +
            '<div class="rs-card-top"><span class="rs-cr" title="' + esc(t.cr_number) + '">' + esc(t.cr_number || '—') + '</span><span class="rs-pill">' + esc(t.category_label) + '</span></div>' +
            '<div class="rs-app" title="' + esc(t.application_name) + '">' + esc(t.application_name || 'Application not set') + '</div>' +
            timeHtml + '<div class="rs-meta">' + esc(meta) + '</div>' +
            '<div class="rs-foot">RFID ' + esc(t.rfid || '—') + ' · ' + esc(t.status || 'No status') + ' ' + fwdChip + '</div>' +
            '<div class="rs-bar"></div></div>';
    }

    function render() {
        const counts = { running: 0, completed: 0, planned: 0, forward: 0, failed: 0 };
        items.forEach(i => { counts[i.category] = (counts[i.category] || 0) + 1; });
        Object.keys(counts).forEach(k => { const el = $('rsvN-' + k); if (el) el.textContent = counts[k]; });
        $('rsvDayLabel').textContent = dayText(day);
        dateInput.value = day;
        document.querySelectorAll('[data-rsv-view]').forEach(b => b.classList.toggle('active', b.dataset.rsvView === view));
        document.querySelectorAll('[data-rsv-filter]').forEach(b => b.classList.toggle('active', b.dataset.rsvFilter === filter));
        if (view === 'live') {
            const rows = items.filter(passes);
            body.innerHTML = rows.length
                ? '<div class="rs-cards">' + rows.map(cardHtml).join('') + '</div>'
                : '<div class="rs-empty"><b>' + (items.length ? 'No matches' : 'No deployments scheduled for this day') + '</b>' +
                  (items.length ? 'Try a different colour filter or search.' : 'Use the arrows to look at another day.') + '</div>';
        } else {
            const rows = sessions.filter(r => { const q = search.value.trim().toLowerCase();
                return !q || String(r.cr_number || '').toLowerCase().includes(q) || String(r.application_name || '').toLowerCase().includes(q); });
            body.innerHTML = rows.length
                ? '<div class="tablewrap"><table style="min-width:900px"><thead><tr><th>SR No.</th><th>CR Number</th><th>RFID</th><th>Application</th><th>Started At</th><th>Stopped At</th><th>Duration</th><th>Status</th></tr></thead><tbody>' +
                  rows.map((r, i) => '<tr><td>' + (i + 1) + '</td><td><strong>' + esc(r.cr_number) + '</strong></td><td>' + esc(r.rfid) + '</td><td>' + esc(r.application_name) +
                    '</td><td>' + fmtT(r.started_at) + '</td><td>' + (r.is_running ? '—' : fmtT(r.stopped_at)) +
                    '</td><td data-hist-id="' + esc(r.id) + '">' + fmtD(histSecs(r)) + '</td><td>' +
                    (r.is_running ? '<span class="rs-hpill amber">● Running</span>' : '<span class="rs-hpill grey">■ Stopped</span>') + '</td></tr>').join('') +
                  '</tbody></table></div>'
                : '<div class="rs-empty"><b>No Start/Stop sessions on this day</b>Sessions are recorded from the moment Start is clicked.</div>';
        }
    }

    async function load() {
        if (document.hidden) return;
        const my = ++seq;
        try {
            const resp = await fetch('/api/running-daily?date=' + encodeURIComponent(day), { cache: 'no-store' });
            let data;
            try { data = await resp.json(); } catch (_) { throw new Error('HTTP ' + resp.status + ', not JSON (route missing? restart Flask)'); }
            if (my !== seq) return;
            if (!data.success) throw new Error(data.message || 'failed');
            items = data.items || []; sessions = data.sessions || [];
            fetchedAt = Date.now();
            if (badge) { badge.textContent = data.running_total || 0; badge.hidden = !data.running_total; }
            sub.textContent = 'Updated ' + new Date().toLocaleTimeString() + ' · refreshes every 5 seconds';
            render();
        } catch (e) {
            if (my === seq) sub.textContent = 'Could not refresh: ' + (e && e.message ? e.message : e) + ' - test http://127.0.0.1:5050/api/running-daily';
        }
    }

    function setDay(d, fromEvent) {
        if (!d) return;
        day = d; window.__rsDay = d;
        if (!fromEvent) document.dispatchEvent(new CustomEvent('rs:day', { detail: d }));
        items = []; sessions = []; render(); load();
    }
    $('rsvPrev').addEventListener('click', () => setDay(shiftDay(day, -1)));
    $('rsvNext').addEventListener('click', () => setDay(shiftDay(day, 1)));
    $('rsvToday').addEventListener('click', () => setDay(ymd(new Date())));
    dateInput.addEventListener('change', () => setDay(dateInput.value));
    document.addEventListener('rs:day', e => { if (e.detail !== day) setDay(e.detail, true); });
    document.querySelectorAll('[data-rsv-view]').forEach(b => b.addEventListener('click', () => { view = b.dataset.rsvView; render(); }));
    document.querySelectorAll('[data-rsv-filter]').forEach(b => b.addEventListener('click', () => { filter = b.dataset.rsvFilter; render(); }));
    search.addEventListener('input', render);
    modal.addEventListener('modal:open', () => { view = 'live'; render(); load(); });

    setInterval(() => {
        if (modal.hidden || document.hidden) return;
        items.forEach(t => { if (!t.is_running) return;
            const el = body.querySelector('[data-rs-id="' + t.deployment_id + '"]'); if (el) el.textContent = fmtD(liveSecs(t)); });
        sessions.forEach(h => { if (!h.is_running) return;
            const el = body.querySelector('[data-hist-id="' + h.id + '"]'); if (el) el.textContent = fmtD(histSecs(h)); });
    }, 1000);
    setInterval(() => { ticks++; if (!modal.hidden || ticks % 4 === 0) load(); }, 5000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
    render(); load();
})();
</script>

'''

CSS_DAILY = r'''
/* ===== Running Status: daily view, colour code ===== */
/* amber = running, green = completed, grey = planned, blue = forward fix, red = failed / rolled back */
.rsp-row.cs-running, .rs-card.cs-running { background:linear-gradient(135deg,#d97706 0%,#f59e0b 60%,#fbbf24 100%); box-shadow:0 8px 20px #f59e0b45; }
.rsp-row.cs-completed, .rs-card.cs-completed { background:linear-gradient(135deg,#047857 0%,#10b981 100%); box-shadow:0 8px 20px #10b98138; }
.rsp-row.cs-planned, .rs-card.cs-planned { background:linear-gradient(135deg,#64748b 0%,#94a3b8 100%); box-shadow:0 8px 20px #64748b30; }
.rsp-row.cs-forward, .rs-card.cs-forward { background:linear-gradient(135deg,#1d4ed8 0%,#3b82f6 100%); box-shadow:0 8px 20px #3b82f640; }
.rsp-row.cs-failed, .rs-card.cs-failed { background:linear-gradient(135deg,#b91c1c 0%,#f43f5e 100%); box-shadow:0 8px 20px #e11d4838; }
.rsp-row.running .rsp-pill::before, .rs-card.running .rs-pill::before { background:#fff7d6; }
.rs-fwd { display:inline-block; margin-left:6px; padding:1px 7px; border-radius:20px; background:#1d4ed8; border:1px solid #ffffff90; color:#fff; font-size:8.5px; font-weight:850; letter-spacing:.5px; }
.rsp-cr .rs-fwd { margin-left:0; }
.rsp-meta { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.rsp-slot, .rs-slot { opacity:.95; }

/* daily navigation (dashboard panel) */
.rsp-daynav { display:inline-flex; align-items:center; gap:6px; }
.rsp-daynav input[type=date] { width:auto; min-width:0; padding:6px 9px; border:1px solid #dce4ef; border-radius:8px; background:#fff; color:#334155; font-size:11px; }
.rsp-today { padding:6px 11px; }
.rsp-day { margin:-6px 0 12px; font-size:12px; font-weight:800; color:#3656c8; }

/* colour counters (click to filter) */
.rsp-stats { grid-template-columns:repeat(6,minmax(0,1fr)); max-width:none; gap:8px; }
.rsp-stat { border:0; cursor:pointer; font:inherit; transition:transform .15s, box-shadow .15s; }
.rsp-stat:hover { transform:translateY(-2px); }
.rsp-stat.active { box-shadow:0 0 0 2px currentColor; }
.rsp-stat span { line-height:1.25; }
.rsp-stat.t-all { background:linear-gradient(135deg,#e0e7ff,#ede9fe); color:#4338ca; }
.rsp-stat.t-running { background:linear-gradient(135deg,#fef3c7,#fde68a); color:#b45309; }
.rsp-stat.t-completed { background:linear-gradient(135deg,#d1fae5,#a7f3d0); color:#047857; }
.rsp-stat.t-planned { background:linear-gradient(135deg,#e5e7eb,#f1f5f9); color:#475569; }
.rsp-stat.t-forward { background:linear-gradient(135deg,#dbeafe,#bfdbfe); color:#1d4ed8; }
.rsp-stat.t-failed { background:linear-gradient(135deg,#fee2e2,#fecdd3); color:#b91c1c; }
@media (max-width:760px) { .rsp-stats { grid-template-columns:repeat(3,minmax(0,1fr)); } }

/* full view header counters + day navigation */
.rsv-stats { grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; }
.rsv-stat span { line-height:1.25; }
.rsv-stat.t-running b { color:#fcd34d; } .rsv-stat.t-completed b { color:#6ee7b7; } .rsv-stat.t-planned b { color:#cbd5e1; }
.rsv-stat.t-forward b { color:#93c5fd; } .rsv-stat.t-failed b { color:#fda4af; }
@media (max-width:760px) { .rsv-stats { grid-template-columns:repeat(3,minmax(0,1fr)); } }
.rsv-daynav { display:inline-flex; align-items:center; gap:8px; }
.rsv-arrow { width:38px; height:38px; border:0; border-radius:12px; background:linear-gradient(135deg,#4f67f6,#7c5cf0); color:#fff; font-size:22px; line-height:1; cursor:pointer; box-shadow:0 4px 12px #4f67f640; }
.rsv-arrow:hover { transform:translateY(-2px); }
.rsv-daybox { display:grid; gap:3px; min-width:180px; text-align:center; }
.rsv-daybox b { font-size:13px; color:#172554; }
.rsv-daybox input { padding:5px 8px; border:1px solid #dce4ef; border-radius:8px; font-size:11px; color:#334155; background:#fff; }
.rsv-today { padding:9px 13px; border:0; border-radius:10px; background:#edf1ff; color:#354fd2; font-size:12px; font-weight:800; cursor:pointer; }
.rsv-today:hover { background:#dfe6ff; }
.rs-hpill { display:inline-flex; padding:4px 10px; border-radius:7px; font-size:10px; font-weight:800; }
.rs-hpill.amber { background:#fef3c7; color:#b45309; }
.rs-hpill.grey { background:#e5e7eb; color:#475569; }
'''


APP_INSERT_NEW = r'''cur.execute(
                    """
                    INSERT INTO public.deployment_tracker (
                        cr_number, rfid, deployment_date, slots, slot_start, slot_end,
                        application_name, category, deployment_count, deployment_type,
                        change_type, released_by, verified_and_deployed_by,
                        change_requestor, business_unit, environment, git_links,
                        incident_number, roll_back, status
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    (
                        cr_number, rfid, deployment_date,
                        f"{slot_start:%H:%M} - {slot_end:%H:%M}",
                        slot_start, slot_end,
                        application_name,
                        (category or "").strip() or None,
                        deployment_count,
                        data["deployment_type"], data["change_type"],
                        released_by, verified_and_deployed_by,
                        change_requestor, business_unit, environment,
                        json.dumps(git_links),
                        data.get("incident_number"),
                        data["roll_back"], data["status"],
                    ),
                )
                record_id = cur.fetchone()[0]'''

APP_INSERT_REGEX = (
    r'cur\.execute\(\s*"""\s*INSERT INTO public\.deployment_tracker \(.*?'
    r'data\["status"\],\s*\),\s*\)\s*record_id = cur\.fetchone\(\)\[0\]'
)

CSS_CATEGORY = r'''/* ===== Running status by deployment category (dynamic columns) ===== */
.cat-table-wrap { overflow:auto; border:1px solid var(--line); border-radius:14px; background:#fff; }
.cat-table { width:100%; min-width:0; border-collapse:separate; border-spacing:0; table-layout:fixed; }
.cat-table th { padding:14px; text-align:left; white-space:normal; background:linear-gradient(135deg,#243b82,#4f67f6) !important; color:#fff !important; font-size:12px; letter-spacing:.3px; text-transform:none; }
.cat-table th small { display:inline-block; margin-left:6px; padding:1px 8px; border-radius:20px; background:#ffffff30; font-size:10px; }
.cat-table td { padding:8px; vertical-align:top; white-space:normal; border-bottom:1px solid #edf1f6; background:#fff !important; }
.cat-table tbody tr:hover td { background:#fff !important; }
.cat-cell { position:relative; padding:10px 12px; border-radius:12px; color:#fff; display:grid; gap:2px; }
.cat-cell .cc-top { display:flex; justify-content:space-between; align-items:center; gap:6px; font-weight:850; font-size:13px; }
.cat-cell .cc-pill { padding:1px 7px; border-radius:20px; background:#ffffff2e; font-size:8.5px; font-weight:850; letter-spacing:.4px; }
.cat-cell .cc-app { font-size:11px; opacity:.92; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.cat-cell .cc-time { font-size:18px; font-weight:850; font-variant-numeric:tabular-nums; }
.cat-cell .cc-meta { font-size:9.5px; opacity:.85; }
.cat-cell.cs-running { background:linear-gradient(135deg,#d97706,#fbbf24); }
.cat-cell.cs-completed { background:linear-gradient(135deg,#047857,#10b981); }
.cat-cell.cs-planned { background:linear-gradient(135deg,#64748b,#94a3b8); }
.cat-cell.cs-forward { background:linear-gradient(135deg,#1d4ed8,#3b82f6); }
.cat-cell.cs-failed { background:linear-gradient(135deg,#b91c1c,#f43f5e); }
</style>
</head>'''

HELPER_NEW = r'''<script>
window.rsCategoryTable = function (items, o) {
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const groups = new Map();
    items.forEach(i => {
        const k = i.cr_category || 'Uncategorized';
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push(i);
    });
    const cats = [...groups.keys()].sort((a, b) => a.localeCompare(b));
    if (!cats.length) return '';
    const rows = Math.max(...cats.map(c => groups.get(c).length));
    const cell = t => {
        const slot = String(t.slots || '').split('-')[0].trim() || '—';
        const time = t.has_timer
            ? '<div class="cc-time" ' + o.idAttr + '="' + esc(t.deployment_id) + '">' + o.fmtD(o.secs(t)) + '</div>'
            : '<div class="cc-time">' + esc(slot) + '</div>';
        return '<div class="cat-cell cs-' + t.category + '">' +
            '<div class="cc-top"><span>' + esc(t.cr_number || '—') + '</span><span class="cc-pill">' + esc(t.category_label) + '</span></div>' +
            '<div class="cc-app" title="' + esc(t.application_name) + '">' + esc(t.application_name || 'Application not set') + '</div>' +
            time + '<div class="cc-meta">' + esc(t.status || 'No status') + '</div></div>';
    };
    let h = '<div class="cat-table-wrap"><table class="cat-table"><thead><tr>' +
        cats.map(c => '<th>' + esc(c) + '<small>' + groups.get(c).length + '</small></th>').join('') +
        '</tr></thead><tbody>';
    for (let r = 0; r < rows; r++) {
        h += '<tr>' + cats.map(c => { const t = groups.get(c)[r]; return '<td>' + (t ? cell(t) : '') + '</td>'; }).join('') + '</tr>';
    }
    return h + '</tbody></table></div>';
};
</script>

<script>
(function runningStatusView() {'''

VIEW_SWITCH_NEW = (
    '<div class="rsv-seg" id="rsvViews"><button type="button" class="active" data-rsv-view="live">Live</button>'
    '<button type="button" data-rsv-view="category">By Category</button>'
    '<button type="button" data-rsv-view="history">History</button></div>'
)

VIEW_RENDER_NEW = r'''if (view === 'category') {
            const rows = items.filter(passes);
            body.innerHTML = rows.length
                ? window.rsCategoryTable(rows, { idAttr: 'data-rs-id', secs: liveSecs, fmtD })
                : '<div class="rs-empty"><b>' + (items.length ? 'No matches' : 'No deployments scheduled for this day') + '</b>Use the arrows to look at another day.</div>';
        } else if (view === 'live') {'''

PANEL_BTN_NEW = (
    '<span class="rsp-live"><i></i>LIVE</span>\n'
    '                    <button class="btn light" type="button" id="rspTableToggle">▦ By Category</button>\n'
    '                    <button class="btn light" type="button" data-open-modal="runningStatusModal">Full view ↗</button>'
)

PANEL_VAR_NEW = (
    "let items = [], other = 0, fetchedAt = Date.now(), cat = 'all', seq = 0;\n"
    "    let tableMode = false;"
)

PANEL_RENDER_NEW = r'''if (tableMode) {
            list.style.display = 'block';
            list.innerHTML = window.rsCategoryTable(rows, { idAttr: 'data-rsp-id', secs, fmtD });
            return;
        }
        list.style.display = '';
        list.innerHTML = rows.slice(0, MAX).map(rowHtml).join('') + (rows.length > MAX
            ? '<button type="button" class="rsp-more" data-open-modal="runningStatusModal">+' + (rows.length - MAX) + ' more · View all</button>' : '');'''

PANEL_HANDLER_NEW = r'''$('rspTableToggle').addEventListener('click', e => {
        tableMode = !tableMode;
        e.currentTarget.textContent = tableMode ? '▤ Cards' : '▦ By Category';
        render();
    });
    $('rspPrev').addEventListener('click', () => setDay(shiftDay(day, -1)));'''

SQL_TEXT = "ALTER TABLE public.deployment_tracker ADD COLUMN IF NOT EXISTS category TEXT;\n"


PATCHES = [
    # ---------------- STEP 1: wide layout ----------------
    ("html", "1 wide", "CSS: full-width Running Status row",
     "Running Status wide layout", "</style>", CSS_WIDE + "</style>", False),

    # ---------------- STEP 2: daily view ----------------
    ("app", "2 daily", "route: running_category + /api/running-daily",
     "def running_daily",
     r'@app\.route\("/api/deployment-by-id/<int:deployment_id>", methods=\["PUT"\]\)',
     lambda m: APP_ROUTE + m.group(0), True),

    ("html", "2 daily", "panel: day navigation + colour counters",
     'id="rspPrev"',
     r'<div class="panel rsp-panel">[\s\S]*?id="rspList">[\s\S]*?Loading running status…</div></div>\s*</div>',
     PANEL, True),

    ("html", "2 daily", "panel: script",
     'id="rspPrev"',
     r"<script>\s*\(function runningStatusPanel\(\) \{[\s\S]*?</script>\s*",
     PANEL_JS, True),

    ("html", "2 daily", "full view popup: markup",
     'id="rspPrev"',
     r"<!-- ===== Running Status view[^\n]*-->[\s\S]*?(?=</main>)",
     MODAL, True),

    ("html", "2 daily", "full view popup: script",
     'id="rspPrev"',
     r"<script>\s*\(function runningStatusView\(\) \{[\s\S]*?</script>\s*",
     MODAL_JS, True),

    ("html", "2 daily", "CSS: colour code + day navigation",
     "Running Status: daily view, colour code", "</style>", CSS_DAILY + "</style>", False),

    # ---------------- STEP 3: category table ----------------
    ("app", "3 category", "ITSM: return category + impacted_application",
     '"category": category,                          # NEW',
     '"environment": environment, "cab_dates": cab_dates }',
     '"environment": environment,\n'
     '            "category": category,                          # NEW\n'
     '            "impacted_application": impacted_application,  # NEW\n'
     '            "cab_dates": cab_dates\n'
     '        }', False),

    ("app", "3 category", "save_deployment: fix INSERT and save category",
     "application_name, category, deployment_count, deployment_type,",
     APP_INSERT_REGEX, APP_INSERT_NEW, True),

    ("app", "3 category", "running-daily: select d.category",
     "COALESCE(t.is_running, FALSE),\n                           d.category",
     "COALESCE(t.accumulated_seconds, 0), COALESCE(t.is_running, FALSE) "
     "FROM public.deployment_tracker d",
     "COALESCE(t.accumulated_seconds, 0), COALESCE(t.is_running, FALSE),\n"
     "                           d.category\n"
     "                    FROM public.deployment_tracker d", False),

    ("app", "3 category", "running-daily: unpack cr_category",
     "acc, is_running, cr_category) in rows:",
     "for (dep_id, cr, rfid, app_name, status, ctype, rb, slots, "
     "started_at, stopped_at, acc, is_running) in rows:",
     "for (dep_id, cr, rfid, app_name, status, ctype, rb, slots,\n"
     "             started_at, stopped_at, acc, is_running, cr_category) in rows:", False),

    ("app", "3 category", "running-daily: add cr_category to JSON",
     '"cr_category": (cr_category or "Uncategorized")',
     '"category": category, "category_label": label, })',
     '"category": category, "category_label": label,\n'
     '                "cr_category": (cr_category or "Uncategorized").strip() or "Uncategorized",\n'
     '            })', False),

    ("html", "3 category", "CSS: category table",
     ".cat-table-wrap", "</style> </head>", CSS_CATEGORY, False),

    ("html", "3 category", "JS helper rsCategoryTable",
     "window.rsCategoryTable = function",
     "<script> (function runningStatusView() {", HELPER_NEW, False),

    ("html", "3 category", "full view: 'By Category' tab",
     'data-rsv-view="category"',
     '<div class="rsv-seg" id="rsvViews"><button type="button" class="active" '
     'data-rsv-view="live">Live</button><button type="button" '
     'data-rsv-view="history">History</button></div>',
     VIEW_SWITCH_NEW, False),

    ("html", "3 category", "full view: render category table",
     "window.rsCategoryTable(rows, { idAttr: 'data-rs-id'",
     "if (view === 'live') {", VIEW_RENDER_NEW, False),

    ("html", "3 category", "panel: toggle button",
     'id="rspTableToggle"',
     '<span class="rsp-live"><i></i>LIVE</span> '
     '<button class="btn light" type="button" data-open-modal="runningStatusModal">Full view ↗</button>',
     PANEL_BTN_NEW, False),

    ("html", "3 category", "panel: tableMode variable",
     "let tableMode = false;",
     "let items = [], other = 0, fetchedAt = Date.now(), cat = 'all', seq = 0;",
     PANEL_VAR_NEW, False),

    ("html", "3 category", "panel: render category table",
     "window.rsCategoryTable(rows, { idAttr: 'data-rsp-id'",
     "list.innerHTML = rows.slice(0, MAX).map(rowHtml).join('') + (rows.length > MAX "
     "? '<button type=\"button\" class=\"rsp-more\" data-open-modal=\"runningStatusModal\">+' "
     "+ (rows.length - MAX) + ' more · View all</button>' : '');",
     PANEL_RENDER_NEW, False),

    ("html", "3 category", "panel: toggle click handler",
     "$('rspTableToggle').addEventListener",
     "$('rspPrev').addEventListener('click', () => setDay(shiftDay(day, -1)));",
     PANEL_HANDLER_NEW, False),
]


ROLLBACK_APP = r'''def ensure_rollback_timer_tables(cur):
    """Rollback timers: one accumulated timer per deployment + one history row per Start->Stop."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS public.deployment_rollback_timers (
            deployment_id BIGINT PRIMARY KEY
                REFERENCES public.deployment_tracker(id) ON DELETE CASCADE,
            started_at TIMESTAMPTZ,
            stopped_at TIMESTAMPTZ,
            accumulated_seconds BIGINT NOT NULL DEFAULT 0,
            is_running BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS public.deployment_rollback_timer_history (
            id BIGSERIAL PRIMARY KEY,
            deployment_id BIGINT NOT NULL,
            cr_number TEXT,
            started_at TIMESTAMPTZ NOT NULL,
            stopped_at TIMESTAMPTZ,
            duration_seconds BIGINT
        )
    """)


@app.route("/api/deployment-rollback-timer/<int:deployment_id>", methods=["GET"])
def get_deployment_rollback_timer(deployment_id):
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                ensure_rollback_timer_tables(cur)
                cur.execute("SELECT 1 FROM public.deployment_tracker WHERE id = %s", (deployment_id,))
                if not cur.fetchone():
                    return jsonify({"success": False, "message": "Deployment record not found."}), 404
                cur.execute("""
                    SELECT started_at, stopped_at, accumulated_seconds, is_running
                    FROM public.deployment_rollback_timers
                    WHERE deployment_id = %s
                """, (deployment_id,))
                row = cur.fetchone()
        return jsonify({"success": True, "timer": serialize_running_timer(row)})
    except Exception as e:
        app.logger.exception("Could not load rollback timer: %s", deployment_id)
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route("/api/deployment-rollback-timer/<int:deployment_id>", methods=["POST"])
def update_deployment_rollback_timer(deployment_id):
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "").strip().lower()
    if action not in ("start", "stop"):
        return jsonify({"success": False, "message": "Timer action must be start or stop."}), 400
    conn = None
    cr_number = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                ensure_rollback_timer_tables(cur)
                cur.execute("SELECT cr_number FROM public.deployment_tracker WHERE id = %s", (deployment_id,))
                dep_row = cur.fetchone()
                if not dep_row:
                    return jsonify({"success": False, "message": "Deployment record not found."}), 404
                cr_number = dep_row[0]
                cur.execute("""
                    INSERT INTO public.deployment_rollback_timers (deployment_id)
                    VALUES (%s) ON CONFLICT (deployment_id) DO NOTHING
                """, (deployment_id,))
                cur.execute("""
                    SELECT started_at, stopped_at, accumulated_seconds, is_running
                    FROM public.deployment_rollback_timers
                    WHERE deployment_id = %s FOR UPDATE
                """, (deployment_id,))
                row = cur.fetchone()
                started_at, stopped_at, accumulated_seconds, is_running = row
                now = datetime.now().astimezone()

                if action == "start":
                    if is_running:
                        return jsonify({"success": True, "message": "Rollback timer is already running.",
                                        "timer": serialize_running_timer(row)})
                    cur.execute("""
                        UPDATE public.deployment_rollback_timers
                        SET started_at = %s, stopped_at = NULL, is_running = TRUE,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE deployment_id = %s
                        RETURNING started_at, stopped_at, accumulated_seconds, is_running
                    """, (now, deployment_id))
                    updated = cur.fetchone()
                    cur.execute("""
                        INSERT INTO public.deployment_rollback_timer_history
                            (deployment_id, cr_number, started_at) VALUES (%s, %s, %s)
                    """, (deployment_id, cr_number, now))
                    message = "Rollback timer started. Start date and time recorded."
                else:
                    if not is_running or not started_at:
                        return jsonify({"success": False, "message": "The rollback timer is not currently running."}), 409
                    elapsed = max(0, int((now - started_at).total_seconds()))
                    cur.execute("""
                        UPDATE public.deployment_rollback_timers
                        SET stopped_at = %s, accumulated_seconds = accumulated_seconds + %s,
                            is_running = FALSE, updated_at = CURRENT_TIMESTAMP
                        WHERE deployment_id = %s
                        RETURNING started_at, stopped_at, accumulated_seconds, is_running
                    """, (now, elapsed, deployment_id))
                    updated = cur.fetchone()
                    cur.execute("""
                        UPDATE public.deployment_rollback_timer_history
                        SET stopped_at = %s, duration_seconds = %s
                        WHERE id = (
                            SELECT id FROM public.deployment_rollback_timer_history
                            WHERE deployment_id = %s AND stopped_at IS NULL
                            ORDER BY id DESC LIMIT 1
                        )
                    """, (now, elapsed, deployment_id))
                    message = "Rollback timer stopped. Stop date and time recorded."
        write_audit_log("INFO", f"Rollback timer {action} for deployment ID={deployment_id}", cr_number)
        return jsonify({"success": True, "message": message, "timer": serialize_running_timer(updated)})
    except Exception as e:
        app.logger.exception("Could not update rollback timer: %s", deployment_id)
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route("/api/rollback-daily", methods=["GET"])
def rollback_daily():
    """Rolled-back deployments of ONE day with their rollback timer state, plus that day's sessions."""
    date_text = (request.args.get("date") or "").strip()
    try:
        selected = date.fromisoformat(date_text) if date_text else date.today()
    except ValueError:
        return jsonify({"success": False, "message": "Choose a valid date (YYYY-MM-DD)."}), 400
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                ensure_rollback_timer_tables(cur)
                cur.execute("""
                    SELECT d.id, d.cr_number, d.rfid, d.application_name, d.status,
                           d.change_type, d.roll_back, d.slots,
                           t.started_at, t.stopped_at,
                           COALESCE(t.accumulated_seconds, 0), COALESCE(t.is_running, FALSE)
                    FROM public.deployment_tracker d
                    LEFT JOIN public.deployment_rollback_timers t ON t.deployment_id = d.id
                    WHERE d.deployment_date = %s
                      AND (LOWER(TRIM(COALESCE(d.roll_back, ''))) = 'yes'
                           OR LOWER(COALESCE(d.status, '')) LIKE '%%roll%%'
                           OR t.started_at IS NOT NULL)
                    ORDER BY d.slot_start NULLS LAST, d.id
                """, (selected,))
                rows = cur.fetchall()
                cur.execute("""
                    SELECT h.id, h.deployment_id, h.cr_number, d.rfid, d.application_name,
                           h.started_at, h.stopped_at, h.duration_seconds
                    FROM public.deployment_rollback_timer_history h
                    JOIN public.deployment_tracker d ON d.id = h.deployment_id
                    WHERE d.deployment_date = %s
                    ORDER BY h.started_at DESC
                    LIMIT 500
                """, (selected,))
                hrows = cur.fetchall()
                cur.execute("SELECT COUNT(*) FROM public.deployment_rollback_timers WHERE is_running")
                running_total = int(cur.fetchone()[0] or 0)

        now = datetime.now().astimezone()
        items = []
        for (dep_id, cr, rfid, app_name, status, ctype, rb, slots,
             started_at, stopped_at, acc, is_running) in rows:
            is_running = bool(is_running)
            elapsed = int(acc or 0)
            if is_running and started_at:
                elapsed += max(0, int((now - started_at).total_seconds()))
            if is_running:
                state, label = "running", "ROLLBACK RUNNING"
            elif started_at:
                state, label = "completed", "ROLLBACK DONE"
            else:
                state, label = "pending", "NOT STARTED"
            items.append({
                "deployment_id": dep_id, "cr_number": cr, "rfid": rfid,
                "application_name": app_name, "status": status, "change_type": ctype,
                "roll_back": rb, "slots": slots,
                "started_at": started_at.isoformat() if started_at else None,
                "stopped_at": stopped_at.isoformat() if stopped_at else None,
                "has_timer": started_at is not None,
                "is_running": is_running, "elapsed_seconds": elapsed,
                "rb_state": state, "rb_label": label,
            })
        items.sort(key=lambda i: {"running": 0, "pending": 1, "completed": 2}[i["rb_state"]])

        sessions = []
        for hid, dep_id, cr, rfid, app_name, started, stopped, dur in hrows:
            sessions.append({
                "id": hid, "deployment_id": dep_id, "cr_number": cr, "rfid": rfid,
                "application_name": app_name,
                "started_at": started.isoformat() if started else None,
                "stopped_at": stopped.isoformat() if stopped else None,
                "is_running": stopped is None,
                "duration_seconds": int(dur) if dur is not None
                    else max(0, int((now - started).total_seconds())),
            })
        counts = {"running": 0, "completed": 0, "pending": 0}
        for i in items:
            counts[i["rb_state"]] += 1
        return jsonify({
            "success": True, "date": selected.isoformat(), "count": len(items),
            "counts": counts, "running_total": running_total,
            "items": items, "sessions": sessions,
        })
    except Exception:
        app.logger.exception("Could not load daily rollback status")
        return jsonify({"success": False, "message": "Unable to load rollback status."}), 500
    finally:
        if conn is not None:
            conn.close()


'''

CSS_ROLLBACK = r'''
/* ===== Rollback status (manager "Rolled Back" view) ===== */
.rs-card.cs-rb-running { background:linear-gradient(135deg,#be123c 0%,#ea580c 100%); box-shadow:0 8px 20px #ea580c40; }
.rs-card.cs-rb-completed { background:linear-gradient(135deg,#6d28d9 0%,#a78bfa 100%); box-shadow:0 8px 20px #7c3aed35; }
.rs-card.cs-rb-pending { background:linear-gradient(135deg,#64748b 0%,#94a3b8 100%); box-shadow:0 8px 20px #64748b30; }
.rb-section { margin:24px 0 10px; font-size:13px; font-weight:850; color:#172554; }
'''

MGR_RB_HELPERS = r'''const fmtT = iso => iso ? new Date(iso).toLocaleString() : '—';
    const rbSecs = t => t.elapsed_seconds + (t.is_running ? (Date.now() - rbFetchedAt) / 1000 : 0);
    const rbHistSecs = h => h.duration_seconds + (h.is_running ? (Date.now() - rbFetchedAt) / 1000 : 0);

    function rbCard(t) {
        const c = t.rb_state;
        let timeHtml, meta;
        if (t.has_timer) {
            timeHtml = '<div class="rs-time" data-rb-id="' + esc(t.deployment_id) + '">' + fmtD(rbSecs(t)) + '</div>';
            meta = t.is_running ? 'Rollback started ' + fmtShort(t.started_at) : 'Rollback stopped ' + fmtShort(t.stopped_at);
        } else {
            timeHtml = '<div class="rs-time rs-slot">' + esc(slotStart(t)) + '</div>';
            meta = 'Rollback not started · slot ' + (t.slots || '—');
        }
        return '<div class="rs-card cs-rb-' + c + (c === 'running' ? ' running' : '') + '">' +
            '<div class="rs-card-top"><span class="rs-cr" title="' + esc(t.cr_number) + '">' + esc(t.cr_number || '—') + '</span><span class="rs-pill">' + esc(t.rb_label) + '</span></div>' +
            '<div class="rs-app" title="' + esc(t.application_name) + '">' + esc(t.application_name || 'Application not set') + '</div>' +
            timeHtml + '<div class="rs-meta">' + esc(meta) + '</div>' +
            '<div class="rs-foot">RFID ' + esc(t.rfid || '—') + ' · ' + esc(t.status || 'No status') + '</div>' +
            '<div class="rs-bar"></div></div>';
    }

    function rollbackHtml() {
        const q = search.value.trim().toLowerCase();
        const match = r => !q || String(r.cr_number || '').toLowerCase().includes(q) || String(r.application_name || '').toLowerCase().includes(q);
        const rows = rbItems.filter(match), hist = rbSessions.filter(match);
        const n = { running: 0, completed: 0, pending: 0 };
        rbItems.forEach(i => { n[i.rb_state] = (n[i.rb_state] || 0) + 1; });
        let h = '<div class="rs-chips" style="margin-bottom:14px"><span class="rs-chip run">Rollback running: ' + n.running +
            '</span><span class="rs-chip stop">Rollback done: ' + n.completed + '</span><span class="rs-chip tot">Not started: ' + n.pending + '</span></div>';
        h += rows.length
            ? '<div class="rs-cards">' + rows.map(rbCard).join('') + '</div>'
            : '<div class="rs-empty"><b>' + (rbItems.length ? 'No matches' : 'No rolled-back deployments on this day') + '</b>' +
              'A deployment appears here when Roll Back = Yes, its status is Rolled, or its rollback timer was started.</div>';
        if (hist.length) {
            h += '<div class="rb-section">ROLLBACK START / STOP HISTORY</div><div class="tablewrap"><table style="min-width:900px"><thead><tr><th>SR No.</th><th>CR Number</th><th>RFID</th><th>Application</th><th>Started At</th><th>Stopped At</th><th>Duration</th><th>Status</th></tr></thead><tbody>' +
                hist.map((r, i) => '<tr><td>' + (i + 1) + '</td><td><strong>' + esc(r.cr_number) + '</strong></td><td>' + esc(r.rfid) + '</td><td>' + esc(r.application_name) +
                    '</td><td>' + fmtT(r.started_at) + '</td><td>' + (r.is_running ? '—' : fmtT(r.stopped_at)) +
                    '</td><td data-rbh-id="' + esc(r.id) + '">' + fmtD(rbHistSecs(r)) + '</td><td>' +
                    (r.is_running ? '<span class="rs-hpill amber">● Rollback running</span>' : '<span class="rs-hpill grey">■ Stopped</span>') + '</td></tr>').join('') +
                '</tbody></table></div>';
        }
        return h;
    }

    setInterval(() => {
        if (modal.hidden || document.hidden || view !== 'rollback') return;
        rbItems.forEach(t => { if (!t.is_running) return;
            const el = body.querySelector('[data-rb-id="' + t.deployment_id + '"]'); if (el) el.textContent = fmtD(rbSecs(t)); });
        rbSessions.forEach(h => { if (!h.is_running) return;
            const el = body.querySelector('[data-rbh-id="' + h.id + '"]'); if (el) el.textContent = fmtD(rbHistSecs(h)); });
    }, 1000);'''

MGR_RB_LOAD = r'''items = data.items || []; sessions = data.sessions || [];
            try {
                const rr = await fetch('/api/rollback-daily?date=' + encodeURIComponent(day), { cache: 'no-store' });
                const rd = await rr.json();
                if (my !== seq) return;
                if (rd.success) {
                    rbItems = rd.items || []; rbSessions = rd.sessions || []; rbFetchedAt = Date.now();
                    const rbb = document.getElementById('rbBadge');
                    if (rbb) { rbb.textContent = rd.running_total || 0; rbb.hidden = !rd.running_total; }
                }
            } catch (_) { /* rollback API not available yet */ }'''

MGR_RB_OPEN = r'''modal.addEventListener('modal:open', () => { view = window.__rsOpenRollback ? 'rollback' : 'live'; window.__rsOpenRollback = false; render(); load(); });
    const rbBtn = document.getElementById('rspRollbackBtn');
    if (rbBtn) rbBtn.addEventListener('click', () => { window.__rsOpenRollback = true; });'''

MGR_RB_BTN = (
    '<button class="btn light" type="button" id="rspRollbackBtn" data-open-modal="runningStatusModal">'
    '↶ Rolled Back <span class="mgr-live-badge" id="rbBadge" hidden>0</span></button>\n'
    '                    <button class="btn light" type="button" data-open-modal="runningStatusModal">Full view ↗</button>'
)

GOV_TAB_BTN = (
    '<button type="button" class="tracker-tab" id="runningStatusTab" role="tab" aria-selected="false" '
    'aria-controls="runningStatusPanel">Running Status</button>\n'
    '      <button type="button" class="tracker-tab" id="rollbackStatusTab" role="tab" aria-selected="false" '
    'aria-controls="rollbackStatusPanel">Rollback Status</button>'
)

GOV_PANEL = r'''<div id="rollbackStatusPanel" class="tracker-tab-panel" role="tabpanel" aria-labelledby="rollbackStatusTab">
      <div class="field full" id="rollbackStatusField" style="padding:20px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r)">
        <h3 style="margin:0 0 8px">Rollback Running Timer</h3>
        <p class="muted" style="margin:0 0 16px">Start this timer when the rollback work begins and stop it when the rollback is complete. It is saved against this deployment and appears on the Manager dashboard under Running Status &rarr; Rolled Back.</p>
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:18px">
          <button type="button" id="startRollbackTimerButton" class="approve">Start Rollback</button>
          <button type="button" id="stopRollbackTimerButton" class="reject" disabled>Stop Rollback</button>
          <span id="rollbackTimerState" class="badge">Not Started</span>
        </div>
        <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px">
          <div><label>Elapsed Time</label><div id="rollbackElapsedTime" style="font-size:24px;font-weight:800;color:var(--bad);font-variant-numeric:tabular-nums">00:00:00</div></div>
          <div><label>Accumulated Duration</label><div id="rollbackAccumulatedTime" style="font-weight:700">00:00:00</div></div>
          <div><label>Last Start Date &amp; Time</label><div id="rollbackStartedAt" class="muted">Not started</div></div>
          <div><label>Last Stop Date &amp; Time</label><div id="rollbackStoppedAt" class="muted">Not stopped</div></div>
        </div>
        <div id="rollbackTimerMessage" class="muted" role="status" aria-live="polite" style="margin-top:14px"></div>
      </div>
    </div>

    <div id="jenkinsBuildsPanel" class="tracker-tab-panel" role="tabpanel" aria-labelledby="jenkinsBuildsTab">'''

GOV_JS = r'''// Rollback timer: separate from the deployment timer, saved per deployment ID.
  let rollbackTimerInterval = null;
  let rollbackTimerState = null;
  const startRollbackTimerButton = document.getElementById("startRollbackTimerButton");
  const stopRollbackTimerButton = document.getElementById("stopRollbackTimerButton");
  const rollbackTimerMessage = document.getElementById("rollbackTimerMessage");

  function renderRollbackTimer() {
    const state = rollbackTimerState || {};
    const isRunning = Boolean(state.is_running);
    const accumulated = Number(state.accumulated_seconds || 0);
    const liveSeconds = isRunning && state.started_at
      ? Math.max(0, Math.floor((Date.now() - new Date(state.started_at).getTime()) / 1000))
      : 0;
    document.getElementById("rollbackElapsedTime").textContent = formatDuration(accumulated + liveSeconds);
    document.getElementById("rollbackAccumulatedTime").textContent = formatDuration(accumulated);
    document.getElementById("rollbackStartedAt").textContent = formatTimerDate(state.started_at);
    document.getElementById("rollbackStoppedAt").textContent = formatTimerDate(state.stopped_at);
    document.getElementById("rollbackTimerState").textContent =
      isRunning ? "Rollback Running" : (state.started_at ? "Stopped" : "Not Started");
    startRollbackTimerButton.disabled = isRunning || !currentDeploymentId;
    stopRollbackTimerButton.disabled = !isRunning || !currentDeploymentId;
    if (rollbackTimerInterval) { clearInterval(rollbackTimerInterval); rollbackTimerInterval = null; }
    if (isRunning) rollbackTimerInterval = setInterval(renderRollbackTimer, 1000);
  }

  async function loadRollbackTimer() {
    if (!currentDeploymentId) return;
    rollbackTimerMessage.textContent = "Loading rollback timer...";
    try {
      const response = await fetch(
        "/api/deployment-rollback-timer/" + encodeURIComponent(currentDeploymentId),
        { headers: { Accept: "application/json" }, cache: "no-store" }
      );
      const result = await parseApiResponse(response);
      if (!result.success) throw new Error(result.message || "Could not load the rollback timer.");
      rollbackTimerState = result.timer || {};
      renderRollbackTimer();
      rollbackTimerMessage.textContent = "Rollback timer is saved against this deployment record.";
    } catch (error) {
      rollbackTimerMessage.textContent = error.message || "Could not load the rollback timer.";
      console.error("Rollback timer load error:", error);
    }
  }

  async function changeRollbackTimer(action) {
    if (!currentDeploymentId) {
      showMessage("Open a CR deployment record before using the rollback timer.", "error");
      return;
    }
    const button = action === "start" ? startRollbackTimerButton : stopRollbackTimerButton;
    button.disabled = true;
    rollbackTimerMessage.textContent = action === "start" ? "Starting rollback timer..." : "Stopping rollback timer...";
    try {
      const response = await fetch(
        "/api/deployment-rollback-timer/" + encodeURIComponent(currentDeploymentId),
        {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ action })
        }
      );
      const result = await parseApiResponse(response);
      if (!result.success) throw new Error(result.message || "Rollback timer action failed.");
      rollbackTimerState = result.timer || {};
      renderRollbackTimer();
      rollbackTimerMessage.textContent = result.message || "Rollback timer updated successfully.";
    } catch (error) {
      rollbackTimerMessage.textContent = error.message || "Could not update the rollback timer.";
      renderRollbackTimer();
    }
  }

  startRollbackTimerButton.addEventListener("click", () => changeRollbackTimer("start"));
  stopRollbackTimerButton.addEventListener("click", () => changeRollbackTimer("stop"));

  function toggleAuditIdentityFields() {'''

PATCHES += [
    # ---- app.py: rollback timer API + manager feed ----
    ("app", "4 rollback", "routes: rollback timer + /api/rollback-daily",
     "def rollback_daily",
     r'@app\.route\("/api/deployment-by-id/<int:deployment_id>", methods=\["PUT"\]\)',
     lambda m: ROLLBACK_APP + m.group(0), True),

    # ---- governance_tracker.html: Rollback Status tab ----
    ("gov", "4 rollback", "tab button next to Running Status",
     'id="rollbackStatusTab"',
     '<button type="button" class="tracker-tab" id="runningStatusTab" role="tab" aria-selected="false" '
     'aria-controls="runningStatusPanel">Running Status</button>',
     GOV_TAB_BTN, False),
    ("gov", "4 rollback", "tab panel (Start / Stop / timers)",
     'id="rollbackStatusPanel" class',
     '<div id="jenkinsBuildsPanel" class="tracker-tab-panel" role="tabpanel" aria-labelledby="jenkinsBuildsTab">',
     GOV_PANEL, False),
    ("gov", "4 rollback", "tab switching map",
     'rollback: ["rollbackStatusTab"',
     'running: ["runningStatusTab", "runningStatusPanel"],',
     'running: ["runningStatusTab", "runningStatusPanel"],\n'
     '      rollback: ["rollbackStatusTab", "rollbackStatusPanel"],', False),
    ("gov", "4 rollback", "tab click handler",
     'getElementById("rollbackStatusTab").addEventListener',
     'document.getElementById("runningStatusTab").addEventListener("click", () => activateTrackerTab("running"));',
     'document.getElementById("runningStatusTab").addEventListener("click", () => activateTrackerTab("running"));\n'
     '  document.getElementById("rollbackStatusTab").addEventListener("click", () => activateTrackerTab("rollback"));', False),
    ("gov", "4 rollback", "timer script (load / start / stop)",
     "function loadRollbackTimer",
     "function toggleAuditIdentityFields() {", GOV_JS, False),
    ("gov", "4 rollback", "load rollback timer when a record opens",
     "loadRollbackTimer();",
     "runningTimerState = null; renderRunningTimer(); loadRunningTimer();",
     "runningTimerState = null;\n    renderRunningTimer();\n    loadRunningTimer();\n"
     "    rollbackTimerState = null;\n    renderRollbackTimer();\n    loadRollbackTimer();", False),
    ("gov", "4 rollback", "mobile layout for rollback grid",
     "#rollbackStatusField >",
     '#runningStatusField > div[style*="grid-template-columns"] { grid-template-columns: 1fr !important; }',
     '#runningStatusField > div[style*="grid-template-columns"], '
     '#rollbackStatusField > div[style*="grid-template-columns"] { grid-template-columns: 1fr !important; }', False),

    # ---- manager.html: Running Status -> Rolled Back ----
    ("html", "4 rollback", "CSS: rollback card colours",
     "Rollback status (manager", "</style>", CSS_ROLLBACK + "</style>", False),
    ("html", "4 rollback", "full view: 'Rolled Back' tab",
     'data-rsv-view="rollback"',
     '<button type="button" data-rsv-view="history">History</button>',
     '<button type="button" data-rsv-view="rollback">↶ Rolled Back</button>'
     '<button type="button" data-rsv-view="history">History</button>', False),
    ("html", "4 rollback", "full view: rollback state variables",
     "let rbItems",
     "let view = 'live', filter = 'all', items = [], sessions = [], fetchedAt = Date.now(), ticks = 0, seq = 0;",
     "let view = 'live', filter = 'all', items = [], sessions = [], fetchedAt = Date.now(), ticks = 0, seq = 0;\n"
     "    let rbItems = [], rbSessions = [], rbFetchedAt = Date.now();", False),
    ("html", "4 rollback", "full view: rollback renderer",
     "function rollbackHtml",
     "const fmtT = iso => iso ? new Date(iso).toLocaleString() : '—';", MGR_RB_HELPERS, False),
    ("html", "4 rollback", "full view: render rollback tab",
     "body.innerHTML = rollbackHtml();",
     "} else { const rows = sessions.filter(r =>",
     "} else if (view === 'rollback') {\n            body.innerHTML = rollbackHtml();\n"
     "        } else {\n            const rows = sessions.filter(r =>", False),
    ("html", "4 rollback", "full view: fetch /api/rollback-daily",
     "/api/rollback-daily",
     "items = data.items || []; sessions = data.sessions || [];", MGR_RB_LOAD, False),
    ("html", "4 rollback", "full view: open straight on Rolled Back",
     "__rsOpenRollback",
     "modal.addEventListener('modal:open', () => { view = 'live'; render(); load(); });",
     MGR_RB_OPEN, False),
    ("html", "4 rollback", "dashboard panel: Rolled Back button + live badge",
     'id="rspRollbackBtn"',
     '<button class="btn light" type="button" data-open-modal="runningStatusModal">Full view ↗</button>',
     MGR_RB_BTN, False),
]


CSS_TIDY = r"""
/* ===== Running Status panel: tidy toolbar + hideable counters ===== */
.rsp-stats[hidden] { display:none !important; }
.rsp-panel .panelhead { align-items:center; margin-bottom:12px; }
.rsp-head-right { gap:10px; row-gap:8px; justify-content:flex-end; }
.rsp-head-right .btn { padding:9px 13px; }
.rsp-daynav { padding-right:12px; border-right:1px solid #e3e9f7; }
.rsp-bar-row { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; margin:0 0 12px; padding:8px 12px; border-radius:12px; background:#f3f6ff; }
.rsp-bar-row .rsp-day { margin:0; }
.rsp-statsbtn { padding:6px 12px; border:1px solid #cfd8fb; border-radius:20px; background:#fff; color:#3656c8; font-size:11px; font-weight:800; cursor:pointer; box-shadow:none; transition:.15s; }
.rsp-statsbtn:hover { background:#4f67f6; border-color:#4f67f6; color:#fff; }
.rsp-stats { margin-bottom:14px; }
.rsp-stat { padding:8px 6px; }
.rsp-stat b { font-size:20px; }
@media (max-width:760px) { .rsp-daynav { border-right:0; padding-right:0; } }
"""

TIDY_ROW_NEW = (
    '<div class="rsp-bar-row">\n'
    '                <div class="rsp-day" id="rspDayLabel"></div>\n'
    '                <button type="button" class="rsp-statsbtn" id="rspStatsToggle" aria-expanded="true" '
    'title="Show or hide the colour counters">◎ Hide counts</button>\n'
    '            </div>'
)

TIDY_JS_NEW = r"""const statsBox = document.querySelector('.rsp-stats'), statsBtn = $('rspStatsToggle');
    function setStatsHidden(hide) {
        statsBox.hidden = hide;
        statsBtn.setAttribute('aria-expanded', String(!hide));
        statsBtn.textContent = hide ? '◉ Show counts' : '◎ Hide counts';
        try { localStorage.setItem('rspStatsHidden', hide ? '1' : '0'); } catch (_) { /* storage unavailable */ }
        if (hide && cat !== 'all') { cat = 'all'; render(); }
    }
    statsBtn.addEventListener('click', () => setStatsHidden(!statsBox.hidden));
    let startHidden = false;
    try { startHidden = localStorage.getItem('rspStatsHidden') === '1'; } catch (_) { /* ignore */ }
    if (startHidden) setStatsHidden(true);
    $('rspToday').addEventListener('click', () => setDay(ymd(new Date())));"""

PATCHES += [
    ("html", "5 tidy", "CSS: tidy toolbar + hideable counters",
     "Running Status panel: tidy toolbar", "</style>", CSS_TIDY + "</style>", False),
    ("html", "5 tidy", "panel: Hide/Show counts button",
     'id="rspStatsToggle"',
     '<div class="rsp-day" id="rspDayLabel"></div>', TIDY_ROW_NEW, False),
    ("html", "5 tidy", "panel: Hide/Show counts script (remembers choice)",
     "$('rspStatsToggle')",
     "$('rspToday').addEventListener('click', () => setDay(ymd(new Date())));", TIDY_JS_NEW, False),
]


CRD_BACKEND = r'''
CR_DETAIL_JSON_FIELDS = ("git_links", "team_allocations")


def _detail_clean(field, value):
    """JSON-safe value; decode JSON text stored in git_links / team_allocations."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if field in CR_DETAIL_JSON_FIELDS and isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    return str(value)


def _detail_slot_text(start, end):
    def hhmm(v):
        try:
            return datetime.fromisoformat(str(v)).strftime("%H:%M")
        except (TypeError, ValueError):
            return None
    s, e = hhmm(start), hhmm(end)
    return f"{s} - {e}" if s and e else None


@app.route("/api/deployment-detail/<int:deployment_id>", methods=["GET"])
def deployment_detail(deployment_id):
    """Full CR detail for the Manager popup.

    original = what the requester submitted in the Release Calendar
               (rebuilt from the earliest 'old' value of every audited field)
    current  = latest values (including DevOps edits)
    history  = every audited change, newest first
    """
    conn = None
    cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM public.deployment_tracker WHERE id = %s", (deployment_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify({"success": False, "message": "Deployment record not found."}), 404
        columns = [d[0] for d in cur.description]
        current = {k: _detail_clean(k, v) for k, v in zip(columns, row)}

        cur.execute(
            """
            SELECT id, modified_at, modified_by, action, changes
            FROM public.deployment_change_history
            WHERE deployment_id = %s
            ORDER BY modified_at ASC, id ASC
            """,
            (deployment_id,),
        )
        events = []
        for hid, modified_at, modified_by, action, changes in cur.fetchall():
            if isinstance(changes, str):
                try:
                    changes = json.loads(changes)
                except (TypeError, ValueError):
                    changes = {}
            if not isinstance(changes, dict):
                changes = {}
            events.append({
                "id": hid,
                "modified_at": modified_at.isoformat() if modified_at else None,
                "modified_by": modified_by,
                "action": action,
                "changes": changes,
            })

        original = dict(current)
        seen, changed_fields = set(), set()
        for ev in events:
            for field, vals in ev["changes"].items():
                if not isinstance(vals, dict):
                    continue
                changed_fields.add(field)
                if field not in seen and "old" in vals:
                    original[field] = _detail_clean(field, vals["old"])
                    seen.add(field)

        original["slot"] = (
            _detail_slot_text(original.get("slot_start"), original.get("slot_end"))
            or current.get("slots")
        )
        current["slot"] = (
            _detail_slot_text(current.get("slot_start"), current.get("slot_end"))
            or current.get("slots")
        )
        if "slot_start" in changed_fields or "slot_end" in changed_fields:
            changed_fields.add("slot")

        history = []
        for ev in reversed(events):
            history.append({
                "id": ev["id"],
                "modified_at": ev["modified_at"],
                "modified_by": ev["modified_by"],
                "action": ev["action"],
                "changes": [
                    {
                        "field": f,
                        "old": _detail_clean(f, v.get("old")),
                        "new": _detail_clean(f, v.get("new")),
                    }
                    for f, v in ev["changes"].items() if isinstance(v, dict)
                ],
            })

        last = history[0] if history else None
        return jsonify({
            "success": True,
            "deployment": {
                "id": current.get("id"),
                "cr_number": current.get("cr_number"),
                "rfid": current.get("rfid"),
                "application_name": current.get("application_name"),
                "status": current.get("status"),
            },
            "original": original,
            "current": current,
            "changed_fields": sorted(changed_fields),
            "last_modified": (
                {"by": last["modified_by"], "at": last["modified_at"], "action": last["action"]}
                if last else None
            ),
            "history": history,
        })
    except Exception:
        app.logger.exception("Could not load deployment detail %s", deployment_id)
        return jsonify({"success": False, "message": "Unable to load deployment details. Check the Flask logs."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


'''

CRD_CSS = r"""
/* ===== CR detail popup (opened from any Running Status CR card) ===== */
[data-dep-id] { cursor:pointer; }
[data-dep-id]:focus-visible { outline:3px solid #ffffffcc; outline-offset:2px; }
.crd-overlay { position:fixed; inset:0; z-index:1200; display:flex; align-items:flex-start; justify-content:center;
    padding:3vh 16px; background:#0b1230bf; backdrop-filter:blur(3px); overflow:auto; }
.crd-overlay[hidden] { display:none !important; }
.crd-box { width:min(1180px,100%); max-height:94vh; overflow:auto; background:#f5f7fc; border-radius:20px;
    box-shadow:0 30px 80px #0007; animation:mgrIn .18s ease; }
.crd-top { position:sticky; top:0; z-index:3; display:flex; justify-content:space-between; align-items:flex-start; gap:14px; flex-wrap:wrap;
    padding:20px 24px; color:#fff;
    background:radial-gradient(circle at 92% 0%,#8b5cf6 0,transparent 42%),radial-gradient(circle at 5% 0%,#0891b2 0,transparent 36%),linear-gradient(120deg,#111a3d,#243b82 60%,#3449a5); }
.crd-top h3 { margin:0; font-size:22px; letter-spacing:-.4px; color:#fff; }
.crd-top p { margin:7px 0 0; color:#cbd5ff; font-size:12px; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.crd-top-actions { display:flex; align-items:center; gap:9px; flex-wrap:wrap; }
.crd-top .mgr-close { background:#ffffff26; color:#fff; }
.crd-top .mgr-close:hover { background:#dc2626; }
.crd-count { display:inline-grid; place-items:center; min-width:20px; height:20px; padding:0 6px; border-radius:10px; background:#ffffff30; font-size:10px; font-weight:800; }
.crd-body { padding:20px 24px 26px; }
.crd-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; align-items:start; }
@media (max-width:900px) { .crd-grid { grid-template-columns:1fr; } .crd-top, .crd-body { padding-left:14px; padding-right:14px; } }
.crd-panel { overflow:hidden; background:#fff; border:1px solid var(--line); border-radius:18px; box-shadow:0 6px 22px #1b2d590a; }
.crd-ph { padding:15px 18px; }
.crd-ph h4 { margin:0; font-size:14px; font-weight:850; }
.crd-ph small { display:block; margin-top:4px; font-size:11px; font-weight:600; opacity:.85; line-height:1.45; }
.crd-panel.rel .crd-ph { background:linear-gradient(120deg,#e9edff,#f3eeff); color:#3656c8; }
.crd-panel.dev .crd-ph { background:linear-gradient(120deg,#dcfce7,#dff8fc); color:#047857; }
.crd-kv { display:grid; grid-template-columns:150px minmax(0,1fr); gap:4px 12px; padding:10px 18px; border-top:1px solid #edf1f6; font-size:12px; }
.crd-kv > span { color:#8a96aa; font-weight:700; font-size:11px; letter-spacing:.2px; }
.crd-kv > div { color:#243050; font-weight:600; overflow-wrap:anywhere; }
.crd-kv a { color:#3656c8; font-weight:700; text-decoration:none; }
.crd-kv a:hover { text-decoration:underline; }
.crd-kv.crd-changed { background:#fffbeb; box-shadow:inset 3px 0 0 #f59e0b; }
@media (max-width:520px) { .crd-kv { grid-template-columns:1fr; } }
.crd-was { display:block; margin-top:4px; color:#a16207; font-size:10.5px; font-weight:600; }
.crd-empty { color:#a0aabd; }
.crd-pre { white-space:pre-wrap; }
.crd-chip { display:inline-block; margin:0 4px 4px 0; padding:3px 9px; border-radius:20px; background:#edf1ff; color:#3656c8; font-size:11px; font-weight:700; }
.crd-chip b { color:#172554; }
.crd-foot { display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap; padding:13px 18px; border-top:1px solid #edf1f6; background:#fafbff; font-size:11px; color:#64748b; }
.crd-foot .btn { padding:8px 12px; }
.crd-loading, .crd-emptybox { padding:46px 20px; text-align:center; color:#8a96aa; font-size:13px; line-height:1.6; }
.crd-emptybox b { display:block; margin-bottom:3px; font-size:14px; color:#3656c8; }
.crd-timeline { display:grid; gap:14px; }
.crd-ev { overflow:hidden; background:#fff; border:1px solid var(--line); border-radius:16px; box-shadow:0 4px 16px #1b2d5909; }
.crd-ev-head { display:flex; align-items:center; gap:12px; padding:13px 16px; background:linear-gradient(90deg,#fbfcff,#f5f7ff); border-bottom:1px solid #edf1f6; }
.crd-avatar { flex-shrink:0; width:36px; height:36px; display:grid; place-items:center; border-radius:50%; color:#fff; font-size:12px; font-weight:800; background:linear-gradient(135deg,#4f67f6,#7c5cf0); }
.crd-ev-head b { display:block; font-size:13px; color:#172554; overflow-wrap:anywhere; }
.crd-ev-head small { display:block; margin-top:2px; font-size:11px; color:#8a96aa; }
.crd-action { margin-left:auto; padding:4px 10px; border-radius:7px; background:#e8edff; color:#3656c8; font-size:10px; font-weight:800; letter-spacing:.4px; text-transform:uppercase; }
.crd-ev table { width:100%; min-width:0; table-layout:fixed; font-size:12px; }
.crd-ev th, .crd-ev td { padding:10px 14px; white-space:normal; overflow-wrap:anywhere; vertical-align:top; }
.crd-ev th { position:static; }
.crd-ev td.crd-old { background:#fff5f5; }
.crd-ev td.crd-new { background:#f0fdf4; }
.crd-ev td:first-child { font-weight:750; color:#475569; width:24%; }
"""

CRD_HTML = r"""
    <!-- ===== CR detail popup (Release Calendar + DevOps latest + Change History) ===== -->
    <div class="crd-overlay" id="crDetailModal" hidden>
        <div class="crd-box" role="dialog" aria-modal="true" aria-label="CR details">
            <header class="crd-top">
                <div>
                    <h3 id="crdTitle">CR details</h3>
                    <p id="crdSub">Loading…</p>
                </div>
                <div class="crd-top-actions">
                    <button class="btn ghost" type="button" id="crdHistoryBtn" data-crd-toggle>⟲ Change History <span class="crd-count" id="crdHistCount">0</span></button>
                    <button class="mgr-close" type="button" id="crdClose" aria-label="Close CR details">✕</button>
                </div>
            </header>
            <div class="crd-body" id="crdBody"><div class="crd-loading">Loading details…</div></div>
        </div>
    </div>

"""

CRD_JS = r"""
<script>
(function crDetailView() {
    const $ = id => document.getElementById(id);
    const overlay = $('crDetailModal');
    if (!overlay) return;
    const body = $('crdBody'), title = $('crdTitle'), sub = $('crdSub'), histBtn = $('crdHistoryBtn'), histCount = $('crdHistCount');

    const esc = s => String(s ?? '').replace(/[&<>"']/g,
        c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

    const RELEASE = [
        ['cr_number','CR Number'], ['rfid','RFID'], ['application_name','Application'], ['category','Category'],
        ['business_unit','Business Unit'], ['environment','Environment'], ['change_type','Change Type'],
        ['deployment_type','Deployment Type'], ['deployment_count','Deployment Count'], ['change_requestor','Change Requestor'],
        ['deployment_date','Deployment Date'], ['slot','Slot'], ['git_links','Git Links'],
        ['incident_number','Incident Number'], ['roll_back','Roll Back'], ['status','Initial Status']
    ];
    const DEVOPS = [
        ['status','Status'], ['deployment_date','Deployment Date'], ['slot','Slot'], ['application_name','Application'],
        ['deployment_count','Deployment Count'], ['team_allocations','Team Allocation'], ['deployment_type','Deployment Type'],
        ['change_type','Change Type'], ['released_by','Released By'], ['verified_and_deployed_by','Verified & Deployed By'],
        ['change_requestor','Change Requestor'], ['incident_number','Incident Number'], ['roll_back','Roll Back'],
        ['git_links','Git Links'], ['comments','Comments']
    ];
    const LABELS = {};
    RELEASE.concat(DEVOPS).forEach(p => { LABELS[p[0]] = p[1]; });
    LABELS.slot_start = 'Slot Start'; LABELS.slot_end = 'Slot End'; LABELS.slots = 'Slot';

    let data = null, view = 'detail', seq = 0, lastFocus = null;

    function parse(v) {
        if (typeof v === 'string') {
            const t = v.trim();
            if (t[0] === '[' || t[0] === '{') { try { return JSON.parse(t); } catch (_) { /* keep text */ } }
        }
        return v;
    }

    function valHtml(key, raw) {
        const v = parse(raw);
        if (v === null || v === undefined || v === '') return '<span class="crd-empty">—</span>';
        if (key === 'git_links') {
            const arr = Array.isArray(v) ? v : [v];
            if (!arr.length) return '<span class="crd-empty">—</span>';
            return arr.map(l => {
                const s = String(l);
                return /^https?:\/\//i.test(s)
                    ? '<a href="' + esc(s) + '" target="_blank" rel="noopener noreferrer">' + esc(s) + ' ↗</a>'
                    : esc(s);
            }).join('<br>');
        }
        if (key === 'team_allocations' && Array.isArray(v)) {
            if (!v.length) return '<span class="crd-empty">—</span>';
            return v.map(a => '<span class="crd-chip">' + esc(a && a.team) + ': <b>' + esc(a && a.count) + '</b></span>').join('');
        }
        if (key === 'slot_start' || key === 'slot_end') return esc(String(v).replace('T', ' ').slice(0, 16));
        if (key === 'status') return '<span class="pill ' + esc(String(v).toLowerCase().replace(/\s+/g, '-')) + '">' + esc(v) + '</span>';
        if (key === 'comments') return '<span class="crd-pre">' + esc(v) + '</span>';
        if (Array.isArray(v)) return esc(v.join(', '));
        if (typeof v === 'object') return esc(JSON.stringify(v));
        return esc(v);
    }

    function fmtWhen(iso) {
        if (!iso) return '—';
        const d = new Date(iso);
        return isNaN(d.getTime()) ? String(iso).replace('T', ' ') : d.toLocaleString();
    }
    function initials(name) {
        const parts = String(name || '?').split(/[^A-Za-z0-9]+/).filter(Boolean);
        return ((parts[0] || '?')[0] + (parts[1] ? parts[1][0] : '')).toUpperCase();
    }

    function isChanged(k) {
        const keys = k === 'slot' ? ['slot', 'slot_start', 'slot_end'] : [k];
        if (!keys.some(x => data.changed_fields.includes(x))) return false;
        return JSON.stringify(parse(data.original[k])) !== JSON.stringify(parse(data.current[k]));
    }

    function rows(list, src, compare) {
        return list.map(pair => {
            const k = pair[0], label = pair[1];
            const changed = compare && isChanged(k);
            return '<div class="crd-kv' + (changed ? ' crd-changed' : '') + '"><span>' + esc(label) + '</span><div>' + valHtml(k, src[k]) +
                (changed ? '<small class="crd-was">Submitted as: ' + valHtml(k, data.original[k]) + '</small>' : '') + '</div></div>';
        }).join('');
    }

    function detailHtml() {
        const n = data.history.length, lm = data.last_modified;
        const changedCount = data.changed_fields.filter(f => f !== 'slot_start' && f !== 'slot_end').length;
        const foot = lm
            ? 'Last modified by <b>' + esc(lm.by || 'Not provided') + '</b> · ' + esc(fmtWhen(lm.at)) + ' · ' + changedCount + ' field' + (changedCount === 1 ? '' : 's') + ' changed'
            : 'No DevOps edits yet. These are the original details.';
        return '<div class="crd-grid">' +
            '<section class="crd-panel rel"><div class="crd-ph"><h4>📝 Release Calendar</h4><small>Details entered by the requester when the deployment was scheduled (rebuilt from the audit trail).</small></div>' +
                rows(RELEASE, data.original, false) + '</section>' +
            '<section class="crd-panel dev"><div class="crd-ph"><h4>🛠 DevOps Workspace</h4><small>Latest modified details. Highlighted rows were changed after submission.</small></div>' +
                rows(DEVOPS, data.current, true) +
                '<div class="crd-foot"><span>' + foot + '</span>' +
                '<button class="btn light" type="button" data-crd-toggle>⟲ View change history (' + n + ')</button></div></section>' +
            '</div>';
    }

    function historyHtml() {
        const h = data.history;
        if (!h.length) {
            return '<div class="crd-emptybox"><b>No changes recorded</b>This deployment still matches what the requester submitted in the Release Calendar.</div>';
        }
        return '<div class="crd-timeline">' + h.map(ev =>
            '<article class="crd-ev"><div class="crd-ev-head"><span class="crd-avatar">' + esc(initials(ev.modified_by)) + '</span>' +
            '<div><b>' + esc(ev.modified_by || 'Not provided') + '</b><small>' + esc(fmtWhen(ev.modified_at)) + '</small></div>' +
            '<span class="crd-action">' + esc(ev.action || 'Updated') + '</span></div>' +
            '<div class="tablewrap"><table><thead><tr><th>Field</th><th>Previous value</th><th>New value</th></tr></thead><tbody>' +
            ev.changes.map(c =>
                '<tr><td>' + esc(LABELS[c.field] || String(c.field).replace(/_/g, ' ')) + '</td>' +
                '<td class="crd-old">' + valHtml(c.field, c.old) + '</td><td class="crd-new">' + valHtml(c.field, c.new) + '</td></tr>').join('') +
            '</tbody></table></div></article>').join('') + '</div>';
    }

    function render() {
        if (!data) return;
        const d = data.deployment, cur = data.current;
        title.textContent = d.cr_number || 'CR details';
        sub.innerHTML = esc(d.application_name || 'Application not set') + ' · RFID ' + esc(d.rfid || '—') +
            ' · ' + esc(cur.deployment_date || '—') + ' ' + esc(cur.slot || '') + ' ' + valHtml('status', d.status);
        histCount.textContent = data.history.length;
        histBtn.firstChild.nodeValue = view === 'history' ? '← Back to details ' : '⟲ Change History ';
        body.innerHTML = view === 'history' ? historyHtml() : detailHtml();
        body.scrollTop = 0;
        const box = overlay.querySelector('.crd-box');
        if (box) box.scrollTop = 0;
    }

    async function openDetail(id, trigger) {
        const my = ++seq;
        lastFocus = trigger || null;
        data = null; view = 'detail';
        title.textContent = 'CR details'; sub.textContent = 'Loading…'; histCount.textContent = '0';
        histBtn.firstChild.nodeValue = '⟲ Change History ';
        body.innerHTML = '<div class="crd-loading">Loading details…</div>';
        overlay.hidden = false;
        document.body.classList.add('mgr-modal-open');
        try {
            const resp = await fetch('/api/deployment-detail/' + encodeURIComponent(id), { cache: 'no-store' });
            let json;
            try { json = await resp.json(); } catch (_) { throw new Error('HTTP ' + resp.status + ', not JSON (restart Flask so the new route loads)'); }
            if (my !== seq) return;
            if (!json.success) throw new Error(json.message || 'Request failed');
            data = json; render();
        } catch (e) {
            if (my !== seq) return;
            sub.textContent = 'Could not load';
            body.innerHTML = '<div class="crd-emptybox"><b>Could not load details</b>' + esc(e && e.message ? e.message : e) + '</div>';
        }
    }

    function closeDetail() {
        seq++;
        overlay.hidden = true;
        if (!document.querySelector('[data-modal]:not([hidden])')) document.body.classList.remove('mgr-modal-open');
        if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
    }

    document.addEventListener('click', e => {
        if (overlay.contains(e.target)) {
            if (e.target === overlay || e.target.closest('#crdClose')) { closeDetail(); return; }
            if (e.target.closest('[data-crd-toggle]') && data) { view = view === 'history' ? 'detail' : 'history'; render(); }
            return;
        }
        const card = e.target.closest('[data-dep-id]');
        if (card) openDetail(card.dataset.depId, card);
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && !overlay.hidden) { e.stopImmediatePropagation(); closeDetail(); return; }
        if ((e.key === 'Enter' || e.key === ' ') && !overlay.contains(e.target)) {
            const card = e.target.closest && e.target.closest('[data-dep-id]');
            if (card) { e.preventDefault(); openDetail(card.dataset.depId, card); }
        }
    }, true);
})();
</script>

"""

CRD_BOT_ANCHOR = "<!-- ===== Page Assistant (answers only from data on this page; no AI model) ===== -->"

PATCHES += [
    # ---- app.py: detail endpoint ----
    ("app", "6 cr-detail", "route: /api/deployment-detail/<id>",
     "# --- CR DETAIL VIEW (patch) ---",
     re.escape('@app.route("/api/deployment-approval/<int:deployment_id>", methods=["POST"])'),
     lambda m: CRD_BACKEND + m.group(0), True),

    # ---- manager.html: popup CSS, markup, script ----
    ("html", "6 cr-detail", "CSS: CR detail popup",
     "CR detail popup (opened from any Running Status CR card)",
     "</style>", CRD_CSS + "</style>", False),
    ("html", "6 cr-detail", "popup markup + script",
     'id="crDetailModal"',
     re.escape(CRD_BOT_ANCHOR),
     lambda m: CRD_HTML + CRD_JS + m.group(0), True),

    # ---- manager.html: make every CR card clickable ----
    ("html", "6 cr-detail", "clickable: dashboard panel row",
     '" data-dep-id="\' + esc(t.deployment_id) + \'" role="button" tabindex="0" '
     'title="Click for full details"><div class="rsp-main">',
     r"""'<div class="rsp-row cs-' + c + (c === 'running' ? ' running' : '') + '"><div class="rsp-main">'""",
     r"""'<div class="rsp-row cs-' + c + (c === 'running' ? ' running' : '') + '" data-dep-id="' + esc(t.deployment_id) + '" role="button" tabindex="0" title="Click for full details"><div class="rsp-main">'""",
     False),
    ("html", "6 cr-detail", "clickable: full view live card",
     '(c === \'running\' ? \' running\' : \'\') + \'" data-dep-id="\' + esc(t.deployment_id) + \'" '
     'role="button" tabindex="0" title="Click for full details">\'',
     r"""'<div class="rs-card cs-' + c + (c === 'running' ? ' running' : '') + '">'""",
     r"""'<div class="rs-card cs-' + c + (c === 'running' ? ' running' : '') + '" data-dep-id="' + esc(t.deployment_id) + '" role="button" tabindex="0" title="Click for full details">'""",
     False),
    ("html", "6 cr-detail", "clickable: rolled back card",
     '\'<div class="rs-card cs-rb-\' + c + (c === \'running\' ? \' running\' : \'\') + \'" data-dep-id=',
     r"""'<div class="rs-card cs-rb-' + c + (c === 'running' ? ' running' : '') + '">'""",
     r"""'<div class="rs-card cs-rb-' + c + (c === 'running' ? ' running' : '') + '" data-dep-id="' + esc(t.deployment_id) + '" role="button" tabindex="0" title="Click for full details">'""",
     False),
    ("html", "6 cr-detail", "clickable: by-category cell",
     '\'<div class="cat-cell cs-\' + t.category + \'" data-dep-id=',
     r"""'<div class="cat-cell cs-' + t.category + '">'""",
     r"""'<div class="cat-cell cs-' + t.category + '" data-dep-id="' + esc(t.deployment_id) + '" role="button" tabindex="0" title="Click for full details">'""",
     False),
]


def flex(s):
    return r"\s+".join(re.escape(t) for t in s.split())


def read_text(path):
    raw = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    return raw.replace("\r\n", "\n"), crlf


def write_text(path, text, crlf):
    if crlf:
        text = text.replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))


def already_applied(snapshot, marker):
    return re.search(flex(marker), snapshot) is not None


def apply_patch(text, find, new, is_regex):
    pattern = re.compile(find if is_regex else flex(find), re.S)
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        return text, "FAILED (%d matches, expected 1)" % len(matches)
    m = matches[0]
    replacement = new(m) if callable(new) else new
    return text[:m.start()] + replacement + text[m.end():], "applied"


def check_requirements(snap):
    """The daily step needs the base Running Status feature."""
    problems = []
    if "def running_daily" not in snap["app"] and "def ensure_timer_history_table" not in snap["app"]:
        problems.append("app.py has no Running Status timer code (ensure_timer_history_table). "
                        "Apply the base Running Status patch first.")
    if 'id="rspPrev"' not in snap["html"] and (
            "rspList" not in snap["html"] or "runningStatusView" not in snap["html"]):
        problems.append("manager.html has no Running Status panel/popup. "
                        "Apply the base Running Status panel + popup patches first.")
    return problems


def compile_check(text):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        name = tmp.name
    try:
        py_compile.compile(name, doraise=True)
        return None
    except py_compile.PyCompileError as exc:
        return str(exc)
    finally:
        os.unlink(name)


def load_env(env_path):
    values = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def run_sql(project):
    try:
        import psycopg2
    except ImportError:
        print("  ! psycopg2 not installed - run category_migration.sql manually.")
        return
    env = {**load_env(project / ".env"), **os.environ}
    try:
        conn = psycopg2.connect(
            host=env["DB_HOST"], port=int(env.get("DB_PORT", "5432")),
            dbname=env["DB_NAME"], user=env["DB_USER"], password=env["DB_PASSWORD"],
            connect_timeout=15)
        with conn, conn.cursor() as cur:
            cur.execute(SQL_TEXT)
        conn.close()
        print("  ✓ Database: category column is present.")
    except Exception as exc:  # noqa: BLE001
        print("  ! Could not run SQL (%s). Run category_migration.sql manually." % exc)


def restore(project, targets):
    root = project / "patch_backups"
    folders = sorted(p for p in root.glob("*") if p.is_dir()) if root.exists() else []
    if not folders:
        print("No backups found in", root)
        return 1
    latest = folders[-1]
    for path in targets.values():
        src = latest / path.name
        if src.exists():
            shutil.copy2(src, path)
            print("  ✓ restored %s from %s" % (path.name, latest.name))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Running Status: wide layout + daily view + category table + rollback + tidy panel + CR detail popup.")
    ap.add_argument("--dir", default=str(Path.cwd()))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--apply-sql", action="store_true")
    ap.add_argument("files", nargs="*", help="optional: app.py, manager.html, governance_tracker.html")
    args = ap.parse_args()

    project = Path(args.dir).resolve()
    targets = {
        "app": project / "app.py",
        "html": project / "templates" / "manager.html",
        "gov": project / "templates" / "governance_tracker.html",
    }
    for given in args.files:
        path = Path(given).resolve()
        if path.suffix.lower() == ".py":
            targets["app"] = path
            project = path.parent
        elif path.suffix.lower() in (".html", ".htm"):
            targets["gov" if "governance" in path.name.lower() else "html"] = path
        else:
            print("ERROR: unsupported file (use app.py, manager.html, governance_tracker.html):", given)
            return 1
    for key in ("app", "html"):
        if not targets[key].exists():
            print("ERROR: file not found:", targets[key])
            print("Run patch.py from the folder containing app.py, or pass the file paths.")
            return 1
    if not targets["gov"].exists():
        print("NOTE: %s not found - the Rollback tab on the tracker page will be skipped.\n" % targets["gov"].name)
        del targets["gov"]

    if args.restore:
        return restore(project, targets)

    texts, crlfs = {}, {}
    for key, path in targets.items():
        texts[key], crlfs[key] = read_text(path)
    snapshot = dict(texts)

    problems = check_requirements(snapshot)
    if problems:
        print("Cannot continue:")
        for p in problems:
            print("  -", p)
        return 1

    failed, changed = False, {key: False for key in targets}
    print("Patching in %s%s\n" % (project, "  (dry run)" if args.dry_run else ""))
    for key, step, name, marker, find, new, is_regex in PATCHES:
        if key not in targets:
            continue
        if already_applied(snapshot[key], marker):
            status = "already applied"
        else:
            texts[key], status = apply_patch(texts[key], find, new, is_regex)
        icon = "✓" if status in ("applied", "already applied") else "✗"
        print("  %s step %-10s [%s] %s - %s" % (icon, step, targets[key].name, name, status))
        if status == "applied":
            changed[key] = True
        elif status.startswith("FAILED"):
            failed = True

    if failed:
        print("\nAborted: at least one patch could not be matched. No files were changed.")
        print("Send the FAILED line(s) above so the patch can be adjusted.")
        return 1

    if changed.get("app"):
        error = compile_check(texts["app"])
        if error:
            print("\nAborted: patched app.py would not compile:\n", error)
            return 1

    if args.dry_run:
        print("\nDry run finished. Nothing written.")
        return 0

    if any(changed.values()):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = project / "patch_backups" / stamp
        backup_dir.mkdir(parents=True, exist_ok=True)
        for key, path in targets.items():
            if changed[key]:
                shutil.copy2(path, backup_dir / path.name)
        print("\nBackups saved in", backup_dir)
        for key, path in targets.items():
            if changed[key]:
                write_text(path, texts[key], crlfs[key])
                print("  ✓ wrote", path.name)
    else:
        print("\nNothing to do - everything was already applied.")

    sql_file = project / "category_migration.sql"
    sql_file.write_text(SQL_TEXT, encoding="utf-8")
    print("  ✓ wrote", sql_file.name)
    if args.apply_sql:
        run_sql(project)
    else:
        print("\nNEXT (once): add the DB column - run  python patch.py --apply-sql\n"
              "  or execute category_migration.sql in PostgreSQL.")
    print("Then restart Flask and hard-refresh the Manager page (Ctrl+F5).\n"
          "To undo: python patch.py --restore")
    return 0


if __name__ == "__main__":
    sys.exit(main())