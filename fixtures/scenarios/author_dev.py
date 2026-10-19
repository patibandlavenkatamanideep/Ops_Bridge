"""Hand-authored development seed corpus → fixtures/scenarios/dev.yaml.

Every quantity, price and expected label below is typed by hand from the policy files
(packs/*/policies). Nothing is computed by OpsBridge code, so the corpus is an
independent oracle for the matcher, mappings and next-action routing.

Run: uv --directory backend run python ../fixtures/scenarios/author_dev.py
"""

from __future__ import annotations

from pathlib import Path

import yaml

ACME = {"id": "SUP-ACME", "name": "Acme Office Furniture Inc"}
NGS = {"id": "SUP-NGS", "name": "Northgate Seating Co"}
BSD = {"id": "SUP-BSD", "name": "Birch and Steel Desks Ltd"}
LUM = {"id": "VEN-LUMEN", "name": "Lumen Compute GmbH"}
PAR = {"id": "VEN-PARA", "name": "Parallel Peripherals BV"}

QTY = "propose_action:create_follow_up_task"
PRICE = "propose_action:create_supplier_message_draft"
INFO = "request_information:create_follow_up_task"
ESC = "escalate:assign_case"
OK = "no_discrepancy:record_review_note"


def fur(n, fam, tmpl, desc, sup, po_lines, receipts, inv_lines, expected, allocations=(), messages=(),  # noqa: ANN001
        inv_overrides=None):
    po = {"number": f"PO-1{n:03d}", "currency": "USD", "date": "2026-09-01", "lines": po_lines}
    inv = {"number": f"INV-9{n:03d}", "date": "2026-09-30", "supplier_id": sup["id"], "supplier_name": sup["name"],
           "po_number": po["number"], "currency": "USD", "lines": inv_lines}
    inv.update(inv_overrides or {})
    return {"id": f"fur-dev-{n:03d}", "pack": "furniture", "family": fam, "template": tmpl, "split": "dev",
            "description": desc, "supplier": sup, "po": po,
            "receipts": [{"id": f"GR-5{n:03d}{chr(65 + i)}", "date": "2026-09-20", "lines": r}
                         for i, r in enumerate(receipts)],
            "invoice": inv, "allocations": list(allocations),
            "messages": [{"id": f"MSG-F{n:03d}{i}", "text": t} for i, t in enumerate(messages)], "expected": expected}


def it(n, fam, tmpl, desc, sup, po_lines, receipts, inv_lines, expected, currency="EUR", messages=()):  # noqa: ANN001
    po = {"number": f"PO-2{n:03d}", "currency": currency, "date": "2026-08-15", "lines": po_lines}
    inv = {"number": f"INV-7{n:03d}", "date": "2026-09-25", "supplier_id": sup["id"], "supplier_name": sup["name"],
           "po_number": po["number"], "currency": currency, "lines": inv_lines}
    return {"id": f"it-dev-{n:03d}", "pack": "it_hardware", "family": fam, "template": tmpl, "split": "dev",
            "description": desc, "supplier": sup, "po": po,
            "receipts": [{"id": f"GRN-6{n:03d}{chr(65 + i)}", "date": "2026-09-10", "lines": r}
                         for i, r in enumerate(receipts)],
            "invoice": inv, "allocations": [],
            "messages": [{"id": f"MSG-I{n:03d}{i}", "text": t} for i, t in enumerate(messages)], "expected": expected}


def L(line, qty, price, item=None, desc="Item", unit="EA"):  # noqa: ANN001, N802
    return {"line": line, "item": item or f"SKU-{line}", "desc": desc, "qty": qty, "unit": unit, "price": price}


def IL(line, po_line, qty, price, item=None, desc="Item", unit="EA", kind="goods", discount=None):  # noqa: ANN001, N802
    d = {"line": line, "po_line": po_line, "item": item, "desc": desc, "qty": qty, "unit": unit, "price": price,
         "kind": kind}
    if discount is not None:
        d["discount"] = discount
    return d


def R(line, qty, unit="EA", kind="receipt", status="accepted"):  # noqa: ANN001, N802
    return {"line": line, "qty": qty, "unit": unit, "kind": kind, "status": status}


def E(outcome, types, action, note):  # noqa: ANN001, N802
    return {"outcome": outcome, "types": types, "next_action": action, "note": note}


chair = {"item": "CH-100", "desc": "Task chair, mesh back"}
lap = {"item": "LT-14", "desc": "Laptop 14in"}
mon = {"item": "MN-27", "desc": "Monitor 27in"}

S = [
    fur(1, "FUR-QTY-SHORT", "acme-pdf-v1", "Order 20 chairs, receipt 18, invoice 20.", ACME,
        [L(1, "20", "185.00", **chair)], [[R(1, "18")]], [IL(1, 1, "20", "185.00", **chair)],
        E("discrepancies_found", ["quantity_mismatch"], QTY, "2 of 20 not received")),
    fur(2, "FUR-QTY-SHORT", "acme-pdf-v1", "Clean three-way match for desks.", ACME,
        [L(1, "10", "420.00", item="DK-200", desc="Sit-stand desk")], [[R(1, "10")]],
        [IL(1, 1, "10", "420.00", item="DK-200", desc="Sit-stand desk")], E("matched", [], OK, "all equal")),
    fur(3, "FUR-QTY-SHORT", "acme-pdf-v1", "Two partial receipts (7+3) against 12 invoiced.", ACME,
        [L(1, "12", "310.00", item="CB-310", desc="Filing cabinet")], [[R(1, "7")], [R(1, "3")]],
        [IL(1, 1, "12", "310.00", item="CB-310", desc="Filing cabinet")],
        E("discrepancies_found", ["quantity_mismatch"], QTY, "10 received cumulatively, 12 billed")),
    fur(4, "FUR-QTY-SHORT", "acme-pdf-v1", "Full receipt of 30 chairs.", ACME,
        [L(1, "30", "185.00", **chair)], [[R(1, "30")]], [IL(1, 1, "30", "185.00", **chair)], E("matched", [], OK, "")),
    fur(5, "FUR-QTY-RETURN", "northgate-pdf-v1", "Return of 4 after receiving 20; invoice for 20.", NGS,
        [L(1, "20", "150.00", item="ST-150", desc="Stacking chair")], [[R(1, "20")], [R(1, "4", kind="return")]],
        [IL(1, 1, "20", "150.00", item="ST-150", desc="Stacking chair")],
        E("discrepancies_found", ["quantity_mismatch"], QTY, "net 16 received")),
    fur(6, "FUR-QTY-RETURN", "northgate-pdf-v1", "Return entry reversed; net 20 received.", NGS,
        [L(1, "20", "150.00", item="ST-150", desc="Stacking chair")],
        [[R(1, "20")], [R(1, "4", kind="return", status="reversed")]],
        [IL(1, 1, "20", "150.00", item="ST-150", desc="Stacking chair")], E("matched", [], OK, "reversed return ignored")),
    fur(7, "FUR-PRICE", "acme-pdf-v1", "Price +0.50, exactly at absolute tolerance.", ACME,
        [L(1, "20", "185.00", **chair)], [[R(1, "20")]], [IL(1, 1, "20", "185.50", **chair)],
        E("matched", [], OK, "boundary passes")),
    fur(8, "FUR-PRICE", "acme-pdf-v1", "Price +1.85, exactly 1% of 185.00.", ACME,
        [L(1, "20", "185.00", **chair)], [[R(1, "20")]], [IL(1, 1, "20", "186.85", **chair)],
        E("matched", [], OK, "percent boundary passes")),
    fur(9, "FUR-PRICE", "acme-pdf-v1", "Price +1.86, one cent outside 1%.", ACME,
        [L(1, "20", "185.00", **chair)], [[R(1, "20")]], [IL(1, 1, "20", "186.86", **chair)],
        E("discrepancies_found", ["price_mismatch"], PRICE, "one minor unit outside")),
    fur(10, "FUR-PRICE", "birch-pdf-v1", "Sofas billed at 960 vs PO 900.", BSD,
        [L(1, "5", "900.00", item="SF-900", desc="Lounge sofa")], [[R(1, "5")]],
        [IL(1, 1, "5", "960.00", item="SF-900", desc="Lounge sofa")],
        E("discrepancies_found", ["price_mismatch"], PRICE, "+60 per unit")),
    fur(11, "FUR-PRICE", "birch-pdf-v1", "List 200 with a 200.00 line discount on 20 units = net 190; PO 190.", BSD,
        [L(1, "20", "190.00", item="BK-190", desc="Bookcase")], [[R(1, "20")]],
        [IL(1, 1, "20", "200.00", item="BK-190", desc="Bookcase", discount="200.00")],
        E("matched", [], OK, "net price equal")),
    fur(12, "FUR-MISSING-GR", "acme-pdf-v1", "No receiving record for lamps.", ACME,
        [L(1, "15", "45.00", item="LP-45", desc="Desk lamp")], [], [IL(1, 1, "15", "45.00", item="LP-45", desc="Desk lamp")],
        E("needs_information", ["missing_receipt"], INFO, "missing receipt is not non-delivery")),
    fur(13, "FUR-MISSING-GR", "acme-pdf-v1", "No receiving record; supplier email claims full delivery.", ACME,
        [L(1, "15", "45.00", item="LP-45", desc="Desk lamp")], [], [IL(1, 1, "15", "45.00", item="LP-45", desc="Desk lamp")],
        E("needs_information", ["missing_receipt"], INFO, "a supplier claim does not establish delivery"),
        messages=["Hello AP team,\nWe delivered all 15 lamps for PO-1013 on 18 September.\nRegards, Acme billing"]),
    fur(14, "FUR-MIXED", "birch-pdf-v1", "Line 1 short-received, line 2 overpriced.", BSD,
        [L(1, "20", "185.00", **chair), L(2, "4", "600.00", item="TB-600", desc="Meeting table")],
        [[R(1, "18"), R(2, "4")]],
        [IL(1, 1, "20", "185.00", **chair), IL(2, 2, "4", "640.00", item="TB-600", desc="Meeting table")],
        E("discrepancies_found", ["price_mismatch", "quantity_mismatch"], QTY, "quantity route has priority")),
    fur(15, "FUR-MIXED", "birch-pdf-v1", "Line 1 has no receiving record, line 2 received.", BSD,
        [L(1, "10", "100.00", item="PN-100", desc="Privacy panel"), L(2, "10", "50.00", item="CL-050", desc="Coat hook")],
        [[R(2, "10")]],
        [IL(1, 1, "10", "100.00", item="PN-100", desc="Privacy panel"),
         IL(2, 2, "10", "50.00", item="CL-050", desc="Coat hook")],
        E("needs_information", ["missing_receipt"], INFO, "line 1 receipt unavailable")),
    fur(16, "FUR-ALLOC", "northgate-pdf-v1", "Receipt of 10 already matched to an earlier invoice.", NGS,
        [L(1, "20", "185.00", **chair)], [[R(1, "10")]], [IL(1, 1, "10", "185.00", **chair)],
        E("discrepancies_found", ["quantity_mismatch"], QTY, "no double allocation"),
        allocations=[{"invoice": "INV-8016", "line": 1, "qty": "10", "state": "active"}]),
    fur(17, "FUR-ALLOC", "northgate-pdf-v1", "20 received, 10 previously matched, 10 invoiced now.", NGS,
        [L(1, "20", "185.00", **chair)], [[R(1, "20")]], [IL(1, 1, "10", "185.00", **chair)], E("matched", [], OK, ""),
        allocations=[{"invoice": "INV-8017", "line": 1, "qty": "10", "state": "active"}]),
    fur(18, "FUR-ALLOC", "northgate-pdf-v1", "Earlier allocation cancelled, so the receipt is available.", NGS,
        [L(1, "20", "185.00", **chair)], [[R(1, "10")]], [IL(1, 1, "10", "185.00", **chair)], E("matched", [], OK, ""),
        allocations=[{"invoice": "INV-8018", "line": 1, "qty": "10", "state": "cancelled"}]),
    fur(19, "FUR-REVIEW", "acme-pdf-v1", "Invoice uses an undefined unit ('crate').", ACME,
        [L(1, "20", "185.00", **chair)], [[R(1, "20")]], [IL(1, 1, "20", "185.00", unit="crate", **chair)],
        E("manual_review", [], ESC, "unit unknown")),
    fur(20, "FUR-REVIEW", "acme-pdf-v1", "Invoice in EUR against a USD PO.", ACME,
        [L(1, "20", "185.00", **chair)], [[R(1, "20")]], [IL(1, 1, "20", "185.00", **chair)],
        E("manual_review", [], ESC, "no currency conversion"), inv_overrides={"currency": "EUR"}),
    fur(21, "FUR-REVIEW", "acme-pdf-v1", "No supplier ID and a similar but different supplier name.", ACME,
        [L(1, "20", "185.00", **chair)], [[R(1, "18")]], [IL(1, 1, "20", "185.00", **chair)],
        E("manual_review", [], ESC, "no silent cross-supplier join"),
        inv_overrides={"supplier_id": None, "supplier_name": "Acme Office Furnishings LLC"}),
    fur(22, "FUR-UNITS", "birch-pdf-v1", "Invoice in boxes of 10; PO and receipt in each.", BSD,
        [L(1, "40", "18.00", item="CT-018", desc="Cable tray")], [[R(1, "40")]],
        [IL(1, 1, "4", "180.00", item="CT-018", desc="Cable tray", unit="box of 10")],
        E("matched", [], OK, "configured conversion")),
    fur(23, "FUR-CONFLICT", "acme-pdf-v1", "Supplier email says 20 delivered; receiving record says 18.", ACME,
        [L(1, "20", "185.00", **chair)], [[R(1, "18")]], [IL(1, 1, "20", "185.00", **chair)],
        E("discrepancies_found", ["quantity_mismatch"], QTY, "receiving record controls by policy"),
        messages=["Hi,\nWe delivered all 20 chairs for PO-1023 last week, please pay the full invoice.\nThanks"]),
    fur(24, "FUR-DOC-DEFECT", "acme-pdf-v1", "Invoice PDF line amount inconsistent with qty x price.", ACME,
        [L(1, "20", "185.00", **chair)], [[R(1, "20")]], [IL(1, 1, "20", "185.00", **chair)],
        E("manual_review", [], ESC, "extraction not authoritative"), inv_overrides={"defect": "amount_mismatch"}),
    fur(25, "FUR-MIXED", "acme-pdf-v1", "Freight charge line alongside a matched goods line.", ACME,
        [L(1, "10", "300.00", item="DR-300", desc="Pedestal drawer")], [[R(1, "10")]],
        [IL(1, 1, "10", "300.00", item="DR-300", desc="Pedestal drawer"),
         IL(2, None, "1", "75.00", desc="Freight", kind="charge")],
        E("matched", [], OK, "charges excluded and disclosed")),
    it(1, "IT-QTY", "lumen-json-v1", "Laptops fully received.", LUM, [L(1, "10", "1450.00", **lap)], [[R(1, "10")]],
       [IL(1, 1, "10", "1450.00", **lap)], E("matched", [], OK, "")),
    it(2, "IT-QTY", "lumen-json-v1", "8 of 10 laptops received.", LUM, [L(1, "10", "1450.00", **lap)], [[R(1, "8")]],
       [IL(1, 1, "10", "1450.00", **lap)], E("discrepancies_found", ["quantity_mismatch"], QTY, "")),
    it(3, "IT-PRICE", "para-json-v1", "Monitor +5.00: absolute passes, 2% (4.40) fails; both rules required.", PAR,
       [L(1, "25", "220.00", **mon)], [[R(1, "25")]], [IL(1, 1, "25", "225.00", **mon)],
       E("discrepancies_found", ["price_mismatch"], PRICE, "both-rules policy")),
    it(4, "IT-PRICE", "para-json-v1", "Monitor +4.40: exactly 2% and within 5.00.", PAR,
       [L(1, "25", "220.00", **mon)], [[R(1, "25")]], [IL(1, 1, "25", "224.40", **mon)], E("matched", [], OK, "boundary")),
    it(5, "IT-PRICE", "lumen-json-v1", "Invoice below PO price; policy checks over-billing only.", LUM,
       [L(1, "10", "1450.00", **lap)], [[R(1, "10")]], [IL(1, 1, "10", "1400.00", **lap)],
       E("matched", [], OK, "over_only")),
    it(6, "IT-MISSING", "lumen-json-v1", "No goods-received note yet.", LUM, [L(1, "10", "1450.00", **lap)], [],
       [IL(1, 1, "10", "1450.00", **lap)], E("needs_information", ["missing_receipt"], INFO, "")),
    it(7, "IT-CONFLICT", "lumen-json-v1", "Vendor says 10 delivered, GRN shows 8; no precedence defined.", LUM,
       [L(1, "10", "1450.00", **lap)], [[R(1, "8")]], [IL(1, 1, "10", "1450.00", **lap)],
       E("manual_review", ["quantity_mismatch"], ESC, "undefined precedence escalates"),
       messages=["Dear customer,\nRegarding PO-2007: all 10 laptops were delivered to your intake desk.\nVendor support"]),
    it(8, "IT-UNITS", "para-json-v1", "Keyboards invoiced in packs of 5.", PAR,
       [L(1, "20", "30.00", item="KB-030", desc="Keyboard")], [[R(1, "20")]],
       [IL(1, 1, "4", "150.00", item="KB-030", desc="Keyboard", unit="pack of 5")], E("matched", [], OK, "")),
    it(9, "IT-QTY", "para-json-v1", "USD order fully matched.", PAR,
       [L(1, "5", "800.00", item="DS-800", desc="Docking station")], [[R(1, "5")]],
       [IL(1, 1, "5", "800.00", item="DS-800", desc="Docking station")], E("matched", [], OK, ""), currency="USD"),
    it(10, "IT-QTY", "lumen-json-v1", "10 received, 2 returned via RMA, 10 invoiced.", LUM,
       [L(1, "10", "1450.00", **lap)], [[R(1, "10")], [R(1, "2", kind="return")]], [IL(1, 1, "10", "1450.00", **lap)],
       E("discrepancies_found", ["quantity_mismatch"], QTY, "net 8")),
    it(11, "IT-LINES", "para-json-v1", "Invoice lines reference part numbers only.", PAR,
       [L(1, "25", "220.00", **mon), L(2, "20", "30.00", item="KB-030", desc="Keyboard")], [[R(1, "25"), R(2, "20")]],
       [IL(1, None, "25", "220.00", **mon), IL(2, None, "20", "30.00", item="KB-030", desc="Keyboard")],
       E("matched", [], OK, "exact item-code line match is disclosed")),
    it(12, "IT-REVIEW", "lumen-json-v1", "Promotional zero-price dock billed at 15.00.", LUM,
       [L(1, "10", "1450.00", **lap), L(2, "10", "0.00", item="DK-PROMO", desc="Promo dock")],
       [[R(1, "10"), R(2, "10")]],
       [IL(1, 1, "10", "1450.00", **lap), IL(2, 2, "10", "15.00", item="DK-PROMO", desc="Promo dock")],
       E("manual_review", [], ESC, "percent tolerance undefined at zero price")),
]

if __name__ == "__main__":
    out = Path(__file__).with_name("dev.yaml")
    header = ("# GENERATED from author_dev.py (hand-written table). Synthetic data only.\n"
              "# Expected labels are written by hand from the policy files, not produced by OpsBridge.\n"
              "# Families group near-duplicate templates; splits are assigned by family.\n")
    doc = {"schema_version": 1, "corpus_id": "opsbridge-dev-seed-v1", "scenarios": S}
    out.write_text(header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    print(f"{len(S)} scenarios → {out}")
