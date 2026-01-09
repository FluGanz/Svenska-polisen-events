from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries

from .const import (
    CONF_AREA,
    CONF_HOURS,
    CONF_MATCH_MODE,
    CONF_MAX_ITEMS,
    DEFAULT_HOURS,
    DEFAULT_MATCH_MODE,
    DEFAULT_MAX_ITEMS,
    DOMAIN,
)


class PolisenEventsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return PolisenEventsOptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            title = user_input.get(CONF_AREA) or "Polisen Events"
            return self.async_create_entry(title=title, data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_AREA, default=""): str,
                vol.Required(CONF_MATCH_MODE, default=DEFAULT_MATCH_MODE): vol.In(
                    {
                        "contains": "contains",
                        "exact": "exact",
                    }
                ),
                vol.Required(CONF_HOURS, default=DEFAULT_HOURS): vol.All(int, vol.Range(min=1, max=168)),
                vol.Required(CONF_MAX_ITEMS, default=DEFAULT_MAX_ITEMS): vol.All(int, vol.Range(min=0, max=50)),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


class PolisenEventsOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    @staticmethod
    def _as_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**(self.config_entry.data or {}), **(self.config_entry.options or {})}

        area_default = str(current.get(CONF_AREA) or "")
        match_mode_default = str(current.get(CONF_MATCH_MODE) or DEFAULT_MATCH_MODE)
        if match_mode_default not in ("contains", "exact"):
            match_mode_default = DEFAULT_MATCH_MODE

        schema = vol.Schema(
            {
                vol.Required(CONF_AREA, default=area_default): str,
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
                ): vol.All(int, vol.Range(min=1, max=168)),
                vol.Required(
                    CONF_MAX_ITEMS,
                    default=self._as_int(current.get(CONF_MAX_ITEMS), DEFAULT_MAX_ITEMS),
                ): vol.All(int, vol.Range(min=0, max=50)),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
