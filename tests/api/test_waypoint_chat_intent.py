import pytest

from world_engine.application.waypoint_chat import explicit_waypoint_choice


@pytest.mark.parametrize(
    ("text", "choice"),
    [
        ("我在驿站停留", "stop"),
        ("在此停留。", "stop"),
        ("我想停下来！", "stop"),
        ("我想在这里停留……", "stop"),
        ("stay here", "stop"),
        ("I WANT TO STOP HERE.", "stop"),
        ("请让我在此停留。", "stop"),
        ("继续原行程", "continue"),
        ("按原计划继续！", "continue"),
        ("不在这里停留，继续赶路", "continue"),
        ("我不在这里停留, 继续赶路。", "continue"),
        ("keep going", "continue"),
        ("Please continue the original itinerary.", "continue"),
        ("麻烦继续赶路。", "continue"),
    ],
)
def test_recognizes_allowlisted_whole_message_choices(text, choice):
    assert explicit_waypoint_choice(text) == choice


@pytest.mark.parametrize(
    "text",
    [
        "",
        "停留",
        "继续",
        "我在驿站停留并搜索附近",
        "继续赶路，然后买些食物",
        "如果这里安全，我就停留",
        "要不要在这里停留",
        "我可以在这里停留吗",
        "stay here?",
        "他说：我在驿站停留",
        "“我在驿站停留”",
        "'keep going'",
        "停留，或者继续赶路",
        "停留；继续赶路",
        "请问我可以在此停留吗？",
        "请让我在此停留并帮我找住处",
    ],
)
def test_ambiguous_questions_quotes_and_extra_actions_are_not_choices(text):
    assert explicit_waypoint_choice(text) is None
