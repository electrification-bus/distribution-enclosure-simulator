"""Internal seam over ebus_sdk property construction.

Localises Property construction so a future SDK change to the property-dict
shape touches one file. NOT an abstraction layer: other modules hold and mutate
``ebus_sdk.Property`` instances directly (the publisher calls ``Property.set_value``;
the SDK owns ``/set`` decode and value encoding).
"""

from __future__ import annotations

from typing import Any

import ebus_sdk
from ebus_sdk import MqttClient, PropertyDatatype, Unit


def make_property(
    *,
    node: ebus_sdk.Node,
    key: str,
    name: str,
    datatype: PropertyDatatype,
    unit: Unit | None,
    format_str: str | None,
    settable: bool,
) -> ebus_sdk.Property:
    """Construct an ebus_sdk.Property from a profile property and attach it to a node."""
    spec: dict[str, Any] = {
        "id": key,
        "name": name,
        "datatype": datatype,
    }
    if unit is not None:
        spec["unit"] = unit
    if format_str is not None:
        spec["format"] = format_str
    if settable:
        spec["settable"] = True
    return node.add_property_from_dict(spec)


def owned_client(mqttc: object) -> MqttClient | None:
    """Return *mqttc* when it is a client the SDK built and therefore owns.

    ebus-sdk 0.18 narrowed the injected-transport contract: ``MqttDeviceTransport``
    deliberately omits ``start`` / ``stop``, because those resolve only on the
    concrete client the SDK constructs for an ``mqtt_cfg=`` root — never on one the
    caller injected and still owns. The SDK makes the same distinction internally
    (``Controller.stop`` stops via its owned handle, "never via self.mqttc").

    Narrowing to the concrete class states that rule in the types instead of
    assuming it: a bring-your-own-transport root returns None here and is left for
    its owner to stop, which is the behaviour the SDK's contract asks for.
    """
    return mqttc if isinstance(mqttc, MqttClient) else None
