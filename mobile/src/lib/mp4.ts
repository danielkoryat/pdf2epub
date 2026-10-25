/**
 * Reads what an M4B audiobook carries besides its sound: title and author tags, length, Nero chapter
 * marks, the cover picture, and the timed read-along text pdf2epub's voice worker stores in a private
 * `uuid` box (see voice-worker/src/pdf2epub_voice/readalong.py). Only the boxes needed are read, so a
 * 400 MB book is not loaded into memory.
 */
import { File } from 'expo-file-system';

export type Chapter = { title: string; start: number }; // seconds
export type Cue = { start: number; end: number; text: string }; // seconds

export type Details = {
  title: string;
  author: string;
  duration: number; // seconds, 0 when unknown
  chapters: Chapter[];
  cover: { bytes: Uint8Array; png: boolean } | null;
  /** Where the read-along text is stored in the file, if it has any. */
  text: { offset: number; size: number } | null;
};

const READ_ALONG_ID = '914bbd195f3b4b64a32fa44bb9cf8e11';
const MAX_MOOV = 64 * 1024 * 1024;
const MAX_TEXT = 16 * 1024 * 1024;

const u32 = (b: Uint8Array, i: number) => b[i] * 0x1000000 + ((b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]);
const u64 = (b: Uint8Array, i: number) => u32(b, i) * 0x100000000 + u32(b, i + 4);
const tag = (b: Uint8Array, i: number) => String.fromCharCode(b[i], b[i + 1], b[i + 2], b[i + 3]);
const hex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');

export function utf8(bytes: Uint8Array): string {
  if (typeof TextDecoder !== 'undefined') return new TextDecoder('utf-8').decode(bytes);
  let out = '';
  for (let i = 0; i < bytes.length; ) {
    const c = bytes[i++];
    let code = c;
    if (c >= 0xf0) code = ((c & 7) << 18) | ((bytes[i++] & 63) << 12) | ((bytes[i++] & 63) << 6) | (bytes[i++] & 63);
    else if (c >= 0xe0) code = ((c & 15) << 12) | ((bytes[i++] & 63) << 6) | (bytes[i++] & 63);
    else if (c >= 0xc0) code = ((c & 31) << 6) | (bytes[i++] & 63);
    out += String.fromCodePoint(code);
  }
  return out;
}

type Box = { type: string; start: number; size: number; header: number };

/** The boxes inside `bytes` between `from` and `to`. */
function boxes(bytes: Uint8Array, from: number, to: number): Box[] {
  const found: Box[] = [];
  let at = from;
  while (at + 8 <= to) {
    let size = u32(bytes, at);
    let header = 8;
    if (size === 1) {
      size = u64(bytes, at + 8);
      header = 16;
    } else if (size === 0) size = to - at;
    if (size < header || at + size > to) break;
    found.push({ type: tag(bytes, at + 4), start: at, size, header });
    at += size;
  }
  return found;
}

const child = (bytes: Uint8Array, box: Box, type: string, skip = 0) =>
  boxes(bytes, box.start + box.header + skip, box.start + box.size).find((b) => b.type === type);

export function readDetails(file: File): Details {
  const details: Details = { title: '', author: '', duration: 0, chapters: [], cover: null, text: null };
  const handle = file.open();
  try {
    const length = handle.size ?? file.size;
    let at = 0;
    while (at + 8 <= length) {
      handle.offset = at;
      const head = handle.readBytesSync(16);
      let size = u32(head, 0);
      let header = 8;
      const type = tag(head, 4);
      if (size === 1) {
        size = u64(head, 8);
        header = 16;
      } else if (size === 0) size = length - at;
      if (size < header || at + size > length) break;
      if (type === 'moov' && size <= MAX_MOOV) {
        handle.offset = at;
        readMovie(handle.readBytesSync(size), details);
      } else if (type === 'uuid' && size >= header + 16) {
        handle.offset = at + header;
        if (hex(handle.readBytesSync(16)) === READ_ALONG_ID)
          details.text = { offset: at + header + 16, size: size - header - 16 };
      }
      at += size;
    }
  } finally {
    handle.close();
  }
  return details;
}

function readMovie(bytes: Uint8Array, details: Details) {
  const moov: Box = { type: 'moov', start: 0, size: bytes.length, header: 8 };
  const mvhd = child(bytes, moov, 'mvhd');
  if (mvhd) {
    const at = mvhd.start + mvhd.header;
    const scale = bytes[at] === 1 ? u32(bytes, at + 20) : u32(bytes, at + 12);
    const length = bytes[at] === 1 ? u64(bytes, at + 24) : u32(bytes, at + 16);
    if (scale > 0) details.duration = length / scale;
  }
  const udta = child(bytes, moov, 'udta');
  if (!udta) return;
  const chpl = child(bytes, udta, 'chpl');
  if (chpl) details.chapters = readChapters(bytes, chpl);
  const meta = child(bytes, udta, 'meta');
  if (!meta) return;
  // ISO meta boxes have 4 bytes of version and flags before their children; QuickTime ones do not.
  const full = tag(bytes, meta.start + meta.header + 8) === 'hdlr';
  const ilst = child(bytes, meta, 'ilst', full ? 4 : 0);
  if (!ilst) return;
  for (const item of boxes(bytes, ilst.start + ilst.header, ilst.start + ilst.size)) {
    const data = child(bytes, item, 'data');
    if (!data) continue;
    const kind = u32(bytes, data.start + data.header) & 0xffffff;
    const payload = bytes.subarray(data.start + data.header + 8, data.start + data.size);
    const value = () => utf8(payload).trim();
    if (item.type === '©nam' && !details.title) details.title = value();
    else if (item.type === '©alb' && !details.title) details.title = value();
    else if ((item.type === '©ART' || item.type === 'aART') && !details.author) details.author = value();
    else if (item.type === 'covr' && !details.cover && payload.length > 0)
      details.cover = { bytes: payload.slice(), png: kind === 14 };
  }
}

function readChapters(bytes: Uint8Array, chpl: Box): Chapter[] {
  const end = chpl.start + chpl.size;
  let at = chpl.start + chpl.header;
  const version = bytes[at];
  at += version === 1 ? 8 : 4;
  const count = bytes[at++];
  const chapters: Chapter[] = [];
  for (let i = 0; i < count && at + 9 <= end; i++) {
    const start = u64(bytes, at) / 1e7; // 100 ns units
    const length = bytes[at + 8];
    at += 9;
    if (at + length > end) break;
    chapters.push({ title: utf8(bytes.subarray(at, at + length)), start });
    at += length;
  }
  return chapters;
}

/** The book's read-along passages: a matching .vtt next to it, or the text inside the file. */
export function readCues(file: File, text: Details['text']): Cue[] {
  const sidecar = new File(file.uri + '.vtt');
  if (sidecar.exists) return parseVtt(sidecar.textSync());
  if (!text) return [];
  if (text.size > MAX_TEXT) throw new Error('The read-along text is too large');
  const handle = file.open();
  let json: string;
  try {
    handle.offset = text.offset;
    json = utf8(handle.readBytesSync(text.size));
  } finally {
    handle.close();
  }
  const doc = JSON.parse(json) as { version: number; cues: { start: number; end: number; text: string }[] };
  if (doc.version !== 1) throw new Error('Unsupported read-along version');
  return doc.cues.map((c) => ({ start: c.start / 1000, end: c.end / 1000, text: c.text }));
}

export function parseVtt(source: string): Cue[] {
  const timestamp = (value: string) => value.split(':').reduce((total, part) => total * 60 + Number(part), 0);
  const cues: Cue[] = [];
  const pattern = /^([0-9:.]+) --> ([0-9:.]+)[^\n]*\n([\s\S]*?)(?=\n\n|$(?![\s\S]))/gm;
  let previous = 0;
  for (const match of source.replace(/\r/g, '').matchAll(pattern)) {
    const start = timestamp(match[1]);
    const end = timestamp(match[2]);
    if (start < previous || end <= start) throw new Error('Overlapping or invalid cue times');
    cues.push({ start, end, text: match[3].replace(/<[^>]*>/g, '') });
    previous = end;
  }
  if (cues.length === 0) throw new Error('No timed captions found. Choose a WebVTT (.vtt) transcript.');
  return cues;
}
