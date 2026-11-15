import copy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from manager_bridge.asset_registry import AssetRegistry
from manager_bridge.design_variants import compare_variant_design, VariantPolicyError
from manager_bridge.store import Store, Conflict


def image_bytes(edit=None, size=(64, 32), *, exif=None, icc=None, mode='RGB'):
    output = BytesIO()
    with Image.new(mode, size, 'white') as image:
        if edit:
            edit(image)
        options = {}
        if exif is not None:
            options['exif'] = exif
        if icc is not None:
            options['icc_profile'] = icc
        image.save(output, format='PNG', **options)
    return output.getvalue()


def golden_layout():
    return {'canvas': {'width': 64, 'height': 32}, 'objects': [
        {'id': 'sender', 'text': '가상 보내는 분', 'x': 4, 'y': 4, 'width': 20, 'height': 8, 'font_sha256': 'a'*64},
        {'id': 'logo', 'text': '고정 문구', 'x': 40, 'y': 8, 'width': 12, 'height': 12, 'font_sha256': 'b'*64}]}


class DesignVariantTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(root / 'state.db')
        self.owner = self.store.add_token('owner', 'owner', ['*'])
        self.store.register_project(self.owner, 'common', 'fixture/common')
        self.manager = self.store.add_token('manager', 'manager', ['common'])
        self.worker = self.store.add_token('worker', 'worker', ['common'])
        self.registry = AssetRegistry(self.store, root / 'assets')
        self.layout = golden_layout()
        self.golden = image_bytes()
        self.image_ref = self.asset('image', self.golden, 'image')
        self.layout_ref = self.asset('layout', json.dumps(self.layout, ensure_ascii=False).encode(), 'template')
        self.policy = {'schema_version': 1, 'image_ref': self.image_ref, 'layout_ref': self.layout_ref,
                       'allowed_text_object_ids': ['sender'],
                       'pixel_rectangles': [{'object_id': 'sender', 'x': 4, 'y': 4, 'width': 20, 'height': 8}]}
        self.policy_ref = self.policy_asset(self.policy)

    def asset(self, identity, content, kind, approve=True):
        asset = self.registry.register_draft(self.manager, 'common', identity, 'v1', content, kind)
        if approve:
            self.registry.approve(self.owner, 'common', identity, 'v1', asset['sha256'])
        return {'asset_id': identity, 'version': 'v1', 'sha256': asset['sha256']}

    def policy_asset(self, value, identity='policy', approve=True):
        return self.asset(identity, json.dumps(value, ensure_ascii=False).encode(), 'template', approve)

    def compare(self, **changes):
        args = dict(registry=self.registry, token=self.worker, project='common', image_ref=self.image_ref,
                    layout_ref=self.layout_ref, policy_ref=self.policy_ref, candidate_bytes=self.golden,
                    candidate_layout=self.layout)
        args.update(changes)
        return compare_variant_design(**args)

    def test_exact_and_permitted_variable_text_region_match_without_granting_approval(self):
        self.assertTrue(self.compare()['matches'])
        changed_layout = copy.deepcopy(self.layout)
        changed_layout['objects'][0]['text'] = '허용된 가상 문구'
        result = self.compare(candidate_layout=changed_layout,
                              candidate_bytes=image_bytes(lambda image: image.putpixel((5, 5), (0, 0, 0))))
        self.assertTrue(result['matches'])
        self.assertFalse(result['strict_matches'])
        self.assertEqual(result['outside_changed_pixels'], 0)
        self.assertEqual(result['allowed_mask_pixels'], 160)
        self.assertFalse(result['design_approval_granted'])
        self.assertFalse(result['semantic_ocr_verified'])
        self.assertNotIn('허용된 가상 문구', json.dumps(result, ensure_ascii=False))

    def test_one_changed_pixel_outside_region_or_on_half_open_edge_is_not_hidden(self):
        for coordinate in ((31, 31), (24, 5)):
            result = self.compare(candidate_bytes=image_bytes(lambda image: image.putpixel(coordinate, (0, 0, 0))))
            self.assertFalse(result['matches'])
            self.assertEqual(result['outside_changed_pixels'], 1)

    def test_fixed_text_geometry_font_order_and_canvas_changes_never_vary(self):
        variants = []
        for field, value in (('x', 5), ('font_sha256', 'c'*64), ('width', 21)):
            changed = copy.deepcopy(self.layout)
            changed['objects'][0][field] = value
            variants.append(changed)
        changed = copy.deepcopy(self.layout)
        changed['objects'][1]['text'] = '고정 문구 변경 금지'
        variants.append(changed)
        changed = copy.deepcopy(self.layout)
        changed['objects'].reverse()
        variants.append(changed)
        changed = copy.deepcopy(self.layout)
        changed['canvas']['width'] = 63
        variants.append(changed)
        for candidate in variants:
            result = self.compare(candidate_layout=candidate)
            self.assertFalse(result['matches'])
            self.assertFalse(result['layout_changes_allowed'])

    def test_size_mode_icc_and_exif_orientation_changes_are_not_masked(self):
        orientation = Image.Exif()
        orientation[274] = 6
        images = [image_bytes(size=(63, 32)), image_bytes(mode='RGBA'),
                  image_bytes(icc=b'fixture profile metadata'), image_bytes(exif=orientation)]
        for content in images:
            result = self.compare(candidate_bytes=content)
            self.assertFalse(result['matches'])
            self.assertFalse(result['metadata_unchanged'])

    def test_nonorientation_exif_change_is_also_rejected(self):
        metadata = Image.Exif()
        metadata[270] = 'fixture description metadata, not a pixel change'
        result = self.compare(candidate_bytes=image_bytes(exif=metadata))
        self.assertTrue(result['strict_matches'])  # Existing strict comparator checks orientation only.
        self.assertFalse(result['exif_metadata_unchanged'])
        self.assertFalse(result['matches'])
        self.assertNotIn('fixture description metadata', json.dumps(result))

    def test_policy_must_be_approved_exact_template_asset(self):
        draft = self.policy_asset(self.policy, 'draft-policy', approve=False)
        with self.assertRaises(Conflict):
            self.compare(policy_ref=draft)
        wrong_kind = self.asset('wrong-kind-policy', json.dumps(self.policy).encode(), 'image')
        with self.assertRaises(VariantPolicyError):
            self.compare(policy_ref=wrong_kind)
        with self.assertRaises(Conflict):
            self.compare(policy_ref=self.policy_ref | {'sha256': 'f'*64})

    def test_policy_cannot_be_reused_with_different_exact_golden_reference(self):
        other_ref = self.asset('same-pixels-new-id', self.golden, 'image')
        with self.assertRaises(VariantPolicyError):
            self.compare(image_ref=other_ref)

    def test_mask_overlapping_fixed_object_is_rejected(self):
        altered = copy.deepcopy(self.layout)
        altered['objects'][1].update(x=10, y=5, width=4, height=4)
        layout_ref = self.asset('overlap-layout', json.dumps(altered).encode(), 'template')
        policy = copy.deepcopy(self.policy)
        policy['layout_ref'] = layout_ref
        policy_ref = self.policy_asset(policy, 'overlap-policy')
        with self.assertRaises(VariantPolicyError):
            self.compare(layout_ref=layout_ref, candidate_layout=altered, policy_ref=policy_ref)

    def test_whole_canvas_or_excessive_mask_is_rejected_even_for_dynamic_object(self):
        altered = copy.deepcopy(self.layout)
        altered['objects'] = [altered['objects'][0]]
        altered['objects'][0].update(x=0, y=0, width=64, height=32)
        layout_ref = self.asset('canvas-layout', json.dumps(altered).encode(), 'template')
        for rect, name in (({'object_id': 'sender', 'x': 0, 'y': 0, 'width': 64, 'height': 32}, 'whole'),
                           ({'object_id': 'sender', 'x': 0, 'y': 0, 'width': 32, 'height': 32}, 'excess')):
            policy = self.policy | {'layout_ref': layout_ref, 'pixel_rectangles': [rect]}
            policy_ref = self.policy_asset(policy, name+'-policy')
            with self.assertRaises(VariantPolicyError):
                self.compare(layout_ref=layout_ref, candidate_layout=altered, policy_ref=policy_ref)

    def test_duplicate_masks_outside_bbox_and_missing_region_are_rejected(self):
        invalid = []
        overlapping = copy.deepcopy(self.policy)
        overlapping['pixel_rectangles'].append(dict(overlapping['pixel_rectangles'][0]))
        invalid.append(overlapping)
        outside = copy.deepcopy(self.policy)
        outside['pixel_rectangles'][0]['x'] = 0
        invalid.append(outside)
        no_mask = copy.deepcopy(self.policy)
        no_mask['pixel_rectangles'] = []
        invalid.append(no_mask)
        fractional = copy.deepcopy(self.policy)
        fractional['pixel_rectangles'][0]['width'] = 5.5
        invalid.append(fractional)
        unknown_object = copy.deepcopy(self.policy)
        unknown_object['allowed_text_object_ids'] = ['unregistered-object']
        invalid.append(unknown_object)
        for index, policy in enumerate(invalid):
            ref = self.policy_asset(policy, 'invalid-'+str(index))
            with self.assertRaises(VariantPolicyError):
                self.compare(policy_ref=ref)

    def test_no_variable_policy_behaves_as_strict_comparison(self):
        policy = self.policy | {'allowed_text_object_ids': [], 'pixel_rectangles': []}
        ref = self.policy_asset(policy, 'fixed-only')
        self.assertTrue(self.compare(policy_ref=ref)['matches'])
        changed = image_bytes(lambda image: image.putpixel((5, 5), (0, 0, 0)))
        self.assertFalse(self.compare(policy_ref=ref, candidate_bytes=changed)['matches'])


if __name__ == '__main__':
    unittest.main()
