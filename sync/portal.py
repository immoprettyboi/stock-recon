"""
Stock Recon - Takealot Seller Portal reader.

Opens Chrome with the sync's own saved login, then reads the data feeds the
portal's own pages use: View Sales and View Returns. Nothing is clicked or
changed in the portal.

This file is reloaded on every sync run, so updates apply without restarting.
"""
from __future__ import annotations
import json
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
        (DEBUG_DIR / "otp_page.html").write_text(page.content()[:200000], encoding="utf-8")
    except Exception:
        pass
    body = " ".join((page.inner_text("body") or "").split())
    m = re.search(r"sent to (\S+@\S+?)[.\s]", body)
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
    page.wait_for_timeout(500)
    btn = page.locator("button[type=submit], button:has-text('Verify'), button:has-text('Submit'), button:has-text('Continue'), button:has-text('Log In')")
    vis = [btn.nth(i) for i in range(btn.count()) if btn.nth(i).is_visible() and btn.nth(i).is_enabled()]
    if vis:
        vis[0].click()
    else:
        page.keyboard.press("Enter")
    log(f"{name}: PIN from the app typed in.")
    return True


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
                    save_returns(cfg, returns, log)
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



def save_returns(cfg, returns, log):
    """Keep every return (from View Returns) in takealot_returns, so the app can count returns per month."""
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
