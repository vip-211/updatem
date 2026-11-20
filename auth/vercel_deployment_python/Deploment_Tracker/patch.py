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

USAGE (run from the folder that contains app.py):
    python patch.py                                  apply everything
    python patch.py app.py templates\\manager.html    same, with explicit paths
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

# ===========================================================================
# STEP 1 - WIDE LAYOUT (CSS only; its JS tweaks are superseded by step 2)
# ===========================================================================
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

# ===========================================================================
# STEP 2 - DAILY VIEW
# ===========================================================================
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
        items.sort(key=lambda i: 0 if i["category"] == "running" else 1)   # stable: slot order kept

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

# ===========================================================================
# STEP 3 - CATEGORY TABLE
# ===========================================================================
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

# ===========================================================================
# PATCH TABLE: (file, step, name, marker, find, new, is_regex)
#   marker  : if found in the ORIGINAL file, the patch is already applied
#   find    : literal text (whitespace-flexible) or raw regex
#   new     : replacement text, or a function(match) -> text
# Order matters: wide -> daily -> category.
# ===========================================================================
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


# ===========================================================================
# Engine
# ===========================================================================
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
    ap = argparse.ArgumentParser(description="Running Status: wide layout + daily view + category table.")
    ap.add_argument("--dir", default=str(Path.cwd()))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--apply-sql", action="store_true")
    ap.add_argument("files", nargs="*", help="optional: app.py and templates\\manager.html")
    args = ap.parse_args()

    project = Path(args.dir).resolve()
    targets = {"app": project / "app.py", "html": project / "templates" / "manager.html"}
    for given in args.files:
        path = Path(given).resolve()
        if path.suffix.lower() == ".py":
            targets["app"] = path
            project = path.parent
        elif path.suffix.lower() in (".html", ".htm"):
            targets["html"] = path
        else:
            print("ERROR: unsupported file (use app.py and manager.html):", given)
            return 1
    for path in targets.values():
        if not path.exists():
            print("ERROR: file not found:", path)
            print("Run patch.py from the folder containing app.py, or pass the file paths.")
            return 1

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

    failed, changed = False, {"app": False, "html": False}
    print("Patching in %s%s\n" % (project, "  (dry run)" if args.dry_run else ""))
    for key, step, name, marker, find, new, is_regex in PATCHES:
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

    if changed["app"]:
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
    print("Then restart Flask and refresh the Manager page.\n"
          "To undo: python patch.py --restore")
    return 0


if __name__ == "__main__":
    sys.exit(main())
