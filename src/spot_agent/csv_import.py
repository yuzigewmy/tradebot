"""CSV syntax only. Market rules are checked once by build_frame."""

import csv
from io import StringIO


def parse_csv(text):
    reader = csv.reader(StringIO(text.lstrip("\ufeff")), strict=True)
    headers = [v.strip() for v in next(reader, [])]
    columns = ["timestamp", "open", "high", "low", "close", "volume"]
    if len(headers) != 6 or set(headers) != set(columns):
        raise ValueError("CSV表头必须恰好包含 timestamp,open,high,low,close,volume")
    order = [headers.index(name) for name in columns]
    rows = []
    for line, cells in enumerate(reader, 2):
        if not cells:
            continue
        if len(cells) != 6:
            raise ValueError(f"CSV第{line}行必须有6列")
        values = [cells[i].strip() for i in order]
        if not values[0].isascii() or not values[0].isdigit():
            raise ValueError(f"CSV第{line}行时间必须为整数UTC毫秒")
        try:
            rows.append([int(values[0]), *map(float, values[1:])])
        except ValueError as error:
            raise ValueError(f"CSV第{line}行包含非数值字段") from error
    if not 30 <= len(rows) <= 12000:
        raise ValueError("CSV需要30至12000根K线")
    return rows
