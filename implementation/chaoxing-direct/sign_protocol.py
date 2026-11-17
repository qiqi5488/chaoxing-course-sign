"""Pure reconstruction of observed sign-in request fields; performs no I/O.

Evidence: analysis/upstream/sign-protocol.json. These builders do not establish
upstream compatibility. Validation tokens, face credentials, photos and location
inputs must be supplied by the caller; none are generated here.
"""

import json
import math
import re
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

MOBILELEARN_ORIGIN = "https://mobilelearn.chaoxing.com"
PRESIGN_PATH = "/newsign/preSign"
SIGN_PATH = "/pptSign/stuSignajax"


def _string(value):
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _text(value):
    return _string(value) if value else ""


def encode_form(fields):
    """URLSearchParams-style form encoding; retain false, zero and empty text."""
    # urllib always preserves '~'; URLSearchParams encodes it as %7E.
    return urlencode([(str(k), _string(v)) for k, v in fields.items() if v is not None], safe="*").replace("~", "%7E")


def normalize_device_code(value):
    return re.sub(r"[?&]", "", _text(value).strip())


def get_cookie_value(cookie, name):
    for part in str(cookie or "").split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key.strip() == name:
            return value.strip()
    return ""


def build_presign_url(context, *, cross_class=False, rcode="", face_enc_aid=""):
    """Official target is explicit in the course client. QR extras are optional."""
    fields = {
        "courseId": _text(context.get("courseId")),
        "classId": _text(context.get("classId")),
        "activePrimaryId": _text(context.get("activeId")),
        "general": "1", "sys": "1", "ls": "1", "appType": "15",
        "uid": _text(context.get("uid")),
    }
    if cross_class and rcode:
        fields["rcode"] = str(rcode)
    fields["isTeacherViewOpen"] = "0"
    if cross_class and face_enc_aid:
        fields["faceEncAid"] = str(face_enc_aid)
    query = "&".join(k + "=" + quote(v, safe="~!*'()-._") for k, v in fields.items())
    return MOBILELEARN_ORIGIN + PRESIGN_PATH + "?" + query


def build_presign_headers(user_agent):
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh-Hans;q=0.9", "User-Agent": str(user_agent),
    }


def build_submit_headers(presign_url, user_agent):
    return {
        "Accept": "*/*", "X-Requested-With": "XMLHttpRequest",
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": MOBILELEARN_ORIGIN, "User-Agent": str(user_agent),
        "Referer": str(presign_url),
    }


def build_ordinary_query(context, *, validate=""):
    """Observed GET shape. fid is explicit input; do not copy the source uid/fid bug."""
    return {
        "activeId": _text(context.get("activeId")), "uid": _text(context.get("uid")),
        "clientip": "", "useragent": "", "latitude": "-1", "longitude": "-1",
        "appType": "15", "fid": _text(context.get("fid")), "name": "",
        "deviceCode": _text(context.get("deviceCode")), "validate": str(validate),
    }


def build_code_query(context, sign_code, *, validate=""):
    fields = build_ordinary_query(context, validate=validate)
    fields.pop("useragent")
    fields["signCode"] = str(sign_code)
    return fields


def _number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Coordinates and image dimensions must be finite")
    return int(number) if number.is_integer() else number


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def build_location_fields(location):
    """Observed metadata constants are not a device attestation or a location check."""
    address = _text(location.get("address"))
    longitude = _number(location["longitude"])
    latitude = _number(location["latitude"])
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise ValueError("Coordinates outside geographic bounds")
    return {
        "location": _json({"result": "1", "address": address, "longitude": longitude, "latitude": latitude}),
        "locationResult": _json({"result": "1", "address": address, "longitude": longitude,
                                 "mockData": {"probability": 0}, "latitude": latitude,
                                 "poiList": [], "altitude": 15}),
    }


def _add_face(fields, face_fields):
    face = face_fields or {}
    if face.get("currentFaceId"):
        fields["currentFaceId"] = str(face["currentFaceId"])
        fields["ifCFP"] = "0"
        if face.get("faceEnc"):
            fields["faceEnc"] = str(face["faceEnc"])


def build_position_form(context, location, *, face_fields=None, validate=""):
    payload = build_location_fields(location)
    fields = {
        "name": _text(context.get("name")), "address": _text(location.get("address")),
        "activeId": _text(context.get("activeId")), "courseId": _text(context.get("courseId")),
        "uid": _text(context.get("uid")), "clientip": "",
        "latitude": location["latitude"], "longitude": location["longitude"],
        "fid": _text(context.get("fid")), "appType": "15", "ifTiJiao": "1",
        "validate": str(validate), "deviceCode": normalize_device_code(context.get("deviceCode")),
        "vpProbability": "0", "vpStrategy": "", "locationResult": payload["locationResult"],
    }
    _add_face(fields, face_fields)
    return fields


def build_located_code_form(context, sign_code, location, *, face_fields=None, validate=None):
    fields = build_position_form(context, location, face_fields=face_fields)
    fields.pop("address")
    fields.pop("ifTiJiao")
    fields.pop("validate")
    fields["signCode"] = str(sign_code)
    fields["location"] = build_location_fields(location)["location"]
    if validate is not None:
        fields["validate"] = str(validate)
    return fields


def build_photo_payload(upload, file_meta, dimensions, *, converted_object_id=None):
    """Construct metadata only; does not upload or convert an image ID."""
    data = upload.get("data") or {}
    top_id = _text(upload.get("objectId")).strip()
    nested_id = _text(data.get("objectId")).strip()
    if top_id and nested_id and top_id != nested_id:
        raise ValueError("Conflicting upload object IDs")
    original_id = nested_id or top_id
    object_id = _text(converted_object_id or original_id).strip()
    resid = _text(data.get("residstr") or data.get("resid") or upload.get("resid")).strip()
    width, height = _number(dimensions.get("width", 0)), _number(dimensions.get("height", 0))
    size = _number(data.get("size") or upload.get("size") or file_meta.get("size") or 0)
    file_name = file_meta.get("name", "")
    suffix = data.get("suffix") or data.get("filetype") or (
        file_name.rsplit(".", 1)[-1] if "." in file_name else ""
    ) or str(file_meta.get("type", "")).split("/")[-1] or "jpg"
    suffix = str(suffix).removeprefix(".").lower()
    if suffix == "jpeg":
        suffix = "jpg"
    name = str(data.get("name") or upload.get("name") or file_name or object_id + "." + suffix)
    fallback_url = "https://p.cldisk.com/star4/" + object_id + "/origin.jpg" if object_id else ""
    image_url = fallback_url if converted_object_id and object_id != original_id else data.get("previewUrl") or fallback_url
    if not object_id or not resid or width <= 0 or height <= 0 or size <= 0 or not image_url:
        raise ValueError("Incomplete photo metadata")
    mirror = data.get("isMirror")
    photo = {"height": height, "isMirror": mirror is True or mirror == 1 or mirror == "true",
             "size": size, "width": width, "imageUrl": image_url, "objectid": object_id,
             "resid": resid, "name": name, "type": suffix}
    return {"objectId": object_id, "photoResult": _json({"result": 1,
            "protocol": "CLIENT_CHOOSE_IMAGE_RESULT", "camera": 5, "files": _json([photo])})}


def build_photo_form(context, photo_payload, *, validate=""):
    return {
        "activeId": _text(context.get("activeId")), "courseId": _text(context.get("courseId")),
        "uid": _text(context.get("uid")), "clientip": "", "useragent": "",
        "latitude": "-1", "longitude": "-1", "appType": "15", "fid": _text(context.get("fid")),
        "objectId": str(photo_payload["objectId"]), "name": _text(context.get("name")),
        "validate": str(validate), "deviceCode": normalize_device_code(context.get("deviceCode")),
        "photoResult": str(photo_payload["photoResult"]),
    }


def _extract_qr_parameter(text, names):
    if "http" in text:
        try:
            parsed = urlsplit(text)
            if parsed.scheme:
                query = parse_qs(parsed.query, keep_blank_values=True)
                for name in names:
                    if query.get(name, [""])[0]:
                        return query[name][0]
        except ValueError:
            pass
    for name in names:
        match = re.search(r"(?:^|[?&:])" + re.escape(name) + r"=([^&]*)", text)
        if match:
            raw = match[1]
            if re.search(r"%(?![0-9a-fA-F]{2})", raw):
                return raw
            try:
                return unquote(raw, errors="strict")
            except UnicodeDecodeError:
                return raw
    return ""


def parse_teacher_qr(text):
    text = str(text or "")
    return {"enc": _extract_qr_parameter(text, ["enc"]),
            "activityId": _extract_qr_parameter(text, ["id", "aid", "activeId"]),
            "code": _extract_qr_parameter(text, ["c", "Code"])}


def build_qr_form(context, enc, *, location=None, face_fields=None, challenge=None):
    location_fields = {"location": "", "locationResult": ""}
    if str(context.get("ifopenAddress", "0")) == "1" and location is not None:
        location_fields = build_location_fields(location)
    face = face_fields or {}
    fields = {"enc": str(enc), "name": _text(context.get("name")),
              "activeId": _text(context.get("activeId")), "uid": _text(context.get("uid")),
              "clientip": "", "location": location_fields["location"], "latitude": "-1",
              "longitude": "-1", "fid": _text(context.get("fid")), "appType": "15",
              "deviceCode": normalize_device_code(context.get("deviceCode")),
              "vpProbability": "", "vpStrategy": ""}
    if challenge and challenge.get("enc2") and challenge.get("validate"):
        fields.update(enc2=str(challenge["enc2"]), validate=str(challenge["validate"]))
    fields.update(currentFaceId=_text(face.get("currentFaceId")), ifCFP="0",
                  courseId=_text(context.get("courseId")))
    if face.get("faceEnc"):
        fields["faceEnc"] = str(face["faceEnc"])
    fields["faceEncAid"] = _text(context.get("faceEncAid"))
    fields["locationResult"] = location_fields["locationResult"]
    return fields


def extract_challenge(body, family):
    text = str(body or "").strip()
    if family == "course-post":
        return {"status": "requires_validation"} if text == "validate" else None
    if family != "qr":
        raise ValueError("family must be course-post or qr")
    if not text.startswith("validate_"):
        return None
    enc2 = text[len("validate_"):].strip()
    return {"status": "requires_validation", "enc2": enc2} if enc2 else {"status": "invalid_challenge"}


def classify_sign_response(body, *, http_status=200):
    """Conservative classification, intentionally stricter than the original UI.

    reported_success means a response claim, not a verified attendance record.
    No classification triggers network requests or automatic resubmission.
    """
    text = str(body or "").strip()
    result = {"status": "unknown", "rawBody": str(body or ""), "httpStatus": http_status}
    if not 200 <= http_status < 300:
        result["status"] = "http_error"
        return result
    if "同一设备不允许重复签到" in text:
        result["status"] = "device_conflict"
        return result
    challenge = extract_challenge(text, "qr") or extract_challenge(text, "course-post")
    if challenge:
        result.update(challenge)
        return result
    if text in {"success", "success2", "签到成功"}:
        result["status"] = "reported_success"
        return result
    if text in {"已签", "已签到", "已经签到"}:
        result["status"] = "already_signed"
        return result
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return result
    if isinstance(data, dict):
        message = data.get("errorMsg") or data.get("msg") or ""
        if not isinstance(message, str):
            return result
        if "同一设备不允许重复签到" in str(message):
            result["status"] = "device_conflict"
        elif message in {"已签", "已签到", "已经签到"}:
            result["status"] = "already_signed"
        elif str(data.get("result")) == "1" and (
            not message or message in {"success", "success2", "签到成功"}
        ):
            result["status"] = "reported_success"
        elif str(data.get("result")) == "0":
            result["status"] = "rejected"
    return result
