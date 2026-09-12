---
name: xlsx
description: Read, create, or edit Excel spreadsheets. Trigger on .xlsx/.xlsm file paths, the words "spreadsheet", "workbook", "worksheet", "pivot table", "cell formula", or a request to produce tabular/financial output as an Excel file.
---

# Working with spreadsheets

Use **openpyxl**. It is already installed — do not try to `pip install`
anything; the sandbox has no network access and will fail if you do.

## Before writing anything: inspect first

Never open a large or unfamiliar workbook with `read` — a spreadsheet's XML
is not meant for a human (or a model) to read raw, and a big one will blow
your tool-result budget. Instead:

```bash
python3 .skills/xlsx/scripts/xlsx_inspect.py path/to/file.xlsx
```

This prints sheet names, dimensions, and a header-row preview per sheet
without loading the whole file into context. Read this before deciding
anything about the workbook's structure.

## Reading

```python
from openpyxl import load_workbook

wb = load_workbook("file.xlsx", data_only=False)  # see the trap below
ws = wb["Sheet1"]  # or wb.active for whichever sheet was open last
for row in ws.iter_rows(min_row=2, values_only=True):  # skip header, values only
    ...
```

**The `data_only` trap.** `data_only=True` returns a formula cell's last
*cached* value — not a recomputed one. openpyxl does not evaluate formulas,
ever. If the file was never opened in a spreadsheet application after the
formula was written, the cached value is `None`, and you will silently get
nothing where you expected a number. Default to `data_only=False` and read
formula cells as formula text; if you need an actual computed number, compute
it yourself in Python from the underlying values.

For a large file you only need to read (not edit), use
`load_workbook(path, read_only=True)` — much lower memory, but the result is
not editable; do not try to save from a `read_only` handle.

## Writing

```python
from openpyxl import Workbook

wb = Workbook()
ws = wb.active
ws.title = "Summary"
ws["A1"] = "Product"          # or ws.cell(row=1, column=1, value="Product")
ws.cell(row=2, column=1, value="Widget")
wb.save("summary.xlsx")
```

Rows and columns are **1-indexed**, not 0-indexed. `ws.cell(row=1, column=1)`
is `A1`.

**Formulas are text, not computation.** Writing `ws["B2"] = "=SUM(B3:B10)"`
stores the formula string; openpyxl does not calculate it, and reading the
cell back immediately (in the same script) gives you the formula text or
`None`, never a computed number. If your output needs to *show* a total,
compute it in Python and write the number — or write the formula for
Excel/LibreOffice to compute on open, but don't try to verify its result by
reading it back with openpyxl.

**Number formats** are set per cell, and are strings copied from Excel's own
format codes:

```python
ws["B2"].number_format = "#,##0.00"    # 1,234.50
ws["C2"].number_format = "0.0%"        # 12.3%
ws["D2"].number_format = "$#,##0.00"   # $1,234.50
ws["E2"].number_format = "yyyy-mm-dd"
```

**Percent format displays a fraction, not the whole number.** A `0.0%` format
multiplies the cell's stored value by 100 for display — store `0.12`, not
`12`, or the sheet will show `1200.0%`. Compute the fraction in Python; let
the format do the scaling.

**Dates must be real date objects, not strings.** Write a Python `date` or
`datetime` — `ws["E2"] = date(2026, 3, 15)` — and openpyxl applies a sensible
date format automatically. Writing `"2026-03-15"` as a plain string looks
identical at a glance but Excel treats it as text: it won't sort
chronologically, can't be used in a date formula, and `SUM`/date arithmetic
over that column silently does nothing. If a source CSV gave you date
strings, parse them (`datetime.strptime` or similar) before writing.

**Styling** is per cell — there is no "style this whole range" shortcut
except looping:

```python
from openpyxl.styles import Font, PatternFill, Alignment

header_font = Font(bold=True, color="FFFFFF")
header_fill = PatternFill("solid", fgColor="1F49C4")
for cell in ws[1]:  # row 1
    cell.font = header_font
    cell.fill = header_fill
    cell.alignment = Alignment(horizontal="center")
```

**Column widths** are never auto-fit — openpyxl cannot measure rendered text
width. Set them explicitly or the output will look cramped. A rough
character-count heuristic is enough for most reports:

```python
for col_cells in ws.columns:
    letter = col_cells[0].column_letter
    longest = max(len(str(c.value)) for c in col_cells if c.value is not None)
    ws.column_dimensions[letter].width = longest + 2
```

**Freeze the header row** on anything more than a few rows long, so it stays
visible while scrolling — cheap, and it's what makes a sheet read as a
finished report rather than a raw data dump:

```python
ws.freeze_panes = "A2"  # freezes row 1; the pane reference is the first
                         # scrollable cell, not the last frozen one
```

**Merged cells**: `ws.merge_cells("A1:C1")` — only the top-left cell (`A1`)
holds a value. Writing to `B1` or `C1` afterward raises `AttributeError`
(they become `MergedCell`, which has no value setter). Unmerge first
(`ws.unmerge_cells("A1:C1")`) if you need to rewrite that range.

**Charts** reference cell ranges, not raw values:

```python
from openpyxl.chart import BarChart, Reference

chart = BarChart()
data = Reference(ws, min_col=2, min_row=1, max_row=ws.max_row)
cats = Reference(ws, min_col=1, min_row=2, max_row=ws.max_row)
chart.add_data(data, titles_from_data=True)
chart.set_categories(cats)
ws.add_chart(chart, "E2")
```

## Verify what you actually wrote

After saving, re-open the file fresh (`load_workbook` again, new handle) and
read back the values that matter before reporting success. In-memory state
from the write step can diverge from what actually landed on disk, especially
if the same task involved several edits — don't trust your own prior
in-process assumptions about the file's contents.

## Reference

`scripts/xlsx_inspect.py` — sheet names, dimensions, header preview, without
loading the workbook whole. Run it, don't read its source unless you're
debugging it.
