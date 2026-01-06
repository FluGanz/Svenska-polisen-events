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
  - Area (t.ex. `Malmö`, `Lund` – det som matchas är text i `location.name` från Polisens API)
  - Match mode: `contains` (standard) eller `exact`
  - Hours: hur långt bak i tiden som ska räknas (standard 24)
  - Max items: max antal händelser som läggs i attribut (standard 5)

## Sensor

- State: senaste händelsens rubrik ("name")
- Attributes:
  - `latest` (dict) med `name`, `url`, `datetime`, `type`, `location`, `matched_areas`
  - `events` (lista, max `max_items`) med rubriker + länkar
  - `count` (antal matchande händelser i tidsfönstret)

## Lovelace (Dashboard) – kort

Här är två snyggare kort-alternativ (custom cards) som kan visa senaste rubriken och öppna polisens länk vid klick.

### Snyggare kort (Mushroom – `custom:mushroom-template-card`)

Kräver att du installerat Mushroom via HACS.

Obs: Mushroom Template Card stödjer templating i texter, men `tap_action.url_path` templatas inte alltid av Home Assistant-frontend. Om du lägger en template där kan den skickas vidare som rå text (och du hamnar på en `/lovelace/...`-URL som ser ut som din template). För en klickbar länk till senaste händelsen rekommenderas därför button-card-exemplet nedan.

```yaml
type: custom:mushroom-template-card
entity: sensor.polisen_events
primary: >-
  {% set e = state_attr('sensor.polisen_events', 'latest') %}
  {{ e.name if e else 'Inga matchande händelser' }}
secondary: >-
  {% set e = state_attr('sensor.polisen_events', 'latest') %}
  {% if e %}
    {{ e.location.name }} • {{ e.type }} • {{ e.datetime }}
  {% else %}
    Senaste {{ state_attr('sensor.polisen_events', 'hours') or 24 }} timmar.
  {% endif %}
icon: mdi:police-badge
multiline_secondary: true
tap_action:
  action: more-info
hold_action:
  action: more-info
```

### Snyggare kort (button-card – `custom:button-card`)

Kräver att du installerat button-card via HACS.

```yaml
type: custom:button-card
entity: sensor.polisen_events
name: >
  [[[ return states['sensor.polisen_events'].state || 'Polisen'; ]]]
label: >
  [[[ 
    const e = states['sensor.polisen_events'].attributes?.latest;
    if (!e) return 'Inga matchande händelser';
    const where = e.location?.name || '';
    const when = e.datetime || '';
    return `${where} • ${when}`;
  ]]]
show_label: true
icon: mdi:police-badge
tap_action:
  action: url
  url_path: >
    [[[ return states['sensor.polisen_events'].attributes?.latest?.url || ''; ]]]
hold_action:
  action: more-info
```

## Exempel

Om du sätter Area till `Malmö` får du händelser där Polisens API returnerar `location.name` som innehåller “Malmö”.

Exempel med flera områden:

- `Malmö / Lund / Eslöv`

## Notering

Polisens API saknar ett separat fält för t.ex. kommun/län-kod i svaret; detta bygger på textmatchning mot `location.name` från API:t.

Det innebär t.ex. att:
- En händelse som har `location.name = "Malmö"` matchar **Malmö**, men matchar inte automatiskt **Skåne län** (även om Malmö ligger i Skåne).
- För att täcka en hel region idag behöver du lägga in flera områden i Area-fältet, t.ex. `Malmö / Eslöv / Löberöd`.

För transparens sätter integrationen även `matched_areas` per event så du kan se exakt vilken del av din Area-lista som gav träff.
