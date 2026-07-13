import {defineConfig} from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  use: {baseURL: "http://127.0.0.1:4173"},
  webServer: [
    {command: "../.venv/bin/python ../tests/api/e2e_server.py", url: "http://127.0.0.1:8765/api/v1/health", reuseExistingServer: true},
    {command: "npm run dev -- --port 4173", url: "http://127.0.0.1:4173", reuseExistingServer: true},
  ],
});
