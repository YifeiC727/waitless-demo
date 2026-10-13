// 原型另两条脚本案例（C 线；Q1、R57、§3.2；AC-17）。原 localResult 逻辑，界面标明是脚本演示，不接真实引擎。
// 品牌一律换成虚构的 Kestrel Wholesale Club（会员卡取消）与 Bluefin Harbor Bank（重复扣款核查）。
// r2（第 3 步 C 线缺口 G6，暂定，Jev 0.58）：按 tech §6.1，schedule_callback 与 preview_callback 只作为脚本案例里的
// 本地演示步骤保留（Bluefin Harbor Bank 案例的回拨时间与回拨预览），不进 MCP、不经 mcp.callTool、不做真实回拨；
// schedule_callback 的结果带 real_booking:false。
//
// SCRIPTED_CASES：两项，键名自定；每项至少有 provider（虚构品牌名）、request（用户请求原文，写虚构品牌）字符串，
// 其余字段（id、task、goal、options、confirmation 等）从原型 CASES 移植并改用虚构品牌；不含主线公司 Lakeshore Energy。

// todo(C): 从原型 CASES 与 localResult 移植两条案例；字段、选项、确认号改用虚构品牌。
export const SCRIPTED_CASES = Object.freeze({});

// 脚本案例的工具结果（替代原型 localResult；只给这两条案例用）。caseId 是 SCRIPTED_CASES 的键；
// 至少 prepare_task、get_case_result 返回对象；Bluefin 案例另有 schedule_callback、preview_callback；
// caseId 不是这两条之一时抛错。
export function scriptedResult(caseId, toolName, args = {}) {
  throw new Error('not implemented');
}
