"""Facebook Group Master import: raw row evidence first, conservative normalization second.

The workbook is read-only source evidence. Every group row keeps its exact tab/row coordinates,
recorded cell values and hyperlink targets. Normalized fields never replace that evidence, and
identity is only derived from a Facebook group path; similar names or member counts never merge.
"""

import argparse
import hashlib
import json
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.file_storage import storage_key, write_private
from app.models import FacebookGroupRecord, SourceMaterial

CATALOG_KIND = "FACEBOOK_GROUP_MASTER"
IMPORTER_VERSION = "group-master-import-v1"
MAX_WORKBOOK_BYTES = 20 * 1024 * 1024
MAX_SHEET_XML_BYTES = 40 * 1024 * 1024
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

GROUP_PATH = re.compile(
    r"^https?://(?:www\.|m\.|web\.)?facebook\.com/groups/(?P<key>[^/?#\s]+)", re.IGNORECASE
)
SHARE_ALIAS = re.compile(
    r"^https?://(?:www\.|m\.|web\.)?facebook\.com/share/g/(?P<key>[^/?#\s]+)", re.IGNORECASE
)

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming",
}  # fmt: skip
STATE_BY_NAME = {name.casefold(): code for code, name in US_STATES.items()}
NATIONAL_VALUES = {"US", "USA"}
CANADIAN_PROVINCES = (
    "ontario", "quebec", "british columbia", "alberta", "manitoba", "saskatchewan",
    "nova scotia", "new brunswick",
)  # fmt: skip
STOPWORDS = {
    "a", "an", "and", "at", "by", "for", "from", "group", "groups", "in", "of", "on", "or",
    "the", "to", "with", "&", "-",
}  # fmt: skip
EVENT_PROMOTION_BLOCK = re.compile(
    r"\b(?:do not|don't|never)\s+(?:post|promote|advertise|share)\b|"
    r"\bno\s+(?:event|promotional|advertising|self[- ]promotion)\s*(?:posts?)?\b",
    re.IGNORECASE,
)
POSTING_RULE = re.compile(r"\b(?:criteria|ban spammers?|posts? need|members only)\b", re.I)
ADMIN_CHANGE_RESTRICTION = re.compile(r"do not make any changes(?:\s*\([^)]*\)?)?", re.I)


class CatalogImportError(ValueError):
    pass


# --- Workbook reading (stdlib only, read-only) ---------------------------------------------


@dataclass
class Cell:
    value: str | None
    cell_type: str


@dataclass
class Sheet:
    name: str
    cells: dict[str, Cell] = field(default_factory=dict)
    hyperlinks: dict[str, str] = field(default_factory=dict)


def _column(ref: str) -> str:
    return re.match(r"[A-Z]+", ref).group(0)


def _row(ref: str) -> int:
    return int(re.search(r"\d+", ref).group(0))


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_SHEET_XML_BYTES:
        raise CatalogImportError("workbook part is too large")
    return archive.read(name)


def read_workbook(data: bytes) -> list[Sheet]:
    """Read cell text/types and hyperlink targets; formulas are read as cached values only."""
    if len(data) > MAX_WORKBOOK_BYTES:
        raise CatalogImportError("workbook is too large")
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile:
        raise CatalogImportError("not an xlsx workbook") from None
    with archive:
        names = set(archive.namelist())
        if "xl/workbook.xml" not in names:
            raise CatalogImportError("not an xlsx workbook")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(_read_member(archive, "xl/sharedStrings.xml"))
            shared = ["".join(item.itertext()) for item in root]
        book = ET.fromstring(_read_member(archive, "xl/workbook.xml"))
        rels = {
            rel.get("Id"): rel.get("Target")
            for rel in ET.fromstring(_read_member(archive, "xl/_rels/workbook.xml.rels"))
        }
        sheets: list[Sheet] = []
        for entry in book.findall("s:sheets/s:sheet", NS):
            target = rels[entry.get(f"{{{REL_NS}}}id")]
            member = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            sheet = Sheet(entry.get("name"))
            root = ET.fromstring(_read_member(archive, member))
            for cell in root.findall(".//s:sheetData/s:row/s:c", NS):
                cell_type = cell.get("t") or "n"
                if cell_type == "inlineStr":
                    value = "".join(t.text or "" for t in cell.findall(".//s:t", NS))
                else:
                    raw = cell.find("s:v", NS)
                    value = raw.text if raw is not None else None
                    if cell_type == "s" and value is not None:
                        value = shared[int(value)]
                if value is not None:
                    sheet.cells[cell.get("r")] = Cell(value, cell_type)
            member_path = PurePosixPath(member)
            rel_name = str(member_path.parent / "_rels" / f"{member_path.name}.rels")
            sheet_rels = {}
            if rel_name in names:
                sheet_rels = {
                    rel.get("Id"): rel.get("Target")
                    for rel in ET.fromstring(_read_member(archive, rel_name))
                    if rel.get("TargetMode") == "External"
                }
            for link in root.findall(".//s:hyperlinks/s:hyperlink", NS):
                target = sheet_rels.get(link.get(f"{{{REL_NS}}}id"))
                if target:
                    sheet.hyperlinks[link.get("ref")] = target
            sheets.append(sheet)
        return sheets


# --- Row selection ---------------------------------------------------------------------------


@dataclass
class TabLayout:
    headers: dict[str, str | None]
    name: str
    link: str
    state: str
    members: str
    first_row: int


def _layout(sheet: Sheet) -> TabLayout | None:
    first = {
        _column(ref): (cell.value or "").strip()
        for ref, cell in sheet.cells.items()
        if _row(ref) == 1
    }
    if first.get("A") == "Group Name" and first.get("C") == "Link":
        by_header = {value: column for column, value in first.items()}
        if not {"State", "# Members"}.issubset(by_header):
            return None
        return TabLayout(
            headers={column: value or None for column, value in first.items()},
            name="A",
            link="C",
            state=by_header["State"],
            members=by_header["# Members"],
            first_row=2,
        )
    # The audited "Available" tab has data in row 1 and no headings; positions are evidence.
    if sheet.name == "Available":
        return TabLayout(headers={}, name="A", link="B", state="C", members="D", first_row=1)
    return None


@dataclass
class SourceRow:
    tab: str
    row: int
    cells: dict[str, dict]
    hyperlinks: dict[str, str]
    layout: TabLayout

    def value(self, column: str) -> str | None:
        cell = self.cells.get(column)
        return cell["value"] if cell else None


def group_rows(sheets: list[Sheet]) -> tuple[list[SourceRow], list[dict]]:
    """A group row has a name and a Facebook link in that tab's link column."""
    rows: list[SourceRow] = []
    skipped: list[dict] = []
    for sheet in sheets:
        layout = _layout(sheet)
        if layout is None:
            skipped.append({"tab": sheet.name, "reason": "UNRECOGNIZED_TAB_LAYOUT"})
            continue
        by_row: dict[int, dict[str, Cell]] = {}
        for ref, cell in sheet.cells.items():
            by_row.setdefault(_row(ref), {})[_column(ref)] = cell
        for number in sorted(by_row):
            if number < layout.first_row:
                continue
            cells = by_row[number]
            name = (cells.get(layout.name).value if cells.get(layout.name) else "") or ""
            link = (cells.get(layout.link).value if cells.get(layout.link) else "") or ""
            if not any((cell.value or "").strip() for cell in cells.values()):
                continue
            if not name.strip() or "facebook.com" not in link.casefold():
                skipped.append({"tab": sheet.name, "row": number, "reason": "NOT_A_GROUP_ROW"})
                continue
            rows.append(
                SourceRow(
                    tab=sheet.name,
                    row=number,
                    cells={
                        column: {
                            "header": layout.headers.get(column),
                            "value": cell.value,
                            "type": cell.cell_type,
                        }
                        for column, cell in sorted(cells.items())
                        if (cell.value or "").strip()
                    },
                    hyperlinks={
                        _column(ref): target
                        for ref, target in sorted(sheet.hyperlinks.items())
                        if _row(ref) == number
                    },
                    layout=layout,
                )
            )
    return rows, skipped


# --- Normalization ---------------------------------------------------------------------------


def classify_url(value: str | None) -> dict:
    raw = value or ""
    trimmed = raw.strip()
    direct = GROUP_PATH.match(trimmed)
    if direct:
        key = direct.group("key")
        return {
            "kind": "GROUP_PATH",
            "group_key": key,
            "canonical_url": f"https://www.facebook.com/groups/{key}",
        }
    alias = SHARE_ALIAS.match(trimmed)
    if alias:
        return {"kind": "UNRESOLVED_SHARE_ALIAS", "alias_key": alias.group("key")}
    return {"kind": "UNRECOGNIZED_URL"}


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _name_states(name: str) -> list[str]:
    remaining = name.casefold()
    found: list[str] = []
    for state_name in sorted(STATE_BY_NAME, key=len, reverse=True):
        pattern = rf"\b{re.escape(state_name)}\b"
        if re.search(pattern, remaining):
            found.append(STATE_BY_NAME[state_name])
            remaining = re.sub(pattern, " ", remaining)
    return sorted(set(found))


def normalize_geography(raw_state: str | None, display_name: str) -> dict:
    raw = raw_state or ""
    tokens = [token.strip().upper() for token in re.split(r"[/,&]", raw) if token.strip()]
    codes = sorted({token for token in tokens if token in US_STATES})
    national = any(token in NATIONAL_VALUES for token in tokens)
    unrecognized = [t for t in tokens if t not in US_STATES and t not in NATIONAL_VALUES]
    name_states = _name_states(display_name)
    province = any(
        re.search(rf"\b{re.escape(p)}\b", display_name.casefold()) for p in CANADIAN_PROVINCES
    )
    if national and not codes:
        scope = "NATIONAL"
    elif codes:
        scope = "STATES"
    else:
        scope = "UNKNOWN"
    status = "RECOGNIZED" if not unrecognized and (codes or national) else "UNRECOGNIZED"
    if codes and unrecognized:
        status = "PARTIAL"
    conflict = bool(
        (name_states and (codes or unrecognized) and not set(name_states) & set(codes))
        or (province and codes == ["CA"])
    )
    if conflict:
        status = "CONFLICTING"
    return {
        "raw": raw,
        "codes": codes,
        "scope": scope,
        "unrecognized": unrecognized,
        "name_states": name_states,
        "status": status,
        "label": (
            "Nationwide"
            if scope == "NATIONAL"
            else ", ".join(US_STATES[code] for code in codes)
            if scope == "STATES"
            else None
        ),
    }


def _member_count(cell: dict | None) -> int | None:
    if not cell:
        return None
    try:
        value = float(str(cell["value"]).replace(",", ""))
    except ValueError:
        return None
    if value < 0 or value != int(value):
        return None
    return int(value)


def tokens(*values: str | None) -> list[str]:
    words: set[str] = set()
    for value in values:
        for word in re.findall(r"[a-z0-9]+", (value or "").casefold()):
            if word not in STOPWORDS and len(word) > 1:
                words.add(word)
    return sorted(words)


def row_fingerprint(row: SourceRow) -> str:
    payload = {
        "tab": row.tab,
        "cells": {column: cell["value"] for column, cell in row.cells.items()},
        "hyperlinks": row.hyperlinks,
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_row(row: SourceRow) -> dict:
    layout = row.layout
    name_raw = row.value(layout.name) or ""
    display_name = _clean(name_raw)
    link_raw = row.value(layout.link)
    link = classify_url(link_raw)
    recorded_urls = [
        {"origin": "LINK_COLUMN", "cell": f"{layout.link}{row.row}", "value": link_raw, **link}
    ]
    flags: list[dict] = []
    for column, target in row.hyperlinks.items():
        if column == layout.link and target.strip() == (link_raw or "").strip():
            continue
        other = classify_url(target)
        recorded_urls.append(
            {
                "origin": "NAME_HYPERLINK" if column == layout.name else "CELL_HYPERLINK",
                "cell": f"{column}{row.row}",
                "value": target,
                **other,
            }
        )
        if other.get("group_key", other.get("alias_key")) != link.get(
            "group_key", link.get("alias_key")
        ):
            flags.append({"code": "HYPERLINK_MISMATCH", "cell": f"{column}{row.row}"})

    geography = normalize_geography(row.value(layout.state), display_name)
    if geography["status"] == "CONFLICTING":
        flags.append({"code": "GEOGRAPHY_CONFLICT", "cell": f"{layout.state}{row.row}"})

    members_cell = row.cells.get(layout.members)
    member_count = _member_count(members_cell)
    if members_cell and member_count is None:
        flags.append({"code": "MEMBER_COUNT_UNPARSEABLE", "cell": f"{layout.members}{row.row}"})

    category = None
    if row.tab == "Team A" and row.cells.get("R"):
        category = _clean(row.cells["R"]["value"])
    audience = {
        "category_clue": category,
        "category_source": f"{row.tab}!R (unnamed column)" if category else None,
    }

    identity_columns = {layout.name, layout.link, layout.state, layout.members}
    if category:
        identity_columns.add("R")
    admin_notes = [
        {
            "cell": f"{column}{row.row}",
            "header": cell["header"],
            "value": _clean(cell["value"]),
        }
        for column, cell in row.cells.items()
        if column not in identity_columns
    ]

    restrictions: list[dict] = []
    change_rule = ADMIN_CHANGE_RESTRICTION.search(name_raw)
    if change_rule:
        restrictions.append(
            {
                "scope": "GROUP_ADMINISTRATION",
                "text": _clean(change_rule.group(0)),
                "cell": f"{layout.name}{row.row}",
                "blocks_event_promotion": False,
            }
        )
    for note in admin_notes:
        if EVENT_PROMOTION_BLOCK.search(note["value"]):
            restrictions.append(
                {
                    "scope": "EVENT_PROMOTION",
                    "text": note["value"][:300],
                    "cell": note["cell"],
                    "blocks_event_promotion": True,
                }
            )
        elif POSTING_RULE.search(note["value"]):
            restrictions.append(
                {
                    "scope": "MEMBERSHIP_OR_POSTING_RULE",
                    "text": note["value"][:300],
                    "cell": note["cell"],
                    "blocks_event_promotion": False,
                }
            )
    retention = {_clean(n["value"]).casefold() for n in admin_notes}
    if "keep" in retention and any("leave" in value for value in retention):
        flags.append({"code": "RETENTION_CONFLICT", "cell": f"{row.tab}!{row.row}"})
    if any(value == "not my group" for value in retention):
        flags.append({"code": "OWNERSHIP_NOTE_NOT_MY_GROUP", "cell": f"{row.tab}!{row.row}"})

    return {
        "display_name": display_name[:300],
        "recorded_urls": recorded_urls,
        "chosen_url": link.get("canonical_url"),
        "group_key": link.get("group_key"),
        "identity_status": link["kind"],
        "geography": geography,
        "member_count": member_count,
        "audience": audience,
        "admin_notes": admin_notes,
        "restrictions": restrictions,
        "quality_flags": flags,
        "search_tokens": tokens(display_name, category, " ".join(geography["codes"])),
    }


BLOCKING_FLAGS = {"HYPERLINK_MISMATCH", "RETENTION_CONFLICT"}


def apply_identity_review(records: list[FacebookGroupRecord], existing: list) -> None:
    """Flag shared URLs and same-name records; never merge them into one identity."""
    population = [*existing, *records]
    by_key: dict[str, list] = {}
    by_name: dict[str, list] = {}
    for record in population:
        if record.group_key:
            by_key.setdefault(record.group_key.casefold(), []).append(record)
        by_name.setdefault(record.display_name.casefold(), []).append(record)
    new_ids = {record.id for record in records}

    for key_records in by_key.values():
        if len(key_records) < 2:
            continue
        names = {r.display_name.casefold() for r in key_records}
        geographies = {tuple(r.geography.get("codes", [])) for r in key_records}
        representative = key_records[0]
        for record in key_records:
            if record.id not in new_ids:
                continue
            if len(names) == 1 and len(geographies) == 1 and record is not representative:
                record.quality_flags = [
                    *record.quality_flags,
                    {"code": "DUPLICATE_SOURCE_ROW", "duplicate_of": str(representative.id)},
                ]
                record.review_status = "EXCLUDED"
            elif len(names) > 1 or len(geographies) > 1:
                record.quality_flags = [
                    *record.quality_flags,
                    {
                        "code": "CONFLICTING_DUPLICATE_URL",
                        "other_records": [str(r.id) for r in key_records if r is not record],
                    },
                ]
                record.review_status = "CONFLICT"

    for name_records in by_name.values():
        keys = {(r.group_key or "").casefold() for r in name_records}
        if len(name_records) < 2 or len(keys) < 2:
            continue
        for record in name_records:
            if record.id in new_ids and record.review_status != "EXCLUDED":
                record.quality_flags = [
                    *record.quality_flags,
                    {
                        "code": "SAME_NAME_DIFFERENT_URL",
                        "other_records": [str(r.id) for r in name_records if r is not record],
                    },
                ]
                record.review_status = "CONFLICT"

    for record in records:
        if any(flag["code"] in BLOCKING_FLAGS for flag in record.quality_flags):
            record.review_status = "CONFLICT"


# --- Import ----------------------------------------------------------------------------------


@dataclass
class ImportResult:
    source_material_id: str
    already_imported: bool
    group_rows: int
    created: int
    retained: int
    superseded: int
    skipped_rows: int

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def import_group_master(
    sessions: sessionmaker,
    private_storage_dir: Path,
    data: bytes,
    original_name: str,
) -> ImportResult:
    """Idempotent import. Same bytes are a no-op; changed bytes retain unchanged rows."""
    digest = hashlib.sha256(data).hexdigest()
    sheets = read_workbook(data)
    rows, skipped = group_rows(sheets)
    if not rows:
        raise CatalogImportError("no Facebook Group rows were found")

    with sessions.begin() as db:
        existing_source = db.scalar(
            select(SourceMaterial).where(
                SourceMaterial.catalog_kind == CATALOG_KIND, SourceMaterial.sha256 == digest
            )
        )
        if existing_source is not None:
            count = len(
                db.scalars(
                    select(FacebookGroupRecord.id).where(
                        FacebookGroupRecord.source_material_id == existing_source.id
                    )
                ).all()
            )
            return ImportResult(str(existing_source.id), True, len(rows), 0, count, 0, len(skipped))

        key = storage_key()
        path = write_private(private_storage_dir, key, data)
        try:
            at = datetime.now(UTC)
            source = SourceMaterial(
                id=uuid.uuid4(),
                event_id=None,
                catalog_kind=CATALOG_KIND,
                kind="FILE",
                original_name=original_name[:240],
                storage_key=key,
                original_text=None,
                sha256=digest,
                byte_count=len(data),
                detected_type=XLSX_TYPE,
                input_method="UPLOAD",
                processing_status="IMPORTED",
                extraction={
                    "importer_version": IMPORTER_VERSION,
                    "supplied_through": "OPERATOR_CATALOG_IMPORT",
                    "group_rows": len(rows),
                    "skipped_rows": skipped[:500],
                    "tabs": [sheet.name for sheet in sheets],
                },
                supplied_by=None,
                created_at=at,
            )
            db.add(source)
            db.flush()
            current = db.scalars(
                select(FacebookGroupRecord).where(FacebookGroupRecord.import_status == "CURRENT")
            ).all()
            current_by_fingerprint: dict[str, FacebookGroupRecord] = {}
            for record in current:
                current_by_fingerprint.setdefault(record.row_fingerprint, record)
            seen: set[uuid.UUID] = set()
            created: list[FacebookGroupRecord] = []
            for row in rows:
                fingerprint = row_fingerprint(row)
                retained = current_by_fingerprint.get(fingerprint)
                if retained is not None and retained.id not in seen:
                    seen.add(retained.id)
                    continue
                normalized = normalize_row(row)
                record = FacebookGroupRecord(
                    id=uuid.uuid4(),
                    source_material_id=source.id,
                    source_tab=row.tab,
                    source_row=row.row,
                    row_fingerprint=fingerprint,
                    raw_record={
                        "tab": row.tab,
                        "row": row.row,
                        "cells": row.cells,
                        "hyperlinks": row.hyperlinks,
                    },
                    review_status="UNREVIEWED",
                    import_status="CURRENT",
                    revision=1,
                    created_at=at,
                    updated_at=at,
                    **normalized,
                )
                created.append(record)
            superseded = [record for record in current if record.id not in seen]
            for record in superseded:
                # Curated review fields stay as they are; the record simply stops being current.
                record.import_status = "SUPERSEDED"
                record.revision += 1
                record.updated_at = at
            retained_records = [record for record in current if record.id in seen]
            apply_identity_review(created, retained_records)
            db.add_all(created)
            db.flush()
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return ImportResult(
            str(source.id),
            False,
            len(rows),
            len(created),
            len(seen),
            len(superseded),
            len(skipped),
        )


def main(argv: list[str] | None = None) -> None:
    from app.config import Settings
    from app.db import Database

    parser = argparse.ArgumentParser(description="Import the local Facebook Group Master copy.")
    parser.add_argument("workbook", type=Path, help="Local .xlsx export (read only)")
    args = parser.parse_args(argv)
    data = args.workbook.read_bytes()
    settings = Settings()
    database = Database(settings)
    try:
        result = import_group_master(
            database.sessions, settings.private_storage_dir, data, args.workbook.name
        )
    finally:
        database.engine.dispose()
    print(json.dumps(result.as_dict(), indent=2))


if __name__ == "__main__":
    main()
