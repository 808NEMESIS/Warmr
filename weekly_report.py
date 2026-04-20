"""
weekly_report.py — Monday morning deliverability report per client.

Queries 7-day aggregates (warmup volume, reputation trend, replies, bounces,
spam rescues, top campaigns) and sends an HTML email via Resend.

Called Mondays 08:00 by launchd. Skips clients without a `reply_to_email`
or `email` in their clients row.

Idempotent: adds a `weekly_report_sent` row in notifications so reruns the
same week don't send twice.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("BRIEFING_FROM_EMAIL", "briefing@meet-aerys.nl")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def gather_client_metrics(sb: Client, client_id: str) -> dict[str, Any]:
    """Aggregate all the numbers we want in the report for one client."""
    week_ago = (_now() - timedelta(days=7)).isoformat()

    # Inboxes
    inboxes = sb.table("inboxes").select(
        "id, email, reputation_score, status, daily_sent, spam_rescues, spam_complaints"
    ).eq("client_id", client_id).execute().data or []

    inbox_ids = [i["id"] for i in inboxes]
    avg_rep = round(sum(i.get("reputation_score") or 0 for i in inboxes) / len(inboxes), 1) if inboxes else 0.0

    # Warmup activity — 7 days
    warmup_sent = warmup_replied = warmup_rescued = errors = 0
    if inbox_ids:
        logs = (
            sb.table("warmup_logs")
            .select("action")
            .in_("inbox_id", inbox_ids)
            .gte("timestamp", week_ago)
            .execute()
        ).data or []
        for log in logs:
            a = log.get("action", "")
            if a == "sent": warmup_sent += 1
            elif a == "replied": warmup_replied += 1
            elif a == "spam_rescued": warmup_rescued += 1
            elif a == "error": errors += 1

    # Bounces in the week
    bounces_hard = bounces_soft = complaints = 0
    if inbox_ids:
        bounce_rows = (
            sb.table("bounce_log")
            .select("bounce_type")
            .in_("inbox_id", inbox_ids)
            .gte("timestamp", week_ago)
            .execute()
        ).data or []
        for b in bounce_rows:
            t = b.get("bounce_type", "")
            if t == "hard": bounces_hard += 1
            elif t == "soft": bounces_soft += 1
            elif t == "spam_complaint": complaints += 1

    # Campaign activity
    campaigns = (
        sb.table("campaigns")
        .select("id, name, status")
        .eq("client_id", client_id)
        .in_("status", ["active", "paused"])
        .execute()
    ).data or []

    camp_sent = camp_opened = camp_replied = camp_bounced = 0
    for c in campaigns:
        events = (
            sb.table("email_events")
            .select("event_type")
            .eq("campaign_id", c["id"])
            .gte("created_at", week_ago)
            .execute()
        ).data or []
        for e in events:
            t = e.get("event_type", "")
            if t == "sent": camp_sent += 1
            elif t == "opened": camp_opened += 1
            elif t == "replied": camp_replied += 1
            elif t == "bounced": camp_bounced += 1

    return {
        "inboxes": inboxes,
        "avg_reputation": avg_rep,
        "warmup_sent": warmup_sent,
        "warmup_replied": warmup_replied,
        "warmup_rescued": warmup_rescued,
        "warmup_errors": errors,
        "bounces_hard": bounces_hard,
        "bounces_soft": bounces_soft,
        "complaints": complaints,
        "active_campaigns": sum(1 for c in campaigns if c["status"] == "active"),
        "campaign_sent": camp_sent,
        "campaign_opened": camp_opened,
        "campaign_replied": camp_replied,
        "campaign_bounced": camp_bounced,
    }


def build_html(client_email: str, company: str, m: dict[str, Any]) -> str:
    """Render the weekly metrics as a simple styled HTML email."""
    reply_rate = round(m["campaign_replied"] / m["campaign_sent"] * 100, 1) if m["campaign_sent"] else 0.0
    open_rate  = round(m["campaign_opened"] / m["campaign_sent"] * 100, 1) if m["campaign_sent"] else 0.0
    bounce_rate = round(m["campaign_bounced"] / m["campaign_sent"] * 100, 1) if m["campaign_sent"] else 0.0

    def row(label: str, value: Any, hint: str = "") -> str:
        return (
            f'<tr><td style="padding:8px 12px;color:#555;font-size:14px">{label}'
            + (f'<span style="color:#888;font-size:11px;display:block">{hint}</span>' if hint else '')
            + f'</td><td style="padding:8px 12px;text-align:right;font-weight:700;font-size:14px">{value}</td></tr>'
        )

    inbox_rows = "".join(
        f'<tr><td style="padding:6px 12px;font-size:13px">{i["email"]}</td>'
        f'<td style="padding:6px 12px;text-align:right;font-weight:700;color:{("#16a34a" if (i.get("reputation_score") or 0) >= 70 else "#f59e0b" if (i.get("reputation_score") or 0) >= 40 else "#dc2626")}">{i.get("reputation_score") or 0}/100</td></tr>'
        for i in m["inboxes"]
    )

    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#fafafa;padding:24px;color:#1a1a2e">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.05)">
  <tr><td style="background:linear-gradient(135deg,#a29bfe,#6c5ce7);padding:24px;color:#fff">
    <h1 style="margin:0;font-size:22px">Weekrapport Warmr</h1>
    <p style="margin:4px 0 0;opacity:.9;font-size:13px">{company} &middot; {date.today().isoformat()}</p>
  </td></tr>

  <tr><td style="padding:20px 12px 8px"><h2 style="margin:0;font-size:16px;padding-left:12px">Warmup</h2></td></tr>
  <tr><td><table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
    {row("Gem. reputatiescore", f"{m['avg_reputation']}/100")}
    {row("Emails verstuurd (warmup)", m["warmup_sent"])}
    {row("Replies ontvangen", m["warmup_replied"])}
    {row("Uit spam gered", m["warmup_rescued"])}
    {row("Fouten", m["warmup_errors"], "tijdelijk, automatisch hersteld")}
  </table></td></tr>

  <tr><td style="padding:20px 12px 8px"><h2 style="margin:0;font-size:16px;padding-left:12px">Campagnes</h2></td></tr>
  <tr><td><table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
    {row("Actieve campagnes", m["active_campaigns"])}
    {row("Emails verstuurd", m["campaign_sent"])}
    {row("Open rate", f"{open_rate}%")}
    {row("Reply rate", f"{reply_rate}%")}
    {row("Bounce rate", f"{bounce_rate}%", "limiet 3% — auto-pauze bij overschrijding")}
  </table></td></tr>

  <tr><td style="padding:20px 12px 8px"><h2 style="margin:0;font-size:16px;padding-left:12px">Deliverability</h2></td></tr>
  <tr><td><table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
    {row("Hard bounces", m["bounces_hard"])}
    {row("Soft bounces", m["bounces_soft"])}
    {row("Spam klachten", m["complaints"], "kritiek — onderzoek bij > 0")}
  </table></td></tr>

  <tr><td style="padding:20px 12px 8px"><h2 style="margin:0;font-size:16px;padding-left:12px">Inbox status</h2></td></tr>
  <tr><td><table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
    {inbox_rows or '<tr><td style="padding:12px;color:#888;font-size:13px">Geen inboxes geconfigureerd</td></tr>'}
  </table></td></tr>

  <tr><td style="padding:24px 12px;text-align:center;border-top:1px solid #eee;margin-top:20px">
    <a href="http://localhost:8000/dashboard.html" style="background:linear-gradient(135deg,#a29bfe,#6c5ce7);color:#fff;text-decoration:none;padding:10px 20px;border-radius:8px;font-weight:600;font-size:14px">Bekijk dashboard</a>
  </td></tr>
</table>
<p style="text-align:center;color:#888;font-size:11px;margin-top:16px">Warmr weekly report &middot; {client_email}</p>
</body></html>"""


def already_sent_this_week(sb: Client, client_id: str) -> bool:
    """Check if we already sent a report this week (Monday-based)."""
    week_start = (_now() - timedelta(days=_now().weekday())).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    try:
        resp = (
            sb.table("notifications")
            .select("id")
            .eq("client_id", client_id)
            .eq("type", "weekly_report_sent")
            .gte("timestamp", week_start)
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception:
        return False


def send_via_resend(to_email: str, subject: str, html: str) -> bool:
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — skipping actual send. Would have emailed %s.", to_email)
        return False
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": FROM_EMAIL, "to": to_email, "subject": subject, "html": html},
            timeout=15,
        )
        if resp.status_code >= 300:
            logger.error("Resend API returned %d: %s", resp.status_code, resp.text[:300])
            return False
        return True
    except Exception as exc:
        logger.error("Resend send failed for %s: %s", to_email, exc)
        return False


def main() -> int:
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.critical("SUPABASE_URL/KEY not set.")
        return 1

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Only send on Mondays unless FORCE=1 passed
    if _now().weekday() != 0 and os.getenv("WARMR_FORCE_WEEKLY", "0") != "1":
        logger.info("Not Monday (weekday=%d). Skipping weekly report.", _now().weekday())
        return 0

    clients = (
        sb.table("clients")
        .select("id, email, company_name, suspended")
        .eq("suspended", False)
        .execute()
    ).data or []

    # Per-client override for the recipient email lives in client_settings
    settings_map: dict[str, str] = {}
    try:
        for row in sb.table("client_settings").select("client_id, reply_to_email").execute().data or []:
            if row.get("reply_to_email"):
                settings_map[row["client_id"]] = row["reply_to_email"]
    except Exception:
        pass

    sent_count = 0
    for client in clients:
        client_id = client["id"]
        to_email = settings_map.get(client_id) or client.get("email")
        if not to_email:
            continue

        if already_sent_this_week(sb, client_id):
            logger.info("Skipping %s — already sent this week.", to_email)
            continue

        try:
            metrics = gather_client_metrics(sb, client_id)
            html = build_html(to_email, client.get("company_name") or "Klant", metrics)
            subject = f"Weekrapport Warmr — rep {metrics['avg_reputation']}/100"
            ok = send_via_resend(to_email, subject, html)

            # Log regardless, so the UI shows the summary even if email failed
            sb.table("notifications").insert({
                "client_id": client_id,
                "type": "weekly_report_sent",
                "message": f"Weekrapport: {metrics['warmup_sent']} warmup sends, rep {metrics['avg_reputation']}/100, {metrics['campaign_replied']} replies."
                           + ("" if ok else " (email verzenden mislukt — zie dashboard)"),
                "priority": "low" if ok else "medium",
            }).execute()

            if ok:
                sent_count += 1
                logger.info("Sent weekly report to %s.", to_email)
        except Exception as exc:
            logger.error("Weekly report failed for client %s: %s", client_id, exc)

    logger.info("Weekly report done. Delivered to %d client(s).", sent_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
