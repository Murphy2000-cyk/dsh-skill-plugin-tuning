#!/usr/bin/env node
/**
 * audit-plugin.mjs —— DSH 插件包体检。
 *
 * 与同目录的 `audit.py`（管 skill 的 SKILL.md）分工不同：这个脚本管**插件包**，
 * 也就是 package.json + 工具 schema + Agent 补丁三者拼起来的东西。
 *
 * 用法：
 *   node audit-plugin.mjs --path <插件包目录>
 *   node audit-plugin.mjs --path <插件包目录> --json
 *
 * 退出码：发现 E 级问题返回 1，否则 0。
 *
 * 设计原则：
 *   - 只报**能判定的**事，拿不准的写进 hints，不编；
 *   - 需要跑插件自己的代码时才 import，import 失败就降级跳过并说明原因；
 *   - 每条结论都要能指回文件或行，方便直接去改。
 */

import { readdirSync, readFileSync, existsSync, statSync } from 'node:fs';
import { basename, extname, join, relative, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const argv = process.argv.slice(2);
const opt = (name) => {
  const hit = argv.findIndex((item) => item === `--${name}`);
  return hit === -1 ? null : argv[hit + 1] ?? true;
};

const root = resolve(String(opt('path') ?? '.'));
const asJson = argv.includes('--json');
const SKIP_DIRS = new Set(['node_modules', '.git', 'art', 'smoke-out', 'dist', 'coverage', 'tmp']);
const SKIP_FILE = /(^|[\\/])(smoke-out|audit-out|names-out|test-out|contract-out)[^\\/]*$/;
const MAX_FILE_LINES = 360;

const errors = [];
const warnings = [];
const infos = [];
const hints = [];
const E = (m) => errors.push(m);
const W = (m) => warnings.push(m);
const I = (m) => infos.push(m);

if (!existsSync(root)) {
  console.error(`找不到目录：${root}`);
  process.exit(2);
}

/** 递归收集文件（跳过显然是产物的目录）。 */
function walk(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue;
      walk(join(dir, entry.name), out);
    } else if (!SKIP_FILE.test(entry.name)) {
      out.push(join(dir, entry.name));
    }
  }
  return out;
}

const files = walk(root);
const rel = (file) => relative(root, file).replaceAll('\\', '/');
const readText = (file) => {
  try {
    return readFileSync(file, 'utf8');
  } catch {
    return '';
  }
};
const countLines = (file) => readText(file).split('\n').length;
/**
 * 去掉注释。
 *
 * 统计"某个名字有没有人读"时不能把注释算进去：一个键在注释里被提过一句，
 * 就会让"写了没人用"的判据失效（实测踩过：注释里写了键名，真 bug 被漏掉）。
 */
const stripComments = (text) =>
  text.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:'"`\\])\/\/[^\n]*/g, '$1');
const codeText = (file) => stripComments(readText(file));

/* ---------------------------------------------------------------- *
 * P0 / P1  package.json
 * ---------------------------------------------------------------- */

const pkgPath = join(root, 'package.json');
let pkg = null;
if (!existsSync(pkgPath)) {
  E('P0 找不到 package.json —— 这不是一个 DSH 插件包目录');
} else {
  try {
    pkg = JSON.parse(readText(pkgPath));
    I(`P0 package.json 可解析：${pkg.name ?? '(无 name)'} @ ${pkg.version ?? '(无 version)'}`);
  } catch (error) {
    E(`P0 package.json 不是合法 JSON：${String(error.message).split('\n')[0]}`);
  }
}

if (pkg !== null) {
  for (const field of ['name', 'version', 'type']) {
    if (typeof pkg[field] !== 'string' || pkg[field].trim() === '') W(`P1 package.json 缺少 ${field}`);
  }
  if (pkg.type !== undefined && pkg.type !== 'module' && pkg.type !== 'commonjs') {
    W(`P1 package.json 的 type 是 ${JSON.stringify(pkg.type)}，只认 "module" 或 "commonjs"`);
  }
  // files 白名单：列了就必须存在，否则打包出来少文件（Agent 装完当场跑不起来）
  if (Array.isArray(pkg.files)) {
    for (const item of pkg.files) {
      // 白名单里允许 glob（lib/*.js 这种），只有具体路径才逐项确认
      if (/[*?]/.test(item)) {
        const dir = join(root, item.split(/[*?]/)[0].replace(/\/$/, ''));
        if (!existsSync(dir)) W(`P1 files 里列了 ${item}，但它所在的目录不存在`);
        continue;
      }
      const target = join(root, item.replace(/\/$/, ''));
      if (!existsSync(target)) W(`P1 files 里列了 ${item}，但目录里没有`);
    }
  } else {
    I('P1 没有 files 白名单（会把整个目录打包，含 test/scripts）');
  }
}

/* ---------------------------------------------------------------- *
 * P2  图标：Agent 只认包根 icon.svg
 * ---------------------------------------------------------------- */

const svg = join(root, 'icon.svg');
const png = join(root, 'icon.png');
if (!existsSync(svg)) {
  if (existsSync(png)) {
    E('P2 包根有 icon.png 但缺 icon.svg —— Agent 解析插件图标时只读包根 icon.svg，缺了会 ENOENT；把 PNG 用 data URI 内嵌进一个 svg 壳文件即可');
  } else {
    W('P2 包根既没有 icon.svg 也没有 icon.png，插件会没有图标');
  }
} else {
  const body = readText(svg);
  if (!/data:image\/|<\s*svg/i.test(body)) W('P2 icon.svg 里既没有矢量也没有内嵌位图，可能是坏文件');
  if (pkg?.exports !== undefined && JSON.stringify(pkg.exports).includes('./icon')) {
    I('P2 package.json 里声明了 ./icon 导出；Agent 不会读它，图标仍以包根 icon.svg 为准');
  }
}

/* ---------------------------------------------------------------- *
 * P3  cordis 补丁
 * ---------------------------------------------------------------- */

const patchSpec = pkg?.['dsh']?.bundle?.patch ?? pkg?.dsh?.patch ?? null;
if (patchSpec === null) {
  W('P3 package.json 里没有 dsh.bundle.patch —— Agent 不知道该怎么把这个包插进运行时');
} else {
  const patchPath = join(root, String(patchSpec));
  if (!existsSync(patchPath)) {
    E(`P3 dsh.bundle.patch 指向的 ${patchSpec} 不存在`);
  } else {
    const text = readText(patchPath);
    const ids = [...text.matchAll(/id:\s*([A-Za-z0-9._-]+)/g)].map((m) => m[1]);
    if (ids.length === 0) W(`P3 ${patchSpec} 里没有 id: —— Agent 靠它去重，没有会重复插入`);
    const name = String(pkg.name ?? '').replace(/^@[^/]+\//, '');
    if (ids.length > 0 && name !== '' && !ids.some((id) => id === name || name.endsWith(id))) {
      W(`P3 补丁里的 id (${ids.join(', ')}) 与包名 (${pkg.name}) 不一致，确认是否有意`);
    }
    I(`P3 补丁 ${patchSpec} 存在，id：${ids.join(', ') || '(无)'}`);
  }
}

/* ---------------------------------------------------------------- *
 * P4  工具契约：要不要 import 插件自己的代码
 * ---------------------------------------------------------------- */

const sourceFiles = files.filter((file) => ['.js', '.mjs', '.cjs'].includes(extname(file)));
const entryFromPkg = (() => {
  if (pkg === null) return null;
  const exp = pkg.exports;
  if (typeof exp === 'string') return join(root, exp);
  if (exp && typeof exp === 'object') {
    const dot = exp['.'];
    if (typeof dot === 'string') return join(root, dot);
    if (dot && typeof dot === 'object' && typeof dot.import === 'string') return join(root, dot.import);
  }
  if (typeof pkg.main === 'string') return join(root, pkg.main);
  return existsSync(join(root, 'index.js')) ? join(root, 'index.js') : null;
})();

let cfg = null;
let tools = null;
let importNote = null;
if (entryFromPkg === null) {
  W('P4 package.json 没有可用的入口（exports["."] / main / index.js 都没有），跳过契约检查');
} else {
  try {
    const mod = await import(pathToFileURL(entryFromPkg).href);
    if (typeof mod.resolveConfig === 'function') {
      try {
        cfg = mod.resolveConfig({});
      } catch (error) {
        W(`P4 resolveConfig({}) 抛错：${String(error.message).split('\n')[0]}`);
      }
    }
    const builder = typeof mod.buildTools === 'function' ? mod.buildTools : null;
    if (builder !== null) {
      // 桩只提供"形状"，不真跑：契约检查只需要定义对象
      const stub = { ctx: null, run: async () => ({ stdout: '', stderr: '' }), cfg: cfg ?? {} };
      try {
        tools = builder(stub);
        if (!Array.isArray(tools)) {
          W('P4 buildTools() 没返回数组');
          tools = null;
        }
      } catch (error) {
        W(`P4 buildTools(桩) 抛错，跳过契约检查：${String(error.message).split('\n')[0]}`);
      }
    } else {
      importNote = '入口没有导出 buildTools()，跳过参数/返回值契约检查（约定：导出 buildTools(deps) 才能静态体检）';
    }
  } catch (error) {
    importNote = `入口 import 失败，跳过契约检查：${String(error.message).split('\n')[0]}`;
  }
}
if (importNote !== null) I(`P4 ${importNote}`);

/** schema 里 Agent 明确不接受的写法。嵌套属性也要走一遍——数组 type 最常藏在属性里。 */
function checkSchemaShape(label, schema, depth = 0) {
  if (schema === null || typeof schema !== 'object') {
    E(`P4 ${label} 不是对象`);
    return;
  }
  if (Array.isArray(schema.type)) {
    E(`P4 ${label}.type 是数组 ${JSON.stringify(schema.type)} —— Agent 会报 unsupported JSON schema，只接受单个类型字符串；联合类型改用 oneOf`);
  } else if (schema.type !== undefined && typeof schema.type !== 'string') {
    E(`P4 ${label}.type 必须是字符串，现在是 ${typeof schema.type}`);
  }
  const props = schema.properties;
  if (props !== undefined && (props === null || typeof props !== 'object')) {
    E(`P4 ${label}.properties 必须是对象`);
  } else if (props !== undefined) {
    if (schema.additionalProperties !== false) {
      W(`P4 ${label} 是对象但没写 additionalProperties:false —— Agent 按严格模式校验，多余键会拒`);
    }
    if (Array.isArray(schema.required)) {
      const declared = new Set(Object.keys(props));
      const missing = schema.required.filter((key) => !declared.has(key));
      if (missing.length > 0) E(`P4 ${label}.required 里有未声明的属性：${missing.join(', ')}`);
    }
    if (depth < 3) {
      for (const [key, child] of Object.entries(props)) checkSchemaShape(`${label}.${key}`, child, depth + 1);
    }
  }
  for (const branch of ['items', 'additionalProperties']) {
    const child = schema[branch];
    if (child !== null && typeof child === 'object' && depth < 3) checkSchemaShape(`${label}.${branch}`, child, depth + 1);
  }
  for (const branch of ['oneOf', 'anyOf', 'allOf']) {
    if (Array.isArray(schema[branch]) && depth < 3) {
      schema[branch].forEach((child, index) => checkSchemaShape(`${label}.${branch}[${index}]`, child, depth + 1));
    }
  }
}

let paramCount = 0;
if (Array.isArray(tools)) {
  const names = new Set();
  for (const tool of tools) {
    const label = tool?.name ?? '(无名工具)';
    if (names.has(label)) E(`P4 工具名重复：${label}`);
    names.add(label);
    if (typeof tool?.description !== 'string' || tool.description.trim() === '') {
      E(`P4 ${label} 没有 description —— 模型靠它决定调不调用`);
    }
    const params = tool?.parameters;
    if (params === undefined) {
      E(`P4 ${label} 没有 parameters`);
    } else {
      checkSchemaShape(`${label}.parameters`, params);
      if (params.type !== 'object') E(`P4 ${label}.parameters.type 必须是 "object"，现在是 ${JSON.stringify(params.type)}`);
      paramCount += Object.keys(params.properties ?? {}).length;
    }
    const out = tool?.output?.schema;
    if (out === undefined) {
      W(`P4 ${label} 没有 output.schema —— Agent 仍会校验返回值，缺声明容易在运行期报 INVALID_TOOL_OUTPUT`);
    } else {
      checkSchemaShape(`${label}.output.schema`, out);
    }
  }
  I(`P4 契约：${tools.length} 个工具 / ${paramCount} 个参数，形态检查完成`);
}

/* ---------------------------------------------------------------- *
 * P5  配置死键：写了但没人读
 * ---------------------------------------------------------------- */

// 排除的是**被体检包自己根目录下**的 test/ 与 scripts/；用相对路径判断，
// 否则包被放在别的 scripts/ 目录里时（例如本 skill 的 fixtures），源码会被整体漏掉。
const nonTestSource = sourceFiles.filter((file) => !/^(test|scripts)\//.test(rel(file)));
const blob = nonTestSource.map(codeText).join('\n');
if (cfg !== null && typeof cfg === 'object') {
  const keys = Object.keys(cfg);
  const dead = [];
  for (const key of keys) {
    const uses = [...blob.matchAll(new RegExp(`\\b${key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'g'))].length;
    // 定义处 1 次 + 至少 1 次真实读取才算活
    if (uses <= 1) dead.push(`${key}(${uses})`);
  }
  if (dead.length > 0) {
    W(`P5 这些配置键除了默认值定义之外没人读：${dead.join(', ')} —— 用户写了不生效，比报错更难查`);
  } else {
    I(`P5 配置 ${keys.length} 个键，全部有读取处`);
  }
  // 工具参数里声明了、但执行代码里找不到名字的
  if (Array.isArray(tools)) {
    const orphan = [];
    for (const tool of tools) {
      for (const key of Object.keys(tool?.parameters?.properties ?? {})) {
        const uses = [...blob.matchAll(new RegExp(`\\b${key}\\b`, 'g'))].length;
        if (uses === 0) orphan.push(`${tool.name}.${key}`);
      }
    }
    if (orphan.length > 0) I(`P5 这些工具参数在源码里一次都没出现（可能只透传，需人工确认）：${orphan.join(', ')}`);
  }
}

/* ---------------------------------------------------------------- *
 * P6  文件规模 / TODO
 * ---------------------------------------------------------------- */

const big = nonTestSource
  .map((file) => ({ file: rel(file), lines: countLines(file) }))
  .filter((item) => item.lines > MAX_FILE_LINES)
  .sort((left, right) => right.lines - left.lines);
if (big.length > 0) I(`P6 超过 ${MAX_FILE_LINES} 行的文件：${big.map((x) => `${x.file}(${x.lines})`).join(', ')}`);

let todoCount = 0;
for (const file of nonTestSource) {
  const hits = [...readText(file).matchAll(/\b(TODO|FIXME|XXX)\b/g)].length;
  if (hits > 0) {
    todoCount += hits;
    I(`P6 ${rel(file)} 有 ${hits} 处 TODO/FIXME`);
  }
}
if (todoCount === 0) I('P6 没有 TODO/FIXME 残留');

/* ---------------------------------------------------------------- *
 * P7  文档里引用的相对路径
 * ---------------------------------------------------------------- */

const docs = files.filter((file) => extname(file) === '.md' && !/[\\/]node_modules[\\/]/.test(file));
// 文档里常只写文件名（icon.png、whale-girl.png），按全包同名文件兜一次，避免误报
const allBasenames = new Set(files.map((file) => basename(file)));
const missingPaths = new Map();
for (const doc of docs) {
  const text = readText(doc);
  for (const match of text.matchAll(/`([A-Za-z0-9._\-\/]+\.(?:m?js|cjs|json|md|svg|png|yml|yaml))`/g)) {
    const token = match[1];
    if (token.startsWith('http') || token.includes('*') || token.startsWith('../')) continue;
    if (allBasenames.has(basename(token))) continue;
    const candidates = [join(root, token), join(root, 'scripts', token), join(root, 'lib', token), join(root, 'art', token)];
    if (!candidates.some((candidate) => existsSync(candidate))) {
      const key = `${rel(doc)} → ${token}`;
      missingPaths.set(key, true);
    }
  }
}
if (missingPaths.size > 0 && missingPaths.size <= 8) {
  for (const key of missingPaths.keys()) W(`P7 文档引用的文件找不到：${key}（可能是名字写错，也可能只是举例）`);
} else if (missingPaths.size > 8) {
  I(`P7 文档里 ${missingPaths.size} 个反引号路径找不到，多为示例，不逐条报`);
} else {
  I('P7 文档里引用的相对路径都在');
}

/* ---------------------------------------------------------------- *
 * P8  跨文件没人用的导出（近似）
 * ---------------------------------------------------------------- */

const exportsFound = [];
// Agent 按约定直接读的导出，包内没人引用是正常的
const HOST_READ = new Set(['apply', 'inject', 'name', 'Config', 'buildTools', 'resolveConfig', 'schema']);
for (const file of nonTestSource) {
  const text = readText(file);
  for (const match of text.matchAll(/export\s+(?:async\s+)?(?:function|const|class)\s+([A-Za-z_$][\w$]*)/g)) {
    exportsFound.push({ name: match[1], from: rel(file) });
  }
}
const unused = [];
for (const item of exportsFound) {
  if (HOST_READ.has(item.name)) continue;
  // 测试与脚本也算引用：导出给单测用是正当用法，不该报成死代码
  const others = files.filter((file) => rel(file) !== item.from && ['.js', '.mjs', '.cjs'].includes(extname(file)));
  const referenced = others.some((file) => new RegExp(`\\b${item.name}\\b`).test(codeText(file)));
  const usedLocally = [...codeText(join(root, item.from)).matchAll(new RegExp(`\\b${item.name}\\b`, 'g'))].length > 1;
  if (!referenced && !usedLocally) unused.push(`${item.name}(${item.from})`);
}
if (unused.length > 0) {
  I(`P8 ${unused.length} 个导出在包内既没被别的文件用、本文件也没再用：${unused.slice(0, 12).join(', ')}${unused.length > 12 ? ' …' : ''}`);
} else {
  I(`P8 ${exportsFound.length} 个导出都有引用（本文件内或跨文件）`);
}

/* ---------------------------------------------------------------- *
 * 收尾
 * ---------------------------------------------------------------- */

hints.push('P9 静态检查看不到的三件事，必须人工过一遍：');
hints.push('  · 工具描述里的默认值和实际 preset/配置是否一致（删除配置键后常见 "default undefined"）');
hints.push('  · 同一个概念在代码、README、使用手册里是不是同一个词（术语一致性）');
hints.push('  · 真实调用一次工具，看返回值有没有被 Agent 以 INVALID_TOOL_OUTPUT 拒掉');

const report = { root, errors, warnings, infos, hints };
if (asJson) {
  console.log(JSON.stringify(report, null, 2));
} else {
  const line = '='.repeat(72);
  console.log(line);
  console.log(`DSH 插件包体检：${root}`);
  console.log(line);
  for (const item of errors) console.log(`  E  ${item}`);
  for (const item of warnings) console.log(`  W  ${item}`);
  for (const item of infos) console.log(`  .  ${item}`);
  console.log(line);
  console.log(`汇总：${errors.length} 个错误 / ${warnings.length} 个警告 / ${infos.length} 条信息`);
  for (const item of hints) console.log(item);
  console.log(line);
}

process.exit(errors.length > 0 ? 1 : 0);
