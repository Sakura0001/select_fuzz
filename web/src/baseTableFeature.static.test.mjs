import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const sourceDirectory = fileURLToPath(new URL(".", import.meta.url));

function source(name) {
  return readFileSync(new URL(name, import.meta.url), "utf8");
}

test("任务类型和旧任务规范化保持种子为字符串", () => {
  const types = source("types.ts");
  const api = source("api.ts");

  assert.match(types, /base_table_seed:\s*string\s*\|\s*null/);
  assert.match(types, /base_table_generator_version:\s*BaseTableGeneratorVersion\s*\|\s*null/);
  assert.match(api, /expand_base_table_columns:\s*task\.expand_base_table_columns\s*\?\?\s*false/);
  assert.match(api, /base_table_seed:\s*task\.base_table_seed\s*\?\?\s*null/);
  assert.doesNotMatch(api, /(?:Number|parseInt)\s*\(\s*task\.base_table_seed/);
});

test("复现种子只接受规范的无符号 64 位 ASCII 十进制字符串", async () => {
  const helperPath = `${sourceDirectory}baseTableForm.ts`;
  assert.ok(existsSync(helperPath), "缺少复现种子表单校验模块");
  const { baseTableSeedValidationError } = await import(pathToFileURL(helperPath).href);
  const helper = source("baseTableForm.ts");

  for (const seed of ["", null, undefined, "0", "1", "18446744073709551615"]) {
    assert.equal(baseTableSeedValidationError(seed), null, `应接受 ${JSON.stringify(seed)}`);
  }
  for (const seed of [1, true, false, "00", "01", "+1", "-1", " 1", "1 ", "1.0", "١", "18446744073709551616", "9".repeat(1000)]) {
    assert.notEqual(baseTableSeedValidationError(seed), null, `应拒绝 ${JSON.stringify(seed)}`);
  }
  assert.doesNotMatch(helper, /\bBigInt\s*\(/, "上限校验不应解析任意长度的 BigInt");
});

test("提交时按开关规范化版本和种子且不改变字符串精度", async () => {
  const helperPath = `${sourceDirectory}baseTableForm.ts`;
  assert.ok(existsSync(helperPath), "缺少基表字段提交规范化模块");
  const { normalizeBaseTableFormFields } = await import(pathToFileURL(helperPath).href);

  assert.deepEqual(
    normalizeBaseTableFormFields({
      expand_base_table_columns: false,
      base_table_generator_version: "v1",
      base_table_seed: "18446744073709551615"
    }),
    {
      expand_base_table_columns: false,
      base_table_generator_version: null,
      base_table_seed: null
    }
  );
  assert.deepEqual(
    normalizeBaseTableFormFields({
      expand_base_table_columns: true,
      base_table_generator_version: null,
      base_table_seed: ""
    }),
    {
      expand_base_table_columns: true,
      base_table_generator_version: "v1",
      base_table_seed: null
    }
  );
  assert.equal(
    normalizeBaseTableFormFields({
      expand_base_table_columns: true,
      base_table_generator_version: "v1",
      base_table_seed: "18446744073709551615"
    }).base_table_seed,
    "18446744073709551615"
  );
});

test("表单条件字段、任务卡片和准备进度文案完整呈现", () => {
  const app = source("App.tsx");
  const styles = source("styles.css");

  assert.match(app, /Form\.useWatch\(\s*"expand_base_table_columns"/);
  assert.match(app, /<Switch\b/);
  assert.match(app, /扩展基表列（每表 200～500 列）/);
  assert.match(app, /name="base_table_generator_version"/);
  assert.match(app, /name="base_table_seed"/);
  assert.match(app, /<Input[^>]*maxLength=\{20\}[^>]*placeholder="留空自动生成，例如 12345"/);
  assert.match(app, /copyable=\{\{/);
  assert.match(app, /基表模式：核心列（42 列）/);
  assert.match(app, /基表模式：扩展列（200～500 列）/);
  assert.match(app, /每表 10～100 行/);
  assert.doesNotMatch(app, /每表 10 行/);
  assert.match(app, /className="task-card-header"/);
  assert.match(app, /className="task-card-steps"/);
  assert.match(app, /className="base-table-reproduction-id"/);
  assert.match(styles, /\.task-card-steps[\s\S]*margin-top:/);
  assert.match(styles, /\.base-table-mode\s+\.base-table-reproduction-id[\s\S]*white-space:\s*nowrap/);
  assert.doesNotMatch(styles, /\.base-table-mode\s*\{[\s\S]*?overflow-wrap:\s*anywhere/);
});
