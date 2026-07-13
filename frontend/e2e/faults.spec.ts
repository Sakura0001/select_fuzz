import {expect, test} from "@playwright/test";

test("validation and not-found failures remain route scoped", async ({page, request}) => {
  const invalid = await request.post("http://127.0.0.1:8765/api/v1/runs", {
    data: {mode: "performance", workers: 10},
    headers: {"Content-Type": "application/json", "Idempotency-Key": "invalid-performance-workers"},
  });
  expect(invalid.status()).toBe(422);

  await page.goto("/unknown");
  await expect(page.getByRole("alert", {name: ""})).toContainText("Page not found");
  await page.goto("/runs/new");
  await expect(page.getByRole("button", {name: "Start run"})).toBeEnabled();
});

test("a stopped subprocess-backed run remains visible after refresh", async ({page}) => {
  await page.goto("/runs/new");
  await page.getByRole("button", {name: "Start run"}).click();
  await expect(page.getByText("running", {exact: true})).toBeVisible();
  await page.getByRole("button", {name: "Stop run"}).click();
  await expect(page.getByText("stopped", {exact: true})).toBeVisible();
  await page.reload();
  await expect(page.getByText("stopped", {exact: true})).toBeVisible();
});
