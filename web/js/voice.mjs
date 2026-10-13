// 模拟语音（C 线；Q8、R11；AC-16）。只插入模拟文字和「Simulated voice」标注，不调用 getUserMedia。

// insertText(text)：把模拟转写放进输入框；showLabel(text)：显示标注。返回 { cancel() }。
export function startSimulatedVoice({ text, insertText, showLabel } = {}) {
  throw new Error('not implemented');
}
