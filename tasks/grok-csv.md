You are implementing ONE small module for the project in D:\crypto-spot-agent.
Model requested: grok-4.7, reasoning effort high. No architecture or strategy decisions.

Return ONLY complete Python source code, no Markdown fences, no prose. Do not use tools, read files, run commands or modify files.

Implement parse_csv(text: str) -> list[list[float]] in a standalone module, standard library only.
Contract:
- Exact required headers timestamp,open,high,low,close,volume, any order; reject duplicate headers and extra columns. Strip header/cell whitespace; accept UTF-8 BOM on the first header.
- Max 12000 rows, max text length 2000000; at least 30 rows.
- timestamp is an integer epoch in milliseconds, positive; reject decimal, exponent notation, ISO dates, nan, inf.
- Price fields finite positive floats, volume finite >=0; low <= min(open,close) <= max(open,close) <= high.
- Rows must be strictly increasing timestamp; reject duplicates. No silent sort or filling.
- Reject malformed rows and missing/extra cell counts, nonfinite values and empty fields. Skip only entirely blank lines.
- Return timestamp as int and other values as float in exact OHLCV column order.
- Raise ValueError with a short Chinese error message including row number when relevant. Never include entire source text in errors.
- No file/network IO, no imports besides csv, io, math, re.
- About 70 lines or less; readable code.
