from __future__ import annotations

import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

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


LOGGER = logging.getLogger(__name__)


class PolisenEventsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    def _coerce_and_validate(user_input: dict) -> tuple[dict, dict[str, str]]:
        errors: dict[str, str] = {}

        data = dict(user_input or {})

        data[CONF_AREA] = str(data.get(CONF_AREA) or "")

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

        schema = vol.Schema(
            {
                vol.Required(CONF_AREA, default=""): str,
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

            area_default = self._as_area_str(current.get(CONF_AREA))
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
                }
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to build options schema")
            errors["base"] = "unknown"
            schema = vol.Schema({vol.Required(CONF_AREA, default=""): str})

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
