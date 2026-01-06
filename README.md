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

- State: senaste händelsens rubrik ("name")
- Attributes:
  - `latest` (dict) med `name`, `url`, `datetime`, `type`, `location`, `matched_areas`
  - `events` (lista, max `max_items`) med rubriker + länkar
  - `count` (antal matchande händelser i tidsfönstret)

## Lovelace (Dashboard) – template/markdown card

### Visa senaste rubriken + klickbar länk

```yaml
type: markdown
title: Polisen (senaste)
content: >
  {% set e = state_attr('sensor.polisen_events', 'latest') %}
  {% if e %}
  [{{ e.name }}]({{ e.url }})  
  {{ e.location.name }} • {{ e.type }} • {{ e.datetime }}

  Matchade: {{ (e.matched_areas | default([])) | join(', ') }}
  {% else %}
  Inga matchande händelser senaste {{ state_attr('sensor.polisen_events', 'hours') or 24 }} timmar.
  {% endif %}
```

### Visa en lista med flera händelser (rubrik + länk)

```yaml
type: markdown
title: Polisen (lista)
content: >
  {% set events = state_attr('sensor.polisen_events', 'events') | default([]) %}
  {% if events | length == 0 %}
  Inga matchande händelser.
  {% else %}
  {% for e in events %}
  - [{{ e.name }}]({{ e.url }}) ({{ e.location.name }})
  {% endfor %}
  {% endif %}
```

## Exempel

Om du sätter Area till `Hallands län` får du länets händelser.

## Notering

Polisens API saknar ett separat fält för t.ex. kommun/län-kod i svaret; detta bygger på textmatchning mot `location.name` från API:t.

Det innebär t.ex. att:
- En händelse som har `location.name = "Malmö"` matchar **Malmö**, men matchar inte automatiskt **Skåne län** (även om Malmö ligger i Skåne).
- För att täcka en hel region idag behöver du lägga in flera områden i Area-fältet, t.ex. `Malmö / Eslöv / Löberöd`.

För transparens sätter integrationen även `matched_areas` per event så du kan se exakt vilken del av din Area-lista som gav träff.
