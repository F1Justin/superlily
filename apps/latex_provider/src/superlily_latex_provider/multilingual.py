"""Script-aware text runs for the isolated XeTeX renderer."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
import json
import unicodedata

import regex

# Script names are fontspec identifiers; font names never come from document text.
SCRIPT_FONTS = {
    'Arabic': 'Noto Naskh Arabic',
    **{name: f'Noto Serif {name}' for name in (
        'Hebrew', 'Armenian', 'Georgian', 'Devanagari', 'Bengali', 'Gujarati',
        'Gurmukhi', 'Kannada', 'Malayalam', 'Tamil', 'Telugu', 'Sinhala',
        'Thai', 'Lao', 'Khmer', 'Myanmar', 'Tibetan', 'Ethiopic', 'Balinese',
    )},
    **{name: f'Noto Sans {name}' for name in (
        'Syriac', 'Thaana', 'Mongolian', 'Cherokee', 'Coptic', 'Glagolitic',
        'Gothic', 'Runic', 'Ogham', 'Yi',
    )},
}
SYMBOL_FONTS = {'Symbols': 'Noto Sans Symbols', 'SymbolsTwo': 'Noto Sans Symbols2', 'Music': 'Noto Music', 'MathSymbols': 'Noto Sans Math'}
_COVERAGE_PATH = Path(__file__).with_name('fonts') / 'coverage.json'
_COVERAGE = {name: frozenset(n for lo, hi in ranges for n in range(lo, hi + 1))
             for name, ranges in json.loads(_COVERAGE_PATH.read_text()).items()}
_SCRIPT_PATTERNS = [(name, regex.compile(rf'\p{{Script={name}}}')) for name in SCRIPT_FONTS]
_EMOJI = regex.compile(r'\p{Emoji_Presentation}|\p{Regional_Indicator}|\u20e3')
_PICTOGRAPH = regex.compile(r'\p{Extended_Pictographic}')
_GRAPHEMES = regex.compile(r'\X')
_ESCAPES = str.maketrans({
    '\\': r'\textbackslash{}', '{': r'\{', '}': r'\}', '$': r'\$',
    '&': r'\&', '#': r'\#', '%': r'\%', '_': r'\_',
    '^': r'\textasciicircum{}', '~': r'\textasciitilde{}',
})
RTL_SCRIPTS = {'Arabic', 'Hebrew', 'Syriac', 'Thaana'}


def _escape(value: str) -> str:
    return value.translate(_ESCAPES).replace('\n', '\\\\\n')


def text_latex(value: str) -> str:
    runs: list[tuple[str, str]] = []
    for cluster in _GRAPHEMES.findall(value):
        if '\ufe0e' not in cluster and (_EMOJI.search(cluster) or ('\ufe0f' in cluster and _PICTOGRAPH.search(cluster))):
            script = 'Emoji'
        else:
            script = next((s for s, pattern in _SCRIPT_PATTERNS if pattern.search(cluster)), '')
            if not script and any(ord(c) not in _COVERAGE['Main'] for c in cluster):
                script = next((name for name in SYMBOL_FONTS if all(
                    ord(c) in _COVERAGE[name] for c in cluster if c != '\ufe0e'
                )), '')
            if not script and runs and runs[-1][0] in RTL_SCRIPTS and all(
                (ord(c) < 0x3000 and unicodedata.category(c)[0] in 'PNZ') or c in '\u200c\u200d' for c in cluster
            ):
                script = runs[-1][0]
        if runs and runs[-1][0] == script:
            runs[-1] = (script, runs[-1][1] + cluster)
        else:
            runs.append((script, cluster))
    result = []
    for script, content in runs:
        escaped = _escape(content.replace('\ufe0e', ''))
        if script in RTL_SCRIPTS:
            trailing = content[len(content.rstrip()):]
            content = content.rstrip()
            pieces = regex.split(r'([\p{N}]+(?:[.,:/-][\p{N}]+)*|[^\p{L}\p{M}\s\u200c\u200d]+)', content)
            escaped = ''.join(
                (r'\LR{' + (r'\rmfamily ' if all(ord(c) in _COVERAGE['Main'] for c in p) else '') + _escape(p) + '}') if p and p[0].isnumeric()
                else (r'{\rmfamily ' + _escape(p) + '}') if p and unicodedata.category(p[0])[0] in 'PS' and all(ord(c) in _COVERAGE['Main'] for c in p)
                else _escape(p) for p in pieces
            )
            result.append(r'\RL{{\slfont' + script + ' ' + escaped + '}}' + _escape(trailing))
        elif script:
            result.append(r'{\makexeCJKinactive\slfont' + script + ' ' + escaped + '}')
        else:
            result.append(escaped)
    return ''.join(result)


def font_preamble(latex: str) -> str:
    used = set(re.findall(r'\\slfont([A-Za-z]+)', latex)) & (set(SCRIPT_FONTS) | set(SYMBOL_FONTS) | {'Emoji'})
    definitions = []
    for name in sorted(used):
        if name == 'Emoji':
            definitions.append(r'\newfontfamily\slfontEmoji{Noto Emoji}[BoldFont={Noto Emoji},ItalicFont={Noto Emoji},BoldItalicFont={Noto Emoji}]')
        elif name in SYMBOL_FONTS:
            font = SYMBOL_FONTS[name]
            definitions.append(r'\newfontfamily\slfont' + name + '{' + font + '}[BoldFont={' + font + '},ItalicFont={' + font + '},BoldItalicFont={' + font + '}]')
        else:
            font = SCRIPT_FONTS[name]
            definitions.append(r'\newfontfamily\slfont' + name + '{' + font + '}[Script=' + name + ',AutoFakeSlant=0.15]')
    if used & RTL_SCRIPTS:
        definitions.append(r'\usepackage{bidi}')
    return '\n'.join(definitions) + '\n'


def implementation_sha256() -> str:
    return sha256(Path(__file__).read_bytes() + _COVERAGE_PATH.read_bytes() + Path(__file__).with_name('fonts').joinpath('manifest.json').read_bytes()).hexdigest()
