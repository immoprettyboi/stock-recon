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
    while time.time() < end and not code:
        rows = db.select("sync_runs", f"select=id,message&id=gt.{since_id}&message=like.otp:*&order=id.desc")
        if rows:
            code = re.sub(r"\D", "", rows[0]["message"])
            for r in rows:
                db._req("DELETE", f"sync_runs?id=eq.{r['id']}", None, {"Prefer": "return=minimal"})
            break
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
    if prev not in have and prev not in recon and prev >= "2026-10" and now.hour >= 6:
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
    units = db.select("units", "select=id,product,status,location,order_id,po,comment,courier_shipment_id,flag&status=eq.return_me")
    live_from = {}
    for r in rows:
        if r["direction"] == "from_takealot" and r["status"] not in ("cancelled",) and not str(r.get("waybill") or "").upper().endswith("-R"):
            live_from.setdefault(r["takealot_dc"], []).append(r)
    now = datetime.utcnow().isoformat() + "Z"
    for sid, x in delivered.items():
        mine = [u for u in units if u.get("courier_shipment_id") == sid]
        if not mine and not str(x.get("waybill") or "").upper().endswith("-R") and len(live_from.get(x["takealot_dc"], [])) == 1:
            mine = [u for u in units if not u.get("courier_shipment_id") and u.get("location") == x["takealot_dc"]]
        day = str(x.get("delivered_at") or now)[:10]
        for u in mine:
            c = (u.get("comment") or "").strip()
            if "Takealot rejected" in c:
                body = {"status": "in_stock", "location": "HOME", "flag": False, "updated_at": now,
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
        if _PUSH_READY is False:
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
            return None, None


def notify(cfg, title, body, url="./", topic="price", log=None):
    """Send a notification to every phone that switched notifications on in the app."""
    import stockrecon_sync as S
    key_file = HERE / "vapid_private.pem"
    if not key_file.exists():
        return
    db = S.DB(cfg["url"], cfg["key"])
    try:
        subs = db.select("push_subscriptions", "select=endpoint,keys,topics")
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
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": topic})
    for x in subs:
        try:
            webpush(subscription_info={"endpoint": x["endpoint"], "keys": x["keys"]}, data=payload,
                    vapid_private_key=str(key_file), vapid_claims={"sub": "mailto:stockrecon@example.com"}, ttl=3600)
            db.patch("push_subscriptions", f"endpoint=eq.{quote(x['endpoint'], safe='')}", {"last_sent_at": datetime.utcnow().isoformat() + "Z"})
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):                         # the phone unsubscribed / app removed
                try:
                    db._req("DELETE", f"push_subscriptions?endpoint=eq.{quote(x['endpoint'], safe='')}", None, {"Prefer": "return=minimal"})
                except Exception:
                    pass
            elif log:
                log(f"Notification failed ({code}): {str(e)[:120]}")
        except Exception as e:
            if log:
                log("Notification failed: " + repr(e)[:150])


# ---------------------------------------------------------------- price fighter
def _due(rule, now):
    lc = rule.get("last_checked")
    if not lc:
        return True
    try:
        t = datetime.fromisoformat(str(lc).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return True
    return (now - t).total_seconds() >= max(1, int(rule.get("check_minutes") or 10)) * 60 - 25


def price_tick(cfg, log, force=False):
    """Called every minute between syncs. Checks the Buy Box of each listing you switched on (when it's due) and,
    in fight mode, changes your price within your floor / ceiling. Watch mode only records what it would do."""
    import math, stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    try:
        rules = db.select("price_rules", "select=*&enabled=is.true")
    except RuntimeError:
        return                                            # before 13_database_update.sql
    now = datetime.utcnow()
    due = [r for r in rules if force or _due(r, now)]
    if not due:
        return
    prods = {p["sku"]: p for p in db.select("products", "select=sku,product,takealot_title,takealot_stock")}
    from playwright.sync_api import sync_playwright
    import stockrecon_sync as SS
    with sync_playwright() as p:
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
            if any(r.get("mode") == "fight" for r in due):
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
            for r in due:
                try:
                    _fight_one(ctx, tk, db, r, prods.get(r["sku"]) or {}, auth, log)
                except Exception as e:
                    log(f"Price fighter {r['sku']}: {repr(e)[:160]}")
        finally:
            ctx.close()


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
    return math.ceil((cost + float(rule.get("min_profit") or 0)) / max(0.5, 1 - ratio))


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
    got = {}
    try:                                                   # fast: ask Takealot's own product API from inside the page
        got = page.evaluate("""async (plid) => { const r = await fetch(`https://api.takealot.com/rest/v-1-19-0/product-details/PLID${plid}?platform=desktop`,
                                  {credentials: "include"}); return r.ok ? await r.json() : {}; }""", str(plid)) or {}
    except Exception:
        got = {}
    if not (got.get("buybox") or {}).get("items"):         # slower fallback: open the product page
        def on_resp(r):
            try:
                if "/product-details/PLID" in r.url and "json" in (r.headers.get("content-type") or "") and not got.get("buybox"):
                    got.update(r.json())
            except Exception:
                pass
        pg = ctx.new_page()
        pg.on("response", on_resp)
        try:
            pg.goto(f"https://www.takealot.com/x/PLID{plid}", wait_until="domcontentloaded", timeout=60_000)
            for _ in range(30):
                if got.get("buybox"):
                    break
                pg.wait_for_timeout(500)
        finally:
            pg.close()
    items = ((got.get("buybox") or {}).get("items")) or []
    item = next((i for i in items if i.get("is_selected")), items[0] if items else None)
    if not item:
        db.patch("price_rules", f"sku=eq.{sku}", {"last_checked": nowiso, "last_action": "Couldn't read the Buy Box"})
        return
    bb_price, bb_seller = float(item.get("price") or 0), str(item.get("sponsored_ads_seller_id") or "")
    winning = bb_seller == mine
    my_price = float(rule.get("my_price") or offer.get("price") or 0)
    if winning:
        my_price = bb_price
    floor = _floor_for(db, rule, prod)
    ceiling = float(rule["ceiling_price"]) if rule.get("ceiling_price") else None
    step = max(1.0, float(rule.get("step") or 1))
    fight = rule.get("mode") == "fight"
    # how many real changes in the last 24 h
    since = (datetime.utcnow() - timedelta(hours=24)).isoformat() + "Z"
    try:
        changes = len(db.select("price_log", f"select=id&sku=eq.{sku}&action=in.(lowered,raised,at_floor,reset)&at=gt.{since}"))
    except Exception:
        changes = 0
    target, action, note = None, None, ""
    if not winning:
        want = math.floor(bb_price - step)
        if floor is None:
            note = "No cost found for this product, so no floor: not changing the price"
        elif want >= floor:
            if want < my_price or my_price == 0:
                target, action = want, "lowered"
        elif (rule.get("floor_action") or "ceiling") == "ceiling" and ceiling:
            # can't win without going under the floor: go back up to the ceiling and let the other bot follow us up
            if my_price < ceiling:
                target, action, note = math.floor(ceiling), "reset", f"Competitor at R{bb_price:,.0f} is below your floor R{floor:,.0f}: back up to your ceiling"
            else:
                note = f"Waiting at your ceiling for the competitor (R{bb_price:,.0f}) to come back up"
        elif my_price > floor:
            target, action, note = math.ceil(floor), "at_floor", f"Buy Box R{bb_price:,.0f} is below your floor R{floor:,.0f}"
        else:
            note = f"Buy Box R{bb_price:,.0f} is below your floor R{floor:,.0f}: holding at the floor"
        if target is not None and ceiling and target > ceiling:
            target = math.floor(ceiling)
    elif rule.get("raise_when_winning") and ceiling and my_price < ceiling:
        lc = rule.get("last_change_at")
        quiet = True
        if lc:
            try:
                quiet = (datetime.utcnow() - datetime.fromisoformat(str(lc).replace("Z", "+00:00")).replace(tzinfo=None)).total_seconds() \
                        >= int(rule.get("raise_every_minutes") or 120) * 60
            except Exception:
                pass
        if quiet:
            target, action = math.floor(min(ceiling, my_price + step)), "raised"
    upd = {"last_checked": nowiso, "bb_price": bb_price, "bb_seller": bb_seller, "winning": winning,
           "floor_used": floor, "my_price": my_price}
    cfgn = {"url": db.base.replace("/rest/v1/", ""), "key": db.h["apikey"]}
    alerts = []
    if rule.get("winning") is not None and bool(rule.get("winning")) != winning:
        alerts.append(("✅ You won the Buy Box" if winning else "❌ You lost the Buy Box",
                       f"{title} · Buy Box R{bb_price:,.0f}" + ("" if winning else f" (you R{my_price:,.0f})")))
    if target is not None and int(target) != int(my_price):
        if not fight:
            action = "would_" + ("lower" if action in ("lowered", "at_floor") else "reset" if action == "reset" else "raise")
            upd["last_action"] = f"Would change R{my_price:,.0f} → R{target:,.0f} (watch mode)"
            if rule.get("last_action") != upd["last_action"]:          # log it once, not every check
                db.insert("price_log", [{"sku": sku, "my_price": my_price, "bb_price": bb_price, "bb_seller": bb_seller,
                                         "new_price": target, "action": action, "note": note or "watch mode"}])
        elif changes >= int(rule.get("max_changes_day") or 20):
            upd["last_action"] = f"Daily limit of {rule.get('max_changes_day')} changes reached"
        elif not auth or not offer_id:
            upd["last_action"] = "Not logged in to the Seller Portal: price not changed"
        else:
            r = ctx.request.patch(f"https://{API_HOST}/v2/offers/offer/{offer_id}", headers={**auth, "content-type": "application/json"},
                                  data=json.dumps({"selling_price": int(target)}), timeout=60_000)
            errs = None
            try:
                errs = (r.json() or {}).get("validation_errors")
            except Exception:
                pass
            if r.ok and not errs:
                db.insert("price_log", [{"sku": sku, "my_price": my_price, "bb_price": bb_price, "bb_seller": bb_seller,
                                         "new_price": target, "action": action, "note": note}])
                upd.update(my_price=target, last_change_at=nowiso, last_action=(f"Back to ceiling R{target:,.0f} (competitor below your floor)" if action == "reset" else f"{'Lowered' if action != 'raised' else 'Raised'} R{my_price:,.0f} → R{target:,.0f}"))
                st = dict(prod.get("takealot_stock") or {})
                st["OFFER"] = {**offer, "price": int(target)}
                db.patch("products", f"sku=eq.{sku}", {"takealot_stock": st})
                log(f"Price fighter: {title}: R{my_price:,.0f} → R{target:,.0f} (Buy Box R{bb_price:,.0f}, {'you' if winning else bb_seller}).")
                alerts.append((("🔺 Price raised" if action == "raised" else "🔻 Price lowered" if action == "lowered" else "↩️ Back up to your ceiling" if action == "reset" else "🔻 Down to your floor"),
                               f"{title}: R{my_price:,.0f} → R{target:,.0f} · Buy Box was R{bb_price:,.0f}"))
            else:
                msg = (errs[0].get("message") if errs and isinstance(errs[0], dict) else errs) or r.text()[:150]
                db.insert("price_log", [{"sku": sku, "my_price": my_price, "bb_price": bb_price, "bb_seller": bb_seller,
                                         "new_price": target, "action": "failed", "note": str(msg)[:300]}])
                upd["last_action"] = f"Takealot refused the change: {str(msg)[:120]}"
                alerts.append(("⚠️ Price change refused", f"{title}: Takealot said {str(msg)[:100]}"))
    else:
        upd["last_action"] = note or ("You have the Buy Box" if winning else "Holding")
        if note and "below your floor" in note and rule.get("last_action") != upd["last_action"]:
            alerts.append(("⚠️ Competitor below your floor", f"{title}: {note}"))
    db.patch("price_rules", f"sku=eq.{sku}", upd)
    for t, b in alerts:
        try:
            notify(cfgn, t, b, url="./?view=price", topic="price", log=log)
        except Exception as e:
            log("Notification failed: " + repr(e)[:120])


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
                total = (data.get("page_summary") or {}).get("total") or 0
                if not batch or len(sales) >= total or pg >= 100:
                    break
                pg += 1

            # ---- Takealot's monthly Stock Recon report (missing stock to claim)
            try:
                takealot_recon(ctx, cfg, auth, log)
            except Exception as e:
                log("Takealot stock recon couldn't be read: " + repr(e)[:200])

            # ---- one-off: read how the portal asks for a report (debug/recon_discover3.txt)
            try:
                if (DEBUG_DIR / "recon_discover3.txt").exists():
                    (DEBUG_DIR / "recon_discover3.txt").unlink()
                    import re as _re
                    js = ctx.request.get("https://sellers.takealot.com/assets/main-DS7F8Hpa.js", timeout=60_000).text()
                    segs = []
                    for key in ("leadtime_stock", "merchant_warehouse_id:", "/v2/offers/offer/", "/v2/offers/batch", "offers/offer"):
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
                offers, n = [], 1
                while n <= 50:
                    url = (f"https://{API_HOST}/v2/offers/detailed?page_number={n}&page_size=100"
                           f"&sort_key=date_added&sort_dir=asc&status_ids=1&status_ids=2&status_ids=3")
                    rr = ctx.request.get(url, headers=auth, timeout=60_000)
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
                apply_leadtime_requests(ctx, cfg, auth, offers, log)
            except Exception as e:
                log("Lead-time stock change failed: " + repr(e)[:200])
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
            return sales, returns
        finally:
            ctx.close()


WAREHOUSE_NAMES = {1: "CPT", 3: "JHB", 6: "DBN"}


def apply_leadtime_requests(ctx, cfg, auth, offers, log):
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
            for u in db.select("units", "select=product,condition&status=eq.in_stock&location=eq.HOME"):
                if (u.get("condition") or "new") == "new":
                    g = group.get(u["product"], u["product"])
                    home[g] = home.get(g, 0) + 1
        for f in rules:
            sku = str(f["sku"])
            o = by_sku.get(sku)
            if not o or sku in asked:
                continue
            target = home.get(f.get("product"), 0) if int(f["leadtime_fixed"]) < 0 else int(f["leadtime_fixed"])
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
    rows = []
    for o in offers:
        sku = str(o.get("merchant_sku") or "")
        if not sku:
            continue
        status = ((o.get("status") or {}).get("description") or "")
        total = (o.get("total_takealot_stock") or 0) + (o.get("total_stock_on_way") or 0) + (o.get("total_stock_in_receiving") or 0)
        if sku not in known and status != "Buyable" and not total:
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


REJECTED = {"returned-to-sender", "delivery-rejected"}


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
        for u in [u for u in units if u.get("po") == po]:
            if u.get("courier_shipment_id") == link:
                continue                                   # already handled
            if u.get("courier_shipment_id") == x["id"]:    # handled before the return waybill existed
                if u["status"] == "return_me" and link != x["id"]:
                    db.patch("units", f"id=eq.{u['id']}", {"courier_shipment_id": link, "updated_at": now})
                continue
            if u["status"] not in ("in_stock", "awaiting_fees", "settled"):
                continue
            note = f"Takealot rejected {x['waybill']} (PO {po}){why}; coming back on {back['waybill'] if back else 'a return waybill'}."
            if u.get("order_id"):
                pool = sorted([v for v in units if v["status"] == "in_stock" and not v.get("order_id") and v.get("po") != po
                               and v.get("location") == x["takealot_dc"] and grp(v) == grp(u)],
                              key=lambda v: (str(v.get("received") or ""), str(v.get("created_at") or "")))
                if not pool:
                    log(f"  Rejected PO {po}: no unsold {u['product']} at {x['takealot_dc']} to move order {u['order_id']} to; left as is.")
                    continue
                v = pool[0]
                moved = {k: u.get(k) for k in ("order_id", "sold_date", "gross", "fees", "status", "match_method", "product", "sku")}
                moved["comment"] = ((v.get("comment") or "") + f" Order {u['order_id']} moved here: PO {po} was rejected, "
                                    f"so Takealot filled it from its {x['takealot_dc']} stock.").strip()
                db.patch("units", f"id=eq.{u['id']}", {"order_id": "", "sold_date": None, "gross": None, "fees": None})
                db.patch("units", f"id=eq.{v['id']}", moved | {"updated_at": now})
                db.patch("takealot_sales", f"order_id=eq.{u['order_id']}", {"unit_id": v["id"]})
                v.update(moved)
                log(f"  Rejected PO {po}: order {u['order_id']} moved to a {x['takealot_dc']} unit (in {v.get('received')}).")
            db.patch("units", f"id=eq.{u['id']}", {"status": "return_me", "location": x["takealot_dc"], "order_id": "",
                                                    "sold_date": None, "gross": None, "fees": None, "courier_shipment_id": link,
                                                    "comment": ((u.get("comment") or "") + " " + note).strip(), "updated_at": now})
            u.update(status="return_me", order_id="", courier_shipment_id=link)
            log(f"  Rejected PO {po}: {u['product']} set to Return: me ({note})")



def fetch_takealot_pos(ctx, cfg, auth, log):
    """Takealot shipments (POs) due from ~6 weeks before the start date, with their product lines."""
    from datetime import date as _d, timedelta as _td
    cutoff = (_d.fromisoformat(cfg.get("since") or "2026-10-01") - _td(days=45)).isoformat()
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
