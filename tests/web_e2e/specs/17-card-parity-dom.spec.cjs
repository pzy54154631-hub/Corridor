// 17-card-parity-dom (core): the phone and the device show the same authoritative card, fact
// by fact, equal to the API's CardVM; notes and the gate are drawn outside the card frame.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;

async function facts(card) {
  return card.locator(T('card-fact')).evaluateAll((elements) =>
    elements.map((element) => ({
      key: element.getAttribute('data-key'),
      text: element.innerText.replace(/\s+/g, ' ').trim(),
    })),
  );
}

async function compare(page, bodies, rkey, label) {
  const reply = h.responseOf(page, rkey);
  const phone = reply.locator(`${T('card')}[data-owner="phone"]`).first();
  const device = page.locator(`${T('device-pane')} ${T('card')}[data-owner="device"]`).first();
  await expect(phone).toBeVisible();
  await expect(device).toBeVisible();
  const phoneFacts = await facts(phone);
  const deviceFacts = await facts(device);
  expect(phoneFacts.length, `${label}: facts drawn`).toBeGreaterThan(0);
  expect(deviceFacts, `${label}: device equals phone`).toEqual(phoneFacts);
  expect(await device.getAttribute('data-tier')).toBe(await phone.getAttribute('data-tier'));
  expect(await device.locator(T('card-amount')).innerText()).toBe(
    await phone.locator(T('card-amount')).innerText(),
  );

  const vm = (await h.responseState(bodies, rkey)).cards[0];
  expect(phoneFacts.map((fact) => fact.key)).toEqual(vm.facts.map((fact) => fact.key));
  vm.facts.forEach((fact, index) => {
    expect(phoneFacts[index].text).toContain(fact.label.trim());
    expect(phoneFacts[index].text).toContain(fact.value.trim());
  });
  const state = await h.latestState(bodies);
  const task = state.device.tasks.find((item) => item.rkey === rkey);
  const strip = ({ key, ...rest }) => rest;
  expect(task.cards.map(strip)).toEqual(state.chat.responses[rkey].cards.map(strip));

  for (const inside of ['card-note', 'gate', 'note-fraud', 'note-suitability']) {
    await expect(page.locator(`${T('card')} ${T(inside)}`), `${label}: ${inside}`).toHaveCount(0);
  }
  for (const fact of await facts(phone)) {
    expect(['red', 'fraud', 'suitability', 'irreversible']).not.toContain(fact.key);
  }
}

async function run(page, view) {
  const bodies = h.collectApiBodies(page);
  await h.start(page, { view });
  const yellow = await h.send(page, SAY.yellow);
  await compare(page, bodies, yellow, `${view} yellow`);
  await h.stopAll(page);

  const red = await h.send(page, SAY.red);
  await compare(page, bodies, red, `${view} red`);
  await h.screenshot(page, '17-card-parity-dom', `red-${view}`);
  await h.stopAll(page);

  const clarify = await h.send(page, SAY.CLARIFY_PLAIN);
  const reply = h.responseOf(page, clarify);
  await reply.locator(T('clarify-choice-0')).click();
  await h.clickAndWait(page, reply.locator(T('clarify-review')), '/recipient');
  await compare(page, bodies, clarify, `${view} clarify`);
}

test.describe('17-card-parity-dom', () => {
  test('customer view: yellow, red and clarified cards match across panes and the API @scripted', async ({
    page,
  }) => {
    await run(page, 'customer');
  });

  test('judge view: the same three cards match @scripted', async ({ page }) => {
    await run(page, 'judge');
  });
});
