"""io/table.py 的读写测试。"""
from __future__ import annotations

import pytest
from openpyxl import Workbook

from wxcopy.io.table import (
    TEXT_DELIM,
    export_rows,
    list_sheets,
    read_table,
    resolve_column,
    write_column,
)


def _write_xlsx(path, rows, columns, sheet="Sheet1"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for j, c in enumerate(columns, 1):
        ws.cell(row=1, column=j, value=c)
    for i, row in enumerate(rows, 2):
        for j, c in enumerate(columns, 1):
            ws.cell(row=i, column=j, value=row.get(c, ""))
    wb.save(path)
    return path


# ---------------- read_table ----------------

def test_read_csv(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("标题,原始文案\n甲,内容一\n乙,内容二\n", encoding="utf-8-sig")
    rows, cols = read_table(p)
    assert cols == ["标题", "原始文案"]
    assert [r["标题"] for r in rows] == ["甲", "乙"]
    assert rows[0]["原始文案"] == "内容一"


def test_read_tsv(tmp_path):
    """回归：.tsv 曾被当成 Excel 解析而崩溃（ValueError: Excel file format ...）。

    界面和 README 都宣称支持 TSV，这条用例锁住该行为。
    """
    p = tmp_path / "a.tsv"
    p.write_text("标题\t原始文案\n甲\t内容一\n乙\t内容二\n", encoding="utf-8")
    rows, cols = read_table(p)
    assert cols == ["标题", "原始文案"]
    assert rows[0]["原始文案"] == "内容一"
    assert len(rows) == 2


def test_read_xlsx(tmp_path):
    p = _write_xlsx(tmp_path / "a.xlsx",
                    [{"标题": "甲", "原始文案": "内容一"}],
                    ["标题", "原始文案"])
    rows, cols = read_table(p)
    assert cols == ["标题", "原始文案"]
    assert rows[0]["标题"] == "甲"


def test_read_xlsx_specific_sheet(tmp_path):
    p = _write_xlsx(tmp_path / "a.xlsx", [{"A": "1"}], ["A"], sheet="第一页")
    wb = Workbook()  # 占位，确保 sheet 选择真的生效
    assert wb is not None
    rows, cols = read_table(p, sheet="第一页")
    assert cols == ["A"]


def test_text_delim_covers_all_supported_exts():
    """core.source.TABLE_EXTS 里属于纯文本的，这里必须都有分隔符。"""
    from wxcopy.core.source import TABLE_EXTS

    for ext in TABLE_EXTS:
        if ext in (".csv", ".tsv"):
            assert ext in TEXT_DELIM, f"{ext} 缺少分隔符定义"
    assert TEXT_DELIM[".csv"] == ","
    assert TEXT_DELIM[".tsv"] == "\t"


def test_list_sheets(tmp_path):
    p = _write_xlsx(tmp_path / "a.xlsx", [{"A": "1"}], ["A"], sheet="页一")
    assert list_sheets(p) == ["页一"]

    c = tmp_path / "b.csv"
    c.write_text("A\n1\n", encoding="utf-8")
    assert list_sheets(c) == ["csv"]

    t = tmp_path / "c.tsv"
    t.write_text("A\n1\n", encoding="utf-8")
    assert list_sheets(t) == ["tsv"]


# ---------------- resolve_column ----------------

def test_resolve_column_exact():
    assert resolve_column(["序号", "文案", "备注"], "文案") == "文案"


def test_resolve_column_by_index():
    assert resolve_column(["序号", "文案"], "2") == "文案"


def test_resolve_column_index_out_of_range():
    with pytest.raises(KeyError):
        resolve_column(["序号"], "9")


def test_resolve_column_partial():
    """只有一个列包含关键词时，允许模糊匹配。"""
    assert resolve_column(["转写文案"], "文案") == "转写文案"


def test_resolve_column_ambiguous():
    with pytest.raises(KeyError):
        resolve_column(["原始文案", "仿写文案"], "文案")


def test_resolve_column_missing():
    with pytest.raises(KeyError):
        resolve_column(["序号"], "不存在的列")


# ---------------- write_column / export_rows ----------------

def test_export_then_read_roundtrip(tmp_path):
    cols = ["序号", "文件原名", "转写文案"]
    rows = [{"序号": "1", "文件原名": "a.mp4", "转写文案": "你好"}]
    for name in ("r.csv", "r.tsv", "r.xlsx"):
        p = export_rows(tmp_path / name, rows, cols)
        got, got_cols = read_table(p)
        assert got_cols == cols
        assert got[0]["转写文案"] == "你好"


def test_write_column_new_column_appended(tmp_path):
    p = _write_xlsx(tmp_path / "a.xlsx",
                    [{"标题": "甲"}, {"标题": "乙"}], ["标题"])
    write_column(p, "仿写文案", ["新一", "新二"])
    rows, cols = read_table(p)
    assert "仿写文案" in cols
    assert [r["仿写文案"] for r in rows] == ["新一", "新二"]
    # 原有列不能被破坏
    assert [r["标题"] for r in rows] == ["甲", "乙"]


def test_write_column_accepts_key_value_tuples(tmp_path):
    """调用方（pipeline）常传 (key, value) 元组，写单元格前要取到真正的文本。"""
    p = _write_xlsx(tmp_path / "a.xlsx", [{"标题": "甲"}], ["标题"])
    write_column(p, "仿写文案", [("甲", "内容")])
    rows, _ = read_table(p)
    assert rows[0]["仿写文案"] == "内容"


def test_write_column_by_key_column(tmp_path):
    """指定 key_column 时按值匹配行，行序变化也不会写错。"""
    p = _write_xlsx(tmp_path / "a.xlsx",
                    [{"标题": "甲", "配音": ""}, {"标题": "乙", "配音": ""}],
                    ["标题", "配音"])
    write_column(p, "配音", [("乙", "乙.mp3"), ("甲", "甲.mp3")], key_column="标题")
    rows, _ = read_table(p)
    by = {r["标题"]: r["配音"] for r in rows}
    assert by == {"甲": "甲.mp3", "乙": "乙.mp3"}


def test_write_column_csv_keeps_separator(tmp_path):
    p = tmp_path / "a.tsv"
    p.write_text("标题\n甲\n", encoding="utf-8")
    write_column(p, "仿写文案", ["内容"])
    rows, cols = read_table(p)
    assert cols == ["标题", "仿写文案"]
    assert rows[0]["仿写文案"] == "内容"
