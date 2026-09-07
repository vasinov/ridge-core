"""Summarize the example sales CSV without third-party dependencies."""

import csv
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

totals: dict[str, Decimal] = defaultdict(Decimal)
with Path("sales.csv").open(newline="", encoding="utf-8") as source:
    for row in csv.DictReader(source):
        totals[row["region"]] += Decimal(row["revenue"])

report = "region,revenue\n" + "".join(
    f"{region},{total:.2f}\n" for region, total in sorted(totals.items())
)
Path("report.csv").write_text(report, encoding="utf-8")
print(report, end="")
