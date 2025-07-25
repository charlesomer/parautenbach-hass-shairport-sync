"""For media players that are controlled via MQTT."""
import hashlib
import logging

import voluptuous as vol
from homeassistant.components.media_player import (
    PLATFORM_SCHEMA,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
)
from homeassistant.components.media_player.const import (
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.components.mqtt import async_publish, async_subscribe
from homeassistant.components.mqtt.const import CONF_TOPIC
from homeassistant.components.mqtt.util import valid_publish_topic
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, Command, TopLevelTopic

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_TOPIC): valid_publish_topic,
    },
    extra=vol.REMOVE_EXTRA,
)

SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_SET
)


async def async_setup_platform(
    hass, config, async_add_entities, discovery_info=None
) -> None:
    """Set up the MQTT media players."""
    _LOGGER.debug(config)

    async_add_entities(
        [ShairportSyncMediaPlayer(hass, config.get(CONF_NAME), config.get(CONF_TOPIC),)]
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Setup the media player platform for Shairport Sync."""

    # Get config from entry
    config = config_entry.data

    async_add_entities(
        [ShairportSyncMediaPlayer(hass, config.get(CONF_NAME), config.get(CONF_TOPIC),)]
    )


class ShairportSyncMediaPlayer(MediaPlayerEntity):
    """Representation of an MQTT-controlled media player."""

    def __init__(self, hass, name, topic) -> None:
        """Initialize the MQTT media device."""
        _LOGGER.debug("Initialising %s", name)
        self.hass = hass
        self._name = name
        self._base_topic = topic
        self._remote_topic = f"{self._base_topic}/{TopLevelTopic.REMOTE}"
        self._player_state = MediaPlayerState.IDLE
        self._title = None
        self._artist = None
        self._album = None
        self._media_image = None
        self._subscriptions = []
        self._volume_level = None

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        await self._subscribe_to_topics()

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        _LOGGER.debug("Removing %s subscriptions", len(self._subscriptions))
        for unsubscribe in self._subscriptions:
            unsubscribe()

    def _set_state(self, state: MediaPlayerState) -> None:
        """Update the player state."""

        _LOGGER.debug("Setting state to '%s'.", state)
        self._player_state = state

        # Clear metadata in idle state so media card doesn't display stale data
        if state == MediaPlayerState.IDLE:
            self._title = None
            self._artist = None
            self._album = None
            self._media_image = None

        self.async_write_ha_state()

    def calculate_volume_level(self, volume: str) -> float:
        """Calculate the volume level from a string."""
        if volume is None:
            return 0.0
        try:
            volume_parts = volume.split(",")
            volume_level = float(volume_parts[0])
            calculated_volume = (volume_level / 30) + 1
            calculated_volume = max(0.0, min(1.0, calculated_volume))
            _LOGGER.debug("Calculated volume level: %s", calculated_volume)
            return calculated_volume
        except (ValueError, IndexError) as e:
            _LOGGER.error("Error calculating volume level: %s", e)
            return 0.0

    async def _subscribe_to_topics(self):
        """(Re)Subscribe to topics."""

        @callback
        def play_started(_) -> None:
            """Handle the play MQTT message."""
            _LOGGER.debug("Play started")
            self._set_state(MediaPlayerState.PLAYING)

        @callback
        def play_ended(_) -> None:
            """Handle the pause MQTT message."""
            _LOGGER.debug("Play ended")
            self._set_state(MediaPlayerState.PAUSED)

        @callback
        def active_ended(_) -> None:
            """Handle the active ended MQTT message."""
            _LOGGER.debug("Active ended")
            self._set_state(MediaPlayerState.IDLE)

        def set_metadata(attr):
            """Construct a callback that sets the desired metadata attribute."""

            @callback
            def _callback(msg) -> None:
                setattr(self, f"_{attr}", msg.payload)
                _LOGGER.debug("New %s: %s", attr, msg.payload)

            return _callback

        @callback
        def artwork_updated(message) -> None:
            """Handle the artwork updated MQTT message."""
            # https://en.wikipedia.org/wiki/Magic_number_%28programming%29
            # https://en.wikipedia.org/wiki/List_of_file_signatures
            header = " ".join(f"{b:02X}" for b in message.payload[:4])
            _LOGGER.debug(
                "New artwork (%s bytes); header: %s", len(message.payload), header
            )
            self._media_image = message.payload
            self.async_write_ha_state()

        @callback
        def set_metadata_volume_level(msg) -> None:
            """Handle the volume MQTT message."""
            calculated_volume = self.calculate_volume_level(msg.payload)
            _LOGGER.debug("New volume level: %s", calculated_volume)
            self._volume_level = calculated_volume
            self.async_write_ha_state()

        topic_map = {
            TopLevelTopic.PLAY_START: (play_started, "utf-8"),
            TopLevelTopic.PLAY_RESUME: (play_started, "utf-8"),
            TopLevelTopic.PLAY_END: (play_ended, "utf-8"),
            TopLevelTopic.PLAY_FLUSH: (play_ended, "utf-8"),
            TopLevelTopic.ACTIVE_END: (active_ended, "utf-8"),
            TopLevelTopic.ARTIST: (set_metadata("artist"), "utf-8"),
            TopLevelTopic.ALBUM: (set_metadata("album"), "utf-8"),
            TopLevelTopic.TITLE: (set_metadata("title"), "utf-8"),
            TopLevelTopic.COVER: (artwork_updated, None),
            TopLevelTopic.VOLUME: (set_metadata_volume_level, "utf-8"),
        }

        for (top_level_topic, (topic_callback, encoding)) in topic_map.items():
            topic = f"{self._base_topic}/{top_level_topic}"
            _LOGGER.debug(
                "Subscribing to topic %s with callback %s",
                topic,
                topic_callback.__name__,
            )
            subscription = await async_subscribe(
                self.hass, topic, topic_callback, encoding=encoding
            )
            self._subscriptions.append(subscription)

    @property
    def should_poll(self) -> bool:
        """No polling needed."""
        return False

    @property
    def unique_id(self) -> str:
        return f"shairport-sync-{self._base_topic}"

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._base_topic)},
            "name": self.name,
            "manufacturer": "mikebrady",
        }

    @property
    def name(self) -> str:
        """Return the name of the player."""
        _LOGGER.debug("Getting name: %s", self._name)
        return self._name

    @property
    def state(self) -> MediaPlayerState | None:
        """Return the current state of the media player."""
        _LOGGER.debug("Getting state: %s", self._player_state)
        return self._player_state

    @property
    def media_content_type(self) -> MediaType | None:
        """Return the content type of currently playing media."""
        _LOGGER.debug("Getting media content type: %s", MediaType.MUSIC)
        return MediaType.MUSIC

    @property
    def media_title(self) -> str | None:
        """Title of current playing media."""
        _LOGGER.debug("Getting media title: %s", self._title)
        return self._title

    @property
    def media_artist(self) -> str | None:
        """Artist of current playing media, music track only."""
        _LOGGER.debug("Getting media artist: %s", self._artist)
        return self._artist

    @property
    def media_album_name(self) -> str | None:
        """Album of current playing media, music track only."""
        _LOGGER.debug("Getting media album: %s", self._album)
        return self._album

    @property
    def media_image_hash(self) -> str | None:
        """Hash value for the media image."""
        if self._media_image:
            image_hash = hashlib.md5(self._media_image).hexdigest()
            _LOGGER.debug("Media image hash: %s", image_hash)
            return image_hash
        return None

    @property
    def volume_level(self) -> float | None:
        """Return the volume level of the media player."""
        _LOGGER.debug("Getting volume level: %s", self._volume_level)
        return self._volume_level

    @property
    def supported_features(self) -> int:
        """Flag media player features that are supported."""
        return SUPPORTED_FEATURES

    @property
    def device_class(self) -> MediaPlayerDeviceClass:
        return MediaPlayerDeviceClass.SPEAKER

    async def _send_remote_command(self, command) -> None:
        """Send a command to the remote control topic."""
        _LOGGER.debug("Sending '%s' command", command)
        await async_publish(self.hass, self._remote_topic, command)

    async def _send_command_update_state(
        self, command: Command, state: MediaPlayerState
    ) -> None:
        """Send the command and update local state."""
        await self._send_remote_command(command)
        self._set_state(state)

    async def async_media_play(self) -> None:
        """Send play command."""
        await self._send_command_update_state(Command.PLAY, MediaPlayerState.PLAYING)

    async def async_media_pause(self) -> None:
        """Send pause command."""
        await self._send_command_update_state(Command.PAUSE, MediaPlayerState.PAUSED)

    async def async_media_stop(self) -> None:
        """Send stop command."""
        await self._send_command_update_state(Command.STOP, MediaPlayerState.IDLE)

    async def async_media_previous_track(self) -> None:
        """Send previous track command."""
        await self._send_remote_command(Command.SKIP_PREVIOUS)

    async def async_media_next_track(self) -> None:
        """Send next track command."""
        await self._send_remote_command(Command.SKIP_NEXT)

    async def async_volume_up(self) -> None:
        """Turn volume up for media player."""
        await self._send_remote_command(Command.VOLUME_UP)

    async def async_volume_down(self) -> None:
        """Turn volume down for media player."""
        await self._send_remote_command(Command.VOLUME_DOWN)

    def set_volume_level(self, volume: float) -> None:
        """PLACEHOLDER"""
        _LOGGER.debug("Setting volume level to %s", volume)
        pass

    async def async_media_play_pause(self) -> None:
        """Play or pause the media player."""
        _LOGGER.debug(
            "Sending toggle play/pause command; currently %s", self._player_state
        )
        if self._player_state == MediaPlayerState.PLAYING:
            await self._send_command_update_state(
                Command.PAUSE, MediaPlayerState.PAUSED
            )
        else:
            await self._send_command_update_state(
                Command.PLAY, MediaPlayerState.PLAYING
            )

    async def async_get_media_image(self) -> tuple[str | None, str | None]:
        """Fetch the image of the currently playing media."""
        _LOGGER.debug("Getting media image")
        if self._media_image:
            return (self._media_image, "image/jpeg")
        return (None, None)