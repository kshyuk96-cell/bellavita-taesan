import copy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from manager_bridge.asset_registry import AssetRegistry
from manager_bridge.design_compare import compare_design, DesignCompareError, MAX_IMAGE_BYTES
from manager_bridge.store import Store, Conflict, NotFound


def image_bytes(size=(32, 16), color='white', mode='RGB', edit=None):
    with Image.new(mode, size, color) as image:
        if edit:
            edit(image)
        output = BytesIO()
        image.save(output, format='PNG')
        return output.getvalue()


def layout(size=(32, 16)):
    return {'canvas': {'width': size[0], 'height': size[1]}, 'objects': [
        {'id': 'sender', 'text': '가상 보내는 분', 'x': 1, 'y': 2, 'width': 20, 'height': 10, 'font_sha256': 'a' * 64},
        {'id': 'event', 'text': '가상 행사', 'x': 2, 'y': 4, 'width': 24, 'height': 8, 'font_sha256': 'b' * 64}]}


class DesignCompareTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(root / 'state.db')
        self.owner = self.store.add_token('owner', 'owner', ['*'])
        self.store.register_project(self.owner, 'bellavita', 'fixture/design')
        self.manager = self.store.add_token('manager', 'manager', ['bellavita'])
        self.worker = self.store.add_token('worker', 'worker', ['bellavita'])
        self.registry = AssetRegistry(self.store, root / 'assets')
        self.golden = image_bytes()
        self.layout = layout()
        self.image_ref = self.register('golden', self.golden, 'image')
        self.layout_ref = self.register('layout', json.dumps(self.layout, ensure_ascii=False).encode(), 'template')

    def register(self, name, content, kind, approve=True):
        asset = self.registry.register_draft(self.manager, 'bellavita', name, 'v1', content, kind)
        if approve:
            self.registry.approve(self.owner, 'bellavita', name, 'v1', asset['sha256'])
        return {'asset_id': name, 'version': 'v1', 'sha256': asset['sha256']}

    def compare(self, **changes):
        args = dict(registry=self.registry, token=self.worker, project='bellavita', image_ref=self.image_ref,
                    layout_ref=self.layout_ref, candidate_bytes=self.golden, candidate_layout=self.layout)
        args.update(changes)
        return compare_design(**args)

    def test_exact_approved_inputs_match_without_claiming_ocr_font_execution_or_approval(self):
        result = self.compare()
        self.assertTrue(result['matches'])
        self.assertEqual(result['pixel']['changed_pixels'], 0)
        self.assertTrue(result['layout']['matches'])
        self.assertFalse(result['semantic_ocr_verified'])
        self.assertFalse(result['font_files_verified'])
        self.assertFalse(result['design_approval_granted'])
        self.assertEqual(result['reference']['image'], self.image_ref)
        self.assertNotIn('가상 보내는 분', json.dumps(result, ensure_ascii=False))

    def test_single_color_pixel_difference_is_measured_with_location(self):
        changed = image_bytes(edit=lambda image: image.putpixel((5, 7), (255, 0, 0)))
        result = self.compare(candidate_bytes=changed)
        self.assertFalse(result['matches'])
        self.assertEqual(result['pixel']['changed_pixels'], 1)
        self.assertEqual(result['pixel']['difference_box'], [5, 7, 6, 8])
        self.assertGreater(result['pixel']['mean_abs_channel_delta'], 0)
        self.assertEqual(result['pixel']['max_channel_delta'], 255)
        self.assertTrue(result['layout']['matches'])

    def test_different_size_is_not_resized_or_reported_comparable(self):
        result = self.compare(candidate_bytes=image_bytes(size=(16, 16)), candidate_layout=layout(size=(16, 16)))
        self.assertFalse(result['matches'])
        self.assertFalse(result['pixel']['comparable'])
        self.assertIsNone(result['pixel']['changed_pixels'])
        self.assertTrue(result['layout']['canvas_changed'])

    def test_text_coordinate_and_font_hash_changes_are_reported_by_object_without_raw_text(self):
        candidate = copy.deepcopy(self.layout)
        candidate['objects'][0].update(text='변경된 비공개 문구', x=3, font_sha256='c' * 64)
        result = self.compare(candidate_layout=candidate)
        self.assertTrue(result['pixel']['matches'])
        self.assertFalse(result['layout']['matches'])
        self.assertEqual(result['layout']['changed_objects'],
                         [{'id': 'sender', 'fields': ['text', 'x', 'font_sha256']}])
        self.assertNotIn('변경된 비공개 문구', json.dumps(result, ensure_ascii=False))

    def test_object_removal_addition_and_paint_order_are_detected(self):
        candidate = copy.deepcopy(self.layout)
        candidate['objects'][0]['id'] = 'replacement'
        result = self.compare(candidate_layout=candidate)
        self.assertEqual(result['layout']['added_objects'], ['replacement'])
        self.assertEqual(result['layout']['removed_objects'], ['sender'])
        reversed_layout = copy.deepcopy(self.layout)
        reversed_layout['objects'].reverse()
        self.assertTrue(self.compare(candidate_layout=reversed_layout)['layout']['object_order_changed'])

    def test_opaque_rgba_mode_change_and_alpha_change_are_not_hidden(self):
        opaque = self.compare(candidate_bytes=image_bytes(mode='RGBA'))
        self.assertTrue(opaque['pixel']['mode_changed'])
        self.assertEqual(opaque['pixel']['changed_pixels'], 0)
        self.assertFalse(opaque['matches'])
        alpha = image_bytes(mode='RGBA', edit=lambda image: image.putpixel((1, 1), (255, 255, 255, 0)))
        self.assertEqual(self.compare(candidate_bytes=alpha)['pixel']['changed_pixels'], 1)

    def test_identical_pixels_with_changed_color_profile_metadata_do_not_match(self):
        output = BytesIO()
        with Image.new('RGB', (32, 16), 'white') as source:
            source.save(output, format='PNG', icc_profile=b'fixture profile metadata only')
        result = self.compare(candidate_bytes=output.getvalue())
        self.assertEqual(result['pixel']['changed_pixels'], 0)
        self.assertTrue(result['pixel']['color_profile_metadata_changed'])
        self.assertFalse(result['matches'])
        self.assertFalse(result['color_profile_rendering_verified'])

    def test_exif_orientation_change_with_identical_decoded_pixels_does_not_match(self):
        def jpeg(orientation):
            output = BytesIO()
            metadata = Image.Exif()
            metadata[274] = orientation
            with Image.new('RGB', (32, 16), 'white') as source:
                source.putpixel((1, 1), (255, 0, 0))
                source.save(output, format='JPEG', quality=90, exif=metadata)
            return output.getvalue()
        reference = self.register('orientation-golden', jpeg(1), 'image')
        unchanged = self.compare(image_ref=reference, candidate_bytes=jpeg(1))
        self.assertTrue(unchanged['matches'])
        rotated_metadata = self.compare(image_ref=reference, candidate_bytes=jpeg(6))
        self.assertEqual(rotated_metadata['pixel']['changed_pixels'], 0)
        self.assertEqual(rotated_metadata['pixel']['reference_orientation'], 1)
        self.assertEqual(rotated_metadata['pixel']['candidate_orientation'], 6)
        self.assertTrue(rotated_metadata['pixel']['orientation_metadata_changed'])
        self.assertFalse(rotated_metadata['matches'])
        self.assertEqual(rotated_metadata['pixel']['candidate_size'], [32, 16])  # No implicit rotation.
        self.assertFalse(rotated_metadata['color_profile_rendering_verified'])
        self.assertFalse(rotated_metadata['semantic_ocr_verified'])

    def test_candidate_canvas_inconsistency_is_reported_without_inventing_a_match(self):
        candidate = copy.deepcopy(self.layout)
        candidate['canvas']['width'] = 31
        result = self.compare(candidate_layout=candidate)
        self.assertFalse(result['candidate_canvas_matches_image'])
        self.assertFalse(result['matches'])

    def test_draft_missing_version_and_wrong_hash_are_rejected_without_fallback(self):
        draft = self.register('draft-golden', self.golden, 'image', approve=False)
        with self.assertRaises(Conflict):
            self.compare(image_ref=draft)
        with self.assertRaises(NotFound):
            self.compare(image_ref=self.image_ref | {'version': 'v99'})
        with self.assertRaises(Conflict):
            self.compare(layout_ref=self.layout_ref | {'sha256': 'f' * 64})

    def test_approved_wrong_asset_kinds_and_bad_reference_shapes_are_rejected(self):
        wrong_kind = self.register('font-as-image', self.golden, 'font')
        with self.assertRaises(DesignCompareError):
            self.compare(image_ref=wrong_kind)
        for reference in ({'asset_id': 'golden'}, self.image_ref | {'url': 'https://example.invalid/image'},
                          self.image_ref | {'asset_id': '../other'}):
            with self.assertRaises(DesignCompareError):
                self.compare(image_ref=reference)

    def test_reference_image_layout_inconsistency_is_rejected(self):
        wrong_layout = layout(size=(31, 16))
        ref = self.register('wrong-layout', json.dumps(wrong_layout).encode(), 'template')
        with self.assertRaises(DesignCompareError):
            self.compare(layout_ref=ref)

    def test_invalid_truncated_oversize_and_unsupported_images_are_rejected(self):
        for content in (b'', b'not an image', self.golden[:20], b'x' * (MAX_IMAGE_BYTES + 1)):
            with self.assertRaises(DesignCompareError):
                self.compare(candidate_bytes=content)
        output = BytesIO()
        with Image.new('RGB', (32, 16), 'white') as source:
            source.save(output, format='BMP')
        with self.assertRaises(DesignCompareError):
            self.compare(candidate_bytes=output.getvalue())

    def test_pixel_limit_is_checked_before_decoding_and_bomb_warning_is_an_error(self):
        with patch('manager_bridge.design_compare.MAX_PIXELS', 511):
            with self.assertRaises(DesignCompareError):
                self.compare()
        with patch('PIL.Image.MAX_IMAGE_PIXELS', 255):
            with self.assertRaises(DesignCompareError):
                self.compare()

    def test_multiframe_image_is_rejected(self):
        output = BytesIO()
        with Image.new('RGB', (32, 16), 'white') as one, Image.new('RGB', (32, 16), 'red') as two:
            one.save(output, format='TIFF', save_all=True, append_images=[two])
        with self.assertRaises(DesignCompareError):
            self.compare(candidate_bytes=output.getvalue())

    def test_duplicate_ids_nonfinite_coordinates_and_extra_unchecked_fields_are_rejected(self):
        variants = []
        duplicate = copy.deepcopy(self.layout)
        duplicate['objects'][1]['id'] = 'sender'
        variants.append(duplicate)
        infinite = copy.deepcopy(self.layout)
        infinite['objects'][0]['x'] = float('inf')
        variants.append(infinite)
        unknown = copy.deepcopy(self.layout)
        unknown['objects'][0]['unvalidated_color'] = 'red'
        variants.append(unknown)
        boolean = copy.deepcopy(self.layout)
        boolean['objects'][0]['y'] = True
        variants.append(boolean)
        huge_integer = copy.deepcopy(self.layout)
        huge_integer['objects'][0]['x'] = 10**500
        variants.append(huge_integer)
        for candidate in variants:
            with self.assertRaises(DesignCompareError):
                self.compare(candidate_layout=candidate)

    def test_duplicate_json_keys_and_nonfinite_json_reference_are_rejected(self):
        duplicate = self.register('duplicate-layout', b'{"canvas": {}, "canvas": {}, "objects": []}', 'template')
        with self.assertRaises(DesignCompareError):
            self.compare(layout_ref=duplicate)
        invalid = self.register('nonfinite-layout', b'{"canvas": {"width": NaN,"height": 16}, "objects": []}', 'template')
        with self.assertRaises(DesignCompareError):
            self.compare(layout_ref=invalid)


if __name__ == '__main__':
    unittest.main()
