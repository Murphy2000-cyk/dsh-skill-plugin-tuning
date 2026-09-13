# DSH Skill 规格速查

本文件是 DeepSeek Harness 的 skill 机制**权威约束**的浓缩。需要精确细节时读这里；日常写作读 SKILL.md 主体的铁律表即可。

---

## 一、发现规则

skill 可以是两种形态，都必须在**被扫描根目录的下一层**：

```
<root>/<name>/SKILL.md      ← 目录 bundle（可以带 scripts/ references/ assets/）
<root>/<name>.md            ← 平铺单文件
```

**刻意不支持 `**/SKILL.md` 嵌套发现**，也忽略包 manifest。这是官方写明的主动取舍，不是遗漏。

**文件格式**：必须以 YAML frontmatter 开头 —— `---` 开始，`---` 结束。

---

## 二、Frontmatter 字段

| 字段 | 必填 | 说明 |
|---|:--:|---|
| `name` | ✅ | **必须匹配 `^[a-z0-9]+(-[a-z0-9]+)*$`**（全小写 ASCII）。违规 → **整个 skill 被静默丢弃**并记警告，不会报错到用户 |
| `description` | ✅ | 非空。**唯一的触发机制** |
| `whenToUse` | ❌ | 合法字段，但**不进入会话目录**，对触发决策**毫无作用**。写了等于没写 |
| `metadata` | ❌ | 提供方元数据，不渲染 |
| `disable-model-invocation` | ❌ | `true` 时从**面向模型的目录与 loader** 中排除 |
| `user-invocable` | ❌ | `false` 时从**面向用户的命令**中排除 |

**布尔值解析**：接受 YAML 布尔，以及不区分大小写的 `true/false`、`yes/no`、`on/off`、`1/0`。
**被拒绝的拼写或非布尔值会让整个 skill 随警告丢弃**，而不是静默允许某个接口——这是刻意的严格。

### 调用策略 2×2

| `disable-model-invocation` | `user-invocable` | 结果 |
|---|---|---|
| 未设 / false | 未设 / true | 模型可自动触发 **且** 用户可 `/name` 调用（常规） |
| **true** | 未设 / true | **模型看不到**；只能用户手敲 `/name` 注入 |
| 未设 / false | **false** | 只对模型可见；用户不能 `/name` |
| true | false | 完全隐藏（无实际用途） |

**用户专属调用（`disable-model-invocation: true`）的价值**：这是唯一能让模型**无法自动触发、也无法绕过**的机制。适合硬性约束、安全规则、必须人工把关的流程。
**代价**：模型不会主动用它，用户必须记得敲 `/name`。

---

## 三、根目录与优先级

默认按 rank 从小到大扫描，rank 小者胜：

| Rank | 来源 | 路径 |
|---:|---|---|
| 100 | `project-dsh` | `<projectRoot>/.dsh/skills` |
| 200 | `project-agents` | `<projectRoot>/.agents/skills` |
| 300 | `custom` | 配置项 `customSkillDirs` |
| 400 | `user-dsh` | `$DSH_HOME/skills`（默认 `~/.dsh/skills`） |
| 500 | `user-agents` | `$DSH_AGENTS_HOME/skills`（默认 `~/.agents/skills`） |
| 600 | `bundled` | `bundledSkillDir` 配置项 |

- **项目根** = 含 `.git` 的最近祖先目录；没有则用当前 cwd
- `user-dsh` 根会**跳过 `.system` 子目录**
- `includeDefaultRoots: false` 让提供方只看自己的根
- 用户 DSH 根：`$DSH_HOME` 或 `~/.dsh`；用户 agents 根：`$DSH_AGENTS_HOME` 或 `~/.agents`

### 同名裁决

跨层：**最近的层直接胜出**。
同层内：依次按 **rank → 提供方注册顺序 → 提供方本地顺序** 裁决。
被遮蔽的项**只记日志**，没有 API 可以枚举它们。

---

## 四、会话目录与 token 成本

模型看到的目录形如：

```markdown
<system-reminder>
A skill is a reusable set of task-specific instructions. The following skills are available in this session:

<available_skills>
- `name`: <规范化并截断的描述>
</available_skills>

If the user names a skill, or the task clearly matches a skill's description, call the `skill` tool with the exact skill name before taking task actions. ...
</system-reminder>
```

关键事实：

1. **目录只渲染 `name` + `description`**，不含 `whenToUse`、来源、提供方元数据 → **路由只基于名称和描述**
2. **description 被截断到 `catalogDescriptionMaxLength`，默认 500 字符**，最小 3
3. 目录是一条**持久 user 角色消息**，在**首次请求之前**注入
4. 成员、描述或可见性变化时，追加一份**完整的替换目录**（全量列表，不是增量）
5. 删除全部 skill 时追加**空目录**，明确停用旧名称
6. **正文**通过 `skill` 工具加载，作为**工具结果**进入历史，后续步骤重发直到压缩

**成本结论**：description 是常驻成本（每次请求），正文是按需成本（触发后每次请求直到压缩）。

---

## 五、热重载与失效

- 现有根目录由 Chokidar 以**深度 1** 监视
- 触发刷新的事件：直属 bundle 的添加/移除、平铺 `.md` 的添加/移除、`SKILL.md` 的添加/移除/变更
- **`references/`、`scripts/`、`assets/` 下的编辑不触发目录刷新**
- 变更在**每个微任务批次合并为一次失效** → 下一个模型步骤即可见，**无需重启**
- **第一方 `write`/`edit` 工具会直接使提供方失效**（`fs/observed` 事件），模型改完自己的 skill 立刻能看到
- **注册表没有 TTL**，失效完全由提供方驱动

### 热重载只覆盖"模型侧"

上文的即时生效指的是**模型看到的会话目录**。**Web UI 的 `/` 菜单不在此列**——它来自 `skills/list` Remote，结果由客户端持有，新建或改名后需要**刷新页面**才能更新。详见第六节末。

### 目录与正文生命周期分离

- **发现**阶段把 frontmatter 解析为目录条目（摘要）
- **每次加载**都重新读取当前文件正文
- 因此：**仅改正文不会改变目录 digest，也不需要任何缓存失效**；改 frontmatter（name/description/可见性）才触发目录替换

---

## 六、触发机制：两条路径

DSH 的触发**不是字符串匹配**。有两条完全独立的路径，服务两个不同界面、受两个不同开关控制。

### 路径 A：模型自动触发（读 description）

模型在首个请求前收到会话目录（见第四节），末尾指令是：

> If the user **names a skill**, or the task **clearly matches a skill's description**, call the `skill` tool with the exact skill name before taking task actions.

所以模型自动触发只有两种情形：

1. **用户说出了 skill 名**（字面命中）
2. **模型读了 description 后，判断任务与它匹配**（语义判断）

**没有关键词表、没有正则、没有匹配打分。** 加自定义的 `triggers:` 字段完全无效。

**「触发词」是写作技巧，不是机制**：往 description 里写用户真会说的原话，本质是给模型的语义判断提供更多线索。

### 路径 B：用户显式调用（`/` 菜单）

用户在输入框敲 `/` 会打开 skill 菜单。机制（来自 `dsh-client-ui-skill`）：

- 候选来自宿主的 **`skills/list` Remote**
- 宿主提供**每一个用户可调用**的 skill（即 `userInvocable`）
- `disable-model-invocation: true` 的 skill **只在这条路径上可见**，菜单里会带上"仅限用户"标记
- 匹配规则：**不区分大小写、有序子序列**匹配 skill 名，**前缀命中排最前**，同分保持宿主顺序
- 发出的消息携带字面文本 `/name`，宿主加载 skill 并把指令注入该轮次
- 若名字与宿主内置命令同名，**解析为命令**，不会成为提示词

敲 `/name` 后，skill 正文以 `<skill_content>` 块注入该轮次，模型看到的形态与 `skill` 工具加载完全一致。

### 两个开关

控制这两条路径的开关即第二节的「调用策略 2×2」。要点：**省略即允许**——这是默认，也是绝大多数 skill 该有的状态。只有确实需要"模型绝不能自动用"时才设 `disable-model-invocation: true`。

### 模型只在"需要帮助"时才查 skill

**模型只对"自己搞不定的复杂多步任务"才去查 skill。**

简单单步请求（"读一下这个 PDF"）**即使 description 完美匹配也不会触发**，因为模型用基本工具就能完成。

**推论**：
- 测试用例必须**足够实质**，否则测不出 description 的质量
- 值得做成 skill 的是：领域知识密集、步骤非显然、或需要固化特定输出格式的任务
- 显而易见的流程写成 skill 是浪费——它永远不会被触发

### 客户端缓存：一个反直觉的坑

**模型侧目录是热重载的，但 Web UI 的 `/` 菜单不是。**

- 模型侧：Chokidar 失效 → 下一个模型步骤即可见（见第五节）
- 用户侧：`skills/list` Remote 的结果由客户端持有，**新建/改名后菜单不会自动更新**

**症状**：模型目录里已经有新 skill，`/` 菜单里却没有。
**处理**：刷新页面（F5）。

注：`skills/list` 调用失败时会被记录并**静默丢弃该菜单组**，此时菜单只显示 pending/ready 状态——所以菜单异常时也要怀疑 Remote 失败。

---

## 七、平台差异（与 Claude Code 对比）

| 维度 | DSH | Claude Code |
|---|---|---|
| 来源 | **可插拔注册表**，支持磁盘 / 插件内存 / 远程 | 固定扫描 `.claude/skills` + `.claude/commands` |
| 调用策略 | **2×2**（模型 × 用户） | 模型侧单一 |
| 发现深度 | **刻意只有一层** | 支持嵌套 |
| 热重载 | **监听 + 免重启** | 需重新加载 |
| 打包格式 | 无（就是目录） | `.skill` |
| 描述上限 | **500 字符**（可配） | — |

**不要移植 Claude 生态里依赖 `.claude/commands/`、`claude -p`、`.skill` 打包的做法**——那些在 DSH 上不适用。
