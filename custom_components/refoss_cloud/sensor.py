"""Refoss Cloud energy history sensors."""

from __future__ import annotations

import base64
from calendar import monthrange
import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import logging
import random
import socket
import ssl
import string
import struct
import time
from typing import Any

from aiohttp import ClientError
import voluptuous as vol

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_EMAIL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Config entry and YAML option keys.
CONF_CHANNELS = "channels"
CONF_READING_DAY = "reading_day"
CONF_UUID = "uuid"
READING_DAY_LAST = "last"

DEFAULT_NAME = "Refoss Cloud"
DEFAULT_SCAN_INTERVAL = timedelta(seconds=15)
DEFAULT_API_BASE = "https://iotx.refoss.net"
# Fixed salt used by the Refoss/Meross HTTP API signature.
SECRET = "23x17ahWarFH6w29"
SNAPSHOT_STORE_VERSION = 1
SNAPSHOT_STORE_KEY = "refoss_cloud_mconsume_snapshots"
SNAPSHOT_RETENTION_DAYS = 40
MONTH_RESET_SNAPSHOT_MAX_WH = 50
SENSOR_SLUG = "billing_month_energy"
SENSOR_LABEL = "Billing month energy"
TODAY_SENSOR_SLUG = "this_day_energy"
TODAY_SENSOR_LABEL = "This Day Energy"

CHANNEL_LABELS = {
    1: "A1",
    2: "B1",
    3: "C1",
    4: "A2",
    5: "B2",
    6: "C2",
}

# ElectricityX returns mConsume and instantaneous values in one response.
INSTANT_SENSOR_TYPES: dict[str, dict[str, Any]] = {
    "power": {
        "label": "Power",
        "device_class": SensorDeviceClass.POWER,
        "unit": "W",
    },
    "voltage": {
        "label": "Voltage",
        "device_class": SensorDeviceClass.VOLTAGE,
        "unit": "V",
    },
    "power_factor": {
        "label": "PF",
        "device_class": None,
        "unit": None,
    },
    "current": {
        "label": "Current",
        "device_class": SensorDeviceClass.CURRENT,
        "unit": "A",
    },
}

PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_UUID): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_CHANNELS, default=[1, 2, 3, 4, 5, 6]): vol.All(
            cv.ensure_list, [vol.Coerce(int)]
        ),
        vol.Optional(CONF_READING_DAY, default=24): vol.Any(
            READING_DAY_LAST, vol.All(vol.Coerce(int), vol.Range(min=1, max=31))
        ),
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): cv.time_period,
    }
)


@dataclass(slots=True)
class ChannelElectricity:
    """Current ElectricityX values for a Refoss channel."""

    # Raw Refoss units: Wh, mA, mV, mW, and PF as a float.
    mconsume_wh: int
    current_ma: int
    voltage_mv: int
    power_mw: int
    factor: float


@dataclass(slots=True)
class ChannelConsumption:
    """Recent ConsumptionH values for a Refoss channel."""

    # ConsumptionH.total matches the app's current-day net energy value.
    # data contains recent hourly-ish Wh rows used to fill missing yesterday hours.
    today_wh: int
    history_rows: int
    date_totals_wh: dict[date, int]
    date_hour_totals_wh: dict[tuple[date, int], int]
    latest_history_date: date | None


@dataclass(slots=True)
class ChannelSnapshot:
    """Stored mConsume baseline for one channel."""

    date: date
    mconsume_wh: int
    source: str


@dataclass(slots=True)
class ChannelData:
    """Energy and instantaneous values for a Refoss channel."""

    # net_wh is the billing-period net energy shown in HA.
    # It may be negative on channels with solar generation.
    net_wh: int
    today_wh: int
    current_mconsume_wh: int
    today_snapshot_wh: int | None
    today_snapshot_source: str
    completed_history_wh: int
    history_rows: int
    billing_source: str
    today_source: str
    today_history_rows: int
    yesterday_http_rows: int
    yesterday_filled_hours: list[int]
    yesterday_missing_hours: list[int]
    current_ma: int
    voltage_mv: int
    power_mw: int
    factor: float


@dataclass(slots=True)
class CompletedDailyHistory:
    """Cached completed daily history values for the billing period."""

    total_wh: int
    history_rows: int
    yesterday_http_rows: int
    yesterday_filled_hours: list[int]
    yesterday_missing_hours: list[int]


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    """Set up Refoss Cloud sensors from YAML."""

    await _async_setup_sensors(
        hass=hass,
        config=config,
        async_add_entities=async_add_entities,
        scan_interval=config[CONF_SCAN_INTERVAL],
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Refoss Cloud sensors from a config entry."""

    config = dict(entry.data)
    config.update(entry.options)
    await _async_setup_sensors(
        hass=hass,
        config=config,
        async_add_entities=async_add_entities,
        scan_interval=_scan_interval_from_config(config),
        entry=entry,
    )


async def _async_setup_sensors(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    scan_interval: timedelta,
    entry: ConfigEntry | None = None,
) -> None:
    """Set up Refoss Cloud sensors."""

    client = RefossCloudClient(
        session=async_get_clientsession(hass),
        email=config[CONF_EMAIL],
        password=config[CONF_PASSWORD],
        uuid=config[CONF_UUID],
    )
    # Preserve order while dropping duplicate channels.
    channels = [int(channel) for channel in dict.fromkeys(config[CONF_CHANNELS])]
    reading_day = _normalize_reading_day(config[CONF_READING_DAY])
    snapshot_store = MConsumeSnapshotStore(hass, config[CONF_UUID])
    await snapshot_store.async_load()
    polling_lock = asyncio.Lock()

    # One coordinator refreshes all channels and all entity types.
    coordinator: DataUpdateCoordinator[dict[int, ChannelData]] = DataUpdateCoordinator(
        hass,
        logger=_LOGGER,
        name=f"{DOMAIN}_{config[CONF_UUID]}",
        update_interval=scan_interval,
        update_method=lambda: _async_update_data(
            client, channels, reading_day, snapshot_store, polling_lock
        ),
    )
    remove_midnight_refresh = async_track_time_change(
        hass,
        lambda now: hass.add_job(
            _async_create_midnight_snapshots(
                coordinator,
                client,
                channels,
                snapshot_store,
                scan_interval,
                polling_lock,
            )
        ),
        hour=0,
        minute=0,
        second=0,
    )
    if entry is not None:
        entry.async_on_unload(remove_midnight_refresh)

    await coordinator.async_config_entry_first_refresh()

    entities: list[SensorEntity] = []
    for channel in channels:
        # Billing-period net energy.
        entities.append(
            RefossCloudEnergySensor(
                coordinator=coordinator,
                name=config[CONF_NAME],
                uuid=config[CONF_UUID],
                channel=channel,
                reading_day=reading_day,
            )
        )
        # Current-day net energy from mConsume minus the stored daily snapshot.
        entities.append(
            RefossCloudTodayEnergySensor(
                coordinator=coordinator,
                name=config[CONF_NAME],
                uuid=config[CONF_UUID],
                channel=channel,
            )
        )
        # Instantaneous values shown by the app.
        for sensor_type in INSTANT_SENSOR_TYPES:
            entities.append(
                RefossCloudInstantSensor(
                    coordinator=coordinator,
                    name=config[CONF_NAME],
                    uuid=config[CONF_UUID],
                    channel=channel,
                    sensor_type=sensor_type,
                )
            )

    async_add_entities(entities)


async def _async_update_data(
    client: RefossCloudClient,
    channels: list[int],
    reading_day: int | str,
    snapshot_store: MConsumeSnapshotStore,
    polling_lock: asyncio.Lock,
) -> dict[int, ChannelData]:
    """Fetch one billing-period snapshot for all configured channels."""

    async with polling_lock:
        return await _async_update_data_locked(
            client, channels, reading_day, snapshot_store
        )


async def _async_update_data_locked(
    client: RefossCloudClient,
    channels: list[int],
    reading_day: int | str,
    snapshot_store: MConsumeSnapshotStore,
) -> dict[int, ChannelData]:
    """Fetch one billing-period snapshot while polling is locked."""

    try:
        period = _billing_period(reading_day)
        # Reuse HTTP daily history except around midnight and 00:05.
        history_refresh_token = _history_refresh_token()
        # Live mConsume and instantaneous values come from ElectricityX.
        current_electricity = await client.async_current_electricity()
        try:
            current_consumption = await client.async_cached_consumption_history(
                channels, history_refresh_token
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Refoss cached ConsumptionH update failed: %s", err)
            current_consumption = {}
        current_snapshots = await snapshot_store.async_get_today_snapshots(
            current_electricity, channels, current_consumption
        )
        data: dict[int, ChannelData] = {}
        for channel in channels:
            data[channel] = await client.async_billing_period_energy(
                channel,
                period,
                current_electricity,
                current_snapshots,
                current_consumption,
                history_refresh_token,
            )
        return data
    except Exception as err:  # noqa: BLE001
        raise UpdateFailed(str(err)) from err


async def _async_create_midnight_snapshots(
    coordinator: DataUpdateCoordinator[dict[int, ChannelData]],
    client: RefossCloudClient,
    channels: list[int],
    snapshot_store: MConsumeSnapshotStore,
    scan_interval: timedelta,
    polling_lock: asyncio.Lock,
) -> None:
    """Pause periodic polling briefly and create exact midnight snapshots."""

    async with polling_lock:
        coordinator.update_interval = None
        try:
            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    current_electricity = await client.async_current_electricity()
                    await snapshot_store.async_get_today_snapshots(
                        current_electricity,
                        channels,
                        {},
                        backfill_from_consumption=False,
                        replace_backfilled_snapshot=True,
                    )
                    break
                except Exception as err:  # noqa: BLE001
                    if attempt >= max_attempts - 1:
                        raise
                    _LOGGER.debug(
                        "Refoss midnight mConsume snapshot failed, retrying attempt %s/%s: %s",
                        attempt + 2,
                        max_attempts,
                        err,
                    )
                    await asyncio.sleep(2)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Refoss midnight mConsume snapshot failed: %s", err)
        finally:
            coordinator.update_interval = scan_interval

    await coordinator.async_request_refresh()

@dataclass(slots=True)
class BillingPeriod:
    """Timestamp boundaries for the current billing period."""

    local_start: int
    end: int


def _billing_period(reading_day: int | str) -> BillingPeriod:
    """Return local timestamp boundaries for the current billing period."""

    now = dt_util.now()

    # "last" means the actual last day of each month.
    day_this_month = _reading_day_for_month(reading_day, now.year, now.month)
    local_start = datetime(
        now.year, now.month, day_this_month, tzinfo=now.tzinfo
    )
    if now < local_start:
        # Before this month's reading day, the billing period started last month.
        prev_month = now.month - 1 or 12
        prev_year = now.year if now.month > 1 else now.year - 1
        prev_day = _reading_day_for_month(reading_day, prev_year, prev_month)
        local_start = datetime(prev_year, prev_month, prev_day, tzinfo=now.tzinfo)

    return BillingPeriod(
        local_start=int(local_start.timestamp()),
        end=int(now.timestamp()),
    )


def _normalize_reading_day(value: Any) -> int | str:
    """Normalize configured reading day values."""

    if value == READING_DAY_LAST:
        return READING_DAY_LAST

    day = int(value)
    # Older configs may have stored 28-31; treat them as "last day".
    if day >= 28:
        return READING_DAY_LAST
    return day


def _scan_interval_from_config(config: dict[str, Any]) -> timedelta:
    """Return the configured MQTT polling interval."""

    value = config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    # Config flow stores seconds, while YAML may already pass a timedelta.
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds())
    else:
        seconds = int(value)

    return timedelta(seconds=max(10, seconds))


def _reading_day_for_month(reading_day: int | str, year: int, month: int) -> int:
    """Return the actual reading day for a specific calendar month."""

    if reading_day == READING_DAY_LAST:
        return monthrange(year, month)[1]

    return min(int(reading_day), monthrange(year, month)[1])


def _history_refresh_token() -> str:
    """Return the current daily history refresh bucket.

    MQTT values are refreshed every coordinator update. HTTP history is only
    needed for past daily correction, so refresh it once after local midnight
    and again after five minutes to catch delayed daily rows.
    """

    now = dt_util.now()
    midnight_retry = now.replace(hour=0, minute=5, second=0, microsecond=0)
    midnight_first = now.replace(hour=0, minute=0, second=5, microsecond=0)

    # Avoid caching incomplete daily history immediately after midnight.
    if now >= midnight_retry:
        date = now.date().isoformat()
        bucket = "0005"
    elif now >= midnight_first:
        date = now.date().isoformat()
        bucket = "0000"
    else:
        date = (now - timedelta(days=1)).date().isoformat()
        bucket = "0005"

    return f"{date}:{bucket}"


def _midnight_backfill_guard_active(now: datetime) -> bool:
    """Return true while ConsumptionH may still be cached from yesterday.

    If the exact 00:00 snapshot fails, the first coordinator refresh can happen
    before the history refresh bucket rolls over at 00:00:05. In that narrow
    window, a missing snapshot must not be reconstructed from stale
    ConsumptionH data from the previous day.
    """

    return now.hour == 0 and now.minute == 0 and now.second < 5


def _month_reset_guard_active(now: datetime) -> bool:
    """Return true during the EM06 first-day reset guard window."""

    last_day = monthrange(now.year, now.month)[1]
    return (
        (now.day == last_day and now.hour == 23 and now.minute >= 50)
        or (now.day == 1 and now.hour == 0 and now.minute <= 10)
    )


def _month_reset_zero_snapshot_active(now: datetime) -> bool:
    """Return true when first-day near-zero mConsume values should become zero."""

    return now.day == 1 and now.hour == 0 and now.minute <= 10


def _set_snapshot_if_changed(
    snapshot_data: dict[str, Any], key: str, entry: dict[str, Any]
) -> bool:
    """Set a persisted snapshot entry and return whether it changed."""

    if snapshot_data.get(key) == entry:
        return False
    snapshot_data[key] = entry
    return True


def _snapshot_entry(value: int, source: str) -> dict[str, Any]:
    """Build one persisted mConsume snapshot entry."""

    return {"mconsume_wh": int(value), "source": source}


def _snapshot_entry_value(entry: Any) -> tuple[int, str] | None:
    """Read a snapshot entry, including entries from older versions."""

    if isinstance(entry, dict):
        try:
            value = int(entry["mconsume_wh"])
        except (KeyError, TypeError, ValueError):
            return None
        return value, str(entry.get("source") or "mconsume_snapshot")

    try:
        return int(entry), "mconsume_snapshot"
    except (TypeError, ValueError):
        return None


def _prune_old_snapshots(device_data: dict[str, Any], today: date) -> bool:
    """Remove daily snapshots older than the retention window."""

    cutoff = today - timedelta(days=SNAPSHOT_RETENTION_DAYS)
    changed = False
    for date_key in list(device_data):
        try:
            snapshot_date = date.fromisoformat(date_key)
        except ValueError:
            continue
        if snapshot_date < cutoff:
            device_data.pop(date_key, None)
            changed = True
    return changed


def _stored_snapshot(
    device_data: dict[str, Any],
    snapshot_date: date,
    channel: int,
) -> ChannelSnapshot | None:
    """Return one stored daily mConsume snapshot."""

    day_data = device_data.get(snapshot_date.isoformat())
    if not isinstance(day_data, dict):
        return None
    entry = _snapshot_entry_value(day_data.get(str(channel)))
    if entry is None:
        return None
    value, source = entry
    return ChannelSnapshot(snapshot_date, value, source)


class MConsumeSnapshotStore:
    """Persist daily mConsume baselines used for current-day energy."""

    def __init__(self, hass: HomeAssistant, uuid: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, SNAPSHOT_STORE_VERSION, SNAPSHOT_STORE_KEY
        )
        self._uuid = uuid
        self._data: dict[str, Any] = {"devices": {}}
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load stored snapshots."""

        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._data = stored
        self._data.setdefault("devices", {}).setdefault(self._uuid, {})

    async def async_get_today_snapshots(
        self,
        current_electricity: dict[int, ChannelElectricity],
        channels: list[int],
        current_consumption: dict[int, ChannelConsumption],
        backfill_from_consumption: bool = True,
        replace_backfilled_snapshot: bool = False,
    ) -> dict[int, ChannelSnapshot]:
        """Return today's baseline snapshots, creating missing rows if needed."""

        async with self._lock:
            now = dt_util.now()
            today = now.date()
            date_key = today.isoformat()
            device_data = self._data.setdefault("devices", {}).setdefault(
                self._uuid, {}
            )
            day_data: dict[str, Any] = device_data.setdefault(date_key, {})

            midnight_backfill_guard = _midnight_backfill_guard_active(now)
            reset_guard_active = _month_reset_guard_active(now)
            zero_snapshot_active = _month_reset_zero_snapshot_active(now)
            changed = _prune_old_snapshots(device_data, today)

            for channel in channels:
                if channel not in current_electricity:
                    continue
                key = str(channel)
                current_mconsume_wh = current_electricity[channel].mconsume_wh
                if reset_guard_active:
                    if (
                        zero_snapshot_active
                        and 0 <= current_mconsume_wh <= MONTH_RESET_SNAPSHOT_MAX_WH
                    ):
                        zero_entry = _snapshot_entry(
                            0,
                            "month_reset_zero_snapshot",
                        )
                        if _set_snapshot_if_changed(day_data, key, zero_entry):
                            changed = True
                    continue
                existing = _snapshot_entry_value(day_data.get(key))
                if (
                    replace_backfilled_snapshot
                    and existing is not None
                    and existing[1] == "mconsume_snapshot_backfilled_from_consumptionh"
                ):
                    day_data[key] = _snapshot_entry(
                        current_mconsume_wh, "mconsume_snapshot"
                    )
                    changed = True
                    continue
                if key not in day_data:
                    consumption = current_consumption.get(channel)
                    if backfill_from_consumption and midnight_backfill_guard:
                        continue
                    if backfill_from_consumption and consumption is not None:
                        day_data[key] = _snapshot_entry(
                            current_mconsume_wh - consumption.today_wh,
                            "mconsume_snapshot_backfilled_from_consumptionh",
                        )
                    else:
                        day_data[key] = _snapshot_entry(
                            current_mconsume_wh, "mconsume_snapshot"
                        )
                    changed = True

            if changed:
                await self._store.async_save(self._data)

            result: dict[int, ChannelSnapshot] = {}
            for channel in channels:
                snapshot = _snapshot_entry_value(day_data.get(str(channel)))
                if snapshot is None:
                    continue
                value, source = snapshot
                result[channel] = ChannelSnapshot(
                    date=today,
                    mconsume_wh=value,
                    source=source,
                )
            return result

class RefossCloudClient:
    """Small client for the Refoss/Meross cloud HTTP API."""

    def __init__(
        self,
        session: Any,
        email: str,
        password: str,
        uuid: str,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._uuid = uuid
        self._token: str | None = None
        self._userid: str | None = None
        self._key: str | None = None
        self._mqtt_domain: str | None = None
        self._api_base = DEFAULT_API_BASE
        self._history_cache: dict[
            tuple[int, int, str], CompletedDailyHistory
        ] = {}
        self._consumption_cache: dict[
            tuple[tuple[int, ...], str], dict[int, ChannelConsumption]
        ] = {}
        self._mqtt_lock = asyncio.Lock()

    async def async_billing_period_energy(
        self,
        channel: int,
        period: BillingPeriod,
        current_electricity: dict[int, ChannelElectricity],
        current_snapshots: dict[int, ChannelSnapshot],
        current_consumption: dict[int, ChannelConsumption],
        history_refresh_token: str,
    ) -> ChannelData:
        """Fetch electric history for one billing-period channel."""

        snapshot = current_electricity[channel]
        current_mconsume_wh = snapshot.mconsume_wh
        today = dt_util.now().date()
        snapshot_baseline = current_snapshots.get(channel)
        if snapshot_baseline is not None and snapshot_baseline.date == today:
            today_wh = current_mconsume_wh - snapshot_baseline.mconsume_wh
            today_snapshot_wh = snapshot_baseline.mconsume_wh
            today_snapshot_source = snapshot_baseline.source
            today_source = "cloud_mqtt_mconsume_minus_midnight_snapshot"
        else:
            today_wh = 0
            today_snapshot_wh = None
            today_snapshot_source = "missing_midnight_snapshot"
            today_source = "unavailable_midnight_snapshot"

        today_consumption = current_consumption.get(channel)
        today_history_rows = (
            today_consumption.history_rows if today_consumption is not None else 0
        )

        history = await self._async_completed_billing_history(
            channel, period, history_refresh_token, today_consumption
        )
        billing_source = (
            "cloud_http_1d_until_two_days_ago_plus_http_1h_yesterday"
            "_with_consumptionh_fill_plus_mconsume_today"
        )
        if snapshot_baseline is None:
            billing_source += "_midnight_snapshot_unavailable"

        return ChannelData(
            net_wh=history.total_wh + today_wh,
            today_wh=today_wh,
            current_mconsume_wh=current_mconsume_wh,
            today_snapshot_wh=today_snapshot_wh,
            today_snapshot_source=today_snapshot_source,
            completed_history_wh=history.total_wh,
            history_rows=history.history_rows,
            billing_source=billing_source,
            today_source=today_source,
            today_history_rows=today_history_rows,
            yesterday_http_rows=history.yesterday_http_rows,
            yesterday_filled_hours=history.yesterday_filled_hours,
            yesterday_missing_hours=history.yesterday_missing_hours,
            current_ma=snapshot.current_ma,
            voltage_mv=snapshot.voltage_mv,
            power_mw=snapshot.power_mw,
            factor=snapshot.factor,
        )

    async def _async_completed_billing_history(
        self,
        channel: int,
        period: BillingPeriod,
        history_refresh_token: str,
        current_consumption: ChannelConsumption | None,
    ) -> CompletedDailyHistory:
        """Return cached usage from billing start through yesterday."""

        cache_key = (channel, period.local_start, history_refresh_token)
        if cache_key in self._history_cache:
            return self._history_cache[cache_key]

        stale_keys = [key for key in self._history_cache if key[0] == channel]
        for key in stale_keys:
            self._history_cache.pop(key, None)

        now = dt_util.now()
        today = now.date()
        yesterday = today - timedelta(days=1)
        two_days_ago = today - timedelta(days=2)
        period_start_date = datetime.fromtimestamp(
            period.local_start, dt_util.DEFAULT_TIME_ZONE
        ).date()

        total_wh = 0
        daily_rows: list[dict[str, Any]] = []
        if period_start_date <= two_days_ago:
            daily_rows = await self._async_electric_history(
                channel=channel,
                start_time=_local_day_start_timestamp(period_start_date),
                end_time=_local_day_end_timestamp(two_days_ago),
                step="1d",
            )
            total_wh += sum(_row_net_wh(row) for row in daily_rows)

        yesterday_hourly: dict[int, int] = {}
        yesterday_rows: list[dict[str, Any]] = []
        if period_start_date <= yesterday:
            yesterday_rows = await self._async_electric_history(
                channel=channel,
                start_time=_local_day_start_timestamp(yesterday),
                end_time=_local_day_end_timestamp(yesterday),
                step="1h",
            )
            for row in yesterday_rows:
                row_dt = _row_local_datetime(row)
                if row_dt is None or row_dt.date() != yesterday:
                    continue
                yesterday_hourly[row_dt.hour] = (
                    yesterday_hourly.get(row_dt.hour, 0) + _row_net_wh(row)
                )

        filled_hours: list[int] = []
        missing_hours: list[int] = []
        if period_start_date <= yesterday:
            for hour in range(24):
                if hour in yesterday_hourly:
                    continue
                fill_value = (
                    current_consumption.date_hour_totals_wh.get((yesterday, hour))
                    if current_consumption is not None
                    else None
                )
                if fill_value is None:
                    missing_hours.append(hour)
                    continue
                yesterday_hourly[hour] = fill_value
                filled_hours.append(hour)
            total_wh += sum(yesterday_hourly.values())

        history = CompletedDailyHistory(
            total_wh=total_wh,
            history_rows=len(daily_rows) + len(yesterday_rows),
            yesterday_http_rows=len(yesterday_rows),
            yesterday_filled_hours=filled_hours,
            yesterday_missing_hours=missing_hours,
        )
        if not missing_hours or current_consumption is not None:
            self._history_cache[cache_key] = history
        return history

    async def async_current_electricity(
        self, max_attempts: int = 3
    ) -> dict[int, ChannelElectricity]:
        """Fetch current ElectricityX values through Refoss cloud MQTT."""

        if self._token is None:
            await self.async_login()

        # Socket MQTT is blocking I/O, so run it in a worker thread.
        last_err: OSError | TimeoutError | None = None
        async with self._mqtt_lock:
            for attempt in range(max_attempts):
                try:
                    return await asyncio.to_thread(self._mqtt_current_electricity)
                except (OSError, TimeoutError, RuntimeError) as err:
                    last_err = err
                    if attempt < max_attempts - 1:
                        # Retry transient DNS, network, or MQTT response delays.
                        _LOGGER.debug(
                            "Refoss MQTT update failed, retrying attempt %s/%s: %s",
                            attempt + 2,
                            max_attempts,
                            err,
                        )
                        await asyncio.sleep(2)

        if last_err is not None:
            raise last_err
        raise RuntimeError("Refoss MQTT ElectricityX response failed")

    async def async_current_consumption(
        self, channels: list[int], max_attempts: int = 3
    ) -> dict[int, ChannelConsumption]:
        """Fetch current-day ConsumptionH values through Refoss cloud MQTT."""

        if self._token is None:
            await self.async_login()

        last_err: OSError | TimeoutError | RuntimeError | None = None
        async with self._mqtt_lock:
            for attempt in range(max_attempts):
                try:
                    return await asyncio.to_thread(
                        self._mqtt_current_consumption, channels
                    )
                except (OSError, TimeoutError, RuntimeError) as err:
                    last_err = err
                    if attempt < max_attempts - 1:
                        _LOGGER.debug(
                            "Refoss ConsumptionH update failed, retrying attempt %s/%s: %s",
                            attempt + 2,
                            max_attempts,
                            err,
                        )
                        await asyncio.sleep(2)

        if last_err is not None:
            raise last_err
        raise RuntimeError("Refoss MQTT ConsumptionH response failed")

    async def async_cached_consumption_history(
        self, channels: list[int], history_refresh_token: str
    ) -> dict[int, ChannelConsumption]:
        """Fetch ConsumptionH once per HTTP history refresh bucket."""

        cache_key = (tuple(channels), history_refresh_token)
        if cache_key in self._consumption_cache:
            return self._consumption_cache[cache_key]

        data = await self.async_current_consumption(channels)
        self._consumption_cache.clear()
        self._consumption_cache[cache_key] = data
        return data

    async def _async_electric_history(
        self, channel: int, start_time: int, end_time: int, step: str
    ) -> list[dict[str, Any]]:
        """Fetch electric history rows for one channel."""

        payload = {
            "uuid": self._uuid,
            "subDeviceId": "",
            "channel": channel,
            "startTime": start_time,
            "endTime": end_time,
            "query": [
                {
                    "metric": "electricH",
                    "queryType": ["stepSum"],
                    "step": step,
                }
            ],
        }

        if self._token is None:
            await self.async_login()

        # HTTP history is used only for completed daily billing-period rows.
        response = await self._async_post(
            "/historage/v1/deviceTelemetry/query", payload, token=self._token
        )
        if response.get("apiStatus") in (1001, 1002, 5001):
            await self.async_login()
            response = await self._async_post(
                "/historage/v1/deviceTelemetry/query", payload, token=self._token
            )

        self._raise_for_api_error(response)
        rows = (
            response.get("data", {})
            .get("electricH", {})
            .get("result", {})
            .get("stepSumElectricH", [])
        )
        return rows

    async def async_login(self) -> dict[str, Any]:
        """Log in and cache the Refoss cloud token."""

        # The Refoss app sends an MD5 password hash, then returns HTTP/MQTT auth data.
        password_md5 = hashlib.md5(self._password.encode("utf8")).hexdigest()
        response = await self._async_post(
            "/v1/Auth/signIn",
            {
                "email": self._email,
                "password": password_md5,
                "accountCountryCode": "kr",
                "encryption": 1,
                "agree": 1,
                "mobileInfo": {
                    "deviceModel": "Home Assistant",
                    "mobileOsVersion": "Home Assistant",
                    "mobileOs": "Home Assistant",
                    "uuid": f"{DOMAIN}-{self._uuid}",
                    "carrier": "",
                },
            },
        )
        self._raise_for_api_error(response)
        data = response["data"]
        self._token = data["token"]
        self._userid = str(data["userid"])
        self._key = data["key"]
        self._mqtt_domain = data["mqttDomain"]
        self._api_base = data.get("domain") or self._api_base
        return data

    def _mqtt_current_electricity(self) -> dict[int, ChannelElectricity]:
        """Fetch ElectricityX through the Refoss cloud MQTT broker."""

        if self._userid is None or self._key is None or self._mqtt_domain is None:
            raise RuntimeError("Refoss MQTT credentials are missing")

        app_id = hashlib.md5(_random_string(16).encode()).hexdigest()
        app_topic = f"/app/{self._userid}-{app_id}/subscribe"
        client_id = f"app:{app_id}"
        password = hashlib.md5(f"{self._userid}{self._key}".encode()).hexdigest()

        # Publish a GET request and wait for the matching messageId GETACK.
        raw_socket = socket.create_connection((self._mqtt_domain, 443), timeout=12)
        with ssl.create_default_context().wrap_socket(
            raw_socket, server_hostname=self._mqtt_domain
        ) as mqtt:
            mqtt.settimeout(12)
            connect = (
                _mqtt_string("MQTT")
                + bytes([4, 0xC2])
                + struct.pack("!H", 60)
                + _mqtt_string(client_id)
                + _mqtt_string(self._userid)
                + _mqtt_string(password)
            )
            mqtt.sendall(_mqtt_packet(0x10, connect))
            packet_type, packet = _mqtt_read_packet(mqtt)
            if packet_type != 0x20 or packet[-1] != 0:
                raise RuntimeError("Refoss MQTT connection failed")

            # Responses arrive on the app subscribe topic.
            subscribe = struct.pack("!H", 1) + _mqtt_string(app_topic) + b"\x00"
            mqtt.sendall(_mqtt_packet(0x82, subscribe))
            packet_type, packet = _mqtt_read_packet(mqtt)
            if packet_type != 0x90 or packet[-1] == 0x80:
                raise RuntimeError("Refoss MQTT subscribe failed")

            message_id = hashlib.md5(app_id.encode()).hexdigest()
            timestamp = int(time.time())
            sign = hashlib.md5(
                f"{message_id}{self._key}{timestamp}".encode()
            ).hexdigest()
            # ElectricityX returns all EM06 channel readings in one request.
            message = {
                "header": {
                    "from": app_topic,
                    "messageId": message_id,
                    "method": "GET",
                    "namespace": "Appliance.Control.ElectricityX",
                    "payloadVersion": 1,
                    "sign": sign,
                    "timestamp": timestamp,
                    "triggerSrc": "HA",
                    "uuid": self._uuid,
                },
                "payload": {"electricity": {"channel": 65535}},
            }
            publish_topic = f"/appliance/{self._uuid}/subscribe"
            mqtt.sendall(
                _mqtt_packet(
                    0x30,
                    _mqtt_string(publish_topic)
                    + json.dumps(message, separators=(",", ":")).encode(),
                )
            )

            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                packet_type, packet = _mqtt_read_packet(mqtt)
                if packet_type != 0x30:
                    continue

                topic_length = struct.unpack("!H", packet[:2])[0]
                payload = json.loads(packet[2 + topic_length :].decode())
                if (
                    payload.get("header", {}).get("messageId") != message_id
                    or payload.get("header", {}).get("method") != "GETACK"
                ):
                    continue

                # Store raw units; entity classes convert them for HA display.
                rows = payload.get("payload", {}).get("electricity", [])
                return {
                    int(row["channel"]): ChannelElectricity(
                        mconsume_wh=int(row["mConsume"]),
                        current_ma=int(row.get("current") or 0),
                        voltage_mv=int(row.get("voltage") or 0),
                        power_mw=int(row.get("power") or 0),
                        factor=float(row.get("factor") or 0),
                    )
                    for row in rows
                    if "channel" in row and "mConsume" in row
                }

            raise RuntimeError("Refoss MQTT ElectricityX response timed out")

    def _mqtt_current_consumption(
        self, channels: list[int]
    ) -> dict[int, ChannelConsumption]:
        """Fetch ConsumptionH through the Refoss cloud MQTT broker."""

        if self._userid is None or self._key is None or self._mqtt_domain is None:
            raise RuntimeError("Refoss MQTT credentials are missing")

        app_id = hashlib.md5(_random_string(16).encode()).hexdigest()
        app_topic = f"/app/{self._userid}-{app_id}/subscribe"
        client_id = f"app:{app_id}"
        password = hashlib.md5(f"{self._userid}{self._key}".encode()).hexdigest()

        raw_socket = socket.create_connection((self._mqtt_domain, 443), timeout=12)
        with ssl.create_default_context().wrap_socket(
            raw_socket, server_hostname=self._mqtt_domain
        ) as mqtt:
            mqtt.settimeout(12)
            connect = (
                _mqtt_string("MQTT")
                + bytes([4, 0xC2])
                + struct.pack("!H", 60)
                + _mqtt_string(client_id)
                + _mqtt_string(self._userid)
                + _mqtt_string(password)
            )
            mqtt.sendall(_mqtt_packet(0x10, connect))
            packet_type, packet = _mqtt_read_packet(mqtt)
            if packet_type != 0x20 or packet[-1] != 0:
                raise RuntimeError("Refoss MQTT connection failed")

            subscribe = struct.pack("!H", 1) + _mqtt_string(app_topic) + b"\x00"
            mqtt.sendall(_mqtt_packet(0x82, subscribe))
            packet_type, packet = _mqtt_read_packet(mqtt)
            if packet_type != 0x90 or packet[-1] == 0x80:
                raise RuntimeError("Refoss MQTT subscribe failed")

            result: dict[int, ChannelConsumption] = {}
            for channel in channels:
                message_id = hashlib.md5(f"{app_id}:{channel}".encode()).hexdigest()
                timestamp = int(time.time())
                sign = hashlib.md5(
                    f"{message_id}{self._key}{timestamp}".encode()
                ).hexdigest()
                # ConsumptionH backs the app's current-day and hourly energy views.
                # 65535 does not answer reliably, so query one channel at a time.
                message = {
                    "header": {
                        "from": app_topic,
                        "messageId": message_id,
                        "method": "GET",
                        "namespace": "Appliance.Control.ConsumptionH",
                        "payloadVersion": 1,
                        "sign": sign,
                        "timestamp": timestamp,
                        "triggerSrc": "HA",
                        "uuid": self._uuid,
                    },
                    "payload": {"consumptionH": [{"channel": channel}]},
                }
                publish_topic = f"/appliance/{self._uuid}/subscribe"
                mqtt.sendall(
                    _mqtt_packet(
                        0x30,
                        _mqtt_string(publish_topic)
                        + json.dumps(message, separators=(",", ":")).encode(),
                    )
                )

                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    packet_type, packet = _mqtt_read_packet(mqtt)
                    if packet_type != 0x30:
                        continue

                    topic_length = struct.unpack("!H", packet[:2])[0]
                    payload = json.loads(packet[2 + topic_length :].decode())
                    if (
                        payload.get("header", {}).get("messageId") != message_id
                        or payload.get("header", {}).get("method") != "GETACK"
                    ):
                        continue

                    rows = payload.get("payload", {}).get("consumptionH", [])
                    for row in rows:
                        if "channel" not in row or "total" not in row:
                            continue
                        date_totals_wh: dict[date, int] = {}
                        date_hour_totals_wh: dict[tuple[date, int], int] = {}
                        for item in row.get("data") or []:
                            item_dt = _timestamp_local_datetime(
                                item.get("timestamp")
                            )
                            if item_dt is None:
                                continue
                            value = int(item.get("value") or 0)
                            item_date = item_dt.date()
                            date_totals_wh[item_date] = (
                                date_totals_wh.get(item_date, 0) + value
                            )
                            hour_key = (item_date, item_dt.hour)
                            date_hour_totals_wh[hour_key] = (
                                date_hour_totals_wh.get(hour_key, 0) + value
                            )
                        latest_history_date = (
                            max(date_totals_wh) if date_totals_wh else None
                        )
                        result[int(row["channel"])] = ChannelConsumption(
                            today_wh=int(row["total"]),
                            history_rows=len(row.get("data") or []),
                            date_totals_wh=date_totals_wh,
                            date_hour_totals_wh=date_hour_totals_wh,
                            latest_history_date=latest_history_date,
                        )
                    break
                else:
                    raise RuntimeError(
                        f"Refoss MQTT ConsumptionH response timed out for channel {channel}"
                    )

            return result

    async def async_devices(self) -> list[dict[str, Any]]:
        """Return devices from the Refoss cloud account."""

        if self._token is None:
            await self.async_login()

        response = await self._async_post("/v1/Device/devList", {}, token=self._token)
        self._raise_for_api_error(response)
        return response.get("data", [])

    async def _async_post(
        self, path: str, params: Any, token: str | None = None
    ) -> dict[str, Any]:
        encoded_params = base64.b64encode(json.dumps(params).encode("utf8")).decode(
            "utf8"
        )
        timestamp = int(round(time.time() * 1000))
        nonce = "".join(
            random.SystemRandom().choice(string.ascii_uppercase + string.digits)
            for _ in range(16)
        )
        sign = hashlib.md5(
            f"{SECRET}{timestamp}{nonce}{encoded_params}".encode("utf8")
        ).hexdigest()

        headers = {
            "AppVersion": "1.16.0",
            "Authorization": "Basic" if token is None else f"Basic {token}",
            "vender": "refoss",
            "AppType": "Refoss",
            "AppLanguage": "EN",
            "User-Agent": "Refoss/1.16.0",
            "Content-Type": "application/json",
        }

        last_err: ClientError | OSError | TimeoutError | None = None
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                async with self._session.post(
                    f"{self._api_base}{path}",
                    json={
                        "params": encoded_params,
                        "sign": sign,
                        "timestamp": timestamp,
                        "nonce": nonce,
                    },
                    headers=headers,
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except (ClientError, OSError, TimeoutError) as err:
                last_err = err
                if attempt < max_attempts - 1:
                    # Match the MQTT retry policy for transient HTTP failures.
                    _LOGGER.debug(
                        "Refoss HTTP request failed, retrying attempt %s/%s: %s",
                        attempt + 2,
                        max_attempts,
                        err,
                    )
                    await asyncio.sleep(2)

        if last_err is not None:
            raise last_err
        raise RuntimeError("Refoss HTTP request failed")

    @staticmethod
    def _raise_for_api_error(response: dict[str, Any]) -> None:
        if response.get("apiStatus") != 0:
            raise RuntimeError(
                f"Refoss API error {response.get('apiStatus')}: {response.get('info')}"
            )


def _row_net_wh(row: dict[str, Any]) -> int:
    """Return net Wh for one history row."""

    # value is already net Wh. Solar channels may legitimately be negative.
    value = row.get("value")
    if value is not None:
        return int(value)

    valcons = int(row.get("valcons") or 0)
    valprod = int(row.get("valprod") or 0)
    return valcons + valprod


def _row_local_date(row: dict[str, Any]) -> date | None:
    """Return the local date for a cloud history row when available."""

    row_dt = _row_local_datetime(row)
    if row_dt is not None:
        return row_dt.date()

    value = row.get("date")
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None

    return None


def _row_local_datetime(row: dict[str, Any]) -> datetime | None:
    """Return the local datetime for a cloud history row when available."""

    for key in ("timestamp", "time", "ts"):
        row_dt = _timestamp_local_datetime(row.get(key))
        if row_dt is not None:
            return row_dt

    return None


def _timestamp_local_date(value: Any) -> date | None:
    """Convert a second or millisecond timestamp to a Home Assistant local date."""

    row_dt = _timestamp_local_datetime(value)
    return row_dt.date() if row_dt is not None else None


def _timestamp_local_datetime(value: Any) -> datetime | None:
    """Convert a second or millisecond timestamp to a Home Assistant local time."""

    if value in (None, ""):
        return None

    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None

    if timestamp > 10_000_000_000:
        timestamp //= 1000

    return datetime.fromtimestamp(timestamp, UTC).astimezone(
        dt_util.DEFAULT_TIME_ZONE
    )


def _local_day_start_timestamp(day: date) -> int:
    """Return the Unix timestamp for local midnight at the start of a day."""

    return int(
        datetime(
            day.year,
            day.month,
            day.day,
            tzinfo=dt_util.DEFAULT_TIME_ZONE,
        ).timestamp()
    )


def _local_day_end_timestamp(day: date) -> int:
    """Return the Unix timestamp for the final second of a local day."""

    return _local_day_start_timestamp(day + timedelta(days=1)) - 1


def _random_string(length: int) -> str:
    """Return a random uppercase string for Refoss message ids."""

    return "".join(
        random.SystemRandom().choice(string.ascii_uppercase + string.digits)
        for _ in range(length)
    )


def _mqtt_string(value: str) -> bytes:
    """Encode an MQTT UTF-8 string."""

    # MQTT strings are a 2-byte length prefix followed by UTF-8 bytes.
    encoded = value.encode()
    return struct.pack("!H", len(encoded)) + encoded


def _mqtt_remaining_length(length: int) -> bytes:
    """Encode an MQTT remaining length field."""

    # MQTT remaining length uses a 7-bit variable-length encoding.
    result = b""
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 128
        result += bytes([digit])
        if not length:
            return result


def _mqtt_packet(packet_type: int, payload: bytes) -> bytes:
    """Build an MQTT packet."""

    return bytes([packet_type]) + _mqtt_remaining_length(len(payload)) + payload


def _mqtt_read_packet(mqtt: ssl.SSLSocket) -> tuple[int, bytes]:
    """Read one MQTT packet."""

    # Keep the dependency surface small by implementing minimal MQTT framing.
    header = mqtt.recv(1)
    if not header:
        raise RuntimeError("Refoss MQTT connection closed")

    multiplier = 1
    remaining = 0
    while True:
        encoded_byte = mqtt.recv(1)[0]
        remaining += (encoded_byte & 127) * multiplier
        if not encoded_byte & 128:
            break
        multiplier *= 128

    payload = b""
    while len(payload) < remaining:
        chunk = mqtt.recv(remaining - len(payload))
        if not chunk:
            raise RuntimeError("Refoss MQTT connection closed")
        payload += chunk

    return header[0], payload


def _channel_label(channel: int) -> str:
    """Return the user-facing EM06 channel label."""

    return CHANNEL_LABELS.get(channel, f"C{channel}")


class RefossCloudEnergySensor(CoordinatorEntity, SensorEntity):
    """Refoss Cloud billing period energy sensor."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[int, ChannelData]],
        name: str,
        uuid: str,
        channel: int,
        reading_day: int | str,
    ) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._reading_day = _normalize_reading_day(reading_day)
        self._attr_unique_id = f"{uuid}_{_channel_label(channel).lower()}_{SENSOR_SLUG}"
        self._attr_name = f"{name} {_channel_label(channel)} {SENSOR_LABEL}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, uuid)},
            "name": name,
            "manufacturer": "Refoss",
            "model": "EM06",
        }

    @property
    def available(self) -> bool:
        """Return if entity is available."""

        return self.coordinator.last_update_success and self._channel in (
            self.coordinator.data or {}
        )

    @property
    def native_value(self) -> float | None:
        """Return sensor value in kWh."""

        data = (self.coordinator.data or {}).get(self._channel)
        if data is None:
            return None

        # Internal energy values are Wh; HA displays kWh.
        return round(data.net_wh / 1000, 3)

    @property
    def last_reset(self) -> datetime:
        """Return the start of the current billing period."""

        period = _billing_period(self._reading_day)
        return datetime.fromtimestamp(period.local_start, dt_util.DEFAULT_TIME_ZONE)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""

        period = _billing_period(self._reading_day)
        data = (self.coordinator.data or {}).get(self._channel)
        attrs: dict[str, Any] = {
            "channel": self._channel,
            "channel_label": _channel_label(self._channel),
            "reading_day": self._reading_day,
            "period_start": datetime.fromtimestamp(
                period.local_start, dt_util.DEFAULT_TIME_ZONE
            ).isoformat(),
            "period_end": datetime.fromtimestamp(
                period.end, dt_util.DEFAULT_TIME_ZONE
            ).isoformat(),
        }
        if data is None:
            return attrs

        attrs.update(
            {
                "current_mconsume_kwh": round(data.current_mconsume_wh / 1000, 3),
                "today_snapshot_kwh": (
                    round(data.today_snapshot_wh / 1000, 3)
                    if data.today_snapshot_wh is not None
                    else None
                ),
                "completed_history_kwh": round(
                    data.completed_history_wh / 1000, 3
                ),
                "today_snapshot_delta_kwh": round(data.today_wh / 1000, 3),
                "yesterday_filled_hours": data.yesterday_filled_hours,
                "yesterday_missing_hours": data.yesterday_missing_hours,
            }
        )
        return {
            **attrs,
        }

    async def async_update(self) -> None:
        """Update the entity."""

        await self.coordinator.async_request_refresh()


class RefossCloudTodayEnergySensor(CoordinatorEntity, SensorEntity):
    """Refoss Cloud current-day energy sensor."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[int, ChannelData]],
        name: str,
        uuid: str,
        channel: int,
    ) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_unique_id = (
            f"{uuid}_{_channel_label(channel).lower()}_{TODAY_SENSOR_SLUG}"
        )
        self._attr_name = f"{name} {_channel_label(channel)} {TODAY_SENSOR_LABEL}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, uuid)},
            "name": name,
            "manufacturer": "Refoss",
            "model": "EM06",
        }

    @property
    def available(self) -> bool:
        """Return if entity is available."""

        return self.coordinator.last_update_success and self._channel in (
            self.coordinator.data or {}
        )

    @property
    def native_value(self) -> float | None:
        """Return today's net energy in kWh."""

        data = (self.coordinator.data or {}).get(self._channel)
        if data is None:
            return None
        if data.today_source != "cloud_mqtt_mconsume_minus_midnight_snapshot":
            return None

        return round(data.today_wh / 1000, 3)

    @property
    def last_reset(self) -> datetime:
        """Return local midnight for the current day."""

        now = dt_util.now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""

        now = dt_util.now()
        data = (self.coordinator.data or {}).get(self._channel)
        attrs: dict[str, Any] = {
            "channel": self._channel,
            "channel_label": _channel_label(self._channel),
            "date": now.date().isoformat(),
        }
        if data is None:
            return attrs

        attrs.update(
            {
                "current_mconsume_kwh": round(data.current_mconsume_wh / 1000, 3),
                "today_snapshot_kwh": (
                    round(data.today_snapshot_wh / 1000, 3)
                    if data.today_snapshot_wh is not None
                    else None
                ),
                "today_snapshot_delta_kwh": round(data.today_wh / 1000, 3),
            }
        )
        return attrs

    async def async_update(self) -> None:
        """Update the entity."""

        await self.coordinator.async_request_refresh()


class RefossCloudInstantSensor(CoordinatorEntity, SensorEntity):
    """Refoss Cloud instantaneous ElectricityX sensor."""

    _attr_suggested_display_precision = 3
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[int, ChannelData]],
        name: str,
        uuid: str,
        channel: int,
        sensor_type: str,
    ) -> None:
        super().__init__(coordinator)
        description = INSTANT_SENSOR_TYPES[sensor_type]
        self._channel = channel
        self._sensor_type = sensor_type
        self._attr_unique_id = f"{uuid}_{channel}_{sensor_type}"
        self._attr_name = (
            f"{name} {_channel_label(channel)} {description['label']}"
        )
        self._attr_device_class = description["device_class"]
        self._attr_native_unit_of_measurement = description["unit"]
        self._attr_device_info = {
            "identifiers": {(DOMAIN, uuid)},
            "name": name,
            "manufacturer": "Refoss",
            "model": "EM06",
        }

    @property
    def available(self) -> bool:
        """Return if entity is available."""

        return self.coordinator.last_update_success and self._channel in (
            self.coordinator.data or {}
        )

    @property
    def native_value(self) -> float | None:
        """Return instantaneous sensor value."""

        data = (self.coordinator.data or {}).get(self._channel)
        if data is None:
            return None

        # Convert raw ElectricityX units to HA display units.
        if self._sensor_type == "power":
            return round(data.power_mw / 1000, 3)
        if self._sensor_type == "voltage":
            return round(data.voltage_mv / 1000, 3)
        if self._sensor_type == "power_factor":
            return round(data.factor, 3)
        if self._sensor_type == "current":
            return round(data.current_ma / 1000, 3)

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""

        return {
            "channel": self._channel,
            "channel_label": _channel_label(self._channel),
            "source": "cloud_mqtt_electricityx",
        }

    async def async_update(self) -> None:
        """Update the entity."""

        await self.coordinator.async_request_refresh()
