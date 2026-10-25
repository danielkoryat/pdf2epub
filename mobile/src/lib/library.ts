/**
 * The books on this phone and what the app remembers about each: where the listener stopped, the
 * speed, and the details read from the file. Books live in the app's document folder, the folder the
 * earlier native Android app used, so its books stay when this app is installed over it.
 */
import AsyncStorage from '@react-native-async-storage/async-storage';
import { getDocumentAsync } from 'expo-document-picker';
import { Directory, File, FileMode, Paths } from 'expo-file-system';
import { useSyncExternalStore } from 'react';
import { Platform } from 'react-native';
import { type Chapter, type Details, parseVtt, readDetails } from './mp4';

export const AUDIO = /\.(m4b|m4a|mp3)$/i;

export type Book = {
  id: string; // the file name
  uri: string;
  title: string;
  author: string;
  duration: number; // seconds, 0 when not known yet
  chapters: Chapter[];
  cover: string | null; // a file uri
  text: Details['text'];
  added: number;
  fingerprint: string;
};

export type Progress = { position: number; speed: number; played: number; done: boolean };
export type Settings = { bookScope: boolean; readSize: number };
export type Import = { busy: boolean; name: string; copied: number; total: number; message: string; added: string | null };

type State = {
  ready: boolean;
  books: Book[];
  progress: Record<string, Progress>;
  last: string | null;
  settings: Settings;
  import: Import;
};

const KEY = 'book-listen';
let state: State = {
  ready: false,
  books: [],
  progress: {},
  last: null,
  settings: { bookScope: false, readSize: 19 },
  import: { busy: false, name: '', copied: 0, total: 0, message: '', added: null },
};
const listeners = new Set<() => void>();
let writing: Promise<void> = Promise.resolve();

export function getState() {
  return state;
}

export function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useLibrary<T>(select: (s: State) => T): T {
  return useSyncExternalStore(subscribe, () => select(state));
}

function update(patch: Partial<State>, persist = true) {
  state = { ...state, ...patch };
  listeners.forEach((l) => l());
  if (persist) save();
}

/**
 * Writes at once, one write after another. Not on a timer: Android pauses JavaScript timers while the
 * app is in the background, which is exactly when the place must still be saved.
 */
export function save() {
  const { books, progress, last, settings } = state;
  const data = JSON.stringify({ books, progress, last, settings });
  writing = writing.then(() => AsyncStorage.setItem(KEY, data)).catch(() => undefined);
  return writing;
}

export async function load() {
  const saved = await AsyncStorage.getItem(KEY);
  if (saved) {
    const data = JSON.parse(saved) as Pick<State, 'books' | 'progress' | 'last' | 'settings'>;
    state = { ...state, ...data, settings: { ...state.settings, ...data.settings } };
  } else migrateNativeApp();
  update({ ready: true });
  scan();
}

/** Version 0.4 (native Android) kept positions in SharedPreferences; carry them over once. */
function migrateNativeApp() {
  try {
    const prefs = new File(Paths.document.uri.replace(/files\/?$/, 'shared_prefs/books.xml'));
    if (!prefs.exists) return;
    const xml = prefs.textSync();
    const value = (kind: string, name: string) =>
      new RegExp(`<${kind} name="${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}" value="([^"]*)"`).exec(xml)?.[1];
    const progress: Record<string, Progress> = {};
    for (const match of xml.matchAll(/<long name="([^"]+\.(?:m4b|m4a|mp3))" value="(\d+)"/gi)) {
      const name = match[1];
      progress[name] = {
        position: Number(match[2]) / 1000,
        speed: Number(value('float', `${name}.speed`) ?? 1),
        played: Number(value('long', `${name}.played`) ?? 0),
        done: value('boolean', `${name}.done`) === 'true',
      };
    }
    const last = /<string name="last">([^<]*)<\/string>/.exec(xml)?.[1]?.split('/').pop() || null;
    state = { ...state, progress, last };
  } catch {
    // Nothing to carry over.
  }
}

const coverFolder = () => new Directory(Paths.cache, 'covers');

/** Finds the books in the document folder, reading the details of new ones. */
export function scan() {
  const files = Paths.document
    .list()
    .filter((f): f is File => f instanceof File && AUDIO.test(f.name));
  const known = new Map(state.books.map((b) => [b.id, b]));
  const books: Book[] = [];
  const fresh: File[] = [];
  for (const file of files) {
    const fingerprint = `${file.size}:${file.modificationTime ?? 0}`;
    const book = known.get(file.name);
    if (book && book.fingerprint === fingerprint) books.push(book);
    else {
      fresh.push(file);
      books.push(placeholder(file, fingerprint));
    }
  }
  update({ books });
  // Reading tags takes a moment per book: one at a time, so the list shows at once.
  const next = () => {
    const file = fresh.shift();
    if (!file) return;
    const book = describe(file);
    update({ books: state.books.map((b) => (b.id === book.id ? book : b)) });
    setTimeout(next, 0);
  };
  setTimeout(next, 0);
}

function placeholder(file: File, fingerprint: string): Book {
  return {
    id: file.name,
    uri: file.uri,
    title: file.name.replace(AUDIO, ''),
    author: '',
    duration: 0,
    chapters: [],
    cover: null,
    text: null,
    added: file.modificationTime ?? Date.now(),
    fingerprint: `new:${fingerprint}`,
  };
}

function describe(file: File): Book {
  const book = placeholder(file, `${file.size}:${file.modificationTime ?? 0}`);
  book.fingerprint = book.fingerprint.slice(4);
  if (!/\.mp3$/i.test(file.name)) {
    try {
      const details = readDetails(file);
      if (details.title) book.title = details.title;
      book.author = details.author;
      book.duration = details.duration;
      book.chapters = details.chapters;
      book.text = details.text;
      if (details.cover) {
        const folder = coverFolder();
        if (!folder.exists) folder.create({ intermediates: true });
        const cover = new File(folder, `${file.name}.${details.cover.png ? 'png' : 'jpg'}`);
        cover.writeSync(details.cover.bytes);
        book.cover = cover.uri;
      }
    } catch {
      // Unreadable tags: the file name stays the title.
    }
  }
  return book;
}

export function progressOf(id: string): Progress {
  return state.progress[id] ?? { position: 0, speed: 1, played: 0, done: false };
}

export function saveProgress(id: string, patch: Partial<Progress>) {
  if (!state.books.some((b) => b.id === id)) return; // deleted while it played
  update({ progress: { ...state.progress, [id]: { ...progressOf(id), ...patch } } });
}

export function setLast(id: string | null) {
  if (state.last !== id) update({ last: id });
}

export function setDuration(id: string, duration: number) {
  const book = state.books.find((b) => b.id === id);
  if (book && duration > 0 && Math.abs(book.duration - duration) > 1)
    update({ books: state.books.map((b) => (b.id === id ? { ...b, duration } : b)) });
}

export function setSettings(patch: Partial<Settings>) {
  update({ settings: { ...state.settings, ...patch } });
}

/** Books, the most recently played or added first. */
export function sorted(books: Book[], progress: Record<string, Progress>) {
  const recent = (b: Book) => Math.max(progress[b.id]?.played ?? 0, b.added);
  return [...books].sort((a, b) => recent(b) - recent(a));
}

export function deleteBook(id: string) {
  const book = state.books.find((b) => b.id === id);
  if (!book) return;
  for (const uri of [book.uri, book.uri + '.vtt', book.cover]) {
    if (!uri) continue;
    const file = new File(uri);
    if (file.exists) file.delete();
  }
  const { [id]: _, ...progress } = state.progress;
  update({
    books: state.books.filter((b) => b.id !== id),
    progress,
    last: state.last === id ? null : state.last,
    import: state.import.added === id ? { ...state.import, message: '', added: null } : state.import,
  });
}

// -- import ------------------------------------------------------------------------------------------

let cancelled = false;

function setImport(patch: Partial<Import>) {
  update({ import: { ...state.import, ...patch } }, false);
}

export function cancelImport() {
  cancelled = true;
}

export function dismissImport() {
  setImport({ message: '', added: null });
}

/** Lets the listener pick a book and copies it into the app, for offline listening. */
export async function importBook() {
  if (state.import.busy) return;
  // The document picker, not File.pickFileAsync: only it reports the file's real name on Android.
  // On iOS the app may read a picked file only through the picker's own copy.
  const picked = await getDocumentAsync({ type: '*/*', copyToCacheDirectory: Platform.OS === 'ios' });
  if (picked.canceled) return;
  const asset = picked.assets[0];
  const source = new File(asset.uri);
  const name = asset.name.replace(/[\\/:*?"<>|]/g, ' ');
  if (!AUDIO.test(name)) {
    setImport({ message: 'Choose an M4B, M4A or MP3 audiobook.', added: null });
    return;
  }
  let target = new File(Paths.document, name);
  if (target.exists) target = new File(Paths.document, `${Date.now()}-${name}`);
  const partial = new File(Paths.cache, `import-${Date.now()}.partial`);
  cancelled = false;
  setImport({ busy: true, name, copied: 0, total: asset.size ?? 0, message: '', added: null });
  try {
    await copy(source, partial);
    if (cancelled) throw new Error('Cancelled');
    partial.moveSync(target);
    scan();
    setImport({ busy: false, message: `Ready to listen · ${name}`, added: target.name });
  } catch (error) {
    if (partial.exists) partial.delete();
    setImport({
      busy: false,
      message: cancelled ? 'Import cancelled' : `Import failed · ${(error as Error).message}`,
      added: null,
    });
  }
}

/** Copies in pieces, so the progress shows and the import can be cancelled. */
async function copy(source: File, target: File) {
  let input;
  try {
    input = source.open(FileMode.ReadOnly);
  } catch {
    await source.copy(target); // a provider that cannot be read in pieces
    return;
  }
  target.create({ overwrite: true });
  const output = target.open(FileMode.WriteOnly);
  try {
    let copied = 0;
    for (;;) {
      if (cancelled) throw new Error('Cancelled');
      const chunk = await input.readBytes(1024 * 1024);
      if (chunk.length === 0) break;
      await output.writeBytes(chunk);
      copied += chunk.length;
      setImport({ copied });
    }
    if (copied === 0) throw new Error('The file is empty');
  } finally {
    input.close();
    output.close();
  }
}

/** Attaches a WebVTT transcript as the book's read-along text. */
export async function attachText(id: string) {
  const book = state.books.find((b) => b.id === id);
  if (!book) return;
  const picked = await File.pickFileAsync({ mimeTypes: ['*/*'] });
  if (picked.canceled) return;
  const text = await picked.result.text();
  parseVtt(text); // throws when it is not usable
  new File(book.uri + '.vtt').writeSync(text);
  update({ books: [...state.books] });
}
