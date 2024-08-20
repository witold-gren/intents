from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any, Dict, Optional, List, Set, Tuple

from hassil import parse_sentence
from hassil.intents import SlotList, TextSlotList, is_template
from hassil.recognize import RecognizeResult
from hassil.sample import sample_expression
from jinja2 import BaseLoader, Environment, StrictUndefined

_TEST_DATETIME = datetime(year=2013, month=9, day=17, hour=1, minute=2)


@dataclass
class State:
    """Minimal HA-like state object for responses."""

    entity_id: str
    name: str
    hass_state: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    area_id: Optional[str] = None
    human_state: Optional[str] = None
    aliases: Set[str] = field(default_factory=set)
    _domain: Optional[str] = None

    @property
    def state(self) -> str:
        if self.human_state is not None:
            return self.human_state

        return self.hass_state

    @property
    def domain(self) -> str:
        if self._domain is None:
            self._domain = self.entity_id.split(".", maxsplit=1)[0]

        return self._domain

    @property
    def state_with_unit(self) -> str:
        unit = self.attributes.get("unit_of_measurement")
        if unit:
            return f"{self.state} {unit}"

        return self.state


@dataclass
class AreaEntry:
    """Minimal HA-like area object for responses."""

    id: str
    name: str
    aliases: Set[str] = field(default_factory=set)


@dataclass
class Timer:
    """Minimal HA-like object for intent timers."""

    is_active: bool
    start_hours: Optional[int]
    start_minutes: Optional[int]
    start_seconds: Optional[int]
    rounded_hours_left: int
    rounded_minutes_left: int
    rounded_seconds_left: int
    name: Optional[str]
    area: Optional[str]
    total_seconds_left: int

    def asdict(self) -> Dict[str, Any]:
        """Convert to dict for response template."""
        return {
            "is_active": self.is_active,
            "start_hours": self.start_hours or 0,
            "start_minutes": self.start_minutes or 0,
            "start_seconds": self.start_seconds or 0,
            "rounded_hours_left": self.rounded_hours_left,
            "rounded_minutes_left": self.rounded_minutes_left,
            "rounded_seconds_left": self.rounded_seconds_left,
            "name": self.name or "",
            "area": self.area or "",
            "total_seconds_left": self.total_seconds_left,
        }


def get_matched_states(
    states: List[State], areas: List[AreaEntry], result: RecognizeResult
) -> Tuple[List[State], List[State]]:
    """Split states into matched/unmatched."""
    if result.intent.name in ("HassClimateGetTemperature", "HassClimateSetTemperature"):
        # Match climate entities only
        states = [state for state in states if state.domain == "climate"]
    elif result.intent.name in ("HassGetWeather",):
        # Match weather entities only
        states = [state for state in states if state.domain == "weather"]

    # Implement some matching logic from Home Assistant
    entity_name: Optional[str] = None
    name_entity = result.entities.get("name")
    if name_entity is not None:
        entity_name = _normalize_name(name_entity.value)

    area_name: Optional[str] = None
    area_entity = result.entities.get("area")
    if area_entity is not None:
        area_name = _normalize_name(area_entity.value)

    domain_name: Optional[str] = None
    domain_entity = result.entities.get("domain")
    if domain_entity is not None:
        domain_name = _normalize_name(domain_entity.value)

    device_class: Optional[str] = None
    device_class_entity = result.entities.get("device_class")
    if device_class_entity is not None:
        device_class = device_class_entity.value

    state_name: Optional[str] = None
    state_entity = result.entities.get("state")
    if state_entity is not None:
        state_name = state_entity.value

    # name -> id
    area_ids: Dict[str, str] = {}
    for area in areas:
        area_ids[_normalize_name(area.name)] = area.id
        for area_alias in area.aliases:
            area_ids[_normalize_name(area_alias)] = area.id

    matched: List[State] = []
    unmatched: List[State] = []

    for state in states:
        if entity_name is not None:
            name_match = _normalize_name(state.name) == entity_name

            if not name_match:
                # Check aliases
                for state_alias in state.aliases:
                    if _normalize_name(state_alias) == entity_name:
                        name_match = True
                        break

            if not name_match:
                # Filter by entity name
                continue

        if (area_name is not None) and (state.area_id != area_ids.get(area_name)):
            # Filter by area
            continue

        if (domain_name is not None) and (domain_name != state.domain):
            # Filter by domain
            continue

        if (device_class is not None) and (
            device_class != state.attributes.get("device_class")
        ):
            # Filter by entity name
            continue

        if state_name is not None:
            # Match state
            if state.hass_state == state_name:
                matched.append(state)
            else:
                unmatched.append(state)
        else:
            # Everything "matches" with no state constraint
            matched.append(state)

    return matched, unmatched


def get_matched_timers(timers: List[Timer], result: RecognizeResult) -> List[Timer]:
    if result.intent.name not in (
        "HassTimerStatus",
        "HassCancelTimer",
        "HassIncreaseTimer",
        "HassDecreaseTimer",
        "HassPauseTimer",
        "HassUnpauseTimer",
    ):
        return []

    slots = {slot.name: slot.value for slot in result.entities.values()}
    start_hours: Optional[int] = None
    start_minutes: Optional[int] = None
    start_seconds: Optional[int] = None

    if "start_hours" in slots:
        start_hours = slots["start_hours"]

    if "start_minutes" in slots:
        start_minutes = slots["start_minutes"]

    if "start_seconds" in slots:
        start_seconds = slots["start_seconds"]

    if (
        (start_hours is not None)
        or (start_minutes is not None)
        or (start_seconds is not None)
    ):
        timers = [
            t
            for t in timers
            if (t.start_hours == start_hours)
            and (t.start_minutes == start_minutes)
            and (t.start_seconds == start_seconds)
        ]
        if not timers:
            return []

    name = slots.get("name")
    if name:
        name = _normalize_name(name)
        timers = [t for t in timers if t.name == name]
        if not timers:
            return []

    area = slots.get("area")
    if area:
        area = _normalize_name(area)
        timers = [t for t in timers if t.area == area]
        if not timers:
            return []

    return timers


def _normalize_name(name: str) -> str:
    return name.strip().casefold()


def get_jinja2_environment() -> Environment:
    """Create default Jinja2 environment."""
    return Environment(loader=BaseLoader(), undefined=StrictUndefined)


def render_response(
    response_template: str,
    result: RecognizeResult,
    matched: List[State],
    unmatched: Optional[List[State]] = None,
    env: Optional[Environment] = None,
    timers: Optional[List[Timer]] = None,
) -> str:
    """Renders a response template using Jinja2."""
    assert response_template, "Empty response template"
    if unmatched is None:
        unmatched = []

    # First matched or unmatched state
    state1: Optional[State] = None
    if matched:
        state1 = matched[0]
    elif unmatched:
        state1 = unmatched[0]

    if env is None:
        env = get_jinja2_environment()

    slots: dict[str, Any] = {
        entity.name: entity.text_clean or entity.value
        for entity in result.entities_list
    }

    # For timer intents
    if timers:
        slots["timers"] = [t.asdict() for t in timers]
    else:
        slots["timers"] = []

    # For date/time intents
    slots["date"] = _TEST_DATETIME.date()
    slots["time"] = _TEST_DATETIME.time()

    return env.from_string(response_template).render(
        {
            "slots": slots,
            "state": state1,
            "query": {
                "matched": matched,
                "unmatched": unmatched,
                "total_results": len(matched) + len(unmatched),
            },
            "state_attr": partial(state_attr, matched),
        }
    )


def state_attr(states: List[State], entity_id: str, attr_name: str) -> Any | None:
    """Mimic state_attr from Home Assistant."""
    for state in states:
        if state.entity_id == entity_id:
            return state.attributes.get(attr_name)

    return None


def get_slot_lists(fixtures: dict[str, Any]) -> dict[str, SlotList]:
    # Load test areas and entities for language
    slot_lists: dict[str, SlotList] = {}

    # area/floor
    area_names: List[str] = []
    floor_names: List[str] = []
    for area in fixtures["areas"]:
        area_name = area["name"]
        if is_template(area_name):
            area_names.extend(
                area_name.strip()
                for area_name in sample_expression(parse_sentence(area_name))
            )
        else:
            area_names.append(area_name.strip())

        floor_name = area.get("floor")
        if not floor_name:
            continue

        if is_template(floor_name):
            floor_names.extend(
                floor_name.strip()
                for floor_name in sample_expression(parse_sentence(floor_name))
            )
        else:
            floor_names.append(floor_name.strip())

    slot_lists["area"] = TextSlotList.from_strings(area_names)
    slot_lists["floor"] = TextSlotList.from_strings(floor_names)

    # name
    entity_tuples: List[Tuple[str, str, Dict[str, Any]]] = []
    for entity in fixtures["entities"]:
        context = _entity_context(entity)
        entity_name = entity["name"]
        if is_template(entity_name):
            entity_names = list(
                name.strip() for name in sample_expression(parse_sentence(entity_name))
            )
        else:
            entity_names = [entity_name.strip()]

        entity_tuples.extend((name, name, context) for name in entity_names)

    slot_lists["name"] = TextSlotList.from_tuples(entity_tuples)

    return slot_lists


def _entity_context(entity: dict[str, Any]) -> dict[str, Any]:
    """Extract matching context from test fixture entity."""
    entity_id = entity["id"]
    domain = entity_id.split(".", maxsplit=1)[0]
    return {"domain": domain, **entity.get("attributes", {})}


def get_states(fixtures: dict[str, Any]) -> List[State]:
    """Load entity states from test fixtures."""
    states: List[State] = []
    for entity in fixtures.get("entities", []):
        # State may either be a string or a dict with in/out, where "in" is the
        # human (translated) name and "out" is the Home Assistant state name.
        entity_state = entity.get("state", "off")
        if isinstance(entity_state, str):
            hass_state = entity_state
            human_state: Optional[str] = None
        else:
            human_state = entity_state["in"]
            hass_state = entity_state["out"]

        entity_names: List[str] = []
        entity_name = entity["name"]
        if is_template(entity_name):
            entity_names.extend(
                entity_name.strip()
                for entity_name in sample_expression(parse_sentence(entity_name))
            )
        else:
            entity_names.append(entity_name.strip())

        states.append(
            State(
                entity_id=entity["id"],
                name=entity_names[0],
                aliases=set(entity_names[1:]),
                hass_state=hass_state,
                human_state=human_state,
                area_id=entity.get("area"),
                attributes=entity.get("attributes", {}),
            )
        )
    return states


def get_areas(fixtures: dict[str, Any]) -> List[AreaEntry]:
    """Load areas from test fixtures."""
    areas: List[AreaEntry] = []
    for area in fixtures.get("areas", []):
        area_names: List[str] = []
        area_name = area["name"]
        if is_template(area_name):
            area_names.extend(
                area_name.strip()
                for area_name in sample_expression(parse_sentence(area_name))
            )
        else:
            area_names.append(area_name.strip())

        areas.append(
            AreaEntry(id=area["id"], name=area_names[0], aliases=set(area_names[1:]))
        )
    return areas


def get_timers(fixtures: dict[str, Any]) -> List[Timer]:
    """Load timers from test fixtures."""
    timers: List[Timer] = []
    for timer in fixtures.get("timers", []):
        timer_name = timer.get("name")
        if timer_name:
            timer_name = _normalize_name(timer_name)

        timer_area = timer.get("area")
        if timer_area:
            # Resolve area id to name
            for area in fixtures.get("areas", []):
                if area["id"] == timer_area:
                    timer_area = area["name"]
                    break

            timer_area = _normalize_name(timer_area)

        timers.append(
            Timer(
                is_active=timer.get("is_active", True),
                start_hours=timer.get("start_hours"),
                start_minutes=timer.get("start_minutes"),
                start_seconds=timer.get("start_seconds"),
                rounded_hours_left=timer["rounded_hours_left"],
                rounded_minutes_left=timer["rounded_minutes_left"],
                rounded_seconds_left=timer["rounded_seconds_left"],
                name=timer_name,
                area=timer_area,
                total_seconds_left=timer["total_seconds_left"],
            )
        )
    return timers
