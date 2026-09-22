import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import os

def detect_test_window(file_path):
    df = pd.read_csv(file_path)
    timestamps = df['timeStamp'].dropna().astype(int)
    return timestamps.min(), timestamps.max()

def parse_jmeter_csv(file_path, green_sla, amber_sla, rag_basis, start_time=None, end_time=None, error_sla=2.0):
    df = pd.read_csv(file_path)

    # Normalize fields
    df['timeStamp'] = pd.to_numeric(df['timeStamp'], errors='coerce').astype('Int64')
    df['elapsed'] = pd.to_numeric(df['elapsed'], errors='coerce')
    df['success'] = df['success'].astype(str).str.strip().str.lower()
    df['label'] = df['label'].astype(str).str.strip()

    # Remove rows with missing core fields
    df = df.dropna(subset=['timeStamp', 'elapsed', 'label'])

    # Filter by steady state window if provided and valid
    if start_time and end_time:
        try:
            start_time = int(start_time)
            end_time = int(end_time)
            df = df[(df['timeStamp'] >= start_time) & (df['timeStamp'] <= end_time)]
        except ValueError:
            # Skip filtering if inputs are not valid integers
            pass

    summary = []
    grouped = df.groupby('label')

    for label, group in grouped:
        samples = len(group)
        if samples == 0:
            continue

        # Compute metrics in seconds
        avg = group['elapsed'].mean() / 1000.0
        p90 = group['elapsed'].quantile(0.90) / 1000.0
        p95 = group['elapsed'].quantile(0.95) / 1000.0

        # Error percentage across all rows (do not filter successes for timing)
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
            # Fallback: treat as avg
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
            'Avg (s)': f"{avg:.2f}",
            '90th % (s)': f"{p90:.2f}",
            '95th % (s)': f"{p95:.2f}",
            'Error %': f"{error_pct:.2f}",
            'RAG': rag
        })

    # Overall test RAG
    test_rag = 'GREEN'
    if any(row['RAG'] == 'RED' for row in summary):
        test_rag = 'RED'
    elif any(row['RAG'] == 'AMBER' for row in summary):
        test_rag = 'AMBER'

    # ✅ Removed internal generate_graphs call
    return summary, test_rag
