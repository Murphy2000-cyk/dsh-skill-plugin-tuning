# DSH 插件包：开发和调试

这份文件讲 **插件包**——它是代码，由 npm 包 + 工具定义 + Agent 补丁三样东西拼起来。
**总框架、两侧分流、共同纪律在 `SKILL.md`**；skill 那一支的细则在 `references/skills.md`。

**读这份文件之前先确认**：用户要动的是插件包（`package.json`、`index.js`、工具 schema、
`cordis.patch.yml`、装进 profile 的配置块），不是 SKILL.md。拿不准就问一句。

---

## 一、两套硬约束不重叠

| | skill | 插件包 |
|---|---|---|
| 载体 | `<name>/SKILL.md` | npm 包目录 |
| 改的是什么 | Agent 的**判断**（何时做、按什么步骤） | Agent 的**能力**（新工具、新服务、界面槽位） |
| 身份 | frontmatter 的 `name` | `package.json` 的 `name` |
| Agent 读什么 | 6 个 frontmatter 字段 | `package.json`、入口模块导出的 `apply` / `inject` / `buildTools` |
| 载入方式 | 扫描技能目录，每轮把 name + description 放进会话目录 | 启动时按 profile 补丁装成运行时插件 |
| 接入方式 | 放进技能根目录即被扫描 | `dsh.bundle.patch` 指向的补丁 + 写进 profile 的 `cordis.patch.yml` |
| 影响范围 | 被调用的那一轮 | 整个 profile 的全部会话 |
| 图标 | 无 | **只认包根 `icon.svg`**（见踩坑 3） |
| 工具描述长度 | description ≤ 400 字符，超了截断 | 没找到 Agent 侧的硬限制；但描述每轮都在付费，仍要精简 |
| 改完怎么生效 | 模型侧热重载，`/` 菜单要刷新页面 | **要重启 Agent**（重新拉起 `dsh web`），config 与 schema 都不会热更新 |
| 怎么验证 | `python scripts/audit.py` | `node scripts/audit-plugin.mjs` + 契约校验 + 端到端 smoke |
| 校核判据 | 14 项（见 `references/skills.md` 五） | 9 类（见本文件第三节） |

**最容易被忽略的差别是最后一行**：skill 改完立刻生效，插件改完不重启就等于没改。
症状是"我明明改了参数，模型看到的还是旧 schema"。

### 工具描述就是插件的 description

上表只列了差别，这里补一件**同构**的事：skill 靠 `description` 被触发，插件靠**工具描述**被触发，失败方式一模一样——模型只读得到描述和参数名，读不出"什么时候该用"就永远不会调它。Agent 自己把这件事算进成本：请求信封里有一个 `toolsTokens`，就是上一次请求工具 schema 的 token 数。

所以调插件时有三个动作和调 skill 完全对应：

| 要做的 | skill 那边的对应物 |
|---|---|
| 工具描述里写 `Not for ...` 边界、写清什么时候该用 | description 的"不适用"段 + 触发场景 |
| 参数名要能自解释，参数个数直接进账单 | description 长度与措辞 |
| 按 agent 摘工具用 `ctx.tools.restrict()` | `disable-model-invocation` / `user-invocable` |

触发评估也可以同构做：读 session 日志看 `tool/call` 里有没有那个工具名。**`scripts/eval_trigger.py` 目前只实现了 skill 那一支**，插件的这段得手工做或另写。

---

## 二、体检流程

三步，缺一步都算没验完。

```bash
# 1. 静态体检（本 skill 自带，Node 脚本，因为插件是 JS）
node <技能目录>/scripts/audit-plugin.mjs --path <插件包目录>

# 2. 用 Agent 自己的校验器验 schema
node <插件包目录>/scripts/verify-host-contract.mjs

# 3. 端到端：真造一个输入跑一遍
node <插件包目录>/scripts/smoke.mjs
```

第 1 步查 9 类（P0–P8）：package.json 合法性、`files` 白名单、图标、补丁 id、工具契约形态、
**配置死键**、文件规模与 TODO、文档引用路径、没人用的导出。

第 2 步不能用自写脚本代替：Agent 校验器才是最终裁判，自写脚本只能近似。第 2 步的价值在于它
用**真实产出**去校验 schema——手写样例漏一个键不会报，真实返回值漏一个键当场报。

**脚本自己也要验一次。** `scripts/fixtures/broken-plugin/` 是一个故意做坏的包（缺 icon.svg、
无 description、required 有未声明项、参数 type 是数组、两个配置键没人读）。跑到它身上应该报出
4 个错误 + 5 个警告；如果一片绿，说明检测坏了，不是包变好了：

```bash
node scripts/audit-plugin.mjs --path scripts/fixtures/broken-plugin   # 期望 exit 1
node scripts/audit-plugin.mjs --path <真插件目录>                      # 期望 exit 0
```

脚本报出来的东西，按 **E → W → 信息** 的顺序处理，并把结论讲给用户，不要只丢一串检查项。

---

## 三、九维清单（6 维脚本可判、1 维半自动、2 维人工）

| # | 查什么 | 判据 | 谁查 |
|---|---|---|---|
| 1 | 工具参数声明了但没用 | 每个 `parameters.properties` 的键，在 execute 链路里真被读到 | 脚本给候选，人工确认 |
| 2 | 配置键写了没人读（死键） | `resolveConfig()` 的键在源码里出现次数 ≤ 1 | 脚本 P5 |
| 3 | 工具描述与实现不一致 | 描述里的默认值/取值域是否等于 preset 或配置的实际值 | 人工（P9 提示） |
| 4 | 输出 schema 与返回值不齐 | schema 多余的键不致命，**遗漏的键致命** | 契约校验 |
| 5 | 单文件行数 | > 360 行考虑拆（编排 / 渲染 / 算法分开） | 脚本 P6 |
| 6 | `files` 白名单与实际 | 列了就存在 | 脚本 P1 |
| 7 | 文档引用的路径 | 反引号里的相对路径是否存在 | 脚本 P7 |
| 8 | 没人用的导出 | 包内（含 test/scripts）零引用 | 脚本 P8 |
| 9 | **名词与数值一致性** | 见第五节 | **人工，必做** |

第 9 维是脚本查不了、又最容易漏的一维。它漏掉的代价很具体：上一轮体检报"全部通过"，
下一轮就查出 `default undefined`、`233 token`（实际 384）、`默认 16`（实际 5）三处。

---

## 四、实测踩过的坑

每条都来自真实返工，含当时的错误串。

**1. 输出 schema 漏键 → 整次调用失败**
Agent 按 `output.schema` 严格校验返回值（`additionalProperties: false`）。
返回值多一个 schema 没声明的键，报 `INVALID_TOOL_OUTPUT: "value.estimate.singles" is not a declared property`，
这次工具调用对模型来说就是失败。**手写样例验不出来**，必须拿真实返回值去测。

**2. 参数 schema 的 `type` 不能是数组**
写 `type: ['number', 'string']` 报 `unsupported JSON schema: gridMode.type must be a single type string`。
要联合类型就用 `oneOf: [{ type: 'number' }, { type: 'string' }]`（数组元素的联合也在这个位置）。

**3. 图标只认包根 `icon.svg`**
`package.json` 里写了 `exports: { './icon': './icon.svg' }` 也没用，Agent 仍按包根路径读。
只有 `icon.png` 时报 `ENOENT ... icon.svg`，整个插件加载失败。修法：把 PNG 用 data URI 内嵌进一个
`icon.svg` 壳文件（`<svg><image href="data:image/png;base64,..."/></svg>`）。

**4. 配置键被预设静默覆盖**
配置里写 `a: 0.3`、`maxGap: 5`，而代码把它展开成 `{...cfg, ...preset}`，preset 里也带这几个键，
于是**配置永远不生效、也不报错**。用户写的东西没反应是最难查的一类。判据：同一个键同时出现在
"配置默认值"和"预设成员"里，就一定会有一方被覆盖，只能留一处。

**5. 缓存键漏字段 → 缓存永不命中或命中错数据**
缓存键包括文件指纹和全部分析口径（帧率、尺寸、块参数）。少一个字段，改了参数还复用旧结果；
读取函数少返回一个字段（例如 `area`），则永远 miss，白扫。
判据：写入时用到的每一个口径参数，读取时都要能对上。

**6. 描述里插值已删除的配置键**
删掉配置键后，描述模板里还写着 `` `default ${cfg.maxSheets}` ``，实际渲染成
`(default undefined)` 直接进了提示词。**删配置键要连描述一起改**，静态脚本查不到插值结果，
只能靠第 9 维或者真跑一次看。

**7. 同一件事两个名字**
预算单位曾经叫 `maxFrames`（理解为"抽多少帧"），实际语义是"交付几张图"，改名为 `maxSheets` 才对齐。
命名错位的后果不是报错，而是**用户和模型都在按错的理解用参数**。

**8. 单位换了，注释和文档没换**
画质分档从 800×450（233 token）换到 1440p（384 token）后，注释里还写着"约 233 token"、
"默认 16"（实际 5）。文档与代码打架时，读的人不知道信哪个。

---

## 五、名词与数值一致性（第 9 维，人工）

四条判据，逐条过一遍。前两条可以半自动（`grep` 加人工看），后两条纯人工。

1. **同一标识符的默认值和取值域，在任何地方都必须相同。**
   抓法：把所有出现处连默认值一起列出来对比。`maxSheets` 在 watch 描述里是 5/10，
   在 plan 描述里是 `undefined`，一比就现形。

2. **同一份数据里的两个字段不能指同一件事。**
   例：`groupCost` 同时返回 `sheets` 和 `groups`（1:1 相等）；`stats.sheets` 是数，`cost.groups` 是明细数组。
   同名不同义，读代码的人一定会误会。

3. **同一概念不能有两个中文名。**
   做过一遍才发现：一张图被叫「交付单元 / 图片张数 / 张 / 拼图」四种；
   「格」同时指"拼图里的一格"和"一整帧画面"；`preset` 在参数里叫预设、在文档标题里叫模式。
   判断方法：把专有名词列成一张表，每个词回到原文里看它出现的每一句是不是同一个意思。

4. **定义不能是循环。**
   「图片张数 = 交付单元里的图数」——用被定义词的同义词去解释它，等于没解释。
   判据：定义句里不能出现被定义词本身，也不能只出现它的另一个叫法。

**做自己的术语表**：真正的交付物不是一份检查清单，而是**一张"概念 → 唯一名字 → 停用别名"的表**。
表定了，改名才有依据；没有表就改名，只是把混乱换个位置。

---

## 六、能力与边界（插件侧）

**能做到的**（都有可执行判据，验过就敢下结论）：

- 装载链路：`package.json` 合法、`files` 白名单、包根 `icon.svg`、`dsh.bundle.patch` 与补丁 id
- 工具契约：`parameters.type` 必须是单个字符串、`additionalProperties: false`、`required` 不含未声明项、`output.schema` 存在
- **配置死键**：`resolveConfig()` 的键在源码里真被读到（剥注释后统计）
- 文件规模、TODO、文档引用路径、没人用的导出
- 端到端：插件自己的 smoke / 契约脚本跑通（**前提是它写了**，本技能不代写）

**做不到的**（方法有，但没有工具，只能人工）：

- **工具会不会被模型选中**——触发评估的方法与 skill 完全同构（读 session 日志的 `tool/call`），但 `scripts/eval_trigger.py` 只实现了 skill 那一支，插件这段得手工做
- 返回值在真实对话里是否被拒绝——契约脚本只能验形态，宿主运行时还有别的校验

**完全没覆盖**（别装作懂）：

- **UI / 客户端插件**：slot 注册、主题 token、组件崩溃，一次实测都没有
- **MCP 配置型 bundle**：没有搭过
- **多 bundle 冲突**：本机 web profile 只有一个自研插件，组合顺序与冲突未验证

**待补**（有进展就写进这一节，不要散到正文里）：插件版触发评估脚本、一个 UI 插件的最小样例与验收清单。

---

## 七、来源与借鉴

**一份判据，两个来源，分清楚才不会误信**：

| 内容 | 来源 |
|---|---|
| 知识来源顺序、什么不算验证、最小可用版优先、不枚举目录这四条做法 | 借鉴 DSH 随包的 `cordis-plugin-development` 技能（`SKILL.md` 的 "Knowledge sources, in order" 与 "Deliver a working plugin first"，`references/verification.md` 的验证边界） |
| 9 类判据（P0–P8）、两套硬约束对照、8 条踩坑、术语一致性判据 | **不是借的**，是在 `dsh-video-understanding` 上真踩出来的（`INVALID_TOOL_OUTPUT`、`type` 数组被拒、`icon.svg` ENOENT、配置被预设覆盖、缓存键漏字段、描述渲染出 `undefined`） |
| `ctx.tools.restrict()`、`toolsTokens`、profile 组合方式 | DSH 包内文档与 `dsh-plugin-manager` 实现，查证过再写 |

`cordis-plugin-development` **不在本会话的技能目录里**（它随 `dsh-agent-preset` 包分发，当前 profile 没挂出来），所以这里借鉴的是它的文档，不是让它去干活。它的长处在"怎么搭一个插件"（`cordis_inspect_query`、模板、`install_bundle`）；本文件补的是**搭完之后怎么验**，两者互补。
---

## 八、这份文件什么时候不该用

- 只被 Agent 调用一次、没有配置、没有补丁的临时脚本 → 按普通 Node 代码处理，不必走插件流程。
- 插件只是**被别的 skill 调用**（skill 里写一段 node 命令） → 那是 skill 的事，回 SKILL.md。
- 用户问的是"这个插件行为为什么不对"，先复现再说：插件调的是 Agent 服务，猜是猜不准的。