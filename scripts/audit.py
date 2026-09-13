#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skill-create-debug-dsh / scripts/audit.py
DSH skill 体检器 —— 合规性 + 质量 + 成本 + 冲突

用法:
    python audit.py                      # 体检全部 skill
    python audit.py --skill <name>       # 体检单个
    python audit.py --roots <dir> [...]  # 指定 skill 根目录
    python audit.py --json               # 机器可读输出

退出码: 0 = 无错误(可有警告); 1 = 存在错误
"""
import io, json, os, re, sys, argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------- DSH 规则常量 ----------
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DESC_CATALOG_MAX = 500      # 目录中 description 的截断上限
BODY_SOFT_MAX = 360         # 强烈建议的正文行数上限（创建/调试 skill）；DSH 不强制
BODY_SUGGEST_SPLIT = 250    # 超过此值且无 references/ 时建议拆分
DESC_MIN_USEFUL = 40        # 低于此值几乎不可能可靠触发

# DSH 只识别这些 frontmatter 字段；其余会被静默忽略（不报错，但完全无效）
KNOWN_FIELDS = {"name", "description", "whenToUse", "metadata",
                "disable-model-invocation", "user-invocable"}

# ---------- W7 排斥性声明检测 ----------
# 第三方技能（尤其随程序包分发的）常在自己的正文里宣称"我是首选""不要用别的工具"。
# 那类话是写给"没有内置工具的通用 agent"的；在 DSH 上，内置工具已存在，
# 这个前提不成立，而模型分不清"厂商自我定位"与"用户策略"。
# 判据：同一处上下文里既有「优先级/禁止」措辞，又点了「工具」。
EXCL_PREF = re.compile(
    r"(preferred\s+(?:path|way|route|method)"
    r"|use\s+them\s+directly"
    r"|before\s+falling\s+back"
    r"|fall\s+back\s+to"
    r"|do\s+not\s+reach\s+for"
    r"|do\s+not\s+use"
    r"|never\s+use"
    r"|instead\s+of\s+(?:using|the)"
    r"|first-party"
    r"|首选|优先(?:使用|选用|采用|走)"
    r"|禁止(?:使用|用)|不要(?:使用|用)|不得(?:使用|用))",
    re.I)
EXCL_TOOL = re.compile(
    r"(web_search|web_fetch|WebSearch|WebFetch|built-?in|内置"
    r"|curl|r\.jina\.ai|fetch"
    r"|其他\s*(?:工具|技能|skill)|别的\s*(?:工具|技能)"
    r"|other\s+tools?|other\s+skills?|raw\s+`?\w+)",
    re.I)
EXCL_WINDOW = 140   # 前后各取多少字符判定"同一处上下文"


def find_exclusion_claims(body):
    """返回 [(片段, 命中词)] —— 疑似"排斥其他工具"的声明。

    只扫散文：跳过 fenced code block 与表格行 —— 那些位置多是在**引用/举例**
    这类声明（例如讲 W7 自己的文档），而不是技能自己在主张优先。
    """
    if not body:
        return []
    text = re.sub(r"```.*?```", " ", body, flags=re.S)          # 去代码块
    text = "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith("|"))         # 去表格行
    flat = re.sub(r"\s+", " ", text)
    hits, seen = [], set()
    for m in EXCL_PREF.finditer(flat):
        lo, hi = max(0, m.start() - EXCL_WINDOW), min(len(flat), m.end() + EXCL_WINDOW)
        win = flat[lo:hi]
        t = EXCL_TOOL.search(win)
        if not t:
            continue
        key = (m.group(0).lower(), t.group(0).lower())
        if key in seen:
            continue
        seen.add(key)
        hits.append((win.strip(), "%s + %s" % (m.group(0), t.group(0))))
    return hits

# rank 表（DSH 官方）：rank 越小优先级越高
RANK_ROOTS = [
    (100, "project-dsh",    os.path.join(".dsh", "skills")),
    (200, "project-agents", os.path.join(".agents", "skills")),
    (400, "user-dsh",       os.path.join(os.path.expanduser("~"), ".dsh", "skills")),
    (500, "user-agents",    os.path.join(os.path.expanduser("~"), ".agents", "skills")),
]


def _try_yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        return None


def split_frontmatter(text):
    """返回 (yaml_text, body) 或 (None, text)。"""
    if not text.startswith("---"):
        return None, text
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.S)
    if not m:
        return None, text
    return m.group(1), m.group(2)


def parse_fm(yaml_text):
    """优先 PyYAML；失败则用最小子集解析（单行 key: value）。"""
    if yaml_text is None:
        return None, "无 frontmatter"
    y = _try_yaml()
    if y is not None:
        try:
            d = y.safe_load(yaml_text)
            if isinstance(d, dict):
                return d, None
            return None, "frontmatter 不是 YAML 映射"
        except Exception as e:
            return None, "YAML 解析失败: %s" % e
    # 回退：只取单行标量
    d = {}
    for line in yaml_text.splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            v = m.group(2).strip().strip("'\"")
            if v and not v.startswith(("|", ">")):
                d[m.group(1)] = v
    return d, None


def find_skill_roots(extra=None):
    roots = []
    if extra:
        for r in extra:
            roots.append((300, "custom", os.path.abspath(r)))
        return roots
    # 项目根 = 最近的含 .git 的祖先，否则 cwd
    cur = os.path.abspath(os.getcwd())
    proj = cur
    while True:
        if os.path.isdir(os.path.join(proj, ".git")):
            break
        parent = os.path.dirname(proj)
        if parent == proj:
            proj = cur
            break
        proj = parent
    for rank, source, rel in RANK_ROOTS:
        if source.startswith("project-"):
            p = os.path.join(proj, rel)
        else:
            p = rel
        roots.append((rank, source, p))
    # 去重保序
    seen, out = set(), []
    for r in roots:
        if r[2] not in seen:
            seen.add(r[2]); out.append(r)
    return out


def discover(roots):
    """返回 [(name_dir, skill_md_path, rank, source, root)]"""
    out = []
    for rank, source, root in roots:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            d = os.path.join(root, entry)
            if os.path.isdir(d):
                md = os.path.join(d, "SKILL.md")
                if os.path.isfile(md):
                    out.append((entry, md, rank, source, root))
            elif entry.lower().endswith(".md"):
                out.append((entry[:-3], os.path.join(root, entry), rank, source, root))
    return out


def extract_triggers(desc):
    """从 description 中抽取引号内的触发词（中英文引号）。"""
    if not desc:
        return []
    pats = [r'"([^"]{2,40})"', r"'([^']{2,40})'", r"「([^」]{2,40})」",
            r"“([^”]{2,40})”", r"『([^』]{2,40})』", r"`([^`]{2,40})`"]
    found = []
    for p in pats:
        found += re.findall(p, desc)
    # 去重保序
    seen, out = set(), []
    for f in found:
        f = f.strip()
        if f and f not in seen:
            seen.add(f); out.append(f)
    return out


def estimate_tokens(text):
    """粗估 token：CJK 约 1 字 1 token，其他按 4 字符 1 token。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    other = len(text) - cjk
    return int(cjk + other / 4)


def audit_one(name_dir, md_path, rank, source, root=None):
    r = {"dir": name_dir, "path": md_path, "rank": rank, "source": source, "root": root,
         "errors": [], "warnings": [], "info": []}
    d = os.path.dirname(md_path)

    try:
        text = io.open(md_path, encoding="utf-8").read()
    except Exception as e:
        r["errors"].append("E1 SKILL.md 无法读取: %s" % e)
        return r

    r["lines"] = len(text.splitlines())
    yaml_text, body = split_frontmatter(text)
    if yaml_text is None:
        r["errors"].append("E2 缺少 YAML frontmatter（文件必须以 --- 开头）")
        fm, err = None, None
    else:
        fm, err = parse_fm(yaml_text)
        if fm is None:
            r["errors"].append("E2 frontmatter 解析失败: %s" % err)
    fm = fm or {}

    # E3 name
    nm = fm.get("name")
    if not nm or not isinstance(nm, str):
        r["errors"].append("E3 缺少必填字段 name")
    else:
        r["name"] = nm
        if not NAME_RE.match(nm):
            r["errors"].append(
                "E3 name 非法: %r —— DSH 要求全小写 kebab-case ^[a-z0-9]+(-[a-z0-9]+)*$，"
                "违规会导致整个 skill 被静默丢弃" % nm)
        if nm != name_dir:
            r["info"].append(
                "I0 name(%r) 与目录名(%r) 不一致。这在 DSH 中合法——发现用目录名，"
                "身份由 frontmatter 的 name 决定。仅提示：改 name 会破坏既有引用" % (nm, name_dir))

    # E4 description
    desc = fm.get("description")
    if not desc or not isinstance(desc, str) or not desc.strip():
        r["errors"].append("E4 缺少必填字段 description —— 它是唯一的触发机制")
        desc = ""
    desc = desc.strip()
    r["desc_len"] = len(desc)
    if desc:
        if len(desc) > DESC_CATALOG_MAX:
            r["warnings"].append(
                "W1 description %d 字符 > %d，目录中会被截断，尾部内容模型看不到"
                % (len(desc), DESC_CATALOG_MAX))
        if len(desc) < DESC_MIN_USEFUL:
            r["warnings"].append("W1 description 仅 %d 字符，几乎不可能可靠触发" % len(desc))

        triggers = extract_triggers(desc)
        r["triggers"] = triggers
        if not triggers:
            r["warnings"].append(
                "W2 description 没有列举具体触发词。建议用引号列出用户真会说的原话，"
                "模型天然欠触发，明确的触发词能显著提升命中率")
        has_when = bool(re.search(r"(当|只要|如果|如果用户|when|whenever|use (this|when)|trigger)", desc, re.I))
        if not has_when:
            r["warnings"].append(
                "W2 description 只说了做什么，没说什么时候用。所有'何时使用'的信息"
                "必须写在 description 里（写进 whenToUse 无效）")

    # W3 whenToUse 陷阱
    if fm.get("whenToUse"):
        r["warnings"].append(
            "W3 使用了 whenToUse 字段。该字段不进入会话目录，对触发决策毫无作用 —— "
            "请把内容移入 description")

    # W6 未知字段（DSH 静默忽略，不报错）
    unknown = sorted(k for k in fm if k not in KNOWN_FIELDS)
    if unknown:
        r["warnings"].append(
            "W6 未知 frontmatter 字段: %s —— DSH 只识别 %s，其余被静默忽略"
            "（不报错，但完全无效，等于白写）"
            % (", ".join(unknown), ", ".join(sorted(KNOWN_FIELDS))))

    # W7 排斥性声明（第三方技能常自称"首选"、要求别用其他工具）
    excl = find_exclusion_claims(body)
    r["exclusions"] = [h[1] for h in excl]
    if excl:
        r["warnings"].append(
            "W7 正文存在排斥性声明 %d 处（%s）。这类\"我是首选 / 不要用别的工具\"多见于"
            "随程序包分发的第三方技能，是写给**没有内置工具的通用 agent** 的；"
            "在 DSH 上内置工具已存在，该前提不成立，而模型分不清\"厂商自我定位\"与\"用户策略\"。"
            "请核对它与你的实际策略是否一致；不一致就在 $DSH_HOME/AGENTS.md 里写明路由规则覆盖它"
            % (len(excl), "; ".join(h[1] for h in excl[:3])))

    # 正文行数与结构
    body_lines = len(body.splitlines()) if body else 0
    r["body_lines"] = body_lines
    subs = [s for s in ("scripts", "references", "assets") if os.path.isdir(os.path.join(d, s))]
    r["subdirs"] = subs
    if body_lines > BODY_SOFT_MAX:
        r["warnings"].append(
            "W4 正文 %d 行，超过强烈建议值 %d 行。DSH 不检查行数，不影响加载；"
            "但正文每次触发都进上下文，超出会明显推高成本，强烈建议把细节移入 references/"
            % (body_lines, BODY_SOFT_MAX))
    elif body_lines > BODY_SUGGEST_SPLIT and "references" not in subs:
        r["info"].append(
            "I1 正文 %d 行且无 references/ 目录。可考虑把按需阅读的细节外置以降低常驻成本"
            % body_lines)

    # 成本估算
    r["tok_desc"] = estimate_tokens(desc)
    r["tok_body"] = estimate_tokens(body or "")
    r["tok_boot"] = r["tok_desc"] + estimate_tokens(nm or "") + 8  # 目录条目开销

    # 调用策略
    if fm.get("disable-model-invocation") in (True, "true", "yes", "on", "1"):
        r["info"].append("I2 模型不可调用（用户专属 /<name>）—— 模型不会自动触发它")
    if fm.get("user-invocable") in (False, "false", "no", "off", "0"):
        r["info"].append("I2 用户不可调用（仅模型自动触发）")

    # 发现模拟：DSH 会不会接受这个 skill
    # 任何 E 级问题都会导致它被静默丢弃；W 级不影响加载
    r["discoverable"] = (len(r["errors"]) == 0)
    return r


def cross_check(rows):
    """跨 skill 检查：触发词重叠 + 目录名冲突。"""
    findings = []
    tmap = {}
    for r in rows:
        for t in r.get("triggers", []):
            tmap.setdefault(t.lower(), []).append(r.get("name") or r["dir"])
    for t, owners in sorted(tmap.items()):
        uniq = sorted(set(owners))
        if len(uniq) > 1:
            findings.append(("W5", "触发词 %r 被多个 skill 使用: %s —— 会互相争夺触发，"
                                   "建议各自加边界说明" % (t, ", ".join(uniq))))
    # name 冲突
    nmap = {}
    for r in rows:
        nm = r.get("name")
        if nm:
            nmap.setdefault(nm, []).append(r)
    for nm, rs in nmap.items():
        if len(rs) > 1:
            findings.append(("E5", "name %r 在 %d 处重复定义: %s —— DSH 会按 rank 优先级裁决，"
                                   "低优先级者被静默遮蔽"
                                   % (nm, len(rs), ", ".join(x["path"] for x in rs))))
    return findings


def main():
    ap = argparse.ArgumentParser(description="DSH skill 体检器")
    ap.add_argument("--skill", help="只体检指定 skill（name 或目录名）")
    ap.add_argument("--roots", nargs="*", help="自定义 skill 根目录")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    roots = find_skill_roots(args.roots)
    found = discover(roots)

    if args.skill:
        s = args.skill.lower()
        found = [f for f in found if f[0].lower() == s or f[0].lower().replace("_", "-") == s]
        if not found:
            print("未找到 skill: %s" % args.skill, file=sys.stderr)
            print("已扫描根目录:", file=sys.stderr)
            for rank, src, p in roots:
                print("  [%d] %-16s %s%s" % (rank, src, p, "" if os.path.isdir(p) else "  (不存在)"),
                      file=sys.stderr)
            return 1

    rows = [audit_one(*f) for f in found]
    cross = cross_check(rows)
    for code, msg in cross:
        rows.append({"dir": "(跨 skill)", "errors": [code + " " + msg] if code.startswith("E") else [],
                     "warnings": [code + " " + msg] if code.startswith("W") else [], "info": []})

    n_err = sum(len(r["errors"]) for r in rows)
    n_warn = sum(len(r["warnings"]) for r in rows)

    if args.json:
        print(json.dumps({"roots": [{"rank": a, "source": b, "path": c} for a, b, c in roots],
                          "results": rows,
                          "summary": {"skills": len(found), "errors": n_err, "warnings": n_warn}},
                         ensure_ascii=False, indent=2))
        return 1 if n_err else 0

    # ---- 文本报告 ----
    print("=" * 72)
    print("DSH Skill 体检报告")
    print("=" * 72)
    print("扫描的根目录（按 rank 优先级）:")
    for rank, src, p in roots:
        print("  [%3d] %-16s %s%s" % (rank, src, p, "" if os.path.isdir(p) else "   (不存在)"))
    print("\n发现 %d 个 skill\n" % len(found))

    for r in rows:
        if r["dir"] == "(跨 skill)":
            print("-" * 72)
            for m in r["errors"] + r["warnings"]:
                print("  %s" % m)
            print()
            continue
        nm = r.get("name") or r["dir"]
        flags = []
        if r["errors"]:
            flags.append("%d 错误" % len(r["errors"]))
        if r["warnings"]:
            flags.append("%d 警告" % len(r["warnings"]))
        head = "%-26s" % nm
        if not flags:
            head += "OK"
        else:
            head += "  ".join(flags)
        print(head)
        if r.get("lines") is not None:
            print("    正文 %d 行 | description %d 字符 | 目录常驻约 %d tok | 正文约 %d tok | 子目录: %s"
                  % (r.get("body_lines", 0), r.get("desc_len", 0), r.get("tok_boot", 0),
                     r.get("tok_body", 0), ",".join(r.get("subdirs", [])) or "无"))
        for m in r["errors"]:
            print("    ✗ %s" % m)
        for m in r["warnings"]:
            print("    ! %s" % m)
        for m in r["info"]:
            print("    · %s" % m)
        print()

    print("=" * 72)
    boot = sum(r.get("tok_boot", 0) for r in rows if r.get("tok_boot"))
    print("汇总: %d 个 skill | %d 错误 | %d 警告" % (len(found), n_err, n_warn))
    print("每次会话的 skill 目录常驻成本约 %d tokens（仅 description）" % boot)
    print("=" * 72)

    # ---- 发现名单：用于与 Web UI 的 `/` 菜单对照 ----
    names = sorted(r["name"] for r in rows if r.get("name"))
    print()
    print("发现名单（%d 个）—— 可与 Web UI 的 `/` 菜单对照" % len(names))
    print("菜单匹配规则：不区分大小写的有序子序列，前缀命中排最前。")
    print("若某 skill 不在此名单 → 它根本没被 DSH 加载，先修 E 级问题。")
    print("若在此名单但菜单里没有 → 多半是客户端缓存，刷新页面（F5）。")
    print()
    for i, n in enumerate(names, 1):
        row = next((r for r in rows if r.get("name") == n), None)
        mark = "✓" if (row or {}).get("discoverable") else "✗"
        print("  %2d. %s %s" % (i, mark, n))
    undiscoverable = [r for r in rows if r.get("name") and not r.get("discoverable")]
    if undiscoverable:
        print()
        print("  ✗ 不会被 DSH 加载的 skill:")
        for r in undiscoverable:
            print("     - %s: %s" % (r["name"], r["errors"][0] if r["errors"] else "未知"))
    print()
    print("=" * 72)
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
