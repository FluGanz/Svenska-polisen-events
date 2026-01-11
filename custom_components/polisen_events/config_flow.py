from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_URL,
    CONF_AREA,
    CONF_HOURS,
    CONF_MATCH_MODE,
    CONF_MAX_ITEMS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_HOURS,
    DEFAULT_MATCH_MODE,
    DEFAULT_MAX_ITEMS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)


LOGGER = logging.getLogger(__name__)


_COUNTY_LOCATIONS: list[str] = [
    "Blekinge län",
    "Dalarnas län",
    "Gotlands län",
    "Gävleborgs län",
    "Hallands län",
    "Jämtlands län",
    "Jönköpings län",
    "Kalmar län",
    "Kronobergs län",
    "Norrbottens län",
    "Skåne län",
    "Stockholms län",
    "Södermanlands län",
    "Uppsala län",
    "Värmlands län",
    "Västerbottens län",
    "Västernorrlands län",
    "Västmanlands län",
    "Västra Götalands län",
    "Örebro län",
    "Östergötlands län",
]


def _split_areas(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(v).strip() for v in raw if str(v).strip()]

    text = str(raw).strip()
    if not text:
        return []

    parts: list[str] = [text]
    for delim in ["/", ",", ";", "|", "\n"]:
        next_parts: list[str] = []
        for p in parts:
            next_parts.extend(p.split(delim))
        parts = next_parts

    cleaned = [p.strip() for p in parts]
    return [p for p in cleaned if p]


def _join_areas(values: object) -> str:
    areas = _split_areas(values)
    return " / ".join(areas)


_POLISEN_LISTPAGE_URL = "https://polisen.se/aktuellt/polisens-nyheter/1/"


class _PolisenLocationDatalistParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: set[str] = set()
        self._in_datalist = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "datalist":
            attrs_map = dict(attrs)
            datalist_id = str(attrs_map.get("id") or "")
            self._in_datalist = datalist_id.startswith("datalist-")
            return

        if not self._in_datalist:
            return

        if tag == "option":
            attrs_map = dict(attrs)
            raw_value = attrs_map.get("value")
            if raw_value:
                value = unescape(str(raw_value)).strip()
                if value:
                    self.values.add(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "datalist":
            self._in_datalist = False


async def _async_get_location_suggestions(hass) -> list[str]:
    """Best-effort list to power a searchable dropdown.

    Polisen's website exposes the full municipality/county list as a <datalist> on
    the list page. We scrape that list (same list as their dropdown), cache it, and
    fall back to building suggestions from the current events feed if scraping fails.
    """

    cache = hass.data.setdefault(DOMAIN, {})
    cache_key = "location_suggestions"
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        ts = cached.get("ts")
        values = cached.get("values")
        if isinstance(ts, datetime) and isinstance(values, list):
            if datetime.now(timezone.utc) - ts < timedelta(hours=12):
                return [v for v in values if isinstance(v, str) and v.strip()]

    session = async_get_clientsession(hass)

    suggestions: set[str] = set(_COUNTY_LOCATIONS)

    try:
        async with session.get(_POLISEN_LISTPAGE_URL, timeout=20) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            html_text = await resp.text()

        parser = _PolisenLocationDatalistParser()
        parser.feed(html_text)
        parser.close()
        if parser.values:
            suggestions.update(parser.values)
    except Exception as err:  # noqa: BLE001
        LOGGER.debug("Failed to scrape Polisen location datalist: %s", err)

    if not suggestions:
        suggestions.update(_COUNTY_LOCATIONS)

    # Fallback: also include locations from the live events feed (can help if
    # Polisen changes the list page markup).
    try:
        async with session.get(API_URL, timeout=15) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            data = await resp.json()
            if not isinstance(data, list):
                raise RuntimeError("API returned non-list JSON")

        for ev in data:
            if not isinstance(ev, dict):
                continue
            loc = ev.get("location")
            if not isinstance(loc, dict):
                continue
            name = str(loc.get("name") or "").strip()
            if name:
                suggestions.add(name)
    except Exception as err:  # noqa: BLE001
        LOGGER.debug("Failed to build location suggestions from API: %s", err)

    values = sorted(suggestions, key=lambda s: s.casefold())
    cache[cache_key] = {"ts": datetime.now(timezone.utc), "values": values}
    return values


class PolisenEventsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    def _coerce_and_validate(user_input: dict) -> tuple[dict, dict[str, str]]:
        errors: dict[str, str] = {}

        data = dict(user_input or {})

        data[CONF_AREA] = _join_areas(data.get(CONF_AREA))

        match_mode = str(data.get(CONF_MATCH_MODE) or DEFAULT_MATCH_MODE)
        if match_mode not in ("contains", "exact"):
            errors[CONF_MATCH_MODE] = "invalid_match_mode"
        data[CONF_MATCH_MODE] = match_mode

        try:
            hours = int(data.get(CONF_HOURS, DEFAULT_HOURS))
        except (TypeError, ValueError):
            hours = DEFAULT_HOURS
            errors[CONF_HOURS] = "invalid_hours"
        if not (1 <= hours <= 168):
            errors[CONF_HOURS] = "invalid_hours"
        data[CONF_HOURS] = hours

        try:
            max_items = int(data.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS))
        except (TypeError, ValueError):
            max_items = DEFAULT_MAX_ITEMS
            errors[CONF_MAX_ITEMS] = "invalid_max_items"
        if not (0 <= max_items <= 50):
            errors[CONF_MAX_ITEMS] = "invalid_max_items"
        data[CONF_MAX_ITEMS] = max_items

        try:
            update_interval = int(data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        except (TypeError, ValueError):
            update_interval = DEFAULT_UPDATE_INTERVAL
            errors[CONF_UPDATE_INTERVAL] = "invalid_update_interval"
        if not (1 <= update_interval <= 60):
            errors[CONF_UPDATE_INTERVAL] = "invalid_update_interval"
        data[CONF_UPDATE_INTERVAL] = update_interval

        return data, errors

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return PolisenEventsOptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            data, errors = self._coerce_and_validate(user_input)
            if not errors:
                title = data.get(CONF_AREA) or "Polisen Events"
                return self.async_create_entry(title=title, data=data)
        else:
            errors = {}

        location_options = await _async_get_location_suggestions(self.hass)

        area_default_list = []
        area_selector = None
        if location_options:
            area_selector = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=location_options,
                    multiple=True,
                    custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_AREA, default=area_default_list): area_selector if area_selector else str,
                vol.Required(CONF_MATCH_MODE, default=DEFAULT_MATCH_MODE): vol.In(
                    {
                        "contains": "contains",
                        "exact": "exact",
                    }
                ),
                vol.Required(
                    CONF_HOURS,
                    default=DEFAULT_HOURS,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=168,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_MAX_ITEMS,
                    default=DEFAULT_MAX_ITEMS,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=50,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=DEFAULT_UPDATE_INTERVAL,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=60,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


class PolisenEventsOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    @staticmethod
    def _as_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _as_area_str(value) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple, set)):
            return " / ".join(str(v) for v in value if str(v).strip())
        return str(value)

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            data, errors = PolisenEventsConfigFlow._coerce_and_validate(user_input)
            if not errors:
                return self.async_create_entry(title="", data=data)
        else:
            errors = {}

        errors: dict[str, str] = {}
        try:
            current = {**(self._config_entry.data or {}), **(self._config_entry.options or {})}

            location_options = await _async_get_location_suggestions(self.hass)

            area_default_list = _split_areas(current.get(CONF_AREA))
            area_default_str = self._as_area_str(current.get(CONF_AREA))
            area_selector = None
            if location_options:
                area_selector = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=location_options,
                        multiple=True,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            match_mode_default = str(current.get(CONF_MATCH_MODE) or DEFAULT_MATCH_MODE)
            if match_mode_default not in ("contains", "exact"):
                match_mode_default = DEFAULT_MATCH_MODE

            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_AREA,
                        default=area_default_list if area_selector else area_default_str,
                    ): area_selector if area_selector else str,
                    vol.Required(
                        CONF_MATCH_MODE,
                        default=match_mode_default,
                    ): vol.In(
                        {
                            "contains": "contains",
                            "exact": "exact",
                        }
                    ),
                    vol.Required(
                        CONF_HOURS,
                        default=self._as_int(current.get(CONF_HOURS), DEFAULT_HOURS),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=168,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_MAX_ITEMS,
                        default=self._as_int(current.get(CONF_MAX_ITEMS), DEFAULT_MAX_ITEMS),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=50,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_UPDATE_INTERVAL,
                        default=self._as_int(current.get(CONF_UPDATE_INTERVAL), DEFAULT_UPDATE_INTERVAL),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=60,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to build options schema")
            errors["base"] = "unknown"
            schema = vol.Schema({vol.Required(CONF_AREA, default=""): str})

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
