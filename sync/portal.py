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
LAST_OFFERS: list = []   # offers read on the last run (also saved to debug/offers.json)
API_HOST = "seller-api.takealot.com"
SALES_API = "https://seller-api.takealot.com/v2/sales"


class NeedsLogin(Exception):
    pass


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
