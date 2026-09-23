export type Turn = {
  id: string;
  start: number;
  end: number;
  text: string;
  speaker_id: string | null;
  overlap: boolean;
  speaker_uncertain: boolean;
  timestamp_uncertain?: boolean;
};
export type Action = {
  text: string;
  assignee: string | null;
  deadline_phrase: string | null;
  due_date: string | null;
  source_turn_ids: string[];
  confirmation_turn_ids: string[];
  needs_review: boolean;
  confirmation_checked?: boolean;
};
export type Meeting = {
  id: string;
  meeting_date: string;
  roster: string[];
  status: 'queued' | 'processing' | 'review' | 'failed';
  error: string | null;
  transcript: Turn[];
  summary: string;
  actions: Action[];
  speaker_names: Record<string, string>;
};
export type MeetingEdits = Partial<Pick<Meeting,
  'roster' | 'summary' | 'transcript' | 'actions' | 'speaker_names'
>>;
export type Exports = {docx: string; pdf: string | null; warning?: string};

function errorDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map(item => {
      if (typeof item === 'string') return item;
      if (item && typeof item === 'object' && 'msg' in item && typeof item.msg === 'string') return item.msg;
      return '';
    }).filter(Boolean);
    return messages.join('; ') || null;
  }
  return null;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try { response = await fetch(path, options); }
  catch { throw new Error('Не удалось связаться с сервером. Проверьте соединение и повторите.'); }
  if (!response.ok) {
    let detail = `Ошибка сервера (${response.status})`;
    try { detail = errorDetail((await response.json()).detail) || detail; }
    catch { /* An error response may be empty or contain HTML. */ }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export const getMeeting = (id: string) => request<Meeting>(`/meetings/${encodeURIComponent(id)}`);
export const createMeeting = (form: FormData) => request<Meeting>('/meetings', {method: 'POST', body: form});
export const startProcessing = (id: string) => request<Meeting>(`/meetings/${encodeURIComponent(id)}/process`, {method: 'POST'});
export const saveMeeting = (id: string, edits: MeetingEdits) => request<Meeting>(`/meetings/${encodeURIComponent(id)}`, {
  method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(edits),
});
export const exportMeeting = (id: string, format: 'docx' | 'pdf' = 'docx') => request<Exports>(
  `/meetings/${encodeURIComponent(id)}/export?format=${format}`, {method: 'POST'},
);
