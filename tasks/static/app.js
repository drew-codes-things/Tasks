'use strict';

const VIEW_NAMES = { today: 'Today', upcoming: 'Upcoming', completed: 'Completed', bin: 'Bin', search: 'Search' };
const EMPTY_TEXT = {
  today: 'Nothing due today. Add a task above.',
  upcoming: 'Nothing due in the next seven days.',
  list: 'No open tasks in this list.',
  completed: 'Tasks you finish appear here.',
  bin: 'The bin is empty.',
  search: 'No tasks match.',
};
const DAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const REFRESH_MS = 60000;
const SEARCH_DELAY_MS = 250;
const PREVIEW_DELAY_MS = 200;
const TOAST_MS = 3500;
const WEEK_DAYS = 7;
const MS_PER_DAY = 86400000;
const INBOX_ID = 1;
const ICONS = {
  edit: 'M4 20h4L19 9l-4-4L4 16zM14 6l4 4',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  restore: 'M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5',
  forever: 'M4 7h16M6 7l1 13h10l1-13M9 7V4h6v3M10 11l4 5M14 11l-4 5',
  repeat: 'M17 2l4 4-4 4M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 0 1-3 3H3',
  calendar: 'M4 5h16v16H4zM4 9h16M8 3v4M16 3v4',
  list: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  subtasks: 'M4 4v12a2 2 0 0 0 2 2h4M10 8h10M14 18h6',
  close: 'M6 6l12 12M18 6 6 18',
};

const state = {
  view: 'today', listId: null, query: '', tasks: [], more: false, lists: [], counts: {}, today: '', csrf: '', importLimits: null,
  selectedId: null, editing: null, listDialogMode: 'new',
};
const byId = (id) => document.getElementById(id);

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(method, path, body, contentType = 'application/json') {
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
  let payload;
  if (body !== undefined) {
    headers['Content-Type'] = contentType;
    payload = contentType === 'application/json' ? JSON.stringify(body) : body;
  }
  const response = await fetch(path, { method, headers, body: payload, credentials: 'same-origin' });
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const message = data && data.error ? data.error : `the server answered ${response.status}`;
    throw new ApiError(message, response.status);
  }
  return data;
}

let toastTimer = 0;
function showToast(message, isError = false) {
  const toast = byId('toast');
  toast.textContent = message;
  toast.classList.toggle('error', isError);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, TOAST_MS);
}

function reportError(error) {
  if (error instanceof ApiError && error.status === 401) {
    showToast('You are signed out. Reload the page to sign in again.', true);
    return;
  }
  showToast(error.message || String(error), true);
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', ICONS[name]);
  svg.append(path);
  return svg;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function iconButton(name, label, onClick) {
  const button = element('button', 'icon-btn');
  button.type = 'button';
  button.title = label;
  button.setAttribute('aria-label', label);
  button.append(icon(name));
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    onClick(event);
  });
  return button;
}

function parseDay(isoDay) {
  const [year, month, day] = isoDay.split('-').map(Number);
  return new Date(year, month - 1, day);
}

function daysFromToday(isoDay) {
  return Math.round((parseDay(isoDay) - parseDay(state.today)) / MS_PER_DAY);
}

function dayLabel(isoDay) {
  const offset = daysFromToday(isoDay);
  if (offset === 0) return 'Today';
  if (offset === 1) return 'Tomorrow';
  if (offset === -1) return 'Yesterday';
  const day = parseDay(isoDay);
  const sameYear = day.getFullYear() === parseDay(state.today).getFullYear();
  if (offset > 1 && offset < WEEK_DAYS) return day.toLocaleDateString('en-GB', { weekday: 'long' });
  return day.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', year: sameYear ? undefined : 'numeric' });
}

function dueText(task) {
  if (!task.dueDate) return '';
  return task.dueTime ? `${dayLabel(task.dueDate)} ${task.dueTime}` : dayLabel(task.dueDate);
}

function currentListName() {
  const found = state.lists.find((entry) => entry.id === state.listId);
  return found ? found.name : 'List';
}

function isListView() {
  return state.view === 'list';
}

async function runAction(action, message) {
  try {
    const result = await action();
    if (message) showToast(typeof message === 'function' ? message(result) : message);
    await refresh();
  } catch (error) {
    reportError(error);
  }
}

function completionMessage(result) {
  if (result.next) return `Done. Next one: ${dueText(result.next)}`;
  return 'Done';
}

function toggleDone(task) {
  if (task.completed) {
    return runAction(() => api('POST', `/api/tasks/${task.id}/uncomplete`), (result) => (result.binned ? 'Not done, and the next repeat went to the bin' : 'Not done'));
  }
  return runAction(() => api('POST', `/api/tasks/${task.id}/complete`), completionMessage);
}

function moveToBin(task) {
  return runAction(() => api('POST', `/api/tasks/${task.id}/trash`), 'Moved to the bin');
}

async function deleteForever(task) {
  if (!confirm('Delete this task for good? This cannot be undone.')) return;
  await runAction(() => api('DELETE', `/api/tasks/${task.id}`), 'Deleted');
}

function metaLine(task) {
  const meta = element('div', 'task-meta');
  if (task.dueDate) {
    const due = element('span', 'due');
    if (task.overdue) due.classList.add('overdue');
    else if (daysFromToday(task.dueDate) > 0) due.classList.add('later');
    due.append(icon('calendar'), document.createTextNode(dueText(task)));
    meta.append(due);
  }
  if (task.repeatText) {
    const repeat = element('span');
    repeat.append(icon('repeat'), document.createTextNode(task.repeatText));
    meta.append(repeat);
  }
  if (task.subtasks && task.subtasks.length && !isListView()) {
    const done = task.subtasks.filter((subtask) => subtask.completed).length;
    const count = element('span');
    count.append(icon('subtasks'), document.createTextNode(`${done}/${task.subtasks.length}`));
    meta.append(count);
  }
  if (task.parentTitle) meta.append(element('span', '', `in ${task.parentTitle}`));
  if (!isListView() || task.trashed) {
    const listName = element('span');
    listName.append(icon('list'), document.createTextNode(task.list));
    meta.append(listName);
  }
  return meta;
}

function taskTools(task) {
  const tools = element('div', 'task-tools');
  if (task.trashed) {
    tools.append(
      iconButton('restore', 'Restore', () => runAction(() => api('POST', `/api/tasks/${task.id}/restore`), 'Restored')),
      iconButton('forever', 'Delete for good', () => deleteForever(task)),
    );
    return tools;
  }
  tools.append(iconButton('edit', 'Edit', () => openEditor(task)), iconButton('trash', 'Move to bin', () => moveToBin(task)));
  return tools;
}

function renderTask(task, isNested = false) {
  const row = element('li', `task${task.completed ? ' done' : ''}${isNested ? ' subtask' : ''}`);
  row.dataset.id = String(task.id);
  row.dataset.priority = String(task.priority);
  row.tabIndex = -1;
  row.setAttribute('aria-selected', String(state.selectedId === task.id));
  const box = element('input', 'check');
  box.type = 'checkbox';
  box.checked = task.completed;
  box.disabled = task.trashed;
  box.setAttribute('aria-label', task.completed ? `Mark "${task.title}" not done` : `Mark "${task.title}" done`);
  box.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopPropagation();
    toggleDone(task);
  });
  const main = element('div', 'task-main');
  main.append(element('span', 'task-title', task.title));
  const firstNoteLine = task.notes.split('\n').find((line) => line.trim());
  if (firstNoteLine) main.append(element('span', 'task-notes', firstNoteLine));
  const meta = metaLine(task);
  if (meta.childElementCount) main.append(meta);
  row.append(box, main, taskTools(task));
  row.addEventListener('click', (event) => {
    if (event.target.closest('button, input, a')) return;
    selectTask(task.id);
    if (!task.trashed) openEditor(task);
  });
  return row;
}

function renderTaskList(tasks) {
  const list = element('ul', 'task-list');
  for (const task of tasks) {
    list.append(renderTask(task));
    if (isListView() && task.subtasks) for (const subtask of task.subtasks) list.append(renderTask(subtask, true));
  }
  return list;
}

function group(title, tasks, extraClass = '') {
  const section = element('section', 'group');
  if (title) section.append(element('h2', `section-title group-title ${extraClass}`.trim(), title));
  section.append(renderTaskList(tasks));
  return section;
}

function groupsForView() {
  if (state.view === 'today') {
    const overdue = state.tasks.filter((task) => task.dueDate < state.today);
    const today = state.tasks.filter((task) => task.dueDate >= state.today);
    return [overdue.length ? group('Overdue', overdue, 'overdue') : null, today.length ? group(overdue.length ? 'Today' : '', today) : null];
  }
  if (state.view === 'upcoming') {
    const byDay = new Map();
    for (const task of state.tasks) {
      if (!byDay.has(task.dueDate)) byDay.set(task.dueDate, []);
      byDay.get(task.dueDate).push(task);
    }
    return [...byDay.entries()].map(([day, tasks]) => group(dayLabel(day), tasks));
  }
  return state.tasks.length ? [group('', state.tasks)] : [];
}

function renderTasks() {
  const groups = groupsForView().filter(Boolean);
  byId('groups').replaceChildren(...groups);
  const empty = byId('empty-state');
  empty.hidden = state.tasks.length > 0;
  empty.textContent = EMPTY_TEXT[state.view];
  const more = byId('more-note');
  more.hidden = !state.more;
  more.textContent = `Showing the first ${state.tasks.length} tasks only. Search to find the others.`;
}

function renderHead() {
  const title = isListView() ? currentListName() : VIEW_NAMES[state.view];
  byId('view-title').textContent = title;
  document.title = `${title} - Tasks`;
  byId('view-date').textContent = state.view === 'today' && state.today ? parseDay(state.today).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' }) : '';
  const isOwnList = isListView() && state.listId !== INBOX_ID;
  byId('list-rename').hidden = !isOwnList;
  byId('list-delete').hidden = !isOwnList;
  byId('bin-empty').hidden = state.view !== 'bin' || state.tasks.length === 0;
  byId('bin-note').hidden = state.view !== 'bin';
  byId('quick-add').hidden = ['completed', 'bin'].includes(state.view);
  byId('filter-line').hidden = state.view !== 'search';
  byId('filter-text').textContent = `Search "${state.query}" in every list, done tasks included`;
}

function setCount(id, value, hideZero = true) {
  const node = byId(id);
  node.hidden = hideZero && !value;
  node.textContent = String(value || 0);
}

function renderSidebar() {
  for (const button of document.querySelectorAll('.view-btn')) {
    button.setAttribute('aria-current', String(button.dataset.view === state.view));
  }
  setCount('count-today', state.counts.today);
  setCount('count-upcoming', state.counts.upcoming);
  setCount('count-bin', state.counts.bin);
  const buttons = state.lists.map((entry) => {
    const button = element('button', 'list-btn');
    button.type = 'button';
    button.setAttribute('aria-current', String(isListView() && state.listId === entry.id));
    button.append(element('span', 'name', entry.name));
    if (entry.open) button.append(element('span', 'count', String(entry.open)));
    button.addEventListener('click', () => selectList(entry.id));
    return button;
  });
  byId('list-nav').replaceChildren(...buttons);
  const listSelect = byId('editor-list');
  const chosenList = listSelect.value;
  const options = state.lists.map((entry) => {
    const option = element('option', '', entry.name);
    option.value = String(entry.id);
    return option;
  });
  listSelect.replaceChildren(...options);
  if (state.lists.some((entry) => String(entry.id) === chosenList)) listSelect.value = chosenList;
}

function viewQuery() {
  const parameters = new URLSearchParams({ view: state.view });
  if (isListView()) parameters.set('list', String(state.listId));
  if (state.view === 'search') parameters.set('q', state.query);
  return parameters;
}

async function refresh() {
  const [listing, summary] = await Promise.all([api('GET', `/api/tasks?${viewQuery()}`), api('GET', '/api/lists')]);
  state.tasks = listing.tasks;
  state.more = Boolean(listing.more);
  state.today = listing.today;
  state.lists = summary.lists;
  state.counts = summary.counts;
  if (isListView() && !state.lists.some((entry) => entry.id === state.listId)) {
    state.view = 'today';
    return refresh();
  }
  renderSidebar();
  renderHead();
  renderTasks();
  if (state.editing) refreshEditorSubtasks();
  return null;
}

function safeRefresh() {
  refresh().catch(reportError);
}

function selectView(view) {
  state.view = view;
  state.selectedId = null;
  if (view !== 'search') {
    state.query = '';
    byId('search').value = '';
  }
  safeRefresh();
}

function selectList(listId) {
  state.view = 'list';
  state.listId = listId;
  state.selectedId = null;
  state.query = '';
  byId('search').value = '';
  safeRefresh();
}

function visibleTaskRows() {
  return [...document.querySelectorAll('#groups .task')];
}

function selectTask(taskId) {
  state.selectedId = taskId;
  for (const row of visibleTaskRows()) row.setAttribute('aria-selected', String(Number(row.dataset.id) === taskId));
  const selected = document.querySelector(`#groups .task[data-id="${taskId}"]`);
  if (selected) selected.focus({ preventScroll: false });
}

function moveSelection(step) {
  const rows = visibleTaskRows();
  if (!rows.length) return;
  const index = rows.findIndex((row) => Number(row.dataset.id) === state.selectedId);
  const next = index === -1 ? (step > 0 ? 0 : rows.length - 1) : Math.min(rows.length - 1, Math.max(0, index + step));
  selectTask(Number(rows[next].dataset.id));
}

function findTask(taskId) {
  for (const task of state.tasks) {
    if (task.id === taskId) return task;
    for (const subtask of task.subtasks || []) if (subtask.id === taskId) return subtask;
  }
  return null;
}

function selectedTask() {
  return state.selectedId === null ? null : findTask(state.selectedId);
}

let previewTimer = 0;
function previewChips(understood) {
  const parts = [];
  if (understood.dueDate) parts.push(understood.dueTime ? `${dayLabel(understood.dueDate)} ${understood.dueTime}` : dayLabel(understood.dueDate));
  if (understood.repeatText) parts.push(understood.repeatText);
  if (understood.priority) parts.push(`priority ${understood.priority}`);
  if (understood.list) parts.push(`#${understood.list}`);
  return parts;
}

function showPreview() {
  clearTimeout(previewTimer);
  const text = byId('quick-text').value;
  const preview = byId('quick-preview');
  if (!text.trim()) {
    preview.replaceChildren();
    return;
  }
  previewTimer = setTimeout(async () => {
    try {
      const understood = await api('GET', `/api/parse?${new URLSearchParams({ text })}`);
      const parts = previewChips(understood);
      preview.replaceChildren(document.createTextNode(`"${understood.title}"`));
      if (parts.length) preview.append(document.createTextNode(' '), element('span', 'hint', parts.join(', ')));
    } catch (error) {
      preview.replaceChildren(element('span', 'error', error.message));
    }
  }, PREVIEW_DELAY_MS);
}

async function quickAdd(event) {
  event.preventDefault();
  const input = byId('quick-text');
  const text = input.value.trim();
  if (!text) return;
  const body = { text };
  if (isListView()) body.listId = state.listId;
  try {
    const result = await api('POST', '/api/tasks/quick', body);
    input.value = '';
    byId('quick-preview').replaceChildren();
    const due = result.task.dueDate ? `, ${dueText(result.task)}` : '';
    showToast(`Added to ${result.task.list}${due}`);
    await refresh();
  } catch (error) {
    reportError(error);
  }
}

function buildDayToggles() {
  const toggles = DAY_SHORT.map((name, index) => {
    const toggle = element('button', 'day-toggle', name);
    toggle.type = 'button';
    toggle.dataset.day = String(index);
    toggle.setAttribute('aria-pressed', 'false');
    toggle.addEventListener('click', () => toggle.setAttribute('aria-pressed', String(toggle.getAttribute('aria-pressed') !== 'true')));
    return toggle;
  });
  byId('repeat-day-toggles').replaceChildren(...toggles);
}

function showRepeatFields() {
  const kind = byId('editor-repeat').value;
  byId('repeat-days').hidden = kind !== 'weekly';
  byId('repeat-month-day').hidden = kind !== 'monthly';
  byId('repeat-interval').hidden = kind !== 'interval';
}

function fillRepeat(repeat, dueDate) {
  byId('editor-repeat').value = repeat ? repeat.kind : '';
  const dueWeekday = dueDate ? (parseDay(dueDate).getDay() + 6) % WEEK_DAYS : 0;
  const days = repeat && repeat.kind === 'weekly' ? repeat.days : [dueWeekday];
  for (const toggle of byId('repeat-day-toggles').children) toggle.setAttribute('aria-pressed', String(days.includes(Number(toggle.dataset.day))));
  byId('editor-month-day').value = repeat && repeat.kind === 'monthly' ? repeat.day : (dueDate ? parseDay(dueDate).getDate() : 1);
  byId('editor-interval').value = repeat && repeat.kind === 'interval' ? repeat.days : 2;
  showRepeatFields();
}

function readRepeat() {
  const kind = byId('editor-repeat').value;
  if (!kind) return null;
  if (kind === 'weekly') {
    const days = [...byId('repeat-day-toggles').children].filter((toggle) => toggle.getAttribute('aria-pressed') === 'true').map((toggle) => Number(toggle.dataset.day));
    return { kind, days };
  }
  if (kind === 'monthly') return { kind, day: Number(byId('editor-month-day').value) };
  if (kind === 'interval') return { kind, days: Number(byId('editor-interval').value) };
  return { kind };
}

function subtaskRow(subtask) {
  const row = element('li', `subtask-row${subtask.completed ? ' done' : ''}`);
  const box = element('input', 'check');
  box.type = 'checkbox';
  box.checked = subtask.completed;
  box.setAttribute('aria-label', subtask.completed ? 'Mark not done' : 'Mark done');
  box.addEventListener('click', (event) => {
    event.preventDefault();
    toggleDone(subtask);
  });
  row.append(box, element('span', 'subtask-title', subtask.title));
  row.append(iconButton('trash', 'Move subtask to bin', () => moveToBin(subtask)));
  return row;
}

async function refreshEditorSubtasks() {
  const task = state.editing;
  if (!task || task.parentId) return;
  try {
    const fresh = await api('GET', `/api/tasks/${task.id}`);
    byId('editor-subtask-list').replaceChildren(...fresh.subtasks.map(subtaskRow));
  } catch (error) {
    reportError(error);
  }
}

function openEditor(task) {
  state.editing = task;
  byId('editor-title').value = task.title;
  byId('editor-notes').value = task.notes;
  byId('editor-date').value = task.dueDate || '';
  byId('editor-time').value = task.dueTime || '';
  byId('editor-priority').value = String(task.priority);
  byId('editor-list').value = String(task.listId);
  byId('editor-list').disabled = Boolean(task.parentId);
  byId('editor-repeat').disabled = Boolean(task.parentId);
  fillRepeat(task.repeat, task.dueDate);
  byId('editor-parent').hidden = !task.parentId;
  byId('editor-parent').textContent = task.parentId ? `Subtask of ${task.parentTitle}` : '';
  byId('editor-subtasks').hidden = Boolean(task.parentId);
  byId('editor-subtask-list').replaceChildren(...(task.subtasks || []).map(subtaskRow));
  byId('editor').showModal();
  byId('editor-title').focus();
}

async function saveEditor(event) {
  event.preventDefault();
  const task = state.editing;
  if (!task) return;
  const dueDate = byId('editor-date').value || null;
  const fields = {
    title: byId('editor-title').value,
    notes: byId('editor-notes').value,
    dueDate,
    dueTime: dueDate ? (byId('editor-time').value || null) : null,
    priority: Number(byId('editor-priority').value),
  };
  if (!task.parentId) Object.assign(fields, { listId: Number(byId('editor-list').value), repeat: dueDate ? readRepeat() : null });
  try {
    await api('PATCH', `/api/tasks/${task.id}`, fields);
    byId('editor').close();
    await refresh();
  } catch (error) {
    reportError(error);
  }
}

async function addSubtask(event) {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  const input = event.target;
  const text = input.value.trim();
  if (!text || !state.editing) return;
  try {
    await api('POST', '/api/tasks/quick', { text, parentId: state.editing.id });
    input.value = '';
    await refresh();
  } catch (error) {
    reportError(error);
  }
}

function openListDialog(mode) {
  state.listDialogMode = mode;
  byId('list-dialog-title').textContent = mode === 'new' ? 'New list' : 'Rename list';
  byId('list-name').value = mode === 'new' ? '' : currentListName();
  byId('list-dialog').showModal();
}

async function saveList(event) {
  event.preventDefault();
  const name = byId('list-name').value;
  try {
    if (state.listDialogMode === 'new') {
      const created = await api('POST', '/api/lists', { name });
      byId('list-dialog').close();
      selectList(created.id);
      return;
    }
    await api('PATCH', `/api/lists/${state.listId}`, { name });
    byId('list-dialog').close();
    await refresh();
  } catch (error) {
    reportError(error);
  }
}

async function deleteList() {
  if (!confirm(`Delete the list "${currentListName()}"? Its tasks go to the bin, and come back into the Inbox if you restore them.`)) return;
  try {
    const result = await api('DELETE', `/api/lists/${state.listId}`);
    showToast(`List deleted, ${result.binned} tasks moved to the bin`);
    selectView('today');
  } catch (error) {
    reportError(error);
  }
}

async function emptyBin() {
  if (!confirm('Delete every task in the bin for good?')) return;
  await runAction(() => api('POST', '/api/bin/empty'), (result) => `Deleted ${result.removed} tasks`);
}

async function readExport(file) {
  try {
    return JSON.parse(await file.text());
  } catch (error) {
    reportError(error instanceof SyntaxError ? new Error('That file is not a Tasks export') : error);
    return null;
  }
}

function partLabel(parts, index) {
  return parts.length > 1 ? `part ${index + 1} of ${parts.length}: ` : '';
}

async function checkImportParts(parts) {
  if (parts.length < 2) return true;
  for (const [index, part] of parts.entries()) {
    try {
      await api('POST', '/api/import?check=1', part);
    } catch (error) {
      reportError(new ApiError(`Nothing was imported. ${partLabel(parts, index)}${error.message}`, error.status));
      return false;
    }
  }
  return true;
}

async function importFile(file) {
  const payload = await readExport(file);
  if (payload === null) return;
  const parts = importParts(payload, state.importLimits);
  if (!(await checkImportParts(parts))) return;
  let imported = 0;
  for (const [index, part] of parts.entries()) {
    try {
      imported += (await api('POST', '/api/import', part)).imported;
    } catch (error) {
      const done = imported ? `Imported ${imported} tasks, then stopped at ` : '';
      reportError(new ApiError(`${done}${partLabel(parts, index)}${error.message}`, error.status));
      safeRefresh();
      return;
    }
  }
  showToast(`Imported ${imported} tasks`);
  safeRefresh();
}

async function importTodoist(file) {
  const listName = file.name.replace(/\.csv$/i, '').trim().slice(0, 50) || 'Todoist';
  try {
    const result = await api('POST', `/api/import/todoist?${new URLSearchParams({ list: listName })}`, await file.arrayBuffer(), 'text/csv');
    showToast(`Imported ${result.added} tasks into ${result.list}`);
    await refresh();
    const target = state.lists.find((entry) => entry.name.toLowerCase() === result.list.toLowerCase());
    if (target) selectList(target.id);
  } catch (error) {
    reportError(error);
  }
}

function isTyping(target) {
  return target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName));
}

function withSelected(action) {
  return () => {
    const task = selectedTask();
    if (task) action(task);
  };
}

const KEY_ACTIONS = {
  j: () => moveSelection(1),
  k: () => moveSelection(-1),
  ArrowDown: () => moveSelection(1),
  ArrowUp: () => moveSelection(-1),
  x: withSelected((task) => { if (!task.trashed) toggleDone(task); }),
  e: withSelected((task) => { if (!task.trashed) openEditor(task); }),
  Enter: withSelected((task) => { if (!task.trashed) openEditor(task); }),
  Delete: withSelected((task) => { if (!task.trashed) moveToBin(task); }),
  Backspace: withSelected((task) => { if (!task.trashed) moveToBin(task); }),
  q: () => { if (!byId('quick-add').hidden) byId('quick-text').focus(); },
  n: () => { if (!byId('quick-add').hidden) byId('quick-text').focus(); },
  t: () => selectView('today'),
  u: () => selectView('upcoming'),
  c: () => selectView('completed'),
  i: () => selectList(INBOX_ID),
  '/': () => byId('search').focus(),
  '?': () => byId('keys-dialog').showModal(),
  Escape: () => selectTask(null),
};

function handleKey(event) {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (document.querySelector('dialog[open]')) return;
  if (isTyping(event.target)) {
    if (event.key === 'Escape') event.target.blur();
    return;
  }
  const action = KEY_ACTIONS[event.key];
  if (!action) return;
  event.preventDefault();
  action();
}

let searchTimer = 0;
function onSearchInput(event) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = event.target.value.trim();
    if (state.query) {
      state.view = 'search';
      safeRefresh();
    } else if (state.view === 'search') {
      selectView('today');
    }
  }, SEARCH_DELAY_MS);
}

function wireEvents() {
  for (const button of document.querySelectorAll('.view-btn')) {
    button.addEventListener('click', () => selectView(button.dataset.view));
  }
  byId('search').addEventListener('input', onSearchInput);
  byId('quick-add').addEventListener('submit', quickAdd);
  byId('quick-text').addEventListener('input', showPreview);
  byId('filter-clear').addEventListener('click', () => selectView('today'));
  byId('bin-empty').addEventListener('click', emptyBin);
  byId('new-list').addEventListener('click', () => openListDialog('new'));
  byId('list-rename').addEventListener('click', () => openListDialog('rename'));
  byId('list-delete').addEventListener('click', deleteList);
  byId('list-form').addEventListener('submit', saveList);
  byId('keys-btn').addEventListener('click', () => byId('keys-dialog').showModal());
  byId('import-btn').addEventListener('click', () => byId('import-file').click());
  byId('todoist-btn').addEventListener('click', () => byId('todoist-file').click());
  for (const [inputId, handler] of [['import-file', importFile], ['todoist-file', importTodoist]]) {
    byId(inputId).addEventListener('change', (event) => {
      const [file] = event.target.files;
      event.target.value = '';
      if (file) handler(file);
    });
  }
  byId('editor-form').addEventListener('submit', saveEditor);
  byId('editor-cancel').addEventListener('click', () => byId('editor').close());
  byId('editor-trash').addEventListener('click', () => {
    const task = state.editing;
    byId('editor').close();
    if (task) moveToBin(task);
  });
  byId('editor').addEventListener('close', () => { state.editing = null; });
  byId('editor-repeat').addEventListener('change', showRepeatFields);
  byId('editor-new-subtask').addEventListener('keydown', addSubtask);
  for (const button of document.querySelectorAll('[data-close]')) {
    button.addEventListener('click', () => button.closest('dialog').close());
  }
  document.addEventListener('keydown', handleKey);
  setInterval(() => {
    if (!document.querySelector('dialog[open]') && document.visibilityState === 'visible') safeRefresh();
  }, REFRESH_MS);
}

async function start() {
  buildDayToggles();
  wireEvents();
  try {
    const session = await api('GET', '/api/session');
    state.csrf = session.csrfToken;
    state.today = session.today;
    state.importLimits = session.importLimits;
    await refresh();
  } catch (error) {
    reportError(error);
  }
}

start();
