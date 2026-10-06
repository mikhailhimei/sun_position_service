"""Run with: python -m unittest discover -s tests -v (Home Assistant installed)."""
import importlib.util
from pathlib import Path
import unittest

import voluptuous as vol
from homeassistant import config_entries, loader
from homeassistant.core import HomeAssistant

COMPONENT = Path(__file__).resolve().parents[1] / 'custom_components/sun-position-service'
spec = importlib.util.spec_from_file_location('sun_position_under_test', COMPONENT / '__init__.py', submodule_search_locations=[str(COMPONENT)])
import sys
component = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = component
spec.loader.exec_module(component)


class BrightnessTests(unittest.TestCase):
    def test_user_example_and_hysteresis(self):
        calculate = component._calculate_blind_state
        self.assertEqual(calculate(89.9, 'direct', 4949, 27.7), 'side')
        self.assertEqual(calculate(89.9, 'direct', 4949, 27.7, 'open', 150), 'tilted')
        self.assertEqual(calculate(89.9, 'direct', 4949, 27.7, 'side', 150), 'side')
        self.assertEqual(calculate(89.9, 'direct', 3600, 27.7, 'side', 150), 'tilted')

    def test_side_boundaries(self):
        calculate = component._calculate_blind_state
        self.assertEqual(calculate(90, 'direct', 6000 / .9, 30, 'open', 150), 'side')
        self.assertEqual(calculate(90, 'direct', 5999 / .9, 30, 'open', 150), 'tilted')
        self.assertEqual(calculate(90, 'direct', 3300 / .9, 30, 'side', 150), 'side')
        self.assertEqual(calculate(90, 'direct', 3299 / .9, 30, 'side', 150), 'tilted')

    def test_normal_incidence_rule_scales(self):
        calculate = component._calculate_blind_state
        self.assertEqual(calculate(98, 'direct', 2000, 30), 'direct')
        self.assertEqual(calculate(98, 'direct', 2000, 30, 'open', 150), 'open')
        self.assertEqual(calculate(98, 'direct', 2250, 30, 'open', 150), 'direct')

    def test_schema(self):
        schema = component.SERVICE_SCHEMA
        self.assertEqual(schema({})['required_brightness_percent'], 100)
        self.assertEqual(schema({'required_brightness_percent': '150'})['required_brightness_percent'], 150)
        for invalid in (0, -1, 501, None, 'abc', float('inf'), float('nan')):
            with self.subTest(invalid=invalid), self.assertRaises(vol.Invalid):
                schema({'required_brightness_percent': invalid})


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_parameter_reaches_normal_and_day_calculations(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            loader.async_setup(hass)
            hass.config_entries = config_entries.ConfigEntries(hass, {})
            hass.config.latitude = 56.84
            hass.config.longitude = 60.61
            hass.config.time_zone = 'Asia/Yekaterinburg'
            try:
                await component.async_setup(hass, {})
                async def call(data):
                    return await hass.services.async_call('sun_position_service', 'get_state', data, blocking=True, return_response=True)
                data = {'window_azimuths': [40, 135], 'lum': 4949, 'required_brightness_percent': 150}
                with patch.object(component, '_compute_coverage', return_value=89.9):
                    response = await call(data)
                    self.assertEqual(response['result'], 'tilted')
                    self.assertEqual(response['effective_lux'], 4449.2)
                    day = await call({**data, 'all': True, 'date': '2026-06-21'})
                    self.assertGreater(day['points_count'], 40)
                    self.assertTrue(all(p['result'] == 'tilted' for p in day['timeline']))
                    geometry = await call({'window_azimuths': [40, 135], 'required_brightness_percent': 500})
                    self.assertEqual(geometry['result'], 'direct')
            finally:
                await hass.async_stop(force=True)
