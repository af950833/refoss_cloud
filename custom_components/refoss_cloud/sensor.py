"""Refoss EM06 Home Assistant sensors using local API, cloud history, and an in-memory ledger."""

from __future__ import annotations

import asyncio
import base64
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
import random
import re
import string
import subprocess
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
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_CHANNELS = "channels"
CONF_READING_DAY = "reading_day"
CONF_UUID = "uuid"
READING_DAY_LAST = "last"

DEFAULT_NAME = "Refoss Cloud"
DEFAULT_SCAN_INTERVAL = timedelta(seconds=15)
DEFAULT_API_BASE = "https://iotx.refoss.net"
SECRET = "23x17ahWarFH6w29"
HISTORY_BUCKET_SHIFT = timedelta(hours=9)
LOCAL_API_TIMEOUT = 3
CONSUMPTIONH_BATCH_SIZE = 3
CONSUMPTIONH_BATCH_DELAY = 0.1
CONSUMPTIONH_UNSTABLE_GAP_SECONDS = 3600

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

INSTANT_SENSOR_TYPES: dict[str, dict[str, Any]] = {
    "power": {
        "label": "Power",
        "device_class": SensorDeviceClass.POWER,
        "unit": "W",
        "precision": 1,
    },
    "voltage": {
        "label": "Voltage",
        "device_class": SensorDeviceClass.VOLTAGE,
        "unit": "V",
        "precision": 1,
    },
    "power_factor": {
        "label": "PF",
        "device_class": None,
        "unit": None,
        "precision": 3,
    },
    "current": {
        "label": "Current",
        "device_class": SensorDeviceClass.CURRENT,
        "unit": "A",
        "precision": 3,
    },
}

PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_UUID): cv.string,
        vol.Optional(CONF_HOST, default=""): cv.string,
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
class ConsumptionRow:
    """One ConsumptionH row."""

    timestamp: int
    local_dt: datetime
    value_wh: int


@dataclass(slots=True)
class ChannelElectricity:
    """Current ElectricityX values for a Refoss channel."""

    mconsume_wh: int | None = None
    current_ma: int | None = None
    voltage_mv: int | None = None
    power_mw: int | None = None
    factor: float | None = None


@dataclass(slots=True)
class ChannelConsumption:
    """Recent ConsumptionH values for a Refoss channel."""

    raw_total_wh: int | None = None
    rows: list[ConsumptionRow] | None = None

    @property
    def safe_rows(self) -> list[ConsumptionRow]:
        """Return rows as a list."""

        return self.rows or []


@dataclass(slots=True)
class ChannelData:
    """Energy and instantaneous values for a Refoss channel."""

    net_wh: int | None = None
    today_wh: int | None = None
    current_mconsume_wh: int | None = None
    completed_history_wh: int | None = None
    raw_total_wh: int | None = None
    ledger_row_count: int = 0
    today_row_count: int = 0
    current_ma: int | None = None
    voltage_mv: int | None = None
    power_mw: int | None = None
    factor: float | None = None
    source: str = "none"
    error: str | None = None


@dataclass(slots=True)
class BillingPeriod:
    """Boundaries for the current billing period."""

    local_start: int
    end: int

    @property
    def start_date(self) -> date:
        """Return the local date of the billing start."""

        return datetime.fromtimestamp(self.local_start, dt_util.DEFAULT_TIME_ZONE).date()


class HourlyEnergyLedger:
    """In-memory hourly ledger used for completed billing-period rows."""

    def __init__(self, uuid: str) -> None:
        self._uuid = uuid
        self._rows: dict[int, dict[int, ConsumptionRow]] = {}

    @property
    def ledger_id(self) -> str:
        """Return a stable in-memory ledger identifier for logs."""

        return f"{DOMAIN}_in_memory_hourly_ledger_{self._uuid}"

    async def async_rebuild(
        self,
        client: RefossClient,
        channels: list[int],
        reading_day: int | str,
        rebuild_now: datetime,
        current_consumption: dict[int, ChannelConsumption],
    ) -> None:
        """Recreate the in-memory ledger for the current billing period."""

        period = _billing_period(reading_day, rebuild_now)
        period_start = period.start_date
        today = rebuild_now.date()
        yesterday = today - timedelta(days=1)
        rows: dict[int, dict[int, ConsumptionRow]] = {}

        _LOGGER.debug(
            "Refoss ledger rebuild started: channels=%s period_start=%s today=%s yesterday=%s",
            channels,
            period_start,
            today,
            yesterday,
        )

        if period_start <= yesterday:
            try:
                history_rows = await client.async_electric_history_1h_batch(
                    channels, period_start, yesterday
                )
            except Exception as err:  # noqa: BLE001
                history_rows = {channel: [] for channel in channels}
                _LOGGER.warning(
                    "Refoss cloud history fetch failed during ledger rebuild; continuing with local rows only: %s",
                    err,
                    exc_info=True,
                )

            for channel in channels:
                accepted = 0
                for history_row in history_rows.get(channel, []):
                    row_dt = _row_local_datetime(history_row)
                    if row_dt is None or not period_start <= row_dt.date() <= yesterday:
                        continue
                    self._add_row(
                        rows,
                        channel,
                        ConsumptionRow(
                            timestamp=int(row_dt.timestamp()),
                            local_dt=row_dt,
                            value_wh=_row_net_wh(history_row),
                        ),
                    )
                    accepted += 1
                _LOGGER.debug(
                    "Refoss ledger rebuild cloud rows: channel=%s accepted=%s",
                    channel,
                    accepted,
                )

            # The most recent completed day can lag in HTTP history. Fill only
            # missing yesterday hours from the local ConsumptionH buffer.
            for channel in channels:
                present_hours = self._hours_for_day(rows, channel, yesterday)
                consumption = current_consumption.get(channel)
                if consumption is None:
                    continue

                filled = 0
                for item in consumption.safe_rows:
                    if item.local_dt.date() != yesterday:
                        continue
                    if item.local_dt.hour in present_hours:
                        continue
                    self._add_row(rows, channel, item)
                    present_hours.add(item.local_dt.hour)
                    filled += 1

                if filled:
                    _LOGGER.debug(
                        "Refoss ledger rebuild filled yesterday rows from local buffer: channel=%s filled=%s",
                        channel,
                        filled,
                    )

        # Keep today's completed local rows in memory for restart-time rebuilds.
        # Today rows are still excluded from completed_history calculations.
        for channel in channels:
            consumption = current_consumption.get(channel)
            if consumption is None:
                continue
            added_today = sum(
                1
                for item in _consumption_rows_except_latest(consumption, today)
                if self._add_row(rows, channel, item)
            )
            if added_today:
                _LOGGER.debug(
                    "Refoss ledger rebuild stored today's completed local rows: channel=%s rows=%s",
                    channel,
                    added_today,
                )

        self._rows = rows
        _LOGGER.info(
            "Refoss in-memory ledger rebuild completed: ledger_id=%s total_rows=%s",
            self.ledger_id,
            self.total_row_count(),
        )

    async def async_append_runtime_rows(
        self,
        channels: list[int],
        period_start: date,
        current_consumption: dict[int, ChannelConsumption],
    ) -> None:
        """Append the second newest ConsumptionH row from each channel."""

        changed = False
        for channel in channels:
            consumption = current_consumption.get(channel)
            rows = (
                sorted(consumption.safe_rows, key=lambda item: item.timestamp, reverse=True)
                if consumption
                else []
            )
            if len(rows) < 2:
                continue

            row = rows[1]
            if row.local_dt.date() < period_start or self._has_timestamp(channel, row.timestamp):
                continue

            changed = self._add_row(self._rows, channel, row) or changed
            _LOGGER.debug(
                "Refoss ledger appended runtime row: channel=%s timestamp=%s value_wh=%s",
                channel,
                row.timestamp,
                row.value_wh,
            )

        if changed:
            _LOGGER.debug(
                "Refoss in-memory ledger updated: ledger_id=%s total_rows=%s",
                self.ledger_id,
                self.total_row_count(),
            )

    def total_row_count(self) -> int:
        """Return total row count."""

        return sum(len(rows) for rows in self._rows.values())

    def completed_sum_wh(self, channel: int, start: date, end: date) -> int:
        """Return the saved Wh sum between two local dates, inclusive."""

        if end < start:
            return 0
        return sum(
            row.value_wh
            for row in self._channel_rows(channel).values()
            if start <= row.local_dt.date() <= end
        )

    def row_count(self, channel: int, start: date, end: date) -> int:
        """Return the saved row count between two local dates, inclusive."""

        if end < start:
            return 0
        return sum(
            1
            for row in self._channel_rows(channel).values()
            if start <= row.local_dt.date() <= end
        )

    def _channel_rows(self, channel: int) -> dict[int, ConsumptionRow]:
        """Return the row mapping for one channel."""

        return self._rows.setdefault(channel, {})

    def _has_timestamp(self, channel: int, timestamp: int) -> bool:
        """Return true if the row timestamp is already in the ledger."""

        return timestamp in self._channel_rows(channel)

    def _add_row(
        self,
        rows: dict[int, dict[int, ConsumptionRow]],
        channel: int,
        row: ConsumptionRow,
    ) -> bool:
        """Insert one row if its timestamp is not already present."""

        channel_rows = rows.setdefault(channel, {})
        if row.timestamp in channel_rows:
            return False
        channel_rows[row.timestamp] = row
        return True

    def _hours_for_day(
        self,
        rows: dict[int, dict[int, ConsumptionRow]],
        channel: int,
        day: date,
    ) -> set[int]:
        """Return saved hour numbers for one date."""

        return {
            row.local_dt.hour
            for row in rows.setdefault(channel, {}).values()
            if row.local_dt.date() == day
        }


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    """Set up Refoss sensors from YAML."""

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
    """Set up Refoss sensors from a config entry."""

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
    """Set up Refoss sensors.

    Important: never raise from transient local-data problems here.  Raising at
    this stage prevents Home Assistant from creating the entities.
    """

    uuid = config[CONF_UUID]
    name = config.get(CONF_NAME) or DEFAULT_NAME
    channels = [int(channel) for channel in dict.fromkeys(config[CONF_CHANNELS])]
    reading_day = _normalize_reading_day(config[CONF_READING_DAY])

    _LOGGER.info(
        "Refoss sensor setup started: uuid=%s name=%s channels=%s reading_day=%s scan_interval=%ss host=%s entry_id=%s",
        uuid,
        name,
        channels,
        reading_day,
        int(scan_interval.total_seconds()),
        (config.get(CONF_HOST) or "").strip() or "auto",
        getattr(entry, "entry_id", None),
    )

    client = RefossClient(
        session=async_get_clientsession(hass),
        email=config[CONF_EMAIL],
        password=config[CONF_PASSWORD],
        uuid=uuid,
        host=(config.get(CONF_HOST) or "").strip() or None,
    )

    try:
        host = await client.async_resolve_local_host()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Refoss local host resolution failed; entities will still be created and remain unavailable until host is reachable: %s",
            err,
            exc_info=True,
        )
    else:
        _LOGGER.debug("Refoss local host resolved: uuid=%s host=%s", uuid, host)

    polling_lock = asyncio.Lock()
    ledger = HourlyEnergyLedger(uuid)
    _LOGGER.debug(
        "Refoss in-memory ledger initialized: ledger_id=%s total_rows=%s",
        ledger.ledger_id,
        ledger.total_row_count(),
    )

    rebuild_now = dt_util.now()
    try:
        _electricity, bootstrap_consumption = await client.async_current_snapshot(channels)
        await ledger.async_rebuild(
            client, channels, reading_day, rebuild_now, bootstrap_consumption
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Refoss in-memory ledger bootstrap failed; entities will still be created with the current empty/partial ledger: %s",
            err,
            exc_info=True,
        )

    coordinator: DataUpdateCoordinator[dict[int, ChannelData]]
    coordinator = DataUpdateCoordinator(
        hass,
        logger=_LOGGER,
        name=f"{DOMAIN}_{uuid}",
        update_interval=scan_interval,
        update_method=lambda: _async_update_data(
            client,
            ledger,
            channels,
            reading_day,
            polling_lock,
            lambda: coordinator.data,
        ),
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:  # noqa: BLE001
        # This should be rare because _async_update_data also returns safe empty
        # data.  Keep it as a final guard so entity creation never aborts.
        _LOGGER.warning(
            "Refoss first refresh failed; creating entities with unavailable state: %s",
            err,
            exc_info=True,
        )
        coordinator.data = _empty_channel_data(channels, f"first_refresh_failed: {err}")

    entities: list[SensorEntity] = []
    for channel in channels:
        # Keep creation grouped by channel: billing energy, today energy, then instant sensors.
        entities.append(
            RefossEnergySensor(
                coordinator=coordinator,
                name=name,
                uuid=uuid,
                channel=channel,
                sensor_kind="billing",
                reading_day=reading_day,
            )
        )
        entities.append(
            RefossEnergySensor(
                coordinator=coordinator,
                name=name,
                uuid=uuid,
                channel=channel,
                sensor_kind="today",
            )
        )
        for sensor_type in INSTANT_SENSOR_TYPES:
            entities.append(
                RefossInstantSensor(
                    coordinator=coordinator,
                    name=name,
                    uuid=uuid,
                    channel=channel,
                    sensor_type=sensor_type,
                )
            )

    _LOGGER.debug(
        "Refoss entity creation order: %s",
        [getattr(entity, "_attr_name", None) for entity in entities],
    )
    async_add_entities(entities)
    _LOGGER.info(
        "Refoss sensor setup completed: uuid=%s entities=%s ledger_id=%s ledger_rows=%s",
        uuid,
        len(entities),
        ledger.ledger_id,
        ledger.total_row_count(),
    )


async def _async_update_data(
    client: RefossClient,
    ledger: HourlyEnergyLedger,
    channels: list[int],
    reading_day: int | str,
    polling_lock: asyncio.Lock,
    previous_data_getter: Any | None = None,
) -> dict[int, ChannelData]:
    """Fetch one update for all configured channels."""

    async with polling_lock:
        now = dt_util.now()
        period = _billing_period(reading_day, now)
        today = now.date()
        yesterday = today - timedelta(days=1)
        previous_data = (
            previous_data_getter()
            if callable(previous_data_getter)
            else None
        )

        try:
            current_electricity, current_consumption = await client.async_current_snapshot(
                channels
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Refoss update failed before data parse; returning unavailable values: %s",
                err,
                exc_info=True,
            )
            return _empty_channel_data(channels, f"update_failed: {err}")

        try:
            await ledger.async_append_runtime_rows(
                channels, period.start_date, current_consumption
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Refoss runtime ledger append failed; sensor values will continue without this row: %s",
                err,
                exc_info=True,
            )

        data: dict[int, ChannelData] = {}
        for channel in channels:
            electricity = current_electricity.get(channel)
            consumption = current_consumption.get(channel)
            errors: list[str] = []

            if electricity is None:
                errors.append("ElectricityX missing")
                electricity = ChannelElectricity()
            if consumption is None:
                errors.append("ConsumptionH missing")
                consumption = ChannelConsumption(raw_total_wh=None, rows=[])

            completed_wh = ledger.completed_sum_wh(channel, period.start_date, yesterday)
            ledger_row_count = ledger.row_count(channel, period.start_date, yesterday)
            unstable_gap, latest_row, previous_row, gap_seconds = (
                _consumptionh_unstable_latest_gap(consumption)
            )

            if unstable_gap:
                previous_channel_data = (previous_data or {}).get(channel)
                errors.append(
                    "ConsumptionH unstable latest gap"
                    + (f" {gap_seconds}s" if gap_seconds is not None else "")
                )
                _LOGGER.warning(
                    "Refoss ConsumptionH energy update ignored due to unstable latest gap: "
                    "channel=%s latest=%s previous=%s gap_seconds=%s previous_energy_present=%s",
                    channel,
                    _format_consumption_row(latest_row),
                    _format_consumption_row(previous_row),
                    gap_seconds,
                    previous_channel_data is not None
                    and (
                        previous_channel_data.net_wh is not None
                        or previous_channel_data.today_wh is not None
                    ),
                )

                data[channel] = ChannelData(
                    net_wh=previous_channel_data.net_wh
                    if previous_channel_data is not None
                    else None,
                    today_wh=previous_channel_data.today_wh
                    if previous_channel_data is not None
                    else None,
                    current_mconsume_wh=electricity.mconsume_wh,
                    completed_history_wh=completed_wh,
                    raw_total_wh=previous_channel_data.raw_total_wh
                    if previous_channel_data is not None
                    else None,
                    ledger_row_count=ledger_row_count,
                    today_row_count=previous_channel_data.today_row_count
                    if previous_channel_data is not None
                    else 0,
                    current_ma=electricity.current_ma,
                    voltage_mv=electricity.voltage_mv,
                    power_mw=electricity.power_mw,
                    factor=electricity.factor,
                    source="held_previous_energy_unstable_consumptionh_gap"
                    if previous_channel_data is not None
                    else "unstable_consumptionh_gap_no_previous_energy",
                    error="; ".join(errors) if errors else None,
                )

                _LOGGER.debug(
                    "Refoss channel update held previous energy: channel=%s net_wh=%s today_wh=%s "
                    "ledger_rows=%s today_rows=%s mconsume=%s power_mw=%s voltage_mv=%s current_ma=%s error=%s",
                    channel,
                    data[channel].net_wh,
                    data[channel].today_wh,
                    data[channel].ledger_row_count,
                    data[channel].today_row_count,
                    data[channel].current_mconsume_wh,
                    data[channel].power_mw,
                    data[channel].voltage_mv,
                    data[channel].current_ma,
                    data[channel].error,
                )
                continue

            today_wh = _consumption_day_sum(consumption, today)
            today_row_count = sum(
                1 for row in consumption.safe_rows if row.local_dt.date() == today
            )

            # Month/today energy can be calculated only when local ConsumptionH
            # has at least one current-day row or ledger has completed rows.
            energy_available = bool(today_row_count or ledger_row_count)
            net_wh = completed_wh + today_wh if energy_available else None

            data[channel] = ChannelData(
                net_wh=net_wh,
                today_wh=today_wh if today_row_count else None,
                current_mconsume_wh=electricity.mconsume_wh,
                completed_history_wh=completed_wh,
                raw_total_wh=consumption.raw_total_wh,
                ledger_row_count=ledger_row_count,
                today_row_count=today_row_count,
                current_ma=electricity.current_ma,
                voltage_mv=electricity.voltage_mv,
                power_mw=electricity.power_mw,
                factor=electricity.factor,
                source="ledger_plus_local_consumptionh_data"
                if energy_available
                else "no_current_consumption_rows",
                error="; ".join(errors) if errors else None,
            )

            _LOGGER.debug(
                "Refoss channel update: channel=%s net_wh=%s today_wh=%s ledger_rows=%s today_rows=%s mconsume=%s power_mw=%s voltage_mv=%s current_ma=%s error=%s",
                channel,
                data[channel].net_wh,
                data[channel].today_wh,
                ledger_row_count,
                today_row_count,
                data[channel].current_mconsume_wh,
                data[channel].power_mw,
                data[channel].voltage_mv,
                data[channel].current_ma,
                data[channel].error,
            )

        return data


def _consumptionh_channel_batches(channels: list[int]) -> list[list[int]]:
    """Return ConsumptionH channel batches in A1/B1/C1 then A2/B2/C2 order.

    Refoss EM06 channel labels are mapped as 1=A1, 2=B1, 3=C1,
    4=A2, 5=B2, and 6=C2. Keeping the first group separate from the
    second group lowers the local API's concurrent request pressure while
    preserving the preferred electrical phase/order.
    """

    batches: list[list[int]] = []
    seen: set[int] = set()

    for preferred_group in ([1, 2, 3], [4, 5, 6]):
        batch = [
            channel
            for channel in preferred_group
            if channel in channels and channel not in seen
        ]
        if batch:
            batches.append(batch)
            seen.update(batch)

    remaining = [channel for channel in channels if channel not in seen]
    for index in range(0, len(remaining), CONSUMPTIONH_BATCH_SIZE):
        batches.append(remaining[index : index + CONSUMPTIONH_BATCH_SIZE])

    return batches


def _empty_channel_data(channels: list[int], error: str) -> dict[int, ChannelData]:
    """Return unavailable placeholder data for all channels."""

    return {
        channel: ChannelData(source="unavailable", error=error) for channel in channels
    }


def _billing_period(
    reading_day: int | str, now: datetime | None = None
) -> BillingPeriod:
    """Return local timestamp boundaries for the current billing period."""

    now = now or dt_util.now()
    day_this_month = _reading_day_for_month(reading_day, now.year, now.month)
    local_start = datetime(now.year, now.month, day_this_month, tzinfo=now.tzinfo)
    if now < local_start:
        prev_month = now.month - 1 or 12
        prev_year = now.year if now.month > 1 else now.year - 1
        prev_day = _reading_day_for_month(reading_day, prev_year, prev_month)
        local_start = datetime(prev_year, prev_month, prev_day, tzinfo=now.tzinfo)

    return BillingPeriod(local_start=int(local_start.timestamp()), end=int(now.timestamp()))


def _normalize_reading_day(value: Any) -> int | str:
    """Normalize configured reading day values."""

    if value == READING_DAY_LAST:
        return READING_DAY_LAST
    day = int(value)
    if day >= 28:
        return READING_DAY_LAST
    return day


def _scan_interval_from_config(config: dict[str, Any]) -> timedelta:
    """Return the configured polling interval."""

    value = config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
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


class RefossClient:
    """Small client for Refoss cloud history and local EM06 control APIs."""

    def __init__(
        self,
        session: Any,
        email: str,
        password: str,
        uuid: str,
        host: str | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._uuid = uuid
        self._host = host
        self._token: str | None = None
        self._key: str | None = None
        self._api_base = DEFAULT_API_BASE
        self._login_lock = asyncio.Lock()

    async def async_resolve_local_host(self) -> str:
        """Resolve or return the configured local API host."""

        if self._host:
            return self._host

        host = await asyncio.to_thread(_find_host_by_uuid_mac, self._uuid)
        if host:
            self._host = host
            return host

        raise RuntimeError(
            "Refoss local host is not configured and could not be found in ARP"
        )

    async def async_ensure_login(self, reason: str = "unspecified") -> None:
        """Ensure cloud token/key are available before local API signing."""

        if self._token and self._key:
            return

        async with self._login_lock:
            if self._token and self._key:
                return

            _LOGGER.debug(
                "Refoss cloud login required before local request: reason=%s uuid=%s token_present=%s key_present=%s",
                reason,
                self._uuid,
                self._token is not None,
                self._key is not None,
            )
            await self.async_login()

    async def async_refresh_login(self, reason: str = "unspecified") -> None:
        """Refresh cloud token/key after local sign failures."""

        async with self._login_lock:
            _LOGGER.warning(
                "Refoss refreshing cloud login/key: reason=%s uuid=%s",
                reason,
                self._uuid,
            )
            self._token = None
            self._key = None
            await self.async_login()

    async def async_current_snapshot(
        self, channels: list[int]
    ) -> tuple[dict[int, ChannelElectricity], dict[int, ChannelConsumption]]:
        """Fetch ElectricityX and ConsumptionH through the local /public API.

        Local Refoss devices sometimes return no GETACK right after a power
        interruption.  This method gathers requests independently and returns
        partial data instead of raising for every missing channel.
        """

        host = await self.async_resolve_local_host()
        try:
            await self.async_ensure_login("local_snapshot")
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Refoss cloud login/key preparation failed before local snapshot; local requests may return sign error: %s",
                err,
                exc_info=True,
            )
        _LOGGER.debug(
            "Refoss local snapshot started: host=%s uuid=%s channels=%s key_present=%s",
            host,
            self._uuid,
            channels,
            self._key is not None,
        )

        electricity: dict[int, ChannelElectricity] = {}
        consumption: dict[int, ChannelConsumption] = {}

        try:
            electricity_response = await self._async_local_get(
                host,
                "Appliance.Control.ElectricityX",
                {"electricity": [{"channel": 65535}]},
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Refoss local ElectricityX request failed; instantaneous sensors may be unavailable: %s",
                err,
                exc_info=True,
            )
        else:
            rows = electricity_response.get("payload", {}).get("electricity", [])
            electricity = _parse_electricity_rows(rows)
            _LOGGER.debug(
                "Refoss local ElectricityX parsed: requested=%s received_channels=%s row_count=%s",
                channels,
                sorted(electricity),
                len(rows or []),
            )

        consumption_batches = _consumptionh_channel_batches(channels)
        for batch_index, channel_batch in enumerate(consumption_batches, start=1):
            _LOGGER.debug(
                "Refoss local ConsumptionH batch started: batch=%s/%s channels=%s",
                batch_index,
                len(consumption_batches),
                channel_batch,
            )
            tasks = [
                self._async_local_get(
                    host,
                    "Appliance.Control.ConsumptionH",
                    {"consumptionH": [{"channel": channel}]},
                )
                for channel in channel_batch
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            for channel, response in zip(channel_batch, responses, strict=True):
                if isinstance(response, Exception):
                    _LOGGER.warning(
                        "Refoss local ConsumptionH request failed for channel %s; this channel may be unavailable: %s",
                        channel,
                        response,
                        exc_info=response,
                    )
                    continue
                parsed = _parse_consumption_rows(
                    response.get("payload", {}).get("consumptionH", [])
                )
                if channel not in parsed:
                    _LOGGER.warning(
                        "Refoss local ConsumptionH returned no parsable data for channel %s",
                        channel,
                    )
                consumption.update(parsed)

            _LOGGER.debug(
                "Refoss local ConsumptionH batch completed: batch=%s/%s channels=%s parsed_channels=%s",
                batch_index,
                len(consumption_batches),
                channel_batch,
                sorted(channel for channel in channel_batch if channel in consumption),
            )
            if batch_index < len(consumption_batches):
                await asyncio.sleep(CONSUMPTIONH_BATCH_DELAY)

        for channel in channels:
            if channel not in electricity:
                _LOGGER.debug("Refoss ElectricityX missing for channel %s", channel)
            if channel not in consumption:
                _LOGGER.debug("Refoss ConsumptionH missing for channel %s", channel)

        _LOGGER.debug(
            "Refoss local snapshot completed: electricity_channels=%s consumption_channels=%s",
            sorted(electricity),
            sorted(consumption),
        )
        return electricity, consumption

    async def async_electric_history_1h_batch(
        self, channels: list[int], start_day: date, end_day: date
    ) -> dict[int, list[dict[str, Any]]]:
        """Fetch cloud HTTP 1h history for all channels."""

        if end_day < start_day:
            return {channel: [] for channel in channels}

        start_time = _cloud_history_day_start_timestamp(start_day)
        end_time = _cloud_history_day_end_timestamp(end_day)
        _LOGGER.debug(
            "Refoss cloud history batch started: channels=%s start_day=%s end_day=%s start_time=%s end_time=%s",
            channels,
            start_day,
            end_day,
            start_time,
            end_time,
        )
        responses = await asyncio.gather(
            *[
                self._async_electric_history(channel, start_time, end_time, "1h")
                for channel in channels
            ],
            return_exceptions=True,
        )
        result: dict[int, list[dict[str, Any]]] = {}
        for channel, response in zip(channels, responses, strict=True):
            if isinstance(response, Exception):
                _LOGGER.warning(
                    "Refoss cloud history fetch failed for channel %s: %s",
                    channel,
                    response,
                    exc_info=response,
                )
                result[channel] = []
            else:
                result[channel] = response
                _LOGGER.debug(
                    "Refoss cloud history rows received: channel=%s rows=%s",
                    channel,
                    len(response),
                )
        return result

    async def _async_local_get(
        self, host: str, namespace: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Send one local /public GET request.

        The local EM06 API validates the request signature with the cloud login
        key. After a device reboot or power outage, requests signed without a
        fresh key can return ERROR 5001 / sign error. Refresh the login key and
        retry once before treating the request as unavailable.
        """

        last_err: ClientError | OSError | TimeoutError | RuntimeError | None = None
        refreshed_after_sign_error = False

        for attempt in range(3):
            try:
                if self._key is None:
                    await self.async_ensure_login(f"local_{namespace}")

                message = self._local_request_message(host, namespace, payload)
                _LOGGER.debug(
                    "Refoss local request: namespace=%s host=%s attempt=%s payload_keys=%s message_id=%s key_present=%s",
                    namespace,
                    host,
                    attempt + 1,
                    list(payload.keys()),
                    message.get("header", {}).get("messageId"),
                    self._key is not None,
                )
                async with self._session.post(
                    f"http://{host}/public",
                    json=message,
                    timeout=LOCAL_API_TIMEOUT,
                ) as resp:
                    resp.raise_for_status()
                    response = await resp.json()
                header = response.get("header", {})
                method = header.get("method")
                payload_data = response.get("payload") or {}
                error_summary = self._local_error_summary(response)
                _LOGGER.debug(
                    "Refoss local response: namespace=%s attempt=%s method=%s payload_keys=%s error=%s",
                    namespace,
                    attempt + 1,
                    method,
                    list(payload_data.keys()),
                    error_summary,
                )
                if method != "GETACK":
                    if (
                        self._is_local_sign_error(response)
                        and not refreshed_after_sign_error
                    ):
                        refreshed_after_sign_error = True
                        _LOGGER.warning(
                            "Refoss local %s returned sign error; refreshing cloud key and retrying once: %s",
                            namespace,
                            error_summary,
                        )
                        await self.async_refresh_login(f"local_sign_error_{namespace}")
                        await asyncio.sleep(0.2)
                        continue

                    raise RuntimeError(
                        f"Refoss local {namespace} returned no GETACK"
                        + (f": {error_summary}" if error_summary else "")
                    )
                return response
            except (ClientError, OSError, TimeoutError, RuntimeError) as err:
                last_err = err
                _LOGGER.debug(
                    "Refoss local request failed: namespace=%s host=%s attempt=%s/3 error=%s",
                    namespace,
                    host,
                    attempt + 1,
                    err,
                    exc_info=True,
                )
                if attempt < 2:
                    await asyncio.sleep(1)

        if last_err is not None:
            raise last_err
        raise RuntimeError(f"Refoss local {namespace} request failed")

    @staticmethod
    def _local_error_summary(response: dict[str, Any]) -> str | None:
        """Return a compact local API error summary for logs."""

        error = (response.get("payload") or {}).get("error")
        if not isinstance(error, dict):
            return None
        code = error.get("code")
        detail = error.get("detail") or error.get("message")
        if code is None and detail is None:
            return None
        return f"code={code} detail={detail}"

    @staticmethod
    def _is_local_sign_error(response: dict[str, Any]) -> bool:
        """Return true when the local API reports a signature error."""

        error = (response.get("payload") or {}).get("error")
        if not isinstance(error, dict):
            return False
        code = str(error.get("code") or "")
        detail = str(error.get("detail") or error.get("message") or "").lower()
        return code == "5001" or "sign" in detail

    def _local_request_message(
        self, host: str, namespace: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Build one local /public request."""

        timestamp = int(time.time())
        message_id = hashlib.md5(
            f"{namespace}:{timestamp}:{random.random()}".encode()
        ).hexdigest()
        sign_source = f"{message_id}{self._key or ''}{timestamp}"
        return {
            "header": {
                "from": f"http://{host}/config",
                "messageId": message_id,
                "method": "GET",
                "namespace": namespace,
                "payloadVersion": 1,
                "sign": hashlib.md5(sign_source.encode()).hexdigest(),
                "timestamp": timestamp,
                "triggerSrc": "AndroidLocal",
                "uuid": self._uuid,
            },
            "payload": payload,
        }

    async def _async_electric_history(
        self, channel: int, start_time: int, end_time: int, step: str
    ) -> list[dict[str, Any]]:
        """Fetch electric history rows for one channel from the cloud."""

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

        response = await self._async_post(
            "/historage/v1/deviceTelemetry/query", payload, token=self._token
        )
        if response.get("apiStatus") in (1001, 1002, 5001):
            _LOGGER.debug(
                "Refoss token appears expired or invalid during history fetch; logging in again: status=%s",
                response.get("apiStatus"),
            )
            await self.async_login()
            response = await self._async_post(
                "/historage/v1/deviceTelemetry/query", payload, token=self._token
            )

        self._raise_for_api_error(response)
        return (
            response.get("data", {})
            .get("electricH", {})
            .get("result", {})
            .get("stepSumElectricH", [])
        )

    async def async_login(self) -> dict[str, Any]:
        """Log in and cache the Refoss cloud token for history calls."""

        _LOGGER.debug("Refoss cloud login started: email=%s uuid=%s", self._email, self._uuid)
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
        self._key = data["key"]
        self._api_base = data.get("domain") or self._api_base
        _LOGGER.debug("Refoss cloud login completed: api_base=%s", self._api_base)
        return data

    async def async_devices(self) -> list[dict[str, Any]]:
        """Return devices from the Refoss cloud account."""

        if self._token is None:
            await self.async_login()

        response = await self._async_post("/v1/Device/devList", {}, token=self._token)
        self._raise_for_api_error(response)
        devices = response.get("data", [])
        _LOGGER.debug("Refoss cloud devices received: count=%s", len(devices))
        return devices

    async def _async_post(
        self, path: str, params: Any, token: str | None = None
    ) -> dict[str, Any]:
        """Send one signed Refoss cloud HTTP request."""

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
        for attempt in range(3):
            try:
                _LOGGER.debug("Refoss HTTP request: path=%s attempt=%s", path, attempt + 1)
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
                    result = await resp.json()
                _LOGGER.debug(
                    "Refoss HTTP response: path=%s attempt=%s apiStatus=%s keys=%s",
                    path,
                    attempt + 1,
                    result.get("apiStatus"),
                    list(result.keys()),
                )
                return result
            except (ClientError, OSError, TimeoutError) as err:
                last_err = err
                _LOGGER.debug(
                    "Refoss HTTP request failed: path=%s attempt=%s/3 error=%s",
                    path,
                    attempt + 1,
                    err,
                    exc_info=True,
                )
                if attempt < 2:
                    await asyncio.sleep(2)

        if last_err is not None:
            raise last_err
        raise RuntimeError("Refoss HTTP request failed")

    @staticmethod
    def _raise_for_api_error(response: dict[str, Any]) -> None:
        """Raise when a Refoss cloud API response reports an error."""

        if response.get("apiStatus") != 0:
            raise RuntimeError(
                f"Refoss API error {response.get('apiStatus')}: {response.get('info')}"
            )


def _parse_electricity_rows(rows: list[dict[str, Any]]) -> dict[int, ChannelElectricity]:
    """Convert ElectricityX payload rows into channel objects."""

    result: dict[int, ChannelElectricity] = {}
    for row in rows or []:
        if "channel" not in row:
            continue
        try:
            channel = int(row["channel"])
        except (TypeError, ValueError):
            continue
        result[channel] = ChannelElectricity(
            mconsume_wh=_safe_int(row.get("mConsume")),
            current_ma=_safe_int(row.get("current")),
            voltage_mv=_safe_int(row.get("voltage")),
            power_mw=_safe_int(row.get("power")),
            factor=_safe_float(row.get("factor")),
        )
    return result


def _parse_consumption_rows(rows: list[dict[str, Any]]) -> dict[int, ChannelConsumption]:
    """Convert ConsumptionH payload rows into channel objects."""

    result: dict[int, ChannelConsumption] = {}
    for row in rows or []:
        if "channel" not in row:
            continue
        try:
            channel = int(row["channel"])
        except (TypeError, ValueError):
            continue
        parsed_rows: list[ConsumptionRow] = []
        skipped = 0
        for item in row.get("data") or []:
            item_dt = _timestamp_local_datetime(item.get("timestamp"))
            timestamp = _normalize_timestamp(item.get("timestamp"))
            if item_dt is None or timestamp is None:
                skipped += 1
                continue
            parsed_rows.append(
                ConsumptionRow(
                    timestamp=timestamp,
                    local_dt=item_dt,
                    value_wh=int(item.get("value") or 0),
                )
            )
        parsed_rows.sort(key=lambda item: item.timestamp, reverse=True)
        result[channel] = ChannelConsumption(
            raw_total_wh=_safe_int(row.get("total")),
            rows=parsed_rows,
        )
        _LOGGER.debug(
            "Refoss ConsumptionH parsed: channel=%s rows=%s skipped=%s raw_total_wh=%s",
            channel,
            len(parsed_rows),
            skipped,
            result[channel].raw_total_wh,
        )
    return result


def _safe_int(value: Any) -> int | None:
    """Parse an integer while preserving missing values as None."""

    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    """Parse a float while preserving missing values as None."""

    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _consumption_day_sum(consumption: ChannelConsumption, day: date) -> int:
    """Return the raw ConsumptionH row sum for one local date."""

    return sum(row.value_wh for row in consumption.safe_rows if row.local_dt.date() == day)


def _consumptionh_unstable_latest_gap(
    consumption: ChannelConsumption,
) -> tuple[bool, ConsumptionRow | None, ConsumptionRow | None, int | None]:
    """Return true when latest and previous ConsumptionH rows are too far apart.

    Around the hourly rollover the EM06 can temporarily return a new latest row
    without the expected completed row.  When the newest row and the second
    newest row are one hour or more apart, do not use this poll for energy
    totals; keep the previous energy values and wait for the next poll.
    """

    rows = sorted(consumption.safe_rows, key=lambda item: item.timestamp, reverse=True)
    if len(rows) < 2:
        return False, rows[0] if rows else None, None, None

    latest = rows[0]
    previous = rows[1]
    gap_seconds = latest.timestamp - previous.timestamp
    return (
        gap_seconds >= CONSUMPTIONH_UNSTABLE_GAP_SECONDS,
        latest,
        previous,
        gap_seconds,
    )


def _format_consumption_row(row: ConsumptionRow | None) -> str | None:
    """Return a compact ConsumptionH row string for logs."""

    if row is None:
        return None
    return f"{row.local_dt:%Y-%m-%d %H:%M:%S}|ts={row.timestamp}|wh={row.value_wh}"


def _consumption_rows_except_latest(
    consumption: ChannelConsumption, day: date
) -> list[ConsumptionRow]:
    """Return today's rows excluding the latest row only."""

    rows = [row for row in consumption.safe_rows if row.local_dt.date() == day]
    if not rows:
        return []
    latest_timestamp = max(row.timestamp for row in rows)
    skipped_latest = False
    result: list[ConsumptionRow] = []
    for row in rows:
        if row.timestamp == latest_timestamp and not skipped_latest:
            skipped_latest = True
            continue
        result.append(row)
    return result


def _row_net_wh(row: dict[str, Any]) -> int:
    """Return net Wh for one cloud history row."""

    value = row.get("value")
    if value is not None:
        return int(value)

    valcons = int(row.get("valcons") or 0)
    valprod = int(row.get("valprod") or 0)
    return valcons + valprod


def _row_local_datetime(row: dict[str, Any]) -> datetime | None:
    """Return a shifted local datetime for a cloud history row."""

    for key in ("timestamp", "time", "ts"):
        row_dt = _timestamp_shifted_local_datetime(
            row.get(key), shift=-HISTORY_BUCKET_SHIFT
        )
        if row_dt is not None:
            return row_dt
    return None


def _timestamp_local_datetime(value: Any) -> datetime | None:
    """Convert a timestamp to Home Assistant local time."""

    return _timestamp_shifted_local_datetime(value)


def _timestamp_shifted_local_datetime(
    value: Any, shift: timedelta = timedelta(0)
) -> datetime | None:
    """Convert a timestamp to local time and apply an optional shift."""

    timestamp = _normalize_timestamp(value)
    if timestamp is None:
        return None
    return (
        datetime.fromtimestamp(timestamp, UTC).astimezone(dt_util.DEFAULT_TIME_ZONE)
        + shift
    )


def _normalize_timestamp(value: Any) -> int | None:
    """Return a seconds timestamp, accepting millisecond timestamps too."""

    if value in (None, ""):
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    return timestamp


def _cloud_history_day_start_timestamp(day: date) -> int:
    """Return the cloud history start timestamp for one local day."""

    return int(
        (
            datetime(day.year, day.month, day.day, tzinfo=dt_util.DEFAULT_TIME_ZONE)
            + HISTORY_BUCKET_SHIFT
        ).timestamp()
    )


def _cloud_history_day_end_timestamp(day: date) -> int:
    """Return the cloud history end timestamp for one local day."""

    return _cloud_history_day_start_timestamp(day + timedelta(days=1)) - 1



def _find_host_by_uuid_mac(uuid: str) -> str | None:
    """Try to find the local host by the MAC address embedded in the UUID."""

    compact_mac = uuid[-12:].lower()
    if len(compact_mac) != 12:
        return None

    colon_mac = ":".join(compact_mac[index : index + 2] for index in range(0, 12, 2))
    hyphen_mac = colon_mac.replace(":", "-")

    proc_arp = Path("/proc/net/arp")
    if proc_arp.exists():
        try:
            for line in proc_arp.read_text(encoding="utf-8").splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[3].lower() == colon_mac:
                    return parts[0]
        except OSError:
            pass

    try:
        result = subprocess.run(
            ["arp", "-a"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    for line in result.stdout.splitlines():
        lower = line.lower()
        if colon_mac in lower or hyphen_mac in lower:
            for part in line.replace("(", " ").replace(")", " ").split():
                if _looks_like_ipv4(part):
                    return part
    return None


def _looks_like_ipv4(value: str) -> bool:
    """Return true when a string looks like an IPv4 address."""

    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(part) <= 255 for part in parts)
    except ValueError:
        return False


def _channel_label(channel: int) -> str:
    """Return the user-facing EM06 channel label."""

    return CHANNEL_LABELS.get(channel, f"C{channel}")


def _slugify_entity_id_part(value: str) -> str:
    """Return a Home Assistant object-id-safe slug part."""

    slug = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return re.sub(r"_+", "_", slug) or "em06"


def _refoss_object_id_prefix(name: str) -> str:
    """Return an object-id prefix that always starts with refoss_."""

    base = _slugify_entity_id_part(name or DEFAULT_NAME)
    if base == "refoss" or base.startswith("refoss_"):
        return base
    return f"refoss_{base}"


def _refoss_display_name_prefix(name: str) -> str:
    """Return a display name prefix that always starts with Refoss."""

    base = (name or DEFAULT_NAME).strip() or DEFAULT_NAME
    if base.lower().startswith("refoss"):
        return base
    return f"Refoss {base}"


def _device_info(name: str, uuid: str) -> dict[str, Any]:
    """Return the shared Home Assistant device_info payload."""

    return {
        "identifiers": {(DOMAIN, uuid)},
        "name": name,
        "manufacturer": "Refoss",
        "model": "EM06",
    }


class RefossEnergySensor(CoordinatorEntity, SensorEntity):
    """Refoss billing-period or current-day energy sensor."""

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
        sensor_kind: str,
        reading_day: int | str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._is_today = sensor_kind == "today"
        self._reading_day = _normalize_reading_day(reading_day or 1)
        sensor_slug = TODAY_SENSOR_SLUG if self._is_today else SENSOR_SLUG
        sensor_label = TODAY_SENSOR_LABEL if self._is_today else SENSOR_LABEL
        channel_label = _channel_label(channel)
        channel_slug = channel_label.lower()

        self._attr_unique_id = f"{uuid}_{channel_slug}_{sensor_slug}"
        self._attr_name = f"{_refoss_display_name_prefix(name)} {channel_label} {sensor_label}"
        self._attr_device_info = _device_info(name, uuid)

        _LOGGER.debug(
            "Refoss energy sensor naming: channel=%s sensor_slug=%s unique_id=%s name=%s",
            channel,
            sensor_slug,
            self._attr_unique_id,
            self._attr_name,
        )

    def _current_data(self) -> ChannelData | None:
        """Return the latest coordinator data for this channel."""

        return (self.coordinator.data or {}).get(self._channel)

    @property
    def available(self) -> bool:
        """Return if entity is available."""

        data = self._current_data()
        value = data.today_wh if self._is_today and data else data.net_wh if data else None
        return value is not None

    @property
    def native_value(self) -> float | None:
        """Return energy sensor value in kWh."""

        data = self._current_data()
        if data is None:
            return None
        value = data.today_wh if self._is_today else data.net_wh
        return _kwh_or_none(value)

    @property
    def last_reset(self) -> datetime:
        """Return the reset point for this total sensor."""

        now = dt_util.now()
        if self._is_today:
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        period = _billing_period(self._reading_day, now)
        return datetime.fromtimestamp(period.local_start, dt_util.DEFAULT_TIME_ZONE)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""

        data = self._current_data()
        attrs: dict[str, Any] = {
            "channel": self._channel,
            "channel_label": _channel_label(self._channel),
        }
        if data is None:
            return attrs

        if self._is_today:
            attrs.update(
                {
                    "source": data.source,
                    "error": data.error,
                    "today_row_count": data.today_row_count,
                    "raw_total_kwh": _kwh_or_none(data.raw_total_wh),
                }
            )
            return attrs

        period = _billing_period(self._reading_day, dt_util.now())
        attrs.update(
            {
                "reading_day": self._reading_day,
                "period_start": datetime.fromtimestamp(
                    period.local_start, dt_util.DEFAULT_TIME_ZONE
                ).isoformat(),
                "period_end": datetime.fromtimestamp(
                    period.end, dt_util.DEFAULT_TIME_ZONE
                ).isoformat(),
                "source": data.source,
                "error": data.error,
                "current_mconsume_kwh": _kwh_or_none(data.current_mconsume_wh),
                "completed_history_kwh": _kwh_or_none(data.completed_history_wh),
                "today_kwh": _kwh_or_none(data.today_wh),
                "consumptionh_total_kwh": _kwh_or_none(data.raw_total_wh),
                "ledger_row_count": data.ledger_row_count,
                "today_row_count": data.today_row_count,
            }
        )
        return attrs

    async def async_update(self) -> None:
        """Update the entity."""

        await self.coordinator.async_request_refresh()


class RefossInstantSensor(CoordinatorEntity, SensorEntity):
    """Refoss instantaneous electricity sensor."""

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
        self._channel = channel
        self._sensor_type = sensor_type
        meta = INSTANT_SENSOR_TYPES[sensor_type]
        channel_label = _channel_label(channel)
        channel_slug = channel_label.lower()
        object_id_prefix = _refoss_object_id_prefix(name)
        object_id = f"{object_id_prefix}_{channel_slug}_{sensor_type}"

        # Change the instant-sensor unique_id namespace so Home Assistant does
        # not restore the old registry entry such as sensor.em06_a1_power.
        # The visible entity_id is also set explicitly for fresh installs.
        self._attr_unique_id = f"{uuid}_{object_id}"
        self._attr_suggested_object_id = object_id
        self.entity_id = f"sensor.{object_id}"
        self._attr_name = f"{_refoss_display_name_prefix(name)} {channel_label} {meta['label']}"
        self._attr_device_info = _device_info(name, uuid)
        self._attr_device_class = meta["device_class"]
        self._attr_native_unit_of_measurement = meta["unit"]
        self._attr_suggested_display_precision = meta["precision"]

        _LOGGER.debug(
            "Refoss instant sensor naming: channel=%s sensor_type=%s unique_id=%s suggested_object_id=%s entity_id=%s name=%s",
            channel,
            sensor_type,
            self._attr_unique_id,
            self._attr_suggested_object_id,
            self.entity_id,
            self._attr_name,
        )

    def _current_data(self) -> ChannelData | None:
        """Return the latest coordinator data for this channel."""

        return (self.coordinator.data or {}).get(self._channel)

    @property
    def available(self) -> bool:
        """Return if entity is available."""

        return self.native_value is not None

    @property
    def native_value(self) -> float | None:
        """Return the instantaneous sensor value."""

        data = self._current_data()
        if data is None:
            return None
        if self._sensor_type == "power":
            return None if data.power_mw is None else round(data.power_mw / 1000, 1)
        if self._sensor_type == "voltage":
            return None if data.voltage_mv is None else round(data.voltage_mv / 1000, 1)
        if self._sensor_type == "current":
            return None if data.current_ma is None else round(data.current_ma / 1000, 3)
        if self._sensor_type == "power_factor":
            return None if data.factor is None else round(data.factor, 3)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""

        data = self._current_data()
        attrs: dict[str, Any] = {
            "channel": self._channel,
            "channel_label": _channel_label(self._channel),
            "sensor_type": self._sensor_type,
        }
        if data is not None:
            attrs.update({"source": data.source, "error": data.error})
        return attrs

    async def async_update(self) -> None:
        """Update the entity."""

        await self.coordinator.async_request_refresh()


def _kwh_or_none(value_wh: int | None) -> float | None:
    """Convert Wh to kWh while preserving None."""

    if value_wh is None:
        return None
    return round(value_wh / 1000, 3)
