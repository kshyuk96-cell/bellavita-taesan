"""Exact-reference pixel/layout comparison; never an OCR or design approval.

The company originals and rendering engine are not connected by this module.
Only already-approved AssetRegistry image/template references are accepted.
Layout schema: canvas {width,height}, objects [{id,text,x,y,width,height,font_sha256}].
Font hashes describe supplied metadata; this does not verify the renderer used
those font files. EXIF orientation differences are mismatches, never normalized
by rotating inputs. No image resizing, replacement, fetching, or output writing.
"""
from io import BytesIO
import hashlib
import json
import math
import re
import warnings

from PIL import Image, ImageChops, ImageStat, UnidentifiedImageError


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_LAYOUT_BYTES = 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_OBJECTS = 1000
_FORMATS = frozenset(('PNG', 'JPEG', 'WEBP', 'TIFF'))
_MODES = frozenset(('RGB', 'RGBA', 'L', 'LA', 'P'))
_OBJECT_KEYS = frozenset(('id', 'text', 'x', 'y', 'width', 'height', 'font_sha256'))
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')
_HASH = re.compile(r'[a-f0-9]{64}')


class DesignCompareError(ValueError):
    pass


def _reference(value):
    if (not isinstance(value, dict) or set(value) != {'asset_id', 'version', 'sha256'}
            or any(not isinstance(value.get(key), str) or not _ID.fullmatch(value[key])
                   for key in ('asset_id', 'version'))
            or not isinstance(value.get('sha256'), str) or not _HASH.fullmatch(value['sha256'])):
        raise DesignCompareError('승인된 에셋 ID·버전·SHA256을 정확히 지정해 주세요.')
    return dict(value)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise DesignCompareError('레이아웃 JSON에 중복 키가 있어요.')
        result[key] = value
    return result


def _bad_constant(_):
    raise DesignCompareError('레이아웃 숫자는 유한수여야 해요.')


def _layout_from_bytes(content):
    if type(content) is not bytes or not 1 <= len(content) <= MAX_LAYOUT_BYTES:
        raise DesignCompareError('기준 레이아웃 JSON의 크기를 확인해 주세요.')
    try:
        value = json.loads(content.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_bad_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise DesignCompareError('기준 레이아웃 JSON을 안전하게 읽지 못했어요.') from None
    return _layout(value)


def _layout(value):
    if not isinstance(value, dict) or set(value) != {'canvas', 'objects'}:
        raise DesignCompareError('레이아웃의 canvas와 objects가 필요해요.')
    canvas = value['canvas']
    if (not isinstance(canvas, dict) or set(canvas) != {'width', 'height'}
            or any(type(canvas[key]) is not int or canvas[key] <= 0 for key in canvas)
            or canvas['width'] * canvas['height'] > MAX_PIXELS):
        raise DesignCompareError('레이아웃 크기는 양의 정수와 16M 픽셀 이내여야 해요.')
    objects = value['objects']
    if not isinstance(objects, list) or len(objects) > MAX_OBJECTS:
        raise DesignCompareError('레이아웃 객체는 1000개 이내여야 해요.')
    seen, normalized = set(), []
    for item in objects:
        if (not isinstance(item, dict) or set(item) != _OBJECT_KEYS
                or not isinstance(item['id'], str) or not _ID.fullmatch(item['id'])
                or item['id'] in seen or not isinstance(item['text'], str) or len(item['text']) > 10000
                or not isinstance(item['font_sha256'], str) or not _HASH.fullmatch(item['font_sha256'])):
            raise DesignCompareError('레이아웃 객체 ID·문구·폰트 해시를 확인해 주세요.')
        for field in ('x', 'y', 'width', 'height'):
            number = item[field]
            if (type(number) not in (int, float) or abs(number) > 1_000_000 or not math.isfinite(number)
                    or (field in ('width', 'height') and number < 0)):
                raise DesignCompareError('객체 좌표와 크기는 범위 안의 유한수여야 해요.')
        seen.add(item['id'])
        normalized.append(dict(item))
    result = {'canvas': dict(canvas), 'objects': normalized}
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, UnicodeError):
        raise DesignCompareError('레이아웃의 문자·숫자 형식을 확인해 주세요.') from None
    if len(encoded) > MAX_LAYOUT_BYTES:
        raise DesignCompareError('레이아웃 JSON은 1MiB 이내여야 해요.')
    return result


def _image(content):
    if type(content) is not bytes or not 1 <= len(content) <= MAX_IMAGE_BYTES:
        raise DesignCompareError('이미지는 1바이트~8MiB bytes여야 해요.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                if (image.format not in _FORMATS or image.mode not in _MODES
                        or image.width <= 0 or image.height <= 0
                        or image.width * image.height > MAX_PIXELS
                        or getattr(image, 'n_frames', 1) != 1):
                    raise DesignCompareError('지원하는 단일 이미지와 16M 픽셀 이내 크기가 필요해요.')
                image.load()
                profile = image.info.get('icc_profile')
                if profile is not None and (type(profile) is not bytes or len(profile) > MAX_LAYOUT_BYTES):
                    raise DesignCompareError('색상 프로필 메타데이터의 크기를 확인해 주세요.')
                profile_hash = hashlib.sha256(profile).hexdigest() if profile else None
                exif_data = image.info.get('exif')
                if exif_data is not None and (type(exif_data) is not bytes or len(exif_data) > MAX_LAYOUT_BYTES):
                    raise DesignCompareError('이미지 방향 메타데이터의 크기를 확인해 주세요.')
                orientation = image.getexif().get(274, 1)
                if type(orientation) is not int or not 1 <= orientation <= 8:
                    raise DesignCompareError('이미지 EXIF 방향 메타데이터가 올바르지 않아요.')
                return image.convert('RGBA'), image.mode, profile_hash, orientation
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning,
            Image.DecompressionBombError, SyntaxError):
        raise DesignCompareError('이미지 포맷·크기·압축 데이터를 안전하게 확인하지 못했어요.') from None


def _layout_difference(reference, candidate):
    reference_objects = {item['id']: item for item in reference['objects']}
    candidate_objects = {item['id']: item for item in candidate['objects']}
    added = sorted(candidate_objects.keys() - reference_objects.keys())
    removed = sorted(reference_objects.keys() - candidate_objects.keys())
    changed = []
    for identity in sorted(reference_objects.keys() & candidate_objects.keys()):
        fields = [name for name in ('text', 'x', 'y', 'width', 'height', 'font_sha256')
                  if reference_objects[identity][name] != candidate_objects[identity][name]]
        if fields:
            changed.append({'id': identity, 'fields': fields})
    order_changed = ([item['id'] for item in reference['objects']] !=
                     [item['id'] for item in candidate['objects']])
    canvas_changed = reference['canvas'] != candidate['canvas']
    return {'matches': not (added or removed or changed or order_changed or canvas_changed),
            'added_objects': added, 'removed_objects': removed, 'changed_objects': changed,
            'object_order_changed': order_changed, 'canvas_changed': canvas_changed}


def _pixel_difference(reference, candidate, reference_mode, candidate_mode, reference_profile, candidate_profile,
                      reference_orientation, candidate_orientation):
    same_size = reference.size == candidate.size
    result = {'comparable': same_size, 'same_size': same_size,
              'reference_size': list(reference.size), 'candidate_size': list(candidate.size),
              'reference_mode': reference_mode, 'candidate_mode': candidate_mode,
              'mode_changed': reference_mode != candidate_mode,
              'reference_color_profile_sha256': reference_profile, 'candidate_color_profile_sha256': candidate_profile,
              'color_profile_metadata_changed': reference_profile != candidate_profile,
              'reference_orientation': reference_orientation, 'candidate_orientation': candidate_orientation,
              'orientation_metadata_changed': reference_orientation != candidate_orientation,
              'changed_pixels': None, 'changed_pixel_ratio': None,
              'mean_abs_channel_delta': None, 'max_channel_delta': None, 'difference_box': None,
              'matches': False}
    if not same_size:
        return result  # Never resize either input to manufacture a match.
    with ImageChops.difference(reference, candidate) as difference:
        bands = difference.split()
        try:
            mask = bands[0].copy()
            try:
                for band in bands[1:]:
                    next_mask = ImageChops.lighter(mask, band)
                    mask.close()
                    mask = next_mask
                changed = reference.width * reference.height - mask.histogram()[0]
                box = mask.getbbox()
            finally:
                mask.close()
            mean = sum(ImageStat.Stat(difference).mean) / 4
            maximum = max(high for _, high in difference.getextrema())
        finally:
            for band in bands:
                band.close()
    result.update(changed_pixels=changed, changed_pixel_ratio=changed / (reference.width * reference.height),
                  mean_abs_channel_delta=round(mean, 6), max_channel_delta=maximum,
                  difference_box=list(box) if box else None,
                  matches=changed == 0 and reference_mode == candidate_mode and reference_profile == candidate_profile
                  and reference_orientation == candidate_orientation)
    return result


def compare_design(registry, token, project, image_ref, layout_ref, candidate_bytes, candidate_layout):
    """Compare actual pixels and supplied text/geometry/font metadata exactly.

    The return value deliberately omits text contents and does not grant approval.
    Invalid/draft/missing references fail; no alternative asset is ever selected.
    """
    image_ref, layout_ref = _reference(image_ref), _reference(layout_ref)
    reference_image = registry.resolve(token, project, image_ref['asset_id'], image_ref['version'], image_ref['sha256'])
    reference_layout = registry.resolve(token, project, layout_ref['asset_id'], layout_ref['version'], layout_ref['sha256'])
    if reference_image['kind'] != 'image' or reference_layout['kind'] != 'template':
        raise DesignCompareError('이미지 기준은 image, 레이아웃 기준은 template 종류여야 해요.')
    golden_layout = _layout_from_bytes(reference_layout['content'])
    candidate_layout = _layout(candidate_layout)
    golden, golden_mode, golden_profile, golden_orientation = _image(reference_image['content'])
    try:
        if (golden_layout['canvas']['width'], golden_layout['canvas']['height']) != golden.size:
            raise DesignCompareError('승인된 기준 이미지와 기준 레이아웃 크기가 달라요.')
        candidate, candidate_mode, candidate_profile, candidate_orientation = _image(candidate_bytes)
        try:
            pixel = _pixel_difference(golden, candidate, golden_mode, candidate_mode, golden_profile, candidate_profile,
                                      golden_orientation, candidate_orientation)
            layout = _layout_difference(golden_layout, candidate_layout)
            canvas_consistent = (candidate_layout['canvas']['width'], candidate_layout['canvas']['height']) == candidate.size
        finally:
            candidate.close()
    finally:
        golden.close()
    return {'status': 'compared', 'matches': pixel['matches'] and layout['matches'] and canvas_consistent,
            'pixel': pixel, 'layout': layout, 'candidate_canvas_matches_image': canvas_consistent,
            'reference': {'image': image_ref, 'layout': layout_ref},
            'semantic_ocr_verified': False, 'font_files_verified': False, 'color_profile_rendering_verified': False,
            'design_approval_granted': False, 'scope': 'pixel_and_supplied_layout_metadata_only'}
