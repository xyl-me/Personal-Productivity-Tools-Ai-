"""零依赖的 .xlsx 写出。"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"

STYLE_DEFAULT = 0
STYLE_HEADER = 1
STYLE_WARN = 2

_CONTROL = {chr(code): None for code in range(0x20) if code not in (0x09, 0x0A, 0x0D)}


@dataclass
class Sheet:
    name: str
    headers: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    widths: list[int] | None = None
    row_styles: list[int] | None = None

    def __post_init__(self) -> None:
        if len(self.name) > 31:
            self.name = self.name[:31]
        for banned in '[]:*?/\\':
            self.name = self.name.replace(banned, "-")
        if self.widths is None:
            self.widths = [max(10, min(48, len(str(h)) * 3)) for h in self.headers]


def _style_for(sheet: Sheet, row_index: int) -> int:
    if not sheet.row_styles or row_index >= len(sheet.row_styles):
        return STYLE_DEFAULT
    return sheet.row_styles[row_index] or STYLE_DEFAULT


def _escape(text: str) -> str:
    cleaned = "".join(char for char in str(text) if ord(char) >= 0x20 or char in "\t\n\r")
    return (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def column_letter(index: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA。"""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters or "A"


def _cell(reference: str, value: Any, style: int) -> str:
    attrs = f' r="{reference}"' + (f' s="{style}"' if style else "")
    if value is None or value == "":
        return f"<c{attrs}/>" if style else ""
    if isinstance(value, bool):
        return f'<c{attrs} t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c{attrs}><v>{value}</v></c>'
    return f'<c{attrs} t="inlineStr"><is><t xml:space="preserve">{_escape(value)}</t></is></c>'


def _sheet_xml(sheet: Sheet) -> str:
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(sheet.widths or [], start=1)
    )
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<worksheet xmlns="{_NS_MAIN}">',
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>",
        f"<cols>{columns}</cols>" if columns else "",
        "<sheetData>",
    ]

    header_cells = "".join(
        _cell(f"{column_letter(index)}1", header, STYLE_HEADER)
        for index, header in enumerate(sheet.headers, start=1)
    )
    parts.append(f'<row r="1">{header_cells}</row>')

    for row_number, row in enumerate(sheet.rows, start=2):
        row_style = _style_for(sheet, row_number - 2)
        cells = "".join(
            _cell(f"{column_letter(index)}{row_number}", value, row_style)
            for index, value in enumerate(row, start=1)
        )
        parts.append(f'<row r="{row_number}">{cells}</row>')

    parts += ["</sheetData>", "</worksheet>"]
    return "".join(parts)


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<styleSheet xmlns="{_NS_MAIN}">'
        '<fonts count="2">'
        '<font><sz val="11"/><color theme="1"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><color rgb="FF1F3864"/><name val="Calibri"/></font>'
        "</fonts>"
        '<fills count="4">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/><bgColor indexed="64"/>'
        "</patternFill></fill>"
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/><bgColor indexed="64"/>'
        "</patternFill></fill>"
        "</fills>"
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"/>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )


def _content_types(sheet_count: int) -> str:
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{_NS_CT}">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f"{overrides}"
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )


def _workbook_xml(sheets: Sequence[Sheet]) -> str:
    entries = "".join(
        f'<sheet name="{_escape(sheet.name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, sheet in enumerate(sheets, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
        f"<sheets>{entries}</sheets>"
        "</workbook>"
    )


def _workbook_rels(sheet_count: int) -> str:
    rels = "".join(
        f'<Relationship Id="rId{index}" Type="{_NS_REL}/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_NS_PKG_REL}">'
        f"{rels}"
        f'<Relationship Id="rId{sheet_count + 1}" Type="{_NS_REL}/styles" Target="styles.xml"/>'
        "</Relationships>"
    )


def _root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_NS_PKG_REL}">'
        f'<Relationship Id="rId1" Type="{_NS_REL}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )


def write_workbook(path: str | Path, sheets: Sequence[Sheet]) -> Path:
    """把若干张表写成一个 .xlsx 文件。"""
    if not sheets:
        raise ValueError("至少要有一张表")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types(len(sheets)))
        archive.writestr("_rels/.rels", _root_rels())
        archive.writestr("xl/workbook.xml", _workbook_xml(sheets))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
        archive.writestr("xl/styles.xml", _styles_xml())
        for index, sheet in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(sheet))
    return target


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["Sheet", "column_letter", "write_workbook"]
