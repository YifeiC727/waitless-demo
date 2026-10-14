// Local DOM stand-in: retain slots and dispatch the same delegated events as the page.
export function documentHarness() {
  const doc = new EventTarget();
  const nodes = new Map();
  function nodeFor(id) {
    if (nodes.has(id)) return nodes.get(id);
    const node = new EventTarget();
    let html = '';
    Object.assign(node, { textContent: '', value: '', hidden: false, isConnected: true,
      insertAdjacentHTML(_position, value) { this.innerHTML += value; },
      remove() { nodes.delete(id); this.isConnected = false; },
    });
    Object.defineProperty(node, 'innerHTML', {
      get: () => html,
      set(value) {
        html = value;
        for (const match of value.matchAll(/\bid="([^"]+)"/g)) nodeFor(match[1]);
        for (const match of value.matchAll(/<div id="([^"]+)" class="tool-card">([\s\S]*?)<\/div>/g)) nodeFor(match[1]).innerHTML = match[2];
      },
    });
    nodes.set(id, node);
    return node;
  }
  for (const id of ['messages', 'callView', 'chatView', 'traceView', 'error', 'chat', 'topActions', 'mode', 'prompt', 'composer', 'hero']) nodeFor(id);
  nodes.get('callView').hidden = true;
  nodes.get('traceView').hidden = true;
  nodes.get('error').hidden = true;
  doc.querySelector = (selector) => nodes.get(selector.slice(1)) ?? null;
  doc.querySelectorAll = () => [];
  doc.defaultView = new EventTarget();
  function click(action, extras = {}) {
    const button = { disabled: false, dataset: { action, ...extras }, closest: () => button };
    const event = new Event('click');
    Object.defineProperty(event, 'target', { value: button });
    doc.dispatchEvent(event);
  }
  return { doc, nodes, click };
}
