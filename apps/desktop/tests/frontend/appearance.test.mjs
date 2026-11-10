import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
const require = createRequire(import.meta.url);
const { foreground, validColor, displayImage } = require(path.join(process.env.QUINTESSENCE_FRONTEND_BUILD, "appearance.js"));

test("custom palette chooses readable foregrounds for both ends of the range", () => {
  assert.equal(foreground("#ffffff"), "#111111");
  assert.equal(foreground("#000000"), "#ffffff");
  assert.equal(validColor("#ff00aa"), true);
  assert.equal(validColor("red;background:url(x)"), false);
});
test("display images cannot load network resources or active SVG content", () => {
  assert.equal(displayImage("https://example.test/avatar.png"), null);
  assert.equal(displayImage("data:image/svg+xml;base64,PHN2Zz4="), null);
  assert.equal(displayImage('data:image/png;base64,AA=="),url(x)'), null);
  assert.equal(displayImage("data:image/png;base64,AA=="), "data:image/png;base64,AA==");
  assert.equal(displayImage("data:image/png;base64," + "A".repeat(3_000_000)), null);
});
