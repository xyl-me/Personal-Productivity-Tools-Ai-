"""导出层单元测试: xlsx 结构、模板渲染、数据摊平。"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from app.executor.models import Expectation, TestCase
from app.exporter.pytest_script import case_payload, export_pytest, render_pytest
from app.exporter.xlsx import Sheet, column_letter, write_workbook


def _case(case_id: str = "C1") -> TestCase:
    return TestCase(
        id=case_id,
        name="创建订单-金额为负",
        endpoint="POST /api/v1/orders",
        method="POST",
        path="/api/v1/orders",
        body={"item_id": "SKU-1", "amount": -1},
        expect=Expectation(status=400),
        description="边界值",
    )


@pytest.mark.parametrize(
    ("index", "letter"), [(1, "A"), (26, "Z"), (27, "AA"), (52, "AZ"), (53, "BA"), (702, "ZZ")]
)
def test_column_letter(index: int, letter: str) -> None:
    assert column_letter(index) == letter


def test_sheet_name_is_sanitized() -> None:
    sheet = Sheet(name="a/b:c*?" + "x" * 40, headers=["h"])
    assert len(sheet.name) <= 31
    assert not any(char in sheet.name for char in '[]:*?/\\')


def test_workbook_contains_required_parts(tmp_dir: Path) -> None:
    path = write_workbook(
        tmp_dir / "book.xlsx",
        [Sheet(name="汇总", headers=["项目", "值"], rows=[["用例总数", 18]])],
    )
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    assert {
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/styles.xml",
        "xl/worksheets/sheet1.xml",
    } <= names


def test_cells_are_typed_and_escaped(tmp_dir: Path) -> None:
    path = write_workbook(
        tmp_dir / "book.xlsx",
        [Sheet(name="s", headers=["文本", "数字"], rows=[["a<b>&c", 42], [None, 3.5]])],
    )
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")

    assert "a&lt;b&gt;&amp;c" in xml
    assert '<c r="B2"><v>42</v></c>' in xml
    assert '<c r="B3"><v>3.5</v></c>' in xml
    assert 's="1"' in xml


def test_styles_and_sheet_names_are_wired(tmp_dir: Path) -> None:
    path = write_workbook(
        tmp_dir / "book.xlsx",
        [Sheet(name="甲", headers=["a"]), Sheet(name="乙", headers=["b"])],
    )
    with zipfile.ZipFile(path) as archive:
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
        rels = archive.read("xl/_rels/workbook.xml.rels").decode("utf-8")

    assert "甲" in workbook and "乙" in workbook
    assert 'r:id="rId2"' in workbook
    assert 'Target="worksheets/sheet2.xml"' in rels
    assert 'Target="styles.xml"' in rels


def test_row_style_marks_rows(tmp_dir: Path) -> None:
    path = write_workbook(
        tmp_dir / "book.xlsx",
        [Sheet(name="s", headers=["a"], rows=[["x"], ["y"]], row_styles=[0, 2])],
    )
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert '<c r="A3" s="2"' in xml


def test_sheet_xml_is_well_formed(tmp_dir: Path) -> None:
    path = write_workbook(
        tmp_dir / "book.xlsx", [Sheet(name="s", headers=["a", "b"], rows=[["1", "2"]])]
    )
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                ElementTree.fromstring(archive.read(name))


def test_empty_workbook_is_rejected(tmp_dir: Path) -> None:
    with pytest.raises(ValueError):
        write_workbook(tmp_dir / "empty.xlsx", [])


def test_case_payload_contains_execution_fields() -> None:
    payload = case_payload(_case("C9"))
    assert payload["id"] == "C9"
    assert payload["method"] == "POST"
    assert payload["expected"] == {
        "status": [400],
        "json_contains": None,
        "json_length": None,
        "body_contains": [],
    }
    assert payload["body"] == {"item_id": "SKU-1", "amount": -1}


def test_rendered_script_is_python_and_self_contained() -> None:
    code = render_pytest([_case("C1"), _case("C2")], base_url_hint="http://example.test")
    compile(code, "<generated>", "exec")

    assert 'BASE_URL = os.environ.get("BASE_URL", "http://example.test")' in code
    assert "'C1'" in code and "'C2'" in code
    assert "import urllib.request" in code
    assert "import requests" not in code
    assert "from app." not in code
    assert "None" in code or "null" not in code
    assert " null" not in code


def test_rendered_script_keeps_case_count_in_docstring() -> None:
    code = render_pytest([_case("C1"), _case("C2"), _case("C3")])
    assert "3 条" in code


def test_render_rejects_empty_case_list() -> None:
    with pytest.raises(ValueError):
        render_pytest([])


def test_export_writes_file(tmp_dir: Path) -> None:
    result = export_pytest(tmp_dir / "test_demo.py", [_case("C1")])
    assert result.path.exists()
    assert result.case_count == 1
    assert "pytest test_demo.py" in result.render_command("http://x")
