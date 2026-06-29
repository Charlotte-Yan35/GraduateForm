"""只读辅助：整齐打印每个已提交学生的原始问卷数据（q1/user_id/q9 + q11·q12·q13 的
school+project），供人工编纂 program_aliases.csv / destination.csv 时阅读。

**本脚本不做任何归并/匹配决策**。它额外打印两列纯参考信息：
  - cur  = 当前管线对该录入算出的展示串（= correct.xlsx 的 B 列，用于定位 xlsx 行）
  - xlsxC= 若该展示串在 correct.xlsx 命中，则其修正后全名（C 列）

用法（cwd=site, conda base）：
  /opt/miniconda3/bin/python feiyue/_dump_raw.py            # 人类可读分人 dump
  /opt/miniconda3/bin/python feiyue/_dump_raw.py --aliases  # 输出 program_aliases 草稿(去重)
"""

import os
import sys
import csv
import requests
from dotenv import dotenv_values

import normalize

REPO_ROOT = normalize.REPO_ROOT
EXCLUDED_NAMES = {"企鹅", "tr. test"}
EXCLUDED_USER_IDS = {"85e7ddbf-f7f4-479c-81ae-d43e407072f0"}  # 重复的空 Rosie 草稿


def fetch():
    env = {**dotenv_values(REPO_ROOT / ".env"), **os.environ}
    url = env.get("VITE_SUPABASE_URL") or env.get("SUPABASE_URL")
    key = env.get("VITE_SUPABASE_ANON_KEY") or env.get("SUPABASE_ANON_KEY")
    r = requests.get(
        f"{url}/rest/v1/submissions",
        params={"status": "eq.submitted", "select": "user_id,draft,submitted_at"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def roster(rows):
    out = []
    for r in rows:
        d = r.get("draft") or {}
        nm = (d.get("q1") or "").strip()
        if nm.lower() in EXCLUDED_NAMES or r["user_id"] in EXCLUDED_USER_IDS:
            continue
        out.append(r)
    out.sort(key=lambda r: ((r.get("draft") or {}).get("q1") or "").lower())
    return out


def cur_display(sn, pn, school, project, degree_codes):
    """复现当前 _get_or_create_program 的展示串(=B)，仅供定位 xlsx 行。"""
    info = sn.normalize(school)
    if info is None:
        return None
    proj = pn.normalize(info["name"], project)
    level = normalize.derive_level(proj, degree_codes)
    label = normalize.program_label(proj, level)
    return f"{info['abbrv']} {label}".strip() if proj else info["name"]


def load_xlsx_b2c():
    try:
        import openpyxl
    except ImportError:
        return {}
    path = REPO_ROOT / "correct.xlsx"
    if not path.exists():
        return {}
    wb = openpyxl.load_workbook(path, data_only=True)
    b2c = {}
    for row in list(wb["Sheet1"].iter_rows(values_only=True))[1:]:
        if row[0] is None:
            continue
        b = (row[1] or "").strip()
        c = (row[2] or "").strip()
        if b:
            b2c[normalize._alias_key(b)] = c
    return b2c


def entries(d, q):
    return [e for e in (d.get(q) or []) if isinstance(e, dict)]


def main():
    rows = roster(fetch())
    sn, pn = normalize.SchoolNormalizer(), normalize.ProgramNormalizer()
    b2c = load_xlsx_b2c()

    if "--aliases" in sys.argv:
        seen = {}
        for r in rows:
            d = r.get("draft") or {}
            deg = d.get("q5") or []
            for q in ("q11", "q12", "q13"):
                for e in entries(d, q):
                    sch, prj = e.get("school", ""), e.get("project", "")
                    key = (normalize._alias_key(sch), normalize._alias_key(prj))
                    if key in seen:
                        continue
                    cur = cur_display(sn, pn, sch, prj, deg) or ""
                    seen[key] = (sch, prj, b2c.get(normalize._alias_key(cur), ""))
        w = csv.writer(sys.stdout)
        w.writerow(["raw_school", "raw_project", "canonical"])
        for sch, prj, c in sorted(seen.values(), key=lambda x: (x[0].lower(), x[1].lower())):
            w.writerow([sch, prj, c])
        return

    for i, r in enumerate(rows, 1):
        d = r.get("draft") or {}
        print(f"\n{'='*100}\n#{i} {d.get('q1')!r}  user_id={r['user_id']}")
        print(f"   q4={d.get('q4')!r} q5={d.get('q5')!r}")
        print(f"   q9_status={d.get('q9_status')!r}  q9_text={d.get('q9_text')!r}")
        for q, lab in (("q11", "Admit"), ("q12", "Waitlist"), ("q13", "Reject")):
            for e in entries(d, q):
                sch, prj = e.get("school", ""), e.get("project", "")
                cur = cur_display(sn, pn, sch, prj, d.get("q5") or []) or "<skip>"
                c = b2c.get(normalize._alias_key(cur), "")
                print(f"   [{lab:8s}] school={sch!r} project={prj!r}")
                print(f"              cur={cur!r}" + (f"  xlsxC={c!r}" if c else "  xlsxC=∅"))


if __name__ == "__main__":
    main()
