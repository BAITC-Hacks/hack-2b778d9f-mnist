import {afterEach, expect, it, vi} from 'vitest';
import {exportMeeting, getMeeting} from './api';

afterEach(() => vi.unstubAllGlobals());

it('requests a DOCX without invoking PDF conversion and accepts partial PDF success', async () => {
  const result = {docx: '/document.docx', pdf: null, warning: 'PDF недоступен'};
  const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify(result), {status: 200}));
  vi.stubGlobal('fetch', fetch);
  await expect(exportMeeting('meeting one')).resolves.toEqual(result);
  expect(fetch).toHaveBeenCalledWith('/meetings/meeting%20one/export?format=docx', {method: 'POST'});
  await exportMeeting('meeting one', 'pdf');
  expect(fetch).toHaveBeenLastCalledWith('/meetings/meeting%20one/export?format=pdf', {method: 'POST'});
});

it('renders structured validation errors as readable messages', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
    detail: [{loc: ['body', 'actions'], msg: 'Укажите основание'}, {msg: 'Проверьте срок'}],
  }), {status: 422})));
  await expect(getMeeting('demo')).rejects.toThrow('Укажите основание; Проверьте срок');
});

it('explains network failures and handles a non-JSON server error', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch'))
    .mockResolvedValueOnce(new Response('Unavailable', {status: 503})));
  await expect(getMeeting('demo')).rejects.toThrow('Не удалось связаться с сервером');
  await expect(getMeeting('demo')).rejects.toThrow('Ошибка сервера (503)');
});
