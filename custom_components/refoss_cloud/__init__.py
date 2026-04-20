"""Refoss Cloud custom integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

DOMAIN = "refoss_cloud"
PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Refoss Cloud from a config entry."""

    # 센서 플랫폼만 사용한다. config entry의 실제 계정/기기 설정은 sensor.py가 읽는다.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # 옵션에서 polling 주기를 바꾸면 entry를 reload해서 coordinator 주기를 갱신한다.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Refoss Cloud config entry."""

    # HA가 통합을 제거/비활성화할 때 sensor 플랫폼도 함께 unload한다.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload Refoss Cloud when options change."""

    await hass.config_entries.async_reload(entry.entry_id)
