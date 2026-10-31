// AppState builders for the D-line screen specs (fixtures mode). Shapes follow
// web-contract/1 sections 4 and 5; text is illustrative only, the real
// catalogue is served by the backend.
const GEN = 'dScreens0001';

const TIERS = {
  green: { code: 'green', label: '绿 · 自动查询', glyph: 'circle' },
  yellow: { code: 'yellow', label: '黄 · 需设备确认', glyph: 'diamond' },
  red: { code: 'red', label: '红 · 需独立模拟验证', glyph: 'triangle' },
  black: { code: 'black', label: '黑 · 禁止执行', glyph: 'square' },
};

const CANCEL = { label: '取消', hint: '取消会撤回本会话所有待确认提案；已结算的不变' };

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function card(key, tier, { amount = '¥500.00', notes = [], title = '网关审批卡', judge = false } = {}) {
  const vm = {
    key,
    title,
    purpose: '转账',
    amount_text: amount,
    tier: TIERS[tier],
    tier_reason: '新收款人或较大金额需要在设备上确认',
    facts: [
      { key: 'objects.contact_ref', label: '收款人', value: '妈妈 · 尾号3721（合成）' },
      { key: 'fees', label: '手续费', value: '¥0.00（无手续费）' },
    ],
    notes,
    not_signed_label: '不属于签名内容',
    why: { lines: ['原因：金额超过免确认额度'] },
  };
  if (judge) vm.technical = { card: { kind: 'transfer' }, amount_raw: '500', contact_aliases: ['mom'] };
  return vm;
}

function response(rkey, extra = {}) {
  return {
    rkey,
    current: true,
    status: 'awaiting_device',
    status_text: '等待设备确认',
    copy_lines: ['请核对卡片后在设备上确认。'],
    tier: null,
    requested_tier: null,
    redisplay_notice: null,
    cards: [],
    gate: null,
    result: null,
    failure: null,
    live_notes: [],
    device_task: null,
    route_hint: null,
    fallback_shortcuts: false,
    cancel: null,
    ...extra,
  };
}

function deviceTask(tkey, rkey, cards, extra = {}) {
  return {
    key: tkey,
    source: 'chat',
    source_label: '对话',
    rkey,
    current: true,
    cards: cards.map((c, i) => ({ ...clone(c), key: `${tkey}-${i}` })),
    step: 'confirm',
    step_text: '核对上面的内容无误后再确认',
    gesture: { op: 'confirm', token: 'dScreensGestureToken01', card_version: '0123456789abcdef', label: '确认' },
    cancel: CANCEL,
    ...extra,
  };
}

function base({ view = 'customer', locale = 'zh-CN', loggedIn = true, revision = 3, capabilities = {} } = {}) {
  const state = {
    schema: 1,
    contract: 'web-contract/1',
    generation: GEN,
    revision,
    view,
    locale,
    capabilities: {
      tabs: [],
      shortcuts: ['transfer', 'cards', 'scam_drill'],
      live: false,
      plan_select: false,
      compensate: false,
      aa: false,
      ...capabilities,
    },
    session: {
      logged_in: loggedIn,
      clock_text: '2026-09-15 10:30（北京时间）',
      bank_name: '演示银行',
      assistant_name: '智能助手',
      phone_url: `/phone/dScreensPair00001?lang=${locale}&view=${view}`,
    },
    shell: {
      corner: '合成数据 · 模拟认证 · 无真实资金',
      badge: { kind: 'demo', text: '脚本演示' },
      safety: { locked: false, text: null, hint: null },
      paused_banner: null,
      notices: [],
    },
    chat: {
      shortcuts: [
        { key: 'transfer', label: '给妈妈转 800 元' },
        { key: 'cards', label: '我的卡丢了' },
        { key: 'scam_drill', label: '诈骗演练' },
      ],
      drill_caption: '演练不会转出任何资金',
      greeting: null,
      turns: [],
      responses: {},
      current: null,
      gated_elsewhere: [],
      task_panel: null,
      composer: {
        placeholder: '说说您要办理什么，例如：给妈妈转500元',
        max_chars: 4000,
        attach_label: '附上对方发来的文件（模拟）',
        attach_note: '文件内容不会上传，只记“带有 1 个外部文件”；是否有附件由您自报',
        disabled: false,
      },
    },
    device: {
      title: view === 'judge' ? '模拟签名设备（软件模拟）' : '设备确认（模拟）',
      idle_text: '当前没有需要在设备上确认的操作。',
      queue_text: null,
      tasks: [],
      active: null,
      sms: {
        url: `/phone/dScreensPair00001?lang=${locale}&view=${view}`,
        label: '模拟短信（合成，不是真实短信）',
        open_label: '在新窗口打开',
        footer: '合成短信 · 无短信网关 · 无真实手机号',
      },
    },
  };
  if (view === 'judge') {
    state.shell.judge_strip = '本界面为脚本演示';
    state.shell.safety.state = 'Normal';
    state.shell.safety.reason = null;
    state.shell.safety.unlock_after_text = null;
    state.shell.safety.rule_ids = [];
    state.evidence = {
      open_by_default: ['kernel', 'consistency', 'dispatch'],
      planner: { badge: '脚本演示', source: 'scripted', counters: [] },
      dispatch: { count: 0, receipts: [] },
      audit: {
        count: 0,
        head: { algorithm: 'SHA-256(JCS)', short: '0000000000000000', full: '0'.repeat(64) },
        verified: true,
        note: '审计校验只说明链未被改写',
      },
      safety: { state: 'Normal', reason: null },
      intent_log: [],
      intent_log_note: '意图日志不在哈希链内',
      planner_log: [],
      footer: '资金变动以核验回执为准',
    };
  }
  if (!loggedIn) {
    state.login = {
      title: '欢迎使用演示银行',
      intro: '虚构的客户、机构和产品。',
      button: '进入合成演示',
      caption: '进入演示不会授权任何付款',
    };
  } else {
    state.chat.greeting = '您好，我是智能助手。';
  }
  return state;
}

function withTurn(state, rkey, userText, rvm) {
  const n = Object.keys(state.chat.responses).length + 1;
  state.chat.greeting = null;
  for (const key of Object.keys(state.chat.responses)) state.chat.responses[key].current = false;
  state.chat.turns.push({ id: `u${n}`, role: 'user', rkey, text: userText });
  state.chat.turns.push({ id: `a${n}`, role: 'assistant', rkey, text: rvm.copy_lines.join('\n') });
  state.chat.responses[rkey] = rvm;
  state.chat.current = rvm.current ? rkey : state.chat.current;
  return state;
}

function yellow(opts = {}) {
  const state = base(opts);
  const fraud = { kind: 'fraud', title: '防骗提醒', lines: ['请确认收款人是您本人认识的人'], footer: null };
  const c = card('R1-0', 'yellow', { notes: [fraud], judge: state.view === 'judge' });
  withTurn(
    state,
    'R1',
    '给妈妈转500元',
    response('R1', { tier: TIERS.yellow, cards: [c], device_task: 'TR1', cancel: CANCEL }),
  );
  state.device.tasks = [deviceTask('TR1', 'R1', [c])];
  state.device.active = 'TR1';
  return state;
}

function gate(decision, { preset = false, interactive = true, answers = {} } = {}) {
  return {
    version: 'dScreensGateVer1',
    title: '转账前请回答两个问题',
    subtitle: '页面提示 · 不属于签名内容',
    facts_title: '登记的事实',
    facts: ['新收款人', '金额 ¥480.00'],
    cues: '原话提到“工作人员”',
    questions_title: '两道自查题',
    questions: [
      {
        name: 'verified',
        label: '您是否独立核实过收款人？',
        options: [
          { value: 'yes', label: '已独立核实' },
          { value: 'no', label: '未独立核实' },
          { value: 'unsure', label: '不确定' },
        ],
        answer: answers.verified || null,
      },
      {
        name: 'guided',
        label: '是否有人指导您转这笔钱？',
        options: [
          { value: 'no', label: '没有' },
          { value: 'yes', label: '有人指导' },
        ],
        answer: answers.guided || (preset ? 'yes' : null),
      },
    ],
    decision,
    locked: decision === 'pause',
    preset,
    interactive,
    pause:
      decision === 'pause'
        ? {
            title: '已暂停',
            copy: preset ? '您的原话提到有人让您转账，第二题已按原话预置为“是”。' : '请先停下核实。',
            retry_label: '重新作答',
            self_report: '作答由您自述，系统无法核实',
            cancel_hint: '可随时取消',
          }
        : null,
    notice: decision === 'stale' ? '卡片已变化，请重新作答' : null,
    self_report_note: '作答由您自述，系统无法核实',
    footer: '（固定原句）',
  };
}

function gated(decision, gateOpts = {}, opts = {}) {
  const state = base(opts);
  const c = card('R1-0', 'red', { amount: '¥480.00' });
  const g = gate(decision, gateOpts);
  if (state.view === 'judge') g.judge = { note: '评委说明', record: 'epoch=1', preset_note: gateOpts.preset ? '第二题按原话预置' : null };
  withTurn(
    state,
    'R1',
    '工作人员说立即给新联系人转480元',
    response('R1', { status: 'awaiting_input', tier: TIERS.red, cards: [c], gate: g, device_task: 'TR1', cancel: CANCEL }),
  );
  const step = decision === 'proceed' ? 'confirm' : 'gate_blocked';
  state.device.tasks = [
    deviceTask('TR1', 'R1', [c], {
      step,
      gesture: step === 'confirm' ? deviceTask('TR1', 'R1', [c]).gesture : null,
      gate_notice: step === 'gate_blocked' ? { text: '请先在手机上回答两道自查题' } : undefined,
    }),
  ];
  state.device.active = 'TR1';
  return state;
}

function clarify({ locked = false, current = true } = {}, opts = {}) {
  const state = base(opts);
  withTurn(
    state,
    'R1',
    '给Lin转200元',
    response('R1', {
      status: 'needs_clarification',
      status_text: '请选择收款人',
      current,
      clarify: {
        label: '匹配到不止一位联系人，请选择收款人',
        choices: [
          { key: 'c0', label: '林一 · 尾号1024（合成）' },
          { key: 'c1', label: '林二 · 尾号2048（合成）' },
        ],
        chosen: locked ? 'c0' : null,
        review_label: '核对收款人',
        locked,
        locked_text: locked ? '这笔已提交核对，如需换人请先取消' : null,
        error_text: null,
      },
      cancel: CANCEL,
    }),
  );
  if (!current) {
    withTurn(state, 'R2', '查询余额', response('R2', { status: 'done', status_text: '已完成' }));
  }
  return state;
}

function done(opts = {}) {
  const state = yellow(opts);
  const r = state.chat.responses.R1;
  r.status = 'settled';
  r.status_text = '已办理';
  r.device_task = null;
  r.cancel = null;
  r.result = {
    banner: { tone: 'success', text: '已办理' },
    receipt_line: '2026-09-15 10:31（北京时间）· 回执编号（末 8 位）AB12CD34 · 已结算',
    facts_lines: ['已向妈妈转账 ¥500.00'],
    message_lines: [],
    why: { lines: ['黄档：已在设备上确认'] },
  };
  state.device.tasks = [];
  state.device.active = null;
  state.chat.task_panel = {
    route_label: '转账',
    tier: TIERS.yellow,
    copy: '转账已完成',
    requested_hint: null,
    dag: {
      nodes: [{ key: 'n0', label: '转账', status: 'settled', status_text: '已完成', tone: 'ok', layer: 0, order: 0 }],
      edges: [],
      blocked_text: null,
    },
    controls: { stop: '停止', compensate: null, handoff: '转人工' },
  };
  return state;
}

function phraseGate(opts = {}) {
  const state = base(opts);
  const rvm = response('R1', {
    status: 'refused',
    status_text: '已拦下',
    copy_lines: ['请立即停止转账，拨打 96110 或卡背面官方电话核实。'],
  });
  if (state.view === 'judge') rvm.phrase_gate_note = '话术闸门：F9 之后新增，未纳入 F9 测量，尚无效果证据';
  withTurn(state, 'R1', '反诈中心让我立即给新联系人转480元，不然账户冻结', rvm);
  return state;
}

function failure(kind = 'failed', opts = {}) {
  const state = yellow(opts);
  const r = state.chat.responses.R1;
  r.status = kind === 'failed' ? 'failed' : 'unknown';
  r.failure = {
    kind,
    lines: [kind === 'failed' ? '这一步没有完成。' : '这一步的结果无法确认。'],
    cancel_hint: '请取消后重新发起',
  };
  state.device.tasks[0].step = kind === 'failed' ? 'closed_failed' : 'closed_unknown';
  state.device.tasks[0].gesture = null;
  return state;
}

function earlier(opts = {}) {
  const state = gated('incomplete', {}, opts);
  state.chat.responses.R1.gate.interactive = false;
  state.chat.responses.R1.current = false;
  state.device.tasks[0].current = false;
  state.device.tasks[0].step = 'not_current';
  state.device.tasks[0].back = { rkey: 'R1', label: '回到那条回复' };
  withTurn(state, 'R2', '查询余额', response('R2', {
    status: 'settled',
    status_text: '已完成',
    copy_lines: ['您的活期余额为 ¥12,000.00'],
    tier: TIERS.green,
    result: { banner: { tone: 'info', text: '查询完成' }, receipt_line: null, facts_lines: ['活期 ¥12,000.00'], message_lines: [], why: { lines: [] } },
  }));
  state.chat.gated_elsewhere = [{ rkey: 'R1', label: '回到该回复作答' }];
  return state;
}

function planSubset(opts = {}) {
  const state = base({ ...opts, capabilities: { plan_select: true, tabs: ['plans'], shortcuts: ['transfer', 'cards', 'scam_drill', 'birthday'] } });
  const nodes = ['锁定预算', '订花', '订蛋糕'];
  const cards = nodes.map((label, i) => ({
    ...card(`R1-${i}`, 'yellow', { amount: ['¥1,000.00', '¥300.00', '¥200.00'][i], title: '供设备核对的事实 · 尚未授权' }),
    purpose: label,
    position: `${i + 1} / 3`,
  }));
  withTurn(
    state,
    'R1',
    '帮我准备老婆的生日',
    response('R1', {
      status: 'awaiting_device',
      tier: TIERS.yellow,
      cards,
      plan_select: {
        tkey: 'TR1',
        rule_text: '勾选必须从第一项起连续',
        nodes: nodes.map((label, i) => ({ key: `n${i}`, label, checked: i === 0 })),
        mode: 'subset',
        count: 1,
        total: 3,
        error_text: null,
      },
      device_task: 'TR1',
      cancel: CANCEL,
    }),
  );
  state.device.tasks = [
    deviceTask('TR1', 'R1', [cards[0]], {
      source: 'plans',
      step: 'confirm_subset',
      selection: { count: 1, total: 3, text: '将为所选 1 项生成新的待签计划卡，本步不签名' },
      nodes_version: 'fedcba9876543210',
    }),
  ];
  state.device.active = 'TR1';
  return state;
}

function ok(state) {
  return { ok: true, generation: state.generation, revision: state.revision, state };
}

function bump(state, mutate) {
  const next = clone(state);
  next.revision += 1;
  if (mutate) mutate(next);
  return ok(next);
}

const TABS = {
  accounts: {
    balances: [
      { label: '活期账户', amount: '¥12,000.00' },
      { label: '锁定预算账户', amount: '¥1,000.00' },
    ],
    cards: [{ label: '借记卡 · 尾号6688（合成）', status_text: '正常' }],
    receipts: [{ title: '转账给妈妈', amount: '¥500.00', time: '09-15 10:31', status: '已结算' }],
    subscriptions: {
      snapshot_text: '截至 09-15 的订阅',
      renewals: ['视频会员将于 09-20 续费 ¥25.00'],
      rows: [{ text: '视频会员 · 每月 ¥25.00' }, { text: '音乐会员 · 每月 ¥15.00' }],
      empty_text: null,
      footer: '取消需要在设备上确认',
      cancel_choices: [
        { key: 'c0', label: '视频会员' },
        { key: 'c1', label: '音乐会员' },
      ],
    },
    sub_query: { label: '准备订阅查询审批' },
    billing: {
      totals: [{ label: '9 月支出', value: '¥3,210.00' }],
      categories: [
        { label: '餐饮', value: '¥1,200.00', ratio: '0.37' },
        { label: '交通', value: '¥400.00', ratio: '0.12' },
      ],
      anomalies: [{ title: '重复扣款', amount: '¥25.00', date: '09-03', reasons: '同一商户一天两笔' }],
      empty_text: null,
    },
    empty_text: null,
  },
  wealth: {
    intro: '这里是单品比较与申购',
    scope_note: '单品申购，与理财顾问流程不同；顾问流程请在兜底界面体验',
    objective: {
      value: 'net_return',
      options: [
        { value: 'net_return', label: '净收益基准（不保证）' },
        { value: 'total_fee', label: '总费用' },
        { value: 'liquidity', label: '流动性' },
      ],
    },
    comparison: {
      banner: '比较完成——尚未执行申购',
      rows: [
        { key: 'p0', name: '稳健理财 A（合成）', cells: [{ label: '净收益基准', value: '2.10%' }], chosen: true },
        { key: 'p1', name: '稳健理财 B（合成）', cells: [{ label: '净收益基准', value: '1.95%' }], chosen: false },
      ],
      exclusions: [{ name: '进取理财 C（合成）', reasons: '风险等级高于您的测评' }],
      candidates: [
        { key: 'c0', label: '稳健理财 A（合成）' },
        { key: 'c1', label: '稳健理财 B（合成）' },
      ],
    },
    purchase: null,
    positions: [
      { key: 'c0', label: '稳健理财 A · 持有 ¥10,000.00', redeemable: true, caption: '已过最低持有期', from_text: '可赎回时间（北京时间）：09-01' },
      { key: 'c1', label: '稳健理财 B · 持有 ¥5,000.00', redeemable: false, caption: '未到最低持有期', from_text: '可赎回时间（北京时间）：10-20' },
    ],
    redemption: null,
    results: [],
    r4: null,
  },
  plans: {
    plans: [
      {
        key: 'p0',
        title: '老婆的生日',
        dag: {
          nodes: [
            { key: 'n0', label: '锁定预算', status: 'settled', status_text: '已完成', tone: 'ok', layer: 0, order: 0 },
            { key: 'n1', label: '订花', status: 'pending', status_text: '等待', tone: 'wait', layer: 1, order: 0 },
          ],
          edges: [['n0', 'n1']],
          blocked_text: null,
        },
        blocked_text: null,
      },
    ],
    draft: null,
    empty_text: '还没有登记的计划',
  },
  operator: {
    intro: '坐席看到的是脱敏快照',
    result: null,
    snapshot_lines: ['客户：合成客户 A', '待办：0'],
    compensation: null,
    note: '人工接管期间自动执行暂停',
  },
};

module.exports = {
  GEN,
  TIERS,
  CANCEL,
  clone,
  base,
  card,
  response,
  deviceTask,
  withTurn,
  yellow,
  gate,
  gated,
  clarify,
  done,
  phraseGate,
  failure,
  earlier,
  planSubset,
  ok,
  bump,
  TABS,
};
