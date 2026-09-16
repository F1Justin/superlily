"""Rebuild font assets with fonttools==4.61.1 and fonts-noto-core==20201225-2."""
import argparse
from hashlib import sha256
import json
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ASSETS = Path(__file__).resolve().parents[1] / 'apps/latex_provider/src/superlily_latex_provider/fonts'
FONTS = {
    'Main': 'NotoSerif-Regular.ttf',
    'Symbols': 'NotoSansSymbols-Regular.ttf',
    'SymbolsTwo': 'NotoSansSymbols2-Regular.ttf',
    'Music': 'NotoMusic-Regular.ttf',
    'MathSymbols': 'NotoSansMath-Regular.ttf',
}


def ranges(values):
    result = []
    for number in sorted(values):
        if result and number == result[-1][1] + 1:
            result[-1][1] = number
        else:
            result.append([number, number])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--noto-dir', type=Path, default=Path('/usr/share/fonts/truetype/noto'))
    args = parser.parse_args()
    font = TTFont(ASSETS / 'NotoEmoji-Variable.ttf', recalcTimestamp=False)
    instantiateVariableFont(font, {'wght': 400}, inplace=True).save(ASSETS / 'NotoEmoji-Regular.ttf')
    coverage = {}
    for name, filename in FONTS.items():
        with TTFont(args.noto_dir / filename) as font:
            coverage[name] = ranges(font.getBestCmap())
    (ASSETS / 'coverage.json').write_text(json.dumps(coverage, separators=(',', ':')) + '\n')
    manifest = {
        'source': 'https://github.com/google/fonts/tree/8b0a1d0f5983c89bc2b93f1b5fb55f9e252744b5/ofl/notoemoji',
        'derivation': 'fonttools 4.61.1 instantiateVariableFont wght=400, recalcTimestamp=False',
        'coverage_package': 'fonts-noto-core=20201225-2',
        'coverage_inputs': {name: sha256((args.noto_dir / name).read_bytes()).hexdigest() for name in FONTS.values()},
        'files': {name: sha256((ASSETS / name).read_bytes()).hexdigest() for name in (
            'NotoEmoji-Variable.ttf', 'NotoEmoji-Regular.ttf', 'OFL-NotoEmoji.txt', 'coverage.json',
        )},
    }
    (ASSETS / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    main()
