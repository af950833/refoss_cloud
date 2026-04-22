"""Refoss Cloud energy history sensors."""

from __future__ import annotations

import base64
from calendar import monthrange
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Config entry/YAML에서 사용하는 내부 키들이다.
# CONF_READING_DAY는 1~27 또는 "last"를 받을 수 있다.
CONF_CHANNELS = "channels"
CONF_READING_DAY = "reading_day"
CONF_UUID = "uuid"
READING_DAY_LAST = "last"

DEFAULT_NAME = "Refoss Cloud"
DEFAULT_SCAN_INTERVAL = timedelta(seconds=15)
DEFAULT_API_BASE = "https://iotx.refoss.net"
# Refoss/Meross cloud HTTP API 서명에 쓰이는 고정 salt.
SECRET = "23x17ahWarFH6w29"
SENSOR_SLUG = "billing_month_energy"
SENSOR_LABEL = "Billing month energy"
TODAY_SENSOR_SLUG = "this_day_energy"
TODAY_SENSOR_LABEL = "This Day Energy"

# EM06은 내부적으로 채널을 1~6으로 다루지만, 앱/실제 배선 표기는
# A1/B1/C1/A2/B2/C2가 더 자연스럽다. unique_id는 번호 기반으로 유지한다.
CHANNEL_LABELS = {
    1: "A1",
    2: "B1",
    3: "C1",
    4: "A2",
    5: "B2",
    6: "C2",
}

# ElectricityX MQTT 응답에는 현재 전력/전압/역률/전류가 한 번에 들어온다.
# 아래 정의로 같은 MQTT 응답을 여러 센서가 나눠 쓰게 만든다.
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

    # Refoss 응답 단위:
    # mConsume = Wh, current = mA, voltage = mV, power = mW, factor = 실수 PF.
    mconsume_wh: int
    current_ma: int
    voltage_mv: int
    power_mw: int
    factor: float


@dataclass(slots=True)
class ChannelData:
    """Energy and instantaneous values for a Refoss channel."""

    # net_wh는 HA에 표시할 검침월 순사용량이다. 태양광 채널처럼 순방향보다
    # 역방향이 크면 음수가 될 수 있으므로 abs 처리는 하지 않는다.
    net_wh: int
    today_wh: int
    current_mconsume_wh: int
    month_prefix_wh: int
    previous_period_wh: int
    history_rows: int
    today_prefix_wh: int
    today_history_rows: int
    current_ma: int
    voltage_mv: int
    power_mw: int
    factor: float


@dataclass(slots=True)
class HistoryPrefix:
    """Cached billing-period history prefix values."""

    # HTTP history는 과거 일별 보정용이다. 현재값은 MQTT mConsume을 사용한다.
    month_prefix_wh: int
    previous_period_wh: int
    history_rows: int


@dataclass(slots=True)
class TodayPrefix:
    """Cached current-month daily history before today."""

    before_today_wh: int
    history_rows: int


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
    )


async def _async_setup_sensors(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    scan_interval: timedelta,
) -> None:
    """Set up Refoss Cloud sensors."""

    client = RefossCloudClient(
        session=async_get_clientsession(hass),
        email=config[CONF_EMAIL],
        password=config[CONF_PASSWORD],
        uuid=config[CONF_UUID],
    )
    # dict.fromkeys로 순서를 유지하면서 중복 채널을 제거한다.
    channels = [int(channel) for channel in dict.fromkeys(config[CONF_CHANNELS])]
    reading_day = _normalize_reading_day(config[CONF_READING_DAY])

    # 하나의 coordinator가 모든 채널/모든 센서를 갱신한다.
    # MQTT ElectricityX는 갱신마다 1회만 호출하고, 채널별 센서가 그 결과를 공유한다.
    coordinator: DataUpdateCoordinator[dict[int, ChannelData]] = DataUpdateCoordinator(
        hass,
        logger=_LOGGER,
        name=f"{DOMAIN}_{config[CONF_UUID]}",
        update_interval=scan_interval,
        update_method=lambda: _async_update_data(client, channels, reading_day),
    )

    await coordinator.async_config_entry_first_refresh()

    entities: list[SensorEntity] = []
    for channel in channels:
        # 검침일 기준 월 사용량 센서.
        entities.append(
            RefossCloudEnergySensor(
                coordinator=coordinator,
                name=config[CONF_NAME],
                uuid=config[CONF_UUID],
                channel=channel,
                reading_day=reading_day,
            )
        )
        # 오늘 사용량 센서. 현재 월누적 mConsume에서 월초~어제까지의 HTTP daily history를
        # 빼서 계산하므로, 현재값은 MQTT 갱신 주기를 따라가고 HTTP는 캐시된다.
        entities.append(
            RefossCloudTodayEnergySensor(
                coordinator=coordinator,
                name=config[CONF_NAME],
                uuid=config[CONF_UUID],
                channel=channel,
            )
        )
        # 앱에 보이는 현재 전력/전압/역률/전류 센서.
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
    client: RefossCloudClient, channels: list[int], reading_day: int | str
) -> dict[int, ChannelData]:
    """Fetch one billing-period snapshot for all configured channels."""

    try:
        period = _billing_period(reading_day)
        # token은 날짜/시간 bucket이다. 같은 bucket에서는 HTTP history 캐시를
        # 재사용하고, 날짜가 바뀌거나 00:05 재시도 시점이 오면 다시 조회한다.
        history_refresh_token = _history_refresh_token()
        # 현재 mConsume과 전력/전압/전류/PF는 cloud MQTT에서 한 번에 받는다.
        current_electricity = await client.async_current_electricity()
        data: dict[int, ChannelData] = {}
        for channel in channels:
            data[channel] = await client.async_billing_period_energy(
                channel, period, current_electricity, history_refresh_token
            )
        return data
    except Exception as err:  # noqa: BLE001
        raise UpdateFailed(str(err)) from err


@dataclass(slots=True)
class BillingPeriod:
    """Timestamp boundaries for the current billing period."""

    local_start: int
    daily_start: int
    end: int
    month_start: int
    period_started_this_month: bool


def _billing_period(reading_day: int | str) -> BillingPeriod:
    """Return API timestamps for the current billing period.

    Refoss daily history rows are keyed by UTC midnight timestamps, even for
    dates shown as local days in the app. Use the local calendar to choose the
    billing date, then ask the daily API from that date's UTC midnight.
    """

    now = dt_util.now()
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)

    # "말일" 설정은 매월 실제 마지막 날로 치환된다.
    day_this_month = _reading_day_for_month(reading_day, now.year, now.month)
    local_start = datetime(
        now.year, now.month, day_this_month, tzinfo=now.tzinfo
    )
    if now < local_start:
        # 아직 이번 달 검침일 전이면 현재 검침월은 전월 검침일에서 시작한다.
        prev_month = now.month - 1 or 12
        prev_year = now.year if now.month > 1 else now.year - 1
        prev_day = _reading_day_for_month(reading_day, prev_year, prev_month)
        local_start = datetime(prev_year, prev_month, prev_day, tzinfo=now.tzinfo)
        daily_start = datetime(prev_year, prev_month, prev_day, tzinfo=UTC)
        period_started_this_month = False
    else:
        # 이번 달 검침일이 지났으면 현재 검침월은 이번 달 검침일에서 시작한다.
        daily_start = datetime(now.year, now.month, day_this_month, tzinfo=UTC)
        period_started_this_month = True

    return BillingPeriod(
        local_start=int(local_start.timestamp()),
        daily_start=int(daily_start.timestamp()),
        end=int(now.timestamp()),
        month_start=int(month_start.timestamp()),
        period_started_this_month=period_started_this_month,
    )


def _normalize_reading_day(value: Any) -> int | str:
    """Normalize configured reading day values."""

    # 새 config flow는 "last"를 저장한다. 과거에 28~31이 저장된 경우도
    # 사용자가 의도한 말일 검침으로 보고 호환 처리한다.
    if value == READING_DAY_LAST:
        return READING_DAY_LAST

    day = int(value)
    if day >= 28:
        return READING_DAY_LAST
    return day


def _scan_interval_from_config(config: dict[str, Any]) -> timedelta:
    """Return the configured MQTT polling interval."""

    # config flow는 초 단위 정수를 저장하고, YAML은 timedelta를 넘길 수 있다.
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


def _history_refresh_token() -> str:
    """Return the current daily history refresh bucket.

    MQTT values are refreshed every coordinator update. HTTP history is only
    needed for past daily correction, so refresh it once after local midnight
    and again after five minutes to catch delayed daily rows.
    """

    now = dt_util.now()
    midnight_retry = now.replace(hour=0, minute=5, second=0, microsecond=0)
    midnight_first = now.replace(hour=0, minute=0, second=5, microsecond=0)

    # 00:00:05 전에는 전날 bucket을 그대로 써서 자정 직후 불완전한
    # 일별 history를 너무 빨리 캐시하지 않는다.
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
        self._history_cache: dict[tuple[int, int, int, bool, str], HistoryPrefix] = {}
        self._today_cache: dict[tuple[int, int, int, str], TodayPrefix] = {}

    async def async_billing_period_energy(
        self,
        channel: int,
        period: BillingPeriod,
        current_electricity: dict[int, ChannelElectricity],
        history_refresh_token: str,
    ) -> ChannelData:
        """Fetch electric history for one billing-period channel."""

        snapshot = current_electricity[channel]
        current_mconsume_wh = snapshot.mconsume_wh
        # 검침월 보정값은 HTTP daily history에서 가져오지만, 매번 조회하지 않고
        # _async_history_prefix 내부 캐시를 우선 사용한다.
        history = await self._async_history_prefix(
            channel, period, history_refresh_token
        )
        today = await self._async_today_prefix(channel, history_refresh_token)
        mconsume_today_wh = current_mconsume_wh - today.before_today_wh

        # Refoss mConsume은 해당 월 1일부터 현재까지의 누적값이다.
        # 검침일이 이미 지났으면: 월초~검침 전날(prefix)을 빼서 검침일 이후만 남긴다.
        # 검침일 전이면: 지난달 검침일~말일(previous_period)을 더해 현재 검침월을 완성한다.
        return ChannelData(
            net_wh=(
                current_mconsume_wh
                - history.month_prefix_wh
                + history.previous_period_wh
            ),
            today_wh=mconsume_today_wh,
            current_mconsume_wh=current_mconsume_wh,
            month_prefix_wh=history.month_prefix_wh,
            previous_period_wh=history.previous_period_wh,
            history_rows=history.history_rows,
            today_prefix_wh=today.before_today_wh,
            today_history_rows=today.history_rows,
            current_ma=snapshot.current_ma,
            voltage_mv=snapshot.voltage_mv,
            power_mw=snapshot.power_mw,
            factor=snapshot.factor,
        )

    async def _async_history_prefix(
        self,
        channel: int,
        period: BillingPeriod,
        history_refresh_token: str,
    ) -> HistoryPrefix:
        """Fetch or return cached HTTP history correction values."""

        cache_key = (
            channel,
            period.daily_start,
            period.month_start,
            period.period_started_this_month,
            history_refresh_token,
        )
        if cache_key in self._history_cache:
            return self._history_cache[cache_key]

        # 같은 채널의 오래된 캐시는 새 bucket이 생겼을 때 정리한다.
        # 채널 수가 작아도 장기 실행 중 cache key가 계속 쌓이지 않게 하기 위함이다.
        stale_keys = [key for key in self._history_cache if key[0] == channel]
        for key in stale_keys:
            self._history_cache.pop(key, None)

        if period.period_started_this_month:
            # 검침일이 이번 달에 이미 지났다면, 현재 mConsume에서 월초~검침 전날
            # 사용량을 빼야 한다.
            rows = await self._async_electric_history(
                channel=channel,
                start_time=period.month_start,
                end_time=period.daily_start - 1,
                step="1d",
            )
            month_prefix_wh = sum(_row_net_wh(row) for row in rows)
            previous_period_wh = 0
        else:
            # 아직 이번 달 검침일 전이면, 지난달 검침일~지난달 말일 사용량을
            # 이번 달 mConsume에 더해야 한다.
            rows = await self._async_electric_history(
                channel=channel,
                start_time=period.daily_start,
                end_time=period.month_start - 1,
                step="1d",
            )
            month_prefix_wh = 0
            previous_period_wh = sum(_row_net_wh(row) for row in rows)

        history = HistoryPrefix(
            month_prefix_wh=month_prefix_wh,
            previous_period_wh=previous_period_wh,
            history_rows=len(rows),
        )
        self._history_cache[cache_key] = history
        return history

    async def _async_today_prefix(
        self, channel: int, history_refresh_token: str
    ) -> TodayPrefix:
        """Fetch or return cached current-month usage before today."""

        now = dt_util.now()
        month_start = int(datetime(now.year, now.month, 1, tzinfo=UTC).timestamp())
        today_start = int(
            datetime(now.year, now.month, now.day, tzinfo=UTC).timestamp()
        )
        cache_key = (
            channel,
            month_start,
            today_start,
            history_refresh_token,
        )
        if cache_key in self._today_cache:
            return self._today_cache[cache_key]

        stale_keys = [key for key in self._today_cache if key[0] == channel]
        for key in stale_keys:
            self._today_cache.pop(key, None)

        if today_start <= month_start:
            today = TodayPrefix(
                before_today_wh=0,
                history_rows=0,
            )
        else:
            rows = await self._async_electric_history(
                channel=channel,
                start_time=month_start,
                end_time=today_start - 1,
                step="1d",
            )
            rows = _history_rows_before_cutoff(
                rows,
                cutoff=today_start,
                max_rows=(today_start - month_start) // 86400,
            )
            today = TodayPrefix(
                before_today_wh=sum(_row_net_wh(row) for row in rows),
                history_rows=len(rows),
            )

        self._today_cache[cache_key] = today
        return today

    async def async_current_electricity(self) -> dict[int, ChannelElectricity]:
        """Fetch current ElectricityX values through Refoss cloud MQTT."""

        if self._token is None:
            await self.async_login()

        # socket 기반 MQTT 처리는 blocking I/O라 Home Assistant event loop를
        # 막지 않도록 worker thread에서 실행한다.
        last_err: OSError | TimeoutError | None = None
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                return await asyncio.to_thread(self._mqtt_current_electricity)
            except (OSError, TimeoutError) as err:
                last_err = err
                if attempt < max_attempts - 1:
                    # DNS/외부망 연결 또는 MQTT 응답 읽기가 몇 초 흔들리는 경우가 있어
                    # 2초 간격으로 두 번까지 조용히 재시도한다.
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

        # HTTP history는 현재값이 아니라 과거 일별 합계 보정용으로만 쓴다.
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

        # Refoss 앱과 같은 방식으로 비밀번호 MD5를 보낸다. 이후 응답의 token은
        # HTTP API에, userid/key/mqttDomain은 cloud MQTT 인증에 사용한다.
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

        # 상시 subscribe 방식이 아니라, 앱처럼 GET 요청을 MQTT로 publish하고
        # 해당 messageId의 GETACK 응답을 기다리는 요청/응답 방식이다.
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

            # 응답은 /app/<userid>-<appid>/subscribe 토픽으로 돌아온다.
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
            # ElectricityX는 mConsume, power, voltage, current, factor를
            # 채널별로 한 번에 돌려준다.
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

                # Refoss 응답 정수 단위는 센서 클래스에서 사람이 읽는 단위로 변환한다.
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
                    # HTTP API도 자정 직후나 외부망 상태에 따라 잠깐 실패할 수 있으므로
                    # MQTT와 같은 정책으로 2초 간격 두 번까지 조용히 재시도한다.
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

    # daily history의 value는 이미 순사용량이다. 태양광 채널처럼 생산이 더 크면
    # 음수일 수 있으므로 그대로 사용한다.
    value = row.get("value")
    if value is not None:
        return int(value)

    valcons = int(row.get("valcons") or 0)
    valprod = int(row.get("valprod") or 0)
    return valcons + valprod


def _history_rows_before_cutoff(
    rows: list[dict[str, Any]], cutoff: int, max_rows: int
) -> list[dict[str, Any]]:
    """Return daily history rows before a timestamp cutoff."""

    timestamped_rows = [
        (timestamp, row)
        for row in rows
        if (timestamp := _row_timestamp(row)) is not None
    ]
    if timestamped_rows:
        return [
            row
            for timestamp, row in sorted(timestamped_rows, key=lambda item: item[0])
            if timestamp < cutoff
        ]

    # 일부 응답은 timestamp 없이 일별 row만 순서대로 줄 수 있다. 이때는
    # 월초부터 어제까지 필요한 개수만 남겨 오늘 row가 prefix에 섞이지 않게 한다.
    return rows[:max(0, max_rows)]


def _row_timestamp(row: dict[str, Any]) -> int | None:
    """Return a normalized seconds timestamp from one history row."""

    for key in ("time", "timestamp", "timeStamp", "ts", "date"):
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            timestamp = int(value)
            return timestamp // 1000 if timestamp > 10_000_000_000 else timestamp
        if isinstance(value, str):
            try:
                timestamp = int(value)
            except ValueError:
                try:
                    return int(datetime.fromisoformat(value).timestamp())
                except ValueError:
                    continue
            return timestamp // 1000 if timestamp > 10_000_000_000 else timestamp

    return None


def _random_string(length: int) -> str:
    """Return a random uppercase string for Refoss message ids."""

    return "".join(
        random.SystemRandom().choice(string.ascii_uppercase + string.digits)
        for _ in range(length)
    )


def _mqtt_string(value: str) -> bytes:
    """Encode an MQTT UTF-8 string."""

    # MQTT 문자열은 2바이트 길이 prefix + UTF-8 bytes 형식이다.
    encoded = value.encode()
    return struct.pack("!H", len(encoded)) + encoded


def _mqtt_remaining_length(length: int) -> bytes:
    """Encode an MQTT remaining length field."""

    # MQTT remaining length는 7비트씩 나눠서 가변 길이로 인코딩한다.
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

    # paho-mqtt 같은 외부 의존성을 추가하지 않기 위해 필요한 최소 MQTT v3.1.1
    # packet framing만 직접 구현했다.
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
    _attr_state_class = SensorStateClass.MEASUREMENT

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

        # 내부 계산은 Wh, HA 표시는 kWh.
        return round(data.net_wh / 1000, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""

        period = _billing_period(self._reading_day)
        data = (self.coordinator.data or {}).get(self._channel)
        attrs: dict[str, Any] = {
            "channel": self._channel,
            "channel_label": _channel_label(self._channel),
            "kind": "net",
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
                "source": "cloud_mqtt_mconsume",
                # 디버깅용 속성: MQTT 현재 월누적과 HTTP 보정값이 어떻게 합쳐졌는지
                # UI에서 바로 확인할 수 있게 둔다.
                "current_mconsume_kwh": round(data.current_mconsume_wh / 1000, 3),
                "month_prefix_kwh": round(data.month_prefix_wh / 1000, 3),
                "previous_period_kwh": round(data.previous_period_wh / 1000, 3),
                "history_rows": data.history_rows,
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
    _attr_state_class = SensorStateClass.MEASUREMENT

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

        return round(data.today_wh / 1000, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""

        now = dt_util.now()
        data = (self.coordinator.data or {}).get(self._channel)
        attrs: dict[str, Any] = {
            "channel": self._channel,
            "channel_label": _channel_label(self._channel),
            "kind": "net",
            "date": now.date().isoformat(),
            "source": "cloud_mqtt_mconsume_minus_http_daily_history",
        }
        if data is None:
            return attrs

        attrs.update(
            {
                "current_mconsume_kwh": round(data.current_mconsume_wh / 1000, 3),
                "today_prefix_kwh": round(data.today_prefix_wh / 1000, 3),
                "today_history_rows": data.today_history_rows,
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

        # ElectricityX 원본 단위를 HA 표시 단위로 변환한다.
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
