// AC-16：模拟语音只插入模拟文字并显示「Simulated voice」，不碰麦克风（PRD R11、Q8；tech §7.1）。
// 公开接口：startSimulatedVoice({text, insertText, showLabel}) -> {cancel()}。
// 可能有的演示延时用 node:test 的假定时器推进，不真实等待。

import test from 'node:test';
import assert from 'node:assert/strict';

import { startSimulatedVoice } from '../js/voice.mjs';
import { createFakeMediaDevices } from './helpers.mjs';
import { settle } from './c-support.mjs';

// 在 globalThis.navigator 上装假 mediaDevices，并记录 MediaRecorder / AudioContext 的构造
function installMicSpies() {
  const md = createFakeMediaDevices();
  const constructed = [];
  const nav = globalThis.navigator;
  const prevMd = Object.getOwnPropertyDescriptor(nav, 'mediaDevices');
  Object.defineProperty(nav, 'mediaDevices', { value: md, configurable: true, writable: true });
  const prevRec = globalThis.MediaRecorder;
  const prevCtx = globalThis.AudioContext;
  globalThis.MediaRecorder = class { constructor() { constructed.push('MediaRecorder'); } static isTypeSupported() { return true; } };
  globalThis.AudioContext = class { constructor() { constructed.push('AudioContext'); } };
  return {
    md,
    constructed,
    restore() {
      if (prevMd) Object.defineProperty(nav, 'mediaDevices', prevMd);
      else delete nav.mediaDevices;
      if (prevRec === undefined) delete globalThis.MediaRecorder; else globalThis.MediaRecorder = prevRec;
      if (prevCtx === undefined) delete globalThis.AudioContext; else globalThis.AudioContext = prevCtx;
    },
  };
}

async function drain(t) {
  for (let i = 0; i < 6; i += 1) {
    t.mock.timers.runAll();
    await settle(1);
  }
}

test('ac16_voice_button_inserts_simulated_text_with_label_and_no_microphone', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  const spies = installMicSpies();
  try {
    const inserted = [];
    const labels = [];
    const text = 'Start electricity at my new apartment on November 1.';
    const session = startSimulatedVoice({ text, insertText: (x) => inserted.push(x), showLabel: (x) => labels.push(x) });
    assert.equal(typeof session?.cancel, 'function', 'returns { cancel() }');
    await drain(t);
    assert.deepEqual(inserted, [text], 'the simulated transcript is inserted once, verbatim');
    assert.ok(labels.some((l) => String(l).includes('Simulated voice')), `label says "Simulated voice", got ${JSON.stringify(labels)}`);
    assert.equal(spies.md.calls.length, 0, 'getUserMedia is never called');
    assert.deepEqual(spies.constructed, [], 'no MediaRecorder or AudioContext');
  } finally {
    spies.restore();
  }
});
