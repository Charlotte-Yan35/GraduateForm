"""生成《诺丁汉大学数学系升学指导手册》PDF。

与 mkdocs 站点完全解耦的独立脚本(不被 mkdocs 导入,不影响本地预览)。
复用 transform.get_records() 的同一份派生数据,把 前言 + 专栏文章 + 申请案例
重新排成一本书,经 Pandoc + XeLaTeX 输出单个 PDF。

用法(conda base, cwd=site/):
    python feiyue/handbook.py --source cache      # 复用 .cache, 生成 PDF
    python feiyue/handbook.py --md-only           # 只生成中间 manuscript.md(无需 pandoc/TeX)

依赖: pandoc + XeLaTeX + 中文字体(Noto Serif CJK SC)。见 CLAUDE.md / 本地开发说明。
"""
import argparse
import subprocess
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

import docs
import transform

WORKING_DIR = Path.cwd()

# 手册专用结果标签(纯文本/中文,避免网页 emoji 短码在 LaTeX 里变成字面量)
STATUS_TEXT = {
    "Admit": "录取 Admit",
    "Reject": "拒 Reject",
    "Waitlist": "候补 Waitlist",
    "Chosen": "最终去向 Chosen",
}

SUBTITLE = "26 Fall 申请季"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def build_manuscript(records: list, templates_dir: Path, resources_dir: Path) -> str:
    _universities, programs, students, applications = records
    students_by_term = docs.build_students_by_term(students)
    articles_meta = docs.load_articles(WORKING_DIR)

    handbook_res = resources_dir / "handbook"
    preface = _read(handbook_res / "preface.md")
    front_matter = _read(handbook_res / "front-matter.md")

    # 只纳入在 resources/handbook/ 有人工净版的文章;占位文章(如 diy)无净版 → 自动跳过。
    article_bodies = []
    for meta in articles_meta:
        clean = handbook_res / f"{meta['slug']}.md"
        if clean.exists():
            article_bodies.append(_read(clean))

    env = Environment(loader=FileSystemLoader(str(templates_dir)))
    env.globals.update({
        "programs": programs,
        "applications": applications,
        "status_text": STATUS_TEXT,
    })
    template = env.get_template("handbook_manuscript.jinja")
    return template.render(
        subtitle=SUBTITLE,
        year=str(datetime.now().year),
        preface=preface,
        front_matter=front_matter,
        articles=article_bodies,
        students_by_term=students_by_term,
    )


def run_pandoc(manuscript_path: Path, pdf_path: Path, resources_dir: Path,
               pdf_engine: str = "xelatex", cjk_font: str = "Noto Serif CJK SC",
               main_font: str = "Noto Serif CJK SC") -> None:
    header = resources_dir / "handbook" / "header.tex"
    cmd = [
        "pandoc", str(manuscript_path),
        "--from=markdown",
        f"--pdf-engine={pdf_engine}",
        "--toc", "--toc-depth=1",
        "-V", "documentclass=report",
        "-V", f"CJKmainfont={cjk_font}",
        "-V", f"mainfont={main_font}",
        "-V", "geometry:a4paper,margin=2.5cm",
        "-V", "toc-title=目录",
        "-V", "linkcolor=black",
        "-V", "urlcolor=black",
        "-V", "lang=zh-CN",
        f"--include-in-header={header}",
        "-o", str(pdf_path),
    ]
    print(f"[INFO] Running pandoc ({pdf_engine}) ...")
    subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the 升学指导手册 PDF")
    parser.add_argument("--source", choices=["cloud", "cache"], default="cloud",
                        help="cloud: 从 Supabase 重新拉取(默认); cache: 复用上次 .cache")
    parser.add_argument("--templates", type=str, default="templates")
    parser.add_argument("--resources", type=str, default="resources")
    parser.add_argument("--output", type=str, default="output")
    parser.add_argument("--md-only", action="store_true",
                        help="只生成 manuscript.md, 不调用 pandoc(本地无 TeX 时调试用)")
    parser.add_argument("--keep-md", action="store_true",
                        help="保留中间 manuscript.md(默认生成 PDF 后删除)")
    parser.add_argument("--pdf-engine", type=str, default="xelatex",
                        help="pandoc PDF 引擎(默认 xelatex; 本地可用 tectonic 免装 texlive)")
    parser.add_argument("--cjk-font", type=str, default="Noto Serif CJK SC",
                        help="中文字体(默认 Noto Serif CJK SC; 本地无 Noto 时可用 'Songti SC')")
    parser.add_argument("--main-font", type=str, default=None,
                        help="拉丁正文字体(默认与 --cjk-font 相同)")
    args = parser.parse_args()

    print("[INFO] Step 1/3: Deriving records ...")
    records, _image_links = transform.get_records(args.source)

    templates_dir = WORKING_DIR / args.templates
    resources_dir = WORKING_DIR / args.resources
    output_dir = WORKING_DIR / args.output

    print("[INFO] Step 2/3: Assembling handbook manuscript ...")
    manuscript = build_manuscript(records, templates_dir, resources_dir)

    build_dir = output_dir / "handbook"
    build_dir.mkdir(parents=True, exist_ok=True)
    manuscript_path = build_dir / "manuscript.md"
    manuscript_path.write_text(manuscript, encoding="utf-8")

    if args.md_only:
        print(f"[SUCCESS] Manuscript written to {manuscript_path} (--md-only, skipped PDF)")
        return

    print("[INFO] Step 3/3: Rendering PDF ...")
    pdf_dir = output_dir / "docs" / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_dir / "handbook.pdf"
    run_pandoc(manuscript_path, pdf_path, resources_dir,
               pdf_engine=args.pdf_engine, cjk_font=args.cjk_font,
               main_font=args.main_font or args.cjk_font)

    if not args.keep_md:
        manuscript_path.unlink(missing_ok=True)

    print(f"[SUCCESS] Handbook PDF written to {pdf_path}")


if __name__ == "__main__":
    main()
