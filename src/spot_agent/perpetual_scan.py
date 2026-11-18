"""Feed historical windows into the same closed-bar strategy as the monitor."""

from .contracts import TIMEFRAMES
from .perpetual_market import WINDOW
from .perpetual_strategy import detect_setup, detect_trend_setup


def aggregate_minutes(rows, duration):
    step = TIMEFRAMES["1m"]
    size = duration // step
    for offset in range((-rows[0][0] // step) % size, len(rows) - size + 1, size):
        part = rows[offset:offset + size]
        yield [part[0][0], part[0][1], max(row[2] for row in part), min(row[3] for row in part),
               part[-1][4], sum(row[5] for row in part)]


def candidate_jobs(instrument, rows, start, end, profile="v1"):
    minute = TIMEFRAMES["1m"]
    frames = {"1m": rows, **{tf: list(aggregate_minutes(rows, TIMEFRAMES[tf])) for tf in ("15m", "1h")}}
    step = minute if profile == "v1" else TIMEFRAMES["15m"]
    first_close = (start + step - 1) // step * step
    detect = detect_setup if profile == "v1" else detect_trend_setup
    for closed_at in range(first_close, end, step):
        windows = {}
        for tf, series in frames.items():
            duration = TIMEFRAMES[tf]
            index = (closed_at // duration * duration - series[0][0]) // duration
            windows[tf] = series[index - WINDOW:index]
        analysis = detect(windows)
        if analysis["setup"]["strategy"]:
            trigger = "1m" if profile == "v1" else "15m"
            yield {"instrument": instrument, "closed_at": closed_at, "trigger_candle": windows[trigger][-1], **analysis}
