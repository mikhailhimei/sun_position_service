# Sun Position & Blind Controller (HACS Integration)

Кастомный компонент для Home Assistant, предназначенный для адаптивного управления шторами, жалюзи и рольставнями.

Интеграция сопоставляет астрономическое положение солнца (азимут и высоту над горизонтом) относительно плоскости окна с фактическими показаниями датчика освещенности (lux/lum), предотвращая ложные срабатывания и дребезг привода с помощью концепции эффективной инсоляции и гистерезиса.

================================================================================
ВОЗМОЖНОСТИ
================================================================================

* Точные астрономические координаты: расчет положения солнца через библиотеку astral с автоматическим использованием системных координат Home Assistant.
* Поддержка секторов остекления: принимает как одиночный азимут (например, 110), так и диапазон видимости окна ([40, 135]).
* Эффективная инсоляция (Effective Lux): отсекает высокий рассеянный уличный свет, если солнце геометрически не попадает внутрь проема окна.
* Встроенный гистерезис: разнесенные пороги открытия и закрытия защищают моторы от частого переключения при переменной облачности.
* Stateless-вызов с ответом: поддержка механизма SupportsResponse.ONLY для прямого возврата данных в переменную response_variable автоматизаций.

================================================================================
АРХИТЕКТУРА РАБОТЫ
================================================================================

[Время + Координаты HA] ---> Геометрия (Азимут / Высота) ---> Coverage (0-100%)
                                                                    |
[Датчик света (Lux)]   ---------------------------------------------+---> Effective Lux
                                                                    |
[Предыдущее состояние] ---> Гистерезис и матрица переходов ---------+---> Target State

1. Расчет геометрического покрытия (coverage)
Определяет процент попадания солнечного диска в оконный проем:
* Для диапазона азимутов [A_start, A_end] вычисляется затухание от центра проема к его краям.
* Учитывается высота солнца над горизонтом (altitude_factor): низкое солнце светит вглубь комнаты значительно сильнее полуденного.

Категории геометрии (geom_result):
* direct   -- >= 70% (прямое ослепляющее солнце).
* side     -- 35% .. 69% (боковой падающий свет).
* slightly -- 10% .. 34% (касательный свет на краю откоса).
* open     -- < 10% (солнце находится вне сектора видимости окна).

2. Расчет эффективного светового потока (effective_lux)
Уличный люксметр измеряет общую освещенность атмосферы. Сервис проецирует яркость на геометрию конкретного стекла:

   effective_lux = lum * (coverage / 100)

Если на улице 35 000 lx, но солнце уже ушло с направления окна (coverage = 0%), то effective_lux = 0, и шторы не будут закрываться зря.

3. Матрица гистерезиса переходов

| Текущее состояние | Целевое состояние | Условие перехода                          |
| ----------------- | ----------------- | ----------------------------------------- |
| open              | -> direct         | geom: direct И eff_lux >= 6000 lx         |
| open              | -> side           | geom: direct/side И eff_lux >= 2000 lx    |
| open              | -> slightly       | geom: side/slightly И eff_lux >= 800 lx   |
| direct            | -> side/slightly  | eff_lux < 4000 lx ИЛИ coverage < 30%      |
| side              | -> open           | eff_lux < 1200 lx                         |
| slightly          | -> open           | eff_lux < 800 lx                          |

================================================================================
УСТАНОВКА
================================================================================

Через HACS (Custom Repository):
1. Откройте HACS -> Интеграции.
2. В верхнем правом углу нажмите три точки -> Пользовательские репозитории.
3. Укажите URL: mikhailhimei/sun_position_service, категория: Интеграция.
4. Нажмите Добавить, найдите интеграцию в поиске и нажмите Загрузить.
5. Перезагрузите Home Assistant.
6. Перейдите в Настройки -> Устройства и службы -> Добавить интеграцию -> выберите Sun Position Service.

Ручная установка:
1. Скопируйте папку custom_components/sun_position_service в директорию /config/custom_components/.
2. Перезагрузите Home Assistant.
3. Добавьте интеграцию через меню Настройки -> Интеграции.

================================================================================
СЕРВИС: sun_position_service.get_state
================================================================================

Входные параметры:
* window_azimuths (number | list): Одиночный азимут (110) или диапазон ([40, 135]).
* lum (float): Значение освещенности с датчика (люксы).
* previous_state (string): Текущее состояние шторы (open, side, slightly, direct). По умолчанию: open.
* cover_entity_id (string): Сущность, из которой можно автоматически прочитать previous_state.

Формат ответа (response_variable):
{
  "result": "side",
  "geom_result": "side",
  "coverage": 61.5,
  "effective_lux": 6150.0,
  "lum": 10000.0,
  "sun_azimuth": 105.8,
  "sun_altitude": 19.5
}

================================================================================
ПРИМЕР АВТОМАТИЗАЦИИ
================================================================================

alias: "Управление шторами гостиной"
trigger:
  - platform: time_pattern
    minutes: "/5"
  - platform: state
    entity_id: sensor.outdoor_illuminance

action:
  # 1. Расчет инсоляции и целевого состояния
  - action: sun_position_service.get_state
    data:
      window_azimuths:
        - 40
        - 135
      lum: "{{ states('sensor.outdoor_illuminance') | float(0) }}"
      previous_state: "{{ states('input_select.living_room_blind_state') }}"
    response_variable: solar

  # 2. Позиционирование привода
  - choose:
      - conditions:
          - condition: template
            value_template: "{{ solar.result == 'direct' }}"
        sequence:
          - action: cover.close_cover
            target:
              entity_id: cover.living_room_blind

      - conditions:
          - condition: template
            value_template: "{{ solar.result == 'side' }}"
        sequence:
          - action: cover.set_cover_position
            target:
              entity_id: cover.living_room_blind
            data:
              position: 50

      - conditions:
          - condition: template
            value_template: "{{ solar.result == 'slightly' }}"
        sequence:
          - action: cover.set_cover_position
            target:
              entity_id: cover.living_room_blind
            data:
              position: 75

      - conditions:
          - condition: template
            value_template: "{{ solar.result == 'open' }}"
        sequence:
          - action: cover.open_cover
            target:
              entity_id: cover.living_room_blind

  # 3. Сохранение состояния для гистерезиса
  - action: input_select.select_option
    target:
      entity_id: input_select.living_room_blind_state
    data:
      option: "{{ solar.result }}"

