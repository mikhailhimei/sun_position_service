"""Кастомный компонент Sun Position Service для Home Assistant."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Literal

from astral import LocationInfo
from astral.sun import azimuth, elevation
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType
import homeassistant.util.dt as dt_util

from .const import (
    ATTR_COVER_ENTITY_ID,
    ATTR_LUM,
    ATTR_PREVIOUS_STATE,
    ATTR_WINDOW_AZIMUTHS,
    DOMAIN,
    SERVICE_GET_STATE,
)

_LOGGER = logging.getLogger(__name__)

GeomResultType = Literal["direct", "side", "slightly", "open"]
CoverStateType = Literal["open", "side", "slightly", "direct"]


def _angle_diff(a: float, b: float) -> float:
    diff = abs(a - b) % 360.0
    return diff if diff <= 180.0 else 360.0 - diff


def _get_altitude_factor(sun_altitude: float) -> float:
    if sun_altitude <= 0:
        return 0.0
    if sun_altitude < 5:
        return 0.7
    if sun_altitude < 25:
        return 1.0
    if sun_altitude < 45:
        return 0.9
    if sun_altitude < 65:
        return 0.6
    return 0.4


def _calculate_single_azimuth_coverage(sun_az: float, sun_alt: float, target_az: float) -> float:
    diff = _angle_diff(sun_az, target_az)
    direct_limit = min(max(6.0, 30.0 - sun_alt * 0.6), 20.0)
    side_limit = direct_limit * 2.5
    base = 100.0 if diff <= direct_limit else (50.0 if diff <= side_limit else 0.0)
    return base * _get_altitude_factor(sun_alt)


def _calculate_blind_state(
    geom_coverage: float,
    geom_result: GeomResultType,
    lux: float,
    current_state: CoverStateType = "open",
) -> CoverStateType:
    if geom_result == "open" or geom_coverage < 5.0 or lux < 100.0:
        return "open"

    effective_lux = lux * (geom_coverage / 100.0)

    if current_state == "direct":
        if effective_lux < 4000.0 or geom_coverage < 30.0:
            return "side" if geom_result in ("direct", "side") else "slightly"
        return "direct"

    if current_state == "side":
        if effective_lux >= 7000.0 and geom_result == "direct":
            return "direct"
        if effective_lux < 1200.0:
            return "open"
        return "side"

    if current_state == "slightly":
        if effective_lux >= 8000.0 and geom_result == "direct":
            return "direct"
        if effective_lux < 800.0:
            return "open"
        return "slightly"

    if effective_lux >= 6000.0 and geom_result == "direct":
        return "direct"
    if effective_lux >= 2000.0 and geom_result in ("direct", "side"):
        return "side"
    if effective_lux >= 800.0 and geom_result in ("side", "slightly"):
        return "slightly"

    return "open"


SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_WINDOW_AZIMUTHS): vol.Any(
            vol.Coerce(float), [vol.Coerce(float)], None
        ),
        vol.Optional(ATTR_LUM): vol.Any(vol.Coerce(float), None),
        vol.Optional(ATTR_PREVIOUS_STATE): vol.In(["open", "side", "slightly", "direct"]),
        vol.Optional(ATTR_COVER_ENTITY_ID): cv.entity_id,
    }
)


def _register_services(hass: HomeAssistant) -> None:
    """Регистрация сервиса в HA."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_STATE):
        return

    def handle_get_state(call: ServiceCall) -> ServiceResponse:
        loc = LocationInfo(
            name=hass.config.location_name or "Home",
            region="HomeAssistant",
            timezone=str(hass.config.time_zone or "UTC"),
            latitude=float(hass.config.latitude),
            longitude=float(hass.config.longitude),
        )

        now_dt = dt_util.now()

        sun_az = float(azimuth(loc.observer, now_dt))
        sun_alt = float(elevation(loc.observer, now_dt))

        sun_info = {
            "sun_azimuth": round(sun_az, 1),
            "sun_altitude": round(sun_alt, 1),
        }

        current_lum: float | None = call.data.get(ATTR_LUM)
        last_state: CoverStateType = call.data.get(ATTR_PREVIOUS_STATE, "open")
        cover_entity: str | None = call.data.get(ATTR_COVER_ENTITY_ID)

        if call.data.get(ATTR_PREVIOUS_STATE) is None and cover_entity:
            cover_state_obj = hass.states.get(cover_entity)
            if cover_state_obj and cover_state_obj.state in ("open", "side", "slightly", "direct"):
                last_state = cover_state_obj.state  # type: ignore[assignment]

        if sun_alt <= 0:
            return {
                "result": "open",
                "geom_result": "open",
                "coverage": 0.0,
                "effective_lux": 0.0,
                "lum": current_lum,
                **sun_info,
            }

        raw_azimuths = call.data.get(ATTR_WINDOW_AZIMUTHS)
        if raw_azimuths is None:
            az_list: list[float] = []
        elif isinstance(raw_azimuths, (int, float)):
            az_list = [float(raw_azimuths)]
        else:
            az_list = [float(x) for x in raw_azimuths]

        if not az_list:
            return {
                "result": "open",
                "geom_result": "open",
                "coverage": 0.0,
                "effective_lux": 0.0,
                "lum": current_lum,
                **sun_info,
            }

        if len(az_list) == 1:
            coverage = _calculate_single_azimuth_coverage(sun_az, sun_alt, az_list[0])
        else:
            start, end = min(az_list), max(az_list)
            width = end - start

            if width == 0:
                coverage = _calculate_single_azimuth_coverage(sun_az, sun_alt, start)
            elif sun_az < start or sun_az > end:
                return {
                    "result": "open",
                    "geom_result": "open",
                    "coverage": 0.0,
                    "effective_lux": 0.0,
                    "lum": current_lum,
                    **sun_info,
                }
            else:
                center = (start + end) / 2.0
                distance = abs(sun_az - center)
                half = width / 2.0
                azimuth_factor = max(0.0, min(1.0, 1.0 - (distance / half)))
                coverage = azimuth_factor * _get_altitude_factor(sun_alt) * 100.0

        coverage = round(max(0.0, min(100.0, coverage)), 1)

        if coverage >= 70.0:
            geom_result: GeomResultType = "direct"
        elif coverage >= 35.0:
            geom_result: GeomResultType = "side"
        elif coverage > 10.0:
            geom_result: GeomResultType = "slightly"
        else:
            geom_result = "open"

        if current_lum is not None:
            effective_lux = round(current_lum * (coverage / 100.0), 1)
            result = _calculate_blind_state(
                coverage, geom_result, current_lum, last_state
            )
        else:
            effective_lux = 0.0
            result = geom_result

        return {
            "result": result,
            "geom_result": geom_result,
            "coverage": coverage,
            "effective_lux": effective_lux,
            "lum": current_lum,
            **sun_info,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_STATE,
        handle_get_state,
        schema=SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Поддержка конфигурации через YAML (configuration.yaml)."""
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Поддержка добавления через UI (Настройки -> Интеграции)."""
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Удаление интеграции через UI."""
    return True