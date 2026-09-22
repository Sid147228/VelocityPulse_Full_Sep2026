from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.platypus import Table, TableStyle

def create_invoice(filename):
    c = canvas.Canvas(filename, pagesize=A4)
    width, height = A4

    # Border
    margin = 20
    c.setStrokeColor(colors.black)
    c.setLineWidth(2)
    c.rect(margin, margin, width - 2*margin, height - 2*margin)

    # Header
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(width/2, height - 50, "INVOICE")

    # Garage details
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 90, "Kris’s Garage & Tyres")
    c.drawString(40, height - 105, "93b Station Rd, Edinburgh, Ratho Station,")
    c.drawString(40, height - 120, "Newbridge EH28 8QT, United Kingdom")
    c.drawString(40, height - 135, " ")
    c.drawString(40, height - 150, " ")

    # Invoice details
    c.drawString(width - 250, height - 90, "Invoice No: KG-INV-2025-0128")
    c.drawString(width - 250, height - 105, "Invoice Date: 25 Sept 2025")

    # Bill To
    c.setFont("Helvetica-Bold", 12)
    billto_height = 110
    c.rect(40, height - 260, width - 80, billto_height)

    text_top = height - 230
    c.drawString(50, text_top, "Bill To:")
    c.setFont("Helvetica", 10)
    c.drawString(120, text_top, "Warrantywise Ltd")
    c.drawString(120, text_top - 15, "The Rocket Centre, 3 Trident Way, Blackburn, BB1 3NU")
    c.drawString(120, text_top - 30, " ")
    c.drawString(120, text_top - 45, " ")

    # Vehicle & Work Details
    c.setFont("Helvetica-Bold", 12)
    section_height = 200
    c.rect(40, height - 260 - section_height, width - 80, section_height)
    c.drawString(50, height - 275, "Vehicle & Work Details")

    y = height - 295
    c.setFont("Helvetica", 10)
    c.drawString(60, y, "Registration: AY19 PZS")
    y -= 15
    c.drawString(60, y, "Description: MG 2019, 1.0L 3-cylinder Turbo (Petrol, MG1)")
    y -= 15
    c.drawString(60, y, "Job: Cylinder Head Gasket Replacement")

    # Scope of Work Summary
    y -= 25
    c.setFont("Helvetica-Bold", 10)
    c.drawString(60, y, "Scope of Work (Summary):")
    y -= 15
    c.setFont("Helvetica", 9)
    scope = [
        "• Removal of cylinder head and related components",
        "• Drain coolant and engine oil",
        "• Replace head gasket and full gasket set",
        "• Refit timing chain and reset timing",
        "• Reassembly of all removed components",
        "• Refill with fresh engine oil, filter, and coolant",
        "• Final testing and adjustments"
    ]
    for line in scope:
        c.drawString(70, y, line)
        y -= 12

    # Parts & Labour Section
    y -= 40
    c.setFont("Helvetica-Bold", 12)
    box_height = 180
    c.rect(40, y - box_height + 20, width - 80, box_height)
    c.drawString(50, y, "Parts & Labour")

    data = [
        ["Description", "Qty", "Unit Price", "Total"],
        ["Cylinder Head Gasket Repair + Oil and Filter Change", "1", "£325", "£325"],
        ["Diagnostics", "1", "£75", "£75"],
        ["Coolant & Consumables", "-", "Included", "Included"],
        ["Labour: Cylinder Head Gasket Repair", "6 hrs", "£75/hr", "£450"],
        ["", "", "Subtotal", "£850"],
        ["", "", "VAT (if applicable)", "£0.00"],
        ["", "", "Total Due", "£850"],
    ]

    table = Table(data, colWidths=[250, 60, 80, 80])
    style = TableStyle([
        ("GRID", (0,0), (-1,-1), 0.5, colors.black),
        ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
        ("ALIGN", (1,1), (-1,-1), "CENTER"),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTNAME", (0,1), (-1,-1), "Helvetica"),
        ("SPAN", (0,5), (2,5)),
        ("SPAN", (0,6), (2,6)),
        ("SPAN", (0,7), (2,7)),
        ("FONTNAME", (0,7), (-1,7), "Helvetica-Bold"),
    ])
    table.setStyle(style)

    table.wrapOn(c, width, height)
    table.drawOn(c, 50, y - 160)

    # Footer with Bank Details
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, 120, "Bank Details:")
    c.setFont("Helvetica", 9)
    c.drawString(60, 105, "Account Name: Krzysztof Gryszkiewicz")
    c.drawString(60, 90, "Sort Code: 40 11 92")
    c.drawString(60, 75, "Account Number: 25182491")

    c.setFont("Helvetica", 8)
    c.drawString(50, 55, "This invoice is issued for Warrantywise reimbursement.")

    c.save()

# Run
create_invoice("invoice.pdf")