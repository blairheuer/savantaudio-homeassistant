"""End-to-end tests over a real socket, against a fake that answers like the SSA-3220D."""
import asyncio
from datetime import timedelta
import re
from unittest.mock import patch

from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ENABLED, CONF_HOST, CONF_NAME, CONF_PORT, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from savantaudio import client as sa

from custom_components.savantaudio.const import (
    CONF_NUMBER,
    CONF_SOURCES,
    CONF_ZONES,
    DEFAULT_SOURCE,
    DOMAIN,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

KITCHEN = "media_player.savant_kitchen"
DINING = "media_player.savant_dining"
FAMILY = "media_player.savant_family"
ZONE_NUMBERS = {KITCHEN: 6, DINING: 5, FAMILY: 13}


class FakeSwitch:
    """Answers like the real switch, per a read-only probe on 2026-11-04."""

    def __init__(self):
        self.commands = []
        self.connections = 0
        self.open_now = 0
        self.silent = False
        self.volume = {6: -28, 5: -38, 13: -38}
        self.mute = {}
        self.links = {6: 1}
        self.conf = {}
        self.mono = {}
        self.refuse = {}
        self._writers = set()
        self._server = None
        self.port = None

    async def start(self, port=0):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._server is None:
            return
        self._server.close()
        for writer in list(self._writers):
            writer.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(self, reader, writer):
        self.connections += 1
        self.open_now += 1
        self._writers.add(writer)
        try:
            while line := await reader.readline():
                command = line.decode().strip()
                self.commands.append(command)
                if self.silent:
                    continue
                for reply in self._reply(command):
                    writer.write(reply.encode() + b"\r\n")
                writer.write(b"\r\n")
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            self.open_now -= 1
            self._writers.discard(writer)
            writer.close()

    def _reply(self, command):
        for pattern, code in self.refuse.items():
            if re.fullmatch(pattern, command):
                return [code]
        if command == "fwrev":
            return ["fwrevPrimary; 8.1:601 T"]
        if command == "fpga-rev":
            return ["fpga-rev80.7"]
        if command == "status":
            return ["statusAPI1.0; Standalone-Audio-Switch-With-Delay; rev0; pn168-0008-05; sn41178615; ready=yes"]
        if m := re.fullmatch(r"switch-get(\d+)", command):
            return [f"switch{m[1]}.{self.links.get(int(m[1]), 0)}"]
        if m := re.fullmatch(r"switch-set(\d+)\.(\w+)", command):
            if not (m[2] == "disconnect" or m[2].isdigit()):
                return ["err2"]
            self.links[int(m[1])] = 0 if m[2] == "disconnect" else int(m[2])
            return [f"switch{m[1]}.{self.links[int(m[1])]}"]
        if m := re.fullmatch(r"aoutput-vol-get(\d+)", command):
            return [f"aoutput-vol{m[1]}:{self.volume.get(int(m[1]), -38)}dB"]
        if m := re.fullmatch(r"aoutput-vol-set(\d+):(-?\d+)dB", command):
            self.volume[int(m[1])] = int(m[2])
            return [f"aoutput-vol{m[1]}:{m[2]}dB"]
        if m := re.fullmatch(r"aoutput-mute-get(\d+)", command):
            return [f"aoutput-mute{m[1]}:{self.mute.get(int(m[1]), 'off')}"]
        if m := re.fullmatch(r"aoutput-mute-set(\d+):(on|off)", command):
            self.mute[int(m[1])] = m[2]
            return [f"aoutput-mute{m[1]}:{m[2]}"]
        if m := re.fullmatch(r"aoutput-conf-get(\d+)", command):
            return [f"aoutput-conf{m[1]}:{self.conf.get(int(m[1]), 'processed')}"]
        if m := re.fullmatch(r"aoutput-conf-set(\d+):(processed|passthru)", command):
            self.conf[int(m[1])] = m[2]
            return [f"aoutput-conf{m[1]}:{m[2]}"]
        if m := re.fullmatch(r"aoutput-mono-get(\d+)", command):
            return [f"aoutput-mono{m[1]}:{self.mono.get(int(m[1]), 'off')}"]
        if m := re.fullmatch(r"aoutput-mono-set(\d+):(on|off)", command):
            self.mono[int(m[1])] = m[2]
            return [f"aoutput-mono{m[1]}:{m[2]}"]
        if m := re.fullmatch(r"aoutput-delayboth-get(\d+)", command):
            return [f"aoutput-delayleft{m[1]}:0ms", f"aoutput-delayright{m[1]}:0ms"]
        return ["err1"]

    def set_commands(self):
        return [c for c in self.commands if "-set" in c]


def make_entry(port):
    sources = {
        "1": {CONF_NAME: "Sonos 1", CONF_ENABLED: True},
        "2": {CONF_NAME: "Sonos 2", CONF_ENABLED: True},
        "3": {CONF_NAME: "Input 3", CONF_ENABLED: False},
    }
    zones = {
        f"savant_{name}": {
            CONF_NAME: name.title(),
            CONF_NUMBER: number,
            CONF_ENABLED: True,
            DEFAULT_SOURCE: None,
        }
        for name, number in (("kitchen", 6), ("dining", 5), ("family", 13))
    }
    zones["savant_unused"] = {
        CONF_NAME: "Unused",
        CONF_NUMBER: 10,
        CONF_ENABLED: False,
        DEFAULT_SOURCE: None,
    }
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="sn41178615",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: port, CONF_NAME: "Savant"},
        options={CONF_SOURCES: sources, CONF_ZONES: zones},
    )


@pytest.fixture(autouse=True)
def quick_retries():
    with patch.object(sa, "RETRY_DELAY", 0):
        yield


@pytest.fixture
async def fake(socket_enabled):
    switch = FakeSwitch()
    await switch.start()
    yield switch
    await switch.stop()


@pytest.fixture
async def entry(hass, fake):
    config_entry = make_entry(fake.port)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    await settle(hass)
    yield config_entry
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def settle(hass):
    """Let the fake see any socket the client has just closed."""
    await hass.async_block_till_done()
    for _ in range(5):
        await asyncio.sleep(0.01)


async def wait_for(hass, predicate, timeout=5):
    """Wait on real socket I/O, which async_block_till_done does not track."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await hass.async_block_till_done()
            await asyncio.sleep(0.01)


async def next_poll(hass):
    coordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    await coordinator.async_refresh()
    await settle(hass)


async def call(hass, service, entity_id, **data):
    await hass.services.async_call(
        MEDIA_PLAYER, service, {"entity_id": entity_id, **data}, blocking=True
    )
    await settle(hass)


async def test_setup_reads_zones_and_releases_the_port(hass, fake, entry):
    assert fake.open_now == 0
    kitchen = hass.states.get(KITCHEN)
    assert kitchen.state == "on"
    assert kitchen.attributes["source"] == "Sonos 1"
    assert kitchen.attributes["volume_level"] == pytest.approx(10 / 23)
    assert hass.states.get(DINING).state == "off"
    # The disabled zone is never read.
    assert not [c for c in fake.commands if c.endswith("get10")]


async def test_polls_on_a_one_minute_timer(hass, fake, entry):
    connections = fake.connections

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await settle(hass)
    assert fake.connections == connections

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=1, seconds=1))
    await wait_for(hass, lambda: fake.connections == connections + 1)
    await wait_for(hass, lambda: fake.open_now == 0)


async def test_poll_is_one_socket_per_cycle(hass, fake, entry):
    connections = fake.connections
    fake.commands.clear()

    await next_poll(hass)

    assert fake.connections == connections + 1
    assert fake.open_now == 0
    # Every enabled zone, each read once. No zone polls on its own any more.
    assert len(fake.commands) == 3 * 6
    assert len(set(fake.commands)) == 3 * 6


async def test_poll_picks_up_changes_made_by_the_savant_host(hass, fake, entry):
    fake.links[5] = 2
    fake.volume[5] = -15
    fake.mute[5] = "on"
    fake.links[6] = 0

    await next_poll(hass)

    dining = hass.states.get(DINING)
    assert dining.state == "on"
    assert dining.attributes["source"] == "Sonos 2"
    assert dining.attributes["volume_level"] == 1.0
    assert dining.attributes["is_volume_muted"] is True
    assert hass.states.get(KITCHEN).state == "off"


async def test_poll_failure_marks_zones_unavailable_then_recovers(hass, fake, entry):
    fake.silent = True
    connections = fake.connections
    await next_poll(hass)
    assert hass.states.get(KITCHEN).state == STATE_UNAVAILABLE
    # Bounded: the first command gets its attempts, and the poll stops there.
    assert fake.connections == connections + sa.MAX_ATTEMPTS

    fake.silent = False
    await next_poll(hass)
    assert hass.states.get(KITCHEN).state == "on"
    assert fake.open_now == 0


async def test_error_reply_fails_the_poll(hass, fake, entry):
    fake.refuse[r"aoutput-vol-get6"] = "err4"
    await next_poll(hass)
    assert hass.states.get(KITCHEN).state == STATE_UNAVAILABLE


async def test_setup_retries_when_switch_is_down(hass, fake):
    config_entry = make_entry(fake.port)
    port = fake.port
    await fake.stop()

    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    await fake.start(port)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await wait_for(hass, lambda: config_entry.state is ConfigEntryState.LOADED)
    assert hass.states.get(KITCHEN).state == "on"
    await settle(hass)
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_retries_when_switch_is_silent(hass, fake):
    fake.silent = True
    config_entry = make_entry(fake.port)
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert fake.connections == sa.MAX_ATTEMPTS
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_command_updates_state_at_once_and_releases_the_port(hass, fake, entry):
    connections = fake.connections
    await call(hass, "volume_set", KITCHEN, volume_level=1.0)

    assert fake.volume[6] == -15
    assert hass.states.get(KITCHEN).attributes["volume_level"] == 1.0
    assert fake.connections == connections + 1
    assert fake.open_now == 0


async def test_volume_up_stops_at_the_ceiling(hass, fake, entry):
    fake.volume[6] = -16
    await next_poll(hass)

    await call(hass, "volume_up", KITCHEN)
    assert fake.volume[6] == -15

    fake.commands.clear()
    await call(hass, "volume_up", KITCHEN)
    assert fake.volume[6] == -15
    assert fake.set_commands() == []


async def test_volume_down_works_and_stops_at_the_floor(hass, fake, entry):
    await call(hass, "volume_down", KITCHEN)
    assert fake.volume[6] == -29

    fake.volume[6] = -37
    await next_poll(hass)
    await call(hass, "volume_down", KITCHEN)
    assert fake.volume[6] == -38

    fake.commands.clear()
    await call(hass, "volume_down", KITCHEN)
    assert fake.volume[6] == -38
    assert fake.set_commands() == []


async def test_volume_steps_on_a_zone_left_outside_the_window(hass, fake, entry):
    fake.volume[6] = -10
    await next_poll(hass)

    fake.commands.clear()
    await call(hass, "volume_up", KITCHEN)
    assert fake.volume[6] == -10
    assert fake.set_commands() == []

    await call(hass, "volume_down", KITCHEN)
    assert fake.volume[6] == -15


@pytest.mark.parametrize(
    ("mode", "mono", "conf"),
    [("stereo", "off", "processed"), ("mono", "on", "processed")],
)
async def test_sound_mode_sets_what_was_picked(hass, fake, entry, mode, mono, conf):
    fake.mono[6] = "on" if mono == "off" else "off"
    fake.conf[6] = "passthru"
    await next_poll(hass)

    await call(hass, "select_sound_mode", KITCHEN, sound_mode=mode)

    assert fake.mono[6] == mono
    assert fake.conf[6] == conf
    assert hass.states.get(KITCHEN).attributes["sound_mode"] == mode


@pytest.mark.parametrize("mode", ["stereo,passthru", "mono,passthru", "passthru", "bogus"])
async def test_amp_zones_refuse_passthru(hass, fake, entry, mode):
    fake.commands.clear()
    with pytest.raises(ServiceValidationError):
        await call(hass, "select_sound_mode", KITCHEN, sound_mode=mode)
    assert fake.set_commands() == []


async def test_only_avr_zones_offer_passthru(hass, fake, entry):
    assert hass.states.get(KITCHEN).attributes["sound_mode_list"] == ["stereo", "mono"]
    assert "stereo,passthru" in hass.states.get(FAMILY).attributes["sound_mode_list"]

    await call(hass, "select_sound_mode", FAMILY, sound_mode="stereo,passthru")
    assert fake.conf[13] == "passthru"
    assert fake.mono[13] == "off"


async def test_error_reply_reaches_the_caller(hass, fake, entry):
    fake.refuse[r"aoutput-vol-set6:.*"] = "err4"
    with pytest.raises(HomeAssistantError, match="hardware failure"):
        await call(hass, "volume_set", KITCHEN, volume_level=0.5)
    await settle(hass)
    assert fake.open_now == 0


async def test_turn_on_select_source_and_turn_off(hass, fake, entry):
    await call(hass, "turn_on", DINING)
    assert fake.links[5] == 1
    assert hass.states.get(DINING).attributes["source"] == "Sonos 1"

    await call(hass, "select_source", DINING, source="Sonos 2")
    assert fake.links[5] == 2
    assert hass.states.get(DINING).attributes["source"] == "Sonos 2"

    await call(hass, "turn_off", DINING)
    assert fake.links[5] == 0
    assert hass.states.get(DINING).state == "off"


async def test_kitchen_script_sequence(hass, fake, entry):
    """The one real script: turn_on, select_source, volume_set."""
    fake.links[6] = 0
    await next_poll(hass)

    await call(hass, "turn_on", KITCHEN)
    await call(hass, "select_source", KITCHEN, source="Sonos 1")
    await call(hass, "volume_set", KITCHEN, volume_level=0.3)

    state = hass.states.get(KITCHEN)
    assert state.state == "on"
    assert state.attributes["source"] == "Sonos 1"
    assert fake.volume[6] == -31
    assert fake.open_now == 0


async def test_unknown_source_fails_without_changing_the_route(hass, fake, entry):
    with pytest.raises(HomeAssistantError):
        await call(hass, "select_source", KITCHEN, source="No Such Source")
    assert fake.links[6] == 1


async def test_join_copies_the_source_to_members(hass, fake, entry):
    await call(hass, "join", KITCHEN, group_members=[DINING])
    assert fake.links[5] == 1
    assert hass.states.get(DINING).attributes["source"] == "Sonos 1"


async def test_join_with_an_unnamed_input_still_copies_the_route(hass, fake, entry):
    fake.links[6] = 3
    await next_poll(hass)
    await call(hass, "join", KITCHEN, group_members=[DINING])
    assert fake.links[5] == 3


async def test_join_from_a_zone_with_no_source_leaves_members_alone(hass, fake, entry):
    fake.links[5] = 2
    await next_poll(hass)
    await call(hass, "turn_off", KITCHEN)

    await call(hass, "join", KITCHEN, group_members=[DINING])
    assert fake.links[5] == 2


async def test_unjoin_clears_the_route(hass, fake, entry):
    await call(hass, "unjoin", KITCHEN)
    assert fake.links[6] == 0
    assert hass.states.get(KITCHEN).state == "off"


async def test_commands_during_a_poll_do_not_interleave(hass, fake, entry):
    fake.commands.clear()
    coordinator = entry.runtime_data
    await asyncio.gather(
        coordinator.async_refresh(),
        hass.services.async_call(
            MEDIA_PLAYER, "volume_set", {"entity_id": DINING, "volume_level": 1.0}, blocking=True
        ),
    )
    await settle(hass)
    assert fake.open_now == 0
    assert fake.volume[5] == -15
    position = fake.commands.index("aoutput-vol-set5:-15dB")
    # The set came either before or after the 18 poll commands, not among them.
    assert position == 0 or position == 18


def options_with_kitchen_disabled(entry):
    options = {**entry.options, CONF_ZONES: {**entry.options[CONF_ZONES]}}
    options[CONF_ZONES]["savant_kitchen"] = {
        **options[CONF_ZONES]["savant_kitchen"],
        CONF_ENABLED: False,
    }
    return options


async def test_zones_attach_to_the_switch_without_the_deprecated_lookup(hass, fake):
    config_entry = make_entry(fake.port)
    config_entry.add_to_hass(hass)
    # async_get_device stops working in HA 2027.8.
    with patch.object(dr.DeviceRegistry, "async_get_device") as deprecated:
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        await settle(hass)
        hass.config_entries.async_update_entry(
            config_entry, options=options_with_kitchen_disabled(config_entry)
        )
        await wait_for(hass, lambda: hass.states.get(KITCHEN) is None)
        await settle(hass)
    deprecated.assert_not_called()

    devices = dr.async_get(hass)
    switch = config_entry.runtime_data.switch
    parent = devices.async_get_device_by_identifier(
        (DOMAIN, switch.attributes["sn"]), config_entry.entry_id
    )
    assert parent is not None
    unique_id = er.async_get(hass).async_get(DINING).unique_id
    zone = devices.async_get_device_by_identifier(
        (DOMAIN, unique_id), config_entry.entry_id
    )
    assert zone.via_device_id == parent.id

    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_disabling_a_zone_removes_its_entity_and_device(hass, fake, entry):
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    unique_id = entities.async_get(KITCHEN).unique_id
    identifier = (DOMAIN, unique_id)
    assert devices.async_get_device_by_identifier(identifier, entry.entry_id)

    hass.config_entries.async_update_entry(
        entry, options=options_with_kitchen_disabled(entry)
    )
    await wait_for(hass, lambda: hass.states.get(KITCHEN) is None)
    await settle(hass)

    assert entities.async_get(KITCHEN) is None
    assert devices.async_get_device_by_identifier(identifier, entry.entry_id) is None
    assert hass.states.get(DINING) is not None
    assert fake.open_now == 0


async def test_disabling_a_zone_with_no_device_still_loads_the_others(hass, fake, entry):
    with patch.object(
        dr.DeviceRegistry, "async_get_device_by_identifier", return_value=None
    ):
        hass.config_entries.async_update_entry(
            entry, options=options_with_kitchen_disabled(entry)
        )
        await wait_for(hass, lambda: hass.states.get(KITCHEN) is None)
        await wait_for(hass, lambda: hass.states.get(DINING) is not None)
        await settle(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(DINING).state != STATE_UNAVAILABLE


async def test_reload_keeps_working_and_releases_the_port(hass, fake, entry):
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await settle(hass)
    assert fake.open_now == 0
    assert hass.states.get(KITCHEN).state == "on"

    connections = fake.connections
    await next_poll(hass)
    assert fake.connections == connections + 1


async def test_config_flow_checks_the_switch_and_releases_the_port(hass, fake):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake.port, CONF_NAME: "Savant"},
    )
    await hass.async_block_till_done()
    await settle(hass)
    assert result["type"] == "create_entry"
    assert fake.commands[:3] == ["fwrev", "fpga-rev", "status"]
    assert fake.open_now == 0
    entries = hass.config_entries.async_entries(DOMAIN)
    await hass.config_entries.async_unload(entries[0].entry_id)
    await hass.async_block_till_done()


async def test_config_flow_reports_a_dead_switch(hass, fake):
    port = fake.port
    await fake.stop()
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_HOST: "127.0.0.1", CONF_PORT: port, CONF_NAME: "Savant"},
    )
    assert result["type"] == "abort"
    assert result["reason"] == "cannot_connect"
