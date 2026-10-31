// 14-a11y (core): landmarks and names, keyboard-only yellow transfer, Esc, visible focus,
// contrast, reduced motion, the code field and target sizes (design 6.6).
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;

async function tabTo(page, testid, limit = 80) {
  for (let index = 0; index < limit; index += 1) {
    const here = await page.evaluate(() => {
      const active = document.activeElement;
      const holder = active && active.closest('[data-testid]');
      return holder ? holder.getAttribute('data-testid') : null;
    });
    if (here === testid) return;
    await page.keyboard.press('Tab');
  }
  throw new Error(`keyboard focus never reached ${testid}`);
}

// Contrast of every text-bearing element under the selectors, against its painted background.
async function lowContrast(page, selectors) {
  return page.evaluate((list) => {
    const parse = (value) => {
      const match = value.match(/rgba?\(([^)]+)\)/);
      if (!match) return null;
      const parts = match[1].split(/[ ,/]+/).filter(Boolean).map(Number);
      return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
    };
    const luminance = ({ r, g, b }) => {
      const channel = (value) => {
        const v = value / 255;
        return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
    };
    // The first opaque background up the tree; a gradient counts by its first colour stop.
    const background = (element) => {
      for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        const color = parse(style.backgroundColor);
        if (color && color.a > 0.5) return color;
        const stop = style.backgroundImage !== 'none' && parse(style.backgroundImage);
        if (stop && stop.a > 0.5) return stop;
        for (const pseudo of ['::before', '::after']) {
          const layer = getComputedStyle(node, pseudo);
          const painted = layer.content !== 'none' && parse(layer.backgroundColor);
          if (painted && painted.a > 0.5) return painted;
        }
      }
      return { r: 255, g: 255, b: 255, a: 1 };
    };
    const failures = [];
    for (const selector of list) {
      for (const element of document.querySelectorAll(selector)) {
        const own = [...element.childNodes].some(
          (node) => node.nodeType === 3 && node.textContent.trim(),
        );
        const style = getComputedStyle(element);
        if (!own || style.visibility === 'hidden' || style.display === 'none') continue;
        const fg = parse(style.color);
        const bg = background(element);
        const [l1, l2] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
        const ratio = (l1 + 0.05) / (l2 + 0.05);
        const size = parseFloat(style.fontSize);
        const large = size >= 24 || (size >= 18.66 && Number(style.fontWeight) >= 700);
        if (ratio < (large ? 3 : 4.5)) {
          failures.push(`${selector}: ${element.textContent.trim().slice(0, 24)} ${ratio.toFixed(2)}`);
        }
      }
    }
    return failures;
  }, selectors);
}

test.describe('14-a11y', () => {
  test('landmarks, named buttons and the lang attribute @scripted', async ({ page }) => {
    await h.start(page, { view: 'judge' });
    await expect(page.getByRole('banner')).toHaveCount(1);
    await expect(page.getByRole('main')).toHaveCount(1);
    await expect(page.getByRole('complementary').first()).toBeVisible();
    for (const id of ['phone-pane', 'device-pane', 'evidence-pane']) {
      const name = await page.locator(T(id)).evaluate((element) => {
        const labelled = element.getAttribute('aria-labelledby');
        if (labelled) return document.getElementById(labelled)?.textContent?.trim() || '';
        return element.getAttribute('aria-label') || '';
      });
      expect(name, `${id} has an accessible name`).not.toBe('');
    }
    const unnamed = await page.evaluate(() =>
      [...document.querySelectorAll('button, [role="button"]')]
        .filter((element) => element.offsetParent !== null)
        .filter(
          (element) =>
            !(element.textContent || '').trim() &&
            !element.getAttribute('aria-label') &&
            !element.getAttribute('aria-labelledby') &&
            !element.getAttribute('title'),
        )
        .map((element) => element.outerHTML.slice(0, 80)),
    );
    expect(unnamed).toEqual([]);
    await h.setLocale(page, 'en');
    await expect(page.locator('html')).toHaveAttribute('lang', 'en');
    await h.setLocale(page, 'zh-CN');
  });

  test('keyboard only: yellow transfer, visible focus ring, Esc closes the dialog @scripted', async ({
    page,
  }) => {
    await h.start(page);
    await page.locator('body').click({ position: { x: 2, y: 2 } });
    await tabTo(page, 'composer-input');
    await page.keyboard.type(SAY.yellow);
    await Promise.all([h.waitApi(page, '/api/chat/message'), page.keyboard.press('Enter')]);
    await h.expectStep(page, 'confirm');
    await tabTo(page, 'device-confirm');
    const outline = await page.evaluate(() => {
      const element = document.querySelector(':focus-visible');
      if (!element) return 0;
      const style = getComputedStyle(element);
      const shadow = style.boxShadow && style.boxShadow !== 'none' ? 2 : 0;
      return Math.max(parseFloat(style.outlineWidth) || 0, shadow);
    });
    expect(outline).toBeGreaterThanOrEqual(2);
    await Promise.all([h.waitApi(page, '/confirm'), page.keyboard.press('Enter')]);
    await expect(page.locator(T('result-banner')).last()).toBeVisible();

    await page.locator(T('reset')).click();
    await expect(page.locator(T('reset-confirm'))).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.locator(T('reset-confirm'))).toBeHidden();
  });

  test('contrast of tier badges, buttons, muted and evidence text is at least 4.5:1 @scripted', async ({
    page,
  }) => {
    await h.start(page, { view: 'judge' });
    await h.send(page, SAY.red);
    await h.expectStep(page, 'confirm');
    const failures = await lowContrast(page, [
      `${T('card')} *`,
      `${T('device-pane')} button`,
      `${T('device-pane')} *`,
      `${T('evidence-pane')} *`,
      `${T('stage-header')} *`,
      `${T('chat-log')} *`,
    ]);
    expect(failures).toEqual([]);
  });

  test('reduced motion turns transitions off @scripted', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await h.start(page);
    await h.send(page, SAY.yellow);
    const moving = await page.evaluate(() =>
      [...document.querySelectorAll('button, [data-testid]')]
        .map((element) => getComputedStyle(element).transitionDuration)
        .filter((value) => value.split(',').some((part) => parseFloat(part) > 0)),
    );
    expect(moving).toEqual([]);
  });

  test('code field: label, one-time-code, error described; device buttons are at least 44x44 @scripted', async ({
    page,
  }) => {
    await h.start(page);
    await h.send(page, SAY.red);
    await h.expectStep(page, 'confirm');
    const confirm = page.locator(`${T('device-pane')} ${T('device-confirm')}`);
    const box = await confirm.boundingBox();
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    await h.requestCode(page);
    const code = await h.readSmsCode(page);
    const input = await h.codeInput(page);
    await expect(input).toHaveAttribute('autocomplete', 'one-time-code');
    await expect(input).toHaveAttribute('inputmode', 'numeric');
    const labelled = await input.evaluate(
      (element) =>
        element.labels?.length > 0 ||
        !!element.getAttribute('aria-label') ||
        !!element.getAttribute('aria-labelledby'),
    );
    expect(labelled).toBe(true);
    const wrong = code.slice(0, 5) + String((Number(code[5]) + 1) % 10);
    const verify = await h.enterCode(page, wrong);
    expect(verify.ok === false || verify.result?.status !== 'mfa_verified').toBe(true);
    const described = await (await h.codeInput(page)).getAttribute('aria-describedby');
    expect(described).toBeTruthy();
    const error = page.locator(`[id="${described.split(' ')[0]}"]`);
    await expect(error).toBeVisible();
    await expect(error).not.toHaveText('');
  });
});
