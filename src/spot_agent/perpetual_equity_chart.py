"""Render the saved equity CSV as a standalone chart."""

import argparse
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def draw_equity(directory):
    frame = pd.read_csv(directory / "equity.csv")
    frame["local"] = pd.to_datetime(frame["utc"], utc=True).dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    frame["drawdown"] = (frame["equity"] / frame["equity"].cummax() - 1) * 100
    sampled = set()
    for _, hour in frame.groupby(frame["local"].dt.floor("h")):
        sampled.update((hour.index[0], hour.index[-1], hour["equity"].idxmin(), hour["equity"].idxmax(), hour["drawdown"].idxmin()))
    points = frame.loc[sorted(sampled)]
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.68, 0.32], vertical_spacing=.12)
    figure.add_trace(go.Scatter(x=points["local"], y=points["equity"], name="权益", line={"color": "#076f70", "width": 2},
                                hovertemplate="权益 %{y:.3f} USDT<extra></extra>"), row=1, col=1)
    figure.add_hline(y=float(frame["equity"].iloc[0]), line_dash="dot", line_color="#65717c", row=1, col=1)
    figure.add_trace(go.Scatter(x=points["local"], y=points["drawdown"], name="回撤", line={"color": "#aa432c", "width": 1.5},
                                fill="tozeroy", fillcolor="rgba(170,67,44,.13)",
                                hovertemplate="回撤 %{y:.2f}%<extra></extra>"), row=2, col=1)
    figure.update_yaxes(title_text="权益 · USDT", row=1, col=1)
    figure.update_yaxes(title_text="回撤 · %", row=2, col=1)
    figure.update_xaxes(title_text="北京时间", row=2, col=1)
    figure.update_layout(title="OKX 永续模拟账户 · 权益与回撤", template="plotly_white", showlegend=False,
                         hovermode="x unified", height=690, margin={"l": 68, "r": 28, "t": 72, "b": 62})
    figure.write_html(directory / "equity.html", include_plotlyjs=True, config={"responsive": True, "displaylogo": False})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    draw_equity(parser.parse_args().directory)
