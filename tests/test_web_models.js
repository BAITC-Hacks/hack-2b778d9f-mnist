// Behavioral tests for the browser script, with a small DOM sufficient for its model controls.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname, '..', 'src', 'meeting_protocol', 'web', 'app.js'), 'utf8');
const operations = ['asr', 'diarization', 'extract_tasks', 'verify_tasks', 'resolve_speakers', 'generate_summary'];
const catalog = {
  schema_version: 1,
  defaults: Object.fromEntries(operations.map(op => [op, `default-${op}`])),
  profiles: operations.flatMap(op => [
    {id: `default-${op}`, label: `Default ${op}`, operations: [op], availability: 'configured'},
    {id: `other-${op}`, label: `Other ${op}`, operations: [op], availability: 'configured'},
  ]),
};

function meeting(model_selection = {}) {
  return {
    id: 'meeting-1', title: 'Saved meeting', status: 'uploaded', has_audio: true,
    model_selection, summary: {text: ''}, decisions: [], tasks: [], speaker_mappings: [],
    transcript: [], attempts: [],
  };
}

function dom() {
  const fixed = new Map();
  const dynamic = new Map();
  class Element {
    constructor(id = '') {
      this.id = id;
      this.value = '';
      this.textContent = '';
      this.children = [];
      this.controls = [];
      this.disabled = false;
      this._html = '';
    }
    get innerHTML() { return this._html; }
    set innerHTML(html) {
      this._html = html;
      this.controls = [];
      for (const match of html.matchAll(/<select data-model="([^"]+)">([\s\S]*?)<\/select>/g)) {
        const control = new Element();
        control.dataset = {model: match[1]};
        const options = [...match[2].matchAll(/<option value="([^"]*)"([^>]*)>/g)];
        control.value = options.find(option => /\bselected\b/.test(option[2]))?.[1] ?? options[0]?.[1] ?? '';
        this.controls.push(control);
      }
      if (this.id === 'detail') {
        dynamic.clear();
        for (const match of html.matchAll(/<button id="([^"]+)"([^>]*)>/g)) {
          const button = new Element(match[1]);
          button.disabled = /\bdisabled\b/.test(match[2]);
          dynamic.set(match[1], button);
        }
        if (html.includes('<audio id="player"')) dynamic.set('player', new Element('player'));
      }
    }
    querySelectorAll(selector) { return selector === '[data-model]' ? this.controls : []; }
    querySelector(selector) {
      const match = selector.match(/^\[data-model="([^"]+)"\]$/);
      return match ? this.controls.find(control => control.dataset.model === match[1]) ?? null : null;
    }
    replaceChildren() { this.children = []; }
    append(child) { this.children.push(child); }
  }
  for (const id of ['key', 'error', 'createModels', 'meetings', 'detail', 'connect', 'create', 'title', 'date', 'participants']) fixed.set(id, new Element(id));
  return {
    getElementById(id) { return dynamic.get(id) ?? fixed.get(id) ?? null; },
    querySelectorAll() { return []; },
    createElement() { return new Element(); },
  };
}

async function tick() { await new Promise(resolve => setImmediate(resolve)); }

function harness({saved = {}, initialKey = '', catalogResponse = () => catalog} = {}) {
  const document = dom();
  const values = new Map([['meetingKey', initialKey]]);
  const sessionStorage = {
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, value); },
  };
  const calls = [];
  const fetch = async (url, options = {}) => {
    calls.push({url, options});
    const key = options.headers?.['X-Agent-API-Key'];
    let result;
    if (url === '/api/v1/model-profiles') result = catalogResponse(key);
    else if (url === '/api/v1/meetings') result = [{id: 'meeting-1', title: 'Saved meeting', status: 'uploaded'}];
    else if (url === '/api/v1/meetings/meeting-1') result = meeting(saved);
    else if (url === '/api/v1/meetings/meeting-1/audio') result = {};
    else if (url === '/api/v1/meetings/meeting-1/process') result = {};
    else throw Error(`Unexpected test request: ${url}`);
    if (typeof result === 'number') return {ok: false, status: result};
    return {ok: true, status: 200, json: async () => result, blob: async () => result};
  };
  const context = vm.createContext({
    document, sessionStorage, fetch, setInterval() {},
    URL: {createObjectURL() { return 'blob:meeting'; }, revokeObjectURL() {}},
  });
  vm.runInContext(app, context, {filename: 'app.js'});
  return {document, sessionStorage, calls};
}

async function openSaved(h) {
  await tick();
  assert.equal(h.document.getElementById('meetings').children.length, 1);
  await h.document.getElementById('meetings').children[0].onclick();
}

function processingCalls(h) {
  return h.calls.filter(call => call.url === '/api/v1/meetings/meeting-1/process');
}

test('catalog 401 recovers on Connect and reloads selected meeting with the stored key', async () => {
  const h = harness({initialKey: 'expired', catalogResponse: key => key === 'valid' ? catalog : 401});
  await openSaved(h);
  assert.equal(h.document.getElementById('saveModels').disabled, true);
  assert.match(h.document.getElementById('detail').innerHTML, /Saved meeting/);

  h.document.getElementById('key').value = 'valid';
  await h.document.getElementById('connect').onclick();
  assert.equal(h.sessionStorage.getItem('meetingKey'), 'valid');
  assert.equal(h.document.getElementById('saveModels').disabled, false);
  assert.ok(h.document.getElementById('detail').querySelector('[data-model="asr"]'));
  assert.equal(h.calls.filter(call => call.url === '/api/v1/model-profiles').length, 2);
  assert.equal(h.calls.filter(call => call.url === '/api/v1/meetings/meeting-1').length, 2);
  assert.equal(h.calls.at(-2).options.headers['X-Agent-API-Key'], 'valid');
});

test('unavailable catalog disables Save; processing retains saved override without replacement', async () => {
  const h = harness({saved: {asr: 'saved-asr'}, catalogResponse: () => 503});
  await openSaved(h);
  assert.equal(h.document.getElementById('saveModels').disabled, true);
  assert.match(h.document.getElementById('detail').innerHTML, /saved-asr/);
  await h.document.getElementById('process').onclick();
  assert.equal(processingCalls(h).length, 1);
  assert.equal(processingCalls(h)[0].options.method, 'POST');
  assert.equal(processingCalls(h)[0].options.body, undefined);
});

test('removed saved profile remains selected; only a user change replaces it', async () => {
  const h = harness({saved: {asr: 'retired-asr'}});
  await openSaved(h);
  assert.equal(h.document.getElementById('detail').querySelector('[data-model="asr"]').value, 'retired-asr');
  assert.match(h.document.getElementById('detail').innerHTML, /no longer listed/);
  await h.document.getElementById('process').onclick();
  assert.equal(processingCalls(h)[0].options.body, undefined);

  h.document.getElementById('detail').querySelector('[data-model="asr"]').value = 'other-asr';
  await h.document.getElementById('process').onclick();
  assert.deepEqual(JSON.parse(processingCalls(h)[1].options.body), {model_selection: {asr: 'other-asr'}});
});

test('listed saved profile sends no replacement until the user clears its selection', async () => {
  const h = harness({saved: {asr: 'other-asr'}});
  await openSaved(h);
  assert.equal(h.document.getElementById('detail').querySelector('[data-model="asr"]').value, 'other-asr');
  await h.document.getElementById('process').onclick();
  assert.equal(processingCalls(h)[0].options.body, undefined);

  h.document.getElementById('detail').querySelector('[data-model="asr"]').value = '';
  await h.document.getElementById('process').onclick();
  assert.deepEqual(JSON.parse(processingCalls(h)[1].options.body), {model_selection: {}});
});
