"""
Stock Recon - monthly bank statement check.

FNB e-mails the business statement (PDF) to the Gmail in config.ini (report_from). Every sync this:
  1. looks in that Gmail for FNB statement e-mails it hasn't read yet (last 45 days),
  2. reads every transaction out of the PDF,
  3. sorts each payment out of the account:
       - known business costs (fuel, ads, data, bank fees, salaries, loan interest ...) -> added to Salaries & expenses
       - supplier / stock payments, Courier Guy top-ups (already counted) and money in -> left out
       - anything it hasn't seen before -> "Bank payments to sort" in the app, where you pick what it is once;
         next month the same payment is sorted by itself.
Nothing already in Salaries & expenses is added twice (same month, same amount).
"""
from __future__ import annotations
import hashlib, json, re
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATEMENTS = HERE / "statements"
DONE_FILE = HERE / "debug" / "bank_done.json"
DRY = []
MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}

# (pattern on the description, category, type, description to use)  -- first match wins
#   category: "salary" / "expense" = a business cost; "bank_ignored" = not a cost (left out, kept for the record)
RULES = [
    (r"\bmagtape credit\b|cashfocus", "bank_ignored", "Takealot payout", None),
    (r"\binv\b|\binvoice\b", "bank_ignored", "Supplier payment", None),
    (r"loan repayment", "expense", "Loan interest", "Loan interest (monthly)"),
    (r"scheduled payment to (\w+) loan", "salary", "Salary", "{1} salary"),
    (r"payment to loan\b|off-us loan", "bank_ignored", "Supplier payment (Shakil)", None),
    (r"\btcg\d+", "bank_ignored", "Courier Guy top-up (counted from Courier Guy)", None),
    (r"\bflip\b", "bank_ignored", "Phone flip (sale)", None),
    (r"\bsalary\b|\bwages?\b", "salary", "Salary", None),
    (r"\bshell\b|\bsasol\b|\bengen\b|\btotal\b|\bbp\b|\bcaltex\b|\bastron\b|\buber\b|\bbolt\b|e-?toll", "expense", "Fuel & transport", None),
    (r"google ?\*ads|tiktok|facebk|\bmeta\b|instagram", "expense", "Advertising", None),
    (r"\bmtn\b|vodacom|telkom|cell ?c\b|\brain\b|afrihost|webafrica", "expense", "Phone & internet", None),
    (r"google tseller|block seven|microsoft|adobe|canva|shopify|wix|godaddy|openai|claude|anthropic", "expense", "Software & subscriptions", None),
    (r"islamic rel|gift of the givers|donation", "expense", "Donations", None),
    (r"^bank service fees$|service fee|monthly account fee", "expense", "Bank charges", "FNB service fees"),
]


def key_of(desc: str) -> str:
    """The same shop / payee every month gives the same key (card numbers, dates and references dropped)."""
    d = desc.lower()
    d = re.sub(r"\d{6}\*\d{4}", " ", d)                 # card number
    d = re.sub(r"\b\d{1,2} (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", " ", d)
    d = re.sub(r"\d+", "#", d)
    d = re.sub(r"[^a-z#*' -]", " ", d)
    return " ".join(d.split())[:60]


def parse_statement(text: str):
    """[(date 'YYYY-MM-DD', description, amount (+ in / - out), balance)] and (period_from, period_to)."""
    m = re.search(r"Statement Period\s*:\s*(\d{1,2}) (\w+) (\d{4}) to (\d{1,2}) (\w+) (\d{4})", text)
    if not m:
        return [], None
    end_y, end_m = int(m.group(6)), datetime.strptime(m.group(5)[:3], "%b").month
    start_y = int(m.group(3))
    period = (f"{m.group(3)}-{datetime.strptime(m.group(2)[:3], '%b').month:02d}-{int(m.group(1)):02d}",
              f"{end_y}-{end_m:02d}-{int(m.group(4)):02d}")
    ob = re.search(r"Opening Balance\s+([\d,]+\.\d{2})\s*(Cr|Dr)", text)
    prev = float(ob.group(1).replace(",", "")) * (1 if not ob or ob.group(2) == "Cr" else -1) if ob else None
    rows = []
    line_re = re.compile(r"^(\d{2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(.*?)\s*([\d,]+\.\d{2})(Cr)?\s+([\d,]+\.\d{2})(Cr|Dr)(?:\s+[\d.,]+)?$")
    for raw in text.splitlines():
        line = " ".join(raw.split())
        lm = line_re.match(line)
        if not lm:
            continue
        day, mon, desc = int(lm.group(1)), MONTHS[lm.group(2)], lm.group(3).strip()
        bal = float(lm.group(6).replace(",", "")) * (1 if lm.group(7) == "Cr" else -1)
        amt = float(lm.group(4).replace(",", ""))
        amt = amt if lm.group(5) else -amt
        if prev is not None and abs((bal - prev) - amt) > 0.01 and abs((bal - prev) + amt) < 0.01:
            amt = -amt                                    # the running balance says which way it went
        prev = bal
        # the card's own purchase date ("400568*2782 07 Oct") is when it was spent
        cd = re.search(r"\d{6}\*\d{4}\s+(\d{2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", desc)
        if cd:
            day, mon = int(cd.group(1)), MONTHS[cd.group(2)]
            desc = desc[:cd.start()].strip()
        y = end_y if mon <= end_m else start_y
        if not desc:
            desc = "Bank service fees"
        rows.append((f"{y}-{mon:02d}-{day:02d}", desc, round(amt, 2), bal))
    return rows, period


def _clean_name(desc: str) -> str:
    d = re.sub(r"^(POS Purchase|FNB App (Geo |Rtc )?(Payment|Pmt) To|Internet Pmt To|Payshap Account Off-Us|Scheduled Payment To|FNB OB Pmt)\s*", "", desc, flags=re.I)
    return d.strip()[:60] or desc[:60]


def classify(desc: str, learned: dict):
    k = key_of(desc)
    if k in learned:
        return learned[k]
    low = desc.lower()
    for pat, cat, typ, label in RULES:
        mm = re.search(pat, low)
        if mm:
            if label and "{1}" in label:
                label = label.replace("{1}", (mm.group(1) or "").title())
            return {"category": cat, "type": typ, "description": label or _clean_name(desc)}
    return None


def _pdf_text(data: bytes) -> str:
    try:
        import pypdf
    except ImportError:
        import subprocess, sys
        exe = sys.executable.replace("pythonw.exe", "python.exe")
        subprocess.run([exe, "-m", "pip", "install", "--quiet", "pypdf"], timeout=300, check=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        import pypdf
    import io, logging
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    r = pypdf.PdfReader(io.BytesIO(data))
    if r.is_encrypted:
        r.decrypt("")
    return "\n".join((p.extract_text() or "") for p in r.pages)


def process(db, rows, since: str, log, source_name: str, dry=False):
    """Add the month's business costs; queue the unknown ones. Returns (added, to_sort, total_added)."""
    existing = db.select("expenses", f"select=id,date,category,amount,description,raw&date=gte.{since}")
    have_ids = {e["id"] for e in existing}
    learned = {}
    for e in existing:
        k = (e.get("raw") or {}).get("key")
        if k and e["category"] in ("salary", "expense", "bank_ignored"):
            learned[k] = {"category": e["category"], "type": (e.get("raw") or {}).get("type") or "Other", "description": e.get("description")}
    added, to_sort, total, new = 0, 0, 0.0, []
    for d, desc, amt, bal in rows:
        if amt >= 0 or d < since:                        # money in, or before the app started
            continue
        amount = round(-amt, 2)
        rid = "bank-" + hashlib.sha1(f"{d}|{desc}|{amount}|{bal}".encode()).hexdigest()[:14]
        if rid in have_ids:
            continue
        k = key_of(desc)
        c = classify(desc, learned)
        # already recorded by hand / from the October check / "Repeat monthly costs": same month, same amount, a cost
        cands = [e for e in existing if e["category"] in ("salary", "expense", "courier_topup", "adam_delivery")
                 and str(e["date"])[:7] == d[:7] and abs(float(e["amount"] or 0) - amount) < 0.01
                 and not (e.get("raw") or {}).get("bank_matched")
                 and (c is None or c["category"] != "salary" or e["category"] == "salary")]
        dd = lambda e: abs((date.fromisoformat(str(e["date"])[:10]) - date.fromisoformat(d)).days)
        words = set(re.findall(r"[a-z]{3,}", desc.lower())) - {"payment", "purchase", "scheduled", "loan", "pos", "fnb", "app"}
        same = lambda e: -len(words & set(re.findall(r"[a-z]{3,}", str(e.get("description") or "").lower())))
        twin = min(cands, key=lambda e: (dd(e), same(e))) if cands else None
        if twin and (c is None or c["category"] != "bank_ignored"):
            raw = dict(twin.get("raw") or {}, bank_matched=True)
            raw.setdefault("key", k)
            if dry:
                DRY.append({"match": d, "line": desc, "amount": amount, "with": twin["description"], "with_date": str(twin["date"])})
            else:
                db.patch("expenses", f"id=eq.{twin['id']}", {"raw": raw})
            twin["raw"] = raw
            continue
        base = {"id": rid, "date": d, "amount": amount, "source": "bank",
                "raw": {"key": k, "line": desc, "balance": bal, "from": source_name}}
        if c is None:
            new.append(base | {"category": "bank_review", "description": _clean_name(desc)})
            to_sort += 1
        else:
            new.append(base | {"category": c["category"], "description": c["description"] or _clean_name(desc),
                               "raw": base["raw"] | {"type": c["type"], "auto": True}})
            if c["category"] in ("salary", "expense"):
                added += 1
                total += amount
        have_ids.add(rid)
    if new and not dry:
        db.insert("expenses", new, upsert_on="id")
    if dry:
        DRY.extend({"new": x["category"], "date": x["date"], "amount": x["amount"], "desc": x["description"], "type": (x.get("raw") or {}).get("type")} for x in new)
    return added, to_sort, round(total, 2)


def check_gmail_statements(cfg, log, notify=None, force_file: Path | None = None, dry=False):
    """Find FNB statement e-mails in Gmail that haven't been read yet and process them."""
    import configparser, imaplib, email, email.header, time as _t
    import stockrecon_sync as S
    db = S.DB(cfg["url"], cfg["key"])
    since = cfg.get("since") or "2026-10-01"
    done = {}
    try:
        done = json.loads(DONE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    pdfs = []                                            # (name, bytes, msg id)
    if force_file:
        pdfs.append((force_file.name, force_file.read_bytes(), "file:" + force_file.name))
    else:
        cp = configparser.ConfigParser()
        cp.read(HERE / "config.ini", encoding="utf-8")
        c = cp["stockrecon"] if "stockrecon" in cp else {}
        # the Gmail FNB sends statements to: bank_gmail (+ bank_gmail_app_password) if set, else the report Gmail
        user = (c.get("bank_gmail") or c.get("report_from") or "").strip()
        pw = (c.get("bank_gmail_app_password") if c.get("bank_gmail") else c.get("report_app_password")) or ""
        pw = pw.replace(" ", "").strip()
        if not (user and pw):
            return
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        M.login(user, pw)
        M.select('"[Gmail]/All Mail"', readonly=True)
        day = _t.strftime("%d-%b-%Y", _t.gmtime(_t.time() - 45 * 86400))
        typ, data = M.search(None, f'(OR OR FROM "fnb" SUBJECT "statement" FROM "firstrand" SINCE {day})')
        if dry:
            try:
                typ, allfnb = M.search(None, '(FROM "fnb.co.za")')
                typ, allst = M.search(None, '(SUBJECT "statement")')
                typ, tot = M.search(None, f'(SINCE {day})')
                log(f"Bank statement dry run: mailbox {user[:3]}…; FNB mails ever: {len((allfnb[0] or b'').split())}, "
                    f"'statement' subjects ever: {len((allst[0] or b'').split())}, all mails in 45 days: {len((tot[0] or b'').split())}")
            except Exception as e:
                log("dry run search: " + repr(e)[:150])
            found = []
            for i in (data[0] or b"").split()[-20:]:
                typ, hd = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] BODYSTRUCTURE)")
                found.append(" | ".join(" ".join(str(x) for x in (y if isinstance(y, tuple) else (y,))) for y in hd)[:300])
            log("Bank statement dry run, e-mails found: " + str(len(found)) + "\n  " + "\n  ".join(found))
        for i in (data[0] or b"").split()[-30:]:
            typ, md = M.fetch(i, "(RFC822)")
            msg = email.message_from_bytes(md[0][1])
            mid = msg.get("Message-ID") or f"imap:{i.decode()}"
            if mid in done:
                continue
            got = False
            for part in msg.walk():
                fn = part.get_filename() or ""
                if fn:
                    fn = str(email.header.make_header(email.header.decode_header(fn)))
                if fn.lower().endswith(".pdf") or part.get_content_type() == "application/pdf":
                    pdfs.append((fn or "statement.pdf", part.get_payload(decode=True), mid))
                    got = True
            if not got:
                done[mid] = "no pdf"
        M.logout()
    for name, data, mid in pdfs:
        try:
            text = _pdf_text(data)
        except Exception as e:
            log(f"Bank statement {name}: couldn't read the PDF ({repr(e)[:120]}).")
            done[mid] = "unreadable"
            continue
        rows, period = parse_statement(text)
        if not period:
            done[mid] = "not a statement"
            continue
        STATEMENTS.mkdir(exist_ok=True)
        (STATEMENTS / f"FNB statement {period[1]}.pdf").write_bytes(data)
        added, to_sort, total = process(db, rows, since, log, f"FNB statement {period[0]} to {period[1]}", dry=dry)
        if dry:
            (HERE / "debug" / "bank_dry.json").write_text(json.dumps(DRY, indent=1), encoding="utf-8")
            log(f"Bank statement dry run ({period[1]}): {added} cost(s) R{total:,.2f}, {to_sort} to sort; details in debug/bank_dry.json")
            continue
        done[mid] = period[1]
        mon = datetime.strptime(period[1], "%Y-%m-%d").strftime("%B %Y")
        msg = (f"{mon}: {added} business cost(s) added (R{total:,.2f})"
               + (f", {to_sort} payment(s) to sort in the app" if to_sort else ", nothing to sort"))
        log("Bank statement " + msg + f" ({len(rows)} lines read).")
        if notify and (added or to_sort):
            try:
                notify({"url": cfg["url"], "key": cfg["key"]}, "🏦 Bank statement checked", msg, url="./?view=expenses", topic="price", log=log)
            except Exception:
                pass
    if dry:
        return
    DONE_FILE.parent.mkdir(exist_ok=True)
    DONE_FILE.write_text(json.dumps(done, indent=1), encoding="utf-8")
