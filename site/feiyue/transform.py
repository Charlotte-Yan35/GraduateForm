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
# 重复账户: 按 user_id 屏蔽(空 Rosie 草稿, 与真 Rosie 9437367d 同名)
EXCLUDED_USER_IDS = {"85e7ddbf-f7f4-479c-81ae-d43e407072f0"}

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

def _entries(value) -> list[dict]:
    """规整 q11/q12/q13/q16/q17 的值为 dict 列表(忽略空/字符串遗留项)。"""
    if not isinstance(value, list):
        return []
    return [e for e in value if isinstance(e, dict)]


# 纯"无值"话术: 整串完全等于其一(去空白、忽略大小写)时归一为空白。
# 仅清纯无值词; 含真实信息的(如 "无标化语言"/"无G"/"waive")保留。
_NO_VALUE = {
    "无", "没有", "暂无", "暂未", "暂时没有", "空", "空白", "无。", "无.",
    "n/a", "na", "n.a.", "n/a.", "none", "null", "nil",
    "/", "／", "-", "--", "—", "——", "、", "。", ".", "·",
}


def _blank_if_none(value: str) -> str:
    """把'描述没有'的纯无值话术(无/N/A/没有/空/-/暂无 等整串)归一为空白。
    只在整串完全等于无值词时清空, 不动含真实信息的文本(如 '无标化语言')。"""
    s = (value or "").strip()
    return "" if s.lower() in _NO_VALUE else s


def _md_multiline(text: str) -> str:
    """把用户文本框里的换行忠实还原到 Markdown:
    空行 -> 段落分隔; 段内单换行 -> 硬换行(行尾两空格 = <br>)。
    Markdown 默认把单个 \\n 当空格, 不处理会把多行散文挤成一段。"""
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return ""
    paras = re.split(r"\n[ \t]*\n", s)  # 空行分段
    return "\n\n".join(
        "  \n".join(ln.rstrip() for ln in p.split("\n"))  # 段内单换行 -> 硬换行
        for p in paras
    )


class Deriver:
    def __init__(self):
        self.norm = normalize.SchoolNormalizer()
        self.prognorm = normalize.ProgramNormalizer()
        self.destinations = normalize.load_destinations()  # user_id -> {canonical, school}
        self.universities: dict = {}
        self.programs: dict = {}
        self.students: dict = {}
        self.applications: dict = {}
        self.skipped_schools = 0
        self.skipped_tests = 0
        self.unresolved_dest = 0
        # 哨兵项目: 去向未定。放入 programs 但不挂到任何 University。
        self.programs[SENTINEL_PID] = {
            "_id": SENTINEL_PID, "p_id": SENTINEL_PID,
            "abbrv": "待定 / Undecided", "label": "待定 / Undecided",
            "level": "", "name": "尚未确定最终去向",
        }

    def _ensure_program_node(self, info: dict, canonical: str, degree_codes: list) -> str:
        """以 canonical 全名为 identity 建/取 program 节点, 挂到 info 对应的 University。
        canonical 即展示名(program.abbrv); 相同 canonical → 同一节点(正确合并)。
        先做确定性清洗(压缩空白/统一撇号), 避免仅不可见字节不同的写法分裂成两个节点。"""
        canonical = normalize.clean_canonical(canonical)
        u_id = _hash_id("u", info["name"])
        p_id = _hash_id("p", canonical)
        if u_id not in self.universities:
            self.universities[u_id] = {
                "_id": u_id, "name": info["name"], "abbrv": info["abbrv"],
                "region": info["region"], "programs": [],
            }
        if p_id not in self.programs:
            level = normalize.derive_level(canonical, degree_codes)
            self.programs[p_id] = {
                "_id": p_id, "p_id": p_id, "abbrv": canonical,
                "label": canonical, "level": level, "name": canonical,
            }
            self.universities[u_id]["programs"].append(
                {"row_id": p_id, "display_value": canonical}
            )
        return p_id

    def _get_or_create_program(self, raw_school: str, raw_project: str, degree_codes: list) -> str | None:
        info = self.norm.normalize(raw_school)
        if info is None:
            self.skipped_schools += 1
            return None
        canonical = self.prognorm.lookup(raw_school, raw_project)
        if canonical == "-":  # 人工标记: 无效条目(如只填学校未填项目), 丢弃, 不建 program 节点
            return None
        if canonical is None:
            # 未编纂(新提交): 回退旧式 school_abbrv + 项目 拼接, 不阻断构建; 已记待补全
            project = normalize.normalize_project(raw_project)
            if project:
                level = normalize.derive_level(project, degree_codes)
                label = normalize.program_label(project, level)
                canonical = f"{info['abbrv']} {label}".strip()
            else:
                canonical = info["name"]
        return self._ensure_program_node(info, canonical, degree_codes)

    def _resolve_destination(self, user_id: str, degree_codes: list) -> str | None:
        """返回去向 program p_id 或 None(→哨兵)。完全靠人工编纂的 destination.csv:
        chosen_canonical 已写定; 节点若不存在则按 chosen_school 建。不做任何启发式。"""
        entry = self.destinations.get(user_id)
        if not entry:
            self.unresolved_dest += 1
            return None
        canonical = normalize.clean_canonical(entry["canonical"])  # 与 _ensure_program_node 同一清洗, 保证 p_id 一致
        p_id = _hash_id("p", canonical)
        if p_id not in self.programs:
            info = self.norm.normalize(entry["school"])
            if info is None:
                self.unresolved_dest += 1
                return None
            self._ensure_program_node(info, canonical, degree_codes)
        return p_id

    def _build_contact(self, d: dict) -> str:
        """联系方式: 仅 q2='1'(公开) 才展示, 与 q15(经历意愿)无关。"""
        if d.get("q2") != "1":
            return ""
        cs = []
        email, wechat, other = (_blank_if_none(d.get(k)) for k in ("q3_email", "q3_wechat", "q3_other"))
        if email: cs.append(f"邮箱 {email}")
        if wechat: cs.append(f"微信 {wechat}")
        if other: cs.append(other)
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
                body = " ".join(x for x in [_blank_if_none(e.get("content")), _blank_if_none(e.get("output"))] if x)
                if body:
                    lines.append(f"    {body}")
            parts.append("\n".join(lines))

        intern = _entries(d.get("q17"))
        if intern:
            lines = ["## 实习经历"]
            for e in intern:
                head = " · ".join(x for x in [e.get("time"), e.get("company"), e.get("duration")] if x)
                lines.append(f"- **{head}**")
                content = _blank_if_none(e.get("content"))
                if content:
                    lines.append(f"    {content}")
            parts.append("\n".join(lines))

        q19, q20 = _blank_if_none(d.get("q19")), _blank_if_none(d.get("q20"))
        if q19:
            parts.append(f"## 推荐信\n\n{_md_multiline(q19)}")
        if q20:
            parts.append(f"## 荣誉奖项\n\n{_md_multiline(q20)}")
        return "\n\n".join(parts)

    def _build_sharing(self, d: dict) -> str:
        parts = []
        q21 = d.get("q21")
        q23, q25, q26 = (_blank_if_none(d.get(k)) for k in ("q23", "q25", "q26"))
        if q21 in ("full", "half") and d.get("q22") == "yes" and q23:
            parts.append(f"## 中介分享\n\n{_md_multiline(q23)}")
        if q21 == "diy" and d.get("q24") == "yes" and q25:
            parts.append(f"## DIY 分享\n\n{_md_multiline(q25)}")
        if q26:
            parts.append(f"## 申请经验心得\n\n{_md_multiline(q26)}")
        return "\n\n".join(parts)

    def add_student(self, row: dict) -> None:
        d = row.get("draft") or {}
        if (d.get("q1") or "").strip().lower() in EXCLUDED_NAMES \
                or row.get("user_id") in EXCLUDED_USER_IDS:
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
                cond, schol, enote = (_blank_if_none(e.get(k)) for k in ("cond", "scholarship", "note"))
                note = " / ".join(x for x in [
                    f"Cond {cond}" if cond else "",
                    f"奖 {schol}" if schol else "",
                    enote,
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
        dest_pid = None
        if d.get("q9_status") == "decided":
            dest_pid = self._resolve_destination(row["user_id"], degree_codes)

        if dest_pid:
            # 去向恒作为 Chosen 进入「申请结果」: 已有同项目记录则升级(优先 Admit),
            # 否则新建一条 Chosen 行 —— 故申请结果 = 去向 + admit + waitlist + reject。
            same = [ref for ref in app_refs
                    if self.applications[ref["row_id"]]["program"][0]["row_id"] == dest_pid]
            if same:
                same.sort(key=lambda ref: self.applications[ref["row_id"]]["result"] != "Admit")
                self.applications[same[0]["row_id"]]["result"] = "Chosen"
            else:
                a_id = _hash_id("a", s_id, "dest", dest_pid)
                self.applications[a_id] = {
                    "_id": a_id, "result": "Chosen",
                    "submit_date": "", "result_date": "", "note": "最终去向",
                    "program": [{"row_id": dest_pid}],
                    "student": [{"row_id": s_id}],
                }
                app_refs.append({"row_id": a_id})
            choice = {"row_id": dest_pid, "display_value": self.programs[dest_pid]["abbrv"]}
        elif d.get("q9_status") == "decided" and (d.get("q9_text") or "").strip():
            # 已定但无法解析院校: 显示原文, 链接回退哨兵页(待 school_map 补全后自动归位)
            choice = {"row_id": SENTINEL_PID, "display_value": d["q9_text"].strip()}
        else:
            choice = {"row_id": SENTINEL_PID, "display_value": "待定 / Undecided"}

        prefer = [FIELD_MAP.get(x, x) for x in (d.get("q6") or [])]
        q6_other = _blank_if_none(d.get("q6_other_text"))
        if q6_other:
            prefer = [p for p in prefer if p != "其他"] + [q6_other]
        gpa = _blank_if_none(d.get("q10_gpa_pct"))    # 百分制三年均分
        gpa4 = _blank_if_none(d.get("q10_gpa_4"))     # 4.0 制 GPA

        self.students[s_id] = {
            "_id": s_id, "s_id": s_id, "name": d.get("q1") or "匿名",
            "term": TERM,
            "major": MAJOR_MAP.get(d.get("q4"), d.get("q4") or ""),
            "apply_degree": "、".join(DEGREE_MAP.get(x, x) for x in degree_codes),
            "prefer_field": "、".join(prefer),
            "gpa": gpa,
            "gpa4": gpa4,
            "lang": _blank_if_none(d.get("q10_language")),
            "gre": _blank_if_none(d.get("q10_gre")),
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
        print(f"[ACTION] {np} new program entry(ies) flushed to site/data/program_aliases.csv — author canonical name(s)")
    if deriver.unresolved_dest:
        print(f"[ACTION] {deriver.unresolved_dest} decided destination(s) not in destination.csv — author chosen_canonical")

    return [deriver.universities, deriver.programs, deriver.students, deriver.applications], {}
