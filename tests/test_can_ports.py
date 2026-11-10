import pytest
from rebot_teleop.__main__ import pin_arms


def test_pinned_ports_follow_the_adapters_when_names_swap():
    ports = {"can1": "1-2.1.2", "can2": "1-2.1.1.4"}
    arms, notes = pin_arms({"left": "can1", "right": "can2"}, {"left": "1-2.1.1.4", "right": "1-2.1.2"}, ports)
    assert arms == {"left": "can2", "right": "can1"}
    assert len(notes) == 2


def test_a_missing_adapter_keeps_its_arm_offline():
    arms, notes = pin_arms({"left": "can1", "right": "can2"}, {"left": "1-2.1.1.4", "right": "1-2.1.2"}, {"can1": "1-2.1.2"})
    assert arms["right"] == "can1"
    assert arms["left"].startswith("missing")
    assert "not plugged in" in notes[0]


def test_two_arms_on_one_adapter_are_refused():
    with pytest.raises(ValueError, match="same CAN adapter"):
        pin_arms({"left": "can1", "right": "can2"}, {"left": "1-2.1.2", "right": "1-2.1.2"}, {"can1": "1-2.1.2"})


def test_unpinned_arms_keep_their_interfaces():
    arms, notes = pin_arms({"left": "can1", "right": "can2"}, {}, {"can1": "x", "can2": "y"})
    assert arms == {"left": "can1", "right": "can2"} and notes == []
