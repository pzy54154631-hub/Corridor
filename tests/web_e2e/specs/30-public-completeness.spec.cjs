const { test, expect } = require('playwright/test');
const fs = require('fs');
const path = require('path');
const h = require('../lib/helpers.cjs');
const { T } = h;

async function capture(page, name, testInfo) {
  const folder = path.resolve('.tmp/c60');
  fs.mkdirSync(folder, { recursive: true });
  await page.screenshot({ path: path.join(folder, `${name}-${testInfo.project.name}.png`), animations: 'disabled' });
}

async function aaJourney({ page }, testInfo) {
  await h.start(page);
  const rkey = await h.send(page, '这顿饭300我们3个人AA');
  const reply = h.responseOf(page, rkey);
  await expect(reply.locator(T('aa-select'))).toBeVisible();
  await expect(reply.locator(T('aa-review'))).toBeDisabled();
  await reply.locator(T('aa-choice-0')).check();
  await expect(reply.locator(T('aa-review'))).toBeDisabled();
  await reply.locator(T('aa-choice-1')).check();
  await capture(page, 'aa', testInfo);
  const response = await h.clickAndWait(page, reply.locator(T('aa-review')), '/aa');
  const created = (await response.json()).rkey;
  expect(created).not.toBe(rkey);
  await h.expectStep(page, 'confirm_intent');
  expect((await h.deviceConfirm(page)).ok).toBe(true);
  await h.expectStep(page, 'done');
}

async function cardJourney({ page }, testInfo) {
  await h.start(page);
  const rkey = await h.send(page, '帮我把卡片额度提到3000');
  await h.responseOf(page, rkey).locator(T('route-hint-go')).click();
  await expect(page.locator(T('card-controls'))).toBeVisible();
  await page.locator(T('card-operation')).selectOption('lower_card_limit');
  await page.locator(T('card-limit')).fill('500');
  await capture(page, 'cards', testInfo);
  await h.clickAndWait(page, page.locator(T('card-review')), '/api/cards/preview');
  await h.expectStep(page, 'confirm_intent');
  await expectCardFact(page, 'card_change.before.limit', '变更前额度', '¥2,000.00');
  await expectCardFact(page, 'card_change.after.limit', '变更后额度', '¥500.00');
  expect((await h.deviceConfirm(page)).ok).toBe(true);
  await h.expectStep(page, 'done');
}

async function expectCardFact(page, key, label, value) {
  const row = h.activeTask(page).locator(`${T('card-fact')}[data-key="${key}"]`);
  await expect(row).toBeVisible();
  await expect(row.locator('dt')).toHaveText(label);
  await expect(row.locator('dd')).toHaveText(value);
}

async function raisedCardJourney({ page }) {
  await h.start(page, { locale: 'en' });
  await page.locator(T('tab-accounts')).click();
  await page.locator(T('card-operation')).selectOption('raise_card_limit');
  await page.locator(T('card-limit')).fill('3000');
  await h.clickAndWait(page, page.locator(T('card-review')), '/api/cards/preview');
  await h.expectStep(page, 'confirm_intent');
  await expectCardFact(page, 'card_change.before.limit', 'Previous transaction limit', 'CNY 2,000.00');
  await expectCardFact(page, 'card_change.after.limit', 'New transaction limit', 'CNY 3,000.00');
  expect((await h.deviceConfirm(page)).ok).toBe(true);
  await h.expectStep(page, 'mfa_request');
  await expectCardFact(page, 'card_change.after.limit', 'New transaction limit', 'CNY 3,000.00');
  await h.completeRed(page);
  await h.expectStep(page, 'done');
}

async function restrictedCardJourney({ page }) {
  await h.start(page);
  await page.locator(T('tab-accounts')).click();
  await page.locator(T('card-operation')).selectOption('restrict_card');
  await page.locator(T('card-restrict-cash')).check();
  await page.locator(T('card-restrict-online')).check();
  await h.clickAndWait(page, page.locator(T('card-review')), '/api/cards/preview');
  await h.expectStep(page, 'confirm_intent');
  const row = h.activeTask(page).locator(`${T('card-fact')}[data-key="card_change.after.restrictions"]`);
  await expect(row).toBeVisible();
  await expect(row.locator('dt')).toHaveText('变更后限制类别');
  await expect(row.locator('dd')).toContainText('现金交易');
  await expect(row.locator('dd')).toContainText('线上交易');
  expect((await h.deviceConfirm(page)).ok).toBe(true);
  await h.expectStep(page, 'done');
}

async function pendingCardJourney({ page }) {
  await h.start(page);
  await page.locator(T('tab-accounts')).click();
  await expect(page.locator(T('card-controls'))).toBeVisible();
  await page.locator(T('card-operation')).selectOption('lower_card_limit');
  await page.locator(T('card-limit')).fill('500');
  await h.clickAndWait(page, page.locator(T('card-review')), '/api/cards/preview');
  await h.expectStep(page, 'confirm_intent');
  await page.keyboard.press('Escape');
  await page.locator(T('card-operation')).selectOption('lower_card_limit');
  await page.locator(T('card-limit')).fill('600');
  const refused = await h.clickAndWait(page, page.locator(T('card-review')), '/api/cards/preview');
  expect((await refused.json()).error.code).toBe('not_applicable');
  await expect(page.getByText('请先完成或取消当前卡片操作', { exact: false })).toBeVisible();
  await h.expectStep(page, 'confirm_intent');
}

async function riskJourney({ page }, testInfo) {
  await h.start(page);
  await page.locator(T('tab-wealth')).click();
  await expect(page.locator(T('risk-questionnaire'))).toBeVisible();
  const retake = await h.clickAndWait(page, page.locator(T('risk-retake')), '/api/wealth/risk/retake');
  expect((await retake.json()).ok).toBe(true);
  await expect(page.locator(T('risk-retake'))).toHaveCount(0);
  for (let index = 0; index < 5; index++) await page.locator(T(`risk-answer-${index}`)).selectOption('0');
  await page.locator(T('risk-questionnaire')).scrollIntoViewIfNeeded();
  await capture(page, 'risk', testInfo);
  await expect(page.locator(T('risk-sign'))).toBeEnabled();
  const signed = await h.clickAndWait(page, page.locator(T('risk-sign')), '/api/wealth/risk');
  expect((await signed.json()).ok).toBe(true);
  await expect(page.locator(T('risk-profile'))).toContainText('C1');
  await expect(page.locator(T('risk-limit-reached'))).toBeVisible();
  await expect(page.locator(T('risk-sign'))).toHaveCount(0);
  await expect(page.locator(T('risk-retake'))).toHaveCount(0);
}

test('30-public-completeness AA selection and receipt @scripted @public', aaJourney);
test('30-public-completeness AA selection and receipt @scripted @public @mobile', aaJourney);
test('30-public-completeness card form and receipt @scripted @public', cardJourney);
test('30-public-completeness card form and receipt @scripted @public @mobile', cardJourney);
test('30-public-completeness risk questionnaire @scripted @public', riskJourney);
test('30-public-completeness risk questionnaire @scripted @public @mobile', riskJourney);
test('30-public-completeness pending card guidance @scripted @public', pendingCardJourney);
test('30-public-completeness pending card guidance @scripted @public @mobile', pendingCardJourney);
test('30-public-completeness raised limit facts @scripted @public', raisedCardJourney);
test('30-public-completeness raised limit facts @scripted @public @mobile', raisedCardJourney);
test('30-public-completeness restricted category facts @scripted @public', restrictedCardJourney);
test('30-public-completeness restricted category facts @scripted @public @mobile', restrictedCardJourney);
