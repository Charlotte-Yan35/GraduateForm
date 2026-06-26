"""从 Supabase 拉取已提交问卷, 派生参考仓库所需的 4 张表 (University / Program /
Student / Application), 替代原 SeaTable 版 db.py。

每次构建都重新拉取全部 status=submitted 行 —— 未来新提交的词条无需改代码即自动并入。
所有 4 个返回值均为「以行 id 为键的 dict」, 与 docs.py 的 id 互查约定一致。
"""

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import requests
from dotenv import dotenv_values

import normalize

WORKING_DIR = Path.cwd()
CACHE_DIR = WORKING_DIR / ".cache"
REPO_ROOT = Path(__file__).resolve().parents[2]

TERM = "26Fall"
SENTINEL_PID = "p_undecided"

# 测试账户: 这些 q1 姓名代号不纳入站点(大小写/首尾空白不敏感)
EXCLUDED_NAMES = {"企鹅", "tr. test"}

MAJOR_MAP = {
    "math_2p2": "数学与应用数学 (2+2)",
    "math_4p0": "数学与应用数学 (4+0)",
    "stats": "统计",
}
DEGREE_MAP = {"phd": "博士 PhD", "master": "硕士 Master"}
FIELD_MAP = {
    "statistics": "Statistics", "data_science": "Data Science", "ml_ai": "ML/AI",
    "fin_math": "金融数学/金融统计", "pure_math": "纯数", "applied_math": "应用数学/计算数学",
    "biostat": "生物统计", "analytics": "Analytics/BA", "or": "运筹", "business": "经管类",
    "other": "其他",
}


def _hash_id(prefix: str, *parts: str) -> str:
    h = hashlib.md5("||".join(parts).encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{h}"


def save_to_cache(filename: str, data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    with open(CACHE_DIR / filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_from_cache(filename: str) -> dict:
    with open(CACHE_DIR / filename, encoding="utf-8") as f:
        return json.load(f)


def _fetch_submitted() -> list[dict]:
    env = {**dotenv_values(REPO_ROOT / ".env"), **os.environ}
    url = env.get("VITE_SUPABASE_URL") or env.get("SUPABASE_URL")
    key = env.get("VITE_SUPABASE_ANON_KEY") or env.get("SUPABASE_ANON_KEY")
    if not url or not key:
        raise Exception("Missing Supabase credentials (VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY)")
    resp = requests.get(
        f"{url}/rest/v1/submissions",
        params={"status": "eq.submitted", "select": "id,user_id,draft,submitted_at"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise Exception(f"Supabase fetch failed: {resp.status_code} {resp.text}")
    return resp.json()


# ---- 派生辅助 ----

def _parse_destination(q9_text: str) -> tuple[str, str]:
    """把 q9_text 拆成 (school, project)。形如 'School（Project）' 或 'School (Project)'。"""
    s = (q9_text or "").strip()
    if not s:
        return "", ""
    m = re.match(r"^(.*?)[（(]\s*(.+?)\s*[)）]\s*$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return s, ""


def _entries(value) -> list[dict]:
    """规整 q11/q12/q13/q16/q17 的值为 dict 列表(忽略空/字符串遗留项)。"""
    if not isinstance(value, list):
        return []
    return [e for e in value if isinstance(e, dict)]


class Deriver:
    def __init__(self):
        self.norm = normalize.SchoolNormalizer()
        self.prognorm = normalize.ProgramNormalizer()
        self.universities: dict = {}
        self.programs: dict = {}
        self.students: dict = {}
        self.applications: dict = {}
        self.program_univ: dict = {}  # p_id -> {name, abbrv, region}(内部用, 不入表)
        self.skipped_schools = 0
        self.skipped_tests = 0
        self.unresolved_dest = 0
        # 哨兵项目: 去向未定。放入 programs 但不挂到任何 University。
        self.programs[SENTINEL_PID] = {
            "_id": SENTINEL_PID, "p_id": SENTINEL_PID,
            "abbrv": "待定 / Undecided", "level": "", "name": "尚未确定最终去向",
        }

    def _get_or_create_program(self, raw_school: str, raw_project: str, degree_codes: list) -> str | None:
        info = self.norm.normalize(raw_school)
        if info is None:
            self.skipped_schools += 1
            return None
        project = self.prognorm.normalize(info["name"], raw_project)
        u_id = _hash_id("u", info["name"])
        p_id = _hash_id("p", info["name"], project)

        # University
        if u_id not in self.universities:
            self.universities[u_id] = {
                "_id": u_id, "name": info["name"], "abbrv": info["abbrv"],
                "region": info["region"], "programs": [],
            }
        # Program
        if p_id not in self.programs:
            level = normalize.derive_level(project, degree_codes)
            abbrv = f"{info['abbrv']} {project}".strip() if project else info["name"]
            self.programs[p_id] = {
                "_id": p_id, "p_id": p_id, "abbrv": abbrv,
                "level": level, "name": project or "—",
            }
            self.program_univ[p_id] = info
            self.universities[u_id]["programs"].append(
                {"row_id": p_id, "display_value": abbrv}
            )
        return p_id

    def _resolve_destination(self, d: dict, app_refs: list, degree_codes: list):
        """返回 (p_id, mark_chosen) 或 (None, False)。优先把去向匹配到学生自己的 Admit
        项目(复用并标 Chosen);否则尝试规范化新建;再不行回退 None(→哨兵)。"""
        q9 = (d.get("q9_text") or "").strip()
        if not q9:
            return None, False
        key = normalize._alias_key(q9)

        # 1) 特征词匹配到某条 Admit 记录的院校
        for ref in app_refs:
            app = self.applications[ref["row_id"]]
            p_id = app["program"][0]["row_id"]
            info = self.program_univ.get(p_id)
            if not info:
                continue
            if any(tok in key for tok in normalize.signature_tokens(info["name"], info["abbrv"])):
                return p_id, True

        # 2) 解析 school 部分并尝试规范化(走 school_map / universities.js)
        dschool, dproject = _parse_destination(q9)
        if dschool:
            info = self.norm.normalize(dschool)
            if info and info["region"] != normalize.OTHER_REGION:
                # 已知院校 → 建去向项目
                p_id = self._get_or_create_program(dschool, dproject, degree_codes)
                if p_id:
                    return p_id, True
        # 3) 无法解析: 计入待审核(已由 normalize 自动 flush), 回退哨兵
        self.unresolved_dest += 1
        return None, False

    def _build_contact(self, d: dict) -> str:
        """联系方式: 仅 q2='1'(公开) 才展示, 与 q15(经历意愿)无关。"""
        if d.get("q2") != "1":
            return ""
        cs = []
        if d.get("q3_email"): cs.append(f"邮箱 {d['q3_email']}")
        if d.get("q3_wechat"): cs.append(f"微信 {d['q3_wechat']}")
        if d.get("q3_other"): cs.append(d["q3_other"])
        return " · ".join(cs)

    def _build_experience(self, d: dict) -> str:
        if d.get("q15") != "willing":
            return ""
        parts = []
        sci = _entries(d.get("q16"))
        if sci:
            lines = ["## 科研 / 项目经历"]
            for e in sci:
                head = " · ".join(x for x in [e.get("time"), e.get("institution"), e.get("title")] if x)
                meta = " · ".join(x for x in [e.get("advisor"), e.get("duration")] if x)
                lines.append(f"- **{head}**" + (f"（{meta}）" if meta else ""))
                body = " ".join(x for x in [e.get("content"), e.get("output")] if x)
                if body:
                    lines.append(f"    {body}")
            parts.append("\n".join(lines))

        intern = _entries(d.get("q17"))
        if intern:
            lines = ["## 实习经历"]
            for e in intern:
                head = " · ".join(x for x in [e.get("time"), e.get("company"), e.get("duration")] if x)
                lines.append(f"- **{head}**")
                if e.get("content"):
                    lines.append(f"    {e['content']}")
            parts.append("\n".join(lines))

        if d.get("q19"):
            parts.append(f"## 推荐信\n\n{d['q19']}")
        if d.get("q20"):
            parts.append(f"## 荣誉奖项\n\n{d['q20']}")
        return "\n\n".join(parts)

    def _build_sharing(self, d: dict) -> str:
        parts = []
        q21 = d.get("q21")
        if q21 in ("full", "half") and d.get("q22") == "yes" and d.get("q23"):
            parts.append(f"## 中介分享\n\n{d['q23']}")
        if q21 == "diy" and d.get("q24") == "yes" and d.get("q25"):
            parts.append(f"## DIY 分享\n\n{d['q25']}")
        if d.get("q26"):
            parts.append(f"## 申请经验心得\n\n{d['q26']}")
        return "\n\n".join(parts)

    def add_student(self, row: dict) -> None:
        d = row.get("draft") or {}
        if (d.get("q1") or "").strip().lower() in EXCLUDED_NAMES:
            self.skipped_tests += 1
            return
        s_id = _hash_id("s", row["user_id"])
        degree_codes = d.get("q5") or []

        # 申请记录: q11=Admit, q12=Waitlist, q13=Reject
        app_refs = []
        for q, result in (("q11", "Admit"), ("q12", "Waitlist"), ("q13", "Reject")):
            for i, e in enumerate(_entries(d.get(q))):
                p_id = self._get_or_create_program(e.get("school", ""), e.get("project", ""), degree_codes)
                if p_id is None:
                    continue
                a_id = _hash_id("a", s_id, q, str(i), p_id)
                note = " / ".join(x for x in [
                    f"Cond {e['cond']}" if e.get("cond") else "",
                    f"奖 {e['scholarship']}" if e.get("scholarship") else "",
                    e.get("note", ""),
                ] if x)
                self.applications[a_id] = {
                    "_id": a_id, "result": result,
                    "submit_date": e.get("submitTime", "") or "",
                    "result_date": e.get("receiveTime", "") or "",
                    "note": note,
                    "program": [{"row_id": p_id}],
                    "student": [{"row_id": s_id}],
                }
                app_refs.append({"row_id": a_id})

        # 最终去向 program_choice (恒存在)
        dest_pid, mark_chosen = (None, False)
        if d.get("q9_status") == "decided":
            dest_pid, mark_chosen = self._resolve_destination(d, app_refs, degree_codes)

        if dest_pid:
            if mark_chosen:
                for ref in app_refs:
                    app = self.applications[ref["row_id"]]
                    if app["program"][0]["row_id"] == dest_pid and app["result"] == "Admit":
                        app["result"] = "Chosen"
                        break
            choice = {"row_id": dest_pid, "display_value": self.programs[dest_pid]["abbrv"]}
        elif d.get("q9_status") == "decided" and (d.get("q9_text") or "").strip():
            # 已定但无法解析院校: 显示原文, 链接回退哨兵页(待 school_map 补全后自动归位)
            choice = {"row_id": SENTINEL_PID, "display_value": d["q9_text"].strip()}
        else:
            choice = {"row_id": SENTINEL_PID, "display_value": "待定 / Undecided"}

        prefer = [FIELD_MAP.get(x, x) for x in (d.get("q6") or [])]
        if d.get("q6_other_text"):
            prefer = [p for p in prefer if p != "其他"] + [d["q6_other_text"]]
        gpa = d.get("q10_gpa_pct", "") or ""
        if d.get("q10_gpa_4"):
            gpa = f"{gpa} / GPA {d['q10_gpa_4']}".strip(" /")

        self.students[s_id] = {
            "_id": s_id, "s_id": s_id, "name": d.get("q1") or "匿名",
            "term": TERM,
            "major": MAJOR_MAP.get(d.get("q4"), d.get("q4") or ""),
            "apply_degree": "、".join(DEGREE_MAP.get(x, x) for x in degree_codes),
            "prefer_field": "、".join(prefer),
            "gpa": gpa,
            "lang": d.get("q10_language", "") or "",
            "gre": d.get("q10_gre", "") or "",
            "program_choice": [choice],
            "applications": app_refs,
            "contact": self._build_contact(d),
            "experience": self._build_experience(d),
            "sharing": self._build_sharing(d),
        }


def get_records(source: str = "cloud") -> tuple[list[dict], dict]:
    if source == "cache" and CACHE_DIR.exists():
        try:
            recs = [load_from_cache(n) for n in
                    ("University.json", "Program.json", "Student.json", "Application.json")]
            print(f"Loaded from cache: {len(recs[2])} students, {len(recs[1])} programs")
            return recs, {}
        except (FileNotFoundError, json.JSONDecodeError):
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
            print("Cache corrupted, fetching from cloud")

    rows = _fetch_submitted()
    print(f"Fetched {len(rows)} submitted entries from Supabase")

    deriver = Deriver()
    for row in rows:
        deriver.add_student(row)

    # 按项目缩写排序各 University.programs (docs.pre_build 也会排, 这里预排稳妥)
    for u in deriver.universities.values():
        u["programs"].sort(key=lambda p: p["display_value"])

    print(f"Derived: {len(deriver.students)} students, "
          f"{len(deriver.universities)} universities, "
          f"{len([p for p in deriver.programs if p != SENTINEL_PID])} programs, "
          f"{len(deriver.applications)} applications "
          f"(skipped {deriver.skipped_schools} entries with unusable school, "
          f"{deriver.skipped_tests} test account row(s))")

    save_to_cache("University.json", deriver.universities)
    save_to_cache("Program.json", deriver.programs)
    save_to_cache("Student.json", deriver.students)
    save_to_cache("Application.json", deriver.applications)

    n = deriver.norm.flush_unmatched()
    if n:
        print(f"[ACTION] {n} new school name(s) need review in site/data/school_map.csv")
    np = deriver.prognorm.flush_unmatched()
    if np:
        print(f"[ACTION] {np} new program name(s) flushed to site/data/program_map.csv for optional merge review")

    return [deriver.universities, deriver.programs, deriver.students, deriver.applications], {}
