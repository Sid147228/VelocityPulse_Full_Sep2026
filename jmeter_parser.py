import math

import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import os

JMETER_STATISTIC_WINDOW = max(1, int(os.getenv("JMETER_REPORT_STATISTIC_WINDOW", "20000")))
JMETER_REPORT_GRANULARITY_MS = max(1, int(os.getenv("JMETER_REPORT_GRANULARITY_MS", "60000")))


def jmeter_percentile(values, percentile, window_size=None):
    """Match Apache Commons Math Percentile.EstimationType.LEGACY used by JMeter HTML reports.

    JMeter's report generator uses Apache Commons Math's LEGACY estimator:
      pos = p * (N + 1)
    where p is in the range 0..1. Values below/above the sample range are
    clamped to min/max; otherwise the adjacent sorted values are linearly
    interpolated.
    """
    cleaned = []
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isnan(numeric):
            continue
        cleaned.append(numeric)

    if not cleaned:
        return None

    # JMeter's PercentileAggregator uses DescriptiveStatistics with a sliding
    # statistics window (20,000 samples by default). Preserve insertion order
    # while applying that window, then sort for percentile estimation.
    effective_window = JMETER_STATISTIC_WINDOW if window_size is None else int(window_size)
    if effective_window > 0 and len(cleaned) > effective_window:
        cleaned = cleaned[-effective_window:]

    cleaned.sort()
    n = len(cleaned)
    if n == 1:
        return cleaned[0]

    p = float(percentile)
    if p > 1:
        p = p / 100.0
    if p < 0 or p > 1:
        raise ValueError("percentile must be between 0 and 1 (or 0 and 100)")

    pos = p * (n + 1)
    if pos < 1:
        return cleaned[0]
    if pos >= n:
        return cleaned[-1]

    lower_position = math.floor(pos)
    fraction = pos - lower_position

    # Apache Commons Math describes positions as 1-based.
    lower = cleaned[lower_position - 1]
    upper = cleaned[lower_position]
    return lower + fraction * (upper - lower)


def detect_test_window(file_path):
    """Return the first and last valid JMeter timestamps in epoch milliseconds."""
    df = pd.read_csv(file_path)
    if "timeStamp" not in df.columns:
        raise ValueError("JMeter result file does not contain a timeStamp column")

    timestamps = pd.to_numeric(df["timeStamp"], errors="coerce").dropna()
    if timestamps.empty:
        raise ValueError("JMeter result file does not contain valid timestamps")

    return int(timestamps.min()), int(timestamps.max())


def parse_jmeter_csv(file_path, green_sla, amber_sla, rag_basis, start_time=None, end_time=None, error_sla=2.0):
    df = pd.read_csv(file_path)

    # Normalize fields
    df['timeStamp'] = pd.to_numeric(df['timeStamp'], errors='coerce').astype('Int64')
    df['elapsed'] = pd.to_numeric(df['elapsed'], errors='coerce')
    df['success'] = df['success'].astype(str).str.strip().str.lower()
    df['label'] = df['label'].astype(str).str.strip()

    # Remove rows with missing core fields
    df = df.dropna(subset=['timeStamp', 'elapsed', 'label'])

    # Filter by steady-state window if provided.
    if start_time is not None and end_time is not None:
        try:
            start_time = int(start_time)
            end_time = int(end_time)
            df = df[(df['timeStamp'] >= start_time) & (df['timeStamp'] <= end_time)]
        except (TypeError, ValueError):
            pass

    summary = []
    grouped = df.groupby('label')

    for label, group in grouped:
        samples = len(group)
        if samples == 0:
            continue

        # Match JMeter statistics-summary calculations. Summary percentiles include
        # all samples (successful and failed); the over-time percentile graph is
        # success-only and is handled separately in build_report_chart_data().
        elapsed_values = group['elapsed'].dropna().tolist()
        avg = group['elapsed'].mean() / 1000.0
        p90 = jmeter_percentile(elapsed_values, 0.90) / 1000.0
        p95 = jmeter_percentile(elapsed_values, 0.95) / 1000.0

        error_count = (group['success'] != 'true').sum()
        error_pct = 100.0 * error_count / samples

        # RAG assignment per selected basis
        if rag_basis == "avg":
            metric = avg
            if metric <= green_sla:
                rag = "GREEN"
            elif metric <= amber_sla:
                rag = "AMBER"
            else:
                rag = "RED"

        elif rag_basis == "p90":
            metric = p90
            if metric <= green_sla:
                rag = "GREEN"
            elif metric <= amber_sla:
                rag = "AMBER"
            else:
                rag = "RED"

        elif rag_basis == "avg+error":
            if error_pct > error_sla:
                rag = "RED"
            else:
                metric = avg
                if metric <= green_sla:
                    rag = "GREEN"
                elif metric <= amber_sla:
                    rag = "AMBER"
                else:
                    rag = "RED"

        elif rag_basis == "p90+error":
            if error_pct > error_sla:
                rag = "RED"
            else:
                metric = p90
                if metric <= green_sla:
                    rag = "GREEN"
                elif metric <= amber_sla:
                    rag = "AMBER"
                else:
                    rag = "RED"
        else:
            metric = avg
            if metric <= green_sla:
                rag = "GREEN"
            elif metric <= amber_sla:
                rag = "AMBER"
            else:
                rag = "RED"

        summary.append({
            'Transaction': label,
            '#Samples': samples,
            # Preserve full precision internally. Formatting belongs in the UI;
            # rounding here can change the value after converting seconds back
            # to JMeter's millisecond display.
            'Avg (s)': float(avg),
            '90th % (s)': float(p90),
            '95th % (s)': float(p95),
            'Error %': float(error_pct),
            'RAG': rag
        })

    test_rag = 'GREEN'
    if any(row['RAG'] == 'RED' for row in summary):
        test_rag = 'RED'
    elif any(row['RAG'] == 'AMBER' for row in summary):
        test_rag = 'AMBER'

    return summary, test_rag
