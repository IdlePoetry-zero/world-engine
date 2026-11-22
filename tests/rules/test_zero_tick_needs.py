from world_engine.domain.components import NeedsComponent
from world_engine.domain.needs import advance_passive_needs, next_deprivation_damage_tick


def test_deprivation_starting_at_zero_retains_its_boundary_across_windows():
    day = 86_400
    needs = NeedsComponent(food={"value": 19, "critical_below": 20})
    for start, end in ((0, 3600), (3600, 7200)):
        needs = advance_passive_needs(needs, from_tick=start, to_tick=end, day_seconds=day).needs
        assert needs.food.last_critical_damage_tick == 0
        assert next_deprivation_damage_tick(needs, from_tick=end, day_seconds=day) == day
