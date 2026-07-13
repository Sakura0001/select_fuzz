import tseslint from "typescript-eslint";

export default tseslint.config(
  {ignores: ["dist/**", "coverage/**", "node_modules/**", "test-results/**", "playwright-report/**"]},
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}", "e2e/**/*.ts", "*.config.ts"],
    rules: {
      "@typescript-eslint/consistent-type-imports": "error",
      "@typescript-eslint/no-explicit-any": "error",
    },
  },
);
