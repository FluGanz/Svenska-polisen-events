from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import html
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
from homeassistant.util import dt as dt_util
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


def _day_priority_group(dt: datetime, today: date, yesterday: date) -> int:
    """Return sort group for an event datetime.

    0: today, 1: yesterday, 2: older (still within cutoff).
    """

    local_day = dt_util.as_local(dt).date()
    if local_day == today:
        return 0
    if local_day == yesterday:
        return 1
    return 2


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

_DETAIL_PREAMBLE_RE = re.compile(r"<p\s+class=\"preamble\"[^>]*>\s*(.*?)\s*</p>", re.IGNORECASE | re.DOTALL)
_DETAIL_BODY_RE = re.compile(
    r"<div\s+class=\"text-body\s+editorial-html\"[^>]*>\s*(.*?)\s*</div>",
    re.IGNORECASE | re.DOTALL,
)
_DETAIL_SENDER_RE = re.compile(
    r"Text\s+av\s*</span>\s*<br\s*/?>\s*<span[^>]*>\s*(.*?)\s*</span>",
    re.IGNORECASE | re.DOTALL,
)
_DETAIL_PUBLISHED_DISPLAY_RE = re.compile(
    r"<time[^>]*class=\"date\"[^>]*>\s*(.*?)\s*</time>",
    re.IGNORECASE | re.DOTALL,
)
_DETAIL_PUBLISHED_ISO_RE = re.compile(
    r"<time[^>]*class=\"date\"[^>]*datetime=\"([^\"]+)\"",
    re.IGNORECASE | re.DOTALL,
)

_DETAILS_TTL = timedelta(hours=12)
_DETAILS_MAX_CONCURRENCY = 4


def _format_dt_with_space_before_offset(dt: datetime) -> str:
    s = dt.isoformat(sep=" ")
    if len(s) >= 6 and s[-6] in ("+", "-"):
        return s[:-6] + " " + s[-6:]
    return s


def _normalize_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    u = url.strip()
    if not u:
        return None
    if u.startswith("/"):
        return "https://polisen.se" + u
    return u


def _html_to_text(fragment: str) -> str:
    s = fragment or ""
    s = re.sub(r"<\s*br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</\s*p\s*>", "\n\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<\s*p[^>]*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _parse_event_details_from_html(page_html: str) -> dict[str, Any]:
    """Parse subtitle/body/sender/published_display from a Polisen event page HTML."""

    result: dict[str, Any] = {}
    if not page_html:
        return result

    m = _DETAIL_PREAMBLE_RE.search(page_html)
    if m:
        subtitle = _html_to_text(m.group(1))
        if subtitle:
            result["subtitle"] = subtitle

    m = _DETAIL_BODY_RE.search(page_html)
    if m:
        body = _html_to_text(m.group(1))
        if body:
            result["body"] = body

    m = _DETAIL_SENDER_RE.search(page_html)
    if m:
        sender = _html_to_text(m.group(1))
        if sender:
            result["sender"] = sender

    m = _DETAIL_PUBLISHED_DISPLAY_RE.search(page_html)
    if m:
        published_display = _html_to_text(m.group(1))
        if published_display:
            result["published_display"] = published_display

    m = _DETAIL_PUBLISHED_ISO_RE.search(page_html)
    if m:
        published_iso = html.unescape(m.group(1)).strip()
        if published_iso:
            result["published_iso"] = published_iso

    return result


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
        parsed = datetime(
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

    # Handle year rollovers (e.g. title says "31 december" but publish is in January).
    if isinstance(fallback, datetime) and parsed > fallback + timedelta(days=30):
        try:
            parsed = parsed.replace(year=year - 1)
        except ValueError:
            return fallback

    # Polisen sometimes publishes after midnight about a late-night event.
    # If the parsed event time ends up *after* the publish/update time, shift it back.
    if isinstance(fallback, datetime) and parsed > fallback + timedelta(minutes=2):
        parsed = parsed - timedelta(days=1)

    return parsed


def _event_sort_key(ev: dict[str, Any]) -> datetime | None:
    """Return a UTC datetime used for sorting events newest-first.

    Prefer event time (händelsetid) if available; fall back to published/updated time.
    """

    dt = _parse_dt(str(ev.get("event_datetime") or ""))
    if dt is None or dt.tzinfo is None:
        dt = _parse_dt(str(ev.get("datetime") or ""))
    if dt is None or dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


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

    details_cache: dict[int, tuple[datetime, dict[str, Any]]] = {}
    details_sem = asyncio.Semaphore(_DETAILS_MAX_CONCURRENCY)

    async def _async_update_data() -> dict[str, Any]:
        async def _fetch(params: dict[str, str] | None) -> list[dict[str, Any]]:
            async with session.get(API_URL, params=params, timeout=15) as resp:
                if resp.status != 200:
                    raise UpdateFailed(f"HTTP {resp.status}")
                payload = await resp.json()
                if not isinstance(payload, list):
                    raise UpdateFailed("API returned non-list JSON")
                return [e for e in payload if isinstance(e, dict)]

        async def _fetch_details(url: str) -> dict[str, Any]:
            async with details_sem:
                async with session.get(url, timeout=15) as resp:
                    if resp.status != 200:
                        raise UpdateFailed(f"Details HTTP {resp.status}")
                    text = await resp.text()
            return _parse_event_details_from_html(text)

        def _to_public_event(e: dict[str, Any]) -> dict[str, Any]:
            url = _normalize_url(e.get("url"))

            return {
                "id": e.get("id"),
                # Polisen API "datetime" is publish/update time; keep for compatibility.
                "datetime": e.get("datetime"),
                "published": e.get("datetime"),
                "published_display": e.get("published_display"),
                # Event time (händelsetid) parsed from `name`.
                "event_datetime": e.get("event_datetime"),
                "name": e.get("name"),
                "summary": e.get("summary"),
                "subtitle": e.get("subtitle"),
                "body": e.get("body"),
                "sender": e.get("sender"),
                "type": e.get("type"),
                "url": url,
                "location": (e.get("location") or {}),
            }

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)
        today_local = dt_util.as_local(now).date()
        yesterday_local = today_local - timedelta(days=1)

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

            scored: list[tuple[int, float, dict[str, Any]]] = []
            for ev in result:
                published_dt = _parse_dt(str(ev.get("datetime") or ""))
                if published_dt is None or published_dt.tzinfo is None:
                    continue

                published_dt_utc = published_dt.astimezone(timezone.utc)
                if published_dt_utc < cutoff:
                    continue

                event_dt = _parse_event_dt_from_name(str(ev.get("name") or ""), published_dt)
                if event_dt is None or event_dt.tzinfo is None:
                    continue

                ev2 = dict(ev)
                ev2["event_datetime"] = _format_dt_with_space_before_offset(event_dt)

                # Prioritize by publish/update date (Polisen website uses "Uppdaterad …").
                group = _day_priority_group(published_dt, today_local, yesterday_local)
                scored.append((group, -published_dt_utc.timestamp(), ev2))

            scored.sort(key=lambda item: (item[0], item[1]))
            filtered = [ev for _group, _ts, ev in scored]

            today_events = [ev for group, _ts, ev in scored if group == 0]
            other_events = [ev for group, _ts, ev in scored if group != 0]
            remaining = max(0, max_items - len(today_events))
            trimmed = today_events + other_events[:remaining]

            # Enrich the visible events with details (subtitle/body/sender/published_display).
            # Keep this lightweight by caching per event id.
            now_utc = datetime.now(timezone.utc)

            async def _enrich_one(ev2: dict[str, Any]) -> None:
                ev_id = ev2.get("id")
                if not isinstance(ev_id, int):
                    return

                cached = details_cache.get(ev_id)
                if cached and (now_utc - cached[0]) < _DETAILS_TTL:
                    ev2.update(cached[1])
                    return

                url = _normalize_url(ev2.get("url"))
                if not url:
                    return

                try:
                    details = await _fetch_details(url)
                except Exception as err:  # noqa: BLE001
                    LOGGER.debug("Failed to fetch details for event %s: %s", ev_id, err)
                    return

                if details:
                    details_cache[ev_id] = (now_utc, details)
                    ev2.update(details)

            await asyncio.gather(*[_enrich_one(ev2) for ev2 in trimmed if isinstance(ev2, dict)])

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

        now = datetime.now(timezone.utc)
        today_local = dt_util.as_local(now).date()
        yesterday_local = today_local - timedelta(days=1)

        scored: list[tuple[int, float, dict[str, Any]]] = []
        for ev in events:
            published_dt = _parse_dt(str(ev.get("datetime") or ""))
            dt = published_dt if isinstance(published_dt, datetime) and published_dt.tzinfo else _event_sort_key(ev)
            if dt is None:
                continue
            group = _day_priority_group(dt, today_local, yesterday_local)
            scored.append((group, -dt.timestamp(), ev))
        scored.sort(key=lambda item: (item[0], item[1]))
        if not scored:
            return None
        latest = scored[0][2]
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

        now = datetime.now(timezone.utc)
        today_local = dt_util.as_local(now).date()
        yesterday_local = today_local - timedelta(days=1)

        scored: list[tuple[int, float, dict[str, Any]]] = []
        for ev in events:
            published_dt = _parse_dt(str(ev.get("datetime") or ""))
            dt = published_dt if isinstance(published_dt, datetime) and published_dt.tzinfo else _event_sort_key(ev)
            if dt is None:
                continue
            group = _day_priority_group(dt, today_local, yesterday_local)
            scored.append((group, -dt.timestamp(), ev))
        scored.sort(key=lambda item: (item[0], item[1]))

        max_items = int(cfg.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS))
        today_events = [ev for group, _ts, ev in scored if group == 0]
        other_events = [ev for group, _ts, ev in scored if group != 0]
        remaining = max(0, max_items - len(today_events))
        trimmed = today_events + other_events[:remaining]
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
