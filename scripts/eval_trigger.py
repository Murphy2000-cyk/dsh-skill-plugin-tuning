#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dsh-skill-plugin-tuning / scripts/eval_trigger.py
DSH skill 触发准确率评估 —— 用真实 session 日志判定 skill 是否被调用

原理:
    dsh --profile headless "<查询>"  →  读该次运行的 session 日志
    →  查 tool/call 事件中 name=="skill" 且 arguments.name == 目标 skill
    →  与期望比对，得出触发率 / 误触发率

用法:
    python eval_trigger.py --skill <name> --gen              # 生成 cases.json 模板
    python eval_trigger.py --skill <name> --cases cases.json # 跑评估
    python eval_trigger.py --skill <name> --cases c.json --repeat 3 --json
    python eval_trigger.py --check                            # 检查环境就绪性

依赖: zstandard (pip install zstandard)
"""
import io, json, os, re, sys, glob, time, shutil, argparse, subprocess, datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DSH_HOME = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
PROFILE = "headless"
PROFILE_DIR = os.path.join(DSH_HOME, "profiles", PROFILE)
SESSIONS = os.path.join(DSH_HOME, "sessions")

PROFILE_PKG = {
    "name": "dsh-profile-%s" % PROFILE,
    "private": True,
    "dependencies": {},
    "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"]}},
}


# ---------------- 环境 ----------------
def ensure_profile(verbose=True):
    """确保 headless profile 存在。返回 (ok, message)。"""
    pj = os.path.join(PROFILE_DIR, "package.json")
    if os.path.isfile(pj):
        return True, "headless profile 已存在"
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        io.open(pj, "w", encoding="utf-8").write(json.dumps(PROFILE_PKG, indent=2) + "\n")
        return True, "已创建 headless profile: %s" % PROFILE_DIR
    except Exception as e:
        return False, "无法创建 headless profile: %s" % e


def check_env():
    ok = True
    print("=" * 66)
    print("环境就绪性检查")
    print("=" * 66)
    g = shutil.which("dsh")
    print("  dsh CLI      : %s" % (g or "✗ 未找到（PATH 中没有 dsh）"))
    ok &= bool(g)
    try:
        import zstandard  # noqa
        print("  zstandard    : ✓ 已安装")
    except ImportError:
        print("  zstandard    : ✗ 未安装 —— 请运行  pip install zstandard")
        ok = False
    print("  DSH_HOME     : %s" % DSH_HOME)
    print("  sessions 目录: %s%s" % (SESSIONS, "" if os.path.isdir(SESSIONS) else "  ✗ 不存在"))
    p_ok, msg = ensure_profile()
    print("  profile      : %s" % msg)
    ok &= p_ok
    if os.path.isdir(SESSIONS):
        n = len(glob.glob(os.path.join(SESSIONS, "*", "*", "session*.jsonl.zstd")))
        print("  历史 session : %d 个" % n)
    print("=" * 66)
    print("结论: %s" % ("✓ 可以运行触发评估" if ok else "✗ 环境不满足，请先解决上述问题"))
    return 0 if ok else 1


# ---------------- session 解析 ----------------
def snapshot():
    """当前所有 session 文件的路径集合。"""
    return set(glob.glob(os.path.join(SESSIONS, "*", "*", "session*.jsonl.zstd")))


def load_events(path):
    import zstandard as zstd
    with open(path, "rb") as f:
        raw = zstd.ZstdDecompressor().stream_reader(f).read()
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def extract_skill_calls(events):
    """返回本次运行中被加载的 skill 名列表（按顺序）。"""
    loaded = []
    for e in events:
        if e.get("type") != "tool/call":
            continue
        d = e.get("data") or {}
        if d.get("name") != "skill":
            continue
        raw = d.get("arguments")
        nm = None
        if isinstance(raw, str):
            try:
                nm = (json.loads(raw) or {}).get("name")
            except Exception:
                m = re.search(r'"name"\s*:\s*"([^"]+)"', raw)
                nm = m.group(1) if m else None
        elif isinstance(raw, dict):
            nm = raw.get("name")
        if nm:
            loaded.append(nm)
    return loaded


def all_tool_calls(events):
    return [((e.get("data") or {}).get("name")) for e in events if e.get("type") == "tool/call"]


def resolve_dsh():
    """返回可直接执行的 argv 前缀。

    Windows 上 npm 安装的 dsh 是 .CMD 批处理 shim，subprocess 无法直接执行它。
    优先解析到 node + lib/bin.js 直接调用（确定、无 shell 解析）；否则退回 cmd /c。
    """
    node = shutil.which("node")
    g = shutil.which("dsh")
    if g:
        base = os.path.dirname(g)
        binjs = os.path.join(base, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js")
        if node and os.path.isfile(binjs):
            return [node, binjs], None
    if not g:
        return None, "PATH 中找不到 dsh 命令"
    if os.name == "nt" and g.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/c", g], None
    return [g], None


# ---------------- Windows 错误弹窗抑制 ----------------
_ERRMODE_SET = False


def suppress_error_dialogs():
    """抑制子进程启动失败时 Windows 弹出的模态错误框。

    评估到点会掐断 headless 进程，而该进程可能已派生 pwsh 与 python 子进程。
    进程树拆除途中若有个子进程启动失败，Windows 默认会弹「应用程序无法正常
    启动 (0xc0000142)」对话框。SetErrorMode 会被子进程继承，可抑制这类弹窗。
    """
    global _ERRMODE_SET
    if _ERRMODE_SET or os.name != "nt":
        return
    try:
        import ctypes
        SEM_FAILCRITICALERRORS = 0x0001
        SEM_NOGPFAULTERRORBOX = 0x0002
        SEM_NOOPENFILEERRORBOX = 0x8000
        ctypes.windll.kernel32.SetErrorMode(
            SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX)
        _ERRMODE_SET = True
    except Exception:
        pass


def kill_tree(pid, grace=10):
    """终止整棵进程树。

    Windows 上 terminate() 只终止直接子进程，其派生的 pwsh / python 会变成
    孤儿并可能在后续启动失败，因此用 taskkill /T 连子孙一起清理。
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=grace)
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        pass


# ---------------- 运行 ----------------
def run_query(query, probe=120):
    """跑一次 headless 并判定触发。

    只关心「skill 有没有被加载」，而加载发生在任务最开始。之后模型会真的去执行
    整个工作流（可能耗时数分钟），对判定毫无价值。因此在 probe 秒后主动掐断，
    解析已落盘的日志即可 —— 这能把单条用例从数分钟压到 1-2 分钟。

    返回 (ok, session_path, loaded_skills, note)
    """
    argv0, err = resolve_dsh()
    if argv0 is None:
        return False, None, [], err
    suppress_error_dialogs()
    before = snapshot()
    truncated = False
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            argv0 + ["--profile", PROFILE, query],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=flags)
    except FileNotFoundError as e:
        return False, None, [], "无法执行 dsh: %s" % e
    try:
        proc.wait(timeout=probe)
    except subprocess.TimeoutExpired:
        truncated = True          # 日志已增量落盘，仍可解析
        kill_tree(proc.pid)       # 杀整棵树，避免遗留 pwsh / python 孤儿
        try:
            proc.wait(timeout=10)
        except Exception:
            pass

    after = snapshot()
    new = sorted(after - before, key=os.path.getmtime)
    if not new:
        return False, None, [], "未产生新的 session（进程被杀或启动失败）"
    target = new[-1]
    try:
        ev = load_events(target)
    except Exception as e:
        return False, target, [], "session 解析失败: %s" % e
    note = "已在 %ds 后掐断" % probe if truncated else None
    return True, target, extract_skill_calls(ev), note


# ---------------- 用例 ----------------
def gen_template(skill, out):
    cases = {
        "skill": skill,
        "note": "should_trigger=true 表示这条查询应当触发本 skill；false 表示不应触发。"
                "请务必使用真实、具体、口语化的查询，不要用抽象请求。",
        "cases": [
            {"id": 1, "query": "（示例）帮我用 X 做 Y，文件在 F:/.../a.xlsx", "should_trigger": True,
             "why": "明确指名需求"},
            {"id": 2, "query": "（示例）这个流程每次都一样，能不能固化下来", "should_trigger": True,
             "why": "不点名但明显需要"},
            {"id": 3, "query": "（示例）帮我看看这份 PDF 讲了什么", "should_trigger": False,
             "why": "近似误触发：涉及文档但不需要本 skill"},
        ],
    }
    io.open(out, "w", encoding="utf-8").write(json.dumps(cases, ensure_ascii=False, indent=2) + "\n")
    print("已生成用例模板: %s" % out)
    print("\n请人工审阅并补足：应触发 8-10 条，不应触发 8-10 条。")
    print("关键：负例要选「近似误触发」（共享关键词但实际需要别的东西），不要选显而易见的无关请求。")


def run_eval(skill, cases_path, repeat=1, probe=120, as_json=False):
    data = json.load(io.open(cases_path, encoding="utf-8"))
    cases = data.get("cases") or []
    if not cases:
        print("用例文件为空", file=sys.stderr)
        return 1

    results = []
    total = len(cases) * repeat
    i = 0
    n_trunc = 0
    for c in cases:
        q = c.get("query", "")
        exp = bool(c.get("should_trigger"))
        hits = []
        for _ in range(repeat):
            i += 1
            if not as_json:
                sys.stderr.write("\r  [%d/%d] 运行中… %s" % (i, total, q[:44]))
                sys.stderr.flush()
            ok, sess, loaded, note = run_query(q, probe)
            if ok and note:
                n_trunc += 1
            hits.append({"ok": ok, "loaded": loaded,
                         "err": None if ok else note, "note": note if ok else None,
                         "hit": (skill in loaded) if ok else None})
        n_ok = sum(1 for h in hits if h["ok"])
        n_hit = sum(1 for h in hits if h["hit"])
        got = (n_hit > 0)
        results.append({
            "id": c.get("id"), "query": q, "should_trigger": exp,
            "runs": repeat, "successful_runs": n_ok, "triggered": n_hit,
            "trigger_rate": (n_hit / n_ok) if n_ok else None,
            "verdict": "PASS" if (got == exp and n_ok) else ("ERROR" if not n_ok else "FAIL"),
            "loaded_skills": [h["loaded"] for h in hits],
            "errors": [h["err"] for h in hits if h["err"]],
            "truncated": [h["note"] for h in hits if h["note"]],
        })
    if not as_json:
        sys.stderr.write("\r" + " " * 70 + "\r")

    pos = [r for r in results if r["should_trigger"]]
    neg = [r for r in results if not r["should_trigger"]]
    pos_pass = sum(1 for r in pos if r["verdict"] == "PASS")
    neg_pass = sum(1 for r in neg if r["verdict"] == "PASS")
    summary = {
        "skill": skill,
        "cases": len(results),
        "repeat": repeat,
        "should_trigger_total": len(pos),
        "should_trigger_passed": pos_pass,
        "trigger_rate": (pos_pass / len(pos)) if pos else None,
        "should_not_trigger_total": len(neg),
        "should_not_trigger_passed": neg_pass,
        "false_trigger_rate": ((len(neg) - neg_pass) / len(neg)) if neg else None,
        "errors": sum(1 for r in results if r["verdict"] == "ERROR"),
    }

    if as_json:
        print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))
    else:
        print("=" * 74)
        print("触发准确率评估: %s" % skill)
        print("=" * 74)
        for r in results:
            mark = {"PASS": "✓", "FAIL": "✗", "ERROR": "!"}[r["verdict"]]
            exp = "应触发  " if r["should_trigger"] else "不应触发"
            got = "已触发" if (r["trigger_rate"] or 0) > 0 else "未触发"
            print("  %s %s | 期望:%s 实际:%s  %s" % (mark, "%-4s" % ("#%s" % r["id"]), exp, got,
                                                     r["query"][:46]))
            if r["verdict"] == "FAIL":
                others = sorted({s for run in r["loaded_skills"] for s in run if s != skill})
                if r["should_trigger"]:
                    print("       → 欠触发。建议在 description 中补充这类措辞的触发词")
                else:
                    print("       → 误触发%s。建议在 description 中写明不适用的情形"
                          % ("（实际加载了: %s）" % ", ".join(others) if others else ""))
            if r["errors"]:
                print("       错误: %s" % "; ".join(str(x) for x in r["errors"][:2]))
        print("-" * 74)
        if summary["trigger_rate"] is not None:
            print("  应触发命中率  : %d/%d = %.0f%%" % (pos_pass, len(pos), summary["trigger_rate"] * 100))
        if summary["false_trigger_rate"] is not None:
            print("  误触发率      : %d/%d = %.0f%%" % (len(neg) - neg_pass, len(neg),
                                                        summary["false_trigger_rate"] * 100))
        if summary["errors"]:
            print("  运行失败      : %d 条（结果不可信，请检查环境）" % summary["errors"])
        print("=" * 74)
        if n_trunc:
            print("  注: %d/%d 次运行在探测超时后被掐断 —— 触发决策取自已落盘日志，判定有效"
                  % (n_trunc, total))
        if summary["errors"]:
            print("⚠ %d/%d 条用例运行失败，结果不可信 —— 请先修复环境（--check）"
                  % (summary["errors"], len(results)))
            bad = {e for r in results for e in r["errors"]}
            for e in sorted(bad):
                print("   原因: %s" % e)
        else:
            if summary["trigger_rate"] is not None and summary["trigger_rate"] < 0.7:
                print("诊断: 命中率偏低 → description 缺少用户真会说的触发词或场景描述")
            if summary["false_trigger_rate"] is not None and summary["false_trigger_rate"] > 0.3:
                print("诊断: 误触发偏高 → description 边界不清，或与竞争 skill 触发词重叠")
            print("提示: 每次只改一个变量后重跑，否则无法归因。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="DSH skill 触发准确率评估")
    ap.add_argument("--skill", help="目标 skill 的 name（小写 kebab-case）")
    ap.add_argument("--gen", action="store_true", help="生成用例模板")
    ap.add_argument("--out", default="cases.json", help="--gen 的输出路径（默认 cases.json）")
    ap.add_argument("--cases", help="用例文件路径")
    ap.add_argument("--repeat", type=int, default=1, help="每条查询重复次数（默认 1；触发有随机性，建议 3）")
    ap.add_argument("--probe", type=int, default=120,
                    help="单条用例的探测秒数，到点掐断（默认 120）。触发决策发生在任务最开始，"
                         "无需等模型跑完整个工作流；调小可显著加速")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--check", action="store_true", help="仅检查环境")
    args = ap.parse_args()

    if args.check:
        return check_env()

    if not args.skill:
        ap.error("必须指定 --skill（除非使用 --check）")
    if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", args.skill):
        print("警告: %r 不是合法的小写 kebab-case skill 名，DSH 可能根本无法加载它"
              % args.skill, file=sys.stderr)

    if args.gen:
        gen_template(args.skill, args.out)
        return 0

    if not args.cases:
        ap.error("必须指定 --cases，或用 --gen 生成模板")

    ok, msg = ensure_profile()
    if not ok:
        print(msg, file=sys.stderr)
        return 1
    try:
        import zstandard  # noqa
    except ImportError:
        print("缺少依赖 zstandard。请运行:  pip install zstandard", file=sys.stderr)
        return 1

    return run_eval(args.skill, args.cases, args.repeat, args.probe, args.json)


if __name__ == "__main__":
    sys.exit(main())
