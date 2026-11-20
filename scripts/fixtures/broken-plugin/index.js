/**
 * 故意做坏的插件包，用来验证 audit-plugin.mjs 真的会报错，而不是一律通过。
 *
 * 预期报出 4 个错误：缺 icon.svg（只有 icon.png）、工具没有 description、
 * required 里有未声明属性、参数 type 是数组。另有 5 个警告：files 里的目录不存在、
 * 补丁 id 与包名不一致、output.schema 缺 additionalProperties、两个配置键没人读。
 *
 * 改这个文件或改脚本后跑一遍 `node ../audit-plugin.mjs --path .` :
 * 报出这些错误，说明脚本还有效；一片绿，说明检测坏了。
 */

export function resolveConfig(config) {
  return {
    logLevel: 'info',
    // 下面这个键只在注释里被提到过，不是真读取——脚本统计前会去注释
    deadKey: 42,
    scanFps: 10,
  };
}

export function buildTools(deps) {
  const { cfg } = deps;
  return [
    {
      name: 'broken_watch',
      // 故意不写 description
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          input: { type: 'string' },
          mode: { type: ['string', 'number'] }, // Agent 不接受的数组类型
          fps: { type: 'integer' },
        },
        required: ['input', 'notDeclared'], // required 里有未声明的属性
      },
      output: {
        schema: {
          type: 'object',
          properties: { ok: { type: 'boolean' } }, // 缺 additionalProperties:false
        },
      },
      async execute() {
        return { ok: true, fps: cfg.scanFps };
      },
    },
  ];
}
