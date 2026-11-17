"""Validate the observed course photo metadata; never invent or fetch a photo."""
import json
import math
import re


def validate_photo_payload(payload):
    try:
        object_id, raw = payload['objectId'], payload['photoResult']
        if (not isinstance(object_id, str) or not re.fullmatch(r'[a-fA-F0-9]{32}', object_id)
                or not isinstance(raw, str) or len(raw) > 16000):
            raise ValueError()
        result = json.loads(raw)
        if (not isinstance(result, dict) or type(result.get('result')) is not int
                or result['result'] != 1 or result.get('protocol') != 'CLIENT_CHOOSE_IMAGE_RESULT'
                or type(result.get('camera')) is not int or result['camera'] != 5
                or not isinstance(result.get('files'), str)):
            raise ValueError()
        files = json.loads(result['files'])
        if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
            raise ValueError()
        photo = files[0]
        if photo.get('objectid') != object_id:
            raise ValueError()
        if any(type(photo.get(key)) not in (int, float) or not math.isfinite(photo[key]) or photo[key] <= 0
               for key in ('width', 'height', 'size')):
            raise ValueError()
        if any(not isinstance(photo.get(key), str) or not photo[key].strip()
               for key in ('resid', 'imageUrl', 'name', 'type')):
            raise ValueError()
        if type(photo.get('isMirror')) is not bool:
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('课程拍照需要匹配 objectId 的完整本次照片元数据。') from None
