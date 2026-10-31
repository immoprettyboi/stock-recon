"""
Stock Recon - Takealot Chrome sync

Every 30 minutes:
  1. Opens Chrome (its own profile, logged into the Seller Portal once)
  2. Goes to Sales > View Sales and clicks "Export Sales Details"
  3. Reads the CSV, matches each sale to a unit in your stock
  4. Writes the results to your Stock Recon database

Run modes
  pythonw stockrecon_sync.py            loop forever, Chrome hidden (START.bat)
  python  stockrecon_sync.py --once     one sync, then exit (SYNC_NOW.bat)
  python  stockrecon_sync.py --login    open Chrome so you can log into Takealot (LOGIN.bat)
  python  stockrecon_sync.py --file X   sync from an export you downloaded yourself
"""
from __future__ import annotations
import configparser, json, os, sys, time, traceback, urllib.request, urllib.error, urllib.parse
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from matcher import parse_export, parse_api, match, suggest_sku_map, IGNORE_PRODUCT  # noqa: E402

CONFIG = HERE / "config.ini"
LOG = HERE / "sync_log.txt"
PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA", str(HERE))) / "StockRecon" / "chrome-profile"
DOWNLOADS = HERE / "exports"
SALES_URL = "https://sellers.takealot.com/sales"


# ---------------------------------------------------------------- utilities
def log(msg: str):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if LOG.stat().st_size > 2_000_000:  # keep the log small
            text = LOG.read_text(encoding="utf-8")[-500_000:]
            LOG.write_text(text, encoding="utf-8")
    except Exception:
        pass


def load_config():
    cp = configparser.ConfigParser()
    cp.read(CONFIG, encoding="utf-8")
    c = cp["stockrecon"] if "stockrecon" in cp else {}
    cfg = {
        "url": (c.get("supabase_url") or "").strip().rstrip("/"),
        "key": (c.get("service_key") or "").strip(),
        "since": (c.get("sync_sales_from") or "2026-10-01").strip(),
        "interval": int(c.get("every_minutes") or 30),
        "browser": (c.get("browser") or "chrome").strip(),
    }
    if not cfg["url"] or not cfg["key"] or "PASTE" in cfg["key"]:
        raise SystemExit("config.ini is missing the Supabase service key. Open config.ini in Notepad and paste it in.")
    return cfg


class DB:
    """Tiny Supabase REST client (no extra packages needed)."""
    def __init__(self, url, key):
        self.base = url + "/rest/v1/"
        self.h = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}

    def _req(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={**self.h, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                txt = r.read().decode() or "null"
                return json.loads(txt)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Database said {e.code}: {e.read().decode()[:300]}") from None

    def select(self, table, query="select=*"):
        rows, start = [], 0
        while True:
            page = self._req("GET", f"{table}?{query}", headers={"Range": f"{start}-{start + 999}"})
            rows += page
            if len(page) < 1000:
                return rows
            start += 1000

    def patch(self, table, match_q, body):
        return self._req("PATCH", f"{table}?{match_q}", body, {"Prefer": "return=minimal"})

    def insert(self, table, rows, upsert_on=None, returning=False):
        q = f"{table}" + (f"?on_conflict={upsert_on}" if upsert_on else "")
        pref = ["return=representation" if returning else "return=minimal"]
        if upsert_on:
            pref.append("resolution=merge-duplicates")
        return self._req("POST", q, rows, {"Prefer": ",".join(pref)})


# ---------------------------------------------------------------- browser
def _launch(p, cfg, headless):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS.mkdir(exist_ok=True)
    return p.chromium.launch_persistent_context(
        str(PROFILE_DIR), channel=cfg["browser"], headless=headless, accept_downloads=True,
        downloads_path=str(DOWNLOADS), viewport={"width": 1400, "height": 900},
        args=["--disable-blink-features=AutomationControlled"])


def login_flow(cfg):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _launch(p, cfg, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(SALES_URL)
        log("Chrome is open. Log into the Takealot Seller Portal; this window closes by itself once you're in.")
        deadline = time.time() + 15 * 60
        while time.time() < deadline:
            if "/login" not in page.url and "sellers.takealot.com" in page.url:
                page.wait_for_timeout(3000)
                log("Logged in. The sync can now run on its own.")
                ctx.close()
                return True
            page.wait_for_timeout(1500)
        ctx.close()
        log("Gave up waiting for the login after 15 minutes.")
        return False


DEBUG_DIR = HERE / "debug"


def _record(page, net, tag):
    """Save what the portal looks like, for fixing the sync when Takealot changes."""
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{tag}.png"), full_page=True)
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{tag}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    (DEBUG_DIR / "network.json").write_text(json.dumps(net, indent=1), encoding="utf-8")


API = "https://seller-api.takealot.com/v2/sales"


def fetch_sales(cfg, headless=True) -> list[dict]:
    """
    Opens the Seller Portal's sales page with the saved login and reads the same
    data feed the page itself uses (all pages, from the start date until today).
    """
    from playwright.sync_api import sync_playwright
    from datetime import date, timedelta
    auth = {}

    def on_request(req):
        if req.url.startswith(API) and not auth:
            auth.update({k: v for k, v in req.headers.items()
                         if k.lower() in ("authorization", "x-api-key", "x-csrftoken", "x-xsrf-token", "accept")})

    with sync_playwright() as p:
        ctx = _launch(p, cfg, headless=headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("request", on_request)
            page.goto(SALES_URL, wait_until="domcontentloaded", timeout=90_000)
            for _ in range(40):                      # up to 20 s for the table's first data call
                if auth or "/login" in page.url:
                    break
                page.wait_for_timeout(500)
            if "/login" in page.url:
                raise NeedsLogin()
            if not auth:
                _record(page, [], "no_sales_call")
                raise RuntimeError("The sales page didn't load its data. Details saved in the debug folder.")

            start = max(date.fromisoformat(cfg["since"]), date.today() - timedelta(days=90))
            end = date.today()
            items, pg = [], 1
            while True:
                url = (f"{API}?filters=start_date%3A{start}%2Cend_date%3A{end}"
                       f"&page_size=100&page_number={pg}")
                r = ctx.request.get(url, headers=auth, timeout=60_000)
                if r.status in (401, 403):
                    raise NeedsLogin()
                if not r.ok:
                    raise RuntimeError(f"Takealot's sales feed answered {r.status}: {r.text()[:200]}")
                data = r.json()
                batch = data.get("sales") or []
                items += batch
                total = (data.get("page_summary") or {}).get("total") or 0
                if not batch or len(items) >= total or pg >= 100:
                    break
                pg += 1
            return items
        finally:
            ctx.close()


def download_export(cfg, headless=True) -> str:
    from playwright.sync_api import sync_playwright
    net = []

    def on_response(r):
        try:
            if r.request.resource_type in ("xhr", "fetch", "document"):
                item = {"t": datetime.now().isoformat(), "method": r.request.method, "url": r.url, "status": r.status,
                        "type": r.headers.get("content-type", ""), "disp": r.headers.get("content-disposition", "")}
                if r.request.method != "GET":
                    item["post"] = (r.request.post_data or "")[:2000]
                if "json" in item["type"] and "takealot" in r.url:
                    try:
                        item["body"] = r.text()[:6000]
                    except Exception:
                        pass
                net.append(item)
        except Exception:
            pass

    with sync_playwright() as p:
        ctx = _launch(p, cfg, headless=headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("response", on_response)
            page.goto(SALES_URL, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(4000)
            if "/login" in page.url:
                raise NeedsLogin()
            btn = page.get_by_role("button", name="Export Sales Details")
            if not btn.count():
                btn = page.get_by_text("Export Sales Details", exact=True)
            btn = btn.first
            btn.wait_for(state="visible", timeout=60_000)
            page.wait_for_timeout(3000)  # let the table finish loading
            _record(page, net, "1_before_export")
            try:
                with page.expect_download(timeout=45_000) as dl:
                    btn.click()
                    page.wait_for_timeout(5000)
                    _record(page, net, "2_after_click")
            except Exception:
                _record(page, net, "3_no_download")
                raise RuntimeError("Takealot didn't start a download after Export Sales Details. "
                                   "Details saved in the debug folder.")
            d = dl.value
            target = DOWNLOADS / f"sales_{datetime.now():%Y%m%d_%H%M%S}.csv"
            d.save_as(str(target))
            # keep only the newest 20 exports
            for old in sorted(DOWNLOADS.glob("sales_*.csv"))[:-20]:
                old.unlink(missing_ok=True)
            return target.read_text(encoding="utf-8-sig")
        finally:
            ctx.close()


class NeedsLogin(Exception):
    pass


# ---------------------------------------------------------------- sync
UNIT_FIELDS = "id,product,supplier,sku,location,received,po,order_id,status,gross,fees,sold_date,return_date,comment,flag,match_method,cost"


def run_sync(cfg, csv_text: str | None = None, headless=True):
    # matcher.py and portal.py are reloaded every run, so updates apply without a restart
    import importlib, matcher as _m, portal as _p
    importlib.reload(_m)
    importlib.reload(_p)
    db = DB(cfg["url"], cfg["key"])
    run = db.insert("sync_runs", [{"message": "Running"}], returning=True)[0]
    try:
        returns = []
        if csv_text is None:
            raw_sales, raw_returns = _p.fetch_all(cfg, headless, PROFILE_DIR, DOWNLOADS, log)
            sales = _m.parse_api(raw_sales)
            returns = _m.parse_returns(raw_returns)
        else:
            sales = _m.parse_export(csv_text)
        log(f"Read {len(sales)} sale lines and {len(returns)} return(s) from Takealot.")

        units = db.select("units", f"select={UNIT_FIELDS}")
        for u in units:
            for k in ("gross", "fees", "cost"):
                if u.get(k) is not None:
                    u[k] = float(u[k])
        products = db.select("products")
        sku_map = {p["sku"]: p["product"] for p in products if p.get("product")}

        # learn SKUs we haven't seen, and list any new ones so you can link them in the app
        learned = _m.suggest_sku_map(sales, units)
        new_rows, known = [], {p["sku"] for p in products}
        for s in sales:
            if s["sku"] and s["sku"] not in known:
                known.add(s["sku"])
                prod = learned.get(s["sku"])
                new_rows.append({"sku": s["sku"], "tsin": s["tsin"], "takealot_title": s["product_title"], "product": prod})
                if prod:
                    sku_map[s["sku"]] = prod
        if new_rows:
            db.insert("products", new_rows, upsert_on="sku")
            log(f"Found {len(new_rows)} new Takealot product(s).")

        skip = {(r["order_id"], r["sku"]) for r in db.select("takealot_sales", "select=order_id,sku,user_ignored&user_ignored=is.true")}
        todo = [s for s in sales if (s["order_id"], s["sku"]) not in skip]

        updates, results = _m.match(todo, units, sku_map, since=cfg["since"])
        r_updates, new_units, notes = _m.apply_returns(returns, units, sku_map)
        for uid, f in r_updates.items():
            updates.setdefault(uid, {}).update(f)
        for n in notes:
            log("  " + n)

        unit_by_id = {u["id"]: u for u in units if u.get("id")}
        for uid, fields in updates.items():
            u = unit_by_id.get(uid, {})
            interesting = {k: v for k, v in fields.items() if k not in ("sku", "comment")}
            if interesting:
                log(f"  Unit: {u.get('product','?')} ({u.get('supplier') or ''} {u.get('location','')}, in {u.get('received','')}) "
                    f"<- " + ", ".join(f"{k}={v}" for k, v in interesting.items()))
            fields["updated_at"] = datetime.utcnow().isoformat() + "Z"
            db.patch("units", f"id=eq.{uid}", fields)
        if new_units:
            db.insert("units", new_units)
        sale_by_key = {(s["order_id"], s["sku"]): s for s in sales}
        for r in results:
            if r["match_status"] == "unmatched":
                s = sale_by_key.get((r["order_id"], r["sku"]), {})
                log(f"  Needs matching: order {r['order_id']} {s.get('product_title','')} {s.get('sold_date','')} "
                    f"{s.get('fulfilment_dc','')} ({s.get('sale_status','')}): {r['reason']}")

        by_key = {(r["order_id"], r["sku"]): r for r in results}
        now = datetime.utcnow().isoformat() + "Z"
        rows = []
        for s in sales:
            if (s["order_id"], s["sku"]) in skip:
                continue
            r = by_key.get((s["order_id"], s["sku"]))
            if r is None:  # older than the start date
                status, note, unit = "ignored", "before start date", None
            else:
                status, note = r["match_status"], r["reason"]
                unit = r["unit_ids"][0] if r["unit_ids"] else None
            rows.append({k: s[k] for k in ("order_id", "sku", "order_date", "sale_status", "product_title", "tsin", "qty",
                                           "fulfilment_dc", "customer_dc", "gross", "fees", "net", "po", "raw")}
                        | {"po": s["po"] or None, "unit_id": unit, "match_status": status, "match_note": note, "last_seen": now})
        for i in range(0, len(rows), 200):
            db.insert("takealot_sales", rows[i:i + 200], upsert_on="order_id,sku")

        matched = sum(1 for r in results if r["match_status"] == "matched")
        unmatched = sum(1 for r in results if r["match_status"] == "unmatched")
        msg = f"{len(updates)} unit(s) updated" + (f", {len(new_units)} added from returns" if new_units else "") \
              + (f", {unmatched} sale(s) need matching" if unmatched else "")
        db.patch("sync_runs", f"id=eq.{run['id']}", {"finished_at": now, "ok": True, "message": msg,
                                                     "rows_read": len(sales), "matched": matched, "unmatched": unmatched})
        log("Sync done: " + msg)
        return True
    except Exception as e:
        if type(e).__name__ == "NeedsLogin":
            msg = "Takealot login needed. On the PC, double-click LOGIN.bat in the Stock Recon Sync folder."
            db.patch("sync_runs", f"id=eq.{run['id']}", {"finished_at": datetime.utcnow().isoformat() + "Z", "ok": False, "message": msg})
            log(msg)
            return False
        log("Sync failed: " + repr(e))
        log(traceback.format_exc())
        try:
            db.patch("sync_runs", f"id=eq.{run['id']}", {"finished_at": datetime.utcnow().isoformat() + "Z", "ok": False,
                                                         "message": f"Sync failed: {str(e)[:200]}"})
        except Exception:
            pass
        return False


def main():
    args = sys.argv[1:]
    cfg = load_config()
    if "--login" in args:
        login_flow(cfg)
        return
    if "--file" in args:
        path = args[args.index("--file") + 1]
        run_sync(cfg, Path(path).read_text(encoding="utf-8-sig"))
        return
    if "--once" in args:
        run_sync(cfg, headless="--show" not in args)
        return
    log(f"Stock Recon sync started. Syncing every {cfg['interval']} minutes.")
    db = DB(cfg["url"], cfg["key"])
    while True:
        run_sync(cfg)
        # wait for the next run, but check every minute for a "Sync now" from the app
        for _ in range(cfg["interval"]):
            time.sleep(60)
            # price fighter: checks the listings you switched on, each at its own interval
            try:
                import importlib, portal as _pp
                importlib.reload(_pp)
                _pp.price_tick(cfg, log)
            except Exception as e:
                log("Price fighter: " + repr(e)[:200])
            try:
                req = db.select("sync_runs", "select=id&requested=is.true&finished_at=is.null")
                if req:
                    ids = ",".join(str(r["id"]) for r in req)
                    db.patch("sync_runs", f"id=in.({ids})", {"finished_at": datetime.utcnow().isoformat() + "Z",
                                                             "ok": True, "message": "Picked up by the PC"})
                    log("Sync requested from the app.")
                    break
            except Exception as e:
                log("Couldn't check for sync requests: " + repr(e))


if __name__ == "__main__":
    main()
