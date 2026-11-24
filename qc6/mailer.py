"""SMTP settings and the crawl summary e-mail (with the Management report attached).

* Settings live in Postgres per workspace (mail_settings). The password is sealed with QC6_SECRET_KEY (AES-256-GCM)
  and never leaves the server: the API only says whether one is set.
* After a completed check: summary numbers straight from the check's crawl database, the Management report built and
  verified (exists, non-empty, a complete .xlsx zip) before it is attached; too big -> sent without it, and the
  e-mail says so. Two tries on a temporary SMTP error. The outcome is written to the check's timeline.
* Sending never fails the check.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import smtplib
import sqlite3
import ssl
import zipfile
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any, Dict, List, Optional, Tuple

from qc6.settings import settings

log = logging.getLogger("qc6.mailer")

MAX_RECIPIENTS = 20
MAX_ATTACH_BYTES = int(os.environ.get("QC6_MAIL_MAX_ATTACH_MB", "20")) * 1024 * 1024
SMTP_TIMEOUT = 30
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})*$")
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class MailError(ValueError):
    pass


# ------------------------------------------------------------------ settings
def split_recipients(value: Any) -> List[str]:
    items = value if isinstance(value, list) else re.split(r"[,;\s]+", str(value or ""))
    return [x.strip() for x in items if x and x.strip()]


def validate(body: Dict[str, Any], have_password: bool) -> Dict[str, Any]:
    """Clean and check what the settings form sent. Raises MailError with a plain-language message."""
    def text(k: str, n: int = 320) -> str:
        v = str(body.get(k) or "").strip()
        if "\r" in v or "\n" in v:
            raise MailError(f"{k.replace('_', ' ').capitalize()} can't contain line breaks.")
        if len(v) > n:
            raise MailError(f"{k.replace('_', ' ').capitalize()} is too long.")
        return v
    enabled = bool(body.get("enabled"))
    host = text("host", 253)
    if host and not HOST_RE.match(host):
        raise MailError("SMTP host should be a name like smtp.example.com or an IP address.")
    try:
        port = int(body.get("port") or 587)
    except (TypeError, ValueError):
        raise MailError("SMTP port should be a number, usually 587 (STARTTLS), 465 (SSL) or 25.")
    if not 1 <= port <= 65535:
        raise MailError("SMTP port should be between 1 and 65535.")
    security = str(body.get("security") or "starttls")
    if security not in ("starttls", "ssl", "none"):
        raise MailError("Security should be STARTTLS, SSL/TLS or None.")
    username = text("username")
    sender = text("sender")
    if sender and not EMAIL_RE.match(sender):
        raise MailError("Sender should be one e-mail address, like qc@example.com.")
    recipients = split_recipients(body.get("recipients"))
    for r in recipients:
        if not EMAIL_RE.match(r) or len(r) > 254:
            raise MailError(f"'{r[:60]}' isn't an e-mail address.")
    if len(recipients) > MAX_RECIPIENTS:
        raise MailError(f"At most {MAX_RECIPIENTS} recipients.")
    recipients = list(dict.fromkeys(recipients))
    pw = body.get("password")
    if pw is not None and ("\r" in str(pw) or "\n" in str(pw) or len(str(pw)) > 512):
        raise MailError("The password can't contain line breaks.")
    if enabled:
        missing = [n for n, v in (("SMTP host", host), ("sender", sender), ("at least one recipient", recipients)) if not v]
        if missing:
            raise MailError("To turn e-mail on, fill in " + ", ".join(missing) + ".")
        if username and not (have_password or pw):
            raise MailError("Enter the SMTP password for that username.")
    return {"enabled": enabled, "host": host, "port": port, "security": security, "username": username,
            "sender": sender, "recipients": recipients, "attach_report": bool(body.get("attach_report", True)), "password": pw}


def public(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """What the browser may see: everything except the password (only whether one is saved)."""
    r = dict(row or {})
    return {"enabled": bool(r.get("enabled")), "host": r.get("host") or "", "port": int(r.get("port") or 587),
            "security": r.get("security") or "starttls", "username": r.get("username") or "",
            "password_set": bool(r.get("password_sealed")), "sender": r.get("sender") or "",
            "recipients": list(r.get("recipients") or []), "attach_report": r.get("attach_report", True) is not False,
            "last_result": r.get("last_result") or {}, "updated_at": r.get("updated_at"),
            "secret_key_set": bool(os.environ.get("QC6_SECRET_KEY")),
            "max_attach_mb": MAX_ATTACH_BYTES // (1024 * 1024)}


async def load(pool, workspace_id: int) -> Optional[Dict[str, Any]]:
    r = await pool.fetchrow("SELECT * FROM mail_settings WHERE workspace_id = $1", workspace_id)
    return dict(r) if r else None


async def save(pool, workspace_id: int, member_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    cur = await load(pool, workspace_id)
    clean = validate(body, bool(cur and cur.get("password_sealed")))
    pw = clean.pop("password")
    if pw is None:                                       # not sent: keep the saved one
        sealed = (cur or {}).get("password_sealed")
    elif pw == "":                                       # sent empty: remove it
        sealed = None
    else:
        from qc6.site_access import AccessError, seal
        try:
            sealed = seal({"p": pw})
        except AccessError as e:
            raise MailError(str(e).replace("staging logins", "the SMTP password"))
    await pool.execute("""
        INSERT INTO mail_settings (workspace_id, enabled, host, port, security, username, password_sealed, sender, recipients,
                                   attach_report, updated_by, updated_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11, now())
        ON CONFLICT (workspace_id) DO UPDATE SET enabled = $2, host = $3, port = $4, security = $5, username = $6,
            password_sealed = $7, sender = $8, recipients = $9, attach_report = $10, updated_by = $11, updated_at = now()""",
        workspace_id, clean["enabled"], clean["host"], clean["port"], clean["security"], clean["username"], sealed,
        clean["sender"], clean["recipients"], clean["attach_report"], member_id)
    return public(await load(pool, workspace_id))


def _password(cfg: Dict[str, Any]) -> str:
    if cfg.get("password") is not None:                  # unsaved, from the test form
        return str(cfg["password"])
    if not cfg.get("password_sealed"):
        return ""
    from qc6.site_access import AccessError, unseal
    try:
        return unseal(cfg["password_sealed"])["p"]
    except AccessError:
        raise MailError("The saved SMTP password can't be read: QC6_SECRET_KEY changed since it was saved. Enter it again.")


# ------------------------------------------------------------------ sending
def _friendly(e: BaseException, cfg: Dict[str, Any]) -> str:
    where = f"{cfg.get('host')}:{cfg.get('port')}"
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "The SMTP server refused the username or password."
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "The SMTP server refused the recipient address(es): " + ", ".join(list(e.recipients)[:5])
    if isinstance(e, smtplib.SMTPSenderRefused):
        return f"The SMTP server refused the sender address {cfg.get('sender')}."
    if isinstance(e, smtplib.SMTPNotSupportedError) or "STARTTLS" in str(e):
        return f"{where} doesn't offer STARTTLS. Try Security: SSL/TLS (port 465) or None."
    if isinstance(e, ssl.SSLError):
        return f"TLS with {where} failed ({e.__class__.__name__}). Check the port and the Security setting."
    if isinstance(e, TimeoutError):
        return f"{where} didn't answer within {SMTP_TIMEOUT} s."
    if isinstance(e, ConnectionRefusedError):
        return f"Nothing is listening at {where} (connection refused)."
    if isinstance(e, OSError) and getattr(e, "errno", None) in (-2, -3, -5, 11001):
        return f"The SMTP host {cfg.get('host')} wasn't found (DNS)."
    if isinstance(e, smtplib.SMTPResponseException):
        return f"The SMTP server answered {e.smtp_code}: {e.smtp_error.decode(errors='replace')[:200]}"
    return f"{e.__class__.__name__}: {str(e)[:200]}"


def _temporary(e: BaseException) -> bool:
    if isinstance(e, smtplib.SMTPResponseException):
        return 400 <= e.smtp_code < 500
    return isinstance(e, (TimeoutError, ConnectionResetError, smtplib.SMTPServerDisconnected))


def send_now(cfg: Dict[str, Any], to: List[str], subject: str, text: str, html_body: Optional[str] = None,
             attachments: Optional[List[Tuple[str, str]]] = None) -> None:
    """Blocking send (call in a thread). attachments: (path, file name). Raises MailError with a plain message."""
    msg = EmailMessage()
    msg["From"] = formataddr(("QC", cfg["sender"]))
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject.replace("\r", " ").replace("\n", " ")[:250]
    msg["Message-ID"] = make_msgid(domain=cfg["sender"].split("@")[-1])
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for path, name in attachments or []:
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype="application",
                               subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=name)
    password = _password(cfg)
    last: Optional[BaseException] = None
    for attempt in range(2):
        try:
            ctx = ssl.create_default_context()
            if cfg["security"] == "ssl":
                s = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT, context=ctx)
            else:
                s = smtplib.SMTP(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT)
            with s:
                s.ehlo()
                if cfg["security"] == "starttls":
                    s.starttls(context=ctx)
                    s.ehlo()
                if cfg.get("username"):
                    s.login(cfg["username"], password)
                s.send_message(msg)
            return
        except Exception as e:                              # noqa: BLE001 - reported in plain words
            last = e
            if attempt == 0 and _temporary(e):
                import time
                time.sleep(3)
                continue
            break
    raise MailError(_friendly(last, cfg)) from None


# ------------------------------------------------------------------ the summary
CATEGORY_SQL = """CASE
  WHEN c.status_bucket = 'Timeout' THEN 'timeout'
  WHEN c.status_bucket = 'DNS/Network' THEN 'dns_network'
  WHEN c.status_bucket = 'SSL' THEN 'ssl'
  WHEN c.status_bucket IN ('Robots blocked', 'WAF blocked') THEN 'blocked'
  WHEN c.http_status BETWEEN 500 AND 599 THEN 's5xx'
  WHEN c.http_status BETWEEN 400 AND 499 THEN 's4xx'
  WHEN c.http_status BETWEEN 300 AND 399 OR (COALESCE(c.redirect_count, 0) > 0 AND c.http_status BETWEEN 200 AND 299) THEN 's3xx'
  WHEN c.http_status BETWEEN 200 AND 299 THEN 's2xx'
  WHEN c.status_bucket = 'PENDING' THEN 'not_checked'
  ELSE 'other' END"""


def crawl_numbers(run_id: int) -> Optional[Dict[str, Any]]:
    """Counts for the summary, read-only from the check's own crawl database."""
    path = os.path.join(settings.data_dir, "runs", str(int(run_id)), "coverage.db")
    if not os.path.exists(path):
        return None
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    job = f"RUN{run_id}"
    one = lambda sql: c.execute(sql, (job,)).fetchone()                                    # noqa: E731
    try:
        p = one("""SELECT COUNT(*), SUM(crawl_state != 'PENDING'),
                          SUM(crawl_state != 'PENDING' AND http_status BETWEEN 200 AND 299 AND COALESCE(redirect_count, 0) = 0),
                          SUM(crawl_state != 'PENDING' AND (http_status BETWEEN 300 AND 399 OR (COALESCE(redirect_count, 0) > 0
                              AND http_status BETWEEN 200 AND 299))),
                          SUM(crawl_state != 'PENDING' AND http_status BETWEEN 400 AND 499),
                          SUM(crawl_state != 'PENDING' AND http_status BETWEEN 500 AND 599),
                          SUM(crawl_state != 'PENDING' AND http_status IS NULL)
                     FROM on_page_source_pages WHERE job_id = ?""")
        cats = dict(c.execute(f"SELECT {CATEGORY_SQL} AS k, COUNT(*) FROM on_page_validation_cache c WHERE c.job_id = ? GROUP BY k",
                              (job,)).fetchall())
        rels = one("SELECT COUNT(*), COALESCE(SUM(occurrence_count), 0) FROM on_page_link_relationships WHERE job_id = ?")
        imgs = one("SELECT COUNT(*) FROM on_page_image_relationships WHERE job_id = ?")[0]
        uimg = one("SELECT COUNT(*) FROM on_page_image_validation_cache WHERE job_id = ?")[0]
        rc = one("""SELECT SUM(remark LIKE '%worked on a second check%'), SUM(remark LIKE '%confirmed on a second check%')
                      FROM on_page_validation_cache WHERE job_id = ?""")
        recheck = {"recheck_recovered": int(rc[0] or 0), "recheck_confirmed": int(rc[1] or 0)}
    finally:
        c.close()
    total, crawled, ok, redir, c4, c5, noans = [int(x or 0) for x in p]
    unique = sum(cats.values())
    return {
        "pages": {"submitted": total, "crawled": crawled, "ok": ok, "redirect": redir, "s4xx": c4, "s5xx": c5,
                  "no_answer": noans, "processed": crawled - noans, "failed": c4 + c5 + noans},
        "links": {"extracted": int(rels[0] or 0), "occurrences": int(rels[1] or 0), "unique": unique,
                  "duplicates_skipped": max(0, int(rels[0] or 0) - unique),
                  **{k: int(cats.get(k, 0)) for k in ("s2xx", "s3xx", "s4xx", "s5xx", "timeout", "dns_network", "ssl", "blocked",
                                                       "other", "not_checked")}},
        "images": {"extracted": int(imgs or 0), "unique": int(uimg or 0)},
        "recheck": recheck,
    }


def _fmt(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    s = str(abs(n))                                  # Indian grouping: 1,25,000
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        s = f"{head},{tail}"
    return ("-" if n < 0 else "") + s


def _dur(a: Optional[datetime], b: Optional[datetime]) -> str:
    if not a or not b:
        return "-"
    sec = int((b - a).total_seconds())
    h, m = divmod(sec // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min {sec % 60} s"


def compose(run: Dict[str, Any], site: Dict[str, Any], source: str, nums: Optional[Dict[str, Any]],
            report: Dict[str, Any], tickets: Dict[str, Any]) -> Tuple[str, str, str]:
    """Subject, plain text and HTML of the summary e-mail."""
    host = site.get("primary_host") or site.get("name") or ""
    ok_run = run.get("status") == "completed"
    problems: List[str] = []
    if not ok_run:
        problems.append(f"The check ended as {run.get('status')}: {run.get('error') or ''}".strip())
    if nums:
        pg, ln = nums["pages"], nums["links"]
        if pg["submitted"] and pg["crawled"] < pg["submitted"]:
            problems.append(f"{_fmt(pg['submitted'] - pg['crawled'])} pages weren't crawled")
        net = ln["timeout"] + ln["dns_network"] + ln["ssl"]
        if ln["unique"] and net / ln["unique"] > 0.05:
            problems.append(f"{_fmt(net)} links failed for a network reason (over 5%)")
    if report.get("status") != "attached" and report.get("status") != "off":
        problems.append(f"Report: {report.get('note')}")
    verdict = "Completed successfully" if not problems else "Completed with problems" if ok_run else "Failed"
    broken = (nums["links"]["s4xx"] + nums["links"]["s5xx"]) if nums else 0
    subject = f"[QC] {host} - check {run['id']}: {verdict.lower()}" + (f", {_fmt(broken)} broken links" if nums else "")

    rows: List[Tuple[str, List[Tuple[str, Any]]]] = [("Check", [
        ("Crawl job ID", f"Check {run['id']} (job RUN{run['id']})"), ("Sitemap / source", source or site.get("base_url", "")),
        ("Status", verdict), ("Started", run["started_at"].strftime("%d %b %Y %H:%M:%S %Z") if run.get("started_at") else "-"),
        ("Finished", run["finished_at"].strftime("%d %b %Y %H:%M:%S %Z") if run.get("finished_at") else "-"),
        ("Total processing time", _dur(run.get("started_at"), run.get("finished_at")))])]
    if nums:
        pg, ln, im = nums["pages"], nums["links"], nums["images"]
        rows.append(("Pages (URLs submitted)", [
            ("Total URLs submitted", _fmt(pg["submitted"])),
            ("Duplicate URLs removed from the input", _fmt(pg.get("input_duplicates", 0))), ("URLs crawled", _fmt(pg["crawled"])),
            ("Successfully processed", _fmt(pg["processed"])), ("Failed (4xx / 5xx / no answer)", _fmt(pg["failed"])),
            ("200 OK", _fmt(pg["ok"])), ("3xx redirect", _fmt(pg["redirect"])), ("4xx", _fmt(pg["s4xx"])), ("5xx", _fmt(pg["s5xx"])),
            ("No answer (DNS / network / timeout)", _fmt(pg["no_answer"]))]))
        rows.append(("Links found on the pages (each unique URL checked once)", [
            ("Total extracted URLs (page -> URL)", _fmt(ln["extracted"])), ("Total unique URLs", _fmt(ln["unique"])),
            ("Duplicate URLs skipped (result reused)", _fmt(ln["duplicates_skipped"])),
            ("200 OK", _fmt(ln["s2xx"])), ("Redirects (3xx)", _fmt(ln["s3xx"])), ("4xx", _fmt(ln["s4xx"])), ("5xx", _fmt(ln["s5xx"])),
            ("DNS failures / network errors", _fmt(ln["dns_network"])), ("Timeout failures", _fmt(ln["timeout"])),
            ("SSL failures", _fmt(ln["ssl"])), ("Blocked (robots / firewall)", _fmt(ln["blocked"])),
            ("Worked on a second check (not reported as failures)", _fmt(nums["recheck"].get("recheck_recovered", 0)))]))
        rows.append(("Images", [("Total images extracted (placements)", _fmt(im["extracted"])), ("Unique images", _fmt(im["unique"]))]))
    rows.append(("Report and tickets", [
        ("Report generation", report.get("note", "-")),
        ("Ticket processing", tickets.get("note", "-"))]))

    text_lines = [f"QC crawl summary - {host}", verdict, ""]
    if problems:
        text_lines += ["Needs attention:"] + [f"  - {p}" for p in problems] + [""]
    for title, items in rows:
        text_lines.append(title)
        text_lines += [f"  {k}: {v}" for k, v in items]
        text_lines.append("")
    text_lines.append("Sent by QC after the check finished.")
    color = "#09644F" if not problems else "#B42318"
    h = [f"""<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1f1f1f;max-width:680px">
<h2 style="margin:0 0 4px">QC crawl summary - {html.escape(host)}</h2>
<p style="margin:0 0 14px;font-weight:600;color:{color}">{html.escape(verdict)}</p>"""]
    if problems:
        h.append("<p style='margin:0 0 4px'><b>Needs attention</b></p><ul style='margin:0 0 14px'>" +
                 "".join(f"<li>{html.escape(p)}</li>" for p in problems) + "</ul>")
    for title, items in rows:
        h.append(f"<h3 style='margin:14px 0 6px;font-size:15px'>{html.escape(title)}</h3>"
                 "<table style='border-collapse:collapse;width:100%'>" +
                 "".join(f"<tr><td style='padding:4px 8px;border-bottom:1px solid #eee'>{html.escape(str(k))}</td>"
                         f"<td style='padding:4px 8px;border-bottom:1px solid #eee;text-align:right;font-variant-numeric:tabular-nums'>"
                         f"<b>{html.escape(str(v))}</b></td></tr>" for k, v in items) + "</table>")
    h.append("<p style='color:#777;font-size:12px;margin-top:18px'>Sent by QC after the check finished.</p></div>")
    return subject, "\n".join(text_lines), "".join(h)


def verify_xlsx(path: Optional[str]) -> Tuple[bool, str]:
    """The report exists, isn't empty and is a complete .xlsx (zip with a workbook part, no CRC errors)."""
    if not path or not os.path.isfile(path):
        return False, "the report file wasn't created"
    size = os.path.getsize(path)
    if size == 0:
        return False, "the report file is empty"
    try:
        with zipfile.ZipFile(path) as z:
            if "xl/workbook.xml" not in z.namelist():
                return False, "the report file isn't a complete Excel workbook"
            bad = z.testzip()
            if bad:
                return False, f"the report file is damaged ({bad})"
    except zipfile.BadZipFile:
        return False, "the report file isn't a complete Excel workbook"
    return True, ""


async def summary_for_run(pool, run_id: int, site: Dict[str, Any], attach: bool = True) -> Dict[str, Any]:
    """Everything the e-mail needs: numbers, the verified report (path), the ticket status."""
    run = dict(await pool.fetchrow("SELECT * FROM runs WHERE id = $1", run_id))
    lst = await pool.fetchrow("SELECT label, progress FROM url_lists WHERE run_id = $1 ORDER BY id DESC LIMIT 1", run_id)
    source = (lst["label"] if lst else "") or ""
    nums = await asyncio.to_thread(crawl_numbers, run_id)
    if nums is not None:
        prog = (lst["progress"] if lst else None) or {}
        if isinstance(prog, str):
            import json
            prog = json.loads(prog or "{}")
        nums["pages"]["input_duplicates"] = int(prog.get("duplicates") or 0)
    report: Dict[str, Any] = {"status": "off", "note": "Not attached (turned off in the e-mail settings)"}
    if attach and run.get("status") == "completed":
        path = None
        try:
            from qc6 import smt_report
            path = await smt_report.report_file(pool, site, run_id)
            good, why = await asyncio.to_thread(verify_xlsx, path)
            if not good:
                report = {"status": "failed", "note": f"Not attached: {why}"}
            elif os.path.getsize(path) > MAX_ATTACH_BYTES:
                mb = os.path.getsize(path) / 1048576
                report = {"status": "too_big", "path": path,
                          "note": f"Generated ({mb:.1f} MB) but too big to attach (limit {MAX_ATTACH_BYTES // 1048576} MB): "
                                  "download it from QC"}
            else:
                report = {"status": "attached", "path": path,
                          "note": f"Generated and attached ({os.path.getsize(path) / 1048576:.1f} MB, verified)"}
        except Exception as e:                       # noqa: BLE001
            log.warning("management report for run %s failed: %r", run_id, e)
            report = {"status": "failed", "note": f"Not attached: the report couldn't be built ({e.__class__.__name__})"}
            if path and os.path.exists(path):
                os.unlink(path)
    elif attach:
        report = {"status": "failed", "note": "Not generated: the check didn't complete"}
    tickets: Dict[str, Any] = {"note": "-"}
    try:
        from qc6 import internal_tickets
        cnt = (await internal_tickets.list_for_site(pool, site, "all", "", "", run_id=run_id))["counts"]
        tickets = {"counts": cnt, "note": f"Completed: {_fmt(cnt.get('all', 0))} issue tickets for this check "
                                          f"({_fmt(cnt.get('to_raise', 0))} to raise, {_fmt(cnt.get('raised', 0))} raised, "
                                          f"{_fmt(cnt.get('closed', 0))} closed)"}
    except Exception as e:                           # noqa: BLE001
        tickets = {"note": f"Failed ({e.__class__.__name__})"}
    subject, text, html_body = compose(run, site, source, nums, report, tickets)
    return {"subject": subject, "text": text, "html": html_body, "report": report, "numbers": nums, "tickets": tickets,
            "file_name": f"QC-management-report-{(site.get('primary_host') or 'site').replace(':', '_')}-check{run_id}.xlsx"}


async def send_summary(pool, run_id: int, site: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None,
                       to: Optional[List[str]] = None) -> Dict[str, Any]:
    """Build and send the summary; records the outcome on the check's timeline and in mail_settings.last_result."""
    from qc6 import events
    if cfg is None:
        cfg = await load(pool, site["workspace_id"])
    if not cfg or not cfg.get("host") or not cfg.get("sender"):
        raise MailError("Set up e-mail first (SMTP host, sender and recipients).")
    to = to or list(cfg.get("recipients") or [])
    if not to:
        raise MailError("Add at least one recipient.")
    s = await summary_for_run(pool, run_id, site, attach=cfg.get("attach_report", True) is not False)
    files = [(s["report"]["path"], s["file_name"])] if s["report"]["status"] == "attached" else []
    result: Dict[str, Any] = {"run_id": run_id, "at": datetime.utcnow().isoformat() + "Z", "to": len(to),
                              "report": s["report"]["status"]}
    try:
        await asyncio.to_thread(send_now, cfg, to, s["subject"], s["text"], s["html"], files)
        result["ok"] = True
        await events.record(pool, run_id, "mail", f"Summary e-mail sent to {len(to)} recipient(s); report: {s['report']['note']}")
    except MailError as e:
        result.update(ok=False, error=str(e))
        await events.record(pool, run_id, "mail", f"Summary e-mail not sent: {e}")
        raise
    finally:
        p = s["report"].get("path")
        if p and os.path.exists(p):
            os.unlink(p)
        await pool.execute("UPDATE mail_settings SET last_result = $2 WHERE workspace_id = $1", site["workspace_id"], result)
    return {**result, "subject": s["subject"], "report_note": s["report"]["note"]}


async def after_run(pool, run_id: int, site: Dict[str, Any]) -> None:
    """Called when a check completes. Sends only when e-mail is on; never raises."""
    try:
        cfg = await load(pool, site["workspace_id"])
        if not cfg or not cfg.get("enabled"):
            return
        opts = await pool.fetchval("SELECT options FROM runs WHERE id = $1", run_id) or {}
        if isinstance(opts, dict) and opts.get("spot"):
            return                                       # a few pages checked by hand: no summary e-mail
        await send_summary(pool, run_id, site, cfg)
    except MailError as e:
        log.warning("summary e-mail for run %s not sent: %s", run_id, e)
    except Exception:                                    # noqa: BLE001
        log.exception("summary e-mail for run %s failed", run_id)
