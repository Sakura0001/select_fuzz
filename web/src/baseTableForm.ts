import type { BaseTableGeneratorVersion } from "./types";

export const MAX_BASE_TABLE_SEED = "18446744073709551615";

const CANONICAL_UINT64_PATTERN = /^(0|[1-9][0-9]*)$/;

export interface BaseTableFormFields {
  expand_base_table_columns: boolean;
  base_table_seed: string | null;
  base_table_generator_version: BaseTableGeneratorVersion | null;
}

export function baseTableSeedValidationError(seed: unknown): string | null {
  if (seed === "" || seed === null || seed === undefined) {
    return null;
  }
  if (typeof seed !== "string" || !CANONICAL_UINT64_PATTERN.test(seed)) {
    return "复现种子必须是规范的无符号十进制整数，不允许前导零、符号、空白、小数或非 ASCII 数字";
  }
  if (
    seed.length > MAX_BASE_TABLE_SEED.length
    || (seed.length === MAX_BASE_TABLE_SEED.length && seed > MAX_BASE_TABLE_SEED)
  ) {
    return `复现种子不能大于 ${MAX_BASE_TABLE_SEED}`;
  }
  return null;
}

export function normalizeBaseTableFormFields(fields: BaseTableFormFields): BaseTableFormFields {
  if (!fields.expand_base_table_columns) {
    return {
      expand_base_table_columns: false,
      base_table_seed: null,
      base_table_generator_version: null
    };
  }
  return {
    expand_base_table_columns: true,
    base_table_seed: fields.base_table_seed === "" || fields.base_table_seed == null ? null : fields.base_table_seed,
    base_table_generator_version: fields.base_table_generator_version ?? "v1"
  };
}
