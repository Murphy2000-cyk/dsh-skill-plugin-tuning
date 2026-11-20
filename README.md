# dsh-skill-plugin-tuning

一个给 **DeepSeek Harness (DSH)** 用的工作台，管两样东西：**skill**（一份 SKILL.md 文本）和**插件包**（npm 包 + 工具定义 + Agent 补丁）。

它不替你写业务逻辑，只管两件最容易出错的事：**让 skill 真的会被触发**，**让插件真的能被 Agent 接受**。

`SKILL.md 109 行（计入上下文 103 行）` · `6 篇 references` · `3 个脚本` · `Python 3.8+ / Node 18+` · `audit.py 零第三方依赖` · `MIT`

---

## 它解决什么问题

写 skill 的门槛看起来很低：一个 Markdown 文件，加一段 frontmatter，放进目录就能用。

难点在于，DSH 的失败方式**几乎全是静默的**：

- `name` 写成大写或中文，整个 skill 被丢弃，屏幕上没有任何报错；
- 字段名写了 DSH 不认识的（`triggers`、`version`、`author`），一律忽略，文件看上去一切正常；
- 触发场景写进了 `whenToUse`，而这个字段根本不进会话目录，对触发毫无作用；
- `description` 超过上限，尾部被截断，模型永远读不到你精心写的那几条；
- `SKILL.md` 放在嵌套目录里，而扫描深度刻意只有一层，找不到；
- 正文写了 500 行领域知识，每次触发都把这 500 行重新塞进上下文。

插件包则是另一种麻烦：它不静默，但报错点分散在三个文件里——

- 图标只在**包根 `icon.svg`** 里找，写了 `exports["./icon"]` 也没用，缺了直接 `ENOENT`；
- 参数 schema 里写 `type: ["number", "string"]`，Agent 会以 `unsupported JSON schema` 整条拒绝；
- 返回值多一个 `output.schema` 没声明的键，整次工具调用以 `INVALID_TOOL_OUTPUT` 失败；
- 配置项写了却不生效（被 preset 覆盖、或根本没人读源码里那个键），而屏幕上一切正常。

所以「写个 Markdown」或者「装个包」都不够，你需要**静态体检** + **实测触发率** + 一份把机制讲清楚的规格速查。

---

## 两支的分工

两者只有"都给 Agent 加能力"这一点相同，硬约束几乎不重叠：

| | skill | 插件包 |
|---|---|---|
| 改的是什么 | Agent 的**判断**（何时做、按什么步骤） | Agent 的**能力**（新工具、新服务、界面槽位） |
| 载体 | `<name>/SKILL.md` | npm 包 + `cordis.patch.yml` |
| 载入方式 | 扫描技能目录，每轮把 name + description 放进会话目录 | 启动时按 profile 补丁装成运行时插件 |
| 影响范围 | 被调用的那一轮 | 整个 profile 的全部会话 |
| 改完生效 | 模型侧热重载；`/` 菜单要刷新页面 | **要重启 Agent**，schema 与配置都不热更新 |
| 校核工具 | `scripts/audit.py`（14 项判据） | `scripts/audit-plugin.mjs`（9 类判据） |

**结构是刻意的**：`SKILL.md` 只放两支共同的部分（判支、同构、纪律），差别与各自的做法全在 references 里。

```
SKILL.md  →  判这一单是 skill 还是插件  →  只读那一份细则
                ├── references/skills.md    skill 侧全部细则
                └── references/plugins.md   插件侧全部细则
```

调插件时不必读 skill 的 6 条铁律，调 skill 时不必读图标与补丁，这是省 token 也省注意力的关键。

---

## 核心概念速览（skill 侧）

### 6 条硬约束

| # | 约束 | 违反后果 |
|---|---|---|
| 1 | `name` 必须匹配 `^[a-z0-9]+(-[a-z0-9]+)*$`（全小写 ASCII） | **整个 skill 被静默丢弃** |
| 2 | `description` 必填、非空 | 同上 |
| 3 | 发现深度**只有一层**：`<root>/<name>/SKILL.md` | 嵌套目录里的 skill 找不到 |
| 4 | 会话目录里**只渲染 `name` + `description`** | `whenToUse` 对触发**毫无作用** |
| 5 | `description` 超长会被截断（DSH 默认 500，本机自行收到 400） | 超出部分模型看不到 |
| 6 | 目录是**持久消息**，正文是**工具结果** | 两者都常驻消耗 token |

第 4、5 条最容易踩：`whenToUse` 合法但不参与触发，`description` 超长的部分彻底消失。**把最重要的触发场景放最前面。**

### 只有这 6 个字段被识别

| 字段 | 必填 | 作用 |
|---|:--:|---|
| `name` | 是 | skill 的身份，跨来源合并的唯一键 |
| `description` | 是 | **模型自动触发的唯一依据** |
| `whenToUse` | 否 | 合法，但不进会话目录，写了等于没写 |
| `metadata` | 否 | 提供方元数据，不渲染 |
| `disable-model-invocation` | 否 | `true` 时模型看不到，只能用户手敲 |
| `user-invocable` | 否 | `false` 时用户不能 `/name` 调用 |

其余字段一律静默忽略。**真实案例**：某 skill 写了 592 字符的 `triggers:` 关键词表，一个字都没生效，而真正起作用的 `description` 反而写得敷衍。

两个开关键都是可选，**省略即允许**，这也是绝大多数 skill 该有的状态。值写错（例如 `是`、`true-ish`）会**丢弃整个 skill**，而不是退回默认值。

### 没有触发词机制

DSH **没有关键词表、没有正则、没有匹配打分**。触发是模型读完 `description` 之后的**语义判断**。

所以「触发词」只是写作技巧，不是注册机制：往 `description` 里写用户真会说的原话，本质是给模型的判断多提供一点线索。

### 两条独立触发路径

| 路径 | 谁发起 | 依据什么 | 关闭它的开关 |
|---|---|---|---|
| 模型自动 | 模型判断 | 会话目录里的 `description` | `disable-model-invocation: true` |
| 用户显式 | 用户敲 `/name` | `/` 菜单里的 skill 名 | `user-invocable: false` |

同一份 frontmatter 服务两个表面。**改 `description` 只影响第一条路径**，第二条路径看的是名字。

还有一个反直觉的事实：模型只对**自己搞不定的复杂多步任务**才去查 skill。简单单步请求（「读一下这个 PDF」）即使 description 完美匹配也不会触发。显而易见的流程写成 skill 是浪费，它永远不会被触发。

---

## 插件侧的三条硬事实

这三条是两支**同构**的地方，改 skill 的经验可以直接平移：

1. **工具描述就是插件的 `description`。** 模型只读得到工具的描述和参数名，读不出"什么时候该用"就永远不会调它；而这段描述**每轮都在上下文里计费**（Agent 自己按 `toolsTokens` 统计）。
2. **参数 schema 的 `type` 只能是单个类型字符串。** 联合类型要写 `oneOf`，写成数组会被整条拒绝。
3. **改完必须重启 Agent。** skill 改 frontmatter 立刻刷新模型侧目录，插件不行——不重启，模型看到的还是旧 schema。

---

## 安装

安装本身就是「把目录拷进去」，但有两个前提必须满足，否则装了也不生效。

**前提一：必须在 skills 根目录的下一层。** 发现深度只有一层，`<root>/<name>/SKILL.md` 是唯一合法形态，多套一层目录就找不到。

**前提二：放对根目录。** DSH 按 rank 由小到大扫描，rank 小者胜：

| Rank | 来源 | 路径 |
|---:|---|---|
| 100 | `project-dsh` | `<项目根>/.dsh/skills` |
| 200 | `project-agents` | `<项目根>/.agents/skills` |
| 300 | `custom` | 配置项 `customSkillDirs` |
| 400 | `user-dsh` | `$DSH_HOME/skills`（默认 `~/.dsh/skills`） |
| 500 | `user-agents` | `$DSH_AGENTS_HOME/skills`（默认 `~/.agents/skills`） |
| 600 | `bundled` | `bundledSkillDir` 配置项 |

项目根 = 含 `.git` 的最近祖先目录，没有则用当前工作目录。低 rank 会遮蔽高 rank 的同名 skill，被遮蔽者只记日志，没有 API 能枚举出来。

Windows 上用户级 agents 目录即 `C:\Users\<用户名>\.agents\skills`。

```powershell
# Windows PowerShell：用户级安装，对所有项目生效
Copy-Item -Recurse .\dsh-skill-plugin-tuning "$env:USERPROFILE\.agents\skills\"
```

```bash
# Linux / macOS
cp -r ./dsh-skill-plugin-tuning ~/.agents/skills/
```

项目级安装则把同一份目录放到 `<项目根>/.agents/skills/` 或 `<项目根>/.dsh/skills/` 下。

**无需重启。** 改 frontmatter 后模型侧目录会热重载，下一个模型步骤即可见。但 Web UI 的 `/` 菜单不会自动刷新，**需要按 F5**。`references/`、`scripts/`、`assets/` 里的编辑不触发目录刷新，改这些文件不需要任何操作。

验证是否装上：在 DSH 会话里输入 `/dsh`，`dsh-skill-plugin-tuning` 应该出现在候选前列（`/` 菜单是不区分大小写的有序子序列匹配，前缀命中排最前）。如果模型侧目录里有它、菜单里没有，先刷新页面，不要急着改 frontmatter。

---

## 脚本用法

三个脚本都在 `scripts/` 下。前两个是 Python，第三个是 Node（插件是 JS，要 import 插件自己的代码才验得准）。

### audit.py：skill 静态体检

```bash
python scripts/audit.py                    # 体检全部 skill
python scripts/audit.py --skill <name>     # 体检单个 skill
python scripts/audit.py --json             # 机器可读输出
```

14 项判据，编码即报告前缀：`E1`–`E4`（读不到 / frontmatter / name / description）、`W1`–`W7`（description 长度与触发词、`whenToUse`、正文行数、跨 skill 触发词重叠、死字段、排斥性声明）、`I0`–`I2`（name 与目录名、references 结构、调用开关提示）。

真实输出（本仓库自身，实测）：

```
$ python scripts/audit.py --skill dsh-skill-plugin-tuning

dsh-skill-plugin-tuning    OK
    正文 103 行 | description 216 字符 | 目录常驻约 148 tok | 正文约 2255 tok | 子目录: scripts,references
```

注意 `正文 103 行` 与文件总行数（109 行）略有差别：前者统计的是计入上下文成本的正文部分。

拿到报告后按「错误 → 警告 → 信息」的顺序处理。**错误必须先清零**，警告可以判断后再决定。

### audit-plugin.mjs：插件包体检

```bash
node scripts/audit-plugin.mjs --path <插件包目录>
node scripts/audit-plugin.mjs --path <插件包目录> --json
```

9 类判据：`package.json` 与 `files` 白名单、图标（只认包根 `icon.svg`）、`dsh.bundle.patch` 与补丁 id、**工具契约形态**（`type` 不能是数组、`additionalProperties: false`、`required` 不含未声明项、必须有 `output.schema`）、**配置死键**、文件规模与 TODO、文档引用路径、没人用的导出。

`P4` 的深度检查需要插件导出 `buildTools(deps)` 与 `resolveConfig()`；不导出就只做文件级检查，脚本会明说"跳过"，不会假装通过。

**脚本自己也要验一次。** 带一个故意做坏的夹具，跑它应该报 4 个错误 + 5 个警告：

```bash
node scripts/audit-plugin.mjs --path scripts/fixtures/broken-plugin   # 期望 exit 1
node scripts/audit-plugin.mjs --path <真插件目录>                      # 期望 exit 0
```

一片绿说明检测坏了，不是包变好了。

### eval_trigger.py：skill 触发准确率评估

```bash
python scripts/eval_trigger.py --check                            # 环境就绪性检查
python scripts/eval_trigger.py --skill <name> --gen               # 生成 cases.json 用例模板
python scripts/eval_trigger.py --skill <name> --cases cases.json
python scripts/eval_trigger.py --skill <name> --cases c.json --repeat 3 --probe 60
python scripts/eval_trigger.py --skill <name> --cases c.json --json
```

`--gen` 生成的模板只是骨架（含 3 条示例），**必须人工补足后再跑**：应触发 8 到 10 条，不应触发 8 到 10 条。应触发用例要覆盖正式、口语、不点名但明显需要等不同写法；不应触发用例里最该放的是「近似误触发」，也就是共享关键词但实际需要别的东西的请求。

示例输出（**数字为示意，非实测**）：

```
$ python scripts/eval_trigger.py --skill <name> --cases cases.json --repeat 3 --probe 60

  ✓ #1   期望:应触发   实际:已触发  帮我写个 skill 抓取网页公告
  ✗ #2   期望:应触发   实际:未触发  这个 skill 为什么没触发
  ✓ #8   期望:不应触发 实际:未触发  帮我读一下这个 PDF

  应触发命中率  : 7/8 = 88%
  误触发率      : 0/8 = 0%
  注: 6/48 次运行在探测超时后被掐断 —— 触发决策取自已落盘日志，判定有效
```

**命中率低是欠触发**，去 `description` 里补触发场景。**误触发率高是边界不清**，加「不适用」段。每次只改一个变量再重跑，否则无法归因。

**这个脚本目前只覆盖 skill。** 插件的工具触发评估方法完全同构（读同一份 session 日志、看 `tool/call` 里有没有那个工具名），但还没实现成脚本，只能人工做。

---

## 触发准确率是怎么测的

原理很短：跑一次**真实的无头任务**，然后去读那一次的 session 日志。

```
dsh --profile headless "<查询>"  →  读 session 日志  →  查是否调用了 skill 工具
```

判定依据是日志里 `tool/call` 事件的 `data.name == "skill"`，且 `data.arguments` 里 `name` 等于目标 skill。**这是真实的调用记录，不需要模型自述**，模型说自己「用了 skill」或「没用」都不参与判定。

脚本在 `--probe` 秒后**主动掐断进程**（默认 120 秒）。原因是触发决策发生在任务最开始，之后模型会真的去执行整个工作流（可能数分钟），这部分对判定毫无价值。掐断把单条用例从数分钟压到 1 到 2 分钟。

### 局限（请先读这段再解读数字）

- **headless 是单轮的**，测不出多轮对话里的触发行为；
- 它只回答「有没有被加载」，**测不出加载后模型有没有正确执行正文指令**，那需要人工看轨迹；
- 模型采样有随机性，`--repeat` 少了得到的只是噪声，建议至少 3 次；
- 判定依赖 DSH 的 session 日志格式与 `tool/call` 事件结构。DSH 若改了日志格式，解析可能失效，此时脚本会报「未产生新的 session」或解析不到事件，而不是悄悄给出错误的 0%；
- 用例集的质量决定结论的价值。用显而易见无关的请求凑数，会得到一份好看但无用的 100%。

**插件侧的能力边界**：能做到静态与契约层面的验证；工具会不会被模型选中（触发率）没有脚本；UI 插件、MCP 配置型 bundle 完全未覆盖；多 bundle 冲突未验证。

---

## ⚠️ 副作用与隔离：先把 `DSH_HOME` 指向临时目录

**跑每一条评估用例，都会真实启动一次 headless DSH 会话**，因而会在 `$DSH_HOME/sessions/` 下产生**真实的 session 记录**。

如果直接用默认的 `~/.dsh`，这些 session 会出现在你的会话列表里（在 DSH Web 界面中显示为「未分组」），看起来就像凭空多出来的一堆垃圾会话。跑 12 条用例乘 3 次重复，就是 36 条。

**推荐做法：把 `DSH_HOME` 指向一个临时目录，再跑评估。** 这样评估产生的会话完全隔离，不污染主环境。脚本原生支持这个环境变量：

```python
DSH_HOME = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
```

```powershell
# Windows
$env:DSH_HOME = "F:\tmp\dsh-eval"
python scripts/eval_trigger.py --check
python scripts/eval_trigger.py --skill <name> --cases cases.json
```

```bash
# Linux / macOS
export DSH_HOME=/tmp/dsh-eval
python scripts/eval_trigger.py --skill <name> --cases cases.json
```

另外，脚本会自动创建 `headless` profile（位于 `$DSH_HOME/profiles/headless/`），这也是它写在 `DSH_HOME` 下的内容之一。用临时目录时，评估结束后整个目录可以直接删掉。

---

## 仓库结构

```
dsh-skill-plugin-tuning/
├── SKILL.md                        109 行（计入上下文 103 行），框架：判支、同构、纪律
├── scripts/
│   ├── audit.py                    466 行，skill 静态体检，仅用标准库
│   ├── eval_trigger.py             414 行，触发率评估，依赖 zstandard
│   ├── audit-plugin.mjs            424 行，插件包体检，无第三方依赖
│   └── fixtures/broken-plugin/     故意做坏的插件包，用来验证体检脚本真的会报错
├── references/
│   ├── skills.md                   407 行，skill 侧全部细则
│   ├── plugins.md                  211 行，插件侧全部细则
│   ├── dsh-spec.md                 198 行，frontmatter 字段语义、发现规则、rank 优先级
│   ├── dsh-behavior.md             183 行，运行期行为、目录与正文生命周期、token 成本模型
│   ├── authoring.md                235 行，写法方法、description 打磨、渐进式披露
│   └── eval-methodology.md         187 行，用例设计、结果解读、A/B 对比
├── LICENSE                         MIT
└── .gitignore
```

合计约 2900 行。`references/` 是按需加载的长文档，正文只在需要精确细节时指向它们，这样触发成本才可控。

---

## 兼容性

| 项目 | 要求 |
|---|---|
| Python | 3.8+（开发环境为 3.13），供 `audit.py` 与 `eval_trigger.py` |
| Node | 18+（开发环境为 24），供 `audit-plugin.mjs`；要 import 插件自己的代码 |
| 第三方包 | `audit.py`、`audit-plugin.mjs` 无；`eval_trigger.py` 需要 `zstandard` |
| DSH CLI | 仅 `eval_trigger.py` 需要，且 `dsh` 必须在 PATH 中 |
| 平台 | 在 Windows 上开发，含 Windows 专属处理；Node 脚本跨平台 |

`zstandard` 是评估脚本的**硬依赖**，缺少时 session 日志解不开：

```bash
pip install zstandard
```

Windows 专属处理包括：任务栏弹窗抑制（防止掐断进程树时弹出「应用程序无法正常启动」对话框）、用 `taskkill /T` 结束整棵无头进程树、解析 npm 的 `.CMD` shim 以定位 `dsh`。其余路径有跨平台分支（用 `os.killpg` 结束进程组），但**非 Windows 平台的实测覆盖较少**，在 Linux 或 macOS 上遇到问题请提 issue。

关于 DSH 版本：本 skill 记录的约束（描述截断、rank 表、日志事件结构、schema 限制、图标约定）均以写作时的 DSH 行为为准。DSH 若调整这些默认值，`references/` 里的规格与两个体检脚本的检查项可能随之漂移。

### 来源

skill 侧的主体骨架（四目录结构、渐进式披露、`description` 写作要点、触发评估的用例设计）借鉴 Anthropic 的 [`skill-creator`](https://github.com/anthropics/skills)；插件侧的四条做法（知识来源顺序、什么不算验证、最小可用版优先、不枚举目录）借鉴 DSH 随包的 `cordis-plugin-development`。

判据本身不是借的：skill 的 14 项与插件的 9 类，都是在本机真实返工里踩出来的（`INVALID_TOOL_OUTPUT`、`type` 数组被拒、`icon.svg` ENOENT、配置被 preset 覆盖、缓存键漏字段、描述渲染出 `undefined`）。具体到每一条的出处，写在 `references/skills.md` 第十一节与 `references/plugins.md` 第七节。

---

## 许可证

本项目采用 **MIT License**，Copyright (c) 2026 ChengYK (https://github.com/Murphy2000-cyk)，全文见 `LICENSE`。

你可以自由使用、修改、再分发，包括商用，保留版权声明即可。
