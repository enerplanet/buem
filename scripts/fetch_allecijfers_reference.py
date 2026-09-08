"""Fetch a Dutch village's official building and business statistics from
AlleCijfers, as an independent reference for buem's own classification.

Why this exists
---------------
buem derives how many buildings in a region are dwellings and how many are
service buildings from BAG registration data. Nothing in that pipeline
tells us whether the answer is *right*. AlleCijfers republishes CBS and
BAG statistics per woonplaats (locality), including two figures that make
a direct check possible:

``Woningvoorraad``
    Official dwelling stock -- compare against buem's residential
    building/unit counts.
``Niet-woningvoorraad``
    Official non-residential stock -- compare against how many buildings
    buem actually routes to a service-building type. A large shortfall
    means real non-residential buildings are being dropped or
    misclassified.

It also carries business establishments split across the eight aggregated
CBS SBI sectors. That split is the best available evidence for *which*
service-building activities a village actually contains, and so for
whether the BAG ``gebruiksdoel`` -> occupancy service-type mapping in
``nl_building_classifier`` distributes buildings plausibly. The two are
not the same quantity -- an establishment is a business, a building is a
structure, and several businesses can share one building -- so the sector
split is a proportional cross-check, not a target to match one-for-one.

Usage::

    python scripts/fetch_allecijfers_reference.py loenen heeten
    python scripts/fetch_allecijfers_reference.py loenen --output-dir results/

Writes ``<name>_allecijfers.csv`` per locality (one metric per row) plus a
combined ``allecijfers_summary.csv`` comparing the headline stock figures
across every locality fetched.
"""
from __future__ import annotations

import argparse
import csv
import html
import logging
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_URL = "https://allecijfers.nl/woonplaats/{name}/"
USER_AGENT = "buem/1.0 (building-energy model; research use)"

# Table captions whose rows are worth keeping. AlleCijfers renders many
# tables per page; these are the ones carrying building-stock and
# activity-mix evidence rather than demographics or traffic statistics.
WANTED_TABLE_HEADERS = ("Bedrijven", "Woningen", "Energieverbruik", "Huishoudens", "Bevolking")

# Metrics pulled into the cross-locality summary.
SUMMARY_METRICS = (
    "Woningvoorraad",
    "Niet-woningvoorraad",
    "Bedrijfsvestigingen totaal",
    "Inwoners",
    "Huishoudens",
)

# The eight aggregated CBS SBI sectors AlleCijfers reports, mapped to the
# occupancy service-building types whose activity they most plausibly
# occur in. A sector is an economic classification and a service type is
# an occupancy/equipment pattern, so this is a reading aid for comparing
# proportions -- never a classification path. Buildings are typed from
# BAG gebruiksdoel in nl_building_classifier, not from here.
SECTOR_TO_SERVICE_TYPES = {
    "A Landbouw, bosbouw en visserij": ("warehouse",),
    "B-F Nijverheid en energie": ("warehouse",),
    "G+I Handel en horeca": ("supermarket", "restaurant", "hotel", "bakery"),
    "H+J Vervoer, informatie en communicatie": ("warehouse", "office"),
    "K-L Financiele diensten, onroerend goed": ("office",),
    "M-N Zakelijke dienstverlening": ("office",),
    "O-Q Overheid, onderwijs en zorg": ("school", "clinic", "office"),
    "R-U Cultuur, recreatie, overige diensten": ("restaurant",),
}


def _clean(fragment: str) -> str:
    """Strip tags and entities from one table cell."""
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    # AlleCijfers pages are latin-1-ish in places; normalise the stray
    # replacement characters that produces so keys stay comparable.
    return text.replace("\xa0", " ").replace("�", "e").strip()


def parse_number(raw: str) -> float | None:
    """Parse AlleCijfers' Dutch number formatting.

    Thousands are separated with ``.`` and decimals with ``,`` -- the
    opposite of the Python default -- and percentages carry a ``%``
    suffix. Returns ``None`` for a non-numeric cell.
    """
    if not raw:
        return None
    text = raw.replace("%", "").replace("€", "").strip()
    text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def fetch_page(name: str, retries: int = 3) -> str:
    """Download one locality's AlleCijfers page."""
    url = BASE_URL.format(name=name.lower())
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries - 1:
                raise
            logger.warning("fetch failed for %s (%s), retrying", name, type(exc).__name__)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


def extract_metrics(page: str) -> list[dict[str, str]]:
    """Pull every ``metric | value | unit`` row from the wanted tables.

    AlleCijfers renders its statistics as plain three-column tables whose
    first header cell names the topic, so the topic is recoverable
    without depending on surrounding page structure.
    """
    metrics: list[dict[str, str]] = []
    for table in re.findall(r"<table[^>]*>(.*?)</table>", page, re.S):
        rows = [
            [_clean(cell) for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)
        ]
        rows = [row for row in rows if row]
        if not rows:
            continue
        topic = rows[0][0]
        if topic not in WANTED_TABLE_HEADERS:
            continue
        for row in rows[1:]:
            if len(row) < 2 or not row[0]:
                continue
            value = parse_number(row[1])
            metrics.append({
                "topic": topic,
                "metric": row[0],
                "value_raw": row[1],
                "value": "" if value is None else repr(value),
                "unit": row[2] if len(row) > 2 else "",
            })
    return metrics


def service_activity_mix(metrics: list[dict[str, str]]) -> list[dict[str, str]]:
    """Business establishments per CBS sector, with the occupancy service
    types each sector plausibly occupies.

    Percentages are recomputed from the counts rather than read off the
    page's own ``% ...`` rows: those are rounded to two significant
    figures and do not sum to 100.
    """
    counts = {
        m["metric"]: parse_number(m["value_raw"])
        for m in metrics
        if m["topic"] == "Bedrijven" and not m["metric"].startswith("%")
    }
    total = counts.get("Bedrijfsvestigingen totaal") or 0.0
    rows = []
    for sector, service_types in SECTOR_TO_SERVICE_TYPES.items():
        count = counts.get(sector)
        if count is None:
            # AlleCijfers' sector labels carry accented characters that
            # survive encoding inconsistently; match on the SBI prefix.
            prefix = sector.split(" ", 1)[0]
            count = next(
                (v for k, v in counts.items() if k.startswith(prefix + " ")), None
            )
        if count is None:
            continue
        rows.append({
            "sbi_sector": sector,
            "establishments": f"{count:.0f}",
            "share_pct": f"{100 * count / total:.1f}" if total else "",
            "plausible_service_types": "|".join(service_types),
        })
    return rows


def write_locality(name: str, metrics: list[dict[str, str]],
                   mix: list[dict[str, str]], output_dir: Path) -> Path:
    path = output_dir / f"{name.lower()}_allecijfers.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["section", "key", "value", "unit", "note"])
        for m in metrics:
            writer.writerow([m["topic"], m["metric"], m["value_raw"], m["unit"], ""])
        for row in mix:
            writer.writerow([
                "ActivityMix", row["sbi_sector"], row["establishments"],
                f"{row['share_pct']}%", row["plausible_service_types"],
            ])
    return path


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("localities", nargs="+", help="Woonplaats names as they appear in the AlleCijfers URL, e.g. loenen heeten")
    parser.add_argument("--output-dir", default="results", help="Directory for the CSV output (default: results)")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, dict[str, str]] = {}
    for name in args.localities:
        logger.info("fetching %s", name)
        try:
            page = fetch_page(name)
        except Exception as exc:  # noqa: BLE001 - report and continue to the next locality
            print(f"ERROR: could not fetch {name}: {exc}", file=sys.stderr)
            continue

        metrics = extract_metrics(page)
        if not metrics:
            print(f"ERROR: no recognisable statistics tables on {name}'s page", file=sys.stderr)
            continue
        mix = service_activity_mix(metrics)
        path = write_locality(name, metrics, mix, output_dir)
        logger.info("wrote %d metrics and %d sectors to %s", len(metrics), len(mix), path)

        by_metric = {m["metric"]: m["value_raw"] for m in metrics}
        summary[name] = {key: by_metric.get(key, "") for key in SUMMARY_METRICS}

        print(f"\n=== {name.title()} ===")
        for key in SUMMARY_METRICS:
            print(f"  {key:32s} {by_metric.get(key, '-')}")
        print("  business establishments by sector:")
        for row in mix:
            print(f"    {row['sbi_sector']:45s} {row['establishments']:>5s}  ({row['share_pct']:>4s}%)"
                  f"  -> {row['plausible_service_types']}")

    if summary:
        summary_path = output_dir / "allecijfers_summary.csv"
        with summary_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["locality", *SUMMARY_METRICS])
            for name, values in summary.items():
                writer.writerow([name, *(values[k] for k in SUMMARY_METRICS)])
        logger.info("wrote %s", summary_path)

    return 0 if summary else 1


if __name__ == "__main__":
    raise SystemExit(main())
