import csv
from dataclasses import dataclass
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "catalog.csv"


@dataclass(frozen=True)
class CatalogItem:
    sku: str
    name: str
    tags: tuple[str, ...]
    pack_size: int
    price_cents: int
    recycled: bool
    supplier: str


def load_catalog(path: Path = CATALOG_PATH) -> list[CatalogItem]:
    items: list[CatalogItem] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            items.append(
                CatalogItem(
                    sku=row["sku"].strip(),
                    name=row["name"].strip(),
                    tags=tuple(tag.strip() for tag in row["tags"].split("|") if tag.strip()),
                    pack_size=int(row["pack_size"]),
                    price_cents=int(row["price_cents"]),
                    recycled=row["recycled"].strip() == "1",
                    supplier=row["supplier"].strip(),
                )
            )
    return items
