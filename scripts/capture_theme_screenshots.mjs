#!/usr/bin/env node
/**
 * Capture-and-compare tool for the shared-token visual no-op proof (issue #57).
 *
 * `setup` creates the first account against a fresh instance and saves its
 * signed-in storage state for reuse by `capture`.
 *
 * `capture` renders six pages (login, deck list, deck page, settings
 * general, settings sources, account) at 1024px and 390px width in three
 * theme states (follow-system, light, dark), writes one PNG per
 * (page, width, theme) under the given output directory, and writes a
 * hashes.json manifest of their sha256 digests. The signed-out login page
 * has no `data-theme` on the document root by product design, so its light
 * and dark runs set that attribute on the page directly before capture;
 * the follow-system run leaves it unset so the browser's own preference
 * decides, exactly as a real signed-out visitor would see it.
 *
 * `compare` reads two manifests written by `capture` and prints one
 * before/after hash line per (page, width, theme) triple plus a final
 * verdict.
 *
 * Playwright's install location varies by machine; it is resolved as the
 * bare specifier "playwright" unless PLAYWRIGHT_MODULE names another entry
 * point.
 *
 * `setup` and `capture` create the first account and change themes, so the
 * base URL must resolve to a loopback host (127.0.0.1, localhost, ::1) — a
 * probe target — and the tool refuses, with no override, any other host so
 * the operator's live stack can never be a valid target of this proof.
 *
 * Usage:
 *   node scripts/capture_theme_screenshots.mjs setup --base-url <url> --out-dir <dir>
 *   node scripts/capture_theme_screenshots.mjs capture --base-url <url> --out-dir <dir>
 *   node scripts/capture_theme_screenshots.mjs compare --before <dir> --after <dir>
 */

import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const PLAYWRIGHT_MODULE = process.env.PLAYWRIGHT_MODULE ?? "playwright";
const USER = process.env.PRESENTATOR_USER ?? "felix";
const PASSWORD = process.env.PRESENTATOR_PASSWORD ?? "correct-horse-battery-staple-57";

const PAGES = [
  { id: "login", path: "/login", signedIn: false },
  { id: "deck-list", path: "/", signedIn: true },
  { id: "deck-page", path: "/deck/hello-deck", signedIn: true },
  { id: "settings-general", path: "/settings", signedIn: true },
  { id: "settings-sources", path: "/settings/sources", signedIn: true },
  { id: "account", path: "/account", signedIn: true },
];

const WIDTHS = [
  { id: "1024", width: 1024, height: 800 },
  { id: "390", width: 390, height: 844 },
];

const THEMES = ["follow-system", "light", "dark"];

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

function refuseUnlessLoopback(baseUrl) {
  const hostname = new URL(baseUrl).hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (LOOPBACK_HOSTS.has(hostname)) return;
  process.stderr.write(
    `refusing ${baseUrl}: this proof creates the first account and changes themes, so it ` +
      "only targets a loopback host (127.0.0.1, localhost, ::1) — the operator's live " +
      "stack must never be a valid target of this proof, and there is no override\n",
  );
  process.exit(1);
}

function colorSchemeFor(theme) {
  return theme === "light" ? "dark" : "light";
}

function storagePathFor(outDir) {
  return path.join(outDir, "storage.json");
}

function manifestPathFor(outDir) {
  return path.join(outDir, "hashes.json");
}

async function launch(chromium, colorScheme, storageState) {
  const browser = await chromium.launch({
    headless: true,
    args: ["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
  });
  const context = await browser.newContext({
    colorScheme,
    storageState,
    reducedMotion: "reduce",
  });
  const page = await context.newPage();
  return { browser, context, page };
}

async function closeBrowser(session) {
  await session.context.close();
  await session.browser.close();
}

async function runSetup(chromium, baseUrl, outDir) {
  const session = await launch(chromium, "light");
  const { page } = session;
  await page.goto(`${baseUrl}/setup`, { waitUntil: "networkidle" });
  if (new URL(page.url()).pathname === "/setup") {
    await page.fill("#username", USER);
    await page.fill("#password", PASSWORD);
    await page.fill("#repeated-password", PASSWORD);
    await Promise.all([
      page.waitForURL((url) => url.pathname === "/"),
      page.click('button[type="submit"]'),
    ]);
  }
  await mkdir(outDir, { recursive: true });
  await session.context.storageState({ path: storagePathFor(outDir) });
  await closeBrowser(session);
}

async function signIn(page, baseUrl) {
  await page.goto(`${baseUrl}/login`, { waitUntil: "networkidle" });
  if (new URL(page.url()).pathname !== "/login") return;
  await page.fill("#username", USER);
  await page.fill("#password", PASSWORD);
  await Promise.all([
    page.waitForURL((url) => url.pathname !== "/login"),
    page.click('button[type="submit"]'),
  ]);
}

async function selectSignedInTheme(chromium, baseUrl, storagePath, theme) {
  const session = await launch(chromium, colorSchemeFor(theme), storagePath);
  const { page } = session;
  await page.goto(`${baseUrl}/settings`, { waitUntil: "networkidle" });
  await page.selectOption("#default-theme", theme);
  await Promise.all([
    page.waitForURL("**/settings"),
    page.click('button[type="submit"][form="general"]'),
  ]);
  await page.goto(`${baseUrl}/`, { waitUntil: "networkidle" });
  await page.locator("header details > summary").click();
  await page.locator(`header input[name="theme"][value="${theme}"]`).check();
  await page.waitForFunction(
    (expected) => {
      const current = document.documentElement.getAttribute("data-theme");
      return expected === "follow-system" ? current === null : current === expected;
    },
    theme,
    { timeout: 15000 },
  );
  await session.context.storageState({ path: storagePath });
  await closeBrowser(session);
}

async function forceRootTheme(page, theme) {
  if (theme === "follow-system") return;
  await page.evaluate((value) => {
    document.documentElement.setAttribute("data-theme", value);
  }, theme);
}

async function captureOne(chromium, baseUrl, destDir, definition, width, theme, storagePath) {
  await mkdir(destDir, { recursive: true });
  const dest = path.join(destDir, `${definition.id}-${width.id}-${theme}.png`);
  const session = await launch(
    chromium,
    colorSchemeFor(theme),
    definition.signedIn ? storagePath : undefined,
  );
  try {
    await session.page.setViewportSize({ width: width.width, height: width.height });
    await session.page.goto(`${baseUrl}${definition.path}`, { waitUntil: "networkidle" });
    if (definition.signedIn && new URL(session.page.url()).pathname === "/login") {
      await signIn(session.page, baseUrl);
      await session.page.goto(`${baseUrl}${definition.path}`, { waitUntil: "networkidle" });
    }
    if (!definition.signedIn) {
      await forceRootTheme(session.page, theme);
    }
    await session.page.screenshot({ path: dest, fullPage: false });
    const digest = createHash("sha256").update(await readFile(dest)).digest("hex");
    return digest;
  } finally {
    await closeBrowser(session);
  }
}

async function runCapture(chromium, baseUrl, outDir) {
  const storagePath = storagePathFor(outDir);
  const manifest = {};
  for (const theme of THEMES) {
    await selectSignedInTheme(chromium, baseUrl, storagePath, theme);
    for (const definition of PAGES) {
      for (const width of WIDTHS) {
        const key = `${definition.id}:${width.id}:${theme}`;
        const digest = await captureOne(
          chromium,
          baseUrl,
          outDir,
          definition,
          width,
          theme,
          storagePath,
        );
        manifest[key] = digest;
        process.stdout.write(`${key} ${digest}\n`);
      }
    }
  }
  await writeFile(manifestPathFor(outDir), `${JSON.stringify(manifest, null, 2)}\n`);
}

async function runCompare(beforeDir, afterDir) {
  const before = JSON.parse(await readFile(manifestPathFor(beforeDir), "utf8"));
  const after = JSON.parse(await readFile(manifestPathFor(afterDir), "utf8"));
  const keys = Object.keys(before).sort();
  let mismatches = 0;
  const lines = [];
  for (const key of keys) {
    const beforeHash = before[key];
    const afterHash = after[key];
    const match = beforeHash === afterHash;
    if (!match) mismatches += 1;
    lines.push(`${key} before=${beforeHash} after=${afterHash} ${match ? "MATCH" : "MISMATCH"}`);
  }
  for (const line of lines) process.stdout.write(`${line}\n`);
  const verdict =
    mismatches === 0
      ? `VERDICT: ${keys.length}/${keys.length} pairs byte-identical`
      : `VERDICT: ${keys.length - mismatches}/${keys.length} pairs byte-identical, ${mismatches} mismatched`;
  process.stdout.write(`${verdict}\n`);
  return { lines, verdict, mismatches };
}

function readArg(name) {
  const index = process.argv.indexOf(`--${name}`);
  if (index === -1 || index === process.argv.length - 1) return undefined;
  return process.argv[index + 1];
}

function requireArg(name) {
  const value = readArg(name);
  if (!value) {
    process.stderr.write(`missing required --${name}\n`);
    process.exit(2);
  }
  return value;
}

async function main() {
  const command = process.argv[2];
  if (command === "setup" || command === "capture") {
    const baseUrl = requireArg("base-url");
    const outDir = requireArg("out-dir");
    refuseUnlessLoopback(baseUrl);
    const { chromium } = await import(PLAYWRIGHT_MODULE);
    if (command === "setup") await runSetup(chromium, baseUrl, outDir);
    else await runCapture(chromium, baseUrl, outDir);
    return;
  }
  if (command === "compare") {
    const beforeDir = requireArg("before");
    const afterDir = requireArg("after");
    await runCompare(beforeDir, afterDir);
    return;
  }
  process.stderr.write("usage: capture_theme_screenshots.mjs setup|capture --base-url <url> --out-dir <dir>\n");
  process.stderr.write("       capture_theme_screenshots.mjs compare --before <dir> --after <dir>\n");
  process.stderr.write(
    "setup/capture create the first account and change themes, so --base-url must be a " +
      "loopback host (127.0.0.1, localhost, ::1); no other host is ever accepted\n",
  );
  process.exit(2);
}

await main();
