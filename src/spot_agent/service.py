from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
import time

from . import __version__
from .contracts import PROJECT_SCHEMA, TIMEFRAMES
from .csv_import import parse_csv
from .market import build_frame, demo_market, fetch_market
from .portfolio import assess_portfolio
from .report import markdown
from .strategy import build_strategy
from .technicals import PARAMETERS, analyze_frame

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "data" / "reports"


def analyze(request):
    frames = demo_market(request.symbol) if request.source == "synthetic" else fetch_market(request.exchange, request.symbol)
    return assemble(frames, request.portfolio)


def import_analysis(request):
    rows = parse_csv(request.csv)
    as_of = min(int(time.time() * 1000), rows[-1][0] + TIMEFRAMES[request.timeframe])
    frame = build_frame(rows, request.exchange, request.symbol, request.timeframe, "import", as_of)
    return assemble({request.timeframe: frame}, request.portfolio)


def assemble(frames, position):
    reference = next(iter(frames.values()))
    analyses = {tf: analyze_frame(frame) for tf, frame in frames.items()}
    strategy = build_strategy(analyses)
    price = analyses["1h"]["price"] if "1h" in analyses else next(iter(analyses.values()))["price"]
    portfolio = None if position is None else assess_portfolio(position, price, strategy["eligible_to_plan_addition"])
    snapshot = {
        "schema": PROJECT_SCHEMA, "code_version": __version__, "rule_version": "ta-spot-v1",
        "source": reference.source, "exchange": reference.exchange, "symbol": reference.symbol,
        "market_type": "spot", "as_of": reference.as_of, "fetched_at": reference.fetched_at,
        "parameters": PARAMETERS, "raw_frames": {tf: asdict(frame) for tf, frame in frames.items()},
        "position_input": None if position is None else position.model_dump(),
        "timeframes": analyses, "consensus": strategy, "portfolio": portfolio,
        "evidence": [e for analysis in analyses.values() for e in analysis["evidence"]],
    }
    content = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False)
    snapshot["snapshot_id"] = hashlib.sha256(content.encode()).hexdigest()[:24]
    snapshot["report"] = markdown(snapshot)
    REPORTS.mkdir(parents=True, exist_ok=True)
    target = REPORTS / f"{snapshot['snapshot_id']}.json"
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=REPORTS, suffix=".tmp", delete=False) as temporary:
        json.dump(snapshot, temporary, ensure_ascii=False, allow_nan=False)
    Path(temporary.name).replace(target)
    return snapshot


def load_report(snapshot_id):
    return json.loads((REPORTS / f"{snapshot_id}.json").read_text(encoding="utf-8"))
