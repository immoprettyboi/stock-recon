"""
Stock Recon - matching engine.

Takes Takealot sales export rows and the current list of units, and decides
which unit each sale belongs to. Pure Python, no network, so it can be tested
on its own. The sync program feeds it data from the database and writes the
changes it returns back.

Rules (in order) for a sale that is not linked to a unit yet:
  1. A unit already carrying this order ID          -> update it
  2. Sale has a PO number -> unsold unit of that product with the same PO
                          -> else unsold unit of that product at HOME
                             (stock sent in for that order)
  3. No PO               -> unsold unit of that product at the same
                             Takealot warehouse (JHB/CPT/DBN)
  In every case the unit that arrived first is used (first in, first out).
  If nothing fits, the sale goes on the "Needs matching" list.
"""
from __future__ import annotations
import csv, io, re
from datetime import datetime

FEE_COLS = ["Success Fee", "Fulfilment Fee", "Courier Collection Fee", "Stock Transfer Fee"]
SOLD_STATES = {"Shipped to Customer", "Confirmed Shipment", "Preparing for Customer",
               "Accepted", "New Lead Time Order", "Inbound", "Ready for Collection"}
IGNORE_PRODUCT = "__ignore__"
_LAST_SALES: list = []   # the sales seen by the last match() call, used by apply_returns


def _num(s):
    s = (s or "").strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _date(s, fmt):
    s = (s or "").strip()
    if s in ("", "-"):
        return None
    try:
        return datetime.strptime(s, fmt)
    except ValueError:
        return None


def parse_export(text: str) -> list[dict]:
    """Parse the 'Export Sales Details' CSV. Customer names are dropped on purpose."""
    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        if not r.get("Order ID"):
            continue
        fee_parts = [_num(r.get(c)) for c in FEE_COLS]
        fees = round(sum(f for f in fee_parts if f is not None), 2) if any(f is not None for f in fee_parts) else None
        od = _date(r.get("Order Date"), "%d %b %Y %H:%M:%S")
        po = (r.get("PO Number") or "").strip()
        out.append({
            "order_id": r["Order ID"].strip(),
            "sku": (r.get("SKU") or "").strip(),
            "order_date": od.isoformat() if od else None,
            "sold_date": od.date().isoformat() if od else None,
            "sale_status": (r.get("Sale Status") or "").strip(),
            "product_title": (r.get("Product Title") or "").strip(),
            "tsin": (r.get("TSIN") or "").strip(),
            "qty": int(_num(r.get("Qty")) or 1),
            "fulfilment_dc": (r.get("Fulfilment DC") or "").strip(),
            "customer_dc": (r.get("Customer DC") or "").strip(),
            "gross": _num(r.get("Gross Sales")),
            "fees": fees,
            "net": _num(r.get("Net Sales Amount")),
            "po": "" if po == "-" else po,
            "raw": {k: v for k, v in r.items() if k != "Customer"},
        })
    return out


def parse_api(items: list[dict]) -> list[dict]:
    """Parse sales from the Seller Portal's own data feed (seller-api /v2/sales). Customer names are dropped."""
    out = []
    for r in items:
        if not r.get("order_id"):
            continue
        parts = [r.get(k) for k in ("success_fee", "fulfillment_fee", "courier_collection_fee", "stock_transfer_fee")]
        fee_sum = round(sum(float(p) for p in parts if p is not None), 2)
        if not fee_sum and r.get("total_fee"):
            fee_sum = round(float(r["total_fee"]), 2)
        fees = fee_sum if fee_sum > 0 else None          # Takealot shows 0 until it charges the fees
        qty = int(r.get("quantity") or 1)
        price = r.get("selling_price")
        gross = round(float(price) * qty, 2) if price is not None else None
        od = _date(r.get("order_date"), "%d %b %Y %H:%M:%S")
        po = r.get("po_number")
        raw = {k: v for k, v in r.items() if k != "customer"}
        out.append({
            "order_id": str(r["order_id"]),
            "sku": str(r.get("sku") or ""),
            "order_date": od.isoformat() if od else None,
            "sold_date": od.date().isoformat() if od else None,
            "sale_status": (r.get("sale_status") or "").strip(),
            "product_title": (r.get("product_title") or "").strip(),
            "tsin": str(r.get("tsin") or ""),
            "qty": qty,
            "fulfilment_dc": (r.get("stock_source_region") or r.get("dc") or "").strip(),
            "customer_dc": (r.get("customer_dc") or "").strip(),
            "gross": gross,
            "fees": fees,
            "net": round(gross - (fees or 0), 2) if gross is not None else None,
            "po": str(po) if po not in (None, "", 0, "-") else "",
            "raw": raw,
        })
    return out


def kind(sale_status: str) -> str:
    s = sale_status.lower()
    if s.startswith("cancel"):
        return "cancelled"
    if s.startswith("return"):
        return "returned"
    return "sold"


def match(sales: list[dict], units: list[dict], sku_map: dict[str, str], since: str | None = None):
    """
    sales:   parsed export rows
    units:   dicts with id, product, location, received, po, order_id, status, gross, fees, sold_date, comment, flag
    sku_map: SKU -> your product name (or IGNORE_PRODUCT)
    since:   ignore orders before this date (YYYY-MM-DD), e.g. the first day you started using the app

    Returns (unit_updates: {unit_id: {field: value}}, sale_results: [ {order_id, sku, unit_ids, match_status, reason} ])
    Units are updated in place as well, so later sales in the same run see earlier decisions.
    """
    global _LAST_SALES
    _LAST_SALES = sales
    updates: dict[str, dict] = {}
    results = []
    by_order: dict[str, list[dict]] = {}
    for u in units:
        if u.get("order_id"):
            by_order.setdefault(u["order_id"], []).append(u)
    # Several Takealot listings can be the same physical product. sku_map gives each SKU's product
    # group; units are named after the listing they sold on, so map listing titles back to groups.
    title_group = {}
    for s0 in sales:
        g = sku_map.get(s0["sku"])
        if g and g != IGNORE_PRODUCT and s0.get("product_title"):
            title_group[s0["product_title"]] = g
    group = lambda name: title_group.get(name, name)

    def upd(u, **fields):
        changed = {k: v for k, v in fields.items() if u.get(k) != v}
        if changed:
            u.update(changed)
            updates.setdefault(u["id"], {}).update(changed)

    # oldest orders first, so first-in-first-out follows the order sales happened
    for s in sorted(sales, key=lambda x: x["order_date"] or ""):
        if since and (s["sold_date"] or "") < since:
            continue
        k = kind(s["sale_status"])
        linked = by_order.get(s["order_id"], [])
        res = {"order_id": s["order_id"], "sku": s["sku"], "unit_ids": [], "match_status": "unmatched", "reason": ""}
        qty = max(1, s["qty"])
        per_gross = round(s["gross"] / qty, 2) if s["gross"] is not None else None
        per_fees = round(s["fees"] / qty, 2) if s["fees"] is not None else None

        if k == "cancelled":
            for u in linked:
                # a customer cancelling before shipping is routine: straight back into stock, nothing to check
                upd(u, status="in_stock", order_id="", sold_date=None, gross=None, fees=None,
                    flag="customer" not in s["sale_status"].lower(),
                    comment=(f"Takealot order {s['order_id']} was {s['sale_status'].lower()} on "
                             f"{s['sold_date']}. Unit put back in stock. " + (u.get("comment") or "")).strip(),
                    match_method=None)
            by_order.pop(s["order_id"], None)
            res.update(match_status="ignored", reason="cancelled", unit_ids=[u["id"] for u in linked])
            results.append(res)
            continue

        if k == "returned":
            for u in linked:
                if u["status"] not in ("return_takealot", "return_me"):
                    upd(u, status="return_takealot", flag=True,
                        comment=(f"Takealot shows order {s['order_id']} as Returned. Check: sellable "
                                 f"(back in Takealot stock) or coming back to you? " + (u.get("comment") or "")).strip())
            res.update(match_status="matched" if linked else "ignored",
                       reason="return" if linked else "return of a sale that was never linked to a unit",
                       unit_ids=[u["id"] for u in linked])
            results.append(res)
            continue

        # ---- a live sale ----
        if linked:
            for u in linked:
                fields = {"gross": per_gross, "sold_date": s["sold_date"]}
                if per_fees is not None:
                    fields["fees"] = per_fees
                if u["status"] in ("in_stock", "awaiting_fees", "settled"):
                    fields["status"] = "settled" if (per_fees if per_fees is not None else u.get("fees")) is not None else "awaiting_fees"
                if s["sku"] and not u.get("sku"):
                    fields["sku"] = s["sku"]
                if s.get("product_title") and sku_map.get(s["sku"]) not in (None, IGNORE_PRODUCT):
                    fields["product"] = s["product_title"]      # named after the listing it sold on
                if s["po"] and not u.get("po"):          # PO appears once the unit is sent in
                    fields["po"] = s["po"]
                # once the unit has left home (it's on a PO, or Takealot is past waiting for it), show it at
                # the warehouse the sale ships from, as on the Seller Portal
                if u.get("location") == "HOME" and s.get("fulfilment_dc") in ("JHB", "CPT", "DBN") and \
                        (s["po"] or u.get("po") or s.get("sale_status") not in ("New Lead Time Order", "Confirmed Shipment", "Inbound")):
                    fields["location"] = s["fulfilment_dc"]
                upd(u, **fields)
            res.update(match_status="matched", reason="order", unit_ids=[u["id"] for u in linked])
            results.append(res)
            continue

        product = sku_map.get(s["sku"])
        if product == IGNORE_PRODUCT:
            res.update(match_status="ignored", reason="product not tracked")
            results.append(res)
            continue
        if not product:
            res.update(reason=f"SKU {s['sku']} is not linked to a product yet")
            results.append(res)
            continue

        picked = []
        method = None
        for _ in range(qty):
            pool = [u for u in units if u["status"] == "in_stock" and not u.get("order_id")
                    and group(u["product"]) == product and u not in picked]
            cand = []
            if s["po"]:
                cand = [u for u in pool if u.get("po") == s["po"]]
                method = "po"
                if not cand:
                    cand = [u for u in pool if u.get("location") == "HOME"]
                    method = "po-home"
            if not cand and "lead time" in s["sale_status"].lower():
                # lead-time orders are filled from your own stock, sent in for that order
                cand = [u for u in pool if u.get("location") == "HOME"]
                method = "leadtime-home"
            if not cand:
                cand = [u for u in pool if u.get("location") == s["fulfilment_dc"]]
                method = "fifo"
            if not cand:
                break
            cand.sort(key=lambda u: (u.get("received") or "9999", u.get("id")))
            picked.append(cand[0])

        if len(picked) < qty:
            res.update(reason=(f"No unsold {product} at {s['fulfilment_dc']}"
                               + (f" or with PO {s['po']}" if s["po"] else "")))
            results.append(res)
            continue

        for u in picked:
            fields = dict(order_id=s["order_id"], sold_date=s["sold_date"], gross=per_gross, fees=per_fees,
                          product=s.get("product_title") or u["product"],
                          status="settled" if per_fees is not None else "awaiting_fees",
                          sku=s["sku"], match_method=method)
            if method == "po-home":
                fields["po"] = s["po"]
                fields["location"] = s["fulfilment_dc"]
            if method == "leadtime-home":
                fields["comment"] = (f"Lead-time order for {s['fulfilment_dc']}: send this unit in. "
                                     + (u.get("comment") or "")).strip()
            upd(u, **fields)
            by_order.setdefault(s["order_id"], []).append(u)
        res.update(match_status="matched", reason=method, unit_ids=[u["id"] for u in picked])
        results.append(res)

    return updates, results


def suggest_sku_map(sales, units):
    """First-time helper: learn SKU -> product from orders that are already linked by order ID."""
    by_order = {u["order_id"]: u["product"] for u in units if u.get("order_id")}
    m = {}
    for s in sales:
        p = by_order.get(s["order_id"])
        if p and s["sku"] and s["sku"] not in m:
            m[s["sku"]] = p
    names = {u["product"].lower(): u["product"] for u in units}
    for s in sales:
        if s["sku"] not in m and s["product_title"].lower() in names:
            m[s["sku"]] = names[s["product_title"].lower()]
    return m


# ------------------------------------------------------------------ returns
def _pick(d: dict, *names, contains=None):
    low = {k.lower(): v for k, v in d.items()}
    for n in names:
        if n in low and low[n] not in (None, ""):
            return low[n]
    if contains:
        for k, v in low.items():
            if any(c in k for c in contains) and v not in (None, ""):
                return v
    return None


def _text(v):
    if isinstance(v, dict):
        v = _pick(v, "name", "description", "label", "value", "status") or ""
    return str(v or "").strip()


WAREHOUSES = {1: "CPT", 3: "JHB", 6: "DBN"}   # Takealot warehouse ids (from the offers stock columns)


def parse_returns(items: list[dict]) -> list[dict]:
    """Parse rows from the portal's View Returns feed (seller-api /v2/returns)."""
    out = []
    for r in items:
        oid = _pick(r, "order_id", "orderid")
        if not oid:
            continue
        # the current state is the latest entry in return_outcomes (e.g. pending_sellable_stock);
        # seller_return_outcome is only Takealot's first evaluation
        outcome = ""
        hist = r.get("return_outcomes") or []
        if isinstance(hist, list) and hist:
            last = sorted(hist, key=lambda h: h.get("id") or 0)[-1]
            outcome = str(last.get("status") or "").replace("_", " ")
        if not outcome:
            outcome = _text(_pick(r, "seller_return_outcome", "stock_outcome", "outcome"))
        rd = _pick(r, "return_date", "date_returned", "created_at")
        rdate = None
        if isinstance(rd, (int, float)) or (isinstance(rd, str) and rd.isdigit()):
            rdate = datetime.fromtimestamp(int(rd)).date().isoformat()
        elif rd:
            rd = str(rd)
            for cand in (rd, rd[:19], rd[:11].strip(), rd[:10]):
                for fmt in ("%d %b %Y %H:%M:%S", "%d %b %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                    try:
                        rdate = datetime.strptime(cand, fmt).date().isoformat()
                        break
                    except ValueError:
                        continue
                if rdate:
                    break
        wid = r.get("warehouse_id")
        dc = WAREHOUSES.get(wid) if isinstance(wid, int) else None
        out.append({
            "order_id": str(oid),
            "sku": str(_pick(r, "sku") or ""),
            "title": _text(_pick(r, "title", "product_title")),
            "qty": int(_pick(r, "qty", "quantity") or 1),
            "dc": dc or _text(_pick(r, "dc", "return_region", "region")),
            "reason": _text(_pick(r, "seller_return_reason", "return_reason", "reason")),
            "outcome": outcome,
            "return_date": rdate,
        })
    return out


def outcome_kind(outcome: str) -> str:
    o = outcome.lower()
    if any(w in o for w in ("removal", "return to seller", "returned to seller", "unsellable", "collect", "dispos")):
        return "removal"
    if "pending" in o:
        return "pending"
    if "sellable" in o:
        return "sellable"
    return "unknown"


def apply_returns(returns: list[dict], units: list[dict], sku_map: dict[str, str], sales: list[dict] | None = None):
    """
    Pending sellable stock      -> Return: Takealot (orange)
    Sellable stock              -> sale reversed, unit back In stock at that warehouse
    Removal order / to seller   -> Return: me (red); if the unit isn't in your list, it is added
    Returns (updates {unit_id: fields}, new_units [dict], notes [str]).
    """
    updates, new_units, notes = {}, [], []
    by_order = {}
    for u in units:
        if u.get("order_id"):
            by_order.setdefault(u["order_id"], []).append(u)

    def upd(u, **f):
        ch = {k: v for k, v in f.items() if u.get(k) != v}
        if ch:
            u.update(ch)
            updates.setdefault(u["id"], {}).update(ch)

    sale_of = {x["order_id"]: x for x in (sales if sales is not None else _LAST_SALES)}
    for r in returns:
        if not r["dc"] and r["order_id"] in sale_of:
            r["dc"] = sale_of[r["order_id"]].get("fulfilment_dc") or ""
    for r in returns:
        k = outcome_kind(r["outcome"])
        linked = by_order.get(r["order_id"], [])
        # repair: a unit wrongly put back in stock for a return that is still pending
        if k == "pending" and not linked:
            tag = f"Customer return (order {r['order_id']},"
            for u in units:
                c = u.get("comment") or ""
                if u.get("status") == "in_stock" and not u.get("order_id") and c.startswith(tag) and "Sale reversed." in c:
                    s0 = sale_of.get(r["order_id"], {})
                    upd(u, order_id=r["order_id"], status="return_takealot", sold_date=s0.get("sold_date") or u.get("sold_date"),
                        match_method="import", comment=c.split("Sale reversed.", 1)[1].strip())
                    by_order.setdefault(r["order_id"], []).append(u)
                    notes.append(f"Repaired: order {r['order_id']} put back as Return: Takealot (still pending)")
            linked = by_order.get(r["order_id"], [])
        when = r["return_date"] or ""
        if k == "unknown":
            notes.append(f"Return {r['order_id']}: outcome '{r['outcome']}' not recognised, left alone")
            continue
        if not linked:
            if k == "removal":
                # already booked in earlier (the unit's order number was cleared when it went back into stock)
                if any(f"order {r['order_id']}" in (u.get("comment") or "") for u in units):
                    continue
                product = sku_map.get(r["sku"])
                if product == IGNORE_PRODUCT:
                    continue
                for _ in range(max(1, r["qty"])):
                    nu = {"product": r["title"] or product or f"SKU {r['sku']}", "sku": r["sku"] or None,
                          "status": "return_me", "location": r["dc"] or "HOME", "order_id": r["order_id"],
                          "return_date": r["return_date"], "cost": 0, "supplier": "", "flag": True,
                          "match_method": "return", "source": "sync",
                          "comment": (f"Removal order: Takealot is sending this back to you (order {r['order_id']}, "
                                      f"{r['reason'] or 'no reason given'}, returned {when}). It wasn't in your stock "
                                      f"list, so it was added. Enter its supplier and cost.")}
                    new_units.append(nu)
                    units.append(nu)
                    by_order.setdefault(r["order_id"], []).append(nu)
                notes.append(f"Removal order {r['order_id']} ({r['title']}): added to your stock as coming back to you")
            continue
        for u in linked:
            if k == "pending":
                if u["status"] not in ("return_takealot", "return_me"):
                    upd(u, status="return_takealot", return_date=r["return_date"], flag=False,
                        comment=(f"Customer return (order {r['order_id']}, {r['reason']}), {when}: pending sellable "
                                 f"stock at Takealot {r['dc']}. " + (u.get("comment") or "")).strip())
            elif k == "removal":
                if u.get("status") == "return_me" and u.get("match_method") == "return" and u.get("location") == "HOME" and r["dc"] \
                        and "Arrived back" not in (u.get("comment") or ""):
                    upd(u, location=r["dc"])
                if u["status"] != "return_me" and not (u["status"] == "in_stock" and u.get("location") == "HOME"):
                    upd(u, status="return_me", return_date=r["return_date"], location=r["dc"] or u.get("location"),
                        flag=True, comment=(f"Removal order: Takealot is sending this back to you (order {r['order_id']}, "
                                            f"{r['reason']}), {when}. When it arrives, set Location HOME and In stock. "
                                            + (u.get("comment") or "")).strip())
            elif k == "sellable":
                if u["status"] != "in_stock" or u.get("order_id"):
                    upd(u, status="in_stock", location=r["dc"] or u.get("location"), order_id="", sold_date=None,
                        gross=None, fees=None, return_date=r["return_date"], flag=False, match_method=None,
                        comment=(f"Customer return (order {r['order_id']}, {r['reason']}), {when}: back in Takealot "
                                 f"sellable stock at {r['dc']}. Sale reversed. " + (u.get("comment") or "")).strip())
    return updates, new_units, notes
