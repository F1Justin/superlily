import importlib.util
from pathlib import Path

p = Path(__file__).parents[1] / 'bridges/nekro/superlily_bridge/media_metadata.py'
spec = importlib.util.spec_from_file_location('bridge_media_metadata', p)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_image_labels_survive_conversion_without_retaining_urls():
    key = 'a' * 32
    captured = module.capture_image_metadata([
        {'type': 'image', 'data': {'file': key.upper()+'.jpg', 'summary': '[动画表情]',
                                 'sub_type': 1, 'url': 'https://invalid/?secret=secret'}},
    ])
    assert 'secret' not in str(captured)
    original = [{'type': 'image', 'file_name': key+'.jpg', 'local_path': '/kept'}]
    result = module.enrich_image_segments(original, captured)
    assert result[0]['summary'] == '[动画表情]'
    assert result[0]['sub_type'] == 1
    assert result[0]['local_path'] == '/kept'
    assert 'summary' not in original[0]


def test_repeated_images_keep_distinct_labels_and_unmatched_images_stay_unknown():
    key = 'b' * 32
    raw = [{'type': 'image', 'data': {'file': key+'.jpg', 'summary': name, 'sub_type': kind}}
           for name, kind in [('a', 0), ('b', 1)]]
    result = module.enrich_image_segments([
        {'type': 'image', 'file_name': key+'.jpg'},
        {'type': 'image', 'file_name': key+'.jpg'},
        {'type': 'image', 'file_name': 'unrelated.gif'},
    ], module.capture_image_metadata(raw))
    assert [s.get('summary') for s in result] == ['a', 'b', None]
    assert result[-1].get('sub_type') is None
