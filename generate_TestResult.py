import pandas as pd

def evaluate_sla(summary, green_sla, amber_sla, rag_basis="avg", include_error=False, error_threshold=None):
    """
    Evaluate SLA compliance for a given summary of transactions.

    Args:
        summary (list of dict): Parsed transaction summary from JMeter CSV.
        green_sla (float): Green SLA threshold in seconds.
        amber_sla (float): Amber SLA threshold in seconds.
        rag_basis (str): Basis for RAG evaluation ("avg" or "p90").
        include_error (bool): Whether to include error % in evaluation.
        error_threshold (float): Error % threshold if include_error is True.

    Returns:
        tuple: (updated_summary, overall_rag)
    """

    updated_summary = []
    for row in summary:
        try:
            avg_s = float(row.get("Avg (s)", 0))
            p90_s = float(row.get("90th % (s)", 0))
            error_pct = float(row.get("Error %", 0))
        except (ValueError, TypeError):
            avg_s = p90_s = error_pct = 0.0

        # Choose basis value
        basis_value = avg_s if rag_basis == "avg" else p90_s

        # Default RAG by response time
        if basis_value > amber_sla:
            rag = "RED"
        elif basis_value > green_sla:
            rag = "AMBER"
        else:
            rag = "GREEN"

        # Error % check
        if include_error and error_threshold is not None:
            if error_pct > float(error_threshold):
                rag = "RED"

        # Update row with recalculated RAG
        new_row = dict(row)
        new_row["RAG"] = rag
        updated_summary.append(new_row)

    # Overall RAG = worst case
    overall_rag = "GREEN"
    if any(r["RAG"] == "RED" for r in updated_summary):
        overall_rag = "RED"
    elif any(r["RAG"] == "AMBER" for r in updated_summary):
        overall_rag = "AMBER"

    return updated_summary, overall_rag


# Example usage (for testing only):
if __name__ == "__main__":
    sample_summary = [
        {"Transaction": "Login", "Avg (s)": "1.2", "90th % (s)": "1.5", "Error %": "0.0", "RAG": ""},
        {"Transaction": "Search", "Avg (s)": "3.2", "90th % (s)": "3.5", "Error %": "0.0", "RAG": ""},
    ]
    updated, overall = evaluate_sla(
        sample_summary,
        green_sla=2.0,
        amber_sla=5.0,
        rag_basis="avg",
        include_error=True,
        error_threshold=2.0
    )
    print(updated)
    print("Overall RAG:", overall)