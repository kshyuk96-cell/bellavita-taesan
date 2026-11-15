"""Owner-pinned variable text regions around strict design comparison.

Masks allow arbitrary changed pixels only in the approved text rectangles.
They do not prove those pixels are letters or that supplied text/font metadata
was used by a renderer. No OCR, rendering, template substitution, or approval.
"""
import json
import hashlib
from io import BytesIO

from PIL import Image, ImageChops

from .design_compare import (DesignCompareError, _reference, _layout_from_bytes,
                             _pairs, _bad_constant, _image, compare_design)

MAX_POLICY_BYTES = 65536
MAX_TEXT_OBJECTS = 50
MAX_RECTANGLES = 50
MAX_MASK_FRACTION = 0.20
_POLICY_KEYS = {'schema_version', 'image_ref', 'layout_ref', 'allowed_text_object_ids', 'pixel_rectangles'}
_RECT_KEYS = {'object_id', 'x', 'y', 'width', 'height'}


class VariantPolicyError(DesignCompareError):
    pass


def _intersects(a, b):
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _policy(content, image_ref, layout_ref, layout):
    if type(content) is not bytes or not 1 <= len(content) <= MAX_POLICY_BYTES:
        raise VariantPolicyError('변경 영역 정책은 64KiB 이내 JSON이어야 해요.')
    try:
        value = json.loads(content.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_bad_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise VariantPolicyError('승인된 변경 영역 정책 JSON을 확인하지 못했어요.') from None
    if (not isinstance(value, dict) or set(value) != _POLICY_KEYS
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or _reference(value['image_ref']) != image_ref or _reference(value['layout_ref']) != layout_ref):
        raise VariantPolicyError('변경 영역 정책이 정확한 기준 이미지·레이아웃과 결합돼야 해요.')
    identities = value['allowed_text_object_ids']
    if (not isinstance(identities, list) or len(identities) > MAX_TEXT_OBJECTS
            or any(not isinstance(identity, str) for identity in identities)
            or len(set(identities)) != len(identities)):
        raise VariantPolicyError('중복 없는 동적 문구 객체를 50개 이내로 지정해 주세요.')
    objects = {item['id']: item for item in layout['objects']}
    if any(identity not in objects for identity in identities):
        raise VariantPolicyError('기준 레이아웃에 없는 동적 문구 객체를 허용할 수 없어요.')
    allowed = set(identities)
    fixed = [item for item in layout['objects'] if item['id'] not in allowed]
    width, height = layout['canvas']['width'], layout['canvas']['height']
    rectangles = value['pixel_rectangles']
    if not isinstance(rectangles, list) or len(rectangles) > MAX_RECTANGLES:
        raise VariantPolicyError('변경 픽셀 영역은 50개 이내여야 해요.')
    boxes, represented, area = [], set(), 0
    for rect in rectangles:
        if (not isinstance(rect, dict) or set(rect) != _RECT_KEYS
                or not isinstance(rect['object_id'], str) or rect['object_id'] not in allowed
                or any(type(rect[key]) is not int for key in ('x', 'y', 'width', 'height'))
                or rect['x'] < 0 or rect['y'] < 0 or rect['width'] <= 0 or rect['height'] <= 0
                or rect['x'] + rect['width'] > width or rect['y'] + rect['height'] > height):
            raise VariantPolicyError('동적 문구 객체 안의 양의 정수 픽셀 영역이 필요해요.')
        box = (rect['x'], rect['y'], rect['x'] + rect['width'], rect['y'] + rect['height'])
        item = objects[rect['object_id']]
        if (box[0] < item['x'] or box[1] < item['y']
                or box[2] > item['x'] + item['width'] or box[3] > item['y'] + item['height']):
            raise VariantPolicyError('문구 변경 영역이 해당 객체의 기준 영역을 벗어났어요.')
        if any(_intersects(box, other) for other in boxes):
            raise VariantPolicyError('변경 픽셀 영역을 겹쳐 지정하지 마세요.')
        for item in fixed:
            other = (item['x'], item['y'], item['x'] + item['width'], item['y'] + item['height'])
            if _intersects(box, other):
                raise VariantPolicyError('고정 객체와 겹치는 변경 픽셀 영역은 허용하지 않아요.')
        boxes.append(box)
        represented.add(rect['object_id'])
        area += rect['width'] * rect['height']
    if represented != allowed:
        raise VariantPolicyError('동적 문구 객체마다 제한된 픽셀 영역을 지정해야 해요.')
    if area * 5 > width * height:  # Exact 20% bound without floating-point rounding.
        raise VariantPolicyError('전체 또는 과도한 영역을 숨길 수 없어요. 변경 영역은 canvas의 20% 이내여야 해요.')
    return value, boxes, area


def _outside_changes(reference_bytes, candidate_bytes, boxes):
    reference, _, _, _ = _image(reference_bytes)
    try:
        candidate, _, _, _ = _image(candidate_bytes)
        try:
            with ImageChops.difference(reference, candidate) as difference:
                bands = difference.split()
                try:
                    mask = bands[0].copy()
                    try:
                        for band in bands[1:]:
                            combined = ImageChops.lighter(mask, band)
                            mask.close()
                            mask = combined
                        for box in boxes:
                            mask.paste(0, box)
                        outside = reference.width * reference.height - mask.histogram()[0]
                        bounds = mask.getbbox()
                    finally:
                        mask.close()
                finally:
                    for band in bands:
                        band.close()
            return outside, list(bounds) if bounds else None
        finally:
            candidate.close()
    finally:
        reference.close()


def _exif_signature(content):
    # Strict comparison already performed bounded decode. Hash metadata only;
    # never retain descriptions, customer text, GPS, or a full EXIF dump.
    try:
        with Image.open(BytesIO(content)) as image:
            metadata = image.info.get('exif')
            if metadata is None and image.format == 'TIFF':
                metadata = image.getexif().tobytes()
            if metadata is not None and (type(metadata) is not bytes or len(metadata) > 1024 * 1024):
                raise VariantPolicyError('EXIF 메타데이터 크기를 확인해 주세요.')
            return hashlib.sha256(metadata).hexdigest() if metadata is not None else None
    except (OSError, ValueError, SyntaxError):
        raise VariantPolicyError('EXIF 메타데이터를 안전하게 대조하지 못했어요.') from None


def compare_variant_design(registry, token, project, image_ref, layout_ref, policy_ref,
                           candidate_bytes, candidate_layout):
    """Permit only owner-approved text IDs and bounded pixel rectangles.

    Exact pixel comparisons outside those rectangles remain mandatory. Geometry,
    object order, font hashes, size, mode, ICC and EXIF orientation never vary.
    """
    image_ref, layout_ref, policy_ref = _reference(image_ref), _reference(layout_ref), _reference(policy_ref)
    policy_asset = registry.resolve(token, project, policy_ref['asset_id'], policy_ref['version'], policy_ref['sha256'])
    if policy_asset['kind'] != 'template':
        raise VariantPolicyError('변경 영역 정책은 승인된 template 에셋이어야 해요.')
    golden_layout = registry.resolve(token, project, layout_ref['asset_id'], layout_ref['version'], layout_ref['sha256'])
    if golden_layout['kind'] != 'template':
        raise VariantPolicyError('기준 레이아웃은 승인된 template 에셋이어야 해요.')
    layout = _layout_from_bytes(golden_layout['content'])
    policy, boxes, area = _policy(policy_asset['content'], image_ref, layout_ref, layout)
    strict = compare_design(registry, token, project, image_ref, layout_ref, candidate_bytes, candidate_layout)
    difference = strict['layout']
    allowed = set(policy['allowed_text_object_ids'])
    layout_allowed = (not difference['added_objects'] and not difference['removed_objects']
                      and not difference['object_order_changed'] and not difference['canvas_changed']
                      and all(item['id'] in allowed and item['fields'] == ['text']
                              for item in difference['changed_objects']))
    pixel = strict['pixel']
    golden_image = registry.resolve(token, project, image_ref['asset_id'], image_ref['version'], image_ref['sha256'])
    exif_unchanged = _exif_signature(golden_image['content']) == _exif_signature(candidate_bytes)
    metadata_allowed = (pixel['same_size'] and not pixel['mode_changed']
                        and not pixel['color_profile_metadata_changed'] and not pixel['orientation_metadata_changed']
                        and strict['candidate_canvas_matches_image'] and exif_unchanged)
    outside, bounds = None, None
    if layout_allowed and metadata_allowed:
        outside, bounds = _outside_changes(golden_image['content'], candidate_bytes, boxes)
    return {'status': 'compared', 'matches': layout_allowed and metadata_allowed and outside == 0,
            'strict_matches': strict['matches'], 'strict_comparison': strict,
            'policy_ref': policy_ref, 'policy_constraints_verified': True,
            'layout_changes_allowed': layout_allowed, 'metadata_unchanged': metadata_allowed,
            'exif_metadata_unchanged': exif_unchanged,
            'allowed_mask_pixels': area, 'max_mask_fraction': MAX_MASK_FRACTION,
            'outside_changed_pixels': outside, 'outside_difference_box': bounds,
            'semantic_ocr_verified': False, 'font_files_verified': False,
            'color_profile_rendering_verified': False, 'design_approval_granted': False,
            'scope': 'owner_pinned_text_metadata_and_pixel_regions_only'}
