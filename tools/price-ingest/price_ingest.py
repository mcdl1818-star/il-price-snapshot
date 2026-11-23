#!/usr/bin/env python3
"""Israeli supermarket public price files (Price Transparency Law) -> one compact JSON.

Python 3 standard library only. Sequential, polite requests; per-branch failure isolation.

Usage:
    python tools/price-ingest/price_ingest.py                 # all configured branches
    python tools/price-ingest/price_ingest.py --branches shukhair-004,netto-054
    python tools/price-ingest/price_ingest.py --contract      # also write the verbose contract file

Outputs (default tools/price-ingest/out/):
    prices.json          columnar-v1 (compact, see below) - the file the app should fetch
    prices.contract.json only with --contract: the verbose documented contract (large, ~10x)
    prices.meta.json     small summary: version, per-branch status/counts/errors, sizes

Verbose contract (prices.contract.json, also produced by expand_columnar()):
    {"version","generatedAt","promoIgnored":true,
     "branches":[{"id","chain","name","address","status","sourceUpdatedAt"}],
     "products":[{"productId":"<str int>","barcode":"<str>","name","packageLabel","unit"}],
     "prices":[{"branchId","productId","priceAgorot","unitPriceAgorot","unit",
                "sourceUpdatedAt","fetchedAt","sourceFileId"}]}

columnar-v1 (prices.json) carries exactly the same information:
    {"format":"columnar-v1","version","generatedAt","promoIgnored":true,
     "branches":[ same objects as above ],
     "units":[str,...],                       # shared string table for unit labels
     "productCols":["productId","barcode","name","packageLabel","unit"],
     "products":[[int,str,str,str,unitIdx],...],   # row index = product index
     "priceSets":[{"branchId","sourceUpdatedAt","fetchedAt","sourceFileId",
                   "p":[...], "a":[...], "u":[...], "n":[...]}]}
    p = product indexes, DELTA-encoded (p[0] absolute, then differences; ascending);
    a = priceAgorot; u = unitPriceAgorot (0 = absent); n = unit table index (-1 = none).
    sourceUpdatedAt / fetchedAt / sourceFileId are constant per branch, so hoisted.
    productId is stable across runs: int(barcode without leading zeros).

Prices are regular shelf prices only. PromoFull files are deliberately ignored (promoIgnored).
Times: version/generatedAt/fetchedAt are UTC. sourceUpdatedAt is the source file's own
timestamp (Israel local time in the file name) converted to UTC.
"""
import argparse
import datetime as dt
import gzip
import http.cookiejar
import io
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

USER_AGENT = "price-ingest/1.0 (sequential, low-volume, public price files only)"
REQUEST_DELAY_S = 0.4
MIN_ITEMS_PER_BRANCH = 50
MIN_PRICE_AGOROT = 10  # chains publish 0.01 placeholder rows
FORMAT_ID = "columnar-v1"

CERBERUS_BASE = "https://url.publishedprices.co.il"

# One entry per chain. `expect` are substrings of the resolved store name+address used to
# verify the store id still points at the intended branch (any one match is enough).
CHAINS = {
    "shukhair": {
        "label": "שוק העיר", "kind": "bina", "base": "https://shuk-hayir.binaprojects.com",
        "chainId": "7290058148776",
        "branches": {"004": {"expect": ["רמות"]}},
    },
    "netto": {
        "label": "נטו", "kind": "bina", "base": "https://supersapir.binaprojects.com",
        "chainId": "7290058156016",
        "branches": {"054": {"expect": ["רמות"]}},
    },
    "zolbegadol": {
        "label": "זול ובגדול", "kind": "bina", "base": "https://zolvebegadol.binaprojects.com",
        "chainId": "7290058173198",
        "branches": {"024": {"expect": ["רמות"]}},
    },
    "osherad": {
        "label": "אושר עד", "kind": "cerberus", "user": "osherad", "chainId": "7290103152017",
        "branches": {
            "003": {"expect": ["גבעת שאול"]},
            "026": {"expect": ["תלפיות", "הדר"]},
            "028": {"expect": ["שמגר"]},
        },
    },
    "ramilevy": {
        "label": "רמי לוי", "kind": "cerberus", "user": "RamiLevi", "chainId": "7290058140886",
        "branches": {"003": {"expect": ["רמות"]}, "001": {"expect": ["תלפיות"]}},
    },
}

CITY_CODES = {"3000": "ירושלים"}
UNKNOWN_UNITS = {"", "לא ידוע", "unknown"}

FILE_RE = re.compile(
    r"^(PriceFull|PromoFull|Price|Promo|Stores)(\d{13})-(\d{1,3})(?:-(\d{1,4}))?-(\d{8})-?(\d{6})",
    re.IGNORECASE,
)


class IngestError(Exception):
    pass


# --------------------------------------------------------------------------- time helpers

def _last_sunday(year, month):
    d = dt.date(year, month, 31)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def israel_to_utc(local):
    """Naive Israel-local datetime -> aware UTC (no tz database needed).
    DST: Friday before the last Sunday of March 02:00 -> last Sunday of October 02:00."""
    start = dt.datetime.combine(_last_sunday(local.year, 3) - dt.timedelta(days=2), dt.time(2))
    end = dt.datetime.combine(_last_sunday(local.year, 10), dt.time(2))
    offset = 3 if start <= local < end else 2
    return (local - dt.timedelta(hours=offset)).replace(tzinfo=dt.timezone.utc)


def iso(d):
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- file names

class FileRef:
    __slots__ = ("name", "kind", "chain", "store", "stamp")

    def __init__(self, name, kind, chain, store, stamp):
        self.name, self.kind, self.chain, self.store, self.stamp = name, kind, chain, store, stamp

    @property
    def local_time(self):
        return dt.datetime.strptime(self.stamp, "%Y%m%d%H%M%S")

    @property
    def utc_iso(self):
        return iso(israel_to_utc(self.local_time))


def parse_file_name(name):
    m = FILE_RE.match(name or "")
    if not m:
        return None
    kind, chain, _sub, store, date, tm = m.groups()
    kind = {"pricefull": "PriceFull", "promofull": "PromoFull", "price": "Price",
            "promo": "Promo", "stores": "Stores"}[kind.lower()]
    return FileRef(name, kind, chain, int(store) if store else None, date + tm)


def newest(refs, kind, store=None):
    cands = [r for r in refs if r and r.kind == kind and (store is None or r.store == store)]
    return max(cands, key=lambda r: r.stamp) if cands else None


# --------------------------------------------------------------------------- bytes -> XML

def sniff_decompress(data, max_layers=4):
    """Peel gzip / zip layers by magic bytes (names lie: zip under .gz, plain XML under .gz)."""
    for _ in range(max_layers):
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        elif data[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = [n for n in z.namelist() if not n.endswith("/")]
                if not names:
                    raise IngestError("empty zip archive")
                data = z.read(names[0])
        else:
            break
    return data


def decode_text(data):
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8")
    m = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([\w-]+)", data)
    candidates = ([m.group(1).decode("ascii")] if m else []) + ["utf-8", "cp1255"]
    for enc in candidates:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")


def xml_records(data, tags):
    """Yield {lowercase_child_tag: text} for each element whose lowercase local tag is in `tags`."""
    text = _CTRL.sub("", decode_text(sniff_decompress(data)))
    text = _DECL.sub("", text, count=1)
    for _ev, el in ET.iterparse(io.StringIO(text), events=("end",)):
        tag = el.tag.rsplit("}", 1)[-1].lower()
        if tag in tags:
            yield {c.tag.rsplit("}", 1)[-1].lower(): (c.text or "").strip() for c in el}
            el.clear()


# --------------------------------------------------------------------------- parsing rows

def to_agorot(value):
    """'7.90' -> 790. Decimal arithmetic, ROUND_HALF_UP; None for missing/invalid/non-positive."""
    if value is None:
        return None
    s = str(value).strip().replace(",", ".")
    if not s:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    a = int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return a if 0 < a < 10_000_000 else None


def clean_barcode(value):
    b = re.sub(r"\s+", "", value or "")
    if not b.isdigit() or not 7 <= len(b) <= 14 or not b.strip("0"):
        return None
    return b


def package_label(qty, unit_qty):
    unit = (unit_qty or "").strip()
    unit = "" if unit.lower() in UNKNOWN_UNITS else unit
    try:
        q = Decimal((qty or "").strip().replace(",", "."))
        q = format(q.normalize(), "f") if q > 0 else ""
    except InvalidOperation:
        q = ""
    if not q:
        return unit
    if not unit:
        return "" if q == "1" else q
    return f"{q} {unit}"


def parse_price_file(data):
    """Return {barcode: row} (last / newest PriceUpdateTime wins), counting skipped rows."""
    rows, skipped = {}, 0
    for rec in xml_records(data, {"item", "product"}):
        code = clean_barcode(rec.get("itemcode"))
        price = to_agorot(rec.get("itemprice"))
        name = (rec.get("itemname") or rec.get("manufactureitemdescription") or "").strip()
        if not code or price is None or price < MIN_PRICE_AGOROT or not name:
            skipped += 1
            continue
        stamp = rec.get("priceupdatetime", "")
        old = rows.get(code)
        if old and old["stamp"] > stamp:
            continue
        rows[code] = {
            "stamp": stamp, "name": name, "price": price,
            "unitPrice": to_agorot(rec.get("unitofmeasureprice")),
            "unit": (rec.get("unitofmeasure") or "").strip(),
            "packageLabel": package_label(rec.get("quantity"), rec.get("unitqty")),
            "productUnit": (rec.get("unitqty") or "").strip(),
        }
    return rows, skipped


def parse_stores(data):
    """-> {int store id: {"name","address"}}"""
    out = {}
    for rec in xml_records(data, {"store", "branch"}):
        sid = rec.get("storeid") or rec.get("storenumber")
        if not sid or not sid.strip().isdigit():
            continue
        city = (rec.get("city") or "").strip()
        city = CITY_CODES.get(city, "" if city.isdigit() else city)
        addr = (rec.get("address") or "").strip()
        if city and city not in addr:
            addr = f"{addr}, {city}" if addr else city
        out[int(sid)] = {"name": (rec.get("storename") or "").strip(), "address": addr}
    return out


# --------------------------------------------------------------------------- HTTP

class Http:
    def __init__(self, delay=REQUEST_DELAY_S, attempts=4, timeout=90):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener.addheaders = [("User-Agent", USER_AGENT)]
        self.delay, self.attempts, self.timeout = delay, attempts, timeout
        self._last = 0.0

    def request(self, url, data=None):
        last = None
        for attempt in range(self.attempts):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                with self.opener.open(url, data, timeout=self.timeout) as r:
                    body = r.read()
                self._last = time.monotonic()
                return body
            except urllib.error.HTTPError as e:
                self._last = time.monotonic()
                last = e
                if e.code < 500 and e.code != 429:
                    break
            except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
                self._last = time.monotonic()
                last = e
            if attempt + 1 < self.attempts:
                time.sleep(2 ** (attempt + 1))
        raise IngestError(f"request failed: {url.split('?')[0]}: {last}")


class CerberusSource:
    def __init__(self, user, http_client):
        self.user, self.http, self._listing = user, http_client, None

    @staticmethod
    def _token(html):
        m = re.search(r'csrftoken"\s+content="([^"]+)"', html)
        if not m:
            raise IngestError("csrf token not found on page")
        return m.group(1)

    def _login(self):
        html = self.http.request(CERBERUS_BASE + "/login").decode("utf-8", "replace")
        body = urllib.parse.urlencode(
            {"username": self.user, "password": "", "csrftoken": self._token(html)}).encode()
        self.http.request(CERBERUS_BASE + "/login/user", body)

    def list_files(self):
        if self._listing is None:
            self._login()
            html = self.http.request(CERBERUS_BASE + "/file").decode("utf-8", "replace")
            body = urllib.parse.urlencode({"sSearch": "", "iDisplayStart": 0,
                                           "iDisplayLength": 100000,
                                           "csrftoken": self._token(html)}).encode()
            try:
                rows = json.loads(self.http.request(CERBERUS_BASE + "/file/json/dir", body))["aaData"]
            except (ValueError, KeyError) as e:
                raise IngestError(f"bad directory listing for {self.user}: {e}")
            self._listing = [parse_file_name(r.get("name")) for r in rows if r.get("type") == "file"]
            self._listing = [r for r in self._listing if r]
        return self._listing

    def download(self, ref):
        return self.http.request(CERBERUS_BASE + "/file/d/" + urllib.parse.quote(ref.name))


class BinaSource:
    def __init__(self, base, chain_id, http_client):
        self.base, self.chain_id, self.http = base, chain_id, http_client
        self._cache = {}

    def _list(self, file_type):
        if file_type not in self._cache:
            url = (f"{self.base}/MainIO_Hok.aspx?_={self.chain_id}"
                   f"&wReshet={urllib.parse.quote('הכל')}&WFileType={file_type}")
            try:
                rows = json.loads(self.http.request(url))
            except ValueError as e:
                raise IngestError(f"bad Bina listing: {e}")
            refs = [parse_file_name(r.get("FileNm")) for r in rows]
            self._cache[file_type] = [r for r in refs if r]
        return self._cache[file_type]

    def list_files(self):
        return self._list(4) + self._list(1)

    def download(self, ref):
        meta = self.http.request(f"{self.base}/Download.aspx?FileNm={urllib.parse.quote(ref.name)}")
        try:
            path = json.loads(meta)[0]["SPath"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise IngestError(f"bad Bina download response for {ref.name}: {e}")
        return self.http.request(path)


def make_source(cfg, http_client):
    if cfg["kind"] == "cerberus":
        return CerberusSource(cfg["user"], http_client)
    return BinaSource(cfg["base"], cfg["chainId"], http_client)


# --------------------------------------------------------------------------- ingestion

def branch_id(chain_key, store):
    return f"{chain_key}-{store}"


def ingest_branch(chain_key, store, cfg, source, stores_cache, fetched_at):
    """Returns (branch_dict, rows, file_name). Raises IngestError for any problem."""
    bid = branch_id(chain_key, store)
    files = source.list_files()
    if "stores" not in stores_cache:
        ref = newest(files, "Stores")
        if not ref:
            raise IngestError("no Stores file listed")
        stores_cache["stores"] = parse_stores(source.download(ref))
    info = stores_cache["stores"].get(int(store))
    if not info:
        raise IngestError(f"store id {store} not found in Stores file (id no longer resolves)")
    hay = f"{info['name']} {info['address']}"
    expect = cfg["branches"][store].get("expect", [])
    if expect and not any(e in hay for e in expect):
        raise IngestError(f"store {store} resolves to '{hay}', expected one of {expect}")
    ref = newest(files, "PriceFull", int(store))
    if not ref:
        raise IngestError(f"no PriceFull file listed for store {store}")
    rows, skipped = parse_price_file(source.download(ref))
    if len(rows) < MIN_ITEMS_PER_BRANCH:
        raise IngestError(f"only {len(rows)} valid items in {ref.name} (parse problem?)")
    branch = {"id": bid, "chain": cfg["label"], "name": info["name"], "address": info["address"],
              "status": "available", "sourceUpdatedAt": ref.utc_iso}
    return branch, rows, ref.name, skipped


def run_ingest(selected=None, log=print, http_client=None):
    http_client = http_client or Http()
    fetched_at = iso(dt.datetime.now(dt.timezone.utc))
    branches, per_branch, report = [], {}, {}
    for chain_key, cfg in CHAINS.items():
        store_ids = [s for s in cfg["branches"] if not selected or branch_id(chain_key, s) in selected]
        if not store_ids:
            continue
        source, stores_cache = make_source(cfg, http_client), {}
        for store in store_ids:
            bid, t0 = branch_id(chain_key, store), time.monotonic()
            try:
                branch, rows, fname, skipped = ingest_branch(
                    chain_key, store, cfg, source, stores_cache, fetched_at)
                per_branch[bid] = (rows, fname, branch["sourceUpdatedAt"])
                report[bid] = {"status": "available", "items": len(rows), "skippedRows": skipped,
                               "file": fname, "seconds": round(time.monotonic() - t0, 1)}
            except Exception as e:  # isolate every failure to its branch
                info = (stores_cache.get("stores") or {}).get(int(store), {})
                branch = {"id": bid, "chain": cfg["label"], "name": info.get("name", ""),
                          "address": info.get("address", ""), "status": "not_connected",
                          "sourceUpdatedAt": None}
                report[bid] = {"status": "not_connected", "error": str(e),
                               "seconds": round(time.monotonic() - t0, 1)}
                log(f"FAILED {bid}: {e}")
            branches.append(branch)
            r = report[bid]
            log(f"{bid}: {r['status']} items={r.get('items', 0)} {r['seconds']}s")
    return branches, per_branch, report, fetched_at


# --------------------------------------------------------------------------- output formats

def build_documents(branches, per_branch, fetched_at, now=None):
    """-> (columnar_doc, contract_builder) from per-branch rows."""
    now = now or dt.datetime.now(dt.timezone.utc)
    master = {}  # barcode key (no leading zeros) -> product dict
    for bid, (rows, _f, _s) in per_branch.items():
        for code, row in rows.items():
            p = master.get(code.lstrip("0"))
            if p is None:
                master[code.lstrip("0")] = {
                    "barcode": code, "name": row["name"], "packageLabel": row["packageLabel"],
                    "unit": row["productUnit"]}
            else:
                for k, src in (("name", "name"), ("packageLabel", "packageLabel"),
                               ("unit", "productUnit")):
                    if not p[k] and row[src]:
                        p[k] = row[src]
    keys = sorted(master, key=lambda k: int(k))
    index = {k: i for i, k in enumerate(keys)}
    units, unit_idx = [], {}

    def uid(u):
        if not u:
            return -1
        if u not in unit_idx:
            unit_idx[u] = len(units)
            units.append(u)
        return unit_idx[u]

    products = [[int(k), master[k]["barcode"], master[k]["name"], master[k]["packageLabel"],
                 uid(master[k]["unit"])] for k in keys]
    price_sets = []
    for b in branches:
        if b["id"] not in per_branch:
            continue
        rows, fname, source_at = per_branch[b["id"]]
        ordered = sorted(((index[c.lstrip("0")], r) for c, r in rows.items()), key=lambda t: t[0])
        p, prev = [], 0
        for i, _r in ordered:
            p.append(i - prev)
            prev = i
        price_sets.append({
            "branchId": b["id"], "sourceUpdatedAt": source_at, "fetchedAt": fetched_at,
            "sourceFileId": fname, "p": p,
            "a": [r["price"] for _i, r in ordered],
            "u": [r["unitPrice"] or 0 for _i, r in ordered],
            "n": [uid(r["unit"]) for _i, r in ordered]})
    return {
        "format": FORMAT_ID, "version": now.strftime("%Y%m%d%H%M"), "generatedAt": iso(now),
        "promoIgnored": True, "branches": branches, "units": units,
        "productCols": ["productId", "barcode", "name", "packageLabel", "unit"],
        "products": products, "priceSets": price_sets}


def expand_columnar(doc):
    """columnar-v1 -> verbose documented contract."""
    units = doc["units"]

    def unit(i):
        return units[i] if i >= 0 else ""

    products = [{"productId": str(r[0]), "barcode": r[1], "name": r[2], "packageLabel": r[3],
                 "unit": unit(r[4])} for r in doc["products"]]
    prices = []
    for ps in doc["priceSets"]:
        idx = 0
        for d, a, u, n in zip(ps["p"], ps["a"], ps["u"], ps["n"]):
            idx += d
            prices.append({"branchId": ps["branchId"], "productId": products[idx]["productId"],
                           "priceAgorot": a, "unitPriceAgorot": u or None, "unit": unit(n),
                           "sourceUpdatedAt": ps["sourceUpdatedAt"], "fetchedAt": ps["fetchedAt"],
                           "sourceFileId": ps["sourceFileId"]})
    return {"version": doc["version"], "generatedAt": doc["generatedAt"],
            "promoIgnored": doc["promoIgnored"], "branches": doc["branches"],
            "products": products, "prices": prices}


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "out"))
    ap.add_argument("--branches", help="comma separated branch ids, e.g. shukhair-004,netto-054")
    ap.add_argument("--contract", action="store_true", help="also write verbose prices.contract.json")
    args = ap.parse_args(argv)
    selected = set(args.branches.split(",")) if args.branches else None
    os.makedirs(args.out, exist_ok=True)

    t0 = time.monotonic()
    branches, per_branch, report, fetched_at = run_ingest(selected)
    if not per_branch:
        print("ALL BRANCHES FAILED - keeping previous output untouched", file=sys.stderr)
        return 1
    doc = build_documents(branches, per_branch, fetched_at)
    raw = dumps(doc)
    write_atomic(os.path.join(args.out, "prices.json"), raw)
    sizes = {"prices.json": {"rawBytes": len(raw), "gzipBytes": len(gzip.compress(raw, 9))}}
    if args.contract:
        full = dumps(expand_columnar(doc))
        write_atomic(os.path.join(args.out, "prices.contract.json"), full)
        sizes["prices.contract.json"] = {"rawBytes": len(full),
                                         "gzipBytes": len(gzip.compress(full, 9))}
    meta = {"version": doc["version"], "generatedAt": doc["generatedAt"], "format": FORMAT_ID,
            "promoIgnored": True, "products": len(doc["products"]),
            "prices": sum(len(ps["p"]) for ps in doc["priceSets"]),
            "branches": report, "files": sizes, "durationSeconds": round(time.monotonic() - t0, 1)}
    write_atomic(os.path.join(args.out, "prices.meta.json"),
                 json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))
    for name, s in sizes.items():
        print(f"{name}: raw {s['rawBytes'] / 1e6:.2f} MB, gzip {s['gzipBytes'] / 1e6:.2f} MB")
    failed = [b for b, r in report.items() if r["status"] != "available"]
    print(f"products={meta['products']} prices={meta['prices']} failed={failed or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
