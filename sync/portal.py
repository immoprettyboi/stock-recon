"""
Stock Recon - Takealot Seller Portal reader.

Opens Chrome with the sync's own saved login, then reads the data feeds the
portal's own pages use: View Sales and View Returns. Nothing is clicked or
changed in the portal.

This file is reloaded on every sync run, so updates apply without restarting.
"""
from __future__ import annotations
import json
import math
from urllib.parse import quote
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEBUG_DIR = HERE / "debug"
SALES_URL = "https://sellers.takealot.com/sales"
RETURNS_URL = "https://sellers.takealot.com/returns"
OFFERS_URL = "https://sellers.takealot.com/existing-offers/manage-offers"
COURIER_URL = "https://portal.thecourierguy.co.za/shipments"
LAST_OFFERS: list = []   # offers read on the last run (also saved to debug/offers.json)
API_HOST = "seller-api.takealot.com"
SALES_API = "https://seller-api.takealot.com/v2/sales"


class NeedsLogin(Exception):
    pass


def _creds(site):
    """Login details the owner typed into config.ini (they never leave the PC). site: 'takealot' or 'courier'."""
    import configparser
    cp = configparser.ConfigParser()
    cp.read(HERE / "config.ini", encoding="utf-8")
    c = cp["stockrecon"] if "stockrecon" in cp else {}
    e, pw = (c.get(f"{site}_email") or "").strip(), (c.get(f"{site}_password") or "").strip()
    return (e, pw) if e and pw and "PASTE" not in pw.upper() else (None, None)


def _otp_inputs(page):
    """The PIN boxes on a two-step verification page (one box, or one per digit)."""
    if "otp" not in (page.inner_text("body") or "").lower() and "one-time" not in (page.inner_text("body") or "").lower():
        return []
    boxes = page.locator("input:not([type=hidden]):not([type=password]):not([type=email]):not([type=checkbox])")
    return [boxes.nth(i) for i in range(boxes.count()) if boxes.nth(i).is_visible()]


def _enter_otp(page, cfg, log, name):
    """Ask for the PIN in the app (sync card), wait up to 9 minutes for it, then type it in."""
    import re, time, stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        (DEBUG_DIR / "otp_page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(DEBUG_DIR / "otp_page.png"))
    except Exception:
        pass
    body = " ".join((page.inner_text("body") or "").split())
    m = re.search(r"sent to (\S+@[\w.-]+\.\w+)", body)
    to = m.group(1) if m else "your email"
    runs = db.select("sync_runs", "select=id,message&finished_at=is.null&requested=is.false&order=id.desc&limit=1")
    run_id = runs[0]["id"] if runs else None
    last = db.select("sync_runs", "select=id&order=id.desc&limit=1")
    since_id = last[0]["id"] if last else 0
    msg = f"Takealot code needed: a one-time PIN was emailed to {to}. Type it in the app."
    if run_id:
        db.patch("sync_runs", f"id=eq.{run_id}", {"message": msg})
    log(f"{name}: asked for a one-time PIN (emailed to {to}); waiting for it in the app.")

    code, end = None, time.time() + 9 * 60
    asked_at, next_mail, alerted = time.time(), 0, False
    while time.time() < end and not code:
        if time.time() >= next_mail:                       # read the PIN straight from the Gmail inbox
            next_mail = time.time() + 10
            code = gmail_otp(asked_at, log)
            if code:
                log(f"{name}: read the PIN from the e-mail; logging in.")
                break
        rows = db.select("sync_runs", f"select=id,message&id=gt.{since_id}&message=like.otp:*&order=id.desc")
        if rows:
            code = re.sub(r"\D", "", rows[0]["message"])
            for r in rows:
                db._req("DELETE", f"sync_runs?id=eq.{r['id']}", None, {"Prefer": "return=minimal"})
            break
        if not code and not alerted and time.time() - asked_at > 75:   # the mail didn't come through: ask the phones
            alerted = True
            try:
                notify({"url": cfg["url"], "key": cfg["key"]}, "🔑 Takealot needs a PIN",
                       f"{name} emailed a one-time PIN to {to}. Open Stock Recon and type it in within 8 minutes.", url="./", topic="price", log=log)
            except Exception:
                pass
        page.wait_for_timeout(4000)
    if run_id:
        db.patch("sync_runs", f"id=eq.{run_id}", {"message": "Running"})
    if not code:
        log(f"{name}: no PIN was entered in the app within 9 minutes.")
        return False
    boxes = _otp_inputs(page)
    if len(boxes) >= len(code) and len(boxes) > 1:
        for b, ch in zip(boxes, code):
            b.fill(ch)
    elif boxes:
        boxes[0].fill(code)
    else:
        page.keyboard.type(code)
    # tick "Remember this device" so Takealot doesn't ask for a PIN on every login
    try:
        cb = page.locator("input[type=checkbox]")
        ticked = False
        for i in range(cb.count()):
            if not cb.nth(i).is_checked():
                cb.nth(i).check(force=True)
            ticked = True
        if not ticked:
            page.get_by_text(re.compile(r"remember", re.I)).first.click(timeout=3000)
        log(f"{name}: ticked 'Remember this device'.")
    except Exception as e:
        log(f"{name}: couldn't tick 'Remember this device': {repr(e)[:120]}")
    page.wait_for_timeout(500)
    btn = page.locator("button[type=submit], button:has-text('Verify'), button:has-text('Submit'), button:has-text('Continue'), button:has-text('Log In')")
    vis = [btn.nth(i) for i in range(btn.count()) if btn.nth(i).is_visible() and btn.nth(i).is_enabled()]
    if vis:
        vis[0].click()
    else:
        page.keyboard.press("Enter")
    log(f"{name}: PIN from the app typed in.")
    return True


REPORT_URL = "https://immoprettyboi.github.io/stock-recon/report.html"


def monthly_report(p, cfg, log):
    """On the 1st of the month (from 07:00), e-mail last month's report as a PDF. Once per month.
    debug/report_test_request.txt sends this month's report so far, as a test."""
    import configparser, smtplib, ssl
    from email.message import EmailMessage
    cp = configparser.ConfigParser()
    cp.read(HERE / "config.ini", encoding="utf-8")
    c = cp["stockrecon"] if "stockrecon" in cp else {}
    sender, app_pw = (c.get("report_from") or "").strip(), (c.get("report_app_password") or "").replace(" ", "").strip()
    to = [x.strip() for x in (c.get("report_to") or "").replace(";", ",").split(",") if x.strip()]
    now = datetime.now()
    test = (DEBUG_DIR / "report_test_request.txt").exists()
    first = now.replace(day=1)
    month = now.strftime("%Y-%m") if test else (first - timedelta(days=1)).strftime("%Y-%m")
    flag = DEBUG_DIR / f"report_sent_{month}.txt"
    if not test and (now.hour < 7 or flag.exists() or month < "2026-10"):
        return
    tried = DEBUG_DIR / f"report_tried_{month}.txt"
    if not test:
        try:                                              # e-mail failing: try again every 3 hours, not every sync
            if __import__("time").time() - tried.stat().st_mtime < 3 * 3600:
                return
        except OSError:
            pass
        DEBUG_DIR.mkdir(exist_ok=True)
        tried.write_text(now.isoformat(), encoding="utf-8")
    if test:
        (DEBUG_DIR / "report_test_request.txt").unlink()
    can_send = bool(sender and app_pw and "PASTE" not in app_pw.upper() and to)
    if not can_send and not test:
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    out = HERE / "reports"
    out.mkdir(exist_ok=True)
    pdf = out / f"Stock Recon report {month}.pdf"
    browser = p.chromium.launch(channel=cfg.get("browser") or "chrome", headless=True)
    try:
        page = browser.new_page(viewport={"width": 1000, "height": 1400})
        page.add_init_script(f"window.SR_PC_KEY = {json.dumps(cfg['key'])};")
        page.goto(f"{REPORT_URL}?m={month}&t={int(now.timestamp())}", wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_selector("body[data-ready='1']", timeout=60_000)
        page.wait_for_timeout(1500)                       # fonts
        summary = page.evaluate("window.SR_SUMMARY") or {}
        page.pdf(path=str(pdf), format="A4", print_background=True,
                 margin={"top": "14mm", "bottom": "14mm", "left": "12mm", "right": "12mm"})
    finally:
        browser.close()
    label = summary.get("month") or month
    if not can_send:
        log(f"Monthly report: PDF saved in the reports folder ({pdf.name}); not e-mailed because report_from, "
            "report_app_password and report_to aren't filled in in config.ini yet.")
        return
    msg = EmailMessage()
    msg["Subject"] = f"Stock Recon: {label} report" + (" (month to date, test)" if test else "")
    msg["From"] = f"Stock Recon <{sender}>"
    msg["To"] = ", ".join(to)
    lines = [f"Hi,", "", f"Here is the {label} Takealot report" + (" so far (test)." if test else "."), "",
             f"Profit after all costs: {summary.get('profit', '?')} ({summary.get('margin', '?')} margin)",
             f"Units sold: {summary.get('units', '?')}   Sales: {summary.get('sales', '?')}", "",
             "The full report is attached as a PDF. You can also open it any time in the app: Monthly report.",
             "https://immoprettyboi.github.io/stock-recon/", "", "Stock Recon"]
    msg.set_content("\n".join(lines))
    msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
    try:
        host = (c.get("report_smtp_host") or "smtp.gmail.com").strip()
        port = int((c.get("report_smtp_port") or "465").strip())
        user = (c.get("report_smtp_user") or sender).strip()
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60)
        else:
            smtp = smtplib.SMTP(host, port, timeout=60)
            smtp.starttls(context=ssl.create_default_context())
        with smtp:
            smtp.login(user, app_pw)
            smtp.send_message(msg)
    except Exception as e:
        log("Monthly report: the e-mail couldn't be sent: " + repr(e)[:200])
        return
    if not test:
        flag.write_text(now.isoformat(), encoding="utf-8")
    log(f"Monthly report: {label} e-mailed to {', '.join(to)}" + (" (test)." if test else "."))


def gmail_otp(since_ts, log=None, peek=False):
    """Takealot's one-time PIN from the Gmail inbox that also sends the monthly report (report_from + its app password).
    Only looks at mails from Takealot that arrived after since_ts. Returns the PIN, or None. With peek=True: a short
    description of the newest Takealot PIN mail (no PIN), for checking the setup."""
    import configparser, imaplib, email, email.header, re as _re, time as _t
    from email.utils import parsedate_to_datetime
    cp = configparser.ConfigParser()
    cp.read(HERE / "config.ini", encoding="utf-8")
    c = cp["stockrecon"] if "stockrecon" in cp else {}
    user, pw = (c.get("report_from") or "").strip(), (c.get("report_app_password") or "").replace(" ", "").strip()
    if not (user and pw) or (c.get("otp_from_gmail") or "yes").strip().lower() in ("no", "0", "false", "off"):
        return None
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        M.login(user, pw)
        M.select("INBOX", readonly=True)
        day = _t.strftime("%d-%b-%Y", _t.gmtime(since_ts - 86400))
        typ, data = M.search(None, f'(FROM "takealot" SINCE {day})')
        ids = (data[0] or b"").split()[-(40 if peek else 10):]
        best = None
        for i in reversed(ids):
            typ, msg_data = M.fetch(i, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            try:
                dt = parsedate_to_datetime(msg["Date"])
                if dt.tzinfo is None:                       # "-0000" = UTC
                    from datetime import timezone as _tz
                    dt = dt.replace(tzinfo=_tz.utc)
                when = dt.timestamp()
            except Exception:
                when = 0
            subj = str(email.header.make_header(email.header.decode_header(msg.get("Subject") or "")))
            text = ""
            for part in (msg.walk() if msg.is_multipart() else [msg]):
                if part.get_content_type() in ("text/plain", "text/html"):
                    try:
                        text += part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore") + " "
                    except Exception:
                        pass
            text = _re.sub(r"(?is)<(style|script|head)\b.*?</\1>", " ", text)
            text = _re.sub(r"(?s)/\*.*?\*/|@font-face\s*\{[^}]*\}|@media[^{]*\{(?:[^{}]*\{[^}]*\})*[^}]*\}", " ", text)
            text = _re.sub(r"<[^>]+>", " ", text)
            text = _re.sub(r"https?://\S+", " ", text)
            if not _re.search(r"\bOTP\b|one[- ]time|\bPIN\b", subj + " " + text, _re.I):
                continue
            m = _re.search(r"(?:OTP|PIN|code)[^0-9]{0,80}?\b(\d{4,8})\b", text, _re.I) or _re.search(r"\b(\d{6})\b", text)
            if peek:
                flat = " ".join(text.split())
                if m:
                    k = flat.find(m.group(1))
                    flat = flat[max(0, k - 120):k + 60]
                masked = _re.sub(r"\d", "#", flat)[:500]
                best = (f"newest Takealot PIN mail: '{subj[:60]}' at {_t.strftime('%Y-%m-%d %H:%M', _t.localtime(when))}; "
                        f"PIN found: {'yes' if m else 'no'}; text (digits hidden): {masked}")
                break
            if m and when >= since_ts - 30:
                best = m.group(1)
                break
        M.logout()
        return best
    except Exception as e:
        if log:
            log("Reading the Takealot PIN from Gmail failed: " + repr(e)[:150])
        return None


def send_mail(subject, body, attachments, log, what="E-mail"):
    """Send with the report_* settings in config.ini. attachments: [(filename, bytes, maintype, subtype)]. Returns True if sent."""
    import configparser, smtplib, ssl
    from email.message import EmailMessage
    cp = configparser.ConfigParser()
    cp.read(HERE / "config.ini", encoding="utf-8")
    c = cp["stockrecon"] if "stockrecon" in cp else {}
    sender, app_pw = (c.get("report_from") or "").strip(), (c.get("report_app_password") or "").replace(" ", "").strip()
    to = [x.strip() for x in (c.get("report_to") or "").replace(";", ",").split(",") if x.strip()]
    if not (sender and app_pw and "PASTE" not in app_pw.upper() and to):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Stock Recon <{sender}>", ", ".join(to)
    msg.set_content(body)
    for name, data, mt, st in attachments:
        msg.add_attachment(data, maintype=mt, subtype=st, filename=name)
    try:
        host = (c.get("report_smtp_host") or "smtp.gmail.com").strip()
        port = int((c.get("report_smtp_port") or "465").strip())
        user = (c.get("report_smtp_user") or sender).strip()
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60)
        else:
            smtp = smtplib.SMTP(host, port, timeout=60)
            smtp.starttls(context=ssl.create_default_context())
        with smtp:
            smtp.login(user, app_pw)
            smtp.send_message(msg)
        log(f"{what}: e-mailed to {', '.join(to)}.")
        return True
    except Exception as e:
        log(f"{what}: the e-mail couldn't be sent: " + repr(e)[:200])
        return False


RECON_TYPE = 27      # Takealot report type "Stock Recon"
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def _num(v):
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def parse_recon(text):
    """Takealot's Stock Recon CSV -> (product lines, missing lines)."""
    import csv, io
    rows, missing = [], []
    for r in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        if not str(r.get("period") or "").strip().isdigit():
            continue                                           # the line that explains each column
        line = {"sku": (r.get("sku") or "").strip(), "tsin": (r.get("tsin_id") or "").strip(), "title": (r.get("product_title") or "").strip()}
        for k, v in r.items():
            if k not in ("period", "sku", "tsin_id", "product_title") and k:
                line[k] = _num(v)
        rows.append(line)
        lost = line.get("warehouse_down_adjustments", 0)
        # picked-out / counted-out stock that wasn't found again in the same month (found stock cancels it out)
        other = max(0.0, line.get("other_outflow", 0) - line.get("other_inflow", 0) - line.get("warehouse_up_adjustments", 0)
                    - line.get("lost_in_transit_returns_found", 0))
        if lost > 0 or other > 0:
            missing.append({"sku": line["sku"], "title": line["title"], "lost": lost, "other_outflow": other,
                            "claim_value": line.get("total_claim_value", 0), "average_selling_price": line.get("average_selling_price", 0),
                            "success_fee": line.get("success_fee", 0), "fulfilment_fee": line.get("fulfilment_fee", 0)})
    return rows, missing


def takealot_recon(ctx, cfg, auth, log):
    """Takealot releases a Stock Recon report for each month. From the 1st, ask for last month's, download it when it's
    ready, and save it (file + database). Missing stock shows in the app (Needs you / Stock check); no e-mail is sent."""
    import stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    try:
        have = {r["month"] for r in db.select("takealot_recons", "select=month")}
    except RuntimeError as e:
        if "takealot_recons" in str(e):
            flag = DEBUG_DIR / "recon_needs_sql.txt"
            if not flag.exists():
                DEBUG_DIR.mkdir(exist_ok=True)
                flag.write_text("1")
                log("Takealot stock recon: run 9_database_update.sql in Supabase to switch this on.")
            return
        raise
    now = datetime.now()
    prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    r = ctx.request.get(f"https://{API_HOST}/v2/tasks/group/report?check_archived=true&page_number=1&page_size=50&only_archived=false",
                        headers=auth, timeout=60_000)
    if not r.ok:
        log(f"Takealot stock recon: the reports list answered {r.status}")
        return
    recon = {}
    for t in (r.json() or {}).get("tasks") or []:
        if (t.get("task_type") or {}).get("task_type_id") != RECON_TYPE:
            continue
        try:
            params = json.loads(t.get("request_params") or "{}")
        except Exception:
            params = {}
        m = str(params.get("date_from") or "")[:7]
        if len(m) != 7:
            continue
        done = (t.get("task_status") or {}).get("task_status_type_id") == 4 and bool((t.get("result") or {}).get("success"))
        cur = recon.get(m)
        if not cur or (done and not cur["done"]) or (done == cur["done"] and str(t.get("date_created")) > str(cur["t"].get("date_created"))):
            recon[m] = {"t": t, "done": done}
    try:                                                     # keep the latest reports list for checking
        (DEBUG_DIR / "recon_tasks.json").write_text(json.dumps((r.json() or {}).get("tasks") or [], indent=1)[:200000], encoding="utf-8")
    except Exception:
        pass
    if prev in recon and not recon[prev]["done"] and prev not in have:
        t0 = recon[prev]["t"]
        why = (t0.get("result") or {}).get("message") or (t0.get("task_status") or {}).get("task_status_type_id")
        log(f"Takealot stock recon: the {prev} report isn't available yet (Takealot: {why}). Trying again every hour.")
    stuck = False
    if prev in recon and not recon[prev]["done"]:
        try:
            made = datetime.fromisoformat(str(recon[prev]["t"].get("date_created")).replace("Z", "+00:00")).replace(tzinfo=None)
            failed = (recon[prev]["t"].get("task_status") or {}).get("task_status_type_id") == 5
            # failed (usually: Takealot hasn't released the month yet) -> ask again after an hour; stuck -> after 6 hours
            stuck = (datetime.now() - made).total_seconds() > (3600 if failed else 6 * 3600)   # Takealot's times are SA time
        except Exception:
            stuck = False
    if prev not in have and (prev not in recon or stuck) and prev >= "2026-10" and now.hour >= 6:
        rq = ctx.request.post(f"https://{API_HOST}/v2/task/report", headers={**auth, "content-type": "application/json"},
                              data=json.dumps({"check_duplicate": True, "task_type_id": RECON_TYPE,
                                               "request_params": {"delimeter": ",", "date_from": prev}}), timeout=60_000)
        log(f"Takealot stock recon: asked Takealot for the {prev} report ({rq.status}); it downloads once it's ready."
            if rq.ok else f"Takealot stock recon: asking for the {prev} report failed ({rq.status}): {rq.text()[:150]}")
        return
    for m, x in sorted(recon.items()):
        if m in have or not x["done"] or m < "2026-08":
            continue
        t = x["t"]
        d = ctx.request.get(f"https://{API_HOST}/v2/task/{t['task_id']}/report/download", headers=auth, timeout=90_000)
        if not d.ok:
            log(f"Takealot stock recon: downloading {m} answered {d.status}")
            continue
        text = d.text()
        if text.lstrip("\ufeff").startswith("{"):                 # a link instead of the file
            j = json.loads(text)
            link = j.get("url") or j.get("download_url") or (j.get("result") or {}).get("url")
            text = ctx.request.get(link, timeout=90_000).text() if link else ""
        rows, missing = parse_recon(text)
        name = f"Takealot Stock Recon {m}.csv"
        out = HERE / "reports"
        out.mkdir(exist_ok=True)
        (out / name).write_text(text, encoding="utf-8")
        units = int(sum(x["lost"] + x["other_outflow"] for x in missing))
        claim = round(sum(x["claim_value"] for x in missing), 2)
        db.insert("takealot_recons", [{"month": m, "task_id": str(t["task_id"]), "file_name": name, "csv": text, "rows": rows,
                                       "missing": missing, "missing_units": units, "claim_value": claim,
                                       "claim_status": "open" if missing else "nothing", "downloaded_at": now.isoformat()}], upsert_on="month")
        label = f"{_MONTHS[int(m[5:7]) - 1]} {m[:4]}"
        log(f"Takealot stock recon {label}: {len(rows)} product line(s), "
            + (f"{units} unit(s) missing, claim value R{claim:,.2f}." if missing else "nothing missing."))


def receive_returns(cfg, rows, log):
    """A Courier Guy shipment from a Takealot DC to us shows Delivered -> book in the units it carried.
    Units from a shipment Takealot rejected (good stock) go straight to In stock at HOME.
    Removal-order units (customer returns, often defective) are marked as arrived at HOME for you to check first."""
    import stockrecon_sync as S
    delivered = {r["id"]: r for r in rows if r["direction"] == "from_takealot" and r["status"] == "delivered"}
    if not delivered:
        return
    db = S.DB(cfg["url"], cfg["key"])
    units = db.select("units", "select=id,product,status,location,order_id,po,comment,courier_shipment_id,flag,updated_at&status=eq.return_me")
    live_from = {}
    for r in rows:
        if r["direction"] == "from_takealot" and r["status"] not in ("cancelled",) and not str(r.get("waybill") or "").upper().endswith("-R") \
                and str(r.get("created_at") or "")[:10] >= (date.today() - timedelta(days=14)).isoformat():
            live_from.setdefault(r["takealot_dc"], []).append(r)          # bookings from that DC in the last 2 weeks
    now = datetime.utcnow().isoformat() + "Z"
    for sid, x in delivered.items():
        mine = [u for u in units if u.get("courier_shipment_id") == sid]
        if not mine and not str(x.get("waybill") or "").upper().endswith("-R") and len(live_from.get(x["takealot_dc"], [])) == 1:
            # only units already waiting to come back when this shipment was delivered (not a later removal order),
            # and only while the delivery is recent
            dl = str(x.get("delivered_at") or "")
            if dl and dl[:10] >= (date.today() - timedelta(days=3)).isoformat():
                mine = [u for u in units if not u.get("courier_shipment_id") and u.get("location") == x["takealot_dc"]
                        and str(u.get("updated_at") or "") <= (dl + "T23:59:59" if len(dl) == 10 else dl)]
                for u in mine:
                    db.patch("units", f"id=eq.{u['id']}", {"courier_shipment_id": sid})
        day = str(x.get("delivered_at") or now)[:10]
        for u in mine:
            c = (u.get("comment") or "").strip()
            if "Takealot rejected" in c:
                body = {"status": "in_stock", "location": "HOME", "flag": False, "updated_at": now, "po": "",
                        "comment": (c + f" Arrived back {day} on {x['waybill']}: in stock at HOME.").strip()}
                try:
                    db.patch("units", f"id=eq.{u['id']}", body | {"arrived_at": day})
                except RuntimeError:
                    db.patch("units", f"id=eq.{u['id']}", body)
                log(f"  Arrived on {x['waybill']}: {u['product']} set to In stock at HOME.")
            elif u.get("location") != "HOME":
                body = {"location": "HOME", "flag": True, "updated_at": now,
                        "comment": (f"Arrived back {day} on {x['waybill']}. Check it on the Removal orders tab: Back in stock (used) "
                                    "or Faulty. " + c).strip()}
                try:
                    db.patch("units", f"id=eq.{u['id']}", body | {"arrived_at": day, "removal_order_id": u.get("order_id") or None})
                except RuntimeError:
                    db.patch("units", f"id=eq.{u['id']}", body)          # before 10_database_update.sql
                log(f"  Arrived on {x['waybill']}: {u['product']} (order {u.get('order_id') or '-'}) is at HOME, waiting for you to check it.")


# ---------------------------------------------------------------- phone notifications (web push)
_PUSH_READY = None


def _webpush():
    """pywebpush, installed on first use."""
    global _PUSH_READY
    try:
        from pywebpush import webpush, WebPushException
        _PUSH_READY = True
        return webpush, WebPushException
    except ImportError:
        import stockrecon_sync as _S
        if _PUSH_READY is False or (time_ := getattr(_S, "_PUSH_FAILED_AT", 0)) and __import__("time").time() - time_ < 6 * 3600:
            return None, None
        import subprocess, sys
        exe = sys.executable.replace("pythonw.exe", "python.exe")
        try:
            subprocess.run([exe, "-m", "pip", "install", "--quiet", "pywebpush"], timeout=300, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            from pywebpush import webpush, WebPushException
            _PUSH_READY = True
            return webpush, WebPushException
        except Exception:
            _PUSH_READY = False
            _S._PUSH_FAILED_AT = __import__("time").time()       # don't try pip again for 6 hours (portal.py is reloaded often)
            return None, None


def notify(cfg, title, body, url="./", topic="price", log=None, tag=None):
    """Send a notification to every phone that switched notifications on in the app."""
    import stockrecon_sync as S
    key_file = HERE / "vapid_private.pem"
    if not key_file.exists():
        return
    db = S.DB(cfg["url"], cfg["key"])
    try:
        subs = db.select("push_subscriptions", "select=endpoint,keys,topics,user_email")
    except RuntimeError:
        return                                            # before 14_database_update.sql
    subs = [x for x in subs if topic in (x.get("topics") or ["price"])]
    if not subs:
        return
    webpush, WebPushException = _webpush()
    if not webpush:
        if log:
            log("Notifications: couldn't install pywebpush.")
        return
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag or topic})
    results = []
    for x in subs:
        who = (x.get("user_email") or "?").split("@")[0]
        try:
            resp = webpush(subscription_info={"endpoint": x["endpoint"], "keys": x["keys"]}, data=payload,
                    vapid_private_key=str(key_file), vapid_claims={"sub": "mailto:stockrecon@example.com"}, ttl=3600,
                    headers={"Urgency": "high"})          # high: Android delivers it straight away instead of holding it for later
            db.patch("push_subscriptions", f"endpoint=eq.{quote(x['endpoint'], safe='')}", {"last_sent_at": datetime.utcnow().isoformat() + "Z"})
            results.append(f"{who}: sent ({getattr(resp, 'status_code', 'ok')})")
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            results.append(f"{who}: refused ({code})")
            if code in (404, 410):                         # the phone unsubscribed / app removed
                try:
                    db._req("DELETE", f"push_subscriptions?endpoint=eq.{quote(x['endpoint'], safe='')}", None, {"Prefer": "return=minimal"})
                except Exception:
                    pass
            elif log:
                log(f"Notification failed ({code}): {str(e)[:120]}")
        except Exception as e:
            results.append(f"{who}: error")
            if log:
                log("Notification failed: " + repr(e)[:150])
    try:
        from importlib.metadata import version as _v
        results.append(f"pywebpush {_v('pywebpush')}, py_vapid {_v('py-vapid')}, http_ece {_v('http-ece')}")
    except Exception:
        pass
    return results


# ---------------------------------------------------------------- price fighter
def _due(rule, now):
    lc = rule.get("last_checked")
    if not lc:
        return True
    try:
        t = datetime.fromisoformat(str(lc).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return True
    cm = rule.get("check_minutes")
    cm = 10 if cm is None else int(cm)
    if cm <= 0:                                            # continuous: every few seconds
        return (now - t).total_seconds() >= 3
    return (now - t).total_seconds() >= cm * 60 - 25


def price_tick(cfg, log, force=False):
    """Called every minute between syncs. Checks the Buy Box of each listing you switched on (when it's due) and,
    in fight mode, changes your price within your floor / ceiling. Watch mode only records what it would do."""
    import math, stockrecon_sync as S
    restart_if_updated(log)
    try:
        heartbeat(cfg)
    except Exception:
        pass
    _READ_LOG[0] = log
    global _READ_FAIL_SINCE
    if not hasattr(S, "_read_fail"):
        S._read_fail = [0, 0]
    _READ_FAIL_SINCE = S._read_fail                    # kept in the sync program, so it survives portal.py reloads
    global _LAST_ACTIVE
    if not hasattr(S, "_last_active"):
        S._last_active = [__import__("time").time()]
    _LAST_ACTIVE = S._last_active
    _READ_NOTIFY[0] = lambda t, b: notify({"url": cfg["url"], "key": cfg["key"]}, t, b, url="./?view=price", topic="price", log=log)
    db = S.DB(cfg["url"], cfg["key"])
    try:
        rules = db.select("price_rules", "select=*&enabled=is.true")
    except RuntimeError:
        return                                            # before 13_database_update.sql
    now = datetime.utcnow()
    if (DEBUG_DIR / "rules_dump.txt").exists():
        (DEBUG_DIR / "rules_dump.txt").unlink()
        allr = db.select("price_rules", "select=sku,enabled,mode,check_minutes,last_checked,last_action,winning,my_price,bb_price")
        log("Price fighter rules: " + json.dumps(allr, default=str)[:1500])
    due = [r for r in rules if force or _due(r, now)]
    fast = any(r.get("check_minutes") is not None and int(r["check_minutes"]) <= 0 for r in rules)
    if not due:
        return fast
    prods = {p["sku"]: p for p in db.select("products", "select=sku,product,takealot_title,takealot_stock")}
    from playwright.sync_api import sync_playwright
    import stockrecon_sync as SS
    with sync_playwright() as p:
        fresh = None
        if _creds_key():
            # with the API key the fighter needs no Seller Portal login: use a clean browser that looks like normal Chrome
            # (the sync's own profile started getting blocked by Takealot's bot protection)
            fresh = p.chromium.launch(channel=cfg["browser"], headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = fresh.new_context(user_agent=BROWSER_UA, locale="en-ZA", timezone_id="Africa/Johannesburg",
                                    viewport={"width": 1400, "height": 900})
        else:
            ctx = launch(p, cfg, True, SS.PROFILE_DIR, SS.DOWNLOADS)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            auth = {}
            def on_req(req):
                if API_HOST in req.url and not auth:
                    h = {k: v for k, v in req.headers.items() if k.lower() in ("authorization", "x-api-key", "x-csrftoken", "x-xsrf-token", "accept")}
                    if h:
                        auth.update(h)
            page.on("request", on_req)
            if any(r.get("mode") == "fight" for r in due) and not _creds_key():
                page.goto(SALES_URL, wait_until="domcontentloaded", timeout=90_000)
                for _ in range(40):
                    if auth or "/login" in page.url:
                        break
                    page.wait_for_timeout(500)
            # one takealot.com page; each listing's Buy Box is then read with a quick in-page request (well under a second)
            tk = ctx.new_page()
            try:
                tk.goto("https://www.takealot.com/", wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass
            import time as _time
            # continuous listings: keep one browser for up to 10 minutes, checking every ~3 s (starting Chrome is the
            # heavy part, so don't restart it every minute); stop early if the app asks for a sync
            t_end, next_req = _time.time() + 600, 0
            while True:
                for r in due:
                    try:
                        _fight_one(ctx, tk, db, r, prods.get(r["sku"]) or {}, auth, log)
                    except Exception as e:
                        log(f"Price fighter {r['sku']}: {repr(e)[:160]}")
                try:
                    sale_watch(ctx, cfg, log)                 # new sales -> phone, every 2 minutes
                except Exception:
                    pass
                if _time.time() > t_end - 3:
                    break
                # in a live fight (Buy Box changed in the last 10 min) check every 2 s; when it's quiet every 5 s,
                # so Takealot sees fewer requests overall and is less likely to block us
                _time.sleep(2 if _time.time() - _LAST_ACTIVE[0] < 600 else 5)
                if _time.time() >= next_req:
                    next_req = _time.time() + 15
                    try:
                        if db.select("sync_runs", "select=id&requested=is.true&finished_at=is.null&limit=1"):
                            break
                    except Exception:
                        pass
                try:
                    rules = db.select("price_rules", "select=*&enabled=is.true")
                except Exception:
                    break
                due = [r for r in rules if r.get("check_minutes") is not None and int(r["check_minutes"]) <= 0]
                if not due:
                    break
        finally:
            ctx.close()
            if fresh:
                fresh.close()
    return fast


def _floor_for(db, rule, prod):
    if rule.get("floor_mode") == "manual" and rule.get("floor_price"):
        return float(rule["floor_price"])
    group = prod.get("product") or prod.get("takealot_title")
    titles = {prod.get("takealot_title"), group}
    try:
        others = db.select("products", f"select=takealot_title&product=eq.{quote(group or '')}")
        titles |= {o["takealot_title"] for o in others}
    except Exception:
        pass
    costs = []
    for t in [x for x in titles if x]:
        try:
            costs += [float(u["cost"]) for u in db.select("units", f"select=cost&status=eq.in_stock&product=eq.{quote(t)}") if float(u.get("cost") or 0) > 0]
        except Exception:
            pass
    if not costs:
        for t in [x for x in titles if x]:
            try:
                costs += [float(u["cost"]) for u in db.select("units", f"select=cost,received&product=eq.{quote(t)}&order=received.desc&limit=5") if float(u.get("cost") or 0) > 0]
            except Exception:
                pass
    if not costs:
        return None
    cost = max(costs)                                       # the dearest unit you might sell, to be safe
    ratio = 0.12
    try:
        ss = [x for x in db.select("takealot_sales", f"select=gross,fees&sku=eq.{rule['sku']}&fees=gt.0&order=order_date.desc&limit=10") if float(x.get("gross") or 0) > 0]
        if ss:
            ratio = sum(float(x["fees"]) / float(x["gross"]) for x in ss) / len(ss)
    except Exception:
        pass
    profit = max(float(rule.get("min_profit") or 0), cost * float(rule.get("min_profit_pct") or 0) / 100)   # rand or % of cost
    return math.ceil((cost + profit) / max(0.5, 1 - ratio))


# ---- sniper timing: Takealot publishes price changes in batches. Learn when, and send ours just before the cut-off.
TIMING_FILE = DEBUG_DIR / "publish_times.json"


def _tload():
    try:
        return json.loads(TIMING_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"times": [], "lead": 30, "rounds": []}


def _tsave(t):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        t["times"] = sorted(set(int(x) for x in t.get("times", [])))[-200:]
        t["rounds"] = t.get("rounds", [])[-100:]
        TIMING_FILE.write_text(json.dumps(t, indent=1), encoding="utf-8")
    except Exception:
        pass


def _tadd(t, ts):
    ts = int(ts)
    if not any(abs(ts - x) < 20 for x in t.get("times", [])):      # one batch = one moment
        t.setdefault("times", []).append(ts)


def _period(times):
    ts = sorted(times)
    d = [b - a for a, b in zip(ts, ts[1:]) if 150 <= b - a <= 3600]
    if len(d) < 2:
        return None
    base = min(d)
    vals = sorted(x / max(1, round(x / base)) for x in d)
    p = vals[len(vals) // 2]
    return p if 150 <= p <= 900 else None


def _next_publish(t, now):
    times = t.get("times") or []
    p = _period(times)
    if not p:
        return None, None
    last = max(times)
    n = max(1, math.ceil((now - last) / p))
    nxt = last + n * p
    while nxt - now < 4:
        nxt += p
    return nxt, p


BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
_READ_FAIL_LOGGED = 0
_READ_LOG = [print]
_READ_NOTIFY = [None]
_READ_FAIL_SINCE = [0, 0]             # since when reads fail, when the phones were last told


def _read_live(page, plid, offer_id, mine):
    """(Buy Box seller, Buy Box price, your live price or None, cheapest other price, signature) from takealot.com."""
    global _READ_FAIL_LOGGED
    why = ""
    try:
        j = page.evaluate("""async (plid) => { try { const r = await fetch(`https://api.takealot.com/rest/v-1-19-0/product-details/PLID${plid}?platform=desktop&_=${Date.now()}`,
                                  {credentials: "include", cache: "no-store"}); return r.ok ? await r.json() : {_status: r.status, _text: (await r.text()).slice(0, 200)}; }
                                  catch (e) { return {_status: 0, _text: String(e).slice(0, 200)}; } }""", str(plid)) or {}
    except Exception as e:
        j, why = {}, repr(e)[:200]
    if not ((j.get("buybox") or {}).get("items")):
        # the in-page request failed (page not on takealot.com any more, or blocked): reopen the page, and ask directly
        try:
            if "takealot.com" not in (page.url or ""):
                page.goto("https://www.takealot.com/", wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            pass
        try:
            import time as _t0
            rr = page.context.request.get(f"https://api.takealot.com/rest/v-1-19-0/product-details/PLID{plid}?platform=desktop&_={int(_t0.time()*1000)}",
                                          headers={"Referer": "https://www.takealot.com/", "Origin": "https://www.takealot.com",
                                                   "Accept": "application/json"}, timeout=20_000)
            if rr.ok:
                j = rr.json() or {}
            else:
                why = (why + f" | direct: {rr.status} {rr.text()[:120]}").strip(" |")
        except Exception as e:
            why = (why + " | direct: " + repr(e)[:120]).strip(" |")
    items = ((j.get("buybox") or {}).get("items")) or []
    it = next((i for i in items if i.get("is_selected")), items[0] if items else None)
    if not it:
        import time as _t
        if _t.time() - _READ_FAIL_LOGGED > 600:              # say why, at most every 10 minutes
            _READ_FAIL_LOGGED = _t.time()
            _READ_LOG[0](f"Price fighter: couldn't read the Buy Box for PLID{plid}: "
                          + (why or f"Takealot answered {j.get('_status', 'no buy box')} {str(j.get('_text') or '')[:150]}"))
            if _READ_FAIL_SINCE[0] == 0:
                _READ_FAIL_SINCE[0] = _t.time()
            elif _t.time() - _READ_FAIL_SINCE[0] > 900 and _t.time() - _READ_FAIL_SINCE[1] > 3 * 3600 and _READ_NOTIFY[0]:
                _READ_FAIL_SINCE[1] = _t.time()               # blind for 15+ minutes: tell the phones (at most every 3 hours)
                try:
                    _READ_NOTIFY[0]("⚠️ Price fighter can't see the Buy Box",
                                    "Takealot is blocking the repricer's checks, so prices aren't being changed. If a VPN is on, try switching it off.")
                except Exception:
                    pass
        return None
    _READ_FAIL_SINCE[0] = 0
    seller, price = str(it.get("sponsored_ads_seller_id") or ""), float(it.get("price") or 0)
    others = []
    for c in ((j.get("other_offers") or {}).get("conditions") or []):
        for o in c.get("items") or []:
            others.append((str(o.get("product_id") or ""), float(o.get("price") or 0)))
    mine_live = price if seller == mine else next((p for pid, p in others if pid == str(offer_id)), None)
    # Takealot sometimes shows two offers: "Fastest Delivery" (selected, a seller with warehouse stock) and "Best Price"
    slot = ""
    for i in items:
        if not i.get("is_selected") and str(i.get("sponsored_ads_seller_id") or "") == mine:
            slot = (i.get("offer_detail") or {}).get("preference") or "other"
            if mine_live is None and i.get("price"):
                mine_live = float(i["price"])
    other_min = min([p for pid, p in others if pid != str(offer_id)] + ([price] if seller != mine else []), default=None)
    sig = f"{seller}:{price}|" + ",".join(f"{pid}:{p}" for pid, p in sorted(others))
    return {"seller": seller, "price": price, "mine": mine_live, "other_min": other_min, "sig": sig, "slot": slot}



_FCACHE = {}
_LAST_ACTIVE = [__import__("time").time()]           # start fast after each restart / reload


def _fight_one(ctx, page, db, rule, prod, auth, log):
    import math
    sku = rule["sku"]
    offer = (prod.get("takealot_stock") or {}).get("OFFER") or {}
    plid, offer_id, mine = offer.get("plid"), offer.get("offer_id"), f"M{offer.get('merchant_id') or 29853221}"
    title = (prod.get("takealot_title") or sku)[:50]
    nowiso = datetime.utcnow().isoformat() + "Z"
    if not plid:
        db.patch("price_rules", f"sku=eq.{sku}", {"last_checked": nowiso, "last_action": "Listing not found on Takealot yet (next sync)"})
        return
    # nothing to sell (no stock at any Takealot warehouse and 0 on lead time): don't fight. Lowering the price can't win
    # the Buy Box without stock, only drag the price down, and it would keep sending notifications.
    tsk = prod.get("takealot_stock") or {}
    sellable = sum(int((tsk.get(d) or {}).get("qty") or 0) for d in ("JHB", "CPT", "DBN")) + int((tsk.get("LEADTIME") or {}).get("qty") or 0)
    if tsk and sellable <= 0:
        msg = "No stock to sell on Takealot: not fighting until stock is back"
        if rule.get("last_action") != msg:
            db.patch("price_rules", f"sku=eq.{sku}", {"last_checked": nowiso, "last_action": msg, "winning": False})
        return
    live = _read_live(page, plid, offer_id, mine)
    if not live:
        db.patch("price_rules", f"sku=eq.{sku}", {"last_checked": nowiso})      # keep last_action: try again next check
        return
    bb_price, bb_seller = live["price"], live["seller"]
    # how long Takealot took to put our last change live (measured, shown in the history)
    import time as _time
    tm = _tload()
    pend = (tm.get("pending") or {}).get(sku)
    if pend and live["mine"] is not None and int(live["mine"]) == int(pend["price"]):
        delay = int(_time.time() - pend["sent"])
        tm.setdefault("rounds", []).append({"sku": sku, "sent": pend["sent"], "live": int(_time.time()), "via": pend.get("via"),
                                            "delay": delay, "won": bb_seller == mine})
        tm["pending"].pop(sku, None)
        _tsave(tm)
        try:
            db.patch("price_log", f"sku=eq.{sku}&new_price=eq.{int(pend['price'])}&at=gt.{(datetime.utcnow() - timedelta(minutes=30)).isoformat()}Z",
                     {"note": f"live on Takealot after {delay // 60}m{delay % 60:02d}s via {pend.get('via', 'portal')}" +
                              (" · ✅ you got the Buy Box" if bb_seller == mine else " · competitor still ahead")})
        except Exception:
            pass
    winning = bb_seller == mine
    my_price = float(rule.get("my_price") or offer.get("price") or 0)
    pend = (_tload().get("pending") or {}).get(sku)
    waiting_live = bool(pend and _time.time() - pend["sent"] < 900)     # our last change isn't live on Takealot yet
    if waiting_live:
        my_price = float(pend["price"])
    elif winning:
        my_price = bb_price
    elif live.get("mine") is not None:
        my_price = float(live["mine"])                     # what Takealot shows now (e.g. after a manual change)
    import stockrecon_sync as _S
    ck = _S.__dict__.setdefault("_FCACHE", {}).setdefault(sku, {})   # kept on the main module: survives portal.py reloads
    fkey = tuple(rule.get(k) for k in ("floor_mode", "floor_price", "min_profit", "min_profit_pct"))
    if _time.time() - ck.get("floor_at", 0) > 300 or ck.get("fkey") != fkey:   # floor changes with stock / sales / your settings
        ck["floor"], ck["floor_at"], ck["fkey"] = _floor_for(db, rule, prod), _time.time(), fkey
    floor = ck["floor"]
    ceiling = float(rule["ceiling_price"]) if rule.get("ceiling_price") else None
    step = max(1.0, float(rule.get("step") or 1))
    fight = rule.get("mode") == "fight"
    # how many real changes in the last 24 h
    since = (datetime.utcnow() - timedelta(hours=24)).isoformat() + "Z"
    if _time.time() - ck.get("changes_at", 0) > 60:
        try:
            ck["changes"] = len(db.select("price_log", f"select=id&sku=eq.{sku}&action=in.(lowered,raised,at_floor,reset,follow)&at=gt.{since}"))
        except Exception:
            ck["changes"] = 0
        ck["changes_at"] = _time.time()
    changes = ck["changes"]
    target, action, note = None, None, ""
    new_phase = {"p": "fight", "since": 0}                  # default: normal fighting (reset unless the floor logic says otherwise)
    if not winning:
        want = math.floor(bb_price - step)
        if floor is None:
            note = "No cost found for this product, so no floor: not changing the price"
        elif want >= floor:
            if want < my_price or my_price == 0:
                target, action = want, "lowered"
            elif want > my_price and not waiting_live:
                # the seller ahead of us is well above our price (e.g. they're shown first for warehouse stock):
                # come up to just under them instead of leaving money on the table
                target, action, note = want, "raised", f"The Buy Box is R{bb_price:,.0f}: up to R{step:,.0f} under it"
        elif (rule.get("floor_action") or "ceiling") == "ceiling" and ceiling:
            # can't win without going under the floor:
            #  1. go back up to the ceiling and let the other bot follow us up;
            #  2. if it hasn't come back up after 30 minutes, sit just above it (its price + step, never below the floor),
            #     ready to fight again as soon as it moves up past the floor.
            ph = (_tload().get("phase") or {}).get(sku) or {}
            phase = ph.get("p") or "fight"
            shadow = max(math.ceil(floor), math.floor(bb_price + step))
            if phase == "fight":
                if my_price < ceiling:
                    target, action, note = math.floor(ceiling), "reset", f"Competitor at R{bb_price:,.0f} is below your floor R{floor:,.0f}: back up to your ceiling"
                else:
                    note = f"Waiting at your ceiling for the competitor (R{bb_price:,.0f}) to come back up"
                new_phase = {"p": "wait", "since": _time.time()}
            elif phase == "wait":
                waited = (_time.time() - float(ph.get("since") or _time.time())) / 60
                if waited >= 30:
                    target, action, note = shadow, "follow", f"Competitor stayed at R{bb_price:,.0f} for 30 min: staying R{step:,.0f} above them"
                    new_phase = {"p": "follow", "since": _time.time()}
                else:
                    new_phase = ph
                    note = f"Waiting at your ceiling for the competitor (R{bb_price:,.0f}) to come back up ({int(waited)} of 30 min)"
            else:
                new_phase = ph
                if shadow != int(my_price):
                    target, action, note = shadow, "follow", f"Following: R{step:,.0f} above the competitor at R{bb_price:,.0f}"
                else:
                    note = f"Staying R{step:,.0f} above the competitor (R{bb_price:,.0f}), ready to fight when they go back up"
        elif my_price > floor:
            target, action, note = math.ceil(floor), "at_floor", f"Buy Box R{bb_price:,.0f} is below your floor R{floor:,.0f}"
        else:
            note = f"Buy Box R{bb_price:,.0f} is below your floor R{floor:,.0f}: holding at the floor"
        if target is not None and ceiling and target > ceiling:
            target = math.floor(ceiling)
    elif rule.get("raise_when_winning") and ceiling and my_price < ceiling and not waiting_live:
        lc = rule.get("last_change_at")
        quiet = True
        if lc:
            try:
                quiet = (datetime.utcnow() - datetime.fromisoformat(str(lc).replace("Z", "+00:00")).replace(tzinfo=None)).total_seconds() \
                        >= int(rule.get("raise_every_minutes") or 120) * 60
            except Exception:
                pass
        if quiet:
            cap = ceiling
            if live.get("other_min"):                          # never raise up to (or past) the next seller's price
                cap = min(cap, math.floor(live["other_min"] - step))
            if cap > my_price:
                target, action = math.floor(min(cap, my_price + step)), "raised"
    if target is None and ceiling and my_price > ceiling and not waiting_live:
        target, action, note = math.floor(ceiling), "lowered", f"Your price R{my_price:,.0f} was above your ceiling R{ceiling:,.0f}"
    if target is not None and floor is not None and target < math.ceil(floor):
        target, note = None, f"Your floor R{floor:,.0f} is above your ceiling R{(ceiling or 0):,.0f}: not changing the price (check the settings)"
    tmp = _tload()
    if (tmp.get("phase") or {}).get(sku) != new_phase and fight:
        tmp.setdefault("phase", {})[sku] = new_phase
        _tsave(tmp)
    upd = {"last_checked": nowiso, "bb_price": bb_price, "bb_seller": bb_seller, "winning": winning,
           "floor_used": floor, "my_price": my_price}
    cfgn = {"url": db.base.replace("/rest/v1/", ""), "key": db.h["apikey"]}
    alerts = []
    # three states: the Buy Box (Fastest Delivery), the Best Price box (cheapest, but on lead time), or neither
    state = "win" if winning else "best" if live.get("slot") == "lowest_priced" else "lost"
    tms = _tload()
    prev = (tms.get("bbstate") or {}).get(sku)
    if prev is None and rule.get("winning") is not None:
        prev = "win" if rule.get("winning") else "lost"
    if prev != state:
        tms.setdefault("bbstate", {})[sku] = state
        _tsave(tms)
        if prev is not None:
            if state == "win":
                alerts.append(("✅ You won the Buy Box", f"{title} · Buy Box R{bb_price:,.0f}"))
            elif state == "best":
                alerts.append(("🏷️ You have the Best Price box", f"{title} · you're the cheapest at R{my_price:,.0f}. "
                               f"The Fastest Delivery box is with a seller who has warehouse stock (R{bb_price:,.0f})."))
            else:
                alerts.append(("❌ You lost the Buy Box", f"{title} · Buy Box R{bb_price:,.0f} (you R{my_price:,.0f})"))
    if target is not None and int(target) != int(my_price):
        if not fight:
            action = "would_" + ("lower" if action in ("lowered", "at_floor") else "reset" if action == "reset" else "follow" if action == "follow" else "raise")
            upd["last_action"] = f"Would change R{my_price:,.0f} → R{target:,.0f} (watch mode)"
            if rule.get("last_action") != upd["last_action"]:          # log it once, not every check
                db.insert("price_log", [{"sku": sku, "my_price": my_price, "bb_price": bb_price, "bb_seller": bb_seller,
                                         "new_price": target, "action": action, "note": note or "watch mode"}])
        elif int(rule.get("max_changes_day") or 0) > 0 and changes >= int(rule["max_changes_day"]):
            upd["last_action"] = f"Daily limit of {rule.get('max_changes_day')} changes reached"
        elif not (auth or _creds_key()) or not offer_id:
            upd["last_action"] = "Not logged in to the Seller Portal and no API key: price not changed"
        elif ((_tload().get("refused") or {}).get(sku) or {}).get("price") == int(target) and \
                _time.time() - ((_tload().get("refused") or {}).get(sku) or {}).get("at", 0) < 600:
            pass                                            # Takealot refused this exact price < 10 min ago: don't hammer it
        else:
            key = _creds_key()
            sent_at = _time.time()
            if key:                                         # Takealot Seller API
                via = "API"
                r = ctx.request.patch(f"https://seller-api.takealot.com/v2/offers/offer/ID{offer_id}",
                                      headers={"Authorization": f"Key {key}", "Content-Type": "application/json"},
                                      data=json.dumps({"selling_price": int(target)}), timeout=30_000)
            else:                                           # logged-in Seller Portal
                via = "portal"
                r = ctx.request.patch(f"https://{API_HOST}/v2/offers/offer/{offer_id}", headers={**auth, "content-type": "application/json"},
                                      data=json.dumps({"selling_price": int(target)}), timeout=60_000)
            snipe_note = f"via {via}"
            errs = None
            try:
                errs = (r.json() or {}).get("validation_errors")
            except Exception:
                pass
            if r.ok and not errs:
                db.insert("price_log", [{"sku": sku, "my_price": my_price, "bb_price": bb_price, "bb_seller": bb_seller,
                                         "new_price": target, "action": action, "note": note}])
                upd.update(my_price=target, last_change_at=nowiso, last_action=(f"Back to ceiling R{target:,.0f} (competitor below your floor)" if action == "reset" else f"Following: R{target:,.0f}, just above the competitor (R{bb_price:,.0f})" if action == "follow" else f"{'Lowered' if action != 'raised' else 'Raised'} R{my_price:,.0f} → R{target:,.0f}"))
                st = dict(prod.get("takealot_stock") or {})
                st["OFFER"] = {**offer, "price": int(target)}
                db.patch("products", f"sku=eq.{sku}", {"takealot_stock": st})
                log(f"Price fighter: {title}: R{my_price:,.0f} → R{target:,.0f} (Buy Box R{bb_price:,.0f}, {'you' if winning else bb_seller}){' · ' + snipe_note if snipe_note else ''}.")
                tm = _tload()
                tm.setdefault("pending", {})[sku] = {"price": int(target), "sent": int(sent_at), "via": via}
                _tsave(tm)
                alerts.append((("🔺 Price raised" if action == "raised" else "🔻 Price lowered" if action == "lowered" else "↩️ Back up to your ceiling" if action == "reset" else "➡️ Staying just above the competitor" if action == "follow" else "🔻 Down to your floor"),
                               f"{title}: R{my_price:,.0f} → R{target:,.0f} · Buy Box was R{bb_price:,.0f}"))
            else:
                msg = (errs[0].get("message") if errs and isinstance(errs[0], dict) else errs) or r.text()[:150]
                db.insert("price_log", [{"sku": sku, "my_price": my_price, "bb_price": bb_price, "bb_seller": bb_seller,
                                         "new_price": target, "action": "failed", "note": str(msg)[:300]}])
                upd["last_action"] = f"Takealot refused the change: {str(msg)[:120]}"
                tmr = _tload()
                tmr.setdefault("refused", {})[sku] = {"price": int(target), "at": _time.time()}
                _tsave(tmr)
                if rule.get("last_action") != upd["last_action"]:
                    alerts.append(("⚠️ Price change refused", f"{title}: Takealot said {str(msg)[:100]}"))
    else:
        upd["last_action"] = note or ("You have the Buy Box" if winning else
                                      "Best Price slot: you're the cheapest; Takealot shows the seller with warehouse stock first (Fastest Delivery)"
                                      if live.get("slot") == "lowest_priced" else "Holding")
        tmf = _tload()
        below = bool(note and "below your floor" in note)
        if below != bool((tmf.get("below_floor") or {}).get(sku)):
            tmf.setdefault("below_floor", {})[sku] = below
            _tsave(tmf)
            if below:
                alerts.append(("⚠️ Competitor below your floor", f"{title}: {note}"))
    if _time.time() - ck.get("obs_at", 0) >= 600:              # price history for the chart: every 10 minutes
        ck["obs_at"] = _time.time()
        try:
            db.insert("price_log", [{"sku": sku, "my_price": upd.get("my_price"), "bb_price": bb_price, "bb_seller": bb_seller,
                                     "new_price": None, "action": "observe",
                                     "note": json.dumps({"other_min": live.get("other_min"), "slot": live.get("slot") or "", "win": winning})}])
        except Exception:
            pass
    bbsig = f"{bb_seller}:{bb_price}:{live.get('other_min')}"
    if ck.get("bbsig") not in (None, bbsig) or target is not None:
        _LAST_ACTIVE[0] = _time.time()                       # something moved: stay on fast checks
    ck["bbsig"] = bbsig
    sig = json.dumps({k: upd.get(k) for k in ("my_price", "bb_price", "bb_seller", "winning", "floor_used", "last_action", "last_change_at")}, default=str)
    if sig != ck.get("sig") or _time.time() - ck.get("saved_at", 0) > 60:     # only write when something changed (or once a minute)
        db.patch("price_rules", f"sku=eq.{sku}", upd)
        ck["sig"], ck["saved_at"] = sig, _time.time()
    if upd.get("last_change_at"):
        ck["changes_at"] = 0                                   # recount after a change
    # During a fast fight, won / lost / lowered / raised would fire every few seconds and the phone mutes the app.
    # Those are rolled into one update at most every 15 minutes; the important ones (floor, ceiling, refused) always go.
    routine = ("✅ You won", "❌ You lost", "🔺 Price raised", "🔻 Price lowered", "➡️")
    alerts = [(t, b) for t, b in alerts if not t.startswith(routine)]
    # time-based summary: how much of the last 15 minutes you held the Buy Box, sent when things are settled
    tm = _tload()
    st = tm.setdefault("held", {}).setdefault(sku, {"seen": 0, "win": 0, "tot": 0})
    st.setdefault("best", 0)
    nowt = _time.time()
    if st["seen"]:
        dt = min(60, nowt - st["seen"])
        st["tot"] += dt
        st["win"] += dt if winning else 0
        st["best"] += dt if (not winning and live.get("slot") == "lowest_priced") else 0
    st["seen"] = nowt
    last = (tm.get("last_routine") or {}).get(sku, 0)
    if not last:
        tm.setdefault("last_routine", {})[sku] = int(nowt)
    elif nowt - last >= 15 * 60 and target is None and not waiting_live and st["tot"] > 60:
        pct = round(100 * st["win"] / st["tot"])
        bpct = round(100 * st["best"] / st["tot"])
        try:
            n = len(db.select("price_log", f"select=id&sku=eq.{sku}&action=in.(lowered,raised)&at=gt.{(datetime.utcnow() - timedelta(minutes=15)).isoformat()}Z"))
        except Exception:
            n = 0
        # quiet when nothing happened: held the Buy Box the whole time, or sat in the Best Price box the whole time
        # (cheapest, on lead time: the 🏷️ message already said so once)
        if n or (pct < 100 and pct + bpct < 99):
            now_s = "yours" if winning else "Best Price box yours (Fastest Delivery with warehouse stock)" if live.get("slot") == "lowest_priced" else "competitor"
            head = (f"✅ Buy Box: you held it {pct}% of the time" if pct >= 50 else
                    f"🏷️ Buy Box {pct}%, Best Price box {bpct}% of the time" if bpct else
                    f"❌ Buy Box: you held it only {pct}% of the time")
            alerts.insert(0, (head, f"{title}: last 15 min · now {now_s} at R{bb_price:,.0f}" + (f" · {n} price changes" if n else "")))
            # (so the phone doesn't get 20 "100%" messages an hour and start muting the app)
        tm["last_routine"][sku] = int(nowt)
        st["win"], st["tot"], st["best"] = 0, 0, 0
    _tsave(tm)
    for t, b in alerts:
        try:
            notify(cfgn, t, b, url="./?view=price", topic="price", log=log)
        except Exception as e:
            log("Notification failed: " + repr(e)[:120])


def restart_if_updated(log):
    """On the server: stockrecon_sync.py itself only changes on a restart. If a newer one has arrived from GitHub,
    exit, and the server's service manager starts it again with the new code (on the PC nothing restarts it, so skip)."""
    import os, sys, stockrecon_sync as S
    if not sys.platform.startswith("linux"):
        return
    started = getattr(S, "_started_at", None)
    if started is None:
        S._started_at = started = __import__("time").time()
        return
    try:
        if (HERE / "stockrecon_sync.py").stat().st_mtime > started + 5:
            log("New version of the sync program arrived: restarting.")
            os._exit(0)
    except Exception:
        pass


def server_test(cfg, log):
    """Standby check on a new server: can it read the Buy Box, use the API key and read Gmail? Reported in the app."""
    try:                                                     # switched on from GitHub (sync/server_mode.txt = live)
        if (HERE / "server_mode.txt").read_text(encoding="utf-8").strip().lower() == "live" and (HERE / "standby.txt").exists():
            (HERE / "standby.txt").unlink()
            log("Server switched to LIVE: starting the sync and the price fighter.")
            restart_if_updated(log)
            import os
            os._exit(0)                                       # restart straight into the live sync (newest program)
    except FileNotFoundError:
        pass
    import socket, time as _t, stockrecon_sync as S
    from playwright.sync_api import sync_playwright
    out, ok = [], True
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(channel=cfg.get("browser") or "chrome", headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = b.new_context(user_agent=BROWSER_UA, locale="en-ZA", timezone_id="Africa/Johannesburg")
            pg = ctx.new_page()
            try:
                pg.goto("https://www.takealot.com/", wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass
            live = _read_live(pg, "95228805", "231380523", "M29853221")
            if live:
                out.append(f"Buy Box OK ({'you' if live['seller'] == 'M29853221' else 'competitor'} R{live['price']:,.0f})")
            else:
                ok = False
                out.append("Buy Box BLOCKED")
            key = _creds_key()
            if key:
                r = ctx.request.get(f"https://{API_HOST}/v2/offers/offer/ID231380523", headers={"Authorization": f"Key {key}"}, timeout=30_000)
                out.append(f"API key {'OK' if r.ok else 'failed ' + str(r.status)}")
                ok = ok and r.ok
            b.close()
    except Exception as e:
        ok = False
        out.append("browser: " + repr(e)[:150])
    g = gmail_otp(_t.time() - 30 * 86400, log, peek=True)
    out.append("Gmail OK" if g else "Gmail not readable")
    ok = ok and bool(g)
    msg = f"[server test {socket.gethostname()}] " + " · ".join(out)
    log(msg)
    try:
        S.DB(cfg["url"], cfg["key"]).insert("sync_runs", [{"message": msg[:300], "ok": ok, "finished_at": datetime.utcnow().isoformat() + "Z"}])
    except Exception as e:
        log("Server test: couldn't report to the app: " + repr(e)[:150])


def test_logins(p, cfg, log):
    """One-off check (debug/login_test_request.txt): log in with the config.ini details in a fresh, separate browser,
    so the sync's own session is left alone."""
    req = DEBUG_DIR / "login_test_request.txt"
    if not req.exists():
        return
    req.unlink()
    browser = p.chromium.launch(channel=cfg.get("browser") or "chrome", headless=True,
                                args=["--disable-blink-features=AutomationControlled"])
    try:
        for site, url, ok in (
            ("takealot", SALES_URL, lambda pg: "/login" not in pg.url and "sellers.takealot.com" in pg.url),
            ("courier", COURIER_URL, lambda pg: "thecourierguy" in pg.url and "login" not in pg.url.lower()
                                                 and pg.locator("input[type=password]").count() == 0)):
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(4000)
                if ok(page) and not page.locator("input[type=password]").count():
                    log(f"Login test ({site}): opened without a login page, nothing to test.")
                elif not _creds(site)[0]:
                    log(f"Login test ({site}): no email/password in config.ini.")
                else:
                    good = auto_login(page, site, log, ok, cfg=cfg)
                    log(f"Login test ({site}): {'PASSED' if good else 'FAILED'}.")
            except Exception as e:
                log(f"Login test ({site}) error: {repr(e)[:200]}")
            finally:
                ctx.close()
    finally:
        browser.close()


def _creds_key():
    """The API key from config.ini; re-read only when the file changes (the repricer asks many times a minute)."""
    f = HERE / "config.ini"
    try:
        mt = f.stat().st_mtime
    except OSError:
        return ""
    c0 = getattr(_creds_key, "_c", None)
    if c0 and c0[0] == mt:
        return c0[1]
    k = _creds_key_read()
    _creds_key._c = (mt, k)
    return k


def _creds_key_read():
    import configparser
    cp = configparser.ConfigParser()
    cp.read(HERE / "config.ini", encoding="utf-8")
    c = cp["stockrecon"] if "stockrecon" in cp else {}
    k = (c.get("takealot_api_key") or "").strip()
    return "" if "PASTE" in k.upper() else k


def auto_login(page, site, log, ok_check, timeout_s=40, cfg=None):
    """Fill in the portal's own login form with the details from config.ini. Returns True once logged in."""
    email, pw = _creds(site)
    name = {"takealot": "Takealot", "courier": "Courier Guy"}[site]
    if not email:
        return False
    try:
        page.wait_for_selector("input[type=password]", timeout=20_000)
        user = page.locator("input[type=email], input[name*=email i], input[name*=user i], input[id*=email i], input[id*=user i]").first
        user.fill(email)
        page.locator("input[type=password]").first.fill(pw)
        btn = page.locator("button[type=submit], input[type=submit]")
        if btn.count():
            btn.first.click()
        else:
            page.locator("input[type=password]").first.press("Enter")
        deadline = datetime.now() + timedelta(seconds=timeout_s)
        while datetime.now() < deadline:
            page.wait_for_timeout(1000)
            if ok_check(page):
                log(f"{name}: logged back in automatically.")
                return True
            if cfg and _otp_inputs(page):
                if _enter_otp(page, cfg, log, name):
                    for _ in range(30):
                        page.wait_for_timeout(1000)
                        if ok_check(page):
                            log(f"{name}: logged back in with the PIN from the app.")
                            return True
                break
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"{site}_login_failed.png"))
        body = (page.inner_text("body") or "")[:400].replace("\n", " ")
        why = ("it asked for a code" if page.locator("input[autocomplete=one-time-code], input[name*=otp i], input[name*=code i]").count()
               else "it didn't accept the email or password" if any(w in body.lower() for w in ("invalid", "incorrect", "wrong"))
               else "it didn't finish")
        log(f"{name}: automatic login failed ({why}). Page said: {body[:200]}")
    except Exception as e:
        log(f"{name}: automatic login failed: {repr(e)[:200]}")
    return False


def launch(p, cfg, headless, profile_dir: Path, downloads: Path):
    profile_dir.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(exist_ok=True)
    return p.chromium.launch_persistent_context(
        str(profile_dir), channel=cfg["browser"], headless=headless, accept_downloads=True,
        downloads_path=str(downloads), viewport={"width": 1400, "height": 900},
        args=["--disable-blink-features=AutomationControlled"])


def _find_rows(obj):
    """Find the first list of records in a JSON answer that carry an order id."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and any("order" in k.lower() and "id" in k.lower() for k in obj[0]):
            return obj
        for x in obj:
            r = _find_rows(x)
            if r is not None:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = _find_rows(v)
            if r is not None:
                return r
    return None


def _find_list(obj, key):
    """Find the first list of records that have the given field (or all of a tuple of fields)."""
    keys = key if isinstance(key, tuple) else (key,)
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and all(k in obj[0] for k in keys):
            return obj
        for x in obj:
            r = _find_list(x, key)
            if r is not None:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = _find_list(v, key)
            if r is not None:
                return r
    return None


def _total(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("total", "total_count", "count") and isinstance(v, int):
                return v
            t = _total(v) if isinstance(v, dict) else None
            if t:
                return t
    return None


def _paged(url, size, number):
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    u = urlsplit(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    for k in list(q):
        if k in ("page_size", "pageSize", "limit"):
            q[k] = str(size)
        if k in ("page_number", "page", "pageNumber"):
            q[k] = str(number)
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q, safe=":,"), u.fragment))


def fetch_all(cfg, headless, profile_dir: Path, downloads: Path, log):
    """Returns (sales_rows, returns_rows). Raises NeedsLogin when the portal asks for a login."""
    from playwright.sync_api import sync_playwright
    auth = {}
    returns_calls = []   # (url, json)
    offer_calls = []     # (url, json)
    phase = {"now": "sales"}

    def on_request(req):
        if API_HOST in req.url and not auth:
            h = {k: v for k, v in req.headers.items()
                 if k.lower() in ("authorization", "x-api-key", "x-csrftoken", "x-xsrf-token", "accept")}
            if h:
                auth.update(h)

    def on_response(r):
        try:
            if API_HOST in r.url and r.request.method == "GET" and "json" in (r.headers.get("content-type") or ""):
                if phase["now"] == "offers":
                    offer_calls.append((r.url, r.json()))
                elif "return" in r.url.lower():
                    returns_calls.append((r.url, r.json()))
        except Exception:
            pass

    with sync_playwright() as p:
        try:
            test_logins(p, cfg, log)
        except Exception as e:
            log("Login test couldn't run: " + repr(e)[:200])
        try:
            monthly_report(p, cfg, log)
        except Exception as e:
            log("Monthly report couldn't be made: " + repr(e)[:200])
        ctx = launch(p, cfg, headless, profile_dir, downloads)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("request", on_request)
            page.on("response", on_response)

            # ---- sales
            page.goto(SALES_URL, wait_until="domcontentloaded", timeout=90_000)
            for _ in range(40):
                if auth or "/login" in page.url:
                    break
                page.wait_for_timeout(500)
            if "/login" in page.url:
                if not auto_login(page, "takealot", log, cfg=cfg, ok_check=lambda pg: "/login" not in pg.url and "sellers.takealot.com" in pg.url):
                    raise NeedsLogin()
                auth.clear()
                page.goto(SALES_URL, wait_until="domcontentloaded", timeout=90_000)
                for _ in range(40):
                    if auth or "/login" in page.url:
                        break
                    page.wait_for_timeout(500)
                if "/login" in page.url:
                    raise NeedsLogin()
            if not auth:
                raise RuntimeError("The sales page didn't load its data.")
            start = max(date.fromisoformat(cfg["since"]), date.today() - timedelta(days=90))
            end = date.today()
            sales, pg = [], 1
            while True:
                url = f"{SALES_API}?filters=start_date%3A{start}%2Cend_date%3A{end}&page_size=100&page_number={pg}"
                r = ctx.request.get(url, headers=auth, timeout=60_000)
                if r.status in (401, 403):
                    raise NeedsLogin()
                if not r.ok:
                    raise RuntimeError(f"Takealot's sales feed answered {r.status}: {r.text()[:200]}")
                data = r.json()
                batch = data.get("sales") or []
                sales += batch
                total = (data.get("page_summary") or {}).get("total")
                if not batch or len(batch) < 100 or (total and len(sales) >= total) or pg >= 100:
                    break
                pg += 1
            try:
                sale_alerts(cfg, sales, log, full=True)         # any sale the 2-minute watch didn't catch
            except Exception as e:
                log("Sale notifications failed: " + repr(e)[:150])

            # ---- Takealot's monthly Stock Recon report (missing stock to claim)
            try:
                takealot_recon(ctx, cfg, auth, log)
            except Exception as e:
                log("Takealot stock recon couldn't be read: " + repr(e)[:200])

            # ---- one-off: try the Takealot Seller API key (debug/api_probe.txt)
            try:
                if (DEBUG_DIR / "api_probe.txt").exists():
                    (DEBUG_DIR / "api_probe.txt").unlink()
                    key = _creds_key()
                    out = {}
                    oid = "231380523"
                    for name, url, hdr in (
                        ("v2_offer_ID", f"https://seller-api.takealot.com/v2/offers/offer/ID{oid}", {"Authorization": f"Key {key}"}),
                        ("v2_offer_plain", f"https://seller-api.takealot.com/v2/offers/offer/{oid}", {"Authorization": f"Key {key}"}),
                        ("v2_offers", "https://seller-api.takealot.com/v2/offers?page_number=1&page_size=2", {"Authorization": f"Key {key}"}),
                        ("mkt_offers_key", "https://marketplace-api.takealot.com/v1/offers?limit=2", {"Authorization": f"Key {key}"}),
                        ("mkt_offers_bearer", "https://marketplace-api.takealot.com/v1/offers?limit=2", {"Authorization": f"Bearer {key}"}),
                        ("mkt_offers_xkey", "https://marketplace-api.takealot.com/v1/offers?limit=2", {"X-API-Key": key}),
                        ("mkt_docs", "https://marketplace-api.takealot.com/v1/openapi.json", {}),
                    ):
                        try:
                            r = ctx.request.get(url, headers=hdr, timeout=30_000)
                            out[name] = {"status": r.status, "body": r.text()[:6000],
                                         "headers": {k: v for k, v in r.headers.items() if k.lower().startswith("x-ratelimit")}}
                        except Exception as e:
                            out[name] = {"error": repr(e)[:200]}
                    (DEBUG_DIR / "api_probe.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
                    log("API probe saved.")
            except Exception as e:
                log("API probe failed: " + repr(e)[:200])

            # ---- one-off: search the Seller Portal's code for keywords (debug/bundle_search.txt, one keyword per line)
            try:
                if (DEBUG_DIR / "bundle_search.txt").exists():
                    keys = [k.strip() for k in (DEBUG_DIR / "bundle_search.txt").read_text(encoding="utf-8").splitlines() if k.strip()]
                    (DEBUG_DIR / "bundle_search.txt").unlink()
                    import re as _rb
                    home = ctx.request.get("https://sellers.takealot.com/", timeout=60_000).text()
                    mains = _rb.findall(r'/assets/(main-[A-Za-z0-9_-]+\.js)', home)
                    js = ""
                    for mn in mains[:3]:
                        js += ctx.request.get(f"https://sellers.takealot.com/assets/{mn}", timeout=60_000).text()
                    chunks = sorted(set(_rb.findall(r'assets/([A-Za-z0-9_.-]+\.js)', js)))
                    for c in chunks[:600]:
                        try:
                            js += "\n/*" + c + "*/\n" + ctx.request.get(f"https://sellers.takealot.com/assets/{c}", timeout=30_000).text()
                        except Exception:
                            pass
                    segs = [{"key": "_chunks", "text": f"{len(chunks)} chunks, {len(js)} chars"}]
                    for key in keys:
                        for m in list(_rb.finditer(_rb.escape(key), js))[:6]:
                            segs.append({"key": key, "text": js[max(0, m.start() - 700): m.start() + 700]})
                    (DEBUG_DIR / "bundle_search.json").write_text(json.dumps(segs, indent=1), encoding="utf-8")
                    log(f"Portal code search: {len(segs)} pieces saved.")
            except Exception as e:
                log("Portal code search failed: " + repr(e)[:200])

            # ---- one-off: read how the portal asks for a report (debug/recon_discover3.txt)
            try:
                if (DEBUG_DIR / "recon_discover3.txt").exists():
                    (DEBUG_DIR / "recon_discover3.txt").unlink()
                    import re as _re
                    import re as _re2
                    home = ctx.request.get("https://sellers.takealot.com/", timeout=60_000).text()
                    mains = _re2.findall(r'/assets/(main-[A-Za-z0-9_-]+\.js)', home) or ["main-DS7F8Hpa.js"]
                    js = ctx.request.get(f"https://sellers.takealot.com/assets/{mains[0]}", timeout=60_000).text()
                    chunks = sorted(set(_re2.findall(r'assets/([A-Za-z0-9_.-]+\.js)', js)))
                    for c in chunks[:400]:
                        try:
                            js += "\n/*" + c + "*/\n" + ctx.request.get(f"https://sellers.takealot.com/assets/{c}", timeout=30_000).text()
                        except Exception:
                            pass
                    segs = [{"key": "chunks", "text": " ".join(chunks)[:3000]}]
                    for key in ("buybox", "buy_box", "BuyBox", "buyBox", "competitor", "lowest_price", "lowestPrice", "winning_price", "competitive"):
                        for m in list(_re.finditer(_re.escape(key), js))[:4]:
                            segs.append({"key": key, "text": js[max(0, m.start() - 900): m.start() + 600]})
                    (DEBUG_DIR / "recon_discovery3.json").write_text(json.dumps(segs, indent=1), encoding="utf-8")
                    log(f"Stock recon discovery 3: {len(segs)} pieces saved.")
            except Exception as e:
                log("Stock recon discovery 3 failed: " + repr(e)[:200])

            # ---- one-off: record how a Stock Recon report row downloads, and what the request form looks like
            try:
                if (DEBUG_DIR / "recon_discover2.txt").exists():
                    (DEBUG_DIR / "recon_discover2.txt").unlink()
                    calls, out = [], {}
                    def lis2(r):
                        try:
                            if "takealot" in r.url and r.request.resource_type in ("xhr", "fetch", "document", "other"):
                                ct = r.headers.get("content-type", "")
                                calls.append({"url": r.url, "method": r.request.method, "status": r.status, "type": ct,
                                              "post": (r.request.post_data or "")[:1000],
                                              "body": r.text()[:1500] if ("json" in ct or "text" in ct) else ""})
                        except Exception:
                            pass
                    page.on("response", lis2)
                    page.goto("https://sellers.takealot.com/reports", wait_until="domcontentloaded", timeout=90_000)
                    page.wait_for_timeout(7000)
                    row = page.locator("tr", has_text="Stock Recon").first
                    out["row_html"] = row.inner_html()[:4000]
                    calls.clear()
                    try:
                        with page.expect_download(timeout=30_000) as dl:
                            row.locator("a, button, [role=button], svg").last.click()
                        d = dl.value
                        dest = HERE / "reports" / ("takealot_" + d.suggested_filename)
                        dest.parent.mkdir(exist_ok=True)
                        d.save_as(str(dest))
                        out["download"] = {"file": str(dest), "url": d.url}
                    except Exception as e:
                        out["download_error"] = repr(e)[:300]
                    page.wait_for_timeout(3000)
                    out["download_calls"] = list(calls)
                    # the request form: pick "Stock Recon" in the report type list, don't submit
                    calls.clear()
                    try:
                        sel = page.locator("select").first
                        if sel.count():
                            sel.select_option(label="Stock Recon")
                        else:
                            page.get_by_text("Select", exact=True).first.click(); page.wait_for_timeout(800)
                            page.get_by_text("Stock Recon", exact=True).last.click()
                        page.wait_for_timeout(2500)
                        page.screenshot(path=str(DEBUG_DIR / "recon_form.png"))
                        form = page.locator("form").first
                        out["form_html"] = (form.inner_html() if form.count() else page.locator("main").first.inner_html())[:8000]
                    except Exception as e:
                        out["form_error"] = repr(e)[:300]
                    out["form_calls"] = list(calls)
                    # the page's script files, to find how a report is requested
                    out["scripts"] = page.evaluate("() => [...document.scripts].map(s => s.src).filter(Boolean)")
                    page.remove_listener("response", lis2)
                    (DEBUG_DIR / "recon_discovery2.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
                    log("Stock recon discovery 2 saved.")
            except Exception as e:
                log("Stock recon discovery 2 failed: " + repr(e)[:200])

            # ---- one-off discovery of Takealot's stock recon report (debug/recon_discover.txt)
            try:
                if (DEBUG_DIR / "recon_discover.txt").exists():
                    (DEBUG_DIR / "recon_discover.txt").unlink()
                    found = {"links": [], "pages": []}
                    calls = []
                    def lis(r):
                        try:
                            if ("takealot" in r.url) and r.request.resource_type in ("xhr", "fetch"):
                                ct = r.headers.get("content-type", "")
                                calls.append({"url": r.url, "status": r.status, "type": ct,
                                              "body": r.text()[:3000] if "json" in ct else ""})
                        except Exception:
                            pass
                    page.on("response", lis)
                    page.goto("https://sellers.takealot.com/", wait_until="domcontentloaded", timeout=90_000)
                    page.wait_for_timeout(6000)
                    # open every collapsible menu entry so hidden links render
                    for el in page.locator("nav [aria-expanded=false], [class*=menu] [aria-expanded=false]").all()[:40]:
                        try:
                            el.click(timeout=1500); page.wait_for_timeout(200)
                        except Exception:
                            pass
                    links = page.evaluate("""() => [...document.querySelectorAll('a')].map(a => ({href: a.href, text: (a.innerText||'').trim().slice(0,80)}))""")
                    found["links"] = links
                    found["home_text"] = page.inner_text("body")[:4000]
                    cands = []
                    for l in links:
                        t = (l["href"] + " " + l["text"]).lower()
                        if any(w in t for w in ("recon", "report", "inventory", "stock", "statement", "document", "download")) \
                                and l["href"].startswith("https://sellers.takealot.com") and l["href"] not in cands:
                            cands.append(l["href"])
                    for i, u in enumerate(cands[:15]):
                        calls.clear()
                        try:
                            page.goto(u, wait_until="domcontentloaded", timeout=60_000)
                            page.wait_for_timeout(6000)
                            page.screenshot(path=str(DEBUG_DIR / f"recon_page_{i}.png"))
                            found["pages"].append({"url": u, "text": page.inner_text("body")[:5000], "calls": list(calls)[:40]})
                        except Exception as e:
                            found["pages"].append({"url": u, "error": repr(e)[:200]})
                    page.remove_listener("response", lis)
                    DEBUG_DIR.mkdir(exist_ok=True)
                    (DEBUG_DIR / "recon_discovery.json").write_text(json.dumps(found, indent=1, default=str), encoding="utf-8")
                    log(f"Stock recon discovery: {len(links)} links, {len(cands)} candidate pages saved.")
            except Exception as e:
                log("Stock recon discovery failed: " + repr(e)[:200])

            # ---- one-off history request (a history_request.txt file with a start date, e.g. 2026-08-01)
            try:
                req = HERE / "history_request.txt"
                if req.exists():
                    since = date.fromisoformat(req.read_text(encoding="utf-8").strip()[:10])
                    hist, a = [], since
                    while a <= date.today():
                        b = min(a + timedelta(days=29), date.today())
                        pg = 1
                        while True:
                            url = f"{SALES_API}?filters=start_date%3A{a}%2Cend_date%3A{b}&page_size=100&page_number={pg}"
                            r = ctx.request.get(url, headers=auth, timeout=60_000)
                            if not r.ok:
                                break
                            batch = r.json().get("sales") or []
                            hist += [{k: v for k, v in x.items() if k != "customer"} for x in batch]
                            if len(batch) < 100:
                                break
                            pg += 1
                        a = b + timedelta(days=1)
                    rets = []
                    rr = ctx.request.get(f"https://{API_HOST}/v2/returns?page_number=1&page_size=100&date_start={since}&date_end={date.today()}",
                                         headers=auth, timeout=60_000)
                    if rr.ok:
                        rets = _find_rows(rr.json()) or []
                    DEBUG_DIR.mkdir(exist_ok=True)
                    (DEBUG_DIR / "sales_history.json").write_text(json.dumps({"since": str(since), "sales": hist, "returns": rets,
                        "saved": datetime.now().isoformat()}, indent=1, default=str), encoding="utf-8")
                    req.unlink()
                    log(f"History: saved {len(hist)} sale lines and {len(rets)} return(s) since {since}.")
            except Exception as e:
                log("History request failed: " + repr(e)[:200])

            # ---- one-off: removal order details (debug/removal_discover.txt)
            try:
                if (DEBUG_DIR / "removal_discover.txt").exists():
                    (DEBUG_DIR / "removal_discover.txt").unlink()
                    calls, out = [], {"pages": []}
                    def lis4(r):
                        try:
                            if "seller-api" in r.url and r.request.resource_type in ("xhr", "fetch"):
                                ct = r.headers.get("content-type", "")
                                calls.append({"url": r.url, "status": r.status, "body": r.text()[:5000] if "json" in ct else ""})
                        except Exception:
                            pass
                    ctx.on("response", lis4)
                    for u in ("https://sellers.takealot.com/shipments/removal-orders/ready-for-pickup",
                              "https://sellers.takealot.com/shipments/removal-orders/submitted/2318514"):
                        calls.clear()
                        page.goto(u, wait_until="domcontentloaded", timeout=90_000)
                        page.wait_for_timeout(7000)
                        try:
                            el = page.get_by_text("2318514").first
                            if el.count() and "submitted/2318514" not in u:
                                el.click(timeout=5000); page.wait_for_timeout(5000)
                        except Exception:
                            pass
                        out["pages"].append({"url": u, "now": page.url, "text": page.inner_text("body")[:5000], "calls": list(calls)})
                    ctx.remove_listener("response", lis4)
                    (DEBUG_DIR / "removal_discovery.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
                    log("Removal order discovery saved.")
            except Exception as e:
                log("Removal order discovery failed: " + repr(e)[:200])

            # ---- one-off: how a return's details (customer comment) load (debug/return_discover.txt)
            # ---- one-off: check the Gmail PIN reader works (debug/otp_mail_test.txt); never logs the PIN itself
            try:
                if (DEBUG_DIR / "otp_mail_test.txt").exists():
                    (DEBUG_DIR / "otp_mail_test.txt").unlink()
                    log("Gmail PIN reader: " + (gmail_otp(__import__("time").time() - 30 * 86400, log, peek=True) or "no Takealot PIN mail found in the last 30 days (or Gmail login failed)"))
            except Exception as e:
                log("Gmail PIN reader test failed: " + repr(e)[:200])
            # ---- one-off: dump our sales + units for checking against the transactions (debug/sales_dump.txt)
            try:
                if (DEBUG_DIR / "sales_dump.txt").exists():
                    (DEBUG_DIR / "sales_dump.txt").unlink()
                    import stockrecon_sync as S
                    dbx = S.DB(cfg["url"], cfg["key"])
                    out = {}
                    for t, q in (("takealot_sales", "select=*"), ("units", "select=id,product,sku,status,order_id,cost,gross,fees,sold_date,location,source"),
                                 ("takealot_returns", "select=return_id,rrn,order_id,sku,dc,reason,outcome,return_date")):
                        rows, off = [], 0
                        while True:
                            b = dbx.select(t, f"{q}&limit=1000&offset={off}")
                            rows += b
                            if len(b) < 1000:
                                break
                            off += 1000
                        for r in rows:
                            r.pop("raw", None)
                        out[t] = rows
                    (DEBUG_DIR / "sales_dump.json").write_text(json.dumps(out, default=str), encoding="utf-8")
                    log("Sales dump saved.")
            except Exception as e:
                log("Sales dump failed: " + repr(e)[:200])
            # ---- one-off: all transactions of the last 90 days (debug/txn_discover2.txt)
            try:
                if (DEBUG_DIR / "txn_discover2.txt").exists():
                    days = (DEBUG_DIR / "txn_discover2.txt").read_text(encoding="utf-8").strip()
                    days = int(days) if days.isdigit() else 90
                    (DEBUG_DIR / "txn_discover2.txt").unlink()
                    d_to = datetime.now().date()
                    d_from = d_to - timedelta(days=days)
                    allt, pn, info = [], 1, {}
                    while pn < 100:
                        r = ctx.request.get(f"https://{API_HOST}/v1/seller/transactions?date_from={d_from}&date_to={d_to}&page_size=100&page_number={pn}",
                                            headers=auth, timeout=60_000)
                        info[f"p{pn}"] = r.status
                        if not r.ok:
                            info["err"] = r.text()[:500]
                            break
                        j = r.json()
                        allt += j.get("transactions") or []
                        if len(allt) >= (j.get("page_summary") or {}).get("total", 0) or not j.get("transactions"):
                            break
                        pn += 1
                    (DEBUG_DIR / "txn_all.json").write_text(json.dumps({"info": info, "from": str(d_from), "to": str(d_to), "transactions": allt}, indent=1), encoding="utf-8")
                    log(f"Transactions discovery: {len(allt)} rows saved.")
            except Exception as e:
                log("Transactions discovery 2 failed: " + repr(e)[:200])
            # ---- one-off: the Seller Portal transactions page (debug/txn_discover.txt)
            try:
                if (DEBUG_DIR / "txn_discover.txt").exists():
                    (DEBUG_DIR / "txn_discover.txt").unlink()
                    calls, out = [], {}
                    def lis6(r):
                        try:
                            if "seller-api" in r.url and r.request.resource_type in ("xhr", "fetch"):
                                ct = r.headers.get("content-type", "")
                                calls.append({"url": r.url, "status": r.status, "body": r.text()[:40000] if "json" in ct else ""})
                        except Exception:
                            pass
                    page.on("response", lis6)
                    page.goto("https://sellers.takealot.com/", wait_until="domcontentloaded", timeout=90_000)
                    page.wait_for_timeout(6000)
                    out["links"] = page.eval_on_selector_all("a[href]", "els => els.map(e => [e.innerText.trim().slice(0,40), e.getAttribute('href')])")[:300]
                    hrefs = [h for t, h in out["links"] if h and ("transaction" in h.lower() or "transaction" in (t or "").lower())]
                    tried = []
                    for u in hrefs[:1] + ["/finance/transactions", "/account/transactions", "/transactions", "/payments/transactions"]:
                        url = u if u.startswith("http") else "https://sellers.takealot.com" + u
                        calls.clear()
                        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                        page.wait_for_timeout(9000)
                        txt = page.inner_text("body")
                        tried.append({"url": url, "final": page.url, "len": len(txt)})
                        if "isbursement" in txt or "ransaction" in txt[:3000]:
                            break
                    out["tried"] = tried
                    out["url"] = page.url
                    page.set_viewport_size({"width": 1600, "height": 1200})
                    page.wait_for_timeout(1500)
                    page.screenshot(path=str(DEBUG_DIR / "txn_page.png"), full_page=True)
                    out["page_text"] = page.inner_text("body")[:20000]
                    out["calls"] = list(calls)
                    page.remove_listener("response", lis6)
                    (DEBUG_DIR / "txn_discovery.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
                    log("Transactions page discovery saved.")
            except Exception as e:
                log("Transactions page discovery failed: " + repr(e)[:200])
            try:
                if (DEBUG_DIR / "return_discover.txt").exists():
                    (DEBUG_DIR / "return_discover.txt").unlink()
                    calls, out = [], {}
                    def lis3(r):
                        try:
                            if "seller-api" in r.url and r.request.resource_type in ("xhr", "fetch"):
                                ct = r.headers.get("content-type", "")
                                calls.append({"url": r.url, "status": r.status, "body": r.text()[:4000] if "json" in ct else ""})
                        except Exception:
                            pass
                    page.on("response", lis3)
                    page.goto(RETURNS_URL, wait_until="domcontentloaded", timeout=90_000)
                    page.wait_for_timeout(7000)
                    calls.clear()
                    row = page.locator("tr", has_text="225135004").first
                    if not row.count():
                        row = page.locator("tbody tr").first
                    out["row_text"] = row.inner_text()[:500]
                    out["row_html"] = row.inner_html()[:3000]
                    try:
                        row.get_by_text("RRN-BZYN7-KXBV").first.click(timeout=8000)
                    except Exception as e:
                        out["click_error"] = repr(e)[:200]
                    page.wait_for_timeout(5000)
                    page.screenshot(path=str(DEBUG_DIR / "return_detail.png"))
                    out["page_text"] = page.inner_text("body")[:6000]
                    out["calls"] = list(calls)
                    page.remove_listener("response", lis3)
                    (DEBUG_DIR / "return_discovery.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
                    log("Return details discovery saved.")
            except Exception as e:
                log("Return details discovery failed: " + repr(e)[:200])

            # ---- returns (best effort: a problem here never stops the sales sync)
            returns = []
            try:
                page.goto(RETURNS_URL, wait_until="domcontentloaded", timeout=90_000)
                for _ in range(40):
                    if any(_find_rows(j) is not None for _, j in returns_calls):
                        break
                    page.wait_for_timeout(500)
                page.wait_for_timeout(1500)
                best = None
                for url, j in returns_calls:
                    rows = _find_rows(j)
                    if rows is not None and (best is None or len(rows) > len(best[1])):
                        best = (url, rows, j)
                if best:
                    url, rows, j = best
                    returns = list(rows)
                    total = _total(j)
                    if total and total > len(returns):
                        returns, n = [], 1
                        while len(returns) < total and n <= 50:
                            rr = ctx.request.get(_paged(url, 100, n), headers=auth, timeout=60_000)
                            if not rr.ok:
                                break
                            got = _find_rows(rr.json()) or []
                            if not got:
                                break
                            returns += got
                            n += 1
                    DEBUG_DIR.mkdir(exist_ok=True)
                    (DEBUG_DIR / "returns_feed.json").write_text(json.dumps({
                        "url": url, "count": len(returns), "keys": sorted(returns[0].keys()) if returns else [],
                        "sample": [{k: v for k, v in x.items() if "customer" not in k.lower()} for x in returns[:3]],
                        "saved": datetime.now().isoformat()}, indent=1, default=str), encoding="utf-8")
                    save_returns(cfg, returns, log, ctx=ctx, auth=auth)
                else:
                    log("Returns page: no returns data found.")
            except NeedsLogin:
                raise
            except Exception as e:
                log("Returns page couldn't be read: " + repr(e)[:200])
            # ---- offers (best effort, saved for reference: names, SKUs, stock at Takealot)
            try:
                global LAST_OFFERS
                offers, n, filt = [], 1, ""   # no status filter: new listings (e.g. pending / not buyable) come through too
                while n <= 50:
                    url = (f"https://{API_HOST}/v2/offers/detailed?page_number={n}&page_size=100"
                           f"&sort_key=date_added&sort_dir=asc{filt}")
                    rr = ctx.request.get(url, headers=auth, timeout=60_000)
                    if not rr.ok and not filt and n == 1:
                        filt = "&status_ids=1&status_ids=2&status_ids=3"
                        log(f"Offers feed answered {rr.status} without a status filter; using the old filter")
                        continue
                    if not rr.ok:
                        log(f"Offers feed answered {rr.status}")
                        break
                    got = (rr.json() or {}).get("offers") or []
                    offers += got
                    if len(got) < 100:
                        break
                    n += 1
                LAST_OFFERS = offers
                DEBUG_DIR.mkdir(exist_ok=True)
                (DEBUG_DIR / "offers.json").write_text(json.dumps({"count": len(offers), "offers": offers,
                    "saved": datetime.now().isoformat()}, indent=1, default=str), encoding="utf-8")
                log(f"Read {len(offers)} offer(s) from Takealot.")
                save_offer_stock(cfg, offers, log)
            except Exception as e:
                log("Offers couldn't be read: " + repr(e)[:200])
            # ---- one-off: what the public Takealot product page says about prices / other sellers (debug/price_discover.txt)
            try:
                if (DEBUG_DIR / "price_discover.txt").exists():
                    plid = (DEBUG_DIR / "price_discover.txt").read_text(encoding="utf-8").strip() or "100972862"
                    if not plid.isdigit():
                        plid = "100972862"
                    (DEBUG_DIR / "price_discover.txt").unlink()
                    out = {}
                    for name, u in (("details11", f"https://api.takealot.com/rest/v-1-11-0/product-details/PLID{plid}?platform=desktop&display_credit=false"),
                                    ("details10", f"https://api.takealot.com/rest/v-1-10-0/product-details/PLID{plid}?platform=desktop"),
                                    ("offers11", f"https://api.takealot.com/rest/v-1-11-0/product-details/PLID{plid}/offers?platform=desktop"),
                                    ("offers_ol", f"https://api.takealot.com/rest/v-1-11-0/product-details/PLID{plid}/other-offers?platform=desktop")):
                        try:
                            r = ctx.request.get(u, timeout=30_000)
                            out[name] = {"status": r.status, "body": r.text()[:60000]}
                        except Exception as e:
                            out[name] = {"error": repr(e)[:200]}
                    # the public page itself: record the data calls it makes
                    calls = []
                    def lis5(r):
                        try:
                            if "api.takealot.com" in r.url and "json" in (r.headers.get("content-type") or ""):
                                calls.append({"url": r.url, "status": r.status, "body": r.text()[:30000]})
                        except Exception:
                            pass
                    pg2 = ctx.new_page()
                    pg2.on("response", lis5)
                    pg2.goto(f"https://www.takealot.com/x/PLID{plid}", wait_until="domcontentloaded", timeout=90_000)
                    pg2.wait_for_timeout(8000)
                    import re as _re
                    out["calls_before_click"] = len(calls)
                    try:
                        pg2.get_by_text(_re.compile(r"other (offers|sellers)", _re.I)).first.click(timeout=6000); pg2.wait_for_timeout(6000)
                        out["clicked"] = True
                    except Exception as e:
                        out["click_error"] = repr(e)[:150]
                    out["page_text"] = pg2.inner_text("body")[:6000]
                    out["page_calls"] = calls
                    pg2.close()
                    (DEBUG_DIR / "price_discovery.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
                    log("Price discovery saved.")
            except Exception as e:
                log("Price discovery failed: " + repr(e)[:200])

            # ---- lead-time stock changes asked for in the app (leadtime_requests) or via debug/set_leadtime.json
            try:
                recent = []                               # orders that came in while this sync was running
                try:
                    rr = ctx.request.get(f"{SALES_API}?filters=start_date%3A{date.today() - timedelta(days=1)}%2Cend_date%3A{date.today()}&page_size=100&page_number=1",
                                         headers=auth, timeout=60_000)
                    if rr.ok:
                        recent = (rr.json() or {}).get("sales") or []
                except Exception:
                    pass
                apply_leadtime_requests(ctx, cfg, auth, offers, log, list(sales) + recent)
            except Exception as e:
                log("Lead-time stock change failed: " + repr(e)[:200])
            # ---- FNB bank statement from Gmail: business costs into Salaries & expenses (debug/bank_dry_run.txt = test only)
            try:
                import importlib, bank as _bank
                importlib.reload(_bank)
                if (DEBUG_DIR / "bank_file.txt").exists():         # process a statement PDF put in the statements folder
                    spec = (DEBUG_DIR / "bank_file.txt").read_text(encoding="utf-8").strip().split("|")
                    (DEBUG_DIR / "bank_file.txt").unlink()
                    _bank.check_gmail_statements(cfg, log, notify=notify, force_file=_bank.STATEMENTS / spec[0], dry=len(spec) > 1 and spec[1] == "dry")
                if (DEBUG_DIR / "bank_dry_run.txt").exists():
                    (DEBUG_DIR / "bank_dry_run.txt").unlink()
                    _bank.check_gmail_statements(cfg, log, dry=True)
                elif (DEBUG_DIR / "bank_on.txt").exists() or (Path(__file__).parent / "server_mode.txt").exists():   # on for the server
                    _bank.check_gmail_statements(cfg, log, notify=notify)
            except Exception as e:
                log("Bank statement check failed: " + repr(e)[:200])
            # ---- the 3 balance tiles + when each transaction first appears (to learn Takealot's hold rule)
            try:
                balance_snapshot(ctx, auth, log)
            except Exception as e:
                log("Balance snapshot failed: " + repr(e)[:200])
            try:
                payout_forecast(ctx, cfg, auth, log)
            except Exception as e:
                log("Payout forecast failed: " + repr(e)[:200])
            try:
                evening_summary(cfg, log)
            except Exception as e:
                log("Evening summary failed: " + repr(e)[:200])
            try:
                morning_summary(cfg, log)
            except Exception as e:
                log("Morning summary failed: " + repr(e)[:200])
            try:
                coming_back_watch(cfg, log)
            except Exception as e:
                log("Coming-back watch failed: " + repr(e)[:200])
            try:
                return_alerts(cfg, log)
            except Exception as e:
                log("Return notifications failed: " + repr(e)[:200])
            try:
                leadtime_deadlines(cfg, log)
            except Exception as e:
                log("Lead-time deadlines failed: " + repr(e)[:200])
            try:
                serial_reminder(cfg, log)
            except Exception as e:
                log("Serial reminder failed: " + repr(e)[:200])
            try:
                pudo_sync(cfg, log)
            except Exception as e:
                log("Pudo bookings failed: " + repr(e)[:200])
            try:
                weekly_backup(cfg, log)
            except Exception as e:
                log("Weekly backup failed: " + repr(e)[:200])
            # ---- Takealot shipments (POs) and what was in each
            try:
                fetch_takealot_pos(ctx, cfg, auth, log)
            except Exception as e:
                log("Takealot shipments couldn't be read: " + repr(e)[:200])

            # ---- Courier Guy shipments (best effort; needs COURIER_LOGIN.bat once)
            try:
                fetch_courier(ctx, cfg, log)
            except Exception as e:
                log("Courier Guy couldn't be read: " + repr(e)[:200])
            # ---- the courier has collected a shipment: mark it as shipped on the Seller Portal
            try:
                mark_collected_shipped(ctx, cfg, auth, log)
            except Exception as e:
                log("Marking shipments as shipped failed: " + repr(e)[:200])
            return sales, returns
        finally:
            ctx.close()


BALANCE_LOG = DEBUG_DIR / "balance_log.jsonl"


SEEN_FILE = DEBUG_DIR / "alerts_seen.json"


def _seen_load():
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _seen_save(d):
    """Saved safely (written to a temp file, then swapped in). Returns False if it couldn't be saved,
    so callers don't send notifications they can't remember sending."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        for k in ("sales", "returns"):
            if k in d:                                   # never create an empty list: that would skip the first-run seeding
                d[k] = d[k][-20000:]                     # far more than 90 days of sales, so old ones are never "new" again
        tmp = SEEN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.replace(SEEN_FILE)
        return True
    except Exception:
        return False


def sale_alerts(cfg, sales, log, full=False):
    """A phone notification for every new Takealot sale (orders seen for the first time).
    sales: Takealot's sales rows (order_id, sku, product_title, quantity, selling_price, sale_status, dc ...).
    The first time it runs it only remembers what's there, so nothing old is announced."""
    import re
    seen = _seen_load()
    first = seen is None or "sales" not in seen
    if first and not full:
        return                       # the first list must be the sync's full one (90 days), or older sales would be announced later
    seen = seen or {}
    known = set(seen.get("sales", []))
    new = []
    for r in sales or []:
        k = f"{r.get('order_id')}|{r.get('sku')}"
        if not r.get("order_id") or k in known:
            continue
        known.add(k)
        seen.setdefault("sales", []).append(k)
        st = str(r.get("sale_status") or "")
        if not first and not re.search(r"cancel", st, re.I):
            new.append(r)
    if new or first:
        if not _seen_save(seen):
            return                   # couldn't remember them: better no notification than the same one every 2 minutes
    if first or not new:
        return
    today = datetime.now().date().isoformat()
    seen.setdefault("day", {})
    if seen["day"].get("d") != today:
        seen["day"] = {"d": today, "n": 0, "g": 0.0}
    lines = []
    for r in new:
        q = int(r.get("quantity") or r.get("qty") or 1)
        price = float(r.get("selling_price") or r.get("gross") or 0)      # Takealot's selling_price is the whole line
        seen["day"]["n"] += q
        seen["day"]["g"] += price
        lead = re.search(r"lead", str(r.get("sale_status") or ""), re.I)
        dc = str(r.get("stock_source_region") or r.get("dc") or r.get("fulfilment_dc") or "").strip()
        title = str(r.get("product_title") or "")[:60]
        body = (f"{q} × " if q > 1 else "") + f"{title} · R{price:,.0f}" + (f" · {dc}" if dc else "") + \
               (" · lead time: send one in" if lead else "") + \
               f"\nToday: {seen['day']['n']} sold · R{seen['day']['g']:,.0f}"
        lines.append((lead, body.split("\n")[0]))
        log("Sale notification: " + body.split("\n")[0])
    _seen_save(seen)
    total = f"Today: {seen['day']['n']} sold · R{seen['day']['g']:,.0f}"
    if len(lines) == 1:                                  # one notification per check, however many sales came in
        title = "🛒 New sale" + (" (lead time)" if lines[0][0] else "")
        text = lines[0][1] + "\n" + total
    else:
        title = f"🛒 {len(lines)} new sales" + (" (incl. lead time)" if any(l for l, _ in lines) else "")
        text = "\n".join(t for _, t in lines[:6]) + ("\n…" if len(lines) > 6 else "") + "\n" + total
    try:
        notify(cfg, title, text.replace(",", " "), url="./?view=sales", topic="price", log=log, tag="sale")
    except Exception as e:
        log("Sale notification failed: " + repr(e)[:120])


def return_alerts(cfg, log):
    """A phone notification for every new customer return (from the returns the sync saved)."""
    import stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    since = (date.today() - timedelta(days=14)).isoformat()
    try:
        rets = db.select("takealot_returns", f"select=return_id,order_id,title,qty,dc,reason,outcome,return_date&return_date=gte.{since}")
    except Exception:
        return
    seen = _seen_load() or {}
    first = "returns" not in seen
    known = set(seen.get("returns", []))
    new = [r for r in rets if str(r.get("return_id")) not in known]
    for r in new:
        seen.setdefault("returns", []).append(str(r.get("return_id")))
    if not new and not first:
        return
    if not _seen_save(seen) or first:
        return
    for r in new:
        body = f"{str(r.get('title') or '')[:60]}" + (f" × {r['qty']}" if (r.get("qty") or 1) > 1 else "") + \
               (f" · {r['dc']}" if r.get("dc") else "") + (f" · {r['reason']}" if r.get("reason") else "") + \
               (f" · {r['outcome']}" if r.get("outcome") else "") + f" · order {r.get('order_id')}"
        try:
            notify(cfg, "↩️ Customer return", body, url="./?view=removals", topic="price", log=log, tag=f"return-{r.get('return_id')}")
        except Exception as e:
            log("Return notification failed: " + repr(e)[:120])
        log("Return notification: " + body)


def sale_watch(ctx, cfg, log):
    """From the repricer's browser: look at Takealot's latest sales every 2 minutes (with the Seller API key)."""
    import time as _t, stockrecon_sync as S
    if _t.time() - getattr(S, "_SALE_WATCH_AT", 0) < 120:
        return
    S._SALE_WATCH_AT = _t.time()
    key = _creds_key()
    if not key:
        return
    start = (date.today() - timedelta(days=1)).isoformat()
    end = date.today().isoformat()
    try:
        r = ctx.request.get(f"{SALES_API}?filters=start_date%3A{start}%2Cend_date%3A{end}&page_size=100&page_number=1",
                            headers={"Authorization": f"Key {key}", "Accept": "application/json"}, timeout=30_000)
        if not r.ok:
            if _t.time() - getattr(S, "_SALE_WATCH_ERR", 0) > 3600:
                S._SALE_WATCH_ERR = _t.time()
                log(f"Sale watch: Takealot answered {r.status} (sale notifications then come with the 30-minute sync)")
                _app_state(cfg, "sale_watch", {"at": datetime.now().isoformat(timespec="seconds"), "ok": False, "status": r.status})
            return
        rows = (r.json() or {}).get("sales") or []
        sale_alerts(cfg, rows, log)
        try:
            leadtime_on_sale(ctx, cfg, rows, {"Authorization": f"Key {key}", "Accept": "application/json"}, log)
        except Exception as e:
            log("Lead time after a sale: " + repr(e)[:150])
        if _t.time() - getattr(S, "_SALE_WATCH_OK", 0) > 1800:            # let the app see it's alive (every 30 min)
            S._SALE_WATCH_OK = _t.time()
            _app_state(cfg, "sale_watch", {"at": datetime.now().isoformat(timespec="seconds"), "ok": True, "rows": len(rows),
                                            "seeded": "sales" in (_seen_load() or {})})
    except Exception as e:
        if _t.time() - getattr(S, "_SALE_WATCH_ERR", 0) > 3600:
            S._SALE_WATCH_ERR = _t.time()
            log("Sale watch failed: " + repr(e)[:150])


def leadtime_on_sale(ctx, cfg, rows, auth, log):
    """A new order on a listing that follows HOME stock (lead time = auto): set lead time on every listing of that
    product straight away (within ~2 minutes), so two listings sharing the same units can't both sell the last one."""
    import stockrecon_sync as S
    seen = getattr(S, "_LT_SALE_SEEN", None)
    ids = {str(x.get("order_id")) + ":" + str(x.get("sku")) for x in rows}
    if seen is None:                                   # first look after a start: remember what's there, act on what's new
        S._LT_SALE_SEEN = ids
        return
    new = [x for x in rows if str(x.get("order_id")) + ":" + str(x.get("sku")) not in seen]
    if not new:
        return
    db = S.DB(cfg["url"], cfg["key"])
    prods = db.select("products", "select=sku,product,leadtime_fixed")
    auto = {str(p["sku"]) for p in prods if p.get("leadtime_fixed") is not None and int(p["leadtime_fixed"]) < 0}
    hit = [x for x in new if str(x.get("sku")) in auto]
    if not hit:
        S._LT_SALE_SEEN = seen | ids
        return
    offers, n = [], 1
    while n <= 20:
        rr = ctx.request.get(f"https://{API_HOST}/v2/offers/detailed?page_number={n}&page_size=100&sort_key=date_added&sort_dir=asc",
                             headers=auth, timeout=60_000)
        if not rr.ok:
            log(f"Lead time after a sale: offers feed answered {rr.status}; the next sync will set it")
            return                                     # not marked seen: try again in 2 minutes
        got = (rr.json() or {}).get("offers") or []
        offers += got
        if len(got) < 100:
            break
        n += 1
    log(f"Lead time after a sale: {len(hit)} new order(s) on an auto lead-time listing: checking every listing of that product.")
    apply_leadtime_requests(ctx, cfg, auth, offers, log, rows)
    S._LT_SALE_SEEN = seen | ids


def _send_mail(subject, body, attach=None, log=None):
    """E-mail from the report Gmail (report_from + report_app_password) to report_to. attach: (filename, bytes, subtype)."""
    import configparser, smtplib, ssl
    from email.message import EmailMessage
    cp = configparser.ConfigParser()
    cp.read(HERE / "config.ini", encoding="utf-8")
    c = cp["stockrecon"] if "stockrecon" in cp else {}
    sender, app_pw = (c.get("report_from") or "").strip(), (c.get("report_app_password") or "").replace(" ", "").strip()
    to = [x.strip() for x in (c.get("report_to") or "").replace(";", ",").split(",") if x.strip()]
    if not (sender and app_pw and to) or "PASTE" in app_pw.upper():
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Stock Recon <{sender}>", ", ".join(to)
    msg.set_content(body)
    if attach:
        msg.add_attachment(attach[1], maintype="application", subtype=attach[2], filename=attach[0])
    host = (c.get("report_smtp_host") or "smtp.gmail.com").strip()
    port = int((c.get("report_smtp_port") or "465").strip())
    user = (c.get("report_smtp_user") or sender).strip()
    smtp = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60) if port == 465 else smtplib.SMTP(host, port, timeout=60)
    with smtp:
        if port != 465:
            smtp.starttls(context=ssl.create_default_context())
        smtp.login(user, app_pw)
        smtp.send_message(msg)
    return True


def weekly_backup(cfg, log):
    """Every Sunday evening: all the app's data as spreadsheet files (CSV) in one zip, e-mailed to report_to."""
    now = datetime.now()
    if now.weekday() != 6 or now.hour < 20:
        return
    flag = DEBUG_DIR / f"backup_{now.date().isoformat()}.txt"
    if flag.exists():
        return
    tried = DEBUG_DIR / "backup_tried.txt"
    try:
        if __import__("time").time() - tried.stat().st_mtime < 3 * 3600:
            return
    except OSError:
        pass
    DEBUG_DIR.mkdir(exist_ok=True)
    tried.write_text(now.isoformat(), encoding="utf-8")
    import csv, io, zipfile, stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    tables = ["units", "takealot_sales", "products", "expenses", "supplier_ledger", "courier_shipments", "takealot_pos",
              "takealot_returns", "takealot_recons", "price_rules", "barcodes", "leadtime_requests", "app_state"]
    buf, counts = io.BytesIO(), {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t in tables:
            try:
                rows = db.select(t, "select=*")
            except Exception:
                continue
            counts[t] = len(rows)
            keys = sorted({k for r in rows for k in r})
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(keys)
            for r in rows:
                w.writerow([json.dumps(r.get(k), default=str) if isinstance(r.get(k), (dict, list)) else ("" if r.get(k) is None else r.get(k)) for k in keys])
            z.writestr(f"{t}.csv", "\ufeff" + out.getvalue())
    name = f"Stock Recon backup {now.date().isoformat()}.zip"
    body = ("Hi,\n\nThe weekly backup of everything in Stock Recon is attached (one spreadsheet file per table).\n\n" +
            "\n".join(f"  {t}: {n} rows" for t, n in counts.items()) + "\n\nKeep this e-mail: it's your copy if anything ever happens to the database.\n\nStock Recon")
    try:
        if _send_mail(f"Stock Recon: weekly backup {now.strftime('%d %b %Y')}", body, (name, buf.getvalue(), "zip"), log):
            flag.write_text(now.isoformat(), encoding="utf-8")
            log(f"Weekly backup e-mailed ({sum(counts.values())} rows, {len(buf.getvalue()) // 1024} KB).")
    except Exception as e:
        log("Weekly backup e-mail failed: " + repr(e)[:200])


def leadtime_deadlines(cfg, log):
    """Lead-time orders still at HOME with nothing booked: a phone reminder the day before the deadline, and when it's late.
    Takealot charges an Order Cancellation Penalty if you don't deliver within the lead time."""
    import stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    units = db.select("units", "select=id,product,order_id,po,location,status,match_method&match_method=eq.leadtime-home&status=in.(awaiting_fees,in_stock)")
    units = [u for u in units if u.get("order_id") and u.get("location") == "HOME"]
    if not units:
        return
    ships = db.select("courier_shipments", "select=reference,status,direction&direction=eq.to_takealot")
    booked_pos = {m for c in ships if c.get("status") != "cancelled" for m in __import__("re").findall(r"\d{9}", str(c.get("reference") or ""))}
    try:
        adam = {str((e.get("raw") or {}).get("po")) for e in db.select("expenses", "select=raw&category=eq.adam_delivery")}
    except Exception:
        adam = set()
    ids = ",".join(u["order_id"] for u in units)
    sales = {s["order_id"]: s for s in db.select("takealot_sales", f"select=order_id,order_date,sku,fulfilment_dc,product_title&order_id=in.({ids})")}
    days_of = {p["sku"]: int(((p.get("takealot_stock") or {}).get("LEADTIME") or {}).get("days") or 4) for p in db.select("products", "select=sku,takealot_stock")}
    seen = _seen_load() or {}
    done = seen.setdefault("lt_alert", {})
    today = date.today()
    deadlines = []
    for u in units:
        if u.get("po") and (u["po"] in booked_pos or u["po"] in adam):
            continue
        s = sales.get(u["order_id"])
        if not s or not s.get("order_date"):
            continue
        odt = datetime.fromisoformat(str(s["order_date"]).replace("Z", "+00:00"))
        if odt.tzinfo:
            odt = odt.astimezone(__import__("zoneinfo").ZoneInfo("Africa/Johannesburg"))
        due = odt.date()
        n = days_of.get(s.get("sku"), 4)
        while n > 0:                                      # lead time is in working days (Mon-Fri)
            due += timedelta(days=1)
            if due.weekday() < 5:
                n -= 1
        left = (due - today).days
        deadlines.append({"order": u["order_id"], "unit": u["id"], "due": due.isoformat(), "left": left, "dc": s.get("fulfilment_dc"), "title": s.get("product_title")})
        if left > 1 or done.get(u["order_id"]) == today.isoformat():
            continue
        done[u["order_id"]] = today.isoformat()
        seen["lt_alert"] = done
        if not _seen_save(seen):
            continue                                      # couldn't note it: don't risk repeating it every sync
        when = "today" if left == 0 else "tomorrow" if left == 1 else f"{-left} day{'s' if left < -1 else ''} late"
        try:
          notify(cfg, "⏰ Lead-time order due " + when, f"{str(s.get('product_title') or u['product'])[:50]} · order {u['order_id']} · to {s.get('fulfilment_dc') or 'Takealot'} by {due.strftime('%a %d %b')}. Book the courier or Adam so Takealot doesn't cancel it (penalty).",
               url="./?view=overview", topic="price", log=log, tag=f"leadtime-{u['order_id']}")
        except Exception as e:
            log("Lead-time notification failed: " + repr(e)[:120])
        log(f"Lead-time reminder: order {u['order_id']} due {due} ({when}).")
    seen["lt_alert"] = {k: v for k, v in done.items() if v >= (today - timedelta(days=14)).isoformat()}
    _seen_save(seen)
    _app_state(cfg, "leadtime_due", {"at": datetime.now().isoformat(timespec="seconds"), "orders": deadlines})


def serial_reminder(cfg, log):
    """Once a day (from 09:00): stock you paid upfront and added before it arrived still has no serial numbers."""
    now = datetime.now()
    if now.hour < 9:
        return
    seen = _seen_load() or {}
    if seen.get("sn_day") == now.date().isoformat():
        return
    import stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    rows = db.select("units", "select=id,product,sn,status,invoice_no,comment&comment=ilike.*serial%20needed*&status=neq.settled")
    rows = [u for u in rows if not str(u.get("sn") or "").strip()]
    seen["sn_day"] = now.date().isoformat()
    if not _seen_save(seen) or not rows:
        return
    by = {}
    for u in rows:
        k = u["product"] + (f" ({u['invoice_no']})" if u.get("invoice_no") else "")
        by[k] = by.get(k, 0) + 1
    body = " · ".join(f"{n} × {k}" for k, n in sorted(by.items(), key=lambda x: -x[1])[:4]) + \
           ". When the stock arrives, open Needs you → Add serial numbers and scan them."
    try:
        notify(cfg, f"🔢 {len(rows)} unit{'s' if len(rows) != 1 else ''} still need a serial number", body, url="./?view=overview", topic="price", log=log, tag="serials")
    except Exception as e:
        log("Serial reminder failed: " + repr(e)[:120])


def pudo_sync(cfg, log):
    """Your Pudo locker bookings (API key pasted in the app, Shipments page). Saved for the app, and a phone notification
    when a parcel's status changes (e.g. ready to collect at the locker)."""
    import re, urllib.request, urllib.error, urllib.parse, stockrecon_sync as S, time as _t
    db = S.DB(cfg["url"], cfg["key"])
    try:
        rows = db.select("app_state", "select=value&key=eq.pudo")
    except Exception:
        return
    key = ((rows[0].get("value") or {}).get("api_key") if rows else "") or ""
    if not key:
        return
    if key == getattr(S, "_PUDO_KEY", None) and _t.time() - getattr(S, "_PUDO_AT", 0) < 600:   # every 10 minutes is plenty
        return
    if key != getattr(S, "_PUDO_KEY", None):
        S._PUDO_HOW = None                                     # a new key: work out again how Pudo wants it
    S._PUDO_AT, S._PUDO_KEY = _t.time(), key
    def get(base, path, how):
        url = f"https://{base}{path}" + (("&" if "?" in path else "?") + "api_key=" + urllib.parse.quote(key) if how == "query" else "")
        h = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 StockRecon"}
        if how == "bearer":
            h["Authorization"] = f"Bearer {key}"
        elif how == "x-api-key":
            h["X-API-KEY"] = key
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
            return json.loads(r.read().decode("utf-8") or "null")
    st = getattr(S, "_PUDO_HOW", None)
    tries = [st] if st else [(b, pth, a) for b in ("api-tcg.co.za", "api-pudo.co.za", "api-tcg.co.za/api/v1", "api-pudo.co.za/api/v1")
                             for pth in ("/shipments", "/tracking/shipments?days=30&page=1")
                             for a in ("bearer", "query", "x-api-key")]
    data, errs = None, []
    for base, pth, how in tries:
        try:
            data = get(base, pth, how)
            S._PUDO_HOW = (base, pth, how)
            break
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:80].replace(key, "KEY")
            except Exception:
                pass
            errs.append(f"{base}{pth.split('?')[0]} {how}: {e.code} {body}")
        except Exception as e:
            errs.append(f"{base}{pth.split('?')[0]} {how}: {repr(e)[:60]}")
    err = " | ".join(errs)[:1500]
    if data is None:
        S._PUDO_HOW = None
        _app_state(cfg, "pudo_status", {"at": datetime.now().isoformat(timespec="seconds"), "ok": False, "error": err})
        return
    items = data.get("shipments") if isinstance(data, dict) else data
    items = items if isinstance(items, list) else []
    def pick(x, *ks):
        for k in ks:
            v = x.get(k)
            if v not in (None, ""):
                return v
        return None
    ships = []
    for x in items[:50]:
        def place(addr, contact):
            a0 = addr if isinstance(addr, dict) else {}
            c0 = contact if isinstance(contact, dict) else {}
            who = pick(c0, "name", "company") or pick(a0, "company", "name")
            where = pick(a0, "terminal_name", "locker_name", "place_name", "local_area", "city", "entered_address", "street_address")
            return " · ".join(str(v) for v in (who, where) if v)
        ca, da = x.get("collection_address") or {}, x.get("delivery_address") or {}
        ships.append({"id": pick(x, "id"), "waybill": pick(x, "short_tracking_reference", "custom_tracking_reference", "tracking_reference"),
                      "status": str(pick(x, "status") or ""), "created": str(pick(x, "time_created") or "")[:16],
                      "from": place(ca, x.get("collection_contact")) or pick(x, "collection_name") or "",
                      "to": place(da, x.get("delivery_contact")) or pick(x, "delivery_name") or "",
                      "ref": pick(x, "customer_reference") or "", "service": pick(x, "service_level_code") or "",
                      "eta": str(pick(x, "estimated_delivery_to", "estimated_delivery_from") or "")[:10]})
    _app_state(cfg, "pudo_shipments", {"at": datetime.now().isoformat(timespec="seconds"), "list": ships})
    _app_state(cfg, "pudo_status", {"at": datetime.now().isoformat(timespec="seconds"), "ok": True, "host": "/".join(S._PUDO_HOW[:2]), "count": len(ships),
                                    "keys": sorted(items[0].keys())[:40] if items and isinstance(items[0], dict) else []})
    # tell the phones when a parcel's status changes (first run only remembers)
    seen = _seen_load() or {}
    first = "pudo" not in seen
    old = seen.get("pudo", {})
    changed = [p for p in ships if p["waybill"] and old.get(p["waybill"]) != p["status"]]
    seen["pudo"] = {p["waybill"]: p["status"] for p in ships if p["waybill"]}
    if not _seen_save(seen) or first:
        return
    for p in changed:
        nice = p["status"].replace("-", " ")
        try:
            notify(cfg, ("📦 Pudo parcel ready to collect" if re.search(r"delivered|ready-for-collection|in-locker", p["status"], re.I) else "📦 Pudo parcel: " + nice),
                   f"{p['waybill']}" + (f" · {p['ref']}" if p.get("ref") else "") + (f" · from {p['from']}" if p.get("from") else "") + f" · {nice}",
                   url="./?view=ship", topic="price", log=log, tag=f"pudo-{p['waybill']}")
        except Exception as e:
            log("Pudo notification failed: " + repr(e)[:120])


def heartbeat(cfg):
    """Tells an outside monitor (healthchecks.io, set in the app) that the server is alive, every 5 minutes.
    If the pings stop (server down or stuck), that service alerts your phone / e-mail."""
    import time as _t, stockrecon_sync as S, urllib.request
    if _t.time() - getattr(S, "_HB_AT", 0) < 300:
        return
    S._HB_AT = _t.time()
    try:
        if _t.time() - getattr(S, "_HB_URL_AT", 0) > 1800:
            rows = S.DB(cfg["url"], cfg["key"]).select("app_state", "select=value&key=eq.healthcheck")
            S._HB_URL = ((rows[0].get("value") or {}).get("url") if rows else "") or ""
            S._HB_URL_AT = _t.time()
        url = getattr(S, "_HB_URL", "")
        if url.startswith("https://"):
            urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "StockRecon"}), timeout=10).read()
    except Exception:
        pass


def fee_check(cfg, txs, when, kind):
    """Takealot's fees per order, checked against the same product's usual fees: charged twice, or much higher than usual.
    Also the storage fees per month."""
    import re, statistics, stockrecon_sync as S
    by = {}
    for x in txs:
        m = re.search(r"Order:\s*(\d+)", str(x.get("reference") or ""))
        if not m:
            continue
        o = by.setdefault(m.group(1), {"pay": [], "succ": [], "succ_rev": [], "ful": [], "ful_rev": [], "date": None})
        k, amt = kind(x).lower(), float(x.get("inc_vat") or 0)
        if "order payment" in k:
            o["pay"].append(amt)
            o["date"] = (when(x) or datetime.now()).date().isoformat()
        elif "success fee charge" in k:
            o["succ"].append(-amt)
        elif "success fee reversal" in k:
            o["succ_rev"].append(amt)
        elif "fulfilment fee charge" in k:
            o["ful"].append(-amt)
        elif "fulfilment fee reversal" in k:
            o["ful_rev"].append(amt)
    if not by:
        return
    db = S.DB(cfg["url"], cfg["key"])
    sku_of, title_of, qty_of = {}, {}, {}
    ids = list(by)
    for i in range(0, len(ids), 80):
        for r in db.select("takealot_sales", f"select=order_id,sku,product_title,qty&order_id=in.({','.join(ids[i:i + 80])})"):
            sku_of[r["order_id"]], title_of[r["order_id"]], qty_of[r["order_id"]] = r.get("sku"), r.get("product_title"), int(r.get("qty") or 1)
    rate, ful = {}, {}
    for oid, o in by.items():
        pay = sum(o["pay"])
        if not sku_of.get(oid):
            continue
        if pay > 0 and o["succ"]:
            rate.setdefault(sku_of.get(oid), []).append(sum(o["succ"]) / pay)
        if o["ful"]:
            ful.setdefault(sku_of.get(oid), []).append(sum(o["ful"]) / qty_of.get(oid, 1))
    flags = []
    for oid, o in by.items():
        sku, pay = sku_of.get(oid), sum(o["pay"])
        name = (title_of.get(oid) or "")[:60]
        net_succ = len(o["succ"]) - len(o["succ_rev"])
        if o["pay"] and net_succ > len(o["pay"]):
            flags.append({"order": oid, "product": name, "date": o["date"], "what": "Success fee charged twice",
                          "amount": round(sum(o["succ"]) - sum(o["succ_rev"]), 2), "usual": round(sum(o["succ"]) / len(o["succ"]), 2)})
        if len(o["ful"]) - len(o["ful_rev"]) > max(1, len(o["pay"])) and qty_of.get(oid, 1) == 1:
            flags.append({"order": oid, "product": name, "date": o["date"], "what": "Fulfilment fee charged twice",
                          "amount": round(sum(o["ful"]) - sum(o["ful_rev"]), 2), "usual": round(o["ful"][0], 2)})
        if not sku:
            continue                                     # product unknown (older than the app): only the double-charge checks apply
        rs = rate.get(sku) or []
        if pay > 0 and o["succ"] and len(rs) >= 3 and not (o["pay"] and net_succ > len(o["pay"])):
            med, r0 = statistics.median(rs), sum(o["succ"]) / pay
            if r0 > med * 1.10 + 0.005:
                flags.append({"order": oid, "product": name, "date": o["date"], "what": f"Success fee {r0 * 100:.1f}% of the sale (usually {med * 100:.1f}%)",
                              "amount": round(sum(o["succ"]), 2), "usual": round(med * pay, 2)})
        fs = ful.get(sku) or []
        if o["ful"] and len(fs) >= 3:
            med, f0 = statistics.median(fs), sum(o["ful"]) / qty_of.get(oid, 1)
            if f0 > med * 1.15 + 1:
                flags.append({"order": oid, "product": name, "date": o["date"], "what": "Fulfilment fee higher than usual",
                              "amount": round(f0, 2), "usual": round(med, 2)})
    storage = {}
    for x in txs:
        if "storage fee" in kind(x).lower() and when(x):
            m = when(x).strftime("%Y-%m")
            storage[m] = round(storage.get(m, 0) - float(x.get("inc_vat") or 0), 2)
    _app_state(cfg, "fee_check", {"at": datetime.now().isoformat(timespec="seconds"), "orders": len(by),
                                  "flags": sorted(flags, key=lambda f: str(f.get("date")), reverse=True)[:60], "storage": storage})


def _app_state(cfg, key, value):
    """Save a figure for the app (table app_state, from 18_database_update.sql). Quietly skipped before that's run."""
    import stockrecon_sync as S
    try:
        S.DB(cfg["url"], cfg["key"]).insert("app_state", [{"key": key, "value": value, "updated_at": datetime.utcnow().isoformat() + "Z"}],
                                            upsert_on="key")
        return True
    except Exception:
        return False


def payout_forecast(ctx, cfg, auth, log):
    """Next Takealot payouts. Takealot pays on Thursdays, for what was recorded on your account up to the Sunday before.
    Takes everything after the last payout (sales, fees, refunds, ...) and splits it by which Thursday it falls into."""
    if not auth:
        return
    import re, time as _t, stockrecon_sync as S
    if _t.time() - getattr(S, "_PAYOUT_AT", 0) < 1500 and getattr(S, "_PAYOUT_VER", 0) == 5 and not (DEBUG_DIR / "payout_now.txt").exists():
        return                                                  # every 30 minutes is plenty
    try:
        (DEBUG_DIR / "payout_now.txt").unlink()
    except OSError:
        pass
    S._PAYOUT_AT, S._PAYOUT_VER = _t.time(), 5
    r = ctx.request.get(f"https://{API_HOST}/v1/seller/balances", headers=auth, timeout=60_000)
    bal = ((r.json() or {}).get("balances") or {}) if r.ok else {}
    d_to = datetime.now().date()
    d_from = d_to - timedelta(days=45)
    txs, pn = [], 1
    while pn < 40:
        q = ctx.request.get(f"https://{API_HOST}/v1/seller/transactions?date_from={d_from}&date_to={d_to}&page_size=100&page_number={pn}",
                            headers=auth, timeout=60_000)
        if not q.ok:
            break
        j = q.json() or {}
        rows = j.get("transactions") or []
        txs += rows
        if len(rows) < 100:
            break
        pn += 1
    def when(x):
        try:
            return datetime.fromisoformat(str(x.get("date_created")).replace("Z", "+00:00").replace(" ", "T")[:19])
        except Exception:
            return None
    kind = lambda x: str(((x.get("transaction_type") or {}).get("description")) or "")
    types = sorted({kind(x) for x in txs})
    payouts = [x for x in txs if re.search(r"disburse|payout|paid to|transfer to", kind(x), re.I)]
    last = max(payouts, key=lambda x: when(x) or datetime.min) if payouts else None
    last_at = when(last) if last else None
    # a Thursday payout covers what was recorded up to the Sunday before it: everything after that Sunday is still to come
    def cut_of(d):                                               # the Sunday before a payout day
        return d - timedelta(days=(d.weekday() - 6) % 7 or 7)
    last_cut = cut_of(last_at.date()) if last_at else None
    open_tx = [x for x in txs if x not in payouts and when(x) and (last_cut is None or when(x).date() > last_cut)]
    # check the rule against Takealot's own payouts: sum what fell in each payout's week and compare
    check = []
    for pz in sorted(payouts, key=lambda x: when(x) or datetime.min)[-3:]:
        pd = when(pz).date()
        hi, lo = cut_of(pd), cut_of(pd) - timedelta(days=7)
        if (lo - d_from).days < 0:
            continue                                             # that week isn't fully inside what we read
        part = [x for x in txs if x not in payouts and when(x) and lo < when(x).date() <= hi]
        tot = round(sum(float(x.get("inc_vat") or 0) for x in part), 2)
        edge = sorted([x for x in txs if x not in payouts and when(x) and hi - timedelta(days=1) <= when(x).date() <= hi + timedelta(days=1)],
                      key=lambda x: when(x))
        edge = [{"at": str(x.get("date_created")), "type": kind(x), "amount": float(x.get("inc_vat") or 0), "ref": str(x.get("reference") or "")[:40]}
                for x in edge if abs(float(x.get("inc_vat") or 0)) >= 500][:40]
        check.append({"edge": edge, "payout": pd.isoformat(), "paid": round(abs(float(pz.get("inc_vat") or 0)), 2), "rule": tot,
                      "diff": round(abs(float(pz.get("inc_vat") or 0)) - tot, 2), "n": len(part), "from": (lo + timedelta(days=1)).isoformat(), "to": hi.isoformat()})
    today = datetime.now().date()
    nxt = today + timedelta(days=(3 - today.weekday()) % 7)          # this or next Thursday
    if last_at and last_at.date() == nxt:                             # today's payout already went out
        nxt += timedelta(days=7)
    buckets = {}
    for x in open_tx:
        w = when(x)
        if not w:
            continue
        d = w.date()
        cut = nxt - timedelta(days=4)                                 # the Sunday before that Thursday
        pay = nxt
        while d > cut:
            pay += timedelta(days=7)
            cut += timedelta(days=7)
        b = buckets.setdefault(pay.isoformat(), {"amount": 0.0, "n": 0})
        b["amount"] += float(x.get("inc_vat") or 0)
        b["n"] += 1
    value = {"at": datetime.now().isoformat(timespec="seconds"), "current": bal.get("current"), "held_back": bal.get("held_back"),
             "available": bal.get("available"),
             "last_payout": {"date": last_at.date().isoformat(), "amount": abs(float(last.get("inc_vat") or 0))} if last else None,
             "next": [{"date": k, "amount": round(v["amount"], 2), "n": v["n"]} for k, v in sorted(buckets.items())],
             "types": types, "check": check}
    _app_state(cfg, "payout", value)
    try:
        fee_check(cfg, txs, when, kind)
    except Exception as e:
        log("Fee check failed: " + repr(e)[:150])
    # per order: when Takealot recorded its payment and which Thursday pays it out
    paid_days = sorted({when(x).date() for x in payouts if when(x)})
    orders = {}
    for x in txs:
        if not re.search(r"order payment|stock loss payment", kind(x), re.I) or not when(x):
            continue
        m = re.search(r"Order:\s*(\d+)", str(x.get("reference") or ""))
        key = m.group(1) if m else ("STOCKLOSS:" + str(x.get("reference") or "")[:30])
        d = when(x).date()
        sunday = d + timedelta(days=(6 - d.weekday()) % 7)
        pay = sunday + timedelta(days=4)
        actual = next((pd for pd in paid_days if pd >= pay), None)       # the payout that actually went out (if any yet)
        orders[key] = {"rec": d.isoformat(), "pay": (actual or pay).isoformat(), "paid": bool(actual),
                       "amount": float(x.get("inc_vat") or 0)}
    _app_state(cfg, "order_pay", {"at": value["at"], "orders": orders})
    # the balance tiles over the last 10 days (from the 30-minute snapshots), to see when money becomes Available
    try:
        hist, keep = [], (datetime.now() - timedelta(days=10)).isoformat()
        for line in BALANCE_LOG.read_text(encoding="utf-8").splitlines():
            j = json.loads(line)
            if str(j.get("at")) >= keep:
                hist.append([j.get("at"), j.get("current"), j.get("held_back"), j.get("available"),
                             round(sum(float(x.get("amount") or 0) for x in j.get("new") or []), 2)])
        _app_state(cfg, "balance_hist", {"rows": hist})
    except Exception as e:
        log("Balance history for the app failed: " + repr(e)[:150])


def coming_back_watch(cfg, log):
    """Units on their way back to you (Return: me). Remembers since when each one has waited (app_state coming_back,
    shown on the Shipments page) and reminds the phone once at 5 and once at 10 days while no collection is booked."""
    import stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    try:
        rows = db.select("units", "select=id,product,location,courier_shipment_id,updated_at&status=eq.return_me")
    except Exception:
        return
    try:
        got = db.select("app_state", "select=value&key=eq.coming_back")
        st = (got[0].get("value") or {}) if got else {}
    except Exception:
        return                                         # before 18_database_update.sql
    today_s = datetime.now().date().isoformat()
    ships = {}
    ids = [str(u["courier_shipment_id"]) for u in rows if u.get("courier_shipment_id")]
    if ids:
        try:
            ships = {str(x["id"]): x for x in db.select("courier_shipments", f"select=id,waybill,status&id=in.({','.join(ids)})")}
        except Exception:
            pass
    out, changed = {}, False
    for u in rows:
        k = u["id"]
        x = dict(st.get(k) or {})
        if not x:
            x = {"since": str(u.get("updated_at") or today_s)[:10]}
            changed = True
        sh = ships.get(str(u.get("courier_shipment_id") or ""))
        booked = bool(sh) and sh.get("status") not in ("cancelled",)
        days = (datetime.now().date() - date.fromisoformat(x["since"])).days
        for lim in (5, 10):
            if not booked and days >= lim and not x.get(f"n{lim}"):
                x[f"n{lim}"] = today_s
                changed = True
                try:
                    notify(cfg, f"↩️ Still not on its way back ({days} days)",
                           f"{u['product'][:60]} at Takealot {u.get('location') or ''}: no collection booked yet. "
                           "Ask Takealot for the booking exception / removal, then book the Courier Guy collection.",
                           url="./?view=ship", topic="price", log=log, tag=f"comingback-{k[:8]}")
                except Exception as e:
                    log("Coming-back reminder failed: " + repr(e)[:120])
        out[k] = x
    if changed or set(out) != set(st):
        _app_state(cfg, "coming_back", out)


def morning_summary(cfg, log):
    """Once a day from 08:00: what needs doing today (lead-time orders due, units coming back, deliveries expected,
    overnight sales, Buy Box)."""
    now = datetime.now()
    if now.hour < 8 or now.hour >= 11:               # a "good morning" at lunch time is no use: skip that day
        return
    flag = DEBUG_DIR / f"morning_{now.date().isoformat()}.txt"
    if flag.exists():
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    flag.write_text(now.isoformat(), encoding="utf-8")
    import stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    parts = []
    try:
        start = (datetime.combine(now.date() - timedelta(days=1), datetime.min.time()) + timedelta(hours=19)).isoformat() + "Z"  # 21:00 SA yesterday
        n = sum(int(x.get("qty") or 1) for x in db.select("takealot_sales", f"select=qty&order_date=gte.{start}"))
        if n:
            parts.append(f"{n} sold overnight")
    except Exception:
        pass
    try:
        got = db.select("app_state", "select=value&key=eq.leadtime_due")
        orders = ((got[0].get("value") or {}).get("orders") or []) if got else []
        late = sum(1 for o in orders if int(o.get("left") or 0) < 0)
        due = sum(1 for o in orders if 0 <= int(o.get("left") or 0) <= 1)
        if late:
            parts.append(f"⛔ {late} lead-time order{'s' if late > 1 else ''} late")
        if due:
            parts.append(f"⏰ {due} lead-time order{'s' if due > 1 else ''} due today/tomorrow")
    except Exception:
        pass
    try:
        back = db.select("units", "select=id,courier_shipment_id&status=eq.return_me")
        live = {str(x["id"]) for x in db.select("courier_shipments", "select=id,status&direction=eq.from_takealot")
                if x.get("status") not in ("delivered", "cancelled")}
        moving = sum(1 for u in back if str(u.get("courier_shipment_id") or "") in live)
        waiting = len(back) - moving
        if moving:
            parts.append(f"↩️ {moving} unit{'s' if moving > 1 else ''} on the way back to you")
        if waiting:
            parts.append(f"⏳ {waiting} still at Takealot (no collection booked)")
        exp = [x for x in db.select("courier_shipments", "select=waybill,status,expected_delivery_to,direction&direction=eq.from_takealot")
               if x.get("status") not in ("delivered", "cancelled") and str(x.get("expected_delivery_to") or "9")[:10] <= now.date().isoformat()]
        if exp:
            parts.append("🚚 courier due today: " + ", ".join(x["waybill"] for x in exp[:3]))
    except Exception:
        pass
    try:
        rules = [r for r in db.select("price_rules", "select=enabled,winning,last_action") if r.get("enabled")]
        if rules:
            won = sum(1 for r in rules if r.get("winning"))
            best = sum(1 for r in rules if not r.get("winning") and str(r.get("last_action") or "").startswith("Best Price"))
            parts.append(f"Buy Box {won}/{len(rules)}" + (f" (+{best} Best Price)" if best else ""))
    except Exception:
        pass
    body = " · ".join(parts) or "Nothing urgent today"
    notify(cfg, "☀️ Good morning: today on Takealot", body.replace(",", " ") if "courier due" not in body else body,
           url="./?view=overview", topic="price", log=log, tag="morning")
    log("Morning summary sent: " + body)


def evening_summary(cfg, log):
    """Once a day after 21:00: today's sales, profit and Buy Box on the phones."""
    now = datetime.now()
    if now.hour < 21:
        return
    flag = DEBUG_DIR / f"evening_{now.date().isoformat()}.txt"
    if flag.exists():
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    flag.write_text(now.isoformat(), encoding="utf-8")
    import stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    start = (datetime.combine(now.date(), datetime.min.time()) - timedelta(hours=2)).isoformat() + "Z"   # SA midnight in UTC
    sales = db.select("takealot_sales", f"select=order_id,qty,gross,unit_id&order_date=gte.{start}")
    ids = [x["unit_id"] for x in sales if x.get("unit_id")]
    units = db.select("units", "select=id,gross,fees,cost") if ids else []
    um = {u["id"]: u for u in units}
    # fees not in yet: use the average fee rate of your settled sales
    paid = [u for u in units if u.get("fees") is not None and float(u.get("gross") or 0) > 0]
    rate = (sum(float(u["fees"]) for u in paid) / sum(float(u["gross"]) for u in paid)) if paid else 0.12
    n, g, pr = 0, 0.0, 0.0
    for x in sales:
        q = int(x.get("qty") or 1)
        gross = float(x.get("gross") or 0)
        n += q
        g += gross
        u = um.get(x.get("unit_id"))
        if u:
            fees = float(u["fees"]) if u.get("fees") is not None else gross * rate
            pr += gross - fees - float(u.get("cost") or 0)
    rules = [r for r in db.select("price_rules", "select=enabled,winning") if r.get("enabled")]
    won = sum(1 for r in rules if r.get("winning"))
    body = (f"{n} sold · R{g:,.0f} sales · ≈R{pr:,.0f} profit" if n else "No sales today") + \
           (f" · Buy Box on {won} of {len(rules)}" if rules else "")
    notify(cfg, "📊 Today on Takealot", body.replace(",", " "), topic="price", log=log)
    log("Evening summary sent: " + body)


def balance_snapshot(ctx, auth, log):
    """Every sync (about every 30 min): Current / Held back / Available, plus every transaction not seen before,
    with the time it first appeared. Saved to debug/balance_log.jsonl."""
    if not auth:
        return
    seen = set()
    try:
        for line in BALANCE_LOG.read_text(encoding="utf-8").splitlines():
            seen.update(json.loads(line).get("new_ids") or [])
    except Exception:
        pass
    r = ctx.request.get(f"https://{API_HOST}/v1/seller/balances", headers=auth, timeout=60_000)
    if not r.ok:
        return
    bal = (r.json() or {}).get("balances") or {}
    d_to = datetime.now().date()
    d_from = d_to - timedelta(days=4)
    new, pn = [], 1
    while pn < 20:
        q = ctx.request.get(f"https://{API_HOST}/v1/seller/transactions?date_from={d_from}&date_to={d_to}&page_size=100&page_number={pn}",
                            headers=auth, timeout=60_000)
        if not q.ok:
            break
        j = q.json()
        rows = j.get("transactions") or []
        for x in rows:
            if x["transaction_log_id"] not in seen:
                new.append(x)
        if not rows or pn * 100 >= (j.get("page_summary") or {}).get("total", 0):
            break
        pn += 1
    snap = {"at": datetime.now().isoformat(timespec="seconds"), "current": bal.get("current"), "held_back": bal.get("held_back"),
            "available": bal.get("available"), "new_ids": [x["transaction_log_id"] for x in new],
            "new": [{"id": x["transaction_log_id"], "date": x["date_created"], "type": x["transaction_type"]["description"],
                     "ref": x["reference"], "amount": x["inc_vat"]} for x in new]}
    with open(BALANCE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(snap) + "\n")
    log(f"Takealot balance: current R{float(bal.get('current') or 0):,.2f}, held back R{float(bal.get('held_back') or 0):,.2f}, "
        f"available R{float(bal.get('available') or 0):,.2f} ({len(new)} new transactions).")


WAREHOUSE_NAMES = {1: "CPT", 3: "JHB", 6: "DBN"}


def apply_leadtime_requests(ctx, cfg, auth, offers, log, sales=None):
    if not offers:                                    # Takealot's offers list didn't load: try again next sync
        return
    """Set a listing's lead-time stock on Takealot (PATCH /v2/offers/offer/{id}). Only what you asked for in the app."""
    import stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    wanted = []                                       # (request id or None, sku, qty)
    req = DEBUG_DIR / "set_leadtime.json"
    if req.exists():
        try:
            wanted += [(None, str(w["sku"]), int(w["qty"])) for w in json.loads(req.read_text(encoding="utf-8"))]
        finally:
            req.unlink()
    try:
        rows = db.select("leadtime_requests", "select=id,sku,qty&done_at=is.null&order=id.asc")
    except RuntimeError:
        rows = []                                     # before 11_database_update.sql
    latest = {}
    for r in rows:
        latest[str(r["sku"])] = r                     # the newest request per listing wins
    stale = [r for r in rows if latest[str(r["sku"])] is not r]
    now = datetime.utcnow().isoformat() + "Z"
    for r in stale:
        db.patch("leadtime_requests", f"id=eq.{r['id']}", {"done_at": now, "ok": False, "result": "Replaced by a newer change"})
    wanted += [(r["id"], str(r["sku"]), int(r["qty"])) for r in latest.values()]
    by_sku = {str(o.get("merchant_sku")): o for o in (offers or [])}
    # products.leadtime_fixed: a number = keep the listing at that number (supplier stock);
    #                          -1 = follow what you have at HOME (new units, In stock) for that product
    try:
        asked = {sku for _, sku, _ in wanted}
        prods = db.select("products", "select=sku,product,takealot_title,leadtime_fixed")
        rules = [f for f in prods if f.get("leadtime_fixed") is not None]
        home = {}
        if any(int(f["leadtime_fixed"]) < 0 for f in rules):
            group = {}
            for p in prods:
                if p.get("product") and p["product"] != "__ignore__":
                    group[p.get("takealot_title") or ""] = p["product"]
                    group[p["product"]] = p["product"]
                elif p.get("takealot_title"):               # not linked to a product yet: its own group
                    group[p["takealot_title"]] = p["takealot_title"]
            for u in db.select("units", "select=product,condition&status=eq.in_stock&location=eq.HOME"):
                if (u.get("condition") or "new") == "new":
                    g = group.get(u["product"], u["product"])
                    home[g] = home.get(g, 0) + 1
            # new lead-time orders the sync hasn't put against a unit yet: those units are already sold,
            # so don't put them back on offer (Takealot lowered the listing when the order came in)
            linked = {str(u.get("order_id")) for u in db.select("units", "select=order_id&order_id=not.is.null")}
            by_sku_prod = {str(p["sku"]): (p.get("product") or p.get("takealot_title")) for p in prods}
            for x in {str(y.get("order_id")) + ":" + str(y.get("sku")): y for y in (sales or [])}.values():
                if str(x.get("sale_status") or "") in ("New Lead Time Order", "Confirmed Shipment") and str(x.get("order_id")) not in linked:
                    g = by_sku_prod.get(str(x.get("sku")))
                    if g in home:
                        home[g] = max(0, home[g] - int(x.get("quantity") or 1))
        for f in rules:
            sku = str(f["sku"])
            o = by_sku.get(sku)
            if not o or sku in asked:
                continue
            target = home.get(f.get("product") or f.get("takealot_title"), 0) if int(f["leadtime_fixed"]) < 0 else int(f["leadtime_fixed"])
            if (o.get("total_merchant_stock") or 0) != target:
                wanted.append((None, sku, target))
    except RuntimeError:
        pass                                          # before 11_database_update.sql
    if not wanted:
        return
    for rid, sku, qty in wanted:
        o = by_sku.get(sku)
        ok, msg = False, ""
        if not o:
            msg = "Listing not found on Takealot"
        else:
            mw = ((o.get("stock") or {}).get("merchant_warehouse") or [{}])[0].get("merchant_warehouse_id")
            if not mw:
                mw = 51981                            # your own warehouse on Takealot
            before = o.get("total_merchant_stock") or 0
            r = ctx.request.patch(f"https://{API_HOST}/v2/offers/offer/{o['offer_id']}", headers={**auth, "content-type": "application/json"},
                                  data=json.dumps({"leadtime_stock": [{"merchant_warehouse_id": mw, "quantity": qty}]}), timeout=60_000)
            errs = None
            try:
                errs = (r.json() or {}).get("validation_errors")
            except Exception:
                pass
            if errs and "leadtime days" in str(errs).lower() and qty > 0:
                # a new listing with no lead-time days: Takealot won't take lead-time stock until they're set.
                # Set them (same as your other listings: 4 working days unless another listing says otherwise), then try again.
                days = 4
                try:
                    ds = [int(((x.get("leadtime_days") or {}).get("minimum")) or 0) for x in by_sku.values() if (x.get("total_merchant_stock") or 0) > 0]
                    days = max([d for d in ds if d > 0] or [4])
                except Exception:
                    pass
                r = ctx.request.patch(f"https://{API_HOST}/v2/offers/offer/{o['offer_id']}", headers={**auth, "content-type": "application/json"},
                                      data=json.dumps({"leadtime_days": days, "leadtime_stock": [{"merchant_warehouse_id": mw, "quantity": qty}]}), timeout=60_000)
                errs = None
                try:
                    errs = (r.json() or {}).get("validation_errors")
                except Exception:
                    pass
                log(f"Lead-time stock: set lead-time days to {days} on {sku} first: {'ok' if r.ok and not errs else 'refused'}.")
            title = ((o.get("tsin") or {}).get("title") or sku)[:60]
            if r.ok and not errs:
                ok, msg = True, f"Changed from {before} to {qty}"
                o["total_merchant_stock"] = qty
                try:                                  # show the new number in the app straight away
                    p = db.select("products", f"select=takealot_stock&sku=eq.{sku}")
                    st = (p[0].get("takealot_stock") or {}) if p else {}
                    st["LEADTIME"] = {**(st.get("LEADTIME") or {}), "qty": qty}
                    db.patch("products", f"sku=eq.{sku}", {"takealot_stock": st})
                except Exception:
                    pass
            else:
                msg = f"Takealot said no ({r.status}): {(errs[0].get('message') if errs and isinstance(errs[0], dict) else errs) or r.text()[:150]}"
            log(f"Lead-time stock: {title}: {msg}.")
        if rid is not None:
            db.patch("leadtime_requests", f"id=eq.{rid}", {"done_at": datetime.utcnow().isoformat() + "Z", "ok": ok, "result": msg[:300]})


def save_offer_stock(cfg, offers, log):
    """Store what Takealot holds per listing and warehouse in the products table (for the app's Stock check).
    Only listings that are buyable, hold stock, or are already in the products table are saved."""
    import stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    try:
        known = {p["sku"] for p in db.select("products", "select=sku")}
    except Exception as e:
        log("Stock check: couldn't read products: " + repr(e)[:150])
        return
    now = datetime.utcnow().isoformat() + "Z"
    rows, new_found = [], []
    for o in offers:
        sku = str(o.get("merchant_sku") or "")
        if not sku:
            continue
        status = ((o.get("status") or {}).get("description") or "")
        total = (o.get("total_takealot_stock") or 0) + (o.get("total_stock_on_way") or 0) + (o.get("total_stock_in_receiving") or 0)
        # keep every live listing (Buyable or Not Buyable, e.g. a new lead-time listing with no stock yet);
        # only skip disabled listings that hold nothing and aren't in the products table already
        if sku not in known and status.lower().startswith("disabled") and not total:
            continue
        stock = {}
        for w in ((o.get("stock") or {}).get("takealot_warehouse") or []):
            name = WAREHOUSE_NAMES.get(w.get("takealot_warehouse_id"), str(w.get("takealot_warehouse_id")))
            stock[name] = {"qty": w.get("quantity") or 0, "on_way": w.get("stock_on_way") or 0,
                           "receiving": w.get("stock_in_receiving") or 0, "sold_30d": w.get("quantity_sold_30_days") or 0}
        # for the price fighter: how to find this listing on takealot.com and change its price
        stock["OFFER"] = {"offer_id": o.get("offer_id"), "plid": ((o.get("tsin") or {}).get("productline_id")),
                          "price": o.get("selling_price"), "merchant_id": o.get("merchant_id")}
        # lead-time stock: what you offer from your own warehouse (HOME)
        stock["LEADTIME"] = {"qty": o.get("total_merchant_stock") or 0,
                             "days": ((o.get("leadtime_days") or {}).get("minimum"))}
        row = {"sku": sku, "tsin": str(o.get("tsin_id") or ""), "takealot_title": ((o.get("tsin") or {}).get("title") or ""),
               "takealot_status": status, "takealot_stock": stock, "stock_updated_at": now}
        rows.append(row)
        if known and sku not in known and not status.lower().startswith("disabled"):
            new_found.append(row)
    if new_found:   # a listing you just created on Takealot: tell the phone once, and keep it on the app's Needs you for 2 weeks
        try:
            st = {}
            try:
                got = db.select("app_state", "select=value&key=eq.new_listings")
                st = (got[0].get("value") or {}) if got else {}
            except Exception:
                pass
            for r in new_found:
                if r["sku"] in st:
                    continue
                st[r["sku"]] = {"first_seen": now[:10], "title": r["takealot_title"][:90]}
                notify(cfg, "🆕 New Takealot listing", f"{r['takealot_title'][:80]} ({r['takealot_status'] or 'new'}). "
                       "Link it to your stock and set lead time on the Takealot products page.",
                       url="./?view=products", topic="price", log=log, tag=f"newlisting-{r['sku']}")
            cut = (datetime.utcnow() - timedelta(days=30)).date().isoformat()
            _app_state(cfg, "new_listings", {k: v for k, v in st.items() if v.get("first_seen", "") >= cut})
        except Exception as e:
            log("New listing alert failed: " + repr(e)[:150])
    try:  # what the offers feed returned, so the app can show listings that didn't come through
        seen = [{"sku": str(o.get("merchant_sku") or ""), "plid": ((o.get("tsin") or {}).get("productline_id")),
                 "title": ((o.get("tsin") or {}).get("title") or "")[:90],
                 "status": ((o.get("status") or {}).get("description") or "")} for o in offers]
        _app_state(cfg, "offers_seen", {"at": now, "count": len(offers), "offers": seen})
    except Exception as e:
        log("Stock check: couldn't save the offers list: " + repr(e)[:150])
    try:
        if rows:
            db.insert("products", rows, upsert_on="sku")
    except RuntimeError as e:
        if "takealot_stock" in str(e) or "takealot_status" in str(e) or "column" in str(e):
            log("Stock check: run 3_database_update.sql in Supabase to switch it on.")
        else:
            log("Stock check: couldn't save Takealot stock: " + str(e)[:150])


COURIER_API = "https://api.portal.thecourierguy.co.za"


def _dc_of(addr: str) -> str:
    a = (addr or "").lower()
    if "takealot" not in a and "tal " not in a:
        return ""
    if "cape town" in a or "cpt" in a or "montague" in a:
        return "CPT"
    if "durban" in a or "dbn" in a or "kzn" in a:
        return "DBN"
    if "jhb" in a or "johannesburg" in a or "witfontein" in a or "gp," in a:
        return "JHB"
    return "?"


def _first(d, *keys):
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if v not in (None, "", []):
            return v
    return None


def fetch_courier(ctx, cfg, log):
    """Read Courier Guy shipments to/from Takealot warehouses (last 60 days) and save them to courier_shipments."""
    auth = {}
    page = ctx.new_page()

    def on_req(r):
        if COURIER_API in r.url and not auth:
            h = {k: v for k, v in r.headers.items() if k.lower() == "authorization"}
            if h:
                auth.update(h)
    page.on("request", on_req)
    page.goto(COURIER_URL, wait_until="domcontentloaded", timeout=90_000)
    for _ in range(30):
        if auth:
            break
        page.wait_for_timeout(500)
    if not auth or page.locator("input[type=password]").count() or "login" in page.url.lower():
        ok = lambda pg: "thecourierguy" in pg.url and "login" not in pg.url.lower() and pg.locator("input[type=password]").count() == 0
        if auto_login(page, "courier", log, ok, cfg=cfg):
            page.goto(COURIER_URL, wait_until="domcontentloaded", timeout=90_000)
            for _ in range(30):
                if auth:
                    break
                page.wait_for_timeout(500)
        if not auth:
            log("Courier Guy login needed: double-click COURIER_LOGIN.bat in the Sync folder.")
            page.close()
            return
    from urllib.parse import quote
    shipments, off = [], 0
    while off < 1000:
        q = (f"{COURIER_API}/shipments?date_filter=time_created&absolute_query={quote('last 60 days')}"
             f"&offset={off}&limit=100&order_by=time_created&order=DESC&summary=shipment&provider_id=7")
        r = ctx.request.get(q, headers=auth, timeout=60_000)
        if not r.ok:
            log(f"Courier Guy shipments feed answered {r.status}")
            break
        got = (r.json() or {}).get("shipments") or []
        shipments += got
        if len(got) < 100:
            break
        off += 100
    rows, details_saved = [], False
    for sh in shipments:
        col = ((sh.get("collection_address") or {}).get("entered_address") or "")
        dlv = ((sh.get("delivery_address") or {}).get("entered_address") or "")
        dc_to, dc_from = _dc_of(dlv), _dc_of(col)
        if not dc_to and not dc_from:
            continue                                   # not a Takealot shipment
        direction = "to_takealot" if dc_to else "from_takealot"
        detail, notes = {}, []
        if sh.get("status") not in ("delivered", "cancelled"):
            try:
                rd = ctx.request.get(f"{COURIER_API}/shipments?id={sh['id']}&provider_id=7", headers=auth, timeout=60_000)
                if rd.ok:
                    j = rd.json() or {}
                    lst = j.get("shipments") if isinstance(j, dict) else None
                    detail = (lst[0] if lst else j) if isinstance(j, dict) else {}
                if sh.get("external_note_count") or sh.get("total_note_count"):
                    rn = ctx.request.get(f"{COURIER_API}/shipments/notes/external?shipment_id={sh['id']}&offset=0&limit=30&provider_id=7",
                                         headers=auth, timeout=60_000)
                    if rn.ok:
                        notes = (rn.json() or {}).get("shipment_notes") or []
                if not details_saved:
                    DEBUG_DIR.mkdir(exist_ok=True)
                    (DEBUG_DIR / "courier_detail.json").write_text(json.dumps({"detail": detail, "notes": notes},
                                                                             indent=1, default=str)[:60000], encoding="utf-8")
                    details_saved = True
            except Exception as e:
                log("Courier Guy detail couldn't be read: " + repr(e)[:150])
        d = {**sh, **detail}
        note_texts = []
        for n in notes if isinstance(notes, list) else []:
            t = _first(n, "message", "note", "content", "text", "body") if isinstance(n, dict) else str(n)
            if t:
                import re as _re
                t = _re.sub(r"<[^>]+>", " ", str(t))
                t = _re.sub(r"(\d{4})(CONFIRMED|PENDING|RESCHEDULED)", r"\1 \2 ", t)
                t = _re.sub(r"^(COLLECTION|DELIVERY)(\d)", r"\1 \2", t.strip())
                note_texts.append(" ".join(t.split()))
        reason = _first(d, "collection_rescheduled_reason", "reason_for_reschedule", "requested_date_change_reason")
        if reason:
            note_texts.insert(0, f"Collection date changed: {reason}")
        exp_col = _first(d, "collection_min_date", "estimated_collection")
        exp_from = _first(d, "estimated_delivery_from", "estimated_delivery_date_from")
        exp_to = _first(d, "estimated_delivery_to", "estimated_delivery_date_to")
        dmin = _first(d, "delivery_min_date")
        if not exp_from and dmin and (not exp_col or str(dmin)[:10] >= str(exp_col)[:10]):
            exp_from = dmin
        rows.append({
            "id": str(sh["id"]), "waybill": sh.get("short_tracking_reference"), "reference": sh.get("customer_reference") or "",
            "status": sh.get("status"), "direction": direction, "takealot_dc": dc_to or dc_from,
            "collection_name": col.split(",")[0], "delivery_name": dlv.split(",")[0],
            "service": sh.get("service_level_code"), "parcels": len(sh.get("parcels") or []) or None,
            "created_at": sh.get("time_created"),
            "expected_collection": str(exp_col)[:10] if exp_col else None,
            "expected_delivery_from": str(exp_from)[:10] if exp_from else None,
            "expected_delivery_to": str(exp_to)[:10] if exp_to else None,
            "delivered_at": _first(d, "delivered_date", "time_delivered", "delivery_date") if sh.get("status") == "delivered" else None,
            "note": " | ".join(note_texts + ([sh["latest_tracking_message"]] if sh.get("latest_tracking_message") else []))[:1000],
            "raw": {k: v for k, v in d.items() if k not in ("rates",)},
            "updated_at": datetime.utcnow().isoformat() + "Z",
        })
    # one-off discovery: open one shipment's details view and record the data calls it makes
    try:
        if not (DEBUG_DIR / "courier_click.json").exists():
            seen = []
            page.on("response", lambda r: seen.append({"url": r.url, "status": r.status, "body": (r.text()[:6000] if "json" in (r.headers.get("content-type") or "") else "")})
                    if COURIER_API in r.url else None)
            pick = next((x for x in rows if x["direction"] == "from_takealot" and x["status"] not in ("delivered", "cancelled")), rows[0] if rows else None)
            if pick:
                page.get_by_text(pick["waybill"], exact=True).first.click(timeout=15_000)
                page.wait_for_timeout(5000)
                (DEBUG_DIR / "courier_click.json").write_text(json.dumps({"waybill": pick["waybill"], "page": page.url, "calls": seen},
                                                                        indent=1), encoding="utf-8")
                log(f"Courier Guy: recorded the details view of {pick['waybill']}.")
    except Exception as e:
        log("Courier Guy details discovery: " + repr(e)[:150])
    # billing: Courier Guy top-ups (payments into the account) -> expenses
    try:
        acct = next((sh.get("account", {}).get("id") for sh in shipments if sh.get("account")), None)
        base = f"{COURIER_API}/billing/transactions?active_only=false&limit=100&provider_id=7" + (f"&account_id={acct}" if acct else "")
        txns, off = [], 0
        while off < 20000:
            rb = ctx.request.get(f"{base}&offset={off}", headers=auth, timeout=60_000)
            if not rb.ok:
                log(f"Courier Guy billing answered {rb.status}")
                break
            got = (rb.json() or {}).get("transactions") or []
            txns += got
            if len(got) < 100:
                break
            off += 100
        if (DEBUG_DIR / "courier_billing_dump.txt").exists():      # one-off: keep the raw billing lines for checking
            (DEBUG_DIR / "courier_billing_dump.txt").unlink()
            (DEBUG_DIR / "courier_billing.json").write_text(json.dumps(txns, indent=1, default=str), encoding="utf-8")
            log(f"Courier Guy billing: {len(txns)} lines saved for checking.")
        cost_by_ship = {}
        for t in txns:
            if t.get("shipment_id"):
                cost_by_ship[str(t["shipment_id"])] = cost_by_ship.get(str(t["shipment_id"]), 0) - float(t.get("amount") or 0)
        for row in rows:
            if row["id"] in cost_by_ship:
                row["cost"] = round(cost_by_ship[row["id"]], 2)
        since = cfg.get("since") or "2026-10-01"
        exp = []
        for t in txns:
            if t.get("type") != "payment" or t.get("has_been_reversed"):
                continue
            day = str(t.get("transaction_date") or t.get("effective_date") or "")[:10]
            if day < since:
                continue
            exp.append({"id": f"courier-{t['id']}", "date": day, "category": "courier_topup",
                        "amount": round(float(t.get("amount") or 0), 2),
                        "description": "Courier Guy top-up" + (f" ({t['sub_type']})" if t.get("sub_type") else ""),
                        "source": "sync", "raw": t})
        if exp:
            import stockrecon_sync as S
            try:
                S.DB(cfg["url"], cfg["key"]).insert("expenses", exp, upsert_on="id")
            except RuntimeError as e:
                log("Courier top-ups: run 6_database_update.sql in Supabase to switch this on." if "expenses" in str(e)
                    else "Courier top-ups couldn't be saved: " + str(e)[:150])
                exp = []
        log(f"Courier Guy: {len(exp)} top-up(s) since {since} saved ({len(txns)} billing lines read).")
    except Exception as e:
        log("Courier Guy billing couldn't be read: " + repr(e)[:150])
    page.close()
    if rows:
        import stockrecon_sync as S
        db = S.DB(cfg["url"], cfg["key"])
        keys = set().union(*(r.keys() for r in rows))
        rows = [{k: r.get(k) for k in keys} for r in rows]          # same columns on every row
        try:
            db.insert("courier_shipments", rows, upsert_on="id")
        except RuntimeError as e:
            if "cost" in str(e):
                log("Courier costs: run 7_database_update.sql in Supabase to switch this on.")
                db.insert("courier_shipments", [{k: v for k, v in r.items() if k != "cost"} for r in rows], upsert_on="id")
            else:
                raise
    log(f"Courier Guy: {len(rows)} Takealot shipment(s) saved ({len(shipments)} read).")
    if rows:
        try:
            handle_rejected(cfg, rows, log)
        except Exception as e:
            log("Rejected shipments couldn't be handled: " + repr(e)[:200])
        try:
            receive_returns(cfg, rows, log)
        except Exception as e:
            log("Delivered returns couldn't be booked in: " + repr(e)[:200])


REJECTED = {"returned-to-sender", "returned-to-hub", "delivery-rejected"}


def handle_rejected(cfg, rows, log):
    """A shipment to Takealot that Takealot turned away (returned to sender).
    The units on that PO never got in, so:
      - any sale matched to them was really filled from Takealot's own stock at that DC ->
        move the sale to the oldest unsold unit of the same product at that DC;
      - the units themselves are coming back to us -> Return: me, linked to the return waybill (e.g. NRVWMM-R)."""
    import re, stockrecon_sync as S
    since = cfg.get("since") or "2026-10-01"
    bad = [r for r in rows if r["direction"] == "to_takealot" and r["status"] in REJECTED
           and str(r.get("created_at") or "")[:10] >= since]
    if not bad:
        return
    db = S.DB(cfg["url"], cfg["key"])
    units = db.select("units", "select=id,product,sku,location,po,order_id,sold_date,gross,fees,status,match_method,"
                               "comment,received,created_at,courier_shipment_id")
    groups = {p["sku"]: p["product"] for p in db.select("products", "select=sku,product") if p.get("product")}
    grp = lambda u: groups.get(u.get("sku") or "") or u.get("product")
    now = datetime.utcnow().isoformat() + "Z"
    dfile = DEBUG_DIR / "rejected_done.json"
    try:
        done = json.loads(dfile.read_text(encoding="utf-8"))
    except Exception:
        done = {}
    for x in bad:
        po = (re.search(r"\d{6,}", x.get("reference") or "") or [None])[0]
        if not po:
            continue
        back = next((r for r in rows if r["direction"] == "from_takealot" and r["status"] != "cancelled"
                     and (r.get("waybill") or "").upper() == (x.get("waybill") or "").upper() + "-R"), None) \
            or next((r for r in rows if r["direction"] == "from_takealot" and r["status"] != "cancelled"
                     and po in (r.get("reference") or "") and str(r.get("created_at")) >= str(x.get("created_at"))), None)
        link = (back or x)["id"]
        m = re.search(r"rejected by Takealot due to (.+?) and", x.get("note") or "", re.I)
        why = f" ({m.group(1).strip().lower()})" if m else ""
        seen = done.get(str(x["id"]))                       # units that were on the PO when this rejection was first handled
        on_po = [u for u in units if u.get("po") == po]
        if seen is None:
            done[str(x["id"])] = [u["id"] for u in on_po]
        for u in on_po:
            if seen is not None and u["id"] not in seen and u.get("courier_shipment_id") != x["id"]:
                continue                                   # put on this PO after the rejection (a re-send): not rejected
            if u.get("courier_shipment_id") == link:
                continue                                   # already handled
            if u.get("courier_shipment_id") == x["id"] and u["status"] == "return_me":   # handled before the return waybill existed
                if link != x["id"]:
                    db.patch("units", f"id=eq.{u['id']}", {"courier_shipment_id": link, "updated_at": now})
                continue
            # (a unit still In stock that was sent on this very shipment never got in either: handle it below)
            if u["status"] not in ("in_stock", "awaiting_fees", "settled"):
                continue
            if f"rejected on {x['waybill']}; send it in" in (u.get("comment") or ""):
                continue                                   # the replacement we put on this PO, not a rejected unit
            note = f"Takealot rejected {x['waybill']} (PO {po}){why}; coming back on {back['waybill'] if back else 'a return waybill'}."
            if u.get("order_id"):
                # a lead-time order: Takealot has none of its own to fill it with, so it waits for the replacement you
                # send in on the same PO -> the oldest new unit at HOME takes the order (and the PO)
                leadtime = u.get("match_method") == "leadtime-home" or "Lead-time order" in (u.get("comment") or "")
                where = "HOME" if leadtime else x["takealot_dc"]
                pool = sorted([v for v in units if v["status"] == "in_stock" and not v.get("order_id") and v.get("po") != po
                               and v.get("location") == where and grp(v) == grp(u)],
                              key=lambda v: (str(v.get("received") or ""), str(v.get("created_at") or "")))
                if not pool:
                    log(f"  Rejected PO {po}: no unsold {u['product']} at {where} to move order {u['order_id']} to; left as is.")
                    continue
                v = pool[0]
                moved = {k: u.get(k) for k in ("order_id", "sold_date", "gross", "fees", "status", "match_method", "product", "sku")}
                moved["comment"] = ((v.get("comment") or "") + (
                    f" Lead-time order {u['order_id']}: replacement for the unit Takealot rejected on {x['waybill']}; send it in on PO {po}."
                    if leadtime else f" Order {u['order_id']} moved here: PO {po} was rejected, so Takealot filled it from its {x['takealot_dc']} stock.")).strip()
                if leadtime:
                    moved["po"] = po
                db.patch("units", f"id=eq.{u['id']}", {"order_id": "", "sold_date": None, "gross": None, "fees": None})
                db.patch("units", f"id=eq.{v['id']}", moved | {"updated_at": now})
                db.patch("takealot_sales", f"order_id=eq.{u['order_id']}", {"unit_id": v["id"]})
                v.update(moved)
                log(f"  Rejected PO {po}: order {u['order_id']} moved to a {where} unit (in {v.get('received')}).")
            db.patch("units", f"id=eq.{u['id']}", {"status": "return_me", "location": x["takealot_dc"], "order_id": "",
                                                    "sold_date": None, "gross": None, "fees": None, "courier_shipment_id": link,
                                                    "comment": ((u.get("comment") or "") + " " + note).strip(), "updated_at": now})
            u.update(status="return_me", order_id="", courier_shipment_id=link)
            log(f"  Rejected PO {po}: {u['product']} set to Return: me ({note})")
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        dfile.write_text(json.dumps(done), encoding="utf-8")
    except Exception:
        pass



def fetch_takealot_pos(ctx, cfg, auth, log):
    """Takealot shipments (POs) due from ~6 weeks before the start date, with their product lines."""
    from datetime import date as _d, timedelta as _td
    cutoff = max(_d.fromisoformat(cfg.get("since") or "2026-10-01") - _td(days=45), _d.today() - _td(days=90)).isoformat()   # last 3 months, not all history
    ships, pg = [], 1
    while pg <= 100:
        r = ctx.request.get(f"https://{API_HOST}/v2/shipments?page_number={pg}&page_size=100", headers=auth, timeout=60_000)
        if not r.ok:
            log(f"Takealot shipments feed answered {r.status}")
            break
        j = r.json() or {}
        got = j.get("shipments") or []
        ships += got
        total = (j.get("page_summary") or {}).get("total") or 0
        if not got or len(ships) >= total:
            break
        pg += 1
    def due(sh):
        ts = ((sh.get("due_date") or {}).get("timestamp"))
        return datetime.fromtimestamp(ts).date().isoformat() if ts else None
    recent = [sh for sh in ships if (due(sh) or "") >= cutoff and sh.get("instruction_id")]
    rows = []
    for sh in recent:
        items, po_data = [], sh.get("purchase_order_data") or {}
        for state in ("", "&shipment_state=3", "&shipment_state=2", "&shipment_state=1"):
            ri = ctx.request.get(f"https://{API_HOST}/v1/shipment/{sh['shipment_id']}/shipment_items?page_number=1&page_size=100"
                                 f"&get_instruction_data=true&get_po_data=true{state}", headers=auth, timeout=60_000)
            if ri.ok:
                j = ri.json() or {}
                po_data = ((j.get("shipment") or {}).get("purchase_order_data")) or po_data
                for it in j.get("shipment_items") or []:
                    sl = it.get("seller_listing") or {}
                    q = it.get("quantity") or {}
                    items.append({"sku": str(sl.get("seller_sku") or ""), "title": sl.get("title") or "",
                                  "qty": q.get("sending") or q.get("required") or 0,
                                  "received": (it.get("purchase_order_data") or {}).get("received")})
                if items:
                    break
        qty = sh.get("quantity") or {}
        num = lambda v: v if isinstance(v, int) and v >= 0 else None
        rows.append({"po": str(sh["instruction_id"]), "shipment_id": str(sh["shipment_id"]), "reference": sh.get("reference"),
                     "warehouse": (sh.get("warehouse") or {}).get("name"), "facility": (sh.get("facility") or {}).get("code"),
                     "due_date": due(sh), "status": po_data.get("status") or ("Shipped" if sh.get("shipped") else "Not shipped"),
                     "leadtime": sh.get("shipment_type_id") == 1, "qty_sending": qty.get("sending"),
                     "qty_received": num(po_data.get("received")), "qty_damaged": num(po_data.get("damaged")),
                     "items": items, "raw": sh, "updated_at": datetime.utcnow().isoformat() + "Z"})
    if rows:
        import stockrecon_sync as S
        try:
            S.DB(cfg["url"], cfg["key"]).insert("takealot_pos", rows, upsert_on="po")
        except RuntimeError as e:
            log("Takealot shipments: run 7_database_update.sql in Supabase to switch this on." if "takealot_pos" in str(e)
                else "Takealot shipments couldn't be saved: " + str(e)[:150])
            return
    log(f"Takealot shipments: {len(rows)} PO(s) saved ({len(ships)} read).")
    try:
        allocate_po_units(cfg, rows, log)
    except Exception as e:
        log("Allocating units to new shipments failed: " + repr(e)[:200])


def mark_collected_shipped(ctx, cfg, auth, log):
    """When Courier Guy has collected a shipment to Takealot (its reference holds the PO number), mark that PO as
    shipped on the Seller Portal and put the waybill in its tracking info, as you'd do by hand."""
    import re as _r, stockrecon_sync as S
    if not auth:
        return
    db = S.DB(cfg["url"], cfg["key"])
    # the PO's own status says "Shipped" as soon as it's issued; whether YOU marked it shipped is the shipment's "shipped" flag
    open_pos = {p["po"]: p for p in db.select("takealot_pos", "select=po,shipment_id,status,reference,warehouse,shipped:raw->shipped")
                if p.get("shipment_id") and p.get("shipped") is False and not _r.search(r"cancel|receiv|clos", str(p.get("status") or ""), _r.I)}
    if not open_pos:
        return
    not_yet = ("cancelled", "new", "pending", "draft", "submitted", "quote", "quoted", "returned-to-sender", "unallocated")
    todo = []                                               # (po, how it went, tracking info)
    for c in db.select("courier_shipments", "select=waybill,reference,status,direction&direction=eq.to_takealot"):
        st = str(c.get("status") or "").lower()
        if not st or "collection" in st or st in not_yet:     # not collected yet (or never will be)
            continue
        for po in _r.findall(r"\b(\d{9})\b", str(c.get("reference") or "")):
            todo.append((po, f"Courier Guy collected it ({c['waybill']}, {st})", f"Courier Guy {c['waybill']}", c["waybill"]))
    # dropped off by Adam (or someone else): recorded in the app with the PO, on or before today
    try:
        for e in db.select("expenses", "select=category,date,raw&category=in.(adam_delivery,delivery_other)"):
            raw = e.get("raw") or {}
            po = str(raw.get("po") or "")
            if po and (e["category"] == "delivery_other" or raw.get("dropped")):     # only once you pressed "dropped off"
                who = "Adam" if e["category"] == "adam_delivery" else "own delivery"
                day = raw.get("dropped_at") or e["date"]
                todo.append((po, f"dropped off by {who} on {day}", f"Delivered by {who} {day}", who))
    except Exception:
        pass
    for po, how, tracking, ref in todo:
            p = open_pos.get(po)
            if not p:
                continue
            sid = p["shipment_id"]
            hdr = {**auth, "content-type": "application/json"}
            try:
                ctx.request.put(f"https://{API_HOST}/v1/shipment/{sid}/tracking_info", headers=hdr,
                                data=json.dumps({"tracking_info": tracking}), timeout=60_000)
            except Exception:
                pass
            ok, why = False, ""
            for path in (f"vi/shipment/{sid}/shipped?status=true", f"v1/shipment/{sid}/shipped?status=true"):
                r = ctx.request.put(f"https://{API_HOST}/{path}", headers=hdr, timeout=60_000)
                if r.ok:
                    ok = True
                    break
                why = f"{r.status} {r.text()[:120]}"
            if ok:
                db.patch("takealot_pos", f"po=eq.{po}", {"updated_at": datetime.utcnow().isoformat() + "Z"})
                open_pos.pop(po, None)
                log(f"Shipment PO {po}: {how}, so it's now marked as shipped on the Seller Portal.")
                try:
                    notify({"url": cfg["url"], "key": cfg["key"]}, "🚚 Shipment marked as shipped",
                           f"PO {po} ({p.get('warehouse') or ''}): {how}.", url="./?view=ship", topic="price", log=log)
                except Exception:
                    pass
            else:
                log(f"Shipment PO {po}: couldn't mark it as shipped on the Seller Portal ({why}). Please do it by hand.")


def allocate_po_units(cfg, rows, log):
    """A new shipment (PO) you created to send stock in: put that many of your HOME units on it, at its warehouse.
    Lead-time POs are left to the sales matching (the unit sold on that order goes on the PO)."""
    import stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    today = date.today().isoformat()
    new = [r for r in rows if not r.get("leadtime") and (r.get("due_date") or "") >= today
           and "cancel" not in str(r.get("status") or "").lower() and r.get("items")]
    if not new:
        return
    # a PO Takealot already rejected a shipment on: what goes on it again is up to you (re-send / Adam), never auto-filled
    import re as _re
    bad_po = {m.group(0) for c in db.select("courier_shipments", "select=reference,status,direction")
              if c.get("direction") == "to_takealot" and c.get("status") in REJECTED
              for m in [_re.search(r"\d{6,}", c.get("reference") or "")] if m}
    new = [r for r in new if str(r.get("po")) not in bad_po]
    if not new:
        return
    prods = db.select("products", "select=sku,product,takealot_title")
    grp_of_sku = {str(p["sku"]): (p.get("product") or p.get("takealot_title")) for p in prods}
    grp_of_name = {}
    for p in prods:
        g = p.get("product") or p.get("takealot_title")
        for n in (p.get("product"), p.get("takealot_title")):
            if n:
                grp_of_name[n] = g
    units = db.select("units", "select=id,product,status,location,po,order_id,condition,received,comment")
    now = datetime.utcnow().isoformat() + "Z"
    for r in new:
        dc = str(r.get("warehouse") or r.get("facility") or "")[:3].upper()
        if dc not in ("JHB", "CPT", "DBN"):
            continue
        for it in r["items"]:
            g = grp_of_sku.get(str(it.get("sku")))
            want = int(it.get("qty") or 0)
            if not g or want <= 0:
                continue
            on_po = [u for u in units if u.get("po") == r["po"] and grp_of_name.get(u["product"], u["product"]) == g]
            need = want - len(on_po)
            if need <= 0:
                continue
            pool = [u for u in units if u["status"] == "in_stock" and u.get("location") == "HOME" and not u.get("order_id")
                    and not u.get("po") and (u.get("condition") or "new") == "new" and grp_of_name.get(u["product"], u["product"]) == g]
            pool.sort(key=lambda u: (u.get("received") or "9999", u["id"]))
            for u in pool[:need]:
                c = (f"Sent to Takealot {dc} on PO {r['po']}. " + (u.get("comment") or "")).strip()
                db.patch("units", f"id=eq.{u['id']}", {"po": r["po"], "location": dc, "comment": c, "updated_at": now,
                                                        "courier_shipment_id": None})   # an old return waybill no longer applies
                u.update(po=r["po"], location=dc)
            got = min(need, len(pool))
            log(f"  PO {r['po']}: {got} × {g} moved from HOME to {dc}" + (f" ({need - got} not found at HOME)" if got < need else "") + ".")



def _find_comment(obj):
    """The customer's comment somewhere in a return's details."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "comment" in k.lower() and "seller" not in k.lower() and isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            c = _find_comment(v)
            if c:
                return c
    elif isinstance(obj, list):
        for v in obj:
            c = _find_comment(v)
            if c:
                return c
    return None


def save_returns(cfg, returns, log, ctx=None, auth=None):
    """Keep every return (from View Returns) in takealot_returns, so the app can count returns per month.
    Also reads each new return's details once for the customer's comment."""
    if not returns:
        return
    import matcher, stockrecon_sync as S
    parsed = matcher.parse_returns(returns)
    rows = []
    for raw, r in zip([x for x in returns if (x.get("order_id") or x.get("orderId"))], parsed):
        rid = str(raw.get("seller_return_id") or raw.get("rrn") or f"{r['order_id']}-{r['sku']}")
        rows.append({"return_id": rid, "rrn": raw.get("rrn"), "order_id": r["order_id"], "sku": r["sku"], "title": r["title"],
                     "qty": r["qty"], "dc": r["dc"], "reason": r["reason"], "outcome": r["outcome"], "return_date": r["return_date"],
                     "raw": {k: v for k, v in raw.items() if "customer" not in k.lower()},
                     "updated_at": datetime.utcnow().isoformat() + "Z"})
    try:
        S.DB(cfg["url"], cfg["key"]).insert("takealot_returns", rows, upsert_on="return_id")
    except RuntimeError as e:
        log("Returns history: run 8_database_update.sql in Supabase to switch this on." if "takealot_returns" in str(e)
            else "Returns history couldn't be saved: " + str(e)[:150])
        return
    # customer comments (once per return)
    if ctx is None or not auth:
        return
    db = S.DB(cfg["url"], cfg["key"])
    try:
        have = {r["return_id"] for r in db.select("takealot_returns", "select=return_id,customer_comment&customer_comment=not.is.null")}
    except RuntimeError:
        return                                                     # before 10_database_update.sql
    tried = DEBUG_DIR / "return_comments_tried.json"
    try:
        done = set(json.loads(tried.read_text(encoding="utf-8")))
    except Exception:
        done = set()
    for row in rows:
        rid = row["return_id"]
        if rid in have or rid in done or not rid.isdigit():
            continue
        try:
            r = ctx.request.get(f"https://{API_HOST}/v2/return/{rid}/details", headers=auth, timeout=60_000)
            if not r.ok:
                continue
            j = r.json()
            if not (DEBUG_DIR / "return_details.json").exists():
                DEBUG_DIR.mkdir(exist_ok=True)
                (DEBUG_DIR / "return_details.json").write_text(json.dumps(j, indent=1, default=str)[:20000], encoding="utf-8")
            c = _find_comment(j)
            done.add(rid)
            if c:
                db.patch("takealot_returns", f"return_id=eq.{rid}", {"customer_comment": c[:2000]})
                log(f"Return {row['order_id']}: customer comment saved.")
        except Exception as e:
            log(f"Return {row['order_id']}: details couldn't be read: {repr(e)[:120]}")
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        tried.write_text(json.dumps(sorted(done)), encoding="utf-8")
    except Exception:
        pass
