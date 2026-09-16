from superlily_contracts import RenderDocument
from superlily_latex_provider.multilingual import text_latex
from superlily_latex_provider.worker import document_latex


def test_unicode_text_cannot_introduce_tex_commands():
    tex = text_latex('العربية \\input{/etc/passwd} 10% & # 😀')
    assert r'\input{' not in tex
    assert r'\textbackslash{}' in tex
    assert r'\%' in tex and r'\&' in tex and r'\#' in tex


def test_arabic_words_stay_joined_and_numbers_read_left_to_right():
    tex = text_latex('مرحبا بالعالم 123.45 中文')
    assert 'مرحبا بالعالم' in tex
    assert r'\LR{\rmfamily 123.45}' in tex
    assert tex.count(r'\RL{') == 1
    assert tex.endswith('中文')


def test_emoji_sequences_are_not_split_into_separate_font_runs():
    for value in ['👨‍👩‍👧‍👦', '👍🏽', '🇨🇳', '1️⃣', '❤️']:
        tex = text_latex(value)
        assert value in tex
        assert tex.count(r'\slfontEmoji') == 1
        assert r'\makexeCJKinactive' in tex
    assert 'slfontEmoji' not in text_latex('123 # *')


def test_script_fonts_are_in_preamble_and_math_is_unchanged():
    doc = RenderDocument(instance_id='test', conversation_key='onebot_v11-group_test', blocks=[
        {'kind':'text', 'node_id':'p', 'text':'中文 Русский Ελληνικά العربية हिन्दी 😀'},
        {'kind':'math', 'node_id':'m', 'latex':r'\int_0^1 x^2\,dx = \frac13'},
    ])
    tex = document_latex(doc)
    preamble, body = tex.split(r'\begin{document}', 1)
    assert r'\setmainfont{Noto Serif}' in preamble
    assert 'Noto Naskh Arabic' in preamble and 'Noto Serif Devanagari' in preamble
    assert r'\usepackage{bidi}' in preamble
    assert r'\tracinglostchars=3' in preamble
    assert r'\int_0^1 x^2\,dx = \frac13' in body
    assert 'Русский Ελληνικά' in body


def test_symbol_fallback_and_text_presentation_selector():
    assert 'slfont' in text_latex('♩')
    assert 'slfontEmoji' not in text_latex('♥︎')
    assert '\ufe0e' not in text_latex('♥︎')


def test_plain_symbols_keep_text_presentation_and_rtl_trailing_space():
    assert 'slfontEmoji' not in text_latex('↔ © ™')
    assert text_latex('العربية हिन्दी').startswith(r'\RL{{\slfontArabic العربية}} ')
