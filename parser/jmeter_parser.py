import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import os

def detect_test_window(file_path):
    df = pd.read_csv(file_path)
    timestamps = df['timeStamp'].dropna().astype(int)
    return timestamps.min(), timestamps.max()

def parse_jmeter_csv(file_path, green_sla, amber_sla, rag_basis, start_time=None, end_time=None):
    df = pd.read_csv(file_path)
    df['timeStamp'] = df['timeStamp'].astype(int)
    df['elapsed'] = df['elapsed'].astype(float)
    df['success'] = df['success'].astype(str).str.strip().str.lower()
    df['label'] = df['label'].astype(str)

    # Filter by steady state window
    if start_time and end_time:
        df = df[(df['timeStamp'] >= start_time) & (df['timeStamp'] <= end_time)]

    summary = []
    grouped = df.groupby('label')

    for label, group in grouped:
        samples = len(group)
        avg = group['elapsed'].mean() / 1000
        p90 = group['elapsed'].quantile(0.90) / 1000
        p95 = group['elapsed'].quantile(0.95) / 1000
        error_pct = 100.0 * len(group[group['success'] != 'true']) / samples

        rag_value = avg if rag_basis == 'avg' else p90
        if rag_value <= green_sla / 1000:
            rag = 'GREEN'
        elif rag_value <= amber_sla / 1000:
            rag = 'AMBER'
        else:
            rag = 'RED'

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
