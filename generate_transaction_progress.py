import pandas as pd
import matplotlib
matplotlib.use("Agg")   # add this line
import matplotlib.pyplot as plt
import seaborn as sns
import os

from jmeter_parser import JMETER_REPORT_GRANULARITY_MS

def generate_transaction_progress(df, out_file="static/reports/graphs/transaction_progress.png"):
    os.makedirs(os.path.dirname(out_file), exist_ok=True)

    # ✅ Ensure DataFrame
    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception as e:
            print("⚠ Could not convert df to DataFrame:", e)
            return

    # Normalize columns and reproduce JMeter Transactions Per Second semantics.
    frame = df.copy()
    frame.columns = [str(col).strip().lower() for col in frame.columns]

    required = {"timestamp", "elapsed", "label"}
    if not required.issubset(frame.columns):
        print("Skipping transaction TPS graph: required columns missing")
        return

    frame["timestamp"] = pd.to_numeric(frame["timestamp"], errors="coerce")
    frame["elapsed"] = pd.to_numeric(frame["elapsed"], errors="coerce")
    frame["label"] = frame["label"].astype(str).str.strip()
    if "success" in frame.columns:
        frame["success"] = frame["success"].astype(str).str.strip().str.lower().isin(["true", "1"])
    else:
        frame["success"] = True

    frame = frame.dropna(subset=["timestamp", "elapsed", "label"]).copy()
    if frame.empty:
        return

    frame["end_timestamp"] = pd.to_datetime(
        frame["timestamp"] + frame["elapsed"], unit="ms", errors="coerce"
    )
    frame = frame.dropna(subset=["end_timestamp"])
    bucket_rule = f"{JMETER_REPORT_GRANULARITY_MS}ms"
    frame["time_bucket"] = frame["end_timestamp"].dt.floor(bucket_rule)
    frame["status"] = frame["success"].map({True: "success", False: "failure"})
    frame["series"] = frame["label"] + "-" + frame["status"]

    grouped = (
        frame.groupby(["time_bucket", "series"])
        .size()
        .div(JMETER_REPORT_GRANULARITY_MS / 1000.0)
        .unstack(fill_value=0.0)
        .sort_index()
    )

    if grouped.empty:
        return

    plt.figure(figsize=(10, 5))
    grouped.plot(ax=plt.gca())
    plt.title("Transactions Per Second")
    plt.xlabel("Time")
    plt.ylabel("Transactions / second")
    plt.tight_layout()
    plt.savefig(out_file)
    plt.close()
