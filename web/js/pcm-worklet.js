// AudioWorklet 采集处理器（C 线；tech §6.4；AC-14）。在 AudioWorkletGlobalScope 里运行，不是 ES 模块的导入目标，
// node:test 用 node:vm 加载它。每次 process 把输入的 Float32 帧经 this.port.postMessage 交给 handoff-audio.mjs。
// G15：注册名必须等于 handoff-audio.mjs 导出的 PCM_WORKLET_PROCESSOR（'pcm-capture'）；本文件不 import，名字写字面量。

class PcmCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    // todo(C): 取 inputs[0][0]（第一声道 Float32Array），复制后 this.port.postMessage(copy)；
    //          没有输入或声道为空时什么都不发；始终返回 true 保持运行。
    return true;
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor);
