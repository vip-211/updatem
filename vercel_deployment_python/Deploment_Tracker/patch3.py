#!/usr/bin/env python3
"""
Patch: Manager "CR detail" popup (Release Calendar + DevOps latest + Change History)

Run from the project folder:
    python patch_cr_detail.py
or:
    python patch_cr_detail.py path/to/app.py path/to/templates/manager.html

Backups (*.bak) are created before any change. Safe to re-run (idempotent).
Restart Flask afterwards.
"""
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
APP_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "app.py"
if len(sys.argv) > 2:
    HTML_PATH = Path(sys.argv[2])
else:
    HTML_PATH = BASE / "templates" / "manager.html"
    if not HTML_PATH.exists() and (BASE / "manager.html").exists():
        HTML_PATH = BASE / "manager.html"

APP_MARKER = "# --- CR DETAIL VIEW (patch) ---"
HTML_MARKER = 'id="crDetailModal"'


def read(p):
    with open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write(p, text):
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def backup(p):
    b = Path(str(p) + ".bak")
    if not b.exists():
        shutil.copy2(p, b)
        print(f"  backup  -> {b}")


def replace_once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"[{label}] expected exactly 1 match, found {n}. Patch aborted; nothing was written.")
    return text.replace(old, new)


# =====================================================================
# 1) BACKEND  (app.py)
# =====================================================================
BACKEND = r'''# --- CR DETAIL VIEW (patch) ---
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

APP_ANCHOR = '@app.route("/api/deployment-approval/<int:deployment_id>", methods=["POST"])'

# =====================================================================
# 2) FRONTEND  (manager.html)
# =====================================================================
CSS = r"""
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

HTML = r"""
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

JS = r"""
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

# (anchor, replacement, label) - makes every CR card clickable
CARD_REPLACEMENTS = [
    (
        r"""'<div class="rsp-row cs-' + c + (c === 'running' ? ' running' : '') + '"><div class="rsp-main">'""",
        r"""'<div class="rsp-row cs-' + c + (c === 'running' ? ' running' : '') + '" data-dep-id="' + esc(t.deployment_id) + '" role="button" tabindex="0" title="Click for full details"><div class="rsp-main">'""",
        "dashboard panel row",
    ),
    (
        r"""'<div class="rs-card cs-' + c + (c === 'running' ? ' running' : '') + '">'""",
        r"""'<div class="rs-card cs-' + c + (c === 'running' ? ' running' : '') + '" data-dep-id="' + esc(t.deployment_id) + '" role="button" tabindex="0" title="Click for full details">'""",
        "full view live card",
    ),
    (
        r"""'<div class="rs-card cs-rb-' + c + (c === 'running' ? ' running' : '') + '">'""",
        r"""'<div class="rs-card cs-rb-' + c + (c === 'running' ? ' running' : '') + '" data-dep-id="' + esc(t.deployment_id) + '" role="button" tabindex="0" title="Click for full details">'""",
        "rolled back card",
    ),
    (
        r"""'<div class="cat-cell cs-' + t.category + '">'""",
        r"""'<div class="cat-cell cs-' + t.category + '" data-dep-id="' + esc(t.deployment_id) + '" role="button" tabindex="0" title="Click for full details">'""",
        "by-category cell",
    ),
]

HTML_STYLE_END = "</style>"
HTML_BOT_ANCHOR = "<!-- ===== Page Assistant (answers only from data on this page; no AI model) ===== -->"


def patch_app():
    print(f"Patching {APP_PATH}")
    text = read(APP_PATH)
    if APP_MARKER in text:
        print("  already patched - skipped")
        return
    backup(APP_PATH)
    text = replace_once(text, APP_ANCHOR, BACKEND + APP_ANCHOR, "app.py anchor")
    write(APP_PATH, text)
    print("  added  /api/deployment-detail/<id>")


def patch_html():
    print(f"Patching {HTML_PATH}")
    text = read(HTML_PATH)
    if HTML_MARKER in text:
        print("  already patched - skipped")
        return
    backup(HTML_PATH)
    text = replace_once(text, HTML_STYLE_END, CSS + HTML_STYLE_END, "css anchor")
    for old, new, label in CARD_REPLACEMENTS:
        text = replace_once(text, old, new, label)
    text = replace_once(text, HTML_BOT_ANCHOR, HTML + JS + HTML_BOT_ANCHOR, "html/js anchor")
    write(HTML_PATH, text)
    print("  added  popup CSS, modal, script and clickable cards")


def main():
    for p in (APP_PATH, HTML_PATH):
        if not p.exists():
            raise SystemExit(f"File not found: {p}")
    patch_app()
    patch_html()
    print("\nDone. Restart the Flask app, then hard-refresh the Manager page (Ctrl+F5).")


if __name__ == "__main__":
    main()