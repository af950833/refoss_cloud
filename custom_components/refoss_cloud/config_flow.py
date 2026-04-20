"""Config flow for Refoss Cloud."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import (
    CONF_EMAIL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from . import DOMAIN
from .sensor import (
    CHANNEL_LABELS,
    CONF_CHANNELS,
    CONF_READING_DAY,
    CONF_UUID,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    READING_DAY_LAST,
    RefossCloudClient,
)

# EM06 내부 채널 번호는 1~6이지만, 사용자에게는 실제 배선 표기인
# A1/B1/C1/A2/B2/C2로 보여준다. 저장값은 계속 번호를 써서 unique_id가
# 바뀌지 않게 유지한다.
CHANNEL_OPTIONS = [
    {"value": str(channel), "label": label}
    for channel, label in CHANNEL_LABELS.items()
]

# 검침일은 슬라이더 대신 드롭다운으로 받는다. 1~27일은 고정 날짜이고,
# "last"는 sensor.py에서 각 월의 실제 말일로 변환된다.
READING_DAY_OPTIONS = [
    {"value": str(day), "label": f"{day}일"} for day in range(1, 28)
] + [{"value": READING_DAY_LAST, "label": "말일"}]
DEFAULT_SCAN_INTERVAL_SECONDS = int(DEFAULT_SCAN_INTERVAL.total_seconds())


class RefossCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Refoss Cloud config flow."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow."""

        # 이미 추가한 통합도 옵션 화면에서 MQTT polling 주기를 조정할 수 있다.
        return RefossCloudOptionsFlow(config_entry)

    def __init__(self) -> None:
        self._email: str | None = None
        self._password: str | None = None
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial account step."""

        errors: dict[str, str] = {}

        if user_input is not None:
            # 첫 단계에서는 계정 로그인 가능 여부만 확인한다. 성공하면 같은
            # 토큰으로 기기 목록을 받아 다음 단계의 선택지로 사용한다.
            client = RefossCloudClient(
                session=async_get_clientsession(self.hass),
                email=user_input[CONF_EMAIL],
                password=user_input[CONF_PASSWORD],
                uuid="config-flow",
            )
            try:
                await client.async_login()
                self._devices = await client.async_devices()
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                self._email = user_input[CONF_EMAIL]
                self._password = user_input[CONF_PASSWORD]
                return await self.async_step_device()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle device selection."""

        errors: dict[str, str] = {}
        device_options = {
            device["uuid"]: f"{device.get('devName') or device['uuid']} ({device.get('deviceType', 'device')})"
            for device in self._devices
            if device.get("uuid")
        }

        if not device_options:
            return self.async_abort(reason="no_devices")

        if user_input is not None:
            channels = _parse_channels(user_input[CONF_CHANNELS])
            if not channels:
                errors[CONF_CHANNELS] = "invalid_channels"
            else:
                uuid = user_input[CONF_UUID]
                await self.async_set_unique_id(uuid)
                self._abort_if_unique_id_configured()

                # 표시 이름은 사용자가 정할 수 있지만, 계정/기기/채널/검침일 같은
                # 실제 동작 설정은 entry.data에 저장한다.
                title = user_input.get(CONF_NAME) or device_options[uuid].split(" (", 1)[0]
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_EMAIL: self._email,
                        CONF_PASSWORD: self._password,
                        CONF_UUID: uuid,
                        CONF_NAME: title,
                        CONF_READING_DAY: _parse_reading_day(
                            user_input[CONF_READING_DAY]
                        ),
                        CONF_CHANNELS: channels,
                        CONF_SCAN_INTERVAL: _parse_scan_interval(
                            user_input[CONF_SCAN_INTERVAL]
                        ),
                    },
                )

        first_uuid = next(iter(device_options))
        first_name = device_options[first_uuid].split(" (", 1)[0]
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_UUID, default=first_uuid): vol.In(device_options),
                    vol.Optional(CONF_NAME, default=first_name or DEFAULT_NAME): str,
                    vol.Required(
                        CONF_READING_DAY, default="24"
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=READING_DAY_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_CHANNELS,
                        default=[str(channel) for channel in CHANNEL_LABELS],
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=CHANNEL_OPTIONS,
                            multiple=True,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=DEFAULT_SCAN_INTERVAL_SECONDS,
                    ): vol.All(vol.Coerce(int), vol.Range(min=10)),
                }
            ),
            errors=errors,
        )


class RefossCloudOptionsFlow(config_entries.OptionsFlow):
    """Handle Refoss Cloud options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage Refoss Cloud options."""

        if user_input is not None:
            # 옵션 변경 후에는 __init__.py의 update listener가 entry를 reload해서
            # 새 polling 주기를 즉시 반영한다.
            return self.async_create_entry(
                title="",
                data={
                    CONF_SCAN_INTERVAL: _parse_scan_interval(
                        user_input[CONF_SCAN_INTERVAL]
                    )
                },
            )

        current = self._config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self._config_entry.data.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS
            ),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=_parse_scan_interval(current),
                    ): vol.All(vol.Coerce(int), vol.Range(min=10)),
                }
            ),
        )


def _parse_reading_day(value: str | int) -> int | str:
    """Parse the billing reading day from the selector."""

    if value == READING_DAY_LAST:
        return READING_DAY_LAST
    return int(value)


def _parse_scan_interval(value: str | int) -> int:
    """Parse the MQTT polling interval in seconds."""

    # 너무 짧은 주기로 cloud MQTT를 반복 호출하지 않도록 최소 10초를 보장한다.
    return max(10, int(value))


def _parse_channels(value: str | list[str]) -> list[int]:
    """Parse selected channels."""

    # 새 UI는 ["1", "2"] 형태를 넘기지만, 기존 텍스트 입력("1,2,3")이나
    # 사용자가 직접 A1/B1 같은 라벨을 넣은 경우도 계속 받아준다.
    channels: list[int] = []
    parts = value if isinstance(value, list) else value.split(",")
    labels = {label.lower(): channel for channel, label in CHANNEL_LABELS.items()}
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.lower() in labels:
            channel = labels[part.lower()]
        else:
            try:
                channel = int(part)
            except ValueError:
                return []
        if channel not in CHANNEL_LABELS:
            return []
        channels.append(channel)
    return list(dict.fromkeys(channels))
