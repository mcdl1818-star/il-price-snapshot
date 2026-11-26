import datetime as dt
import gzip
import io
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import price_ingest as pi  # noqa: E402

PRICE_XML = """<?xml version="1.0" encoding="utf-8"?>
<Root><ChainID>7290058140886</ChainID><Items>
<Item><PriceUpdateTime>2026-01-01T10:00:00</PriceUpdateTime><ItemCode>07290000211169</ItemCode>
<ItemName>חלב תנובה 3% 1 ליטר</ItemName><Quantity>1.00</Quantity><UnitQty>ליטר</UnitQty>
<UnitOfMeasure>ליטר</UnitOfMeasure><ItemPrice>7.90</ItemPrice><UnitOfMeasurePrice>7.90</UnitOfMeasurePrice></Item>
<Item><ItemCode>7290000211312</ItemCode><ItemName>גבינה 250 גרם</ItemName><Quantity>250.00</Quantity>
<UnitQty>גרם</UnitQty><UnitOfMeasure>100 גרם</UnitOfMeasure><ItemPrice>1.005</ItemPrice><UnitOfMeasurePrice></UnitOfMeasurePrice></Item>
<Item><ItemCode>123</ItemCode><ItemName>internal code</ItemName><ItemPrice>5</ItemPrice></Item>
<Item><ItemCode>7290000999999</ItemCode><ItemName>no price</ItemName><ItemPrice>0</ItemPrice></Item>
<Item><ItemCode>7290000888888</ItemCode><ItemName>bad price</ItemName><ItemPrice>abc</ItemPrice></Item>
<Item><ItemCode>7290000777777</ItemCode><ItemName></ItemName><ItemPrice>3</ItemPrice></Item>
<Item><ItemCode>07290000211169</ItemCode><PriceUpdateTime>2025-01-01T10:00:00</PriceUpdateTime>
<ItemName>older duplicate</ItemName><ItemPrice>1.00</ItemPrice></Item>
</Items></Root>"""

STORES_XML = """<Root><SubChains><SubChain><Stores>
<Store><StoreID>003</StoreID><StoreName>רמות</StoreName><Address>90 דרך החורש</Address><City>3000</City></Store>
<Store><StoreID>7</StoreID><StoreName>אחר</StoreName><Address>רחוב 1, חיפה</Address><City>4000</City></Store>
</Stores></SubChain></SubChains></Root>"""


class SniffTests(unittest.TestCase):
    def test_plain_utf16_with_bom(self):
        data = b"\xff\xfe" + PRICE_XML.replace("utf-8", "utf-16").encode("utf-16-le")
        rows, _ = pi.parse_price_file(data)
        self.assertIn("7290000211312", rows)

    def test_gzip(self):
        rows, _ = pi.parse_price_file(gzip.compress(PRICE_XML.encode("utf-8")))
        self.assertEqual(rows["7290000211312"]["name"], "גבינה 250 גרם")

    def test_zip_under_gz_name(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("inner.xml", PRICE_XML.encode("utf-8-sig"))
        self.assertEqual(pi.sniff_decompress(buf.getvalue())[:3], b"\xef\xbb\xbf")
        rows, _ = pi.parse_price_file(buf.getvalue())
        self.assertEqual(len(rows), 2)

    def test_gzip_of_utf16(self):
        raw = b"\xff\xfe" + PRICE_XML.replace("utf-8", "utf-16").encode("utf-16-le")
        rows, _ = pi.parse_price_file(gzip.compress(raw))
        self.assertEqual(len(rows), 2)

    def test_cp1255_fallback(self):
        xml = '<Root><Items><Item><ItemCode>7290000211312</ItemCode><ItemName>חלב</ItemName><ItemPrice>2</ItemPrice></Item></Items></Root>'
        rows, _ = pi.parse_price_file(xml.encode("cp1255"))
        self.assertEqual(rows["7290000211312"]["name"], "חלב")


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.skipped = pi.parse_price_file(PRICE_XML.encode("utf-8"))

    def test_barcode_is_string_with_leading_zero(self):
        self.assertIn("07290000211169", self.rows)
        self.assertIsInstance(next(iter(self.rows)), str)

    def test_skips_invalid(self):
        self.assertEqual(set(self.rows), {"07290000211169", "7290000211312"})
        self.assertEqual(self.skipped, 4)

    def test_placeholder_penny_price_skipped(self):
        xml = ('<Root><Items><Item><ItemCode>7290000211312</ItemCode><ItemName>x</ItemName>'
               '<ItemPrice>0.01</ItemPrice></Item></Items></Root>')
        self.assertEqual(pi.parse_price_file(xml.encode())[0], {})

    def test_newest_price_update_wins_over_later_older_row(self):
        self.assertEqual(self.rows["07290000211169"]["price"], 790)

    def test_fields(self):
        r = self.rows["7290000211312"]
        self.assertEqual((r["packageLabel"], r["unit"], r["unitPrice"]), ("250 גרם", "100 גרם", None))
        self.assertEqual(self.rows["07290000211169"]["unitPrice"], 790)

    def test_stores(self):
        s = pi.parse_stores(STORES_XML.encode("utf-8"))
        self.assertEqual(s[3], {"name": "רמות", "address": "90 דרך החורש, ירושלים"})
        self.assertEqual(s[7]["address"], "רחוב 1, חיפה")


class AgorotTests(unittest.TestCase):
    def test_rounding(self):
        cases = {"7.90": 790, "1.005": 101, "0.995": 100, "19.99": 1999, "0.29": 29,
                 "1.1": 110, "2,50": 250, "8.285": 829, " 3 ": 300}
        for src, want in cases.items():
            self.assertEqual(pi.to_agorot(src), want, src)

    def test_no_float_drift(self):
        self.assertEqual(pi.to_agorot("4.35"), 435)   # 4.35*100 == 434.99999999999994 as float
        self.assertEqual(pi.to_agorot("1.15"), 115)

    def test_invalid(self):
        for src in (None, "", "0", "0.00", "-1", "abc", "NaN", "Infinity", "100000"):
            self.assertIsNone(pi.to_agorot(src), src)

    def test_barcode_rules(self):
        self.assertEqual(pi.clean_barcode(" 0729 0000211169"), "07290000211169")
        for bad in ("123", "000000000000", "72900A0211312", "", None, "1" * 15):
            self.assertIsNone(pi.clean_barcode(bad))

    def test_package_label(self):
        self.assertEqual(pi.package_label("1.00", "ליטר"), "1 ליטר")
        self.assertEqual(pi.package_label("0.50", "ק\"ג"), "0.5 ק\"ג")
        self.assertEqual(pi.package_label("1.00", "לא ידוע"), "")
        self.assertEqual(pi.package_label("", "גרם"), "גרם")


class NamesAndTimeTests(unittest.TestCase):
    def test_file_names_case_insensitive(self):
        a = pi.parse_file_name("pricefull7290058140886-001-003-20261123-060006.gz")
        self.assertEqual((a.kind, a.store, a.stamp), ("PriceFull", 3, "20261123060006"))
        b = pi.parse_file_name("PriceFull7290058148776-000-004-20261123-050308.GZ")
        self.assertEqual(b.store, 4)
        s = pi.parse_file_name("Stores7290058140886-000-20261123-050500.xml")
        self.assertEqual((s.kind, s.store), ("Stores", None))
        self.assertEqual(pi.parse_file_name("Price7290058140886-001-001-20261123-070008.gz").kind, "Price")
        self.assertIsNone(pi.parse_file_name("readme.txt"))

    def test_newest_picks_latest_full(self):
        refs = [pi.parse_file_name(n) for n in (
            "PriceFull7290058140886-001-003-20261123-001013.gz",
            "PriceFull7290058140886-001-003-20261123-060006.gz",
            "Price7290058140886-001-003-20261123-090006.gz",
            "PriceFull7290058140886-001-005-20261123-120006.gz")]
        self.assertEqual(pi.newest(refs, "PriceFull", 3).stamp, "20261123060006")

    def test_israel_dst(self):
        winter = pi.israel_to_utc(dt.datetime(2026, 11, 23, 6, 0, 0))
        summer = pi.israel_to_utc(dt.datetime(2026, 7, 1, 6, 0, 0))
        self.assertEqual(pi.iso(winter), "2026-11-23T04:00:00Z")
        self.assertEqual(pi.iso(summer), "2026-07-01T03:00:00Z")
        self.assertEqual(pi.iso(pi.israel_to_utc(dt.datetime(2026, 3, 26, 12))), "2026-03-26T10:00:00Z")
        self.assertEqual(pi.iso(pi.israel_to_utc(dt.datetime(2026, 3, 27, 3))), "2026-03-27T00:00:00Z")
        self.assertEqual(pi.iso(pi.israel_to_utc(dt.datetime(2026, 3, 27, 1))), "2026-03-26T23:00:00Z")
        self.assertEqual(pi.iso(pi.israel_to_utc(dt.datetime(2026, 10, 25, 3))), "2026-10-25T01:00:00Z")


class FormatTests(unittest.TestCase):
    def test_columnar_roundtrip_and_dedupe(self):
        r1, _ = pi.parse_price_file(PRICE_XML.encode("utf-8"))
        r2, _ = pi.parse_price_file(PRICE_XML.replace("7.90", "8.50").encode("utf-8"))
        branches = [{"id": "a-1", "status": "available"}, {"id": "b-2", "status": "available"},
                    {"id": "c-3", "status": "not_connected"}]
        per = {"a-1": (r1, "fileA", "2026-11-23T04:00:00Z"), "b-2": (r2, "fileB", "2026-11-23T05:00:00Z")}
        now = dt.datetime(2026, 11, 23, 7, 30, tzinfo=dt.timezone.utc)
        doc = pi.build_documents(branches, per, "2026-11-23T07:30:00Z", now)
        self.assertEqual(doc["version"], "202611230730")
        self.assertTrue(doc["promoIgnored"])
        self.assertEqual(len(doc["products"]), 2)
        full = pi.expand_columnar(doc)
        self.assertEqual(len(full["prices"]), 4)
        milk = [p for p in full["products"] if p["barcode"] == "07290000211169"][0]
        by_branch = {p["branchId"]: p for p in full["prices"] if p["productId"] == milk["productId"]}
        self.assertEqual((by_branch["a-1"]["priceAgorot"], by_branch["b-2"]["priceAgorot"]), (790, 850))
        self.assertEqual(by_branch["a-1"]["sourceFileId"], "fileA")
        self.assertIsNone([p for p in full["prices"] if p["priceAgorot"] == 101][0]["unitPriceAgorot"])
        self.assertEqual(milk["productId"], "7290000211169")
        self.assertEqual(full["branches"][2]["status"], "not_connected")


V2_XML = """<Root><Items>
<Item><ItemCode>7290000211312</ItemCode><ItemName>גבינה</ItemName><ManufactureItemDescription>גבינה לבנה 250 גרם</ManufactureItemDescription>
<ManufactureName>תנובה</ManufactureName><bIsWeighted>0</bIsWeighted><Quantity>250</Quantity><UnitQty>גרם</UnitQty><ItemPrice>6.50</ItemPrice></Item>
<Item><ItemCode>706</ItemCode><ItemName>עגבניות</ItemName><ManufactureName>לא ידוע</ManufactureName><bIsWeighted>1</bIsWeighted><ItemPrice>24.90</ItemPrice><UnitOfMeasurePrice>24.90</UnitOfMeasurePrice></Item>
<Item><ItemCode>102</ItemCode><ItemName>חסה</ItemName><ManufactureName>מבצעי ראש השנה</ManufactureName><bIsWeighted>0</bIsWeighted><ItemPrice>6</ItemPrice></Item>
<Item><ItemCode>55</ItemCode><ItemName>שקית ניילון</ItemName><ItemPrice>0.30</ItemPrice></Item>
<Item><ItemCode>56</ItemCode><ItemName>דמי משלוח</ItemName><ItemPrice>25</ItemPrice></Item>
<Item><ItemCode>57</ItemCode><ItemName>פלפל</ItemName><ItemPrice>0.05</ItemPrice></Item>
<Item><ItemCode></ItemCode><ItemName>ללא קוד</ItemName><ItemPrice>5</ItemPrice></Item>
</Items></Root>"""


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.rows, self.stats = pi.parse_price_file_ex(V2_XML.encode("utf-8"))

    def test_local_rows_kept_and_counted(self):
        self.assertEqual(set(self.rows), {"7290000211312", "L:706", "L:102"})
        self.assertEqual((self.stats["keptBarcoded"], self.stats["keptLocal"]), (1, 2))
        self.assertEqual(self.stats["dropped"], {"invalidPrice": 1, "noName": 0, "noCode": 1,
                                                 "nonGoods": 2, "duplicate": 0})

    def test_weighted_and_brand(self):
        self.assertEqual(self.rows["L:706"]["weighted"], 1)
        self.assertEqual(self.rows["L:102"]["weighted"], 0)
        self.assertEqual(self.rows["7290000211312"]["brand"], "תנובה")
        self.assertEqual(self.rows["L:706"]["brand"], "")
        self.assertEqual(self.rows["L:102"]["brand"], "")  # department label is noise

    def test_longer_name_preferred(self):
        self.assertEqual(self.rows["7290000211312"]["longName"], "גבינה לבנה 250 גרם")
        self.assertEqual(self.rows["7290000211312"]["name"], "גבינה")

    def test_v1_view_unchanged(self):
        rows, skipped = pi.parse_price_file(V2_XML.encode("utf-8"))
        self.assertEqual(set(rows), {"7290000211312"})
        self.assertEqual(skipped, 6)

    def _docs(self):
        other, _ = pi.parse_price_file_ex(V2_XML.replace("24.90", "26.00").encode("utf-8"))
        other["L:706"]["weighted"] = 0
        branches = [{"id": "shukhair-004", "status": "available"},
                    {"id": "netto-054", "status": "available"}]
        per = {"shukhair-004": (self.rows, "f1", "2026-11-26T04:00:00Z"),
               "netto-054": (other, "f2", "2026-11-26T05:00:00Z")}
        now = dt.datetime(2026, 11, 26, 7, 30, tzinfo=dt.timezone.utc)
        return (pi.build_documents(branches, per, "x", now),
                pi.build_documents(branches, per, "x", now, v2=True))

    def test_v1_shape_unchanged_and_excludes_local(self):
        d1, _ = self._docs()
        self.assertEqual(d1["format"], "columnar-v1")
        self.assertEqual(d1["productCols"], ["productId", "barcode", "name", "packageLabel", "unit"])
        self.assertEqual(len(d1["products"]), 1)
        self.assertEqual(len(d1["products"][0]), 5)
        self.assertEqual(d1["products"][0][2], "גבינה")
        self.assertNotIn("weightedConflicts", d1)

    def test_v2_roundtrip(self):
        _, d2 = self._docs()
        self.assertEqual(d2["format"], "columnar-v2")
        self.assertEqual(d2["productCols"][-3:], ["weighted", "brand", "local"])
        self.assertEqual(len(d2["products"]), 5)  # 1 barcoded + 2 local x 2 chains
        self.assertEqual(d2["products"][0][0], 7290000211312)
        ids = [p[0] for p in d2["products"]]
        self.assertIn("L-shukhair-706", ids)
        self.assertIn("L-netto-706", ids)
        self.assertEqual(d2["weightedConflicts"], 0)
        full = pi.expand_columnar_v2(d2)
        tom = {p["productId"]: p for p in full["products"]}["L-shukhair-706"]
        self.assertEqual((tom["barcode"], tom["weighted"], tom["local"], tom["name"]), ("", True, True, "עגבניות"))
        price = {(x["branchId"], x["productId"]): x for x in full["prices"]}
        self.assertEqual(price[("shukhair-004", "L-shukhair-706")]["priceAgorot"], 2490)
        self.assertEqual(price[("netto-054", "L-netto-706")]["priceAgorot"], 2600)
        self.assertEqual(price[("netto-054", "L-netto-706")]["unitPriceAgorot"], 2600)
        self.assertEqual(price[("shukhair-004", "7290000211312")]["priceAgorot"], 650)
        cheese = {p["productId"]: p for p in full["products"]}["7290000211312"]
        self.assertEqual((cheese["name"], cheese["brand"], cheese["local"]), ("גבינה לבנה 250 גרם", "תנובה", False))

    def test_weighted_any_branch_and_conflict_count(self):
        branches = [{"id": "osherad-003"}, {"id": "osherad-026"}]
        a, _ = pi.parse_price_file_ex(V2_XML.encode("utf-8"))
        b, _ = pi.parse_price_file_ex(V2_XML.encode("utf-8"))
        a["7290000211312"]["weighted"] = 1
        per = {"osherad-003": (a, "f", "t"), "osherad-026": (b, "f", "t")}
        d = pi.build_documents(branches, per, "x", None, v2=True)
        cheese = [p for p in d["products"] if p[1] == "7290000211312"][0]
        self.assertEqual(cheese[5], 1)
        self.assertEqual(d["weightedConflicts"], 1)
        self.assertEqual(len([p for p in d["products"] if p[7]]), 2)  # same chain: merged


if __name__ == "__main__":
    unittest.main()
