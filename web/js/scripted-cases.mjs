// 原型另两条脚本案例（C 线；Q1、R57、§3.2；AC-17）。原 localResult 逻辑，界面标明是脚本演示，不接真实引擎。
// 品牌一律换成虚构的 Kestrel Wholesale Club（会员卡取消）与 Bluefin Harbor Bank（重复扣款核查）。
// r2（第 3 步 C 线缺口 G6，暂定，Jev 0.58）：按 tech §6.1，schedule_callback 与 preview_callback 只作为脚本案例里的
// 本地演示步骤保留（Bluefin Harbor Bank 案例的回拨时间与回拨预览），不进 MCP、不经 mcp.callTool、不做真实回拨；
// schedule_callback 的结果带 real_booking:false。
//
// SCRIPTED_CASES：两项，键名自定；每项至少有 provider（虚构品牌名）、request（用户请求原文，写虚构品牌）字符串，
// 其余字段（id、task、goal、options、confirmation 等）从原型 CASES 移植并改用虚构品牌；不含主线公司 Lakeshore Energy。

export const SCRIPTED_CASES = Object.freeze({
  kestrel: {
    id: 'kestrel_membership_cancel_demo', task: 'WL-2049',
    provider: 'Kestrel Wholesale Club', mark: 'K', providerType: 'Member services',
    request: 'Cancel my Kestrel Wholesale Club membership and ask about a membership-fee refund.',
    goal: 'Cancel membership', taskSummary: 'Cancel membership and ask about a refund',
    fields: ['name', 'phone_number', 'membership_last_four'],
    inquiry: 'Ask about cancellation and a membership-fee refund.',
    menu: 'Selecting membership services', explain: 'Requesting cancellation and refund details',
    approvalTitle: 'When should your membership end?',
    approvalIntro: 'Choose a change. Any refund is subject to confirmation.',
    options: [
      { id: 'cancel_now', title: 'Cancel membership now', detail: 'End membership access and ask about a membership-fee refund.', action: 'Approve cancellation', message: 'Cancel my membership now and ask about a refund.' },
      { id: 'stop_renewal', title: 'Only turn off auto-renewal', detail: 'Keep membership active for the current term. Don’t request a cancellation refund.', action: 'Approve renewal change', message: 'Keep my membership for now. Only turn off auto-renewal.' },
    ],
    confirmation: 'DEMO-KW-48291', callback: false,
  },
  bluefin: {
    id: 'bluefin_duplicate_charge_demo', task: 'WL-2050',
    provider: 'Bluefin Harbor Bank', mark: 'B', providerType: 'Credit card services · demo',
    request: 'Call Bluefin Harbor Bank about two $84.50 charges from Northstar Market on October 8. I think one is a duplicate.',
    goal: 'Investigate duplicate charge', taskSummary: 'Ask about a possible duplicate $84.50 charge',
    fields: ['name', 'phone_number'], menu: 'Selecting credit card billing support',
    explain: 'Asking to speak with a billing representative', options: [],
    confirmation: 'DEMO-BH-48292', callback: false, verifyFirst: true,
  },
});

// 脚本案例的工具结果（替代原型 localResult；只给这两条案例用）。caseId 是 SCRIPTED_CASES 的键；
// 至少 prepare_task、get_case_result 返回对象；Bluefin 案例另有 schedule_callback、preview_callback；
// caseId 不是这两条之一时抛错。
export function scriptedResult(caseId, toolName, args = {}) {
  if (!Object.hasOwn(SCRIPTED_CASES, caseId)) throw new Error('Unknown scripted case');
  const c = SCRIPTED_CASES[caseId];
  const base = { demo: true, task_id: c.task, scenario: c.id, real_booking: false };
  if (toolName === 'prepare_task') return { ...base, status: 'ready', missing: [], provider: c.provider, request: c.request, goal: c.goal, options: structuredClone(c.options), message: 'Scripted demo · no real call' };
  if (toolName === 'get_case_result') {
    const keep = args.option_id === 'stop_renewal';
    return { ...base, company_name: c.provider, outcome: c.verifyFirst ? 'needs_follow_up' : 'confirmed',
      headline: c.verifyFirst ? 'Billing inquiry recorded' : keep ? 'Auto-renewal turned off' : 'Membership canceled',
      verified: c.verifyFirst ? [] : [{ label: 'Membership', value: keep ? 'Active for its current term' : 'Cancellation complete (scripted)', evidence: { call_id: c.id, turn_seq: 1 } }],
      follow_ups: c.verifyFirst ? [{ label: 'Duplicate charge review', detail: 'Review pending · no refund confirmed. No dispute was submitted by this demo.' }] : keep ? [] : [{ label: 'Membership-fee refund', detail: 'Refund requested, not yet received' }],
      confirmation_number: c.confirmation, summary: 'Scripted demo · no real call or account change', calls: [], history: [],
    };
  }
  if (c.verifyFirst && toolName === 'schedule_callback') return { ...base, status: 'callback_scheduled', date: args.date, time: args.time, timezone: args.timezone, review_status: 'pending' };
  if (c.verifyFirst && toolName === 'preview_callback') return { ...base, review_status: 'completed', duplicate_charge: true, amount: 84.50, reversal_status: 'processing', credit_posted: false };
  throw new Error('Unknown scripted step');
}
