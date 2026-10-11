"""Synthetic Group Master workbook for tests and the browser harness.

Every group, URL and note is invented. The workbook mirrors the layout the importer supports
(three headed tabs plus a header-less "Available" tab) and deliberately includes the data-quality
cases the importer must handle: share-link aliases, conflicting duplicate URLs, same-name groups
with different URLs, name-cell hyperlink disagreement, admin restrictions, event-promotion bans,
contradictory geography, exact duplicate rows, and non-group section/total rows.
"""

import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

HEADERS = ["Group Name", "State", "Link", "# Members", "Second Admin"]


def _sheet_xml(rows: dict[int, dict[str, object]], links: dict[str, str]) -> tuple[str, str]:
    cells = []
    for number in sorted(rows):
        parts = []
        for column, value in sorted(rows[number].items()):
            ref = f"{column}{number}"
            if isinstance(value, (int, float)):
                parts.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                parts.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        cells.append(f'<row r="{number}">{"".join(parts)}</row>')
    link_xml = "".join(
        f'<hyperlink ref="{ref}" r:id="rId{index}"/>'
        for index, ref in enumerate(sorted(links), start=1)
    )
    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheetData>{''.join(cells)}</sheetData>"
        + (f"<hyperlinks>{link_xml}</hyperlinks>" if links else "")
        + "</worksheet>"
    )
    rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{index}" Type="hyperlink" Target="{escape(links[ref])}" '
            'TargetMode="External"/>'
            for index, ref in enumerate(sorted(links), start=1)
        )
        + "</Relationships>"
    )
    return sheet, rels


def workbook(tabs: dict[str, tuple[dict[int, dict[str, object]], dict[str, str]]]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        sheets, rels = [], []
        for index, (name, (rows, links)) in enumerate(tabs.items(), start=1):
            sheets.append(f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>')
            rels.append(
                f'<Relationship Id="rId{index}" Type="worksheet" '
                f'Target="worksheets/sheet{index}.xml"/>'
            )
            sheet_xml, sheet_rels = _sheet_xml(rows, links)
            archive.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml)
            archive.writestr(f"xl/worksheets/_rels/sheet{index}.xml.rels", sheet_rels)
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{''.join(sheets)}</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{''.join(rels)}</Relationships>",
        )
    return buffer.getvalue()


def headed(*rows: list) -> dict[int, dict[str, object]]:
    result = {1: dict(zip("ABCDE", HEADERS, strict=True))}
    for number, values in enumerate(rows, start=2):
        result[number] = {
            column: value for column, value in zip("ABCDE", values, strict=False) if value
        }
    return result


SAMPLE_MASTER_NAME = "sample-group-master.xlsx"
FB = "https://www.facebook.com/groups/"

COLORADO = [
    ("Denver Business Network", "denversmallbiz", 4200),
    ("Aurora Entrepreneurs", "auroraentrepreneurs", 1800),
    ("Boulder Founders Circle", "boulderfounders", 2600),
    ("Fort Collins Business Owners", "fococbiz", 3100),
    ("Lakewood Networking", "lakewoodnet", 950),
    ("Littleton Professionals", "littletonpros", 700),
    ("Southern Colorado Business Community", "socobiz", 1200),
    ("Colorado Women in Business", "cowomenbiz", 640),
    ("Arvada Business Owners", "arvadabiz", 1500),
    ("Northern Colorado Networking", "nocont", 820),
    ("Longmont Entrepreneurs", "longmontentre", 1100),
    ("Castle Rock Professionals Network", "castlerockbiz", 2300),
]


def _team_a() -> tuple[dict, dict]:
    rows = [[name, "CO", FB + key, members] for name, key, members in COLORADO]
    rows += [
        ["Colorado Groups", None, None, None],  # section heading, not a group row
        ["Parker Professionals", "CO", FB + "parkerpros", 900],  # name hyperlink disagrees
        ["Grand Junction Owners DO NOT MAKE ANY CHANGES (admin)", "CO", FB + "gjowners", 400],
        ["Englewood Makers", "CO", FB + "englewoodmakers", 300, "No event posts please"],
        ["Broomfield Business", "CO", "https://www.facebook.com/share/g/sharealias1/", 210],
    ]
    sheet = headed(*rows)
    parker_row = 2 + len(COLORADO) + 1
    return sheet, {f"A{parker_row}": FB + "someothergroup"}


def _team_b() -> tuple[dict, dict]:
    rows = [
        ["Texas Entrepreneurs", "TX", FB + "texasentre", 5000],
        ["Utah Business Owners", "UT", FB + "utahbiz", 2700],
        ["Tea Area Business", "SD", FB + "teaarea", 300],  # same URL, different name/state
        ["Tea Area Networking", "IA", FB + "teaarea", 280],
        ["Omaha Business Network", "NE", FB + "111111", 600],
        ["Phoenix Small Business", "AZ", FB + "phxsmallbiz", 3900],
        ["Total", None, None, 12780],  # totals row, not a group row
    ]
    return headed(*rows), {}


def _team_c() -> tuple[dict, dict]:
    rows = [
        ["Omaha Business Network", "NE", FB + "222222", 640],  # same name, different URL
        ["Texas Business Owners", "CO", FB + "txowners", 800],  # name contradicts state
        ["USA Small Business Owners", "USA", FB + "usasmallbiz", 15000],
        ["Denver Business Network", "CO", FB + "denversmallbiz", 4200],  # exact duplicate
        ["Nashville Networking", "TN", FB + "nashvillenet", 1300],
        ["Remote Founders", "", "https://www.facebook.com/share/g/sharealias2/", 500],
        ["Mountain Makers", "CO", "https://www.facebook.com/share/g/sharealias3/", 450],
    ]
    return headed(*rows), {}


def _available() -> tuple[dict, dict]:
    rows = {
        1: {"A": "Highlands Ranch Business", "B": FB + "hrbiz", "C": "CO", "D": 1600},
        2: {"A": "Thornton Entrepreneurs", "B": FB + "thorntonentre", "C": "CO", "D": 720},
        3: {"A": "Louisville Small Business", "B": FB + "louisvillesb", "C": "CO", "D": 530},
    }
    return rows, {}


def sample_group_master() -> bytes:
    return workbook(
        {
            "Team A": _team_a(),
            "Team B": _team_b(),
            "Team C": _team_c(),
            "Available": _available(),
        }
    )
