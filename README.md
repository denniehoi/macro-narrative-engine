# Macro Narrative Engine

Elke ochtend bepaalt dit systeem welke macro-thema's de beurs domineren, of ze goed genoeg zijn om in te beleggen (maximaal 6), en welke aandelen daarbij horen met welk gewicht. Het resultaat staat in een dashboard met login. Orders plaats je zelf: het systeem adviseert alleen.

## Wat er per dag gebeurt

1. **Goedkeuringen verwerken.** Open verzoeken verlopen na 48 uur. Goedgekeurde shocks en merges worden uitgevoerd.
2. **Meten.** Per gevolgd topic één artikel-zoekactie bij newsAPI.ai (1 token): het volume plus 30 artikelen. Haiku beoordeelt welk deel echt over de these gaat (alignment).
3. **Discovery.** Eén event-zoekactie (5 tokens) haalt de grootste events van gisteren op. Haiku kiest kandidaten met MMP ≥ 70. Structurele thema's gaan naar On Deck, shocks naar de shock-radar. Een nieuw topic krijgt eenmalig een basislijn van 30 dagen (5 tokens).
4. **Koersen.** yfinance, met de database als cache en terugval.
5. **Reasoning.** Sonnet bepaalt de horizon en kiest 1–3 benchmarks uit een vaste lijst (`config/benchmarks.yaml`, vast per topic-versie). Ook beoordeelt Sonnet de resolutie, de shock-mutatie en overlap met andere topics. Dit gebeurt alleen voor Active topics, nieuwe topics en kandidaten in de buurt van de drempel.
6. **Score en gates.** Zie hieronder.
7. **Levenscyclus.** Uitstappen, instappen (met Stock Search) en opruimen.
8. **Allocatie.** Topicgewichten (de pie chart), doelgewichten per ticker en het rebalance-besluit.

## Rekenregels

**Narratief.** R volgt een genormaliseerde EMA (halfwaardetijd 10 dagen, shocks 3 dagen) van `alignment × ln(1 + artikelen)`. Het evenwicht is gelijk aan de input, dus de keuze van de decay geeft geen bias. Ontbrekende data houdt R vast in plaats van naar nul te trekken.

**Harde gates (alle vier).**
- These: gemiddelde alignment over 7 dagen ≥ 0,6.
- Horizon ≥ 3 maanden.
- Mandje: minstens 4 geldige tickers.
- Niet opgelost.

**Investeerbaarheid.** I = 100 × (N × M × D × (1 − P))^¼, met een ondergrens van 0,05 per component.

| Component | Meting |
|---|---|
| N | Percentiel van R tegenover alle topics over de laatste 90 dagen |
| M | 20-daagse beweging van de benchmarks in σ-eenheden in de verwachte richting, z/2 begrensd op 0–1. Aandelen-ETF's worden gemeten ten opzichte van ACWI. |
| D | 3–6 mnd = 0,6 · 6–12 mnd = 0,8 · > 12 mnd = 1,0 |
| P | Ingeprijsd-meter: de beweging over 120 handelsdagen in σ. Bij 1σ is niets ingeprijsd, bij 4σ alles. |

**In en uit.**
- In: I ≥ 60 drie dagen op rij, of een goedgekeurde shock.
- Uit: I < 45, of een gate die 3 dagen faalt.
- Een uitdager moet het laagste Active topic met 10% verslaan.
- Topicgewicht is evenredig met I, met een maximum van 25% per topic. De rest staat in cash.

**Shocks.** Een shock gaat eerst naar de radar. Bij een volumepiek (z ≥ 3) of high impact, plus alignment ≥ 0,6, verschijnt een goedkeuringsverzoek in het dashboard. Na toelating volgen 7 dagen bescherming. Vanaf dag 5 vraagt Sonnet of de shock een blijvend knelpunt heeft veroorzaakt. Zo ja, dan wordt het topic structureel (nieuwe versie, nieuwe benchmarks, nieuw mandje).

**Stock Search** (aparte fase, maximaal 3 per run). Het zoeken gebeurt bij promotie, mutatie, een nieuwe topic-versie en als een mandje ouder is dan 30 dagen.
1. Sonnet met web search stelt per rol kandidaten voor, met een bron voor de omzetblootstelling.
2. Python filtert: de ticker bestaat, heeft ≥ 60 dagen historie en ≥ $5 mln dagomzet, en zit niet al in een ander mandje.
3. Sonnet kiest 4–8 namen.

De tier volgt hard uit het omzetaandeel: ≥ 50% is pure play, 20–50% is pick-and-shovel, < 20% is second order. Pre-revenue bedrijven tellen als pure play met een cap van 10%.

**Weging.**
- Tierbudgetten 40/40/20. Een lege tier wordt naar rato over de andere verdeeld.
- Binnen een tier inverse volatiliteit, met σ minimaal 20%.
- Paren met een correlatie boven 0,85 delen één gewicht.
- Maximaal 30% per ticker in een mandje en 10% per ticker in de portefeuille.
- Een positie is maximaal 1% van de dagomzet.
- Rebalance bij een nieuwe maand, een gewijzigde samenstelling of drift boven 5 procentpunt.

Alle getallen staan in `config/settings.yaml`. Het zijn startwaarden.

## Afwijkingen van het ontwerp

- **Temperature.** De huidige Anthropic-SDK en -modellen hebben geen temperature-parameter meer. Consistentie komt van geforceerde, schema-gebonden uitvoer en discrete klassen in plaats van vrije getallen.
- **Polymarket.** Polymarket is nooit een veto. Een afgewikkeld contract beëindigt een thema niet automatisch: Sonnet beoordeelt de resolutie, met de contractstatus als input. Markten onder $10.000 liquiditeit tellen niet mee.
- **Merges.** Overlap tussen On Deck-topics wordt automatisch samengevoegd. Zit er een Active topic bij, dan volgt eerst een goedkeuringsverzoek.

## Installatie

De Supabase-database is al aangemaakt (`macro-narrative`, Frankfurt) met tabellen, toegangsregels en de goedkeuringsfunctie (`supabase/migrations/`).

1. **Repo.** Maak een privé-repository op GitHub en push deze map.
2. **Secrets** (Settings → Secrets and variables → Actions):
   - Variable `SUPABASE_URL` = `https://cmmikebzpakirfowzqjp.supabase.co`
   - Secret `SUPABASE_SECRET_KEY`: in Supabase onder Project Settings → API Keys, de *secret key* (of de legacy `service_role`)
   - Secret `NEWSAPI_KEY`
   - Secret `ANTHROPIC_API_KEY`
3. **Controle.** Draai de workflow *Controle sleutels* onder Actions. Die test alle koppelingen en kost geen nieuws-tokens.
4. **Eerste run.** Start de workflow *Dagelijkse run* met de hand. Daarna draait hij elke ochtend vanzelf.
5. **Dashboard.** GitHub Pages instellen (Settings → Pages → Source: GitHub Actions) publiceert `dashboard/` bij elke wijziging. Pages vanuit een privé-repo vraagt een betaald GitHub-abonnement. Alternatieven zijn `dashboard/index.html` direct openen in de browser, of de map uploaden naar Netlify of Cloudflare Pages. De pagina bevat alleen de publieke sleutel; de data is afgeschermd door login en row level security.
6. **Toegang.** Gebruik `python -m engine add-user --email naam@domein.nl --name "Naam"`, met `--role viewer` voor alleen meekijken. Dit maakt een account met een tijdelijk wachtwoord dat de gebruiker in het dashboard kan wijzigen.

Met `dashboard/index.html?demo` zie je het dashboard met gesimuleerde data.

## Lokaal

```bash
pip install -r requirements.txt
python -m pytest -q             # rekenkern, weging, pipeline end-to-end
python -m engine simulate       # synthetische maand, zonder sleutels; ververst dashboard/demo-data.json
```

## IJken

Vanaf dag 1 bewaart het systeem alle ruwe meetwaarden (`topic_daily`), koersen (`price_bars`), versies (`topic_versions`) en momentopnames. Na 2–3 maanden kun je daarmee de drempels (60/45), de halfwaardetijd en de P-grenzen backtesten in plaats van gokken.

## Kosten en limieten

- **newsAPI.ai.** Er is een harde limiet van 60 tokens per dag en 4.500 per maand. Een normale dag kost 25–40 tokens.
- **Anthropic.** Tokens en web-zoekacties staan per run in het dashboard. Stock Search is begrensd op 3 rondes per dag.
- **GitHub.** In een publieke repository zet GitHub geplande workflows uit na 60 dagen zonder activiteit.
