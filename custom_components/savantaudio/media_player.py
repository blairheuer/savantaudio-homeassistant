"""Support for Savant Audio Switches (SSA-3220)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from http.client import SWITCHING_PROTOCOLS
import logging

# from homeassistant.components.media_player.const import DOMAIN
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify
import savantaudio.client as sa
from typing_extensions import Required
import voluptuous as vol

from .const import (
    CONF_NUMBER,
    CONF_SOURCES,
    CONF_ZONES,
    DEFAULT_SOURCE,
    DOMAIN,
    KNOWN_ZONES,
)
from .coordinator import SavantAudioCoordinator

_LOGGER = logging.getLogger(__name__)


ATTR_PASSTHRU = "passthru"
ATTR_STEREO = "stereo"
ATTR_DELAY_LEFT = "delay_left"
ATTR_DELAY_RIGHT = "delay_right"
ATTR_VOLUME_DB = "volume_db"


SUPPORT_SAVANTAUDIO = (
    MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.SELECT_SOUND_MODE
    | MediaPlayerEntityFeature.GROUPING
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.VOLUME_STEP
)

KNOWN_HOSTS: list[str] = []

SOUND_MODE_LIST = ['stereo', 'mono']
PASSTHRU_SOUND_MODE_LIST = ['stereo,passthru', 'mono,passthru']

# Passthru outputs a fixed 0dB that ignores this integration's volume control,
# at least 15dB above the slider's window, so one pick on an amp zone is
# dangerously loud. Only the outputs feeding an AVR, which has its own volume, may use it:
# 13 Family Room and 14 Theater.
PASSTHRU_OUTPUTS = frozenset({13, 14})

DEFAULT_SOURCES = { n: {"name": f'Source {n}'} for n in range(1,32) }
DEFAULT_ZONES = { n: {"name": f'Zone {n}', DEFAULT_SOURCE: None} for n in range(1,20) }

SOURCE_IDS = vol.All(vol.Coerce(int), vol.Range(min=1, max=32))
SOURCE_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME, default="Unknown Source"): cv.string,
    vol.Required(CONF_ENABLED, default=True): bool,
})

ZONE_IDS = vol.All( vol.Coerce(int), vol.Range(min=1, max=20) )
ZONE_SCHEMA = vol.Schema({
    vol.Required(CONF_NUMBER): ZONE_IDS,
    vol.Required(CONF_NAME, default="Audio Zone"): cv.string,
    vol.Optional(DEFAULT_SOURCE): cv.positive_int,
    vol.Required(CONF_ENABLED, default=True): bool,
})

# dB window the 0..1 volume slider maps onto, out of the switch's -117 to +10dB.
# Line level into these amps is painfully loud well before 0dB. It needs to reach
# well down, too: on 2026-11-04 -37dB was very loud in the Office, where the
# Savant host had set -57dB.
VOLUME_MIN_DB = -75.0
VOLUME_MAX_DB = -15.0


TIMEOUT_MESSAGE = "Timeout waiting for response."

async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Setup sensors from a config entry created in the integrations UI."""
    _LOGGER.info(f'media_player.async_setup_entry: {DOMAIN}')
    config = hass.data[DOMAIN][config_entry.entry_id]
    known_zones = hass.data[DOMAIN].setdefault(KNOWN_ZONES, [])

    devices: list[SavantAudioZone] = []

    coordinator: SavantAudioCoordinator = config_entry.runtime_data
    switch = coordinator.switch

    # add device for switch
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, switch.attributes['sn'])},
        manufacturer="Savant",
        name=config[CONF_NAME],
        model=str(switch.model),
        sw_version=switch.attributes['fwrev'],
        hw_version=switch.attributes['rev'],
    )

    sn = switch.attributes['sn']
    device_ids = []
    entity_ids = []
    if CONF_SOURCES in config and CONF_ZONES in config:
        sources = {
            int(source_id): extra[CONF_NAME] for source_id, extra in config[CONF_SOURCES].items() if extra.get(CONF_ENABLED, True)
        }
        for entity_id, extra in config[CONF_ZONES].items():
            if extra.get(CONF_ENABLED, True):
                zonedevice = SavantAudioZone(
                        coordinator,
                        entity_id,
                        sources,
                        switch.output(int(extra[CONF_NUMBER])),
                        extra[CONF_NAME],
                        switch_name=config.get(CONF_NAME),
                        default_source=extra.get(DEFAULT_SOURCE, None),
                    )
                known_zones.append(zonedevice)
                devices.append(zonedevice)
                device_ids.append(zonedevice.unique_id)
                entity_ids.append(zonedevice.entity_id)
    if sn not in KNOWN_HOSTS:
        KNOWN_HOSTS.append(sn)
    unknown_entities = [zone for zone in known_zones if zone.switch.attributes['sn'] == sn and zone.entity_id not in entity_ids]
    for zone in unknown_entities:
        entity_registry.async_remove(zone.entity_id)
        _LOGGER.debug(f'Removed Zone Entity id={zone.entity_id}, name={zone.name}')
    unknown_zones = [zone for zone in known_zones if zone.switch.attributes['sn'] == sn and zone.unique_id not in device_ids]
    for zone in unknown_zones:
        known_zones.remove(zone)
        device = device_registry.async_get_device_by_identifier(
            (DOMAIN, zone.unique_id), config_entry.entry_id
        )
        if device is not None:
            device_registry.async_remove_device(device.id)
            _LOGGER.debug(f'Removed Zone Device id={device.id}, name={zone.name}')

    async_add_entities(devices)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info(f'media_player.async_unload_entry: {DOMAIN}')
    known_zones = hass.data[DOMAIN].setdefault(KNOWN_ZONES, [])
    to_remove = [ zone for zone in known_zones if zone.switch.attributes['sn'] == entry.unique_id ]
    for zone in to_remove:
        known_zones.remove(zone)

    return True

class SavantAudioZone(CoordinatorEntity[SavantAudioCoordinator], MediaPlayerEntity):
    """Representation of an SAVANTAUDIO device."""

    _attr_supported_features = SUPPORT_SAVANTAUDIO

    def __init__(
        self,
        coordinator: SavantAudioCoordinator,
        entity_id,
        sources,
        output,
        zone_name: str = None,
        switch_name: str = None,
        default_source: int = None
    ):
        """Initialize the SAVANTAUDIO Receiver."""
        super().__init__(coordinator)
        switch = coordinator.switch
        self._switch = switch
        self._output = output
        self.entity_id = f'media_player.{entity_id}'
        self._switch_name = switch_name if switch_name is not None else f'{switch.model}'
        self._default_source = default_source

        self._attr_unique_id = (
            f"{switch.attributes['sn']}_{output.number}"
        )

        if zone_name:
            self._attr_name = zone_name
        else:
            self._attr_name = f'{self._switch_name} Zone {output.number}'

        if sources is None:
            sources = DEFAULT_SOURCES
        self.set_sources(sources)
        self._current_source = None
        self._attributes = {}
        self._volume = 0
        self._mute = False
        self._pwstate = STATE_OFF
        self._sync_state()

    def set_sources(self, sources):
        self._source_list = list(sources.values())
        self._source_mapping = sources
        self._reverse_mapping = {value: key for key, value in sources.items()}

    def set_name(self, name: str):
        self._attr_name = name

    def _sync_state(self) -> None:
        """Copy what the client last read from the switch."""
        self._current_source = self._switch.links.get(self._output.number)
        self._pwstate = STATE_ON if self._current_source is not None else STATE_OFF

        volume_raw = self._output.volume
        self._mute = self._output.mute

        # Map the switch's dB level onto 0..1 across the configured window.
        # Clamped because the level can be set outside that window by the
        # Savant host or the front panel, and Home Assistant rejects a
        # volume_level outside 0..1.
        span = VOLUME_MAX_DB - VOLUME_MIN_DB
        self._volume = max(0.0, min(1.0, (volume_raw - VOLUME_MIN_DB) / span))

        # The real level, which the clamped volume_level can't show, and which
        # stays readable while the zone is off.
        self._attributes[ATTR_VOLUME_DB] = volume_raw
        self._attributes[ATTR_PASSTHRU] = self._output.passthru
        self._attributes[ATTR_STEREO] = self._output.stereo
        self._attributes[ATTR_DELAY_LEFT] = self._output.delay[0]
        self._attributes[ATTR_DELAY_RIGHT] = self._output.delay[1]

    @callback
    def _handle_coordinator_update(self) -> None:
        """Publish the state the coordinator just read."""
        self._sync_state()
        super()._handle_coordinator_update()

    @asynccontextmanager
    async def _zone_session(self):
        """Run commands on one short-lived socket, then publish the zone's real state."""
        try:
            async with self._switch.session():
                yield
                await self._output.refresh()
                await self._switch.refresh_link(self._output.number)
        except sa.SavantError as err:
            raise HomeAssistantError(f"{self.name}: {err}") from err
        self._sync_state()
        self.async_write_ha_state()
        self.coordinator.async_routes_changed()

    @property
    def device_info(self):
        info = {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self.unique_id)
            },
            "name": self.name,
            "manufacturer": "Savant",
            "model": str(self._switch.model),
            "sw_version": self._switch.attributes['fwrev'],
            "hw_version": self._switch.attributes['rev'],
        }
        # HA 2026.9 dropped the `via_device` identifier tuple in favour of
        # `via_device_id`, which wants the parent's device registry id. Look the
        # switch up rather than hard-failing: device_info is also read outside the
        # entity lifecycle (zone cleanup in async_setup_entry), where hass is unset.
        if (hass := getattr(self, "hass", None)) is not None:
            parent = dr.async_get(hass).async_get_device_by_identifier(
                (DOMAIN, self._switch.attributes['sn']),
                self.coordinator.config_entry.entry_id,
            )
            if parent is not None:
                info["via_device_id"] = parent.id
        return info

    @property
    def switch(self):
        return self._switch

    @property
    def number(self):
        return self._output.number

    @property
    def state(self):
        """Return the state of the device."""
        return self._pwstate

    @property
    def volume_level(self):
        """Return the volume level of the media player (0..1)."""
        return self._volume

    @property
    def is_volume_muted(self):
        """Return boolean indicating mute status."""
        return self._mute

    @property
    def source(self):
        """Return the current source source of the device."""
        if self._current_source is not None:
            # A zone routed to an input that isn't an enabled source has no name.
            return self._source_mapping.get(self._current_source)
        else:
            return None

    @property
    def source_list(self):
        """List of available source sources."""
        return self._source_list

    @property
    def extra_state_attributes(self):
        """Return device specific state attributes."""
        return self._attributes

    @property
    def sound_mode(self):
        modes = []
        if self._output.stereo:
            modes.append('stereo')
        else:
            modes.append('mono')
        if self._output.passthru: modes.append('passthru')
        return ','.join(modes)

    @property
    def sound_mode_list(self):
        if self._output.number in PASSTHRU_OUTPUTS:
            return SOUND_MODE_LIST + PASSTHRU_SOUND_MODE_LIST
        return SOUND_MODE_LIST

    @property
    def device_class(self):
        return MediaPlayerDeviceClass.RECEIVER

    async def async_turn_off(self):
        """Turn the media player off."""
        async with self._zone_session():
            await self._switch.unlink(self._output.number)

    async def async_set_volume_level(self, volume):
        """
        Set volume level, source is range 0..1.

        Mapped onto VOLUME_MIN_DB..VOLUME_MAX_DB rather than the switch's full
        range, so the whole slider covers usable listening levels.
        """
        span = VOLUME_MAX_DB - VOLUME_MIN_DB
        async with self._zone_session():
            await self._output.set_volume(round(VOLUME_MIN_DB + volume * span))

    async def async_volume_up(self):
        """Increase volume by 1 step, staying inside the slider's window."""
        await self._async_step_volume(1)

    async def async_volume_down(self):
        """Decrease volume by 1 step, staying inside the slider's window."""
        await self._async_step_volume(-1)

    async def _async_step_volume(self, step: int) -> None:
        current = self._output.volume
        target = int(max(VOLUME_MIN_DB, min(VOLUME_MAX_DB, current + step)))
        # A zone the Savant host left outside the window comes back into it when
        # stepped towards it, and otherwise stays put.
        if (target - current) * step > 0:
            async with self._zone_session():
                await self._output.set_volume(target)

    async def async_mute_volume(self, mute):
        """Mute (true) or unmute (false) media player."""
        async with self._zone_session():
            await self._output.set_mute(mute)

    async def async_turn_on(self):
        """Turn the media player on."""
        if self._pwstate == STATE_OFF:
            source = self._current_source
            if source is None:
                source = self._default_source
            if source is None and self._source_mapping:
                # No default configured and nothing previously selected. Any
                # enabled source is a better outcome than silently doing
                # nothing, which leaves the entity reporting on while the
                # output stays unlinked.
                source = sorted(self._source_mapping)[0]
            if source is None:
                _LOGGER.warning(
                    "Cannot turn on %s: no sources are enabled", self.name
                )
                return
            async with self._zone_session():
                await self._switch.link(self._output.number, source)

    async def async_select_source(self, source):
        """Set the source source."""
        async with self._zone_session():
            if source is not None:
                if source in self._source_list:
                    source = self._reverse_mapping[source]
                # Link unconditionally: selecting a source means "play this", and
                # gating on power state made the call a no-op on any zone that was
                # not already routed.
                await self._switch.link(self._output.number, source)
            else:
                await self._switch.unlink(self._output.number)

    async def async_select_sound_mode(self, sound_mode: str):
        """Set the sound mode."""
        if sound_mode not in self.sound_mode_list:
            raise ServiceValidationError(
                f"{self.name} does not support sound mode '{sound_mode}'"
            )
        modes = sound_mode.split(',')
        async with self._zone_session():
            # Passthru first: leaving it is what restores volume control.
            await self._output.set_passthru('passthru' in modes)
            await self._output.set_mono('mono' in modes)

    async def async_join_players(self, group_members: list[str]) -> None:
        """Join `group_members` as a player group with the current player."""
        if self._current_source is None:
            _LOGGER.info("%s has no source to share. Not syncing", self.name)
            return

        zone_ids = {
            p.entity_id: p for p in self.hass.data[DOMAIN][KNOWN_ZONES]
        }

        for other_player in group_members:
            other = zone_ids.get(other_player)
            if other is not None and other._switch.host == self._switch.host:
                await other.async_select_source(self._current_source)
            else:
                _LOGGER.info(
                    "Could not find player_id for %s. Not syncing", other_player
                )

    async def async_unjoin_player(self) -> None:
        """Remove this player from any group."""
        async with self._zone_session():
            await self._switch.unlink(self._output.number)

    @property
    def icon(self):
        if self.state == STATE_OFF or self.is_volume_muted:
            return 'mdi:speaker-off'
        else:
            return 'mdi:speaker'
