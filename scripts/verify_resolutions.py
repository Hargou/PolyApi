"""
Verify that our recorded resolution outcomes match what actually happened.

Cross-checks:
1. For each resolved market, find the spot price at window_start_ts and window_end_ts
2. Compare: did spot go up? Does that match our recorded outcome?
3. Check both Coinbase spot and Chainlink prices (Polymarket resolves via Chainlink)
4. Flag any mismatches — these represent incorrect PnL in our backtests

Also checks:
- Time gap between market discovery (market_info event) and actual window_start_ts
- Whether the spot_at_window_start snapshot is accurate
"""

import sys
import datetime
from collections import defaultdict

import pyarrow.parquet as pq


def ts_to_str(ts_ms):
    return datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "data_store/replay_data.parquet"
    print(f"Loading {path}...")
    table = pq.read_table(path)
    df_len = table.num_rows
    print(f"Loaded {df_len:,} rows")

    # Convert to columnar for fast access
    ts_col = table.column("ts").to_pylist()
    type_col = table.column("type").to_pylist()
    sym_col = table.column("sym").to_pylist()
    price_col = table.column("price").to_pylist()
    mi_cid_col = table.column("mi_condition_id").to_pylist()
    mi_slug_col = table.column("mi_slug").to_pylist()
    mi_asset_col = table.column("mi_asset").to_pylist()
    mi_wstart_col = table.column("mi_window_start_ts").to_pylist()
    mi_wend_col = table.column("mi_window_end_ts").to_pylist()
    res_cid_col = table.column("res_condition_id").to_pylist()
    res_outcome_col = table.column("res_outcome").to_pylist()

    # Pass 1: Collect market info
    markets = {}  # cid -> {asset, slug, window_start_ts, window_end_ts, discovery_ts}
    for i in range(df_len):
        if type_col[i] == "market_info":
            cid = mi_cid_col[i]
            if cid and cid not in markets:
                markets[cid] = {
                    "asset": mi_asset_col[i],
                    "slug": mi_slug_col[i],
                    "window_start_ts": mi_wstart_col[i],  # unix seconds
                    "window_end_ts": mi_wend_col[i],       # unix seconds
                    "discovery_ts_ms": ts_col[i],           # unix ms
                }

    # Pass 2: Collect spot prices (both Coinbase and Chainlink) as time series
    # Key: symbol -> [(ts_ms, price), ...]
    coinbase_spots = defaultdict(list)  # btcusdt -> [(ts, price)]
    chainlink_spots = defaultdict(list)

    for i in range(df_len):
        etype = type_col[i]
        if etype == "spot":
            sym = sym_col[i]
            p = price_col[i]
            if sym and p:
                coinbase_spots[sym].append((ts_col[i], p))
        elif etype == "chainlink":
            sym = sym_col[i]
            p = price_col[i]
            if sym and p:
                chainlink_spots[sym].append((ts_col[i], p))

    # Sort by timestamp
    for sym in coinbase_spots:
        coinbase_spots[sym].sort()
    for sym in chainlink_spots:
        chainlink_spots[sym].sort()

    # Pass 3: Collect resolutions
    resolutions = {}  # cid -> outcome
    resolution_ts = {}  # cid -> ts_ms
    for i in range(df_len):
        if type_col[i] == "resolution":
            cid = res_cid_col[i]
            if cid:
                resolutions[cid] = res_outcome_col[i]
                resolution_ts[cid] = ts_col[i]

    print(f"\nMarkets discovered: {len(markets)}")
    print(f"Markets resolved:   {len(resolutions)}")
    print(f"Coinbase spot symbols: {list(coinbase_spots.keys())}")
    print(f"Chainlink spot symbols: {list(chainlink_spots.keys())}")

    # Helper: find closest spot price at a given timestamp
    def find_price_at(series, target_ts_ms):
        """Binary search for closest price <= target_ts_ms."""
        if not series:
            return None, None
        lo, hi = 0, len(series) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if series[mid][0] <= target_ts_ms:
                lo = mid
            else:
                hi = mid - 1
        ts, price = series[lo]
        if ts > target_ts_ms:
            return None, None
        return ts, price

    # Verify each resolved market
    print(f"\n{'='*120}")
    print(f"RESOLUTION VERIFICATION")
    print(f"{'='*120}")

    mismatches = []
    close_calls = []  # within 5 bps
    no_data = []
    verified = []

    for cid, outcome in sorted(resolutions.items(), key=lambda x: resolution_ts.get(x[0], 0)):
        if cid not in markets:
            continue

        info = markets[cid]
        asset = (info["asset"] or "").upper()
        slug = info["slug"] or ""
        w_start = info["window_start_ts"]  # unix seconds
        w_end = info["window_end_ts"]      # unix seconds
        disc_ts = info["discovery_ts_ms"]

        sym = f"{asset.lower()}usdt"

        # Find Coinbase spot at window start and end
        cb_start_ts, cb_start = find_price_at(coinbase_spots.get(sym, []), w_start * 1000)
        cb_end_ts, cb_end = find_price_at(coinbase_spots.get(sym, []), w_end * 1000)

        # Find Chainlink spot at window start and end
        cl_start_ts, cl_start = find_price_at(chainlink_spots.get(sym, []), w_start * 1000)
        cl_end_ts, cl_end = find_price_at(chainlink_spots.get(sym, []), w_end * 1000)

        # Also find spot at discovery time (what the engine uses as spot_at_window_start)
        _, cb_at_discovery = find_price_at(coinbase_spots.get(sym, []), disc_ts)

        # Calculate expected outcomes
        cb_expected = None
        cl_expected = None
        cb_return_bps = None
        cl_return_bps = None

        if cb_start and cb_end:
            cb_expected = "yes" if cb_end > cb_start else "no"
            cb_return_bps = (cb_end - cb_start) / cb_start * 10000
        if cl_start and cl_end:
            cl_expected = "yes" if cl_end > cl_start else "no"
            cl_return_bps = (cl_end - cl_start) / cl_start * 10000

        # Time gap: how late was the market discovered vs actual window start?
        disc_delay_sec = (disc_ts / 1000) - w_start if disc_ts and w_start else None

        # Spot error: how different is spot at discovery vs spot at true window start?
        spot_snap_error_bps = None
        if cb_at_discovery and cb_start:
            spot_snap_error_bps = (cb_at_discovery - cb_start) / cb_start * 10000

        # Check for mismatch
        is_mismatch_cb = cb_expected is not None and cb_expected != outcome
        is_mismatch_cl = cl_expected is not None and cl_expected != outcome
        is_close = cb_return_bps is not None and abs(cb_return_bps) < 5.0

        entry = {
            "cid": cid[:12],
            "asset": asset,
            "slug": slug[-30:] if slug else "",
            "outcome": outcome,
            "cb_start": cb_start,
            "cb_end": cb_end,
            "cb_expected": cb_expected,
            "cb_return_bps": cb_return_bps,
            "cl_start": cl_start,
            "cl_end": cl_end,
            "cl_expected": cl_expected,
            "cl_return_bps": cl_return_bps,
            "disc_delay_sec": disc_delay_sec,
            "spot_snap_error_bps": spot_snap_error_bps,
            "mismatch_cb": is_mismatch_cb,
            "mismatch_cl": is_mismatch_cl,
        }

        if cb_expected is None and cl_expected is None:
            no_data.append(entry)
        elif is_mismatch_cb or is_mismatch_cl:
            mismatches.append(entry)
        elif is_close:
            close_calls.append(entry)
        else:
            verified.append(entry)

    # Print summary
    total = len(verified) + len(mismatches) + len(close_calls) + len(no_data)
    print(f"\nTotal resolved markets with info: {total}")
    print(f"  Verified (outcome matches spot):  {len(verified)}")
    print(f"  Mismatches (outcome != spot):     {len(mismatches)}")
    print(f"  Close calls (<5 bps move):        {len(close_calls)}")
    print(f"  No spot data:                     {len(no_data)}")

    # Print mismatches in detail
    if mismatches:
        print(f"\n{'='*120}")
        print("MISMATCHES — recorded outcome disagrees with spot price movement")
        print(f"{'='*120}")
        for e in mismatches:
            print(f"\n  {e['asset']} | {e['slug']}")
            print(f"  Recorded outcome: {e['outcome']}")
            print(f"  Coinbase: {e['cb_start']:.2f} -> {e['cb_end']:.2f} ({e['cb_return_bps']:+.1f} bps) -> expected {e['cb_expected']}")
            if e['cl_start'] and e['cl_end']:
                print(f"  Chainlink: {e['cl_start']:.2f} -> {e['cl_end']:.2f} ({e['cl_return_bps']:+.1f} bps) -> expected {e['cl_expected']}")
            print(f"  Mismatch: CB={e['mismatch_cb']}, CL={e['mismatch_cl']}")

    # Print close calls
    if close_calls:
        print(f"\n{'='*120}")
        print(f"CLOSE CALLS — spot moved <5 bps (Coinbase/Chainlink divergence could flip these)")
        print(f"{'='*120}")
        for e in close_calls[:20]:  # limit to 20
            cl_info = ""
            if e['cl_return_bps'] is not None:
                cl_info = f" | CL: {e['cl_return_bps']:+.1f}bps->{e['cl_expected']}"
            print(f"  {e['asset']} | outcome={e['outcome']} | CB: {e['cb_return_bps']:+.1f}bps->{e['cb_expected']}{cl_info} | {e['slug']}")
        if len(close_calls) > 20:
            print(f"  ... and {len(close_calls) - 20} more")

    # Discovery delay analysis
    delays = [e["disc_delay_sec"] for e in verified + mismatches + close_calls if e["disc_delay_sec"] is not None]
    if delays:
        print(f"\n{'='*120}")
        print("MARKET DISCOVERY DELAY (how late market_info arrives vs window_start_ts)")
        print(f"{'='*120}")
        print(f"  Min:    {min(delays):.1f}s")
        print(f"  Median: {sorted(delays)[len(delays)//2]:.1f}s")
        print(f"  Mean:   {sum(delays)/len(delays):.1f}s")
        print(f"  Max:    {max(delays):.1f}s")
        print(f"  >30s:   {sum(1 for d in delays if d > 30)}/{len(delays)} ({sum(1 for d in delays if d > 30)/len(delays)*100:.1f}%)")
        print(f"  >60s:   {sum(1 for d in delays if d > 60)}/{len(delays)} ({sum(1 for d in delays if d > 60)/len(delays)*100:.1f}%)")

    # Spot snapshot error analysis
    snap_errors = [e["spot_snap_error_bps"] for e in verified + mismatches + close_calls if e["spot_snap_error_bps"] is not None]
    if snap_errors:
        print(f"\n{'='*120}")
        print("SPOT SNAPSHOT ERROR (spot at market discovery vs spot at true window start)")
        print(f"{'='*120}")
        print(f"  Min:    {min(snap_errors):+.1f} bps")
        print(f"  Median: {sorted(snap_errors)[len(snap_errors)//2]:+.1f} bps")
        print(f"  Mean:   {sum(snap_errors)/len(snap_errors):+.1f} bps")
        print(f"  Max:    {max(snap_errors):+.1f} bps")
        abs_errors = [abs(e) for e in snap_errors]
        print(f"  Mean |error|: {sum(abs_errors)/len(abs_errors):.1f} bps")
        print(f"  >5 bps:  {sum(1 for e in abs_errors if e > 5)}/{len(abs_errors)}")
        print(f"  >10 bps: {sum(1 for e in abs_errors if e > 10)}/{len(abs_errors)}")
        print(f"  >50 bps: {sum(1 for e in abs_errors if e > 50)}/{len(abs_errors)}")

    # Chainlink vs Coinbase divergence
    both = [(e["cb_return_bps"], e["cl_return_bps"]) for e in verified + mismatches + close_calls
            if e["cb_return_bps"] is not None and e["cl_return_bps"] is not None]
    if both:
        divergences = [abs(cb - cl) for cb, cl in both]
        disagree = sum(1 for cb, cl in both if (cb > 0) != (cl > 0))
        print(f"\n{'='*120}")
        print("CHAINLINK vs COINBASE DIVERGENCE")
        print(f"{'='*120}")
        print(f"  Markets with both feeds: {len(both)}")
        print(f"  Mean divergence:  {sum(divergences)/len(divergences):.1f} bps")
        print(f"  Max divergence:   {max(divergences):.1f} bps")
        print(f"  Disagree on direction: {disagree}/{len(both)} ({disagree/len(both)*100:.1f}%)")

    # Resolution method check
    print(f"\n{'='*120}")
    print("RESOLUTION METHOD CHECK")
    print(f"{'='*120}")
    print(f"  Our recorder uses: CLOB last_trade_price > 0.5 -> YES")
    print(f"  Polymarket uses:   Chainlink Data Streams (NOT Coinbase)")
    print(f"  fetcher.py uses:   spot_end > spot_start -> YES (Coinbase)")
    if mismatches:
        print(f"\n  WARNING: {len(mismatches)} mismatches found — our resolution may not match Polymarket's!")
    else:
        print(f"\n  All {len(verified) + len(close_calls)} verified markets match. Resolution appears accurate.")


if __name__ == "__main__":
    main()
