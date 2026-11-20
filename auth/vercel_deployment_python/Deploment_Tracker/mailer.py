"""mailer.py - all e-mail logic for the Deployment Tracker.

Split out of app.py.  This module never imports app.py (no circular import);
anything it needs from the application (logger, pending-status text, the
registered audit e-mails) is passed in by the thin wrappers left in app.py.

Every notification is sent as multipart/alternative:
  * text/plain  - aligned table, for clients that cannot show HTML
  * text/html   - branded, table-based layout with inline CSS (Outlook safe)
Configuration comes from .env (MAIL_SERVER, MAIL_PORT, MAIL_USERNAME,
MAIL_PASSWORD, MAIL_FROM_EMAIL, MAIL_FROM_NAME, MAIL_USE_SSL, MAIL_USE_TLS,
MAIL_TIMEOUT_SECONDS, DEPLOYMENT_EMAIL_RECIPIENTS).
"""
import html
import json
import logging
import os
import re
import smtplib
import ssl
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import formataddr

_default_log = logging.getLogger(__name__)

NOT_PROVIDED = "Not provided"
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

# Palette (matches the web UI)
C_DARK, C_BLUE = "#1e3472", "#2457d6"
C_LABEL_BG, C_ROW_ALT, C_BORDER = "#eef3ff", "#f7f9fd", "#d7e0f0"
C_TEXT, C_MUTED = "#172554", "#64748b"

STATUS_COLOURS = {  # status -> (background, text)
    "approved": ("#dcfce7", "#166534"),
    "deployment completed": ("#dcfce7", "#166534"),
    "rejected": ("#fee2e2", "#991b1b"),
    "deployment failed": ("#fee2e2", "#991b1b"),
    "cancelled": ("#fee2e2", "#991b1b"),
    "rolled": ("#fee2e2", "#991b1b"),
    "scheduled deployment": ("#fef3c7", "#92400e"),
    "on hold": ("#fef3c7", "#92400e"),
    "postponed": ("#fef3c7", "#92400e"),
}


class Raw(str):
    """Marks a value as already-safe HTML (not escaped again)."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _flag(name, default):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _display(value):
    if value is None:
        return NOT_PROVIDED
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    value = str(value).strip()
    return value or NOT_PROVIDED


def _as_list(value):
    """git_links / team_allocations may be list, JSON text or None."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _split_addresses(raw):
    return [a.strip() for a in re.split(r"[,;\n]+", raw or "") if a.strip()]


def _clock(value):
    if isinstance(value, datetime):
        return value.strftime("%I:%M %p")
    if isinstance(value, time):
        return value.strftime("%I:%M %p")
    if value is None:
        return NOT_PROVIDED
    raw = str(value)
    for parser in (datetime.fromisoformat, time.fromisoformat):
        try:
            return parser(raw).strftime("%I:%M %p")
        except ValueError:
            continue
    return raw


def _date_text(value):
    if isinstance(value, (datetime, date)):
        return value.strftime("%d-%m-%Y")
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d-%m-%Y")
    except (TypeError, ValueError):
        return _display(value)


def _smtp_config():
    cfg = {
        "host": (os.getenv("MAIL_SERVER") or "").strip(),
        "username": (os.getenv("MAIL_USERNAME") or "").strip(),
        "password": os.getenv("MAIL_PASSWORD", ""),
        "port": int(os.getenv("MAIL_PORT", "587")),
        "use_ssl": _flag("MAIL_USE_SSL", "false"),
        "use_tls": _flag("MAIL_USE_TLS", "true"),
        "timeout": int(os.getenv("MAIL_TIMEOUT_SECONDS", "20")),
        "from_name": (os.getenv("MAIL_FROM_NAME") or "Deployment Tracker").strip(),
    }
    cfg["from_email"] = (os.getenv("MAIL_FROM_EMAIL") or cfg["username"]).strip()
    return cfg if cfg["host"] and cfg["from_email"] else None


def _deliver(message, recipients, cfg):
    ctx = ssl.create_default_context()
    if cfg["use_ssl"]:
        smtp_cm = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=cfg["timeout"], context=ctx)
    else:
        smtp_cm = smtplib.SMTP(cfg["host"], cfg["port"], timeout=cfg["timeout"])
    with smtp_cm as smtp:
        if not cfg["use_ssl"]:
            smtp.ehlo()
            if cfg["use_tls"]:
                smtp.starttls(context=ctx)
                smtp.ehlo()
        if cfg["username"] and cfg["password"]:
            smtp.login(cfg["username"], cfg["password"])
        smtp.send_message(message, to_addrs=recipients)


# --------------------------------------------------------------------------
# Table rendering
#   rows = list of:
#     ("section", "Heading text")
#     ("row", "Label", value)          value: str, or Raw(html)
# --------------------------------------------------------------------------
def status_badge(status):
    bg, fg = STATUS_COLOURS.get(str(status).strip().lower(), ("#e0e7ff", "#1e3a8a"))
    return Raw(
        f'<span style="display:inline-block;padding:4px 12px;border-radius:999px;'
        f'background:{bg};color:{fg};font-weight:bold;font-size:12px;">'
        f"{html.escape(str(status))}</span>"
    )


def links_html(links):
    links = [str(l).strip() for l in links if str(l).strip()]
    if not links:
        return NOT_PROVIDED
    return Raw("<br>".join(
        f'<a href="{html.escape(l, quote=True)}" style="color:{C_BLUE};'
        f'text-decoration:none;">{html.escape(l)}</a>' for l in links))


def allocations_html(allocs):
    items = [a for a in allocs if isinstance(a, dict) and a.get("team")]
    if not items:
        return NOT_PROVIDED
    body = "".join(
        f'<tr><td style="padding:4px 10px;border:1px solid {C_BORDER};">{html.escape(str(a["team"]))}</td>'
        f'<td align="center" style="padding:4px 10px;border:1px solid {C_BORDER};">'
        f'{html.escape(str(a.get("count", "")))}</td></tr>' for a in items)
    head = (f'<tr style="background:{C_LABEL_BG};"><th align="left" style="padding:4px 10px;'
            f'border:1px solid {C_BORDER};font-size:12px;">Team</th><th style="padding:4px 10px;'
            f'border:1px solid {C_BORDER};font-size:12px;">Deployments</th></tr>')
    return Raw(f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;'
               f'font-size:13px;">{head}{body}</table>')


def render_html(title, intro, rows, footer):
    out = []
    shade = False
    for item in rows:
        if item[0] == "section":
            shade = False
            out.append(
                f'<tr><td colspan="2" style="padding:10px 16px;background:{C_DARK};'
                f'background-image:linear-gradient(100deg,{C_DARK},{C_BLUE});color:#ffffff;'
                f'font-size:13px;font-weight:bold;letter-spacing:.6px;text-transform:uppercase;">'
                f"{html.escape(item[1])}</td></tr>")
            continue
        _, label, value = item
        cell = str(value) if isinstance(value, Raw) else html.escape(str(value)).replace("\n", "<br>")
        bg = C_ROW_ALT if shade else "#ffffff"
        shade = not shade
        out.append(
            f'<tr style="background:{bg};">'
            f'<td width="34%" valign="top" style="padding:10px 16px;background:{C_LABEL_BG};'
            f"border-bottom:1px solid {C_BORDER};border-right:1px solid {C_BORDER};"
            f'color:{C_DARK};font-weight:bold;font-size:13px;">{html.escape(label)}</td>'
            f'<td valign="top" style="padding:10px 16px;border-bottom:1px solid {C_BORDER};'
            f'color:{C_TEXT};font-size:13px;word-break:break-word;">{cell}</td></tr>')
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f1f5fb;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5fb;padding:24px 0;">
<tr><td align="center">
  <table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;background:#ffffff;
         border:1px solid {C_BORDER};border-radius:10px;overflow:hidden;">
    <tr><td style="padding:22px 24px;background:{C_DARK};background-image:linear-gradient(105deg,#30365f,#5b526f 58%,#876678);color:#ffffff;">
      <div style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;opacity:.8;">Deployment Tracker</div>
      <div style="font-size:21px;font-weight:bold;margin-top:4px;">{html.escape(title)}</div>
    </td></tr>
    <tr><td style="padding:18px 24px 6px;color:{C_TEXT};font-size:14px;line-height:1.5;">{intro}</td></tr>
    <tr><td style="padding:10px 24px 22px;">
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:separate;border:1px solid {C_BORDER};
             border-radius:8px;overflow:hidden;">{''.join(out)}</table>
    </td></tr>
    <tr><td style="padding:14px 24px;background:#f8fafc;border-top:1px solid {C_BORDER};color:{C_MUTED};font-size:11px;text-align:center;">
      {html.escape(footer)}
    </td></tr>
  </table>
</td></tr></table></body></html>"""


def render_text(title, rows, footer):
    width = max((len(r[1]) for r in rows if r[0] == "row"), default=10)
    lines = [title, "=" * len(title), ""]
    for item in rows:
        if item[0] == "section":
            lines += ["", item[1].upper(), "-" * len(item[1])]
        else:
            value = re.sub(r"<br\s*/?>", "\n", str(item[2]))
            if isinstance(item[2], Raw):
                value = html.unescape(re.sub(r"<[^>]+>", " ", value))
                value = " ".join(value.split())
            first, *rest = str(value).splitlines() or [""]
            lines.append(f"{item[1].ljust(width)} : {first}")
            lines += [f"{' ' * width}   {r}" for r in rest]
    return "\n".join(lines + ["", footer])


def _build_message(subject, cfg, to_header, title, intro_html, rows, footer):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["from_name"], cfg["from_email"]))
    msg["To"] = to_header
    msg.set_content(render_text(title, rows, footer))
    msg.add_alternative(render_html(title, intro_html, rows, footer), subtype="html")
    return msg


FOOTER = "This is an automated notification from the Deployment Tracker. Please do not reply."


# --------------------------------------------------------------------------
# 1) "Save Deployment" e-mail (Release Calendar form)
# --------------------------------------------------------------------------
def send_deployment_notification(data, rfid, pending_status="Scheduled Deployment", logger=None):
    """Returns (sent, reason). Mail failure never rolls back a saved deployment."""
    log = logger or _default_log
    requestor_email = (data.get("change_requestor") or "").strip()
    application_name = (data.get("application_name") or "").strip() or NOT_PROVIDED

    recipients = _split_addresses(os.getenv("DEPLOYMENT_EMAIL_RECIPIENTS", ""))
    if requestor_email and EMAIL_RE.fullmatch(requestor_email):
        recipients.append(requestor_email)
    recipients = sorted(set(recipients))
    if not recipients:
        log.warning("Deployment saved, but email was not sent: DEPLOYMENT_EMAIL_RECIPIENTS is not configured.")
        return False, "Email recipients are not configured in .env."

    cfg = _smtp_config()
    if cfg is None:
        log.warning("Deployment saved, but email was not sent: SMTP configuration is incomplete.")
        return False, "SMTP settings are incomplete in .env."

    try:
        cr_suffix = re.sub(r"^CR-", "", (data.get("cr_number") or "").strip(), flags=re.IGNORECASE).strip()
        cr_number = f"CR-{cr_suffix}" if cr_suffix else ""
        requestor = requestor_email or "Unknown user"
        deployment_date = datetime.strptime(data["deployment_date"], "%Y-%m-%d").date()
        slot_start = datetime.combine(deployment_date, datetime.strptime(data["slot_start"], "%H:%M").time())
        slot_end = slot_start + timedelta(minutes=30)

        rows = [
            ("section", "Request"),
            ("row", "CR Number", cr_number or NOT_PROVIDED),
            ("row", "RFID", _display(rfid)),
            ("row", "Scheduled By", requestor),
            ("row", "Status", status_badge(pending_status)),
            ("section", "Schedule"),
            ("row", "Deployment Date", deployment_date.strftime("%d-%m-%Y (%A)")),
            ("row", "Slot Timing", f"{slot_start:%I:%M %p} - {slot_end:%I:%M %p}"),
            ("section", "Application & Change"),
            ("row", "Application Name", _display(data.get("application_name"))),
            ("row", "Category", _display(data.get("category"))),
            ("row", "Impacted Application", _display(data.get("impacted_application"))),
            ("row", "App Id", _display(data.get("business_unit"))),
            ("row", "oneQ Reference", _display(data.get("environment"))),
            ("row", "Change Type", _display(data.get("change_type"))),
            ("row", "Deployment Type", _display(data.get("deployment_type"))),
            ("row", "Deployment Count", _display(data.get("deployment_count"))),
            ("row", "Incident Number", _display(data.get("incident_number"))),
            ("section", "Source Control"),
            ("row", "Git Links", links_html(_as_list(data.get("git_links")))),
        ]
        intro = (f"<strong>{html.escape(requestor)}</strong> scheduled a deployment for "
                 f"<strong>{html.escape(application_name)}</strong>. Details are below.")
        subject = f"{application_name}-{requestor} scheduled the deployment - {cr_number}"
        msg = _build_message(subject, cfg, ", ".join(recipients),
                             "Deployment Scheduled", intro, rows, FOOTER)
        _deliver(msg, recipients, cfg)
        log.info("Deployment notification sent to %d recipient(s) for CR %s, RFID %s",
                 len(recipients), cr_number, rfid)
        return True, None
    except Exception:
        log.exception("Deployment saved, but notification email failed for CR %s", data.get("cr_number", ""))
        return False, ("The deployment was saved, but the notification email could not be sent. "
                       "Check the application logs and SMTP settings.")


# --------------------------------------------------------------------------
# 2) "Submit" e-mail (DevOps Governance page + approve/reject)
# --------------------------------------------------------------------------
def send_status_change_notification(record, old_status, new_status,
                                    registered_recipients=None, logger=None):
    """Notify configured + registered recipients of a status change.

    Modified-By e-mail / passcode are intentionally never included.
    Recipients are sent as BCC-style (To: Undisclosed recipients).
    """
    log = logger or _default_log
    configured = [a.lower() for a in _split_addresses(os.getenv("DEPLOYMENT_EMAIL_RECIPIENTS", ""))]
    registered = [str(a).strip().lower() for a in (registered_recipients or []) if a and str(a).strip()]
    recipients = sorted(set(configured + registered))
    if not recipients:
        log.warning("Status changed to %s, but no email recipients are configured", new_status)
        return False, "No email recipients are configured."

    cfg = _smtp_config()
    if cfg is None:
        log.warning("Status notification email skipped because SMTP settings are incomplete")
        return False, "SMTP settings are incomplete."

    cr_number = _display(record.get("cr_number"))
    try:
        changed_at = datetime.now().astimezone().strftime("%d-%m-%Y %I:%M:%S %p %Z")
        application_name = _display(record.get("application_name"))
        git_links = [str(l).strip() for l in _as_list(record.get("git_links")) if str(l).strip()]

        rows = [
            ("section", "Status Change"),
            ("row", "Previous Status", status_badge(old_status)),
            ("row", "New Status", status_badge(new_status)),
            ("row", "Changed At", changed_at),
            ("section", "Request"),
            ("row", "CR Number", cr_number),
            ("row", "RFID", _display(record.get("rfid"))),
            ("row", "Change Requestor", _display(record.get("change_requestor"))),
            ("row", "Incident Number", _display(record.get("incident_number"))),
            ("section", "Schedule"),
            ("row", "Deployment Date", _date_text(record.get("deployment_date"))),
            ("row", "Slot Timing", f"{_clock(record.get('slot_start'))} - {_clock(record.get('slot_end'))}"),
            ("section", "Application & Change"),
            ("row", "Application Name", application_name),
            ("row", "Change Type", _display(record.get("change_type"))),
            ("row", "Deployment Type", _display(record.get("deployment_type"))),
        ]
        if record.get("deployment_count") is not None:
            rows.append(("row", "Deployment Count", _display(record.get("deployment_count"))))
        if "team_allocations" in record:
            rows.append(("row", "Team Allocation", allocations_html(_as_list(record.get("team_allocations")))))
        rows += [
            ("row", "Roll Back", _display(record.get("roll_back"))),
            ("section", "Ownership"),
            ("row", "Released By", _display(record.get("released_by"))),
            ("row", "Verified and Deployed By", _display(record.get("verified_and_deployed_by"))),
            ("section", "Source Control & Notes"),
            ("row", "Git Repository Links", links_html(git_links)),
            ("row", "Comments", _display(record.get("comments"))),
        ]
        intro = (f"The status of <strong>{html.escape(cr_number)}</strong> "
                 f"(<strong>{html.escape(application_name)}</strong>) changed from "
                 f"<strong>{html.escape(str(old_status))}</strong> to "
                 f"<strong>{html.escape(str(new_status))}</strong>.")
        subject = f"Deployment Status Update: {application_name} - {cr_number} - {new_status}"
        msg = _build_message(subject, cfg, "Undisclosed recipients:;",
                             "Deployment Status Update", intro, rows, FOOTER)
        _deliver(msg, recipients, cfg)
        log.info("Status notification sent for %s: %s -> %s to %d recipient(s)",
                 cr_number, old_status, new_status, len(recipients))
        return True, None
    except Exception:
        log.exception("Deployment status saved, but notification email failed for CR %s", cr_number)
        return False, "Email delivery failed."
