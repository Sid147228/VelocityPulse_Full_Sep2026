from datetime import datetime

import pandas as pd

from jmeter_parser import JMETER_REPORT_GRANULARITY_MS, jmeter_percentile


def build_report_chart_data(df, summary):
    """Build chart payloads using JMeter HTML-dashboard semantics."""
    empty_payload = {
        "rag_counts": {
            "GREEN": sum(1 for row in summary if row.get("RAG") == "GREEN"),
            "AMBER": sum(1 for row in summary if row.get("RAG") == "AMBER"),
            "RED": sum(1 for row in summary if row.get("RAG") == "RED"),
        },
        "chart_time_labels": [],
        "series_avg_by_txn": {},
        "series_p90_by_txn": {},
        "series_response_percentiles_over_time": {},
        "series_error_rate_by_txn": {},
        "series_throughput_over_time": [],
        "series_tps_by_txn": {},
        "labels": [row.get("Transaction") for row in summary],
        "avg_values": [row.get("Avg (s)") for row in summary],
        "p90_values": [row.get("90th % (s)") for row in summary],
        "p95_values": [row.get("95th % (s)") for row in summary],
        "error_values": [row.get("Error %") for row in summary],
    }

    chart_df = df.copy()
    chart_df.columns = [str(col).strip().lower() for col in chart_df.columns]
    required = {"timestamp", "elapsed", "label"}
    if not required.issubset(chart_df.columns):
        return empty_payload

    chart_df["timestamp"] = pd.to_numeric(chart_df["timestamp"], errors="coerce")
    chart_df["elapsed"] = pd.to_numeric(chart_df["elapsed"], errors="coerce")
    chart_df["label"] = chart_df["label"].astype(str).str.strip()
    if "success" in chart_df.columns:
        chart_df["success"] = (
            chart_df["success"].astype(str).str.strip().str.lower().isin(["true", "1"])
        )
    else:
        chart_df["success"] = True

    chart_df = chart_df.dropna(subset=["timestamp", "elapsed", "label"]).copy()
    if chart_df.empty:
        return empty_payload

    # JMeter TimeStampKeysSelector buckets over-time graphs on sample END time.
    chart_df["end_timestamp_ms"] = chart_df["timestamp"] + chart_df["elapsed"]
    granularity_ms = JMETER_REPORT_GRANULARITY_MS
    granularity_seconds = granularity_ms / 1000.0
    chart_df["time_bucket_ms"] = (
        (chart_df["end_timestamp_ms"] // granularity_ms) * granularity_ms
    ).astype("int64")

    time_buckets = sorted(chart_df["time_bucket_ms"].unique().tolist())
    time_format = "%H:%M:%S" if granularity_ms < 60000 else "%H:%M"
    labels_fmt = [
        datetime.fromtimestamp(bucket / 1000.0).strftime(time_format)
        for bucket in time_buckets
    ]

    series_avg_by_txn = {}
    series_p90_by_txn = {}
    series_error_rate_by_txn = {}
    series_tps_by_txn = {}

    for txn, group in chart_df.groupby("label"):
        grouped = group.groupby("time_bucket_ms")
        avg_ms = grouped["elapsed"].mean()
        error_rate = grouped["success"].apply(
            lambda values: 100.0 * ((~values).sum() / len(values))
        )

        successful = group[group["success"]]
        successful_grouped = successful.groupby("time_bucket_ms")["elapsed"]
        p90_by_bucket = {
            bucket: jmeter_percentile(values.tolist(), 0.90)
            for bucket, values in successful_grouped
        }

        series_avg_by_txn[txn] = [
            round(float(avg_ms.get(bucket)), 4)
            if pd.notnull(avg_ms.get(bucket))
            else None
            for bucket in time_buckets
        ]
        # Retain per-transaction P90 as a VelocityPulse supplemental series.
        series_p90_by_txn[txn] = [
            round(float(p90_by_bucket[bucket]), 4)
            if bucket in p90_by_bucket and p90_by_bucket[bucket] is not None
            else None
            for bucket in time_buckets
        ]
        series_error_rate_by_txn[txn] = [
            round(float(error_rate.get(bucket)), 4)
            if pd.notnull(error_rate.get(bucket))
            else None
            for bucket in time_buckets
        ]

        # JMeter TransactionsPerSecondGraphConsumer creates transaction-success
        # and transaction-failure series, each expressed as count per second.
        for success_value, suffix in ((True, "success"), (False, "failure")):
            counts = (
                group[group["success"] == success_value]
                .groupby("time_bucket_ms")
                .size()
            )
            series_tps_by_txn[f"{txn}-{suffix}"] = [
                round(float(counts.get(bucket, 0)) / granularity_seconds, 6)
                for bucket in time_buckets
            ]

    # JMeter ResponseTimePercentilesOverTimeGraphConsumer is aggregate (not
    # transaction-specific) and includes only successful responses.
    successful_df = chart_df[chart_df["success"]]
    successful_by_bucket = {
        bucket: values.tolist()
        for bucket, values in successful_df.groupby("time_bucket_ms")["elapsed"]
    }
    percentile_specs = (
        ("Min", None),
        ("Max", None),
        ("Median", 0.50),
        ("90th percentile", 0.90),
        ("95th percentile", 0.95),
        ("99th percentile", 0.99),
    )
    series_response_percentiles = {}
    for series_name, percentile in percentile_specs:
        points = []
        for bucket in time_buckets:
            values = successful_by_bucket.get(bucket, [])
            if not values:
                points.append(None)
            elif series_name == "Min":
                points.append(round(float(min(values)), 4))
            elif series_name == "Max":
                points.append(round(float(max(values)), 4))
            else:
                value = jmeter_percentile(values, percentile)
                points.append(round(float(value), 4) if value is not None else None)
        series_response_percentiles[series_name] = points

    total_counts = chart_df.groupby("time_bucket_ms").size()
    total_tps = [
        round(float(total_counts.get(bucket, 0)) / granularity_seconds, 6)
        for bucket in time_buckets
    ]

    return {
        **empty_payload,
        "chart_time_labels": labels_fmt,
        "series_avg_by_txn": series_avg_by_txn,
        "series_p90_by_txn": series_p90_by_txn,
        "series_response_percentiles_over_time": series_response_percentiles,
        "series_error_rate_by_txn": series_error_rate_by_txn,
        "series_throughput_over_time": total_tps,
        "series_tps_by_txn": series_tps_by_txn,
    }

