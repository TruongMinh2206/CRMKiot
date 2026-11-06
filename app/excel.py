"""Xuất báo cáo ra Excel."""
from io import BytesIO
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

HEAD_FILL = PatternFill("solid", fgColor="1F6F43")
HEAD_FONT = Font(bold=True, color="FFFFFF")
THIN = Side(style="thin", color="DDDDDD")


def to_xlsx(sheets):
    """sheets: list of (title, columns[(key,label,fmt)], rows[dict])"""
    wb = Workbook()
    wb.remove(wb.active)
    for title, cols, rows in sheets:
        ws = wb.create_sheet(title[:31])
        ws.append([c[1] for c in cols])
        for cell in ws[1]:
            cell.fill = HEAD_FILL
            cell.font = HEAD_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for r in rows:
            ws.append([r.get(c[0]) for c in cols])
        for i, c in enumerate(cols, 1):
            fmt = c[2] if len(c) > 2 else None
            width = max(10, min(45, max((len(str(r.get(c[0]) or "")) for r in rows), default=8) + 2, len(c[1]) + 2))
            ws.column_dimensions[get_column_letter(i)].width = width
            if fmt:
                for row in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                    for cell in row:
                        cell.number_format = fmt
        for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=len(cols)):
            for cell in row:
                cell.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


MONEY = "#,##0"
PCT = "0.0"
