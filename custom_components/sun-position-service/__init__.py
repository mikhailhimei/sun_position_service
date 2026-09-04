"""Кастомный компонент Sun Position Service для Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Final, Literal

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
from homeassistant.helpers.typing import ConfigType
import homeassistant.util.dt as dt_util

from .const import (
    ATTR_LUM,
    ATTR_PREVIOUS_STATE,
    ATTR_WINDOW_AZIMUTHS,
    DOMAIN,
    SERVICE_GET_STATE,
)

_LOGGER = logging.getLogger(__name__)

GeomResultType = Literal["direct", "side", "slightly", "open"]
CoverStateType = Literal["open", "side", "slightly", "direct"]

VALID_STATES: Final[tuple[CoverStateType, ...]] = (
    "open",
    "side",
    "slightly",
    "direct",
)


@dataclass(frozen=True, slots=True)
class ThresholdConfig:
    """Конфигурация порогов фотометрии и геометрии."""

    # 1. Эмпирическое правило сильного залития окна
    high_coverage_pct: float = 90.0
    high_coverage_direct_lux: float = 5000.0

    # 2. Правило низкого солнца (слепящий эффект при остром угле к полу)
    low_altitude_deg: float = 25.0
    low_altitude_direct_lux: float = 8000.0
    default_side_direct_lux: float = 12000.0
    min_side_coverage_pct: float = 35.0

    # 3. Базовые переходы для геометрического direct
    standard_direct_open_lux: float = 6000.0
    standard_direct_side_lux: float = 7000.0

    # 4. Гистерезис удержания direct (anti-flapping на открытие)
    direct_retain_lux_low_alt: float = 3500.0
    direct_retain_lux_high_alt: float = 4500.0
    direct_retain_min_coverage: float = 25.0

    # 5. Пороги для side / slightly / open
    side_min_lux: float = 2000.0
    side_exit_lux: float = 1200.0
    slightly_min_lux: float = 800.0


THRESHOLDS: Final = ThresholdConfig()


def _angle_diff(a: float, b: float) -> float:
    """Вычисляет минимальную угловую разницу между двумя направлениями."""
    diff = abs(a - b) % 360.0
    return diff if diff <= 180.0 else 360.0 - diff


def _get_altitude_factor(sun_altitude: float) -> float:
    """Коэффициент проникновения солнечного потока в проем в зависимости от высоты."""
    if sun_altitude <= 0.0:
        return 0.0
    if sun_altitude < 5.0:
        return 0.7
    if sun_altitude < 25.0:
        return 1.0
    if sun_altitude < 45.0:
        return 0.9
    if sun_altitude < 65.0:
        return 0.6
    return 0.4


def _calculate_single_azimuth_coverage(
    sun_az: float, sun_alt: float, target_az: float
) -> float:
    """Расчет геометрического покрытия для одиночного направления."""
    diff = _angle_diff(sun_az, target_az)
    direct_limit = min(max(6.0, 30.0 - sun_alt * 0.6), 20.0)
    side_limit = direct_limit * 2.5
    base = 100.0 if diff <= direct_limit else (50.0 if diff <= side_limit else 0.0)
    return base * _get_altitude_factor(sun_alt)


def _calculate_range_azimuth_coverage(
    sun_az: float, sun_alt: float, start: float, end: float
) -> float:
    """Расчет покрытия для диапазона азимутов с корректной обработкой перехода через 0/360."""
    span = (end - start) % 360.0
    if span == 0.0:
        return _calculate_single_azimuth_coverage(sun_az, sun_alt, start)

    offset = (sun_az - start) % 360.0
    if offset > span:
        return 0.0

    center_offset = span / 2.0
    distance = abs(offset - center_offset)
    azimuth_factor = max(0.0, min(1.0, 1.0 - (distance / center_offset)))
    return azimuth_factor * _get_altitude_factor(sun_alt) * 100.0


def _should_trigger_direct(
    geom_result: GeomResultType,
    effective_lux: float,
    coverage: float,
    sun_altitude: float,
    from_state: CoverStateType,
) -> bool:
    """Предикат проверки условий перехода в режим direct."""
    # 1. Залитие окна >= 90% и люксы >= 5000
    if coverage >= THRESHOLDS.high_coverage_pct and effective_lux >= THRESHOLDS.high_coverage_direct_lux:
        return True

    # 2. Стандартный direct по геометрии
    direct_min_lux = (
        THRESHOLDS.standard_direct_side_lux
        if from_state in ("side", "slightly")
        else THRESHOLDS.standard_direct_open_lux
    )
    if geom_result == "direct" and effective_lux >= direct_min_lux:
        return True

    # 3. Боковой свет высокой интенсивности (ослепление низким или ярким солнцем)
    is_low_sun = 0.0 < sun_altitude <= THRESHOLDS.low_altitude_deg
    glare_lux_limit = (
        THRESHOLDS.low_altitude_direct_lux
        if is_low_sun
        else THRESHOLDS.default_side_direct_lux
    )
    if coverage >= THRESHOLDS.min_side_coverage_pct and effective_lux >= glare_lux_limit:
        return True

    return False


def _calculate_blind_state(
    geom_coverage: float,
    geom_result: GeomResultType,
    lux: float,
    sun_altitude: float,
    current_state: CoverStateType = "open",
) -> CoverStateType:
    """Расчет положения шторы с адаптивными порогами ослепления и гистерезисом."""
    if geom_result == "open" or geom_coverage < 5.0 or lux < 100.0:
        return "open"

    effective_lux = lux * (geom_coverage / 100.0)
    is_low_sun = 0.0 < sun_altitude <= THRESHOLDS.low_altitude_deg

    # 1. Гистерезис удержания direct
    if current_state == "direct":
        retain_lux = (
            THRESHOLDS.direct_retain_lux_low_alt
            if is_low_sun
            else THRESHOLDS.direct_retain_lux_high_alt
        )
        if effective_lux < retain_lux or geom_coverage < THRESHOLDS.direct_retain_min_coverage:
            return "side" if geom_result in ("direct", "side") else "slightly"
        return "direct"

    # 2. Централизованный переход в direct
    if _should_trigger_direct(geom_result, effective_lux, geom_coverage, sun_altitude, current_state):
        return "direct"

    # 3. Выход / удержание в side
    if current_state == "side":
        if effective_lux < THRESHOLDS.side_exit_lux:
            return "open"
        return "side"

    # 4. Выход / удержание в slightly
    if current_state == "slightly":
        if effective_lux < THRESHOLDS.slightly_min_lux:
            return "open"
        return "slightly"

    # 5. Базовый подъем из open
    if effective_lux >= THRESHOLDS.side_min_lux and geom_result in ("direct", "side"):
        return "side"
    if effective_lux >= THRESHOLDS.slightly_min_lux and geom_result in ("side", "slightly"):
        return "slightly"

    return "open"


SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_WINDOW_AZIMUTHS): vol.Any(
            vol.Coerce(float), [vol.Coerce(float)], None
        ),
        vol.Optional(ATTR_LUM): vol.Any(vol.Coerce(float), None),
        vol.Optional(ATTR_PREVIOUS_STATE, default="open"): vol.In(VALID_STATES),
    }
)


def _register_services(hass: HomeAssistant) -> None:
    """Регистрация сервиса в Home Assistant."""
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

        # Ночной режим
        if sun_alt <= 0.0:
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

        # Расчет покрытия окна
        if len(az_list) == 1:
            coverage = _calculate_single_azimuth_coverage(
                sun_az, sun_alt, az_list[0]
            )
        else:
            coverage = _calculate_range_azimuth_coverage(
                sun_az, sun_alt, min(az_list), max(az_list)
            )

        coverage = round(max(0.0, min(100.0, coverage)), 1)

        # Геометрическая категория
        if coverage >= 70.0:
            geom_result: GeomResultType = "direct"
        elif coverage >= 35.0:
            geom_result: GeomResultType = "side"
        elif coverage > 10.0:
            geom_result: GeomResultType = "slightly"
        else:
            geom_result = "open"

        # Фотометрический расчет
        if current_lum is not None:
            effective_lux = round(current_lum * (coverage / 100.0), 1)
            result = _calculate_blind_state(
                coverage, geom_result, current_lum, sun_alt, last_state
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
    """Поддержка конфигурации через YAML."""
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Поддержка добавления через UI."""
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Выгрузка интеграции."""
    return True
