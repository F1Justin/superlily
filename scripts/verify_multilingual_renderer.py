"""Run inside the font-enabled worker sandbox; write reviewable PNGs to /out."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import time

from superlily_contracts import RenderDocument
from superlily_contracts.markdown_rendering import MarkdownDocumentIn, markdown_to_render_document
from superlily_contracts.festival_themes import PALETTES
from superlily_latex_provider.runtime import LatexWorkerError, inspect_png
from superlily_latex_provider.worker import document_latex, render_document_png, template_sha256, renderer_versions, DEFAULT_XELATEX, DEFAULT_PDFTOPPM

SAMPLE = r'''# 多语言排版 · Typography
中文 English 123 — **衬线字体**

Русский: Привет, мир! Українська: Привіт, світе!

Ελληνικά: Καλημέρα κόσμε! café naïve č š ł ă

阿拉伯文：السَّلَامُ عَلَيْكُمْ — مرحبا بالعالم 123

עברית: שלום עולם 123

हिन्दी: नमस्ते दुनिया · ไทย: สวัสดีชาวโลก

日本語：こんにちは · 한국어: 안녕하세요

😀 😭 ❤️ 👍🏽 👨‍👩‍👧‍👦 🇨🇳 🇷🇺 1️⃣ 🐏 🧨

**Русский العربية 😀**

公式：$E=mc^2$，$\alpha + \beta = \gamma$。
'''


def document(markdown):
    return markdown_to_render_document(MarkdownDocumentIn(
        instance_id='font-preview', conversation_key='onebot_v11-group_font-preview', markdown=markdown,
    ))


def main():
    out = Path('/out')
    out.mkdir(exist_ok=True)
    out.joinpath('sample.md').write_text(SAMPLE)
    doc = document(SAMPLE)
    results = {}
    for theme in PALETTES:
        started = time.monotonic()
        data = render_document_png(doc, theme_id=theme)
        out.joinpath(theme + '.png').write_bytes(data)
        results[theme] = {'sha256': sha256(data).hexdigest(), 'size': inspect_png(data), 'seconds': round(time.monotonic()-started, 2)}
        print(theme, results[theme], flush=True)
    # Include AST paths that use fonts differently: table, monospace, italic, and headings.
    extra = document('''# **العربية ١٢٣، فارسی**

Հայերեն · ქართული · বাংলা · ગુજરાતી · ਪੰਜਾਬੀ

ಕನ್ನಡ · മലയാളം · தமிழ் · తెలుగు · සිංහල

ພາສາລາວ · ខ្មែរ · မြန်မာ · བོད་ཡིག · አማርኛ

☑ ✓ → ↔ ∞ ∑ ∫ ≠ ≤ ≥ ♩ 𝄞 ♥︎

> Русский العربية हिन्दी ไทย 😀

| Русский | العربية | emoji |
| --- | --- | --- |
| Привет | مرحبا ١٢٣ | 👍🏽 |

```
Русский Ελληνικά العربية हिन्दी 😀 <x> & \\path
```
''')
    data = render_document_png(extra)
    out.joinpath('extended.png').write_bytes(data)
    # Fail closed on a character no packaged font supports.
    unsupported = document('unsupported: \U0010ffff')
    try:
        render_document_png(unsupported)
    except LatexWorkerError as error:
        assert error.error_code == 'execution_failed', error
    else:
        raise AssertionError('missing character returned a successful artifact')
    tex = document_latex(doc)
    out.joinpath('sample.tex').write_text(tex)
    compiled = subprocess.run([str(DEFAULT_XELATEX), '-no-shell-escape', '-interaction=nonstopmode', '-halt-on-error', '-output-directory', '/work', '/out/sample.tex'], cwd='/work', capture_output=True, text=True, check=True)
    assert 'Missing character:' not in compiled.stdout
    out.joinpath('compile.log').write_text(compiled.stdout)
    out.joinpath('verification.json').write_text(json.dumps({
        'template_sha256': template_sha256(), 'engine_versions': renderer_versions(DEFAULT_XELATEX, DEFAULT_PDFTOPPM),
        'renders': results, 'missing_glyph_rejected': True,
    }, indent=2))
    print('extended scripts/styles and missing-glyph rejection passed', flush=True)


if __name__ == '__main__':
    main()
