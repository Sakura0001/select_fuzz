import AxeBuilder from "@axe-core/playwright";
import {expect, test} from "@playwright/test";

for (const path of ["/", "/runs/new", "/findings", "/reports"]) {
  test(`${path} has no serious or critical accessibility violations`, async ({page}) => {
    await page.goto(path);
    await expect(page.locator("main")).toBeVisible();
    const scan = await new AxeBuilder({page}).analyze();
    const severe = scan.violations.filter((violation) => ["serious", "critical"].includes(violation.impact ?? ""));
    expect(severe, severe.map((item) => `${item.id}: ${item.help}`).join("\n")).toEqual([]);
  });
}
