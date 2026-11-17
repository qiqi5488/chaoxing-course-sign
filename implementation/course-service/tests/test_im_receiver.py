"""Official-transport boundary tests; no real credentials or network."""
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from im_receiver import RealtimeReceivers, notice_candidate, socket_url
from test_course_notifications import course_notice, group_notice


def incoming(attachment=None, **changes):
    return {'type': 'notice', 'id': 'message-123', 'to': '900', 'chatType': 'groupChat',
            'attachment': attachment or course_notice(), **changes}


def test_candidates_preserve_course_pair_and_do_not_keep_raw_message():
    result = notice_candidate(incoming())
    assert (result['courseId'], result['classId'], result['activeId']) == ('700', '800', '300')
    assert result['activityScope'] == 'course' and result['status'] == 'unknown'
    assert 'attachment' not in result and 'url' not in result and 'from' not in result
    assert len(result['noticeKey']) == 64
    assert result == notice_candidate(incoming())
    assert result['noticeKey'] != notice_candidate(incoming(id='message-456'))['noticeKey']


@pytest.mark.parametrize('change', [{'chatType': 'singleChat'}, {'chatType': 'chatRoom'},
    {'to': '900/other'}, {'to': ''}, {'id': ''}, {'id': 'bad\nvalue'}, {'id': 'x' * 129},
    {'type': 'other'}, {'attachment': '{"attachmentType":15,"attachmentType":15}'},
    {'attachment': {'attachmentType': 18}}, {'attachment': course_notice(aid='999')}])
def test_invalid_or_non_attendance_messages_are_ignored(change):
    assert notice_candidate(incoming(**change)) is None


def test_group_candidate_must_match_received_conversation():
    result = notice_candidate(incoming(group_notice()))
    assert result['chatId'] == '900' and result['activityScope'] == 'group'
    assert notice_candidate(incoming(group_notice(group='901'))) is None


def discovery(hosts, status=200):
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value = SimpleNamespace(status_code=status, content=b'{}',
        json=lambda: {'msync-wx': {'hosts': hosts}})
    return session


def test_discovery_uses_anonymous_fixed_origin_and_only_official_tls_host():
    session = discovery([{'domain': 'im-api-wechat-vip6.easemob.com', 'protocol': 'https', 'port': '443'}])
    with patch('im_receiver.requests.Session', return_value=session):
        assert socket_url() == 'wss://im-api-wechat-vip6.easemob.com/websocket'
    args, kwargs = session.get.call_args
    assert args == ('https://rs.easemob.com/easemob/server.json',)
    assert kwargs['allow_redirects'] is False and session.trust_env is False
    assert 'headers' not in kwargs and 'cookies' not in kwargs


@pytest.mark.parametrize('host', [
    {'domain': 'evil.example', 'protocol': 'https', 'port': '443'},
    {'domain': 'easemob.com.evil.example', 'protocol': 'https', 'port': '443'},
    {'domain': 'im.easemob.com', 'protocol': 'http', 'port': '443'},
    {'domain': 'im.easemob.com', 'protocol': 'https', 'port': '8080'},
    {'domain': '127.0.0.1', 'protocol': 'https', 'port': '443'},
    {'domain': 'im.easemob.com/path', 'protocol': 'https', 'port': '443'},
])
def test_discovery_never_sends_token_to_untrusted_routes(host):
    with patch('im_receiver.requests.Session', return_value=discovery([host])):
        with pytest.raises(ValueError):
            socket_url()


class DummyListener:
    def __init__(self, spec, gateway, callback):
        self.spec, self.closed = spec, False
        self.thread = MagicMock()

    def close(self):
        self.closed = True


def spec(key=('owner', 'account'), stamp=('1001', 'login', 'grant')):
    return {'key': key, 'stamp': stamp, 'identity': {'uid': '1001'}, 'cookies': ['cookie-1']}


def test_settings_and_cookie_updates_keep_connection_but_relogin_and_removal_close_it():
    receiver = RealtimeReceivers(None, MagicMock(), DummyListener)
    receiver.sync([spec()])
    first = receiver.listeners[('owner', 'account')]
    changed = spec()
    changed.update(cookies=['cookie-2'], version='new-config-version')
    receiver.sync([changed])
    assert receiver.listeners[('owner', 'account')] is first
    assert first.spec['cookies'] == ['cookie-2']
    receiver.sync([spec(stamp=('1001', 'new-login', 'grant'))])
    assert first.closed
    second = receiver.listeners[('owner', 'account')]
    receiver.sync([])
    assert second.closed and receiver.listeners == {}


def test_one_identity_gets_one_listener_and_device_block_does_not_restart():
    callback = MagicMock()
    receiver = RealtimeReceivers(None, callback, DummyListener)
    receiver.sync([spec(key=('other', 'account-2'))])
    previous = receiver.listeners[('other', 'account-2')]
    receiver.sync([spec(), spec(key=('other', 'account-2'))])
    assert len(receiver.listeners) == 1
    assert previous.closed
    assert callback.call_args.args[1]['status'] == 'blocked'
    receiver.close()
    blocked = spec()
    blocked['blocked'] = True
    receiver.sync([blocked])
    assert not receiver.listeners
