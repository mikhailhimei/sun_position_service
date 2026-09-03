"""Кастомный компонент Sun Position Service для Home Assistant."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any, Final, Literal

from astral import LocationInfo
from astral.sun import azimuth, elevation, sun
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

# Дополнительные ключи для отладки
ATTR_DATE: Final = "date"
ATTR_ALL: Final = "all"

GeomResultType = Literal["direct", "side", "slightly", "open"]
CoverStateType = Literal["open", "slightly", "side", "direct"]

VALID_STATES: Final[tuple[CoverStateType, ...]] = (
    "open",
    "slightly",
    "side",
    "direct",
)


def _angle_diff(a: float, b: float) -> float:
    """Вычисляет минимальную угловую разницу между двумя направлениями."""
    diff = abs(a - b) % 360.0
    return diff if diff <= 180.0 else 360.0 - diff


def _get_altitude_factor(sun_altitude: float) -> float:
    """Коэффициент проникновения солнечного потока в зависимости от высоты."""
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


def _compute_coverage(az_list: list[float], sun_az: float, sun_alt: float) -> float:
    """Вспомогательная функция вычисления покрытия по геометрии."""
    if not az_list or sun_alt <= 0.0:
        return 0.0

    if len(az_list) == 1:
        coverage = _calculate_single_azimuth_coverage(sun_az, sun_alt, az_list[0])
    else:
        start, end = min(az_list), max(az_list)
        width = end - start

        if width == 0.0:
            coverage = _calculate_single_azimuth_coverage(sun_az, sun_alt, start)
        elif sun_az < start or sun_az > end:
            return 0.0
        else:
            center = (start + end) / 2.0
            distance = abs(sun_az - center)
            half = width / 2.0
            azimuth_factor = max(0.0, min(1.0, 1.0 - (distance / half)))
            coverage = azimuth_factor * _get_altitude_factor(sun_alt) * 100.0

    return round(max(0.0, min(100.0, coverage)), 1)


def _coverage_to_geom_result(coverage: float) -> GeomResultType:
    """Определение категории геометрического результата."""
    if coverage >= 70.0:
        return "direct"
    if coverage >= 35.0:
        return "side"
    if coverage > 10.0:
        return "slightly"
    return "open"


def _calculate_blind_state(
    geom_coverage: float,
    geom_result: GeomResultType,
    lux: float,
    sun_altitude: float,
    current_state: CoverStateType = "open",
) -> CoverStateType:
    """Расчет положения шторы с жестким гео-ограничением и гистерезисом по эффективным люксам."""
    if geom_result == "open" or geom_coverage < 10.0 or lux < 300.0:
        return "open"

    effective_lux = lux * (geom_coverage / 100.0)
    is_low_sun = 0.0 < sun_altitude <= 25.0

    direct_on = 10000.0 if is_low_sun else 14000.0
    direct_off = 6000.0 if is_low_sun else 8000.0

    side_on = 4000.0
    side_off = 2200.0

    slightly_on = 1500.0
    slightly_off = 700.0

    if current_state == "direct":
        if effective_lux >= direct_off:
            target_state = "direct"
        elif effective_lux >= side_off:
            target_state = "side"
        elif effective_lux >= slightly_off:
            target_state = "slightly"
        else:
            target_state = "open"

    elif current_state == "side":
        if effective_lux >= direct_on:
            target_state = "direct"
        elif effective_lux >= side_off:
            target_state = "side"
        elif effective_lux >= slightly_off:
            target_state = "slightly"
        else:
            target_state = "open"

    elif current_state == "slightly":
        if effective_lux >= direct_on:
            target_state = "direct"
        elif effective_lux >= side_on:
            target_state = "side"
        elif effective_lux >= slightly_off:
            target_state = "slightly"
        else:
            target_state = "open"

    else:  # current_state == "open"
        if effective_lux >= direct_on:
            target_state = "direct"
        elif effective_lux >= side_on:
            target_state = "side"
        elif effective_lux >= slightly_on:
            target_state = "slightly"
        else:
            target_state = "open"

    allowed_order: list[CoverStateType] = ["open", "slightly", "side", "direct"]
    max_allowed_idx = allowed_order.index(geom_result)
    target_idx = allowed_order.index(target_state)

    final_idx = min(target_idx, max_allowed_idx)
    return allowed_order[final_idx]


SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_WINDOW_AZIMUTHS): vol.Any(
            vol.Coerce(float), [vol.Coerce(float)], None
        ),
        vol.Optional(ATTR_LUM): vol.Any(vol.Coerce(float), None),
        vol.Optional(ATTR_PREVIOUS_STATE, default="open"): vol.Any(
            vol.In(VALID_STATES),
            None,
            "",
        ),
        # Скрытые сервисные параметры (доступны только через YAML)
        vol.Optional(ATTR_DATE): vol.Any(cv_string := str, None),
        vol.Optional(ATTR_ALL, default=False): vol.Coerce(bool),
    }
)


def _register_services(hass: HomeAssistant) -> None:
    """Регистрация сервиса в Home Assistant."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_STATE):
        return

    def handle_get_state(call: ServiceCall) -> ServiceResponse:
        tz = dt_util.get_time_zone(hass.config.time_zone or "UTC")
        loc = LocationInfo(
            name=hass.config.location_name or "Home",
            region="HomeAssistant",
            timezone=str(hass.config.time_zone or "UTC"),
            latitude=float(hass.config.latitude),
            longitude=float(hass.config.longitude),
        )

        # Парсим дату или берем текущее время
        date_str = call.data.get(ATTR_DATE)
        if date_str:
            try:
                base_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
            except ValueError:
                base_date = dt_util.now().date()
        else:
            base_date = dt_util.now().date()

        current_lum: float | None = call.data.get(ATTR_LUM)
        raw_prev_state = call.data.get(ATTR_PREVIOUS_STATE)
        last_state: CoverStateType = (
            raw_prev_state if raw_prev_state in VALID_STATES else "open"
        )

        raw_azimuths = call.data.get(ATTR_WINDOW_AZIMUTHS)
        if raw_azimuths is None:
            az_list: list[float] = []
        elif isinstance(raw_azimuths, (int, float)):
            az_list = [float(raw_azimuths)]
        else:
            az_list = [float(x) for x in raw_azimuths]

        # --- РЕЖИМ ОТЛАДКИ: ВСЕ ТОЧКИ ДНЯ (all: true) ---
        if call.data.get(ATTR_ALL):
            try:
                s_info = sun(loc.observer, date=base_date, tzinfo=tz)
                # Берем запас в 30 минут до восхода и после заката
                start_dt = s_info["sunrise"] - timedelta(minutes=30)
                end_dt = s_info["sunset"] + timedelta(minutes=30)
            except Exception:
                start_dt = datetime.combine(base_date, datetime.min.time(), tzinfo=tz)
                end_dt = datetime.combine(base_date, datetime.max.time(), tzinfo=tz)

            step = timedelta(minutes=15)
            curr_dt = start_dt
            running_state = last_state
            timeline: list[dict[str, Any]] = []

            while curr_dt <= end_dt:
                s_az = float(azimuth(loc.observer, curr_dt))
                s_alt = float(elevation(loc.observer, curr_dt))

                cov = _compute_coverage(az_list, s_az, s_alt)
                g_res = _coverage_to_geom_result(cov)

                if current_lum is not None:
                    eff_lux = round(current_lum * (cov / 100.0), 1)
                    res = _calculate_blind_state(cov, g_res, current_lum, s_alt, running_state)
                else:
                    eff_lux = 0.0
                    res = g_res

                timeline.append({
                    "time": curr_dt.strftime("%H:%M"),
                    "sun_azimuth": round(s_az, 1),
                    "sun_altitude": round(s_alt, 1),
                    "coverage": cov,
                    "effective_lux": eff_lux,
                    "geom_result": g_res,
                    "result": res,
                })
                # Сохраняем состояние для отработки гистерезиса на следующем шаге
                running_state = res
                curr_dt += step

            return {
                "date": base_date.strftime("%Y-%m-%d"),
                "lum_tested": current_lum,
                "points_count": len(timeline),
                "timeline": timeline,
            }

        # --- ОБЫЧНЫЙ РЕЖИМ: ОДНО ТЕКУЩЕЕ ЗНАЧЕНИЕ ---
        now_dt = dt_util.now()
        sun_az = float(azimuth(loc.observer, now_dt))
        sun_alt = float(elevation(loc.observer, now_dt))

        sun_info = {
            "sun_azimuth": round(sun_az, 1),
            "sun_altitude": round(sun_alt, 1),
        }

        coverage = _compute_coverage(az_list, sun_az, sun_alt)
        geom_result = _coverage_to_geom_result(coverage)

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
