from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_URL,
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


@dataclass(frozen=True, kw_only=True)
class PolisenSensorDescription(SensorEntityDescription):
    pass


DESCRIPTION = PolisenSensorDescription(
    key="events",
    name="Polisen events",
    icon="mdi:police-badge",
)


def _parse_dt(dt_str: str) -> datetime | None:
    dt_str = (dt_str or "").strip()
    if not dt_str:
        return None

    # Polisen API uses "YYYY-MM-DD HH:MM:SS +01:00" which is ISO-ish.
    # Python supports this with fromisoformat.
    try:
        return datetime.fromisoformat(dt_str)
    except ValueError:
        return None


def _matches_area(location_name: str, area: str, match_mode: str) -> bool:
    location_name_cf = (location_name or "").strip().casefold()
    area_cf = (area or "").strip().casefold()

    if not area_cf:
        return True
    if not location_name_cf:
        return False

    if match_mode == "exact":
        return location_name_cf == area_cf

    # default: contains
    return area_cf in location_name_cf


def _parse_areas(raw: str) -> list[str]:
    # Allow multiple areas: "Malmö / Eslöv / Skåne län" or "Malmö,Eslöv" etc.
    raw = (raw or "").strip()
    if not raw:
        return []

    parts: list[str] = [raw]
    for delim in ["/", ",", ";", "|", "\n"]:
        next_parts: list[str] = []
        for p in parts:
            next_parts.extend(p.split(delim))
        parts = next_parts

    cleaned = [p.strip() for p in parts]
    return [p for p in cleaned if p]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    area = entry.data.get(CONF_AREA, "")
    match_mode = entry.data.get(CONF_MATCH_MODE, DEFAULT_MATCH_MODE)
    hours = int(entry.data.get(CONF_HOURS, DEFAULT_HOURS))
    max_items = int(entry.data.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS))
    areas = _parse_areas(str(area))

    session = async_get_clientsession(hass)

    async def _async_update_data() -> dict[str, Any]:
        try:
            async with session.get(API_URL, timeout=15) as resp:
                if resp.status != 200:
                    raise UpdateFailed(f"HTTP {resp.status}")
                data = await resp.json()
                if not isinstance(data, list):
                    raise UpdateFailed("API returned non-list JSON")
        except Exception as err:
            raise UpdateFailed(str(err)) from err

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)

        filtered: list[dict[str, Any]] = []
        for ev in data:
            if not isinstance(ev, dict):
                continue
            loc = ev.get("location")
            loc_name = ""
            if isinstance(loc, dict):
                loc_name = str(loc.get("name") or "")

            if areas:
                if not any(_matches_area(loc_name, a, str(match_mode)) for a in areas):
                    continue
            else:
                if not _matches_area(loc_name, str(area), str(match_mode)):
                    continue

            dt = _parse_dt(str(ev.get("datetime") or ""))
            if dt is None:
                continue

            # normalize naive datetimes as local? safest is to skip
            if dt.tzinfo is None:
                continue

            if dt.astimezone(timezone.utc) < cutoff:
                continue

            filtered.append(ev)

        filtered.sort(key=lambda e: str(e.get("datetime") or ""), reverse=True)

        trimmed: list[dict[str, Any]] = filtered[: max(0, max_items)]
        latest = trimmed[0] if trimmed else None

        def _to_public_event(e: dict[str, Any]) -> dict[str, Any]:
            url = e.get("url")
            if isinstance(url, str) and url.startswith("/"):
                url = "https://polisen.se" + url

            return {
                "id": e.get("id"),
                "datetime": e.get("datetime"),
                "name": e.get("name"),
                "type": e.get("type"),
                "url": url,
                "location": (e.get("location") or {}),
            }

        return {
            "count": len(filtered),
            "latest": _to_public_event(latest) if isinstance(latest, dict) else None,
            "events": [_to_public_event(e) for e in trimmed if isinstance(e, dict)],
        }

    coordinator = DataUpdateCoordinator(
        hass,
        logger=LOGGER,
        name=f"Polisen Events ({area})",
        update_method=_async_update_data,
        update_interval=timedelta(minutes=5),
    )

    await coordinator.async_config_entry_first_refresh()

    async_add_entities([PolisenEventsSensor(entry, coordinator)])


class PolisenEventsSensor(SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, coordinator: DataUpdateCoordinator[dict[str, Any]]):
        self.entity_description = DESCRIPTION
        self._entry = entry
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_events"

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        return self._coordinator.last_update_success

    @property
    def native_value(self) -> int | None:
        data = self._coordinator.data
        if not isinstance(data, dict):
            return None
        latest = data.get("latest")
        if isinstance(latest, dict):
            name = latest.get("name")
            if isinstance(name, str) and name.strip():
                return name
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._coordinator.data
        if not isinstance(data, dict):
            return {}

        latest = data.get("latest") if isinstance(data.get("latest"), dict) else None

        return {
            "area": self._entry.data.get(CONF_AREA),
            "match_mode": self._entry.data.get(CONF_MATCH_MODE),
            "hours": self._entry.data.get(CONF_HOURS),
            "max_items": self._entry.data.get(CONF_MAX_ITEMS),
            "count": data.get("count", 0),
            "latest": latest,
            "events": data.get("events", []),
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(self.async_write_ha_state))

    async def async_update(self) -> None:
        await self._coordinator.async_request_refresh()
