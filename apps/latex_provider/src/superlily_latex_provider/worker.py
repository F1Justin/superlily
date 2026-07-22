"""无网络、无 Core 凭据的单并发 LaTeX PNG 渲染 worker。"""

from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
from typing import Any

from superlily_contracts import (
    AlternativeBlock,
    ArtifactRefBlock,
    CodeBlock,
    GroupBlock,
    ImageBlock,
    NoticeBlock,
    ProgressBlock,
    QuoteBlock,
    RenderDocument,
    TableBlock,
    canonicalize_json_value,
    split_inline_math,
    strict_json_loads,
)

from .runtime import (
    MAX_ARTIFACT_BYTES,
    MAX_DIMENSION_PIXELS,
    MAX_HEADER_BYTES,
    MAX_LATEX_BYTES,
    MAX_REQUEST_BYTES,
    MIME_TYPE,
    LatexWorkerError,
    inspect_png,
)


DEFAULT_SOCKET_PATH = Path("/latex-ipc/worker.sock")
DEFAULT_WORK_ROOT = Path("/work")
DEFAULT_XELATEX = Path("/usr/local/texlive/2024/bin/x86_64-linux/xelatex")
DEFAULT_PDFTOPPM = Path("/usr/bin/pdftoppm")
DEFAULT_PDFINFO = Path("/usr/bin/pdfinfo")
COMPILE_TIMEOUT_SECONDS = 18
CONVERT_TIMEOUT_SECONDS = 8
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_PAGE_POINTS = 2_048
_PAGE_RE = re.compile(rb"(?m)^Pages:\s+([0-9]+)\s*$")
_SIZE_RE = re.compile(rb"(?m)^Page size:\s+([0-9.]+) x ([0-9.]+) pts")

TEMPLATE_PREFIX = r"""\documentclass[border=2pt]{standalone}
\usepackage{amsmath}
\usepackage{mathtools}
\usepackage{amsfonts}
\usepackage{amssymb}
\usepackage{xcolor}
\usepackage{ulem}
\usepackage{physics}
\usepackage{mhchem}
\usepackage{tikz}
\usepackage{chemfig}
\usepackage{tcolorbox}
\usepackage{graphicx}
\usepackage{tikz-cd}
\usepackage{tensor}
\usepackage{pgfplots}
\usepackage{enumitem}
\usepackage{bbm}
\usepackage{mathrsfs}
\usepackage[punct=kaiming,fontset=none]{ctex}
\setCJKmainfont{Noto Serif CJK SC}
\setCJKsansfont{Noto Sans CJK SC}
\setCJKmonofont{Noto Sans Mono CJK SC}
\setCJKmathfont{Noto Serif CJK SC}
\begin{document}
"""
TEMPLATE_SUFFIX = "\n\\end{document}\n"

DOCUMENT_TEMPLATE_PREFIX = r"""\documentclass[12pt,border=8pt,varwidth=350pt]{standalone}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{mathtools}
\usepackage{xcolor}
\usepackage{enumitem}
\usepackage{tabularx}
\usepackage[punct=kaiming,fontset=none]{ctex}
\setCJKmainfont{Noto Serif CJK SC}
\setCJKsansfont{Noto Sans CJK SC}
\setCJKmonofont{Noto Sans Mono CJK SC}
\setCJKmathfont{Noto Serif CJK SC}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.25em}
\setlength{\abovedisplayskip}{0.35em}
\setlength{\belowdisplayskip}{0.35em}
\setlength{\abovedisplayshortskip}{0.2em}
\setlength{\belowdisplayshortskip}{0.2em}
\setlist{itemsep=0.15em,topsep=0.15em,parsep=0pt,leftmargin=1.75em}
\linespread{1.03}
\hyphenpenalty=10000
\exhyphenpenalty=10000
\sloppy
\begin{document}
\fontsize{14pt}{17pt}\selectfont
"""


def template_sha256() -> str:
    templates = (
        "formula\x00"
        + TEMPLATE_PREFIX
        + "<LATEX>"
        + TEMPLATE_SUFFIX
        + "\x00document\x00"
        + DOCUMENT_TEMPLATE_PREFIX
        + "<RENDER_DOCUMENT_AST>"
        + TEMPLATE_SUFFIX
    )
    return sha256(templates.encode("utf-8")).hexdigest()


def _safe_version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("renderer version probe failed") from exc
    combined = result.stdout + result.stderr
    line = combined.splitlines()[0].decode("utf-8", "replace").strip() if combined else ""
    if not line or len(line) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in line):
        raise RuntimeError("renderer returned an invalid version")
    return line


def renderer_versions(xelatex: Path, pdftoppm: Path) -> tuple[str, str]:
    return _safe_version([str(xelatex), "--version"]), _safe_version([str(pdftoppm), "-v"])


def _document(latex: str) -> str:
    equation = (
        "\\begin{equation*}\\begin{aligned}\n"
        + latex
        + "\n\\end{aligned}\\end{equation*}"
        if r"\\" in latex
        else "$\\displaystyle " + latex + "$"
    )
    return TEMPLATE_PREFIX + equation + TEMPLATE_SUFFIX


_TEXT_ESCAPES = str.maketrans(
    {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "$": r"\$",
        "&": r"\&",
        "#": r"\#",
        "%": r"\%",
        "_": r"\_",
        "^": r"\textasciicircum{}",
        "~": r"\textasciitilde{}",
    }
)


def _escape_text(value: str) -> str:
    return value.translate(_TEXT_ESCAPES).replace("\n", r"\\" + "\n")


def _mixed_text_latex(value: str) -> str:
    return "".join(
        _escape_text(content) if kind == "text" else r"\(" + content + r"\)"
        for kind, content in split_inline_math(value)
    )


def _leaf_block_latex(block: Any) -> str:
    if block.kind == "heading":
        size = (
            r"\fontsize{16pt}{19pt}\selectfont"
            if block.level == 1
            else r"\fontsize{14pt}{17pt}\selectfont"
        )
        return "{" + size + r"\bfseries " + _mixed_text_latex(block.text) + "}\\par\n"
    if block.kind == "text":
        return _mixed_text_latex(block.text) + "\\par\n"
    if block.kind == "math":
        if block.display:
            return r"\[\displaystyle " + block.latex + r"\]" + "\n"
        return r"\(" + block.latex + r"\)\par" + "\n"
    if block.kind == "list":
        environment = "enumerate" if block.ordered else "itemize"
        items = "".join(r"\item " + _mixed_text_latex(item) + "\n" for item in block.items)
        return f"\\begin{{{environment}}}\n{items}\\end{{{environment}}}\n"
    if isinstance(block, QuoteBlock):
        attribution = (
            r"\hfill--- " + _mixed_text_latex(block.attribution) + "\n"
            if block.attribution
            else ""
        )
        return (
            r"\begin{quote}\itshape "
            + _mixed_text_latex(block.text)
            + r"\par "
            + attribution
            + "\\end{quote}\n"
        )
    if isinstance(block, CodeBlock):
        language = (
            r"{\scriptsize\sffamily " + _escape_text(block.language) + r"}\par "
            if block.language
            else ""
        )
        return (
            r"\begingroup\small\ttfamily\raggedright "
            + language
            + _escape_text(block.code)
            + r"\par\endgroup"
            + "\n"
        )
    if isinstance(block, TableBlock):
        columns = "|" + "|".join("X" for _ in block.columns) + "|"
        rows = [block.columns, *block.rows]
        body = "\n".join(
            " & ".join(_mixed_text_latex(cell) for cell in row) + r"\\\hline"
            for row in rows
        )
        return (
            r"\begingroup\small\renewcommand{\arraystretch}{1.15}"
            + f"\\begin{{tabularx}}{{\\linewidth}}{{{columns}}}\\hline\n"
            + body
            + "\n\\end{tabularx}\\par\\endgroup\n"
        )
    if isinstance(block, NoticeBlock):
        colors = {"info": "blue!45!black", "warning": "orange!70!black", "error": "red!65!black"}
        title = _mixed_text_latex(block.title) + r"\quad " if block.title else ""
        return (
            r"\noindent\fcolorbox{"
            + colors[block.severity]
            + r"}{white}{\parbox{0.92\linewidth}{\bfseries "
            + title
            + r"\normalfont "
            + _mixed_text_latex(block.text)
            + "}}\\par\n"
        )
    if isinstance(block, ProgressBlock):
        detail = r"\quad " + _mixed_text_latex(block.detail) if block.detail else ""
        return (
            r"\noindent\textbf{"
            + _mixed_text_latex(block.label)
            + "}: "
            + str(block.value)
            + r"\%"
            + detail
            + "\\par\n"
        )
    if isinstance(block, (ImageBlock, ArtifactRefBlock)):
        label = (
            block.caption
            if isinstance(block, ImageBlock) and block.caption
            else block.label
            if isinstance(block, ArtifactRefBlock)
            else block.accessibility_text
        )
        return (
            r"\noindent\fbox{\parbox{0.92\linewidth}{\sffamily "
            + _mixed_text_latex(label or block.accessibility_text or "制品")
            + "}}\\par\n"
        )
    raise ValueError("unsupported render block")


def _render_block_latex(block: Any) -> str:
    if isinstance(block, GroupBlock):
        label = (
            r"{\bfseries " + _mixed_text_latex(block.label) + r"}\par "
            if block.label
            else ""
        )
        return label + "".join(_leaf_block_latex(child) for child in block.blocks)
    if isinstance(block, AlternativeBlock):
        option = next(
            option for option in block.options if option.option_id == block.preferred_option_id
        )
        return "".join(_leaf_block_latex(child) for child in option.blocks)
    return _leaf_block_latex(block)


def document_latex(document: RenderDocument) -> str:
    """Compile the reviewed RenderDocument AST into a bounded TeX document."""

    parts = [DOCUMENT_TEMPLATE_PREFIX]
    if document.title:
        parts.append(
            r"{\fontsize{20pt}{24pt}\selectfont\bfseries "
            + _mixed_text_latex(document.title)
            + r"}\par\smallskip"
            + "\n"
        )
    for block in document.blocks:
        parts.append(_render_block_latex(block))
    parts.append(TEMPLATE_SUFFIX)
    return "".join(parts)


def _bounded_environment(work_dir: Path) -> dict[str, str]:
    return {
        "HOME": str(work_dir),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/texlive/2024/bin/x86_64-linux:/usr/bin:/bin",
        "TEXMFCONFIG": str(work_dir / "texmf-config"),
        "TEXMFHOME": str(work_dir / "texmf-home"),
        "TEXMFOUTPUT": str(work_dir),
        "TEXMFSYSVAR": "/usr/local/texlive/2024/texmf-var",
        "TEXMFVAR": str(work_dir / "texmf-var"),
        "openin_any": "p",
        "openout_any": "p",
        "shell_escape": "f",
    }


def render_latex_png(
    latex: str,
    *,
    work_root: Path = DEFAULT_WORK_ROOT,
    xelatex: Path = DEFAULT_XELATEX,
    pdftoppm: Path = DEFAULT_PDFTOPPM,
    pdfinfo: Path = DEFAULT_PDFINFO,
) -> bytes:
    """渲染一个公式；任何失败都只抛出不含用户内容的安全错误。"""

    if not isinstance(latex, str) or not latex.strip() or "\x00" in latex:
        raise LatexWorkerError("invalid_output", "latex input is invalid")
    if len(latex.encode("utf-8")) > MAX_LATEX_BYTES:
        raise LatexWorkerError("budget_exceeded", "latex input exceeded its byte limit")
    if not all(path.is_absolute() and path.is_file() for path in (xelatex, pdftoppm, pdfinfo)):
        raise LatexWorkerError("internal_error", "latex renderer binaries are unavailable")
    try:
        with tempfile.TemporaryDirectory(prefix="render-", dir=work_root) as raw_dir:
            work_dir = Path(raw_dir)
            tex_path = work_dir / "input.tex"
            pdf_path = work_dir / "input.pdf"
            png_path = work_dir / "artifact.png"
            tex_path.write_text(_document(latex), encoding="utf-8")
            environment = _bounded_environment(work_dir)
            try:
                compiled = subprocess.run(
                    [
                        str(xelatex),
                        "--no-shell-escape",
                        "-interaction=nonstopmode",
                        "-halt-on-error",
                        "-file-line-error",
                        "-output-directory",
                        str(work_dir),
                        str(tex_path),
                    ],
                    cwd=work_dir,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=COMPILE_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise LatexWorkerError("timeout", "latex compilation exceeded its hard wall time") from exc
            if compiled.returncode != 0 or not pdf_path.is_file():
                raise LatexWorkerError("execution_failed", "latex compilation failed safely")
            if not 1 <= pdf_path.stat().st_size <= MAX_PDF_BYTES:
                raise LatexWorkerError("budget_exceeded", "latex PDF exceeded its hard byte bound")
            try:
                info = subprocess.run(
                    [str(pdfinfo), str(pdf_path)],
                    cwd=work_dir,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=3,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise LatexWorkerError("timeout", "latex PDF inspection timed out") from exc
            pages = _PAGE_RE.search(info.stdout)
            page_size = _SIZE_RE.search(info.stdout)
            if info.returncode != 0 or pages is None or int(pages.group(1)) != 1 or page_size is None:
                raise LatexWorkerError("invalid_output", "latex PDF violated the single-page contract")
            width_points, height_points = (float(page_size.group(1)), float(page_size.group(2)))
            if not (
                0 < width_points <= MAX_PAGE_POINTS and 0 < height_points <= MAX_PAGE_POINTS
            ):
                raise LatexWorkerError("budget_exceeded", "latex PDF page dimensions exceeded the bound")
            try:
                converted = subprocess.run(
                    [
                        str(pdftoppm),
                        "-png",
                        "-singlefile",
                        "-f",
                        "1",
                        "-l",
                        "1",
                        "-scale-to",
                        str(MAX_DIMENSION_PIXELS),
                        str(pdf_path),
                        str(png_path.with_suffix("")),
                    ],
                    cwd=work_dir,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=CONVERT_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise LatexWorkerError("timeout", "latex PNG conversion timed out") from exc
            if converted.returncode != 0 or not png_path.is_file():
                raise LatexWorkerError("execution_failed", "latex PNG conversion failed safely")
            if not 1 <= png_path.stat().st_size <= MAX_ARTIFACT_BYTES:
                raise LatexWorkerError("budget_exceeded", "latex PNG exceeded its hard byte bound")
            content = png_path.read_bytes()
            inspect_png(content)
            return content
    except LatexWorkerError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise LatexWorkerError("internal_error", "latex renderer failed its local boundary") from exc


def render_document_png(
    document: RenderDocument,
    *,
    work_root: Path = DEFAULT_WORK_ROOT,
    xelatex: Path = DEFAULT_XELATEX,
    pdftoppm: Path = DEFAULT_PDFTOPPM,
    pdfinfo: Path = DEFAULT_PDFINFO,
) -> bytes:
    """Render a validated mixed CJK/math document with the same isolated toolchain."""

    latex = document_latex(document)
    if len(latex.encode("utf-8")) > 64 * 1024:
        raise LatexWorkerError("budget_exceeded", "document exceeded its compiled byte limit")
    try:
        with tempfile.TemporaryDirectory(prefix="document-", dir=work_root) as raw_dir:
            work_dir = Path(raw_dir)
            tex_path = work_dir / "input.tex"
            pdf_path = work_dir / "input.pdf"
            png_path = work_dir / "artifact.png"
            tex_path.write_text(latex, encoding="utf-8")
            environment = _bounded_environment(work_dir)
            compiled = subprocess.run(
                [
                    str(xelatex),
                    "--no-shell-escape",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "-file-line-error",
                    "-output-directory",
                    str(work_dir),
                    str(tex_path),
                ],
                cwd=work_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=COMPILE_TIMEOUT_SECONDS,
                check=False,
            )
            if compiled.returncode != 0 or not pdf_path.is_file():
                raise LatexWorkerError("execution_failed", "document compilation failed safely")
            if not 1 <= pdf_path.stat().st_size <= MAX_PDF_BYTES:
                raise LatexWorkerError("budget_exceeded", "document PDF exceeded its hard byte bound")
            info = subprocess.run(
                [str(pdfinfo), str(pdf_path)],
                cwd=work_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=3,
                check=False,
            )
            pages = _PAGE_RE.search(info.stdout)
            page_size = _SIZE_RE.search(info.stdout)
            if info.returncode != 0 or pages is None or int(pages.group(1)) != 1 or page_size is None:
                raise LatexWorkerError("invalid_output", "document PDF violated the single-page contract")
            width_points, height_points = (float(page_size.group(1)), float(page_size.group(2)))
            if not (0 < width_points <= MAX_PAGE_POINTS and 0 < height_points <= MAX_PAGE_POINTS):
                raise LatexWorkerError("budget_exceeded", "document page dimensions exceeded the bound")
            converted = subprocess.run(
                [
                    str(pdftoppm),
                    "-png",
                    "-singlefile",
                    "-f",
                    "1",
                    "-l",
                    "1",
                    "-scale-to",
                    str(MAX_DIMENSION_PIXELS),
                    str(pdf_path),
                    str(png_path.with_suffix("")),
                ],
                cwd=work_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=CONVERT_TIMEOUT_SECONDS,
                check=False,
            )
            if converted.returncode != 0 or not png_path.is_file():
                raise LatexWorkerError("execution_failed", "document PNG conversion failed safely")
            content = png_path.read_bytes()
            if not 1 <= len(content) <= MAX_ARTIFACT_BYTES:
                raise LatexWorkerError("budget_exceeded", "document PNG exceeded its hard byte bound")
            inspect_png(content)
            return content
    except LatexWorkerError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise LatexWorkerError("timeout", "document rendering exceeded its hard wall time") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise LatexWorkerError("internal_error", "document renderer failed its local boundary") from exc


class LatexWorkerServer:
    def __init__(
        self,
        socket_path: Path,
        *,
        work_root: Path,
        xelatex: Path,
        pdftoppm: Path,
        pdfinfo: Path,
    ) -> None:
        if not socket_path.is_absolute() or not work_root.is_absolute():
            raise ValueError("worker paths must be absolute")
        self.socket_path = socket_path
        self.work_root = work_root
        self.xelatex = xelatex
        self.pdftoppm = pdftoppm
        self.pdfinfo = pdfinfo
        self.tex_version, self.poppler_version = renderer_versions(xelatex, pdftoppm)
        self.requests = 0
        self._render_lock = asyncio.Lock()

    def _prepare_socket(self) -> None:
        parent = self.socket_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_stat = parent.lstat()
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise RuntimeError("worker socket parent is not an authoritative directory")
        if parent_stat.st_uid != os.getuid():
            raise RuntimeError("worker socket parent has the wrong owner")
        parent.chmod(0o700)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            endpoint = self.socket_path.lstat()
            if not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.getuid():
                raise RuntimeError("refusing to replace a non-authoritative worker socket")
            self.socket_path.unlink()

    async def serve(self) -> None:
        self._prepare_socket()
        server = await asyncio.start_unix_server(self._handle, path=self.socket_path)
        self.socket_path.chmod(0o600)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        async with server:
            await stop.wait()
        self.socket_path.unlink(missing_ok=True)

    async def _send(self, writer: asyncio.StreamWriter, header: dict[str, Any], body: bytes = b"") -> None:
        writer.write(canonicalize_json_value(header).canonical_bytes + b"\n" + body)
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2)
            if not raw or not raw.endswith(b"\n") or len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("request frame invalid")
            payload = strict_json_loads(raw[:-1])
            if not isinstance(payload, dict):
                raise ValueError("request must be an object")
            if payload == {"op": "health"}:
                await self._send(
                    writer,
                    {
                        "ok": True,
                        "status": "ready",
                        "requests": self.requests,
                        "uid": os.getuid(),
                        "pid": os.getpid(),
                        "tex_version": self.tex_version,
                        "poppler_version": self.poppler_version,
                    },
                )
                return
            operation = payload.get("op")
            if operation == "render":
                if set(payload) != {"op", "latex"} or not isinstance(payload.get("latex"), str):
                    raise ValueError("latex request invalid")
                render_function = render_latex_png
                render_input: str | RenderDocument = payload["latex"]
            elif operation == "render_document":
                if set(payload) != {"op", "document"} or not isinstance(payload.get("document"), dict):
                    raise ValueError("document request invalid")
                render_function = render_document_png
                render_input = RenderDocument.model_validate(payload["document"])
            else:
                raise ValueError("request operation invalid")
            async with self._render_lock:
                content = await asyncio.to_thread(
                    render_function,
                    render_input,
                    work_root=self.work_root,
                    xelatex=self.xelatex,
                    pdftoppm=self.pdftoppm,
                    pdfinfo=self.pdfinfo,
                )
                self.requests += 1
            width, height = inspect_png(content)
            await self._send(
                writer,
                {
                    "ok": True,
                    "mime_type": MIME_TYPE,
                    "byte_size": len(content),
                    "content_sha256": sha256(content).hexdigest(),
                    "width_pixels": width,
                    "height_pixels": height,
                },
                content,
            )
        except LatexWorkerError as exc:
            await self._send(writer, {"ok": False, "error_code": exc.error_code})
        except (TimeoutError, ValueError, json.JSONDecodeError):
            await self._send(writer, {"ok": False, "error_code": "invalid_output"})
        except (OSError, RuntimeError):
            await self._send(writer, {"ok": False, "error_code": "internal_error"})
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superlily-latex-worker")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--xelatex", type=Path, default=DEFAULT_XELATEX)
    parser.add_argument("--pdftoppm", type=Path, default=DEFAULT_PDFTOPPM)
    parser.add_argument("--pdfinfo", type=Path, default=DEFAULT_PDFINFO)
    parser.add_argument("--print-template-sha256", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.print_template_sha256:
        print(template_sha256())
        return 0
    os.umask(0o077)
    asyncio.run(
        LatexWorkerServer(
            args.socket,
            work_root=args.work_root,
            xelatex=args.xelatex,
            pdftoppm=args.pdftoppm,
            pdfinfo=args.pdfinfo,
        ).serve()
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
