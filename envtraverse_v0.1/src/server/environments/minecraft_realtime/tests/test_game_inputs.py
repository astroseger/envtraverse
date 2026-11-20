from types import SimpleNamespace

import pytest

from minecraft_realtime.game.bridge.controls import InputController
from minecraft_realtime.game.bridge.actions import validate_action


class Driver:
    def __init__(self): self.events = []
    def key(self, *args): self.events.append(('key', *args))
    def button(self, *args): self.events.append(('button', *args))
    def motion(self, *args): self.events.append(('motion', *args))
    def flush(self): pass


def controls():
    now, events = [0.0], []
    driver = Driver()
    control = InputController(driver, clock=lambda: now[0], events=events.append)
    return control, driver, now, events


def test_key_down_persists_through_time_and_mouse_movement():
    control, driver, now, _ = controls()
    control.execute({'id': 'key_down', 'key': 'w'})
    control.execute({'id': 'mouse_move', 'dx': 12})
    now[0] = 3600
    control.expire()
    assert control.keys == {'w'}
    assert driver.events == [('key', 'w', True), ('motion', 12, 0, True)]
    control.execute({'id': 'key_up', 'key': 'w'})
    assert not control.keys


def test_timed_key_does_not_release_unrelated_key_or_mouse_button():
    control, driver, now, events = controls()
    control.execute({'id': 'key_down', 'key': 'w'})
    control.execute({'id': 'mouse_down', 'button': 'left'})
    control.execute({'id': 'key_press', 'key': 'space', 'duration_ms': 250})
    now[0] = .249
    control.expire()
    assert 'space' in control.keys
    now[0] = .251
    control.expire()
    assert control.keys == {'w'} and control.buttons == {'left'}
    assert events[-1]['type'] == 'automatic_release'
    assert events[-1]['data']['controls'] == [{'kind': 'key', 'name': 'space'}]


def test_old_timer_cannot_release_new_persistent_hold():
    control, driver, now, _ = controls()
    control.execute({'id': 'key_press', 'key': 'w', 'duration_ms': 100})
    now[0] = .05
    control.execute({'id': 'key_down', 'key': 'w'})
    now[0] = 10
    control.expire()
    assert control.keys == {'w'}
    assert ('key', 'w', False) not in driver.events


def test_click_moves_then_presses_and_expires_independently():
    control, driver, now, _ = controls()
    control.execute({'id': 'key_down', 'key': 'shift'})
    control.execute({'id': 'mouse_click', 'button': 'left', 'x': 200, 'y': 150})
    assert driver.events[-2:] == [('motion', 200, 150, False), ('button', 1, True)]
    now[0] = .06
    control.expire()
    assert control.keys == {'shift'} and not control.buttons
    assert driver.events[-1] == ('button', 1, False)


def test_click_ends_old_drag_before_moving():
    control, driver, now, _ = controls()
    control.execute({'id': 'mouse_down', 'button': 'left'})
    control.execute({'id': 'mouse_click', 'button': 'left', 'x': 20, 'y': 30})
    assert driver.events[-3:] == [('button', 1, False), ('motion', 20, 30, False), ('button', 1, True)]
    control.execute({'id': 'mouse_down', 'button': 'left'})
    now[0] = 2
    control.expire()
    assert control.buttons == {'left'}


def test_repeated_click_cancels_previous_release_deadline():
    control, driver, now, events = controls()
    control.execute({'id': 'mouse_click', 'button': 'left'})
    now[0] = .03
    control.execute({'id': 'mouse_click', 'button': 'left'})
    assert driver.events == [('button', 1, True), ('button', 1, False), ('button', 1, True)]
    now[0] = .06
    control.expire()
    assert control.buttons == {'left'}
    now[0] = .09
    control.expire()
    assert not control.buttons and events[-1]['type'] == 'automatic_release'


@pytest.mark.parametrize('action', [
    {'id': 'key_down', 'key': 'alt'}, {'id': 'key_up', 'key': 'F4'},
    {'id': 'key_press', 'key': 'w', 'duration_ms': True},
    {'id': 'key_press', 'key': 'w', 'duration_ms': 10001},
    {'id': 'mouse_move', 'x': 10}, {'id': 'mouse_move', 'x': 1, 'y': 2, 'dx': 0},
    {'id': 'mouse_click', 'button': 'left', 'x': 960, 'y': 0},
    {'id': 'mouse_down', 'button': 'left', 'keys': ['w']},
    {'id': 'mouse_scroll', 'amount': False}, {'id': 'release_all', 'extra': 1},
])
def test_invalid_action_does_not_affect_held_controls(action):
    control, driver, now, events = controls()
    control.execute({'id': 'key_down', 'key': 'w'})
    before = list(driver.events)
    with pytest.raises(ValueError): control.execute(action)
    assert driver.events == before and control.keys == {'w'} and len(events) == 1


def test_release_all_cancels_all_timers():
    control, driver, now, _ = controls()
    control.execute({'id': 'key_press', 'key': 'w'})
    control.execute({'id': 'mouse_click', 'button': 'left'})
    control.execute({'id': 'release_all'})
    assert not control.keys and not control.buttons and not control.deadlines
    before = list(driver.events)
    now[0] = 999
    control.expire()
    assert before == driver.events


def test_legacy_full_state_and_explicit_input_share_held_state():
    control, driver, now, _ = controls()
    action = SimpleNamespace(keys=['w'], buttons=['left'], x=None, y=None,
                             dx=0, dy=0, wheel=0, duration_ms=100)
    action.model_dump = lambda: {'keys': action.keys, 'duration_ms': 100}
    control.apply(action)
    control.execute({'id': 'mouse_move', 'dx': 5})
    control.execute({'id': 'key_down', 'key': 'w'})
    now[0] = .2
    control.expire()
    assert control.keys == {'w'} and not control.buttons


def test_confirmation_sequence_and_private_events_match():
    control, driver, now, events = controls()
    accepted = control.execute({'id': 'key_down', 'key': 'w'})
    assert accepted['accepted'] and accepted['action_id'] == events[-1]['seq']
    assert events[-1]['data']['held_keys'] == ['w']
