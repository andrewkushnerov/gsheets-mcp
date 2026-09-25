#!/usr/bin/env python
"""A made-up Amazon settlement report, big enough to test paging against.

Amazon's settlement report (Payments -> Reports -> flat file V2) is the biggest
export a seller routinely feeds to Sheets: one line of totals, then one line per
money movement, so a single order is five to ten lines — Principal, Tax, the FBA
fee, the Commission, a promotion... This script writes a report with the same 24
columns and the same row grammar, but every value in it is invented: a fictional
catalogue, random ids, a settlement period of its own. Nothing from a real seller
account goes in, so the file can sit in the repo and in a shared spreadsheet.

    python scripts/generate_amazon_settlement.py                    # the fixture set, see FIXTURES
    python scripts/generate_amazon_settlement.py --rows 20000 --out /tmp/big.csv

The delimiter follows the extension: ``.csv`` gets commas, anything else the tabs of
Amazon's own ``.txt`` download. The fixture sizes are chosen around the server's page
(GSHEETS_MAX_READ_ROWS), which is what a sheet made from them is for testing.

Deterministic: the same --seed always writes the same file, so a test can pin its
expectations (row count, the settlement total, one known order id) to it.
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import re
import string
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(ROOT, "tests", "fixtures")
DEFAULT_SEED = 2026
DEFAULT_START = "2026-08-02"

#: What a bare run writes. 5k is a whisker over the default GSHEETS_MAX_READ_ROWS
#: page, so reading the whole tab has to page exactly once — the boundary worth
#: testing — and still costs nearly 600,000 tokens if something reads it raw.
#: 50k is ten pages: the stress case. Different seeds and weeks, so they are two
#: settlements rather than one and a prefix of it.
FIXTURES = [
    # (file name, movement lines, seed, first day of the period)
    ("amazon_settlement_test_5k.txt", 5000, 5, "2026-08-02"),
    ("amazon_settlement_test_50k.txt", 50000, 50, "2026-08-16"),
]

#: The report's columns, in Amazon's order and spelling.
COLUMNS = [
    "settlement-id", "settlement-start-date", "settlement-end-date", "deposit-date",
    "total-amount", "currency", "transaction-type", "order-id", "merchant-order-id",
    "adjustment-id", "shipment-id", "marketplace-name", "amount-type",
    "amount-description", "amount", "fulfillment-id", "posted-date", "posted-date-time",
    "order-item-code", "merchant-order-item-id", "merchant-adjustment-item-id", "sku",
    "quantity-purchased", "promotion-id",
]
_INDEX = {name.replace("-", "_"): i for i, name in enumerate(COLUMNS)}
AMOUNT = _INDEX["amount"]


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Product:
    sku: str
    price: int        # list price, cents
    fba_fee: int      # FBA per-unit fulfilment fee on an Amazon.com order, cents
    referral_bp: int  # referral fee ("Commission"): basis points of the discounted principal
    weight: int       # how often it sells, relative to the others


#: A fictional houseware brand with kitchen (KT), garden (GD), pet (PT), office (OF)
#: and treats (TR) lines; size, colour or pack count is the SKU suffix, the way sellers
#: usually write them. The last two are the ``XX-XXXX-XXXX`` SKUs Amazon assigns when a
#: listing is created without one. Prices, fees and rates are in the ranges a US
#: catalogue actually has — the treats under $15 pay the 8% grocery rate, everything
#: else 15% — but no line is a real listing.
CATALOG = [
    Product("BW-KT-0101-SS", 1249, 347, 1500, 10),
    Product("BW-KT-0102-BLK", 999, 322, 1500, 7),
    Product("BW-KT-0103-RED", 1849, 415, 1500, 4),
    Product("BW-KT-0104-2PK", 1599, 372, 1500, 6),
    Product("BW-KT-0107-GRY", 2399, 443, 1500, 5),
    Product("BW-KT-0112-L", 3299, 568, 1500, 12),
    Product("BW-KT-0112-XL", 4199, 657, 1500, 5),
    Product("BW-KT-0118-SET", 5899, 761, 1500, 3),
    Product("BW-KT-0120-WHT", 2149, 475, 1500, 4),
    Product("BW-KT-0125-3PK", 1379, 372, 1500, 6),
    Product("BW-KT-0130-SS", 2699, 510, 1500, 2),
    Product("BW-GD-0201-GRN", 2749, 540, 1500, 8),
    Product("BW-GD-0203-2PK", 1999, 443, 1500, 4),
    Product("BW-GD-0206-BRN", 3649, 613, 1500, 3),
    Product("BW-GD-0210-SET", 6499, 868, 1500, 2),
    Product("BW-GD-0212-BLU", 1729, 415, 1500, 3),
    Product("BW-GD-0215-L", 4499, 717, 1500, 2),
    Product("BW-GD-0219-XL", 7999, 933, 1500, 1),
    Product("BW-PT-0301-S", 1149, 322, 1500, 6),
    Product("BW-PT-0301-M", 1449, 347, 1500, 9),
    Product("BW-PT-0301-L", 1899, 415, 1500, 5),
    Product("BW-PT-0305-BLU", 2599, 510, 1500, 3),
    Product("BW-PT-0308-2PK", 899, 322, 1500, 4),
    Product("BW-PT-0310-GRY", 3899, 593, 1500, 2),
    Product("BW-OF-0501-BLK", 799, 322, 1500, 5),
    Product("BW-OF-0502-WHT", 1299, 347, 1500, 4),
    Product("BW-OF-0504-2PK", 1549, 372, 1500, 3),
    Product("BW-OF-0507-NAT", 2949, 540, 1500, 3),
    Product("BW-OF-0509-SET", 4749, 717, 1500, 2),
    Product("BW-OF-0511-GRY", 2249, 475, 1500, 2),
    Product("BW-OF-0515-L", 5499, 761, 1500, 1),
    Product("BW-TR-0401-BAG", 749, 322, 800, 8),
    Product("BW-TR-0402-BAG", 1099, 347, 800, 7),
    Product("BW-TR-0403-3PK", 1449, 372, 800, 5),
    Product("BW-TR-0405-BOX", 2199, 475, 1500, 3),
    Product("BW-TR-0408-6PK", 3399, 593, 1500, 2),
    Product("7H-4NDK-XQ2B", 1699, 415, 1500, 3),
    Product("2R-BM8Y-TL0C", 3149, 568, 1500, 2),
]
CATALOG_WEIGHTS = [product.weight for product in CATALOG]

# ---------------------------------------------------------------------------
# The money movements
# ---------------------------------------------------------------------------

#: Every kind of line the report can carry, as its amount-type / amount-description
#: pair. The names are Amazon's own vocabulary and are kept verbatim, so a test that
#: filters on "Commission" or sums "ItemFees" here would work on a real report
#: unchanged. Signs are the seller's: money in is positive, money out negative.
PRINCIPAL = ("ItemPrice", "Principal")               # what the buyer paid for the item
TAX = ("ItemPrice", "Tax")                           # sales tax the buyer paid...
SHIPPING = ("ItemPrice", "Shipping")                 # shipping the buyer was charged
SHIPPING_TAX = ("ItemPrice", "ShippingTax")
WITHHELD_TAX = ("ItemWithheldTax", "MarketplaceFacilitatorTax-Principal")  # ...and Amazon
WITHHELD_SHIPPING_TAX = ("ItemWithheldTax", "MarketplaceFacilitatorTax-Shipping")  # remits
FBA_FEE = ("ItemFees", "FBAPerUnitFulfillmentFee")   # pick, pack and ship, per unit
COMMISSION = ("ItemFees", "Commission")              # referral fee on the discounted principal
REFUND_COMMISSION = ("ItemFees", "RefundCommission")  # the 20% of it Amazon keeps on a refund
SHIPPING_CHARGEBACK = ("ItemFees", "ShippingChargeback")  # buyer-paid shipping, taken back
PROMO_PRINCIPAL = ("Promotion", "Principal")         # a discount the seller funds
PROMO_SHIPPING = ("Promotion", "Shipping")           # free shipping, funded likewise
PREVIOUS_RESERVE = ("other-transaction", "Previous Reserve Amount Balance")  # released
CURRENT_RESERVE = ("other-transaction", "Current Reserve Amount")  # held back this time
STORAGE_FEE = ("FBA Inventory Fee", "Storage Fee")
ADVERTISING = ("Cost of Advertising", "TransactionTotalAmount")
SUBSCRIPTION = ("ServiceFee", "Subscription Fee")

#: Weighted choices: (value, weight) pairs, drawn with :func:`weighted`.
ITEMS_PER_ORDER = [(1, 80), (2, 15), (3, 5)]
QUANTITY = [(1, 88), (2, 9), (3, 3)]
#: The same SKU sells at slightly different prices across a week — a price edited
#: mid-period, a few cents shaved to win the buy box — so a fixture where every line
#: for a SKU carries the identical price would be tidier than any real report.
PRICE_NUDGES = [(0, 86), (-5, 6), (-50, 3), (-100, 3), (50, 2)]
#: US sales tax in basis points; zero is the untaxed states plus the buyers Amazon did
#: not collect from. Tax is charged on the discounted principal and withheld again by
#: Amazon as marketplace facilitator, so the pair nets to zero for the seller.
TAX_RATES = [
    (0, 45), (400, 5), (600, 8), (625, 6), (700, 8), (725, 5), (800, 6), (825, 6),
    (875, 5), (950, 3), (1025, 3),
]
#: What a buyer gets charged for shipping when they do get charged, cents.
SHIPPING_CHARGES = [60, 99, 100, 149, 299, 399, 549, 699]
#: promotion-id is a free-text label. Amazon's real ones are long ("... Universal
#: Merchant Free Rush Shipping") and occasionally blank; these are made up in that spirit.
PRINCIPAL_PROMOTIONS = [
    # (label, percent off in basis points, weight)
    ("Subscribe and Save Promotion", 1000, 6),
    ("Coupon Redemption Promotion", 1500, 3),
    ("Lightning Deal Promotion", 2000, 1),
]
SHIPPING_PROMOTIONS = [
    ("Free Shipping Promotion", 5),
    ("Prime Free Rush Shipping Promotion", 3),
    ("", 2),
]

ALNUM = string.ascii_letters + string.digits


def weighted(rng: random.Random, table):
    """One value from a list of (value, weight) pairs."""
    return rng.choices([value for value, _ in table], [weight for _, weight in table])[0]


def bp(cents: int, rate_bp: int) -> int:
    """cents x rate, rounded half up to the cent, which is how Amazon's fee lines land."""
    return (cents * rate_bp + 5000) // 10000


def money(cents: int) -> str:
    """Cents -> ``-8.91``: two decimals, no grouping, a bare minus."""
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def cents_of(text: str) -> int:
    """The inverse of :func:`money`, and a check that the column holds nothing else."""
    whole, fraction = text.split(".")
    if len(fraction) != 2:
        raise ValueError(f"not a settlement amount: {text!r}")
    return int(whole + fraction) if not whole.startswith("-") else -int(whole[1:] + fraction)


def amount(kind: tuple[str, str], cents: int, promotion_id: str = "") -> dict:
    return {
        "amount_type": kind[0],
        "amount_description": kind[1],
        "amount": money(cents),
        "promotion_id": promotion_id,
    }


def fmt_date(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d")


def fmt_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class Item:
    """One line item of an order, remembered so a later refund can reverse it exactly."""
    product: Product
    quantity: int
    code: str
    principal: int
    tax: int
    commission: int
    promotion: int
    promotion_id: str
    shipping_paid: int   # buyer-paid shipping that Amazon charged back
    shipping_tax: int


@dataclass
class Order:
    order_id: str
    posted: datetime
    items: list = field(default_factory=list)


class Settlement:
    """One settlement period's worth of rows, drawn from a seeded RNG."""

    def __init__(self, seed: int, start: str):
        self.rng = random.Random(seed)
        rng = self.rng
        first_day = datetime.strptime(start, "%Y-%m-%d")
        # Amazon closes a settlement a little short of the full week, and pays out
        # two days after it closes.
        self.start = first_day + timedelta(seconds=rng.randrange(6 * 3600, 22 * 3600))
        self.end = self.start + timedelta(days=7) - timedelta(minutes=rng.randrange(60, 150))
        self.deposit = self.end + timedelta(days=2)
        self.settlement_id = str(rng.randrange(20_000_000_000, 40_000_000_000))
        self.orders: list[Order] = []      # Amazon.com orders not yet refunded
        self.counts = {"amazon": 0, "external": 0, "refund": 0}
        self.gross = 0                      # principal across Amazon.com orders, cents
        self.used_ids: set[str] = set()

    # -- ids and moments ---------------------------------------------------

    def _unique(self, make) -> str:
        while True:
            candidate = make()
            if candidate not in self.used_ids:
                self.used_ids.add(candidate)
                return candidate

    def _order_id(self, prefix: str) -> str:
        rng = self.rng
        return self._unique(
            lambda: f"{prefix}-{rng.randrange(10**7):07d}-{rng.randrange(10**7):07d}"
        )

    def _alnum(self, length: int) -> str:
        return "".join(self.rng.choices(ALNUM, k=length))

    def _shipment_id(self) -> str:
        return "N" + self._alnum(8)

    def _item_code(self) -> str:
        return self._unique(lambda: str(self.rng.randrange(10**13, 10**14)))

    def _external_id(self) -> str:
        """The id a sample-shipping platform hands over with a multi-channel order."""
        return "ext_sample_" + self._alnum(22)

    def _moment(self) -> datetime:
        span = (self.end - self.start).total_seconds()
        return self.start + timedelta(seconds=self.rng.randrange(60, int(span)))

    def _product(self) -> Product:
        """One product, bestsellers more often than the tail."""
        return self.rng.choices(CATALOG, CATALOG_WEIGHTS)[0]

    def _basket(self) -> list[Product]:
        """Distinct products for one order."""
        wanted = weighted(self.rng, ITEMS_PER_ORDER)
        chosen: dict[str, Product] = {}
        while len(chosen) < wanted:
            product = self._product()
            chosen[product.sku] = product
        return list(chosen.values())

    # -- rows ---------------------------------------------------------------

    def _row(self, *parts: dict) -> list[str]:
        line = [""] * len(COLUMNS)
        line[0] = self.settlement_id
        for part in parts:
            for key, value in part.items():
                line[_INDEX[key]] = value
        return line

    def _stamp(self, transaction_type: str, posted: datetime, **cells) -> dict:
        return {
            "transaction_type": transaction_type,
            "posted_date": fmt_date(posted),
            "posted_date_time": fmt_time(posted),
            **cells,
        }

    def amazon_order(self) -> tuple[datetime, list[list[str]]]:
        """A marketplace order: one block of lines per item, fees and all."""
        rng = self.rng
        posted = self._moment()
        order_id = self._order_id(rng.choice(("111", "112", "113", "114")))
        order = Order(order_id, posted)
        common = self._stamp(
            "Order", posted, order_id=order_id, merchant_order_id=order_id,
            shipment_id=self._shipment_id(), marketplace_name="Amazon.com",
            fulfillment_id="AFN",
        )
        tax_bp = weighted(rng, TAX_RATES)
        rows = []
        for product in self._basket():
            quantity = weighted(rng, QUANTITY)
            principal = (product.price + weighted(rng, PRICE_NUDGES)) * quantity
            promotion, promotion_id = 0, ""
            if rng.random() < 0.2:
                promotion_id, off_bp, _ = rng.choices(
                    PRINCIPAL_PROMOTIONS, [w for _, _, w in PRINCIPAL_PROMOTIONS]
                )[0]
                promotion = bp(principal, off_bp)
            taxable = principal - promotion
            tax = bp(taxable, tax_bp)
            commission = bp(taxable, product.referral_bp)
            # A third of the items carry a shipping charge. Mostly a promotion
            # cancels it (Prime-style free shipping); when the buyer really paid,
            # Amazon takes it back from an FBA seller as a chargeback, and taxes it.
            shipping = rng.choice(SHIPPING_CHARGES) if rng.random() < 0.35 else 0
            buyer_paid = bool(shipping) and rng.random() < 0.3
            shipping_tax = bp(shipping, tax_bp) if buyer_paid else 0

            item = Item(
                product, quantity, self._item_code(), principal, tax, commission,
                promotion, promotion_id, shipping if buyer_paid else 0, shipping_tax,
            )
            order.items.append(item)
            self.gross += principal

            moves = [amount(PRINCIPAL, principal)]
            if tax:
                moves.append(amount(TAX, tax))
            if shipping:
                moves.append(amount(SHIPPING, shipping))
            if shipping_tax:
                moves.append(amount(SHIPPING_TAX, shipping_tax))
            if tax:
                moves.append(amount(WITHHELD_TAX, -tax))
            if shipping_tax:
                moves.append(amount(WITHHELD_SHIPPING_TAX, -shipping_tax))
            moves.append(amount(FBA_FEE, -product.fba_fee * quantity))
            moves.append(amount(COMMISSION, -commission))
            if buyer_paid:
                moves.append(amount(SHIPPING_CHARGEBACK, -shipping))
            if promotion:
                moves.append(amount(PROMO_PRINCIPAL, -promotion, promotion_id))
            if shipping and not buyer_paid:
                moves.append(amount(PROMO_SHIPPING, -shipping, weighted(rng, SHIPPING_PROMOTIONS)))

            cells = {
                "order_item_code": item.code,
                "sku": product.sku,
                "quantity_purchased": str(quantity),
            }
            rows.extend(self._row(common, cells, move) for move in moves)
        self.orders.append(order)
        self.counts["amazon"] += 1
        return posted, rows

    def external_order(self) -> tuple[datetime, list[list[str]]]:
        """A multi-channel fulfilment order: a free sample sent off-Amazon.

        The buyer paid nothing, so the block is a zero Principal plus the (dearer)
        MCF fulfilment fee, and the platform's own ids fill the merchant-* columns.
        """
        posted = self._moment()
        product = self._product()
        self.counts["external"] += 1
        common = self._stamp(
            "Order", posted, order_id=self._order_id("S01"),
            merchant_order_id=self._external_id(), shipment_id=self._shipment_id(),
            marketplace_name="Non-Amazon US", fulfillment_id="AFN",
        )
        cells = {
            "order_item_code": self._item_code(),
            "merchant_order_item_id": self._external_id(),
            "sku": product.sku,
            "quantity_purchased": "1",
        }
        moves = [amount(PRINCIPAL, 0), amount(FBA_FEE, -(product.fba_fee * 16 // 10 + 250))]
        return posted, [self._row(common, cells, move) for move in moves]

    def refund(self) -> tuple[datetime, list[list[str]]]:
        """One item of an earlier order in this file, reversed line by line.

        Requires at least one order in ``self.orders``; the order leaves the pool so
        it is not refunded twice.
        """
        rng = self.rng
        source = self.orders.pop(rng.randrange(len(self.orders)))
        self.counts["refund"] += 1
        item = rng.choice(source.items)
        posted = min(source.posted + timedelta(hours=rng.uniform(3, 120)), self.end)
        common = self._stamp(
            "Refund", posted, order_id=source.order_id, merchant_order_id=source.order_id,
            adjustment_id=self._alnum(9), marketplace_name="Amazon.com", fulfillment_id="AFN",
        )
        cells = {
            "order_item_code": item.code,
            "sku": item.product.sku,
            "quantity_purchased": str(item.quantity),
        }
        moves = [amount(PRINCIPAL, -item.principal)]
        if item.tax:
            moves += [amount(TAX, -item.tax), amount(WITHHELD_TAX, item.tax)]
        if item.shipping_paid:
            moves += [amount(SHIPPING, -item.shipping_paid)]
            if item.shipping_tax:
                moves += [
                    amount(SHIPPING_TAX, -item.shipping_tax),
                    amount(WITHHELD_SHIPPING_TAX, item.shipping_tax),
                ]
            moves += [amount(SHIPPING_CHARGEBACK, item.shipping_paid)]
        # The referral fee comes back, less the refund administration fee: 20% of
        # it, capped at $5.
        moves += [
            amount(COMMISSION, item.commission),
            amount(REFUND_COMMISSION, -min(bp(item.commission, 2000), 500)),
        ]
        if item.promotion:
            moves.append(amount(PROMO_PRINCIPAL, item.promotion, item.promotion_id))
        return posted, [self._row(common, cells, move) for move in moves]

    def other(self, kind, cents: int, posted: datetime, transaction_type="other-transaction",
              marketplace="") -> list[str]:
        """A line with no order behind it: reserves, storage, advertising, the subscription."""
        return self._row(
            self._stamp(transaction_type, posted, marketplace_name=marketplace),
            amount(kind, cents),
        )

    # -- the whole report ---------------------------------------------------

    def build(self, rows: int) -> list[list[str]]:
        """Header, totals line, then about ``rows`` lines of movements."""
        rng = self.rng
        blocks: list[tuple[datetime, list[list[str]]]] = []
        count = 0
        overhead = 7  # the reserve, advertising, storage and subscription lines below
        while count + overhead < rows:
            roll = rng.random()
            if roll < 0.04:
                posted, block = self.external_order()
            elif roll < 0.07 and self.orders:
                posted, block = self.refund()
            else:
                posted, block = self.amazon_order()
            # The real report is roughly, not strictly, chronological: smudge each
            # block's sort key by up to half a day either way.
            blocks.append((posted + timedelta(hours=rng.uniform(-12, 12)), block))
            count += len(block)

        # The lines that are not orders. The reserve released at the top and held at
        # the bottom, and the month's overheads somewhere in between.
        released = self.start + timedelta(seconds=7)
        blocks.append((self.start, [
            self.other(PREVIOUS_RESERVE, rng.randrange(500, 400_000), released),
        ]))
        for _ in range(3):
            moment = self._moment()
            spend = -rng.randrange(2_000, 90_000)
            blocks.append((moment, [
                self.other(ADVERTISING, spend, moment, marketplace="Amazon.com"),
            ]))
        moment = self._moment()
        storage = -rng.randrange(3_000, 25_000)
        blocks.append((moment, [
            self.other(STORAGE_FEE, storage, moment, marketplace="Amazon.com"),
        ]))
        moment = self._moment()
        blocks.append((moment, [
            self.other(SUBSCRIPTION, -3999, moment, "ServiceFee", marketplace="Amazon.com"),
        ]))
        blocks.sort(key=lambda block: block[0])
        lines = [line for _, block in blocks for line in block]
        lines.append(self.other(CURRENT_RESERVE, -bp(self.gross, 1200), self.end))

        total = sum(cents_of(line[AMOUNT]) for line in lines)
        summary = self._row({
            "settlement_start_date": fmt_time(self.start),
            "settlement_end_date": fmt_time(self.end),
            "deposit_date": fmt_time(self.deposit),
            "total_amount": money(total),
            "currency": "USD",
        })
        return [list(COLUMNS), summary, *lines]


def generate(rows: int, seed: int = DEFAULT_SEED, start: str = DEFAULT_START):
    """The report as a list of rows: the header, the totals line, then the movements."""
    return Settlement(seed, start).build(rows)


#: How far the rules in :func:`estimate_tokens` fall short of Claude on this text.
#: Measured on Opus 5.5: a 1,000-row page of the 50k fixture, as the server returns
#: it from the demo sheet, is 115,827 tokens where the rules count 96,030. The digit
#: rule holds (a nine-digit number is three tokens), so the shortfall is spread over
#: the rest, and one factor is as precise as the rules themselves.
CALIBRATION = 1.2


def estimate_tokens(table) -> int:
    """Roughly what Claude pays to read the table as the server renders it (TSV).

    A BPE tokenizer takes up to three digits per token, a common word in one, a
    camel-case compound in one per part, and every dash, dot, colon and tab on its
    own — which on this text comes to under two characters a token, not the four
    that prose gets. Scaled by :data:`CALIBRATION`, it lands within a percent or
    two of the measured count; a new tokenizer would move it again.
    """
    text = "\n".join("\t".join(row) for row in table)
    digits = sum(-(-len(run) // 3) for run in re.findall(r"\d+", text))
    words = sum(1 + (len(word) > 10) for word in re.findall(r"[A-Z]?[a-z]+", text))
    acronyms = sum(-(-len(run) // 3) for run in re.findall(r"[A-Z]{2,}(?![a-z])", text))
    marks = len(re.findall(r"[^\w\s]|[\t\n]", text))
    return round((digits + words + acronyms + marks) * CALIBRATION)


def write(rows: int, seed: int, start: str, path: str) -> None:
    """Generate one report, write it, and say what was written.

    The delimiter follows the name: ``.csv`` gets commas, anything else the tabs of
    Amazon's own ``.txt`` download.
    """
    report = Settlement(seed, start)
    table = report.build(rows)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    delimiter = "," if path.lower().endswith(".csv") else "\t"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, delimiter=delimiter, lineterminator="\n").writerows(table)

    counts = report.counts
    print(path)
    print(f"  {len(table) - 2} movement lines under the header and the totals line, "
          f"{os.path.getsize(path) / 1e6:.1f} MB, seed {seed}")
    print(f"  {counts['amazon'] + counts['external']} orders ({counts['external']} of them "
          f"Non-Amazon), {counts['refund']} refunds; settlement total "
          f"{table[1][_INDEX['total_amount']]} USD")
    print(f"  ~{estimate_tokens(table) / 1e6:.2f}M tokens to read in full as TSV, "
          "about twice that as JSON (rough estimate)")


def write_fixtures(directory: str) -> None:
    """The fixture set, exactly as the repo carries it."""
    for name, rows, seed, start in FIXTURES:
        write(rows, seed, start, os.path.join(directory, name))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rows", type=int,
                        help="movement lines to write, give or take one order; with --out, "
                             "writes that one file instead of the fixture set")
    parser.add_argument("--out", help="where to write it; .csv gets commas, anything else tabs")
    parser.add_argument("--seed", type=int, help=f"same seed, same file (default {DEFAULT_SEED})")
    parser.add_argument("--start",
                        help=f"first day of the period, YYYY-MM-DD (default {DEFAULT_START})")
    args = parser.parse_args(argv)

    if args.rows is None and args.out is None:
        if args.seed is not None or args.start is not None:
            parser.error("--seed and --start describe one file: give --rows and --out too")
        write_fixtures(FIXTURES_DIR)
        return 0
    if args.rows is None or args.out is None:
        parser.error("--rows and --out go together")
    if args.rows < 1:
        parser.error("--rows must be positive")
    seed = DEFAULT_SEED if args.seed is None else args.seed
    write(args.rows, seed, args.start or DEFAULT_START, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
