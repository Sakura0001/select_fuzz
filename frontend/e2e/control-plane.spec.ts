import {expect, test} from "@playwright/test";

test("new-run form keeps performance single threaded", async ({page}) => {
  await page.goto("/runs/new");
  await page.getByLabel("Mode").selectOption("performance");
  await expect(page.getByLabel("Workers")).toHaveValue("1");
  await expect(page.getByLabel("Workers")).toBeDisabled();
  await page.getByRole("button", {name: "Start run"}).click();
  await expect(page).toHaveURL(/\/runs\/run-/);
  await expect(page.getByText("running", {exact: true})).toBeVisible();
  await page.getByRole("button", {name: "Stop run"}).click();
  await expect(page.getByText("stopped", {exact: true})).toBeVisible();
});
