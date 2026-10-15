import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../../index.html', import.meta.url), 'utf8');

test('topbar buttons explicitly use a non-white text color on their white background', () => {
  const declarations = [...html.matchAll(/\.top-actions button\{([^}]*)\}/g)]
    .map((match) => match[1]);
  const colors = declarations.flatMap((body) =>
    [...body.matchAll(/(?:^|;)\s*color\s*:\s*([^;]+)/gi)]
      .map((match) => match[1].trim().toLowerCase()));

  assert.ok(colors.length > 0, 'topbar button rules must explicitly declare color');
  assert.ok(!['#fff', '#ffffff', 'white'].includes(colors.at(-1)),
    `the last topbar button color must not be white: ${colors.at(-1)}`);
});

test('topbar retains the New task button and its reset action', () => {
  assert.ok(html.includes('<button type="button" data-action="reset">New task</button>'));
});
