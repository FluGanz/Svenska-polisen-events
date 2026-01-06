# Home Assistant – Polisen Events (Custom Integration)

Detta är en enkel Home Assistant "custom integration" som hämtar öppna händelser från Polisens API (`https://polisen.se/api/events`) och exponerar en sensor filtrerad på stad/kommun/län.

## Installation (manuell)

1. Kopiera mappen `custom_components/polisen_events` till din Home Assistant:
   - `config/custom_components/polisen_events`
2. Starta om Home Assistant.

## Installation (GitHub + HACS)

1. Skapa ett GitHub-repo av mappen `homeassistant-polisen`.
2. Lägg till repot som "Custom repository" i HACS (Integration).
3. Installera och starta om Home Assistant.

## Konfiguration

### Via UI (Config Flow)

- Gå till **Settings → Devices & services → Add integration**
- Sök efter **Polisen Events**
- Fyll i:
  - Area (t.ex. `Malmö`, `Lund`, `Stockholms län`)
  - Match mode: `contains` (standard) eller `exact`
  - Hours: hur långt bak i tiden som ska räknas (standard 24)
  - Max items: max antal händelser som läggs i attribut (standard 5)

## Sensor

- State: antal matchande händelser inom tidsfönstret
- Attributes:
  - `events` (lista, max `max_items`)

## Exempel

Om du sätter Area till `Hallands län` får du länets händelser.

## Notering

Polisens API saknar ett separat fält för "kommun" i svaret; detta bygger på textmatchning mot `location.name` från API:t.
