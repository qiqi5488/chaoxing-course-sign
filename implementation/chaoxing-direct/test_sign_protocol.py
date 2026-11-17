import json
import unittest
from urllib.parse import parse_qs, urlsplit

import sign_protocol as protocol


class SignProtocolTests(unittest.TestCase):
    def setUp(self):
        self.context = {"activeId": "activity-demo", "courseId": "course-demo", "classId": "class-demo",
                        "uid": "user-demo", "fid": "institution-demo", "name": "测试用户", "deviceCode": " ?dev&1? "}
        self.location = {"address": "测试地点", "longitude": "116.25", "latitude": "39.5"}

    def test_form_encoding_retains_zero_false_empty_and_encodes_unicode(self):
        self.assertEqual(protocol.encode_form({"name": "张 三&", "zero": 0, "empty": "", "flag": False, "drop": None}),
                         "name=%E5%BC%A0+%E4%B8%89%26&zero=0&empty=&flag=false")
        self.assertEqual(protocol.encode_form({"value": "~!*'()"}), "value=%7E%21*%27%28%29")
        # Golden value independently checked with Node's built-in URLSearchParams.
        self.assertEqual(protocol.encode_form({"value": "a b+c~*!'\"中文&"}),
                         "value=a+b%2Bc%7E*%21%27%22%E4%B8%AD%E6%96%87%26")

    def test_presign_official_target_and_cross_class_extras(self):
        url = protocol.build_presign_url({"courseId": "c/1", "classId": "k 1", "activeId": "a&1", "uid": "u1"})
        self.assertEqual(url, "https://mobilelearn.chaoxing.com/newsign/preSign?courseId=c%2F1&classId=k%201&activePrimaryId=a%261&general=1&sys=1&ls=1&appType=15&uid=u1&isTeacherViewOpen=0")
        extra = protocol.build_presign_url(self.context, cross_class=True, rcode="SIGNIN:aid=demo&enc=one+two", face_enc_aid="old-demo")
        self.assertEqual(parse_qs(urlsplit(extra).query)["rcode"], ["SIGNIN:aid=demo&enc=one+two"])
        self.assertEqual(parse_qs(urlsplit(extra).query)["faceEncAid"], ["old-demo"])

    def test_headers_have_no_proxy_or_forged_tokens(self):
        headers = protocol.build_submit_headers(protocol.build_presign_url(self.context), "test-client")
        self.assertEqual(headers["Origin"], "https://mobilelearn.chaoxing.com")
        self.assertEqual(headers["User-Agent"], "test-client")
        self.assertFalse(any("proxy" in key.lower() for key in headers))

    def test_get_shapes_keep_fid_distinct_from_uid(self):
        ordinary = protocol.build_ordinary_query(self.context)
        code = protocol.build_code_query(self.context, "1236")
        self.assertEqual(ordinary["fid"], "institution-demo")
        self.assertNotEqual(ordinary["fid"], ordinary["uid"])
        self.assertIn("useragent", ordinary)
        self.assertNotIn("signCode", ordinary)
        self.assertNotIn("useragent", code)
        self.assertEqual(code["signCode"], "1236")

    def test_position_and_located_code_have_different_initial_fields(self):
        position = protocol.build_position_form(self.context, self.location)
        code = protocol.build_located_code_form(self.context, "1236", self.location)
        self.assertEqual(position["deviceCode"], "dev1")
        self.assertEqual(position["ifTiJiao"], "1")
        self.assertEqual(position["validate"], "")
        self.assertNotIn("validate", code)
        self.assertNotIn("ifTiJiao", code)
        self.assertEqual(json.loads(code["location"])["longitude"], 116.25)
        self.assertEqual(json.loads(position["locationResult"])["latitude"], 39.5)
        retry = protocol.build_located_code_form(self.context, "1236", self.location, validate="provided-token")
        self.assertEqual(retry["validate"], "provided-token")

    def test_face_credentials_are_only_copied_from_inputs(self):
        no_face = protocol.build_position_form(self.context, self.location)
        self.assertNotIn("faceEnc", no_face)
        self.assertNotIn("currentFaceId", no_face)
        face = protocol.build_position_form(self.context, self.location, face_fields={"currentFaceId": "face-demo", "faceEnc": "provided-enc"})
        self.assertEqual(face["faceEnc"], "provided-enc")
        self.assertEqual(face["ifCFP"], "0")

    def test_invalid_coordinate_fails_before_request(self):
        for longitude in ["nan", "inf", 181]:
            with self.subTest(longitude=longitude), self.assertRaises(ValueError):
                protocol.build_location_fields({"longitude": longitude, "latitude": 0})

    def test_photo_has_two_json_layers_and_consistent_id(self):
        upload = {"data": {"objectId": "old-demo", "residstr": "res-demo", "size": 1234, "suffix": "JPEG", "name": "demo.jpg"}}
        payload = protocol.build_photo_payload(upload, {"name": "demo.jpg"}, {"width": 80, "height": 60}, converted_object_id="new-demo")
        outer = json.loads(payload["photoResult"])
        self.assertIsInstance(outer["files"], str)
        photo = json.loads(outer["files"])[0]
        self.assertEqual(photo["objectid"], payload["objectId"])
        self.assertEqual(photo["type"], "jpg")
        self.assertEqual(photo["imageUrl"], "https://p.cldisk.com/star4/new-demo/origin.jpg")
        form = protocol.build_photo_form(self.context, payload)
        self.assertEqual(parse_qs(protocol.encode_form(form))["photoResult"], [payload["photoResult"]])
        with self.assertRaises(ValueError):
            protocol.build_photo_payload({"objectId": "a", "data": {"objectId": "b"}}, {}, {})
        with self.assertRaises(ValueError):
            protocol.build_photo_payload({"objectId": "only-id"}, {}, {})

    def test_qr_parses_real_scanner_formats_and_plus_semantics(self):
        parsed = protocol.parse_teacher_qr("SIGNIN:aid=900&source=15&Code=ABC&enc=a%2Bb")
        self.assertEqual(parsed, {"enc": "a+b", "activityId": "900", "code": "ABC"})
        self.assertEqual(protocol.parse_teacher_qr("SIGNIN:aid=900&enc=a+b")["enc"], "a+b")
        self.assertEqual(protocol.parse_teacher_qr("https://example.invalid/?id=900&enc=a+b")["enc"], "a b")
        self.assertEqual(protocol.parse_teacher_qr("SIGNIN:enc=a%20%ZZ")["enc"], "a%20%ZZ")

    def test_qr_retry_requires_both_external_values_and_no_vip_fields(self):
        context = dict(self.context, ifopenAddress=1, vipKey="not-upstream", signActionId="not-upstream")
        initial = protocol.build_qr_form(context, "scanned-enc", location=self.location)
        self.assertNotIn("enc2", initial)
        self.assertNotIn("validate", initial)
        self.assertNotIn("vipKey", initial)
        self.assertNotIn("signActionId", initial)
        self.assertEqual(initial["enc"], "scanned-enc")
        self.assertEqual(initial["latitude"], "-1")
        self.assertEqual(json.loads(initial["location"])["latitude"], 39.5)
        partial = protocol.build_qr_form(context, "scanned-enc", challenge={"enc2": "challenge"})
        self.assertNotIn("enc2", partial)
        retry = protocol.build_qr_form(context, "scanned-enc", challenge={"enc2": "challenge", "validate": "provided-token"})
        self.assertEqual(retry["validate"], "provided-token")

    def test_challenges_are_branch_specific(self):
        self.assertEqual(protocol.extract_challenge(" validate ", "course-post"), {"status": "requires_validation"})
        self.assertIsNone(protocol.extract_challenge("validate", "qr"))
        self.assertIsNone(protocol.extract_challenge("validate_code", "course-post"))
        self.assertEqual(protocol.extract_challenge(" validate_demo-enc2 ", "qr"), {"status": "requires_validation", "enc2": "demo-enc2"})
        self.assertEqual(protocol.extract_challenge("validate_", "qr"), {"status": "invalid_challenge"})

    def test_unknown_and_device_error_never_report_success(self):
        cases = {"not success": "unknown", "successfully maybe": "unknown", "同一设备不允许重复签到": "device_conflict",
                 "success": "reported_success", "success2": "reported_success", "已签到": "already_signed",
                 '{"result":1}': "reported_success", '{"result":0}': "rejected", "请求超时，结果未知": "unknown"}
        for body, expected in cases.items():
            with self.subTest(body=body):
                result = protocol.classify_sign_response(body)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["rawBody"], body)
        self.assertEqual(protocol.classify_sign_response("success", http_status=500)["status"], "http_error")
        self.assertEqual(protocol.classify_sign_response('{"result":1,"msg":"签到失败"}')["status"], "unknown")
        self.assertEqual(protocol.classify_sign_response('{"result":1,"msg":{"error":"failed"}}')["status"], "unknown")
        escaped = json.dumps({"result": 1, "errorMsg": "同一设备不允许重复签到"}, ensure_ascii=True)
        self.assertEqual(protocol.classify_sign_response(escaped)["status"], "device_conflict")

    def test_cookie_value_keeps_equals_inside_value(self):
        self.assertEqual(protocol.get_cookie_value("uid=7; token=a=b=c; fid=9", "token"), "a=b=c")


if __name__ == "__main__":
    unittest.main()
