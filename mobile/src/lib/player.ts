/**
 * One audio player for the whole app. It remembers the listener's place: every few seconds while
 * playing, on pause and seek, and when the app goes to the background; and it reopens the last book
 * where it stopped when the app starts.
 */
import { type AudioStatus, createAudioPlayer, setAudioModeAsync } from 'expo-audio';
import { AppState } from 'react-native';
import { useSyncExternalStore } from 'react';
import { type Book, getState, progressOf, saveProgress, setDuration, setLast, subscribe, whenRemoving } from './library';

export const SPEEDS = [0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2]; // Android plays at most 2x
export const BACK = 15;
export const FORWARD = 30;

type Playback = {
  book: Book | null;
  position: number; // seconds
  duration: number;
  playing: boolean;
  speed: number;
  /** Sleep timer: when to pause (ms since epoch), or a position to pause at (end of chapter). */
  sleepAt: number | null;
  sleepAtPosition: number | null;
  /** Seconds until the sleep timer pauses, for showing it. */
  sleepLeft: number | null;
};

const player = createAudioPlayer(null, { updateInterval: 250 });
let playback: Playback = {
  book: null,
  position: 0,
  duration: 0,
  playing: false,
  speed: 1,
  sleepAt: null,
  sleepAtPosition: null,
  sleepLeft: null,
};
const listeners = new Set<() => void>();
let pending: { position: number; play: boolean } | null = null;
let lockScreen = false;
let savedAt = 0;
let savedPosition = -1;

function set(patch: Partial<Playback>) {
  playback = { ...playback, ...patch };
  listeners.forEach((l) => l());
}

export function usePlayback<T>(select: (p: Playback) => T): T {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => select(playback),
  );
}

export function getPlayback() {
  return playback;
}

function remember(force = false) {
  const { book, position, speed } = playback;
  if (!book || !player.isLoaded) return;
  const now = Date.now();
  const moved = Math.abs(position - savedPosition) > 0.5;
  if (force || (playback.playing ? now - savedAt > 5000 : moved)) {
    saveProgress(book.id, { position, speed });
    savedAt = now;
    savedPosition = position;
  }
}

function onStatus(status: AudioStatus) {
  const book = playback.book;
  if (!book) return;
  if (status.isLoaded && pending) {
    const { position, play } = pending;
    pending = null;
    player.seekTo(position).then(() => {
      if (play) start();
    });
    player.setPlaybackRate(progressOf(book.id).speed);
  }
  if (status.isLoaded && status.duration > 0) setDuration(book.id, status.duration);
  const position = pending ? pending.position : status.currentTime;
  if (status.playing && !playback.playing) saveProgress(book.id, { played: Date.now(), done: false });
  set({
    position,
    duration: status.duration > 0 ? status.duration : book.duration,
    playing: status.playing,
    speed: status.playbackRate || playback.speed,
  });
  if (status.didJustFinish) {
    saveProgress(book.id, { position: 0, done: true });
    savedPosition = 0;
    return;
  }
  checkSleep(position, status.playing);
  remember();
}

function checkSleep(position: number, playing: boolean) {
  const { sleepAt, sleepAtPosition } = playback;
  if (sleepAt === null && sleepAtPosition === null) return;
  const left = sleepAt === null ? Infinity : sleepAt - Date.now();
  if (left <= 0 || (sleepAtPosition !== null && position >= sleepAtPosition)) {
    set({ sleepAt: null, sleepAtPosition: null, sleepLeft: null });
    if (playing) player.pause();
    player.volume = 1;
  } else {
    player.volume = left < 10000 ? left / 10000 : 1; // fades out over the last seconds
    if (sleepAt !== null) set({ sleepLeft: Math.ceil(left / 1000) });
  }
}

export async function startPlayer() {
  await setAudioModeAsync({ playsInSilentMode: true, shouldPlayInBackground: true, interruptionMode: 'doNotMix' });
  player.addListener('playbackStatusUpdate', onStatus);
  AppState.addEventListener('change', (next) => {
    if (next !== 'active') remember(true);
  });
  // Details read from the file after the book was opened (title, cover, chapters) reach the player.
  subscribe(() => {
    const book = playback.book && getState().books.find((b) => b.id === playback.book?.id);
    if (book && book !== playback.book) set({ book });
  });
  // A book about to be deleted or replaced by a newer copy: save the place in it and let go of its file;
  // the newer copy opens where it was, playing if it was.
  whenRemoving((id, replacement) => {
    if (playback.book?.id !== id) return;
    const playing = playback.playing;
    remember(true);
    close(id);
    if (replacement) queueMicrotask(() => open(replacement, { play: playing }));
  });
  const { last, books } = getState();
  const book = books.find((b) => b.id === last);
  if (book) open(book, { play: false });
}

/** Loads a book at its saved place (or the start), and plays it unless `play` is false. */
export function open(book: Book, options: { fromStart?: boolean; play?: boolean } = {}) {
  const play = options.play ?? true;
  if (playback.book?.id === book.id && player.isLoaded) {
    if (options.fromStart) seekTo(0);
    if (play && !playback.playing) start();
    return;
  }
  remember(true);
  const saved = progressOf(book.id);
  const position = options.fromStart || saved.done ? 0 : saved.position;
  pending = { position, play };
  savedPosition = position;
  set({ book, position, duration: book.duration, playing: false, speed: saved.speed });
  setLast(book.id);
  player.replace({ uri: book.uri });
  lockScreen = false;
}

function start() {
  const book = playback.book;
  if (!book) return;
  if (!lockScreen) {
    // Also what keeps Android playing in the background for longer than a few minutes.
    player.setActiveForLockScreen(
      true,
      { title: book.title, artist: book.author || 'Audiobook', albumTitle: book.title, artworkUrl: book.cover ?? undefined },
      { showSeekBackward: true, showSeekForward: true, seekBackwardIntervalSeconds: BACK, seekForwardIntervalSeconds: FORWARD },
    );
    lockScreen = true;
  }
  if (playback.duration > 0 && playback.position >= playback.duration - 1) player.seekTo(0);
  player.play();
}

export function toggle() {
  if (!playback.book) return;
  if (playback.playing) {
    player.pause();
    remember(true);
  } else if (pending) pending.play = true;
  else start();
}

export function seekTo(seconds: number) {
  if (!playback.book) return;
  const end = playback.duration > 0 ? playback.duration - 1 : seconds;
  const target = Math.max(0, Math.min(seconds, end));
  set({ position: target });
  if (pending) pending.position = target;
  else player.seekTo(target).then(() => remember(true));
}

export function skip(seconds: number) {
  seekTo(playback.position + seconds);
}

export function setSpeed(speed: number) {
  player.setPlaybackRate(speed);
  set({ speed });
  if (playback.book) saveProgress(playback.book.id, { speed });
}

export function setSleep(minutes: number | null, atPosition: number | null = null) {
  set({
    sleepAt: minutes ? Date.now() + minutes * 60000 : null,
    sleepAtPosition: atPosition,
    sleepLeft: minutes ? minutes * 60 : null,
  });
  if (!minutes && atPosition === null) player.volume = 1;
}

/** Stops and forgets the open book, before it is deleted. */
export function close(id: string) {
  if (playback.book?.id !== id) return;
  player.pause();
  player.clearLockScreenControls();
  player.replace(null);
  pending = null;
  lockScreen = false;
  set({ book: null, position: 0, duration: 0, playing: false });
}

/** The chapter playing at `seconds`, or -1 before the first chapter mark. */
export function chapterAt(book: Book | null, seconds: number) {
  let index = -1;
  book?.chapters.forEach((c, i) => {
    if (c.start <= seconds) index = i;
  });
  return index;
}
