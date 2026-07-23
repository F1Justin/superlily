from __future__ import annotations

import pytest
from pydantic import ValidationError

from superlily_contracts import RenderDocument
from superlily_latex_provider.worker import document_latex


def _document(block: dict) -> RenderDocument:
    return RenderDocument(
        schema_version="1.3",
        instance_id="nekro-agent",
        conversation_key="onebot_v11-group_1080353942",
        blocks=[block],
    )


@pytest.mark.parametrize(
    "latex",
    [
        r"\input{/etc/passwd}",
        r"\includegraphics{https://example.test/a.png}",
        r"\href{https://example.test}{click}",
        r"\font\evil=/etc/passwd",
        r"\fontspec{/usr/share/fonts/secret.ttf}",
        r"\setmainfont{file:/tmp/evil.ttf}",
        r"\pdfmapfile{/etc/passwd}",
        r"\directlua{os.execute('id')}",
        r"\everyjob{\input{/etc/passwd}}",
        r"\loop x\repeat",
    ],
)
def test_latex_file_network_font_and_expansion_bombs_are_rejected(latex: str) -> None:
    with pytest.raises(ValidationError, match="forbidden"):
        _document(
            {
                "kind": "math",
                "node_id": "unsafe",
                "latex": latex,
            }
        )


def test_markdown_html_and_remote_urls_are_literal_text_never_active_content() -> None:
    hostile = (
        '<script src="https://evil.test/x.js">alert(1)</script>\n'
        "![tracking](https://evil.test/pixel.png) "
        "[open](file:///etc/passwd) "
        '<img src="/etc/passwd">'
    )
    document = _document(
        {
            "kind": "paragraph",
            "node_id": "literal-hostile",
            "text": hostile,
        }
    )
    source = document_latex(document)
    assert "evil.test" in source
    assert "file:///etc/passwd" in source
    assert r"\includegraphics" not in source
    assert r"\href" not in source
    assert r"\input" not in source
    assert r"\url" not in source


def test_artifact_nodes_reject_paths_and_urls_and_svg_is_only_a_placeholder() -> None:
    for artifact_id in (
        "/etc/passwd",
        "../../etc/passwd",
        "https://evil.test/a.png",
        "file:///etc/passwd",
    ):
        with pytest.raises(ValidationError):
            _document(
                {
                    "kind": "image",
                    "node_id": "image",
                    "artifact_id": artifact_id,
                    "accessibility_text": "图片",
                }
            )

    raw = {
        "kind": "image",
        "node_id": "image",
        "artifact_id": "safe:image-1",
        "accessibility_text": "图片",
        "remote_url": "https://evil.test/a.png",
    }
    with pytest.raises(ValidationError, match="Extra inputs"):
        _document(raw)

    svg_reference = _document(
        {
            "kind": "artifact_ref",
            "node_id": "svg",
            "artifact_id": "safe:svg-1",
            "mime_type": "image/svg+xml",
            "label": "矢量图",
            "accessibility_text": "一个矢量图占位符",
        }
    )
    source = document_latex(svg_reference)
    assert "矢量图" in source
    assert "<svg" not in source
    assert r"\includegraphics" not in source


def test_oversized_document_trees_and_canonical_bytes_fail_before_rendering() -> None:
    with pytest.raises(ValidationError, match="at most 64"):
        RenderDocument(
            schema_version="1.3",
            instance_id="nekro-agent",
            conversation_key="onebot_v11-group_1080353942",
            blocks=[
                {"kind": "text", "node_id": f"node-{index}", "text": "x"}
                for index in range(65)
            ],
        )

    with pytest.raises(ValidationError, match="node limit"):
        RenderDocument(
            schema_version="1.3",
            instance_id="nekro-agent",
            conversation_key="onebot_v11-group_1080353942",
            blocks=[
                {
                    "kind": "group",
                    "node_id": f"group-{group}",
                    "blocks": [
                        {
                            "kind": "text",
                            "node_id": f"group-{group}-node-{index}",
                            "text": "x",
                        }
                        for index in range(32)
                    ],
                }
                for group in range(4)
            ],
        )

    with pytest.raises(ValidationError, match="canonical byte limit"):
        RenderDocument(
            schema_version="1.3",
            instance_id="nekro-agent",
            conversation_key="onebot_v11-group_1080353942",
            blocks=[
                {
                    "kind": "code",
                    "node_id": f"large-{index}",
                    "code": "x" * 8_000,
                }
                for index in range(5)
            ],
        )
