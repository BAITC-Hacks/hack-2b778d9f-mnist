import {useEffect, useRef, useState, type FormEvent} from 'react';
import {
  createMeeting, exportMeeting, getMeeting, saveMeeting, startProcessing,
  type Action, type Exports, type Meeting, type MeetingEdits, type Turn,
} from './api';

const tabs = [{id: 'overview', label: 'Обзор', mark: '01'}, {id: 'transcript', label: 'Стенограмма', mark: '02'},
  {id: 'people', label: 'Участники', mark: '03'}, {id: 'actions', label: 'Поручения', mark: '04'},
  {id: 'export', label: 'Экспорт', mark: '05'}] as const;
type Tab = typeof tabs[number]['id'];

const editableFields = ['roster', 'summary', 'transcript', 'actions', 'speaker_names'] as const;
const actionFields = ['text', 'assignee', 'deadline_phrase', 'due_date', 'source_turn_ids', 'confirmation_turn_ids'] as const;
const parseRoster = (text: string) => [...new Set(text.split(/[,\n]/).map(name => name.trim()).filter(Boolean))];
const message = (error: unknown) => error instanceof Error ? error.message : String(error);
const same = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right);
const unconfirmed = (actions: Action[]) => actions.map(action => ({
  ...action, confirmation_checked: false, needs_review: true,
}));
const speakerName = (turn: Turn, names: Record<string, string>) =>
  turn.speaker_id ? names[turn.speaker_id]?.trim() || turn.speaker_id : 'Говорящий не определён';

function validateDraft(draft: Meeting): string | null {
  const ids = new Set(draft.transcript.map(turn => turn.id));
  for (const turn of draft.transcript) {
    if (turn.speaker_id?.startsWith('MANUAL_') && !draft.speaker_names[turn.speaker_id]?.trim()) {
      return `Укажите проверенное имя для ${turn.speaker_id} или оставьте говорящего неопределённым.`;
    }
  }
  for (const [index, action] of draft.actions.entries()) {
    const label = `Поручение ${index + 1}`;
    if (!action.text.trim()) return `${label}: укажите действие или удалите пустой кандидат.`;
    if (!action.source_turn_ids.length) return `${label}: выберите хотя бы одну реплику основания.`;
    if (action.assignee !== null && !action.assignee.trim()) return `${label}: укажите ответственного или очистите поле.`;
    if (action.deadline_phrase !== null && !action.deadline_phrase.trim()) return `${label}: укажите срок или очистите поле.`;
    if ([...action.source_turn_ids, ...action.confirmation_turn_ids].some(id => !ids.has(id))) {
      return `${label}: выбранная реплика отсутствует в стенограмме.`;
    }
    if (action.confirmation_checked && !action.confirmation_turn_ids.length) {
      return `${label}: выберите реплику подтверждения.`;
    }
    if (action.due_date && !/^\d{4}-\d{2}-\d{2}$/.test(action.due_date)) {
      return `${label}: проверьте дату исполнения.`;
    }
  }
  return null;
}

function EvidencePicker({title, selected, turns, names, onChange, onPlay}: {
  title: string;
  selected: string[];
  turns: Turn[];
  names: Record<string, string>;
  onChange: (ids: string[]) => void;
  onPlay: (id: string) => void;
}) {
  return <div className="evidence">
    <strong>{title}</strong>
    {selected.length === 0 && <p className="hint">Реплики не выбраны</p>}
    {selected.map(id => {
      const turn = turns.find(item => item.id === id);
      return turn && <blockquote key={id}>
        <a href={`#turn-${id}`} onClick={() => onPlay(id)}>{id} · {speakerName(turn, names)}</a>: {turn.text}
        {' '}<button type="button" className="textbutton" onClick={() => onPlay(id)}>Прослушать {id}</button>
      </blockquote>;
    })}
    <details><summary>Выбрать реплики: {title.toLowerCase()}</summary>
      {turns.map(turn => <label className="check evidence-option" key={turn.id}>
        <input type="checkbox" checked={selected.includes(turn.id)} aria-label={`${title}: ${turn.id}`}
          onChange={event => onChange(event.target.checked
            ? [...selected, turn.id] : selected.filter(id => id !== turn.id))}/>
        <span>{turn.id} · {speakerName(turn, names)} · {turn.start.toFixed(1)} с — {turn.text}</span>
      </label>)}
    </details>
  </div>;
}

export default function App() {
  const [tab, setTab] = useState<Tab>('overview');
  const [operation, setOperation] = useState<'save' | 'export' | null>(null);
  const [initialId] = useState(() => new URLSearchParams(location.search).get('meeting'));
  const [loading, setLoading] = useState(!!initialId);
  const [meeting, setMeeting] = useState<Meeting | null>(null);
  const [draft, setDraft] = useState<Meeting | null>(null);
  const [rosterText, setRosterText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [exports, setExports] = useState<Exports | null>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const fragmentEnd = useRef<number | null>(null);
  const id = meeting?.id;
  const dirty = !!meeting && !!draft && editableFields.some(key => !same(meeting[key], draft[key]));
  const evidenceChanged = !!meeting && !!draft && (['roster', 'transcript', 'speaker_names'] as const)
    .some(key => !same(meeting[key], draft[key]));
  const changedActions = draft?.actions.map((action, index) => {
    const saved = meeting?.actions[index];
    return !saved || actionFields.some(key => !same(action[key], saved[key]));
  }) || [];

  function acceptMeeting(value: Meeting) {
    const normalized = {...value, actions: value.actions.map(action => ({
      ...action, confirmation_checked: !!action.confirmation_checked,
    }))};
    setMeeting(normalized); setDraft(normalized); setRosterText(value.roster.join('\n'));
  }

  useEffect(() => {
    if (!initialId) return;
    let active = true;
    getMeeting(initialId).then(value => {
      if (active) acceptMeeting(value);
    }).catch(error => {
      if (active) setError(message(error));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [initialId]);

  useEffect(() => {
    if (!id || !['queued', 'processing'].includes(meeting?.status || '')) return;
    let active = true;
    let timer: number;
    async function poll() {
      try {
        const value = await getMeeting(id!);
        if (!active) return;
        acceptMeeting(value); setError('');
        if (!['queued', 'processing'].includes(value.status)) return;
      } catch (error) {
        if (active) setError(message(error));
      }
      if (active) timer = window.setTimeout(poll, 1200);
    }
    timer = window.setTimeout(poll, 1200);
    return () => { active = false; window.clearTimeout(timer); };
  }, [id, meeting?.status]);

  useEffect(() => {
    if (!dirty) return;
    const preventUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', preventUnload);
    return () => window.removeEventListener('beforeunload', preventUnload);
  }, [dirty]);

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const input = event.currentTarget.elements.namedItem('audio') as HTMLInputElement;
    const file = input.files?.[0];
    if (!file || !/\.(mp3|wav)$/i.test(file.name)) {
      setError('Выберите запись в формате MP3 или WAV.'); return;
    }
    if (!file.size || file.size > 200 * 1024 * 1024) {
      setError('Выберите непустую запись размером до 200 МБ.'); return;
    }
    form.set('audio', file);
    form.set('roster', JSON.stringify(parseRoster(String(form.get('roster') || ''))));
    setBusy(true); setError(''); setNotice('');
    try {
      const created = await createMeeting(form);
      history.replaceState(null, '', `?meeting=${encodeURIComponent(created.id)}`);
      acceptMeeting(created);
      acceptMeeting(await startProcessing(created.id));
    } catch (error) { setError(message(error)); }
    finally { setBusy(false); setOperation(null); }
  }

  async function retry() {
    if (!id) return;
    setBusy(true); setError('');
    try { acceptMeeting(await startProcessing(id)); }
    catch (error) { setError(message(error)); }
    finally { setBusy(false); setOperation(null); }
  }

  function newMeeting() {
    if (busy || dirty) return;
    audio.current?.pause(); fragmentEnd.current = null;
    setMeeting(null); setDraft(null); setExports(null); setError(''); setNotice('');
    setTab('overview');
    history.replaceState(null, '', location.pathname);
  }

  function edit(change: MeetingEdits) {
    if (!draft || busy) return;
    const changesEvidence = 'transcript' in change || 'roster' in change || 'speaker_names' in change;
    setDraft({...draft, ...change, ...(changesEvidence ? {actions: unconfirmed(draft.actions)} : {})});
    setExports(null); setNotice(''); setError('');
  }

  function editAction(index: number, change: Partial<Action>, reviewing = false) {
    if (!draft) return;
    edit({actions: draft.actions.map((action, position) => position === index
      ? {...action, ...change, ...(!reviewing ? {confirmation_checked: false, needs_review: true} : {})}
      : action)});
  }

  function assignSpeaker(index: number, selection: string) {
    if (!draft) return;
    let speaker = selection;
    if (speaker === '__new__') {
      const existing = new Set([...draft.transcript.map(turn => turn.speaker_id), ...Object.keys(draft.speaker_names)]);
      let sequence = 1;
      while (existing.has(`MANUAL_${sequence}`)) sequence++;
      speaker = `MANUAL_${sequence}`;
    }
    const transcript = draft.transcript.map((turn, position) => position === index
      ? {...turn, speaker_id: speaker || null, speaker_uncertain: !speaker} : turn);
    const remainingSpeakers = new Set(transcript.map(turn => turn.speaker_id));
    const speaker_names = Object.fromEntries(Object.entries(draft.speaker_names)
      .filter(([id]) => remainingSpeakers.has(id)));
    edit({transcript, speaker_names});
  }

  async function save() {
    if (!id || !draft || !meeting || busy) return;
    const validation = validateDraft(draft);
    if (validation) { setError(validation); return; }
    const changes: MeetingEdits = Object.fromEntries(editableFields
      .filter(key => !same(meeting[key], draft[key])).map(key => [key, draft[key]]));
    setBusy(true); setOperation('save'); setError('');
    try {
      acceptMeeting(await saveMeeting(id, changes)); setExports(null);
      setNotice(evidenceChanged || changedActions.some(Boolean)
        ? 'Правки сохранены. Проверьте подтверждение поручений по обновлённым данным.'
        : 'Правки сохранены.');
    } catch (error) { setError(message(error)); }
    finally { setBusy(false); setOperation(null); }
  }

  async function generate(format: 'docx' | 'pdf') {
    if (!id || busy || dirty) return;
    setBusy(true); setOperation('export'); setError(''); setNotice('');
    try { setExports(await exportMeeting(id, format)); }
    catch (error) { setError(message(error)); }
    finally { setBusy(false); setOperation(null); }
  }

  async function seek(turnId: string) {
    const turn = draft?.transcript.find(item => item.id === turnId);
    if (!turn || !audio.current) return;
    setTab('transcript');
    window.setTimeout(() => document.getElementById(`turn-${turnId}`)?.scrollIntoView?.({block: 'center', behavior: 'smooth'}), 0);
    try {
      fragmentEnd.current = turn.end;
      audio.current.currentTime = turn.start;
      await audio.current.play();
    } catch {
      fragmentEnd.current = null;
      setError('Не удалось воспроизвести запись. Попробуйте включить её в плеере.');
    }
  }

  const speakers = draft ? [...new Set([
    ...draft.transcript.map(turn => turn.speaker_id).filter((value): value is string => !!value),
    ...Object.keys(draft.speaker_names),
  ])] : [];

  return <main className="shell">
    <header className="page-header">
      <div className="eyebrow"><span className="brand-mark">J</span> Jinalys AI</div>
      <h1>Jinalys AI</h1>
      <p>От встречи к решениям — с проверкой каждого поручения.</p>
      {meeting && <button className="secondary" onClick={newMeeting} disabled={busy || dirty}>Новая запись</button>}
    </header>
    {error && <div role="alert" className="notice error">{error}</div>}
    {notice && <div role="status" className="notice success">{notice}</div>}
    {loading && <p role="status">Загрузка совещания…</p>}
    {!meeting && !loading && <section className="card">
      <h2>Новая запись</h2>
      <form onSubmit={upload}>
        <fieldset disabled={busy} className="form">
          <label>Дата совещания<input type="date" name="meeting_date" required/></label>
          <label>Участники, через запятую или с новой строки<textarea name="roster" placeholder="Необязательно"/></label>
          <label>Аудио MP3 или WAV, до 10 минут<input type="file" name="audio" accept=".mp3,.wav,audio/mpeg,audio/wav,audio/x-wav" required/></label>
          <p className="hint">До 200 МБ. Дата нужна для проверки относительных сроков.</p>
          <button>{busy ? 'Загрузка…' : 'Загрузить и обработать'}</button>
        </fieldset>
      </form>
    </section>}
    {meeting && <>
      <section className={`card status status-${meeting.status}`}>
        <div><span className="eyebrow">{meeting.meeting_date}</span>
          <h2>{meeting.status === 'review' ? 'Черновик готов к проверке'
            : meeting.status === 'failed' ? 'Обработка прервана'
              : meeting.status === 'queued' ? 'Запись ожидает обработки' : 'Обработка записи'}</h2>
        </div>
        {['queued', 'failed'].includes(meeting.status) && <button onClick={retry} disabled={busy}>
          {meeting.status === 'failed' ? 'Повторить обработку' : 'Запустить обработку'}
        </button>}
        {meeting.status === 'failed' && <p role="alert">{meeting.error}</p>}
        {['queued', 'processing'].includes(meeting.status) && <p>Результат появится автоматически. Ссылку на эту страницу можно сохранить.</p>}
      </section>
      {meeting.status === 'review' && draft && <>
        <div className="workspace">
          <aside className="sidebar">
            <div className="sidebar-title">Разделы протокола</div>
            <nav role="tablist" aria-label="Разделы протокола" aria-orientation="vertical"
              onKeyDown={event => {
                const position = tabs.findIndex(item => item.id === tab);
                const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
                  : ['ArrowDown', 'ArrowRight'].includes(event.key) ? (position + 1) % tabs.length
                    : ['ArrowUp', 'ArrowLeft'].includes(event.key) ? (position + tabs.length - 1) % tabs.length : -1;
                if (next < 0) return;
                event.preventDefault(); setTab(tabs[next].id);
                document.getElementById(`tab-${tabs[next].id}`)?.focus();
              }}>
              {tabs.map(item => <button key={item.id} id={`tab-${item.id}`} role="tab"
                aria-selected={tab === item.id} aria-controls={`panel-${item.id}`}
                tabIndex={tab === item.id ? 0 : -1} onClick={() => setTab(item.id)}>
                <span className="nav-number" aria-hidden="true">{item.mark}</span>{item.label}
                {item.id === 'transcript' && <span className="nav-count" aria-hidden="true">{draft.transcript.length}</span>}
                {item.id === 'actions' && <span className="nav-count" aria-hidden="true">{draft.actions.length}</span>}
              </button>)}
            </nav>
            <p className="sidebar-note">Проверьте результат перед согласованием. Правки сохраняются одной кнопкой.</p>
          </aside>
          <div className="workspace-content">
        <section className="audio-bar" aria-label="Исходная запись">
          <div className="audio-label"><span className="eyebrow">Аудиозапись</span><strong>Слушайте и сверяйте</strong></div>
          <audio ref={audio} controls src={`/meetings/${encodeURIComponent(meeting.id)}/audio`}
            onError={() => setError('Не удалось загрузить аудио. Проверьте соединение и обновите страницу.')}
            onTimeUpdate={() => {
              if (audio.current && fragmentEnd.current !== null && audio.current.currentTime >= fragmentEnd.current) {
                audio.current.pause(); fragmentEnd.current = null;
              }
            }} onPause={() => { fragmentEnd.current = null; }}/>
        </section>
        <fieldset disabled={busy} className="review-fields" aria-label="Редактирование протокола">
          <section className="card tab-panel" id="panel-people" role="tabpanel" aria-labelledby="tab-people" hidden={tab !== 'people'}>
            <div className="panel-heading"><span className="eyebrow">Кто участвовал</span><h2>Участники и голоса</h2></div>
            <label>Список участников<textarea value={rosterText} onChange={event => {
              setRosterText(event.target.value); edit({roster: parseRoster(event.target.value)});
            }}/></label>
            <p className="hint">Список участников не определяет, кому принадлежит голос. Укажите имя только после прослушивания и проверки.</p>
            <datalist id="participants">{draft.roster.map((name, index) => <option key={index} value={name}/>)}</datalist>
            {speakers.map(speaker => <label key={speaker}>Проверенное имя для {speaker}
              <input list="participants" value={draft.speaker_names[speaker] || ''} placeholder="Имя после проверки"
                onChange={event => edit({speaker_names: {...draft.speaker_names, [speaker]: event.target.value}})}/>
            </label>)}
          </section>
          <section className="card tab-panel" id="panel-transcript" role="tabpanel" aria-labelledby="tab-transcript" hidden={tab !== 'transcript'}>
            <div className="panel-heading"><span className="eyebrow">Текст записи</span><h2>Реплики <span className="heading-count">{draft.transcript.length}</span></h2><p className="hint">Слушайте фрагменты, исправляйте текст и уточняйте говорящих.</p></div>
            {!draft.transcript.length && <p className="hint">Речь не распознана. Проверьте исходную запись.</p>}
            {draft.transcript.map((turn, index) => <article className="turn" id={`turn-${turn.id}`} key={turn.id}>
              <div className="turnhead">
                <strong>{turn.id} · {speakerName(turn, draft.speaker_names)}</strong>
                <span>{turn.start.toFixed(1)}–{turn.end.toFixed(1)} с</span>
                <button className="textbutton" onClick={() => void seek(turn.id)}>Прослушать фрагмент</button>
              </div>
              {turn.overlap && <span className="tag">Одновременная речь</span>}
              {turn.speaker_uncertain && <span className="tag">Говорящий требует проверки</span>}
              {turn.timestamp_uncertain && <span className="tag">Время приблизительно</span>}
              <label>Говорящий в реплике {turn.id}
                <select value={turn.speaker_id || ''} onChange={event => assignSpeaker(index, event.target.value)}>
                  <option value="">Говорящий не определён</option>
                  {speakers.map(speaker => <option value={speaker} key={speaker}>{draft.speaker_names[speaker] || speaker}</option>)}
                  <option value="__new__">Добавить новый голос после проверки</option>
                </select>
              </label>
              <textarea aria-label={`Реплика ${turn.id}`} value={turn.text} onChange={event => edit({
                transcript: draft.transcript.map((item, position) => position === index ? {...item, text: event.target.value} : item),
              })}/>
            </article>)}
          </section>
          <section className="card tab-panel" id="panel-overview" role="tabpanel" aria-labelledby="tab-overview" hidden={tab !== 'overview'}>
            <div className="panel-heading"><span className="eyebrow">Результат встречи</span><h2>Обзор совещания</h2><p className="hint">Начните с содержания, затем проверьте реплики и поручения.</p></div>
            <div className="stats-grid">
              <button className="stat" onClick={() => setTab('transcript')}><strong>{draft.transcript.length}</strong><span>Реплик в записи ↗</span></button>
              <button className="stat" onClick={() => setTab('people')}><strong>{speakers.length}</strong><span>Различимых голосов ↗</span></button>
              <button className="stat" onClick={() => setTab('actions')}><strong>{draft.actions.filter(action => action.needs_review).length}</strong><span>Поручений на проверку ↗</span></button>
            </div>
            <h3>Краткое содержание</h3>
            <textarea aria-label="Краткое содержание" value={draft.summary} onChange={event => edit({summary: event.target.value})}/>
          </section>
          <section className="card tab-panel" id="panel-actions" role="tabpanel" aria-labelledby="tab-actions" hidden={tab !== 'actions'}>
            <div className="panel-heading"><span className="eyebrow">Решения и ответственность</span><h2>Поручения и кандидаты <span className="heading-count">{draft.actions.length}</span></h2></div>
            {!draft.actions.length && <div className="empty-state">Поручений пока нет. Добавьте кандидат, если действие было согласовано в записи.</div>}
            <p className="hint">Проверьте действие, ответственного, срок и подтверждение. Непроверенные кандидаты попадут в раздел «На уточнение».</p>
            {evidenceChanged && <p className="notice">Сначала сохраните изменения реплик и участников, затем подтвердите поручения.</p>}
            {draft.actions.map((action, index) => <article className="action" key={index} aria-label={`Поручение ${index + 1}`}>
              <div className="turnhead"><strong>{index + 1}. {action.needs_review ? 'На уточнение' : 'Поручение проверено'}</strong>
                <button className="textbutton" onClick={() => edit({actions: draft.actions.filter((_, position) => position !== index)})}>Удалить</button>
              </div>
              <label>Действие<input value={action.text} onChange={event => editAction(index, {text: event.target.value})}/></label>
              <div className="two">
                <label>Ответственный<input list="participants" value={action.assignee || ''} placeholder="Ответственный не указан"
                  onChange={event => editAction(index, {assignee: event.target.value || null})}/></label>
                <label>Срок как сказано<input value={action.deadline_phrase || ''} placeholder="Срок не указан"
                  onChange={event => editAction(index, {deadline_phrase: event.target.value || null, due_date: null})}/></label>
              </div>
              <label>Дата исполнения<input type="date" value={action.due_date || ''}
                onChange={event => editAction(index, {due_date: event.target.value || null})}/></label>
              {!action.assignee && <p className="hint">Ответственный не указан</p>}
              {!action.due_date && <p className="hint">Срок требует проверки</p>}
              <EvidencePicker title="Основание" selected={action.source_turn_ids} turns={draft.transcript}
                names={draft.speaker_names} onChange={ids => editAction(index, {source_turn_ids: ids})} onPlay={id => void seek(id)}/>
              <EvidencePicker title="Подтверждение" selected={action.confirmation_turn_ids} turns={draft.transcript}
                names={draft.speaker_names} onChange={ids => editAction(index, {confirmation_turn_ids: ids})} onPlay={id => void seek(id)}/>
              {!action.confirmation_turn_ids.length && <p className="hint">Чтобы подтвердить поручение, выберите реплику с его подтверждением.</p>}
              {changedActions[index] && <p className="hint">Сохраните изменения этого поручения, затем проверьте подтверждение.</p>}
              <label className="check">
                <input type="checkbox" checked={!!action.confirmation_checked}
                  disabled={evidenceChanged || changedActions[index] || !action.confirmation_turn_ids.length}
                  onChange={event => editAction(index, {confirmation_checked: event.target.checked, needs_review: !event.target.checked}, true)}/>
                Я проверил(а) подтверждение уполномоченным участником
              </label>
            </article>)}
            <button className="secondary" onClick={() => edit({actions: [...draft.actions, {
              text: '', assignee: null, deadline_phrase: null, due_date: null,
              source_turn_ids: [], confirmation_turn_ids: [], confirmation_checked: false, needs_review: true,
            }]})}>Добавить кандидат</button>
          </section>
          <section className="card tab-panel" id="panel-export" role="tabpanel" aria-labelledby="tab-export" hidden={tab !== 'export'}>
            <div className="panel-heading"><span className="eyebrow">Документ для согласования</span><h2>Экспорт протокола</h2><p className="hint">Скачайте черновик после проверки. Непроверенные поручения останутся в разделе «На уточнение».</p></div>
            <div className="export-options"><div><strong>DOCX</strong><p>Редактируемый документ для согласования.</p></div><div><strong>PDF</strong><p>Документ для чтения и печати, если конвертер доступен.</p></div></div>
            <div className="downloads">
            <button className="secondary" onClick={() => void generate('docx')} disabled={dirty}>Создать DOCX</button>
            <button className="secondary" onClick={() => void generate('pdf')} disabled={dirty}>Создать DOCX и PDF</button></div>
            {dirty && <p className="hint">Сохраните правки перед экспортом или загрузкой новой записи.</p>}
            {operation === 'export' && <p role="status">Подготовка документа…</p>}
          </section>
        </fieldset>
        {exports && <section className="card" hidden={tab !== 'export'}>
          <h2>Черновик для согласования</h2>
          {exports.warning && <p role="status" className="notice">{exports.warning}</p>}
          <div className="downloads"><a href={exports.docx}>Скачать DOCX</a>
            {exports.pdf && <a href={exports.pdf}>Скачать PDF</a>}
          </div>
          <p className="hint">Документ станет официальным протоколом после утверждения уполномоченным лицом.</p>
        </section>}
          </div>
        </div>
        <div className="save-bar">
          <div><strong>{dirty ? 'Есть несохранённые изменения' : 'Все изменения сохранены'}</strong><span>{dirty ? 'Сохраните перед экспортом и новой записью.' : 'Можно продолжить проверку или перейти к экспорту.'}</span></div>
          <button onClick={save} disabled={!dirty || busy}>{operation === 'save' ? 'Сохранение…' : 'Сохранить правки'}</button>
        </div>
      </>}
    </>}
  </main>;
}
