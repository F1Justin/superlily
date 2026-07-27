from __future__ import annotations

import pytest

from superlily_contracts import (
    MarkdownDocumentIn,
    MarkdownRenderingError,
    markdown_to_render_document,
    render_document_plain_text,
)
from superlily_latex_provider.worker import document_latex


def _payload(markdown: str) -> MarkdownDocumentIn:
    return MarkdownDocumentIn(
        instance_id="nekro-agent",
        conversation_key="onebot_v11-group_861651713",
        source_event_id="qq:message:markdown-1",
        markdown=markdown,
    )


def test_ordinary_markdown_lowers_to_safe_blocks_with_inline_math() -> None:
    document = markdown_to_render_document(
        _payload(
            r"""# 柏拉图著作的传抄过程

## 初期：柏拉图学院时期
已知多项式 $f(x)=x^3+px^2+qx+r$。

- **抄本之王：** 现存最重要的手稿
- **修道院传承：** 由僧侣代代传抄

$$
C^{-1}=\begin{pmatrix}1&0\\0&1\end{pmatrix}
$$

```python
print("**代码保持原样**")
```

| 时期 | 载体 |
| --- | --- |
| 古典 $\\lambda$ | 草纸 |
"""
        )
    )

    assert document.schema_version == "1.3"
    assert [block.kind for block in document.blocks] == [
        "heading",
        "heading",
        "paragraph",
        "list",
        "math",
        "code",
        "table",
    ]
    assert document.blocks[3].items[0].startswith("**抄本之王：**")
    source = document_latex(document)
    assert r"\textbf{抄本之王：}" in source
    assert r"\(f(x)=x^3+px^2+qx+r\)" in source
    assert "**抄本之王" not in source
    assert 'print("**代码保持原样**")' in source
    assert r"\(\lambda\)" in source
    plain = render_document_plain_text(document)
    assert "**抄本之王" not in plain
    assert "抄本之王：" in plain


def test_untrusted_html_links_and_images_remain_literal_text() -> None:
    document = markdown_to_render_document(
        _payload(
            '<script>alert(1)</script> [外链](https://example.com) '
            '![本地](/etc/passwd)'
        )
    )
    assert document.blocks[0].kind == "paragraph"
    source = document_latex(document)
    assert "<script>alert(1)</script>" in source
    assert "https://example.com" in source
    assert "/etc/passwd" in source
    assert r"\href" not in source
    assert r"\includegraphics" not in source


def test_markdown_limits_fail_closed_without_partial_output() -> None:
    with pytest.raises(MarkdownRenderingError) as unclosed:
        markdown_to_render_document(_payload("$$\nx+1"))
    assert unclosed.value.code == "markdown_math_unclosed"

    too_many = "\n\n".join(f"段落 {index}" for index in range(65))
    with pytest.raises(MarkdownRenderingError) as block_limit:
        markdown_to_render_document(_payload(too_many))
    assert block_limit.value.code == "markdown_block_limit"


@pytest.mark.parametrize(
    "markdown",
    [
        "损坏的行内公式 $x=\x0crac{1}{2}$",
        "$$\n\x07lpha + 1\n$$",
        "损坏的加粗公式 **结论：$x=\x08ar{x}$**",
    ],
)
def test_python_string_escape_corruption_is_rejected_before_xelatex(
    markdown: str,
) -> None:
    with pytest.raises(MarkdownRenderingError) as corrupted:
        markdown_to_render_document(_payload(markdown))

    assert corrupted.value.code == "markdown_math_escape_corrupted"
    assert "md001" in corrupted.value.safe_detail


@pytest.mark.parametrize(
    "markdown",
    [
        "多余右括号 $g=h}$",
        "$$\n\\frac{a}{b\n$$",
    ],
)
def test_unbalanced_math_braces_are_rejected_before_xelatex(markdown: str) -> None:
    with pytest.raises(MarkdownRenderingError) as unbalanced:
        markdown_to_render_document(_payload(markdown))

    assert unbalanced.value.code == "markdown_math_unbalanced_braces"


def test_escaped_math_braces_remain_valid() -> None:
    document = markdown_to_render_document(_payload(r"集合 $\{x\mid x>0\}$"))

    assert document.blocks[0].kind == "paragraph"


def test_multiline_display_math_remains_valid() -> None:
    document = markdown_to_render_document(
        _payload(
            r"""$$
\begin{aligned}
x &= 1 \\
y &= 2
\end{aligned}
$$"""
        )
    )

    assert document.blocks[0].kind == "math"
