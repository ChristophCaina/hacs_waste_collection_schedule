"""Sensor platform support for Waste Collection Schedule."""

import collections
import datetime
import logging
from enum import Enum

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import CONF_NAME, CONF_VALUE_TEMPLATE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, UPDATE_SENSORS_SIGNAL

_LOGGER = logging.getLogger(__name__)

CONF_SOURCE_INDEX = "source_index"
CONF_DETAILS_FORMAT = "details_format"
CONF_COUNT = "count"
CONF_LEADTIME = "leadtime"
CONF_DATE_TEMPLATE = "date_template"
CONF_APPOINTMENT_TYPES = "types"


class DetailsFormat(Enum):
    """Values for CONF_DETAILS_FORMAT."""

    upcoming = "upcoming"  # list of "<date> <type1, type2, ...>"
    appointment_types = "appointment_types"  # list of "<type> <date>"
    generic = "generic"  # all values in separate attributes


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_SOURCE_INDEX, default=0): cv.positive_int,
        vol.Optional(CONF_DETAILS_FORMAT, default="upcoming"): cv.enum(DetailsFormat),
        vol.Optional(CONF_COUNT): cv.positive_int,
        vol.Optional(CONF_LEADTIME): cv.positive_int,
        vol.Optional(CONF_APPOINTMENT_TYPES): cv.ensure_list,
        vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
        vol.Optional(CONF_DATE_TEMPLATE): cv.template,
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    value_template = config.get(CONF_VALUE_TEMPLATE)
    if value_template is not None:
        value_template.hass = hass

    date_template = config.get(CONF_DATE_TEMPLATE)
    if date_template is not None:
        date_template.hass = hass

    entities = []

    entities.append(
        ScheduleSensor(
            hass=hass,
            api=hass.data[DOMAIN],
            name=config[CONF_NAME],
            source_index=config[CONF_SOURCE_INDEX],
            details_format=config[CONF_DETAILS_FORMAT],
            count=config.get(CONF_COUNT),
            leadtime=config.get(CONF_LEADTIME),
            appointment_types=config.get(CONF_APPOINTMENT_TYPES),
            value_template=value_template,
            date_template=date_template,
        )
    )

    async_add_entities(entities)


class ScheduleSensor(Entity):
    """Base for sensors."""

    def __init__(
        self,
        hass,
        api,
        name,
        source_index,
        details_format,
        count,
        leadtime,
        appointment_types,
        value_template,
        date_template,
    ):
        """Initialize the entity."""
        self._api = api
        self._name = name
        self._source_index = source_index
        self._details_format = details_format
        self._count = count
        self._leadtime = leadtime
        self._appointment_types = appointment_types
        self._value_template = value_template
        self._date_template = date_template

        self._state = STATE_UNKNOWN
        self._icon = None
        self._picture = None
        self._attributes = []

        async_dispatcher_connect(hass, UPDATE_SENSORS_SIGNAL, self._update_sensor)

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return self._name

    @property
    def should_poll(self):
        return False

    @property
    def icon(self):
        return "mdi:trash-can" if self._icon is None else self._icon

    @property
    def entity_picture(self):
        return self._picture

    @property
    def state(self):
        """Return the state of the entity."""
        return self._state

    @property
    def device_state_attributes(self):
        """Return attributes for the entity."""
        return self._attributes

    async def async_added_to_hass(self):
        """Entities have been added to hass."""
        self._update_sensor()

    @property
    def _scraper(self):
        return self._api.get_scraper(self._source_index)

    @property
    def _separator(self):
        """Return separator string used to join waste types."""
        return self._api.separator

    @property
    def _include_today(self):
        """Return true if appointments for today shall be included in the results."""
        return datetime.datetime.now().time() < self._api._day_switch_time

    def _add_refreshtime(self):
        """Add refresh-time (= last fetch time) to device-state-attributes."""
        refreshtime = ""
        if self._scraper.refreshtime is not None:
            refreshtime = self._scraper.refreshtime.strftime("%x %X")
        self._attributes["attribution"] = f"Last update: {refreshtime}"

    def _set_state(self, upcoming):
        """Set entity state with default format."""
        if len(upcoming) == 0:
            self._state = ""
            self._icon = None
            self._picture = None
            return

        appointment = upcoming[0]
        # appointment::=CollectionAppointmentGroup{date=2020-04-01, types=['Type1', 'Type2']}

        if self._value_template is not None:
            self._state = self._value_template.async_render_with_possible_json_value(
                appointment, None
            )
        else:
            self._state = f"{self._separator.join(appointment.types)} in {appointment.daysTo} days"

        self._icon = appointment.icon
        self._picture = appointment.picture

    def _render_date(self, appointment):
        if self._date_template is not None:
            return self._date_template.async_render_with_possible_json_value(
                appointment, None
            )
        else:
            return appointment.date.isoformat()

    @callback
    def _update_sensor(self):
        """Update the state and the device-state-attributes of the entity.

        Called if a new data has been fetched from the scraper source.
        """
        if self._scraper is None:
            _LOGGER.error(f"source_index {self._source_index} out of range")
            return None

        self._set_state(
            self._scraper.get_upcoming_group_by_day(
                count=1,
                types=self._appointment_types,
                include_today=self._include_today,
            )
        )

        attributes = collections.OrderedDict()

        appointment_types = (
            sorted(self._scraper.get_types())
            if self._appointment_types is None
            else self._appointment_types
        )

        if self._details_format == DetailsFormat.upcoming:
            # show upcoming events list in details
            upcoming = self._scraper.get_upcoming_group_by_day(
                count=self._count,
                leadtime=self._leadtime,
                types=self._appointment_types,
                include_today=self._include_today,
            )
            for appointment in upcoming:
                attributes[self._render_date(appointment)] = self._separator.join(
                    appointment.types
                )
        elif self._details_format == DetailsFormat.appointment_types:
            # show list of appointments in details
            for t in appointment_types:
                appointments = self._scraper.get_upcoming(
                    count=1, types=[t], include_today=self._include_today
                )
                date = (
                    "" if len(appointments) == 0 else self._render_date(appointments[0])
                )
                attributes[t] = date
        elif self._details_format == DetailsFormat.generic:
            # insert generic attributes into details
            attributes["types"] = appointment_types
            attributes["upcoming"] = self._scraper.get_upcoming(
                count=self._count,
                leadtime=self._leadtime,
                types=self._appointment_types,
                include_today=self._include_today,
            )
            refreshtime = ""
            if self._scraper.refreshtime is not None:
                refreshtime = self._scraper.refreshtime.isoformat(timespec="seconds")
            attributes["last_update"] = refreshtime

        self._attributes = attributes
        self._add_refreshtime()

        if self.hass is not None:
            self.async_schedule_update_ha_state()
