"""Print synthetic direct-request examples. This file never sends a request."""
import json
from sign_protocol import (
    MOBILELEARN_ORIGIN, SIGN_PATH, build_presign_url, build_presign_headers,
    build_ordinary_query, build_position_form, build_submit_headers, encode_form,
)

context = {
    'activeId': 'SYNTHETIC_ACTIVITY', 'courseId': 'SYNTHETIC_COURSE',
    'classId': 'SYNTHETIC_CLASS', 'uid': 'SYNTHETIC_UID', 'fid': 'SYNTHETIC_FID',
    'name': '合成测试用户', 'deviceCode': 'SYNTHETIC_DEVICE',
}
user_agent = 'OfflineProtocolExample/0.1'
presign = build_presign_url(context)
examples = {
    'scope': '合成示例；未发送请求。请求形状来自客户端，直连完整流程尚未实测。',
    'presign': {'method': 'GET', 'url': presign, 'headers': build_presign_headers(user_agent)},
    'ordinary': {'method': 'GET', 'url': MOBILELEARN_ORIGIN+SIGN_PATH,
                 'query': build_ordinary_query(context)},
    'position': {'method': 'POST', 'url': MOBILELEARN_ORIGIN+SIGN_PATH,
                 'headers': build_submit_headers(presign, user_agent),
                 'body': encode_form(build_position_form(context, {
                     'address': '合成示例地点', 'longitude': 0, 'latitude': 0,
                 }))},
}
if __name__ == '__main__':
    print(json.dumps(examples, ensure_ascii=False, indent=2))
