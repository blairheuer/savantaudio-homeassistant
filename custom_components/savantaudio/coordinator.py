"""Polling for Savant Audio Switches (SSA-3220)."""
from __future__ import annotations

import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import savantaudio.client as sa

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

IDLE_INTERVAL = datetime.timedelta(minutes=1)
ROUTED_INTERVAL = datetime.timedelta(seconds=15)
# At the routed interval, this many lighter reads between full ones keeps every
# zone at most a minute old.
LIGHT_READS_PER_FULL_READ = 3


class SavantAudioCoordinator(DataUpdateCoordinator[None]):
    """Read the zones in one short session, leaving the port free in between.

    A second controller on this port stops the Savant host's own routing from
    taking effect, so one socket per cycle replaces a poll per zone. The state
    itself stays on the client's Switch and Output objects.

    Nothing tells us when the Savant host changes a level or route, so changes
    only show up on a read. Reads are quicker while any zone is routed to an
    input, and lighter too: every route, but levels only for routed zones.
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
            update_interval=IDLE_INTERVAL,
        )
        self.switch = switch
        self._outputs = outputs
        self._light_reads = 0

    async def _async_setup(self) -> None:
        """Read the switch's identity, which the device registry needs."""
        try:
            async with self.switch.session():
                await self.switch.refresh_info()
        except sa.SavantError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_update_data(self) -> None:
        """Read every zone's route, and the levels of those that need it."""
        full = (
            self.update_interval != ROUTED_INTERVAL
            or self._light_reads >= LIGHT_READS_PER_FULL_READ
        )
        try:
            async with self.switch.session():
                for number in self._outputs:
                    await self.switch.refresh_link(number)
                for number in self._outputs:
                    if full or number in self.switch.links:
                        await self.switch.output(number).refresh()
        except sa.SavantError as err:
            # Don't retry a switch that's down any faster than usual.
            self.update_interval = IDLE_INTERVAL
            raise UpdateFailed(str(err)) from err
        self._light_reads = 0 if full else self._light_reads + 1
        self.update_interval = self._interval_for_routes()

    def _interval_for_routes(self) -> datetime.timedelta:
        """Read quicker while a zone is routed, once startup is over.

        A busy startup already stretches a read to many seconds.
        """
        if self.hass.state is CoreState.running and any(
            number in self.switch.links for number in self._outputs
        ):
            return ROUTED_INTERVAL
        return IDLE_INTERVAL

    @callback
    def async_routes_changed(self) -> None:
        """Change speed at once after a command, rather than on the next read."""
        interval = self._interval_for_routes()
        if interval != self.update_interval:
            self.update_interval = interval
            self._schedule_refresh()
