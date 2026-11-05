"""Polling for Savant Audio Switches (SSA-3220)."""
from __future__ import annotations

import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import savantaudio.client as sa

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = datetime.timedelta(minutes=1)


class SavantAudioCoordinator(DataUpdateCoordinator[None]):
    """Read every zone in one short session, leaving the port free in between.

    A second controller on this port stops the Savant host's own routing from
    taking effect, so one socket per cycle replaces a poll per zone. The state
    itself stays on the client's Switch and Output objects.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        switch: sa.Switch,
        outputs: list[int],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.switch = switch
        self._outputs = outputs

    async def _async_setup(self) -> None:
        """Read the switch's identity, which the device registry needs."""
        try:
            async with self.switch.session():
                await self.switch.refresh_info()
        except sa.SavantError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_update_data(self) -> None:
        """Read the state of every enabled zone."""
        try:
            async with self.switch.session():
                for number in self._outputs:
                    await self.switch.output(number).refresh()
                    await self.switch.refresh_link(number)
        except sa.SavantError as err:
            raise UpdateFailed(str(err)) from err
