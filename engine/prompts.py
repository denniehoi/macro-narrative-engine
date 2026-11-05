"""Systeemprompts en JSON-schema's. Alle LLM-uitvoer loopt via een geforceerde tool-call (gestructureerd)."""
from __future__ import annotations

HORIZONS = ["lt_3m", "3_6m", "6_12m", "gt_12m"]
ROLES = ["pure_play", "pick_and_shovel", "second_order"]

# --- Lite: discovery ------------------------------------------------------------------

DISCOVERY_SYSTEM = """Je bent de discovery-filter van een macro-narratief systeem voor lange (structurele) beleggingsthema's.
Je krijgt de grootste nieuws-events van gisteren en de lijst met topics die al gevolgd worden.

Taak: kies alleen events die een direct transmissiemechanisme hebben naar aandelen, rentes of grondstoffen.
- Negeer events die overlappen met een bestaand topic: zet dan duplicate_of op de slug van dat topic.
- Groepeer events die over hetzelfde thema gaan tot één kandidaat.
- mmp (Market Moving Potential, 0-100): hoe groot en direct is de invloed op de markt? 70+ = duidelijk mechanisme met meetbare sectorimpact.
- regime: "structural" als het thema maanden tot jaren doorwerkt (capex-cycli, regelgeving, toeleveringsketens, demografie);
  "shock" als het een plotselinge gebeurtenis is waarvan de impact waarschijnlijk binnen weken uitdooft.
- high_impact: alleen true voor een shock die nu al grote koersbewegingen veroorzaakt of kan veroorzaken.
- main_macro_driver: één Nederlandse zin die het mechanisme beschrijft. Dit wordt de meetlat waarmee artikelen worden beoordeeld,
  dus wees specifiek (wie, wat, via welk kanaal naar welke sectoren).
- keywords: 3-6 Engelse zoekfrasen van 2-4 woorden die specifiek genoeg zijn (niet "oil", wel "Strait of Hormuz").
- title: korte Nederlandse titel.
Geef maximaal 8 kandidaten en houd transmission op één korte zin.
Lever liever nul kandidaten dan zwakke."""

DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_uris": {"type": "array", "items": {"type": "string"}},
                    "title": {"type": "string"},
                    "main_macro_driver": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 6},
                    "mmp": {"type": "integer", "minimum": 0, "maximum": 100},
                    "transmission": {"type": "string"},
                    "regime": {"type": "string", "enum": ["structural", "shock"]},
                    "high_impact": {"type": "boolean"},
                    "duplicate_of": {"type": ["string", "null"]},
                },
                "required": ["event_uris", "title", "main_macro_driver", "keywords", "mmp", "transmission",
                             "regime", "high_impact", "duplicate_of"],
            },
        }
    },
    "required": ["candidates"],
}

# --- Lite: thesis-alignment -------------------------------------------------------------

ALIGNMENT_SYSTEM = """Je beoordeelt of nieuwsartikelen echt over een beleggingsthese gaan.
Je krijgt de these (main_macro_driver) en een steekproef van artikelen van gisteren.

- alignment (0-1): het aandeel artikelen dat het mechanisme van de these inhoudelijk bespreekt.
  Een artikel dat alleen een zoekwoord noemt, clickbait of een ander onderwerp telt niet.
- backward_looking_share (0-1): het aandeel relevante artikelen dat alleen terugblikt
  (samenvatting, "wat we leerden", afgeronde zaak) in plaats van nieuwe ontwikkelingen te melden.
- emerging_tags: maximaal 5 nieuwe deelthema's die je in de artikelen ziet opkomen (Nederlands, kort).
Antwoord uitsluitend via de tool."""

ALIGNMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "alignment": {"type": "number", "minimum": 0, "maximum": 1},
        "on_thesis_count": {"type": "integer", "minimum": 0},
        "backward_looking_share": {"type": "number", "minimum": 0, "maximum": 1},
        "emerging_tags": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "note": {"type": "string"},
    },
    "required": ["alignment", "on_thesis_count", "backward_looking_share", "emerging_tags", "note"],
}

# --- Reasoning: horizon, benchmarks, resolutie, mutatie, overlap ---------------------------

REASONING_SYSTEM = """Je bent de Reasoning-gatekeeper van een systeem dat lange beleggingsthema's selecteert.
Per topic krijg je: de these, recente koppen, gekoppelde of gevonden Polymarket-markten, de vaste benchmarklijst
en de andere gevolgde topics. Beoordeel nuchter; je bent geen verkoper van het thema.

1. horizon_class: hoe lang blijft dit thema de markt beïnvloeden? lt_3m, 3_6m, 6_12m of gt_12m.
   Gebruik Polymarket-deadlines (ladders als "voor december / maart / juni") als aanwijzing, niet als bewijs.
2. benchmarks: kies 1-3 instrumenten UITSLUITEND uit de lijst die het thema het zuiverst weerspiegelen,
   met direction +1 (thema duwt omhoog) of -1 (thema duwt omlaag). Alleen nodig als needs_benchmarks true is.
3. polymarket_slugs: welke van de aangeboden markten gaan echt over de kern van dit thema (mag leeg zijn).
4. resolved: is het thema voorbij als marktdrijver? Een afgewikkeld Polymarket-contract betekent NIET automatisch
   dat de marktimpact voorbij is (een gesloten Straat van Hormuz blijft relevant voor olie). Alleen true als het nieuws
   vooral terugblikt en er geen nieuwe ontwikkelingen of doorwerking meer zijn.
5. mutation (alleen als mutation_check true is): heeft deze plotselinge gebeurtenis een blijvend knelpunt veroorzaakt
   (toeleveringsketen, monetair, regelgeving) dat maanden doorwerkt? Zo ja: mutate true, nieuwe titel en driver
   die de structurele fase beschrijven.
6. driver_update: alleen als de these aantoonbaar verouderd is. Houd de identiteit van het topic gelijk.
7. overlaps_with: slugs van andere topics die in wezen hetzelfde thema meten (dubbeltelling).
Schrijf alle toelichting in het Nederlands. Antwoord uitsluitend via de tool."""


def reasoning_schema(benchmark_symbols: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "horizon_class": {"type": "string", "enum": HORIZONS},
            "horizon_reason": {"type": "string"},
            "benchmarks": {
                "type": "array", "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "enum": benchmark_symbols},
                        "direction": {"type": "integer", "enum": [1, -1]},
                        "why": {"type": "string"},
                    },
                    "required": ["symbol", "direction", "why"],
                },
            },
            "polymarket_slugs": {"type": "array", "items": {"type": "string"}},
            "resolved": {"type": "boolean"},
            "resolved_reason": {"type": "string"},
            "mutation": {
                "type": "object",
                "properties": {
                    "mutate": {"type": "boolean"}, "new_title": {"type": "string"},
                    "new_driver": {"type": "string"},
                    "new_keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                    "reason": {"type": "string"},
                },
                "required": ["mutate", "reason"],
            },
            "driver_update": {
                "type": "object",
                "properties": {
                    "update": {"type": "boolean"}, "new_driver": {"type": "string"},
                    "new_keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                    "reason": {"type": "string"},
                },
                "required": ["update", "reason"],
            },
            "overlaps_with": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["horizon_class", "horizon_reason", "benchmarks", "polymarket_slugs", "resolved",
                     "resolved_reason", "mutation", "driver_update", "overlaps_with"],
    }


# --- Stock Search -------------------------------------------------------------------------

STOCK_CANDIDATES_SYSTEM = """Je zoekt beursgenoteerde bedrijven (of grondstof-ETF's) die blootstaan aan een beleggingsthema.
Gebruik web search om omzetblootstelling te onderbouwen met bronnen (jaarverslag, investor presentation, 10-K, segmentrapportage).

Zoek per rol ongeveer {n} kandidaten:
- pure_play: kernactiviteit is het thema zelf.
- pick_and_shovel: kritieke toeleverancier van een knelpunt (hardware, software, grondstof) met prijszettingsmacht.
- second_order: groter of gediversifieerd bedrijf dat meeprofiteert.

Per kandidaat:
- symbol: het Yahoo Finance-tickersymbool (bijv. "PWR", "ASML.AS", "RR.L", "6501.T"). Verzin geen tickers.
- exposure_pct: geschat aandeel van de omzet dat aan het thema hangt (0-100). Gebruik een getal uit een bron waar mogelijk.
  null alleen als het bedrijf (nog) geen omzet heeft; zet dan pre_revenue true.
- source_url: de bron voor de blootstelling. evidence: één zin met het getal of citaat.
Sluit penny stocks, SPAC's zonder omzet en illiquide noteringen uit.
Roep als laatste stap de tool submit_candidates aan met alle kandidaten."""

STOCK_CANDIDATES_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"}, "name": {"type": "string"},
                    "role": {"type": "string", "enum": ROLES},
                    "exposure_pct": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
                    "pre_revenue": {"type": "boolean"},
                    "source_url": {"type": "string"}, "evidence": {"type": "string"},
                },
                "required": ["symbol", "name", "role", "exposure_pct", "pre_revenue", "source_url", "evidence"],
            },
        }
    },
    "required": ["candidates"],
}

STOCK_SELECT_SYSTEM = """Je stelt het definitieve mandje samen voor een beleggingsthema.
Je krijgt kandidaten die al door een technische filter kwamen (bestaan, >= 60 dagen koershistorie, voldoende liquide),
met hun geschatte omzetblootstelling, bron, volatiliteit en dagomzet.

- Kies {min_n} tot {max_n} namen die samen het thema zo zuiver mogelijk afdekken over de keten.
- Spreid over deelniches: maximaal 2 namen per deelniche (bijv. niet drie SMR-bedrijven).
- exposure_pct is verplicht voor elk bedrijf met omzet: neem het getal uit de kandidatenlijst over of corrigeer het
  als de bron dat rechtvaardigt; de tier wordt hierna hard uit dit getal berekend
  (>= 50% pure play, 20-50% pick-and-shovel, < 20% second order), dus wees precies en eerlijk.
- rationale: één Nederlandse zin waarom deze naam erin zit.
Antwoord uitsluitend via de tool."""

STOCK_SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "members": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "subniche": {"type": "string"},
                    "exposure_pct": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
                    "pre_revenue": {"type": "boolean"},
                    "rationale": {"type": "string"},
                },
                "required": ["symbol", "subniche", "exposure_pct", "pre_revenue", "rationale"],
            },
        },
        "excluded": {
            "type": "array",
            "items": {"type": "object", "properties": {"symbol": {"type": "string"}, "reason": {"type": "string"}},
                      "required": ["symbol", "reason"]},
        },
    },
    "required": ["members", "excluded"],
}
