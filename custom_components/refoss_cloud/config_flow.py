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

CHANNEL_OPTIONS = [
    {"value": str(channel), "label": label}
    for channel, label in CHANNEL_LABELS.items()
]

READING_DAY_OPTIONS = [
    {"value": str(day), "label": f"{day} day"} for day in range(1, 28)
] + [{"value": READING_DAY_LAST, "label": "Last day"}]
DEFAULT_SCAN_INTERVAL_SECONDS = int(DEFAULT_SCAN_INTERVAL.total_seconds())


class RefossCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Refoss Cloud config flow."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow."""

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

    return max(10, int(value))


def _parse_channels(value: str | list[str]) -> list[int]:
    """Parse selected channels."""

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
