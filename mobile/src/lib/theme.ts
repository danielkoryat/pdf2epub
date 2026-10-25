import { Platform } from 'react-native';

export const colors = {
  bg: '#0F1211',
  surface: '#161A18',
  card: '#1B201D',
  raised: '#252C28',
  green: '#78EFA3',
  greenDim: '#1F3A2A',
  text: '#F2F5F1',
  muted: '#A3AEA7',
  faint: '#737F78',
  track: '#313A35',
  upcoming: '#D0D7D2',
};

export const serif = Platform.select({ ios: 'Georgia', default: 'serif' });

/** 1:02:03 or 4:05. */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s / 60) % 60);
  const sec = String(s % 60).padStart(2, '0');
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

/** A rough length: "16h 4m", "42m". */
export function span(seconds: number): string {
  const minutes = Math.max(seconds > 0 ? 1 : 0, Math.floor(seconds / 60));
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
}

export function speedLabel(speed: number): string {
  return `${Number(speed.toFixed(2))}×`;
}
