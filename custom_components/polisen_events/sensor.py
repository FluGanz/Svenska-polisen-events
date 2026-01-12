from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import slugify

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


_SW_MONTHS: dict[str, int] = {
    "januari": 1,
    "februari": 2,
    "mars": 3,
    "april": 4,
    "maj": 5,
    "juni": 6,
    "juli": 7,
    "augusti": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}


_EVENT_TIME_RE = re.compile(r"^\s*(\d{1,2})\s+([A-Za-zÅÄÖåäö]+)\s+(\d{1,2})\.(\d{2})")


def _format_dt_with_space_before_offset(dt: datetime) -> str:
    s = dt.isoformat(sep=" ")
    if len(s) >= 6 and s[-6] in ("+", "-"):
        return s[:-6] + " " + s[-6:]
    return s


def _parse_event_dt_from_name(name: str, fallback: datetime | None) -> datetime | None:
    """Parse the event time (händelsetid) from Polisen's name field.

    Example: "12 januari 22.16, Mordbrand, Helsingborg".
    We use the year/tzinfo from the API datetime as fallback.
    """

    name = (name or "").strip()
    if not name:
        return fallback

    match = _EVENT_TIME_RE.match(name)
    if not match:
        return fallback

    day_s, month_s, hour_s, minute_s = match.groups()
    month = _SW_MONTHS.get(month_s.casefold())
    if not month:
        return fallback

    year = fallback.year if isinstance(fallback, datetime) else datetime.now().year
    tzinfo = fallback.tzinfo if isinstance(fallback, datetime) and fallback.tzinfo else timezone.utc

    try:
        return datetime(
            year,
            month,
            int(day_s),
            int(hour_s),
            int(minute_s),
            0,
            tzinfo=tzinfo,
        )
    except ValueError:
        return fallback


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
    cfg = {**(entry.data or {}), **(entry.options or {})}

    area = cfg.get(CONF_AREA, "")
    match_mode = cfg.get(CONF_MATCH_MODE, DEFAULT_MATCH_MODE)
    hours = int(cfg.get(CONF_HOURS, DEFAULT_HOURS))
    max_items = int(cfg.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS))
    update_interval = int(cfg.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
    areas = _parse_areas(str(area))

    requested_areas: list[str] = []
    seen = set()
    for a in areas:
        if not isinstance(a, str):
            continue
        a = a.strip()
        if not a:
            continue
        a_cf = a.casefold()
        if a_cf in seen:
            continue
        seen.add(a_cf)
        requested_areas.append(a)

    if not requested_areas:
        requested_areas = [""]

    session = async_get_clientsession(hass)

    async def _async_update_data() -> dict[str, Any]:
        async def _fetch(params: dict[str, str] | None) -> list[dict[str, Any]]:
            async with session.get(API_URL, params=params, timeout=15) as resp:
                if resp.status != 200:
                    raise UpdateFailed(f"HTTP {resp.status}")
                payload = await resp.json()
                if not isinstance(payload, list):
                    raise UpdateFailed("API returned non-list JSON")
                return [e for e in payload if isinstance(e, dict)]

        def _to_public_event(e: dict[str, Any]) -> dict[str, Any]:
            url = e.get("url")
            if isinstance(url, str) and url.startswith("/"):
                url = "https://polisen.se" + url

            return {
                "id": e.get("id"),
                # Polisen API "datetime" is publish/update time; keep for compatibility.
                "datetime": e.get("datetime"),
                "published": e.get("datetime"),
                # Event time (händelsetid) parsed from `name`.
                "event_datetime": e.get("event_datetime"),
                "name": e.get("name"),
                "summary": e.get("summary"),
                "type": e.get("type"),
                "url": url,
                "location": (e.get("location") or {}),
            }

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)

        # Important: The global endpoint is limited and can miss older events for a
        # specific municipality when there are many events nationwide.
        # Query Polisen with `locationname` per selected area and keep results separate.
        tasks: list[asyncio.Task[list[dict[str, Any]]]] = []
        for a in requested_areas:
            params = {"locationname": a} if a else None
            tasks.append(asyncio.create_task(_fetch(params)))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        by_area: dict[str, dict[str, Any]] = {}
        for a, result in zip(requested_areas, results, strict=False):
            if isinstance(result, Exception):
                LOGGER.warning("Failed to fetch events for area '%s': %s", a, result)
                by_area[a] = {"count": 0, "latest": None, "events": []}
                continue

            scored: list[tuple[datetime, dict[str, Any]]] = []
            for ev in result:
                published_dt = _parse_dt(str(ev.get("datetime") or ""))
                if published_dt is None or published_dt.tzinfo is None:
                    continue

                event_dt = _parse_event_dt_from_name(str(ev.get("name") or ""), published_dt)
                if event_dt is None or event_dt.tzinfo is None:
                    continue

                event_dt_utc = event_dt.astimezone(timezone.utc)
                if event_dt_utc < cutoff:
                    continue

                ev2 = dict(ev)
                ev2["event_datetime"] = _format_dt_with_space_before_offset(event_dt)
                scored.append((event_dt_utc, ev2))

            scored.sort(key=lambda item: item[0], reverse=True)
            filtered = [ev for _dt, ev in scored]
            trimmed = filtered[: max(0, max_items)]
            latest = trimmed[0] if trimmed else None

            by_area[a] = {
                "count": len(filtered),
                "latest": _to_public_event(latest) if isinstance(latest, dict) else None,
                "events": [_to_public_event(e) for e in trimmed if isinstance(e, dict)],
            }

        return {"by_area": by_area}

    coordinator = DataUpdateCoordinator(
        hass,
        logger=LOGGER,
        name="Polisen Events",
        update_method=_async_update_data,
        update_interval=timedelta(minutes=max(1, update_interval)),
    )

    await coordinator.async_config_entry_first_refresh()

    entities: list[SensorEntity] = [
        PolisenEventsAllSensor(entry, coordinator),
        *[PolisenEventsAreaSensor(entry, coordinator, a) for a in requested_areas],
    ]
    async_add_entities(entities)


class PolisenEventsAreaSensor(CoordinatorEntity[dict[str, Any]], SensorEntity):
    _attr_icon = "mdi:police-badge"

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        area: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = DESCRIPTION
        self._entry = entry
        self._area = (area or "").strip()

        area_slug = slugify(self._area) if self._area else "alla"
        self._attr_unique_id = f"{entry.entry_id}_events_{area_slug}"
        self._attr_name = f"Polis {self._area}" if self._area else "Polis (alla)"

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not isinstance(data, dict):
            return None
        by_area = data.get("by_area")
        if not isinstance(by_area, dict):
            return None
        bucket = by_area.get(self._area)
        if not isinstance(bucket, dict):
            return None
        latest = bucket.get("latest")
        if isinstance(latest, dict):
            name = latest.get("name")
            if isinstance(name, str) and name.strip():
                return name
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cfg = {**(self._entry.data or {}), **(self._entry.options or {})}

        data = self.coordinator.data
        by_area = data.get("by_area") if isinstance(data, dict) else None
        bucket = by_area.get(self._area) if isinstance(by_area, dict) else None
        if not isinstance(bucket, dict):
            bucket = {"count": 0, "latest": None, "events": []}

        latest = bucket.get("latest") if isinstance(bucket.get("latest"), dict) else None

        return {
            "area": self._area,
            "match_mode": cfg.get(CONF_MATCH_MODE),
            "hours": cfg.get(CONF_HOURS),
            "max_items": cfg.get(CONF_MAX_ITEMS),
            "update_interval": cfg.get(CONF_UPDATE_INTERVAL),
            "count": bucket.get("count", 0),
            "latest": latest,
            "events": bucket.get("events", []),
        }


class PolisenEventsAllSensor(CoordinatorEntity[dict[str, Any]], SensorEntity):
    _attr_icon = "mdi:police-badge"
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = DESCRIPTION
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_events"
        self._attr_name = "Polis (samlat)"

    @staticmethod
    def _flatten_events(by_area: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        total_count = 0
        events: list[dict[str, Any]] = []
        for area, bucket in by_area.items():
            if not isinstance(bucket, dict):
                continue
            count = bucket.get("count")
            if isinstance(count, int):
                total_count += count
            bucket_events = bucket.get("events")
            if isinstance(bucket_events, list):
                for ev in bucket_events:
                    if not isinstance(ev, dict):
                        continue
                    ev2 = dict(ev)
                    if area:
                        ev2["requested_area"] = area
                    events.append(ev2)
        return total_count, events

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not isinstance(data, dict):
            return None
        by_area = data.get("by_area")
        if not isinstance(by_area, dict):
            return None
        _count, events = self._flatten_events(by_area)

        scored: list[tuple[datetime, dict[str, Any]]] = []
        for ev in events:
            dt = _parse_dt(str(ev.get("datetime") or ""))
            if dt is None or dt.tzinfo is None:
                continue
            scored.append((dt.astimezone(timezone.utc), ev))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            return None
        latest = scored[0][1]
        name = latest.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cfg = {**(self._entry.data or {}), **(self._entry.options or {})}

        data = self.coordinator.data
        by_area = data.get("by_area") if isinstance(data, dict) else None
        if not isinstance(by_area, dict):
            return {
                "area": cfg.get(CONF_AREA),
                "match_mode": cfg.get(CONF_MATCH_MODE),
                "hours": cfg.get(CONF_HOURS),
                "max_items": cfg.get(CONF_MAX_ITEMS),
                "update_interval": cfg.get(CONF_UPDATE_INTERVAL),
                "count": 0,
                "latest": None,
                "events": [],
            }

        total_count, events = self._flatten_events(by_area)

        scored: list[tuple[datetime, dict[str, Any]]] = []
        for ev in events:
            dt = _parse_dt(str(ev.get("datetime") or ""))
            if dt is None or dt.tzinfo is None:
                continue
            scored.append((dt.astimezone(timezone.utc), ev))
        scored.sort(key=lambda item: item[0], reverse=True)

        max_items = int(cfg.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS))
        trimmed = [ev for _dt, ev in scored][: max(0, max_items)]
        latest = trimmed[0] if trimmed else None

        return {
            "area": cfg.get(CONF_AREA),
            "match_mode": cfg.get(CONF_MATCH_MODE),
            "hours": cfg.get(CONF_HOURS),
            "max_items": cfg.get(CONF_MAX_ITEMS),
            "update_interval": cfg.get(CONF_UPDATE_INTERVAL),
            "count": total_count,
            "latest": latest,
            "events": trimmed,
        }
