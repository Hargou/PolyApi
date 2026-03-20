"""
Preprocess raw JSONL data into optimized formats for fast replay.

Two-pass approach:
  Pass 1: Scan for market_info events → collect known token_ids
  Pass 2: Filter out irrelevant CLOB events, write Parquet + filtered JSONL

Raw 6GB JSONL (18M events) → ~200-500MB Parquet (only relevant events)
Replay load time: 30 min → 30 seconds.

Usage:
    python -m collector.preprocess data_store/
    python -m collector.preprocess data_store/2026-03-14.jsonl
    python -m collector.preprocess data_store/ --format both  # parquet + jsonl
"""

import argparse
import logging
import time
from pathlib import Path

try:
    import orjson
    def json_loads(s):
        return orjson.loads(s)
    def json_dumps(obj):
        return orjson.dumps(obj)
except ImportError:
    import json
    def json_loads(s):
        return json.loads(s)
    def json_dumps(obj):
        return json.dumps(obj).encode()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def scan_token_ids(paths):
    """Pass 1: Collect all yes_token_ids from market_info events."""
    token_ids = set()
    for path in paths:
        log.info("Pass 1 scanning: %s", path)
        with open(path, "rb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json_loads(line)
                except Exception:
                    continue
                if raw.get("type") == "market_info":
                    d = raw.get("data", {})
                    tid = d.get("yes_token_id", "")
                    if tid:
                        token_ids.add(tid)
    log.info("Pass 1 complete: found %d token_ids from market_info events", len(token_ids))
    return token_ids


def preprocess_to_parquet(paths, token_ids, output_path):
    """Pass 2: Filter events and write to Parquet."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    # Accumulate rows in batches for memory efficiency
    BATCH_SIZE = 500_000
    batch = []
    total_in = 0
    total_out = 0
    writer = None

    schema = pa.schema([
        ("ts", pa.int64()),
        ("type", pa.string()),
        # spot / chainlink
        ("sym", pa.string()),
        ("price", pa.float64()),
        # clob
        ("asset_id", pa.string()),
        ("condition_id", pa.string()),
        ("event_type", pa.string()),
        ("best_bid", pa.float64()),
        ("best_ask", pa.float64()),
        ("last_trade_price", pa.float64()),
        ("bids_json", pa.string()),  # JSON array of [price, size] pairs
        ("asks_json", pa.string()),
        # market_info
        ("mi_condition_id", pa.string()),
        ("mi_yes_token_id", pa.string()),
        ("mi_asset", pa.string()),
        ("mi_slug", pa.string()),
        ("mi_window_start_ts", pa.int64()),
        ("mi_window_end_ts", pa.int64()),
        ("mi_question", pa.string()),
        ("mi_volume", pa.float64()),
        ("mi_liquidity", pa.float64()),
        # resolution
        ("res_condition_id", pa.string()),
        ("res_outcome", pa.string()),
    ])

    def make_row(raw, etype):
        ts = raw.get("t", 0)
        row = {col: None for col in schema.names}
        row["ts"] = ts
        row["type"] = etype

        if etype in ("spot", "chainlink"):
            row["sym"] = raw.get("sym", "")
            row["price"] = raw.get("price", 0.0)

        elif etype == "market_info":
            d = raw.get("data", {})
            row["mi_condition_id"] = d.get("condition_id", "")
            row["mi_yes_token_id"] = d.get("yes_token_id", "")
            row["mi_asset"] = d.get("asset", "")
            row["mi_slug"] = d.get("slug", "")
            row["mi_window_start_ts"] = d.get("window_start_ts", 0)
            row["mi_window_end_ts"] = d.get("window_end_ts", 0)
            row["mi_question"] = d.get("question", "")
            row["mi_volume"] = d.get("volume", 0.0)
            row["mi_liquidity"] = d.get("liquidity", 0.0)

        elif etype == "resolution":
            row["res_condition_id"] = raw.get("condition_id", "")
            row["res_outcome"] = raw.get("outcome", "")

        elif etype == "clob":
            data = raw.get("data", {})
            items = data if isinstance(data, list) else [data]
            # For parquet, we flatten each CLOB sub-event into its own row
            rows = []
            for item in items:
                r = dict(row)
                et = item.get("event_type", "")
                aid = item.get("asset_id", "")
                r["event_type"] = et
                r["asset_id"] = aid

                if et == "book":
                    bids = item.get("bids", [])
                    asks = item.get("asks", [])
                    r["bids_json"] = json_dumps(bids).decode() if bids else "[]"
                    r["asks_json"] = json_dumps(asks).decode() if asks else "[]"
                    r["best_bid"] = float(bids[0]["price"]) if bids and isinstance(bids[0], dict) else (float(bids[0][0]) if bids and isinstance(bids[0], (list, tuple)) else 0.0)
                    r["best_ask"] = float(asks[0]["price"]) if asks and isinstance(asks[0], dict) else (float(asks[0][0]) if asks and isinstance(asks[0], (list, tuple)) else 1.0)
                    ltp = item.get("last_trade_price")
                    r["last_trade_price"] = float(ltp) if ltp else None

                elif et == "best_bid_ask":
                    bid = item.get("best_bid")
                    ask = item.get("best_ask")
                    r["best_bid"] = float(bid) if bid else None
                    r["best_ask"] = float(ask) if ask else None

                elif et == "last_trade_price":
                    p = item.get("price")
                    r["last_trade_price"] = float(p) if p else None

                elif et == "price_change":
                    for ch in item.get("price_changes", []):
                        cr = dict(r)
                        cr["asset_id"] = ch.get("asset_id", "")
                        bid = ch.get("best_bid")
                        ask = ch.get("best_ask")
                        p = ch.get("price")
                        cr["best_bid"] = float(bid) if bid else None
                        cr["best_ask"] = float(ask) if ask else None
                        cr["last_trade_price"] = float(p) if p else None
                        rows.append(cr)
                    continue  # already appended

                rows.append(r)
            return rows

        return [row]

    def _coerce(val, pa_type):
        """Coerce value to the expected type."""
        if val is None:
            return None
        if pa_type == pa.float64():
            try:
                return float(val)
            except (ValueError, TypeError):
                return None
        if pa_type == pa.int64():
            try:
                return int(val)
            except (ValueError, TypeError):
                return None
        return val

    def flush_batch(batch, writer):
        if not batch:
            return writer
        arrays = []
        for field in schema:
            col_data = [_coerce(row.get(field.name), field.type) for row in batch]
            arrays.append(pa.array(col_data, type=field.type))
        table = pa.table(dict(zip(schema.names, arrays)), schema=schema)
        if writer is None:
            writer = pq.ParquetWriter(str(output_path), schema, compression="snappy")
        writer.write_table(table)
        return writer

    for path in sorted(paths):
        log.info("Pass 2 processing: %s", path)
        with open(path, "rb") as f:
            for line in f:
                total_in += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json_loads(line)
                except Exception:
                    continue

                etype = raw.get("type", "")

                # Filter: keep spot, chainlink, market_info, resolution always
                # Keep CLOB only if asset_id matches a known token_id
                if etype == "clob":
                    data = raw.get("data", {})
                    items = data if isinstance(data, list) else [data]
                    has_relevant = False
                    for item in items:
                        et = item.get("event_type", "")
                        if et == "price_change":
                            for ch in item.get("price_changes", []):
                                if ch.get("asset_id", "") in token_ids:
                                    has_relevant = True
                                    break
                        else:
                            if item.get("asset_id", "") in token_ids:
                                has_relevant = True
                        if has_relevant:
                            break
                    if not has_relevant:
                        continue

                rows = make_row(raw, etype)
                for r in rows:
                    # Second filter: for CLOB rows, check asset_id
                    if etype == "clob" and r.get("asset_id") and r["asset_id"] not in token_ids:
                        continue
                    batch.append(r)
                    total_out += 1

                if len(batch) >= BATCH_SIZE:
                    writer = flush_batch(batch, writer)
                    batch = []
                    log.info("  %d/%d events processed, %d kept (%.1f%%)",
                             total_in, total_in, total_out, total_out / max(total_in, 1) * 100)

    writer = flush_batch(batch, writer)
    if writer:
        writer.close()

    log.info("Parquet complete: %d → %d events (%.1f%% reduction)",
             total_in, total_out, (1 - total_out / max(total_in, 1)) * 100)
    return total_in, total_out


def preprocess_to_jsonl(paths, token_ids, output_path):
    """Pass 2 alt: Filter and write compressed JSONL."""
    total_in = 0
    total_out = 0

    with open(output_path, "wb") as out:
        for path in sorted(paths):
            log.info("Pass 2 (JSONL) processing: %s", path)
            with open(path, "rb") as f:
                for line in f:
                    total_in += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json_loads(line)
                    except Exception:
                        continue

                    etype = raw.get("type", "")

                    if etype == "clob":
                        data = raw.get("data", {})
                        items = data if isinstance(data, list) else [data]
                        has_relevant = False
                        for item in items:
                            et = item.get("event_type", "")
                            if et == "price_change":
                                for ch in item.get("price_changes", []):
                                    if ch.get("asset_id", "") in token_ids:
                                        has_relevant = True
                                        break
                            else:
                                if item.get("asset_id", "") in token_ids:
                                    has_relevant = True
                            if has_relevant:
                                break
                        if not has_relevant:
                            continue

                    out.write(line + b"\n")
                    total_out += 1

                    if total_in % 2_000_000 == 0:
                        log.info("  %d lines processed, %d kept (%.1f%%)",
                                 total_in, total_out, total_out / max(total_in, 1) * 100)

    log.info("Filtered JSONL complete: %d → %d events (%.1f%% reduction)",
             total_in, total_out, (1 - total_out / max(total_in, 1)) * 100)
    return total_in, total_out


def main():
    parser = argparse.ArgumentParser(description="Preprocess JSONL data for fast replay")
    parser.add_argument("path", help="JSONL file or directory")
    parser.add_argument("--format", choices=["parquet", "jsonl", "both"], default="both",
                        help="Output format (default: both)")
    parser.add_argument("--output", "-o", help="Output directory (default: same as input)")
    args = parser.parse_args()

    p = Path(args.path)
    if p.is_dir():
        files = sorted(p.glob("*.jsonl"))
        # Exclude any previously filtered files
        files = [f for f in files if not f.stem.endswith("_filtered")]
        out_dir = Path(args.output) if args.output else p
    elif p.is_file():
        files = [p]
        out_dir = Path(args.output) if args.output else p.parent
    else:
        print(f"Not found: {args.path}")
        return

    if not files:
        print("No JSONL files found")
        return

    print(f"Preprocessing {len(files)} file(s)...")
    t0 = time.time()

    # Pass 1: collect token IDs
    token_ids = scan_token_ids(files)

    # Pass 2: filter and write
    if args.format in ("parquet", "both"):
        parquet_path = out_dir / "replay_data.parquet"
        preprocess_to_parquet(files, token_ids, parquet_path)
        size_mb = parquet_path.stat().st_size / 1024 / 1024
        print(f"Parquet: {parquet_path} ({size_mb:.1f} MB)")

    if args.format in ("jsonl", "both"):
        jsonl_path = out_dir / "replay_filtered.jsonl"
        preprocess_to_jsonl(files, token_ids, jsonl_path)
        size_mb = jsonl_path.stat().st_size / 1024 / 1024
        print(f"Filtered JSONL: {jsonl_path} ({size_mb:.1f} MB)")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")
    print(f"\nReplay with:")
    print(f"  python -m collector.replay {out_dir / 'replay_data.parquet'} --all")
    print(f"  python -m collector.replay {out_dir / 'replay_filtered.jsonl'} --all")


if __name__ == "__main__":
    main()
