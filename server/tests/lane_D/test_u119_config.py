import copy
import yaml
import pytest
from lane_D.test_u119_speaker import SPEAKER

@pytest.mark.parametrize('change', ['missing', 'extra', 'threshold', 'bool', 'instructions', 'empty_option'])
def test_speaker_yaml_validated_at_startup(tmp_path, change):
    from waitless.call.keypad import load_speaker_config
    cfg = copy.deepcopy(SPEAKER)
    if change == 'missing':
        cfg['options'].pop('hold')
    elif change == 'extra':
        cfg['options']['other'] = 'other'
    elif change == 'threshold':
        cfg['min_confidence'] = 1.1
    elif change == 'bool':
        cfg['min_confidence'] = True
    elif change == 'instructions':
        cfg['instructions'] = ''
    else:
        cfg['options']['hold'] = ''
    path = tmp_path / 'phone-agent/references'
    path.mkdir(parents=True)
    (path/'speaker.yaml').write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError):
        load_speaker_config(tmp_path)
