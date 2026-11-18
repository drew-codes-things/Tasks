'use strict';

const IMPORT_ENVELOPE_BYTES = 4096;
const importTextEncoder = new TextEncoder();

function importByteLength(value) {
  return importTextEncoder.encode(JSON.stringify(value)).length + 1;
}

function isSplittableExport(payload, limits) {
  return Boolean(limits) && payload !== null && typeof payload === 'object' && Array.isArray(payload.tasks)
    && (payload.lists === undefined || Array.isArray(payload.lists));
}

function importParts(payload, limits) {
  if (!isSplittableExport(payload, limits)) return [payload];
  const budget = limits.bytes - IMPORT_ENVELOPE_BYTES;
  const parts = [];
  const named = new Set();
  let part = null;
  const startPart = () => {
    part = { names: new Map(), tasks: [], rows: 0, bytes: 0 };
    parts.push(part);
  };
  const isEmpty = () => part.tasks.length === 0 && part.names.size === 0;
  const fits = (rows, bytes, newNames) => part.rows + rows <= limits.tasks && part.bytes + bytes <= budget
    && part.names.size + newNames <= limits.lists;
  const addName = (name) => {
    if (part.names.has(name)) return;
    const bytes = importByteLength(name);
    part.names.set(name, bytes);
    part.bytes += bytes;
    named.add(name);
  };
  for (const task of payload.tasks) {
    const rows = 1 + (task && Array.isArray(task.subtasks) ? task.subtasks.length : 0);
    const name = task && typeof task.list === 'string' ? task.list : null;
    const nameBytes = name === null ? 0 : importByteLength(name);
    const taskBytes = importByteLength(task);
    if (part === null) startPart();
    const newNames = name !== null && !part.names.has(name) ? 1 : 0;
    if (!isEmpty() && !fits(rows, taskBytes + newNames * nameBytes, newNames)) startPart();
    if (name !== null) addName(name);
    part.tasks.push(task);
    part.rows += rows;
    part.bytes += taskBytes;
  }
  for (const name of payload.lists || []) {
    if (typeof name !== 'string' || named.has(name)) continue;
    if (part === null || (!isEmpty() && !fits(0, importByteLength(name), 1))) startPart();
    addName(name);
  }
  if (parts.length === 0) return [payload];
  return parts.map((entry) => ({ app: payload.app, version: payload.version, lists: [...entry.names.keys()], tasks: entry.tasks }));
}

if (typeof module === 'object') module.exports = { importParts };
