#!/usr/bin/env python3
"""
Running Status becomes a DAILY view with colour-coded states and previous/next-day arrows.

Colours
  Running ........................ amber
  Completed ...................... green
  Planned (not started) .......... grey
  Forward Fix CR ................. blue   (a blue FORWARD FIX tag also shows on any forward-fix card)
  Failed / Rolled back ........... red
Priority when a deployment fits several: red > green > amber > blue > grey.

Both the dashboard panel and the "Full view" popup show ONE day at a time (default today) with
  <  Today  [date]  >  controls; the two stay in sync. Click a coloured counter to filter by colour.

Usage:  python patch_running_daily.py  app.py  templates\\manager.html
Needs the earlier Running Status + panel + view patches. Backups: *.daily.bak
"""
import re, sys, shutil

# ----------------------------------------------------------------------------- app.py
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

# ----------------------------------------------------------------------------- manager.html
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

CSS = r'''
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


def sub_once(text, pattern, make, label, flags=0):
    ms = list(re.finditer(pattern, text, flags))
    if len(ms) != 1:
        sys.exit(f"ERROR: '{label}' matched {len(ms)} times (expected 1). Nothing was written.")
    m = ms[0]
    return text[:m.start()] + make(m) + text[m.end():]


def patch_app(src):
    if "def running_daily" in src:
        sys.exit("app.py already patched.")
    if "def ensure_timer_history_table" not in src:
        sys.exit("app.py is missing the earlier Running Status patch (apply_running_status.py).")
    return sub_once(src, r'@app\.route\("/api/deployment-by-id/<int:deployment_id>", methods=\["PUT"\]\)',
                    lambda m: APP_ROUTE + m.group(0), "deployment-by-id route")


def patch_html(src):
    if "rspPrev" in src:
        sys.exit("manager.html already patched.")
    if "rspList" not in src or "runningStatusView" not in src:
        sys.exit("manager.html needs the earlier patches first: patch_running_view.py and patch_running_panel.py.")
    src = sub_once(src, r'<div class="panel rsp-panel">[\s\S]*?id="rspList">[\s\S]*?Loading running status…</div></div>\s*</div>',
                   lambda m: PANEL, "Running Status panel")
    src = sub_once(src, r"<script>\s*\(function runningStatusPanel\(\) \{[\s\S]*?</script>\s*", lambda m: PANEL_JS, "panel script")
    src = sub_once(src, r"<!-- ===== Running Status view[^\n]*-->[\s\S]*?(?=</main>)", lambda m: MODAL, "Running Status popup")
    src = sub_once(src, r"<script>\s*\(function runningStatusView\(\) \{[\s\S]*?</script>\s*", lambda m: MODAL_JS, "popup script")
    src = sub_once(src, r"</style>", lambda m: CSS + "</style>", "</style>")
    return src


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    app_path, html_path = sys.argv[1], sys.argv[2]
    app_src = open(app_path, encoding="utf-8").read()
    html_src = open(html_path, encoding="utf-8").read()
    new_app, new_html = patch_app(app_src), patch_html(html_src)
    compile(new_app, app_path, "exec")
    for p in (app_path, html_path):
        shutil.copy2(p, p + ".daily.bak")
    open(app_path, "w", encoding="utf-8").write(new_app)
    open(html_path, "w", encoding="utf-8").write(new_html)
    print("Patched OK. Backups: app.py.daily.bak, manager.html.daily.bak")


if __name__ == "__main__":
    main()