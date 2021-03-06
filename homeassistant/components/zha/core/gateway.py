"""
Virtual gateway for Zigbee Home Automation.

For more details about this component, please refer to the documentation at
https://home-assistant.io/components/zha/
"""

import asyncio
import collections
import itertools
import logging
from homeassistant import const as ha_const
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_component import EntityComponent
from . import const as zha_const
from .const import (
    COMPONENTS, CONF_DEVICE_CONFIG, DATA_ZHA, DATA_ZHA_CORE_COMPONENT, DOMAIN,
    ZHA_DISCOVERY_NEW, DEVICE_CLASS, SINGLE_INPUT_CLUSTER_DEVICE_CLASS,
    SINGLE_OUTPUT_CLUSTER_DEVICE_CLASS, COMPONENT_CLUSTERS, HUMIDITY,
    TEMPERATURE, ILLUMINANCE, PRESSURE, METERING, ELECTRICAL_MEASUREMENT,
    POWER_CONFIGURATION, GENERIC, SENSOR_TYPE, EVENT_RELAY_CLUSTERS,
    LISTENER_BATTERY, UNKNOWN, OPENING, ZONE, OCCUPANCY,
    CLUSTER_REPORT_CONFIGS, REPORT_CONFIG_IMMEDIATE, REPORT_CONFIG_ASAP,
    REPORT_CONFIG_DEFAULT, REPORT_CONFIG_MIN_INT, REPORT_CONFIG_MAX_INT,
    REPORT_CONFIG_OP, SIGNAL_REMOVE)
from .device import ZHADevice
from ..device_entity import ZhaDeviceEntity
from .listeners import (
    LISTENER_REGISTRY, AttributeListener, EventRelayListener, ZDOListener)
from .helpers import convert_ieee

_LOGGER = logging.getLogger(__name__)

SENSOR_TYPES = {}
BINARY_SENSOR_TYPES = {}
EntityReference = collections.namedtuple(
    'EntityReference', 'reference_id zha_device cluster_listeners device_info')


class ZHAGateway:
    """Gateway that handles events that happen on the ZHA Zigbee network."""

    def __init__(self, hass, config):
        """Initialize the gateway."""
        self._hass = hass
        self._config = config
        self._component = EntityComponent(_LOGGER, DOMAIN, hass)
        self._devices = {}
        self._device_registry = collections.defaultdict(list)
        hass.data[DATA_ZHA][DATA_ZHA_CORE_COMPONENT] = self._component

    def device_joined(self, device):
        """Handle device joined.

        At this point, no information about the device is known other than its
        address
        """
        # Wait for device_initialized, instead
        pass

    def raw_device_initialized(self, device):
        """Handle a device initialization without quirks loaded."""
        # Wait for device_initialized, instead
        pass

    def device_initialized(self, device):
        """Handle device joined and basic information discovered."""
        self._hass.async_create_task(
            self.async_device_initialized(device, True))

    def device_left(self, device):
        """Handle device leaving the network."""
        pass

    def device_removed(self, device):
        """Handle device being removed from the network."""
        device = self._devices.pop(device.ieee, None)
        self._device_registry.pop(device.ieee, None)
        if device is not None:
            self._hass.async_create_task(device.async_unsub_dispatcher())
            async_dispatcher_send(
                self._hass,
                "{}_{}".format(SIGNAL_REMOVE, str(device.ieee))
            )

    def get_device(self, ieee_str):
        """Return ZHADevice for given ieee."""
        ieee = convert_ieee(ieee_str)
        return self._devices.get(ieee)

    def get_entity_reference(self, entity_id):
        """Return entity reference for given entity_id if found."""
        for entity_reference in itertools.chain.from_iterable(
                self.device_registry.values()):
            if entity_id == entity_reference.reference_id:
                return entity_reference

    @property
    def devices(self):
        """Return devices."""
        return self._devices

    @property
    def device_registry(self):
        """Return entities by ieee."""
        return self._device_registry

    def register_entity_reference(
            self, ieee, reference_id, zha_device, cluster_listeners,
            device_info):
        """Record the creation of a hass entity associated with ieee."""
        self._device_registry[ieee].append(
            EntityReference(
                reference_id=reference_id,
                zha_device=zha_device,
                cluster_listeners=cluster_listeners,
                device_info=device_info
            )
        )

    async def _get_or_create_device(self, zigpy_device):
        """Get or create a ZHA device."""
        zha_device = self._devices.get(zigpy_device.ieee)
        if zha_device is None:
            zha_device = ZHADevice(self._hass, zigpy_device, self)
            self._devices[zigpy_device.ieee] = zha_device
        return zha_device

    async def async_device_initialized(self, device, is_new_join):
        """Handle device joined and basic information discovered (async)."""
        zha_device = await self._get_or_create_device(device)
        discovery_infos = []
        endpoint_tasks = []
        for endpoint_id, endpoint in device.endpoints.items():
            endpoint_tasks.append(self._async_process_endpoint(
                endpoint_id, endpoint, discovery_infos, device, zha_device,
                is_new_join
            ))
        await asyncio.gather(*endpoint_tasks)

        await zha_device.async_initialize(not is_new_join)

        discovery_tasks = []
        for discovery_info in discovery_infos:
            discovery_tasks.append(_dispatch_discovery_info(
                self._hass,
                is_new_join,
                discovery_info
            ))
        await asyncio.gather(*discovery_tasks)

        device_entity = _create_device_entity(zha_device)
        await self._component.async_add_entities([device_entity])

    async def _async_process_endpoint(
            self, endpoint_id, endpoint, discovery_infos, device, zha_device,
            is_new_join):
        """Process an endpoint on a zigpy device."""
        import zigpy.profiles

        if endpoint_id == 0:  # ZDO
            await _create_cluster_listener(
                endpoint,
                zha_device,
                is_new_join,
                listener_class=ZDOListener
            )
            return

        component = None
        profile_clusters = ([], [])
        device_key = "{}-{}".format(device.ieee, endpoint_id)
        node_config = {}
        if CONF_DEVICE_CONFIG in self._config:
            node_config = self._config[CONF_DEVICE_CONFIG].get(
                device_key, {}
            )

        if endpoint.profile_id in zigpy.profiles.PROFILES:
            profile = zigpy.profiles.PROFILES[endpoint.profile_id]
            if zha_const.DEVICE_CLASS.get(endpoint.profile_id,
                                          {}).get(endpoint.device_type,
                                                  None):
                profile_clusters = profile.CLUSTERS[endpoint.device_type]
                profile_info = zha_const.DEVICE_CLASS[endpoint.profile_id]
                component = profile_info[endpoint.device_type]

        if ha_const.CONF_TYPE in node_config:
            component = node_config[ha_const.CONF_TYPE]
            profile_clusters = zha_const.COMPONENT_CLUSTERS[component]

        if component and component in COMPONENTS:
            profile_match = await _handle_profile_match(
                self._hass, endpoint, profile_clusters, zha_device,
                component, device_key, is_new_join)
            discovery_infos.append(profile_match)

        discovery_infos.extend(await _handle_single_cluster_matches(
            self._hass,
            endpoint,
            zha_device,
            profile_clusters,
            device_key,
            is_new_join
        ))


async def _create_cluster_listener(cluster, zha_device, is_new_join,
                                   listeners=None, listener_class=None):
    """Create a cluster listener and attach it to a device."""
    if listener_class is None:
        listener_class = LISTENER_REGISTRY.get(cluster.cluster_id,
                                               AttributeListener)
    listener = listener_class(cluster, zha_device)
    if is_new_join:
        await listener.async_configure()
    zha_device.add_cluster_listener(listener)
    if listeners is not None:
        listeners.append(listener)


async def _dispatch_discovery_info(hass, is_new_join, discovery_info):
    """Dispatch or store discovery information."""
    component = discovery_info['component']
    if is_new_join:
        async_dispatcher_send(
            hass,
            ZHA_DISCOVERY_NEW.format(component),
            discovery_info
        )
    else:
        hass.data[DATA_ZHA][component][discovery_info['unique_id']] = \
            discovery_info


async def _handle_profile_match(hass, endpoint, profile_clusters, zha_device,
                                component, device_key, is_new_join):
    """Dispatch a profile match to the appropriate HA component."""
    in_clusters = [endpoint.in_clusters[c]
                   for c in profile_clusters[0]
                   if c in endpoint.in_clusters]
    out_clusters = [endpoint.out_clusters[c]
                    for c in profile_clusters[1]
                    if c in endpoint.out_clusters]

    listeners = []
    cluster_tasks = []

    for cluster in in_clusters:
        cluster_tasks.append(_create_cluster_listener(
            cluster, zha_device, is_new_join, listeners=listeners))

    for cluster in out_clusters:
        cluster_tasks.append(_create_cluster_listener(
            cluster, zha_device, is_new_join, listeners=listeners))

    await asyncio.gather(*cluster_tasks)

    discovery_info = {
        'unique_id': device_key,
        'zha_device': zha_device,
        'listeners': listeners,
        'component': component
    }

    if component == 'binary_sensor':
        discovery_info.update({SENSOR_TYPE: UNKNOWN})
        cluster_ids = []
        cluster_ids.extend(profile_clusters[0])
        cluster_ids.extend(profile_clusters[1])
        for cluster_id in cluster_ids:
            if cluster_id in BINARY_SENSOR_TYPES:
                discovery_info.update({
                    SENSOR_TYPE: BINARY_SENSOR_TYPES.get(
                        cluster_id, UNKNOWN)
                })
                break

    return discovery_info


async def _handle_single_cluster_matches(hass, endpoint, zha_device,
                                         profile_clusters, device_key,
                                         is_new_join):
    """Dispatch single cluster matches to HA components."""
    cluster_matches = []
    cluster_match_tasks = []
    event_listener_tasks = []
    for cluster in endpoint.in_clusters.values():
        if cluster.cluster_id not in profile_clusters[0]:
            cluster_match_tasks.append(_handle_single_cluster_match(
                hass,
                zha_device,
                cluster,
                device_key,
                zha_const.SINGLE_INPUT_CLUSTER_DEVICE_CLASS,
                is_new_join,
            ))

    for cluster in endpoint.out_clusters.values():
        if cluster.cluster_id not in profile_clusters[1]:
            cluster_match_tasks.append(_handle_single_cluster_match(
                hass,
                zha_device,
                cluster,
                device_key,
                zha_const.SINGLE_OUTPUT_CLUSTER_DEVICE_CLASS,
                is_new_join,
            ))

        if cluster.cluster_id in EVENT_RELAY_CLUSTERS:
            event_listener_tasks.append(_create_cluster_listener(
                cluster,
                zha_device,
                is_new_join,
                listener_class=EventRelayListener
            ))
    await asyncio.gather(*event_listener_tasks)
    cluster_match_results = await asyncio.gather(*cluster_match_tasks)
    for cluster_match in cluster_match_results:
        if cluster_match is not None:
            cluster_matches.append(cluster_match)
    return cluster_matches


async def _handle_single_cluster_match(hass, zha_device, cluster, device_key,
                                       device_classes, is_new_join):
    """Dispatch a single cluster match to a HA component."""
    component = None  # sub_component = None
    for cluster_type, candidate_component in device_classes.items():
        if isinstance(cluster, cluster_type):
            component = candidate_component
            break

    if component is None or component not in COMPONENTS:
        return
    listeners = []
    await _create_cluster_listener(cluster, zha_device, is_new_join,
                                   listeners=listeners)
    # don't actually create entities for PowerConfiguration
    # find a better way to do this without abusing single cluster reg
    from zigpy.zcl.clusters.general import PowerConfiguration
    if cluster.cluster_id == PowerConfiguration.cluster_id:
        return

    cluster_key = "{}-{}".format(device_key, cluster.cluster_id)
    discovery_info = {
        'unique_id': cluster_key,
        'zha_device': zha_device,
        'listeners': listeners,
        'entity_suffix': '_{}'.format(cluster.cluster_id),
        'component': component
    }

    if component == 'sensor':
        discovery_info.update({
            SENSOR_TYPE: SENSOR_TYPES.get(cluster.cluster_id, GENERIC)
        })
    if component == 'binary_sensor':
        discovery_info.update({
            SENSOR_TYPE: BINARY_SENSOR_TYPES.get(cluster.cluster_id, UNKNOWN)
        })

    return discovery_info


def _create_device_entity(zha_device):
    """Create ZHADeviceEntity."""
    device_entity_listeners = []
    if LISTENER_BATTERY in zha_device.cluster_listeners:
        listener = zha_device.cluster_listeners.get(LISTENER_BATTERY)
        device_entity_listeners.append(listener)
    return ZhaDeviceEntity(zha_device, device_entity_listeners)


def establish_device_mappings():
    """Establish mappings between ZCL objects and HA ZHA objects.

    These cannot be module level, as importing bellows must be done in a
    in a function.
    """
    from zigpy import zcl
    from zigpy.profiles import PROFILES, zha, zll

    if zha.PROFILE_ID not in DEVICE_CLASS:
        DEVICE_CLASS[zha.PROFILE_ID] = {}
    if zll.PROFILE_ID not in DEVICE_CLASS:
        DEVICE_CLASS[zll.PROFILE_ID] = {}

    EVENT_RELAY_CLUSTERS.append(zcl.clusters.general.LevelControl.cluster_id)
    EVENT_RELAY_CLUSTERS.append(zcl.clusters.general.OnOff.cluster_id)

    DEVICE_CLASS[zha.PROFILE_ID].update({
        zha.DeviceType.ON_OFF_SWITCH: 'binary_sensor',
        zha.DeviceType.LEVEL_CONTROL_SWITCH: 'binary_sensor',
        zha.DeviceType.REMOTE_CONTROL: 'binary_sensor',
        zha.DeviceType.SMART_PLUG: 'switch',
        zha.DeviceType.LEVEL_CONTROLLABLE_OUTPUT: 'light',
        zha.DeviceType.ON_OFF_LIGHT: 'light',
        zha.DeviceType.DIMMABLE_LIGHT: 'light',
        zha.DeviceType.COLOR_DIMMABLE_LIGHT: 'light',
        zha.DeviceType.ON_OFF_LIGHT_SWITCH: 'binary_sensor',
        zha.DeviceType.DIMMER_SWITCH: 'binary_sensor',
        zha.DeviceType.COLOR_DIMMER_SWITCH: 'binary_sensor',
    })

    DEVICE_CLASS[zll.PROFILE_ID].update({
        zll.DeviceType.ON_OFF_LIGHT: 'light',
        zll.DeviceType.ON_OFF_PLUGIN_UNIT: 'switch',
        zll.DeviceType.DIMMABLE_LIGHT: 'light',
        zll.DeviceType.DIMMABLE_PLUGIN_UNIT: 'light',
        zll.DeviceType.COLOR_LIGHT: 'light',
        zll.DeviceType.EXTENDED_COLOR_LIGHT: 'light',
        zll.DeviceType.COLOR_TEMPERATURE_LIGHT: 'light',
        zll.DeviceType.COLOR_CONTROLLER: 'binary_sensor',
        zll.DeviceType.COLOR_SCENE_CONTROLLER: 'binary_sensor',
        zll.DeviceType.CONTROLLER: 'binary_sensor',
        zll.DeviceType.SCENE_CONTROLLER: 'binary_sensor',
        zll.DeviceType.ON_OFF_SENSOR: 'binary_sensor',
    })

    SINGLE_INPUT_CLUSTER_DEVICE_CLASS.update({
        zcl.clusters.general.OnOff: 'switch',
        zcl.clusters.measurement.RelativeHumidity: 'sensor',
        zcl.clusters.measurement.TemperatureMeasurement: 'sensor',
        zcl.clusters.measurement.PressureMeasurement: 'sensor',
        zcl.clusters.measurement.IlluminanceMeasurement: 'sensor',
        zcl.clusters.smartenergy.Metering: 'sensor',
        zcl.clusters.homeautomation.ElectricalMeasurement: 'sensor',
        zcl.clusters.general.PowerConfiguration: 'sensor',
        zcl.clusters.security.IasZone: 'binary_sensor',
        zcl.clusters.measurement.OccupancySensing: 'binary_sensor',
        zcl.clusters.hvac.Fan: 'fan',
    })

    SINGLE_OUTPUT_CLUSTER_DEVICE_CLASS.update({
        zcl.clusters.general.OnOff: 'binary_sensor',
    })

    SENSOR_TYPES.update({
        zcl.clusters.measurement.RelativeHumidity.cluster_id: HUMIDITY,
        zcl.clusters.measurement.TemperatureMeasurement.cluster_id:
        TEMPERATURE,
        zcl.clusters.measurement.PressureMeasurement.cluster_id: PRESSURE,
        zcl.clusters.measurement.IlluminanceMeasurement.cluster_id:
        ILLUMINANCE,
        zcl.clusters.smartenergy.Metering.cluster_id: METERING,
        zcl.clusters.homeautomation.ElectricalMeasurement.cluster_id:
        ELECTRICAL_MEASUREMENT,
        zcl.clusters.general.PowerConfiguration.cluster_id:
        POWER_CONFIGURATION,
    })

    BINARY_SENSOR_TYPES.update({
        zcl.clusters.measurement.OccupancySensing.cluster_id: OCCUPANCY,
        zcl.clusters.security.IasZone.cluster_id: ZONE,
        zcl.clusters.general.OnOff.cluster_id: OPENING
    })

    CLUSTER_REPORT_CONFIGS.update({
        zcl.clusters.general.OnOff.cluster_id: [{
            'attr': 'on_off',
            'config': REPORT_CONFIG_IMMEDIATE
        }],
        zcl.clusters.general.LevelControl.cluster_id: [{
            'attr': 'current_level',
            'config': REPORT_CONFIG_ASAP
        }],
        zcl.clusters.lighting.Color.cluster_id: [{
            'attr': 'current_x',
            'config': REPORT_CONFIG_DEFAULT
        }, {
            'attr': 'current_y',
            'config': REPORT_CONFIG_DEFAULT
        }, {
            'attr': 'color_temperature',
            'config': REPORT_CONFIG_DEFAULT
        }],
        zcl.clusters.measurement.RelativeHumidity.cluster_id: [{
            'attr': 'measured_value',
            'config': (
                REPORT_CONFIG_MIN_INT,
                REPORT_CONFIG_MAX_INT,
                50
            )
        }],
        zcl.clusters.measurement.TemperatureMeasurement.cluster_id: [{
            'attr': 'measured_value',
            'config': (
                REPORT_CONFIG_MIN_INT,
                REPORT_CONFIG_MAX_INT,
                50
            )
        }],
        zcl.clusters.measurement.PressureMeasurement.cluster_id: [{
            'attr': 'measured_value',
            'config': REPORT_CONFIG_DEFAULT
        }],
        zcl.clusters.measurement.IlluminanceMeasurement.cluster_id: [{
            'attr': 'measured_value',
            'config': REPORT_CONFIG_DEFAULT
        }],
        zcl.clusters.smartenergy.Metering.cluster_id: [{
            'attr': 'instantaneous_demand',
            'config': REPORT_CONFIG_DEFAULT
        }],
        zcl.clusters.homeautomation.ElectricalMeasurement.cluster_id: [{
            'attr': 'active_power',
            'config': REPORT_CONFIG_DEFAULT
        }],
        zcl.clusters.general.PowerConfiguration.cluster_id: [{
            'attr': 'battery_voltage',
            'config': REPORT_CONFIG_DEFAULT
        }, {
            'attr': 'battery_percentage_remaining',
            'config': REPORT_CONFIG_DEFAULT
        }],
        zcl.clusters.measurement.OccupancySensing.cluster_id: [{
            'attr': 'occupancy',
            'config': REPORT_CONFIG_IMMEDIATE
        }],
        zcl.clusters.hvac.Fan.cluster_id: [{
            'attr': 'fan_mode',
            'config': REPORT_CONFIG_OP
        }],
    })

    # A map of hass components to all Zigbee clusters it could use
    for profile_id, classes in DEVICE_CLASS.items():
        profile = PROFILES[profile_id]
        for device_type, component in classes.items():
            if component not in COMPONENT_CLUSTERS:
                COMPONENT_CLUSTERS[component] = (set(), set())
            clusters = profile.CLUSTERS[device_type]
            COMPONENT_CLUSTERS[component][0].update(clusters[0])
            COMPONENT_CLUSTERS[component][1].update(clusters[1])
