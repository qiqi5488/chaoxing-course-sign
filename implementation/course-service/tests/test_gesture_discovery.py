"""Unknown gesture flags must not consume the monitor's one attempt."""
import pytest

from activity_protocol import parse_detail
from test_course_monitor import monitor, enable, run, COURSE, ACTIVITY
from test_activity_protocol import DETAIL
from upstream import UpstreamError


def gesture_detail(**changes):
    return {'data': {**DETAIL['data'], 'activeId': ACTIVITY['activeId'],
        'courseId': COURSE['courseId'], 'classId': COURSE['classId'], 'otherId': 3, **changes}}


@pytest.mark.parametrize('field', ['ifphoto', 'ifopenAddress', 'openCheckFaceFlag'])
@pytest.mark.parametrize('value', [2, -1, 0.5])
def test_non_binary_gesture_rule_is_unknown_at_discovery(field, value):
    activity = parse_detail(gesture_detail(**{field: value}), ACTIVITY, COURSE)
    assert activity['kind'] == 'gesture' and activity['status'] == 'active'
    assert 'unknown' in activity['requirements']
    assert activity['autoEligible'] is False


@pytest.mark.parametrize('field', ['ifphoto', 'ifopenAddress', 'openCheckFaceFlag'])
def test_unknown_gesture_rule_can_be_rediscovered_without_a_blocking_job(monitor, field):
    activity = parse_detail(gesture_detail(**{field: 2}), ACTIVITY, COURSE)
    monitor.gateway.activities = [activity]
    enable(monitor)
    task = run(monitor)['tasks'][0]
    assert task['status'] == 'needs_input' and not task.get('requestId')
    assert monitor.gateway.sign_calls == []
    with pytest.raises(UpstreamError):
        monitor.manager.sign_now(monitor.token, task['id'])
    assert monitor.vault.prior_job(monitor.token, monitor.account_id, activity['activeId']) is None
    monitor.gateway.activities = [parse_detail(gesture_detail(), ACTIVITY, COURSE)]
    updated = run(monitor)['tasks'][0]
    assert updated['status'] == 'reported_success'
    assert len(monitor.gateway.sign_calls) == 1
