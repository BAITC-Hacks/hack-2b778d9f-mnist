import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import App from './App';
import {createMeeting, exportMeeting, getMeeting, saveMeeting, startProcessing, type Meeting} from './api';

vi.mock('./api', () => ({
  getMeeting: vi.fn(), createMeeting: vi.fn(), startProcessing: vi.fn(),
  saveMeeting: vi.fn(), exportMeeting: vi.fn(),
}));

function fixture(overrides: Partial<Meeting> = {}): Meeting {
  return {
    id: 'demo', meeting_date: '2026-09-23', roster: ['Айгүл'], status: 'review', error: null,
    summary: 'Обсудили аудит.', speaker_names: {},
    transcript: [{id: 't1', start: 2, end: 4, text: 'Фиксируем аудит', speaker_id: null,
      overlap: false, speaker_uncertain: true}],
    actions: [{text: 'Провести аудит', assignee: null, deadline_phrase: null, due_date: null,
      source_turn_ids: ['t1'], confirmation_turn_ids: ['t1'], needs_review: true, confirmation_checked: false}],
    ...overrides,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  window.history.replaceState(null, '', '/?meeting=demo');
  vi.mocked(getMeeting).mockResolvedValue(fixture());
  vi.mocked(startProcessing).mockResolvedValue(fixture());
  vi.mocked(exportMeeting).mockResolvedValue({docx: '/meetings/demo/export/docx', pdf: null});
  vi.mocked(saveMeeting).mockImplementation(async (_id, edits) => fixture(edits));
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

it('uploads the original MP3, date and roster and renders a completed process response', async () => {
  window.history.replaceState(null, '', '/');
  vi.mocked(createMeeting).mockResolvedValue(fixture({status: 'queued', transcript: [], actions: []}));
  render(<App/>);
  const upload = screen.getByLabelText(/Аудио MP3 или WAV/);
  expect(upload.getAttribute('accept')).toContain('.mp3');
  fireEvent.change(screen.getByLabelText('Дата совещания'), {target: {value: '2026-09-23'}});
  fireEvent.change(screen.getByLabelText(/Участники, через запятую/), {target: {value: 'Айгүл, Алия\nЕрлан'}});
  const file = new File(['recording'], 'Совещание №1.mp3', {type: 'audio/mpeg'});
  fireEvent.change(upload, {target: {files: [file]}});
  fireEvent.submit(upload.closest('form')!);
  await screen.findByDisplayValue('Провести аудит');
  const form = vi.mocked(createMeeting).mock.calls[0][0];
  expect(form.get('audio')).toBe(file);
  expect(form.get('meeting_date')).toBe('2026-09-23');
  expect(form.get('roster')).toBe(JSON.stringify(['Айгүл', 'Алия', 'Ерлан']));
  expect(startProcessing).toHaveBeenCalledWith('demo');
  expect(location.search).toBe('?meeting=demo');
});

it('keeps a queued upload recoverable when the processing request fails', async () => {
  window.history.replaceState(null, '', '/');
  const queued = fixture({status: 'queued', transcript: [], actions: []});
  vi.mocked(createMeeting).mockResolvedValue(queued);
  vi.mocked(getMeeting).mockResolvedValue(queued);
  vi.mocked(startProcessing).mockRejectedValueOnce(new Error('Соединение прервано')).mockResolvedValueOnce(fixture());
  render(<App/>);
  const input = screen.getByLabelText(/Аудио MP3 или WAV/);
  fireEvent.change(input, {target: {files: [new File(['wav'], 'meeting.wav', {type: 'audio/wav'})]}});
  fireEvent.submit(input.closest('form')!);
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Соединение прервано');
  fireEvent.click(screen.getByRole('button', {name: 'Запустить обработку'}));
  await screen.findByDisplayValue('Провести аудит');
  expect(startProcessing).toHaveBeenCalledTimes(2);
  expect(createMeeting).toHaveBeenCalledTimes(1);
});

it('retries a failed recording opened by its saved URL', async () => {
  vi.mocked(getMeeting).mockResolvedValue(fixture({status: 'failed', error: 'Модель недоступна'}));
  render(<App/>);
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Модель недоступна');
  fireEvent.click(screen.getByRole('button', {name: 'Повторить обработку'}));
  await screen.findByDisplayValue('Провести аудит');
  expect(startProcessing).toHaveBeenCalledWith('demo');
});

it('polls processing until review becomes available', async () => {
  vi.useFakeTimers();
  vi.mocked(getMeeting).mockResolvedValueOnce(fixture({status: 'processing'})).mockResolvedValueOnce(fixture());
  await act(async () => { render(<App/>); });
  expect(screen.getByRole('heading', {name: 'Обработка записи'})).toBeTruthy();
  await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
  expect(screen.getByRole('heading', {name: 'Черновик готов к проверке'})).toBeTruthy();
  expect(screen.getByDisplayValue('Провести аудит')).toBeTruthy();
});

it('saves only changed fields, prevents edits during save, and starts a new meeting after save', async () => {
  let completeSave!: (meeting: Meeting) => void;
  vi.mocked(saveMeeting).mockReturnValue(new Promise(resolve => { completeSave = resolve; }));
  render(<App/>);
  const summary = await screen.findByLabelText('Краткое содержание');
  fireEvent.change(summary, {target: {value: 'Проверенное содержание'}});
  fireEvent.click(screen.getByRole('tab', {name: 'Экспорт'}));
  expect(screen.getByRole('button', {name: 'Создать DOCX'}).matches(':disabled')).toBe(true);
  expect(screen.getByRole('button', {name: 'Новая запись'}).matches(':disabled')).toBe(true);
  fireEvent.click(screen.getByRole('button', {name: 'Сохранить правки'}));
  expect(saveMeeting).toHaveBeenCalledWith('demo', {summary: 'Проверенное содержание'});
  expect(summary.matches(':disabled')).toBe(true);
  expect(screen.getByLabelText('Действие').matches(':disabled')).toBe(true);
  await act(async () => completeSave(fixture({summary: 'Проверенное содержание'})));
  expect(summary.matches(':disabled')).toBe(false);
  expect(screen.getByRole('button', {name: 'Сохранить правки'}).matches(':disabled')).toBe(true);
  fireEvent.click(screen.getByRole('button', {name: 'Новая запись'}));
  expect(screen.getByLabelText(/Аудио MP3 или WAV/)).toBeTruthy();
  expect(location.search).toBe('');
});

it('allows a verified speaker assignment and requires saving changed evidence before confirmation', async () => {
  const initial = fixture();
  initial.actions[0] = {...initial.actions[0], needs_review: false, confirmation_checked: true};
  vi.mocked(getMeeting).mockResolvedValue(initial);
  let saved = initial;
  vi.mocked(saveMeeting).mockImplementation(async (_id, edits) => {
    saved = {...saved, ...edits};
    return saved;
  });
  render(<App/>);
  const speaker = await screen.findByLabelText('Говорящий в реплике t1');
  fireEvent.change(speaker, {target: {value: '__new__'}});
  fireEvent.change(screen.getByLabelText('Проверенное имя для MANUAL_1'), {target: {value: 'Айгүл'}});
  expect(screen.getAllByText('t1 · Айгүл').length).toBeGreaterThan(0);
  const confirmation = screen.getByLabelText('Я проверил(а) подтверждение уполномоченным участником');
  expect(confirmation).toHaveProperty('checked', false);
  expect(confirmation.matches(':disabled')).toBe(true);
  fireEvent.click(screen.getByRole('button', {name: 'Сохранить правки'}));
  await waitFor(() => expect(confirmation.matches(':disabled')).toBe(false));
  expect(saved.transcript[0]).toMatchObject({speaker_id: 'MANUAL_1', speaker_uncertain: false});
  expect(saved.speaker_names).toEqual({MANUAL_1: 'Айгүл'});
  expect(saved.actions[0]).toMatchObject({needs_review: true, confirmation_checked: false});
  fireEvent.click(confirmation);
  fireEvent.click(screen.getByRole('button', {name: 'Сохранить правки'}));
  await waitFor(() => expect(saveMeeting).toHaveBeenCalledTimes(2));
  expect(Object.keys(vi.mocked(saveMeeting).mock.calls[1][1])).toEqual(['actions']);
  expect(saved.actions[0]).toMatchObject({needs_review: false, confirmation_checked: true});
});

it('validates the action and source selection before sending edits', async () => {
  render(<App/>);
  const action = await screen.findByLabelText('Действие');
  fireEvent.change(action, {target: {value: ' '}});
  fireEvent.click(screen.getByRole('button', {name: 'Сохранить правки'}));
  expect(screen.getByRole('alert').textContent).toContain('укажите действие');
  expect(saveMeeting).not.toHaveBeenCalled();
  fireEvent.change(action, {target: {value: 'Провести аудит'}});
  fireEvent.click(screen.getByText('Выбрать реплики: основание'));
  fireEvent.click(screen.getByLabelText('Основание: t1'));
  fireEvent.click(screen.getByRole('button', {name: 'Сохранить правки'}));
  expect(screen.getByRole('alert').textContent).toContain('выберите хотя бы одну реплику основания');
  expect(saveMeeting).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText('Основание: t1'));
  fireEvent.change(action, {target: {value: 'Провести согласованный аудит'}});
  fireEvent.click(screen.getByRole('button', {name: 'Сохранить правки'}));
  await waitFor(() => expect(saveMeeting).toHaveBeenCalledTimes(1));
});

it('saves an edited action before allowing its confirmation', async () => {
  render(<App/>);
  fireEvent.change(await screen.findByLabelText('Ответственный'), {target: {value: 'Айгүл'}});
  const confirmation = screen.getByLabelText('Я проверил(а) подтверждение уполномоченным участником');
  expect(confirmation.matches(':disabled')).toBe(true);
  fireEvent.click(screen.getByRole('button', {name: 'Сохранить правки'}));
  await waitFor(() => expect(confirmation.matches(':disabled')).toBe(false));
  expect(vi.mocked(saveMeeting).mock.calls[0][1].actions?.[0]).toMatchObject({
    assignee: 'Айгүл', confirmation_checked: false, needs_review: true,
  });
  fireEvent.click(confirmation);
  fireEvent.click(screen.getByRole('button', {name: 'Сохранить правки'}));
  await waitFor(() => expect(saveMeeting).toHaveBeenCalledTimes(2));
  expect(vi.mocked(saveMeeting).mock.calls[1][1].actions?.[0]).toMatchObject({
    assignee: 'Айгүл', confirmation_checked: true, needs_review: false,
  });
});

it('removes a speaker name when its last assigned transcript turn becomes uncertain', async () => {
  const meeting = fixture({speaker_names: {SPEAKER_00: 'Айгүл'}});
  meeting.transcript[0].speaker_id = 'SPEAKER_00';
  meeting.transcript[0].speaker_uncertain = false;
  vi.mocked(getMeeting).mockResolvedValue(meeting);
  render(<App/>);
  fireEvent.change(await screen.findByLabelText('Говорящий в реплике t1'), {target: {value: ''}});
  expect(screen.queryByLabelText('Проверенное имя для SPEAKER_00')).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: 'Сохранить правки'}));
  await waitFor(() => expect(saveMeeting).toHaveBeenCalledOnce());
  expect(vi.mocked(saveMeeting).mock.calls[0][1]).toMatchObject({
    speaker_names: {}, transcript: [{speaker_id: null, speaker_uncertain: true}],
  });
});

it('keeps the reviewed draft after a save failure', async () => {
  vi.mocked(saveMeeting).mockRejectedValue(new Error('Не удалось сохранить'));
  render(<App/>);
  fireEvent.change(await screen.findByLabelText('Краткое содержание'), {target: {value: 'Важные правки'}});
  fireEvent.click(screen.getByRole('button', {name: 'Сохранить правки'}));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Не удалось сохранить');
  expect(screen.getByDisplayValue('Важные правки')).toBeTruthy();
  expect(screen.getByRole('button', {name: 'Сохранить правки'}).matches(':disabled')).toBe(false);
  fireEvent.click(screen.getByRole('tab', {name: 'Экспорт'}));
  expect(screen.getByRole('button', {name: 'Создать DOCX'}).matches(':disabled')).toBe(true);
});

it('offers DOCX independently and preserves its download when PDF conversion fails', async () => {
  vi.mocked(exportMeeting).mockResolvedValue({
    docx: '/meetings/demo/export/docx', pdf: null, warning: 'PDF недоступен; DOCX готов.',
  });
  render(<App/>);
  fireEvent.click(await screen.findByRole('tab', {name: 'Экспорт'}));
  fireEvent.click(screen.getByRole('button', {name: 'Создать DOCX и PDF'}));
  const download = await screen.findByRole('link', {name: 'Скачать DOCX'});
  expect(download.getAttribute('href')).toBe('/meetings/demo/export/docx');
  expect(screen.queryByRole('link', {name: 'Скачать PDF'})).toBeNull();
  expect(screen.getByRole('status').textContent).toContain('DOCX готов');
  expect(exportMeeting).toHaveBeenLastCalledWith('demo', 'pdf');
  fireEvent.click(screen.getByRole('button', {name: 'Создать DOCX'}));
  await waitFor(() => expect(exportMeeting).toHaveBeenLastCalledWith('demo', 'docx'));
});

it('blocks edits during export and exposes an export error without losing the draft', async () => {
  let failExport!: (error: Error) => void;
  vi.mocked(exportMeeting).mockReturnValue(new Promise((_resolve, reject) => { failExport = reject; }));
  render(<App/>);
  fireEvent.click(await screen.findByRole('tab', {name: 'Экспорт'}));
  fireEvent.click(screen.getByRole('button', {name: 'Создать DOCX'}));
  expect(screen.getByLabelText('Реплика t1').matches(':disabled')).toBe(true);
  await act(async () => failExport(new Error('Документ не создан')));
  expect(screen.getByRole('alert').textContent).toBe('Документ не создан');
  expect(screen.getByLabelText('Реплика t1').matches(':disabled')).toBe(false);
  expect(screen.getByDisplayValue('Провести аудит')).toBeTruthy();
});

it('plays only the selected transcript fragment and reports playback failures', async () => {
  const {container} = render(<App/>);
  fireEvent.click(await screen.findByRole('tab', {name: 'Стенограмма'}));
  const play = screen.getByRole('button', {name: 'Прослушать фрагмент'});
  const audio = container.querySelector('audio')!;
  fireEvent.click(play);
  expect(audio.currentTime).toBe(2);
  expect(HTMLMediaElement.prototype.play).toHaveBeenCalledOnce();
  audio.currentTime = 4;
  fireEvent.timeUpdate(audio);
  expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledOnce();
  vi.mocked(HTMLMediaElement.prototype.play).mockRejectedValueOnce(new Error('Unsupported audio'));
  fireEvent.click(play);
  expect((await screen.findByRole('alert')).textContent).toContain('Не удалось воспроизвести запись');
});


it('shows one section at a time and keeps unsaved edits when switching tabs', async () => {
  render(<App/>);
  await screen.findByRole('tab', {name: 'Обзор'});
  expect(screen.getAllByRole('tabpanel')).toHaveLength(1);
  expect(screen.getByRole('tabpanel').getAttribute('id')).toBe('panel-overview');
  fireEvent.change(screen.getByLabelText('Краткое содержание'), {target: {value: 'Сохранить этот текст'}});
  fireEvent.click(screen.getByRole('tab', {name: 'Поручения'}));
  expect(screen.getAllByRole('tabpanel')).toHaveLength(1);
  expect(screen.getByRole('tabpanel').getAttribute('id')).toBe('panel-actions');
  fireEvent.click(screen.getByRole('tab', {name: 'Обзор'}));
  expect(screen.getByLabelText('Краткое содержание')).toHaveProperty('value', 'Сохранить этот текст');
  expect(screen.getByRole('button', {name: 'Сохранить правки'}).matches(':disabled')).toBe(false);
});

it('navigates sections with the keyboard and plays evidence in the transcript section', async () => {
  render(<App/>);
  const overview = await screen.findByRole('tab', {name: 'Обзор'});
  fireEvent.keyDown(overview, {key: 'ArrowDown'});
  expect(screen.getByRole('tab', {name: 'Стенограмма'}).getAttribute('aria-selected')).toBe('true');
  fireEvent.click(screen.getByRole('tab', {name: 'Поручения'}));
  fireEvent.click(screen.getAllByRole('button', {name: 'Прослушать t1'})[0]);
  expect(screen.getByRole('tabpanel').getAttribute('id')).toBe('panel-transcript');
  expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();
});
