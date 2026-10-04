from io import BytesIO
import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from matplotlib.figure import Figure


NAVY = colors.HexColor("#0C3B6C")
BLUE = colors.HexColor("#225FA1")
LIGHT_BLUE = colors.HexColor("#F4F8FC")
BORDER = colors.HexColor("#D8E3EE")
TEXT = colors.HexColor("#17324D")
MUTED = colors.HexColor("#64788D")
GREEN = colors.HexColor("#208247")
GREEN_BG = colors.HexColor("#EAF7EE")
RED = colors.HexColor("#C62F35")
RED_BG = colors.HexColor("#FFF0F0")
AMBER = colors.HexColor("#9B6800")
AMBER_BG = colors.HexColor("#FFF6DD")
GREY_BG = colors.HexColor("#F2F5F7")


def _fmt(value, digits=3, suffix=""):
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _styles():
    styles = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "VPTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=23,
            textColor=colors.white,
            alignment=TA_LEFT,
            spaceAfter=0,
        ),
        "subtitle": ParagraphStyle(
            "VPSubtitle",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=11,
            textColor=colors.white,
        ),
        "section": ParagraphStyle(
            "VPSection",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=13,
            textColor=TEXT,
            spaceBefore=8,
            spaceAfter=7,
        ),
        "body": ParagraphStyle(
            "VPBody",
            parent=styles["BodyText"],
            fontSize=8.5,
            leading=11,
            textColor=TEXT,
        ),
        "small": ParagraphStyle(
            "VPSmall",
            parent=styles["BodyText"],
            fontSize=7.5,
            leading=9.5,
            textColor=MUTED,
        ),
        "center": ParagraphStyle(
            "VPCenter",
            parent=styles["BodyText"],
            fontSize=8,
            leading=9.5,
            alignment=TA_CENTER,
            textColor=TEXT,
        ),
    }


def _header(report, styles, title="VelocityPulse Performance Test Report"):
    report_name = report.get("report_name") or report.get("file_name") or "Untitled Report"
    file_name = report.get("file_name") or "Not available"
    generated = str(report.get("timestamp") or "Not available").replace("T", " ")[:19]

    data = [[
        Paragraph(title, styles["title"]),
        Paragraph(
            f"<b>Generated:</b> {generated}<br/>"
            f"<b>Report:</b> {report_name}<br/>"
            f"<b>File:</b> {file_name}",
            styles["subtitle"],
        ),
    ]]
    table = Table(data, colWidths=[185 * mm, 80 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("BOX", (0, 0), (-1, -1), 0.5, NAVY),
    ]))
    return table


def _test_window_table(report, styles):
    rows = [[
        Paragraph("<b>Result file analysed</b><br/>" + str(report.get("file_name") or "JMeter result file"), styles["small"]),
        Paragraph("<b>Detected Test Window</b><br/>" + str(report.get("test_period") or "Not available") + "<br/>Duration: " + str(report.get("total_duration") or "Not available"), styles["small"]),
        Paragraph("<b>Steady State Period Used</b><br/>" + str(report.get("steady_state") or "Not available") + "<br/>Concurrent users: " + str(report.get("concurrent_users") if report.get("concurrent_users") is not None else "N/A"), styles["small"]),
    ]]
    table = Table(rows, colWidths=[85 * mm, 95 * mm, 85 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BLUE),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return table


def _kpi_table(overview, styles):
    successful = overview.get("successful_samples")
    failed = overview.get("failed_samples")
    error_pct = overview.get("error_pct")

    labels = [
        ("Total Samples", overview.get("total_samples"), colors.white),
        ("Successful Samples", successful, GREEN_BG),
        ("Failed Samples", f"{failed if failed is not None else '—'}\n({_fmt(error_pct, 2, '%')})" if error_pct is not None else failed, RED_BG),
        ("Average Response Time", _fmt(overview.get("avg_s"), 3, " s"), colors.HexColor("#F4F0FF")),
        ("P90 Response Time", _fmt(overview.get("p90_s"), 3, " s"), AMBER_BG),
        ("P95 Response Time", _fmt(overview.get("p95_s"), 3, " s"), RED_BG),
    ]

    cells = []
    for label, value, bg in labels:
        cell = Table(
            [[Paragraph(f"<b>{value if value is not None else '—'}</b>", styles["center"])],
             [Paragraph(label, styles["small"])]],
            colWidths=[43 * mm],
        )
        cell.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), bg),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        cells.append(cell)

    table = Table([cells], colWidths=[44 * mm] * 6)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    return table


def _transaction_table(report, styles):
    summary = report.get("summary") or []
    headers = [
        "#", "Transaction", "Samples", "Avg (s)", "Min (s)", "Max (s)",
        "P90 (s)", "P95 (s)", "Error %", "TPS", "RAG",
    ]
    data = [headers]

    for index, row in enumerate(summary, start=1):
        data.append([
            str(index),
            str(row.get("Transaction") or "—"),
            str(row.get("#Samples", 0)),
            _fmt(row.get("Avg (s)"), 3),
            _fmt(row.get("Min (s)"), 3),
            _fmt(row.get("Max (s)"), 3),
            _fmt(row.get("90th % (s)"), 3),
            _fmt(row.get("95th % (s)"), 3),
            _fmt(row.get("Error %"), 2),
            _fmt(row.get("Throughput (TPS)"), 2),
            str(row.get("RAG") or "—"),
        ])

    table = Table(
        data,
        repeatRows=1,
        colWidths=[
            10 * mm, 48 * mm, 20 * mm, 22 * mm, 20 * mm, 20 * mm,
            21 * mm, 21 * mm, 20 * mm, 18 * mm, 18 * mm,
        ],
    )
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT_BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), TEXT),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.2),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ALIGN", (1, 1), (1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.35, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]

    for idx, row in enumerate(summary, start=1):
        rag = str(row.get("RAG") or "")
        if rag == "GREEN":
            commands.append(("BACKGROUND", (-1, idx), (-1, idx), GREEN_BG))
            commands.append(("TEXTCOLOR", (-1, idx), (-1, idx), GREEN))
        elif rag == "AMBER":
            commands.append(("BACKGROUND", (-1, idx), (-1, idx), AMBER_BG))
            commands.append(("TEXTCOLOR", (-1, idx), (-1, idx), AMBER))
        elif rag == "RED":
            commands.append(("BACKGROUND", (-1, idx), (-1, idx), RED_BG))
            commands.append(("TEXTCOLOR", (-1, idx), (-1, idx), RED))

        try:
            if float(row.get("Error %") or 0) > 0:
                commands.append(("TEXTCOLOR", (8, idx), (8, idx), RED))
        except (TypeError, ValueError):
            pass

    table.setStyle(TableStyle(commands))
    return table


def _chart_card(title, image, styles):
    card = Table(
        [
            [Paragraph(title, styles["small"])],
            [image],
        ],
        colWidths=[128 * mm],
    )
    card.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.35, BORDER),
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT_BLUE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return card


def _line_chart_image(title, labels, series, y_label, divide_by=1.0):
    """Render a report data series to an in-memory PNG for ReportLab."""
    valid_series = {
        str(name): values
        for name, values in (series or {}).items()
        if isinstance(values, list) and any(value is not None for value in values)
    }
    if not labels or not valid_series:
        return None

    figure = Figure(figsize=(7.2, 3.5), dpi=120)
    axis = figure.add_subplot(111)

    x_values = list(range(len(labels)))
    for name, values in valid_series.items():
        plotted = []
        for value in values:
            if value is None:
                plotted.append(float("nan"))
            else:
                plotted.append(float(value) / divide_by)
        axis.plot(x_values[:len(plotted)], plotted, marker="o", markersize=2.5, linewidth=1.4, label=name)

    axis.set_title(title, fontsize=10, fontweight="bold")
    axis.set_ylabel(y_label, fontsize=8)
    axis.grid(True, alpha=0.25)
    axis.tick_params(axis="both", labelsize=7)

    if len(labels) <= 12:
        tick_indexes = x_values
    else:
        step = max(1, len(labels) // 10)
        tick_indexes = x_values[::step]
        if tick_indexes[-1] != x_values[-1]:
            tick_indexes.append(x_values[-1])

    axis.set_xticks(tick_indexes)
    axis.set_xticklabels(
        [labels[index] for index in tick_indexes],
        rotation=35,
        ha="right",
        fontsize=7,
    )

    if len(valid_series) <= 8:
        axis.legend(fontsize=6.5, loc="best", frameon=False)

    figure.tight_layout()

    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=140, bbox_inches="tight")
    buffer.seek(0)

    image = Image(buffer)
    image._restrictSize(128 * mm, 68 * mm)
    return image


def _primary_chart_flowables(report, styles):
    labels = report.get("chart_time_labels") or []
    if not labels:
        return []

    charts = []

    avg_image = _line_chart_image(
        "Average Response Time Over Time",
        labels,
        report.get("series_avg_by_txn") or {},
        "Response Time (seconds)",
        divide_by=1000.0,
    )
    if avg_image:
        charts.append(
            _chart_card(
                "Average Response Time Over Time (seconds, JMeter-compatible)",
                avg_image,
                styles,
            )
        )

    percentile_series = report.get("series_response_percentiles_over_time") or {}
    if not percentile_series:
        percentile_series = report.get("series_p90_by_txn") or {}

    percentile_image = _line_chart_image(
        "Response Time Percentiles Over Time",
        labels,
        percentile_series,
        "Response Time (seconds)",
        divide_by=1000.0,
    )
    if percentile_image:
        charts.append(
            _chart_card(
                "Response Time Percentiles Over Time (seconds, JMeter-compatible)",
                percentile_image,
                styles,
            )
        )

    tps_series = report.get("series_tps_by_txn") or {}
    if not tps_series:
        throughput = report.get("series_throughput_over_time") or []
        if throughput:
            tps_series = {"Total TPS": throughput}

    tps_image = _line_chart_image(
        "Transactions Per Second",
        labels,
        tps_series,
        "Transactions / second",
    )
    if tps_image:
        charts.append(
            _chart_card(
                "Transactions Per Second (JMeter-compatible)",
                tps_image,
                styles,
            )
        )

    error_image = _line_chart_image(
        "Error Rate Over Time",
        labels,
        report.get("series_error_rate_by_txn") or {},
        "Error %",
    )
    if error_image:
        charts.append(
            _chart_card(
                "Error Rate Over Time (%)",
                error_image,
                styles,
            )
        )

    return charts


def _graph_flowables(report, static_root, styles):
    items = []
    graph_paths = report.get("graph_paths") or {}
    labels = [
        ("response_distribution", "Response Time Distribution"),
        ("error_trend", "Error Trend"),
        ("sla_heatmap", "Response Time Heatmap"),
        ("threads_over_time", "Threads Over Time"),
        ("transaction_progress", "Transactions Per Second"),
    ]
    for key, title in labels:
        relative = graph_paths.get(key)
        if not relative:
            continue
        absolute = os.path.join(static_root, relative.replace("/", os.sep))
        if not os.path.isfile(absolute):
            continue
        try:
            image = Image(absolute)
            image._restrictSize(125 * mm, 65 * mm)
            items.append(_chart_card(title, image, styles))
        except Exception:
            continue
    return items


def build_single_report_pdf(report, overview, static_root="static"):
    styles = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=str(report.get("report_name") or "VelocityPulse Report"),
        author="VelocityPulse",
    )

    story = [
        _header(report, styles),
        Spacer(1, 4 * mm),
        _test_window_table(report, styles),
        Spacer(1, 4 * mm),
        _kpi_table(overview or {}, styles),
        Spacer(1, 5 * mm),
        Paragraph("Transaction Summary (Steady State Period)", styles["section"]),
        _transaction_table(report, styles),
    ]

    primary_graphs = _primary_chart_flowables(report, styles)
    supplemental_graphs = _graph_flowables(report, static_root, styles)

    if primary_graphs or supplemental_graphs:
        story.extend([PageBreak(), Paragraph("Performance Graphs", styles["section"])])

    if primary_graphs:
        rows = [
            primary_graphs[index:index + 2]
            for index in range(0, len(primary_graphs), 2)
        ]
        graph_table = Table(rows, colWidths=[132 * mm, 132 * mm])
        graph_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(graph_table)

    if supplemental_graphs:
        story.extend([
            Spacer(1, 4 * mm),
            Paragraph("Supplemental Graphs", styles["section"]),
        ])
        rows = [
            supplemental_graphs[index:index + 2]
            for index in range(0, len(supplemental_graphs), 2)
        ]
        supplemental_table = Table(rows, colWidths=[132 * mm, 132 * mm])
        supplemental_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(supplemental_table)

    observations = report.get("observations") or []
    if observations:
        story.extend([Spacer(1, 4 * mm), Paragraph("Performance Observations", styles["section"])])
        for observation in observations:
            title = str(observation.get("title") or "Observation")
            text = str(observation.get("text") or "")
            story.append(Paragraph(f"<b>{title}</b> — {text}", styles["body"]))
            story.append(Spacer(1, 1.5 * mm))

    doc.build(story)
    return buffer.getvalue()


def _compare_card(report, overview, label, styles):
    rag = str(report.get("rag_result") or "N/A")
    rows = [
        [Paragraph(f"<b>{label}</b>", styles["body"])],
        [Paragraph(str(report.get("report_name") or report.get("file_name") or "Report"), styles["body"])],
        [Paragraph(
            f"Date: {report.get('test_date') or 'N/A'} &nbsp;&nbsp; "
            f"Samples: {overview.get('total_samples', '—')} &nbsp;&nbsp; "
            f"Avg: {_fmt(overview.get('avg_s'), 3, ' s')} &nbsp;&nbsp; "
            f"P95: {_fmt(overview.get('p95_s'), 3, ' s')} &nbsp;&nbsp; "
            f"RAG: {rag}",
            styles["small"],
        )],
    ]
    table = Table(rows, colWidths=[128 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def build_compare_report_pdf(r1, r2, overview1, overview2, metric, comparisons, observations):
    styles = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title="VelocityPulse Comparison Report",
        author="VelocityPulse",
    )

    pseudo = {
        "report_name": f"{r1.get('report_name') or 'Earlier'} vs {r2.get('report_name') or 'Later'}",
        "file_name": "Comparison",
        "timestamp": "",
    }
    story = [
        _header(pseudo, styles, title="VelocityPulse Comparison Report"),
        Spacer(1, 4 * mm),
        Table(
            [[_compare_card(r1, overview1, "Earlier Test", styles),
              _compare_card(r2, overview2, "Later Test", styles)]],
            colWidths=[132 * mm, 132 * mm],
        ),
        Spacer(1, 5 * mm),
        Paragraph(
            f"Transaction Comparison — {metric}" + ("" if metric == "Error %" else " in seconds"),
            styles["section"],
        ),
    ]

    unit = "%" if metric == "Error %" else "s"
    data = [["Transaction", f"Earlier ({unit})", f"Later ({unit})", f"Absolute Δ ({unit})", "Change %", "Status"]]
    for row in comparisons:
        digits = 2 if metric == "Error %" else 3
        change_pct = row.get("change_pct")
        data.append([
            str(row.get("transaction") or "—"),
            _fmt(row.get("v1"), digits),
            _fmt(row.get("v2"), digits),
            f"{float(row.get('diff') or 0):+.{digits}f}",
            f"{float(change_pct):+.2f}%" if change_pct is not None else "—",
            str(row.get("status") or "—"),
        ])

    table = Table(data, repeatRows=1, colWidths=[62 * mm, 38 * mm, 38 * mm, 42 * mm, 34 * mm, 42 * mm])
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT_BLUE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.35, BORDER),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    for idx, row in enumerate(comparisons, start=1):
        status = row.get("status")
        if status == "Improved":
            commands.append(("BACKGROUND", (-1, idx), (-1, idx), GREEN_BG))
            commands.append(("TEXTCOLOR", (-1, idx), (-1, idx), GREEN))
        elif status == "Degraded":
            commands.append(("BACKGROUND", (-1, idx), (-1, idx), RED_BG))
            commands.append(("TEXTCOLOR", (-1, idx), (-1, idx), RED))
        else:
            commands.append(("BACKGROUND", (-1, idx), (-1, idx), GREY_BG))
            commands.append(("TEXTCOLOR", (-1, idx), (-1, idx), MUTED))
    table.setStyle(TableStyle(commands))
    story.append(table)

    if observations:
        story.extend([Spacer(1, 4 * mm), Paragraph("Comparison Observations", styles["section"])])
        for observation in observations:
            story.append(Paragraph("• " + str(observation), styles["body"]))
            story.append(Spacer(1, 1.5 * mm))

    doc.build(story)
    return buffer.getvalue()
