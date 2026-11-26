"""Builds the simple, high-level architecture diagrams in two formats from one spec:

  simple-architecture.drawio   editable in draw.io / diagrams.net (two pages)
  simple-architecture.html     the same diagrams as SVG, rendered to PNG by render.js

Run: python3 docs/architecture/diagrams/simple/build.py && node docs/architecture/diagrams/simple/render.js
Fictional, unbranded bank. Standard library only.
"""
from __future__ import annotations

import html
import os
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))

# Classic draw.io palette (fill, stroke).
BLUE = ("#dae8fc", "#6c8ebf")
GREEN = ("#d5e8d4", "#82b366")
YELLOW = ("#fff2cc", "#d6b656")
PURPLE = ("#e1d5e7", "#9673a6")
GREY = ("#f5f5f5", "#666666")
WHITE = ("#ffffff", "#666666")
ORANGE = ("#ffe6cc", "#d79b00")

# ---------------------------------------------------------------- diagram specs
# group: (id, x, y, w, h, label, colours, dashed)
# box:   (id, x, y, w, h, title, subtitle, colours)
# edge:  (source, target, points [(x, y)...] from start to end, label, dashed)

AZURE = {
    "name": "Azure (target)",
    "title": "SME RM Copilot on Azure: high-level view",
    "w": 1620, "h": 860,
    "groups": [
        ("region", 250, 90, 1080, 700, "Azure UAE North · private network", ("#f7fbff", "#6c8ebf"), True),
        ("platform", 270, 560, 1040, 130, "Shared Azure services (private endpoints)", ("#ffffff", "#999999"), False),
    ],
    "boxes": [
        ("rm", 30, 380, 200, 90, "Relationship manager", "CRM sidebar · Teams tab", WHITE),
        ("entra", 30, 170, 200, 80, "Microsoft Entra ID", "sign-in and identity", PURPLE),
        ("agw", 290, 380, 170, 90, "Application Gateway", "WAF, internal only", GREY),
        ("aks", 500, 360, 300, 130, "Copilot services (AKS)", "supervisor + 3 agents · tool-gateway · workers", GREEN),
        ("apim", 840, 380, 260, 90, "API Management", "AI gateway · MCP gateway · system APIs", GREY),
        ("aoai", 1080, 150, 210, 90, "Azure OpenAI", "in Azure AI Foundry", BLUE),
        ("bank", 1380, 375, 220, 100, "Bank systems", "CRM · core banking · LOS · KYC", ORANGE),
        ("search", 290, 605, 140, 70, "AI Search", "policy documents", BLUE),
        ("safety", 440, 605, 150, 70, "Content Safety", "and PII masking", BLUE),
        ("kv", 600, 605, 135, 70, "Key Vault", "signs approvals", YELLOW),
        ("cosmos", 745, 605, 150, 70, "Cosmos DB", "drafts, approvals", YELLOW),
        ("memory", 905, 605, 215, 70, "Memory (Cosmos DB)", "checkpoints · session · episodic", YELLOW),
        ("audit", 1130, 605, 160, 70, "Audit log", "Event Hubs, WORM Blob", YELLOW),
        ("monitor", 270, 715, 1040, 50, "Azure Monitor", "App Insights · Prometheus · Grafana", GREY),
    ],
    "edges": [
        ("rm", "agw", [(230, 425), (290, 425)], "", False),
        ("rm", "entra", [(130, 380), (130, 250)], "sign-in", True),
        ("agw", "aks", [(460, 425), (500, 425)], "", False),
        ("aks", "apim", [(800, 425), (840, 425)], "", False),
        ("apim", "aoai", [(935, 380), (935, 195), (1080, 195)], "prompts", False),
        ("apim", "bank", [(1100, 425), (1380, 425)], "ExpressRoute", False),
        ("aks", "platform", [(650, 490), (650, 560)], "", False),
    ],
}

LOCAL = {
    "name": "Current application",
    "title": "Current application (python3 stack.py): high-level view",
    "w": 1620, "h": 800,
    "groups": [
        ("svc", 250, 170, 685, 330, "Copilot services · code/services", ("#f7fff6", "#82b366"), True),
        ("mocks", 250, 570, 1310, 150, "Azure mocks · same REST APIs as Azure", ("#f7fbff", "#6c8ebf"), True),
    ],
    "boxes": [
        ("browser", 40, 300, 170, 90, "Browser console", "sign in as a test RM", WHITE),
        ("bff", 275, 300, 160, 90, "web-bff", "sign-in, approvals", GREEN),
        ("orch", 460, 300, 190, 90, "orchestrator", "supervisor + 3 agents", GREEN),
        ("tg", 675, 300, 240, 90, "tool-gateway", "MCP servers · entitlements · writes", GREEN),
        ("workers", 460, 415, 455, 60, "Background workers", "outbox · audit · eval workers", GREEN),
        ("apim", 970, 230, 260, 160, "APIM mock", "AI gateway · MCP gateway · system APIs", GREY),
        ("aoai", 1270, 230, 290, 70, "Azure OpenAI mock", "deterministic model", BLUE),
        ("sor", 1270, 320, 290, 70, "Bank systems mock", "CRM · ODS · LOS · KYC", ORANGE),
        ("checks", 1270, 420, 290, 80, "Checks", "122+ tests · RAGAS gate · metrics · traces", WHITE),
        ("m1", 280, 615, 140, 70, "Entra ID", "", PURPLE),
        ("m2", 435, 615, 140, 70, "Key Vault", "", YELLOW),
        ("m3", 590, 615, 140, 70, "Cosmos DB", "", YELLOW),
        ("m4", 745, 615, 140, 70, "Event Hubs", "", YELLOW),
        ("m5", 900, 615, 140, 70, "Content Safety", "", BLUE),
        ("m6", 1055, 615, 140, 70, "AI Language", "", BLUE),
        ("m7", 1210, 615, 140, 70, "AI Search", "", BLUE),
        ("m8", 1365, 615, 160, 70, "App Configuration", "", YELLOW),
    ],
    "edges": [
        ("browser", "bff", [(210, 345), (275, 345)], "", False),
        ("bff", "orch", [(435, 345), (460, 345)], "", False),
        ("orch", "tg", [(650, 345), (675, 345)], "", False),
        ("orch", "apim", [(555, 300), (555, 260), (970, 260)], "model + MCP calls", False),
        ("tg", "apim", [(915, 345), (970, 345)], "", False),
        ("apim", "aoai", [(1230, 265), (1270, 265)], "", False),
        ("apim", "sor", [(1230, 355), (1270, 355)], "", False),
        ("svc", "mocks", [(540, 500), (540, 570)], "HTTP", False),
    ],
}

DIAGRAMS = [("azure", AZURE), ("local", LOCAL)]


# ---------------------------------------------------------------- draw.io output
def drawio(spec: dict, page_id: str) -> str:
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    cells.append(
        f'<mxCell id="{page_id}-title" value="{escape(spec["title"])}" style="text;html=1;fontSize=22;fontStyle=1;align=left;verticalAlign=middle;" vertex="1" parent="1">'
        f'<mxGeometry x="40" y="20" width="1200" height="40" as="geometry"/></mxCell>'
    )
    for gid, x, y, w, h, label, (fill, stroke), dashed in spec["groups"]:
        style = (f"rounded=1;arcSize=4;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
                 f"verticalAlign=top;align=left;spacingLeft=12;spacingTop=6;fontStyle=1;fontSize=14;fontColor=#333333;"
                 + ("dashed=1;" if dashed else ""))
        cells.append(f'<mxCell id="{page_id}-{gid}" value="{escape(label)}" style="{style}" vertex="1" parent="1">'
                     f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
    for bid, x, y, w, h, t, sub, (fill, stroke) in spec["boxes"]:
        value = f"<b>{html.escape(t)}</b>" + (f"<br><font style=&quot;font-size: 12px&quot;>{html.escape(sub)}</font>" if sub else "")
        style = f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};fontSize=15;fontColor=#1a1a1a;"
        cells.append(f'<mxCell id="{page_id}-{bid}" value="{escape(value, {chr(34): "&quot;"})}" style="{style}" vertex="1" parent="1">'
                     f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
    for i, (src, dst, pts, label, dashed) in enumerate(spec["edges"]):
        style = ("edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=block;endFill=1;strokeWidth=2;strokeColor=#333333;"
                 "fontSize=12;" + ("dashed=1;" if dashed else ""))
        mid = "".join(f'<mxPoint x="{px}" y="{py}"/>' for px, py in pts[1:-1])
        geo = (f'<mxGeometry relative="1" as="geometry"><mxPoint x="{pts[0][0]}" y="{pts[0][1]}" as="sourcePoint"/>'
               f'<mxPoint x="{pts[-1][0]}" y="{pts[-1][1]}" as="targetPoint"/>'
               + (f'<Array as="points">{mid}</Array>' if mid else "") + "</mxGeometry>")
        cells.append(f'<mxCell id="{page_id}-e{i}" value="{escape(label)}" style="{style}" edge="1" parent="1" '
                     f'source="{page_id}-{src}" target="{page_id}-{dst}">{geo}</mxCell>')
    model = (f'<mxGraphModel dx="{spec["w"]}" dy="{spec["h"]}" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" '
             f'arrows="1" fold="1" page="1" pageScale="1" pageWidth="{spec["w"]}" pageHeight="{spec["h"]}" math="0" shadow="0">'
             f'<root>{"".join(cells)}</root></mxGraphModel>')
    return f'<diagram id="{page_id}" name="{escape(spec["name"])}">{model}</diagram>'


# ---------------------------------------------------------------- SVG output (for the PNGs)
def svg(spec: dict) -> str:
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{spec["w"]}" height="{spec["h"]}" viewBox="0 0 {spec["w"]} {spec["h"]}" '
           f'font-family="Helvetica, Arial, sans-serif">',
           '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
           '<path d="M0,0 L10,5 L0,10 z" fill="#333333"/></marker></defs>',
           f'<rect width="{spec["w"]}" height="{spec["h"]}" fill="#ffffff"/>',
           f'<text x="40" y="48" font-size="24" font-weight="bold" fill="#1a1a1a">{html.escape(spec["title"])}</text>']
    for _gid, x, y, w, h, label, (fill, stroke), dashed in spec["groups"]:
        dash = ' stroke-dasharray="8 6"' if dashed else ""
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>')
        out.append(f'<text x="{x + 14}" y="{y + 24}" font-size="15" font-weight="bold" fill="#333333">{html.escape(label)}</text>')
    for src, dst, pts, label, dashed in spec["edges"]:
        dash = ' stroke-dasharray="7 5"' if dashed else ""
        p = " ".join(f"{px},{py}" for px, py in pts)
        out.append(f'<polyline points="{p}" fill="none" stroke="#333333" stroke-width="2" marker-end="url(#a)"{dash}/>')
        if label:
            (x1, y1), (x2, y2) = pts[-2], pts[-1]
            lx, ly = (x1 + x2) / 2, (y1 + y2) / 2
            vertical = x1 == x2
            tx, ty = (lx + 8, ly + 5) if vertical else (lx, ly - 8)
            anchor = "start" if vertical else "middle"
            out.append(f'<text x="{tx}" y="{ty}" font-size="13" fill="#333333" text-anchor="{anchor}" '
                       f'paint-order="stroke" stroke="#ffffff" stroke-width="5">{html.escape(label)}</text>')
    for _bid, x, y, w, h, t, sub, (fill, stroke) in spec["boxes"]:
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
        cy = y + h / 2
        if sub:
            out.append(f'<text x="{x + w / 2}" y="{cy - 4}" font-size="16" font-weight="bold" fill="#1a1a1a" text-anchor="middle">{html.escape(t)}</text>')
            out.append(f'<text x="{x + w / 2}" y="{cy + 17}" font-size="12.5" fill="#444444" text-anchor="middle">{html.escape(sub)}</text>')
        else:
            out.append(f'<text x="{x + w / 2}" y="{cy + 5}" font-size="15" font-weight="bold" fill="#1a1a1a" text-anchor="middle">{html.escape(t)}</text>')
    out.append("</svg>")
    return "".join(out)


def main() -> None:
    mx = ('<mxfile host="app.diagrams.net" type="device">' + "".join(drawio(s, pid) for pid, s in DIAGRAMS) + "</mxfile>")
    with open(os.path.join(HERE, "simple-architecture.drawio"), "w", encoding="utf-8") as f:
        f.write(mx)
    page = ['<!doctype html><meta charset="utf-8"><title>Simple architecture</title>',
            '<body style="margin:0;background:#ffffff">']
    for pid, s in DIAGRAMS:
        page.append(f'<div id="{pid}" style="display:inline-block">{svg(s)}</div><br>')
    with open(os.path.join(HERE, "simple-architecture.html"), "w", encoding="utf-8") as f:
        f.write("".join(page))
    print("wrote simple-architecture.drawio and simple-architecture.html")


if __name__ == "__main__":
    main()
